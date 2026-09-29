#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
急騰予兆（ムーンショット）通知 複利シミュレーションツール
=====================================================
BTCUSDT以外の29銘柄（MOONSHOT_SYMBOLS）について、ライブ運用の急騰予兆通知条件
（デフォルト: 出来高急増 かつ ゴールデンクロス が新たに成立した瞬間）を過去データで
再現し、「通知が来るたびに、その時点の残高の一定割合を投資して、利確ライン
（デフォルト+30%、環境変数で+10%等に変更可）まで到達したら利確する」という
複利シミュレーションを行います。notify_simulation.py（BTC用）と同じ考え方・
同じ関数（fetch_klines_forward, compound_simulate）を再利用しています。

ライブ運用（crypto_signal_screener.py）とは独立していて、手動実行
（workflow_dispatch）でのみ動きます。

★ 手法
- 4時間足を使用（moonshot_analysis.py の検証と条件を揃えるため）。
- 「通知」は、"volume_spike かつ golden_cross"（デフォルト設定）が新たに
  成立した瞬間（前の足では成立していなかった）だけをカウントするエッジ検出方式。
  ライブ運用の急騰予兆通知ロジックと同じ考え方です。
- エントリーは、シグナルが確定した足の次の足の始値（未来の情報は使わない）。
- 利確ラインは MOONSHOT_SIM_TAKE_PROFIT_PCT（デフォルトはライブ運用と同じ+30%。
  「3日以内+10%」版を検証したい場合は環境変数で10.0に変更してください）。
  以後の高値（high）を順に見て、最初に到達した時点で利確したとみなします。
- 利確ラインに到達しないまま MOONSHOT_SIM_MAX_HOLD_DAYS（デフォルト3日）が
  経過したら、その時点の終値で強制決済したものとして扱います。
- 全29銘柄のトレードを時系列順にプール（1日あたりの件数フィルタは行わない。
  ライブ運用の急騰予兆通知自体が日次フィルタを行っていないため）し、毎回
  「その時点の残高の MOONSHOT_SIM_ENTRY_FRACTION 割合」を投資する複利計算。
- 売買手数料はBinanceの一般的なテイカー手数料を参考に、往復0.2%相当を
  控除しています。

★ 重要な注意
- これは過去データの機械的な再現であり、将来の成績を保証するものではありません。
- 資金配分・複利計算・同時保有制限など、実際の取引とは異なる単純化を含みます。
  特に、同じタイミングで複数銘柄の通知が重なった場合に必要な同時資金までは
  考慮していません。
