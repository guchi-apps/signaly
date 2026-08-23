# Signaly

Webhook で受け取った通知をリアルタイム表示するプライベート通知ハブ（PWA 対応）です。CI/CD や外部サービスから POST した内容をブラウザで確認でき、Web Push でスマホにも届きます。

- **バックエンド**: FastAPI + Uvicorn（Python）
- **フロントエンド**: Vanilla JS（PWA）
- **DB**: MySQL + SQLAlchemy
- **認証**: Supabase Auth の Google ログイン（ブラウザ）/ API キー（スクリプト）
- **本番**: systemd + Apache リバースプロキシ（`https://signaly.gucchii.com/` → `127.0.0.1:8002`）

## 主な機能

- Webhook 受信（Discord Webhook 形式 / Signaly 独自形式）
- チャンネル・グループ管理（作成・名前変更・削除・並び替え）
- SSE によるリアルタイム通知フィード
- Web Push（アプリ終了中もスマホに通知）
- チャンネル・グループごとの通知オン/オフ
- チャンネル URL の共有（`?channel=`）と前回チャンネルの復元

## プロジェクト構成

```
signaly/
├── backend/              # FastAPI API
│   ├── main.py
│   ├── database.py
│   ├── auth.py           # 認証の入口（Bearer JWT / API キー / SSE 用 Cookie）
│   ├── supabase_auth.py  # Supabase の JWT を JWKS で検証
│   ├── push.py
│   ├── webhook.py
│   ├── app_login.py      # Supabase Database Webhooks → ログイン通知の変換
│   └── requirements.txt
├── frontend/             # 静的 UI（PWA）
│   ├── app.js
│   ├── auth.js           # Supabase Auth（Google ログイン）
│   ├── auth/callback.html
│   ├── changelog.js
│   └── ...
├── deploy/               # 本番設定
│   ├── setup.sh
│   ├── signaly.service.template
│   └── apache.conf
├── docs/
│   └── webhook.md        # Webhook API マニュアル
├── scripts/
│   ├── dev.sh            # ローカル開発起動
│   ├── setup-tunnel.sh   # Cloudflare Tunnel 初回設定
│   ├── bump_version.py   # バージョン管理
│   └── gen_vapid_keys.py # Web Push 用キー生成
├── .env.example          # 環境変数一覧（値なし）
├── .env.local.example    # ローカル開発用テンプレート（1Password 不要）
└── version.json
```

## ローカル開発

### 前提条件

- Python 3.9+
- MySQL（WSL ローカル推奨）
- [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/)（ログイン / Web Push / スマホ確認用）

### 初回セットアップ

```bash
cd signaly
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt
cp .env.local.example .env.local   # 値を編集（git 管理外）
```

`.env.local` に DB 接続情報・Supabase の接続先・`SECRET_KEY` などを設定します。詳細は `.env.local.example` を参照してください。

ログインは他アプリと共通の Supabase Auth（Google ログイン）です。Supabase プロジェクトは開発用と本番用で分けます。開発用の `project-url` / `publishable-key` は 1Password を経由せず `.env.local` へ直接書き（`SUPABASE_URL` / `SUPABASE_PUBLISHABLE_KEY`）、開発用 Supabase の **Redirect URLs** に `https://<TUNNEL_HOSTNAME>/auth/callback` を登録します。`service_role` キーはフロントエンドにもリポジトリにも置きません。

Web Push を使う場合:

```bash
python scripts/gen_vapid_keys.py mailto:you@example.com
# 出力された VAPID_* を .env.local に追加
```

### 起動（推奨）

固定 URL（Named Tunnel）を使う場合は初回のみ:

```bash
bash scripts/setup-tunnel.sh <your-domain>
```

日常の開発:

```bash
bash scripts/dev.sh
```

- ローカル: `http://127.0.0.1:8001`
- トンネル: `https://<your-domain>/`（ログイン / PWA / Web Push はこちら）
- 停止: `Ctrl+C`

同一 LAN から HTTP のみ確認する場合（ログイン / Push 不可）:

```bash
bash scripts/portforward.sh
```

### テスト

```bash
.venv/bin/python -m unittest discover -s backend -p 'test_*.py'
```

GitHub Actions の `ci.yml` も同じテストを `develop` への push と PR（`main` / `develop` 向け）で実行します。

## 環境変数

