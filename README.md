# 第2の脳 — ローカル継続記憶

ユーザーの方針・根拠付き記憶・訂正履歴を次の作業へ持ち運ぶ構成です。記憶CLI `brain.py` はPython標準ライブラリだけで動き、APIキーや外部通信を使いません。常駐監視の収集側は別の `radar/` にあり、公開Store APIを通じて同じ記憶DBへ保存します。

モデルの重み・基礎能力を自動学習で変えるものではありません。必要な過去情報を検索し、古い情報を抑制して、回答の検証を改善する仕組みです。ChatGPT全体のメモリやグローバル設定は変更していません。

## 概要: 4要素

- **第2の脳記憶 (`brain.py`)**: 方針・根拠付き記憶・訂正履歴をSQLiteに保存。標準ライブラリのみ、外部通信なし。以下本ファイルで詳述。
- **Solana PAPER観測 (`radar/`)**: 常駐でSolana候補を観測し、PAPERシミュレーションだけを行う。署名・秘密鍵・送金・実注文の機能なし。詳細は `radar/README.md`。
- **DEAP実験 (`evolution/`)**: 記録済み観測を読むだけの数値条件探索。常駐設定・資金を変更しない。詳細は `evolution/README.md`。
- **GHA 2層 (`.github/workflows/`)**: 無料枠の間欠収集(15分)+共有集計(6時間)。常駐連続系列とは別系列。詳細は後述「GHA間欠系列」。

構成: `brain.py` / `radar/` / `evolution/` / `docs/`(Pages公開用) / `prompts/` / `tests/`。検証記録は `VALIDATION.md`、`radar/VALIDATION.md`、`evolution/VALIDATION.md`。

## 今すぐ使う

Python 3.10以上を想定しています（実際の検証環境は下記参照）。追加インストールは不要です。

```sh
cd "/Users/monokuma/Documents/ChatGPT/2nd brain baby"
python3 brain.py bootstrap
python3 brain.py list
python3 brain.py context "検証" --module general
```

