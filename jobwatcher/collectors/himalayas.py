import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urldefrag

import requests

from ..models import Job
from .base import Collector


LOGGER = logging.getLogger(__name__)
EU_MEMBER_STATES = {
    "austria",
    "belgium",
    "bulgaria",
    "croatia",
    "cyprus",
    "czech republic",
    "czechia",
    "denmark",
    "estonia",
    "finland",
    "france",
    "germany",
    "greece",
    "hungary",
    "ireland",
    "italy",
    "latvia",
    "lithuania",
    "luxembourg",
    "malta",
    "netherlands",
    "poland",
    "portugal",
    "romania",
    "slovakia",
    "slovenia",
    "spain",
    "sweden",
}


class HimalayasCollector(Collector):
    """Collector for Himalayas' public jobs JSON feed."""

    name = "himalayas"

    def __init__(
        self,
        feed_url: str = "https://himalayas.app/jobs/api?limit=20",
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
        """Fetch Himalayas' public JSON feed and return normalized jobs."""
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
                LOGGER.warning("Himalayas request failed: %s", exc)
                return []

            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt == 0:
                    self.sleeper(1.0)
                    continue
                LOGGER.warning(
                    "Himalayas returned HTTP %s after retry",
                    response.status_code,
                )
                return []

            try:
                response.raise_for_status()
            except requests.HTTPError as exc:
                LOGGER.warning("Himalayas request failed: %s", exc)
                return []

            try:
                payload = response.json()
            except (requests.RequestException, ValueError) as exc:
                LOGGER.warning("Himalayas returned invalid JSON: %s", exc)
                return []
            return self.parse_payload(payload)

        return []

    @staticmethod
    def parse_payload(payload: Any) -> list[Job]:
        """Parse Himalayas' JSON object into normalized jobs."""
        if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
            LOGGER.warning("Himalayas payload has no jobs list")
            return []

        jobs: list[Job] = []
        seen_urls: set[str] = set()
        seen_ids: set[str] = set()
        for listing in payload["jobs"]:
            if not isinstance(listing, dict):
                continue
            job = HimalayasCollector.parse_listing(listing)
            if job is None or job.url in seen_urls or job.source_job_id in seen_ids:
                continue
            seen_urls.add(job.url)
            seen_ids.add(job.source_job_id)
            jobs.append(job)
        return jobs

    @staticmethod
    def parse_listing(listing: dict[str, Any]) -> Job | None:
        """Parse one Himalayas listing into the shared Job model."""
        title = listing.get("title")
        if not isinstance(title, str) or not title.strip():
            return None

        provider_url = listing.get("guid") or listing.get("applicationLink") or listing.get("url")
        if not isinstance(provider_url, str) or not provider_url.strip():
            return None
        url = urldefrag(provider_url.strip())[0]

        provider_id = listing.get("id") or listing.get("jobId") or listing.get("guid")
        if provider_id is None or not str(provider_id).strip():
            source_job_id = hashlib.sha256(url.encode("utf-8")).hexdigest()
        else:
            source_job_id = str(provider_id).strip()

        company = listing.get("companyName")
        if not isinstance(company, str) or not company.strip():
            company = None

        restrictions = listing.get("locationRestrictions")
        if isinstance(restrictions, list) and restrictions:
            locations = [
                str(item).strip()
                for item in restrictions
                if item is not None and str(item).strip()
            ]
            normalized = {item.casefold() for item in locations}
            if normalized == {"united states"} or normalized == {"united states of america"}:
                location = "Remote - US-only"
            elif normalized and normalized.issubset(EU_MEMBER_STATES):
                location = "Remote - EU-only"
            else:
                location = f"Remote - {', '.join(locations)}"
        else:
            location = "Remote worldwide"

        posted_value = listing.get("pubDate") or listing.get("postedAt")
        if isinstance(posted_value, (int, float)) and not isinstance(posted_value, bool):
            try:
                posted_at = datetime.fromtimestamp(
                    posted_value,
                    tz=timezone.utc,
                ).isoformat()
            except (OverflowError, OSError, ValueError):
                posted_at = None
        elif isinstance(posted_value, str) and posted_value.strip():
            posted_at = posted_value.strip()
        else:
            posted_at = None

        return Job(
            source="himalayas",
            source_job_id=source_job_id,
            title=title.strip(),
            company=company,
            location=location,
            url=url,
            posted_at=posted_at,
            raw_text=json.dumps(listing, ensure_ascii=False, sort_keys=True),
        )
