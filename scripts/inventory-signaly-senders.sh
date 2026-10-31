#!/usr/bin/env bash
# Signaly 廃止前の送信元棚卸し（#358・#361）。読み取り専用。
#
# ~/apps 配下の checkout を **GitHub の owner/repo に正規化**し、各リポジトリの
# 配布・実行ブランチ（origin の既定ブランチ）の追跡ファイルから Signaly への送信経路を探す。
# **値は一切出力しない**（Webhook URL はIDがそのまま資格情報のため）。
# 出力はリポジトリ名・分類・ファイル名だけ。
#
# 分類:
#   経路   workflow・スクリプト・ジョブ設定・ソース。1件でもあれば「稼働送信元あり」。
#   再生成 テンプレート・生成器（名前に template / generate / tpl）。経路として数える。
#   資料   *.md・docs/・サンプル・テスト。これだけのリポジトリは稼働送信元にしない。
#
# 同じ origin の別 checkout（git worktree でない複製）は1つにまとめる。調べる対象は
# origin/<既定ブランチ> で、ローカルの作業ツリー（checkout 中のブランチ・未コミット）との
# 差は「作業ツリーとの差」として別に示す。origin が古い疑いがあるときは --fetch を付ける
# （git fetch を実行するので読み取り専用ではなくなる）。
#
# 使い方: bash scripts/inventory-signaly-senders.sh [--fetch] [~/apps]
# 「送信元が残っているリポジトリ」と「未確認のリポジトリ」がどちらも 0 になったら、コード上の送信元は
# 残っていない。未確認は origin の既定ブランチを確定できず調べていないもの（0 に混ぜない。
# `git fetch origin` や `git remote set-head origin -a` で既定を確定してから再実行する）。
# 1Password・GitHub secrets・VPS/サブPCの .env・~/.pm2/dump.pm2 と Discord への実到達は
# 別途確認する（docs/decommission.md）。
set -euo pipefail

fetch=0
if [ "${1:-}" = "--fetch" ]; then fetch=1; shift; fi
root="${1:-$HOME/apps}"
pattern='SIGNALY_[A-Z_]*|signaly-notify|notifySignaly|/webhook/[A-Za-z0-9_-]{8,}'

