import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from jobwatcher.collectors.adzuna import AdzunaCollector
from jobwatcher.storage import initialize_storage, insert_jobs


class FakeResponse:
    def __init__(self, status_code: int, payload) -> None:
        self.status_code = status_code
        self.payload = payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload


class AdzunaCollectorTest(unittest.TestCase):
    def test_parses_public_api_result(self) -> None:
        jobs = AdzunaCollector.parse_payload(
            {
                "results": [
                    {
                        "id": "123",
                        "title": "Python Intern",
                        "company": {"display_name": "Example Co"},
                        "location": {"display_name": "Bengaluru"},
                        "redirect_url": "https://adzuna.example/job/123#apply",
                        "created": "2026-11-21T12:00:00Z",
                        "description": "Build Python tools.",
                    }
                ]
            }
        )
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].source, "adzuna")
        self.assertEqual(jobs[0].source_job_id, "123")
        self.assertEqual(jobs[0].company, "Example Co")
        self.assertEqual(jobs[0].location, "Bengaluru")
        self.assertEqual(jobs[0].url, "https://adzuna.example/job/123")

    def test_missing_id_falls_back_to_url_and_deduplicates(self) -> None:
        result = {
            "title": "Product Designer",
            "redirect_url": "https://adzuna.example/job/design",
        }
        jobs = AdzunaCollector.parse_payload({"results": [result, result]})
        self.assertEqual(len(jobs), 1)
        self.assertEqual(len(jobs[0].source_job_id), 64)

    def test_no_credentials_disables_requests(self) -> None:
        session = Mock(spec=requests.Session)
        collector = AdzunaCollector(None, None, session=session, sleeper=lambda _: None)
        self.assertEqual(collector.fetch(), [])
        session.get.assert_not_called()

    def test_fetch_uses_api_credentials_and_retries_server_error(self) -> None:
        session = Mock(spec=requests.Session)
        session.get.side_effect = [
            FakeResponse(503, {"results": []}),
            FakeResponse(200, {"results": []}),
        ]
        collector = AdzunaCollector(
            "app-id",
            "app-key",
            session=session,
            sleeper=lambda _: None,
        )
        self.assertEqual(collector.fetch(), [])
        self.assertEqual(session.get.call_count, 2)
        self.assertEqual(session.get.call_args.kwargs["params"]["app_id"], "app-id")
        self.assertEqual(session.get.call_args.kwargs["params"]["app_key"], "app-key")
        self.assertEqual(session.get.call_args.kwargs["timeout"], 20.0)

    def test_request_failure_returns_empty(self) -> None:
        session = Mock(spec=requests.Session)
        session.get.side_effect = requests.ConnectionError("offline")
        collector = AdzunaCollector("id", "key", session=session, sleeper=lambda _: None)
        self.assertEqual(collector.fetch(), [])

    def test_adzuna_job_can_be_inserted_into_existing_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "jobs.db"
            connection = sqlite3.connect(database)
            connection.execute(
                """
                CREATE TABLE jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL CHECK (source IN ('remotive', 'remoteok', 'himalayas')),
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
            connection.execute(
                """
                INSERT INTO jobs (source, source_job_id, title, url, raw_text)
                VALUES ('remotive', '1', 'Saved job', 'https://example.com/old', '{}')
                """
            )
            connection.commit()
            connection.close()

            connection = initialize_storage(database)
            jobs = AdzunaCollector.parse_payload(
                {
                    "results": [
                        {
                            "id": "adzuna-1",
                            "title": "Python Intern",
                            "redirect_url": "https://example.com/adzuna-1",
                        }
                    ]
                }
            )
            self.assertEqual(insert_jobs(connection, jobs), jobs)
            self.assertEqual(
                connection.execute(
                    "SELECT source, title FROM jobs ORDER BY id"
                ).fetchall(),
                [("remotive", "Saved job"), ("adzuna", "Python Intern")],
            )
            connection.close()

    def test_run_registers_adzuna_only_when_credentials_exist(self) -> None:
        config = {
            "sources": {
                "remotive": {"enabled": False},
                "remoteok": {"enabled": False},
                "himalayas": {"enabled": False},
                "adzuna": {
                    "enabled": True,
                    "app_id_env": "ADZUNA_APP_ID",
                    "app_key_env": "ADZUNA_APP_KEY",
                    "country": "in",
                    "results_per_page": 50,
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
            "watcher": {"interval_seconds": 900},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "jobs.db"
            with (
                patch("jobwatcher.run.configure_logging"),
                patch("jobwatcher.run.load_config", return_value=config),
                patch(
                    "jobwatcher.run.initialize_storage",
                    side_effect=lambda _: initialize_storage(database),
                ),
                patch("jobwatcher.run.AdzunaCollector") as collector,
                patch.dict(
                    "os.environ",
                    {"ADZUNA_APP_ID": "id", "ADZUNA_APP_KEY": "key"},
                ),
            ):
                from jobwatcher.run import main

                self.assertEqual(main(["--once"]), 0)
            collector.assert_called_once()

    def test_run_skips_adzuna_registration_when_credentials_are_missing(self) -> None:
        config = {
            "sources": {
                "remotive": {"enabled": False},
                "remoteok": {"enabled": False},
                "himalayas": {"enabled": False},
                "adzuna": {
                    "enabled": True,
                    "app_id_env": "ADZUNA_APP_ID",
                    "app_key_env": "ADZUNA_APP_KEY",
                    "country": "in",
                    "results_per_page": 50,
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
            "watcher": {"interval_seconds": 900},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "jobs.db"
            with (
                patch("jobwatcher.run.configure_logging"),
                patch("jobwatcher.run.load_config", return_value=config),
                patch(
                    "jobwatcher.run.initialize_storage",
                    side_effect=lambda _: initialize_storage(database),
                ),
                patch("jobwatcher.run.AdzunaCollector") as collector,
                patch.dict("os.environ", {}, clear=True),
            ):
                from jobwatcher.run import main

                self.assertEqual(main(["--once"]), 0)
            collector.assert_not_called()


if __name__ == "__main__":
    unittest.main()
