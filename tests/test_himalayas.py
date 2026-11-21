import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from jobwatcher.collectors.himalayas import HimalayasCollector
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


class HimalayasCollectorTest(unittest.TestCase):
    def test_parses_api_fields_and_normalizes_us_restrictions(self) -> None:
        jobs = HimalayasCollector.parse_payload(
            {
                "jobs": [
                    {
                        "guid": "https://himalayas.app/companies/example/jobs/python-intern",
                        "applicationLink": "https://himalayas.app/apply/python-intern",
                        "title": "Python Intern",
                        "companyName": "Example Co",
                        "pubDate": 1795021942,
                        "locationRestrictions": ["United States"],
                        "description": "<p>Build Python tools.</p>",
                    }
                ]
            }
        )

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].source, "himalayas")
        self.assertEqual(jobs[0].source_job_id, jobs[0].url)
        self.assertEqual(jobs[0].location, "Remote - US-only")
        self.assertTrue(jobs[0].posted_at.startswith("2026-"))
        self.assertIn("Build Python tools.", jobs[0].raw_text)

    def test_falls_back_to_worldwide_and_deduplicates(self) -> None:
        listing = {
            "guid": "https://himalayas.app/jobs/data-analyst",
            "title": "Data Analyst",
            "locationRestrictions": [],
        }
        jobs = HimalayasCollector.parse_payload({"jobs": [listing, listing]})

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].location, "Remote worldwide")

    def test_normalizes_eu_only_restrictions(self) -> None:
        jobs = HimalayasCollector.parse_payload(
            {
                "jobs": [
                    {
                        "guid": "https://himalayas.app/jobs/eu-role",
                        "title": "Engineer",
                        "locationRestrictions": ["France", "Germany"],
                    }
                ]
            }
        )

        self.assertEqual(jobs[0].location, "Remote - EU-only")

    def test_invalid_payload_returns_empty(self) -> None:
        self.assertEqual(HimalayasCollector.parse_payload({"results": []}), [])

    def test_request_failure_returns_empty(self) -> None:
        session = Mock(spec=requests.Session)
        session.get.side_effect = requests.ConnectionError("offline")
        collector = HimalayasCollector(session=session, sleeper=lambda _: None)

        self.assertEqual(collector.fetch(), [])

    def test_himalayas_job_can_be_stored(self) -> None:
        jobs = HimalayasCollector.parse_payload(
            {
                "jobs": [
                    {
                        "guid": "https://himalayas.app/jobs/ui-designer",
                        "title": "UI Designer",
                    }
                ]
            }
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            connection = initialize_storage(Path(temp_dir) / "jobs.db")
            self.assertEqual(insert_jobs(connection, jobs), jobs)
            self.assertEqual(
                connection.execute("SELECT source FROM jobs").fetchone()[0],
                "himalayas",
            )
            connection.close()

    def test_server_error_gets_one_retry(self) -> None:
        session = Mock(spec=requests.Session)
        session.get.side_effect = [
            FakeResponse(503, {"jobs": []}),
            FakeResponse(200, {"jobs": []}),
        ]
        collector = HimalayasCollector(session=session, sleeper=lambda _: None)

        self.assertEqual(collector.fetch(), [])
        self.assertEqual(session.get.call_count, 2)

    def test_run_registers_all_enabled_collectors(self) -> None:
        config = {
            "sources": {
                "remotive": {"enabled": True},
                "remoteok": {"enabled": True},
                "himalayas": {"enabled": True},
                "adzuna": {"enabled": False},
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
                patch("jobwatcher.run.RemotiveCollector") as remotive,
                patch("jobwatcher.run.RemoteOKCollector") as remoteok,
                patch("jobwatcher.run.HimalayasCollector") as himalayas,
            ):
                from jobwatcher.run import main

                self.assertEqual(main(["--once"]), 0)

            remotive.return_value.fetch.assert_called_once_with()
            remoteok.return_value.fetch.assert_called_once_with()
            himalayas.return_value.fetch.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
