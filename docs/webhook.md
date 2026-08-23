# Signaly Webhook API マニュアル

外部サービスや CI/CD から Signaly に通知を送るための Webhook 仕様です。

**Discord Execute Webhook と同じ JSON 形式**で POST できます。既存の Signaly 形式（レガシー形式）も引き続き利用可能です。

---

## 概要

| 項目 | 内容 |
|------|------|
| メソッド | `POST` |
| エンドポイント | `/webhook/{channel_id}` |
| Content-Type | `application/json`（推奨）または `multipart/form-data`（`payload_json` フィールド） |
| 認証 | **不要**（URL に含まれる `channel_id` が宛先の識別子） |
| 文字コード | UTF-8 |
| 形式判定 | トップレベルに `content` / `embeds` / `username` などの Discord 系キーが**1つでもあれば** Discord 形式、なければレガシー形式として扱われる |

Webhook URL は Signaly にログイン後、**Webhook URL** 画面でチャンネルごとに確認できます。

```
https://<your-host>/webhook/<channel_id>
```

> ペイロードの形を自分で決められない送信元（Supabase の Database Webhooks など）向けには、
> 専用の受け口があります。→ [アプリのログイン通知（Supabase Database Webhooks）](#アプリのログイン通知supabase-database-webhooks)

**内部データモデルについて:** 受信したペイロードは形式によらず、最終的に `title` / `message` / `level` / `color` / `fields` / `source` の6項目に正規化されて保存・配信されます。Discord 形式の `content` はそのままの形では保持されず、後述のルールで `title` / `message` に変換されます。

---

## 送信元（`source`）

チャンネルを用途ごと（CI・ログインなど）に1本へまとめると、1つのフィードに複数のアプリの
通知が混ざります。**どのアプリから来たかは `source` として通知ごとに保存**され、通知カードの
バッジと、チャンネル上部の絞り込みチップに使われます。

送信元は次の順で決まります。**送信側で何も指定しなくても、CI 通知は `App` フィールドから
自動で付きます**（`.github/scripts/signaly-notify.sh` はこのフィールドを必ず載せています）。

| 優先 | 決まり方 | 例 |
|------|---------|-----|
| 1 | リクエストヘッダー `X-Signaly-Source` | `X-Signaly-Source: signaly` |
| 2 | クエリパラメータ `?source=` | `/webhook/<channel_id>?source=バックアップ` |
| 3 | ペイロードの `source` キー | `{"message": "...", "source": "cron"}` |
| 4 | `fields` の `App` → `Repository` | `{"name": "App", "value": "Signaly"}` → `Signaly` |
| 5 | Discord 形式の `username` | `{"username": "Grafana"}` → `Grafana` |

- `Repository` から拾う場合はバッククォートを外し、`owner/repo` の**リポジトリ名だけ**を使います（`` `guchi-apps/car-care` `` → `car-care`）。
- 100 文字を超える場合は切り詰められます。
- **HTTP ヘッダーは ASCII しか運べません。** 日本語の送信元名を使う場合は `?source=`（URL エンコード）かペイロードの `source` キーを使ってください。
- どれにも当てはまらない場合、送信元は未設定になります（絞り込みチップでは「送信元なし」として扱われます）。

```bash
curl -X POST "$SIGNALY_WEBHOOK_URL" \
  -H "Content-Type: application/json" \
  -H "X-Signaly-Source: backup-job" \
  -d '{"title":"バックアップ完了","message":"3.2GB"}'
```

### チャンネルを統合したあとの Webhook URL

チャンネル設定の **別のチャンネルへ統合** で1本にまとめると、統合元のチャンネルは消えますが
**統合元の Webhook URL はそのまま使えます**（統合先へ転送されます）。各リポジトリの
1Password / GitHub secret を書き換える必要はありません。

転送されてきた通知の送信元は、上の表で決まらなかった場合にかぎり、統合時に指定した送信元名
（既定は統合元のチャンネル名）が使われます。

---

## Discord Webhook 形式（推奨）

[Discord Execute Webhook](https://discord.com/developers/docs/resources/webhook#execute-webhook) と同じペイロードをそのまま送れます。Discord の Webhook URL を Signaly の URL に差し替えるだけで、多くのツールがそのまま動作します。

### トップレベルフィールド

| フィールド | 型 | 説明 |
|-----------|-----|------|
| `content` | string | プレーンテキスト本文（最大 2000 文字想定。文字数制限は Signaly 側では未チェック） |
| `embeds` | array | 埋め込みオブジェクト（最大 10 件想定。件数制限は Signaly 側では未チェック） |
| `username` | string | 送信者名の上書き。**`content` が空、かつどの `embeds[].title` も指定されていないときのみ**タイトルとして使われる（後述） |
| `avatar_url` | string | 無視（表示に一切使われない） |
| `tts` | boolean | 無視 |
| `allowed_mentions` | object | 無視 |
| `components` | array | 無視 |
| `attachments` | array | 無視（ファイル添付は未対応） |

`content` と `embeds` の少なくとも一方を含めてください（Discord と同様。どちらも省略するとタイトル・本文とも空の通知になります）。

### `content` / `embeds` から `title` / `message` への変換ルール（重要）

Signaly には `content` というフィールドは存在せず、常に `title`（タイトル）と `message`（本文）に変換されます。組み合わせによって挙動が変わるため注意してください。

| 入力の組み合わせ | `title` | `message` |
|---|---|---|
| `content` のみ・1 行 | `content` 全文 | `""`（**本文は空になり、タイトルだけの通知になる**） |
| `content` のみ・複数行 | 1 行目 | 2 行目以降 |
| `content` + `embeds` | 先頭 embed の `title`（無ければ `username` フォールバック） | `content` 全文 →（改行区切りで）→ 各 embed の `description`（配列の順） |
| `embeds` のみ | 先頭 embed の `title`（無ければ `username` フォールバック） | 各 embed の `description` を改行区切りで連結 |

つまり `content` と `embeds` を同時に使うと、**`content` が最初のパラグラフ、その下に `embeds[].description` が続く**形で本文に表示されます。タイトルは `content` の 1 行目からは取られず、embed 側（`embeds[0].title` 優先）が使われます。

`username` がタイトルに反映されるのは「`content` が空」かつ「どの embed にも `title` がない」場合のみです。実運用では `content` か `embeds[].title` のどちらかを指定することがほとんどのため、**`username` はほぼ常に無視されます**。Discord 本来の「送信者名」という意味合いとは異なる、フォールバック専用の値だと考えてください。

### `embeds[]` オブジェクト

| フィールド | 型 | Signaly での扱い |
|-----------|-----|-----------------|
| `title` | string | 通知タイトル（先頭 embed を優先。2 番目以降の `title` は無視） |
| `description` | string | 本文に結合（上表のルール参照） |
| `url` | string | 先頭 embed のみ、タイトルを `[title](url)` リンク化 |
| `color` | integer | 左ボーダー色（**10進数**。例: `5763719` = `#57f287`）。先頭 embed の値のみ採用 |
| `fields` | array | そのまま `fields` として表示（`name` / `value` / `inline`） |
| `author` | object | `{ "name": "string", "url": "https://..." }`。`icon_url` は無視。**通常の `fields` と同じ見た目**で `Author` という名前のフィールドとして追加される（Discord のような専用レイアウト・アイコン表示はない） |
| `footer` | object | `{ "text": "string" }`。名前のないフィールド（末尾）として `footer.text` を追加 |
| `thumbnail` | object | `{ "url": "https://..." }`。`Thumbnail` という名前のフィールドに **URL 文字列がそのまま** 入る（画像プレビューにもリンクにもならない）。`url` が `attachment://` で始まる場合は無視 |
| `image` | object | `{ "url": "https://..." }`。`Image` という名前のフィールドに同上 |
| `timestamp` | string | 無視（受信時刻をサーバーが付与） |

`author` / `footer` / `thumbnail` / `image` はいずれも**内部的には `fields` に変換されて追加される**だけで、Discord のような特別な見た目にはなりません。`fields` とまとめて `[]` 個の項目として上から順に表示されます。

**thumbnail / image をリンクにしたい場合:** 現状 URL がそのまま文字列として表示されるだけでクリックできません。クリック可能にしたい場合は `thumbnail` / `image` ではなく、`fields` に `[表示名](URL)` という Markdown リンク形式で指定してください（`fields[].value` は `` `code` `` と `[link](url)` に対応）。

### リクエスト例

```bash
curl -X POST "https://example.com/webhook/abc123xyz" \
  -H "Content-Type: application/json" \
  -d '{
    "content": "デプロイ完了",
    "embeds": [{
      "title": "v1.2.3",
      "description": "本番環境に反映しました",
      "color": 5763719,
      "fields": [
        {"name": "Branch", "value": "main", "inline": true},
        {"name": "Commit", "value": "`abc1234`", "inline": true}
      ],
      "footer": {"text": "CI bot"}
    }]
  }'
```

上記の場合、タイトルは `"v1.2.3"`、本文は `"デプロイ完了\n\n本番環境に反映しました"` になります（`content` が先、`description` が後）。

### `content` のみ

```bash
curl -X POST "https://example.com/webhook/abc123xyz" \
  -H "Content-Type: application/json" \
  -d '{"content": "Hello from Signaly!"}'
```

1 行だけの `content` は**タイトルとして扱われ、本文は空**になります（上表参照）。複数行の `content` は 1 行目がタイトル、2 行目以降が本文として表示されます。`**ラベル:** 値` 形式は自動ではフィールド化されません（Markdown テキストとして表示）。

フィールド表示が必要な場合は **`embeds` 形式**を使ってください（後述の SSH 通知例を参照）。

```bash
# 1 行目 → タイトル、2 行目以降 → 本文（プレーンテキスト）
curl -X POST "https://example.com/webhook/abc123xyz" \
  -H "Content-Type: application/json" \
  -d '{"content": "🚀 SSHログイン通知\nサーバー: myserver\nユーザー: root"}'
```

### shell スクリプトから送る場合

bash で JSON を手組みすると、ホスト名やユーザー名に `"` や `\` が含まれたとき JSON が壊れることがあります。**`jq` で JSON を生成してください。**

#### フィールド表示したい場合（`embeds` 推奨）

```bash
USER=$(whoami)
IP=$(echo "$SSH_CLIENT" | awk '{print $1}')
DATE=$(date "+%Y-%m-%d %H:%M:%S")
HOSTNAME=$(hostname)

jq -n \
  --arg user "$USER" \
  --arg ip "$IP" \
  --arg date "$DATE" \
  --arg host "$HOSTNAME" \
  '{
    embeds: [{
      title: "SSHログイン通知",
      color: 5763719,
      fields: [
        {name: "サーバー", value: $host, inline: true},
        {name: "ユーザー", value: $user, inline: true},
        {name: "接続元IP", value: $ip, inline: true},
        {name: "日時", value: $date, inline: false}
      ]
    }]
  }' | curl -fsS -X POST "$WEBHOOK_URL" \
  -H "Content-Type: application/json" \
  -d @-
```

#### シンプルなテキスト通知（`content`）

```bash
jq -n \
  --arg user "$USER" \
  --arg ip "$IP" \
  --arg date "$DATE" \
  --arg host "$HOSTNAME" \
  '{
    content: (
      "🚀 **SSHログイン通知**\n" +
      "**サーバー:** \($host)\n" +
      "**ユーザー:** \($user)\n" +
      "**接続元IP:** \($ip)\n" +
      "**日時:** \($date)"
    )
  }' | curl -fsS -X POST "$WEBHOOK_URL" \
  -H "Content-Type: application/json" \
  -d @-
