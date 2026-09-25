#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
利確ライン到達分析ツール（+5%を超えてどこまで伸びたか）
=====================================================
notify_simulation.py / notify_simulation_matrix.py と同じ「出来高急増 かつ
RSI反発」が新たに成立した瞬間をエントリーとして検出し、現在の利確ライン（+5%）
で区切らずに、エントリー後の保有期間中の最高値（high）がエントリー価格から
何%まで伸びたかを1件ずつ記録します。

その上で、
  - +7%以上まで伸びたケースが何回あったか
  - +10%以上まで伸びたケースが何回あったか
を、対象期間・対象銘柄について集計します（デフォルトは主要5銘柄）。

★ 手法
- エントリー検出・エントリー価格の決め方は notify_simulation.py と同じ
  （次の足の始値、未来を見ない）。
- 「保有期間中の最高値」は、利確ラインに到達したかどうかに関わらず、
  MAX_HOLD_BARS（デフォルト60日分）以内の high の最大値を見る。
  つまり「+5%で利確しなかった場合、そのままどこまで伸びる可能性が
  あったか」という後知恵の分析であり、実際の利確シミュレーションとは別物です。
- 手数料は考慮していません（あくまで「価格が到達したかどうか」の分析のため）。

★ 重要な注意
- これは過去データの機械的な再現であり、将来の成績を保証するものではありません。
- 「+7%や+10%まで伸びたことがある」ことと、「利確ラインをそこまで上げても
  同じ勝率・回数を維持できる」ことは別問題です（伸び切る前に反落するケースも
  当然あります）。利確ラインを上げる場合は、別途そのライン基準でのシミュレーション
  で検証することをおすすめします。
