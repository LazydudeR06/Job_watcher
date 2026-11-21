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


class AdzunaCollector(Collector):
    """Collector for Adzuna's public jobs API."""

    name = "adzuna"

    def __init__(
        self,
        app_id: str | None,
        app_key: str | None,
        country: str = "in",
        results_per_page: int = 50,
        api_url: str = "https://api.adzuna.com/v1/api/jobs",
        session: requests.Session | None = None,
        timeout_seconds: float = 20.0,
        sleeper=time.sleep,
    ) -> None:
        self.app_id = app_id
        self.app_key = app_key
        self.country = country.lower()
        self.results_per_page = results_per_page
        self.api_url = api_url.rstrip("/")
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
        """Fetch public Adzuna listings or return an empty list on source failure."""
        if not self.app_id or not self.app_key:
            LOGGER.info("Adzuna is disabled because its API credentials are unavailable")
            return []

        url = f"{self.api_url}/{self.country}/search/1"
        params = {
            "app_id": self.app_id,
            "app_key": self.app_key,
            "results_per_page": self.results_per_page,
            "content-type": "application/json",
        }
        for attempt in range(2):
            self._wait_for_rate_limit()
            try:
                response = self.session.get(
                    url,
                    params=params,
                    timeout=self.timeout_seconds,
                    headers={"Accept": "application/json"},
                )
            except requests.RequestException as exc:
                LOGGER.warning(
                    "Adzuna request failed (%s)",
                    type(exc).__name__,
                )
                return []

            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt == 0:
                    self.sleeper(1.0)
                    continue
                LOGGER.warning(
                    "Adzuna returned HTTP %s after retry",
                    response.status_code,
                )
                return []

            try:
                response.raise_for_status()
            except requests.HTTPError:
                LOGGER.warning("Adzuna request failed with HTTP %s", response.status_code)
                return []

            try:
                payload = response.json()
            except (requests.RequestException, ValueError) as exc:
                LOGGER.warning("Adzuna returned invalid JSON (%s)", type(exc).__name__)
                return []
            return self.parse_payload(payload)

        return []

    @staticmethod
    def parse_payload(payload: Any) -> list[Job]:
        """Parse Adzuna's API response into normalized jobs."""
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            LOGGER.warning("Adzuna payload has no results list")
            return []

        jobs: list[Job] = []
        seen_urls: set[str] = set()
        seen_ids: set[str] = set()
        for listing in payload["results"]:
            if not isinstance(listing, dict):
                continue
            job = AdzunaCollector.parse_listing(listing)
            if job is None or job.url in seen_urls or job.source_job_id in seen_ids:
                continue
            seen_urls.add(job.url)
            seen_ids.add(job.source_job_id)
            jobs.append(job)
        return jobs

    @staticmethod
    def parse_listing(listing: dict[str, Any]) -> Job | None:
        """Parse one Adzuna result into the shared Job model."""
        title = listing.get("title")
        url = listing.get("redirect_url") or listing.get("url")
        if not isinstance(title, str) or not title.strip():
            return None
        if not isinstance(url, str) or not url.strip():
            return None

        normalized_url = urldefrag(url.strip())[0]
        provider_id = listing.get("id")
        if provider_id is None or not str(provider_id).strip():
            source_job_id = hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()
        else:
            source_job_id = str(provider_id).strip()

        company_info = listing.get("company")
        company = company_info.get("display_name") if isinstance(company_info, dict) else None
        location_info = listing.get("location")
        location = (
            location_info.get("display_name")
            if isinstance(location_info, dict)
            else None
        )
        posted_at = listing.get("created")
        if not isinstance(posted_at, str) or not posted_at.strip():
            posted_at = None

        return Job(
            source="adzuna",
            source_job_id=source_job_id,
            title=title.strip(),
            company=company.strip() if isinstance(company, str) and company.strip() else None,
            location=location.strip() if isinstance(location, str) and location.strip() else None,
            url=normalized_url,
            posted_at=posted_at,
            raw_text=json.dumps(listing, ensure_ascii=False, sort_keys=True),
        )
