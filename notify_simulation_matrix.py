#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
シナリオ比較シミュレーション（対象銘柄数 × 投資割合）
=====================================================
「出来高急増 かつ RSI反発」が新たに成立した瞬間を通知タイミングとして再現し、

  - 対象銘柄の広さ（BTCのみ / 主要5銘柄 / 全30銘柄）
  - 1回のエントリーで投資する割合（10% / 15% / 20%）

を掛け合わせた複数シナリオについて、年ごとの通知回数・勝率・複利シミュレーション後の
資産・最大ドローダウンを一覧比較するツールです。notify_simulation.py の
データ取得・エントリー検出ロジックをそのまま再利用します。

★ 手法（notify_simulation.py と共通）
- crypto_signal_screener.py と同じ compute_signal_series を使用。
- 1時間足（ライブ運用と同じ）。
- 「出来高急増 かつ RSI反発」が新たに成立した瞬間だけをカウント（エッジ検出）。
- エントリーは次の足の始値、利確は+5%到達時、未到達なら60日で強制決済。
- 複利シミュレーション: 毎回「その時点の残高のfraction割合」を賭ける。

★ このスクリプト独自の部分
- 全30銘柄のデータを一度だけ取得・シグナル計算し、そこから
  「BTCのみ」「主要5銘柄」「全30銘柄」という3つの銘柄範囲を切り出して使う
  （銘柄範囲ごとに再取得すると無駄が多いため）。
- 銘柄範囲×投資割合の全組み合わせについて、年ごとの結果を1つの比較表にまとめる。
- 日次の確度フィルタ（1日◯件まで）は使わず、条件に合致したトレードは全て
  プールしてシミュレーションする（「銘柄を広げると頻度がどれだけ増えるか」を
  素直に見るため）。

