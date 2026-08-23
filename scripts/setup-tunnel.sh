#!/usr/bin/env bash
# Cloudflare Named Tunnel の初回セットアップ
# 使い方: bash scripts/setup-tunnel.sh dev.example.com
#
# 前提:
#   - ドメインを Cloudflare に追加済み（ネームサーバーを Cloudflare に向けている）
#   - cloudflared がインストール済み
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
ENV_LOCAL="$ROOT_DIR/.env.local"
TUNNEL_NAME="${TUNNEL_NAME:-dev-tunnel}"
PORT=8001

# WSL では IPv6 が不通のため cloudflared API がタイムアウトすることがある
export GODEBUG="${GODEBUG:+$GODEBUG,}netdns=cgo"

HOSTNAME="${1:-}"
if [[ -z "$HOSTNAME" ]]; then
  echo "使い方: bash scripts/setup-tunnel.sh <ホスト名>"
  echo "  例:   bash scripts/setup-tunnel.sh dev.signaly.example.com"
  exit 1
fi

if ! command -v cloudflared &>/dev/null; then
  echo "ERROR: cloudflared が見つかりません"
  exit 1
fi

CF_DIR="$HOME/.cloudflared"
CF_CONFIG="$CF_DIR/config.yml"
mkdir -p "$CF_DIR"

echo "==> Cloudflare にログインします（ブラウザが開きます）"
if [[ ! -f "$CF_DIR/cert.pem" ]]; then
  cloudflared tunnel login
fi

echo "==> トンネル '${TUNNEL_NAME}' を作成します"
if cloudflared tunnel list 2>/dev/null | awk '{print $2}' | grep -qx "$TUNNEL_NAME"; then
  echo "    既存のトンネルを使用します"
else
  cloudflared tunnel create "$TUNNEL_NAME"
fi

TUNNEL_ID=$(cloudflared tunnel list 2>/dev/null | awk -v name="$TUNNEL_NAME" '$2 == name { print $1; exit }')
if [[ -z "$TUNNEL_ID" ]]; then
  # tunnel list が失敗した場合、認証情報ファイルから ID を取得
  shopt -s nullglob
  for f in "$CF_DIR"/*.json; do
    TUNNEL_ID=$(basename "$f" .json)
    break
  done
  shopt -u nullglob
fi
if [[ -z "$TUNNEL_ID" ]]; then
  echo "ERROR: トンネル ID の取得に失敗しました"
  exit 1
fi

CRED_FILE="$CF_DIR/${TUNNEL_ID}.json"
if [[ ! -f "$CRED_FILE" ]]; then
  echo "ERROR: 認証情報が見つかりません: $CRED_FILE"
  exit 1
fi

echo "==> cloudflared 設定を書き込みます: $CF_CONFIG"
cat > "$CF_CONFIG" <<EOF
tunnel: ${TUNNEL_NAME}
credentials-file: ${CRED_FILE}

ingress:
  - hostname: ${HOSTNAME}
    service: http://127.0.0.1:${PORT}
  - service: http_status:404
EOF

echo "==> DNS レコードを作成します: ${HOSTNAME}"
cloudflared tunnel route dns "$TUNNEL_NAME" "$HOSTNAME"

TUNNEL_URL="https://${HOSTNAME}"

update_env() {
  local key="$1" val="$2"
  touch "$ENV_LOCAL"
  if grep -q "^${key}=" "$ENV_LOCAL" 2>/dev/null; then
    sed -i "s|^${key}=.*|${key}=${val}|" "$ENV_LOCAL"
  else
    echo "${key}=${val}" >> "$ENV_LOCAL"
  fi
}

echo "==> .env.local を更新します"
update_env "TUNNEL_NAME"     "$TUNNEL_NAME"
update_env "TUNNEL_HOSTNAME" "$HOSTNAME"
update_env "APP_URL" "${TUNNEL_URL}/"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  セットアップ完了"
echo ""
echo "  固定 URL:  ${TUNNEL_URL}"
echo "  トンネル:  ${TUNNEL_NAME} (${TUNNEL_ID})"
echo ""
echo "  次の作業:"
echo "  1. 開発用 Supabase の Redirect URLs に登録:"
echo "     ${TUNNEL_URL}/auth/callback"
echo "  2. 開発環境を起動:"
echo "     bash scripts/dev.sh"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
