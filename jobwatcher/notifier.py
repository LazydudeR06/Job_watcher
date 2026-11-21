import logging
from typing import Any, Sequence

import requests


LOGGER = logging.getLogger(__name__)
DISCORD_MESSAGE_LIMIT = 2000
DISCORD_JOBS_PER_MESSAGE = 10


def _shorten(value: object, limit: int) -> str:
    """Keep a field within its message budget."""
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _job_line(job: dict[str, Any]) -> str:
    """Format one scored job as a compact Discord message line."""
    score = job.get("score", job.get("llm_score", ""))
    title = _shorten(job.get("title", "Untitled role"), 400)
    company = _shorten(job.get("company", "Company not specified"), 300)
    url = _shorten(job.get("url", ""), 800)
    priority = job.get("apply_priority", "")
    return (
        f"{title} — {company} | Score: {score} | "
        f"apply_priority: {priority} | {url}"
    )


def send_notifications(
    scored_jobs: Sequence[dict[str, Any]],
    webhook_url: str,
    min_score: int = 75,
    max_jobs: int = DISCORD_JOBS_PER_MESSAGE,
) -> None:
    """Send qualifying scored jobs to a Discord webhook in bounded messages."""
    if not webhook_url:
        return
    if not isinstance(max_jobs, int) or isinstance(max_jobs, bool) or max_jobs < 1:
        raise ValueError("max_jobs must be a positive integer")

    batch_limit = min(max_jobs, DISCORD_JOBS_PER_MESSAGE)
    lines = []
    for job in scored_jobs:
        score = job.get("score", job.get("llm_score"))
        if score is None or score < min_score:
            continue
        lines.append(_job_line(job))

    messages: list[str] = []
    batch: list[str] = []
    for line in lines:
        candidate = "\n".join((*batch, line))
        if batch and (
            len(batch) >= batch_limit or len(candidate) > DISCORD_MESSAGE_LIMIT
        ):
            messages.append("\n".join(batch))
            batch = []
        batch.append(line[:DISCORD_MESSAGE_LIMIT])
    if batch:
        messages.append("\n".join(batch))

    for message in messages:
        try:
            response = requests.post(
                webhook_url,
                json={"content": message},
                timeout=10,
            )
            response.raise_for_status()
        except requests.RequestException:
            LOGGER.exception("Discord webhook request failed")
