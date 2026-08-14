#!/usr/bin/env bash
# 1Password（正）から GitHub の secret / variable へ値を同期する（#1302）。
#
# デプロイのたびに1Passwordを読むとサービスアカウントの日次レート制限
# （1Passwordアカウント全体で1,000リクエスト/日）を使い切るため、実行時の取得先を
# GitHubへ移した。このスクリプトは「値が変わったとき」にだけ実行する。
#
# 重要: ここで使う `op` は**個人アカウントのセッション**であり、サービスアカウントの
# 枠を消費しない。したがってサービスアカウントが枯渇していても実行できる。
#
# 使い方:
#   op signin                      # 先に個人アカウントでサインインしておく
#   scripts/sync-github-secrets.sh --dry-run
#   scripts/sync-github-secrets.sh
#   scripts/sync-github-secrets.sh --only SIGNALY_WEBHOOK_URL,HOST
#
#   # organizationの共通値（#1307）。admin:org スコープが要る
#   scripts/sync-github-secrets.sh --manifest .github/org-secrets-manifest.tsv --dry-run
#   scripts/sync-github-secrets.sh --manifest .github/org-secrets-manifest.tsv
#
# 必要な権限:
#   repo項目 … gh の `repo` スコープ
#   org項目  … gh の `admin:org` スコープ
set -euo pipefail

REPO="${REPO:-guchi-apps/signaly}"
ORG="${ORG:-guchi-apps}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MANIFEST="$REPO_ROOT/.github/secrets-manifest.tsv"
DRY_RUN=false
ONLY=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=true; shift ;;
    --repo) REPO="$2"; shift 2 ;;
    --org) ORG="$2"; shift 2 ;;
    --manifest)
      # 相対パスならリポジトリルートからの相対として解釈する
      case "$2" in
        /*) MANIFEST="$2" ;;
        *)  MANIFEST="$REPO_ROOT/$2" ;;
      esac
      shift 2 ;;
    --only) ONLY="$2"; shift 2 ;;
    -h|--help) sed -n '2,20p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 1 ;;
  esac
done

[[ -f "$MANIFEST" ]] || { echo "マニフェストが見つかりません: $MANIFEST" >&2; exit 1; }
command -v op >/dev/null || { echo "1Password CLI (op) がインストールされていません" >&2; exit 1; }
command -v gh >/dev/null || { echo "GitHub CLI (gh) がインストールされていません" >&2; exit 1; }

if ! op whoami >/dev/null 2>&1; then
  echo "opにサインインしていません。先に 'op signin' を実行してください。" >&2
  echo "（個人アカウントのセッションを使うため、サービスアカウントの枠は消費しません）" >&2
  exit 1
fi

is_selected() {
  [[ -z "$ONLY" ]] && return 0
  [[ ",$ONLY," == *",$1,"* ]]
}

synced=0
skipped=0
failed=0

while IFS=$'\t' read -r key scope kind gh_name source; do
  [[ -z "${key:-}" || "$key" == \#* ]] && continue
  [[ -z "${source:-}" ]] && continue

  if ! is_selected "$key"; then
    continue
  fi

  if [[ "$gh_name" == GITHUB_* ]]; then
    echo "FAIL   $key（GitHubは GITHUB_ で始まる名前を予約しており作成できない。マニフェストのGH_NAMEを修正してください: $gh_name）" >&2
    failed=$((failed + 1))
    continue
  fi

  if [[ "$scope" == "inherit" ]]; then
    echo "skip   $key（organizationの $gh_name を使うため同期しない）"
    skipped=$((skipped + 1))
    continue
  fi

  # 値の中身は絶対に出力しない。失敗時もop側のエラーのみを見せる。
  if ! value="$(op read "$source" 2>/dev/null)"; then
    echo "FAIL   $key（1Passwordから読めません: $source）" >&2
    failed=$((failed + 1))
    continue
  fi

  if [[ -z "$value" ]]; then
    echo "FAIL   $key（値が空です: $source）" >&2
    failed=$((failed + 1))
    continue
  fi

  if [[ "$DRY_RUN" == true ]]; then
    if [[ "$scope" == "org" ]]; then dry_target="org:$ORG"; else dry_target="$REPO"; fi
    echo "dry    $key -> $kind ($dry_target) $gh_name ${#value}文字"
    synced=$((synced + 1))
    continue
  fi

  # gh secret set / gh variable set は --body を省略すると標準入力から値を読む。
  # --body で渡すとプロセス一覧やシェル履歴に値が載るため、必ず標準入力を使う。
  #
  # 1件の失敗でスクリプト全体を止めない。以前は set -e により最初の失敗で即終了し、
  # 残りを試さないまま集計も出さずに終わっていた（GITHUB_ プレフィックスが予約名で
  # 弾かれた際に、28件中16件目で静かに止まった）。
  # organization項目は可視性 all で入れる。privateリポジトリからも参照できるのは
  # GitHub Team以降（2026-08-14に切り替え済み）。Freeでは参照できずorg共通化ができなかった。
  if [[ "$scope" == "org" ]]; then
    target="org:$ORG"
    case "$kind" in
      secret) push_err="$(printf '%s' "$value" | gh secret set "$gh_name" --org "$ORG" --visibility all 2>&1)" && rc=0 || rc=$? ;;
      var)    push_err="$(printf '%s' "$value" | gh variable set "$gh_name" --org "$ORG" --visibility all 2>&1)" && rc=0 || rc=$? ;;
      *) echo "FAIL   $key（不明なKIND: $kind）" >&2; failed=$((failed + 1)); continue ;;
    esac
  else
    target="$REPO"
    case "$kind" in
      secret) push_err="$(printf '%s' "$value" | gh secret set "$gh_name" --repo "$REPO" 2>&1)" && rc=0 || rc=$? ;;
      var)    push_err="$(printf '%s' "$value" | gh variable set "$gh_name" --repo "$REPO" 2>&1)" && rc=0 || rc=$? ;;
      *) echo "FAIL   $key（不明なKIND: $kind）" >&2; failed=$((failed + 1)); continue ;;
    esac
  fi

  if [[ "$rc" -ne 0 ]]; then
    # 値そのものは出力しない。ghのエラー文のみを見せる。
    echo "FAIL   $key -> $gh_name（$push_err）" >&2
    failed=$((failed + 1))
    continue
  fi

  if [[ "$key" == "$gh_name" ]]; then
    echo "ok     $key -> $kind ($target)"
  else
    echo "ok     $key -> $kind ($target) ※GitHub側の名前は $gh_name"
  fi
  synced=$((synced + 1))
done < "$MANIFEST"

echo
echo "同期=$synced スキップ=$skipped 失敗=$failed"
[[ "$failed" -eq 0 ]]
