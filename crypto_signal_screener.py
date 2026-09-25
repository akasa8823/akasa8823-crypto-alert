#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
急騰シグナル・スクリーニング + バックテスト + 実績追跡 + 通知 (v3)
=====================================================
GitHub Actions などのスケジューラ上で定期実行される想定のスクリプトです。

やること:
  1. 対象銘柄の価格・出来高データを取得
  2. 4つのシグナル（出来高急増／ボラティリティ収縮／ゴールデンクロス／RSI反発）を
     過去の全期間にわたって計算（未来のデータを見ない = ルックアヘッドバイアスなし）
  3. バックテスト: 直近取得データ全体で「シグナルがN個重なった時、
     その後 HORIZON_BARS 本後に何%動いたか」を集計（参考値）
  4. 実績追跡（新機能）:
     - シグナルが新規発生した銘柄を alerts_log.csv に記録
     - 記録から HORIZON_BARS 本経過したものは、実際の値動きを検証して
       的中/不的中・リターンを書き戻す
     - こうして「本当に検出→通知したシグナル」の的中率が回を追うごとに
       蓄積されていく（synthetic backtestより信頼できる実績データ）
  5. 直近バーで「出来高急増 かつ RSI反発」が新たに成立した銘柄（BTCUSDTのみ）
     のうち、前回まで検出されていなかった「新規」のものだけ ntfy.sh 経由で通知
     （deep_backtest.py / notify_simulation.py での長期検証で、このシグナルの
     組み合わせが最も安定してエッジ（優位性）を持つことが確認されたため）

★ 重要な注意
- バックテスト・実績追跡はいずれも過去データ上の集計であり、将来の的中を
  保証しません。ダマシ（フェイクシグナル）は多く発生します。
- 投資判断・資金管理はご自身の責任で行ってください。本スクリプトおよび
  作成者は投資助言を行うものではありません。
