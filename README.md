# crypto-signal-screener

過去の価格・出来高データから急騰シグナル（出来高急増／ボラティリティ収縮／
ゴールデンクロス／RSI反発）を検出し、「出来高急増 かつ RSI反発」が新たに
成立した銘柄が出たらスマホにプッシュ通知するツールです。GitHub Actions 上で
自動的に定期実行されます。

対象銘柄は **BTCUSDT のみ**です。`deep_backtest.py`（長期再現性検証）と
`notify_simulation.py`（通知回数・利確シミュレーション）での検証の結果、
30銘柄・スコア合計ベースの通知は年に1000回超と多すぎる上に個々のスコアには
エッジ（優位性）が薄く、逆に「BTCUSDT単体 + 出来高急増とRSI反発の組み合わせ」
に絞った方が、通知頻度が現実的（月4〜5回程度）で、勝率・平均リターンとも
安定して優位性が確認できたため、この設定に切り替えています。

さらに `target_reach_analysis.py` での検証で、利確ラインを+5%ではなく+10%まで
引き上げてもBTCUSDTでは最大ドローダウンがほとんど変わらず、リターンだけが
大きく伸びることが確認できたため、通知本文には参考として**+10%の目安の
利確ライン**（`NOTIFY_TAKE_PROFIT_PCT` 環境変数、デフォルト10%）を価格つきで
表示するようにしています。ただし実際の売買・利確の判断はご自身で行ってください
（本スクリプトは通知のみで、自動売買は一切行いません）。

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
- `alerts_log.csv` — 実際に通知条件に達したアラートの記録と、24時間後の結果
- `accuracy_report.csv` / `signal_accuracy.csv` — 実運用での的中率レポート（スコア別／シグナル別、自動生成）
- `notify_performance_log.csv` — **本番の成績集計**。実際にスマホへ通知した銘柄について、
  その後 `NOTIFY_TAKE_PROFIT_PCT`（デフォルト+10%）まで到達したか、それとも
  `NOTIFY_MAX_HOLD_DAYS`（デフォルト60日）以内に到達せず期限切れになったかを
  1件ずつ記録します。列: `notified_at_utc`（通知時刻）、`entry_price`（通知時の価格）、
  `target_price`（利確目標価格）、`resolved`（決着済みか）、`hit`（利確到達したか）、
  `outcome_pct`（実際のリターン）、`days_held`（保有日数）など。実行のたびにコンソール
  にも「保有中／決着済み」の一覧と、決着済み分の利確到達率・平均リターンが表示されます。
- `notify_performance_report.md` — 上記の集計を人が読みやすいMarkdown形式にまとめた
  レポートです（実行のたびに最新の状態で作り直されます）。サマリー（通知件数・
  利確到達率・平均リターン・平均保有日数・ベスト/ワースト）、保有中の一覧表、
  決着済み履歴の一覧表を含みます。GitHub上でこのファイルを開くと、成績の推移を
  一目で確認できます。

### 決済（利確到達／期限切れ）のプッシュ通知

新規シグナル発生時の通知に加えて、`notify_performance_log.csv` に記録した保有中の
銘柄が「利確ライン到達」または「保有期限切れでの強制決済」によって決着したときにも、
その結果（エントリー価格・決済価格・リターン・保有日数）を都度プッシュ通知します。
シグナル発生の通知と同様、本文は日本語（HTTPヘッダーのみASCII安全な英語）です。

### 長期バックテスト（再現性検証）

- `deep_backtest.py` — 取得できる限り長期間（4時間足、最大約13.7年分）のデータで、
  同じシグナル条件が過去に何回発生し、その後どれくらいの確率・上昇率で
  的中していたかを検証する一括レポートツールです。
- `.github/workflows/deep_backtest.yml` — 手動実行専用のワークフロー
  （Actionsタブ → "crypto-deep-backtest" → "Run workflow"）。データ量が多いため
  数分かかります。
- 出力: `deep_backtest_by_score.csv`（スコア別）、`deep_backtest_by_signal.csv`
  （シグナル種類別）、`deep_backtest_by_combo.csv`（シグナルの組み合わせ別、
  エッジが高い順）、`deep_backtest_coverage.csv`（銘柄別データ取得状況）、
  `deep_backtest_report.md`（人が読みやすいレポート本体）。
- 各CSV・レポートには `edge_over_baseline_pct`（シグナルが一切無いときの的中率と比べた差）
  が含まれます。これがプラスで大きいほど、そのシグナルに本当の優位性（エッジ）がある
  ことを意味します。0前後なら「シグナルがあってもなくても結果は変わらない」という
  ことです。
- `deep_backtest_by_combo.csv` は4つのシグナルの全組み合わせ（15通り）のエッジを
  降順で並べたものです。上位に来る組み合わせが「実際にエッジのある入場条件」の候補
  になります（`DEEP_MIN_COMBO_N`未満の発生回数の組み合わせは自動的に除外されます）。
