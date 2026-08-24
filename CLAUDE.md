# signaly 固有ルール

このリポジトリで作業する Claude Code エージェント向けのルールを記載する。

**GitHub Actions 上での実行は、このリポジトリをチェックアウトしたワークツリーしか参照できない。**
したがって無人実行でも守られる必要があるルールは、このファイルに明文化しておく必要がある。

## このリポジトリの作り

**Node が一切無い。** `package.json` はルートにも `frontend/` にも存在しない。

| 層 | 場所 | 中身 |
|---|---|---|
| バックエンド | `backend/` | Python。依存は `backend/requirements.txt` |
| フロントエンド | `frontend/` | **素の HTML / JS**。ビルドもバンドラもパッケージマネージャも無い |
| 補助スクリプト | `scripts/` | **すべて Python か bash**。Node を使うものは1つも無い |

**`npm`・`npx`・`node` を探さないこと。** フロントエンドを変更したら、ブラウザで動くことが
そのまま成果物になる。ビルド手順は無い。

### 検証コマンド

| 目的 | コマンド |
|---|---|
| 依存のインストール | `pip install -r backend/requirements.txt` |
| バックエンドのテスト | `DB_NAME=ci_signaly python -m unittest discover -s backend -p 'test_*.py' -v` |

**`DB_NAME` を必ず渡すこと。** `backend/database.py` が import 時に要求する。
**実際のDB接続はしない**（テストは全てDBをモックしている）ので、値は何でもよい。
CI は `ci_signaly` を使っている。**忘れると import の時点で落ちる。**

**Lint は無い。** CI（`.github/workflows/ci.yml`）も `backend` ジョブだけで、
実行しているのは上記の unittest のみ。

### エンドポイントは MySQL 無しで検証できる

`backend/main.py` を import しても **DB へは接続しない**（`create_engine` は遅延接続で、
`init_db()` は lifespan の中でしか呼ばれない）。そのため FastAPI の `TestClient` で
エンドポイントを直接叩ける。DB に触る3つの関数だけ差し替えればよい。

```python
patch.object(main, "_resolve_webhook_target", lambda cid: ("<channel_name>", None))
patch.object(main, "_save_notification", saved.append)
patch.object(main, "send_push_notifications", lambda entry: None)
```

実例は `backend/test_app_login_endpoint.py`。**ローカルの MySQL は root が auth_socket
認証のため sudo 無しでは繋がらず、`uvicorn` を起動しても `init_db()` で落ちる。**
エンドポイントの動作確認は上記の方式を使うこと。

### 通知チャンネルと送信元（source）

**チャンネルは「用途」で1本、アプリの区別は `notifications.source` で行う。** アプリごとに
CI・ログインのチャンネルを作ると、アプリ数×用途ぶんチャンネルが増える（#177 の時点で20本近く）。
送信元は受信時に自動判定するので、**送信側（各リポジトリのワークフロー・1Password・GitHub
secret）を変えずに済む**。判定の優先順は `X-Signaly-Source` ヘッダー → `?source=` クエリ →
ペイロードの `source` → `fields` の `App` / `Repository` → Discord 形式の `username`
（`backend/webhook.py`）。`/notify/app-login/{app_id}` は `app_id` がそのまま送信元になる。

**HTTP ヘッダーは ASCII しか運べない。** 日本語の送信元名を渡したいときに
`X-Signaly-Source` を使うと文字化けする。`?source=`（URLエンコード）かペイロードの
`source` キーを使うこと。

**統合しても旧チャンネルIDの Webhook URL は生かす。** チャンネルIDは各リポジトリの1Password /
GitHub secret に Webhook URL として散らばっており、統合のたびに全リポジトリを書き換えるのは
現実的でない。`POST /api/channels/{id}/merge` は旧IDを `channel_aliases` に残し、
`_resolve_webhook_target()` が統合先へ転送する。**Webhook を受ける経路で `_fetch_channels()` を
直接引かないこと**——別名を解決できず、統合済みのURLが404になる。

