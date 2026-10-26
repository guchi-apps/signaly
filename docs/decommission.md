# Signaly 廃止の手順と進捗（#358）

## 方針（2026-10-25・利用者決定）

- Signaly を廃止する。定常通知（成功・通常ログイン・復旧）は不要。
- **異常時のみ Discord** へ通知する。宛先は全リポジトリ共通の既存 Webhook
  （1Password `apps` vault → `Notify` → `alert-webhook-url`。新規発行しない）。
- **全送信元で Discord への実到達を読み戻すまで、Signaly は止めない。**
- `docs/slack-migration.md` は Slack 前提の旧方針。今回の決定が正で、そちらは参考扱い。

## 現状（2026-10-26 時点の棚卸し）

`bash scripts/inventory-signaly-senders.sh` を実行すると、`~/apps` 配下で Signaly への
送信経路（`SIGNALY_*` / `signaly-notify` / `notifySignaly` / `/webhook/<ID>`）が残る
リポジトリを数えられる（値は出力しない）。この日の結果は **23 リポジトリが残存**。
各リポジトリの異常通知への切り替えは、個別アプリの Issue で行う（本Issueでは実装しない）。

## 停止前のゲート（すべて満たすまで次へ進まない）

1. 上記スクリプトの出力が空（残存リポジトリ 0）。
2. 以下の 5 箇所に旧 Signaly の Webhook URL / API キーが無い（チャンネル ID で突き合わせる。
   `docs/webhook.md` の棚卸し手順）:
   1Password / organization secret / repository secret / VPS・サブPCの `.env` / `~/.pm2/dump.pm2`。
   サブPCの `~/.pm2/dump.pm2` は無いことを確認済み。
3. 各送信元から異常を 1 回起こして Discord へ届いたことを読み戻す。
4. VPS 撤去・横断 GitHub Actions 移行・各アプリの異常通知 Issue が完了している。

## 停止後の整理（所有者の確認と読み戻しを経て、最小範囲で）

1. 通知履歴（`notifications`）を必要ならエクスポートしてから保管方針を決める。
2. VPS の `signaly` サービスを停止・無効化し、デプロイ再適用（`deploy.yml`）で復活しないことと、
   公開エンドポイント（`/webhook/*`・`/api/*`）が応答しないことを確認する。
3. DB・公開 URL（Apache VirtualHost / DNS）・Supabase の Redirect URL・Web Push 購読・VAPID 鍵・
   `SIGNALY_*` / `SIGNALY_LOGIN_WEBHOOK_URL` の secret を削除する。外部システムの削除は
   所有者の確認後に行い、削除したあと読み戻す。
4. StatusHub の共通アクセス設定から `SIGNALY_ACCESS_APP_TOKEN` を取り下げる。
5. リポジトリのアーカイブ可否を判断する（上記がすべて済んでから）。
