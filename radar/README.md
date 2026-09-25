# Opportunity Radar

このPCに常駐してSolanaの候補を観測し、重複を集約して長期記憶へ残すローカルCLIです。売買は **$20のPAPERシミュレーション**。署名・秘密鍵・送金・実注文の機能はありません。

`$20 → $10,000,000` は500,000倍の希望目標です。達成見込みや利回りを実証したものではありません。薄い流動性・売却不能・追随の遅れ・手数料で全損する可能性があり、Smart Moneyの過去成績にも選択／生存者バイアスがあります。スコアは固定ルールで、因果関係や確率を示しません。

## 操作

プロジェクトルートでPython 3.10以上を使います。追加pip依存はありません。

```sh
python3 -m radar install   # コード配置、共有記憶を移行、launchd登録と起動
python3 -m radar status
python3 -m radar stop
python3 -m radar start
python3 -m radar uninstall # 常駐登録だけ解除。記憶・paper資産・設定は残す
python3 -m radar history 'mintアドレス' --limit 20
python3 -m radar memory 'Solana' --limit 20
python3 brain.py search 'Solana'
```

- レポート: `~/Library/Application Support/SecondaryBrainRadar/report.html`。ファイルをブラウザで開くと60秒ごとに再読み込みします。外部送信・ポート公開・SNS通知はありません。
- JSON: 同ディレクトリの `status.json`。`generated_at` の古さと `last_completed_tick` を確認してください。ファイルがあるだけでは常駐成功の証明になりません。
- サービス名: `local.secondary-brain.radar`。ログイン時に起動、異常終了時はlaunchdが再起動。PCスリープ・電源断・ログアウト中は収集できません。24時間運転のためのPC電源設定は変更していません。
- コード変更後は再度 `install`。既存runtime設定とDBは上書きしません。
- 通信失敗は `feeds.last_error` にDNS、TLS証明書、タイムアウト等の固定カテゴリと数値コードを記録します。例外本文・URL・認証値は記録しません。診断追加前の `URLError` だけの記録から過去原因を確定することはできません。
- 無料DEXのDNS失敗だけ再試行の待機上限を5分にします。他の通信・HTTPエラーとNansenは従来どおり最大1時間。サーバーが明示した `Retry-After` は従来どおり優先します。これは通信復旧後の余分な待機を減らすもので、DNS障害を直したり、スリープ中の実行を保証したりするものではありません。
- フォアグラウンド実行: 停止後に `python3 -m radar run`。1巡だけなら `once`。同じstateへの二重起動はファイルロックで拒否します。

## Nansen API Proの接続

現在このタスクではAPIキーを発見・認証できていません。Nansenは `missing_credential` と表示し、無料DEXデータの常駐収集を続けます。Pro加入だけで認証済み・利用枠ありとは判断しません。

常駐設定は `~/Library/Application Support/SecondaryBrainRadar/config.json`。以下の2項目に既存のAPIキー保管ファイルの絶対パスと変数名を指定してください。キー値をチャット・Git・plistに貼らないでください。

```json
{
  "key_file": "/Users/あなた/.config/nansen/private.env",
  "key_variable": "NANSEN_API_KEY"
}
```

これは設定項目の抜粋です。実際のconfig.jsonではこの2項目だけを書き換えます。保管ファイルはユーザー所有、mode600で、`NANSEN_API_KEY=値` または1行のキーを読み取れます。環境変数にも対応しますが、対話シェルの環境はlaunchdへ自動継承されないため常駐にはファイル参照を使います。認証情報は `https://api.nansen.ai/api/` 以外へ送らず、リダイレクトも拒否します。

設定は毎巡再読込します。401/402/403は無駄な再試行を止め、キー変更または再起動まで待ちます。キー追加後は `status` の `nansen:account` とAPI表示プラン／残creditsを確認してください。サービスは既存のAPIキーを利用するだけで、購入・アップグレードしません。

## 監視範囲と費用

| 情報 | 標準間隔 | credit / request | 範囲 |
| --- | ---: | ---: | --- |
| DEX latest token profiles | 15分 | 無料 | Solanaの最新掲載分。新規発行を網羅しない |
| DEX token pairs | 約60秒＋処理時間 | 無料 | 最近の候補最大60件、保有を優先、30mintずつ |
| Nansen account GET | 24時間 | 0 | plan / credits_remaining |
| Token Screener | 1時間 | 1 | trader_type=sm、最大50件の先頭ページ |
| Smart Money netflow | 8時間 | 5 | 1h純流入順、最大50件 |
| Smart Money DEX trades | 8時間 | 5 | 最新順最大50取引。両swap legを保存 |
| 指定ウォレット取引 | 6時間 | 1 | 直近24時間最大25件の先頭ページ |

