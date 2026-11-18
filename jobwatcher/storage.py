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
            source TEXT NOT NULL CHECK (source IN ('remotive', 'remoteok', 'himalayas')),
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
    ):
        try:
            connection.execute(statement)
        except sqlite3.OperationalError:
            pass
    connection.commit()
    return connection


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
    survives: bool,
    similarity: float,
    reason: str,
) -> None:
    """Persist processing and filter decision fields for a collected job."""
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


def update_job_score(
    connection: sqlite3.Connection,
    job: "Job",
    score: int,
    rationale: str,
    score_json: str,
) -> None:
    """Persist a validated LLM score and its rationale."""
    connection.execute(
        """
        UPDATE jobs
        SET llm_score = ?, score_rationale = ?, score_json = ?, scored_at = ?
        WHERE source = ? AND url = ? AND survives = 1
        """,
        (
            score,
            rationale,
            score_json,
            datetime.now().astimezone().isoformat(timespec="seconds"),
            job.source,
            job.url,
        ),
    )
    connection.commit()


def get_pending_scores(connection: sqlite3.Connection) -> list["Job"]:
    """Load surviving jobs that have not yet received a valid LLM score."""
    from .models import Job

    rows = connection.execute(
        """
        SELECT source, source_job_id, title, company, location, url, posted_at, raw_text
        FROM jobs
        WHERE survives = 1 AND llm_score IS NULL
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
               llm_score, score_rationale, scored_at
        FROM jobs
        WHERE survives = 1 AND llm_score IS NOT NULL AND scored_at LIKE ?
        ORDER BY llm_score DESC, title COLLATE NOCASE
        """,
        (f"{timestamp_prefix}%",),
    )
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]