- 投資判断・資金管理はご自身の責任で行ってください。
"""

import os
import sys
import time
from datetime import datetime, timezone

try:
    import requests
    import pandas as pd
    import numpy as np
except ImportError:
    print("必要なライブラリがありません: pip install -r requirements.txt")
    sys.exit(1)

# crypto_signal_screener.py のシグナル計算ロジック、notify_simulation.py の
# データ取得・複利シミュレーション関数をそのまま再利用する
import crypto_signal_screener as core
import notify_simulation as nsim

# ============================================================
# 設定
# ============================================================

SYMBOLS = core.MOONSHOT_SYMBOLS  # BTCUSDTを除く29銘柄
SIM_INTERVAL = "4h"  # moonshot_analysis.py の検証と条件を揃える

SIM_START_DATE = os.environ.get("MOONSHOT_SIM_START_DATE", "2018-01-01")
PAGE_LIMIT = 1000
PAGE_SLEEP_SEC = 0.15

REQUIRE_VOLUME_SPIKE = os.environ.get("MOONSHOT_SIM_REQUIRE_VOLUME_SPIKE", "1") != "0"
REQUIRE_GOLDEN_CROSS = os.environ.get("MOONSHOT_SIM_REQUIRE_GOLDEN_CROSS", "1") != "0"

TAKE_PROFIT_PCT = float(os.environ.get("MOONSHOT_SIM_TAKE_PROFIT_PCT", str(core.MOONSHOT_TAKE_PROFIT_PCT)))
MAX_HOLD_DAYS = float(os.environ.get("MOONSHOT_SIM_MAX_HOLD_DAYS", str(core.MOONSHOT_MAX_HOLD_DAYS)))
MAX_HOLD_BARS = int(round(MAX_HOLD_DAYS * 24 / 4))  # 4時間足なので1日=6本
FEE_ROUNDTRIP_PCT = float(os.environ.get("MOONSHOT_SIM_FEE_ROUNDTRIP_PCT", "0.2"))
INITIAL_CAPITAL_JPY = float(os.environ.get("MOONSHOT_SIM_INITIAL_CAPITAL_JPY", "1000000"))
ENTRY_FRACTION = float(os.environ.get("MOONSHOT_SIM_ENTRY_FRACTION", "0.10"))

_OUTPUT_PREFIX = os.environ.get("MOONSHOT_SIM_OUTPUT_PREFIX", "moonshot_simulation")
TRADES_CSV = f"{_OUTPUT_PREFIX}_trades.csv"
REPORT_MD = f"{_OUTPUT_PREFIX}_report.md"

BASE_URL = core.BASE_URL


# ============================================================
# エントリー検出 & 利確シミュレーション
# ============================================================

def simulate_symbol(symbol: str, df: pd.DataFrame) -> list:
    """(symbol, entry_time, entry_price, exit_time, exit_price, return_pct, resolved, bars_held) のリストを返す"""
    sig = core.compute_signal_series(df)
    n = len(df)
    if n < 10:
        return []

    vs = sig["volume_spike"].to_numpy()
    gc = sig["golden_cross"].to_numpy()
    entry_signal = np.ones(n, dtype=bool)
    if REQUIRE_VOLUME_SPIKE:
        entry_signal &= vs
    if REQUIRE_GOLDEN_CROSS:
        entry_signal &= gc
    volume_ratio = sig["volume_ratio"].to_numpy()

    open_time = df["open_time"].to_numpy()
    open_ = df["open"].to_numpy()
    high = df["high"].to_numpy()
    close = df["close"].to_numpy()

    trades = []
    for i in range(1, n - 1):
        if entry_signal[i] and not entry_signal[i - 1]:
            sig_vol_ratio = float(volume_ratio[i]) if pd.notna(volume_ratio[i]) else 0.0
            entry_idx = i + 1  # 次の足の始値でエントリー（未来を見ない）
            if entry_idx >= n:
                continue
            entry_time = pd.Timestamp(open_time[entry_idx])
            entry_price = float(open_[entry_idx])
            if entry_price <= 0:
                continue
            target_price = entry_price * (1 + TAKE_PROFIT_PCT / 100)

            window_end = min(entry_idx + MAX_HOLD_BARS, n)
            sub_high = high[entry_idx:window_end]
            hits = np.where(sub_high >= target_price)[0]

            if hits.size > 0:
                exit_idx = entry_idx + int(hits[0])
                exit_price = target_price
                resolved = True
            else:
                exit_idx = window_end - 1
                exit_price = float(close[exit_idx])
                resolved = False

            exit_time = pd.Timestamp(open_time[exit_idx])
            bars_held = exit_idx - entry_idx
            raw_return_pct = (exit_price - entry_price) / entry_price * 100
            net_return_pct = raw_return_pct - FEE_ROUNDTRIP_PCT

            trades.append({
                "symbol": symbol,
                "entry_time": entry_time,
                "entry_price": entry_price,
                "exit_time": exit_time,
                "exit_price": exit_price,
                "raw_return_pct": round(raw_return_pct, 3),
                "net_return_pct": round(net_return_pct, 3),
                "resolved": resolved,
                "bars_held": bars_held,
                "days_held": round(bars_held * 4 / 24, 2),
                "signal_volume_ratio": round(sig_vol_ratio, 3),
            })
    return trades


def main():
    start_dt = datetime.strptime(SIM_START_DATE, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    start_ms = int(start_dt.timestamp() * 1000)

    condition_label = " かつ ".join(
        ([] if not REQUIRE_VOLUME_SPIKE else ["出来高急増"])
        + ([] if not REQUIRE_GOLDEN_CROSS else ["ゴールデンクロス"])
    ) or "（条件なし）"

    print(f"実行時刻(UTC): {datetime.now(timezone.utc).isoformat()}")
    print(f"対象銘柄: {len(SYMBOLS)}（BTCUSDT除く） / 足種: {SIM_INTERVAL} / 取得開始: {SIM_START_DATE}")
    print(f"通知条件: {condition_label}")
    print(f"利確ライン: +{TAKE_PROFIT_PCT}% / 最大保有期間: {MAX_HOLD_DAYS}日 / 往復手数料: {FEE_ROUNDTRIP_PCT}%")
    print(f"1回のエントリー: 残高の{ENTRY_FRACTION*100:.0f}% / 元本: {INITIAL_CAPITAL_JPY:,.0f}円")
    print("-" * 70)

    all_trades = []
    for i, symbol in enumerate(SYMBOLS, 1):
        try:
            df = nsim.fetch_klines_forward(symbol, SIM_INTERVAL, start_ms, PAGE_LIMIT)
            if len(df) < 300:
                print(f"[{i}/{len(SYMBOLS)}] {symbol}: データ不足のためスキップ ({len(df)}本)")
                continue
            trades = simulate_symbol(symbol, df)
            all_trades.extend(trades)
            print(f"[{i}/{len(SYMBOLS)}] {symbol:10s} {len(df):>6}本  通知(全期間){len(trades):>4}回")
        except Exception as e:
            print(f"[{i}/{len(SYMBOLS)}] {symbol}: エラー ({e})")
        time.sleep(0.1)

    if not all_trades:
        print("通知イベントが1件も取得できませんでした。処理を終了します。")
        return

    trades_df = pd.DataFrame(all_trades).sort_values("entry_time").reset_index(drop=True)
    trades_df.to_csv(TRADES_CSV, index=False, encoding="utf-8-sig")

    n_trades = len(trades_df)
    n_resolved = int(trades_df["resolved"].sum())
    n_unresolved = n_trades - n_resolved
    avg_days_resolved = trades_df.loc[trades_df["resolved"], "days_held"].mean() if n_resolved else float("nan")
    avg_net_return = trades_df["net_return_pct"].mean()
    win_rate = (trades_df["net_return_pct"] > 0).mean() * 100
    reach_rate = n_resolved / n_trades * 100

    span_years = (trades_df["entry_time"].max() - trades_df["entry_time"].min()).days / 365.25
    avg_per_year = n_trades / span_years if span_years > 0 else float("nan")

    sim = nsim.compound_simulate(trades_df, INITIAL_CAPITAL_JPY, ENTRY_FRACTION)
    final_balance = sim["final_balance"]
    total_pnl = final_balance - INITIAL_CAPITAL_JPY
    max_dd = sim["max_drawdown_pct"]

    # --- 年別内訳（参考） ---
    trades_df["year"] = trades_df["entry_time"].dt.year
    year_rows = []
    for yr in sorted(trades_df["year"].unique()):
        sub = trades_df[trades_df["year"] == yr]
        year_rows.append({
            "year": int(yr),
            "n_trades": len(sub),
            "reach_rate_pct": round((sub["resolved"]).mean() * 100, 1),
            "avg_net_return_pct": round(sub["net_return_pct"].mean(), 2),
        })

    print("\n" + "=" * 70)
    print(f"■ 全期間（{trades_df['entry_time'].min().date()} 〜 {trades_df['entry_time'].max().date()}、約{span_years:.1f}年）")
    print("=" * 70)
    print(f"通知回数: {n_trades}回（年平均 約{avg_per_year:.1f}回 / システム全体29銘柄合算）")
    print(f"うち+{TAKE_PROFIT_PCT}%に到達して利確: {n_resolved}回（到達率{reach_rate:.1f}%） / "
          f"期限切れで強制決済: {n_unresolved}回")
    if n_resolved:
        print(f"利確までの平均日数（到達分のみ）: 約{avg_days_resolved:.1f}日")
    print(f"1トレードあたりの平均リターン（手数料控除後）: {avg_net_return:+.2f}%")
    print(f"プラスで終わったトレードの割合: {win_rate:.1f}%")
    print(f"複利シミュレーション: {INITIAL_CAPITAL_JPY:,.0f}円 → {final_balance:,.0f}円 "
          f"({total_pnl:+,.0f}円) / 最大ドローダウン -{max_dd:.1f}%")

    # --- Markdownレポート ---
    lines = []
    lines.append("# 急騰予兆（ムーンショット）複利シミュレーションレポート\n")
    lines.append(f"- 実行日時(UTC): {datetime.now(timezone.utc).isoformat()}")
    lines.append(f"- 通知条件: {condition_label} が新たに成立した瞬間（エッジ検出）")
    lines.append(f"- 対象銘柄数: {len(SYMBOLS)}（BTCUSDTを除く） / 使用した足: {SIM_INTERVAL}")
    lines.append(f"- データ取得開始: {SIM_START_DATE}")
    lines.append(f"- 利確ライン: +{TAKE_PROFIT_PCT}%（到達しない場合は{MAX_HOLD_DAYS}日で強制決済）")
    lines.append(f"- 往復手数料の概算控除: {FEE_ROUNDTRIP_PCT}%")
    lines.append(f"- **資金配分**: 1回のエントリーごとに、その時点の残高の **{ENTRY_FRACTION*100:.0f}%** を投資（複利）")
    lines.append(f"- シミュレーション元本: {INITIAL_CAPITAL_JPY:,.0f}円")
    lines.append("- 1日あたりの件数フィルタは行っていません（29銘柄の通知を全てプールして時系列順に実行）\n")

    lines.append(f"## 全期間の結果（{trades_df['entry_time'].min().date()} 〜 "
                 f"{trades_df['entry_time'].max().date()}、約{span_years:.1f}年）\n")
    lines.append(f"- 通知回数: **{n_trades}回**（年平均 約{avg_per_year:.1f}回、29銘柄合算）")
    lines.append(f"- うち+{TAKE_PROFIT_PCT}%に到達して利確: {n_resolved}回（到達率 **{reach_rate:.1f}%**） / "
                 f"期限切れで強制決済: {n_unresolved}回")
    if n_resolved:
        lines.append(f"- 利確までの平均日数（到達分のみ）: 約{avg_days_resolved:.1f}日")
    lines.append(f"- 1トレードあたりの平均リターン（手数料控除後）: {avg_net_return:+.2f}%")
    lines.append(f"- プラスで終わったトレードの割合: {win_rate:.1f}%")
    lines.append(f"- **複利シミュレーション結果（毎回残高の{ENTRY_FRACTION*100:.0f}%を投資、"
                 f"{n_trades}回を時系列順に実行）:**")
    lines.append(f"  - {INITIAL_CAPITAL_JPY:,.0f}円 → **約{final_balance:,.0f}円**（損益 {total_pnl:+,.0f}円）")
    lines.append(f"  - **最大ドローダウン（残高のピークからの最大下落率）: -{max_dd:.1f}%**\n")

    lines.append("## 年別の内訳（参考。年ごとの複利は計算していません）\n")
    lines.append("| 年 | 通知回数 | 到達率 | 平均リターン |")
    lines.append("|---|---|---|---|")
    for r in year_rows:
        lines.append(f"| {r['year']} | {r['n_trades']} | {r['reach_rate_pct']}% | {r['avg_net_return_pct']:+.2f}% |")

    lines.append("\n## 注意事項\n")
    lines.append("- これは過去データを機械的に再現したシミュレーションであり、将来の成績を保証するものではありません。")
    lines.append("- 1日あたりの件数フィルタを行っていないため、同じ日に複数銘柄で通知が重なった場合に"
                 "必要な同時資金までは考慮していません。実運用ではまとまった資金が同時に必要になる場面が"
                 "起こり得ます。")
    lines.append(f"- 資金は複利（利益を次のトレードの元手に組み入れる）で計算していますが、"
                 f"1回のエントリーで残高の{ENTRY_FRACTION*100:.0f}%を投じるため、"
                 f"連敗が続くとドローダウンが大きくなるリスクがあります。上記の最大ドローダウンを必ずご確認ください。")
    lines.append(f"- 利確ラインに{MAX_HOLD_DAYS}日以内に到達しない場合、その時点の終値で強制決済したとみなしています。")
    lines.append("- スリッページ（指値が想定通りに約定しない可能性）は考慮していません。")
    lines.append(f"- 手数料は往復{FEE_ROUNDTRIP_PCT}%の概算です。実際の手数料率はご自身の契約プランをご確認ください。")
    lines.append("- 本レポートは投資助言ではありません。投資判断はご自身の責任で行ってください。")

    with open(REPORT_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"\n{TRADES_CSV} / {REPORT_MD} を保存しました。")


if __name__ == "__main__":
    main()
