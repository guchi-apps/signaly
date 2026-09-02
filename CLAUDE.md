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

**シークレットの並びは `scripts/generate-workflow-env-block.sh` の出力と突き合わせて確かめる。**
`.github/secrets-manifest.tsv` を編集したら、生成結果が `deploy.yml` のジョブ `env:` ブロックと
一致することを確認する（このリポジトリに順序チェックのCIは無く、ズレても誰も気付かない）。

```bash
diff <(bash scripts/generate-workflow-env-block.sh) <(sed -n '64,85p' .github/workflows/deploy.yml)
```

### エンドポイントは MySQL 無しで検証できる

`backend/main.py` を import しても **DB へは接続しない**（`create_engine` は遅延接続で、
`init_db()` は lifespan の中でしか呼ばれない）。そのため FastAPI の `TestClient` で
エンドポイントを直接叩ける。DB に触る3つの関数だけ差し替えればよい。

```python
patch.object(main, "_resolve_webhook_target", lambda cid: ("<channel_name>", None))
patch.object(main, "_save_notification", saved.append)
patch.object(main, "send_push_notifications", lambda entry: None)
```

実例は `backend/test_login_origin.py`。**ローカルの MySQL は root が auth_socket
認証のため sudo 無しでは繋がらず、`uvicorn` を起動しても `init_db()` で落ちる。**
エンドポイントの動作確認は上記の方式を使うこと。

### 認証（Supabase Auth）

ログインは**他アプリと共通の Supabase Auth（Google）**。バックエンドは
`Authorization: Bearer <access_token>` を受け取り、`backend/supabase_auth.py` が
Supabase の JWKS で署名を検証する（#110）。

**JWT をデコードだけで通さないこと。** ペイロードは誰でも作れる。`PyJWT` の
`PyJWKClient` で公開鍵を引き、`exp` / `iss` / `aud` まで検証する。許可ユーザーの判定は
`ALLOWED_EMAILS` で**API 側でも**行う（403 を返す。401 にするとフロントエンドが
「トークンを更新すれば通る」と誤解する）。

**セッション Cookie を消さないこと。** `EventSource` は Authorization ヘッダーを
付けられないため、SSE（`/api/stream/{channel}`）だけは Cookie で通す。この Cookie は
`POST /auth/session` が**検証済みの JWT と引き換えにのみ**発行するもので、独自の
ログイン経路ではない。**URL へアクセストークンを載せる回避策を採らないこと**
（アクセスログに残る）。

**Cookie へフォールバックする順序に注意。** `require_auth` は Bearer の検証に失敗したら
そこで 401/403 を返し、Cookie へ落ちない。落とすと、期限切れトークンを持つ端末が
古い Cookie でいつまでも通り続ける（`backend/test_supabase_auth.py` が固定している）。

**フロントエンドは `SUPABASE_PUBLISHABLE_KEY` のみを使う。** 値はリポジトリへ埋め込まず
`GET /api/auth/config` から配る。`service_role` キーはフロントエンドにもリポジトリにも
置かない。ビルドが無いため `supabase-js` は esm.sh から動的 import する
（`frontend/auth.js`）。**API を叩くときは必ず `SignalyAuth.fetch` / `authFetch` を通すこと。**
素の `fetch` では Authorization が付かず 401 になる。

**Supabase プロジェクトは開発用と本番用で分ける。** 本番の値は organization の variable
（`SUPABASE_PROJECT_URL` / `SUPABASE_PUBLISHABLE_KEY`）から取り、開発用の値は
`.env.local` へ直接書く（1Password 依存を避けるため）。Redirect URLs には
`<ベースURL>/auth/callback` を登録する。

