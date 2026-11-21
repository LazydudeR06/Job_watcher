"""Executable compliance checks for AUDIT_SPEC.md (one test per spec ID)."""

from __future__ import annotations

import csv
import copy
import hashlib
import importlib
import importlib.util
import inspect
import json
import logging
import math
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, get_type_hints
from zoneinfo import ZoneInfo

import pytest
import requests
import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jobwatcher import config as config_module
from jobwatcher import filter as filter_module
from jobwatcher import reports as reports_module
from jobwatcher.collectors.adzuna import AdzunaCollector
from jobwatcher.collectors.base import Collector
from jobwatcher.collectors.himalayas import HimalayasCollector
from jobwatcher.collectors.remotive import RemotiveCollector
from jobwatcher.collectors.remoteok import RemoteOKCollector
from jobwatcher.filter import FilterDecision, filter_job
from jobwatcher.models import Job
from jobwatcher.scorer import ScoreResult, score_job
from jobwatcher.storage import (
    get_pending_scores,
    initialize_storage,
    insert_jobs,
    update_filter_decision,
    update_job_score,
)


COLLECTORS = (RemotiveCollector, RemoteOKCollector, HimalayasCollector, AdzunaCollector)


def _implemented(condition: bool, missing: str) -> None:
    """Fail with the audit's required wording when a feature is missing."""
    assert condition, f"NOT IMPLEMENTED: {missing}"


def _job(
    title: str = "ML Intern",
    *,
    source: str = "remotive",
    source_job_id: str | None = None,
    location: str = "Remote worldwide",
    raw_text: str = "Build useful software.",
    url: str | None = None,
) -> Job:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return Job(
        source=source,
        source_job_id=source_job_id or slug,
        title=title,
        company="Example Co",
        location=location,
        url=url or f"https://example.test/jobs/{slug}",
        posted_at="2026-01-02T03:04:05Z",
        raw_text=raw_text,
    )


class _Response:
    def __init__(self, payload: Any = None, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def json(self) -> Any:
        return self.payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class _Session:
    def __init__(self, *responses: _Response) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> _Response:
        self.calls.append({"url": url, **kwargs})
        return self.responses.pop(0)


def _listing(source: str) -> dict[str, Any]:
    if source == "remotive":
        return {
            "id": 101,
            "title": "ML Intern",
            "company_name": "Example Co",
            "candidate_required_location": "Worldwide",
            "url": "https://jobs.example.test/remotive/101",
            "publication_date": "2026-01-02T03:04:05Z",
            "description": "Build models and evaluate results.",
        }
    if source == "remoteok":
        return {
            "id": 202,
            "position": "ML Intern",
            "company": "Example Co",
            "location": "Worldwide",
            "url": "https://remoteok.com/remote-jobs/202-ml-intern",
            "date": "2026-01-02T03:04:05Z",
            "description": "Build models and evaluate results.",
        }
    if source == "himalayas":
        return {
            "id": 303,
            "title": "ML Intern",
            "companyName": "Example Co",
            "locationRestrictions": [],
            "guid": "https://jobs.example.test/himalayas/303",
            "pubDate": "2026-01-02T03:04:05Z",
            "description": "Build models and evaluate results.",
        }
    return {
        "id": 404,
        "title": "ML Intern",
        "company": {"display_name": "Example Co"},
        "location": {"display_name": "Worldwide"},
        "redirect_url": "https://jobs.example.test/adzuna/404",
        "created": "2026-01-02T03:04:05Z",
        "description": "Build models and evaluate results.",
    }


def _minimal_config() -> dict[str, Any]:
    return {
        "sources": {
            "remotive": {"enabled": False, "feed_url": "https://example.test/remotive"},
            "remoteok": {"enabled": False, "feed_url": "https://example.test/remoteok"},
            "himalayas": {"enabled": False, "feed_url": "https://example.test/himalayas"},
            "adzuna": {
                "enabled": False,
                "app_id_env": "ADZUNA_APP_ID",
                "app_key_env": "ADZUNA_APP_KEY",
            },
        },
        "outbound": {"enabled": False, "discord_webhook_env": "DISCORD_WEBHOOK_URL"},
        "watcher": {"interval_seconds": 3600},
        "embeddings": {
            "backend": "ollama",
            "model": "nomic-embed-text",
            "ollama_url": "http://localhost:11434",
            "similarity_threshold": 0.35,
        },
        "scoring": {
            "backend": "ollama",
            "model": "qwen3:4b",
            "ollama_url": "http://localhost:11434",
            "timeout_seconds": 60,
        },
    }


def _configure_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    source_factories: dict[str, Callable[..., Any]] | None = None,
    sources: dict[str, bool] | None = None,
    filter_fn: Callable[..., Any] | None = None,
    score_fn: Callable[..., Any] | None = None,
    report_fn: Callable[..., Any] | None = None,
    config: dict[str, Any] | None = None,
    use_real_scorer: bool = False,
) -> Any:
    """Patch the runner to use only local paths, fakes, and supplied config."""
    from jobwatcher import run as runner

    monkeypatch.chdir(tmp_path)
    (tmp_path / "profile.md").write_text("Test candidate profile.", encoding="utf-8")
    selected_config = config or _minimal_config()
    for source_config in selected_config["sources"].values():
        source_config["enabled"] = False
    for source, enabled in (sources or {}).items():
        selected_config["sources"][source]["enabled"] = enabled
    monkeypatch.setattr(runner, "load_config", lambda: selected_config)
    monkeypatch.setattr(
        runner,
        "configure_logging",
        lambda verbose=False: logging.getLogger("jobwatcher"),
    )
    if filter_fn is not None:
        monkeypatch.setattr(runner, "filter_jobs", filter_fn)
    if score_fn is not None:
        monkeypatch.setattr(runner, "score_job", score_fn)
    elif not use_real_scorer:
        monkeypatch.setattr(runner, "score_job", lambda *args, **kwargs: None)
    if report_fn is not None:
        monkeypatch.setattr(runner, "generate_reports", report_fn)
    else:
        monkeypatch.setattr(runner, "generate_reports", lambda *args, **kwargs: [])

    attributes = {
        "remotive": "RemotiveCollector",
        "remoteok": "RemoteOKCollector",
        "himalayas": "HimalayasCollector",
        "adzuna": "AdzunaCollector",
    }
    for source, factory in (source_factories or {}).items():
        monkeypatch.setattr(runner, attributes[source], factory)
    return runner


def _source_files(root: Path) -> list[Path]:
    excluded = {
        ".git",
        ".pytest_cache",
        ".venv",
        "venv",
        "__pycache__",
        "data",
        "logs",
        "output",
    }
    return [
        path
        for path in root.rglob("*")
        if path.is_file()
        and not any(part in excluded for part in path.relative_to(root).parts)
        and path.name not in {".env", "jobs.db"}
        and path.suffix not in {".pyc", ".sqlite", ".sqlite3"}
    ]


def _tree_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in _source_files(root)
        if path.relative_to(root).as_posix() != "tests/test_spec_compliance.py"
    }


