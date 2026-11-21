import json
import sqlite3
from pathlib import Path
from unittest.mock import Mock, patch

from jobwatcher.collectors.linkedin_alerts import (
    DEFAULT_SENDER,
    LinkedInAlertEmailCollector,
)
from jobwatcher.config import load_config
from jobwatcher.models import Job
from jobwatcher.run import main
from jobwatcher.storage import get_pending_filters, initialize_storage, insert_jobs


ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "fixtures" / "linkedin_alerts"


def _expected_jobs() -> dict[str, list[dict[str, object]]]:
    return json.loads((FIXTURES / "expected_jobs.json").read_text(encoding="utf-8"))


def _job_values(job: Job) -> dict[str, object]:
    return {
        "source_job_id": job.source_job_id,
        "title": job.title,
        "company": job.company,
        "location": job.location,
        "work_type": job.work_type,
        "flags": list(job.flags),
        "url": job.url,
    }


def test_multipart_fixture_prefers_html_and_extracts_fields() -> None:
    expected = _expected_jobs()["multipart_alert.eml"]
    raw_message = (FIXTURES / "multipart_alert.eml").read_bytes()

    jobs = LinkedInAlertEmailCollector().parse_message(raw_message)

    assert len(expected) == 6
    assert [_job_values(job) for job in jobs] == expected


def test_plain_text_fixture_handles_wrapped_titles_without_work_type() -> None:
    expected = _expected_jobs()["plain_text_alert.eml"]
    raw_message = (FIXTURES / "plain_text_alert.eml").read_bytes()

    jobs = LinkedInAlertEmailCollector().parse_message(raw_message)

    assert len(expected) == 6
    assert [_job_values(job) for job in jobs] == expected


def test_sender_is_defaulted_and_subject_does_not_control_parsing() -> None:
    collector = LinkedInAlertEmailCollector()
    message = (FIXTURES / "multipart_alert.eml").read_bytes()

    assert collector.sender == DEFAULT_SENDER
    assert len(collector.parse_message(message)) == 6


def test_message_id_and_job_id_deduplicate_reprocessing(tmp_path: Path) -> None:
    raw_message = (FIXTURES / "multipart_alert.eml").read_bytes()
    collector = LinkedInAlertEmailCollector()
    first_batch = collector.parse_message(raw_message)
    assert collector.parse_message(raw_message) == []

    connection = initialize_storage(tmp_path / "jobs.db")
    assert insert_jobs(connection, first_batch) == first_batch
    replayed = LinkedInAlertEmailCollector().parse_message(raw_message)
    assert len(replayed) == 6
    assert insert_jobs(connection, replayed) == []
    assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 6
    connection.close()


def test_duplicate_titles_with_different_job_ids_are_retained() -> None:
    html = """
    <table>
      <tr><a class="font-bold" href="/jobs/view/7001/">Same title</a>
          <p>Same Co · Remote</p></tr>
      <tr><a class="font-bold" href="/jobs/view/7002/">Same title</a>
          <p>Same Co · Remote</p></tr>
    </table>
    """
    message = (
        "From: jobalerts-noreply@linkedin.com\r\n"
        "Message-ID: <two-different-jobs@example.test>\r\n"
        "MIME-Version: 1.0\r\n"
        "Content-Type: text/html; charset=utf-8\r\n\r\n"
        f"{html}"
    )

    jobs = LinkedInAlertEmailCollector().parse_message(message)

    assert [job.source_job_id for job in jobs] == ["7001", "7002"]


def test_imap_fetch_uses_sender_search_and_message_id_dedupe() -> None:
    raw_message = (FIXTURES / "multipart_alert.eml").read_bytes()
    imap = Mock()
    imap.login.return_value = ("OK", [])
    imap.select.return_value = ("OK", [])
    imap.search.return_value = ("OK", [b"1 2"])
    imap.fetch.side_effect = [
        ("OK", [(b"1 (RFC822 {0})", raw_message)]),
        ("OK", [(b"2 (RFC822 {0})", raw_message)]),
    ]
    imap.logout.return_value = ("BYE", [])

    jobs = LinkedInAlertEmailCollector(
        imap_host="imap.example.test",
        username="user",
        password="secret",
        imap_factory=lambda _: imap,
    ).fetch()

    assert len(jobs) == 6
    imap.search.assert_called_once_with(None, "UNSEEN", "FROM", DEFAULT_SENDER)
    imap.logout.assert_called_once()


