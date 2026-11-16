#!/bin/bash
# Usage: bash sync.sh [--commit] [--push] [search-root ...]
#
# 手元の clone のうち vauban 導入済みのもの（.pre-commit-config.yaml に vauban managed
# マーカーがある）を探し、setup.sh を再実行して最新に揃える。
# 層の選択は各リポのファイルの有無から引き継ぐので、リストの管理は要らない。
# リポを増やす: そのリポで setup.sh を 1 回実行する。減らす: setup.sh --remove。
#
#   search-root  探す場所（既定: $HOME）。その下 3 階層までを見る。
#   --commit     vauban のファイルだけをコミットする（既定ブランチにいるときのみ）
#   --push       コミット後に push する（--commit を含む）
#
# CI ではなく手元で動かすので、トークンを GitHub に置かず、自分の gh / git の認証で push する。

set -u

VAUBAN_DIR="$(cd "$(dirname "$0")" && pwd)"
VERSION="$(bash "$VAUBAN_DIR/setup.sh" --version | awk '{print $2}')"
MARKER="vauban managed"
PATHS=(.pre-commit-config.yaml .secrets.baseline .semgrepignore
       .github/workflows/semgrep.yml .github/dependabot.yml scripts/gemini_review.py)

COMMIT=0
PUSH=0
ROOTS=()
for a in "$@"; do
  case "$a" in
    --commit) COMMIT=1 ;;
    --push)   COMMIT=1; PUSH=1 ;;
    -h|--help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) echo "Error: 不明なオプション $a"; exit 1 ;;
    *)  ROOTS+=("$a") ;;
  esac
done
[ ${#ROOTS[@]} -eq 0 ] && ROOTS=("$HOME")

REPOS=()
while IFS= read -r f; do
  d="$(dirname "$f")"
  grep -q "$MARKER" "$f" 2>/dev/null || continue
  [ "$(git -C "$d" rev-parse --show-toplevel 2>/dev/null)" = "$d" ] || continue
  REPOS+=("$d")
done < <(for r in "${ROOTS[@]}"; do
  find "$r" -maxdepth 3 -name .pre-commit-config.yaml \
    -not -path '*/.*/*' -not -path '*/node_modules/*' 2>/dev/null
done | sort -u)

if [ ${#REPOS[@]} -eq 0 ]; then
  echo "vauban 導入済みのリポが見つかりませんでした（探した場所: ${ROOTS[*]}）"
  exit 0
fi

echo "vauban $VERSION に揃えます（${#REPOS[@]} 件）"
echo ""

ok=(); changed=(); committed=(); skipped=(); failed=()
for d in "${REPOS[@]}"; do
  name="$(basename "$d")"
  log="$(mktemp)"
  if ! bash "$VAUBAN_DIR/setup.sh" "$d" > "$log" 2>&1; then
    echo "✗ $name: setup.sh が失敗しました"
    sed 's/^/    /' "$log" | tail -5
    failed+=("$name"); rm -f "$log"; continue
  fi
  rm -f "$log"

  # 存在するか、追跡されているパスだけ（消したファイルも差分に含める）
  present=()
  for p in "${PATHS[@]}"; do
    if [ -e "$d/$p" ] || git -C "$d" ls-files --error-unmatch -- "$p" >/dev/null 2>&1; then
      present+=("$p")
    fi
  done
  diff="$(git -C "$d" status --porcelain -- "${present[@]}")"
  if [ -z "$diff" ]; then
    echo "✓ $name: 変更なし"
    ok+=("$name"); continue
  fi

  echo "● $name: 更新あり"
  echo "$diff" | sed 's/^/    /'

  if [ "$COMMIT" = 0 ]; then
    changed+=("$name"); continue
  fi

  branch="$(git -C "$d" symbolic-ref --quiet --short HEAD 2>/dev/null || true)"
  default="$(git -C "$d" symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null | sed 's#^origin/##')"
  if [ -z "$branch" ] || [ "$branch" != "${default:-main}" ]; then
    echo "    → 既定ブランチ（${default:-main}）にいないのでコミットしません（今: ${branch:-detached}）"
    skipped+=("$name"); continue
  fi

  git -C "$d" add -A -- "${present[@]}"
  if ! git -C "$d" commit --quiet -m "chore: vauban $VERSION に更新" -- "${present[@]}"; then
    echo "    ✗ コミットに失敗しました"
    failed+=("$name"); continue
  fi
  if [ "$PUSH" = 1 ] && ! git -C "$d" push --quiet; then
    echo "    ✗ push に失敗しました（コミットは手元に残っています）"
    failed+=("$name"); continue
  fi
  committed+=("$name")
done

echo ""
echo "── 結果 ──"
echo "  変更なし: ${#ok[@]}"
[ ${#changed[@]} -gt 0 ]   && echo "  更新あり（未コミット）: ${changed[*]}"
[ ${#committed[@]} -gt 0 ] && echo "  コミット$([ "$PUSH" = 1 ] && echo '・push')済み: ${committed[*]}"
[ ${#skipped[@]} -gt 0 ]   && echo "  スキップ（既定ブランチ外）: ${skipped[*]}"
[ ${#failed[@]} -gt 0 ]    && echo "  失敗: ${failed[*]}"
[ ${#failed[@]} -eq 0 ]
