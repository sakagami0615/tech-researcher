import random
import time
from dataclasses import replace
from datetime import UTC, datetime
from difflib import SequenceMatcher
from pathlib import Path

import pytest

from tech_researcher.agent.daily import DailyEntry
from tech_researcher.collect.records import Item, Record
from tech_researcher.config import QuoteLimits
from tech_researcher.validate import quote
from tech_researcher.validate.quote import (
    QuoteSource,
    check_daily_quotes,
    longest_quote,
    meta_terms_by_id,
    normalize,
    weighted_length,
)

FIXTURES = Path(__file__).parent / "fixtures"
EN = (FIXTURES / "quote_en.md").read_text(encoding="utf-8")
JA = (FIXTURES / "quote_ja.md").read_text(encoding="utf-8")
EN_PARAGRAPHS = EN.split("\n\n")  # [見出し, 本文, 箇条書き, 結び]
JA_PARAGRAPHS = JA.split("\n\n")
LIMITS = QuoteLimits()  # 初期値：200（日本語 100 文字・英語 200 文字。本文用も同じ）
SMALL = QuoteLimits(max_en=10, primary_max_en=10)  # 短い文字列で一致の中身を見るとき


def longest(output, source, terms=(), primary=False, limits=LIMITS):
    return longest_quote(output, [QuoteSource(source, primary, tuple(terms))], limits)


