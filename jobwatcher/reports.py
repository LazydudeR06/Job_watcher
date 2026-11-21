import csv
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence
from zoneinfo import ZoneInfo

from .models import Job
from .storage import get_scored_jobs


LOGGER = logging.getLogger(__name__)
TIMEZONE = ZoneInfo("Asia/Kolkata")
DAILY_CSV_FIELDS = ("title", "company", "location", "score", "reasons", "url")
LEGACY_HOURLY_CSV_FIELDS = (
    "source",
    "source_job_id",
    "title",
    "company",
    "location",
    "url",
    "posted_at",
    "llm_score",
    "score_rationale",
    "scored_at",
)


def _kolkata_time(value: datetime | None) -> datetime:
    """Return a timestamp in the report timezone."""
    if value is None:
        return datetime.now(TIMEZONE)
    if value.tzinfo is None:
        return value.replace(tzinfo=TIMEZONE)
    return value.astimezone(TIMEZONE)


def _escape_markdown(value: object) -> str:
    """Escape Markdown table content from feed and model text."""
    text = str(value or "")
    for character in "\\`*_{}[]<>|":
        text = text.replace(character, f"\\{character}")
    return text.replace("\r", " ").replace("\n", " ")


def _score(job: dict[str, Any]) -> float:
    """Read the score from either storage rows or report fixtures."""
    return float(job.get("llm_score", job.get("score", 0)) or 0)


def _reasons(job: dict[str, Any]) -> str:
    """Render structured reasons, falling back to legacy rationale."""
    reasons = job.get("reasons")
    if isinstance(reasons, list):
        return "; ".join(str(reason) for reason in reasons)
    if isinstance(reasons, str) and reasons:
        return reasons
    return str(job.get("score_rationale") or "")


