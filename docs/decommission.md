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
リポジトリを数えられる（値は出力しない）。数える単位は GitHub の owner/repo で、同じ origin の
別 checkout は1つにまとめる。調べるのは origin の既定ブランチ（最新の配布・実行ブランチ）で、
ローカル作業ツリーとの差は別行に出る（古い疑いがあれば `--fetch`）。ヒットしたファイルは
**経路**（workflow・スクリプト・ソース）／**再生成**（テンプレート・生成器）／**資料**（`*.md`・
`docs/`・サンプル・テスト）に分け、資料だけのリポジトリは「稼働送信元あり」にしない。
2026-10-26 の旧版の「23 リポジトリ」は、ローカルディレクトリのヒット数で、稼働送信元の数ではない。
スクリプトは macOS 標準の Bash 3.2 でも動く（連想配列・`mapfile`・BSD `sed` で効かない構文を使わない。
追加の Bash を入れなくてよい想定。#372）。macOS 実機での実行確認は PR で別途記録する。
各リポジトリの異常通知への切り替えは、個別アプリの Issue で行う（本Issueでは実装しない）。

## 停止前のゲート（すべて満たすまで次へ進まない）

1. 上記スクリプトの「送信元が残っているリポジトリ」**と「未確認のリポジトリ」の両方**が 0
   （経路・再生成が1件も無く、既定ブランチを確定できず調べられなかったリポジトリも無い。
   未確認は `git fetch origin` や `git remote set-head origin -a` で既定を確定して再実行する）。
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
