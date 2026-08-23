#!/usr/bin/env bash
# 開発環境起動スクリプト
# 起動順: cloudflared → uvicorn（Named Tunnel 時は URL 固定）
# 終了: Ctrl+C
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
PORT=8001
ENV_LOCAL="$ROOT_DIR/.env.local"
CF_LOG="/tmp/signaly-cloudflared.log"
UV_LOG="/tmp/signaly-uvicorn.log"
UV_PID_FILE="/tmp/signaly.pid"
CF_PID_FILE="/tmp/signaly-cloudflared.pid"

# WSL では IPv6 が不通のため cloudflared API がタイムアウトすることがある
export GODEBUG="${GODEBUG:+$GODEBUG,}netdns=cgo"

# ── cloudflared の確認 ────────────────────────────────────────────────────────

if ! command -v cloudflared &>/dev/null; then
  echo "==> cloudflared が見つかりません"
  echo "    sudo dpkg -i /tmp/cloudflared.deb を実行してインストールしてください"
  exit 1
fi

# ── Named Tunnel 設定（.env.local）────────────────────────────────────────────

TUNNEL_NAME=""
TUNNEL_HOSTNAME=""
if [[ -f "$ENV_LOCAL" ]]; then
  TUNNEL_NAME=$(grep -E '^TUNNEL_NAME=' "$ENV_LOCAL" 2>/dev/null | cut -d= -f2- || true)
  TUNNEL_HOSTNAME=$(grep -E '^TUNNEL_HOSTNAME=' "$ENV_LOCAL" 2>/dev/null | cut -d= -f2- || true)
fi
TUNNEL_NAME="${TUNNEL_NAME:-dev-tunnel}"

# ── 終了時クリーンアップ ──────────────────────────────────────────────────────

cleanup() {
  echo ""
  echo "==> 終了処理中..."
  [[ -f "$UV_PID_FILE" ]] && kill "$(cat "$UV_PID_FILE")" 2>/dev/null || true
  [[ -f "$CF_PID_FILE" ]] && kill "$(cat "$CF_PID_FILE")" 2>/dev/null || true
  rm -f "$UV_PID_FILE" "$CF_PID_FILE"
  echo "==> 停止しました"
}
trap cleanup EXIT INT TERM

# ── 既存プロセスを停止 ────────────────────────────────────────────────────────

kill -9 "$(lsof -t -i:${PORT})" 2>/dev/null || true
pkill -f "cloudflared tunnel run ${TUNNEL_NAME}" 2>/dev/null || true
pkill -f "cloudflared tunnel --url http://127.0.0.1:${PORT}" 2>/dev/null || true

update_env() {
  local key="$1" val="$2"
  if grep -q "^${key}=" "$ENV_LOCAL" 2>/dev/null; then
    sed -i "s|^${key}=.*|${key}=${val}|" "$ENV_LOCAL"
  else
    echo "${key}=${val}" >> "$ENV_LOCAL"
  fi
}

# ── cloudflared を起動 ────────────────────────────────────────────────────────

echo "==> Cloudflare Tunnel を起動します..."
rm -f "$CF_LOG"

if [[ -n "$TUNNEL_HOSTNAME" ]]; then
  TUNNEL_URL="https://${TUNNEL_HOSTNAME}"
  echo "==> Named Tunnel: ${TUNNEL_NAME} → ${TUNNEL_URL}"
  if [[ ! -f "$HOME/.cloudflared/config.yml" ]]; then
    echo "ERROR: ~/.cloudflared/config.yml がありません"
    echo "       bash scripts/setup-tunnel.sh ${TUNNEL_HOSTNAME} を実行してください"
    exit 1
  fi
  cloudflared --no-autoupdate tunnel run "$TUNNEL_NAME" > "$CF_LOG" 2>&1 &
else
  echo "==> Quick Tunnel（起動のたびに URL が変わります）"
  echo "    固定 URL にするには: bash scripts/setup-tunnel.sh dev.<your-domain>"
  cloudflared --no-autoupdate tunnel --url "http://127.0.0.1:${PORT}" > "$CF_LOG" 2>&1 &
fi
CF_PID=$!
echo "$CF_PID" > "$CF_PID_FILE"

TUNNEL_URL="${TUNNEL_URL:-}"
if [[ -z "$TUNNEL_URL" ]]; then
  for i in {1..30}; do
    TUNNEL_URL=$(grep -oP 'https://[a-z0-9-]+\.trycloudflare\.com' "$CF_LOG" 2>/dev/null | head -1 || true)
    [[ -n "$TUNNEL_URL" ]] && break
    sleep 1
  done
  if [[ -z "$TUNNEL_URL" ]]; then
    echo "ERROR: Tunnel URL の取得に失敗しました"
    cat "$CF_LOG"
    exit 1
  fi
  update_env "APP_URL" "${TUNNEL_URL}/"
fi

echo "==> Tunnel URL: $TUNNEL_URL"

load_env_local() {
  set -a
  # PEM などスペースを含む値を安全に export（bash source は使わない）
  eval "$("$ROOT_DIR/.venv/bin/python" - "$ENV_LOCAL" <<'PY'
import shlex
import sys
from pathlib import Path

for line in Path(sys.argv[1]).read_text().splitlines():
    line = line.strip()
    if not line or line.startswith("#"):
        continue
    key, sep, val = line.partition("=")
    if not sep:
        continue
    val = val.strip()
    if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
        val = val[1:-1]
    print(f"export {key}={shlex.quote(val)}")
PY
)"
  set +a
}

# ── uvicorn を起動 ────────────────────────────────────────────────────────────

echo "==> uvicorn を起動します..."
cd "$ROOT_DIR/backend"
load_env_local

setsid "$ROOT_DIR/.venv/bin/uvicorn" main:app \
  --host 127.0.0.1 --port "$PORT" \
  --reload \
  > "$UV_LOG" 2>&1 &
echo $! > "$UV_PID_FILE"
disown $!

for i in {1..15}; do
  curl -sf "http://127.0.0.1:${PORT}/api/channels" &>/dev/null && break
  sleep 1
done
echo "==> uvicorn 起動完了"

# ── 情報表示 ──────────────────────────────────────────────────────────────────

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Signaly 開発環境"
echo ""
echo "  ローカル:  http://127.0.0.1:${PORT}"
echo "  トンネル:  ${TUNNEL_URL}"
echo "             ↑ スマホ・OAuth・PWA 通知はこちら"
if [[ -n "$TUNNEL_HOSTNAME" ]]; then
  echo "             （固定 URL — 再起動しても変わりません）"
fi
echo ""
echo "  開発用 Supabase の Redirect URLs に登録する URL:"
echo "  ${TUNNEL_URL}/auth/callback"
echo ""
echo "  Ctrl+C で終了"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# cloudflared が終了するまでここで待機
wait "$CF_PID"
