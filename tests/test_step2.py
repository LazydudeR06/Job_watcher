import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import requests

from jobwatcher.collectors.remotive import RemotiveCollector
from jobwatcher.models import Job
from jobwatcher.storage import initialize_storage, insert_jobs


class FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        if self._payload is None:
            raise ValueError("invalid JSON")
        return self._payload


class Step2Test(unittest.TestCase):
    def test_collector_parsing_and_fallback_id(self) -> None:
        payload = {
            "jobs": [
                {
                    "title": "Python Developer",
                    "company_name": "Example Co",
                    "candidate_required_location": "Worldwide",
                    "url": "https://example.com/jobs/123#apply",
                    "publication_date": "2026-11-18",
                }
            ]
        }
        jobs = RemotiveCollector.parse_payload(payload)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].source, "remotive")
        self.assertEqual(jobs[0].url, "https://example.com/jobs/123")
        self.assertEqual(len(jobs[0].source_job_id), 64)
        self.assertIn("Python Developer", jobs[0].raw_text)

    def test_url_dedupe_in_storage(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            connection = initialize_storage(Path(temp_dir) / "jobs.db")
            first = Job(
                source="remotive",
                source_job_id="1",
                title="First",
                company="Example",
                location="Remote",
                url="https://example.com/job",
                posted_at=None,
                raw_text="{}",
            )
            duplicate = Job(
                source="remotive",
                source_job_id="2",
                title="Duplicate URL",
                company="Example",
                location="Remote",
                url="https://example.com/job",
                posted_at=None,
                raw_text="{}",
            )

            self.assertEqual(insert_jobs(connection, [first]), [first])
            self.assertEqual(insert_jobs(connection, [duplicate]), [])
            connection.close()

    def test_collector_request_failure_returns_empty(self) -> None:
        session = Mock(spec=requests.Session)
        session.get.side_effect = requests.ConnectionError("offline")
        collector = RemotiveCollector(session=session, sleeper=lambda _: None)

        self.assertEqual(collector.fetch(), [])

    def test_collector_invalid_payload_returns_empty(self) -> None:
        session = Mock(spec=requests.Session)
        session.get.return_value = FakeResponse(200, None)
        collector = RemotiveCollector(session=session, sleeper=lambda _: None)

        self.assertEqual(collector.fetch(), [])


if __name__ == "__main__":
    unittest.main()
