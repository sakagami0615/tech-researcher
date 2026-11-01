"""収集レコードと収集項目（data-model.md 2.2〜2.6、D-06・D-18）。

収集レコードは各収集処理が返す1件、収集項目はそれを正規化した URL でまとめた1件。
外部由来の自由文（title・excerpt・primary_text）はレコードにだけ持たせ、
コードが作る値（出来事・候補リンクの ID など）と混ぜない（US-E7-06）。
"""

from dataclasses import dataclass
from datetime import datetime

EVENT_TYPES = frozenset({"published", "released", "posted", "attention_spike", "new_repo"})
SPIKE_BASES = frozenset({"feed_entry", "threshold", "star_growth"})
PUBLISHED_PRECISIONS = frozenset({"datetime", "date", "unknown"})
PRIMARY_TEXT_ORIGINS = frozenset({"rss_full", "github_release", "github_readme"})


def _check_aware(name: str, moment: datetime | None) -> None:
    if moment is not None and moment.tzinfo is None:
        raise ValueError(f"{name} にタイムゾーンがない")


@dataclass(frozen=True)
class Event:
    """出来事（2.4）。basis は attention_spike のときだけ持つ。"""

    type: str
    at: datetime
    basis: str | None = None

    def __post_init__(self):
        if self.type not in EVENT_TYPES:
            raise ValueError(f"知らない出来事の種類 {self.type}")
        if (self.type == "attention_spike") != (self.basis is not None):
            raise ValueError("basis は attention_spike のときだけ必要")
        if self.basis is not None and self.basis not in SPIKE_BASES:
            raise ValueError(f"知らない basis {self.basis}")
        _check_aware("at", self.at)


@dataclass(frozen=True)
class Attention:
    """注目度の値（2.4）。metric は情報源の定義と対にするので固定しない。"""

    metric: str
    value: float
    measured_at: datetime

    def __post_init__(self):
        _check_aware("measured_at", self.measured_at)


@dataclass(frozen=True)
class GitHubRepo:
    """リポジトリの項目だけが持つ値（2.3）。mentioned_by は言及元の項目（その URL で表す）。"""

    full_name: str
    created_at: datetime
    stars_total: int
    star_delta_24h: int | None = None
    mentioned_by: tuple[str, ...] = ()

    def __post_init__(self):
        _check_aware("created_at", self.created_at)


@dataclass(frozen=True)
class Record:
    """収集レコード（2.3）。link_url は集約型の情報源（HN など）が指している先。"""

    source_id: str
    url: str
    title: str
    published_precision: str
    fetched_at: datetime
    events: tuple[Event, ...]
    link_url: str | None = None
    excerpt: str | None = None
    primary_text: str | None = None
    primary_text_origin: str | None = None
    published_at: datetime | None = None
    posted_at: datetime | None = None
    attention: tuple[Attention, ...] = ()
    links: tuple[str, ...] = ()
    github: GitHubRepo | None = None
    meta_terms: tuple[str, ...] = ()

    def __post_init__(self):
        if self.published_precision not in PUBLISHED_PRECISIONS:
            raise ValueError(f"知らない published_precision {self.published_precision}")
        if (self.published_at is None) != (self.published_precision == "unknown"):
            raise ValueError("published_at がないときだけ published_precision は unknown")
        if self.primary_text_origin not in PRIMARY_TEXT_ORIGINS | {None}:
            raise ValueError(f"知らない primary_text_origin {self.primary_text_origin}")
        if (self.primary_text is None) != (self.primary_text_origin is None):
            raise ValueError("primary_text と primary_text_origin は対で持つ")
        for name in ("fetched_at", "published_at", "posted_at"):
            _check_aware(name, getattr(self, name))


@dataclass(frozen=True)
class CandidateLink:
    """一次情報の候補リンク（2.6）。AI には link_id・url・domain だけを渡す。"""

    link_id: str
    url: str
    domain: str


@dataclass(frozen=True)
class Item:
    """収集項目（2.2）。url は正規化した指している先の URL、sources はまとめた元のレコード。

    events・attention・links・meta_terms は sources の値を順に合わせたもの。links は正規化済みで、
    ID は items の主キーが決まってから candidate_links で付ける。
    急上昇したリポジトリの URL は、それを検出する収集処理がレコードの links に入れる（D-13）。
    """

    url: str
    sources: tuple[Record, ...]
    events: tuple[Event, ...]
    attention: tuple[Attention, ...]
    links: tuple[str, ...]
    meta_terms: tuple[str, ...]
    github: GitHubRepo | None


@dataclass(frozen=True)
class SourceResult:
    """1つの情報源の取得結果。error が None で records が空なら、0件の成功。

    attempts は初回を含めた要求の回数（US-E6-04 の再試行回数の元）。
    """

    source_id: str
    records: tuple[Record, ...]
    attempts: int
    error: str | None = None
