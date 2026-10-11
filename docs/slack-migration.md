# Signaly 通知機能の Slack 移行（#337）

Signaly を廃止し、通知を Slack へ全面移行する前提で、現状の機能を棚卸しした結果です。
**このドキュメントは判断資料であり、実装は含みません。** 実装は末尾の「段取り」に沿って別 Issue で行います。

- 方針: **Signaly 廃止**（受信ハブとして縮小はしない）
- 判定の凡例: **置換** = Slack へ置き換える / **撤去** = 移行後に消す / **未決** = 人の判断が要る

---

## 1. 機能ごとの要否

### 受信・配信

| 機能 | 場所 | 判定 | 備考 |
|---|---|---|---|
| `POST /webhook/{channel_id}`（Discord 形式） | `backend/main.py` `/webhook/{channel_id}`、`backend/webhook.py` | 置換 | 送信側が Slack Webhook へ直接送る |
| Signaly レガシー形式（`title`/`message`/`level`） | `backend/webhook.py` | 撤去 | 送信側の書き換えは各アプリで必要 |
| 旧チャンネルIDの別名転送（`channel_aliases`） | `backend/main.py` `_resolve_webhook_target` | 未決 | 旧 URL の互換期間をどうするか |
| SSE `/api/stream/{channel}` | `backend/main.py`、`backend/auth.py`（`require_auth_sse`） | 撤去 | Slack がリアルタイム表示を担う |
| API キー（`sk_…`、`/api/*` を外部から叩く） | `backend/auth.py`、`docs/api-key.md` | 未決 | 外部から履歴を読む用途が残るか |

### 送信

| 機能 | 場所 | 判定 | 備考 |
|---|---|---|---|
| Web Push（VAPID・購読・テスト送信） | `backend/push.py`、`scripts/gen_vapid_keys.py` | 撤去 | Slack アプリの通知に任せる |
| 既読同期のサイレント Push | `backend/push.py`、`POST /api/read` | 撤去 | 既読は Slack が持つ |
| 通知設定（チャンネル・グループ別 ON/OFF） | `backend/notification_prefs.py` | 撤去 | Slack の通知設定に任せる |
| ログイン通知（Signaly 自身） | `backend/login_notify.py`、`login_format.py` | 置換 | 下記 2 参照 |
| 「見覚えのない接続元」警告 | `backend/login_origin.py`、`main.py` `_dispatch_notification` | **未決** | 下記 2 参照 |
| CI・デプロイ・リリース通知 | `.github/scripts/signaly-notify.sh`（`ci.yml` / `deploy.yml` ×2 / `release.yml` から呼ぶ） | 置換 | `SIGNALY_WEBHOOK_URL` / `SIGNALY_RELEASE_WEBHOOK_URL` を Slack 用へ |
| Discord 版 CI 通知スクリプト | `.github/scripts/discord-notify.sh` | 撤去 | **どのワークフローからも参照されていない未使用ファイル**（置換ではなく削除） |

### フロントエンド

チャンネル一覧・並び替え・統合・削除、グループ、未読カウント、バッジ、既読化、全削除、
履歴・検索・送信元フィルタ、Push 設定ダイアログ、Service Worker（`sw.js`）は、
**いずれも Slack で代替できるため撤去**します。

認証（Supabase）・PWA そのものを残すかは未決です（通知が無くなると画面の用途が無い）。

---

## 2. ログイン通知は「宛先差し替え」だけでは済まない

`login_notify.py` は共通フォーマットのペイロードを `httpx.post` で送るだけです。
「見覚えのない接続元か」の判定（タイトルを `⚠️ … ログイン（初めての接続元）` へ書き換え、
黄色にする）は、**受信側**の `main.py` の `_dispatch_notification` が
`login_origin.annotate` を呼んで行っています（範囲の記憶は `login_origins` テーブル）。

Signaly を廃止すると、受信側が無くなるため**この警告は消えます**。選択肢:

| 案 | 内容 |
|---|---|
| A | 各アプリの**送信側**で判定し、警告を載せた状態で Slack へ送る（判定コードと記憶先の DB が各アプリに要る） |
| B | 警告機能を諦める（Slack には毎回同じ見た目で届く） |
| C | 判定だけを行う最小の受け口を残す（「廃止」方針からは外れる） |

他アプリのログイン通知（`docs/webhook.md` の共通フォーマットで送っているもの）も同じ事情です。

---

## 3. Slack へ通知するもの

| 通知 | 送り元 | 送る価値 | 備考 |
|---|---|---|---|
| CI の成否 | 各リポジトリの GitHub Actions | 高 | 失敗だけに絞るかは未決 |
| デプロイの成否 | 同上 | 高 | |
| リリース（develop→main） | `release.yml` | 中 | |
| ログイン通知 | 各アプリのバックエンド | 高（セキュリティ） | 警告の扱いは 2 の結論次第 |
| 新規ユーザー登録 | 同上 | 中 | |
| SSH ログイン等の個人サーバー通知 | `docs/webhook.md` のシェル例 | 未決 | 現在どのホストが使っているか未確認 |

チャンネル設計は未決です。Signaly では「用途で1本、アプリは `source` で区別」にしていました
（#177）。Slack では**用途別チャンネル（ci / deploy / login）＋メッセージ内にアプリ名**が
同じ考え方になります。

---

## 4. 未決事項（人の判断が要る）

1. Slack の前提: ワークスペース、Incoming Webhook か Bot トークンか、チャンネル設計
2. ログイン通知の「初めての接続元」警告を残すか（2 の A / B / C）
3. API キーと `/api/*` の外部利用が残るか
4. 認証・PWA を残すか、Signaly 全体を止めるか
5. 旧 `/webhook/{id}` URL の互換期間
6. 既存の通知履歴（`notifications` テーブル）を保存するか

---

## 5. 段取り（別 Issue へ分割する案）

撤去の**前に**、送信側の棚卸しが要ります。Webhook URL の残っている場所
（1Password・organization / repository secret・各 `.env`・`~/.pm2/dump.pm2`）は
`docs/webhook.md` の「古いチャンネルを片付ける前の棚卸し」に従って数え上げてください。

1. **Slack の受け口と通知対象の確定**（Slack 側の準備、未決事項 1・2 を決める）
2. **CI・ログイン通知の宛先差し替え**（`signaly-notify.sh`、各アプリの `NOTIFY_*`・`SIGNALY_*_WEBHOOK_URL`、警告の移設）
3. **旧機能と DB テーブルの撤去**（Push・SSE・UI・`login_origins` 等。`discord-notify.sh` の削除もここ）

順序を逆にすると、送信側が生きているのに受け口が消えて通知が黙って失われます。
