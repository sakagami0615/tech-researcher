"""D4 長い引用の禁止（D-07、data-model.md 2.7・4.2、report-templates.md 0.3）。

AI の出力テキストと、AI に渡した入力のすべての元のテキスト（タイトル・抜粋・一次情報の本文）を
正規化して比べ、最長の共通部分文字列の長さが上限を超えたら違反にする。長さは、日本語の文字を
2、それ以外を1と数える（上限は日本語 100 文字・英語 200 文字にあたる。日英が混ざっても段差がない）。
製品名・リポジトリ名・タグ名など、項目のメタデータ（meta_terms）にある語句は、その項目の元の
テキストとの比較で一致の判定から外す。

比べるテキストは本文を含めて長くなりうるので、最長一致を総当たりで求めない。出力から一定の間隔で
取った窓が元のテキストにある位置だけを調べ、その位置から両側へ伸ばして一致の長さを測る
（上限を超える一致には、必ずその窓が1つ含まれる）。difflib の SequenceMatcher は使わない。
`autojunk=True`（既定）が頻出の文字を捨てて一致を見落とす問題が、そもそも起きない。
"""

import re
import unicodedata
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, replace

from tech_researcher.agent.daily import DailyEntry
from tech_researcher.collect.records import Item
from tech_researcher.config import QuoteLimits
from tech_researcher.validate.ids import Excluded

# 日本語の文字（ひらがな・カタカナ・漢字・々）。正規化の後の文字で判定する（NFKC で半角カナは
# 全角カナになる）。長さは2と数える
_CJK = re.compile(
    "[\u3005-\u3007\u3041-\u309f\u30a1-\u30fa\u30fc-\u30ff\u31f0-\u31ff"
    "\u3031-\u3035\u303b\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
    "\U0001b000-\U0001b16f\U00020000-\U0002fa1f\U00030000-\U000323af]"
)

# 見えない文字（書式文字・制御文字・私用領域・未割り当てなど）。挟んで一致を切るのに使えるので、
# NFKC の後に取り除く。空白類の制御文字（改行など）は空白として扱う
_INVISIBLE_CATEGORIES = frozenset({"Cc", "Cf", "Co", "Cs", "Cn"})
# カテゴリでは取れない、表示されない文字（Default_Ignorable_Code_Point など）
_INVISIBLE_RANGES = (
    (0x034F, 0x034F),  # 結合書記素ジョイナー
    (0x115F, 0x1160),  # ハングルの充填文字
    (0x17B4, 0x17B5),  # クメール語の固有母音
    (0x180B, 0x180F),  # モンゴル語の異体字セレクタ
    (0x3164, 0x3164),  # ハングルの充填文字
    (0xFE00, 0xFE0F),  # 異体字セレクタ
    (0xFFA0, 0xFFA0),  # 半角のハングルの充填文字
    (0xE0100, 0xE01EF),  # 異体字セレクタ補助
)

# 幅の狭い空白（U+2006・U+2008〜200A・U+202F・U+205F）は、NFKC の前に目印（_NARROW）に置き換えて、
# どの空白が幅の狭い空白だったかを残す。目印は、語句を伏せるときは空白と同じに（英数字ではない文字
# として）扱い、そのあと、取り除いた形と、普通の空白にした形の2つを作る（_forms）
_NARROW = "\U0010fffe"
_NARROW_SPACES = dict.fromkeys(map(ord, "\u2006\u2008\u2009\u200a\u202f\u205f"), _NARROW)

# メタデータの語句を伏せる印は、語句ごとに私用領域の1文字にする（私用領域の文字は正規化で取り除く
# ので、元の文字とぶつからない）。同じ語句は出力と元のテキストの両方で同じ印になり、一致を切らない
# （語句が何度も出る丸写しを見落とさない）。違う語句どうしは一致しない。長さには印を数えない
_MASK_BASE = 0xE000
_MASKS = re.compile("[-]")

