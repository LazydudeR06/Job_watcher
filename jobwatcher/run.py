import argparse
import logging
import sqlite3
from pathlib import Path
from typing import Sequence

from .config import ConfigError, load_config
from .collectors.himalayas import HimalayasCollector
from .collectors.remotive import RemotiveCollector
from .collectors.remoteok import RemoteOKCollector
from .filter import filter_jobs
from .logging_config import configure_logging
from .reports import generate_reports
from .scorer import score_job
from .scheduler import run_forever
from .storage import (
    get_pending_scores,
    initialize_storage,
    insert_jobs,
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

        def collect_once() -> None:
            for collector in collectors:
                jobs = collector.fetch()
                inserted = insert_jobs(connection, jobs)
                survivors = 0
                if inserted:
                    profile_path = Path("profile.md")
                    profile_text = (
                        profile_path.read_text(encoding="utf-8")
                        if profile_path.is_file()
                        else ""
                    )
                    embeddings = config["embeddings"]
                    decisions = filter_jobs(
                        inserted,
                        profile_text,
                        backend=embeddings["backend"],
                        model=embeddings["model"],
                        ollama_url=embeddings["ollama_url"],
                        threshold=float(embeddings["similarity_threshold"]),
                    )
                    survivors = 0
                    for job, decision in decisions:
                        update_filter_decision(
                            connection,
                            job,
                            decision.survives,
                            decision.similarity,
                            decision.reason,
                        )
                        if decision.survives:
                            survivors += 1
                if args.verbose:
                    print(
                        f"{collector.name}: fetched {len(jobs)} jobs, "
                        f"stored {len(inserted)} new jobs"
                    )
                    if inserted:
                        print(
                            f"{collector.name}: filtered {len(inserted)} new jobs, "
                            f"{survivors} survived"
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
                    )
                    scored += 1
            if args.verbose and pending_scores:
                print(
                    f"Scored {scored} of {len(pending_scores)} filtered survivors"
                )
            generated_reports = generate_reports(connection)
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