"""

import os
import sys
import time
from datetime import datetime, timezone

try:
    import pandas as pd
    import numpy as np
except ImportError:
    print("必要なライブラリがありません: pip install -r requirements.txt")
    sys.exit(1)

import crypto_signal_screener as core
import notify_simulation as nsim

# ============================================================
# 設定
# ============================================================

_default_symbols = "BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT,XRPUSDT"
SYMBOLS = [s.strip() for s in os.environ.get("TARGET_SYMBOLS", _default_symbols).split(",") if s.strip()]

SIM_START_DATE = os.environ.get("SIM_START_DATE", nsim.SIM_START_DATE)
MAX_HOLD_BARS = nsim.MAX_HOLD_BARS  # notify_simulation.py と同じ最大保有期間
TARGET_THRESHOLDS_PCT = [5.0, 7.0, 10.0]  # 参考として現行の5%も含める
YEARS_TO_REPORT = [2023, 2024]

TRADES_CSV = "target_reach_trades.csv"
REPORT_MD = "target_reach_report.md"


# ============================================================
# エントリー検出 & 最大到達率の記録
# ============================================================

def analyze_symbol(symbol: str, df: pd.DataFrame) -> list:
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

    rows = []
    for i in range(1, n - 1):
        if entry_signal[i] and not entry_signal[i - 1]:
            entry_idx = i + 1
            if entry_idx >= n:
                continue
            entry_time = pd.Timestamp(open_time[entry_idx])
            entry_price = float(open_[entry_idx])
            if entry_price <= 0:
                continue

            window_end = min(entry_idx + MAX_HOLD_BARS, n)
            sub_high = high[entry_idx:window_end]
            if len(sub_high) == 0:
                continue
            max_high = float(np.max(sub_high))
            max_rise_pct = (max_high - entry_price) / entry_price * 100
            bars_to_max = int(np.argmax(sub_high))

            row = {
                "symbol": symbol,
                "entry_time": entry_time,
                "entry_price": entry_price,
                "max_high": max_high,
                "max_rise_pct": round(max_rise_pct, 3),
                "days_to_max": round(bars_to_max / 24, 2),
            }
            for th in TARGET_THRESHOLDS_PCT:
                row[f"reached_{th:g}pct"] = max_rise_pct >= th
            rows.append(row)
    return rows


def main():
    start_dt = datetime.strptime(SIM_START_DATE, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    start_ms = int(start_dt.timestamp() * 1000)

    print(f"実行時刻(UTC): {datetime.now(timezone.utc).isoformat()}")
    print(f"対象銘柄: {', '.join(SYMBOLS)} / 足種: {nsim.SIM_INTERVAL} / 取得開始: {SIM_START_DATE}")
    print(f"最大保有期間: {nsim.MAX_HOLD_DAYS}日 / 判定する到達ライン: {TARGET_THRESHOLDS_PCT}")
    print("-" * 70)

    all_rows = []
    for i, symbol in enumerate(SYMBOLS, 1):
        try:
            df = nsim.fetch_klines_forward(symbol, nsim.SIM_INTERVAL, start_ms, nsim.PAGE_LIMIT)
            if len(df) < 300:
                print(f"[{i}/{len(SYMBOLS)}] {symbol}: データ不足のためスキップ ({len(df)}本)")
                continue
            rows = analyze_symbol(symbol, df)
            all_rows.extend(rows)
            print(f"[{i}/{len(SYMBOLS)}] {symbol:12s} {len(df):>6}本  エントリー(全期間){len(rows):>4}回")
        except Exception as e:
            print(f"[{i}/{len(SYMBOLS)}] {symbol}: エラー ({e})")
        time.sleep(0.1)

    if not all_rows:
        print("エントリーイベントが1件も取得できませんでした。処理を終了します。")
        return

    trades_df = pd.DataFrame(all_rows).sort_values("entry_time").reset_index(drop=True)
    trades_df.to_csv(TRADES_CSV, index=False, encoding="utf-8-sig")

    lines = []
    lines.append("# 利確ライン到達分析レポート（+5%を超えてどこまで伸びたか）\n")
    lines.append(f"- 実行日時(UTC): {datetime.now(timezone.utc).isoformat()}")
    lines.append(f"- 対象銘柄: {', '.join(SYMBOLS)}")
    lines.append(f"- 通知条件: 出来高急増 かつ RSI反発 が新たに成立した瞬間（エッジ検出）")
    lines.append(f"- 使用した足: {nsim.SIM_INTERVAL}（ライブ運用と同じ） / データ取得開始: {SIM_START_DATE}")
    lines.append(f"- 最大保有期間: {nsim.MAX_HOLD_DAYS}日（この期間内の最高値を見ています）")
    lines.append("- 手数料・スリッページは考慮していません（価格の到達可否のみを見る分析のため）\n")

    def summarize(df_sub: pd.DataFrame, label: str):
        n = len(df_sub)
        lines.append(f"### {label}（n={n}）\n")
        if n == 0:
            lines.append("該当するエントリーがありませんでした。\n")
            print(f"\n■ {label}: エントリー0回")
            return
        lines.append("| 到達ライン | 到達回数 | 到達率 |")
        lines.append("|---|---|---|")
        print(f"\n■ {label}: エントリー{n}回")
        for th in TARGET_THRESHOLDS_PCT:
            col = f"reached_{th:g}pct"
            cnt = int(df_sub[col].sum())
            pct = cnt / n * 100
            lines.append(f"| +{th:g}%以上 | {cnt}回 | {pct:.1f}% |")
            print(f"  +{th:g}%以上到達: {cnt}回 ({pct:.1f}%)")
        avg_max_rise = df_sub["max_rise_pct"].mean()
        median_max_rise = df_sub["max_rise_pct"].median()
        lines.append(f"\n- 最大到達率の平均: {avg_max_rise:+.2f}% / 中央値: {median_max_rise:+.2f}%\n")
        print(f"  最大到達率の平均: {avg_max_rise:+.2f}% / 中央値: {median_max_rise:+.2f}%")

    lines.append("## 全期間\n")
    summarize(trades_df, "全期間・全銘柄合算")

    for year in YEARS_TO_REPORT:
        year_df = trades_df[trades_df["entry_time"].dt.year == year]
        lines.append(f"## {year}年\n")
        summarize(year_df, f"{year}年・全銘柄合算")

    lines.append("## 銘柄別（全期間）\n")
    lines.append("| 銘柄 | エントリー回数 | +7%以上 | +10%以上 |")
    lines.append("|---|---|---|---|")
    for symbol in SYMBOLS:
        sub = trades_df[trades_df["symbol"] == symbol]
        n = len(sub)
        if n == 0:
            lines.append(f"| {symbol} | 0回 | - | - |")
            continue
        c7 = int(sub["reached_7pct"].sum())
        c10 = int(sub["reached_10pct"].sum())
        lines.append(f"| {symbol} | {n}回 | {c7}回 ({c7/n*100:.1f}%) | {c10}回 ({c10/n*100:.1f}%) |")

    lines.append("\n## 注意事項\n")
    lines.append("- これは過去データの機械的な集計であり、将来の値動きを保証するものではありません。")
    lines.append("- 「最高値がそこまで到達した」という事実と、「利確ラインをそこまで上げても"
                 "実際に利確できる」ことは別問題です。到達する前に反落して、より低い価格で"
                 "決済（または強制決済）していたケースも当然あります。利確ラインの引き上げを"
                 "検討する場合は、そのライン基準での利確シミュレーション（notify_simulation.py の"
                 "SIM_TAKE_PROFIT_PCT を変更して再実行）で改めて検証することをおすすめします。")
    lines.append(f"- 保有期間の上限は{nsim.MAX_HOLD_DAYS}日としています。これより長く保有すれば"
                 "さらに伸びるケースもあり得ますが、ここでは考慮していません。")
    lines.append("- 手数料・スリッページは考慮していません。")
    lines.append("- 本レポートは投資助言ではありません。投資判断はご自身の責任で行ってください。")

    with open(REPORT_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"\n{TRADES_CSV} / {REPORT_MD} を保存しました。")


if __name__ == "__main__":
    main()
