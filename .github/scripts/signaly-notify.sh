#!/usr/bin/env bash
# Signaly CI / デプロイ通知スクリプト（GitHub Actions 用）
#
# 手順の詳細: ../_docs/README.md（CI / デプロイ通知セクション）
# 各アプリの .github/scripts/ にコピーして使用する。
#
# Requires: SIGNALY_WEBHOOK_URL, NOTIFY_STATUS (success|failure|cancelled)
# Optional: NOTIFY_APP, NOTIFY_KIND (e.g. デプロイ / CI), NOTIFY_JOB, NOTIFY_VERSION (リリース時)
# Optional: NOTIFY_RUN_URL … 「Run」のリンク先。既定はこの通知を出しているrun自身。
#   別のrunについて通知する場合だけ指定する（例: deploy-retry.yml が「再実行するデプロイの
#   run」へリンクする。#2134）。未指定なら従来どおりの挙動。
#
# **どう転んでも終了コードは0で返す**（#2237）。通知先が落ちていてもrunを赤くしない。
# 呼び出し側のステップには、あわせて`continue-on-error: true`を付けておく。
set -euo pipefail

if [[ -z "${SIGNALY_WEBHOOK_URL:-}" ]]; then
  echo "SIGNALY_WEBHOOK_URL not set; skipping Signaly notification"
  exit 0
fi

status="${NOTIFY_STATUS:-unknown}"
app_name="${NOTIFY_APP:-}"
kind="${NOTIFY_KIND:-}"
workflow_name="${NOTIFY_WORKFLOW:-${GITHUB_WORKFLOW:-GitHub Actions}}"
job_name="${NOTIFY_JOB:-${GITHUB_JOB:-}}"
version="${NOTIFY_VERSION:-}"
repository="${GITHUB_REPOSITORY:-}"
ref_name="${GITHUB_REF_NAME:-}"
sha="${GITHUB_SHA:-}"
sha_short="${sha:0:7}"
run_url="${NOTIFY_RUN_URL:-${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}}"

case "$status" in
  success)
    emoji="✅"
    color="#57f287"
    status_label="成功"
    ;;
  failure)
    emoji="❌"
    color="#ed4245"
    status_label="失敗"
    ;;
  cancelled)
    emoji="⚪"
    color="#95a5a6"
    status_label="キャンセル"
    ;;
  *)
    emoji="ℹ️"
    color="#5865f2"
    status_label="$status"
    ;;
esac

if [[ -n "$app_name" && -n "$kind" ]]; then
  if [[ "$kind" == "リリース" && -n "$version" ]]; then
    title="${emoji} [${app_name}] ${kind} ${version} ${status_label}"
  else
    title="${emoji} [${app_name}] ${kind} ${status_label}"
  fi
elif [[ -n "$app_name" ]]; then
  title="${emoji} [${app_name}] ${workflow_name} ${status_label}"
else
  title="${emoji} ${workflow_name} ${status_label}"
fi

export NOTIFY_STATUS="$status"
export NOTIFY_APP="$app_name"
export NOTIFY_KIND="$kind"
export NOTIFY_WORKFLOW="$workflow_name"
export NOTIFY_JOB="$job_name"
export NOTIFY_VERSION="$version"
export REPOSITORY="$repository"
export SHA_SHORT="$sha_short"
export RUN_URL="$run_url"
export COLOR="$color"
export TITLE="$title"

payload=$(python3 - <<'PY'
import json
import os

app_name = os.environ.get("NOTIFY_APP", "")
kind = os.environ.get("NOTIFY_KIND", "")
version = os.environ.get("NOTIFY_VERSION", "")
job_name = os.environ.get("NOTIFY_JOB", "")
event_name = os.environ.get("GITHUB_EVENT_NAME", "")
repository = os.environ.get("REPOSITORY", "")
ref_name = os.environ.get("GITHUB_REF_NAME", "")
sha_short = os.environ.get("SHA_SHORT", "")
actor = os.environ.get("GITHUB_ACTOR", "")
run_url = os.environ.get("RUN_URL", "")
color = os.environ["COLOR"]
title = os.environ["TITLE"]

fields = []
if app_name:
    fields.append({"name": "App", "value": app_name, "inline": True})
if kind:
    fields.append({"name": "Type", "value": kind, "inline": True})
if version:
    fields.append({"name": "Version", "value": f"`{version}`", "inline": True})
if repository:
    fields.append({"name": "Repository", "value": f"`{repository}`", "inline": True})
if ref_name:
    fields.append({"name": "Branch", "value": ref_name, "inline": True})
if sha_short:
    fields.append({"name": "Commit", "value": f"`{sha_short}`", "inline": True})
if actor:
    fields.append({"name": "Actor", "value": actor, "inline": True})
if job_name:
    fields.append({"name": "Job", "value": job_name, "inline": True})
if event_name:
    fields.append({"name": "Event", "value": event_name, "inline": True})
fields.append({"name": "Run", "value": f"[Workflow Run]({run_url})", "inline": False})

print(json.dumps({
    "title": title,
    "color": color,
    "fields": fields,
}))
PY
)

# **通知が届かなくてもrunは落とさない**（#2237）。通知はCI・デプロイの結果の記録であって
# 成否そのものではない。Signalyが止まっている間にmainへマージすると、デプロイが成功して
# いるのに`curl: (22) 503`でこのステップだけが失敗し、run全体が赤くなっていた
# （実例: `Deploy to Production` run 32721175959。tag/build/deploy/releaseは全て成功）。
#
# `--retry`はタイムアウトと408・429・500・502・503・504を一時エラーとみなして再試行するので、
# Signaly自身のデプロイ中に当たった503はこれで拾える。それでも駄目なら警告だけ残して0で返す。
#
# **curlのstderrは出さない。** HTTPエラーのメッセージ（`curl: (22) The requested URL returned
# error: 503`）にURLは載らないが、接続に失敗したとき（`curl: (7) Failed to connect to <host>`）は
# webhookのホスト名が載る。GitHubのマスクはsecretの完全一致でしか効かないため、URL全体を
# secretにしていてもホスト名だけがPUBLICなrunのログへ残ってしまう
# （`session-notify.sh`が失敗時にURLを出さないのと同じ理由）。
#
# 代わりに**HTTPコードとcurlの終了コード**を出す。原因の切り分けにはこれで足りる
# （`22`=HTTPエラー、`6`=名前解決、`7`=接続不可、`28`=タイムアウト）。
http_code=""
curl_status=0
http_code="$(curl -fsS -o /dev/null -w '%{http_code}' \
  --max-time 10 --retry 2 --retry-delay 2 \
  -H "Content-Type: application/json" \
  -d "$payload" \
  "$SIGNALY_WEBHOOK_URL" 2>/dev/null)" || curl_status=$?

if [[ "$curl_status" -eq 0 ]]; then
  echo "Signalyへ通知しました (HTTP ${http_code})"
  exit 0
fi

# 接続そのものに失敗した場合、`%{http_code}`は`000`になる。
echo "::warning::Signalyへの通知に失敗しました (HTTP ${http_code:-000} / curl exit ${curl_status})。通知先が停止している可能性がありますが、このrunは成功として扱います"
exit 0
