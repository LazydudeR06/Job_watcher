import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from jobwatcher.models import Job
from jobwatcher.scorer import SCORE_SCHEMA, ScoreResult, score_job
from jobwatcher.storage import (
    get_pending_scores,
    initialize_storage,
    insert_jobs,
    update_filter_decision,
    update_job_score,
)


def sample_job() -> Job:
    """Create a representative filtered job for scorer tests."""
    return Job(
        source="remotive",
        source_job_id="42",
        title="Python Intern",
        company="Example",
        location="Worldwide",
        url="https://example.com/jobs/42",
        posted_at=None,
        raw_text="Build Python applications.",
    )


class Step4Test(unittest.TestCase):
    @patch("jobwatcher.scorer.requests.post")
    def test_valid_json_score(self, post: Mock) -> None:
        post.return_value.json.return_value = {
            "response": '{"score": 87, "rationale": "Strong Python fit."}'
        }
        post.return_value.raise_for_status.return_value = None

        result = score_job(sample_job(), "Candidate profile", "llama3.2", "http://localhost:11434")

        self.assertEqual(result, ScoreResult(87, "Strong Python fit."))
        self.assertEqual(post.call_args.kwargs["json"]["format"], SCORE_SCHEMA)
        self.assertFalse(post.call_args.kwargs["json"]["stream"])

    @patch("jobwatcher.scorer.requests.post")
    def test_invalid_or_out_of_range_json_is_rejected(self, post: Mock) -> None:
        for raw in ('{"score": 101, "rationale": "too high"}', "not json"):
            post.return_value.json.return_value = {"response": raw}
            post.return_value.raise_for_status.return_value = None
            with self.subTest(response=raw):
                result = score_job(
                    sample_job(),
                    "Candidate profile",
                    "llama3.2",
                    "http://localhost:11434",
                )
                self.assertIsNone(result)

    @patch("jobwatcher.scorer.requests.post", side_effect=requests.ConnectionError("offline"))
    def test_request_failure_is_safe(self, post: Mock) -> None:
        result = score_job(sample_job(), "Profile", "llama3.2", "http://localhost:11434")
        self.assertIsNone(result)

    def test_scores_are_persisted_for_survivors_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            connection = initialize_storage(Path(temp_dir) / "jobs.db")
            job = sample_job()
            insert_jobs(connection, [job])
            update_filter_decision(connection, job, True, 0.8, "embedding match")
            self.assertEqual(get_pending_scores(connection), [job])

            result = ScoreResult(87, "Strong Python fit.")
            update_job_score(connection, job, result.score, result.rationale, result.as_json())

            self.assertEqual(get_pending_scores(connection), [])
            row = connection.execute(
                "SELECT llm_score, score_rationale, score_json FROM jobs"
            ).fetchone()
            connection.close()

            self.assertEqual(row, (87, "Strong Python fit.", result.as_json()))


if __name__ == "__main__":
    unittest.main()