# 出力の1つの欄を、元のテキストすべてと比べるときに、窓が元のテキストにある位置を調べる回数の上限。
# 時間を抑えるための上限で、同じ窓が何千回も並ぶ退化した本文（同じ文字の繰り返しなど）でだけ届く。
# 上限まで調べてもまだ出現が残っているときは、調べきれないので違反とみなす（安全側）
_MAX_HITS = 10_000

# 行頭の箇条書き・番号（`- `・`* `・`+ `・`1. `）。記号を消す前に取り除く
_LIST_MARKER = re.compile(r"^[ \t]*(?:[-+*]|\d+[.)])[ \t]+", re.MULTILINE)
# Markdown の記法に使う記号。`-` は語中（GPT-5 など）に使われるので、行頭の箇条書き以外は残す
_MARKDOWN = re.compile(r"[\\`*_~#>|\[\]()!]")
_SPACES = re.compile(r"\s+")
# 日本語の文字の前後の空白（英数字との間も含む）。文の途中の改行の有無で別の文字列にしない
_CJK_GAP = re.compile(rf"(?<={_CJK.pattern}) | (?={_CJK.pattern})")

_Terms = tuple[tuple[re.Pattern[str], str], ...]


@dataclass(frozen=True)
class QuoteSource:
    """出力と比べる元のテキスト1つ。primary は一次情報の本文（上限を別にする。D-07）。"""

    text: str
    primary: bool = False
    meta_terms: tuple[str, ...] = ()  # この元のテキストの項目の語句。ほかの項目の語句は使わない


@dataclass(frozen=True)
class Quote:
    """見つかった最長の一致と、そのときの上限。

    text は、正規化した文字列で、日本語の文字を2つ並べた形（長さがそのまま重み付きの長さになる）。
    語句を伏せた印は含まない。上限を超えない一致は、上限を超えうる窓が元のテキストにあったものに
    限って見つける（それ以外は空）。
    """

    text: str
    limit: int
    primary: bool
    unchecked: bool = False  # 同じ窓が多すぎて調べきれなかった。安全側として違反とみなす
    partial: bool = False  # 違反は見つけたが、途中で調べきれなかった。長さは「少なくとも」

    @property
    def weight(self) -> int:
        return len(self.text)

    @property
    def violates(self) -> bool:
        return self.unchecked or self.weight > self.limit


@dataclass(frozen=True)
class Prepared:
    """正規化して語句を伏せた元のテキスト。1回だけ作って、出力のすべての欄との比較に使う。

    texts は、幅の狭い空白を取り除いた形と、普通の空白にした形（出力の同じ形と組にして比べる）。
    """

    texts: tuple[str, str]
    primary: bool
    terms: _Terms
    terms_key: tuple[str, ...]


def normalize(text: str) -> str:
    """NFKC、見えない文字と Markdown 記号の除去、空白・改行の圧縮、大文字・小文字の統一。

    日本語の文字の前後の空白は取り除く（改行の位置で別の文字列にしない）。幅の狭い空白は、普通の
    空白にする（取り除いた形は _forms が作る）。
    """
    return _finish(_base(text), remove=False)


def _base(text: str) -> str:
    """正規化の途中の形。幅の狭い空白は目印のまま、空白類は1つにまとめた形。語句を伏せるのに使う。"""
    text = _strip_invisible(unicodedata.normalize("NFKC", text.translate(_NARROW_SPACES)))
    text = text.replace("\u2800", " ")  # 点字の空白は空白に見えるので、空白にする
    text = _LIST_MARKER.sub("", text)
    text = _MARKDOWN.sub("", text)
    return _SPACES.sub(" ", text).casefold()


def _finish(base: str, *, remove: bool) -> str:
    """目印を、取り除く（remove）か普通の空白にして、空白を整える。"""
    text = base.replace(_NARROW, "" if remove else " ")
    return _CJK_GAP.sub("", _SPACES.sub(" ", text).strip())


def _forms(base: str, terms: _Terms) -> tuple[str, str]:
    """語句を伏せ、日本語の文字を2つ並べた、2つの形（幅の狭い空白を取り除いた形、普通の空白にした形）。

    語句は目印を空白と同じに扱う段階で伏せるので、幅の狭い空白で区切られた語句の繰り返しも伏せられる。
    """
    masked = _mask(base, terms)
    spaced = _double(_finish(masked, remove=False))
    if _NARROW not in masked:
        return spaced, spaced
    return _double(_finish(masked, remove=True)), spaced


