#!/usr/bin/env bash
# last30days-skill を固定の SHA で取得し、scripts と LICENSE だけを作業ツリーに置く（D-01）。
# clone の後にホストで実行する。何度実行してもよい。
# 第三者コードの指示ファイル（SKILL.md・AGENTS.md・CLAUDE.md など）が残っていれば失敗にする。
set -euo pipefail

readonly SUBMODULE=third_party/last30days
# 作業ツリーに置くもの（sparse-checkout の --no-cone のパターン）
readonly KEEP_PATTERNS=(/skills/last30days/scripts/ /LICENSE)
# 置かれていなければ失敗にするもの
readonly REQUIRED_FILES=(LICENSE skills/last30days/scripts/last30days.py)

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

fail() {
    echo "setup-third-party: $*" >&2
    exit 1
}

git submodule update --init -- "$SUBMODULE"
git -C "$SUBMODULE" sparse-checkout set --no-cone "${KEEP_PATTERNS[@]}"

# AI への指示になり得るファイルか（名前は大文字・小文字を区別しない）
is_instruction_file() {
    local name
    name="$(basename "$1" | tr '[:upper:]' '[:lower:]')"
    case "$name" in
        *.md | *.mdc | .cursorrules | .windsurfrules | .clinerules) return 0 ;;
    esac
    case "/$1/" in
        */.claude/* | */.agents/* | */.codex/* | */.cursor/* | */.github/*) return 0 ;;
    esac
    return 1
}

errors=0
count=0
while IFS= read -r -d '' path; do
    rel="${path#"$SUBMODULE"/}"
    count=$((count + 1))
    if is_instruction_file "$rel"; then
        echo "setup-third-party: 指示ファイルが残っています: $SUBMODULE/$rel" >&2
        errors=$((errors + 1))
    elif [[ "$rel" != LICENSE && "$rel" != skills/last30days/scripts/* ]]; then
        echo "setup-third-party: 対象外のファイルがあります: $SUBMODULE/$rel" >&2
        errors=$((errors + 1))
    fi
done < <(find "$SUBMODULE" -path "$SUBMODULE/.git" -prune -o ! -type d -print0)

for required in "${REQUIRED_FILES[@]}"; do
    [[ -f "$SUBMODULE/$required" ]] || {
        echo "setup-third-party: 必要なファイルがありません: $SUBMODULE/$required" >&2
        errors=$((errors + 1))
    }
done

((errors == 0)) || fail "失敗しました（${errors} 件）"
echo "setup-third-party: $SUBMODULE を $(git -C "$SUBMODULE" rev-parse HEAD) で用意しました（${count} ファイル）"
