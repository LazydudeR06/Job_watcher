import argparse
import logging
import os
import sqlite3
from pathlib import Path
from typing import Sequence

from .config import ConfigError, load_config
from .collectors.adzuna import AdzunaCollector
from .collectors.himalayas import HimalayasCollector
from .collectors.linkedin_alerts import LinkedInAlertEmailCollector
from .collectors.linkedin_email import LinkedInEmailCollector
from .collectors.remotive import RemotiveCollector
from .collectors.remoteok import RemoteOKCollector
from .filter import filter_jobs
from .logging_config import configure_logging
from .models import Job
from .notifier import send_notifications
from .reports import generate_reports
from .scorer import score_job
from .scheduler import run_forever
from .storage import (
    get_pending_filters,
    get_pending_scores,
    initialize_storage,
    insert_jobs,
    mark_score_gave_up,
    update_filter_decision,
    update_job_score,
)

LOGGER = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(description="Run the local privacy-friendly job watcher.")
    parser.add_argument("--once", action="store_true", help="Initialize and exit after one run")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose output")
    parser.add_argument("--interval", type=int, help="Loop interval in seconds")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Load configuration, initialize storage, and run configured collectors."""
    args = build_parser().parse_args(argv)

    try:
        configure_logging(args.verbose)
    except OSError as exc:
        print(f"Logging error: {exc}")
        return 4

    try:
        config = load_config()
    except ConfigError as exc:
        LOGGER.error("Configuration error: %s", exc)
        print(f"Configuration error: {exc}")
        return 2

    interval = args.interval
    if interval is not None and interval < 1:
        print("Configuration error: --interval must be a positive integer")
        return 2
    if interval is None:
        interval = config["watcher"]["interval_seconds"]

    database_path = Path("data/jobs.db")
    try:
        connection = initialize_storage(database_path)
    except sqlite3.Error as exc:
        LOGGER.exception("Could not initialize SQLite storage")
        print(f"Storage error: {exc}")
        return 3

    try:
        if args.verbose:
            print(f"Initialized SQLite storage at {database_path}")
            print(f"Run interval: {interval} seconds")
        LOGGER.info("Job watcher started with interval=%s seconds", interval)

        collectors = []
        if config["sources"]["remotive"]["enabled"]:
            remotive_config = config["sources"]["remotive"]
            collectors.append(
                RemotiveCollector(feed_url=remotive_config.get(
                    "feed_url",
                    "https://remotive.com/api/remote-jobs",
                ))
            )
        if config["sources"]["remoteok"]["enabled"]:
            remoteok_config = config["sources"]["remoteok"]
            collectors.append(
                RemoteOKCollector(
                    feed_url=remoteok_config.get(
                        "feed_url",
                        "https://remoteok.com/api",
                    )
                )
            )
        if config["sources"]["himalayas"]["enabled"]:
            himalayas_config = config["sources"]["himalayas"]
            collectors.append(
                HimalayasCollector(
                    feed_url=himalayas_config.get(
                        "feed_url",
                        "https://himalayas.app/jobs/api?limit=20",
                    )
                )
            )
        adzuna_config = config["sources"]["adzuna"]
        if adzuna_config["enabled"]:
            app_id = os.getenv(adzuna_config["app_id_env"])
            app_key = os.getenv(adzuna_config["app_key_env"])
            if app_id and app_key:
                collectors.append(
                    AdzunaCollector(
                        app_id=app_id,
                        app_key=app_key,
                        country=adzuna_config.get("country", "in"),
                        results_per_page=adzuna_config.get("results_per_page", 50),
                    )
                )
            else:
                LOGGER.info(
                    "Adzuna is disabled because %s and/or %s are not set",
                    adzuna_config["app_id_env"],
                    adzuna_config["app_key_env"],
                )
        linkedin_config = config["sources"].get("linkedin", {})
        if linkedin_config.get("enabled"):
            imap_host = os.getenv(
                linkedin_config.get("imap_host_env", "LINKEDIN_IMAP_HOST")
            )
            username = os.getenv(
                linkedin_config.get("username_env", "LINKEDIN_IMAP_USERNAME")
            )
            password = os.getenv(
                linkedin_config.get("password_env", "LINKEDIN_IMAP_PASSWORD")
            )
            if imap_host and username and password:
                collectors.append(
                    LinkedInAlertEmailCollector(
                        imap_host=imap_host,
                        username=username,
                        password=password,
                        folder=linkedin_config.get("folder", "INBOX"),
                        sender=linkedin_config.get(
                            "sender",
                            "jobalerts-noreply@linkedin.com",
                        ),
                    )
                )
            else:
                LOGGER.warning(
                    "LinkedIn alert collector is enabled but IMAP credentials are missing"
                )
        linkedin_email_config = config["sources"].get("linkedin_email", {})
        if linkedin_email_config.get("enabled"):
            collectors.append(
                LinkedInEmailCollector(
                    enabled=True,
                    imap_host=linkedin_email_config.get(
                        "imap_host",
                        "imap.gmail.com",
                    ),
                    folder=linkedin_email_config.get("folder", "INBOX"),
                    sender_filter=linkedin_email_config.get(
                        "sender_filter",
                        "jobalerts-noreply@linkedin.com",
                    ),
                    max_emails_per_run=linkedin_email_config.get(
                        "max_emails_per_run",
                        50,
                    ),
                    lookback_days=linkedin_email_config.get("lookback_days", 3),
                    imap_user=os.getenv("IMAP_USER", ""),
                    imap_app_password=os.getenv("IMAP_APP_PASSWORD", ""),
                    connection=connection,
                )
            )

        def filter_unprocessed(jobs: list[Job]) -> None:
            if not jobs:
                return
            profile_path = Path("profile.md")
            profile_text = (
                profile_path.read_text(encoding="utf-8")
                if profile_path.is_file()
                else ""
            )
            embeddings = config["embeddings"]
            decisions = filter_jobs(
                jobs,
                profile_text,
                backend=embeddings["backend"],
                model=embeddings["model"],
                ollama_url=embeddings["ollama_url"],
                threshold=float(embeddings["similarity_threshold"]),
            )
            for job, decision in decisions:
                if decision.survives is None:
                    continue
                update_filter_decision(
                    connection,
                    job,
                    decision.survives,
                    decision.similarity,
                    decision.reason,
                )

        def collect_once() -> None:
            filter_unprocessed(get_pending_filters(connection))
            new_jobs: list[Job] = []
            for collector in collectors:
                try:
                    jobs = collector.fetch()
                except Exception as exc:
                    source_name = getattr(
                        collector,
                        "name",
                        collector.__class__.__name__,
                    )
                    LOGGER.exception(
                        "Collector %s failed: %s",
                        source_name,
                        exc,
                    )
                    continue
                inserted = insert_jobs(connection, jobs)
                new_jobs.extend(inserted)
                filter_unprocessed(inserted)
                if args.verbose:
                    print(
                        f"{collector.name}: fetched {len(jobs)} jobs, "
                        f"stored {len(inserted)} new jobs"
                    )
            score_config = config["scoring"]
            profile_path = Path("profile.md")
            profile_text = (
                profile_path.read_text(encoding="utf-8")
                if profile_path.is_file()
                else ""
            )
            pending_scores = get_pending_scores(connection)
            scored = 0
            new_job_urls = {job.url for job in new_jobs}
            newly_scored_jobs: list[dict[str, object]] = []
            for job in pending_scores:
                result = score_job(
                    job,
                    profile_text,
                    model=score_config["model"],
                    ollama_url=score_config["ollama_url"],
                    timeout_seconds=float(score_config["timeout_seconds"]),
                )
                if result is not None:
                    update_job_score(
                        connection,
                        job,
                        result.score,
                        result.rationale,
                        result.as_json(),
                        result.reasons,
                        result.missing_skills,
                        result.apply_priority,
                    )
                    scored += 1
                    if job.url in new_job_urls:
                        newly_scored_jobs.append(
                            {
                                "title": job.title,
                                "company": job.company,
                                "score": result.score,
                                "apply_priority": result.apply_priority,
                                "url": job.url,
                            }
                        )
                else:
                    mark_score_gave_up(connection, job)
                    LOGGER.warning(
                        "Scoring gave up for job %s",
                        job.source_job_id,
                    )
            notification_config = config.get("notifications", {})
            outbound_config = config.get("outbound", {})
            webhook_url = (
                os.getenv("DISCORD_WEBHOOK_URL", "")
                if outbound_config.get("enabled", False)
                else ""
            )
            send_notifications(
                newly_scored_jobs,
                webhook_url,
                min_score=notification_config.get("notification_min_score", 75),
            )
            if args.verbose and pending_scores:
                print(
                    f"Scored {scored} of {len(pending_scores)} filtered survivors"
                )
            generated_reports = generate_reports(
                connection,
                new_jobs=new_jobs,
                digest_min_score=config.get("reports", {}).get(
                    "digest_min_score",
                    60,
                ),
                notification_min_score=config.get("notifications", {}).get(
                    "notification_min_score",
                    75,
                ),
            )
            if args.verbose:
                for report_path in generated_reports:
                    print(f"Wrote report: {report_path}")

        if args.once or not collectors:
            collect_once()
            if not collectors:
                print("No enabled collector implementations exist.")
                LOGGER.warning("No enabled collector implementations exist")
            return 0

        run_forever(collect_once, interval)
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
