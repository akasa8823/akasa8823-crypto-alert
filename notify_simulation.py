#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
通知回数 & 利確シミュレーションツール
=====================================================
「出来高急増」と「RSI反発」が両方同時に立った瞬間（＝通知が来るタイミング）を
過去データから再現し、

  - 2023年・2024年でそれぞれ何回通知が来ていたか
  - 通知が来るたびに資金を均等分配して投資し、+5%まで上昇した時点で
    利確していたら、100万円がいくらになっていたか

を検証するワンショットのシミュレーションツールです。ライブ運用のスクリプトとは
独立していて、手動実行（workflow_dispatch）でのみ動きます。

★ 手法
- crypto_signal_screener.py と同じ compute_signal_series をそのまま再利用。
- ライブ運用は1時間足で毎回チェックしているため、このシミュレーションも
  1時間足を使用（deep_backtest.py の4時間足とは異なる点に注意）。
- 「通知」は、"volume_spike かつ rsi_rebound" が新たに成立した瞬間（前の足では
  成立していなかった）だけをカウントするエッジ検出方式。ライブ運用の状態遷移
  通知ロジックと同じ考え方です。
- エントリーは、シグナルが確定した足の次の足の始値（未来の情報は使わない）。
- 利確ラインはエントリー価格の+5%。以後の高値（high）を順に見て、最初に
  到達した時点で利確したとみなします。
- 利確ラインに到達しないまま MAX_HOLD_BARS（デフォルト60日分）が経過したら、
  その時点の終値で強制決済したものとして扱います（「ずっと持ち続ける」という
  非現実的な前提を避けるための単純化）。
- 資金計算は「その年に発生した通知回数で100万円を均等に分配し、各トレードに
  同時に投資した」という単純化されたシミュレーションです。実際には同時刻に
  複数の通知が重なることもあるため、これは概算です。
- 売買手数料はBinanceの一般的なテイカー手数料を参考に、往復0.2%相当を
  控除しています（正確な税・手数料率はご自身の契約プランをご確認ください）。

★ 重要な注意
- これは過去データの機械的な再現であり、将来の成績を保証するものではありません。
- 資金配分・複利計算・同時保有制限など、実際の取引とは異なる単純化を含みます。
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

# crypto_signal_screener.py のシグナル計算ロジックをそのまま再利用する
import crypto_signal_screener as core

# ============================================================
# 設定
# ============================================================

SYMBOLS = core.SYMBOLS
SIM_INTERVAL = "1h"  # ライブ運用と同じ足

SIM_START_DATE = os.environ.get("SIM_START_DATE", "2022-11-01")  # 指標のウォームアップ分を余裕を持って前倒し
PAGE_LIMIT = 1000
PAGE_SLEEP_SEC = 0.15

TAKE_PROFIT_PCT = float(os.environ.get("SIM_TAKE_PROFIT_PCT", "5.0"))
MAX_HOLD_DAYS = float(os.environ.get("SIM_MAX_HOLD_DAYS", "60"))
MAX_HOLD_BARS = int(MAX_HOLD_DAYS * 24)  # 1時間足なので1日=24本
FEE_ROUNDTRIP_PCT = float(os.environ.get("SIM_FEE_ROUNDTRIP_PCT", "0.2"))  # 往復手数料の概算
INITIAL_CAPITAL_JPY = float(os.environ.get("SIM_INITIAL_CAPITAL_JPY", "1000000"))

YEARS_TO_REPORT = [2023, 2024]

BASE_URL = core.BASE_URL

TRADES_CSV = "notify_simulation_trades.csv"
REPORT_MD = "notify_simulation_report.md"


# ============================================================
# データ取得（指定日時から現在まで、前向きにページング取得）
# ============================================================

def _klines_json_to_df(data: list) -> pd.DataFrame:
    cols = ["open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades",
            "taker_buy_base", "taker_buy_quote", "ignore"]
    df = pd.DataFrame(data, columns=cols)
    for c in ["open", "high", "low", "close", "volume", "quote_volume"]:
        df[c] = df[c].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    return df