API費用表・schemaは2026-09-12に公式資料で照合しました。有料APIは資格情報未確認のため実応答検証が残ります。ローカル上限はUTC日60／月1800credits。HTTP前に永続台帳へ費用を予約し、タイムアウトや再試行も保守的に計上。これは請求書ではなく消費上限推定です。Nansenの残高・実費ヘッダーも記録します。wallet数によって日上限へ達すると追加HTTPを止めます。

`wallets` はSolana公開アドレスの配列。Nansen Smart Money DEX tradesで得たアドレスは合計5件までwatch候補へ追加します。ユーザー指定walletの単純な入出金はSmart Moneyの購入として加点しません。受取は購入を証明しません。netflowもDEX購入だけでなくCEX出入りを含みます。SNS・ニュース・全ウォレット履歴は接続していません。

## 累積記憶と重複防止

- `radar.sqlite3` の `observations`: 同一feed＋正規化JSONのSHA256で集約。初回 `first_at`、最新 `at`、`seen_count` を保持。価格などの意味ある変化は別観測として残します。
- `transaction_hash` があるイベントはchain、tx、log_index、swap両mintから安定キーを作り、再送・追加情報は同イベントへ集約。同一txに区別用indexもtoken差分もない複数swapは区別できないAPI上の限界があります。
- tokenはSolana mint、walletはアドレス、paper注文は固有イベントキーで識別します。長期履歴を7日で消す処理はありません。活動中のquote集合だけ最大60に絞ります。長期ディスク使用量は保持する変化量に比例して増えます。
- 候補仮説は1日最大5件、同日同mintは1件。閉じたpaper取引は結果1件。毎tickの価格を脳の本文へ複製しません。
- 脳への投入は `brain.Store.add(..., dedupe_key=安定イベントID)`。書込後・radar完了フラグ前に停止しても、再送で記憶を増やしません。仮説に確認済みラベルを付けず、1日で失効・7日で再確認。paper結果はローカル台帳で確認したシミュレーションの事実であり、将来利益の知識ではありません。
- 普段の `brain.py search/context` と常駐は **同じ記憶DB** を読みます。install時に元DBをSQLite backupでApplication Supportへ移し、元pathをsymlink化。元DBも `.brain/memory.before-radar-*.sqlite3` として保持します。既存DBが両方に別々に存在したら上書きを拒否します。
- 過去の関連記憶はレポートの「参照した記憶」に分類とともに表示します。記憶から実行命令や自己改変ルールを作らず、固定スコアを勝手に変更しません。

## Paperルール

- 初期$20は初回だけ確定。再起動・設定変更で増額されません。
- 1件$2、最大3件、現金下限$10。
- スコア: 流動性条件25、5分出来高20、買い件数優勢10、5分価格変化0〜25%で10、Nansenの正の純流入20。
- 最低65点、流動性$50,000、5分出来高$2,000、直近取引あり、急変25%以内。買い／売りのDEX legだけでは方向スコアを足しません。
- 手数料片道0.3%、slippage片道1%、network片道$0.002は仮定。約定可能性は未検証。
- 純手取ベース+30%で利確、−15%で損切、6時間で退出。同mint再入場は終了後24時間空けます。
- quote取得が120秒より古い／欠損／時計逆行なら新規売買しません。DEXには最終価格更新時刻がなく `observed_at` は取得時刻です。`pairCreatedAt` を価格時刻として扱いません。停止中の損切や、消滅したpoolでの売却を捏造しません。

検証: `python3 -m unittest discover -s tests -v`。結果は [VALIDATION.md](VALIDATION.md)。1tickの主要処理は取得候補数Cと全取引数Hに対し概ねO(C×H＋C log C)、メモリO(C＋H)、履歴検索は保存文字数に比例する線形走査です。大規模化時の改善候補はpaper残高の集計SQLと履歴索引ですが、現状の小額検証には追加基盤を入れていません。

## 公式出典

- [Nansen Token Screener](https://docs.nansen.ai/api/token-god-mode/token-screener)
- [Smart Money Netflow](https://docs.nansen.ai/api/smart-money/netflows)
- [Smart Money DEX Trades](https://docs.nansen.ai/api/smart-money/dex-trades)
- [Profiler Address Transactions](https://docs.nansen.ai/api/profiler/address-transactions)
- [Nansen credit表](https://docs.nansen.ai/getting-started/credits)
- [Nansen公式CLI account GET契約](https://github.com/nansen-ai/nansen-cli/blob/main/src/api.js)
- [DEX Screener API](https://docs.dexscreener.com/api/reference)
