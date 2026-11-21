import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from jobwatcher.collectors.linkedin_alerts import DEFAULT_SENDER
from jobwatcher.collectors.linkedin_email import LinkedInEmailCollector
from jobwatcher.config import load_config
from jobwatcher.run import main
from jobwatcher.storage import (
    initialize_storage,
    insert_jobs,
    is_email_processed,
)


FIXTURES = Path(__file__).parent / "fixtures" / "linkedin_alerts"
EXPECTED = json.loads(
    (FIXTURES / "expected_jobs.json").read_text(encoding="utf-8")
)


def _collector(
    connection: sqlite3.Connection | None = None,
    **kwargs: object,
) -> LinkedInEmailCollector:
    return LinkedInEmailCollector(
        enabled=True,
        imap_user="person@gmail.com",
        imap_app_password="test-app-password",
        connection=connection,
        **kwargs,
    )


def _imap_mock(messages: list[bytes], numbers: bytes | None = None) -> Mock:
    imap = Mock()
    imap.login.return_value = ("OK", [b"authenticated"])
    imap.select.return_value = ("OK", [b"6"])
    ids = numbers or b" ".join(str(i + 1).encode() for i in range(len(messages)))
    imap.search.return_value = ("OK", [ids])
    imap.fetch.side_effect = [
        ("OK", [(f"{i + 1} (BODY[] {{{len(message)}}})".encode(), message)])
        for i, message in enumerate(messages)
    ]
    imap.logout.return_value = ("BYE", [b"logged out"])
    return imap


def test_parses_html_and_plain_fixtures_into_jobs() -> None:
    expected_date = "2026-11-21T15:30:00+00:00"
    for filename in ("multipart_alert.eml", "plain_text_alert.eml"):
        raw_email = (FIXTURES / filename).read_bytes()
        jobs = _collector().parse_email(raw_email)
        expected = EXPECTED[filename]

        assert len(expected) == 6
        assert len(jobs) == 6
        for job, item in zip(jobs, expected):
            assert job.source == "linkedin_email"
            assert job.source_job_id == item["source_job_id"]
            assert job.title == item["title"]
            assert job.company == item["company"]
            assert job.location == item["location"]
            assert job.url == item["url"]
            assert job.work_type == item["work_type"]
            assert list(job.flags) == item["flags"]
            assert job.posted_at == expected_date
            assert job.title in job.raw_text
            assert job.company in job.raw_text
            assert job.location in job.raw_text


def test_url_cleaning_removes_tracking_query_parameters() -> None:
    jobs = _collector().parse_email(
        (FIXTURES / "multipart_alert.eml").read_bytes()
    )

    assert jobs[0].url == "https://www.linkedin.com/jobs/view/9001/"
    assert all("?" not in job.url and "/jobs/view/" in job.url for job in jobs)


def test_snippets_are_included_in_the_job_text() -> None:
    html_jobs = _collector().parse_email(
        (FIXTURES / "multipart_alert.eml").read_bytes()
    )
    plain_jobs = _collector().parse_email(
        (FIXTURES / "plain_text_alert.eml").read_bytes()
    )

    assert "Help build ML features for local search." in html_jobs[0].raw_text
    assert "Help improve recommendation models." in plain_jobs[0].raw_text


def test_same_job_in_two_emails_deduplicates_through_storage(
    tmp_path: Path,
) -> None:
    first_email = (FIXTURES / "multipart_alert.eml").read_bytes()
    second_email = first_email.replace(
        b"<linkedin-multipart-6@example.test>",
        b"<linkedin-multipart-copy@example.test>",
    )
    collector = _collector()
    first_batch = collector.parse_email(first_email)
    second_batch = collector.parse_email(second_email)
    connection = initialize_storage(tmp_path / "jobs.db")

    assert len(first_batch) == 6
    assert len(second_batch) == 6
    assert len(insert_jobs(connection, first_batch)) == 6
    assert insert_jobs(connection, second_batch) == []
    assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 6
    connection.close()


def test_disabled_and_missing_credentials_do_not_connect() -> None:
    connection_factory = Mock()
    disabled = LinkedInEmailCollector(
        enabled=False,
        imap_user="user",
        imap_app_password="secret",
        imap_factory=connection_factory,
    )
    missing_user = LinkedInEmailCollector(
        enabled=True,
        imap_app_password="secret",
        imap_factory=connection_factory,
    )
    missing_password = LinkedInEmailCollector(
        enabled=True,
        imap_user="user",
        imap_factory=connection_factory,
    )

    assert disabled.fetch() == []
    assert missing_user.fetch() == []
    assert missing_password.fetch() == []
    connection_factory.assert_not_called()


def test_imap_connection_error_is_logged_and_handled(caplog) -> None:
    def fail_to_connect(_: str) -> Mock:
        raise OSError("network unavailable")

    collector = _collector(imap_factory=fail_to_connect)
    with caplog.at_level(logging.WARNING):
        assert collector.fetch() == []

    assert "network unavailable" in caplog.text
    assert "test-app-password" not in caplog.text


def test_wrong_password_is_logged_without_exposing_credentials(caplog) -> None:
    imap = _imap_mock([])
    imap.login.return_value = ("NO", [b"Invalid credentials"])
    collector = _collector(imap_factory=lambda _: imap)

    with caplog.at_level(logging.WARNING):
        assert collector.fetch() == []

    assert "Invalid credentials" in caplog.text
    assert "test-app-password" not in caplog.text
    imap.select.assert_not_called()