★ 重要な注意
- これは過去データの機械的な再現であり、将来の成績を保証するものではありません。
- 投資判断・資金管理はご自身の責任で行ってください。
"""

import os
import sys
import time
from datetime import datetime, timezone

try:
    import pandas as pd
except ImportError:
    print("必要なライブラリがありません: pip install -r requirements.txt")
    sys.exit(1)

import crypto_signal_screener as core
import notify_simulation as nsim

# ============================================================
# 設定
# ============================================================

SIM_START_DATE = os.environ.get("SIM_START_DATE", nsim.SIM_START_DATE)
YEARS_TO_REPORT = [2023, 2024]
INITIAL_CAPITAL_JPY = nsim.INITIAL_CAPITAL_JPY

SYMBOL_TIERS = {
    "BTCのみ（1銘柄）": ["BTCUSDT"],
    "主要5銘柄": ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT"],
    "全30銘柄": core.SYMBOLS,
}

FRACTIONS = [0.10, 0.15, 0.20]

# 比較に必要な銘柄の和集合（全30銘柄なら結局これで全部）
ALL_NEEDED_SYMBOLS = sorted(set(sym for syms in SYMBOL_TIERS.values() for sym in syms))

TRADES_CSV = "notify_simulation_matrix_trades.csv"
MATRIX_CSV = "notify_simulation_matrix.csv"
REPORT_MD = "notify_simulation_matrix_report.md"


# ============================================================
# データ取得＆トレード検出（全銘柄ぶんを一度だけ）
# ============================================================

def collect_all_trades() -> pd.DataFrame:
    start_dt = datetime.strptime(SIM_START_DATE, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    start_ms = int(start_dt.timestamp() * 1000)

    print(f"対象銘柄（和集合）: {len(ALL_NEEDED_SYMBOLS)} / 足種: {nsim.SIM_INTERVAL} / 取得開始: {SIM_START_DATE}")
    print("-" * 70)

    all_trades = []
    for i, symbol in enumerate(ALL_NEEDED_SYMBOLS, 1):
        try:
            df = nsim.fetch_klines_forward(symbol, nsim.SIM_INTERVAL, start_ms, nsim.PAGE_LIMIT)
            if len(df) < 300:
                print(f"[{i}/{len(ALL_NEEDED_SYMBOLS)}] {symbol}: データ不足のためスキップ ({len(df)}本)")
                continue
            trades = nsim.simulate_symbol(symbol, df)
            all_trades.extend(trades)
            print(f"[{i}/{len(ALL_NEEDED_SYMBOLS)}] {symbol:12s} {len(df):>6}本  通知(全期間){len(trades):>4}回")
        except Exception as e:
            print(f"[{i}/{len(ALL_NEEDED_SYMBOLS)}] {symbol}: エラー ({e})")
        time.sleep(0.1)

    if not all_trades:
        return pd.DataFrame()
    trades_df = pd.DataFrame(all_trades).sort_values("entry_time").reset_index(drop=True)
    return trades_df


# ============================================================
# シナリオ比較
# ============================================================

def run_matrix(trades_df: pd.DataFrame) -> pd.DataFrame:
    results = []
    for tier_name, tier_symbols in SYMBOL_TIERS.items():
        tier_trades = trades_df[trades_df["symbol"].isin(tier_symbols)]
        for year in YEARS_TO_REPORT:
            year_trades = tier_trades[tier_trades["entry_time"].dt.year == year].sort_values("entry_time").reset_index(drop=True)
            n_trades = len(year_trades)
            if n_trades == 0:
                for frac in FRACTIONS:
                    results.append({
                        "銘柄範囲": tier_name, "銘柄数": len(tier_symbols), "年": year,
                        "投資割合": f"{frac*100:.0f}%", "通知回数": 0,
                        "勝率(%)": None, "平均リターン(%)": None,
                        "最終残高(円)": INITIAL_CAPITAL_JPY, "損益(円)": 0,
                        "最大ドローダウン(%)": 0.0,
                    })
                continue

            win_rate = (year_trades["net_return_pct"] > 0).mean() * 100
            avg_return = year_trades["net_return_pct"].mean()

            for frac in FRACTIONS:
                sim = nsim.compound_simulate(year_trades, INITIAL_CAPITAL_JPY, frac)
                final_balance = sim["final_balance"]
                total_pnl = final_balance - INITIAL_CAPITAL_JPY
                results.append({
                    "銘柄範囲": tier_name,
                    "銘柄数": len(tier_symbols),
                    "年": year,
                    "投資割合": f"{frac*100:.0f}%",
                    "通知回数": n_trades,
                    "勝率(%)": round(win_rate, 1),
                    "平均リターン(%)": round(avg_return, 2),
                    "最終残高(円)": round(final_balance),
                    "損益(円)": round(total_pnl),
                    "最大ドローダウン(%)": round(sim["max_drawdown_pct"], 1),
                })
    return pd.DataFrame(results)


def main():
    print(f"実行時刻(UTC): {datetime.now(timezone.utc).isoformat()}")
    trades_df = collect_all_trades()
    if len(trades_df) == 0:
        print("通知イベントが1件も取得できませんでした。処理を終了します。")
        return
    trades_df.to_csv(TRADES_CSV, index=False, encoding="utf-8-sig")

    matrix_df = run_matrix(trades_df)
    matrix_df.to_csv(MATRIX_CSV, index=False, encoding="utf-8-sig")

    print("\n" + "=" * 90)
    print("■ シナリオ比較（銘柄範囲 × 投資割合）")
    print("=" * 90)
    with pd.option_context("display.max_rows", None, "display.width", 160):
        print(matrix_df.to_string(index=False))

    lines = []
    lines.append("# シナリオ比較シミュレーションレポート（対象銘柄数 × 投資割合）\n")
    lines.append(f"- 実行日時(UTC): {datetime.now(timezone.utc).isoformat()}")
    lines.append(f"- 通知条件: 出来高急増 かつ RSI反発 が新たに成立した瞬間（エッジ検出）")
    lines.append(f"- 使用した足: {nsim.SIM_INTERVAL}（ライブ運用と同じ） / データ取得開始: {SIM_START_DATE}")
    lines.append(f"- 利確ライン: +{nsim.TAKE_PROFIT_PCT}%（到達しない場合は{nsim.MAX_HOLD_DAYS}日で強制決済）")
    lines.append(f"- シミュレーション元本: {INITIAL_CAPITAL_JPY:,.0f}円")
    lines.append(f"- 比較する銘柄範囲: " + " / ".join(f"{k}（{len(v)}銘柄）" for k, v in SYMBOL_TIERS.items()))
    lines.append(f"- 比較する投資割合（複利、毎回残高に対する割合）: " + " / ".join(f"{f*100:.0f}%" for f in FRACTIONS))
    lines.append("- 日次の確度フィルタ（1日◯件まで絞り込み）は使わず、条件に合致した通知は全てプールして"
                 "時系列順にシミュレーションしています（銘柄を広げた効果を素直に見るため）。\n")

    for year in YEARS_TO_REPORT:
        lines.append(f"## {year}年\n")
        lines.append("| 銘柄範囲 | 投資割合 | 通知回数 | 勝率 | 平均リターン/回 | 最終残高 | 損益 | 最大ドローダウン |")
        lines.append("|---|---|---|---|---|---|---|---|")
        sub = matrix_df[matrix_df["年"] == year]
        for _, r in sub.iterrows():
            win_str = f"{r['勝率(%)']}%" if r["勝率(%)"] is not None else "-"
            ret_str = f"{r['平均リターン(%)']:+.2f}%" if r["平均リターン(%)"] is not None else "-"
            lines.append(
                f"| {r['銘柄範囲']} | {r['投資割合']} | {int(r['通知回数'])}回 | {win_str} | {ret_str} | "
                f"{r['最終残高(円)']:,.0f}円 | {r['損益(円)']:+,.0f}円 | -{r['最大ドローダウン(%)']:.1f}% |"
            )
        lines.append("")

    lines.append("## 注意事項\n")
    lines.append("- これは過去データを機械的に再現したシミュレーションであり、将来の成績を保証するものではありません。")
    lines.append("- 銘柄範囲を広げるほど通知回数（＝トレード機会）は増えますが、複数銘柄が同時期に"
                 "連動して下落する局面では、連敗も重なりやすくなります（分散効果が限定的な場合があります）。")
    lines.append("- 投資割合を上げるほどリターン・ドローダウンの両方が拡大します。「最終残高」だけでなく"
                 "必ず「最大ドローダウン」も見て、その下落幅に耐えられるかご検討ください。")
    lines.append("- 複数の通知が同じ日・同じ時間帯に重なった場合の同時必要資金までは考慮していません"
                 "（本シミュレーションは通知が来た順に逐次、複利で計算しています）。")
    lines.append(f"- 利確ラインに{nsim.MAX_HOLD_DAYS}日以内に到達しない場合、その時点の終値で強制決済したとみなしています。")
    lines.append("- スリッページは考慮していません。")
    lines.append(f"- 手数料は往復{nsim.FEE_ROUNDTRIP_PCT}%の概算です。実際の手数料率はBinance Japanの契約プランをご確認ください。")
    lines.append("- 本レポートは投資助言ではありません。投資判断はご自身の責任で行ってください。")

    with open(REPORT_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"\n{TRADES_CSV} / {MATRIX_CSV} / {REPORT_MD} を保存しました。")


if __name__ == "__main__":
    main()
