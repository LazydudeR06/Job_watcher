import json
import logging
from dataclasses import dataclass
from typing import Any

import requests

from .models import Job


LOGGER = logging.getLogger(__name__)

SCORE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "minimum": 0, "maximum": 100},
        "rationale": {"type": "string"},
    },
    "required": ["score", "rationale"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class ScoreResult:
    """Validated LLM job match score."""

    score: int
    rationale: str

    def as_json(self) -> str:
        """Return the validated score as canonical JSON."""
        return json.dumps(
            {"score": self.score, "rationale": self.rationale},
            ensure_ascii=False,
            sort_keys=True,
        )


def _parse_result(payload: Any) -> ScoreResult | None:
    """Validate the Ollama response envelope and score JSON."""
    if not isinstance(payload, dict) or not isinstance(payload.get("response"), str):
        LOGGER.warning("Ollama scorer response has no text result")
        return None
    try:
        result = json.loads(payload["response"])
    except json.JSONDecodeError as exc:
        LOGGER.warning("Ollama scorer returned invalid JSON: %s", exc)
        return None
    if not isinstance(result, dict) or set(result) != {"score", "rationale"}:
        LOGGER.warning("Ollama scorer JSON does not match the required fields")
        return None
    score = result["score"]
    rationale = result["rationale"]
    if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 100:
        LOGGER.warning("Ollama scorer returned a score outside 0-100")
        return None
    if not isinstance(rationale, str) or not rationale.strip():
        LOGGER.warning("Ollama scorer returned an empty or invalid rationale")
        return None
    return ScoreResult(score=score, rationale=rationale.strip())


def score_job(
    job: Job,
    profile_text: str,
    model: str,
    ollama_url: str,
    timeout_seconds: float = 60.0,
) -> ScoreResult | None:
    """Score one filtered job using Ollama and validate the returned JSON."""
    prompt = (
        "Evaluate how well this job matches the candidate profile for an early-career "
        "internship or entry-level role. Consider role alignment, skills, experience "
        "level, and location. Return only JSON matching the provided schema, with a "
        "score from 0 to 100 and a concise rationale.\n\n"
        f"CANDIDATE PROFILE:\n{profile_text}\n\n"
        f"JOB:\nTitle: {job.title}\nCompany: {job.company or ''}\n"
        f"Location: {job.location or ''}\nDescription:\n{job.raw_text}"
    )
    try:
        response = requests.post(
            f"{ollama_url.rstrip('/')}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "format": SCORE_SCHEMA,
                "stream": False,
                "options": {"temperature": 0},
            },
            timeout=timeout_seconds,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        LOGGER.warning("Ollama scoring request failed for %s: %s", job.url, exc)
        return None

    try:
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        LOGGER.warning("Ollama scoring response was not valid JSON for %s: %s", job.url, exc)
        return None
    return _parse_result(payload)
