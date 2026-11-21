import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any


def initialize_storage(database_path: str | Path = "data/jobs.db") -> sqlite3.Connection:
    """Create the jobs database and return an open SQLite connection."""
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL CHECK (source IN ('remotive', 'remoteok', 'himalayas', 'adzuna')),
            source_job_id TEXT NOT NULL,
            title TEXT NOT NULL,
            company TEXT,
            location TEXT,
            url TEXT NOT NULL,
            posted_at TEXT,
            raw_text TEXT NOT NULL,
            UNIQUE(source, source_job_id)
        )
        """
    )
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS jobs_url_unique ON jobs(url)"
    )
    for statement in (
        "ALTER TABLE jobs ADD COLUMN processed INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE jobs ADD COLUMN survives INTEGER",
        "ALTER TABLE jobs ADD COLUMN similarity REAL",
        "ALTER TABLE jobs ADD COLUMN filter_reason TEXT",
        "ALTER TABLE jobs ADD COLUMN llm_score INTEGER",
        "ALTER TABLE jobs ADD COLUMN score_rationale TEXT",
        "ALTER TABLE jobs ADD COLUMN score_json TEXT",
        "ALTER TABLE jobs ADD COLUMN scored_at TEXT",
        "ALTER TABLE jobs ADD COLUMN score_status TEXT",
        "ALTER TABLE jobs ADD COLUMN reasons TEXT",
        "ALTER TABLE jobs ADD COLUMN missing_skills TEXT",
        "ALTER TABLE jobs ADD COLUMN apply_priority INTEGER",
    ):
        try:
            connection.execute(statement)
        except sqlite3.OperationalError:
            pass
    table_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'jobs'"
    ).fetchone()
    table_sql = table_row[0].lower() if table_row and table_row[0] else ""
    if "check" in table_sql and "'adzuna'" not in table_sql:
        _migrate_jobs_source_constraint(connection)
    connection.commit()
    return connection


def _migrate_jobs_source_constraint(connection: sqlite3.Connection) -> None:
    """Rebuild older jobs tables so Adzuna records pass the source constraint."""
    columns = [
        row[1]
        for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
    ]
    connection.execute("ALTER TABLE jobs RENAME TO jobs_before_adzuna")
    connection.execute(
        """
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL CHECK (source IN ('remotive', 'remoteok', 'himalayas', 'adzuna')),
            source_job_id TEXT NOT NULL,
            title TEXT NOT NULL,
            company TEXT,
            location TEXT,
            url TEXT NOT NULL,
            posted_at TEXT,
            raw_text TEXT NOT NULL,
            processed INTEGER NOT NULL DEFAULT 0,
            survives INTEGER,
            similarity REAL,
            filter_reason TEXT,
            llm_score INTEGER,
            score_rationale TEXT,
            score_json TEXT,
            scored_at TEXT,
            score_status TEXT,
            reasons TEXT,
            missing_skills TEXT,
            apply_priority INTEGER,
            UNIQUE(source, source_job_id)
        )
        """
    )
    new_columns = [
        row[1]
        for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
    ]
    shared_columns = [column for column in columns if column in new_columns]
    column_sql = ", ".join(f'"{column}"' for column in shared_columns)
    connection.execute(
        f"INSERT INTO jobs ({column_sql}) SELECT {column_sql} FROM jobs_before_adzuna"
    )
    connection.execute("DROP TABLE jobs_before_adzuna")
    connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS jobs_url_unique ON jobs(url)")


def insert_jobs(connection: sqlite3.Connection, jobs: list["Job"]) -> list["Job"]:
    """Insert only new jobs, deduplicated by URL or source job ID."""
    from .models import Job

    inserted: list[Job] = []
    for job in jobs:
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO jobs
                (source, source_job_id, title, company, location, url, posted_at, raw_text)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job.source,
                job.source_job_id,
                job.title,
                job.company,
                job.location,
                job.url,
                job.posted_at,
                job.raw_text,
            ),
        )
        if cursor.rowcount == 1:
            inserted.append(job)
    connection.commit()
    return inserted


def update_filter_decision(
    connection: sqlite3.Connection,
    job: "Job",
    survives: bool | None,
    similarity: float,
    reason: str,
) -> None:
    """Persist a completed filter decision; leave transient failures pending."""
    if survives is None:
        return

    connection.execute(
        """
        UPDATE jobs
        SET processed = 1,
            survives = ?,
            similarity = ?,
            filter_reason = ?
        WHERE source = ? AND url = ?
        """,
        (
            1 if survives else 0,
            similarity,
            reason,
            job.source,
            job.url,
        ),
    )
    connection.commit()


def get_pending_filters(connection: sqlite3.Connection) -> list["Job"]:
    """Load unprocessed jobs that still need a filter decision."""
    from .models import Job

    rows = connection.execute(
        """
        SELECT source, source_job_id, title, company, location, url, posted_at, raw_text
        FROM jobs
        WHERE processed = 0 AND survives IS NULL
        ORDER BY id
        """
    ).fetchall()
    return [Job(*row) for row in rows]


def update_job_score(
    connection: sqlite3.Connection,
    job: "Job",
    score: int,
    rationale: str,
    score_json: str,
    reasons: list[str] | None = None,
    missing_skills: list[str] | None = None,
    apply_priority: int | None = None,
) -> None:
    """Persist a validated LLM score and its structured assessment."""
    encoded_reasons = json.dumps(reasons or [], ensure_ascii=False)
    encoded_missing_skills = json.dumps(missing_skills or [], ensure_ascii=False)
    connection.execute(
        """
        UPDATE jobs
        SET llm_score = ?, score_rationale = ?, score_json = ?, scored_at = ?,
            score_status = 'scored', reasons = ?, missing_skills = ?, apply_priority = ?
        WHERE source = ? AND url = ? AND survives = 1
        """,
        (
            score,
            rationale,
            score_json,
            datetime.now().astimezone().isoformat(timespec="seconds"),
            encoded_reasons,
            encoded_missing_skills,
            apply_priority,
            job.source,
            job.url,
        ),
    )
    connection.commit()


def mark_score_gave_up(connection: sqlite3.Connection, job: "Job") -> None:
    """Mark a surviving job as terminal after scoring returns no result."""
    connection.execute(
        """
        UPDATE jobs
        SET score_status = 'gave_up'
        WHERE source = ? AND url = ? AND survives = 1 AND llm_score IS NULL
        """,
        (job.source, job.url),
    )
    connection.commit()


def get_pending_scores(connection: sqlite3.Connection) -> list["Job"]:
    """Load surviving jobs that have not yet received a score or terminal status."""
    from .models import Job

    rows = connection.execute(
        """
        SELECT source, source_job_id, title, company, location, url, posted_at, raw_text
        FROM jobs
        WHERE survives = 1 AND llm_score IS NULL AND score_status IS NULL
        ORDER BY id
        """
    ).fetchall()
    return [Job(*row) for row in rows]


def get_scored_jobs(
    connection: sqlite3.Connection,
    timestamp_prefix: str,
) -> list[dict[str, Any]]:
    """Load scored surviving jobs whose local scoring timestamp has a given prefix."""
    cursor = connection.execute(
        """
        SELECT source, source_job_id, title, company, location, url, posted_at,
               llm_score, score_rationale, scored_at, reasons, missing_skills,
               apply_priority
        FROM jobs
        WHERE survives = 1 AND llm_score IS NOT NULL AND scored_at LIKE ?
        ORDER BY llm_score DESC, title COLLATE NOCASE
        """,
        (f"{timestamp_prefix}%",),
    )
    columns = [column[0] for column in cursor.description]
    jobs = [dict(zip(columns, row)) for row in cursor.fetchall()]
    for job in jobs:
        job["reasons"] = json.loads(job["reasons"]) if job["reasons"] else []
        job["missing_skills"] = (
            json.loads(job["missing_skills"]) if job["missing_skills"] else []
        )
    return jobs