def _unique_jobs(jobs: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the first row for each URL, falling back to source ID."""
    unique: dict[str, dict[str, Any]] = {}
    for job in jobs:
        key = str(
            job.get("url")
            or f"{job.get('source', '')}:{job.get('source_job_id', '')}"
        )
        unique.setdefault(key, job)
    return list(unique.values())


def _write_markdown_table(
    jobs: list[dict[str, Any]],
    target: Path,
    title: str,
) -> Path | None:
    """Write a Markdown report table, or do nothing when there are no jobs."""
    if not jobs:
        return None

    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# {title}",
        "",
        "| Title | Company | Location | Score | Reasons | URL |",
        "| --- | --- | --- | ---: | --- | --- |",
    ]
    for job in jobs:
        score = _score(job)
        lines.append(
            "| "
            + " | ".join(
                (
                    _escape_markdown(job.get("title") or "Untitled role"),
                    _escape_markdown(job.get("company") or "Company not specified"),
                    _escape_markdown(job.get("location") or "Location not specified"),
                    f"{score:g}/100",
                    _escape_markdown(_reasons(job)),
                    _escape_markdown(job.get("url") or ""),
                )
            )
            + " |"
        )
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    LOGGER.info("Wrote report with %s jobs to %s", len(jobs), target)
    return target


def write_daily_digest(
    jobs: list[dict[str, Any]],
    output_dir: str | Path = "output/digests",
    report_date: datetime | None = None,
) -> Path | None:
    """Write a legacy-format daily digest for compatibility with existing callers."""
    if not jobs:
        return None

    date_value = _kolkata_time(report_date)
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{date_value:%Y-%m-%d}.md"

    lines = [
        f"# Job Watcher Digest — {date_value:%Y-%m-%d}",
        "",
        f"{len(jobs)} scored job(s), sorted by match score.",
        "",
    ]
    for job in jobs:
        company = _escape_markdown(job.get("company") or "Company not specified")
        location = _escape_markdown(job.get("location") or "Location not specified")
        title = _escape_markdown(job.get("title") or "Untitled role")
        rationale = _escape_markdown(_reasons(job))
        url = str(job.get("url") or "").replace("`", "")
        lines.extend(
            (
                f"## {title}",
                "",
                f"- **Match score:** {_score(job):g}/100",
                f"- **Company:** {company}",
                f"- **Location:** {location}",
                f"- **Rationale:** {rationale}",
                f"- **URL:** `{url}`",
                "",
            )
        )

    target.write_text("\n".join(lines), encoding="utf-8")
    LOGGER.info("Wrote legacy digest with %s jobs to %s", len(jobs), target)
    return target


def write_daily_csv(
    jobs: list[dict[str, Any]],
    output_dir: str | Path = "output",
    report_date: datetime | None = None,
) -> Path | None:
    """Write the daily CSV using the same qualified jobs as the digest."""
    if not jobs:
        return None

    date_value = _kolkata_time(report_date)
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{date_value:%Y-%m-%d}.csv"
    temporary = target.with_suffix(".csv.tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=DAILY_CSV_FIELDS)
        writer.writeheader()
        for job in jobs:
            writer.writerow(
                {
                    "title": job.get("title") or "",
                    "company": job.get("company") or "",
                    "location": job.get("location") or "",
                    "score": f"{_score(job):g}",
                    "reasons": _reasons(job),
                    "url": job.get("url") or "",
                }
            )
    temporary.replace(target)
    LOGGER.info("Wrote daily CSV with %s jobs to %s", len(jobs), target)
    return target


def write_hourly_markdown(
    jobs: list[dict[str, Any]],
    output_dir: str | Path = "output/hourly",
    report_time: datetime | None = None,
) -> Path | None:
    """Write a Markdown report for jobs newly scored in this run."""
    if not jobs:
        return None

    time_value = _kolkata_time(report_time)
    target = Path(output_dir) / f"{time_value:%Y-%m-%d_%H}.md"
    return _write_markdown_table(
        jobs,
        target,
        f"New jobs — {time_value:%Y-%m-%d %H:%M} Asia/Kolkata",
    )


def write_hourly_csv(
    jobs: list[dict[str, Any]],
    output_dir: str | Path = "output/hourly",
    report_time: datetime | None = None,
) -> Path | None:
    """Write the legacy hourly CSV when called directly by older integrations."""
    if not jobs:
        return None

    time_value = _kolkata_time(report_time)
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{time_value:%Y-%m-%d-%H}.csv"
    temporary = target.with_suffix(".csv.tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=LEGACY_HOURLY_CSV_FIELDS,
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(jobs)
    temporary.replace(target)
    LOGGER.info("Wrote legacy hourly CSV with %s jobs to %s", len(jobs), target)
    return target


def _job_url(job: Job | dict[str, Any]) -> str:
    """Get the URL from a job model or a scored report row."""
    if isinstance(job, dict):
        return str(job.get("url") or "")
    return job.url


def generate_reports(
    connection: Any,
    output_root: str | Path = "output",
    now: datetime | None = None,
    new_jobs: Sequence[Job | dict[str, Any]] | None = None,
    digest_min_score: int = 60,
    notification_min_score: int = 75,
) -> list[Path]:
    """Generate daily reports and a current-run hourly Markdown report."""
    current_time = _kolkata_time(now)
    daily_jobs = _unique_jobs(
        get_scored_jobs(connection, current_time.strftime("%Y-%m-%d"))
    )
    digest_jobs = [
        job for job in daily_jobs if _score(job) >= digest_min_score
    ]

    root = Path(output_root)
    generated: list[Path] = []
    digest_path = _write_markdown_table(
        digest_jobs,
        root / f"{current_time:%Y-%m-%d}.md",
        f"Job Watcher Digest — {current_time:%Y-%m-%d}",
    )
    csv_path = write_daily_csv(digest_jobs, root, current_time)
    if digest_path is not None:
        generated.append(digest_path)
        write_daily_digest(digest_jobs, root / "digests", current_time)
    if csv_path is not None:
        generated.append(csv_path)

    new_urls = {_job_url(job) for job in (new_jobs or ())}
    hourly_jobs = [
        job
        for job in daily_jobs
        if job.get("url") in new_urls and _score(job) >= notification_min_score
    ]
    hourly_path = write_hourly_markdown(
        hourly_jobs,
        root / "hourly",
        current_time,
    )
    if hourly_path is not None:
        generated.append(hourly_path)
    return generated