"""

import os
import sys
import json
import time
from datetime import datetime, timezone

try:
    import requests
    import pandas as pd
except ImportError:
    print("必要なライブラリがありません: pip install -r requirements.txt")
    sys.exit(1)

# ============================================================
# 設定（必要に応じて編集してください）
# ============================================================

# deep_backtest.py / notify_simulation.py での検証の結果、BTCUSDT単体・
# 「出来高急増 かつ RSI反発」の組み合わせが、最も安定してエッジ（優位性）が
# 確認できたため、対象銘柄をBTCUSDTのみに絞っています。
SYMBOLS = ["BTCUSDT"]

INTERVAL = "1h"            # ローソク足の間隔
LOOKBACK_BARS = 500         # 取得本数（Binanceの上限は1000）
INTERVAL_HOURS = {"1h": 1, "4h": 4, "1d": 24}.get(INTERVAL, 1)

# --- シグナル判定パラメータ ---
VOLUME_SPIKE_MULT = 2.5
VOLUME_AVG_WINDOW = 20
BB_WINDOW = 20
BB_STD = 2.0
BB_SQUEEZE_WINDOW = 200     # バンド幅の「収縮」を判定する過去参照期間（未来は見ない）
BB_SQUEEZE_PERCENTILE = 0.15
MA_SHORT = 9
MA_LONG = 26
RSI_WINDOW = 14
RSI_OVERSOLD = 35
RSI_LOOKBACK = 3

# --- バックテスト / 実績検証 設定 ---
HORIZON_BARS = 24            # シグナル発生から何本先の値動きを見るか（1h足なら24=約1日後）
SUCCESS_THRESHOLD_PCT = 3.0  # この%以上の上昇を「的中」とみなす

# --- 通知設定 ---
# 通知条件: deep_backtest.py の組み合わせ別エッジ分析で最も優位性が確認できた
# 「出来高急増 かつ RSI反発」の組み合わせが新規に成立した場合のみ通知する。
# （スコア単純合計にはエッジが薄いことが検証で判明したため、score>=Nベースの
#   閾値通知（旧ALERT_MIN_SCORE）から切り替えた）
NOTIFY_REQUIRE_VOLUME_SPIKE = os.environ.get("NOTIFY_REQUIRE_VOLUME_SPIKE", "1") != "0"
NOTIFY_REQUIRE_RSI_REBOUND = os.environ.get("NOTIFY_REQUIRE_RSI_REBOUND", "1") != "0"
# target_reach_analysis.py での検証で、+5%より+10%まで持った方がリスクを
# ほとんど増やさずリターンを大きく伸ばせることが確認できたため、通知本文に
# +10%の目標価格を参考として添える（実際の売買判断はご自身で行ってください）
NOTIFY_TAKE_PROFIT_PCT = float(os.environ.get("NOTIFY_TAKE_PROFIT_PCT", "10.0"))
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")

STATE_FILE = "alert_state.json"
RESULT_CSV = "signals_result.csv"
BACKTEST_CSV = "backtest_report.csv"
ALERTS_LOG_CSV = "alerts_log.csv"
ACCURACY_CSV = "accuracy_report.csv"
SIGNAL_ACCURACY_CSV = "signal_accuracy.csv"

ALERTS_LOG_COLUMNS = [
    "id", "symbol", "signal_at_utc", "score",
    "volume_spike", "bb_squeeze", "golden_cross", "rsi_rebound",
    "price_at_signal", "resolve_at_utc",
    "resolved", "resolved_at_utc", "price_resolved", "outcome_pct", "hit",
]

BASE_URL = "https://data-api.binance.vision"


# ============================================================
# データ取得
# ============================================================

def fetch_klines(symbol: str, interval: str, limit: int) -> pd.DataFrame:
    url = f"{BASE_URL}/api/v3/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    cols = ["open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades",
            "taker_buy_base", "taker_buy_quote", "ignore"]
    df = pd.DataFrame(data, columns=cols)
    for c in ["open", "high", "low", "close", "volume", "quote_volume"]:
        df[c] = df[c].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    return df


# ============================================================
# シグナル計算（全期間・ベクトル化・未来のデータは参照しない）
# ============================================================

def compute_signal_series(df: pd.DataFrame) -> pd.DataFrame:
    close = df["close"]
    volume = df["volume"]

    vol_avg = volume.rolling(VOLUME_AVG_WINDOW).mean()
    vol_avg_prev = vol_avg.shift(1)
    volume_spike = volume >= (vol_avg_prev * VOLUME_SPIKE_MULT)
    volume_ratio = volume / vol_avg_prev

    ma = close.rolling(BB_WINDOW).mean()
    std = close.rolling(BB_WINDOW).std()
    upper = ma + BB_STD * std
    lower = ma - BB_STD * std
    bandwidth = (upper - lower) / ma
    bw_threshold = bandwidth.rolling(BB_SQUEEZE_WINDOW, min_periods=60).quantile(BB_SQUEEZE_PERCENTILE)
    bb_squeeze = bandwidth <= bw_threshold

    ma_short = close.rolling(MA_SHORT).mean()
    ma_long = close.rolling(MA_LONG).mean()
    diff = ma_short - ma_long
    golden_cross = (diff.shift(1) <= 0) & (diff > 0)

    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(RSI_WINDOW).mean()
    avg_loss = loss.rolling(RSI_WINDOW).mean()
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    rsi = 100 - (100 / (1 + rs))
    prior_min_rsi = rsi.shift(1).rolling(RSI_LOOKBACK).min()
    rsi_rebound = (prior_min_rsi <= RSI_OVERSOLD) & (rsi > prior_min_rsi)

    volume_spike = volume_spike.fillna(False)
    bb_squeeze = bb_squeeze.fillna(False)
    golden_cross = golden_cross.fillna(False)
    rsi_rebound = rsi_rebound.fillna(False)

    score = (
        volume_spike.astype(int)
        + bb_squeeze.astype(int)
        + golden_cross.astype(int)
        + rsi_rebound.astype(int)
    )

    return pd.DataFrame({
        "close": close,
        "volume_ratio": volume_ratio,
        "volume_spike": volume_spike,
        "bb_squeeze": bb_squeeze,
        "bandwidth_pct": bandwidth * 100,
        "golden_cross": golden_cross,
        "rsi": rsi,
        "rsi_rebound": rsi_rebound,
        "score": score,
    })


# ============================================================
# バックテスト（直近取得データでの参考集計）
# ============================================================

def backtest_rows(sig: pd.DataFrame) -> list:
    n = len(sig)
    if n <= HORIZON_BARS:
        return []
    close = sig["close"].to_numpy()
    scores = sig["score"].to_numpy()
    rows = []
    for i in range(n - HORIZON_BARS):
        s = scores[i]
        if s <= 0:
            continue
        c0 = close[i]
        c1 = close[i + HORIZON_BARS]
        if c0 <= 0:
            continue
        rows.append((int(s), (c1 - c0) / c0 * 100))
    return rows


def aggregate_backtest(all_rows: list) -> pd.DataFrame:
    if not all_rows:
        return pd.DataFrame(columns=["score", "n", "win_rate_pct", "mean_return_pct", "median_return_pct"])
    df = pd.DataFrame(all_rows, columns=["score", "fwd_ret_pct"])
    out = []
    for lvl in sorted(df["score"].unique()):
        sub = df[df["score"] == lvl]
        out.append({
            "score": lvl,
            "n": len(sub),
            "win_rate_pct": round((sub["fwd_ret_pct"] >= SUCCESS_THRESHOLD_PCT).mean() * 100, 1),
            "mean_return_pct": round(sub["fwd_ret_pct"].mean(), 2),
            "median_return_pct": round(sub["fwd_ret_pct"].median(), 2),
        })
    return pd.DataFrame(out).sort_values("score")


# ============================================================
# 実績追跡（実際に検出したシグナルの記録と後日検証）
# ============================================================

def load_alerts_log() -> pd.DataFrame:
    if os.path.exists(ALERTS_LOG_CSV):
        try:
            df = pd.read_csv(ALERTS_LOG_CSV)
            for col in ["resolved", "hit", "volume_spike", "bb_squeeze", "golden_cross", "rsi_rebound"]:
                if col in df.columns:
                    df[col] = df[col].map(
                        lambda v: True if str(v) == "True" else (False if str(v) == "False" else v)
                    )
            for col in ALERTS_LOG_COLUMNS:
                if col not in df.columns:
                    df[col] = None
            return df[ALERTS_LOG_COLUMNS]
        except Exception as e:
            print(f"alerts_log.csv の読み込みに失敗: {e}")
    return pd.DataFrame(columns=ALERTS_LOG_COLUMNS)


def save_alerts_log(df: pd.DataFrame):
    df.to_csv(ALERTS_LOG_CSV, index=False, encoding="utf-8-sig")


def append_alert_log(log: pd.DataFrame, symbol: str, signal_time, row: dict) -> pd.DataFrame:
    entry_id = f"{symbol}-{int(pd.Timestamp(signal_time).timestamp())}"
    if len(log) and (log["id"] == entry_id).any():
        return log
    resolve_at = pd.Timestamp(signal_time) + pd.Timedelta(hours=INTERVAL_HOURS * HORIZON_BARS)
    new_row = {
        "id": entry_id,
        "symbol": symbol,
        "signal_at_utc": str(signal_time),
        "score": row["score"],
        "volume_spike": row["volume_spike"],
        "bb_squeeze": row["bb_squeeze"],
        "golden_cross": row["golden_cross"],
        "rsi_rebound": row["rsi_rebound"],
        "price_at_signal": row["price"],
        "resolve_at_utc": str(resolve_at),
        "resolved": False,
        "resolved_at_utc": None,
        "price_resolved": None,
        "outcome_pct": None,
        "hit": None,
    }
    return pd.concat([log, pd.DataFrame([new_row])], ignore_index=True)


def resolve_alerts_for_symbol(log: pd.DataFrame, symbol: str, price_df: pd.DataFrame) -> pd.DataFrame:
    if len(log) == 0:
        return log
    mask = (log["symbol"] == symbol) & (log["resolved"] != True)  # noqa: E712
    if not mask.any():
        return log
    times = pd.to_datetime(price_df["open_time"], utc=True).reset_index(drop=True)
    closes = price_df["close"].reset_index(drop=True)
    for idx in log[mask].index:
        try:
            resolve_at = pd.Timestamp(log.at[idx, "resolve_at_utc"])
            if resolve_at.tzinfo is None:
                resolve_at = resolve_at.tz_localize("UTC")
        except Exception:
            continue
        candidates = times[times >= resolve_at]
        if len(candidates) == 0:
            continue  # まだその時刻に到達していない
        pos = candidates.index[0]
        price_resolved = float(closes.loc[pos])
        price_at_signal = float(log.at[idx, "price_at_signal"])
        if price_at_signal <= 0:
            continue
        outcome_pct = round((price_resolved - price_at_signal) / price_at_signal * 100, 2)
        log.at[idx, "resolved"] = True
        log.at[idx, "resolved_at_utc"] = str(times.loc[pos])
        log.at[idx, "price_resolved"] = price_resolved
        log.at[idx, "outcome_pct"] = outcome_pct
        log.at[idx, "hit"] = outcome_pct >= SUCCESS_THRESHOLD_PCT
    return log


def print_accuracy_report(log: pd.DataFrame):
    print("\n" + "=" * 70)
    print("■ 実績追跡（実際に検出したシグナルのその後・蓄積データ）")
    print("=" * 70)
    if len(log) == 0:
        print("まだ記録がありません。")
        return
    resolved = log[log["resolved"] == True].copy()  # noqa: E712
    pending = log[log["resolved"] != True]  # noqa: E712
    print(f"記録件数: {len(log)}（検証済み {len(resolved)} / 検証待ち {len(pending)}）")
    if len(resolved) == 0:
        print("検証済みデータがまだありません（シグナル発生からHORIZON_BARS本経過すると検証されます）。")
        return
    resolved["outcome_pct"] = resolved["outcome_pct"].astype(float)
    rows = []
    for lvl in sorted(resolved["score"].unique()):
        sub = resolved[resolved["score"] == lvl]
        win_rate = (sub["hit"] == True).mean() * 100  # noqa: E712
        rows.append({
            "score": lvl,
            "n": len(sub),
            "win_rate_pct": round(win_rate, 1),
            "mean_return_pct": round(sub["outcome_pct"].mean(), 2),
        })
        print(f"  score={lvl}: n={len(sub):>3}  的中率={win_rate:5.1f}%  平均リターン={sub['outcome_pct'].mean():6.2f}%")
    overall_win = (resolved["hit"] == True).mean() * 100  # noqa: E712
    print(f"全体的中率: {overall_win:.1f}% (n={len(resolved)})")
    pd.DataFrame(rows).to_csv(ACCURACY_CSV, index=False, encoding="utf-8-sig")

    print_signal_accuracy_report(resolved)


SIGNAL_LABELS = {
    "volume_spike": "出来高急増",
    "bb_squeeze": "ボラティリティ収縮",
    "golden_cross": "ゴールデンクロス",
    "rsi_rebound": "RSI反発",
}


def print_signal_accuracy_report(resolved: pd.DataFrame):
    """シグナルの種類ごと（単体で該当していたかどうか）の的中率を集計する。
    他のシグナルと重複していても構わず「そのシグナルが該当していた場合」で集計するため、
    複数シグナルの合算である score 別レポートより、個々のシグナルの効き目が分かりやすい。
    """
    print("\n" + "=" * 70)
    print("■ シグナル種類別の的中率（そのシグナルが該当していた場合）")
    print("=" * 70)
    if len(resolved) == 0:
        return
    rows = []
    for col, label in SIGNAL_LABELS.items():
        if col not in resolved.columns:
            continue
        sub = resolved[resolved[col] == True]  # noqa: E712
        if len(sub) == 0:
            rows.append({
                "signal": col, "label": label, "n": 0,
                "win_rate_pct": None, "mean_return_pct": None,
            })
            print(f"  {label:12s}: n=  0  (該当データなし)")
            continue
        win_rate = (sub["hit"] == True).mean() * 100  # noqa: E712
        mean_ret = sub["outcome_pct"].mean()
        rows.append({
            "signal": col,
            "label": label,
            "n": len(sub),
            "win_rate_pct": round(win_rate, 1),
            "mean_return_pct": round(mean_ret, 2),
        })
        print(f"  {label:12s}: n={len(sub):>3}  的中率={win_rate:5.1f}%  平均リターン={mean_ret:6.2f}%")
    pd.DataFrame(rows).to_csv(SIGNAL_ACCURACY_CSV, index=False, encoding="utf-8-sig")


# ============================================================
# 状態（通知済み・記録済みの銘柄セット）
# ============================================================

def load_state() -> dict:
    default = {"notify_active": [], "log_active": []}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):  # 旧バージョンとの互換
                return {"notify_active": data, "log_active": []}
            return {
                "notify_active": data.get("notify_active", []),
                "log_active": data.get("log_active", []),
            }
        except Exception:
            return default
    return default


def save_state(notify_active: set, log_active: set):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "notify_active": sorted(notify_active),
            "log_active": sorted(log_active),
        }, f, ensure_ascii=False)


# ============================================================
# 通知（ntfy.sh）
# ============================================================

def send_notification(new_alerts: set, latest_rows: dict):
    if not NTFY_TOPIC:
        print("NTFY_TOPIC が未設定のため通知をスキップしました。")
        return
    lines = []
    for sym in sorted(new_alerts):
        r = latest_rows.get(sym, {})
        price = r.get("price")
        target = round(price * (1 + NOTIFY_TAKE_PROFIT_PCT / 100), 2) if price else None
        lines.append(
            f"{sym}: price={price} 目安の利確ライン(+{NOTIFY_TAKE_PROFIT_PCT:g}%)={target} "
            f"出来高倍率={r.get('volume_ratio')}x RSI={r.get('rsi')} "
            f"(score={r.get('score')})"
        )
    body = "\n".join(lines)
    try:
        requests.post(
            f"{NTFY_SERVER}/{NTFY_TOPIC}",
            data=body.encode("utf-8"),
            headers={
                "Title": "Crypto Signal Alert",
                "Priority": "high",
                "Tags": "rotating_light",
            },
            timeout=10,
        )
        print(f"通知を送信しました: {', '.join(sorted(new_alerts))}")
    except Exception as e:
        print(f"通知送信に失敗しました: {e}")


# ============================================================
# メイン処理
# ============================================================

def main():
    print(f"実行時刻(UTC): {datetime.now(timezone.utc).isoformat()}")
    print(f"対象銘柄: {len(SYMBOLS)} / 足種: {INTERVAL} / 取得本数: {LOOKBACK_BARS}")
    print("-" * 70)

    prev_state = load_state()
    prev_notify_active = set(prev_state["notify_active"])
    prev_log_active = set(prev_state["log_active"])

    alerts_log = load_alerts_log()

    latest_rows = {}
    all_backtest_rows = []
    new_notify_active = set()
    new_log_active = set()

    for i, symbol in enumerate(SYMBOLS, 1):
        try:
            df = fetch_klines(symbol, INTERVAL, LOOKBACK_BARS)
            min_needed = max(BB_WINDOW, MA_LONG, RSI_WINDOW, 60) + HORIZON_BARS + 5
            if len(df) < min_needed:
                print(f"[{i}/{len(SYMBOLS)}] {symbol}: データ不足のためスキップ")
                continue

            sig = compute_signal_series(df)
            all_backtest_rows.extend(backtest_rows(sig))

            last = sig.iloc[-1]
            price = float(df["close"].iloc[-1])
            bars_24h = 24 if INTERVAL == "1h" else 1
            pct24 = None
            if len(df) > bars_24h:
                past_price = float(df["close"].iloc[-bars_24h - 1])
                if past_price > 0:
                    pct24 = round((price - past_price) / past_price * 100, 2)

            row = {
                "score": int(last["score"]),
                "price": price,
                "pct_change_24h_bars": pct24,
                "volume_spike": bool(last["volume_spike"]),
                "volume_ratio": round(float(last["volume_ratio"]), 2) if pd.notna(last["volume_ratio"]) else None,
                "bb_squeeze": bool(last["bb_squeeze"]),
                "bandwidth_pct": round(float(last["bandwidth_pct"]), 2) if pd.notna(last["bandwidth_pct"]) else None,
                "golden_cross": bool(last["golden_cross"]),
                "rsi": round(float(last["rsi"]), 1) if pd.notna(last["rsi"]) else None,
                "rsi_rebound": bool(last["rsi_rebound"]),
            }
            latest_rows[symbol] = row

            notify_condition = True
            if NOTIFY_REQUIRE_VOLUME_SPIKE:
                notify_condition = notify_condition and row["volume_spike"]
            if NOTIFY_REQUIRE_RSI_REBOUND:
                notify_condition = notify_condition and row["rsi_rebound"]

            if row["score"] >= 1:
                new_log_active.add(symbol)
            if notify_condition:
                new_notify_active.add(symbol)

            # 新規にシグナルが立った瞬間だけ記録（同じ状態が続く間は再記録しない）
            if symbol in new_log_active and symbol not in prev_log_active:
                signal_time = df["open_time"].iloc[-1]
                alerts_log = append_alert_log(alerts_log, symbol, signal_time, row)

            # この銘柄について、検証待ちのものがあれば結果を書き戻す
            alerts_log = resolve_alerts_for_symbol(alerts_log, symbol, df)

            print(f"[{i}/{len(SYMBOLS)}] {symbol:12s} score={row['score']}")
        except Exception as e:
            print(f"[{i}/{len(SYMBOLS)}] {symbol}: エラー ({e})")
        time.sleep(0.15)

    save_alerts_log(alerts_log)

    if not latest_rows:
        print("結果がありません。処理を終了します。")
        return

    # --- バックテストレポート（参考値） ---
    print("\n" + "=" * 70)
    print(f"■ バックテスト（直近データの参考値 / {HORIZON_BARS}本先 / {SUCCESS_THRESHOLD_PCT}%以上上昇を的中とみなす）")
    print("=" * 70)
    bt = aggregate_backtest(all_backtest_rows)
    if len(bt):
        print(bt.to_string(index=False))
        bt.insert(0, "run_at_utc", datetime.now(timezone.utc).isoformat())
        header_needed = not os.path.exists(BACKTEST_CSV)
        bt.to_csv(BACKTEST_CSV, mode="a", header=header_needed, index=False, encoding="utf-8-sig")
    else:
        print("バックテスト対象データが不足しています。")

    # --- 実績追跡レポート（本命） ---
    print_accuracy_report(alerts_log)

    # --- 直近スクリーニング結果 ---
    df_res = pd.DataFrame([{"symbol": s, **v} for s, v in latest_rows.items()])
    df_res = df_res.sort_values(["score", "volume_ratio"], ascending=[False, False])
    df_res.to_csv(RESULT_CSV, index=False, encoding="utf-8-sig")

    print("\n" + "=" * 70)
    print("■ 直近シグナル（スコアの高い順）")
    print("=" * 70)
    with pd.option_context("display.max_rows", None, "display.width", 140):
        print(df_res.to_string(index=False))

    # --- 通知判定 ---
    truly_new_notify = new_notify_active - prev_notify_active
    if truly_new_notify:
        print(f"\n新規アラート: {', '.join(sorted(truly_new_notify))}")
        send_notification(truly_new_notify, latest_rows)
    else:
        print("\n新規アラートなし（既に通知済み、または該当なし）")

    save_state(new_notify_active, new_log_active)

    print(f"\n結果を {RESULT_CSV} に保存しました。")
    print("※ 過去パターンとの一致・過去の実績を示すものであり、将来の値動きを保証するものではありません。")


if __name__ == "__main__":
    main()