def fetch_klines_forward(symbol: str, interval: str, start_ms: int, page_limit: int) -> pd.DataFrame:
    """指定した開始時刻から現在まで、前向きにページングして取得する"""
    frames = []
    cur_start = start_ms
    while True:
        params = {"symbol": symbol, "interval": interval, "limit": page_limit, "startTime": cur_start}
        resp = requests.get(f"{BASE_URL}/api/v3/klines", params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        if not data:
            break
        frames.append(_klines_json_to_df(data))
        last_close_ms = int(data[-1][6])
        if len(data) < page_limit:
            break
        cur_start = last_close_ms + 1
        time.sleep(PAGE_SLEEP_SEC)
    if not frames:
        return pd.DataFrame()
    full = pd.concat(frames, ignore_index=True)
    full = full.drop_duplicates(subset="open_time").sort_values("open_time").reset_index(drop=True)
    return full


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
    rr = sig["rsi_rebound"].to_numpy()
    entry_signal = vs & rr

    open_time = df["open_time"].to_numpy()
    open_ = df["open"].to_numpy()
    high = df["high"].to_numpy()
    close = df["close"].to_numpy()

    trades = []
    for i in range(1, n - 1):
        if entry_signal[i] and not entry_signal[i - 1]:
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
                exit_price = target_price  # 指値到達で利確したとみなす
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
                "resolved": resolved,  # True=+5%到達で利確 / False=期限切れで強制決済
                "bars_held": bars_held,
                "days_held": round(bars_held / 24, 2),
            })
    return trades