```

`jq` がない場合は Python を使えます（`embeds` 版）。

```bash
python3 - <<'PY' | curl -fsS -X POST "$WEBHOOK_URL" -H "Content-Type: application/json" -d @-
import json, os, socket
from datetime import datetime
print(json.dumps({
    "embeds": [{
        "title": "SSHログイン通知",
        "color": 5763719,
        "fields": [
            {"name": "サーバー", "value": socket.gethostname(), "inline": True},
            {"name": "ユーザー", "value": os.getenv("USER", "unknown"), "inline": True},
            {"name": "日時", "value": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "inline": False},
        ],
    }],
}, ensure_ascii=False))
PY
```

### `embeds` のみ（CI 通知）

```bash
curl -X POST "https://example.com/webhook/abc123xyz" \
  -H "Content-Type: application/json" \
  -d '{
    "embeds": [{
      "title": "✅ [MyApp] CI 成功",
      "color": 5763719,
      "fields": [
        {"name": "Branch", "value": "main", "inline": true},
        {"name": "Run", "value": "[Workflow Run](https://github.com)", "inline": false}
      ]
    }]
  }'
```

### 色の指定（Discord 形式）

Discord と同様、**10進数の整数**で指定します。`embeds[0].color` のみが採用されます（2 番目以降の embed の `color` は無視）。

| 色 | Hex | 10進数 (`color`) |
|----|-----|------------------|
| 緑 | `#57f287` | `5763719` |
| 黄 | `#fbbf24` | `16512804` |
| 赤 | `#ed4245` | `15548997` |

