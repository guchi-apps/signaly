#!/usr/bin/env bash
# デプロイ後に signaly が実際に応答するまで待つ（guchi-apps/signaly#168）。
#
# restart-service.sh が見ている systemctl --user restart の終了コードは
# 「ユニットの起動要求が受け付けられたか」しか表さない。uvicorn が .env の不備や
# 依存の欠落で即死しても Restart=always で再起動を繰り返すだけなので、
# 再起動の成否だけではデプロイの成功を判定できない。
#
# 叩き先は signaly.service.template の ExecStart（uvicorn --host 127.0.0.1 --port 8002）と、
# backend/main.py がフロントエンドを / へ StaticFiles マウントしていること（認証不要で 200）が根拠。
set -euo pipefail

HEALTH_URL="${1:-http://127.0.0.1:8002/}"
MAX_ATTEMPTS=30
INTERVAL=2

# systemctl --user / journalctl --user は XDG_RUNTIME_DIR が無いと user bus を見つけられない。
# restart-service.sh と同じ扱いにする。
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

echo "Health check... (${HEALTH_URL})"
for _ in $(seq 1 "$MAX_ATTEMPTS"); do
  if curl -fsS -o /dev/null "$HEALTH_URL"; then
    echo "Deployment successful."
    exit 0
  fi
  sleep "$INTERVAL"
done

echo "Health check failed after $((MAX_ATTEMPTS * INTERVAL))s." >&2
systemctl --user status signaly --no-pager || true
journalctl --user -u signaly -n 50 --no-pager || true
exit 1
