import tempfile
import unittest
from pathlib import Path

from jobwatcher.filter import embed_text, filter_job
from jobwatcher.models import Job
from jobwatcher.storage import initialize_storage, insert_jobs, update_filter_decision


def make_job(
    title: str,
    location: str = "Worldwide",
    raw_text: str = "",
) -> Job:
    return Job(
        source="remotive",
        source_job_id=title.lower().replace(" ", "-"),
        title=title,
        company="Example",
        location=location,
        url=f"https://example.com/{title.lower().replace(' ', '-')}",
        posted_at=None,
        raw_text=raw_text,
    )


class Step3Test(unittest.TestCase):
    def test_strong_title_match_survives_low_similarity(self) -> None:
        decision = filter_job(
            make_job("ML Intern"),
            "Profile text",
            threshold=0.35,
        )
        self.assertTrue(decision.survives)
        self.assertIn("strong title match", decision.reason)

    def test_title_exclude_beats_strong_include(self) -> None:
        decision = filter_job(make_job("Senior ML Intern"))
        self.assertFalse(decision.survives)
        self.assertIn("senior", decision.reason)

    def test_remote_us_only_rejected(self) -> None:
        decision = filter_job(make_job("ML Intern", "Remote - US-only"))
        self.assertFalse(decision.survives)
        self.assertIn("US-only", decision.reason)

    def test_on_site_outside_india_rejected(self) -> None:
        decision = filter_job(make_job("ML Intern", "On-site in London"))
        self.assertFalse(decision.survives)
        self.assertIn("outside India", decision.reason)

    def test_work_type_exclusions(self) -> None:
        for keyword in ("Contract", "Freelance", "Unpaid", "Volunteer"):
            decision = filter_job(make_job(f"ML Intern - {keyword}"))
            self.assertFalse(decision.survives)

    def test_ollama_unavailable_backend_falls_back_safely(self) -> None:
        embedding = embed_text(
            "test",
            backend="ollama",
            model="nomic-embed-text",
            ollama_url="http://127.0.0.1:1",
        )
        self.assertIsNone(embedding)

    def test_filter_decision_is_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            connection = initialize_storage(Path(temp_dir) / "jobs.db")
            job = make_job("ML Intern")
            inserted = insert_jobs(connection, [job])
            decision = filter_job(job, "Profile")
            update_filter_decision(
                connection,
                job,
                decision.survives,
                decision.similarity,
                decision.reason,
            )
            row = connection.execute(
                "SELECT processed, survives, similarity, filter_reason FROM jobs"
            ).fetchone()
            connection.close()

            self.assertEqual(len(inserted), 1)
            self.assertEqual(row[0], 1)
            self.assertEqual(row[1], 1)
            self.assertEqual(row[3], decision.reason)


if __name__ == "__main__":
    unittest.main()
