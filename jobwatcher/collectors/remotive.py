import hashlib
import json
import logging
import time
from typing import Any
from urllib.parse import urldefrag

import requests

from ..models import Job
from .base import Collector


LOGGER = logging.getLogger(__name__)


class RemotiveCollector(Collector):
    """Collector for Remotive's public remote jobs feed."""

    name = "remotive"

    def __init__(
        self,
        feed_url: str = "https://remotive.com/api/remote-jobs",
        session: requests.Session | None = None,
        timeout_seconds: float = 20.0,
        sleeper=time.sleep,
    ) -> None:
        self.feed_url = feed_url
        self.session = session or requests.Session()
        self.timeout_seconds = timeout_seconds
        self.sleeper = sleeper
        self._last_request_at: float | None = None

    def _wait_for_rate_limit(self) -> None:
        if self._last_request_at is not None:
            elapsed = time.monotonic() - self._last_request_at
            remaining = 1.0 - elapsed
            if remaining > 0:
                self.sleeper(remaining)
        self._last_request_at = time.monotonic()

    def fetch(self) -> list[Job]:
        """Fetch and parse the Remotive feed, returning an empty list on failure."""
        for attempt in range(2):
            self._wait_for_rate_limit()
            try:
                response = self.session.get(
                    self.feed_url,
                    timeout=self.timeout_seconds,
                    headers={"Accept": "application/json"},
                )
            except requests.RequestException as exc:
                LOGGER.warning("Remotive request failed: %s", exc)
                return []

            if response.status_code in (429, *range(500, 600)):
                if attempt == 0:
                    self.sleeper(1.0)
                    continue
                LOGGER.warning(
                    "Remotive returned HTTP %s after retry",
                    response.status_code,
                )
                return []

            try:
                response.raise_for_status()
            except requests.HTTPError as exc:
                LOGGER.warning("Remotive request failed: %s", exc)
                return []

            try:
                payload = response.json()
            except (requests.JSONDecodeError, ValueError) as exc:
                LOGGER.warning("Remotive returned invalid JSON: %s", exc)
                return []

            return self.parse_payload(payload)

        return []

    @staticmethod
    def parse_payload(payload: Any) -> list[Job]:
        """Parse a Remotive API payload into normalized jobs."""
        if not isinstance(payload, dict):
            LOGGER.warning("Remotive payload is not an object")
            return []

        listings = payload.get("jobs")
        if not isinstance(listings, list):
            LOGGER.warning("Remotive payload has no jobs list")
            return []

        jobs: list[Job] = []
        seen_urls: set[str] = set()
        seen_ids: set[str] = set()
        for listing in listings:
            if not isinstance(listing, dict):
                continue
            job = RemotiveCollector.parse_listing(listing)
            if job is None:
                continue
            if job.url in seen_urls or job.source_job_id in seen_ids:
                continue
            seen_urls.add(job.url)
            seen_ids.add(job.source_job_id)
            jobs.append(job)
        return jobs

    @staticmethod
    def parse_listing(listing: dict[str, Any]) -> Job | None:
        """Parse one Remotive listing, with provider-ID then URL fallback."""
        title = listing.get("title")
        url = listing.get("url")
        if not isinstance(title, str) or not title.strip():
            return None
        if not isinstance(url, str) or not url.strip():
            return None

        url = urldefrag(url.strip())[0].rstrip("/")
        provider_id = listing.get("id")
        if provider_id is not None:
            source_job_id = str(provider_id).strip()
        else:
            source_job_id = hashlib.sha256(url.encode("utf-8")).hexdigest()

        company = listing.get("company_name")
        location = listing.get("candidate_required_location")
        if not isinstance(company, str) or not company.strip():
            company = None
        if not isinstance(location, str) or not location.strip():
            location = None

        posted_at = listing.get("publication_date")
        if not isinstance(posted_at, str) or not posted_at.strip():
            posted_at = None

        raw_text = json.dumps(listing, ensure_ascii=False, sort_keys=True)
        return Job(
            source="remotive",
            source_job_id=source_job_id,
            title=title.strip(),
            company=company,
            location=location,
            url=url,
            posted_at=posted_at,
            raw_text=raw_text,
        )
