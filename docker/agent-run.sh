#!/bin/sh
# agent コンテナで claude を起動する前に、渡す内側のスキルがイメージにあることを確かめる
# （agent/invoke.py）。使い方：agent-run <スキル名> <claude の引数…>
# ホストのリポジトリとイメージのスキルがずれた（イメージを作り直していない）ときに、スキルなしで
# AI を呼ばないため。なければ終了コード 78 で止める（invoke.py が missing_skill に分ける）。
set -eu
skill="${1:-}"
case "$skill" in
    "" | *[!a-z0-9-]*)
        echo "agent-run: スキル名が正しくない" >&2
        exit 64
        ;;
esac
shift
if [ ! -f "/app/.claude/skills/$skill/SKILL.md" ]; then
    echo "agent-run: スキル $skill がイメージにない（docker compose build agent をやり直す）" >&2
    exit 78
fi
exec claude "$@"
