#!/usr/bin/env bash
# Signaly 廃止前の送信元棚卸し（#358）。読み取り専用。
#
# ~/apps 配下の各リポジトリの追跡ファイルから、Signaly への送信経路が残っている
# ファイル数を数える。**値は一切出力しない**（Webhook URL はIDがそのまま資格情報のため）。
# 出力はリポジトリ名・件数・ファイル名だけ。
#
# 使い方: bash scripts/inventory-signaly-senders.sh [~/apps]
# 0件のリポジトリは出さない。全リポジトリが出なくなったら送信元は残っていない。
# 1Password・GitHub secrets・VPS/サブPCの .env・~/.pm2/dump.pm2 は別途確認する
# （docs/decommission.md）。
set -euo pipefail

root="${1:-$HOME/apps}"
pattern='SIGNALY_[A-Z_]*|signaly-notify|notifySignaly|/webhook/[A-Za-z0-9_-]{8,}'

total=0
for dir in "$root"/*/; do
  name="$(basename "$dir")"
  case "$name" in *-worktrees|signaly) continue ;; esac
  git -C "$dir" rev-parse --git-dir >/dev/null 2>&1 || continue
  files="$(git -C "$dir" grep -IlE "$pattern" 2>/dev/null || true)"
  [ -z "$files" ] && continue
  count="$(printf '%s\n' "$files" | wc -l)"
  total=$((total + 1))
  echo "== $name ($count files)"
  printf '%s\n' "$files" | sed 's/^/   /'
done
echo "送信元が残っているリポジトリ: ${total}"
