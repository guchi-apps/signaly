#!/usr/bin/env bash
# マニフェストから、ワークフローのジョブに書く env: ブロックを生成する（#1306）。
#
# 複合アクション .github/actions/load-secrets は `secrets` コンテキストを丸ごと受け取れない
# （`toJSON(secrets)` をアクションの入力へ渡すと、ワークフローのrunがaction_requiredになり
# ジョブが1つも作られなくなる。PR #1315で再現・切り分け済み）。そのためGitHub側の値は
# 呼び出し側がジョブの env: で明示的に渡す。その記述をここで機械生成する。
#
# 使い方:
#   scripts/generate-workflow-env-block.sh              # 全件
#   scripts/generate-workflow-env-block.sh SIGNALY_WEBHOOK_URL,HOST
#
# インデントは既定6（ジョブ直下の env: の下）。第2引数で変更できる。
set -euo pipefail

MANIFEST="${MANIFEST:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/.github/secrets-manifest.tsv}"
ONLY="${1:-}"
INDENT="${2:-6}"
pad="$(printf '%*s' "$INDENT" '')"

while IFS=$'\t' read -r key scope kind gh_name source; do
  [[ -z "${key:-}" || "$key" == \#* ]] && continue
  [[ -z "${source:-}" ]] && continue
  if [[ -n "$ONLY" && ",$ONLY," != *",$key,"* ]]; then
    continue
  fi
  if [[ "$kind" == "var" ]]; then
    printf '%s%s: ${{ vars.%s }}\n' "$pad" "$key" "$gh_name"
  else
    printf '%s%s: ${{ secrets.%s }}\n' "$pad" "$key" "$gh_name"
  fi
done < "$MANIFEST"