def main():
    start_dt = datetime.strptime(SIM_START_DATE, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    start_ms = int(start_dt.timestamp() * 1000)

    print(f"実行時刻(UTC): {datetime.now(timezone.utc).isoformat()}")
    print(f"対象銘柄: {len(SYMBOLS)} / 足種: {SIM_INTERVAL} / 取得開始: {SIM_START_DATE}")
    print(f"利確ライン: +{TAKE_PROFIT_PCT}% / 最大保有期間: {MAX_HOLD_DAYS}日 / 往復手数料: {FEE_ROUNDTRIP_PCT}%")
    print("-" * 70)

    all_trades = []
    for i, symbol in enumerate(SYMBOLS, 1):
        try:
            df = fetch_klines_forward(symbol, SIM_INTERVAL, start_ms, PAGE_LIMIT)
            if len(df) < 300:
                print(f"[{i}/{len(SYMBOLS)}] {symbol}: データ不足のためスキップ ({len(df)}本)")
                continue
            trades = simulate_symbol(symbol, df)
            all_trades.extend(trades)
            print(f"[{i}/{len(SYMBOLS)}] {symbol:12s} {len(df):>6}本  通知(全期間){len(trades):>4}回")
        except Exception as e:
            print(f"[{i}/{len(SYMBOLS)}] {symbol}: エラー ({e})")
        time.sleep(0.1)

    if not all_trades:
        print("通知イベントが1件も取得できませんでした。処理を終了します。")
        return

    trades_df = pd.DataFrame(all_trades).sort_values("entry_time").reset_index(drop=True)
    trades_df.to_csv(TRADES_CSV, index=False, encoding="utf-8-sig")

    lines = []
    lines.append("# 通知回数 & 利確シミュレーションレポート\n")
    lines.append(f"- 実行日時(UTC): {datetime.now(timezone.utc).isoformat()}")
    lines.append(f"- 通知条件: 出来高急増 かつ RSI反発 が新たに成立した瞬間（エッジ検出）")
    lines.append(f"- 対象銘柄数: {len(SYMBOLS)} / 使用した足: {SIM_INTERVAL}（ライブ運用と同じ）")
    lines.append(f"- データ取得開始: {SIM_START_DATE}")
    lines.append(f"- 利確ライン: +{TAKE_PROFIT_PCT}%（到達しない場合は{MAX_HOLD_DAYS}日で強制決済）")
    lines.append(f"- 往復手数料の概算控除: {FEE_ROUNDTRIP_PCT}%")
    lines.append(f"- シミュレーション元本: {INITIAL_CAPITAL_JPY:,.0f}円（年ごとに、その年の通知回数で均等分配）\n")

    for year in YEARS_TO_REPORT:
        year_trades = trades_df[trades_df["entry_time"].dt.year == year]
        n_trades = len(year_trades)

        lines.append(f"## {year}年の結果\n")
        if n_trades == 0:
            lines.append(f"{year}年は通知イベントがありませんでした。\n")
            print(f"\n■ {year}年: 通知0回")
            continue

        n_resolved = int(year_trades["resolved"].sum())
        n_unresolved = n_trades - n_resolved
        avg_days_resolved = year_trades.loc[year_trades["resolved"], "days_held"].mean() if n_resolved else float("nan")
        avg_net_return = year_trades["net_return_pct"].mean()
        win_rate = (year_trades["net_return_pct"] > 0).mean() * 100

        position_size = INITIAL_CAPITAL_JPY / n_trades
        year_trades = year_trades.copy()
        year_trades["pnl_jpy"] = position_size * year_trades["net_return_pct"] / 100
        total_pnl_jpy = year_trades["pnl_jpy"].sum()
        final_balance_jpy = INITIAL_CAPITAL_JPY + total_pnl_jpy

        lines.append(f"- 通知回数: **{n_trades}回**")
        lines.append(f"- うち+{TAKE_PROFIT_PCT}%に到達して利確: {n_resolved}回 / 期限切れで強制決済: {n_unresolved}回")
        if n_resolved:
            lines.append(f"- 利確までの平均日数（到達分のみ）: 約{avg_days_resolved:.1f}日")
        lines.append(f"- 1トレードあたりの平均リターン（手数料控除後）: {avg_net_return:+.2f}%")
        lines.append(f"- プラスで終わったトレードの割合: {win_rate:.1f}%")
        lines.append(f"- 1トレードあたりの投資額（{INITIAL_CAPITAL_JPY:,.0f}円を{n_trades}回に均等分配）: 約{position_size:,.0f}円")
        lines.append(f"- **シミュレーション結果: {INITIAL_CAPITAL_JPY:,.0f}円 → 約{final_balance_jpy:,.0f}円（損益 {total_pnl_jpy:+,.0f}円）**\n")

        print(f"\n■ {year}年: 通知{n_trades}回 / 利確{n_resolved}回 / 強制決済{n_unresolved}回")
        print(f"  平均リターン {avg_net_return:+.2f}% / 勝率 {win_rate:.1f}%")
        print(f"  シミュレーション: {INITIAL_CAPITAL_JPY:,.0f}円 → {final_balance_jpy:,.0f}円 ({total_pnl_jpy:+,.0f}円)")

    lines.append("## 注意事項\n")
    lines.append("- これは過去データを機械的に再現したシミュレーションであり、将来の成績を保証するものではありません。")
    lines.append("- 資金は「その年の通知回数で均等分配し、全トレードに同時投資した」という単純化です。実際には通知が同時刻に重なることもあり、現実の資金管理とは異なります。")
    lines.append("- 複利（利益を次のトレードに回す）は考慮していません。各年、常に元本100万円を使い切る前提です。")
    lines.append(f"- 利確ラインに{MAX_HOLD_DAYS}日以内に到達しない場合、その時点の終値で強制決済したとみなしています。実際にはさらに長く保有する・損切りするなどの選択肢があります。")
    lines.append("- スリッページ（指値が想定通りに約定しない可能性）は考慮していません。")
    lines.append(f"- 手数料は往復{FEE_ROUNDTRIP_PCT}%の概算です。実際の手数料率はBinance Japanの契約プランをご確認ください。")
    lines.append("- 本レポートは投資助言ではありません。投資判断はご自身の責任で行ってください。")

    with open(REPORT_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"\n{TRADES_CSV} / {REPORT_MD} を保存しました。")


if __name__ == "__main__":
    main()
