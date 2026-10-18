#!/usr/bin/env bash
# VPS の app-deploy-gate（guchi-apps/vps#275・#305）が `deploy` のときに実行する、signaly 固有の
# デプロイ手順（#352）。以前 deploy.yml が SSH 先のシェル（heredoc）で行っていた手順をここへ移した。
#
# 呼ばれ方: cwd は置き場（/home/github-user/apps/signaly）。gate が配布物の展開と .env の更新
# （ワークフローが env で送った変数を KEY="VALUE" の形で書く）を済ませた後に
#   MIGRATE_DATABASE_URL=<マイグレーション用の接続先（.env には書かれない）> PORT=8002 bash deploy/gate-install.sh
# で実行される。終わった後、gate が .env の APP_COMMIT_SHA と 127.0.0.1:$PORT/api/app-version の
# sha の一致を最大60秒待つ（稼働版の照合は gate の役目。ここでは行わない）。
#
# 手順（旧 deploy.yml の heredoc と同じ順）:
#   1. VAPID 秘密鍵（base64 1行で届く）を backend/vapid_private.pem へ戻し、.env からは消して
#      VAPID_PRIVATE_KEY_FILE を書く（PEM は複数行のため gate の env では送れない）
#   2. Supabase Auth 移行で使わなくなった GOOGLE_* を .env から消す（#110）
#   3. venv と依存（deploy/ensure_venv.sh）
#   4. スキーマの反映（backend/migrate_db.py）。DDL 権限を持つマイグレーション専用ユーザーは
#      MIGRATE_DATABASE_URL から取り出し、この1コマンドの間だけ DB_ADMIN_USER / DB_ADMIN_PASSWORD
#      として渡す。**.env へは書かない**（書くと常時稼働する uvicorn まで DDL 権限を持つ。#183）
#   5. user systemd のユニット更新と再起動（deploy/restart-service.sh）
#   6. / が応答するまで待つ（deploy/health-check.sh）
#
# 実行時のデータ（.venv・.env・backend/channels.json・backend/vapid_private.pem・backend/data/）は
# 配布物に入れないため、gate はこれらを消さない。
set -euo pipefail

TARGET_DIR="$(pwd -P)"
ENV_FILE="$TARGET_DIR/.env"
EXPECTED_PORT=8002

[ -f "$ENV_FILE" ] || { echo "error: $ENV_FILE がありません（gate が env を反映していません）" >&2; exit 1; }
[ -f "$TARGET_DIR/backend/main.py" ] || { echo "error: $TARGET_DIR は signaly の置き場ではありません" >&2; exit 1; }

# signaly の待受ポートは signaly.service.template の ExecStart に固定（8002）。gate の照合は PORT を
# 叩くので、ずれていれば照合が必ず失敗する。先に分かる形で止める。
if [ "${PORT:-}" != "$EXPECTED_PORT" ]; then
  echo "error: PORT=${PORT:-（未設定）} ですが、signaly は ${EXPECTED_PORT} で待ち受けます（deploy.yml の PORT を確認してください）" >&2
  exit 1
fi

# systemctl --user / journalctl --user は XDG_RUNTIME_DIR が無いと user bus を見つけられない。
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

# 1・2. .env の後始末（VAPID 秘密鍵の書き出しと不要なキーの削除）
ENV_PATH="$ENV_FILE" PEM_PATH="$TARGET_DIR/backend/vapid_private.pem" python3 - <<'PY'
import base64, binascii, os, pathlib, sys

env_path = pathlib.Path(os.environ["ENV_PATH"])
pem_path = pathlib.Path(os.environ["PEM_PATH"])
lines = env_path.read_text(encoding="utf-8").splitlines()


def key_of(line):
    return line.split("=", 1)[0] if "=" in line else None


def unquote(raw):
    # gate は KEY="VALUE"（\\ と \" をエスケープ）で書く。旧形式（クォートなし）も受ける。
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        out, i, body = [], 0, raw[1:-1]
        while i < len(body):
            ch = body[i]
            if ch == "\\" and i + 1 < len(body):
                nxt = body[i + 1]
                out.append({"n": "\n", "\\": "\\", '"': '"'}.get(nxt, "\\" + nxt))
                i += 2
                continue
            out.append(ch)
            i += 1
        return "".join(out)
    return raw


b64_lines = [l for l in lines if key_of(l) == "VAPID_PRIVATE_KEY_B64"]
drop = {"GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REDIRECT_URI"}
if b64_lines:
    encoded = unquote(b64_lines[-1].split("=", 1)[1])
    try:
        pem = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        print("error: VAPID_PRIVATE_KEY_B64 を base64 として読めません", file=sys.stderr)
        sys.exit(1)
    fd = os.open(pem_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(pem)
    os.chmod(pem_path, 0o600)
    # 秘密鍵そのものは .env に残さない（旧手順と同じ。backend/push.py は VAPID_PRIVATE_KEY_FILE を読む）。
    drop |= {"VAPID_PRIVATE_KEY_B64", "VAPID_PRIVATE_KEY", "VAPID_PRIVATE_KEY_FILE"}
    print("VAPID 秘密鍵を backend/vapid_private.pem へ書き出しました")

out = [l for l in lines if key_of(l) not in drop]
if b64_lines:
    out.append('VAPID_PRIVATE_KEY_FILE="vapid_private.pem"')
tmp = env_path.with_name(env_path.name + ".tmp")
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as fh:
    fh.write("\n".join(out) + "\n")
os.replace(tmp, env_path)
PY

# 3. venv と依存
bash "$TARGET_DIR/deploy/ensure_venv.sh" "$TARGET_DIR"

# 4. スキーマの反映。MIGRATE_DATABASE_URL（mysql://<user>:<URLエンコードしたpass>@<host>:<port>/<db>）
#    から資格情報を取り出し、引数ではなく環境変数で migrate_db.py へ渡す。未設定なら従来どおり
#    アプリ用ユーザーへフォールバックする（migrate_db.py が案内付きで失敗する）。
#    接続先のホスト・ポート・DB 名は従来どおり .env の DB_* を使う。
MIGRATE_DATABASE_URL="${MIGRATE_DATABASE_URL:-}" PYTHON_BIN="$TARGET_DIR/.venv/bin/python" \
  MIGRATE_SCRIPT="$TARGET_DIR/backend/migrate_db.py" python3 - <<'PY'
import os, sys
from urllib.parse import unquote, urlsplit

env = dict(os.environ)
url = env.pop("MIGRATE_DATABASE_URL", "")
if url:
    parts = urlsplit(url)
    if parts.scheme not in ("mysql", "mysql+pymysql") or not parts.username:
        print("error: MIGRATE_DATABASE_URL の形が mysql://<user>:<pass>@<host>:<port>/<db> ではありません", file=sys.stderr)
        sys.exit(1)
    env["DB_ADMIN_USER"] = unquote(parts.username)
    env["DB_ADMIN_PASSWORD"] = unquote(parts.password or "")
else:
    env.pop("DB_ADMIN_USER", None)
    env.pop("DB_ADMIN_PASSWORD", None)
os.execve(env["PYTHON_BIN"], [env["PYTHON_BIN"], env["MIGRATE_SCRIPT"]], env)
PY

# 5. user systemd のユニット更新と再起動
bash "$TARGET_DIR/deploy/restart-service.sh" "$TARGET_DIR"

# 6. / が応答するまで待つ。稼働版の SHA の照合はこの後に gate が行う。
bash "$TARGET_DIR/deploy/health-check.sh" "http://127.0.0.1:${PORT}/"
