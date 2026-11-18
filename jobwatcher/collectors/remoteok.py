import hashlib
import json
import logging
import time
from typing import Any
from urllib.parse import urldefrag, urljoin

import requests

from ..models import Job
from .base import Collector


LOGGER = logging.getLogger(__name__)


class RemoteOKCollector(Collector):
    """Collector for RemoteOK's public JSON feed."""

    name = "remoteok"

    def __init__(
        self,
        feed_url: str = "https://remoteok.com/api",
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
        """Enforce at least one second between requests from this collector."""
        if self._last_request_at is not None:
            elapsed = time.monotonic() - self._last_request_at
            remaining = 1.0 - elapsed
            if remaining > 0:
                self.sleeper(remaining)
        self._last_request_at = time.monotonic()

    def fetch(self) -> list[Job]:
        """Fetch RemoteOK's public JSON feed; log failures and return no jobs."""
        for attempt in range(2):
            self._wait_for_rate_limit()
            try:
                response = self.session.get(
                    self.feed_url,
                    timeout=self.timeout_seconds,
                    headers={
                        "Accept": "application/json",
                        "User-Agent": "LocalJobWatcher/1.0",
                    },
                )
            except requests.RequestException as exc:
                LOGGER.warning("RemoteOK request failed: %s", exc)
                return []

            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt == 0:
                    self.sleeper(1.0)
                    continue
                LOGGER.warning(
                    "RemoteOK returned HTTP %s after retry",
                    response.status_code,
                )
                return []

            try:
                response.raise_for_status()
            except requests.HTTPError as exc:
                LOGGER.warning("RemoteOK request failed: %s", exc)
                return []

            try:
                payload = response.json()
            except (requests.RequestException, ValueError) as exc:
                LOGGER.warning("RemoteOK returned invalid JSON: %s", exc)
                return []
            return self.parse_payload(payload)

        return []

    @staticmethod
    def parse_payload(payload: Any) -> list[Job]:
        """Parse RemoteOK's JSON array into normalized jobs."""
        if not isinstance(payload, list):
            LOGGER.warning("RemoteOK payload is not a JSON array")
            return []

        jobs: list[Job] = []
        seen_urls: set[str] = set()
        seen_ids: set[str] = set()
        for listing in payload:
            if not isinstance(listing, dict):
                continue
            job = RemoteOKCollector.parse_listing(listing)
            if job is None or job.url in seen_urls or job.source_job_id in seen_ids:
                continue
            seen_urls.add(job.url)
            seen_ids.add(job.source_job_id)
            jobs.append(job)
        return jobs

    @staticmethod
    def parse_listing(listing: dict[str, Any]) -> Job | None:
        """Parse one RemoteOK listing, falling back from ID to slug or URL."""
        title = listing.get("position") or listing.get("title")
        if not isinstance(title, str) or not title.strip():
            return None

        slug = listing.get("slug")
        raw_url = listing.get("url") or listing.get("apply_url")
        if not isinstance(raw_url, str) or not raw_url.strip():
            if not isinstance(slug, str) or not slug.strip():
                return None
            raw_url = f"https://remoteok.com/remote-jobs/{slug.strip()}"
        url = urldefrag(urljoin("https://remoteok.com/", raw_url.strip()))[0].rstrip("/")

        provider_id = listing.get("id")
        if provider_id is not None and str(provider_id).strip():
            source_job_id = str(provider_id).strip()
        elif isinstance(slug, str) and slug.strip():
            source_job_id = slug.strip()
        else:
            source_job_id = hashlib.sha256(url.encode("utf-8")).hexdigest()

        company = listing.get("company") or listing.get("company_name")
        if not isinstance(company, str) or not company.strip():
            company = None

        location = listing.get("location")
        if not isinstance(location, str):
            locations = listing.get("locations")
            if isinstance(locations, list):
                location = ", ".join(str(item) for item in locations if item)
        if not isinstance(location, str) or not location.strip():
            location = None

        posted_at = listing.get("date") or listing.get("created_at")
        if not isinstance(posted_at, str) or not posted_at.strip():
            posted_at = None

        return Job(
            source="remoteok",
            source_job_id=source_job_id,
            title=title.strip(),
            company=company,
            location=location,
            url=url,
            posted_at=posted_at,
            raw_text=json.dumps(listing, ensure_ascii=False, sort_keys=True),
        )
