import json
import logging
import math
from dataclasses import dataclass
from typing import Iterable

import requests

from .models import Job


LOGGER = logging.getLogger(__name__)

INCLUDE_KEYWORDS = (
    "ml intern",
    "machine learning intern",
    "devops intern",
    "devops",
    "fde intern",
    "forward deployed engineer intern",
    "sde intern",
    "software engineering intern",
    "software engineer intern",
    "ui/ux designer",
    "ui ux designer",
    "product designer",
    "graphic designer",
    "visual designer",
    "interaction designer",
    "winforms",
    "wpf",
    "desktop developer",
    "c# developer",
    "windows developer",
)

EXCLUDE_KEYWORDS = (
    "senior",
    "staff",
    "principal",
    "lead",
    "manager",
    "director",
    "architect",
    "5+ years",
    "7+ years",
    "10+ years",
)

INDIAN_LOCATIONS = (
    "bengaluru",
    "bangalore",
    "hyderabad",
    "chennai",
    "pune",
    "delhi",
    "delhi ncr",
    "ncr",
    "mumbai",
    "india",
)


@dataclass(frozen=True)
class FilterDecision:
    """Result of filtering one collected job."""

    survives: bool | None
    similarity: float
    reason: str
    rank: int


def _text(job: Job) -> str:
    return "\n".join((job.title, job.location or "", job.raw_text)).lower()


def strong_title_match(job: Job) -> tuple[bool, int]:
    """Return whether a title strongly matches and its ranking tier."""
    title = job.title.lower()
    tech = (
        "ml intern",
        "machine learning intern",
        "devops intern",
        "fde intern",
        "forward deployed engineer intern",
        "sde intern",
        "software engineering intern",
        "software engineer intern",
    )
    design = ("ui/ux designer", "ui ux designer", "product designer")
    windows = ("winforms", "wpf", "desktop developer", "c# developer", "windows developer")
    visual = ("graphic designer", "visual designer", "interaction designer")

    if any(keyword in title for keyword in tech):
        return True, 1
    if any(keyword in title for keyword in design):
        return True, 2
    if any(keyword in title for keyword in windows):
        return True, 3
    if any(keyword in title for keyword in visual):
        return True, 4
    if any(keyword in title for keyword in INCLUDE_KEYWORDS):
        return True, 5
    return False, 0


def cosine_similarity(left: list[float], right: list[float]) -> float:
    """Calculate cosine similarity between two embedding vectors."""
    if len(left) != len(right) or not left:
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


def embed_text(text: str, backend: str, model: str, ollama_url: str) -> list[float] | None:
    """Embed text with Ollama or a locally available sentence-transformers backend."""
    if backend == "ollama":
        try:
            response = requests.post(
                f"{ollama_url.rstrip('/')}/api/embeddings",
                json={"model": model, "prompt": text},
                timeout=20,
            )
            response.raise_for_status()
            payload = response.json()
            embedding = payload.get("embedding")
            if isinstance(embedding, list) and all(
                isinstance(value, (int, float)) for value in embedding
            ):
                return [float(value) for value in embedding]
        except requests.RequestException as exc:
            LOGGER.warning("Embeddings are unreachable: %s", exc)
        except (ValueError, json.JSONDecodeError) as exc:
            LOGGER.warning("Ollama returned invalid embedding data: %s", exc)
        return None

    if backend == "sentence_transformers":
        try:
            from sentence_transformers import SentenceTransformer

            encoder = SentenceTransformer(model, device="cpu")
            return encoder.encode(text, normalize_embeddings=True).tolist()
        except (ImportError, OSError, RuntimeError) as exc:
            LOGGER.warning("sentence-transformers backend unavailable: %s", exc)
            return None

    LOGGER.warning("Unsupported embedding backend: %s", backend)
    return None


def filter_job(
    job: Job,
    profile_text: str = "",
    backend: str = "ollama",
    model: str = "nomic-embed-text",
    ollama_url: str = "http://localhost:11434",
    threshold: float = 0.35,
) -> FilterDecision:
    """Apply cheap title, work-type, experience, and location filtering."""
    text = _text(job)
    title = job.title.lower()

    for keyword in EXCLUDE_KEYWORDS:
        if keyword in title:
            return FilterDecision(False, 0.0, f"excluded keyword: {keyword}", 0)

    for keyword in ("contract", "freelance", "unpaid", "volunteer"):
        if keyword in text:
            return FilterDecision(False, 0.0, f"excluded work type: {keyword}", 0)

    location = (job.location or "").lower()
    if "us-only" in location or "usa only" in location or "united states only" in location:
        return FilterDecision(False, 0.0, "remote US-only location", 0)
    if "eu-only" in location or "europe only" in location:
        return FilterDecision(False, 0.0, "remote EU-only location", 0)

    remote = "remote" in location or "worldwide" in location or "anywhere" in location
    on_site = "on-site" in location or "onsite" in location or "hybrid" in location
    if on_site and not remote:
        if not any(place in location for place in INDIAN_LOCATIONS):
            return FilterDecision(False, 0.0, "on-site/hybrid outside India", 0)
    if location and not remote and not on_site and not any(place in location for place in INDIAN_LOCATIONS):
        return FilterDecision(False, 0.0, "location outside accepted India/remote areas", 0)

    strong, rank = strong_title_match(job)
    if strong:
        return FilterDecision(True, 0.0, f"strong title match (rank {rank})", rank)

    profile_embedding = embed_text(profile_text, backend, model, ollama_url)
    job_embedding = embed_text(
        job.title + "\n" + (job.location or "") + "\n" + job.raw_text,
        backend,
        model,
        ollama_url,
    )
    if profile_embedding is None or job_embedding is None:
        return FilterDecision(None, 0.0, "embedding backend unavailable", 0)

    similarity = cosine_similarity(profile_embedding, job_embedding)
    if similarity >= threshold:
        return FilterDecision(True, similarity, f"embedding similarity >= {threshold}", 5)
    return FilterDecision(False, similarity, f"embedding similarity below {threshold}", 0)


def filter_jobs(
    jobs: Iterable[Job],
    profile_text: str,
    backend: str = "ollama",
    model: str = "nomic-embed-text",
    ollama_url: str = "http://localhost:11434",
    threshold: float = 0.35,
) -> list[tuple[Job, FilterDecision]]:
    """Filter a batch of collected jobs and return each job with its decision."""
    return [
        (
            job,
            filter_job(job, profile_text, backend, model, ollama_url, threshold),
        )
        for job in jobs
    ]