def test_fetch_uses_read_only_selection_sender_date_and_message_dedupe(
    tmp_path: Path,
) -> None:
    raw_email = (FIXTURES / "multipart_alert.eml").read_bytes()
    connection = initialize_storage(tmp_path / "jobs.db")
    imap = _imap_mock([raw_email, raw_email], numbers=b"1 2")
    imap.fetch.side_effect = None
    imap.fetch.return_value = (
        "OK",
        [(b"2 (BODY[])", raw_email)],
    )
    collector = _collector(
        connection=connection,
        max_emails_per_run=1,
        lookback_days=3,
    )

    with patch(
        "jobwatcher.collectors.linkedin_email.imaplib.IMAP4_SSL",
        return_value=imap,
    ) as imap_ssl:
        first_batch = collector.fetch()
        second_batch = _collector(
            connection=connection,
            max_emails_per_run=1,
            lookback_days=3,
        ).fetch()

    cutoff = (
        datetime.now(timezone.utc).date() - timedelta(days=3)
    ).strftime("%d-%b-%Y")
    assert imap_ssl.call_count == 2
    imap_ssl.assert_called_with("imap.gmail.com")
    assert len(first_batch) == 6
    assert second_batch == []
    assert imap.select.call_args_list[0].args == ("INBOX",)
    assert imap.select.call_args_list[0].kwargs == {"readonly": True}
    imap.search.assert_called_with(
        None,
        "SINCE",
        cutoff,
        "FROM",
        DEFAULT_SENDER,
    )
    assert imap.fetch.call_count == 2
    assert imap.fetch.call_args.args[1] == "(BODY.PEEK[])"
    assert is_email_processed(
        connection,
        "<linkedin-multipart-6@example.test>",
    )
    connection.close()


def test_fetch_continues_after_an_unparseable_email(tmp_path: Path, caplog) -> None:
    malformed = (
        b"From: jobalerts-noreply@linkedin.com\r\n"
        b"Message-ID: <broken@example.test>\r\n"
        b"Content-Type: text/plain\r\n\r\nNo job data here."
    )
    valid = (FIXTURES / "multipart_alert.eml").read_bytes()
    connection = initialize_storage(tmp_path / "jobs.db")
    imap = _imap_mock([malformed, valid])
    collector = _collector(connection=connection, imap_factory=lambda _: imap)

    with caplog.at_level(logging.WARNING):
        jobs = collector.fetch()

    assert len(jobs) == 6
    assert "no LinkedIn job cards were found" in caplog.text
    assert not is_email_processed(connection, "<broken@example.test>")
    assert is_email_processed(connection, "<linkedin-multipart-6@example.test>")
    connection.close()


def test_fetch_mock_uses_ssl_and_caps_requested_messages(tmp_path: Path) -> None:
    raw_email = (FIXTURES / "plain_text_alert.eml").read_bytes()
    connection = initialize_storage(tmp_path / "jobs.db")
    imap = _imap_mock([raw_email], numbers=b"1 2 3")
    collector = _collector(
        connection=connection,
        max_emails_per_run=1,
        imap_factory=lambda _: imap,
    )

    jobs = collector.fetch()

    assert len(jobs) == 6
    assert imap.fetch.call_count == 1
    assert imap.fetch.call_args.args[0] == b"3"
    connection.close()


def test_imap_ssl_default_host_and_sender_defaults() -> None:
    collector = LinkedInEmailCollector()

    assert collector.imap_host == "imap.gmail.com"
    assert collector.folder == "INBOX"
    assert collector.sender_filter == DEFAULT_SENDER
    assert collector.max_emails_per_run == 50
    assert collector.lookback_days == 3


def test_config_has_linkedin_email_disabled_with_requested_defaults() -> None:
    config = load_config("config.yaml")
    settings = config["sources"]["linkedin_email"]

    assert settings["enabled"] is False
    assert settings["imap_host"] == "imap.gmail.com"
    assert settings["folder"] == "INBOX"
    assert settings["sender_filter"] == DEFAULT_SENDER
    assert settings["max_emails_per_run"] == 50
    assert settings["lookback_days"] == 3


def test_run_registers_collector_with_env_credentials(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = {
        "sources": {
            "remotive": {"enabled": False},
            "remoteok": {"enabled": False},
            "himalayas": {"enabled": False},
            "adzuna": {"enabled": False},
            "linkedin_email": {
                "enabled": True,
                "imap_host": "imap.example.test",
                "folder": "Alerts",
                "sender_filter": DEFAULT_SENDER,
                "max_emails_per_run": 17,
                "lookback_days": 5,
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
    monkeypatch.setenv("IMAP_USER", "person@gmail.com")
    monkeypatch.setenv("IMAP_APP_PASSWORD", "configured-app-password")

    with (
        patch("jobwatcher.run.configure_logging"),
        patch("jobwatcher.run.load_config", return_value=config),
        patch(
            "jobwatcher.run.initialize_storage",
            side_effect=lambda _: initialize_storage(tmp_path / "jobs.db"),
        ),
        patch("jobwatcher.run.LinkedInEmailCollector") as collector_type,
        patch("jobwatcher.run.generate_reports", return_value=[]),
        patch("jobwatcher.run.send_notifications"),
    ):
        assert main(["--once"]) == 0

    collector_type.assert_called_once()
    options = collector_type.call_args.kwargs
    assert options["enabled"] is True
    assert options["imap_host"] == "imap.example.test"
    assert options["folder"] == "Alerts"
    assert options["sender_filter"] == DEFAULT_SENDER
    assert options["max_emails_per_run"] == 17
    assert options["lookback_days"] == 5
    assert options["imap_user"] == "person@gmail.com"
    assert options["imap_app_password"] == "configured-app-password"
    assert options["connection"] is not None
    collector_type.return_value.fetch.assert_called_once_with()
