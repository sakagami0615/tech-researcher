"""GitHub のリポジトリの急上昇・新規の判定（D-13 の判定の流れの 3、data-model.md 2.4・3.5）。

測った Star 数（今日の測定）を前日の測定と比べ、次の2つを判定して、リポジトリの項目の収集
レコードに出来事を足す。コードだけで判定し、AI には重要度の評価と要約だけを任せる。

- 新規（new_repo）：作成から new_repo_days 日以内で、累計が new_repo_stars 以上のもののうち、
  累計の多い順に上位 new_repo_top_n 件。前日の記録は使わない。急上昇ではなく「新しい OSS」として
  出来事の種類を分け、同じリポジトリに急上昇は足さない
- 急上昇（attention_spike、basis = star_growth）：新規でないリポジトリのうち、前日比の増分が
  threshold_increase 以上のものを、増分の大きい順に上位 report_top_n 件
  - 前日の測定がないリポジトリ（初めて見るもの）は判定しない
  - 前日の測定が1件もない日は、判定しなかったことを star_unjudged で返す（ヘッダの定型文。1.6）

すでに急上昇・新規として載せたリポジトリ（featured。D-13）は、上位の数（report_top_n・
new_repo_top_n）に入れない。
レコードは足す（観測として残し、除外は項目の hold_reason で行う）。
レコードの links には、候補リンクとしてリポジトリの URL を入れる（D-13）。
"""

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import timedelta

from tech_researcher.collect.github_stars import StarResult
from tech_researcher.collect.records import Attention, Event, GitHubRepo, Record
from tech_researcher.collect.sources import GITHUB_REPOS_SOURCE_ID
from tech_researcher.config import GitHubRepos
from tech_researcher.db.star_snapshots import StarSnapshot
from tech_researcher.periods import Target, spike_window


@dataclass(frozen=True)
class SpikeResult:
    """判定の結果。star_unjudged は前日の測定が1件もなく、前日比を判定しなかったこと。"""

    records: tuple[Record, ...]
    star_unjudged: bool


def repo_url(repo: str) -> str:
    return f"https://github.com/{repo}"


def judge_star_spikes(
    settings: GitHubRepos,
    target: Target,
    stars: StarResult,
    previous: Mapping[str, StarSnapshot],
    featured: Collection[str] = (),
) -> SpikeResult:
    """今日の測定 stars を前日の測定 previous と比べ、急上昇・新規のレコードを返す。

    日次だけで行う（週次は測らないので空を返す）。featured は載せた項目の canonical_url
    （db.history.featured_urls）。
    """
    if target.cadence != "daily":
        return SpikeResult((), False)
    new: list[StarSnapshot] = []
    growth: list[tuple[int, StarSnapshot]] = []
    for snapshot in stars.snapshots:
        if is_new_repo(settings, target, snapshot):
            new.append(snapshot)
        elif (before := previous.get(snapshot.repo)) is not None:
            delta = snapshot.stars_total - before.stars_total
            if delta >= settings.threshold_increase:
                growth.append((delta, snapshot))
    growth.sort(key=lambda g: (-g[0], g[1].repo))
    # 載せたものは上位の数に入れず、観測のために残す
    shown = [g for g in growth if repo_url(g[1].repo) not in featured]
    top = {g[1].repo for g in shown[: settings.report_top_n]}
    new.sort(key=lambda s: (-s.stars_total, s.repo))
    new_shown = [s for s in new if repo_url(s.repo) not in featured][: settings.new_repo_top_n]
    chosen = [
        *((s, "new_repo") for s in new if s in new_shown or repo_url(s.repo) in featured),
        *((s, "star_growth") for _, s in growth if s.repo in top or repo_url(s.repo) in featured),
    ]
    records = [
        _record(s, kind, previous.get(s.repo), stars.mentioned_by.get(s.repo, ()))
        for s, kind in sorted(chosen, key=lambda c: c[0].repo)
    ]
    return SpikeResult(tuple(records), not previous)


def is_new_repo(settings: GitHubRepos, target: Target, snapshot: StarSnapshot) -> bool:
    """作成から new_repo_days 日以内で、累計が new_repo_stars 以上か。

    日数は、判定の窓の終わり（対象日の翌日 6:00。日次の実行時刻）から数える。測定の時刻ではなく
    窓の終わりにするのは、同じ対象の再実行・ドライランで判定が変わらないようにするため。
    """
    age = spike_window(target).end - snapshot.created_at
    return (
        timedelta(0) <= age <= timedelta(days=settings.new_repo_days)
        and snapshot.stars_total >= settings.new_repo_stars
    )


def _record(
    snapshot: StarSnapshot,
    kind: str,
    previous: StarSnapshot | None,
    mentioned_by: tuple[str, ...],
) -> Record:
    delta = None if previous is None else snapshot.stars_total - previous.stars_total
    event = (
        Event("new_repo", snapshot.measured_at)
        if kind == "new_repo"
        else Event("attention_spike", snapshot.measured_at, "star_growth")
    )
    attention = [Attention("stars_total", snapshot.stars_total, snapshot.measured_at)]
    if delta is not None:
        attention.append(Attention("star_delta_24h", delta, snapshot.measured_at))
    url = repo_url(snapshot.repo)
    return Record(
        source_id=GITHUB_REPOS_SOURCE_ID,
        url=url,
        title=snapshot.repo,
        published_precision="datetime",
        fetched_at=snapshot.measured_at,
        events=(event,),
        published_at=snapshot.created_at,
        attention=tuple(attention),
        links=(url,),
        github=GitHubRepo(
            snapshot.repo, snapshot.created_at, snapshot.stars_total, delta, mentioned_by
        ),
        meta_terms=(snapshot.repo,),
    )
