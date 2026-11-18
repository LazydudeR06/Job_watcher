import csv
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from jobwatcher.reports import generate_reports, write_daily_digest, write_hourly_csv
from jobwatcher.storage import (
    initialize_storage,
    insert_jobs,
    update_filter_decision,
    update_job_score,
)
from jobwatcher.models import Job


class Step5Test(unittest.TestCase):
    def setUp(self) -> None:
        self.job = Job(
            source="remotive",
            source_job_id="99",
            title="Junior Python Developer",
            company="Example Co",
            location="Remote worldwide",
            url="https://example.com/job/99",
            posted_at="2026-11-18",
            raw_text="Build useful software.",
        )
        self.row = {
            "source": "remotive",
            "source_job_id": "99",
            "title": self.job.title,
            "company": self.job.company,
            "location": self.job.location,
            "url": self.job.url,
            "posted_at": self.job.posted_at,
            "llm_score": 86,
            "score_rationale": "Good fit.",
            "scored_at": "2026-11-18T10:30:00+05:30",
        }

    def test_digest_and_hourly_csv_are_written(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            digest = write_daily_digest([self.row], root / "digests", datetime(2026, 11, 18))
            csv_path = write_hourly_csv([self.row], root / "hourly", datetime(2026, 11, 18, 10))

            self.assertTrue(digest and digest.is_file())
            self.assertIn("86/100", digest.read_text(encoding="utf-8"))
            self.assertTrue(csv_path and csv_path.is_file())
            with csv_path.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["llm_score"], "86")

    def test_report_generation_queries_scored_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            connection = initialize_storage(Path(temp_dir) / "jobs.db")
            insert_jobs(connection, [self.job])
            update_filter_decision(connection, self.job, True, 0.8, "matched")
            update_job_score(connection, self.job, 86, "Good fit.", '{"score":86}')
            generated = generate_reports(
                connection,
                Path(temp_dir) / "output",
                now=datetime.now().astimezone(),
            )
            connection.close()

            self.assertEqual(len(generated), 2)
            self.assertTrue(all(path.is_file() for path in generated))

    def test_empty_reports_are_not_created(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            self.assertIsNone(write_daily_digest([], Path(temp_dir) / "digest"))
            self.assertIsNone(write_hourly_csv([], Path(temp_dir) / "csv"))
            self.assertEqual(list(Path(temp_dir).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