**チャンネル統合のテストはモックせず SQLite に通す。** 履歴を `UPDATE` で移し替える不可逆な
操作なので、戻り値ではなく実際の行を確認する必要がある。`database.py` の engine は import 時に
接続しないため、`create_engine("sqlite://", poolclass=StaticPool)` を作って
`main.get_session` / `notification_prefs.get_session` を差し替えれば MySQL 無しで動く
（実例は `backend/test_channel_merge.py`）。**`StaticPool` を省くと接続ごとに空のDBが作られ、
`no such table` で落ちる。**

**`create_all()` は既存テーブルにインデックスを張らない。** 後から足した列
（`notifications.source`）のインデックスは `_migrate_add_columns()` の中で
`CREATE INDEX` を try/except で流す必要がある。

### バージョン管理

**`package.json` ではなく `version.json`**（`{"version": "1.5.8"}`）。
更新は `scripts/bump_version.py`。**手で書き換えないこと。**

### アイコン

**原本の SVG は3枚ある。用途ごとに要件が違うので、1枚で兼ねられない。**
PNG は `scripts/generate_icons.py` が生成する。**PNGを直接編集しないこと。**

| 原本 | 生成物 | 要件 |
|---|---|---|
| `frontend/icon.svg` | `icon-192.png` / `icon-512.png` | 角丸タイル。そのまま表示される用途（`purpose:any`・タブ・PWA一覧） |
| `frontend/icon-full.svg` | `icon-maskable-512.png` / `apple-touch-icon.png` | **角丸なし・四隅まで不透明。** |
| `frontend/icon-badge.svg` | `badge-72.png` | **前景シルエットのみ・背景透過・単色。** |

**maskable を角丸で作らないこと。** maskable は「全面が不透明で、ランチャーが好きな形に切り抜く」
前提のアセット。`convert -background none` で角丸SVGをPNG化すると四隅が透過のまま残り、
円マスク以外（角丸正方形・正方形）では角から壁紙が透ける。iOS も `apple-touch-icon` の透過を
黒で合成するため、地色を黒以外にすると四隅にだけ黒が残る（#173）。

**通知バッジに `icon-192.png` を流用しないこと。** Android のステータスバーはバッジ画像の
**アルファチャンネルだけ**をマスクとして使い、不透明部分を白一色で塗り潰す。地色入りの
アイコンを渡すと角丸矩形の塊になり、形が一切判別できない（#173）。

**図形は中央 半径205px（キャンバスの80%）の円の内側に収めること。** Androidのランチャーは
maskable のこの安全円の外を切り落とす。v1.6.3以前の稲妻は下端の尖端が206.6pxにあり、
1.8pxだけはみ出していた（見た目の実害はほぼ無い程度だが、新しい図形は余裕をもって内側に）。

**`?v=` は手で書き換えない。** `scripts/bump_version.py` が `manifest.json`・`index.html`・
`api-key-docs.html`・`sw.js` を一括で揃える。したがってアイコンを差し替えても、ブラウザや
インストール済みPWAのキャッシュが入れ替わるのは**次のバージョン更新のタイミング**になる。
**アセットを増やしたら `sw.js` 側の置換が届いているか確かめること**（以前は `icon-192.png`
決め打ちの正規表現だった）。

**`theme_color` / `background_color` はアイコンの地色ではなくUIの色。** 前者はブラウザ/OSの
ツールバー色、後者はPWA起動時スプラッシュの背景色で、実際のUI背景（`--bg: #0d0d0d`）と揃える。
アイコンの地色を変えてもここは追随させない。変える場合は `manifest.json` だけでなく
`index.html`・`api-key-docs.html`・`webhook-docs.html` の `<meta name="theme-color">` も
セットで直すこと（4か所ある）。

### シークレットの取得先

デプロイ・CIが実行時に読むのは**GitHubのsecret / variable**。1Passwordは人が管理する正で、
値を変えたときだけ `scripts/sync-github-secrets.sh` で同期する（対応表は
`.github/secrets-manifest.tsv`）。**`.env.tpl` / `.github/deploy.env.tpl` / `.github/ci.env.tpl`
は廃止済み**（guchi-apps/issue-deck#1302）。