**Supabase の Database Webhooks でログイン通知を作る経路（`/notify/app-login/{app_id}`）は
削除済み（#209）。復活させないこと。** Supabase プロジェクトは複数アプリで共有していて
`auth.users` / `auth.sessions` はプロジェクトに1つしかなく、そこへ掛けた Database Webhook は
**どのアプリへのログインでも**発火する。`{app_id}` は設定時に選んだ**表示名にすぎない**ため、
他アプリへのログインが常に同じアプリ名で通知されていた。ペイロード（Supabase の行データ）に
アプリを区別できる情報が無く、**受信側で直しようがないので経路ごと消した**。

**ログイン通知は必ずアプリ自身が送る。** フロントエンドが認証コールバックを終えた時点で
自分のバックエンドを叩き、そこから `source` 付きで共通チャンネルへ送る。Signaly 自身の
ログイン通知は `POST /auth/session` に `event: "login"` が付いたときだけ `login_notify.py`
から送る（この `event` を付けるのは `frontend/auth/callback.html` だけ。**トークン更新のたびに
付けないこと**——ログインしていないのに通知が飛ぶ）。

### 通知チャンネルと送信元（source）

**チャンネルは「用途」で1本、アプリの区別は `notifications.source` で行う。** アプリごとに
CI・ログインのチャンネルを作ると、アプリ数×用途ぶんチャンネルが増える（#177 の時点で20本近く）。
送信元は受信時に自動判定するので、**送信側（各リポジトリのワークフロー・1Password・GitHub
secret）を変えずに済む**。判定の優先順は `X-Signaly-Source` ヘッダー → `?source=` クエリ →
ペイロードの `source` → `fields` の `App` / `Repository` → Discord 形式の `username`
（`backend/webhook.py`）。

**HTTP ヘッダーは ASCII しか運べない。** 日本語の送信元名を渡したいときに
`X-Signaly-Source` を使うと文字化けする。`?source=`（URLエンコード）かペイロードの
`source` キーを使うこと。

**統合しても旧チャンネルIDの Webhook URL は生かす。** チャンネルIDは各リポジトリの1Password /
GitHub secret に Webhook URL として散らばっており、統合のたびに全リポジトリを書き換えるのは
現実的でない。`POST /api/channels/{id}/merge` は旧IDを `channel_aliases` に残し、
`_resolve_webhook_target()` が統合先へ転送する。**Webhook を受ける経路で `_fetch_channels()` を
直接引かないこと**——別名を解決できず、統合済みのURLが404になる。

**旧チャンネルを片付ける前に、Webhook URL がどこに残っているかを数え上げる（#212）。** 探す場所は
1Password・organization secret・repository secret・VPS/サブPCの `.env`・**`~/.pm2/dump.pm2`** の5つ。
最後の1つは `pm2 save` 時点の環境変数が固まっており、`.env` を直しても `pm2 resurrect` で古いURLが
復活する。**チャンネル名からは送信元を推測しないこと**——名前が一致していても送信側が生きているとは
かぎらず、逆に無関係な名前のアイテムが別チャンネルを指していることがある（突き合わせるのは
チャンネルIDだけ）。手順の正は `docs/webhook.md` の「古いチャンネルを片付ける前の棚卸し」。

**統合ダイアログの「送信元名」を既定のまま（＝統合元のチャンネル名）にしないこと。** 送信元チップは
文字列そのままで分かれるため、そのリポジトリの `NOTIFY_APP` と揃えないと同じアプリが2つのチップに
割れる（#204 と同じ理由）。

**ログイン通知も全アプリ共通の1チャンネルへ集約する（#192）。** CI・デプロイ通知
（guchi-apps/issue-deck#2255）と同じ方針。既存チャンネルを寄せる手段はチャンネル統合（merge）で、
統合すれば旧チャンネルIDの URL がそのまま共通チャンネルへ届き、送信元も保たれる。

**Signaly 自身のログイン通知の値は organization secret `SIGNALY_LOGIN_WEBHOOK_URL`
（可視性 all・1Password の正は `op://apps/Notify/login-webhook-url`）から受け取る（#200）。**
`.github/secrets-manifest.tsv` の該当行は `inherit` で、`scripts/sync-github-secrets.sh` は
スキップする。**同名の repository secret を作らないこと**——repository secret は同名の
organization secret を覆い隠すため、アプリ別チャンネルへ戻る。旧名 `LOGIN_WEBHOOK_URL` の行は
本番の `.env` に残るが、`deploy.yml` の `sync_env_var` は書き込む鍵しか触らず、アプリはもう
読まないため害はない。