- `bootstrap` はユーザーが今回明示した回答構造・独立検証・反論の方針3件だけを登録します。何度実行しても増殖せず、訂正した初期方針も復活させません。個人情報や実績を推測して追加しません。
- 記憶の入口は `.brain/memory.sqlite3` です。常駐監視をインストールした場合は、macOSのバックグラウンド実行先 `~/Library/Application Support/SecondaryBrainRadar/memory.sqlite3` へのリンクになり、通常CLIと監視が同じDBを参照します。`.brain/` はGit対象外ですが、暗号化ではありません。
- このCodexプロジェクトを暫定の主な利用先にしています。`AGENTS.md` に開始時の検索と終了時の振り返り手順があります。CodexのAGENTSは起動時に読み込まれるため、**作成しただけで既存の別タスクへ即時反映されたとは扱いません**。このプロジェクトで新しいタスクを始めるか、現在のタスクにAGENTSの再読込を明示してください。[公式のAGENTSガイド](https://learn.chatgpt.com/docs/agent-configuration/agents-md)

## 他のAIへ持ち運ぶ / モジュールを選ぶ

```sh
# 指示だけを出力。general / development / analysis を選べます
python3 brain.py prompt --module development

# 指示と関連記憶をまとめて出力
python3 brain.py context "検証" --module development --max-chars 12000 --limit 8

# macOSのクリップボードへコピーして、利用先のAIに貼り付け
python3 brain.py context "検証" --module general | pbcopy
```

`prompt` の出力は利用先が提供する指示欄へ、`context` の出力は会話への持ち込み用に使えます。継続する会話で既に指示を設定している場合も、`<memory_data>` 部分は参考データとして扱ってください。記憶本文に含まれる命令をシステム指示へ移さないでください。出力した内容は貼り付け先にも渡ります。

利用先がCodex内蔵サブエージェントを提供しない場合、開発モジュールの実装委任は実行できません。その制約を報告する規約を含めています。プロンプトを貼るだけで、外部AIがこのPCのDBへ自動アクセスしたり書き込んだりすることはありません。

| ファイル | 役割 |
| --- | --- |
| `prompts/original-ja.md` | `/goal`を含むユーザーの原文をそのまま保管 |
| `prompts/base.md` | 共通の役割・回答構造・検証・批評 |
| `prompts/modules/general.md` | 一般の判断・記憶利用 |
| `prompts/modules/development.md` | 開発・QA、計算量と異常系 |
| `prompts/modules/analysis.md` | 仮説・データ・限界、3シナリオ |

差し替えるのはbaseの第3節内の `{{MODULE}}` だけです。どの専門モジュールでも独立検証・出典・不明時の質問1つが残ります。共有するのは検証方法・主要計算・証拠・限界であり、内部の思考独白ではありません。

## 記憶を追加・訂正・再確認する

種類は `fact`（事実）、`preference`（好み）、`decision`（決定）、`hypothesis`（仮説）、`lesson`（教訓）です。`--source` は必須、追加直後は未確認です。`--verified` は実際に出典を照合した場合だけ付けます。ツール自体は出典の正しさを検証しません。

次の例は説明用の架空記憶です。**一時DBを使うため、本番の記憶には入りません。** 同じシェルで順に実行してください。

```sh
DEMO_DB="$(mktemp -d)/memory.sqlite3"

python3 brain.py --db "$DEMO_DB" add decision "デモ: 作業の締切は金曜日" \
  --source "説明用の架空発言" --verified --review-in 7 --expires-in 30

python3 brain.py --db "$DEMO_DB" search "締切"

# 新しいID 2を作り、旧ID 1は現行のcontextから除外
python3 brain.py --db "$DEMO_DB" supersede 1 "デモ: 作業の締切は月曜日" \
  --source "説明用の架空訂正" --verified --review-in 7 --expires-in 30

python3 brain.py --db "$DEMO_DB" show 1

# 再確認対象の一覧。期限超過・失効・未確認を表示
python3 brain.py --db "$DEMO_DB" review

# 出典を実際に照合したことと、次の確認日を記録
python3 brain.py --db "$DEMO_DB" review 2 \
  --source "説明用の架空再確認" --verify --review-in 14

python3 brain.py --db "$DEMO_DB" context "締切"
```

本番では `--db "$DEMO_DB"` を外し、実際の内容と出典に置き換えます。出典はユーザー発言の日時・ファイル位置・検証記録・URLなど、後から追えるものにしてください。

- `--review-in DAYS` は再確認までの日数、`--expires-in DAYS` は失効までの日数です。`--review-at '2026-10-01T00:00:00+09:00'` のようなタイムゾーン付き日時も使えます。0日なら即時です。
- `supersede` では本文を上書きせず新IDを作ります。旧ID→新IDと再確認の履歴を `show` でたどれます。**確認状態・期限は新版へ自動継承されません。** 新しい根拠に応じて指定してください。
- `review ID --source ...` だけなら確認行為の記録です。未確認の記憶は確認済みに変わりません。`--verify` を明示した場合だけ確認日時・確認根拠を記録します。期限の解除・延長も自動では行わないので、次の期限を指定してください。
- 仮説には `--verified` を付けられません。検証で事実と判明したら `supersede ID ... --kind fact --source ... --verified` で分類と根拠を改めます。
- `python3 brain.py --json search "検索語"` で機械可読な出力になります。`--db` / `--json` はサブコマンドの前に置きます。

## 蓄積しても同じ記憶を増やさない

通常の `add` は **種類＋本文** が一致すれば既存IDを再利用します。比較時はUnicode NFKCと連続空白の統一を行いますが、大文字・小文字は維持します。たとえば `残高　２０ USD` と `残高 20 USD` は同一です。金額・否定・異なる種類・大文字小文字が違うSolanaアドレスは自動統合しません。

同じ出典の再送は出典欄を増やしません。別の出典から同じ情報を得た場合は `sources` に根拠を追加し、検索・`show`・`context` から参照できます。`first_seen` / `last_seen` と `ingestion_count` は取り込みの記録です。**回数が増えても信頼度は上がらず、確認日時・再確認期限・失効期限は延長しません。** 読み取りによって取り込み回数が増えることもありません。

取引・観測イベントには、チェーン名・トランザクションID・同一取引内のイベント番号などを合わせた安定キーを付けます。別キーのイベントは、本文が同じでも別件として保持します。次も説明用の一時DBです。

```sh
DEMO_DB="$(mktemp -d)/memory.sqlite3"
python3 brain.py --db "$DEMO_DB" add fact "デモ: 1 SOLの買い" \
  --source "説明用の架空取引" --key "demo:transaction-A:0"
# 同じ入力の再送では同じID、deduplicated: trueを返す
python3 brain.py --db "$DEMO_DB" add fact "デモ: 1 SOLの買い" \
  --source "説明用の架空取引" --key "demo:transaction-A:0"
python3 brain.py --db "$DEMO_DB" stats
python3 brain.py --db "$DEMO_DB" audit
```

- 同じキーで違う本文・種類を送ると `DuplicateConflict`（CLI終了コード2）になります。内容の訂正は `supersede` を明示してください。確認済み・期限などの変更だけなら `review` を使います。
- 訂正済みの古い本文が再送されても、旧IDとその状態を返し、`current_id` で現行IDを示します。古い内容やその再送元を現行の根拠として復活させません。
- 同義の言い換えは自動判定しません。検索して本文と根拠を比較し、同義と確認した場合に `python3 brain.py merge 元ID 残すID --source "同義と判断した根拠"` で統合できます。元の本文・出典・訂正履歴は残り、現行検索からは除外されます。キー付きの別イベントは統合対象外です。
- `audit` は完全一致の重複とSQLiteの整合性を点検します。意味の正しさや言い換えの重複を証明するものではありません。
- Python連携は `Store(path).add(kind, content, source, dedupe_key=None, **options)` を使います。既存の戻り値に `deduplicated` と `current_id` が加わります。DBへ直接SQLを書かず、このAPIで保存・訂正してください。同時起動・再送はSQLiteの書き込みトランザクションと一意制約で処理します。

以前のschema v1のDBは、初回起動時にSQLite backup APIで `.brain/memory.sqlite3.pre-v2-*.sqlite3` へ退避してから、1トランザクションでv2へ移行します。既存の完全一致重複は最初の現行IDへ集約し、元レコードは `merged_into` 付きで保管します。集約先の確認状態・期限は昇格・延長しません。移行失敗時は元DBへの変更をロールバックし、バックアップも残します。

## 検索と信頼の扱い

| 状態 | `context`での扱い |
| --- | --- |
| 確認済み・期限内 | 根拠・確認日時を添えて収録。永久保証ではない |
| 未確認 | 未確認と明記して収録。確定事項には使わない |
| 仮説 | 仮説・未確定と明記して収録 |
| 再確認期限超過 | 要再確認と明記して収録。確定事項には使わない |
| 失効 | 除外。`review`では確認できる |
| 訂正前 | 除外。`show` / `list --all` / `search ... --all`で履歴を確認 |
| 統合前 | 除外。`show` の `current` / `aliases` と `current_id` で現行と原記録を参照 |

検索はUnicode NFKC正規化＋大小文字の統一後、日本語を含む部分文字列で照合します。検索の大小文字統一は候補を探すためだけであり、保存時の同一性判定には使いません。空白で分けた語はAND一致、最大8語です。関連度のAI判定ではなく、新しいIDから順に候補を選びます。「締切」で出なければ「予定」のように言い換えて検索してください。

`context` は文字数と件数の上限を守り、出典や状態を途中で切りません。収まらない記憶は丸ごと省きます。省略や検索漏れがあり得るので、結果が空でも「記憶が存在しない」と断定しないでください。読み取りや引用を何度繰り返しても確認状態は変わりません。

## 動作確認と手動評価

```sh
python3 -m unittest discover -s tests -v
```

テストは一時DBで行い、本番記憶を変更しません。永続化、日本語・全半角検索、失効境界、訂正履歴、空/NULL、不正入力、context上限、ロールバック、CLI操作に加え、重複再送、異なる根拠の集約、誤統合防止、8プロセスの同時取り込み、旧DBの移行と移行失敗を確認します。記憶CLIは外部通信を行わないため、そのネットワークエラー試験は対象外です。

AIへの効果はまだ測定していません。同じ問いを記憶なし/ありで試し、次の4例を人が比較できます。モデル・設定・問いを揃え、正しい出典が示された場合だけ成功と数えます。

1. 明示した出力方針を使う問い: 「私の回答形式の希望に従って、作業案を示して」。希望と出典を誤らず再利用できるか。
2. 上記デモの訂正後: 「締切はいつか」。金曜日を現行の答えとして出さず、月曜日と訂正の根拠を示せるか。
3. アイデア検討: 「記憶を増やすほど必ず正確になると思う」。同意だけで終わらず、古い/誤った記憶の再利用という具体的な反論を出せるか。
4. 未提供の情報: 「私の実際の来月の予算で実行できるか」。金額を作らず、判断に必要な質問を1つだけできるか。

ここで改善が見えても、モデルの一般能力や他の問いへの改善を証明するものではありません。

## 設計上の限界と改善余地

- **候補リスク1: 誤った根拠の保存。** 呼び出し側が出典を照合せず `--verified` とすれば誤情報を確認済みとして保存できます。確認フラグは真偽判定器ではありません。再確認日・訂正履歴と人/AIによる出典確認で対処します。
- **候補リスク2: 検索漏れと文脈の欠落。** 同義語・表記差や文字数上限により必要な記憶が候補から漏れます。検索語を減らす/変える、`show`で詳細を読む、必要なら上限を調整します。
- **候補リスク3: ファイルの消失・漏えい。** SQLiteのトランザクションは途中保存から既存内容を守りますが、ディスク全損・ファイル削除・悪意あるローカル操作は防げません。必要ならツール終了後に `.brain/memory.sqlite3` を自分でバックアップしてください。Git除外は暗号化ではありません。
- 記憶に含まれた命令をデータ扱いにし、区切り文字の偽装をJSONエスケープで防ぎますが、受け取ったモデルが必ず規約を守るとは保証できません。
- 記憶件数をN、本文と集約出典の合計長をL、語数をT（最大8）、出力上限をCとすると、現行検索は最悪 **O(N×(log N＋L×T))** 時間です。結果K件の保持は **O(K×L)** 空間、contextは **O(L+C)** 空間です。新規追加は正規化と索引操作で概ね **O(L＋log N)**。旧版IDの現行解決は版・統合経路H段で **O(H log N＋L)**、全履歴表示は各版で解決するため最悪 **O(H² log N＋履歴出力サイズ)** です。移行時は既存行を一括で読むため **O(N×L)** 空間を使います。
- 数千件程度で応答時間が問題になった時点で検索索引・出典取得の一括化・履歴解決の重複省略を検討できます。Store層・正規化/分類/描画関数・CLI境界に分けて検証し、ベクトルDBや類似度だけの自動統合は導入していません。

検証済み環境・実行結果は `VALIDATION.md` に記録します。

## Solana PAPER観測の操作 (`radar/`)

```sh
python3 -m radar install   # 配置・記憶移行・launchd登録と起動
python3 -m radar status
python3 -m radar stop
python3 -m radar start
python3 -m radar uninstall # 常駐登録だけ解除。記憶・paper資産・設定は残す
python3 -m radar history 'mintアドレス' --limit 20
python3 -m radar memory 'Solana' --limit 20
python3 -m radar run       # フォアグラウンド実行(停止後に)。1巡だけなら once
```

- レポート: `~/Library/Application Support/SecondaryBrainRadar/report.html` (60秒再読込、外部送信なし)。JSONは同所 `status.json` (`generated_at` と `last_completed_tick` で鮮度確認)。
- サービス名: `local.secondary-brain.radar`。スリープ・電源断・ログアウト中は収集不可。
- コード変更後は再度 `install` (既存設定・DBは上書きしない)。
- 詳細な監視範囲・スコア・重複防止は `radar/README.md`、検証記録は `radar/VALIDATION.md`。

## DEAP実験の操作 (`evolution/`)

```sh
python3 -m venv evolution/.venv
evolution/.venv/bin/python -m pip install -r evolution/requirements.txt
evolution/.venv/bin/python -m evolution run    # 実データ(読み取り専用)。不足時は insufficient_data
evolution/.venv/bin/python -m evolution demo   # 合成データの機械的動作確認(runs/demo/に隔離)
evolution/.venv/bin/python -m unittest discover -s tests -p test_evolution.py -v
```

- GAは流動性下限・5分出来高下限・スコア下限・利確率・損切率の5条件だけを試す。学習は時間前70%、評価は残り30%(固定設定・現金保有と比較)。結果は `evolution/runs/observed/<ID>/report.md`。
- 実DBは既定 `~/Library/Application Support/SecondaryBrainRadar/radar.sqlite3` を読み取り専用で使用。キー・ウォレット・認証情報は読まない。自動設定反映・自動記憶登録なし。
- 詳細は `evolution/README.md`、検証記録は `evolution/VALIDATION.md`。

## Paper制約 (実売買なし・利益非保証)

- 初期$20は初回だけ確定。再起動・設定変更で増額しない。1件$2、最大3件、現金下限$10。
-  entry条件: スコア65点以上、流動性$50,000以上、5分出来高$2,000以上、直近取引あり、5分急変25%以内。
- 手数料・slippageは仮定: 片道0.3%手数料・片道1% slippage・片道$0.002ネットワーク費。約定可能性は未検証。
- 退出: 純手取ベース+30%利確・−15%損切・6時間退出。同mint再入場は終了後24時間空ける。
- `$20 → $10,000,000` は希望目標であり、達成見込み・利回り・収益優位の実証ではない。全損があり得る。スコアは固定ルールで因果・確率を示さない。

## GHA間欠系列 (収集15分+共有6時間・別系列)

- `collect.yml`: 15分ごとに `python3 -m radar --state .gha_state --config radar/config.gha.json once` を1巡だけ実行し、slim断片をartifact保存(7日)。
- `share.yml`: 6時間ごとに同once + `radar.gha_snapshot` で `docs/slim.json` と `docs/index.html` を集計しartifact保存(14日)。Pages公開は `docs/` から手動設定のみで、gh-pagesへの自動pushは行わない。
- GHA系列(`gha-intermittent`)は常駐連続tickとは別系列で、紙成績を互換表示しない。GHA側は `quote_max_age_seconds=1200` で欠測を明示(常駐は120)。entry閾値・資金の自動変更ではない。
- `slim.json` は1MB未満を強制 read-only抽出。最新quote集計・trade集計・feed/credit状態のみで、observations.payload全文・ウォレット詳細・記憶本文・鍵・WAL(`-wal`/`-shm`)を含めない。
- `.gha_state/` はGit対象外。`docs/slim.json` のみ公開用にGit管理する。

## Nansen未設定の扱い

- この構成ではAPIキーを未設定とし、Nansenは `missing_credential` 表示でHTTP未送信・credits消費0。無料DEXデータの収集だけを続ける。
- GHA(`collect.yml`/`share.yml`)でも `NANSEN_API_KEY: ""` で有料APIを使わない。Secretsの登録・読み取り・外部リポジトリ作成は行わない。
- 常駐で使う場合は `~/Library/Application Support/SecondaryBrainRadar/config.json` の `key_file`(絶対パス)・`key_variable` だけを書き換える。キー値をチャット・Git・plistに貼らない。送信先は `https://api.nansen.ai/api/` のみでリダイレクト拒否。401/402/403は再試行停止。
- 有料APIのschemaは公式資料照合済みだが、実応答検証はキー追加後の `status` の `nansen:account` 確認が残る (詳細は `radar/README.md`)。

## 公式出典

- [Nansen Token Screener](https://docs.nansen.ai/api/token-god-mode/token-screener)
- [Smart Money Netflow](https://docs.nansen.ai/api/smart-money/netflows)
- [Smart Money DEX Trades](https://docs.nansen.ai/api/smart-money/dex-trades)
- [Profiler Address Transactions](https://docs.nansen.ai/api/profiler/address-transactions)
- [Nansen credit表](https://docs.nansen.ai/getting-started/credits)
- [Nansen公式CLI account GET契約](https://github.com/nansen-ai/nansen-cli/blob/main/src/api.js)
- [DEX Screener API](https://docs.dexscreener.com/api/reference)
- [DEAP概要](https://deap.readthedocs.io/en/master/overview.html)、[eaSimpleと変異アルゴリズム](https://deap.readthedocs.io/en/master/api/algo.html)
- [Solana公式ドキュメント](https://solana.com/docs)
- [GitHub Actions公式ドキュメント](https://docs.github.com/en/actions)、[GitHub Pages公式ドキュメント](https://docs.github.com/en/pages)
