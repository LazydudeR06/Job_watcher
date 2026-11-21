import imaplib
import json
import logging
import re
from collections.abc import Callable
from email import policy
from email.message import Message
from email.parser import BytesParser
from email.utils import parseaddr
from html.parser import HTMLParser
from typing import Any

from ..models import Job
from .base import Collector


LOGGER = logging.getLogger(__name__)
DEFAULT_SENDER = "jobalerts-noreply@linkedin.com"
_JOB_PATH_RE = re.compile(r"/jobs/view/([^/?#]+)", re.IGNORECASE)
_JOB_URL_RE = re.compile(
    r"https?://(?:www\.)?linkedin\.com/jobs/view/[^/\s?#]+"
    r"(?:/[^\s?#]*)?(?:\?[^\s]*)?",
    re.IGNORECASE,
)
_WORK_TYPE_RE = re.compile(r"\s+\((Remote|Hybrid|On-site)\)\s*$", re.IGNORECASE)
_IGNORED_LINKS = {
    "messaging",
    "my network",
    "notifications",
    "see all jobs",
    "manage alerts",
    "premium",
}


class _RowHTMLParser(HTMLParser):
    """Collect anchors and paragraphs in each table row."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[tuple[str, str, frozenset[str], str]]] = []
        self._row: list[tuple[str, str, frozenset[str], str]] | None = None
        self._anchor: tuple[str, frozenset[str], list[str]] | None = None
        self._paragraph: tuple[frozenset[str], list[str]] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        if tag == "tr":
            self._finish_row()
            self._row = []
        elif self._row is not None and tag == "a":
            classes = frozenset(attributes.get("class", "").split())
            self._anchor = (attributes.get("href", ""), classes, [])
        elif self._row is not None and tag == "p":
            classes = frozenset(attributes.get("class", "").split())
            self._paragraph = (classes, [])
        elif tag == "br":
            if self._anchor is not None:
                self._anchor[2].append(" ")
            if self._paragraph is not None:
                self._paragraph[1].append(" ")

    def handle_data(self, data: str) -> None:
        if self._anchor is not None:
            self._anchor[2].append(data)
        if self._paragraph is not None:
            self._paragraph[1].append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._anchor is not None:
            href, classes, text = self._anchor
            if self._row is not None:
                self._row.append(("a", href, classes, "".join(text).strip()))
            self._anchor = None
        elif tag == "p" and self._paragraph is not None:
            classes, text = self._paragraph
            if self._row is not None:
                self._row.append(("p", "", classes, "".join(text).strip()))
            self._paragraph = None
        elif tag == "tr":
            self._finish_row()

    def close(self) -> None:
        super().close()
        self._finish_row()

    def _finish_row(self) -> None:
        if self._row is not None:
            self.rows.append(self._row)
            self._row = None


class LinkedInAlertEmailCollector(Collector):
    """Collect LinkedIn jobs from alert emails available through IMAP."""

    name = "linkedin"

    def __init__(
        self,
        imap_host: str | None = None,
        username: str | None = None,
        password: str | None = None,
        folder: str = "INBOX",
        sender: str = DEFAULT_SENDER,
        imap_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self.imap_host = imap_host
        self.username = username
        self.password = password
        self.folder = folder
        self.sender = sender
        self.imap_factory = imap_factory
        self._seen_message_ids: set[str] = set()

    def fetch(self) -> list[Job]:
        """Fetch unseen LinkedIn alert emails and parse their job listings."""
        if not self.imap_host or not self.username or not self.password:
            LOGGER.warning("LinkedIn alert collector is missing IMAP credentials")
            return []

        client = None
        jobs: list[Job] = []
        try:
            factory = self.imap_factory or imaplib.IMAP4_SSL
            client = factory(self.imap_host)
            status, _ = client.login(self.username, self.password)
            if not _imap_ok(status):
                LOGGER.warning("LinkedIn IMAP login failed")
                return []
            status, _ = client.select(self.folder)
            if not _imap_ok(status):
                LOGGER.warning("Could not select LinkedIn IMAP folder %s", self.folder)
                return []
            status, data = client.search(None, "UNSEEN", "FROM", self.sender)
            if not _imap_ok(status):
                LOGGER.warning("Could not search for LinkedIn alert emails")
                return []

            message_ids = data[0].split() if data else []
            for message_id in message_ids:
                status, parts = client.fetch(message_id, "(RFC822)")
                if not _imap_ok(status):
                    LOGGER.warning("Could not fetch LinkedIn alert email %s", message_id)
                    continue
                raw_message = next(
                    (
                        part[1]
                        for part in (parts or [])
                        if isinstance(part, tuple)
                        and len(part) > 1
                        and isinstance(part[1], bytes)
                    ),
                    None,
                )
                if raw_message is not None:
                    jobs.extend(self.parse_message(raw_message))
        except (imaplib.IMAP4.error, OSError) as exc:
            LOGGER.warning("LinkedIn IMAP request failed: %s", exc)
        finally:
            if client is not None:
                try:
                    client.logout()
                except (imaplib.IMAP4.error, OSError):
                    LOGGER.debug("LinkedIn IMAP logout failed", exc_info=True)
        return _deduplicate_jobs(jobs)

    def parse_message(self, raw_message: bytes | str | Message) -> list[Job]:
        """Parse one message from the configured LinkedIn alert sender."""
        if isinstance(raw_message, Message):
            message = raw_message
        else:
            data = (
                raw_message.encode("utf-8")
                if isinstance(raw_message, str)
                else raw_message
            )
            message = BytesParser(policy=policy.default).parsebytes(data)

        sender = parseaddr(str(message.get("From", "")))[1]
        if sender.casefold() != self.sender.casefold():
            return []

        message_id = str(message.get("Message-ID", "")).strip()
        if message_id and message_id in self._seen_message_ids:
            return []

        html_body, plain_body = _extract_bodies(message)
        jobs = _parse_html_jobs(html_body) if html_body else []
        if not jobs and plain_body:
            jobs = _parse_plain_jobs(plain_body)
        if message_id:
            self._seen_message_ids.add(message_id)
        return _deduplicate_jobs(jobs)


def _extract_bodies(message: Message) -> tuple[str | None, str | None]:
    """Return decoded HTML and plain-text MIME bodies, if present."""
    html_body: str | None = None
    plain_body: str | None = None
    for part in message.walk():
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        content_type = part.get_content_type().lower()
        if content_type not in ("text/html", "text/plain"):
            continue
        try:
            content = part.get_content()
        except (LookupError, UnicodeDecodeError, AttributeError):
            continue
        if not isinstance(content, str):
            continue
        if content_type == "text/html" and html_body is None:
            html_body = content
        elif content_type == "text/plain" and plain_body is None:
            plain_body = content
    return html_body, plain_body


def _parse_html_jobs(html_body: str) -> list[Job]:
    """Parse LinkedIn job rows from an HTML alert body."""
    parser = _RowHTMLParser()
    parser.feed(html_body)
    parser.close()

    jobs: list[Job] = []
    for row in parser.rows:
        for index, event in enumerate(row):
            kind, href, classes, title = event
            job_id = _job_id(href)
            if (
                kind != "a"
                or "font-bold" not in classes
                or job_id is None
                or not title
                or title.casefold() in _IGNORED_LINKS
            ):
                continue

            paragraphs = row[index + 1 :]
            detail = next(
                (
                    text
                    for kind, _, classes, text in paragraphs
                    if kind == "p"
                    and "job-card-flavor__detail" not in classes
                    and text
                ),
                "",
            )
            flags = tuple(
                text
                for kind, _, classes, text in row
                if kind == "p"
                and "job-card-flavor__detail" in classes
                and text
            )
            company, location, work_type = _parse_details(
                detail,
                include_work_type=True,
            )
            url = _canonical_job_url(job_id)
            raw_text = json.dumps(
                {
                    "title": title,
                    "company": company,
                    "location": location,
                    "work_type": work_type,
                    "flags": flags,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            jobs.append(
                Job(
                    source="linkedin",
                    source_job_id=job_id,
                    title=title,
                    company=company,
                    location=location,
                    url=url,
                    posted_at=None,
                    raw_text=raw_text,
                    work_type=work_type,
                    flags=flags,
                )
            )
            break
    return _deduplicate_jobs(jobs)


def _parse_plain_jobs(plain_body: str) -> list[Job]:
    """Parse plain-text alert listings, including titles wrapped across lines."""
    lines = plain_body.splitlines()
    jobs: list[Job] = []
    segment_start = 0
    for index, line in enumerate(lines):
        match = _JOB_URL_RE.search(line)
        if match is None:
            continue

        segment = lines[segment_start:index]
        segment_start = index + 1
        url = match.group(0).rstrip(".,);>")
        job_id = _job_id(url)
        if job_id is None:
            continue

        content_lines = [entry.strip() for entry in segment if entry.strip()]
        detail_index = next(
            (
                i
                for i in range(len(content_lines) - 1, -1, -1)
                if " · " in content_lines[i]
            ),
            None,
        )
        if detail_index is None:
            title_lines = content_lines
            detail = ""
        else:
            title_lines = content_lines[:detail_index]
            detail = content_lines[detail_index]
        title_lines = [
            entry
            for entry in title_lines
            if entry.casefold() not in _IGNORED_LINKS
            and entry.casefold() not in {"view job", "apply", "view"}
        ]
        title = " ".join(title_lines).strip()
        if not title or title.casefold() in _IGNORED_LINKS:
            continue

        company, location, _ = _parse_details(detail, include_work_type=False)
        raw_text = "\n".join(
            value for value in (title, company, location) if value
        )
        jobs.append(
            Job(
                source="linkedin",
                source_job_id=job_id,
                title=title,
                company=company,
                location=location,
                url=_canonical_job_url(job_id),
                posted_at=None,
                raw_text=raw_text,
            )
        )
    return _deduplicate_jobs(jobs)


def _parse_details(
    details: str,
    *,
    include_work_type: bool,
) -> tuple[str | None, str | None, str | None]:
    """Split company/location and optionally remove a trailing work type."""
    company: str | None = None
    location: str | None = None
    work_type: str | None = None
    if " · " in details:
        company_text, location_text = details.split(" · ", 1)
        company = company_text.strip() or None
        location = location_text.strip() or None
    elif details.strip():
        location = details.strip()

    if include_work_type and location:
        match = _WORK_TYPE_RE.search(location)
        if match:
            work_type = match.group(1)
            location = location[: match.start()].strip() or None
    return company, location, work_type


def _job_id(url: str) -> str | None:
    """Extract a LinkedIn job identifier from a URL or path."""
    match = _JOB_PATH_RE.search(url)
    return match.group(1) if match else None


def _canonical_job_url(job_id: str) -> str:
    """Build the canonical LinkedIn job URL without tracking parameters."""
    return f"https://www.linkedin.com/jobs/view/{job_id}/"


def _deduplicate_jobs(jobs: list[Job]) -> list[Job]:
    """Keep the first job for each LinkedIn job ID."""
    deduplicated: list[Job] = []
    seen_ids: set[str] = set()
    for job in jobs:
        if job.source_job_id in seen_ids:
            continue
        seen_ids.add(job.source_job_id)
        deduplicated.append(job)
    return deduplicated


def _imap_ok(status: str | bytes) -> bool:
    """Check an IMAP status value without depending on its bytes/string form."""
    return status in ("OK", b"OK")