def test_linkedin_jobs_and_metadata_persist_in_sqlite(tmp_path: Path) -> None:
    connection = initialize_storage(tmp_path / "jobs.db")
    jobs = LinkedInAlertEmailCollector().parse_message(
        (FIXTURES / "multipart_alert.eml").read_bytes()
    )

    assert len(insert_jobs(connection, jobs)) == 6
    stored = get_pending_filters(connection)
    assert stored[0].source == "linkedin"
    assert stored[0].work_type == "Hybrid"
    assert stored[0].flags == ("Easy Apply",)
    connection.close()


def test_existing_source_constraint_is_upgraded(tmp_path: Path) -> None:
    database = tmp_path / "old_jobs.db"
    old_connection = sqlite3.connect(database)
    old_connection.execute(
        """
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL CHECK (
                source IN ('remotive', 'remoteok', 'himalayas', 'adzuna')
            ),
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
    old_connection.execute(
        """
        INSERT INTO jobs
            (source, source_job_id, title, url, raw_text)
        VALUES ('remotive', 'old-1', 'Old job', 'https://example.test/old', '{}')
        """
    )
    old_connection.commit()
    old_connection.close()

    connection = initialize_storage(database)
    linkedin_job = Job(
        source="linkedin",
        source_job_id="new-1",
        title="New job",
        company="New Co",
        location="Remote",
        url="https://www.linkedin.com/jobs/view/new-1/",
        posted_at=None,
        raw_text="{}",
        flags=("Easy Apply",),
    )

    assert insert_jobs(connection, [linkedin_job]) == [linkedin_job]
    assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2
    connection.close()


def test_linkedin_defaults_disabled_in_config() -> None:
    config = load_config("config.yaml")

    assert config["sources"]["linkedin"]["enabled"] is False
    assert config["sources"]["linkedin"]["sender"] == DEFAULT_SENDER


def test_run_registers_linkedin_collector_when_enabled(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = {
        "sources": {
            "remotive": {"enabled": False},
            "remoteok": {"enabled": False},
            "himalayas": {"enabled": False},
            "adzuna": {"enabled": False},
            "linkedin": {
                "enabled": True,
                "imap_host_env": "LINKEDIN_IMAP_HOST",
                "username_env": "LINKEDIN_IMAP_USERNAME",
                "password_env": "LINKEDIN_IMAP_PASSWORD",
                "folder": "INBOX",
                "sender": DEFAULT_SENDER,
            },
        },
        "embeddings": {
            "backend": "ollama",
            "model": "nomic-embed-text",
            "ollama_url": "http://localhost:11434",
            "similarity_threshold": 0.35,
        },
        "scoring": {
            "model": "llama3.2:3b",
            "ollama_url": "http://localhost:11434",
            "timeout_seconds": 60,
        },
        "watcher": {"interval_seconds": 3600},
        "reports": {"digest_min_score": 60},
        "notifications": {"notification_min_score": 75},
        "outbound": {"enabled": False},
    }
    monkeypatch.setenv("LINKEDIN_IMAP_HOST", "imap.example.test")
    monkeypatch.setenv("LINKEDIN_IMAP_USERNAME", "candidate@example.test")
    monkeypatch.setenv("LINKEDIN_IMAP_PASSWORD", "test-password")

    with (
        patch("jobwatcher.run.configure_logging"),
        patch("jobwatcher.run.load_config", return_value=config),
        patch(
            "jobwatcher.run.initialize_storage",
            side_effect=lambda _: initialize_storage(tmp_path / "jobs.db"),
        ),
        patch("jobwatcher.run.LinkedInAlertEmailCollector") as collector_type,
        patch("jobwatcher.run.generate_reports", return_value=[]),
        patch("jobwatcher.run.send_notifications"),
    ):
        assert main(["--once"]) == 0

    collector_type.assert_called_once_with(
        imap_host="imap.example.test",
        username="candidate@example.test",
        password="test-password",
        folder="INBOX",
        sender=DEFAULT_SENDER,
    )
    collector_type.return_value.fetch.assert_called_once_with()