**通知先の設定ミスは何も表面化しない。** URL が空でも `deploy.yml` は空値を `.env` へ書き、
`login_notify.py` は空なら例外もログも出さずに `return` する。環境変数名を変えたときは、
`deploy.yml`（`env:` / `envs:` / `b64` / `sync_env_var` の4か所）・`.env.example` /
`.env.local.example`・`backend/login_notify.py` を必ず揃えること。

**アプリ固有の Webhook URL を共通チャンネルのURLへ差し替えるときは、送信側が
ペイロードに `source` を入れているか先に確かめること。** 統合（merge）で寄せた場合は
`channel_aliases.source` が効くため送信側を変えなくてもアプリを区別できるが
（`backend/main.py` の `source=explicit or parsed.get("source") or alias_source`）、
共通チャンネルのIDを直接叩くと別名を経由せず、この救済が無くなる。**先に `source` を
足してから差し替えること**——順序を逆にすると、集約した瞬間にどのアプリのログインか
分からなくなる。#204 で全アプリの `notifySignalyLogin` を共通フォーマット（`source` 付き）
へ揃えたが、**そのPRがマージされていないアプリでは依然 `source` が付かない**ので、
差し替える前にそのリポジトリの `signaly.ts` を実際に見て確かめること。

**ログイン通知の形の正は `docs/webhook.md` の「ログイン通知の共通フォーマット」（#204）。**
1本のチャンネルへ集約している以上、送る側がばらばらの形で送ると同じ種類の通知に見えない。
**Signaly は受け取った通知を整え直さない**——届いたものはそのまま保存するので、揃えるのは
送る側の役目になる。Signaly 自身が送る `backend/login_notify.py` は
`backend/login_format.py` を通してこの形を組み、他アプリ向けの
コピー元テンプレート（Next.js / Python）も `docs/webhook.md` に置いてある。**受信側で
整形し直す作りにしないこと**——形の定義が送信側と受信側の2か所に散り、通知の中身が
「送信側の書いたとおり」でなくなる。

**ログイン通知の `source` は、そのリポジトリの CI / デプロイ通知の `NOTIFY_APP` と同じ値に
する（#204）。** 通知一覧の送信元チップは文字列そのままで分かれるため、ずれると同じアプリが
2つのチップに割れる。**「リポジトリ名へ揃える」と思い込まないこと**——`_source_from_fields()` は
`App`（`signaly-notify.sh` が `NOTIFY_APP` から作る）を `Repository` より**先に**見るので、
`NOTIFY_APP` を設定しているリポジトリの CI 通知の送信元は表示名になる（car-care は `Car Care`、
portfolio は `Portfolio`、asset-manager は `Asset Manager`）。#204 以前はここが割れていた。

**`接続元IP` というフィールド名を変えないこと。** `backend/login_origin.py` が
「見覚えのない接続元からのログインか」を名前で引いて判定している（`login_format.FIELD_IP`）。
名前を変えると警告が黙って効かなくなる。判定は送信元ごとに IPv4 は /24・IPv6 は /48 へ
丸めた範囲で覚える（`login_origins` テーブル）。**完全一致で覚えないこと**——モバイル回線は
接続のたびにIPが変わるため毎回警告になり、意味がなくなる。**そのアプリで1件目の通知は
覚えるだけで警告しない**（全アプリの1回目が必ず黄色になるのを防ぐ）。