def english_run(n: int) -> str:
    """同じ部分が繰り返さない、n 文字の英小文字の並び。"""
    return "".join(chr(ord("a") + (i * 7 + i // 26) % 26) for i in range(n))


def japanese_run(n: int) -> str:
    """同じ文字が出ない、n 文字の漢字の並び。"""
    return "".join(chr(0x4E00 + i * 3) for i in range(n))


# --- 正規化 ---


def test_normalize_applies_nfkc_case_spaces_and_markdown():
    assert normalize("ＡＢＣ  Ｄｅｆ\n\n**Bold** `code`") == "abc def bold code"
    assert (
        normalize("# 見出し\n- 箇条書き\n1. 番号\n> 引用 [リンク](x)")
        == "見出し箇条書き番号引用リンクx"
    )


def test_normalize_keeps_hyphen_inside_words_but_drops_list_markers():
    assert normalize("- GPT-5 and uv-tool") == "gpt-5 and uv-tool"


def test_line_break_inside_japanese_sentence_does_not_hide_a_quote():
    source = "クエリプランナーを\n全面的に書き直した。"
    result = longest("クエリプランナーを全面的に書き直した。", source, limits=SMALL)
    assert result.weight == 2 * len("クエリプランナーを全面的に書き直した。") - 1  # 。は重み1


def test_line_break_between_latin_and_japanese_does_not_hide_a_quote():
    source = "生成AI\nを全面的に書き直した" + english_run(210)
    flat = "生成AIを全面的に書き直した" + english_run(210)
    assert longest(flat, source).weight == weighted_length(normalize(flat))


def test_space_between_latin_and_japanese_is_dropped():
    assert normalize("Claude 4 を公開") == "claude 4を公開"


def test_weighted_length_counts_japanese_as_two():
    assert weighted_length("abc") == 3
    assert weighted_length("日本語、。々ひらカタ") == 18  # 句読点は1、々は2
    assert weighted_length(normalize("ｶﾀｶﾅ")) == 8  # 半角カナは全角カナになってから数える
    assert weighted_length("ア・イ") == 5  # 中黒は1
    assert weighted_length("\U00020bb7\U00031350") == 4  # 拡張漢字
    assert weighted_length("\U0001b001") == 2  # 変体仮名


# --- 見えない文字を挟む回避 ---

INVISIBLE = [
    "️",  # 異体字セレクタ
    "͏",  # 結合書記素ジョイナー
    "᠋",  # モンゴル語の異体字セレクタ
    "\x7f",  # 制御文字
    "ㅤ",  # ハングルの充填文字
    "ﾠ",
    "ᅟ",
    "ᅠ",
    "\U000e0100",  # 異体字セレクタ補助
    "​",  # ゼロ幅スペース
    "­",  # ソフトハイフン
    "",  # 私用領域
    "⁥",  # 未割り当て（Default_Ignorable_Code_Point）
    "\x00",
]


@pytest.mark.parametrize("char", ["\u2006", "\u2008", "\u2009", "\u200a", "\u202f", "\u205f"])
def test_narrow_space_inside_words_does_not_cut_a_copy(char):
    text = english_run(250)
    disguised = char.join(text[i : i + 5] for i in range(0, 250, 5))
    assert normalize(disguised) == text
    assert longest(disguised, text).violates


def test_braille_blank_is_a_space():
    text = english_run(100) + " " + english_run(110)
    assert longest(text.replace(" ", "\u2800"), text).violates


@pytest.mark.parametrize("char", INVISIBLE)
def test_invisible_character_does_not_cut_a_copy(char):
    text = english_run(250)
    disguised = char.join(text[i : i + 5] for i in range(0, 250, 5))
    assert longest(disguised, text).violates


def test_several_invisible_characters_every_sixty_characters():
    text = english_run(250)
    marks = "️͏᠋\x7fㅤ"
    parts = [text[i : i + 60] for i in range(0, 250, 60)]
    disguised = "".join(p + marks[i % len(marks)] for i, p in enumerate(parts))
    assert longest(disguised, text).violates


def test_invisible_characters_in_the_source_do_not_cut_a_copy():
    text = english_run(250)
    source = "​".join(text[i : i + 7] for i in range(0, 250, 7))
    assert longest(text, source).violates


def test_whitespace_is_kept_as_a_space_but_other_control_characters_are_removed():
    assert normalize("a\tb\nc\fd\u3000e") == "a b c d e"
    assert normalize("ab\x1ccd\x85ef\x0bgh") == "abcdefgh"


# --- 上限の境界（重み付きの長さが上限を超えたら違反） ---


@pytest.mark.parametrize(("n", "violates"), [(199, False), (200, False), (201, True)])
def test_english_boundary_is_n_exceeded(n, violates):
    q = english_run(n)
    result = longest(f"1{q}2", f"8{q}9")
    assert result.limit == 200 and result.violates is violates
    if violates:
        assert result.text == q


@pytest.mark.parametrize(("n", "violates"), [(99, False), (100, False), (101, True)])
def test_japanese_boundary_is_n_exceeded(n, violates):
    q = japanese_run(n)
    result = longest(f"は{q}と", f"が{q}で")
    assert result.violates is violates
    if violates:
        assert result.weight == 2 * n


@pytest.mark.parametrize(("extra", "violates"), [(79, False), (80, False), (81, True)])
def test_mixed_match_boundary_through_the_daily_check(extra, violates):
    # 日本語 60 文字（重み 120）＋英語 extra 文字。合計が 200 を超えたら違反（段差がない）
    quoted = japanese_run(60) + english_run(extra)
    data = data_with(d1=untrusted(primary_text="先頭" + quoted + "末尾"))
    entry = daily_entry(points=("見出し" + quoted + "おわり",))
    kept, _ = check_daily_quotes([entry], data, {}, LIMITS)
    assert (kept == []) is violates


def test_half_width_katakana_counts_as_japanese():
    source = "ｱｲｳｴｵ" * 21  # 105 文字。全角カナにそろえると重み 210
    assert longest(source, "ア" + source).violates


def test_primary_text_uses_its_own_limit():
    limits = replace(LIMITS, primary_max_en=50)
    en = english_run(60)
    assert not longest(en, en, limits=limits).violates  # 本文以外は 200
    assert longest(en, en, primary=True, limits=limits).violates
    ja = japanese_run(30)  # 重み 60
    assert not longest(ja, ja, limits=limits).violates
    assert longest(ja, ja, primary=True, limits=limits).violates


def test_worst_source_decides():
    sources = [QuoteSource("unrelated text here"), QuoteSource(EN, primary=True)]
    result = longest_quote(EN_PARAGRAPHS[1], sources, LIMITS)
    assert result.primary and result.violates


# --- 頻出する文字 ---


def test_frequent_characters_do_not_hide_a_quote():
    # どの文字も元の文字列の1%を超えて現れる。autojunk=True（既定）だと一致を1つも見つけられない
    source, output = "xy" + "ab" * 150, "ab" * 60
    assert SequenceMatcher(None, output, source).find_longest_match().size == 0
    result = longest(output, source, limits=QuoteLimits(max_en=100))
    assert len(result.text) == 120 and result.violates


# --- 日英のフィクスチャ ---


@pytest.mark.parametrize("source", [EN, JA])
def test_copying_a_paragraph_is_a_violation(source):
    paragraph = source.split("\n\n")[1]
    assert longest(paragraph, source, primary=True).violates


def test_markdown_and_spacing_changes_do_not_avoid_detection():
    disguised = "**" + EN_PARAGRAPHS[1].replace(" ", "  ").upper().replace(".", ".\n") + "**"
    assert longest(disguised, EN, primary=True).violates


@pytest.mark.parametrize(
    ("text", "source"),
    [
        ("The new planner cut median latency by a third. Prepared statements are cached.", EN),
        ("新しいプランナーで待ち時間が約3分の1に減った。接続ごとにキャッシュもされる。", JA),
    ],
)
def test_summaries_in_own_words_pass(text, source):
    assert not longest(text, source, primary=True).violates


def test_empty_texts_have_no_match():
    assert not longest("", EN).violates and not longest("text", "").violates


# --- メタデータの語句の除外 ---


def test_meta_terms_are_excluded_from_matching():
    term = "acme-corp/" + "very-long-repository-name-" * 9
    text = f"{term} v1.2.3"
    assert longest(text, text).violates
    assert not longest(text, text, terms=[term, "V1.2.3"]).violates


def test_meta_terms_are_normalized_like_the_text():
    text = "ＯＰＥＮＡＩ／" + "ＬＯＮＧ" * 60
    assert longest(text, text).violates
    assert not longest(text, text, terms=["openai/" + "long" * 60]).violates


def test_text_around_a_meta_term_is_not_joined_into_a_false_match():
    left, right = english_run(150), english_run(150)[::-1]
    result = longest(f"{left} foo/bar {right}", f"{left} baz/qux {right}", ["foo/bar", "baz/qux"])
    assert result.text.strip() == left


def test_same_meta_term_in_both_texts_does_not_cut_a_copy():
    text = ("quillstone " + english_run(100) + " ") * 6  # 語句が何度も出る丸写し
    result = longest(text, text, terms=["Quillstone"])
    assert result.violates  # 語句の位置で一致が切れない
    assert len(result.text) > 600  # 語句の文字を除いた全体が1つの一致になる


def test_meta_terms_match_whole_words_only():
    text = "revue of the uv tool"
    assert longest(text, text, terms=["uv"], limits=SMALL).text.startswith("revue of the ")


# --- 日次の項目の検査 ---


def data_with(**items):
    """items は {項目 ID: untrusted の辞書}。"""
    return {"items": [{"id": i, "untrusted": u} for i, u in items.items()]}


def untrusted(title="", excerpt="", primary_text=None):
    return {"title": title, "excerpt": excerpt, "primary_text": primary_text}


def daily_entry(item_ids=("d1",), headline="見出し", points=("要点",), importance="理由", **kw):
    return DailyEntry(
        tuple(item_ids), None, None, "ai", 1, "full", headline, tuple(points), importance,
        kw.get("period_event"),
    )  # fmt: skip


def test_entry_quoting_the_passed_primary_text_is_excluded():
    data = data_with(d1=untrusted(primary_text=EN))
    bad = daily_entry(points=(EN_PARAGRAPHS[1],))
    good = daily_entry(points=("Prepared statements are cached per connection.",))
    kept, excluded = check_daily_quotes([bad, good], data, {}, LIMITS)
    assert kept == [good] and [x.entry for x in excluded] == [bad]
    assert "一次情報の本文" in excluded[0].reason and "上限 200" in excluded[0].reason


def test_only_the_range_passed_to_ai_is_compared():
    passed = "\n\n".join(EN_PARAGRAPHS[:2])  # AI に渡したのは前半だけ
    data = data_with(d1=untrusted(primary_text=passed))
    beyond = daily_entry(points=(EN_PARAGRAPHS[3],))  # 渡していない範囲は、なぞれない
    inside = daily_entry(points=(EN_PARAGRAPHS[1],))
    kept, excluded = check_daily_quotes([beyond, inside], data, {}, LIMITS)
    assert kept == [beyond] and [x.entry for x in excluded] == [inside]


@pytest.mark.parametrize("field", ["headline", "importance", "period_event", "points"])
def test_every_text_field_is_checked_against_the_title(field):
    title = english_run(210)
    data = data_with(d1=untrusted(title=title))
    value = (title,) if field == "points" else title
    entry = daily_entry(**{field: value})
    kept, excluded = check_daily_quotes([entry], data, {}, LIMITS)
    assert not kept and "タイトル・抜粋" in excluded[0].reason


def test_excerpt_is_checked_with_japanese_counted_double():
    data = data_with(d1=untrusted(excerpt=JA))
    kept, excluded = check_daily_quotes([daily_entry(points=(JA_PARAGRAPHS[1],))], data, {}, LIMITS)
    assert not kept and "上限 200" in excluded[0].reason


def test_entry_is_compared_with_every_item_passed_to_ai():
    # 別の項目の本文を丸写しした項目も除外する
    data = data_with(d1=untrusted(primary_text=EN), d2=untrusted(primary_text="別の記事"))
    points = (EN_PARAGRAPHS[1],)
    for ids in (("d2",), ("d1", "d2"), ("d1",)):
        entry = daily_entry(item_ids=ids, points=points)
        assert check_daily_quotes([entry], data, {}, LIMITS)[0] == []


def test_without_source_text_entries_are_kept():
    entries = [daily_entry(item_ids=("d9",)), daily_entry()]
    assert check_daily_quotes(entries, data_with(d1=untrusted()), {}, LIMITS) == (entries, [])


def test_meta_terms_of_the_item_are_not_counted_as_a_quote():
    term = "acme-corp/" + "very-long-repository-name-" * 9
    data = data_with(d1=untrusted(title=term))
    entry = daily_entry(headline=term)
    assert check_daily_quotes([entry], data, {"d1": [term]}, LIMITS)[0] == [entry]
    assert check_daily_quotes([entry], data, {}, LIMITS)[0] == []


def test_meta_terms_of_another_item_do_not_weaken_the_check():
    data = data_with(d1=untrusted(title="x"), d2=untrusted(title=english_run(300)))
    quoted = daily_entry(item_ids=("d1", "d2"), headline=english_run(300))
    # d1 の語句は d1 の元のテキストにだけ使う。d2 のタイトルの丸写しは見逃さない
    assert check_daily_quotes([quoted], data, {"d1": [english_run(300)]}, LIMITS)[0] == []


def test_meta_terms_by_id_reads_the_items_terms():
    class FakeItem:
        meta_terms = ("a/b", "v1")

    assert meta_terms_by_id({"d1": FakeItem()}) == {"d1": ("a/b", "v1")}


def test_quote_excluded_entry_does_not_take_the_id_from_a_later_entry():
    from tech_researcher.stages.daily import _check_entries

    now = datetime(2026, 10, 23, tzinfo=UTC)
    url = "https://aws.example/post"
    record = Record("rss:aws", url, "t", "datetime", now, (), published_at=now)
    items = {"i1": Item(url, (record,), (), (), (), (), None)}
    data = {
        "items": [
            {
                "id": "i1",
                "links": [],
                "needs_period_event": False,
                "untrusted": untrusted(title=english_run(40)),
            },
        ]
    }
    bad = daily_entry(item_ids=("i1",), headline=english_run(40))
    good = daily_entry(item_ids=("i1",), headline="別の見出し")
    kept, excluded = _check_entries(
        [bad, good], data, items, QuoteLimits(max_en=30, primary_max_en=30)
    )
    assert kept == [good]
    assert [(c, x.entry) for c, x in excluded] == [("long_quote", bad)]


# --- 処理時間（③ の verify_minutes = 2 に十分な余裕を持って収まる） ---

TIME_LIMIT_S = 5


def random_text(seed: int, size: int) -> str:
    rng = random.Random(seed)
    words = [
        "".join(rng.choices("abcdefghijklmnopqrstuvwxyz", k=rng.randint(2, 9))) for _ in range(3000)
    ]
    parts, total = [], 0
    while total < size:
        word = rng.choice(words)
        parts.append(word)
        total += len(word) + 1
    return " ".join(parts)[:size]


def test_large_body_and_many_entries_finish_in_time():
    body = random_text(1, 2_000_000)
    data = data_with(d1=untrusted(primary_text=body))
    unrelated = random_text(2, 4000)
    entries = [
        daily_entry(
            item_ids=("d1",) * 500,  # 重なった item_ids
            headline=unrelated[i * 300 : i * 300 + 100],
            points=tuple(unrelated[i * 300 + j * 50 : i * 300 + j * 50 + 50] for j in range(3)),
            importance=unrelated[i * 300 + 150 : i * 300 + 300],
        )
        for i in range(12)
    ]
    copied = daily_entry(points=(body[1_500_000:1_500_300],))
    start = time.monotonic()
    kept, excluded = check_daily_quotes([*entries, copied], data, {}, LIMITS)
    assert time.monotonic() - start < TIME_LIMIT_S
    assert kept == entries and [x.entry for x in excluded] == [copied]


def test_many_items_finish_in_time():
    # 2MB を 300 項目に分け、各項目にタイトル・抜粋・本文がある
    data = data_with(
        **{
            f"d{i}": untrusted(
                title=random_text(i, 80), excerpt=random_text(i + 1000, 300),
                primary_text=random_text(i + 2000, 6000),
            )
            for i in range(300)
        }
    )  # fmt: skip
    entries = [daily_entry(points=(random_text(5000 + i, 300),)) for i in range(20)]
    copied = daily_entry(points=(data["items"][250]["untrusted"]["primary_text"][2000:2300],))
    start = time.monotonic()
    kept, excluded = check_daily_quotes([*entries, copied], data, {}, LIMITS)
    assert time.monotonic() - start < TIME_LIMIT_S
    assert kept == entries and [x.entry for x in excluded] == [copied]


def test_body_made_of_one_repeated_character_finishes_in_time():
    data = data_with(d1=untrusted(primary_text="a" * 2_000_000))
    entries = [daily_entry(points=("a" * 300,)), daily_entry(points=("ab" * 150,))]
    start = time.monotonic()
    kept, excluded = check_daily_quotes(entries, data, {}, LIMITS)
    assert time.monotonic() - start < TIME_LIMIT_S
    assert kept == [entries[1]] and [x.entry for x in excluded] == [entries[0]]


# --- 窓の方式の健全性（上限を超える一致を見落とさない） ---


def test_long_english_match_does_not_hide_a_japanese_violation():
    en, ja = english_run(150), japanese_run(101)  # 英語は重み150、日本語は重み202
    assert longest(f"{en} x {ja}", f"{en} y {ja}").violates
    assert longest(f"{ja} x {en}", f"{ja} y {en}").violates
    assert longest(f"{en} x {ja}", f"{ja} y {en}").violates  # 元のテキストで順が逆


def test_window_repeated_many_times_before_the_real_match():
    ja = japanese_run(101)
    source = (ja[:50] + "。 ") * 30 + ja
    assert longest(ja, source).violates


def test_many_repeated_windows_before_a_violation_in_a_long_output():
    block = english_run(60)
    source = " ".join([block] * 300) + " " + japanese_run(101)
    output = " ".join([block] * 12) + " zzz " + japanese_run(101)
    assert longest(output, source).violates


def brute_force_weight(a: str, b: str) -> int:
    """最長の共通部分文字列の、重み付きの長さ（日本語は2）を総当たりで求める。"""
    best = 0
    previous = [0] * (len(b) + 1)
    weights = [0 if c == "\ue000" else weighted_length(c) for c in b]
    for x in a:
        row = [0] * (len(b) + 1)
        for j, y in enumerate(b, start=1):
            if x == y:
                row[j] = previous[j - 1] + weights[j - 1]
                best = max(best, row[j])
        previous = row
    return best


def test_decision_matches_brute_force_on_random_mixed_texts():
    rng = random.Random(7)
    alphabet = "abcあいう"
    for _ in range(600):
        a = "".join(rng.choices(alphabet, k=rng.randint(1, 30)))
        b = "".join(rng.choices(alphabet, k=rng.randint(1, 30)))
        limit = rng.randint(1, 12)
        limits = QuoteLimits(max_en=limit, primary_max_en=limit)
        brute = brute_force_weight(normalize(a), normalize(b))
        result = longest(a, b, limits=limits)
        assert result.violates is (brute > limit), (a, b, limit)
        if brute > limit:
            assert result.weight == brute, (a, b, limit)  # 違反のときは最長の長さ


def test_window_repeated_beyond_the_cap_is_treated_as_a_violation():
    ja = japanese_run(101)
    unit = ja[:51]  # 重み102。出力の先頭の窓（重み101）を含む
    cap = quote._MAX_HITS
    over = longest(ja, "。 ".join([unit] * (cap + 1)))
    assert over.violates and over.unchecked  # 調べきれないので違反とみなす
    assert not longest(ja, "。 ".join([unit] * cap)).violates  # 上限に届かなければ、長さを測る


def test_decision_matches_brute_force_with_meta_terms():
    rng = random.Random(11)
    alphabet = "abあい"
    for _ in range(600):
        a = "".join(rng.choices(alphabet, k=rng.randint(1, 30)))
        b = "".join(rng.choices(alphabet, k=rng.randint(1, 30)))
        limit = rng.randint(1, 10)
        limits = QuoteLimits(max_en=limit, primary_max_en=limit)
        masked = [normalize(t).replace("あい", "\ue000") for t in (a, b)]
        brute = brute_force_weight(*masked)
        result = longest(a, b, terms=["あい"], limits=limits)
        assert result.violates is (brute > limit), (a, b, limit)
        if brute > limit:
            assert result.weight == brute, (a, b, limit)


def test_unchecked_entry_is_excluded_with_its_own_reason():
    ja = japanese_run(101)
    source = "。 ".join([ja[:51]] * (quote._MAX_HITS + 1))
    kept, excluded = check_daily_quotes(
        [daily_entry(points=(ja,))], data_with(d1=untrusted(primary_text=source)), {}, LIMITS
    )
    assert kept == [] and "調べきれない" in excluded[0].reason
    assert "上限" not in excluded[0].reason  # 本当ではない長さは出さない


def test_common_prefix_length_by_doubling_and_bisection():
    from tech_researcher.validate.quote import _common

    for limit in (0, 1, 2, 5, 33, 100):
        for true in range(limit + 1):
            assert _common(lambda n, t=true: n <= t, limit) == true


def test_repeated_windows_in_many_entries_finish_in_time():
    unit = english_run(101)
    body = "ZZ".join([unit] * 9999)  # 同じ101字の並びが9,999回（つながらない）
    data = data_with(d1=untrusted(primary_text=body))
    output = "YY".join([unit] * 40)  # 約4,040字
    entries = [daily_entry(headline=f"h{i}", points=(output,)) for i in range(20)]
    start = time.monotonic()
    kept, excluded = check_daily_quotes(entries, data, {}, LIMITS)
    assert time.monotonic() - start < TIME_LIMIT_S
    assert kept == entries and excluded == []  # 一致は101字で、上限（200）を超えない


def test_violation_reports_the_longest_match():
    a, b = "a" * 201, "b" * 300
    result = longest(f"{a}x{b}", f"{a}y{b}")
    assert result.violates and result.weight == 300
    result = longest(f"{b}x{a}", f"{a}y{b}")
    assert result.weight == 300


def test_narrow_space_in_the_source_is_a_plain_space():
    text = english_run(250)
    spaced = " ".join(text[i : i + 5] for i in range(0, 250, 5))
    source = spaced.replace(" ", "\u202f")  # 元のテキストは 10\u202fGB のような書き方
    assert longest(spaced, source).violates
    assert normalize(source, external=True) == spaced
