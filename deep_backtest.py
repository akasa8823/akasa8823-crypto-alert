#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
長期バックテストレポート生成ツール
=====================================================
crypto_signal_screener.py が使っているのと全く同じシグナル定義
（出来高急増／ボラティリティ収縮／ゴールデンクロス／RSI反発）を、
取得できる限り長い過去データに対して適用し、

  「このシグナルが過去に出たとき、実際どれくらいの確率で・
   平均何%くらい値上がりしていたか」

を集計するワンショットのレポートツールです。ライブ運用のスクリプトとは
独立していて、手動実行（workflow_dispatch）でのみ動きます。

★ 手法
- 4時間足を使用（1本=4時間）。ライブ運用と同じ「24時間後（=6本後）に
  何%動いたか」を検証するため、時間軸のスケールを揃えています。
- 各銘柄について、取得できる限り過去にさかのぼってローソク足を取得
  （Binanceにデータが存在する限り、後述の MAX_PAGES まで）。
- 未来のデータは一切参照しないベクトル化計算（crypto_signal_screener.py
  の compute_signal_series をそのまま再利用）。
- シグナルが1つでも立っていたすべての過去の時点について、そこから
  HORIZON_BARS 本先までの値動きを記録し、
    - スコア別（0〜4個のシグナルが重なった場合）
    - シグナルの種類別（そのシグナル単体が該当していた場合）
  で、的中率・平均リターン・中央値リターンを集計します。