def _notifier_sender() -> Callable[..., Any]:
    """Load the assumed notifier API; tests document this unspecified interface."""
    try:
        module = importlib.import_module("jobwatcher.notifier")
    except ImportError:
        pytest.fail(
            "NOT IMPLEMENTED: Discord webhook notifier module "
            "(assumed API: jobwatcher.notifier.send_notifications)"
        )
    sender = getattr(module, "send_notifications", None)
    _implemented(
        callable(sender),
        "Discord notifier send_notifications(scored_jobs, webhook_url)",
    )
    return sender


def _scored_job(title: str, score: int, priority: Any = 1) -> dict[str, Any]:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return {
        "source": "remotive",
        "source_job_id": slug,
        "title": title,
        "company": "Example Co",
        "location": "Remote worldwide",
        "url": f"https://example.test/jobs/{slug}",
        "posted_at": "2026-01-02T03:04:05Z",
        "llm_score": score,
        "score_rationale": "Relevant skills and experience.",
        "reasons": ["Relevant skills"],
        "missing_skills": [],
        "apply_priority": priority,
        "scored_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def _priority_rank(value: Any) -> int:
    if isinstance(value, int):
        return value
    normalized = str(value).strip().lower()
    return {"high": 1, "medium": 2, "low": 3}.get(normalized, 99)


@pytest.fixture(autouse=True)
def _block_live_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make accidental real HTTP/Ollama requests fail immediately in this suite."""

    def blocked_request(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("Live HTTP is forbidden in spec-compliance tests")

    monkeypatch.setattr(requests.sessions.Session, "request", blocked_request)


def test_a1_remotive_fetch_normalizes_job_fields() -> None:
    """A1: Remotive fetch returns normalized fields and listing description."""
    listing = _listing("remotive")
    response = _Response({"jobs": [listing]})
    collector = RemotiveCollector(session=_Session(response), sleeper=lambda _: None)
    jobs = collector.fetch()
    _implemented(len(jobs) == 1, "Remotive fixture response returns one Job")
    job = jobs[0]
    description = json.loads(job.raw_text).get("description")
    assert (
        job.title,
        job.company,
        job.location,
        job.url,
        job.posted_at,
        description,
    ) == (
        listing["title"],
        listing["company_name"],
        listing["candidate_required_location"],
        listing["url"],
        listing["publication_date"],
        listing["description"],
    ), "NOT IMPLEMENTED: Remotive title/company/location/URL/description/posted time mapping"


def test_a2_remoteok_fetch_skips_metadata_item() -> None:
    """A2: RemoteOK fetch skips the first legal/metadata element."""
    listing = _listing("remoteok")
    collector = RemoteOKCollector(
        session=_Session(_Response([{"legal": "metadata"}, listing])),
        sleeper=lambda _: None,
    )
    jobs = collector.fetch()
    _implemented(len(jobs) == 1, "RemoteOK metadata item is skipped")
    job = jobs[0]
    assert (
        job.title == listing["position"]
        and job.company == listing["company"]
        and job.location == listing["location"]
        and job.posted_at == listing["date"]
        and json.loads(job.raw_text)["description"] == listing["description"]
    ), "NOT IMPLEMENTED: RemoteOK normalized job fields"
    assert job.url.endswith("/202-ml-intern")


def test_a3_himalayas_fetch_normalizes_job_fields() -> None:
    """A3: Himalayas fetch returns normalized fields and listing description."""
    listing = _listing("himalayas")
    collector = HimalayasCollector(
        session=_Session(_Response({"jobs": [listing]})),
        sleeper=lambda _: None,
    )
    jobs = collector.fetch()
    _implemented(len(jobs) == 1, "Himalayas fixture response returns one Job")
    job = jobs[0]
    assert (
        job.title == listing["title"]
        and job.company == listing["companyName"]
        and job.location == "Remote worldwide"
        and job.url == listing["guid"]
        and job.posted_at == listing["pubDate"]
        and json.loads(job.raw_text)["description"] == listing["description"]
    ), "NOT IMPLEMENTED: Himalayas normalized job fields"


def test_a4_adzuna_requires_both_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A4: Adzuna registers only with both env credentials and otherwise does nothing."""
    missing_session = _Session()
    empty_jobs = AdzunaCollector(
        None,
        None,
        session=missing_session,
        sleeper=lambda _: None,
    ).fetch()
    _implemented(not empty_jobs and not missing_session.calls, "missing Adzuna credentials disable requests")
    constructed: list[tuple[str | None, str | None]] = []

    class FakeAdzuna:
        def __init__(self, app_id: str, app_key: str, **kwargs: Any) -> None:
            constructed.append((app_id, app_key))

        def fetch(self) -> list[Job]:
            return []

    config = _minimal_config()
    config["sources"]["adzuna"]["enabled"] = True
    _configure_run(
        monkeypatch,
        tmp_path,
        source_factories={"adzuna": FakeAdzuna},
        sources={"adzuna": True},
        config=config,
    )
    monkeypatch.delenv("ADZUNA_APP_ID", raising=False)
    monkeypatch.delenv("ADZUNA_APP_KEY", raising=False)
    assert _configured_main(monkeypatch, tmp_path) == 0
    _implemented(not constructed, "Adzuna is not registered when credentials are empty")

    monkeypatch.setenv("ADZUNA_APP_ID", "test-app-id")
    monkeypatch.setenv("ADZUNA_APP_KEY", "test-app-key")
    assert _configured_main(monkeypatch, tmp_path) == 0
    assert constructed == [("test-app-id", "test-app-key")], (
        "NOT IMPLEMENTED: Adzuna requires both credentials before registration"
    )


def _configured_main(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> int:
    """Run the already-patched CLI once from its isolated test directory."""
    from jobwatcher import run as runner

    return runner.main(["--once"])


def test_a5_all_collectors_share_fetch_interface() -> None:
    """A5: Each collector implements the shared fetch() -> list[Job] interface."""
    expected = list[Job]
    for collector in COLLECTORS:
        _implemented(issubclass(collector, Collector), f"{collector.__name__} uses Collector")
        hints = get_type_hints(collector.fetch)
        assert hints.get("return") == expected, (
            f"NOT IMPLEMENTED: {collector.__name__}.fetch() -> list[Job]"
        )
        assert callable(getattr(collector, "fetch", None))


def test_a6_collector_failure_does_not_stop_later_sources(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A6: A failed collector is logged and later collectors still run."""
    calls: list[str] = []

    class Broken:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def fetch(self) -> list[Job]:
            calls.append("broken")
            raise requests.HTTPError("HTTP 500")

    class Healthy:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def fetch(self) -> list[Job]:
            calls.append("healthy")
            return []

    _configure_run(
        monkeypatch,
        tmp_path,
        source_factories={"remotive": Broken, "remoteok": Healthy},
        sources={"remotive": True, "remoteok": True},
    )
    with caplog.at_level(logging.ERROR):
        try:
            _configured_main(monkeypatch, tmp_path)
        except requests.HTTPError:
            pass
    logged = any("500" in record.getMessage() for record in caplog.records)
    assert calls == ["broken", "healthy"] and logged, (
        "NOT IMPLEMENTED: collector failure is logged and later sources still run"
    )


def test_a7_collectors_rate_limit_timeout_retry_and_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """A7: Every source spaces requests, times out at 20s, and retries 429/5xx once."""
    modules = (
        importlib.import_module("jobwatcher.collectors.remotive"),
        importlib.import_module("jobwatcher.collectors.remoteok"),
        importlib.import_module("jobwatcher.collectors.himalayas"),
        importlib.import_module("jobwatcher.collectors.adzuna"),
    )
    for module, collector_type in zip(modules, COLLECTORS):
        sleeps: list[float] = []
        session = _Session(_Response({"jobs": []}), _Response({"jobs": []}))
        if collector_type is AdzunaCollector:
            collector = collector_type("id", "key", session=session, sleeper=sleeps.append)
        else:
            collector = collector_type(session=session, sleeper=sleeps.append)
        with monkeypatch.context() as patch:
            patch.setattr(module.time, "monotonic", iter((0.0, 0.1, 1.1)).__next__)
            collector.fetch()
            collector.fetch()
        assert [call["timeout"] for call in session.calls] == [20.0, 20.0], (
            f"NOT IMPLEMENTED: {collector_type.__name__} uses a 20-second timeout"
        )
        _implemented(any(delay >= 0.9 for delay in sleeps), f"{collector_type.__name__} spaces requests")

        retry_sleeps: list[float] = []
        retry_session = _Session(_Response(status_code=503), _Response(status_code=503))
        if collector_type is AdzunaCollector:
            retrying = collector_type("id", "key", session=retry_session, sleeper=retry_sleeps.append)
        else:
            retrying = collector_type(session=retry_session, sleeper=retry_sleeps.append)
        retry_jobs = retrying.fetch()
        assert not retry_jobs and len(retry_session.calls) == 2 and 1.0 in retry_sleeps, (
            f"NOT IMPLEMENTED: {collector_type.__name__} retries once with backoff, "
            "then skips the source for that run"
        )


def test_b1_storage_deduplicates_by_url_and_source_id(tmp_path: Path) -> None:
    """B1: Duplicate URL or source ID is persisted only once."""
    connection = initialize_storage(tmp_path / "jobs.db")
    first = _job("ML Intern", source_job_id="one", url="https://example.test/one")
    same_url = _job("DevOps Intern", source_job_id="two", url=first.url)
    same_id = _job("SDE Intern", source_job_id="one", url="https://example.test/three")
    inserted = insert_jobs(connection, [first, same_url, same_id])
    count = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    connection.close()
    _implemented(len(inserted) == 1 and count == 1, "SQLite deduplicates URL and source ID")


def test_b2_only_new_jobs_are_filtered_and_scored_again(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """B2: A second run does not send previously stored jobs to filtering/scoring."""
    job = _job("ML Intern")
    filter_calls: list[str] = []
    score_calls: list[str] = []

    class Feed:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def fetch(self) -> list[Job]:
            return [job]

    def filter_fn(jobs: list[Job], *args: Any, **kwargs: Any):
        filter_calls.extend(item.url for item in jobs)
        return [(item, FilterDecision(True, 0.8, "matched", 1)) for item in jobs]

    def score_fn(candidate: Job, *args: Any, **kwargs: Any) -> ScoreResult:
        score_calls.append(candidate.url)
        return ScoreResult(80, "match")

    _configure_run(
        monkeypatch,
        tmp_path,
        source_factories={"remotive": Feed},
        sources={"remotive": True},
        filter_fn=filter_fn,
        score_fn=score_fn,
    )
    assert _configured_main(monkeypatch, tmp_path) == 0
    assert _configured_main(monkeypatch, tmp_path) == 0
    assert filter_calls == [job.url] and score_calls == [job.url], (
        "NOT IMPLEMENTED: only newly inserted jobs proceed through filtering and scoring"
    )


def test_b3_invalid_score_is_processed_and_not_retried(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """B3: A failed LLM JSON score is marked processed and not rescored next run."""
    job = _job("ML Intern")
    score_calls: list[str] = []

    class Feed:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def fetch(self) -> list[Job]:
            return [job]

    def score_fn(candidate: Job, *args: Any, **kwargs: Any) -> None:
        score_calls.append(candidate.url)
        return None

    _configure_run(
        monkeypatch,
        tmp_path,
        source_factories={"remotive": Feed},
        sources={"remotive": True},
        filter_fn=lambda jobs, *args, **kwargs: [
            (item, FilterDecision(True, 0.8, "matched", 1)) for item in jobs
        ],
        score_fn=score_fn,
    )
    assert _configured_main(monkeypatch, tmp_path) == 0
    assert _configured_main(monkeypatch, tmp_path) == 0
    connection = initialize_storage(tmp_path / "data" / "jobs.db")
    processed = connection.execute("SELECT processed FROM jobs").fetchone()[0]
    connection.close()
    assert len(score_calls) == 1 and processed == 1, (
        "NOT IMPLEMENTED: failed LLM JSON scoring is marked processed and not rescored"
    )


def test_b4_embedding_failure_stays_pending_and_retries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """B4: Embedding failure leaves a job pending, logs a warning, and retries later."""
    job = _job("Machine Learning Engineer")

    class Feed:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def fetch(self) -> list[Job]:
            return [job]

    post = pytest.MonkeyPatch()
    embedding_calls: list[int] = []

    def failed_embedding(*args: Any, **kwargs: Any) -> Any:
        embedding_calls.append(1)
        raise requests.Timeout("offline")

    post.setattr(
        filter_module.requests,
        "post",
        failed_embedding,
    )
    try:
        _configure_run(
            monkeypatch,
            tmp_path,
            source_factories={"remotive": Feed},
            sources={"remotive": True},
        )
        with caplog.at_level(logging.WARNING):
            _configured_main(monkeypatch, tmp_path)
            _configured_main(monkeypatch, tmp_path)
        connection = initialize_storage(tmp_path / "data" / "jobs.db")
        row = connection.execute("SELECT processed, survives FROM jobs").fetchone()
        connection.close()
        warning_logged = any(
            "embeddings are unreachable" in record.getMessage().lower()
            for record in caplog.records
        )
        assert row == (0, None) and len(embedding_calls) >= 4 and warning_logged, (
            "NOT IMPLEMENTED: embedding failure stays pending, retries next run, and logs a warning"
        )
    finally:
        post.undo()


def test_c1_all_required_role_titles_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """C1: Every specified title category passes the cheap filter."""
    titles = (
        "UI Designer",
        "UX Designer",
        "Product Designer",
        "Graphic Designer",
        "Visual Designer",
        "Interaction Designer",
        "WinForms Developer",
        "WPF Developer",
        "Desktop Developer",
        "C# Developer",
        "Windows Developer",
        "ML Intern",
        "DevOps Intern",
        "FDE Intern",
        "SDE Intern",
    )
    vectors = iter(([1.0, 0.0], [0.0, 1.0]) * len(titles))
    monkeypatch.setattr(filter_module, "embed_text", lambda *args, **kwargs: next(vectors))
    failed = [
        title
        for title in titles
        if not filter_job(_job(title)).survives
    ]
    assert not failed, f"NOT IMPLEMENTED: include keywords do not pass these titles: {failed}"


def test_c2_all_experience_exclusions_are_dropped() -> None:
    """C2: Seniority and 5+/7+/10+ years exclusion phrases are rejected."""
    phrases = (
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
    failed = [
        phrase
        for phrase in phrases
        if filter_job(_job(f"ML Intern - {phrase}")).survives
    ]
    assert not failed, f"NOT IMPLEMENTED: experience exclusions not dropped: {failed}"


def test_c3_backend_rule_preserves_devops_and_ml_but_drops_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C3: DevOps/ML interns pass; a generic Backend Engineer is dropped."""
    vectors = iter(([1.0, 0.0], [0.0, 1.0]))
    monkeypatch.setattr(filter_module, "embed_text", lambda *args, **kwargs: next(vectors))
    assert filter_job(_job("DevOps Intern")).survives
    assert filter_job(_job("ML Intern")).survives
    decision = filter_job(_job("Backend Engineer"))
    _implemented(not decision.survives, "Backend Engineer without an include match is dropped")


def test_c4_remote_and_supported_indian_locations() -> None:
    """C4: Worldwide remote and supported Indian hybrid/on-site roles pass."""
    locations = (
        "Remote worldwide",
        "Hybrid in Bengaluru",
        "On-site in Hyderabad",
        "Hybrid in Chennai",
        "On-site in Pune",
        "Hybrid in Delhi NCR",
        "On-site in Mumbai",
    )
    failed = [
        location
        for location in locations
        if not filter_job(_job("ML Intern", location=location)).survives
    ]
    _implemented(not failed, f"valid remote/India locations pass: {failed}")
    outside = filter_job(_job("ML Intern", location="On-site in London"))
    _implemented(not outside.survives, "on-site location outside India is rejected")


def test_c5_remote_us_and_us_eu_restrictions_are_dropped() -> None:
    """C5: US-only and must-be-in-US/EU remote jobs are rejected."""
    locations = ("Remote - US only", "Remote - must be in the US/EU")
    failed = [
        location
        for location in locations
        if filter_job(_job("ML Intern", location=location)).survives
    ]
    assert not failed, f"NOT IMPLEMENTED: restricted remote locations not dropped: {failed}"


def test_c6_work_types_keep_internships_and_entry_roles_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C6: Internship/entry-level full-time pass; contract/freelance/unpaid/volunteer fail."""
    monkeypatch.setattr(filter_module, "embed_text", lambda *args, **kwargs: [1.0, 0.0])
    internship = filter_job(_job("ML Intern"))
    entry_level = filter_job(
        _job("Entry-level Software Engineer", raw_text="Entry-level full-time role.")
    )
    _implemented(internship.survives and entry_level.survives, "internship and entry-level full-time pass")
    excluded = ("Contract", "Freelance", "Unpaid", "Volunteer")
    failed = [
        work_type
        for work_type in excluded
        if filter_job(_job(f"ML Intern - {work_type}")).survives
    ]
    assert not failed, f"NOT IMPLEMENTED: excluded work types pass: {failed}"


def test_c7_experience_limits_and_junior_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C7: 0-2 years passes; mid/senior fails unless the title looks junior."""
    monkeypatch.setattr(filter_module, "embed_text", lambda *args, **kwargs: [1.0, 0.0])
    assert filter_job(_job("ML Intern", raw_text="0-2 years of experience.")).survives
    middle = filter_job(
        _job("Mid-level Machine Learning Engineer", raw_text="Requires 5 years.")
    )
    senior = filter_job(_job("Senior ML Intern"))
    # Assumption: a junior title may pass even if the description uses seniority
    # language or asks for more years; the title is the deciding exception.
    junior_exception = filter_job(
        _job("Junior ML Intern", raw_text="Mid/senior role requiring 5+ years.")
    )
    assert not middle.survives and not senior.survives and junior_exception.survives, (
        "NOT IMPLEMENTED: 0-2-year rules, mid/senior rejection, and junior-title exception"
    )


def test_c8_embedding_threshold_and_strong_title_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C8: Similarity >= 0.35 passes, below fails, and strong titles override."""
    vectors = iter(([1.0, 0.0], [0.5, math.sqrt(0.75)], [1.0, 0.0], [0.0, 1.0]))
    monkeypatch.setattr(filter_module, "embed_text", lambda *args, **kwargs: next(vectors))
    at_or_above = filter_job(_job("Backend Engineer"), threshold=0.35)
    below = filter_job(_job("Backend Engineer"), threshold=0.35)
    strong = filter_job(_job("ML Intern"), threshold=0.35)
    assert at_or_above.survives and at_or_above.similarity >= 0.35, (
        "NOT IMPLEMENTED: embeddings at or above 0.35 pass"
    )
    assert not below.survives and below.similarity < 0.35, (
        "NOT IMPLEMENTED: embeddings below 0.35 are rejected"
    )
    _implemented(strong.survives, "strong title match passes despite low similarity")


def test_c9_config_threshold_changes_filter_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """C9: The configured similarity threshold changes the same job's decision."""
    monkeypatch.setattr(
        filter_module,
        "embed_text",
        lambda text, *args, **kwargs: (
            [1.0, 0.0]
            if text == "Test candidate profile."
            else [0.5, math.sqrt(0.75)]
        ),
    )
    config = config_module.load_config(ROOT / "config.yaml")
    low_config = copy.deepcopy(config)
    high_config = copy.deepcopy(config)
    low_config["embeddings"]["similarity_threshold"] = 0.35
    high_config["embeddings"]["similarity_threshold"] = 0.7
    job = _job("Backend Engineer")
    scored: list[float] = []

    class Feed:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def fetch(self) -> list[Job]:
            return [job]

    def score_fn(candidate: Job, *args: Any, **kwargs: Any) -> ScoreResult:
        scored.append(candidate.url)
        return ScoreResult(80, "match")

    low_dir = tmp_path / "low-threshold"
    high_dir = tmp_path / "high-threshold"
    low_dir.mkdir()
    high_dir.mkdir()
    _configure_run(
        monkeypatch,
        low_dir,
        source_factories={"remotive": Feed},
        sources={"remotive": True},
        score_fn=score_fn,
        config=low_config,
    )
    from jobwatcher import run as runner

    assert runner.main(["--once"]) == 0
    _configure_run(
        monkeypatch,
        high_dir,
        source_factories={"remotive": Feed},
        sources={"remotive": True},
        score_fn=score_fn,
        config=high_config,
    )
    assert runner.main(["--once"]) == 0
    assert scored == [job.url], (
        "NOT IMPLEMENTED: changing embeddings.similarity_threshold changes the result"
    )


def test_c10_embedding_backend_is_configurable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """C10: Ollama/nomic-embed-text is default and sentence-transformers is selectable."""
    config = config_module.load_config(ROOT / "config.yaml")
    assert config["embeddings"]["backend"] == "ollama"
    assert config["embeddings"]["model"] == "nomic-embed-text"
    selectable = copy.deepcopy(config)
    selectable["embeddings"]["backend"] = "sentence_transformers"
    selectable_path = tmp_path / "sentence-transformers.yaml"
    selectable_path.write_text(yaml.safe_dump(selectable), encoding="utf-8")
    _implemented(
        config_module.load_config(selectable_path)["embeddings"]["backend"]
        == "sentence_transformers",
        "sentence_transformers is an accepted config backend",
    )
    fake_calls: list[tuple[Any, ...]] = []

    class FakeEncoder:
        def __init__(self, model: str, device: str) -> None:
            fake_calls.append((model, device))

        def encode(self, text: str, normalize_embeddings: bool) -> Any:
            fake_calls.append((text, normalize_embeddings))
            return SimpleNamespace(tolist=lambda: [0.1, 0.2])

    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        SimpleNamespace(SentenceTransformer=FakeEncoder),
    )
    embedding = filter_module.embed_text(
        "candidate text",
        backend="sentence_transformers",
        model="local-model",
        ollama_url="http://unused.test",
    )
    _implemented(embedding == [0.1, 0.2], "sentence_transformers backend is selectable")
    assert fake_calls == [("local-model", "cpu"), ("candidate text", True)]


def test_d1_only_filter_survivors_are_scored(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """D1: Rejected jobs are never passed to the mocked LLM scorer."""
    survivor = _job("ML Intern", source_job_id="survivor")
    rejected = _job("Backend Engineer", source_job_id="rejected")
    scored: list[str] = []

    class Feed:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def fetch(self) -> list[Job]:
            return [survivor, rejected]

    def filter_fn(jobs: list[Job], *args: Any, **kwargs: Any):
        return [
            (item, FilterDecision(item == survivor, 0.8, "decision", 1 if item == survivor else 0))
            for item in jobs
        ]

    def score_fn(candidate: Job, *args: Any, **kwargs: Any) -> ScoreResult:
        scored.append(candidate.url)
        return ScoreResult(80, "match")

    _configure_run(
        monkeypatch,
        tmp_path,
        source_factories={"remotive": Feed},
        sources={"remotive": True},
        filter_fn=filter_fn,
        score_fn=score_fn,
    )
    assert _configured_main(monkeypatch, tmp_path) == 0
    assert scored == [survivor.url], (
        "NOT IMPLEMENTED: only jobs that passed the cheap filter are sent to the LLM"
    )


def test_d2_scoring_model_and_url_come_from_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """D2: Configured scoring model and Ollama URL are passed to score_job."""
    job = _job("ML Intern")
    captured: list[tuple[Any, ...]] = []

    class Feed:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def fetch(self) -> list[Job]:
            return [job]

    config = _minimal_config()
    config["sources"]["remotive"]["enabled"] = True
    config["scoring"].update({"model": "configured-model", "ollama_url": "http://ollama.test:1234"})

    def score_fn(candidate: Job, profile: str, **kwargs: Any) -> ScoreResult:
        captured.append((kwargs.get("model"), kwargs.get("ollama_url")))
        return ScoreResult(80, "match")

    _configure_run(
        monkeypatch,
        tmp_path,
        source_factories={"remotive": Feed},
        sources={"remotive": True},
        filter_fn=lambda jobs, *args, **kwargs: [
            (item, FilterDecision(True, 0.8, "matched", 1)) for item in jobs
        ],
        score_fn=score_fn,
        config=config,
    )
    assert _configured_main(monkeypatch, tmp_path) == 0
    assert captured == [("configured-model", "http://ollama.test:1234")], (
        "NOT IMPLEMENTED: model and Ollama URL are read from scoring config"
    )


def test_d3_scorer_parses_reasons_skills_and_priority(monkeypatch: pytest.MonkeyPatch) -> None:
    """D3: Valid scoring JSON parses score, reasons, missing skills, and priority."""
    result_payload = {
        "score": 88,
        "reasons": ["Relevant ML experience", "Strong Python"],
        "missing_skills": ["Kubernetes"],
        "apply_priority": 1,
    }

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, str]:
            return {"response": json.dumps(result_payload)}

    monkeypatch.setattr("jobwatcher.scorer.requests.post", lambda *args, **kwargs: Response())
    result = score_job(_job(), "Candidate profile", "model", "http://ollama.test")
    assert result is not None, "NOT IMPLEMENTED: scorer accepts required valid JSON schema"
    assert (
        getattr(result, "score", None) == 88
        and getattr(result, "reasons", None) == result_payload["reasons"]
        and getattr(result, "missing_skills", None) == result_payload["missing_skills"]
        and getattr(result, "apply_priority", None) == 1
    ), "NOT IMPLEMENTED: scorer parses reasons, missing_skills, and apply_priority"


def test_d4_invalid_json_retries_once_then_marks_processed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """D4: Invalid scoring JSON is retried once and then removed from pending scores."""
    job = _job("ML Intern")
    calls: list[int] = []

    class Feed:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def fetch(self) -> list[Job]:
            return [job]

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, str]:
            return {"response": "not valid json"}

    def fake_post(*args: Any, **kwargs: Any) -> Response:
        calls.append(1)
        return Response()

    monkeypatch.setattr("jobwatcher.scorer.requests.post", fake_post)
    _configure_run(
        monkeypatch,
        tmp_path,
        source_factories={"remotive": Feed},
        sources={"remotive": True},
        filter_fn=lambda jobs, *args, **kwargs: [
            (item, FilterDecision(True, 0.8, "matched", 1)) for item in jobs
        ],
        use_real_scorer=True,
    )
    assert _configured_main(monkeypatch, tmp_path) == 0
    connection = initialize_storage(tmp_path / "data" / "jobs.db")
    pending = get_pending_scores(connection)
    processed = connection.execute("SELECT processed FROM jobs").fetchone()[0]
    connection.close()
    assert len(calls) == 2 and not pending and processed == 1, (
        "NOT IMPLEMENTED: invalid JSON gets exactly one retry, then is marked processed"
    )


def test_d5_prompt_and_apply_priority_follow_role_ranking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D5: Prompt and parsed priority rank tech, design, Windows, then graphic roles."""
    prompts: list[str] = []
    responses = iter(
        [
            {"score": 90, "reasons": ["fit"], "missing_skills": [], "apply_priority": 1},
            {"score": 85, "reasons": ["fit"], "missing_skills": [], "apply_priority": 2},
            {"score": 80, "reasons": ["fit"], "missing_skills": [], "apply_priority": 3},
            {"score": 70, "reasons": ["fit"], "missing_skills": [], "apply_priority": 4},
        ]
    )

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, str]:
            return {"response": json.dumps(next(responses))}

    def fake_post(url: str, json: dict[str, Any], **kwargs: Any) -> Response:
        prompts.append(json["prompt"])
        return Response()

    monkeypatch.setattr("jobwatcher.scorer.requests.post", fake_post)
    titles = ("DevOps Intern", "Product Designer", "WPF Developer", "Graphic Designer")
    results = [score_job(_job(title), "Profile", "model", "http://ollama.test") for title in titles]
    _implemented(all(result is not None for result in results), "scorer returns role priority")
    priorities = [_priority_rank(getattr(result, "apply_priority", None)) for result in results]
    assert priorities == sorted(priorities) and priorities == [1, 2, 3, 4], (
        "NOT IMPLEMENTED: apply_priority follows the required role ranking"
    )
    prompt = prompts[0].lower()
    rank_terms = ("ml", "devops", "fde", "sde", "ui/ux", "product design", "c#", "winforms", "wpf", "graphic")
    positions = [prompt.find(term) for term in rank_terms]
    assert all(position >= 0 for position in positions) and positions == sorted(positions), (
        "NOT IMPLEMENTED: scoring prompt states the required role ranking"
    )


def test_d6_profile_is_included_in_scoring_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    """D6: profile.md content is included in or used by the scoring step."""
    marker = "UNIQUE_PROFILE_SKILL_MARKER"
    prompts: list[str] = []

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, str]:
            return {"response": '{"score": 80, "rationale": "match"}'}

    monkeypatch.setattr(
        "jobwatcher.scorer.requests.post",
        lambda url, json, **kwargs: (prompts.append(json["prompt"]) or Response()),
    )
    score_job(_job(), marker, "model", "http://ollama.test")
    _implemented(bool(prompts) and marker in prompts[0], "profile.md content is included in scoring")


def _report_rows() -> list[dict[str, Any]]:
    return [
        _scored_job("ML Intern", 60, 1),
        _scored_job("Graphic Designer", 59, 4),
    ]


def test_e1_daily_digest_path_timezone_threshold_and_fields(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """E1: Kolkata-dated daily Markdown includes only >=60 jobs and required fields."""
    rows = _report_rows()
    monkeypatch.setattr(reports_module, "get_scored_jobs", lambda connection, prefix: rows)
    now = datetime(2026, 11, 21, 20, 0, tzinfo=timezone.utc)
    expected_date = now.astimezone(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d")
    generated = reports_module.generate_reports(object(), tmp_path / "output", now=now)
    digest = tmp_path / "output" / f"{expected_date}.md"
    _implemented(digest.is_file(), "daily digest uses output/YYYY-MM-DD.md in Asia/Kolkata")
    text = digest.read_text(encoding="utf-8")
    required = ("ML Intern", "Example Co", "Remote worldwide", "60", "Relevant skills", "https://example.test/jobs/ml-intern")
    assert all(field in text for field in required), (
        "NOT IMPLEMENTED: digest includes title/company/location/score/reasons/URL"
    )
    assert "Graphic Designer" not in text, "NOT IMPLEMENTED: digest excludes scores below 60"
    assert digest in generated


def test_e2_daily_csv_contains_same_qualified_jobs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """E2: Daily CSV is written at output/YYYY-MM-DD.csv with the digest's jobs."""
    rows = _report_rows()
    monkeypatch.setattr(reports_module, "get_scored_jobs", lambda connection, prefix: rows)
    now = datetime(2026, 11, 21, 20, 0, tzinfo=timezone.utc)
    expected_date = now.astimezone(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d")
    generated = reports_module.generate_reports(object(), tmp_path / "output", now=now)
    target = tmp_path / "output" / f"{expected_date}.csv"
    _implemented(target.is_file(), "daily CSV uses output/YYYY-MM-DD.csv")
    with target.open(encoding="utf-8-sig", newline="") as handle:
        rows_out = list(csv.DictReader(handle))
    assert len(rows_out) == 1 and rows_out[0]["title"] == "ML Intern", (
        "NOT IMPLEMENTED: daily CSV contains the same jobs meeting the score threshold"
    )
    assert target in generated


def test_e3_daily_digest_merges_unique_jobs_across_runs(tmp_path: Path) -> None:
    """E3: Repeated same-day generation merges unique jobs without duplicates."""
    connection = initialize_storage(tmp_path / "jobs.db")
    first = _job("ML Intern", source_job_id="one")
    second = _job("DevOps Intern", source_job_id="two")
    now = datetime.now().astimezone()
    insert_jobs(connection, [first, first])
    update_filter_decision(connection, first, True, 0.8, "matched")
    update_job_score(connection, first, 80, "first", '{"score":80}')
    root = tmp_path / "output"
    reports_module.generate_reports(connection, root, now=now)
    insert_jobs(connection, [second])
    update_filter_decision(connection, second, True, 0.8, "matched")
    update_job_score(connection, second, 85, "second", '{"score":85}')
    reports_module.generate_reports(connection, root, now=now)
    connection.close()
    digest = root / "digests" / f"{now:%Y-%m-%d}.md"
    text = digest.read_text(encoding="utf-8")
    assert text.count("## ML Intern") == 1 and text.count("## DevOps Intern") == 1, (
        "NOT IMPLEMENTED: daily digest merges unique jobs without duplicate entries"
    )


def test_e4_hourly_markdown_contains_only_jobs_new_this_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """E4: Hourly Markdown lists only the jobs newly collected in that run."""
    # Assumption: generate_reports(..., new_jobs=...) is the report boundary for
    # run-local jobs; the existing interface has no new-jobs argument.
    generate = reports_module.generate_reports
    signature = inspect.signature(generate)
    _implemented(
        "new_jobs" in signature.parameters,
        "hourly Markdown output has a way to receive only jobs new in this run",
    )
    first = _scored_job("Earlier ML Intern", 80)
    second = _scored_job("New DevOps Intern", 90)
    captured: list[tuple[Any, ...]] = []

    def fake_get_scored(connection: Any, prefix: str) -> list[dict[str, Any]]:
        return [first, second]

    monkeypatch.setattr(reports_module, "get_scored_jobs", fake_get_scored)
    now = datetime.now().astimezone()
    generated = reports_module.generate_reports(
        object(),
        tmp_path / "output",
        now=now,
        new_jobs=[second],
    )
    target = tmp_path / "output" / "hourly" / f"{now:%Y-%m-%d_%H}.md"
    _implemented(target.is_file(), "hourly update is output/hourly/YYYY-MM-DD_HH.md")
    text = target.read_text(encoding="utf-8")
    assert "New DevOps Intern" in text and "Earlier ML Intern" not in text
    assert target in generated


def test_e5_no_new_jobs_writes_no_hourly_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """E5: A run with no newly collected jobs does not write an hourly file."""
    signature = inspect.signature(reports_module.generate_reports)
    _implemented(
        "new_jobs" in signature.parameters,
        "hourly report generation tracks jobs newly collected in the current run",
    )
    job = _job("ML Intern")
    connection = initialize_storage(tmp_path / "jobs.db")
    insert_jobs(connection, [job])
    update_filter_decision(connection, job, True, 0.8, "matched")
    update_job_score(connection, job, 80, "match", '{"score":80}')
    monkeypatch.setattr(reports_module, "get_scored_jobs", lambda connection, prefix: [_scored_job("Existing", 80)])
    now = datetime.now().astimezone()
    reports_module.generate_reports(
        connection,
        tmp_path / "output",
        now=now,
        new_jobs=[],
    )
    connection.close()
    hourly = tmp_path / "output" / "hourly"
    assert not hourly.exists() or not list(hourly.iterdir()), (
        "NOT IMPLEMENTED: no hourly file is written when the run finds nothing new"
    )


def test_e6_report_thresholds_are_config_values() -> None:
    """E6: Digest and notification thresholds 60 and 75 are configurable."""
    config = config_module.load_config(ROOT / "config.yaml")
    # Assumption: reports.digest_min_score and notifications.notification_min_score
    # are the config names for the thresholds because AUDIT_SPEC.md omits key names.
    report = config.get("reports", {})
    notifications = config.get("notifications", config.get("outbound", {}))
    _implemented(
        report.get("digest_min_score") == 60
        and notifications.get("notification_min_score", notifications.get("min_score")) == 75,
        "config.yaml defines 60-point digest and 75-point notification thresholds",
    )


def test_f1_discord_notifier_sends_only_jobs_at_or_above_75(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F1: Discord webhook notifier sends only jobs scoring at least 75."""
    sender = _notifier_sender()
    posts: list[dict[str, Any]] = []

    class Response:
        def raise_for_status(self) -> None:
            return None

    monkeypatch.setattr(
        requests,
        "post",
        lambda url, **kwargs: (posts.append({"url": url, **kwargs}) or Response()),
    )
    sender([_scored_job("Below", 74), _scored_job("At Threshold", 75)], "https://discord.test/hook")
    payloads = [json.dumps(post.get("json", post.get("data", ""))) for post in posts]
    assert any("At Threshold" in payload for payload in payloads) and not any(
        '"Below"' in payload for payload in payloads
    ), "NOT IMPLEMENTED: notifier enforces the 75-point threshold"


def test_f2_discord_messages_batch_ten_and_include_required_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F2: Discord messages contain at most 10 complete job entries each."""
    sender = _notifier_sender()
    posts: list[dict[str, Any]] = []

    class Response:
        def raise_for_status(self) -> None:
            return None

    monkeypatch.setattr(
        requests,
        "post",
        lambda url, **kwargs: (posts.append(kwargs) or Response()),
    )
    jobs = [_scored_job(f"Job {index}", 80, index % 4 + 1) for index in range(11)]
    sender(jobs, "https://discord.test/hook")
    payloads = [json.dumps(post.get("json", post.get("data", ""))) for post in posts]
    assert len(posts) == 2, "NOT IMPLEMENTED: webhook limits each message to 10 jobs"
    combined = "\n".join(payloads)
    for field in ("Job 0", "Example Co", "80", "apply_priority", "https://example.test/jobs/job-0"):
        assert field in combined, f"NOT IMPLEMENTED: Discord message includes {field}"


def test_f3_empty_discord_webhook_disables_http_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F3: Empty DISCORD_WEBHOOK_URL turns notifier off without HTTP."""
    sender = _notifier_sender()
    called = False

    def fake_post(*args: Any, **kwargs: Any) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(requests, "post", fake_post)
    sender([_scored_job("ML Intern", 90)], "")
    assert not called, "NOT IMPLEMENTED: empty DISCORD_WEBHOOK_URL disables notifications"


def test_f4_webhook_failure_is_logged_and_does_not_crash(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """F4: Webhook HTTP failure is logged without escaping the notifier."""
    sender = _notifier_sender()

    class FailedResponse:
        def raise_for_status(self) -> None:
            raise requests.HTTPError("webhook unavailable")

    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: FailedResponse())
    with caplog.at_level(logging.WARNING):
        try:
            sender([_scored_job("ML Intern", 90)], "https://discord.test/hook")
        except requests.RequestException:
            pytest.fail("NOT IMPLEMENTED: webhook failure must not crash the run")
    _implemented(
        any("webhook" in record.getMessage().lower() for record in caplog.records),
        "webhook failure is logged",
    )


def test_f5_no_active_telegram_code_path() -> None:
    """F5: No Telegram integration is active in the package by default."""
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "jobwatcher").rglob("*.py")
    )
    active_patterns = (
        r"^\s*(?:from|import)\s+telegram\b",
        r"\btelegram\.Bot\b",
        r"\bTELEGRAM_BOT_TOKEN\b",
        r"api\.telegram\.org",
    )
    matches = [pattern for pattern in active_patterns if re.search(pattern, source, re.I | re.M)]
    _implemented(not matches, f"no active Telegram integration (found {matches})")


def test_g1_once_runs_one_cycle_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """G1: run.py --once runs one full cycle and exits with status 0."""
    fetched: list[int] = []

    class Feed:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def fetch(self) -> list[Job]:
            fetched.append(1)
            return []

    runner = _configure_run(
        monkeypatch,
        tmp_path,
        source_factories={"remotive": Feed},
        sources={"remotive": True},
    )
    assert runner.main(["--once"]) == 0 and fetched == [1], (
        "NOT IMPLEMENTED: --once executes one collection cycle and exits 0"
    )


def test_g2_loop_mode_defaults_to_one_hour(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """G2: Loop mode exists and its configured default interval is 3600 seconds."""
    from jobwatcher import run as runner

    config = config_module.load_config(ROOT / "config.yaml")
    parser = runner.build_parser()
    options = {option for action in parser._actions for option in action.option_strings}

    class Feed:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def fetch(self) -> list[Job]:
            return []

    _configure_run(
        monkeypatch,
        tmp_path,
        source_factories={"remotive": Feed},
        sources={"remotive": True},
        config=config,
    )
    scheduled: list[int] = []
    monkeypatch.setattr(runner, "run_forever", lambda task, interval: scheduled.append(interval))
    run_result = runner.main([])
    assert (
        "--interval" in options
        and config["watcher"]["interval_seconds"] == 3600
        and run_result == 0
        and scheduled == [3600]
    ), (
        "NOT IMPLEMENTED: loop mode exists and defaults to a one-hour interval"
    )


def test_g3_config_loads_and_missing_key_has_clear_error(tmp_path: Path) -> None:
    """G3: Config loads and missing required keys produce clear ConfigError."""
    loaded = config_module.load_config(ROOT / "config.yaml")
    _implemented(isinstance(loaded.get("sources"), dict), "config.yaml loads")
    broken = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    del broken["scoring"]["model"]
    path = tmp_path / "missing-key.yaml"
    path.write_text(yaml.safe_dump(broken), encoding="utf-8")
    try:
        config_module.load_config(path)
    except config_module.ConfigError as exc:
        assert "scoring.model" in str(exc), (
            "NOT IMPLEMENTED: missing required config key names the key in its error"
        )
    else:
        pytest.fail("NOT IMPLEMENTED: missing required config key raises clear ConfigError")


def test_g4_secrets_load_from_env_and_are_not_hardcoded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """G4: Secrets load from .env and no credentials are embedded in source."""
    from dotenv import load_dotenv

    env_values = {
        "ADZUNA_APP_ID": "audit-app-id",
        "ADZUNA_APP_KEY": "audit-app-key",
        "DISCORD_WEBHOOK_URL": "https://hooks.test/audit",
    }
    (tmp_path / ".env").write_text(
        "\n".join(f"{key}={value}" for key, value in env_values.items()),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    for key in env_values:
        monkeypatch.delenv(key, raising=False)
    loader_calls: list[bool] = []

    def load_test_env() -> None:
        loader_calls.append(True)
        load_dotenv(tmp_path / ".env", override=True)

    monkeypatch.setattr(config_module, "load_dotenv", load_test_env)
    config_module.load_config(ROOT / "config.yaml")
    _implemented(
        loader_calls and all(os.getenv(key) == value for key, value in env_values.items()),
        "load_config loads secrets from .env",
    )
    # Public API endpoints and localhost defaults are valid literals. This check
    # targets embedded credentials/tokens and credential-bearing URL literals.
    credential_patterns = (
        r"(?:ADZUNA_APP_ID|ADZUNA_APP_KEY|DISCORD_WEBHOOK_URL)\s*=\s*['\"][^'\"]{4,}",
        r"\b(?:sk|ghp|gho|xoxb)-[A-Za-z0-9_-]{16,}\b",
        r"https?://[^/\s:@]+:[^/@\s]+@",
        r"https://hooks\.discord\.com/api/webhooks/\d+/[A-Za-z0-9_-]{16,}",
    )
    matches: list[str] = []
    for path in _source_files(ROOT):
        if path.suffix != ".py":
            continue
        content = path.read_text(encoding="utf-8")
        if any(re.search(pattern, content) for pattern in credential_patterns):
            matches.append(path.relative_to(ROOT).as_posix())
    assert not matches, f"NOT IMPLEMENTED: secret/token literals in source: {matches}"


def test_g5_verbose_controls_debug_level_and_default_is_info(
    tmp_path: Path,
) -> None:
    """G5: Default logging is INFO and --verbose switches to DEBUG."""
    from jobwatcher.logging_config import configure_logging

    logger = configure_logging(False, tmp_path / "default.log")
    default_level = logger.level
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
    logger = configure_logging(True, tmp_path / "verbose.log")
    verbose_level = logger.level
    try:
        assert default_level == logging.INFO and verbose_level == logging.DEBUG, (
            "NOT IMPLEMENTED: --verbose switches logger level from INFO to DEBUG"
        )
    finally:
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
            handler.close()


def test_g6_rotating_log_handler_uses_expected_file(tmp_path: Path) -> None:
    """G6: A rotating file handler is configured for logs/jobwatcher.log."""
    from logging.handlers import TimedRotatingFileHandler

    from jobwatcher.logging_config import configure_logging

    log_path = tmp_path / "logs" / "jobwatcher.log"
    logger = configure_logging(False, log_path)
    try:
        default_path = inspect.signature(configure_logging).parameters["log_path"].default
        handlers = [handler for handler in logger.handlers if isinstance(handler, TimedRotatingFileHandler)]
        _implemented(
            len(handlers) == 1
            and Path(handlers[0].baseFilename) == log_path
            and handlers[0].when == "MIDNIGHT"
            and str(default_path).replace("\\", "/") == "logs/jobwatcher.log",
            "logs/jobwatcher.log uses a timed rotating handler",
        )
    finally:
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
            handler.close()


def test_g7_report_names_use_asia_kolkata_timezone(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """G7: Daily report names use Asia/Kolkata date rather than host local date."""
    rows = [_scored_job("ML Intern", 80)]
    monkeypatch.setattr(reports_module, "get_scored_jobs", lambda connection, prefix: rows)
    now = datetime(2026, 11, 21, 20, 0, tzinfo=timezone.utc)
    expected = now.astimezone(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d")
    generated = reports_module.generate_reports(object(), tmp_path / "output", now=now)
    _implemented(
        any(path.name == f"{expected}.md" for path in generated),
        f"daily naming uses Asia/Kolkata date {expected}",
    )


def test_h1_no_restricted_scraping_or_browser_automation() -> None:
    """H1: No LinkedIn/Indeed HTML scraping, browser automation, or CAPTCHA bypass."""
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "jobwatcher").rglob("*.py")
    )
    forbidden = (
        r"^\s*(?:from|import)\s+(?:selenium|playwright)\b",
        r"(?:linkedin|indeed)\.com",
        r"\b(?:captcha\s+bypass|bypass\s+captcha|solve_captcha)\b",
    )
    found = [pattern for pattern in forbidden if re.search(pattern, source, re.I | re.M)]
    _implemented(not found, f"no restricted scraping or automation (found {found})")


def test_h2_gitignore_excludes_private_runtime_paths() -> None:
    """H2: .gitignore lists .env, data/, output/, logs/, and __pycache__/."""
    content = (ROOT / ".gitignore").read_text(encoding="utf-8")
    entries = {line.strip().replace("\\", "/") for line in content.splitlines()}
    required = {".env", "data/", "output/", "logs/", "__pycache__/"}
    missing = sorted(required - entries)
    assert not missing, f"NOT IMPLEMENTED: .gitignore missing required entries: {missing}"


def test_h3_env_example_lists_only_approved_secret_names() -> None:
    """H3: .env.example lists webhook and Adzuna keys, without bot/channel settings."""
    content = (ROOT / ".env.example").read_text(encoding="utf-8")
    names = {
        line.split("=", 1)[0].strip()
        for line in content.splitlines()
        if line.strip() and not line.lstrip().startswith("#") and "=" in line
    }
    required = {"DISCORD_WEBHOOK_URL", "ADZUNA_APP_ID", "ADZUNA_APP_KEY"}
    _implemented(required.issubset(names), ".env.example contains the approved secret variables")
    assert "DISCORD_BOT_TOKEN" not in names and "DISCORD_JOBS_CHANNEL_ID" not in names


def test_h4_requirements_are_version_pinned() -> None:
    """H4: requirements.txt exists and pins each direct dependency."""
    path = ROOT / "requirements.txt"
    _implemented(path.is_file(), "requirements.txt exists")
    entries = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    unpinned = [entry for entry in entries if "==" not in entry]
    assert entries and not unpinned, f"NOT IMPLEMENTED: dependencies are not exactly pinned: {unpinned}"


def test_h5_readme_has_required_setup_and_no_stale_claim() -> None:
    """H5: README covers setup/models/scheduler/Adzuna and matches implementation."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    required = {
        "setup": re.search(r"^##\s+Setup\b", readme, re.I | re.M),
        "qwen3:4b": re.search(r"ollama\s+pull\s+qwen3:4b", readme, re.I),
        "nomic-embed-text": re.search(r"ollama\s+pull\s+nomic-embed-text", readme, re.I),
        "Windows Task Scheduler": re.search(r"Task Scheduler", readme, re.I),
        "hourly run.py --once": re.search(
            r"(?:hourly|every hour|60 minutes).{0,120}run\.py\s+--once"
            r"|run\.py\s+--once.{0,120}(?:hourly|every hour|60 minutes)",
            readme,
            re.I | re.S,
        ),
        "Adzuna keys": re.search(r"ADZUNA_APP_ID", readme)
        and re.search(r"ADZUNA_APP_KEY", readme),
    }
    stale = re.search(r"Adzuna.{0,60}(?:not implemented|not implemented yet)", readme, re.I | re.S)
    missing = [name for name, result in required.items() if not result]
    if stale:
        missing.append("stale claim that Adzuna is not implemented")
    assert not missing, f"NOT IMPLEMENTED: README documentation gaps/conflicts: {missing}"


def test_h6_no_different_duplicate_project_copy() -> None:
    """H6: Warn/fail when a second project copy differs from the source of truth."""
    duplicate = Path.home() / "Documents" / "Job_finder"
    if not duplicate.is_dir():
        return
    current_hashes = _tree_hashes(ROOT)
    duplicate_hashes = _tree_hashes(duplicate)
    differing = sorted(
        path
        for path in set(current_hashes) | set(duplicate_hashes)
        if current_hashes.get(path) != duplicate_hashes.get(path)
    )
    assert not differing, (
        "NOT IMPLEMENTED: project has a second source-of-truth copy with different "
        f"files: {differing}"
    )


def test_i1_linkedin_route_status_only() -> None:
    """I1: Report whether an IMAP alert-email or Discord-channel route exists."""
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "jobwatcher").rglob("*.py")
    )
    imap_route = re.search(r"\b(?:imaplib|IMAP4|IMAPClient)\b", source, re.I)
    discord_channel_route = re.search(
        r"(?:DISCORD_JOBS_CHANNEL_ID|discord.{0,40}channel|channel.{0,40}discord)",
        source,
        re.I | re.S,
    )
    _implemented(
        bool(imap_route or discord_channel_route),
        "LinkedIn alert-email (IMAP) or Discord-channel collector route",
    )

