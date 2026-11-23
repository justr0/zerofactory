"""Unit tests for the migration runner."""

import sqlite3
from pathlib import Path

from migrations.runner import (
    get_applied_migrations,
    get_migration_status,
    run_migrations,
)


def test_migration_status_does_not_create_missing_database(tmp_path: Path):
    """Inspecting status for a missing database leaves the filesystem unchanged."""
    db_path = tmp_path / "missing.db"
    mdir = tmp_path / "migrations"
    mdir.mkdir()
    (mdir / "0001_first.sql").write_text("CREATE TABLE t (id INTEGER PRIMARY KEY);")

    status = get_migration_status(db_path=db_path, migrations_dir=mdir)

    assert [(item["version"], item["applied"]) for item in status] == [
        ("0001_first", False)
    ]
    assert not db_path.exists()


def test_migration_status_does_not_create_tracking_table(tmp_path: Path):
    """Untracked legacy databases report migrations pending without writes."""
    db_path = tmp_path / "legacy.db"
    mdir = tmp_path / "migrations"
    mdir.mkdir()
    (mdir / "0001_first.sql").write_text("CREATE TABLE t (id INTEGER PRIMARY KEY);")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE legacy (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    status = get_migration_status(db_path=db_path, migrations_dir=mdir)

    assert [(item["version"], item["applied"]) for item in status] == [
        ("0001_first", False)
    ]
    conn = sqlite3.connect(db_path)
    try:
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
            ).fetchone()
            is None
        )
    finally:
        conn.close()


def test_run_migrations_applies_pending_in_order_and_once(tmp_path: Path):
    """Pending migrations apply oldest-first and are recorded for idempotency."""
    mdir = tmp_path / "migrations"
    mdir.mkdir()
    (mdir / "0001_first.sql").write_text("CREATE TABLE t (id INTEGER PRIMARY KEY);")
    (mdir / "0002_second.sql").write_text("INSERT INTO t (id) VALUES (1);")

    conn = sqlite3.connect(tmp_path / "m.db")
    try:
        applied = run_migrations(conn, migrations_dir=mdir)
        assert applied == ["0001_first", "0002_second"]

        # Recorded in schema_migrations and idempotent on re-run.
        assert set(get_applied_migrations(conn)) == {"0001_first", "0002_second"}
        assert run_migrations(conn, migrations_dir=mdir) == []
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1
    finally:
        conn.close()


def test_run_migrations_preserves_semicolon_inside_sql_literal(tmp_path: Path):
    """Semicolons inside a quoted literal do not split a SQL migration."""
    mdir = tmp_path / "migrations"
    mdir.mkdir()
    (mdir / "0001_literal.sql").write_text(
        "CREATE TABLE sample (value TEXT);\nINSERT INTO sample VALUES ('alpha;beta');"
    )

    conn = sqlite3.connect(tmp_path / "literal.db")
    try:
        assert run_migrations(conn, migrations_dir=mdir) == ["0001_literal"]
        assert conn.execute("SELECT value FROM sample").fetchone()[0] == "alpha;beta"
    finally:
        conn.close()


def test_run_migrations_skips_initial_schema_for_untracked_existing_database(
    tmp_path: Path,
):
    """An existing tasks table is treated as the baseline before pending migrations."""
    mdir = tmp_path / "migrations"
    mdir.mkdir()
    (mdir / "0001_initial_schema.sql").write_text(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, repo_alias TEXT);"
    )
    (mdir / "0002_pending.sql").write_text(
        "CREATE TABLE migration_marker (id INTEGER PRIMARY KEY);"
    )

    conn = sqlite3.connect(tmp_path / "existing.db")
    try:
        conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY)")
        assert run_migrations(conn, migrations_dir=mdir) == ["0002_pending"]
        assert "0001_initial_schema" in get_applied_migrations(conn)
        assert "0002_pending" in get_applied_migrations(conn)
        assert run_migrations(conn, migrations_dir=mdir) == []
    finally:
        conn.close()


def test_migration_0001_initial_schema(tmp_path: Path):
    """Verify 0001 initial schema creates boards, board_repositories, tasks, and task_links with unified multi-repo structure."""
    from migrations import run_migrations

    conn = sqlite3.connect(tmp_path / "actual.db")
    try:
        applied = run_migrations(conn)
        assert "0001_initial_schema" in applied

        cur = conn.cursor()
        cur.execute("PRAGMA table_info(boards)")
        board_cols = {row[1] for row in cur.fetchall()}
        assert {
            "slug",
            "description",
            "architecture",
            "max_concurrent_running",
            "auto_record_memory",
            "automerge",
        }.issubset(board_cols)

        cur.execute("PRAGMA table_info(board_repositories)")
        repo_cols = {row[1] for row in cur.fetchall()}
        assert {
            "id",
            "board_slug",
            "repo_alias",
            "git_url",
            "target_branch",
            "additional_reviewer_usernames",
        }.issubset(repo_cols)

        cur.execute("PRAGMA table_info(tasks)")
        task_cols = {row[1] for row in cur.fetchall()}
        assert {
            "repo_alias",
            "worker_pid",
            "started_at",
            "blocked_reason",
        }.issubset(task_cols)

        cur.execute("PRAGMA table_info(task_links)")
        link_cols = {row[1] for row in cur.fetchall()}
        assert "link_type" in link_cols
    finally:
        conn.close()


def test_init_db_applies_pending_migration_to_existing_database(
    tmp_path: Path, monkeypatch
):
    """Normal initialization migrates populated databases and remains idempotent."""
    import dashboard.db as dashboard_db

    db_path = tmp_path / "existing.db"
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    source_migrations = Path(__file__).parents[2] / "migrations"
    (migrations_dir / "0001_initial_schema.sql").write_text(
        (source_migrations / "0001_initial_schema.sql").read_text()
    )
    conn = sqlite3.connect(db_path)
    try:
        from migrations import runner

        runner.run_migrations(conn, migrations_dir=migrations_dir)
        conn.commit()
    finally:
        conn.close()
    (migrations_dir / "0002_pending.sql").write_text(
        "CREATE TABLE migration_marker (id INTEGER PRIMARY KEY);"
    )

    monkeypatch.setenv("ZEROFACTORY_DB", str(db_path))
    dashboard_db._DB_INITIALIZED_PATHS.discard(db_path.resolve())
    monkeypatch.setattr(
        "migrations.run_migrations",
        lambda conn: runner.run_migrations(conn, migrations_dir=migrations_dir),
    )

    dashboard_db.init_db()
    dashboard_db.init_db(force=True)

    conn = sqlite3.connect(db_path)
    try:
        assert "0002_pending" in get_applied_migrations(conn)
        assert conn.execute("SELECT COUNT(*) FROM migration_marker").fetchone()[0] == 0
    finally:
        conn.close()