---

## Signaly レガシー形式

Discord 形式のキー（`content` / `embeds` / `username` 等）を一つも含まない JSON は、従来の Signaly 形式として解釈されます。

| フィールド | 型 | デフォルト | 説明 |
|-----------|-----|-----------|------|
| `title` | string | `""` | タイトル |
| `message` | string | `""` | 本文 |
| `level` | string | `"info"` | `info` / `warning` / `error` |
| `color` | string | `null` | 左ボーダー色（CSS hex。例: `#57f287`）。指定時は `level` より優先される |
| `fields` | array | `null` | `[{name, value, inline}]` |

```bash
curl -X POST "https://example.com/webhook/abc123xyz" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "デプロイ完了",
    "message": "v1.2.3 を本番に反映しました",
    "level": "info"
  }'
```

### `level` による自動色分け

`color` を指定しなかった場合のみ、`level` の値に応じて枠線の色が自動で決まります。`color` を指定すると `level` の値に関係なく常にそちらが優先されます。

| `level` | 色（変数） | 実際の色 |
|---|---|---|
| `info`（デフォルト） | `--info` | `#818cf8`（インディゴ） |
| `warning` | `--warning` | `#fbbf24`（アンバー） |
| `error` | `--error` | `#f87171`（レッド） |

この自動色分けは**レガシー形式の `level` にのみ**適用されます。Discord 形式で送信した通知は内部的に常に `level: "info"` になるため（`warning` / `error` を指定する項目が Discord 形式には存在しない）、Discord 形式で色を付けたい場合は `embeds[].color` を明示的に指定してください。