| 変数 | 用途 |
|------|------|
| `DB_*` | MySQL 接続 |
| `SUPABASE_URL` / `SUPABASE_PUBLISHABLE_KEY` | Supabase Auth の接続先（publishable key はブラウザへ配る前提の公開値） |
| `APP_URL` | ベース URL |
| `ALLOWED_EMAILS` | ログイン許可メール（カンマ区切り。API 側でも判定する） |
| `SECRET_KEY` | SSE 用セッション Cookie の署名 |
| `VAPID_*` | Web Push |
| `TUNNEL_NAME` / `TUNNEL_HOSTNAME` | Cloudflare Named Tunnel（開発用） |

本番 VPS では GitHub の secret / variable の値を GitHub Actions がデプロイ時に `.env` へ同期します（Supabase / SECRET_KEY / VAPID 含む）。`SUPABASE_URL` / `SUPABASE_PUBLISHABLE_KEY` は他アプリと共通のため organization の variable（`SUPABASE_PROJECT_URL` / `SUPABASE_PUBLISHABLE_KEY`）から取ります。ローカル開発は `.env.local`（1Password 不要）を使い、開発用 Supabase プロジェクトを参照します。

## Webhook

外部サービスからの POST 仕様は [docs/webhook.md](./docs/webhook.md) を参照してください。Discord Execute Webhook と同じ JSON 形式で送信できます。

```
POST https://<your-host>/webhook/<channel_id>
```

Webhook URL はログイン後の **Webhook URL** 画面で確認できます。

