# Cloudflare Cron → GitHub Actions `collect.yml`

GitHub の `schedule: */15` は実測で1日6回程度・中央値4.1時間まで間引かれます。この Worker は10分ごとに `workflow_dispatch` を送り、`collect.yml` を実質10分間隔で起動します。PAPER 観測のみで、実売買・Nansenキー・リポジトリへの書き込みは行いません。

所要時間は約5分です。**トークンの値はチャット・Git・ログ・スクリーンショットに貼らないでください。**

## 1. GitHub fine-grained token を作る

1. GitHub → Settings → Developer settings → Personal access tokens → **Fine-grained tokens** → Generate new token。
2. **Repository access**: `Only select repositories` で対象リポジトリ（`RyoSAKu610/Sol-2nd-slime-`）だけを選ぶ。
3. **Permissions**: `Repository permissions` の **Actions: Read and write** だけを付ける。他は付けない。
4. **Expiration**: 短め（7〜30日）にする。期限切れ時はこの手順の 3〜4 をやり直す。
5. 生成された `github_pat_...` をコピーする。この値は次の手順で Cloudflare にだけ登録する。

## 2. Cloudflare に Worker を作る

1. Cloudflare ダッシュボード → Workers & Pages → **Create** → Worker。
2. 名前を `secondary-brain-radar-cron` にする。
3. エディタの内容を、このフォルダの `worker.js` の全文で置き換える。
4. **Settings → Variables and Secrets** で次を追加する。
   - Name: `GITHUB_TOKEN`
   - Type: **Secret**（Plaintext / Variable にしない）
   - Value: 手順1でコピーしたトークン
5. 同じ画面の Variables に次があることを確認する（`wrangler.toml` を使う場合は自動で入る）。値は `wrangler.toml` の `[vars]` と同じ。

   | Name | Value |
   | --- | --- |
   | `GITHUB_OWNER` | `RyoSAKu610` |
   | `GITHUB_REPO` | `Sol-2nd-slime-` |
   | `GITHUB_WORKFLOW` | `collect.yml` |
   | `GITHUB_REF` | `main` |

## 3. Cron Triggers を設定して Deploy

1. Worker → **Settings → Triggers → Cron Triggers** → Add Cron Trigger。
2. `*/10 * * * *` を入力する（10分ごと・UTC基準）。
3. **Deploy** を押す。

CLI で行う場合は、トークン登録だけ手元で実行します。

```sh
cd deploy/cloudflare-worker
npx wrangler deploy
npx wrangler secret put GITHUB_TOKEN   # プロンプトに値を貼る。シェル履歴や引数には残さない
```

## 4. 動作確認

- Worker の URL（`https://secondary-brain-radar-cron.<account>.workers.dev`）をブラウザで開く。
  - `{"ok":true,"owner":"RyoSAKu610","repo":"Sol-2nd-slime-","workflow":"collect.yml","ref":"main","token_configured":true}` のように返れば設定済み。
  - `token_configured` は真偽値だけ。トークンの値は返らない。
- GitHub → Actions → `collect` を開き、`workflow_dispatch` の実行が10分間隔で並ぶことを確認する。
- 実行ログにトークンが出ていないことも確認する。

## 5. 失敗したとき

| status | 確認すること |
| --- | --- |
| 401 / 403 | `GITHUB_TOKEN` の権限（Actions: Read and write）と有効期限。Secret 名の綴り。 |
| 404 | `GITHUB_OWNER` / `GITHUB_REPO` / `GITHUB_WORKFLOW` の名前。リポジトリを token の対象に含めたか。 |
| 422 | `GITHUB_REF` が実在するブランチか（通常は `main`）。 |
| `GITHUB_TOKEN secret is not set` | Secret が未登録、または Worker に反映されていない。登録後に再 Deploy。 |

## 6. トークンを漏らしたとき

1. GitHub → Settings → Developer settings → Fine-grained tokens → 該当トークンを **Revoke**。
2. 新しいトークンを発行し、Cloudflare の Secret `GITHUB_TOKEN` を更新する。
3. 必要なら Worker を再 Deploy する。

漏えいした値は Git 履歴・チャット・ログから消しても無効化が先です。古い値を再登録しないでください。