**`.github/workflows/` に `op://` が無いことは、移行が終わった証拠にならない。** `.env.tpl` を
消したことで `deploy/setup.sh` や `scripts/check_vapid_keys.py` のような**ワークフロー外の経路**が
存在しないファイルを参照したまま残っていた（#132）。移行の確認はリポジトリ全体を
`grep -rn '\.env\.tpl'` すること。

本番VPSの `.env` は `deploy.yml` が書き込む。`deploy/setup.sh` は書かないため、新規にVPSを
立てた場合は setup 後に Deploy を1回走らせる必要がある。

### デプロイ後のヘルスチェック

**`deploy/restart-service.sh` の成功は、デプロイの成功を意味しない。** 見ているのは
`systemctl --user restart signaly` の終了コードだけで、これは**ユニットの起動要求が受け付けられた
かどうか**しか表さない。uvicorn が `.env` の不備や依存の欠落で即死しても `Restart=always` で
再起動を繰り返すだけなので、起動の成否は `deploy/health-check.sh`（`http://127.0.0.1:8002/` を
2秒間隔・最大30回）で判定する（#168）。

**ヘルスチェックを `restart-service.sh` の末尾に入れないこと。** `deploy/setup.sh` も
`restart-service.sh` を呼ぶが、新規VPSでは `.env` がまだ無く signaly は起動できない。末尾に
入れると初回セットアップが必ず60秒待って落ちる。呼び出しは `deploy.yml` 側からのみ行う。

## マルチエージェント運用（GitHub Actions 無人実行）

`@claude` コメントを起点に、計画提示〜実装〜develop向けPR作成までを GitHub Actions 上で無人実行する。
ワークフローの実体は `guchi-apps/issue-deck` にあり、このリポジトリの `.github/workflows/` には
`uses:` で参照する薄い caller だけを置いている。**参照タグは全 caller で揃える**（現在は `@workflows/v23`）。

| ファイル | 役割 |
|---|---|
| `claude-issue-dispatch.yml` | `@claude` 起点の無人実行（計画提示・実装・PR作成・質問応答） |
| `issue-labels.yml` | Issueの進捗（Project Status）の状態遷移 |
| `claude-conflict-resolve.yml` | develop向けPRが`develop`とコンフリクトした際の自動解消 |

### 無人実行で使える環境

**`runtime-setup: minimal` を指定している。** インストールするものが何も無いため。
**`pip install -r backend/requirements.txt` は実装エージェント自身が行う。**

**参照タグを `workflows/v10` 未満へ下げないこと。** v9 までは実装ステップの許可ツールが
`pnpm` 固定で、**`python`・`pip`・`pytest` のいずれも実行できなかった**
（guchi-apps/issue-deck#1147）。**このリポジトリは検証手段が Python のテストしか無いため、
下げると検証が一切できなくなる。**

**Python のバージョンは固定されない。** CI は `setup-python` で 3.11 に固定しているが、
共有ワークフローに Python のプリセットは無く、実装ステップは**ランナー標準の Python** を使う。
バージョン依存の挙動に当たった場合は、無理に回避せず `00.check-user` を付けて相談すること。

**`24.screenshot-required` は無人実行では成立しない。** `minimal` のため Playwright が
インストールされない。ローカル実行でのみ意味を持つラベルとして扱う。

設計・運用の詳細は issue-deck 側を参照する。