Supabase Auth へ移行したアプリのログイン通知は、Supabase の Database Webhooks を直接受ける専用の受け口を使います（アプリ側のコード変更は不要）。詳細は [docs/webhook.md](./docs/webhook.md#アプリのログイン通知supabase-database-webhooks) を参照してください。

```
POST https://<your-host>/notify/app-login/<app_id>
X-Signaly-Token: <channel_id>
```

## デプロイ

`main` ブランチへの push（または Actions から手動実行）で GitHub Actions が VPS へ rsync デプロイします（[設計ガイド](https://github.com/m-guchi/docs/blob/main/README.md) 参照）。

```
main へ push / workflow_dispatch
    ├─ tag      … version.json から v{version} タグを作成
    ├─ deploy   … rsync → systemd restart
    ├─ release  … GitHub Release を自動生成
    ├─ notify   … デプロイ結果を Signaly へ通知
    └─ notify-release … リリース結果を Signaly へ通知
```

**注意:** 同じバージョンのタグが別コミットに既にある場合、workflow はエラーで止まります。`python scripts/bump_version.py` で version を上げてから `main` へマージしてください。

### シークレットの取得先

デプロイ・CI が実行時に読むのは **GitHub の secret / variable** です。1Password は「人が管理する
唯一の正」として残し、**値を変えたときだけ** 1Password から GitHub へ同期します。以前は実行の
たびに 1Password を読んでいましたが、サービスアカウントの日次レート制限（1Password アカウント
全体で 1,000 リクエスト/日）を使い切ってデプロイが止まったため切り替えました
（guchi-apps/issue-deck#1302）。

どの値をどこ（repository / organization）から取るかの対応表は
[`.github/secrets-manifest.tsv`](./.github/secrets-manifest.tsv) にあります。

```bash
op signin                                   # 個人アカウントで（サービスアカウントの枠を使わない）
bash scripts/sync-github-secrets.sh --dry-run
bash scripts/sync-github-secrets.sh
```

issue-deck の画面からは Actions の **Sync secrets**（`.github/workflows/sync-secrets.yml`）でも
同じ同期を起こせます。

#### 1Password 側のアイテム（値の正）

| アイテム | フィールド | 用途 |
|---------|-----------|------|
| `signaly` | `app-url` | `https://signaly.gucchii.com/` |
| `signaly` | `allowed-emails` / `secret-key` | ログイン許可・SSE 用 Cookie の署名 |
| `Supabase` | `project-url` / `publishable-key` | Supabase Auth（全アプリ共通。GitHub 側は organization の variable） |
| `signaly` | `vapid-*` | Web Push |
| `signaly` | `target-dir` / `db-name` | デプロイ先・DB 名 |
| `DB` | `db-user` 等 | MySQL 共通接続情報 |
| `Server` | `host` / `username` / `ssh-port` | SSH 接続 |
| `githubaction-sshkey` | `private_key` | GitHub Actions 用 SSH 秘密鍵 |
| `signaly` | `ci-webhook-url` | CI / デプロイ通知（Signaly Webhook URL 全文） |

`ci-webhook-url` は Signaly 上で通知用チャンネルを作成し、**Webhook URL** 画面で表示される URL（例: `https://signaly.gucchii.com/webhook/...`）を 1Password の `signaly` アイテムに登録します。CI / デプロイは `.github/scripts/signaly-notify.sh` から POST します。

Signaly 自身のログイン通知は、他アプリと同じく Supabase の Database Webhooks から `POST /notify/app-login/signaly` で受けます（アプリ側に通知用の環境変数は不要）。

`DB` / `Server` / `githubaction-sshkey` は他アプリと共通のため、GitHub 側では organization の共通
シークレット（`SHARED_DB_*` / `SERVER_*`）として持ちます。`known_hosts` は 1Password ではなく
`ssh-keyscan` で取得します。

`OP_SERVICE_ACCOUNT_TOKEN` は repository secret に残していますが、デプロイ・CI の実行時には
使いません。

### VPS 初回セットアップ

```bash
bash deploy/setup.sh
```

`setup.sh` は venv 作成・user systemd 登録（`loginctl enable-linger` は初回のみ sudo）まで行います。`.env` は書き込まないため、初回はこのあと `main` へのマージ（または Actions の **Deploy** の手動実行）を1回行ってください。GitHub Actions デプロイは **sudo 不要**です。

初回のみ VPS に SSH して linger を有効化する場合:

```bash
sudo loginctl enable-linger "$(whoami)"
```

Apache には `deploy/apache.conf` を `signaly.gucchii.com` 用 VirtualHost に追記してください（本番ポート **8002**、**ルート `/` をプロキシ**）。完全な例は `deploy/apache.vhost.example` を参照。

1Password / GitHub secret / VPS の `.env` では URL をルートに合わせます:

```
APP_URL=https://signaly.gucchii.com/
```

本番用 Supabase の **Redirect URLs** には `https://signaly.gucchii.com/auth/callback` を登録します（ワイルドカードは使いません）。

### ポート

| 環境 | ポート |
|------|--------|
| ローカル開発 | 8001 |
| 本番（systemd） | 8002 |

## リリース手順

`develop` でバージョンを上げ、`main` へ PR マージします。

```bash
python scripts/bump_version.py patch   # 1.0.0 → 1.0.1
python scripts/bump_version.py minor   # 1.0.0 → 1.1.0
python scripts/bump_version.py major   # 1.0.0 → 2.0.0
```

`frontend/changelog.js` に追加されたスタブの `changes` を編集してからコミットします。

```bash
git commit -m "v1.0.1 をリリースする。"
```

## スクリプト一覧

| コマンド | 説明 |
|---------|------|
| `bash scripts/dev.sh` | cloudflared + uvicorn 起動（開発） |
| `bash scripts/setup-tunnel.sh <hostname>` | Named Tunnel 初回設定 |
| `bash scripts/portforward.sh` | WSL → Windows ポートフォワード |
| `python scripts/bump_version.py [patch\|minor\|major]` | バージョン bump |
| `python scripts/gen_vapid_keys.py <mailto:...>` | VAPID キー生成 |
| `bash scripts/test-notify.sh` | テスト通知送信 |
| `bash scripts/sync-github-secrets.sh [--dry-run]` | 1Password → GitHub の secret / variable 同期 |

## 設計ガイド

VPS 構成・ポート規則・シークレット運用など共通ルールは [m-guchi/docs](https://github.com/m-guchi/docs/blob/main/README.md) を参照してください。

## CI/CD の既知の課題

> 2026-06-29 時点で確認された課題です。対応が完了したら削除または更新してください。

| 優先度 | 課題 | 対象ファイル |
|--------|------|-------------|
| 要確認 | **`ci.yml` の backend テストで MySQL サービスコンテナが未定義** — `DB_HOST: 127.0.0.1` / `DB_PORT: 3306` を設定しているが、MySQL サービスコンテナの `services` 定義がない。テストが実際に DB 接続する場合は CI が通らない。`unittest.mock` で完結しているなら問題なし（動作確認が必要） | `.github/workflows/ci.yml` |