- `deep_backtest_by_score.csv` / `deep_backtest_by_signal.csv` は
  `accuracy_report.csv` / `signal_accuracy.csv` と同じ列構成なので、
  そのままダッシュボードの「精度レポート」パネルに読み込めます。
- 検証期間: `DEEP_MAX_PAGES`（環境変数、デフォルト30 = 最大約13.7年分）で調整可能。

### 通知回数 & 利確シミュレーション

- `notify_simulation.py` — 「出来高急増 かつ RSI反発」が同時に成立した瞬間（＝通知が
  来るタイミング）を1時間足（ライブ運用と同じ足）で再現し、年ごとの通知回数と、
  +5%利確ルールで100万円を運用した場合の想定損益をシミュレーションします。
- **確度フィルタ + 複利版**: 全銘柄を合算した「システム全体」で、1日あたり
  シグナル成立時点の出来高倍率（volume_ratio、未来のリターンは使わない）が
  高い順に上位 `SIM_MAX_SIGNALS_PER_DAY` 件（デフォルト2件）だけに絞り込み、
  絞り込んだ通知だけを時系列順に、毎回その時点の残高の `SIM_ENTRY_FRACTION`
  （デフォルト30%）を投資する複利シミュレーションを行います。
  ピーク残高からの最大下落率（最大ドローダウン）もレポートに含まれます。
- `.github/workflows/notify_simulation.yml` — 手動実行専用のワークフロー
  （Actionsタブ → "crypto-notify-simulation" → "Run workflow"）。1時間足を
  長期間取得するため15〜30分程度かかることがあります。
- 出力: `notify_simulation_trades.csv`（個別トレードの明細。`signal_volume_ratio`＝
  シグナル成立時点の出来高倍率、`selected`＝確度フィルタで採用されたか）、
  `notify_simulation_report.md`（年別サマリーレポート）。
- 絞り込みは全銘柄合算のシステム全体で1日あたりの件数を制限しているだけで、
  同じ日に複数銘柄で通知が重なった場合に必要な同時資金までは考慮していません。
  また30%というポジションサイズは連敗時のドローダウンが大きくなるリスクが
  あるため、レポート内の最大ドローダウンを必ず確認してください。
- 調整可能な環境変数: `SIM_START_DATE`（データ取得開始日）、
  `SIM_TAKE_PROFIT_PCT`（利確ライン、デフォルト5%）、
  `SIM_MAX_HOLD_DAYS`（利確しない場合に強制決済するまでの日数、デフォルト60日）、
  `SIM_FEE_ROUNDTRIP_PCT`（往復手数料の概算、デフォルト0.2%）、
  `SIM_INITIAL_CAPITAL_JPY`（シミュレーション元本、デフォルト100万円）、
  `SIM_MAX_SIGNALS_PER_DAY`（1日あたりの採用件数上限、デフォルト2）、
  `SIM_ENTRY_FRACTION`（1回のエントリーで投資する残高の割合、デフォルト0.30＝30%）。

### シナリオ比較シミュレーション（銘柄範囲 × 投資割合）

- `notify_simulation_matrix.py` — 「出来高急増 かつ RSI反発」の通知条件のもとで、
  対象銘柄の広さ（BTCのみ／主要5銘柄／全30銘柄）と1回の投資割合（10%／15%／20%）を
  掛け合わせた複数シナリオを一括シミュレーションし、年ごとの通知回数・勝率・
  複利後の資産・最大ドローダウンを一覧比較します。
- `.github/workflows/notify_simulation_matrix.yml` — 手動実行専用のワークフロー
  （Actionsタブ → "crypto-notify-simulation-matrix" → "Run workflow"）。
  全30銘柄ぶんのデータを取得するため、30〜60分程度かかることがあります。
- 出力: `notify_simulation_matrix_trades.csv`（個別トレードの明細）、
  `notify_simulation_matrix.csv`（比較表のCSV）、
  `notify_simulation_matrix_report.md`（比較表つきレポート）。
- 日次の確度フィルタ（1日◯件まで）は使わず、条件に合致した通知は全てプールして
  時系列順にシミュレーションしています（銘柄を広げた効果を素直に見るため）。

## 設定の変更

- 通知条件: `crypto_signal_screener.py` の `NOTIFY_REQUIRE_VOLUME_SPIKE` /
  `NOTIFY_REQUIRE_RSI_REBOUND`（デフォルトはどちらも有効=両方成立で通知。
  環境変数で `"0"` にすると個別に無効化できます）
- 実行頻度: `.github/workflows/screener.yml` の `cron`（UTC基準）
- 対象銘柄: `crypto_signal_screener.py` の `SYMBOLS`（現在はBTCUSDTのみ）
- 各シグナルのしきい値: `crypto_signal_screener.py` 冒頭のパラメータ
