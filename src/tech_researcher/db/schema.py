"""本番の DB の接続と表の版（data-model.md 3.2・3.10）。

書き込む接続は `open_db` で開く。外部キー・WAL・待機時間を設定し、表を最新の版にしてから返す。
ドライランはここを使わず、readonly.py で読み取り専用に開く（D-22）。

表の版は `PRAGMA user_version` で持ち、MIGRATIONS を順に当てる。版を上げるときは、
既存の要素を書き換えずに末尾へ足す（すでに当てた DB に同じ版を当て直すことはないため）。
未適用の版は1つのトランザクションでまとめて当てるので、版の文に BEGIN・COMMIT を書かない。
`PRAGMA foreign_keys` もトランザクションの中では効かないので書かない（表を作り直す版が
要るときに、当て方を考える）。
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

# ほかの実行が書いている間に待つ時間（ミリ秒）。月曜は日次と週次が重なりうる（3.2）。
# 書き込みはどれも短いので、待ち切れないのは相手が止まっているときだけとみなし、運用で
# 変える値にはしない（設定ファイルに置くのは D-20 の項目だけ）
BUSY_TIMEOUT_MS = 30_000

# 1要素が1つの版。STRICT で型を確かめる。enum の値は CHECK で縛らず、書き込む側のコードで
# 確かめる（SQLite で CHECK を変えるには表の作り直しが要り、値を足すたびに版が増えるため）。
# 例外は、ドライランの実行 ID を runs に入れないこと（D-22。外部キーで他の表にも効く）
MIGRATIONS: tuple[str, ...] = (
    """
    CREATE TABLE runs (
        run_id TEXT PRIMARY KEY CHECK (run_id NOT LIKE 'dry-%'),
        cadence TEXT NOT NULL,
        target TEXT NOT NULL,
        period_start TEXT NOT NULL,
        period_end TEXT NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        status TEXT NOT NULL,
        failed_stage TEXT,
        error_category TEXT,
        retry_count INTEGER,
        investigation_json TEXT,
        model TEXT
    ) STRICT;
    CREATE INDEX runs_target ON runs (cadence, target);

    CREATE TABLE source_fetches (
        run_id TEXT NOT NULL REFERENCES runs (run_id),
        source_id TEXT NOT NULL,
        status TEXT NOT NULL,
        error_category TEXT,
        attempts INTEGER NOT NULL,
        record_count INTEGER NOT NULL,
        in_period_count INTEGER NOT NULL,
        fetched_at TEXT NOT NULL,
        PRIMARY KEY (run_id, source_id)
    ) STRICT;

    CREATE TABLE items (
        item_id INTEGER PRIMARY KEY,
        cadence TEXT NOT NULL,
        target TEXT NOT NULL,
        canonical_url TEXT NOT NULL,
        kind TEXT NOT NULL,
        title TEXT NOT NULL,
        excerpt TEXT,
        published_at TEXT,
        published_precision TEXT NOT NULL,
        posted_at TEXT,
        inclusion_event TEXT NOT NULL,
        inclusion_basis TEXT,
        inclusion_at TEXT NOT NULL,
        events_json TEXT NOT NULL,
        attention_json TEXT NOT NULL,
        sources_json TEXT NOT NULL,
        links_json TEXT NOT NULL,
        github_json TEXT,
        meta_terms_json TEXT NOT NULL,
        primary_text_hash TEXT,
        primary_text_len INTEGER,
        hold_reason TEXT,
        first_run_id TEXT NOT NULL REFERENCES runs (run_id),
        last_run_id TEXT NOT NULL REFERENCES runs (run_id),
        UNIQUE (cadence, target, canonical_url)
    ) STRICT;
    CREATE INDEX items_canonical_url ON items (canonical_url);
    """,
    # 人気型フィードの取得記録（D-13）。日次の取得ごとに、期間で絞る前の全件を残す
    """
    CREATE TABLE feed_snapshots (
        source_id TEXT NOT NULL,
        target TEXT NOT NULL,
        canonical_url TEXT NOT NULL,
        rank INTEGER NOT NULL,
        attention_json TEXT NOT NULL,
        fetched_at TEXT NOT NULL,
        PRIMARY KEY (source_id, target, canonical_url)
    ) STRICT;
    """,
    # GitHub のリポジトリの累計 Star 数の日次の測定（D-13）。監視リストもここから引く
    """
    CREATE TABLE star_snapshots (
        repo TEXT NOT NULL,
        target TEXT NOT NULL,
        measured_at TEXT NOT NULL,
        stars_total INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        routes_json TEXT NOT NULL
            CHECK (json_valid(routes_json) AND json_type(routes_json) = 'array'),
        PRIMARY KEY (repo, target),
        UNIQUE (repo, measured_at)
    ) STRICT;
    CREATE INDEX star_snapshots_target ON star_snapshots (target);
    """,
    # 生成したレポート案と検証結果、除外した項目、Issue の対応（3.4。D-18）
    """
    CREATE TABLE reports (
        run_id TEXT PRIMARY KEY REFERENCES runs (run_id),
        cadence TEXT NOT NULL,
        target TEXT NOT NULL,
        status TEXT NOT NULL,
        rejected_checks TEXT NOT NULL
            CHECK (json_valid(rejected_checks) AND json_type(rejected_checks) = 'array'),
        counts_json TEXT NOT NULL CHECK (json_valid(counts_json)),
        verification_json TEXT NOT NULL CHECK (json_valid(verification_json)),
        ai_output_json TEXT,
        body_markdown TEXT
    ) STRICT;

    CREATE TABLE report_entries (
        run_id TEXT NOT NULL REFERENCES reports (run_id),
        entry_no INTEGER NOT NULL,
        section TEXT NOT NULL,
        status TEXT NOT NULL,
        exclusion_reasons TEXT NOT NULL
            CHECK (json_valid(exclusion_reasons) AND json_type(exclusion_reasons) = 'array'),
        display_no INTEGER,
        theme_id TEXT,
        item_ids_json TEXT NOT NULL
            CHECK (json_valid(item_ids_json) AND json_type(item_ids_json) = 'array'),
        past_ids_json TEXT NOT NULL
            CHECK (json_valid(past_ids_json) AND json_type(past_ids_json) = 'array'),
        payload_json TEXT NOT NULL
            CHECK (json_valid(payload_json) AND json_type(payload_json) = 'object'),
        PRIMARY KEY (run_id, entry_no)
    ) STRICT;
    CREATE UNIQUE INDEX report_entries_display_no
        ON report_entries (run_id, display_no) WHERE display_no IS NOT NULL;
    CREATE INDEX report_entries_theme_id ON report_entries (theme_id) WHERE theme_id IS NOT NULL;
    CREATE UNIQUE INDEX report_entries_published_theme
        ON report_entries (run_id, theme_id) WHERE section = 'theme' AND status = 'published';

    CREATE TABLE issues (
        id INTEGER PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs (run_id),
        kind TEXT NOT NULL,
        cadence TEXT NOT NULL,
        target TEXT NOT NULL,
        state TEXT NOT NULL,
        repo TEXT,
        number INTEGER,
        created_at TEXT NOT NULL,
        projects_state TEXT,
        projects_attempts INTEGER NOT NULL DEFAULT 0,
        projects_last_error TEXT,
        projects_last_run_id TEXT,
        UNIQUE (run_id, kind)
    ) STRICT;
    CREATE UNIQUE INDEX issues_report_target
        ON issues (cadence, target) WHERE kind = 'report' AND state = 'created';
    """,
)
SCHEMA_VERSION = len(MIGRATIONS)


class SchemaError(RuntimeError):
    pass


def open_db(path: Path, *, busy_timeout_ms: int = BUSY_TIMEOUT_MS) -> sqlite3.Connection:
    """本番の DB を書き込み用に開く。なければ作り、表を最新の版にする。

    busy_timeout_ms はテストで待ち時間を短くするためのもの。

    autocommit にして、トランザクションは `transaction` で明示する。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, autocommit=True)
    try:
        conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        conn.execute("PRAGMA foreign_keys = ON")
        # WAL は DB のファイルに残る設定なので、2回目以降は確かめるだけになる
        (mode,) = conn.execute("PRAGMA journal_mode = WAL").fetchone()
        if mode != "wal":
            raise SchemaError(f"WAL にできない（journal_mode = {mode}）")
        migrate(conn)
    except BaseException:
        conn.close()
        raise
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """書き込みのトランザクション。抜けるときにコミットし、例外なら取り消す。

    最初に書き込みのロックを取る（IMMEDIATE）。読んでから書く形だと、WAL では待たずに
    SQLITE_BUSY になりうるため。ロックを待つ時間は busy_timeout による。入れ子にはできない。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        # ディスクが一杯などでは SQLite がすでに取り消している。元の例外を隠さない
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def migrate(conn: sqlite3.Connection) -> None:
    """表の版を最新にする。同時に起動した実行どうしでも、当てるのは一方だけになる。"""
    with transaction(conn):
        (version,) = conn.execute("PRAGMA user_version").fetchone()
        check_version(version, allow_older=True)
        for number, script in enumerate(MIGRATIONS[version:], start=version + 1):
            # autocommit の接続では executescript はコミットしないので、途中の失敗は取り消せる
            conn.executescript(script)
            conn.execute(f"PRAGMA user_version = {number}")


def check_version(version: int, *, allow_older: bool = False) -> None:
    if version > SCHEMA_VERSION:
        raise SchemaError(f"DB の版 {version} がコードの版 {SCHEMA_VERSION} より新しい")
    if version < SCHEMA_VERSION and not allow_older:
        raise SchemaError(
            f"DB の版 {version} がコードの版 {SCHEMA_VERSION} より古い"
            "（本番の実行で版を上げてから使う）"
        )
