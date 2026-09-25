# 検証記録

2026-09-13 JST。Python 3.14.6、独立した `evolution/.venv`。常駐コード・記憶DB・取引設定への変更なし。

|確認|結果|
|---|---|
|`evolution/.venv/bin/python -m unittest discover -s tests -p test_evolution.py -v`|13件成功|
|`evolution/.venv/bin/python -m pip check`|依存矛盾なし|
|`evolution/.venv/bin/python -m evolution demo`|本物のDEAP 1.4.4で8日・17,283件の合成データ実験完了。46条件を実評価、19回を再利用|
|同じdemoの再実行|`reused_saved_run: true`、同一ID・評価日時の既存結果を再利用|
|`evolution/.venv/bin/python -m evolution run`|実DBを読み取り、0.512日しかないため `insufficient_data`。GA/holdout評価を実行しない|
|同一実験を2プロセスで同時起動|探索呼出し1回。片方が新規・片方が完成済み結果を再利用。実験ID同一|

実データの不足レポート: `runs/observed/2acea535eb110fd2d3c7/report.md`。

合成データの機械的動作レポート: `runs/demo/acf585f5a55bf7b39e27/report.md`。合成データ上の数値は実績・実運用の優位性・年末目標達成の根拠には使えません。

独立レビューで同時起動が2回探索する問題を再現したため、出力領域のプロセス間ロックで結果再確認から探索・保存までを排他にしました。本文を一時ファイルから置き換え、最後にJSONを完成印として公開します。修正前の実験記録は削除せず、コードhash変更後の上記IDを現在の検証成果として使います。

テストで確認した点: 初回時刻のみの利用、DB読み取り不変、改変/非有限/時刻欠落の除外、入力上限時の停止、検出の次の観測での買い、費用控除、欠測/古いシグナルの未約定、消滅銘柄の損失を消さないこと、資金と建玉の上限、時系列分割の非重複、不足時のGA非実行、DEAP creator同一プロセス再利用、seed再現、乱数状態復元、ゼロ取引の失格、holdoutを探索関数に渡さないこと。

依存の全6パッケージを `requirements.txt` に固定しています。ネットワーク取得や認証情報参照はオフライン実験の対象外です。実データでの戦略有効性と実際の売却可能性は未検証です。