- 進捗管理の設計: [progress-status-architecture.md](https://github.com/guchi-apps/issue-deck/blob/main/docs/progress-status-architecture.md)
- 無人実行の挙動: [multi-agent/dispatch.md](https://github.com/guchi-apps/issue-deck/blob/main/docs/multi-agent/dispatch.md)

**`/install-github-app` を実行しないこと。** 生成される素の `claude.yml` は
`claude-issue-dispatch.yml` と同じ `issue_comment` イベントで起動するため、1つのコメントで
Claude が二重に走る（`subscription-lists` で実際に起きた）。

### `.shared-context/` と `.shared-prompts/`

無人実行のたびにワークツリーへcheckoutされる**リポジトリ管理外**のディレクトリ。
`.gitignore` 済み。**編集・`git add`・コミットを一切行わないこと。**

## ブランチ運用

- `main` は本番と一致するリリース用ブランチ。直接pushは禁止し、`develop` → `main` のPRのみで進める
- `develop` が日常の開発ブランチ。**デフォルトブランチは `develop`**（`issues`・`issue_comment`
  イベントはデフォルトブランチのワークフローしか起動しないため、変更すると無人実行が動かなくなる）
- Issue専用ブランチは `develop` から作成し、ブランチ名は **`issue-<Issue番号>`** とする（例: `issue-113`）。
  ワークフローはブランチ名から対象Issueを特定するため、**この命名規約に従わないブランチはすべて対象外**になる

## Issueの進捗

**進捗は GitHub Projects の Status で管理する。進捗ラベルは存在しない**
（issue-deck#1010 / #991 Phase 5 で `01.wip`〜`09.main` を廃止した）。

1. `Ready` — 未着手
2. `Planning` — 計画検討中（`21.plan-required` 選択時のみ経由）
3. `Implementation` — 実装中
4. `Develop PR` — developへPR作成・マージ中
5. `Develop` — developへマージ完了（main未反映）
6. `Release` — mainへPR作成・マージ中
7. `Done` — mainへマージ完了。この時点でissueをcloseする

**`gh issue edit` で進捗を進めることはできない。** Status を書けるのは issue-deck だけで、
ワークフローは進捗報告API（`POST /api/progress`）へ報告する。ブランチのpush・PR作成・PRマージを
トリガーに自動で遷移するため、エージェントが自分で進捗を動かす必要はない。

## 条件を表すラベル（進捗とは別軸）

| ラベル | 意味 |
|---|---|
| `00.check-user` | ユーザーの確認・指示が必要。どの段階でも併用する |
| `00.qa-answered` | 質問への回答のみ完了（`00.check-user` と常に併用） |
| `11.local` | ローカル（VSCode等）で対応中。付いている間は無人実行を起動しない |
| `21.plan-required` | 実装前に計画を提示し承認を得る |
| `22.merge-confirm-required` | 内容によらず、developへのマージ前に必ず `00.check-user` を付ける |
| `23.preview-required` | PR作成前に開発サーバーでの画面確認を必須にする |
| `24.screenshot-required` | PR作成前にスクリーンショット取得を必須にする（**無人実行では成立しない**） |

## 自動マージ不可カテゴリ

以下に該当する変更は自動マージせず `00.check-user` を付与してユーザーの確認を待つ。

- 認証・認可（`backend/auth.py`）
- Web Push の鍵まわり（`scripts/gen_vapid_keys.py`・`scripts/check_vapid_keys.py`・`backend/push.py`）
- DB スキーマ（`backend/database.py`）
- 本番環境の設定（`deploy/`）
- GitHub Actionsやデプロイ設定（`.github/workflows/**`）
- Secretsや環境変数（`.env*`・`.env.tpl`・`channels.example.json`）
- 大規模な依存関係の更新（`backend/requirements.txt`）
- `develop` → `main` のマージ

## 実装エージェントの禁止事項

- `main` / `develop` への直接コミット・push
- 他Issueのブランチの編集
- 不要なforce push
- 自分が作成したPull Requestの自己マージ
- `.shared-context/` / `.shared-prompts/` の編集・コミット
- `version.json` の手動編集（`scripts/bump_version.py` を使う）

## コミット・PR・コメントの書き方

- コミットメッセージ・PRタイトル・PR本文・issueコメントは**日本語**で書く
- コミットの author は `Claude Code <claude-code@example.com>` にする
- `develop` 宛のPR本文には、対応Issue・実装内容・テスト内容・確認方法・注意点を記載する。
  developマージ時点ではissueをcloseしない運用のため、`closes #番号` / `fixes #番号` は使わず
  `#番号` のみ記載する

## 依存関係の追加

新しい依存関係を追加する前には、必ずユーザーに確認を取る。無人実行では確認相手がいないため、
追加が必要だと判断した場合は追加せずに作業を止め、`00.check-user` を付与したうえで
なぜ必要かをIssueコメントで相談する。

**フロントエンドにパッケージマネージャを導入する場合は特に相談すること。** 現状 Node が
一切無い前提で `runtime-setup: minimal` にしているため、caller の見直しが必要になる。
