#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
「急騰（3日以内に+30%以上）」の予兆シグナル 検証ツール
=====================================================
BTCUSDT以外の主要30銘柄（元の全30銘柄リストからBTCUSDTを除いた29銘柄）について、
過去データを使い、「3日以内に+30%以上上昇したケース」の直前に、
crypto_signal_screener.py と同じ4つのシグナル（出来高急増／ボラティリティ収縮／
ゴールデンクロス／RSI反発）がどれくらいの確率で・どんな組み合わせで
立っていたかを検証するワンショットのレポートツールです。

BTCUSDTのライブ運用（crypto_signal_screener.py）とは完全に独立していて、
手動実行（workflow_dispatch）でのみ動きます。このスクリプト単体では通知は
一切行いません。ここで「本当にエッジのある条件」が見つかった場合のみ、
crypto_signal_screener.py 側に「BTCUSDTは今まで通り／他29銘柄は急騰予兆の
シグナルが出たら別途通知」という形で組み込みます。

★ 手法
- 4時間足を使用（1本=4時間）。3日以内 = 18本先までを見る。
- 各時点 i について、close[i] を基準に、その後18本の high の最大値が
  +30%以上に達したかどうか（＝「3日以内に高値ベースで+30%到達」）を判定します。
  target_reach_analysis.py と同じ「到達したかどうか」の考え方で、
  deep_backtest.py の「ちょうどN本後の終値」ではなく、期間中の最高値を見ます。
- 未来のデータは一切参照しないベクトル化計算
  （crypto_signal_screener.py の compute_signal_series をそのまま再利用）。
- スコア別・シグナル組み合わせ別・銘柄別に、+30%到達率とベースライン
  （シグナル無しの場合の到達率）との差（edge）を集計します。

★ 重要な注意
- 「3日で+30%」はかなり極端な値動きです。多くの場合、少数の銘柄・少数の
  期間（特定の相場イベント）に結果が偏る可能性が高く、これは
  「本当に再現性のあるシグナル」なのか「たまたま過去に数回そういう
  ことがあっただけ」なのかを、本レポートの銘柄別内訳・発生回数と
  あわせて必ず確認してください。
- サンプル数（発生回数）が少ない銘柄・組み合わせの数字は参考程度に。
- 投資判断・資金管理はご自身の責任で行ってください。本レポートは
  投資助言ではありません。
