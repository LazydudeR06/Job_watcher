import csv
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from .storage import get_scored_jobs


LOGGER = logging.getLogger(__name__)
CSV_FIELDS = (
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


def _escape_markdown(value: object) -> str:
    """Escape Markdown formatting characters from feed and model content."""
    text = str(value or "")
    for character in "\\`*_{}[]<>":
        text = text.replace(character, f"\\{character}")
    return text.replace("\r", " ").replace("\n", " ")


def write_daily_digest(
    jobs: list[dict[str, Any]],
    output_dir: str | Path = "output/digests",
    report_date: datetime | None = None,
) -> Path | None:
    """Write or refresh today's Markdown digest for scored surviving jobs."""
    if not jobs:
        return None

    date_value = report_date or datetime.now().astimezone()
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
        rationale = _escape_markdown(job.get("score_rationale") or "")
        url = str(job.get("url") or "").replace("`", "")
        lines.extend(
            (
                f"## {title}",
                "",
                f"- **Match score:** {job.get('llm_score', '')}/100",
                f"- **Company:** {company}",
                f"- **Location:** {location}",
                f"- **Rationale:** {rationale}",
                f"- **URL:** `{url}`",
                "",
            )
        )

    target.write_text("\n".join(lines), encoding="utf-8")
    LOGGER.info("Wrote daily digest with %s jobs to %s", len(jobs), target)
    return target


def write_hourly_csv(
    jobs: list[dict[str, Any]],
    output_dir: str | Path = "output/hourly",
    report_time: datetime | None = None,
) -> Path | None:
    """Write or refresh this hour's CSV of scored surviving jobs."""
    if not jobs:
        return None

    time_value = report_time or datetime.now().astimezone()
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{time_value:%Y-%m-%d-%H}.csv"
    temporary = target.with_suffix(".csv.tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(jobs)
    temporary.replace(target)
    LOGGER.info("Wrote hourly CSV with %s jobs to %s", len(jobs), target)
    return target


def generate_reports(
    connection: Any,
    output_root: str | Path = "output",
    now: datetime | None = None,
) -> list[Path]:
    """Generate today's digest and this hour's CSV from scored jobs."""
    current_time = now or datetime.now().astimezone()
    prefix_date = current_time.strftime("%Y-%m-%d")
    prefix_hour = current_time.strftime("%Y-%m-%dT%H")
    daily_jobs = get_scored_jobs(connection, prefix_date)
    hourly_jobs = get_scored_jobs(connection, prefix_hour)

    root = Path(output_root)
    generated: list[Path] = []
    digest_path = write_daily_digest(daily_jobs, root / "digests", current_time)
    csv_path = write_hourly_csv(hourly_jobs, root / "hourly", current_time)
    if digest_path is not None:
        generated.append(digest_path)
    if csv_path is not None:
        generated.append(csv_path)
    return generated