**ログイン通知の集約に Database Webhooks を使わないこと。受け口（`/notify/app-login/{app_id}`）は
#209 で削除した。** `{app_id}` は表示名にすぎず、Supabase プロジェクトを共有している以上、
どのアプリへのログインでも同じ Webhook が発火する。集約先が1本になると、この不一致が
そのまま「全部同じアプリからに見える」という形で表面化した（実際 ops-dashboard を名乗る
通知が、issue-deck へのログインでも飛んでいた）。**受信側では区別する手がかりがペイロードに
無いため、直せるのは送信側だけ。**

**OAuth のコールバックを Supabase がホストしていて通知を差し込む場所が無いアプリは、
Signaly 自身と同じ形を取る。** フロントエンドが認証コールバックを終えた時点で自分の
バックエンドを叩き、そこから `source` 付きで共通チャンネルへ送る
（`frontend/auth/callback.html` → `POST /auth/session` → `login_notify.py`）。
Next.js のアプリなら `/auth/callback` の Route Handler がその場所になる
（ops-dashboard の `src/app/auth/callback/route.ts` が実例）。**フロントエンドを持っている
アプリは必ずこちらを選べる。**

**チャンネル統合のテストはモックせず SQLite に通す。** 履歴を `UPDATE` で移し替える不可逆な
操作なので、戻り値ではなく実際の行を確認する必要がある。`database.py` の engine は import 時に
接続しないため、`create_engine("sqlite://", poolclass=StaticPool)` を作って
`main.get_session` / `notification_prefs.get_session` を差し替えれば MySQL 無しで動く
（実例は `backend/test_channel_merge.py`）。**`StaticPool` を省くと接続ごとに空のDBが作られ、
`no such table` で落ちる。**

**`create_all()` は既存テーブルにインデックスを張らない。** 後から足した列
（`notifications.source`）のインデックスは `_migrate_add_columns()` の中で
`CREATE INDEX` を try/except で流す必要がある。

### 既読と Web Push（既読は端末ローカル・同期は通知を出さない Push）

**既読状態はサーバーに持っていない。** `lastReadAt` / `unread` は各端末の localStorage
（`signaly-last-read` / `signaly-unread`）にしか無く、DB にも API にも既読の概念は無い。
**「既読になったから Push を送らない」という作りにはできない**——Push は webhook を受けた
瞬間に全端末へ送るので、送信時点の通知は必ず未読。既読を反映できるのは「すでに表示されて
いる OS 通知を閉じる」方向だけで、**閉じられるのはその通知を出した端末の Service Worker
だけ**（他端末の通知にはどの API からも触れない）。そのため #216 では、既読にした端末が
`POST /api/read` を叩き、サーバーが本人の Push 登録（`_fetch_subscriptions_for_email`）へ
`{"type":"read","channels":[…]}` を配り、各端末の SW が `getNotifications()` で閉じている。

**この既読同期は通知を出さない Push（サイレント Push）である。** ブラウザによっては、通知を
表示しない Push を繰り返すと「バックグラウンドで更新されました」という代替通知を出したり、
購読そのものを失効させたりする。**1回の既読操作につき1通に抑えること**——`POST /api/read` は
チャンネルの配列を受け、フロントエンドは 500ms デバウンスでまとめる（「すべて既読にする」は
チャンネル数ぶんループするため、まとめないと一気に何通も飛ぶ）。**未読が1件も無いチャンネルを
既読にしたときは送らない**（`markChannelRead` は表示中チャンネルへの新着ごとにも呼ばれる）。

**閉じる通知は必ず時刻で絞る。** Push のペイロードに通知自身の時刻 `ts` を載せ、SW 側で
`ts <= until` のものだけ閉じる。チャンネル名だけで閉じると、**既読にした後に届いた通知まで
消える**（既読同期が遅れて届くほど起きやすいので、`ttl=60` で溜め込ませない）。

**受け取った既読をそのまま送り返さないこと。** SW → ページの `read-sync` で
`markChannelRead()` を呼ぶと、そのまま `POST /api/read` へ戻って端末間で往復する。
`applyRemoteRead()` はフラグで送信を止めている。