---

## Signaly レガシー形式と Discord 形式の違い

| 項目 | Discord 形式 | Signaly レガシー形式 |
|---|---|---|
| タイトルの入力 | `embeds[0].title` 優先 / `content` 1行のみの場合はそれ / なければ `username` | `title` を直接指定 |
| 本文の入力 | `content` + 各 `embeds[].description`（結合される） | `message` を直接指定 |
| 色の指定 | `embeds[0].color`（10進整数） | `color`（CSS hex 文字列）、未指定なら `level` から自動決定 |
| 重要度（`level`） | 概念なし。内部的に常に `"info"` | `info` / `warning` / `error` を指定可能 |
| 追加フィールド | `embeds[].fields` に加え `author` / `footer` / `thumbnail` / `image` も `fields` に変換されて連結 | `fields` をそのまま使用 |
| 形式の判定 | トップレベルに `content` / `embeds` / `username` 等のいずれかが存在する | 上記キーが一つもない |

---

## multipart/form-data

Discord と同様、ファイル添付時の `multipart/form-data` でも `payload_json` フィールドに JSON を入れれば受け付けます（ファイル本体は現時点では未処理）。

```bash
curl -X POST "https://example.com/webhook/abc123xyz" \
  -F 'payload_json={"content":"multipart からの通知"}'
```

