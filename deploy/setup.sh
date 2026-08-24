#!/usr/bin/env bash
# VPS 初回セットアップスクリプト
# 実行: bash deploy/setup.sh
set -euo pipefail

TARGET_DIR="${TARGET_DIR:-/apps/signaly}"

install_systemd_service() {
  bash "${TARGET_DIR}/deploy/restart-service.sh" "${TARGET_DIR}"
}

echo "==> ディレクトリ作成 (${TARGET_DIR})"
sudo mkdir -p "$TARGET_DIR"
sudo chown "$USER":"$USER" "$TARGET_DIR"

echo "==> ファイルをコピー"
rsync -az --delete \
  --exclude='.git' \
  --exclude='.venv' \
  --exclude='backend/data/' \
  --exclude='backend/channels.json' \
  ./ "$TARGET_DIR/"

echo "==> Python 仮想環境を作成"
bash "$TARGET_DIR/deploy/ensure_venv.sh" "$TARGET_DIR"

echo "==> .env の確認"
# .env はデプロイ（.github/workflows/deploy.yml）が GitHub の secret / variable から
# 書き込む。以前はここで 1Password の .env.tpl を op inject していたが、実行時の
# 1Password 呼び出しを外した際に .env.tpl ごと廃止した（guchi-apps/issue-deck#1302）。
if [[ -f "${TARGET_DIR}/.env" ]]; then
  echo "  ${TARGET_DIR}/.env は既に存在します"
else
  echo "  !! ${TARGET_DIR}/.env はまだありません。"
  echo "     main へのマージ（または Actions の Deploy の手動実行）を1回行うと書き込まれます。"
fi

echo "==> channels.json が存在しない場合は例をコピー"
if [[ ! -f "$TARGET_DIR/backend/channels.json" ]]; then
  cp "$TARGET_DIR/channels.example.json" "$TARGET_DIR/backend/channels.json"
  echo "  !! backend/channels.json を編集してチャンネルIDを設定してください"
fi

# テーブルの作成・列の追加はここでは行わない。DDL はマイグレーション専用ユーザーでしか
# 実行できず、その資格情報は deploy.yml（backend/migrate_db.py の実行）しか持たない（#183）。
echo "==> MySQL データベースを作成（存在しない場合のみ）"
echo "  !! DATABASE_URL 環境変数が設定されていることを確認してください"
mysql -u root -e "CREATE DATABASE IF NOT EXISTS app_signaly CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;" 2>/dev/null || \
  echo "  !! DB 作成をスキップ（手動で実行してください）: CREATE DATABASE app_signaly CHARACTER SET utf8mb4;"

echo "==> user systemd の linger を有効化（ログアウト後もサービスを維持）"
sudo loginctl enable-linger "$USER"

echo "==> 旧 system サービスがあれば停止・無効化"
sudo systemctl disable --now signaly 2>/dev/null || true

echo "==> systemd ユーザーサービスを登録 (port 8002)"
install_systemd_service
systemctl --user status signaly --no-pager

echo "==> 完了"
echo "  Apache 設定は deploy/apache.conf を signaly.gucchii.com の VirtualHost に追記してください（ルート / を 8002 にプロキシ）"
