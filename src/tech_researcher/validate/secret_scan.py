"""秘匿情報の検出とマスク（US-E7-05、structure.md 7.3）。

ログに書く前のマスクと、Issue を公開する前の検査は、同じ検出器を使う。検出するのは
(1) 既知の秘匿値（env_file から環境変数として渡された値）と (2) トークンの形。
"""

import json
import re
import urllib.parse
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

# (2) トークンの形。worker には Claude のトークンが渡らない（D-05）ので、
# 形の検出が唯一の防御になる。前側の文字は問わない
# （`%20ghp_…`・`Bearer_ghp_…`・repr の `\nghp_…` も拾う）
TOKEN_PATTERNS: dict[str, re.Pattern[str]] = {
    # GitHub の classic・OAuth・App のトークン（ghp_・gho_・ghu_・ghs_・ghr_）
    "github_token": re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"),
    # GitHub の fine-grained トークン
    "github_pat": re.compile(r"github_pat_[A-Za-z0-9_]{22,}"),
    # Anthropic の API キーと、claude setup-token の OAuth トークン（sk-ant-api…・sk-ant-oat…）
    "anthropic_key": re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
}

# 環境変数の名前にこれを含むものを秘匿値とみなす。worker.env には投稿先のリポジトリ名なども
# 入る（D-20）ので、すべての値を秘匿値にすると、レポートの本文が検査で止まってしまう。
# PAT は PATH と区別するため、`_` で区切られた語のときだけにする
_SECRET_NAME = re.compile(r"TOKEN|KEY|SECRET|PASSWORD|AUTH|CREDENTIAL|(?:^|_)PAT(?:_|$)")

KNOWN = "known"


@dataclass(frozen=True)
class Finding:
    kind: str  # KNOWN か TOKEN_PATTERNS のキー
    start: int
    end: int


def known_secret_values(environ: Mapping[str, str]) -> list[str]:
    """環境変数から既知の秘匿値を選ぶ。"""
    return [value for name, value in environ.items() if _SECRET_NAME.search(name.upper())]


def _variants(value: str) -> set[str]:
    """既知の値と、例外の repr・JSON・URL でエスケープされた形。"""
    return {
        value,
        repr(value)[1:-1],
        json.dumps(value)[1:-1],
        json.dumps(value, ensure_ascii=False)[1:-1],
        urllib.parse.quote(value, safe=""),
    }


class SecretScanner:
    def __init__(self, known_values: Iterable[str] = ()):
        # 前後の空白・改行は落とす（CRLF の env_file 対策）。空の値はすべてに一致するので除く
        values = {v.strip() for v in known_values} - {""}
        self._known = {variant for v in values for variant in _variants(v)}

    def find(self, text: str) -> list[Finding]:
        """検出した箇所を、重なりをまとめて先頭から返す。"""
        found = [
            Finding(KNOWN, start, start + len(value))
            for value in self._known
            for start in _occurrences(text, value)
        ]
        found += [
            Finding(kind, m.start(), m.end())
            for kind, pattern in TOKEN_PATTERNS.items()
            for m in pattern.finditer(text)
        ]
        merged: list[Finding] = []
        # 同じ位置から始まるものは長い方を先にし、既知の値を優先する
        for f in sorted(found, key=lambda f: (f.start, -f.end, f.kind != KNOWN)):
            if merged and f.start < merged[-1].end:
                last = merged[-1]
                merged[-1] = Finding(last.kind, last.start, max(last.end, f.end))
            else:
                merged.append(f)
        return merged

    def contains(self, text: str) -> bool:
        return bool(self.find(text))

    def mask(self, text: str) -> str:
        """検出した箇所を `[MASKED:種類]` に置き換える。値の一部も残さない。"""
        parts: list[str] = []
        pos = 0
        for f in self.find(text):
            parts += [text[pos : f.start], f"[MASKED:{f.kind}]"]
            pos = f.end
        parts.append(text[pos:])
        return "".join(parts)


def _occurrences(text: str, value: str) -> Iterable[int]:
    """重なる出現も含めた、すべての開始位置（`abab` の中の `aba…` の繰り返しを取りこぼさない）。"""
    start = text.find(value)
    while start != -1:
        yield start
        start = text.find(value, start + 1)
