import imaplib
import logging
import re
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from email import policy
from email.message import Message
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any

from ..models import Job
from ..storage import is_email_processed, mark_email_processed
from .base import Collector
from .linkedin_alerts import (
    DEFAULT_SENDER,
    LinkedInAlertEmailCollector as _AlertParser,
    _extract_bodies,
)


LOGGER = logging.getLogger(__name__)
_JOB_URL_RE = re.compile(
    r"https?://(?:www\.)?linkedin\.com/jobs/view/([^/?#\s]+)",
    re.IGNORECASE,
)
_JOB_PATH_RE = re.compile(r"/jobs/view/([^/?#]+)", re.IGNORECASE)


class _JobRowTextParser(HTMLParser):
    """Extract the visible text of each job-card table row."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[tuple[tuple[str, ...], str]] = []
        self._hrefs: list[str] | None = None
        self._text: list[str] | None = None
        self._anchor_href: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        if tag == "tr":
            self._finish_row()
            self._hrefs = []
            self._text = []
        elif tag == "a" and self._hrefs is not None:
            if self._text is not None:
                self._text.append(" ")
            self._anchor_href = attributes.get("href", "")
        elif tag in ("p", "td") and self._text is not None:
            self._text.append(" ")
        elif tag == "br" and self._text is not None:
            self._text.append(" ")

    def handle_data(self, data: str) -> None:
        if self._text is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._anchor_href is not None:
            if self._hrefs is not None:
                self._hrefs.append(self._anchor_href)
            self._anchor_href = None
            if self._text is not None:
                self._text.append(" ")
        elif tag in ("p", "td") and self._text is not None:
            self._text.append(" ")
        elif tag == "tr":
            self._finish_row()

    def close(self) -> None:
        super().close()
        self._finish_row()

    def _finish_row(self) -> None:
        if self._hrefs is None or self._text is None:
            return
        text = " ".join("".join(self._text).split())
        self.rows.append((tuple(self._hrefs), text))
        self._hrefs = None
        self._text = None


class LinkedInEmailCollector(Collector):
    """Read LinkedIn job-alert messages from a Gmail mailbox over read-only IMAP."""

    name = "linkedin_email"

    def __init__(
        self,
        enabled: bool = False,
        imap_host: str = "imap.gmail.com",
        folder: str = "INBOX",
        sender_filter: str = DEFAULT_SENDER,
        max_emails_per_run: int = 50,
        lookback_days: int = 3,
        imap_user: str = "",
        imap_app_password: str = "",
        connection: sqlite3.Connection | None = None,
        imap_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self.enabled = enabled
        self.imap_host = imap_host
        self.folder = folder
        self.sender_filter = sender_filter
        self.max_emails_per_run = max_emails_per_run
        self.lookback_days = lookback_days
        self.imap_user = imap_user
        self.imap_app_password = imap_app_password
        self.connection = connection
        self.imap_factory = imap_factory
        self._seen_message_ids: set[str] = set()

    def fetch(self) -> list[Job]:
        """Fetch new matching emails without modifying mailbox state."""
        if not self.enabled or not self.imap_user or not self.imap_app_password:
            return []

        client = None
        jobs: list[Job] = []
        try:
            factory = self.imap_factory or imaplib.IMAP4_SSL
            client = factory(self.imap_host)
            status, response = client.login(self.imap_user, self.imap_app_password)
            if not _imap_ok(status):
                LOGGER.warning("LinkedIn IMAP login failed: %s", _response_text(response))
                return []

            status, response = client.select(self.folder, readonly=True)
            if not _imap_ok(status):
                LOGGER.warning(
                    "Could not open LinkedIn IMAP folder %s read-only: %s",
                    self.folder,
                    _response_text(response),
                )
                return []

            cutoff = datetime.now(timezone.utc).date() - timedelta(
                days=self.lookback_days
            )
            status, response = client.search(
                None,
                "SINCE",
                cutoff.strftime("%d-%b-%Y"),
                "FROM",
                self.sender_filter,
            )
            if not _imap_ok(status):
                LOGGER.warning(
                    "LinkedIn IMAP search failed: %s",
                    _response_text(response),
                )
                return []

            message_numbers = response[0].split() if response else []
            for message_number in message_numbers[-self.max_emails_per_run :]:
                try:
                    status, payload = client.fetch(
                        message_number,
                        "(BODY.PEEK[])",
                    )
                except (imaplib.IMAP4.error, OSError) as exc:
                    LOGGER.warning(
                        "Could not fetch LinkedIn alert email %s: %s",
                        message_number,
                        exc,
                    )
                    continue
                if not _imap_ok(status):
                    LOGGER.warning(
                        "Could not fetch LinkedIn alert email %s: %s",
                        message_number,
                        _response_text(payload),
                    )
                    continue
                raw_message = _raw_message(payload)
                if raw_message is None:
                    LOGGER.warning(
                        "LinkedIn alert email %s had no message body",
                        message_number,
                    )
                    continue

                message_id = ""
                try:
                    parsed_message = _parse_mime_message(raw_message)
                    message_id = _message_id(parsed_message)
                    if not message_id:
                        raise ValueError("email is missing a Message-ID header")
                    if (
                        message_id in self._seen_message_ids
                        or (
                            self.connection is not None
                            and is_email_processed(
                                self.connection,
                                message_id,
                                source=self.name,
                            )
                        )
                    ):
                        continue

                    parsed_jobs = self._jobs_from_message(parsed_message)
                    if not parsed_jobs:
                        raise ValueError("no LinkedIn job cards were found")
                    if self.connection is not None:
                        mark_email_processed(
                            self.connection,
                            message_id,
                            source=self.name,
                        )
                    self._seen_message_ids.add(message_id)
                    jobs.extend(parsed_jobs)
                except Exception as exc:
                    LOGGER.warning(
                        "Could not parse LinkedIn alert email %s: %s",
                        message_id or message_number,
                        exc,
                    )
        except (imaplib.IMAP4.error, OSError) as exc:
            LOGGER.warning("LinkedIn IMAP connection failed: %s", exc)
        finally:
            if client is not None:
                try:
                    client.logout()
                except (imaplib.IMAP4.error, OSError):
                    LOGGER.debug("LinkedIn IMAP logout failed", exc_info=True)
        return _deduplicate_jobs(jobs)

    def parse_email(self, raw_email: bytes | str) -> list[Job]:
        """Parse one alert email into normalized job records."""
        message = _parse_mime_message(raw_email)
        message_id = _message_id(message)
        if not message_id:
            raise ValueError("email is missing a Message-ID header")
        if message_id in self._seen_message_ids:
            return []

        jobs = self._jobs_from_message(message)
        if not jobs:
            raise ValueError("no LinkedIn job cards were found")
        self._seen_message_ids.add(message_id)
        return jobs

    def _jobs_from_message(self, message: Message) -> list[Job]:
        sender = parseaddr(str(message.get("From", "")))[1]
        if sender.casefold() != self.sender_filter.casefold():
            raise ValueError(
                f"sender {sender or '<missing>'} does not match {self.sender_filter}"
            )

        html_body, plain_body = _extract_bodies(message)
        parser = _AlertParser(sender=self.sender_filter)
        parsed_jobs = parser.parse_message(message)
        if not parsed_jobs:
            return []

        email_date = _email_date(message.get("Date"))
        row_text = _html_row_texts(html_body) if html_body else {}
        jobs: list[Job] = []
        for parsed_job in parsed_jobs:
            snippet = row_text.get(parsed_job.source_job_id)
            if not snippet and plain_body:
                snippet = _plain_job_text(plain_body, parsed_job.source_job_id)
            raw_text = "\n".join(
                value
                for value in (
                    parsed_job.title,
                    parsed_job.company,
                    parsed_job.location,
                    snippet,
                )
                if value
            )
            jobs.append(
                Job(
                    source=self.name,
                    source_job_id=parsed_job.source_job_id,
                    title=parsed_job.title,
                    company=parsed_job.company,
                    location=parsed_job.location,
                    url=parsed_job.url,
                    posted_at=email_date,
                    raw_text=raw_text,
                    work_type=parsed_job.work_type,
                    flags=parsed_job.flags,
                )
            )
        return _deduplicate_jobs(jobs)


def _parse_mime_message(raw_email: bytes | str) -> Message:
    """Parse bytes or text using the standard-library email parser."""
    data = raw_email.encode("utf-8") if isinstance(raw_email, str) else raw_email
    return BytesParser(policy=policy.default).parsebytes(data)


def _message_id(message: Message) -> str:
    """Return a normalized Message-ID header value."""
    return str(message.get("Message-ID", "")).strip()


def _email_date(value: object) -> str | None:
    """Convert an RFC email Date header to an ISO timestamp."""
    if not isinstance(value, str) or not value.strip():
        LOGGER.warning("LinkedIn alert email has no valid Date header")
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError) as exc:
        LOGGER.warning("Could not parse LinkedIn alert email Date header: %s", exc)
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat(timespec="seconds")


def _html_row_texts(html_body: str | None) -> dict[str, str]:
    """Map job IDs to visible text from their HTML table rows."""
    if not html_body:
        return {}
    parser = _JobRowTextParser()
    parser.feed(html_body)
    parser.close()
    result: dict[str, str] = {}
    for hrefs, text in parser.rows:
        for href in hrefs:
            match = _JOB_PATH_RE.search(href)
            if match:
                result.setdefault(match.group(1), text)
    return result


def _plain_job_text(plain_body: str, job_id: str) -> str | None:
    """Return the plain-text block associated with one job URL."""
    previous_url = -1
    lines = plain_body.splitlines()
    for index, line in enumerate(lines):
        match = _JOB_URL_RE.search(line)
        if not match:
            continue
        if match.group(1) == job_id:
            return "\n".join(entry.strip() for entry in lines[previous_url + 1 : index + 1] if entry.strip())
        previous_url = index
    return None


def _raw_message(payload: object) -> bytes | None:
    """Extract the RFC822 body from an IMAP fetch response."""
    if not isinstance(payload, list):
        return None
    for item in payload:
        if isinstance(item, tuple) and len(item) > 1 and isinstance(item[1], bytes):
            return item[1]
    return None


def _response_text(response: object) -> str:
    """Render a server response without exposing client credentials."""
    if isinstance(response, (list, tuple)):
        return "; ".join(_response_text(value) for value in response)
    if isinstance(response, bytes):
        return response.decode("utf-8", errors="replace")
    return str(response)


def _deduplicate_jobs(jobs: list[Job]) -> list[Job]:
    """Keep the first occurrence of each LinkedIn job ID."""
    result: list[Job] = []
    seen: set[str] = set()
    for job in jobs:
        if job.source_job_id not in seen:
            seen.add(job.source_job_id)
            result.append(job)
    return result


def _imap_ok(status: str | bytes) -> bool:
    """Check an IMAP status value in either supported representation."""
    return status in ("OK", b"OK")