classify() {
  local f="$1" base="${1##*/}" lower
  lower="$(printf '%s' "$f" | tr 'A-Z' 'a-z')"
  case "$lower" in
    *.md|*.mdx|*.rst|*.txt|docs/*|*/docs/*|readme*|*/readme*|changelog*|*/changelog*) echo 資料; return ;;
    *.example|*.sample|*example*|*sample*|*fixture*) echo 資料; return ;;
    test_*|*_test.*|*.test.*|*.spec.*|test/*|tests/*|*/test/*|*/tests/*|*/__tests__/*) echo 資料; return ;;
  esac
  case "$(printf '%s' "$base" | tr 'A-Z' 'a-z')" in
    *template*|*generate*|*.tpl) echo 再生成; return ;;
  esac
  echo 経路
}

declare -A seen_repo checkouts hits_ref refname
order=()

for dir in "$root"/*/; do
  name="$(basename "$dir")"
  case "$name" in *-worktrees) continue ;; esac
  git -C "$dir" rev-parse --git-dir >/dev/null 2>&1 || continue
  url="$(git -C "$dir" remote get-url origin 2>/dev/null || true)"
  # 表示用の名前に資格情報を残さない。先に scheme・userinfo（最後の @ まで。パスワードに未エンコードの # ? / があっても
  # 残さない）・クエリ・フラグメントを落としてから GitHub を owner/repo に正規化する。GitHub 以外のホストも
  # userinfo を除いた host/path で表示する（URL 全体を出すと埋め込みトークンが漏れる）。
  safe="$(printf '%s' "$url" | sed -E 's#^[A-Za-z][A-Za-z0-9+.-]*://##; s#^.*@##; s#[?\#].*$##; s#^([^/:]+):([^0-9])#\1/\2#; s#\.git/?$##; s#/+$##')"
  canon="$(printf '%s' "$safe" | sed -E 's#^github\.com/##')"
  [ -z "$canon" ] && canon="(origin なし)/$name"
  # Signaly 自身（別名 checkout を含む）は対象外
  [ "$canon" = "guchi-apps/signaly" ] && continue

  if [ -z "${seen_repo[$canon]:-}" ]; then
    seen_repo[$canon]=1
    order+=("$canon")
    [ "$fetch" = 1 ] && git -C "$dir" fetch --quiet origin 2>/dev/null || true
    head_ref="$(git -C "$dir" symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null || true)"
    if [ -z "$head_ref" ]; then
      for b in develop main master trunk; do
        git -C "$dir" rev-parse --verify --quiet "refs/remotes/origin/$b" >/dev/null && { head_ref="origin/$b"; break; }
      done
    fi
    if [ -z "$head_ref" ]; then
      # 上記の名前でも決まらないときは、origin の追跡ブランチがちょうど1本ならそれを既定とみなす
      # （2本以上は既定を推測できないので未確認のまま）。
      mapfile -t only_refs < <(git -C "$dir" for-each-ref --format='%(refname:short)' refs/remotes/origin 2>/dev/null | grep -vx 'origin/HEAD' | grep -vx 'origin' || true)
      [ "${#only_refs[@]}" = 1 ] && head_ref="${only_refs[0]}"
    fi
    refname[$canon]="$head_ref"
    if [ -n "$head_ref" ]; then
      hits_ref[$canon]="$(git -C "$dir" grep -IlE "$pattern" "$head_ref" 2>/dev/null | sed "s#^${head_ref}:##" || true)"
    else
      hits_ref[$canon]=""
    fi
  fi

  # この checkout の作業ツリー側のヒット（ref 側との差を出すため）
  work="$(git -C "$dir" grep -IlE "$pattern" 2>/dev/null || true)"
  branch="$(git -C "$dir" symbolic-ref --quiet --short HEAD 2>/dev/null || echo '(detached)')"
  diff_only="$(comm -3 <(printf '%s\n' "${hits_ref[$canon]}" | sort) <(printf '%s\n' "$work" | sort) | sed 's/^\t/作業ツリーのみ: /; t; s/^/origin のみ: /' | sed '/^[^:]*: $/d')"
  checkouts[$canon]+="${name}|${branch}|${diff_only//$'\n'/;}"$'\n'
done

live=0
docs_only=0
unresolved=0
for canon in "${order[@]}"; do
  ref="${refname[$canon]}"
  files="${hits_ref[$canon]}"
  route="" doc=""
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    kind="$(classify "$f")"
    if [ "$kind" = 資料 ]; then doc+="   [$kind] $f"$'\n'; else route+="   [$kind] $f"$'\n'; fi
  done <<<"$files"

  diffs=""
  while IFS='|' read -r cname cbranch cdiff; do
    [ -z "$cname" ] && continue
    [ -n "$cdiff" ] && diffs+="   作業ツリーとの差 ($cname @ $cbranch): ${cdiff}"$'\n'
  done <<<"${checkouts[$canon]}"

  if [ -n "$route" ]; then
    live=$((live + 1))
    echo "== $canon [稼働送信元あり] ${ref:-(origin の既定ブランチ不明)}"
    printf '%s' "$route"
    [ -n "$doc" ] && printf '%s' "$doc"
  elif [ -n "$doc" ]; then
    docs_only=$((docs_only + 1))
    echo "== $canon [資料のみ] $ref"
    printf '%s' "$doc"
  elif [ -z "$ref" ]; then
    # 既定ブランチを確定できないリポジトリは「0件」に混ぜない。未確認として数え、停止ゲートに含める。
    unresolved=$((unresolved + 1))
    echo "== $canon [未確認: origin の既定ブランチを確定できず調べられない]"
  fi
  [ -n "$diffs" ] && printf '%s' "$diffs"
done
echo "対象リポジトリ（正規化後）: ${#order[@]}"
echo "資料のみ（稼働送信元としない）: ${docs_only}"
echo "送信元が残っているリポジトリ: ${live}"
echo "未確認のリポジトリ（既定ブランチ不明）: ${unresolved}"
if [ "$live" = 0 ] && [ "$unresolved" = 0 ]; then
  echo "判定: コード上の送信元は残っていない"
else
  echo "判定: 停止ゲート未達（送信元 ${live} 件・未確認 ${unresolved} 件）"
fi