def weighted_length(text: str) -> int:
    """日本語の文字を2、それ以外を1と数えた長さ（正規化した文字列で数える）。"""
    return len(text) + len(_CJK.findall(text))


def _strip_invisible(text: str) -> str:
    drop = {c for c in set(text) if _invisible(c)}
    return text.translate({ord(c): None for c in drop}) if drop else text


def _invisible(c: str) -> bool:
    if c == _NARROW or c in "\t\n\r\f" or unicodedata.category(c).startswith("Z"):
        return False  # 空白は取り除かず、後で1つにまとめる
    if unicodedata.category(c) in _INVISIBLE_CATEGORIES:
        return True
    return any(lo <= ord(c) <= hi for lo, hi in _INVISIBLE_RANGES)


def longest_quote(output: str, sources: Sequence[QuoteSource], limits: QuoteLimits) -> Quote | None:
    """出力と各元のテキストの一致のうち、上限を超えたものの最長（違反がなければ、窓が見つかった
    中の最長。窓が見つからなければ空）。調べきれなかったときは、unchecked の印が付く。"""
    return _compare(_base(output), _prepare(sources), limits)


def _prepare(sources: Sequence[QuoteSource]) -> list[Prepared]:
    compiled: dict[tuple[str, ...], _Terms] = {}
    prepared = []
    for source in sources:
        key = tuple(sorted({normalize(t) for t in source.meta_terms} - {""}, key=len, reverse=True))
        if key not in compiled:
            compiled[key] = _terms(key)
        texts = _forms(_base(source.text), compiled[key])
        prepared.append(Prepared(texts, source.primary, compiled[key], key))
    return prepared


# 出力の形と元のテキストの形の組（0 は幅の狭い空白を取り除いた形、1 は普通の空白にした形）。
# 取り除いた形どうしは、出力で単語の途中に挟む回避を、普通の空白にした形どうしは、元のテキストの
# 空白をそのまま書き写したときを、取り除いた形の出力と普通の空白の元のテキストは、普通の空白で
# 写したうえで単語の途中に挟んだときを、見落とさない
_PAIRS = ((0, 0), (1, 1), (0, 1))


def _compare(output: str, sources: Sequence[Prepared], limits: QuoteLimits) -> Quote | None:
    """正規化の途中の形（_base）の出力を、用意した元のテキストすべてと比べ、最も長く違反した
    一致を返す。

    窓の位置を調べる回数の上限は、組をまたいだ合計。上限まで調べてもまだ出現が残っているときは、
    それまでに違反を見つけていれば、その長さを partial の印を付けて返し、見つけていなければ、
    unchecked を返す（ちょうど上限の回数で出現が尽きたときは、調べ終えているので、通常の判定）。
    """
    worst: Quote | None = None
    budget = [_MAX_HITS]
    outputs: dict[tuple[str, ...], tuple[str, str]] = {}
    for source in sources:
        if source.terms_key not in outputs:
            outputs[source.terms_key] = _forms(output, source.terms)
        forms = outputs[source.terms_key]
        limit = limits.primary_max_en if source.primary else limits.max_en
        done: set[tuple[str, str]] = set()
        for out_index, src_index in _PAIRS:
            pair = (forms[out_index], source.texts[src_index])
            if pair in done:
                continue  # 同じ比べ方は1回だけ
            done.add(pair)
            quote = _best_quote(pair[0], pair[1], source.primary, limit, budget)
            if quote.unchecked:  # 上限まで調べても出現が残っている。これ以上は調べない
                if worst is not None and worst.violates:
                    return replace(worst, partial=True)
                return quote
            if worst is None or _rank(quote) > _rank(worst):
                worst = quote
            if quote.partial:  # 同上。それまでの最長を、少なくともの長さとして返す
                return replace(worst, partial=True)
    return worst


def _rank(quote: Quote) -> tuple[bool, int]:
    # 違反しているものを先にし、同じなら長い一致を取る
    return quote.violates, quote.weight


