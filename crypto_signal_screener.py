#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
急騰シグナル・スクリーニング + バックテスト + 通知 (v2)
=====================================================
GitHub Actions などのスケジューラ上で定期実行される想定のスクリプトです。

やること:
  1. 対象銘柄の価格・出来高データを取得
  2. 4つのシグナル（出来高急増／ボラティリティ収縮／ゴールデンクロス／RSI反発）を
     過去の全期間にわたって計算（未来のデータを見ない = ルックアヘッドバイアスなし）
  3. バックテスト: 「シグナルが N 個重なった時、その後 HORIZON_BARS 本後に
     何%動いたか」を過去データ全体で集計し、精度の目安を出す
  4. 直近バーでシグナルが ALERT_MIN_SCORE 個以上重なっている銘柄を検出し、
     前回まで検出されていなかった「新規」のものだけ ntfy.sh 経由で通知
  5. 状態（前回のアラート銘柄）を alert_state.json に保存し、次回実行時に比較

★ 重要な注意
- バックテストはあくまで過去データ上の集計であり、将来の的中を保証しません。
- ダマシ（フェイクシグナル）は多く発生します。
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

SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "XRPUSDT", "BNBUSDT", "SOLUSDT",
    "ADAUSDT", "DOGEUSDT", "TRXUSDT", "AVAXUSDT", "LINKUSDT",
    "DOTUSDT", "LTCUSDT", "BCHUSDT", "XLMUSDT", "ATOMUSDT",
    "ETCUSDT", "FILUSDT", "APTUSDT", "NEARUSDT", "ARBUSDT",
    "OPUSDT", "SUIUSDT", "SHIBUSDT", "UNIUSDT", "AAVEUSDT",
    "SANDUSDT", "MANAUSDT", "AXSUSDT", "ENJUSDT", "CHZUSDT",
]

INTERVAL = "1h"            # ローソク足の間隔
LOOKBACK_BARS = 500         # 取得本数（Binanceの上限は1000）

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

# --- バックテスト設定 ---
HORIZON_BARS = 24            # シグナル発生から何本先の値動きを見るか（1h足なら24=約1日後）
SUCCESS_THRESHOLD_PCT = 3.0  # この%以上の上昇を「的中」とみなす

# --- 通知設定 ---
ALERT_MIN_SCORE = int(os.environ.get("ALERT_MIN_SCORE", "3"))
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")

STATE_FILE = "alert_state.json"
RESULT_CSV = "signals_result.csv"
BACKTEST_CSV = "backtest_report.csv"

BASE_URL = "https://api.binance.com"


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

    # 1. 出来高スパイク（直近を除いた過去平均と比較）
    vol_avg = volume.rolling(VOLUME_AVG_WINDOW).mean()
    vol_avg_prev = vol_avg.shift(1)
    volume_spike = volume >= (vol_avg_prev * VOLUME_SPIKE_MULT)
    volume_ratio = volume / vol_avg_prev

    # 2. ボリンジャーバンド収縮（過去 BB_SQUEEZE_WINDOW 本の中での下位%）
    ma = close.rolling(BB_WINDOW).mean()
    std = close.rolling(BB_WINDOW).std()
    upper = ma + BB_STD * std
    lower = ma - BB_STD * std
    bandwidth = (upper - lower) / ma
    bw_threshold = bandwidth.rolling(BB_SQUEEZE_WINDOW, min_periods=60).quantile(BB_SQUEEZE_PERCENTILE)
    bb_squeeze = bandwidth <= bw_threshold

    # 3. ゴールデンクロス（直近バーで発生）
    ma_short = close.rolling(MA_SHORT).mean()
    ma_long = close.rolling(MA_LONG).mean()
    diff = ma_short - ma_long
    golden_cross = (diff.shift(1) <= 0) & (diff > 0)

    # 4. RSI 反発
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
# バックテスト
# ============================================================

def backtest_rows(sig: pd.DataFrame) -> list:
    """score>=1 だった各バーについて、HORIZON_BARS 本先までの値動き(%)を記録"""
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
        fwd_ret = (c1 - c0) / c0 * 100
        rows.append((int(s), fwd_ret))
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
# 通知（ntfy.sh）
# ============================================================

def load_state() -> set:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()


def save_state(symbols: set):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(symbols), f, ensure_ascii=False)


def send_notification(new_alerts: set, latest_rows: dict):
    if not NTFY_TOPIC:
        print("NTFY_TOPIC が未設定のため通知をスキップしました。")
        return
    lines = []
    for sym in sorted(new_alerts):
        r = latest_rows.get(sym, {})
        lines.append(f"{sym}: score={r.get('score')} price={r.get('price')} rsi={r.get('rsi')}")
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

    latest_rows = {}
    all_backtest_rows = []

    for i, symbol in enumerate(SYMBOLS, 1):
        try:
            df = fetch_klines(symbol, INTERVAL, LOOKBACK_BARS)
            min_needed = max(BB_WINDOW, MA_LONG, RSI_WINDOW, 60) + HORIZON_BARS + 5
            if len(df) < min_needed:
                print(f"[{i}/{len(SYMBOLS)}] {symbol}: データ不足のためスキップ")
                continue

            sig = compute_signal_series(df)

            # --- バックテスト用データ収集 ---
            all_backtest_rows.extend(backtest_rows(sig))

            # --- 直近バーの状態 ---
            last = sig.iloc[-1]
            price = float(df["close"].iloc[-1])
            bars_24h = 24 if INTERVAL == "1h" else 1
            pct24 = None
            if len(df) > bars_24h:
                past_price = float(df["close"].iloc[-bars_24h - 1])
                if past_price > 0:
                    pct24 = round((price - past_price) / past_price * 100, 2)

            latest_rows[symbol] = {
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
            print(f"[{i}/{len(SYMBOLS)}] {symbol:12s} score={latest_rows[symbol]['score']}")
        except Exception as e:
            print(f"[{i}/{len(SYMBOLS)}] {symbol}: エラー ({e})")
        time.sleep(0.15)

    if not latest_rows:
        print("結果がありません。処理を終了します。")
        return

    # --- バックテストレポート ---
    print("\n" + "=" * 70)
    print(f"■ バックテスト結果（{HORIZON_BARS}本先までの値動き / {SUCCESS_THRESHOLD_PCT}%以上上昇を的中とみなす）")
    print("=" * 70)
    bt = aggregate_backtest(all_backtest_rows)
    if len(bt):
        print(bt.to_string(index=False))
        bt.insert(0, "run_at_utc", datetime.now(timezone.utc).isoformat())
        header_needed = not os.path.exists(BACKTEST_CSV)
        bt.to_csv(BACKTEST_CSV, mode="a", header=header_needed, index=False, encoding="utf-8-sig")
    else:
        print("バックテスト対象データが不足しています。")

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
    current_alerts = {s for s, v in latest_rows.items() if v["score"] >= ALERT_MIN_SCORE}
    prev_alerts = load_state()
    new_alerts = current_alerts - prev_alerts
    if new_alerts:
        print(f"\n新規アラート: {', '.join(sorted(new_alerts))}")
        send_notification(new_alerts, latest_rows)
    else:
        print("\n新規アラートなし（既に通知済み、または該当なし）")
    save_state(current_alerts)

    print(f"\n結果を {RESULT_CSV} に保存しました。")
    print("※ 過去パターンとの一致を示すものであり、将来の値動きを保証するものではありません。")


if __name__ == "__main__":
    main()
