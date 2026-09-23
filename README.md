# crypto-signal-screener

過去の価格・出来高データから急騰シグナル（出来高急増／ボラティリティ収縮／
ゴールデンクロス／RSI反発）を検出し、条件が3つ以上重なった銘柄が出たら
スマホにプッシュ通知するツールです。GitHub Actions 上で自動的に定期実行されます。

**投資助言ではありません。過去パターンの機械的な抽出であり、将来の値動きを
保証するものではありません。**

## セットアップ手順は、Claudeとの会話の中の手順書を参照してください。

必要なもの:
- 無料のGitHubアカウント
- 無料のntfyアプリ（iOS/Android）

## ファイル構成

- `crypto_signal_screener.py` — 本体スクリプト（取得・シグナル計算・バックテスト・通知）
- `requirements.txt` — 依存ライブラリ
- `.github/workflows/screener.yml` — 定期実行の設定（デフォルト30分おき）
- `alert_state.json` — 直近の通知済み銘柄（自動更新されます）
- `signals_result.csv` — 直近の実行結果（自動生成、ダッシュボードで読み込み可能）
- `backtest_report.csv` — バックテストの履歴（自動追記）

## 設定の変更

- 通知の閾値: `.github/workflows/screener.yml` の `ALERT_MIN_SCORE`
- 実行頻度: `.github/workflows/screener.yml` の `cron`（UTC基準）
- 対象銘柄: `crypto_signal_screener.py` の `SYMBOLS`
- 各シグナルのしきい値: `crypto_signal_screener.py` 冒頭のパラメータ