def _double(text: str) -> str:
    """日本語の文字を2つ並べる。最長の共通部分文字列の長さが、そのまま重み付きの長さになる。"""
    return _CJK.sub(lambda m: m.group() * 2, text)


def _best_quote(a: str, b: str, primary: bool, limit: int, budget: list[int]) -> Quote:
    """出力 a と元のテキスト b（どちらも日本語の文字を2つ並べた形）の、長い一致を探す。

    窓が見つかった位置ごとに、その窓を含む一致の長さを両側へ伸ばして測る。上限を超えたらそこで
    測った中で最長のものを返す（上限を超えても打ち切らず、すべての窓を測る。費用は回数の上限に
    収まる）。最長を選ぶときは、伏せた語句の印を長さに数えない。
    """
    best = ""
    partial = False
    reached: dict[int, int] = {}  # 対角線（b の位置 - a の位置）ごとに、測った一致の終わり
    for start, found in _windows(a, b, limit, budget):
        if found < 0:  # 同じ窓が多すぎて調べきれない
            if len(best) > limit:
                partial = True  # すでに違反が見つかっている
                break
            return Quote("", limit, primary, unchecked=True)
        diagonal = found - start
        if reached.get(diagonal, -1) > start:
            continue  # 同じ一致の中の窓
        lo, hi = _extend(a, b, start, found, _width(limit))
        reached[diagonal] = hi
        run = _MASKS.sub("", a[lo:hi])
        if len(run) > len(best):
            best = run
    return Quote(best, limit, primary, partial=partial)


def _extend(a: str, b: str, start: int, found: int, width: int) -> tuple[int, int]:
    """a[start:] と b[found:] の窓を含む一致を、両側へ伸ばした a の範囲。"""
    end_a, end_b = start + width, found + width
    right = _common(
        lambda n: a[end_a : end_a + n] == b[end_b : end_b + n], min(len(a) - end_a, len(b) - end_b)
    )
    left = _common(lambda n: a[start - n : start] == b[found - n : found], min(start, found))
    return start - left, end_a + right


def _common(equal, limit: int) -> int:
    """equal(n) が成り立つ最大の n（0 以上 limit 以下）。

    1・2・4…と倍にしながら比べ、食い違ったところで二分探索に切り替える。比べる費用が、limit
    ではなく、成り立つ n の大きさに比例する。
    """
    lo, step = 0, 1
    while step <= limit and equal(step):
        lo, step = step, step * 2
    hi = min(step - 1, limit)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if equal(mid):
            lo = mid
        else:
            hi = mid - 1
    return lo


def _width(limit: int) -> int:
    """窓の長さ。上限を超える一致は、印を除いて limit + 1 文字以上あるので、印を含めても
    limit + 1 文字以上ある。長さ limit + 1 以上の一致は、窓の長さ w の2倍より1短い長さ以上なので、
    a の w の倍数の位置から始まる窓を必ず1つ含む。"""
    return (limit + 2) // 2


def _windows(a: str, b: str, limit: int, budget: list[int]) -> Iterator[tuple[int, int]]:
    """出力 a の窓が元のテキスト b にある位置（a の位置、b の位置）。

    位置を見つけるたびに budget を減らす。尽きたら、b の位置を -1 にして知らせて終わる。
    """
    width = _width(limit)
    if len(a) <= limit or len(b) <= limit:
        return
    for start in range(0, len(a) - width + 1, width):
        window = a[start : start + width]
        if not _MASKS.sub("", window):
            continue  # 伏せた語句だけの窓は、長さに数えないので、上限を超える一致を含まない
        position = b.find(window)
        while position >= 0:
            if budget[0] <= 0:
                yield start, -1
                return
            budget[0] -= 1
            yield start, position
            position = b.find(window, position + 1)


def _terms(normalized: Sequence[str]) -> _Terms:
    """正規化した語句（長い順）を、検索の式と印の組にする。"""
    return tuple((_term_pattern(t), chr(_MASK_BASE + i)) for i, t in enumerate(normalized))


