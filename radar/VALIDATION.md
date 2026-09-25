# Radar検証記録

検証日: 2026-09-12。Python 3.14、macOS、ローカルユーザーlaunchd。

## 確認済み

- `python3 -m unittest discover -s tests -v`: brain27＋memory dedup16＋radar19、計62件成功。radar単体19件も成功。
- 無料DEX実データ: 初回 `2026-09-12T14:30:24Z` にprofiles/quotesとも `ok`、raw観測23行。継続tickで新価格を保存し、14:34:23Z時点50行。取得時刻と価格更新時刻の不明状態を区別。
- 実profile payloadを隔離DBで2回取り込み、行数1、seen_count2を確認。異なる価格は別観測、30日後cleanupでも履歴を残すテスト成功。
- Nansen未設定: `missing_credential`、HTTP未送信・credits日/月とも0。コードのschemaは公式資料照合済み。wallet date必須、SM DEX bought/sold両leg、単純送受信のSmart Money誤加点防止を検証。
- macOS `install/start/stop/status/uninstall` を実行。停止後loaded=false、uninstall後plist=false、再install後loaded=true/runningを確認。初回PID27245から再導入PID27564/27651へ変わっても観測DB・共有記憶・paper初期$20を保持。
- 実running中に `once` を重ね、既存flockにより二重起動拒否。bootoutがSIGTERM送出直後に返るレースを実検出し、flock解放待ちを追加して再導入成功。
- 異常終了復帰: 対象サービスPID27651をSIGKILL後、launchdだけでPID27702/runs2/runningへ自動復帰。`2026-09-12T14:35:09Z` の新heartbeat、観測50行・paper$20・runtime/brain_sync_errorなしを確認。
- 既存 `.brain/memory.sqlite3` の4件をSQLite backupでruntimeへ移し、元DBbackupを保持、元pathをsymlink化。普通の `brain.py search '重複'` と `radar memory '重複'` の両方が同じID4を参照。再installでもsymlinkと記録を保持。
- 再送クラッシュ窓: brain書込後にradar側brain_idをNULLへ戻して再同期、記憶1件のまま。記憶期限と検証状態を再送で延長しないことはmemory側テスト済み。
- Paper: フィクスチャで$20→$2投入後現金$18、二重建玉防止、費用控除、損切、cooldown、再起動後初期資金の変更禁止、非負cashを検証。実データではこの記録時点で約定0件。実売買の実行証拠はありません。
- API予算ゼロ、失敗時予約維持、再起動後の日額上限、401再試行停止、固定origin、エラーのkey値非表示、時計逆行停止、古い価格／欠損／異常ネスト、HTML外部文字escapeを検証。

## 未確認・範囲の限界

- Nansen APIキー／Proプラン／残高／実応答は未認証。キー追加後にaccount実接続で確認が必要。
- 実売買・ウォレット署名・収益優位・年末1000万ドル達成は実証していません。
- PC再起動そのものやスリープ復帰を実機で強制していません。login起動・KeepAliveはplist設定とプロセス再起動試験で確認する範囲です。
- ローカルHTMLのブラウザ表示はURL policyがfile://を拒否したため目視未確認。拒否を別browser／localhost経由で迂回していません。HTML生成・エスケープ・更新ファイルは確認済み。
- 情報網羅性は保証しません。最新profiles、活動候補60件、Nansen先頭ページ、Solanaのみ。SNS／ニュースなし。Smart Moneyラベルも将来の勝者を保証しません。
- データ取得時刻が新しくてもDEX源の価格更新時刻は不明。売却不能・pool消滅・実スリッページはpaper費用モデルで再現しきれません。

ユーザー明示の継続点検として別途作成された6時間おきCodex heartbeatは、状態／同期／重複をAIが点検するものです。ここで検証した約60秒のlaunchd収集プロセスとは別の実行周期です。

## 2026-09-15 通信診断の追記

- 10:51 UTC、変更前の常駐PID48544が再起動なしにDEX profiles／quotes両batchの取得を再開していることを確認。先行するURLError連続失敗の原因内訳は、旧実装がreasonを保存していないため未確定。
- 同じPython 3.14、runtime作業場所、launchd設定の環境でDNS解決と固定公開DEX profiles URLのHTTPS GETを確認。HTTP200、30行。診断対象のproxy／証明書環境変数はtool側とlaunchd側でいずれも未設定。システム設定・TLS検証・通常のbackoffは変更していない。
- 最小修正はエラー分類のみ。`URLError.reason` または直接の通信例外を固定カテゴリへ変換し、errno／証明書verify_codeは整数だけ保存。未知の文字列reasonやURL・認証情報を含む例外本文は保存しない。
- radar単体21件成功。追加2テストで10種の原因分類、例外内の秘密・URLの非出力、直接timeoutのDB記録、従来の60秒→120秒backoff維持を確認。分類処理の時間・空間計算量はO(1)。
- 残るリスク: 原因文字列しか渡されないエラーはunknown_networkとなる。成功後はfeedsの直前エラーが消えるため、この変更は過去エラー履歴を遡って復元するものではない。

## 2026-09-17 DNS復旧後の待機上限

- 02:31:22 UTCに同じruntime／Python／launchd設定環境でDNS解決・無料DEX GET（HTTP200、30行）が成功した一方、常駐feedはDNS失敗12回の状態で03:28:59 UTCまで約57分の待機を残していた。停止排他後、失敗したDEX feedの次試行時刻だけ一度前倒しし、通常のonceで02:31:51 UTCに全DEX成功を確認して再開。再試行の証跡はruntime `maintenance/20260917T023151Z-dex-manual-retry.json`。
- 直近の電源イベントは02:17:42 DarkWake、02:17:44 Sleep、02:28:53 Wake。観測の時系列は記録するが、DNS失敗の背景原因がスリープだったとは断定しない。
- 最小修正: 構造化した `FeedError.category` が `dns` かつ無料 `dex:` feedの場合だけ、通常の指数待機を60→120→240→300秒（以降300秒）にする。HTTP／TLS／timeout／Nansenは従来の最大3600秒、明示的なRetry-After処理も従来どおり。成功時の失敗数リセットを維持する。
- radar単体24テスト成功。追加テストは連続DNSの上限と成功後60秒への復帰、Nansen DNS・TLS・timeout・HTTP429の従来上限、DNS／HTTP429に設定されたRetry-After優先。既存の原因分類と秘密情報を出さないテストも成功。分類・待機計算はいずれも時間／空間O(1)。
- 統括の独立受入で全80テスト成功。受入後に通常installで配備し、PID90021の02:34:51 UTC tick、feeds.py／runner.pyの配置SHA一致、設定SHA不変、取引24行・観測25736行の保持を確認。
- 配備後02:35:51 UTCの通常quote取得でDNS errno8が再発。初回失敗の次試行は60秒後で新コードの分類・スケジュールは動作しているが、DNS自体は依然不安定。追加の手動再試行や再起動は行っていない。
- 次の通常再試行で02:36:53 UTCに全DEXが再びok、failures0になった。同PID90021、runtime／同期エラーなし、paper24件終了・建玉0、credits0。自然復帰まで確認した配備証跡はruntime `maintenance/20260917T023651Z-dex-dns-retry-cap-deployment.json`。
- 恒久的なDNS修復・スリープ中の稼働・継続利益を検証したものではない。取得済みの新価格による通常paper処理だけを行い、空白中の価格・約定は補完していない。