"""

import os
import sys
import time
import itertools
from datetime import datetime, timezone

try:
    import requests
    import pandas as pd
except ImportError:
    print("必要なライブラリがありません: pip install -r requirements.txt")
    sys.exit(1)

# crypto_signal_screener.py のシグナル計算ロジック、deep_backtest.py の
# 深い過去データ取得ロジックをそのまま再利用する
import crypto_signal_screener as core
import deep_backtest as deep

# ============================================================
# 設定
# ============================================================

# 元の全30銘柄リスト（deep_backtest_coverage.csv で実際に検証済み）から、
# BTCUSDTライブ運用と重複しないよう除外した29銘柄
MOONSHOT_SYMBOLS = [
    "ETHUSDT", "BNBUSDT", "LTCUSDT", "ADAUSDT", "XRPUSDT",
    "XLMUSDT", "TRXUSDT", "ETCUSDT", "LINKUSDT", "ENJUSDT",
    "ATOMUSDT", "DOGEUSDT", "CHZUSDT", "BCHUSDT", "MANAUSDT",
    "SOLUSDT", "SANDUSDT", "DOTUSDT", "UNIUSDT", "AVAXUSDT",
    "NEARUSDT", "FILUSDT", "AAVEUSDT", "AXSUSDT", "SHIBUSDT",
    "OPUSDT", "APTUSDT", "ARBUSDT", "SUIUSDT",
]

DEEP_INTERVAL = "4h"
PAGE_LIMIT = 1000
MAX_PAGES = int(os.environ.get("MOONSHOT_MAX_PAGES", "30"))  # 30ページ=最大約13.7年分
PAGE_SLEEP_SEC = 0.2

# 「3日以内」= 4時間足で18本先まで
BARS_PER_DAY = 24 // 4
HOLD_DAYS = float(os.environ.get("MOONSHOT_HOLD_DAYS", "3"))
HORIZON_BARS = int(round(BARS_PER_DAY * HOLD_DAYS))
SUCCESS_THRESHOLD_PCT = float(os.environ.get("MOONSHOT_SUCCESS_THRESHOLD_PCT", "30.0"))

BY_SCORE_CSV = "moonshot_by_score.csv"
BY_COMBO_CSV = "moonshot_by_combo.csv"
BY_SYMBOL_CSV = "moonshot_by_symbol.csv"
COVERAGE_CSV = "moonshot_coverage.csv"
REPORT_MD = "moonshot_report.md"

# 組み合わせ集計で、この件数未満のものはノイズとして表から除外する
MIN_COMBO_N = int(os.environ.get("MOONSHOT_MIN_COMBO_N", "30"))

SIGNAL_LABELS = core.SIGNAL_LABELS


# ============================================================
# バックテスト集計（「その後 HORIZON_BARS 本以内の高値ベースで
# SUCCESS_THRESHOLD_PCT %以上に到達したか」を判定）
# ============================================================

def moonshot_rows(sig: pd.DataFrame, df: pd.DataFrame, horizon_bars: int, threshold_pct: float) -> list:
    n = len(sig)
    if n <= horizon_bars:
        return []
    close = sig["close"].to_numpy()
    high = df["high"].to_numpy()
    score = sig["score"].to_numpy()
    vs = sig["volume_spike"].to_numpy()
    bb = sig["bb_squeeze"].to_numpy()
    gc = sig["golden_cross"].to_numpy()
    rr = sig["rsi_rebound"].to_numpy()
    vol_ratio = sig["volume_ratio"].to_numpy()
    rows = []
    for i in range(n - horizon_bars):
        c0 = close[i]
        if c0 <= 0:
            continue
        window_high = high[i + 1: i + 1 + horizon_bars].max()
        max_rise_pct = (window_high - c0) / c0 * 100
        reached = max_rise_pct >= threshold_pct
        vr = vol_ratio[i]
        rows.append((
            int(score[i]), bool(vs[i]), bool(bb[i]), bool(gc[i]), bool(rr[i]),
            float(vr) if pd.notna(vr) else None,
            round(float(max_rise_pct), 2), bool(reached),
        ))
    return rows


def main():
    print(f"実行時刻(UTC): {datetime.now(timezone.utc).isoformat()}")
    print(f"対象銘柄: {len(MOONSHOT_SYMBOLS)}（BTCUSDT除く） / 足種: {DEEP_INTERVAL} / 最大ページ数: {MAX_PAGES}")
    print(f"検証: {HOLD_DAYS}日以内（{HORIZON_BARS}本先）の高値ベースで +{SUCCESS_THRESHOLD_PCT}% 以上到達したか")
    print("-" * 70)

    all_rows = []  # (symbol, score, vs, bb, gc, rr, volume_ratio, max_rise_pct, reached)
    coverage = []

    for i, symbol in enumerate(MOONSHOT_SYMBOLS, 1):
        try:
            df = deep.fetch_klines_deep(symbol, DEEP_INTERVAL, MAX_PAGES, PAGE_LIMIT)
            if len(df) < 100:
                print(f"[{i}/{len(MOONSHOT_SYMBOLS)}] {symbol}: データ不足のためスキップ ({len(df)}本)")
                continue

            sig = core.compute_signal_series(df)
            rows = moonshot_rows(sig, df, HORIZON_BARS, SUCCESS_THRESHOLD_PCT)
            for r in rows:
                all_rows.append((symbol,) + r)

            reached_count = sum(1 for r in rows if r[-1])
            signal_occurrences = sum(1 for r in rows if r[0] > 0)

            date_from = df["open_time"].iloc[0]
            date_to = df["open_time"].iloc[-1]
            years = (date_to - date_from).days / 365.25
            coverage.append({
                "symbol": symbol, "bars": len(df),
                "date_from": str(date_from.date()), "date_to": str(date_to.date()),
                "years": round(years, 2),
                "signal_occurrences": signal_occurrences,
                "reached_30pct_occurrences": reached_count,
                "reached_rate_all_bars_pct": round(reached_count / len(rows) * 100, 2) if rows else None,
            })
            print(f"[{i}/{len(MOONSHOT_SYMBOLS)}] {symbol:10s} {len(df):>5}本 ({years:5.2f}年分)  "
                  f"到達{reached_count:>4}回 / シグナル発生{signal_occurrences:>4}回")
        except Exception as e:
            print(f"[{i}/{len(MOONSHOT_SYMBOLS)}] {symbol}: エラー ({e})")
        time.sleep(0.1)

    if not all_rows:
        print("データが取得できませんでした。処理を終了します。")
        return

    df_all = pd.DataFrame(
        all_rows,
        columns=["symbol", "score", "volume_spike", "bb_squeeze", "golden_cross",
                 "rsi_rebound", "volume_ratio", "max_rise_pct", "reached"],
    )
    cov_df = pd.DataFrame(coverage).sort_values("years", ascending=False)
    cov_df.to_csv(COVERAGE_CSV, index=False, encoding="utf-8-sig")

    total_bars = int(cov_df["bars"].sum()) if len(cov_df) else 0

    # --- ベースライン（シグナル無し = score 0 の時の到達率） ---
    baseline_sub = df_all[df_all["score"] == 0]
    baseline_rate = (baseline_sub["reached"] == True).mean() * 100 if len(baseline_sub) else 0.0  # noqa: E712
    baseline_n = len(baseline_sub)

    # --- スコア別集計 ---
    by_score = []
    for lvl in sorted(df_all["score"].unique()):
        sub = df_all[df_all["score"] == lvl]
        rate = (sub["reached"] == True).mean() * 100  # noqa: E712
        by_score.append({
            "score": int(lvl),
            "n": len(sub),
            "reached_rate_pct": round(rate, 2),
            "mean_max_rise_pct": round(sub["max_rise_pct"].mean(), 2),
            "median_max_rise_pct": round(sub["max_rise_pct"].median(), 2),
            "edge_over_baseline_pct": round(rate - baseline_rate, 2),
        })
    by_score_df = pd.DataFrame(by_score).sort_values("score")
    by_score_df.to_csv(BY_SCORE_CSV, index=False, encoding="utf-8-sig")

    # --- シグナル組み合わせ別集計 ---
    signal_cols = list(SIGNAL_LABELS.keys())
    by_combo = []
    for r in range(1, len(signal_cols) + 1):
        for combo in itertools.combinations(signal_cols, r):
            mask = pd.Series(True, index=df_all.index)
            for c in combo:
                mask &= df_all[c] == True  # noqa: E712
            sub = df_all[mask]
            n = len(sub)
            if n < MIN_COMBO_N:
                continue
            rate = (sub["reached"] == True).mean() * 100  # noqa: E712
            n_symbols_hit = sub.loc[sub["reached"] == True, "symbol"].nunique()  # noqa: E712
            label = " + ".join(SIGNAL_LABELS[c] for c in combo)
            by_combo.append({
                "combo": "+".join(combo),
                "label": label,
                "n_signals": len(combo),
                "n": n,
                "reached_rate_pct": round(rate, 2),
                "edge_over_baseline_pct": round(rate - baseline_rate, 2),
                "mean_max_rise_pct": round(sub["max_rise_pct"].mean(), 2),
                "n_distinct_symbols_reached": int(n_symbols_hit),
            })
    by_combo_df = pd.DataFrame(by_combo)
    if len(by_combo_df):
        by_combo_df = by_combo_df.sort_values("edge_over_baseline_pct", ascending=False).reset_index(drop=True)
    by_combo_df.to_csv(BY_COMBO_CSV, index=False, encoding="utf-8-sig")

    # --- 銘柄別集計（score>=1 のケースに限定：特定の銘柄に結果が偏っていないか確認用） ---
    by_symbol = []
    for symbol in MOONSHOT_SYMBOLS:
        sub = df_all[(df_all["symbol"] == symbol) & (df_all["score"] >= 1)]
        if len(sub) == 0:
            continue
        rate = (sub["reached"] == True).mean() * 100  # noqa: E712
        by_symbol.append({
            "symbol": symbol,
            "n_signal_occurrences": len(sub),
            "reached_rate_pct": round(rate, 2),
            "reached_count": int((sub["reached"] == True).sum()),  # noqa: E712
        })
    by_symbol_df = pd.DataFrame(by_symbol)
    if len(by_symbol_df):
        by_symbol_df = by_symbol_df.sort_values("reached_count", ascending=False).reset_index(drop=True)
    by_symbol_df.to_csv(BY_SYMBOL_CSV, index=False, encoding="utf-8-sig")

    overall_sub = df_all[df_all["score"] > 0]
    overall_rate = (overall_sub["reached"] == True).mean() * 100 if len(overall_sub) else 0.0  # noqa: E712

    print("\n" + "=" * 70)
    print(f"■ ベースライン（シグナル無し）: 到達率 {baseline_rate:.2f}% (n={baseline_n})")
    print("=" * 70)
    print(by_score_df.to_string(index=False))
    print("\n" + "=" * 70)
    print(f"■ シグナル組み合わせ別（n>={MIN_COMBO_N}のみ、edge降順）")
    print("=" * 70)
    if len(by_combo_df):
        print(by_combo_df[["label", "n", "reached_rate_pct", "edge_over_baseline_pct",
                            "n_distinct_symbols_reached"]].to_string(index=False))
    else:
        print("（条件を満たす組み合わせがありませんでした）")
    print(f"\n全体到達率（score>0の全ケース）: {overall_rate:.2f}% (n={len(overall_sub)}) / edge: {overall_rate - baseline_rate:+.2f}pt")

    # --- Markdownレポート ---
    lines = []
    lines.append("# 急騰予兆シグナル検証レポート（3日以内+30%以上到達）\n")
    lines.append(f"- 実行日時(UTC): {datetime.now(timezone.utc).isoformat()}")
    lines.append(f"- 対象銘柄数: {len(MOONSHOT_SYMBOLS)}（BTCUSDTを除く。データ取得成功: {len(cov_df)}）")
    lines.append(f"- 使用した足: {DEEP_INTERVAL}（4時間足）")
    lines.append(f"- 検証ホライズン: シグナル発生から {HOLD_DAYS:g}日以内（{HORIZON_BARS}本先）の**高値ベース**での到達")
    lines.append(f"- 到達の定義: {SUCCESS_THRESHOLD_PCT:g}% 以上の上昇")
    lines.append(f"- 取得した総本数: {total_bars:,} 本\n")

    lines.append("## ベースライン（シグナルが一切無いときの到達率）\n")
    lines.append(
        f"- ベースライン到達率: **{baseline_rate:.2f}%** (n={baseline_n:,})\n"
        f"「シグナルに意味があるか」は、このベースラインとどれだけ差があるか（edge）でしか判断できません。"
    )

    lines.append("\n## スコア別の到達率\n")
    lines.append("| スコア | 発生回数 | 到達率 | ベースライン比(edge) | 平均最大上昇率 |")
    lines.append("|---|---|---|---|---|")
    for _, r in by_score_df.iterrows():
        tag = "0（ベースライン）" if int(r["score"]) == 0 else str(int(r["score"]))
        lines.append(f"| {tag} | {int(r['n'])} | {r['reached_rate_pct']}% | {r['edge_over_baseline_pct']:+.2f}pt | {r['mean_max_rise_pct']}% |")

    lines.append(f"\n**全体到達率（score>0の全ケース）: {overall_rate:.2f}%**（n={len(overall_sub):,}）"
                  f" / **ベースライン比 edge: {overall_rate - baseline_rate:+.2f}pt**\n")

    lines.append("## シグナル組み合わせ別の到達率（エッジが高い順）\n")
    lines.append(
        f"発生回数が{MIN_COMBO_N}回未満の組み合わせはノイズとして除外しています。"
        f"`n_distinct_symbols_reached` は、その組み合わせで実際に到達した銘柄が何種類あったか"
        f"（特定の1銘柄だけに結果が偏っていないかの目安）です。\n"
    )
    if len(by_combo_df):
        lines.append("| 組み合わせ | 発生回数 | 到達率 | ベースライン比(edge) | 平均最大上昇率 | 到達した銘柄数 |")
        lines.append("|---|---|---|---|---|---|")
        for _, r in by_combo_df.iterrows():
            lines.append(
                f"| {r['label']} | {int(r['n'])} | {r['reached_rate_pct']}% | "
                f"{r['edge_over_baseline_pct']:+.2f}pt | {r['mean_max_rise_pct']}% | {int(r['n_distinct_symbols_reached'])} |"
            )
    else:
        lines.append(f"（発生回数が{MIN_COMBO_N}回以上ある組み合わせがありませんでした）")

    lines.append("\n## 銘柄別の内訳（score>=1のケースに限定）\n")
    lines.append("結果が特定の銘柄だけに偏っていないかを確認するための内訳です。\n")
    if len(by_symbol_df):
        lines.append("| 銘柄 | シグナル発生回数 | 到達回数 | 到達率 |")
        lines.append("|---|---|---|---|")
        for _, r in by_symbol_df.iterrows():
            lines.append(f"| {r['symbol']} | {int(r['n_signal_occurrences'])} | {int(r['reached_count'])} | {r['reached_rate_pct']}% |")
    else:
        lines.append("（データなし）")

    lines.append("\n## 銘柄別のデータ取得状況\n")
    lines.append("| 銘柄 | 取得期間 | 年数 | シグナル発生回数 | 到達回数（全期間中） | 到達率（全バー中） |")
    lines.append("|---|---|---|---|---|---|")
    for _, r in cov_df.iterrows():
        lines.append(
            f"| {r['symbol']} | {r['date_from']} 〜 {r['date_to']} | {r['years']} | "
            f"{int(r['signal_occurrences'])} | {int(r['reached_30pct_occurrences'])} | {r['reached_rate_all_bars_pct']}% |"
        )

    lines.append("\n## 重要な注意事項\n")
    lines.append("- 「3日以内に+30%以上」はかなり極端な値動きです。過去に実際に発生した回数（n）が")
    lines.append("  少ない場合、その数字は偶然の産物である可能性が高く、再現性は保証されません。")
    lines.append("- 上の「銘柄別の内訳」で、到達が特定の1〜2銘柄に極端に偏っている場合、")
    lines.append("  それは「シグナルのエッジ」ではなく「その銘柄固有のイベント（上場・提携発表等）」")
    lines.append("  を拾っているだけの可能性が高いです。")
    lines.append("- 手数料・スリッページは考慮していません。")
    lines.append("- 本レポートは過去データの機械的な集計であり、投資助言ではありません。")
    lines.append("  投資判断・資金管理はご自身の責任で行ってください。")

    with open(REPORT_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"\n{BY_SCORE_CSV} / {BY_COMBO_CSV} / {BY_SYMBOL_CSV} / {COVERAGE_CSV} / {REPORT_MD} を保存しました。")


if __name__ == "__main__":
    main()
