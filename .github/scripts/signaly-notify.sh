#!/usr/bin/env bash
# Signaly CI / デプロイ / リリース通知スクリプト（GitHub Actions 用）
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
# Optional: SIGNALY_RELEASE_WEBHOOK_URL … **リリース通知だけを別チャンネルへ送る**webhook
#   （#2391）。CI・デプロイは1日に何十件も流れるため、月に数回のリリースがその中に埋もれる。
#   **未設定なら SIGNALY_WEBHOOK_URL へ送る。** 配布先のワークフローがまだ渡していなくても、
#   organization secretが未登録でも、通知そのものが消えないようにするためのフォールバック。
#
# Optional: NOTIFY_NOTES_FILE … リリース通知の本文に載せる変更内容のファイル
#   （既定 `.github/release-notes.md`）。共有ワークフロー
#   `reusable-release-develop-to-main.yml` がバージョンbumpのたびに書き出す。
#   **先頭の見出しが NOTIFY_VERSION と一致したときだけ載せる**——別バージョンの文面を
#   貼ってしまうより、本文なしで送るほうがましなため。
#
# **どう転んでも終了コードは0で返す**（#2237）。通知先が落ちていてもrunを赤くしない。
# 呼び出し側のステップには、あわせて`continue-on-error: true`を付けておく。
set -euo pipefail

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
server_url="${GITHUB_SERVER_URL:-https://github.com}"
run_url="${NOTIFY_RUN_URL:-${server_url}/${repository}/actions/runs/${GITHUB_RUN_ID:-}}"
notes_file="${NOTIFY_NOTES_FILE:-.github/release-notes.md}"

# リリースだけは宛先も見た目も別扱いにする（#2391）。判定はここ1か所に閉じ込める。
is_release=false
[[ "$kind" == "リリース" ]] && is_release=true

# **リリース用のwebhookが無ければ従来のチャンネルへ送る。** 分離が済んでいない配布先でも
# 通知が消えないようにするためのフォールバック（上のコメントを参照）。
webhook_url="${SIGNALY_WEBHOOK_URL:-}"
if [[ "$is_release" == "true" && -n "${SIGNALY_RELEASE_WEBHOOK_URL:-}" ]]; then
  webhook_url="$SIGNALY_RELEASE_WEBHOOK_URL"
fi

if [[ -z "$webhook_url" ]]; then
  echo "SIGNALY_WEBHOOK_URL not set; skipping Signaly notification"
  exit 0
fi

case "$status" in
  success)
    emoji="✅"
    color="#57f287"
    status_label="成功"
    # リリース専用チャンネルでは全件が成功のリリースになりうるので、✅より内容が分かる絵文字にする
    [[ "$is_release" == "true" ]] && emoji="🚀"
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
  if [[ "$is_release" == "true" && -n "$version" ]]; then
    title="${emoji} [${app_name}] ${kind} ${version} ${status_label}"
  else
    title="${emoji} [${app_name}] ${kind} ${status_label}"
  fi
elif [[ -n "$app_name" ]]; then
  title="${emoji} [${app_name}] ${workflow_name} ${status_label}"
else
  title="${emoji} ${workflow_name} ${status_label}"
fi

release_url=""
if [[ "$is_release" == "true" && -n "$version" && -n "$repository" ]]; then
  release_url="${server_url}/${repository}/releases/tag/${version}"
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
export RELEASE_URL="$release_url"
export COLOR="$color"
export TITLE="$title"
export IS_RELEASE="$is_release"
export NOTES_FILE="$notes_file"

payload=$(python3 - <<'PY'
import json
import os
import re

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
release_url = os.environ.get("RELEASE_URL", "")
color = os.environ["COLOR"]
title = os.environ["TITLE"]
is_release = os.environ.get("IS_RELEASE") == "true"
notes_file = os.environ.get("NOTES_FILE", "")

# 本文が長すぎると通知一覧が1件で埋まる。切るときは切ったことが分かるようにする。
MESSAGE_LIMIT = 1500


def read_release_notes(path, expected_version):
    """`.github/release-notes.md` から本文を取り出す（#2391）。

    先頭のHTMLコメント（「手で編集しない」の断り書き）を捨て、最初の見出し行
    `# v1.2.3` をバージョンの照合に使う。**照合できなければ本文を載せない。**
    リリースの流れの外でファイルが取り残されたとき、古い文面を新しいバージョンの
    通知に貼ってしまうほうが、本文が無いことより悪いため。
    """
    if not path or not expected_version:
        return ""
    try:
        with open(path, encoding="utf-8") as handle:
            raw = handle.read()
    except OSError:
        return ""

    lines = [line for line in raw.splitlines() if not re.fullmatch(r"\s*<!--.*-->\s*", line)]
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines:
        return ""

    heading = re.fullmatch(r"#\s*(\S+)\s*", lines[0])
    if not heading:
        return ""
    if heading.group(1).lstrip("v") != expected_version.lstrip("v"):
        return ""

    body = "\n".join(lines[1:]).strip()
    if len(body) > MESSAGE_LIMIT:
        body = body[:MESSAGE_LIMIT].rstrip() + "…"
    return body


message = read_release_notes(notes_file, version) if is_release else ""

fields = []
if app_name:
    fields.append({"name": "App", "value": app_name, "inline": True})
if is_release:
    # **リリース専用のチャンネルでは、毎回同じ値になる項目を並べない**（#2391）。
    # Type（常に「リリース」）・Branch（常にmain）・Actor・Job・Eventがそれで、
    # 代わりにGitHub Releaseへのリンクを出す。
    if version:
        fields.append({"name": "Version", "value": f"`{version}`", "inline": True})
    if repository:
        fields.append({"name": "Repository", "value": f"`{repository}`", "inline": True})
    if sha_short:
        fields.append({"name": "Commit", "value": f"`{sha_short}`", "inline": True})
    if release_url and version:
        fields.append({"name": "Release", "value": f"[{version}]({release_url})", "inline": True})
else:
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

payload = {
    "title": title,
    "color": color,
    "fields": fields,
}
# 本文が空のときはキーごと落とす。空文字を送ると、Signalyが本文の枠だけを描く。
if message:
    payload["message"] = message

print(json.dumps(payload))
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
  "$webhook_url" 2>/dev/null)" || curl_status=$?

if [[ "$curl_status" -eq 0 ]]; then
  echo "Signalyへ通知しました (HTTP ${http_code})"
  exit 0
fi

# 接続そのものに失敗した場合、`%{http_code}`は`000`になる。
echo "::warning::Signalyへの通知に失敗しました (HTTP ${http_code:-000} / curl exit ${curl_status})。通知先が停止している可能性がありますが、このrunは成功として扱います"
exit 0