def _term_pattern(term: str) -> re.Pattern[str]:
    # 語の途中は伏せない（`uv` で `revue` を壊さない）。端が英数字のときだけ境界を見る
    head = r"(?<![a-z0-9])" if re.match(r"[a-z0-9]", term) else ""
    tail = r"(?![a-z0-9])" if re.search(r"[a-z0-9]$", term) else ""
    return re.compile(f"{head}{re.escape(term)}{tail}")


def _mask(text: str, terms: _Terms) -> str:
    for pattern, mask in terms:
        text = pattern.sub(mask, text)
    return text


def check_daily_quotes(
    entries: Sequence[DailyEntry],
    data: dict,
    meta_terms: Mapping[str, Sequence[str]],
    limits: QuoteLimits,
) -> tuple[list[DailyEntry], list[Excluded]]:
    """日次の項目の文章（見出し・要点・重要性の理由・期間内の出来事）を、元のテキストと比べる。

    元のテキストは、AI に渡した入力 data のすべての項目の untrusted（title・excerpt・
    primary_text）。別の項目の本文を写した場合も見逃さないため、自項目のものに限らない。
    AI が見ていない範囲はなぞれないので、渡した範囲だけで足りる（data-model.md 2.7）。
    meta_terms は item_id から語句への対応で、語句はその項目の元のテキストとの比較にだけ使う。
    """
    prepared = quote_sources(data, meta_terms)
    kept: list[DailyEntry] = []
    excluded: list[Excluded] = []
    for entry in entries:
        reason = _violation(entry, prepared, limits)
        if reason:
            excluded.append(Excluded(entry, reason))
        else:
            kept.append(entry)
    return kept, excluded


def quote_sources(data: dict, meta_terms: Mapping[str, Sequence[str]]) -> list[Prepared]:
    """D4 で出力と比べる元のテキスト（正規化済み）。AI に渡した入力 data のすべての観測のもの。

    items の untrusted（title・excerpt・primary_text。週次は primary_text なし）と、週次の
    past_observations の title。語句（meta_terms）は、その項目の元のテキストにだけ使う。
    """
    sources: list[QuoteSource] = []
    for item in data["items"]:
        untrusted = item.get("untrusted", {})
        terms = tuple(meta_terms.get(item["id"], ()))
        texts = [(untrusted.get(k), False) for k in ("title", "excerpt")]
        texts.append((untrusted.get("primary_text"), True))
        sources += [QuoteSource(t, primary, terms) for t, primary in texts if t]
    for past in data.get("past_observations", ()):
        title = past.get("untrusted", {}).get("title")
        if title:
            sources.append(QuoteSource(title))
    return _prepare(sources)


def meta_terms_by_id(items: Mapping[str, Item]) -> dict[str, tuple[str, ...]]:
    """項目 ID ごとの meta_terms（まとめた全レコードの語句。Item が持つ）。"""
    return {item_id: item.meta_terms for item_id, item in items.items()}


def _violation(entry: DailyEntry, sources: Sequence[Prepared], limits: QuoteLimits) -> str | None:
    for text in (entry.headline, *entry.points, entry.importance, entry.period_event):
        if reason := quote_violation(text, sources, limits):
            return reason
    return None


def quote_violation(
    text: str | None, sources: Sequence[Prepared], limits: QuoteLimits
) -> str | None:
    """1つの文章が元のテキストの長い引用になっていれば、除外の理由を返す。"""
    if not text:
        return None
    quote = _compare(_base(text), sources, limits)
    if quote is not None and quote.unchecked:
        return "同じ部分が元のテキストに多数あり、調べきれないため除外"
    if quote is not None and quote.violates:
        origin = "一次情報の本文" if quote.primary else "タイトル・抜粋"
        if quote.partial:
            return (
                f"{origin}との共通部分の長さ（日本語は2倍）が少なくとも {quote.weight} で、"
                f"上限 {quote.limit} を超えた（同じ部分が多く、途中で調べきれなかった）"
            )
        return (
            f"{origin}との共通部分の長さ（日本語は2倍）が {quote.weight} で、"
            f"上限 {quote.limit} を超えた"
        )
    return None