---

## レスポンス

### 成功（200 OK）

```json
{
  "ok": true,
  "id": "550e8400-e29b-41d4-a716-446655440000"
}
```

Discord は `204 No Content` を返しますが、Signaly は通知 ID を返します。

### エラー

| HTTP ステータス | 条件 |
|----------------|------|
| `400 Bad Request` | JSON / `payload_json` が不正、またはオブジェクトでない |
| `404 Not Found` | `channel_id` が存在しない |

**注意:** リクエストボディが空（`Content-Type: application/json` で本文なし、または `payload_json` が未指定）の場合はエラーにはならず、`title` / `message` とも `""` の空の通知として **200 OK** で保存されます。意図しない空通知が飛ぶ可能性があるため、送信側で本文を組み立ててから POST してください。

---

## 表示について

- `content` と `embeds[].description` は本文（`message`）として結合表示（結合順は上記の変換ルール参照）
- `embeds[].fields` と `author` / `footer` / `thumbnail` / `image` は同じ見た目の `fields` として一覧表示（特別なレイアウトの違いはない）
- 本文とフィールドは**両方とも表示**されます
- フィールドの `value` では `` `code` ``・`[link](url)`・`**強調**` / `__強調__`・`*斜体*` / `_斜体_` が使えます（`thumbnail` / `image` の URL 文字列自体はこの記法を通らないため、リンクにはなりません）。タイトル・本文も同じ記法に対応します
- リンクの URL に `_` や `*` が含まれていても壊れません（`https://example.com/session_01ABC` のような URL をそのまま書けます）。`` `code` `` の中身も強調変換されず、書いたとおりに表示されます

---

## 受信後の動作

