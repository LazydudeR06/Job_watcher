import json
import logging
from dataclasses import dataclass, field
from typing import Any

import requests

from .models import Job


LOGGER = logging.getLogger(__name__)

SCORE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "minimum": 0, "maximum": 100},
        "reasons": {"type": "array", "items": {"type": "string"}},
        "missing_skills": {"type": "array", "items": {"type": "string"}},
        "apply_priority": {"type": "integer", "minimum": 1, "maximum": 4},
        "rationale": {"type": "string"},
    },
    "required": ["score", "reasons", "missing_skills", "apply_priority"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class ScoreResult:
    """Validated LLM job match score."""

    score: int
    rationale: str = ""
    reasons: list[str] = field(default_factory=list)
    missing_skills: list[str] = field(default_factory=list)
    apply_priority: int = 4

    def as_json(self) -> str:
        """Return the validated score as canonical JSON."""
        return json.dumps(
            {
                "score": self.score,
                "reasons": self.reasons,
                "missing_skills": self.missing_skills,
                "apply_priority": self.apply_priority,
                "rationale": self.rationale,
            },
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
    if not isinstance(result, dict):
        LOGGER.warning("Ollama scorer JSON does not match the required fields")
        return None

    legacy_result = set(result) == {"score", "rationale"}
    required_fields = {"score", "reasons", "missing_skills", "apply_priority"}
    allowed_fields = required_fields | {"rationale"}
    if not legacy_result and (
        not required_fields.issubset(result) or not set(result).issubset(allowed_fields)
    ):
        LOGGER.warning("Ollama scorer JSON does not match the required fields")
        return None

    score = result["score"]
    if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 100:
        LOGGER.warning("Ollama scorer returned a score outside 0-100")
        return None

    if legacy_result:
        rationale = result["rationale"]
        if not isinstance(rationale, str) or not rationale.strip():
            LOGGER.warning("Ollama scorer returned an empty or invalid rationale")
            return None
        return ScoreResult(score=score, rationale=rationale.strip())

    reasons = result["reasons"]
    missing_skills = result["missing_skills"]
    apply_priority = result["apply_priority"]
    if not isinstance(reasons, list) or not all(isinstance(item, str) for item in reasons):
        LOGGER.warning("Ollama scorer returned invalid reasons")
        return None
    if not isinstance(missing_skills, list) or not all(
        isinstance(item, str) for item in missing_skills
    ):
        LOGGER.warning("Ollama scorer returned invalid missing skills")
        return None
    if (
        isinstance(apply_priority, bool)
        or not isinstance(apply_priority, int)
        or not 1 <= apply_priority <= 4
    ):
        LOGGER.warning("Ollama scorer returned an invalid apply priority")
        return None
    rationale = result.get("rationale")
    if rationale is not None and not isinstance(rationale, str):
        LOGGER.warning("Ollama scorer returned an invalid rationale")
        return None
    if not rationale:
        rationale = "; ".join(reasons)
    return ScoreResult(
        score=score,
        rationale=rationale.strip(),
        reasons=reasons,
        missing_skills=missing_skills,
        apply_priority=apply_priority,
    )


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
        "level, and location. Rank roles in this exact order: ML, DevOps, FDE, and SDE "
        "internships highest; then UI/UX and product design; then C# / WinForms / WPF "
        "desktop roles; then graphic and visual design lowest. Set apply_priority to "
        "1 for the highest-priority group, 2 for the next group, 3 for the next group, "
        "and 4 for the lowest-priority group. Return only JSON matching the schema, "
        "with score from 0 to 100, reasons and missing_skills as string arrays, and "
        "apply_priority as an integer from 1 to 4.\n\n"
        f"CANDIDATE PROFILE:\n{profile_text}\n\n"
        f"JOB:\nTitle: {job.title}\nCompany: {job.company or ''}\n"
        f"Location: {job.location or ''}\nDescription:\n{job.raw_text}"
    )
    for attempt in range(2):
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
            LOGGER.warning(
                "Ollama scoring response was not valid JSON for %s: %s",
                job.url,
                exc,
            )
            continue

        result = _parse_result(payload)
        if result is not None:
            return result

    return None
