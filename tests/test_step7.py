import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import requests

from jobwatcher.collectors.remoteok import RemoteOKCollector
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


class Step7Test(unittest.TestCase):
    def test_parses_public_feed_and_skips_metadata(self) -> None:
        payload = [
            {"legal": "Please include a user agent"},
            {
                "id": 1234,
                "slug": "python-intern",
                "position": "Python Intern",
                "company": "Example Co",
                "location": "Remote - Worldwide",
                "url": "/remote-jobs/1234-python-intern",
                "date": "2026-11-18T10:00:00+00:00",
                "description": "Build Python tools.",
            },
        ]

        jobs = RemoteOKCollector.parse_payload(payload)

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].source, "remoteok")
        self.assertEqual(jobs[0].source_job_id, "1234")
        self.assertEqual(jobs[0].title, "Python Intern")
        self.assertEqual(jobs[0].url, "https://remoteok.com/remote-jobs/1234-python-intern")
        self.assertIn("Build Python tools.", jobs[0].raw_text)

    def test_url_fallback_and_duplicate_normalization(self) -> None:
        listing = {
            "slug": "design-intern",
            "title": "Product Designer Intern",
            "url": "https://remoteok.com/remote-jobs/design-intern#apply",
        }

        jobs = RemoteOKCollector.parse_payload([listing, listing])

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].source_job_id, "design-intern")
        self.assertEqual(jobs[0].url, "https://remoteok.com/remote-jobs/design-intern")

    def test_request_failure_returns_empty(self) -> None:
        session = Mock(spec=requests.Session)
        session.get.side_effect = requests.ConnectionError("offline")
        collector = RemoteOKCollector(session=session, sleeper=lambda _: None)

        self.assertEqual(collector.fetch(), [])

    def test_server_error_gets_one_retry(self) -> None:
        session = Mock(spec=requests.Session)
        session.get.side_effect = [
            FakeResponse(503, []),
            FakeResponse(200, []),
        ]
        collector = RemoteOKCollector(session=session, sleeper=lambda _: None)

        self.assertEqual(collector.fetch(), [])
        self.assertEqual(session.get.call_count, 2)

    def test_invalid_feed_shape_returns_empty(self) -> None:
        session = Mock(spec=requests.Session)
        session.get.return_value = FakeResponse(200, {"jobs": []})
        collector = RemoteOKCollector(session=session, sleeper=lambda _: None)

        self.assertEqual(collector.fetch(), [])

    def test_remoteok_job_can_be_stored(self) -> None:
        jobs = RemoteOKCollector.parse_payload(
            [
                {
                    "id": 99,
                    "position": "Junior Python Developer",
                    "url": "https://remoteok.com/remote-jobs/99",
                }
            ]
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            connection = initialize_storage(Path(temp_dir) / "jobs.db")
            self.assertEqual(insert_jobs(connection, jobs), jobs)
            self.assertEqual(
                connection.execute("SELECT source FROM jobs").fetchone()[0],
                "remoteok",
            )
            connection.close()


if __name__ == "__main__":
    unittest.main()