1. **送信元を判定** — [送信元（`source`）](#送信元source) のルールで決定
2. **DB に保存** — 通知履歴として永続化
3. **SSE で配信** — 該当チャンネルを開いているブラウザにリアルタイム表示
4. **Web Push** — VAPID が設定されていれば、登録済み端末へプッシュ通知（本文の先頭に送信元が付きます）

---

## ローカルでのテスト

```bash
bash scripts/test-notify.sh <channel_id> embed
bash scripts/test-notify.sh <channel_id> simple
bash scripts/test-notify.sh <channel_id> warning
bash scripts/test-notify.sh <channel_id> error
```

---

## 制限・注意事項

- Webhook エンドポイントは**認証なし**です。`channel_id` の漏洩に注意してください。
- ファイル添付（`files[n]`）は未対応です。
- `components`（ボタン等）・`poll` は未対応です。
- 同じ内容を複数回 POST すると、それぞれ別通知として保存されます。
- `content` が 1 行のみの場合、本文は空になりタイトルだけの通知になります。
- `username` は `content` も `embeds[].title` もない場合のみタイトルに使われます（通常はほぼ発生しません）。
- `thumbnail` / `image` は URL 文字列がフィールドにそのまま表示されるだけで、画像プレビューにもリンクにもなりません。
- `author.icon_url` / トップレベルの `avatar_url` は無視されます（表示に使われません）。
- リクエストボディが空の場合はエラーにならず、空の通知として保存されます。

---

## クイックリファレンス（Discord 形式）

```http
POST /webhook/{channel_id}
Content-Type: application/json

{
  "content": "optional plain text",
  "username": "optional fallback title (content/embeds title がない場合のみ使用)",
  "embeds": [{
    "title": "string",
    "description": "string",
    "url": "https://...",
    "color": 5763719,
    "fields": [
      {"name": "string", "value": "string", "inline": true}
    ],
    "footer": {"text": "string"},
    "author": {"name": "string", "url": "https://..."},
    "thumbnail": {"url": "https://..."},
    "image": {"url": "https://..."}
  }]
}
```

---

## アプリのログイン通知（Supabase Database Webhooks）

Google ログインを **Supabase Auth** に統一したアプリ（ops-dashboard など）向けの受け口です。

これらのアプリは OAuth のコールバックを Supabase がホストするため、アプリのバックエンドに
ログイン通知のコードを差し込む場所がありません。代わりに **Supabase の Database Webhooks**
（`auth.users` などの変更をトリガーに HTTP POST する機能）を Signaly へ向けます。
**アプリ側のコードは一切変更しません。**

Database Webhooks が送るペイロードは形式が固定で変更できないため、`/webhook/{channel_id}`
（Discord 形式）では受けられません。このエンドポイントが Supabase の生ペイロードを
通知フォーマットへ変換します。

### 概要

| 項目 | 内容 |
|------|------|
| メソッド | `POST` |
| エンドポイント | `/notify/app-login/{app_id}` |
| Content-Type | `application/json` |
| 認証 | **必要**（宛先チャンネルの `channel_id` をトークンとしてヘッダーで送る） |

```
POST https://<your-host>/notify/app-login/ops-dashboard
X-Signaly-Token: <channel_id>
Content-Type: application/json
```

- `{app_id}` は**通知タイトルに出る表示名**です（例: `🔐 ops-dashboard ログイン`）。
  使える文字は `A-Z a-z 0-9 . _ -` の 1〜64 文字で、外れると `400` になります。
  宛先の決定には使われません（宛先はトークンだけで決まります）。
- **宛先チャンネルはトークンで決まります。** Signaly の **Webhook URL** 画面に出ている
  `https://<your-host>/webhook/<channel_id>` の `<channel_id>` 部分をそのまま使ってください。
- アプリを増やすときは Signaly でチャンネルを作り、そのチャンネルIDを Supabase 側の
  ヘッダーに貼るだけです。Signaly の設定変更・再デプロイは不要です。

### トークンの渡し方

次の順で見ます。上にあるものが優先されます。

| # | 渡し方 | 備考 |
|---|--------|------|
| 1 | `X-Signaly-Token: <channel_id>` | **推奨** |
| 2 | `Authorization: Bearer <channel_id>` | 送信元の UI が `Authorization` しか設定できない場合 |
| 3 | `?token=<channel_id>`（クエリ文字列） | **ヘッダーがどうしても付けられない場合の保険。** URL は Web サーバーのアクセスログに残るため、使えるならヘッダーにしてください |

トークンが無い、または既存のチャンネルIDに一致しない場合は `401` を返し、通知は作られません。

### 受け付けるペイロード

Supabase の Database Webhooks が送る形をそのまま受け取ります。

```json
{
  "type": "UPDATE",
  "table": "users",
  "schema": "auth",
  "record": { "...変更後の行..." },
  "old_record": { "...変更前の行..." }
}
```

| 条件 | 動作 |
|---|---|
| `auth.sessions` の `INSERT` | ログイン通知（🔐） |
| `auth.users` の `INSERT` | 新規ユーザー登録の通知（🎉） |
| `auth.users` の `UPDATE` で `last_sign_in_at` が変化した | ログイン通知（🔐） |
| `auth.users` の `UPDATE` で `last_sign_in_at` が変わらない | **通知しない**（`200` + `{"skipped":"no_sign_in"}`） |
| `type` が `DELETE` | **通知しない**（`200` + `{"skipped":"delete"}`） |
| 上記以外のテーブル | 汎用のイベント通知（🔔）として配信。本文に `schema.table / type` が入る |

`auth.users` の `UPDATE` はログイン以外（メールアドレス変更・メタデータ更新など）でも飛ぶため、
`last_sign_in_at` が動いたときだけログインとして扱っています。

通知しないケースでも **`200` を返します**（Supabase 側のリトライとエラーログを増やさないため）。

### どのテーブルを起点にするか

| 起点 | 取れる情報 | 取れない情報 |
|---|---|---|
| `auth.users`（`UPDATE`） | メール・ユーザー名・プロバイダ・メール確認済 | 接続元IP・User-Agent |
| `auth.sessions`（`INSERT`） | 接続元IP・User-Agent・ユーザーID | メール・ユーザー名 |

両方受け付けるので、両方設定すれば1回のログインで2通届きます。**通常は `auth.users` の
`UPDATE` だけで十分**です（誰がログインしたかが分かるため）。接続元IPも知りたい場合に
`auth.sessions` を足してください。

### 通知に出る項目

受け取った行データのうち、**次の項目だけ**を通知に載せます。

| 表示名 | 取得元 |
|---|---|
| ユーザー | `raw_user_meta_data` の `full_name` → `name` → `user_name` |
| メール | `email` |
| プロバイダ | `raw_app_meta_data.provider` |
| 接続元IP | `ip` |
| メール確認済 | `email_confirmed_at` / `confirmed_at` の有無 |
| ユーザーID | `user_id` / `id`（メールもユーザー名も取れなかった場合のみ） |
| 日時 | `last_sign_in_at` → `created_at` → 受信時刻 |
| User-Agent | `user_agent` |

URL パスの `<app_id>` はタイトル（`🔐 <app_id> ログイン`）に加えて
[送信元（`source`）](#送信元source) にもなります。**複数アプリのログイン通知を1本の
チャンネルへ集約しても、アプリごとに絞り込めます。**

**`auth.users` の行にはパスワードハッシュ（`encrypted_password`）や各種トークン
（`confirmation_token` / `recovery_token` / `email_change_token_*` /
`reauthentication_token`）が含まれます。** 上の表に無い項目は通知にも通知履歴にも
一切出しません。各値は 500 文字で切り詰めます。

### Supabase 側の設定

Supabase プロジェクトのダッシュボードで **Database → Webhooks → Create a new hook** から作ります。

| 設定項目 | 値 |
|---|---|
| Table | `auth` スキーマの `users`（または `sessions`） |
| Events | `Update`（`sessions` を使う場合は `Insert`） |
| Type | HTTP Request |
| Method | `POST` |
| URL | `https://<your-host>/notify/app-login/<app_id>` |
| HTTP Headers | `X-Signaly-Token: <channel_id>` を追加 |

ダッシュボードの Webhooks 画面に `auth` スキーマが出てこない場合は、SQL Editor から
`supabase_functions.http_request` を呼ぶトリガーを直接作ります。

### 動作確認

```bash
curl -i -X POST "https://<your-host>/notify/app-login/ops-dashboard" \
  -H "X-Signaly-Token: <channel_id>" \
  -H "Content-Type: application/json" \
  -d '{
    "type": "UPDATE",
    "table": "users",
    "schema": "auth",
    "record": {
      "id": "00000000-0000-4000-8000-000000000001",
      "email": "you@example.com",
      "last_sign_in_at": "2026-08-17T10:00:00Z",
      "email_confirmed_at": "2026-01-01T00:00:00Z",
      "raw_user_meta_data": {"full_name": "Your Name"},
      "raw_app_meta_data": {"provider": "google"}
    },
    "old_record": {"last_sign_in_at": "2026-08-16T10:00:00Z"}
  }'
```

### レスポンス

| HTTP ステータス | 条件 |
|----------------|------|
| `200 OK` | 通知を作成した（`{"ok":true,"id":"..."}`）／条件に合わず通知しなかった（`{"ok":true,"skipped":"..."}`） |
| `400 Bad Request` | `app_id` が不正、またはボディが JSON オブジェクトでない |
| `401 Unauthorized` | トークンが無い、または存在しないチャンネルID |

### 制限・注意事項

- チャンネルIDは**宛先の識別子であると同時に資格情報**です。`/webhook/{channel_id}` と同じ扱いで、
  漏洩するとそのチャンネルへ偽の通知を投げられます（影響範囲はそのチャンネルに閉じます）。
- `{app_id}` は表示名でしかなく、検証されません。同じチャンネルIDを持っていれば任意の名前で
  通知を投げられます（上記のとおり `/webhook/{channel_id}` でできることと同等です）。
- Supabase の Database Webhooks は `pg_net` を使う非同期送信のため、**Signaly が落ちていた場合の
  ログインは通知されません**（リトライされません）。監査ログの代わりにはなりません。