★ 重要な注意
- これは過去データの統計であり、将来の値動きを保証するものではありません。
- 相場のレジーム（強気/弱気相場など）によって成績は大きく変動します。
- サンプル数が少ない銘柄・シグナルの数字は参考程度に見てください。
- 投資判断・資金管理はご自身の責任で行ってください。
"""

import os
import sys
import time
from datetime import datetime, timezone

try:
    import requests
    import pandas as pd
except ImportError:
    print("必要なライブラリがありません: pip install -r requirements.txt")
    sys.exit(1)

# crypto_signal_screener.py のシグナル計算ロジックをそのまま再利用する
import crypto_signal_screener as core

# ============================================================
# 設定
# ============================================================

SYMBOLS = core.SYMBOLS

DEEP_INTERVAL = "4h"          # 4時間足（長期間を効率よく取得するため）
PAGE_LIMIT = 1000              # 1回のAPI呼び出しで取得する本数（Binanceの上限）
MAX_PAGES = int(os.environ.get("DEEP_MAX_PAGES", "30"))  # 30ページ=最大約13.7年分
PAGE_SLEEP_SEC = 0.2

# ライブ運用と同じ「24時間後」を見るため、4時間足なら6本先
BARS_PER_DAY = 24 // 4
HORIZON_BARS = int(os.environ.get("DEEP_HORIZON_BARS", str(BARS_PER_DAY * 1)))  # 24時間後
SUCCESS_THRESHOLD_PCT = float(os.environ.get("DEEP_SUCCESS_THRESHOLD_PCT", str(core.SUCCESS_THRESHOLD_PCT)))

BY_SCORE_CSV = "deep_backtest_by_score.csv"
BY_SIGNAL_CSV = "deep_backtest_by_signal.csv"
COVERAGE_CSV = "deep_backtest_coverage.csv"
REPORT_MD = "deep_backtest_report.md"

BASE_URL = core.BASE_URL


# ============================================================
# データ取得（過去にさかのぼってページング取得）
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


def fetch_klines_deep(symbol: str, interval: str, max_pages: int, page_limit: int) -> pd.DataFrame:
    """過去にさかのぼれるだけさかのぼって取得する（endTimeを遡らせて繰り返し取得）"""
    frames = []
    end_time_ms = None
    for _ in range(max_pages):
        params = {"symbol": symbol, "interval": interval, "limit": page_limit}
        if end_time_ms is not None:
            params["endTime"] = end_time_ms
        resp = requests.get(f"{BASE_URL}/api/v3/klines", params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        if not data:
            break
        frames.append(_klines_json_to_df(data))
        if len(data) < page_limit:
            break  # これ以上過去のデータがない
        earliest_open_ms = data[0][0]
        end_time_ms = earliest_open_ms - 1
        time.sleep(PAGE_SLEEP_SEC)
    if not frames:
        return pd.DataFrame()
    full = pd.concat(frames[::-1], ignore_index=True)
    full = full.drop_duplicates(subset="open_time").sort_values("open_time").reset_index(drop=True)
    return full


# ============================================================
# バックテスト集計（スコア別・シグナル別、未来は見ない）
# ============================================================

def backtest_rows_detailed(sig: pd.DataFrame, horizon_bars: int) -> list:
    n = len(sig)
    if n <= horizon_bars:
        return []
    close = sig["close"].to_numpy()
    score = sig["score"].to_numpy()
    vs = sig["volume_spike"].to_numpy()
    bb = sig["bb_squeeze"].to_numpy()
    gc = sig["golden_cross"].to_numpy()
    rr = sig["rsi_rebound"].to_numpy()
    rows = []
    for i in range(n - horizon_bars):
        s = score[i]
        c0 = close[i]
        c1 = close[i + horizon_bars]
        if c0 <= 0:
            continue
        ret = (c1 - c0) / c0 * 100
        # score==0（シグナル無し）の行も含める。これが「ベースライン」（比較対象）になる。
        rows.append((int(s), bool(vs[i]), bool(bb[i]), bool(gc[i]), bool(rr[i]), ret))
    return rows


SIGNAL_LABELS = core.SIGNAL_LABELS if hasattr(core, "SIGNAL_LABELS") else {
    "volume_spike": "出来高急増",
    "bb_squeeze": "ボラティリティ収縮",
    "golden_cross": "ゴールデンクロス",
    "rsi_rebound": "RSI反発",
}


def main():
    print(f"実行時刻(UTC): {datetime.now(timezone.utc).isoformat()}")
    print(f"対象銘柄: {len(SYMBOLS)} / 足種: {DEEP_INTERVAL} / 最大ページ数: {MAX_PAGES} (最大約{MAX_PAGES * PAGE_LIMIT * 4 / 24 / 365:.1f}年分)")
    print(f"検証ホライズン: {HORIZON_BARS}本先（{HORIZON_BARS * 4}時間後） / 的中ライン: {SUCCESS_THRESHOLD_PCT}%以上")
    print("-" * 70)

    all_rows = []  # (symbol, score, vs, bb, gc, rr, ret)
    coverage = []

    for i, symbol in enumerate(SYMBOLS, 1):
        try:
            df = fetch_klines_deep(symbol, DEEP_INTERVAL, MAX_PAGES, PAGE_LIMIT)
            if len(df) < 100:
                print(f"[{i}/{len(SYMBOLS)}] {symbol}: データ不足のためスキップ ({len(df)}本)")
                continue

            sig = core.compute_signal_series(df)
            rows = backtest_rows_detailed(sig, HORIZON_BARS)
            for r in rows:
                all_rows.append((symbol,) + r)

            signal_occurrences = sum(1 for r in rows if r[0] > 0)  # score>0 のみカウント

            date_from = df["open_time"].iloc[0]
            date_to = df["open_time"].iloc[-1]
            years = (date_to - date_from).days / 365.25
            coverage.append({
                "symbol": symbol, "bars": len(df),
                "date_from": str(date_from.date()), "date_to": str(date_to.date()),
                "years": round(years, 2), "signal_occurrences": signal_occurrences,
            })
            print(f"[{i}/{len(SYMBOLS)}] {symbol:12s} {len(df):>5}本 ({years:5.2f}年分)  シグナル発生{signal_occurrences:>4}回")
        except Exception as e:
            print(f"[{i}/{len(SYMBOLS)}] {symbol}: エラー ({e})")
        time.sleep(0.1)

    if not all_rows:
        print("データが取得できませんでした。処理を終了します。")
        return

    df_all = pd.DataFrame(all_rows, columns=["symbol", "score", "volume_spike", "bb_squeeze", "golden_cross", "rsi_rebound", "ret"])
    cov_df = pd.DataFrame(coverage).sort_values("years", ascending=False)
    cov_df.to_csv(COVERAGE_CSV, index=False, encoding="utf-8-sig")

    total_years_span = cov_df["years"].max() if len(cov_df) else 0
    total_bars = int(cov_df["bars"].sum()) if len(cov_df) else 0

    # --- ベースライン（シグナル無し = score 0 の時の成績） ---
    baseline_sub = df_all[df_all["score"] == 0]
    if len(baseline_sub) > 0:
        baseline_win_rate = (baseline_sub["ret"] >= SUCCESS_THRESHOLD_PCT).mean() * 100
        baseline_mean = baseline_sub["ret"].mean()
        baseline_median = baseline_sub["ret"].median()
        baseline_n = len(baseline_sub)
    else:
        baseline_win_rate = baseline_mean = baseline_median = 0.0
        baseline_n = 0

    # --- スコア別集計 ---
    by_score = []
    for lvl in sorted(df_all["score"].unique()):
        sub = df_all[df_all["score"] == lvl]
        win_rate = (sub["ret"] >= SUCCESS_THRESHOLD_PCT).mean() * 100
        by_score.append({
            "score": int(lvl),
            "n": len(sub),
            "win_rate_pct": round(win_rate, 1),
            "mean_return_pct": round(sub["ret"].mean(), 2),
            "median_return_pct": round(sub["ret"].median(), 2),
            "edge_over_baseline_pct": round(win_rate - baseline_win_rate, 1),
        })
    by_score_df = pd.DataFrame(by_score).sort_values("score")
    by_score_df.to_csv(BY_SCORE_CSV, index=False, encoding="utf-8-sig")

    # --- シグナル種類別集計（単体で該当していた場合） ---
    by_signal = []
    for col, label in SIGNAL_LABELS.items():
        sub = df_all[df_all[col] == True]  # noqa: E712
        if len(sub) == 0:
            by_signal.append({"signal": col, "label": label, "n": 0, "win_rate_pct": None, "mean_return_pct": None, "median_return_pct": None, "edge_over_baseline_pct": None})
            continue
        win_rate = (sub["ret"] >= SUCCESS_THRESHOLD_PCT).mean() * 100
        by_signal.append({
            "signal": col, "label": label, "n": len(sub),
            "win_rate_pct": round(win_rate, 1),
            "mean_return_pct": round(sub["ret"].mean(), 2),
            "median_return_pct": round(sub["ret"].median(), 2),
            "edge_over_baseline_pct": round(win_rate - baseline_win_rate, 1),
        })
    by_signal_df = pd.DataFrame(by_signal)
    by_signal_df.to_csv(BY_SIGNAL_CSV, index=False, encoding="utf-8-sig")

    overall_sub = df_all[df_all["score"] > 0]
    overall_win = (overall_sub["ret"] >= SUCCESS_THRESHOLD_PCT).mean() * 100 if len(overall_sub) else 0.0

    print("\n" + "=" * 70)
    print(f"■ ベースライン（シグナル無し）: 的中率 {baseline_win_rate:.1f}% / 平均 {baseline_mean:.2f}% / 中央値 {baseline_median:.2f}% (n={baseline_n})")
    print("=" * 70)
    print("\n" + "=" * 70)
    print("■ スコア別（edge_over_baseline_pct = ベースラインとの的中率の差）")
    print("=" * 70)
    print(by_score_df.to_string(index=False))
    print("\n" + "=" * 70)
    print("■ シグナル種類別（単体該当時）")
    print("=" * 70)
    print(by_signal_df.to_string(index=False))
    print(f"\n全体的中率（score>0の全ケース）: {overall_win:.1f}% (n={len(overall_sub)})  / ベースライン比 edge: {overall_win - baseline_win_rate:+.1f}pt")

    # --- Markdownレポート ---
    lines = []
    lines.append("# 長期バックテストレポート\n")
    lines.append(f"- 実行日時(UTC): {datetime.now(timezone.utc).isoformat()}")
    lines.append(f"- 対象銘柄数: {len(SYMBOLS)}（データ取得成功: {len(cov_df)}）")
    lines.append(f"- 使用した足: {DEEP_INTERVAL}（4時間足）")
    lines.append(f"- 検証ホライズン: シグナル発生から {HORIZON_BARS * 4} 時間後の値動き")
    lines.append(f"- 的中の定義: {SUCCESS_THRESHOLD_PCT}% 以上の上昇")
    lines.append(f"- 最長データ期間: 約 {total_years_span:.1f} 年（銘柄により異なる。詳細は `{COVERAGE_CSV}`）")
    lines.append(f"- 取得した総本数: {total_bars:,} 本\n")

    lines.append("## ベースライン（シグナルが一切無いときの成績）\n")
    lines.append(
        f"シグナルに意味があるかどうかは、**「シグナル無し」の成績と比べてどれだけ良いか**でしか判断できません。"
        f"以下がその比較対象（ベースライン）です。\n"
    )
    lines.append(f"- ベースライン的中率: **{baseline_win_rate:.1f}%** / 平均リターン: {baseline_mean:.2f}% / 中央値: {baseline_median:.2f}% (n={baseline_n:,})\n")
    lines.append(
        "以下の表の `edge_over_baseline_pct` は「そのスコア／シグナルの的中率 − ベースライン的中率」です。"
        "**プラスが大きいほど、シグナルに本当の優位性がある**ことを意味し、0前後や大差ない場合は"
        "「シグナルがあってもなくても結果は変わらない」ことを意味します。\n"
    )

    lines.append("## スコア別の成績（シグナルが何個重なっていたか）\n")
    lines.append("| スコア | 発生回数 | 的中率 | ベースライン比(edge) | 平均リターン | 中央値リターン |")
    lines.append("|---|---|---|---|---|---|")
    for _, r in by_score_df.iterrows():
        edge = r['edge_over_baseline_pct']
        edge_str = f"{edge:+.1f}pt" if pd.notna(edge) else "-"
        tag = "0（ベースライン）" if int(r['score']) == 0 else str(int(r['score']))
        lines.append(f"| {tag} | {int(r['n'])} | {r['win_rate_pct']}% | {edge_str} | {r['mean_return_pct']}% | {r['median_return_pct']}% |")

    lines.append("\n## シグナル種類別の成績（そのシグナルが単体で該当していた場合）\n")
    lines.append("| シグナル | 発生回数 | 的中率 | ベースライン比(edge) | 平均リターン | 中央値リターン |")
    lines.append("|---|---|---|---|---|---|")
    for _, r in by_signal_df.iterrows():
        n = int(r["n"])
        if n == 0:
            lines.append(f"| {r['label']} | 0 | - | - | - | - |")
        else:
            edge = r['edge_over_baseline_pct']
            edge_str = f"{edge:+.1f}pt" if pd.notna(edge) else "-"
            lines.append(f"| {r['label']} | {n} | {r['win_rate_pct']}% | {edge_str} | {r['mean_return_pct']}% | {r['median_return_pct']}% |")

    lines.append(f"\n**全体的中率（score>0の全ケース）: {overall_win:.1f}%**（n={len(overall_sub):,}）"
                  f" / **ベースライン比 edge: {overall_win - baseline_win_rate:+.1f}pt**\n")

    lines.append("## 銘柄別のデータ取得状況\n")
    lines.append("| 銘柄 | 取得期間 | 年数 | シグナル発生回数 |")
    lines.append("|---|---|---|---|")
    for _, r in cov_df.iterrows():
        lines.append(f"| {r['symbol']} | {r['date_from']} 〜 {r['date_to']} | {r['years']} | {int(r['signal_occurrences'])} |")

    lines.append("\n## 注意事項\n")
    lines.append("- これは過去データを機械的に集計したものであり、将来の的中を保証するものではありません。")
    lines.append("- 相場全体が上昇トレンドの期間が長い銘柄ほど、的中率・平均リターンが高く出やすい傾向があります（銘柄選定バイアス）。")
    lines.append("- サンプル数（発生回数）が少ないシグナル・銘柄の数字は誤差が大きいため、参考程度にしてください。")
    lines.append("- 手数料・スリッページは考慮していません。")
    lines.append("- 本レポートは投資助言ではありません。投資判断はご自身の責任で行ってください。")

    with open(REPORT_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"\n{BY_SCORE_CSV} / {BY_SIGNAL_CSV} / {COVERAGE_CSV} / {REPORT_MD} を保存しました。")


if __name__ == "__main__":
    main()