**未読件数を数える範囲は表示場所ごとに違う（#241）。** アプリのバッジ・タブタイトル・右上
ベルマークの件数（`totalUnread()`）と、ベルマークの未読一覧（`loadUnreadMessages()`）は
**通知を有効にしているチャンネルの未読だけ**を数える。サイドバーのチャンネル別バッジ・
グループのバッジ（`updateBadge()` / `groupUnreadTotal()`）は今まで通り**全ての未読**を数える
（通知を切ったチャンネルにも新着が来ていることは、アプリを開けば分かる状態を保つ）。

**この判定に `isNotificationEnabled()` を直接使わないこと。** 同関数は通知設定を読み込む前
（`notificationPrefsReady === false`）を `false` として返すため、そのまま件数へ使うと起動直後の
バッジとベルの件数が一瞬 0 になる。件数側は `countsTowardUnreadBadge()` を通し、**未読込は
「有効」として数えて**おき、`refreshNotifIndicators()`（通知設定の読み込み・変更のたびに
呼ばれる）から数え直す。**Push 側は元から整合している**——`backend/push.py` が
`resolve_notification_enabled()` で宛先を絞るため、無効チャンネルの Push はそもそも届かず、
Service Worker のバッジ加算（`incrementAppBadgeCount()`）も走らない。

### DBスキーマの反映（アプリ起動時にDDLを流さない）

**`backend/main.py` の lifespan から `init_db()`（`create_all`）を呼ばないこと。** 本番のDB
ユーザーは用途で分かれていて、常時稼働するアプリ用ユーザーは SELECT / INSERT / UPDATE /
DELETE しか持たない（正は `guchi-apps/vps` の `docs/web-stack.md`）。起動のたびに
`create_all()` を流す作りだと、**モデルを1つ足した瞬間にデプロイが落ちる**——
`create_all()` は既存テーブルには `CREATE TABLE` を発行しないため、それまでは権限が無くても
通ってしまい、新規テーブルが増えた回だけ `CREATE command denied` で起動に失敗する。#183 は
`channel_aliases`（#177で追加）で実際にこれを踏み、本番が503になった。

**スキーマの反映は `backend/migrate_db.py` だけが行う。** `.github/workflows/deploy.yml` が
`ensure_venv.sh` の後・`restart-service.sh` の前に1回実行する。DDL権限を持つのは
マイグレーション専用ユーザー（organizationの `SHARED_DB_MIGRATE_USER` /
`SHARED_DB_MIGRATE_PASSWORD`）だけで、**この1コマンドの実行中だけ `DB_ADMIN_USER` /
`DB_ADMIN_PASSWORD` として環境変数で渡す。`.env` へは書かないこと**——書くと常時稼働する
uvicorn まで DDL 権限を持つ。未設定ならアプリ用ユーザーへフォールバックする
（ローカル向け。本番では必ず渡る）。

**マイグレーション専用ユーザーには `app_%` へのワイルドカード GRANT が既に付いている。DBごとの
個別 GRANT は要らない。** VPS の MySQL では
`GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, REFERENCES, INDEX, ALTER ON app_%.*`
が付与済みで、MySQL は**DB名の部分に限り** `_` / `%` をパターンとして解釈する
（`mysql.db` に LIKE 相当で格納される）。そのため `app_signaly` を含む
`app_` 始まりのDBは、後から作っても自動的に対象になる（#205 で実機の `SHOW GRANTS` により確認、
2026-08-27）。**アプリを追加するたびに GRANT を付与する手作業Issueを起票しないこと。**

個別の GRANT が要るのは**DB名が `app_` で始まらない場合**だけ。権限が無ければ `migrate_db.py` が
GRANT 文を添えて失敗するので、そのメッセージが出たときにVPS上で手作業で付与する
（`guchi-apps/vps` の `mysql/` はデプロイの対象外で、`deploy.yml` の `paths` に無いため
コードとしては流せない）。現状の確認は VPS 上の
`sudo mysql -N -e "SHOW GRANTS FOR '<マイグレーション専用ユーザー>'@'localhost'"`。

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
