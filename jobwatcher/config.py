import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


class ConfigError(ValueError):
    """Raised when the watcher configuration is invalid."""


def load_config(path: str | Path = "config.yaml") -> dict[str, Any]:
    """Load and validate config.yaml and optional environment secrets."""
    load_dotenv()
    config_path = Path(path)
    if not config_path.is_file():
        raise ConfigError(f"Configuration file not found: {config_path}")

    try:
        with config_path.open("r", encoding="utf-8") as handle:
            config = yaml.safe_load(handle)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {config_path}: {exc}") from exc

    if not isinstance(config, dict):
        raise ConfigError("Configuration root must be a mapping")

    required_sections = ("sources", "outbound", "watcher")
    missing = [section for section in required_sections if section not in config]
    if missing:
        raise ConfigError(f"Missing required section(s): {', '.join(missing)}")

    sources = config["sources"]
    outbound = config["outbound"]
    watcher = config["watcher"]
    if not isinstance(sources, dict):
        raise ConfigError("The sources section must be a mapping")
    if not isinstance(outbound, dict):
        raise ConfigError("The outbound section must be a mapping")
    if not isinstance(watcher, dict):
        raise ConfigError("The watcher section must be a mapping")

    reports = config.setdefault("reports", {})
    notifications = config.setdefault("notifications", {})
    if not isinstance(reports, dict):
        raise ConfigError("The reports section must be a mapping")
    if not isinstance(notifications, dict):
        raise ConfigError("The notifications section must be a mapping")
    reports.setdefault("digest_min_score", 60)
    notifications.setdefault("notification_min_score", 75)
    for section, key in (
        (reports, "digest_min_score"),
        (notifications, "notification_min_score"),
    ):
        threshold = section[key]
        if (
            not isinstance(threshold, int)
            or isinstance(threshold, bool)
            or not 0 <= threshold <= 100
        ):
            raise ConfigError(f"{key} must be an integer from 0 to 100")

    required_sources = ("remotive", "remoteok", "himalayas", "adzuna")
    missing_sources = [name for name in required_sources if name not in sources]
    if missing_sources:
        raise ConfigError(f"Missing source configuration(s): {', '.join(missing_sources)}")

    for name in required_sources:
        source_config = sources[name]
        if not isinstance(source_config, dict) or "enabled" not in source_config:
            raise ConfigError(f"Source '{name}' must define an enabled boolean")
        if not isinstance(source_config["enabled"], bool):
            raise ConfigError(f"Source '{name}.enabled' must be a boolean")

    linkedin = sources.get("linkedin")
    if linkedin is not None:
        if not isinstance(linkedin, dict) or not isinstance(
            linkedin.get("enabled"), bool
        ):
            raise ConfigError("Source 'linkedin' must define an enabled boolean")
        if linkedin["enabled"]:
            for key, default in (
                ("imap_host_env", "LINKEDIN_IMAP_HOST"),
                ("username_env", "LINKEDIN_IMAP_USERNAME"),
                ("password_env", "LINKEDIN_IMAP_PASSWORD"),
            ):
                env_name = linkedin.get(key, default)
                if not isinstance(env_name, str) or not env_name.strip():
                    raise ConfigError(f"sources.linkedin.{key} must be a non-empty string")
                if not os.getenv(env_name):
                    raise ConfigError(
                        f"{env_name} is required when the LinkedIn source is enabled"
                    )
        sender = linkedin.get("sender", "jobalerts-noreply@linkedin.com")
        if not isinstance(sender, str) or not sender.strip():
            raise ConfigError("sources.linkedin.sender must be a non-empty string")

    linkedin_email = sources.get("linkedin_email")
    if linkedin_email is not None:
        if not isinstance(linkedin_email, dict) or not isinstance(
            linkedin_email.get("enabled"), bool
        ):
            raise ConfigError(
                "Source 'linkedin_email' must define an enabled boolean"
            )
        linkedin_email.setdefault("imap_host", "imap.gmail.com")
        linkedin_email.setdefault("folder", "INBOX")
        linkedin_email.setdefault(
            "sender_filter",
            "jobalerts-noreply@linkedin.com",
        )
        linkedin_email.setdefault("max_emails_per_run", 50)
        linkedin_email.setdefault("lookback_days", 3)
        for key in ("imap_host", "folder", "sender_filter"):
            if not isinstance(linkedin_email[key], str) or not linkedin_email[key].strip():
                raise ConfigError(f"sources.linkedin_email.{key} must be a non-empty string")
        for key in ("max_emails_per_run", "lookback_days"):
            value = linkedin_email[key]
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ConfigError(f"sources.linkedin_email.{key} must be a positive integer")

    if outbound.get("discord_webhook_env") != "DISCORD_WEBHOOK_URL":
        raise ConfigError("outbound.discord_webhook_env must be DISCORD_WEBHOOK_URL")
    if "enabled" not in outbound or not isinstance(outbound["enabled"], bool):
        raise ConfigError("outbound.enabled must be a boolean")
    if outbound["enabled"] and not os.getenv("DISCORD_WEBHOOK_URL"):
        raise ConfigError("DISCORD_WEBHOOK_URL is required when outbound is enabled")

    adzuna = sources["adzuna"]
    if adzuna["enabled"]:
        if adzuna.get("app_id_env") != "ADZUNA_APP_ID" or adzuna.get("app_key_env") != "ADZUNA_APP_KEY":
            raise ConfigError("Adzuna environment names must be ADZUNA_APP_ID and ADZUNA_APP_KEY")
        country = adzuna.get("country", "in")
        if not isinstance(country, str) or not country.strip():
            raise ConfigError("sources.adzuna.country must be a non-empty string")
        results_per_page = adzuna.get("results_per_page", 50)
        if (
            not isinstance(results_per_page, int)
            or isinstance(results_per_page, bool)
            or not 1 <= results_per_page <= 50
        ):
            raise ConfigError("sources.adzuna.results_per_page must be an integer from 1 to 50")

    interval = watcher.get("interval_seconds")
    if not isinstance(interval, int) or isinstance(interval, bool) or interval < 1:
        raise ConfigError("watcher.interval_seconds must be a positive integer")

    embeddings = config.get("embeddings")
    if not isinstance(embeddings, dict):
        raise ConfigError("The embeddings section must be a mapping")
    if embeddings.get("backend") not in ("ollama", "sentence_transformers"):
        raise ConfigError("embeddings.backend must be ollama or sentence_transformers")
    if not isinstance(embeddings.get("model"), str) or not embeddings["model"].strip():
        raise ConfigError("embeddings.model must be a non-empty string")
    if not isinstance(embeddings.get("ollama_url"), str) or not embeddings["ollama_url"].strip():
        raise ConfigError("embeddings.ollama_url must be a non-empty string")
    threshold = embeddings.get("similarity_threshold")
    if not isinstance(threshold, (int, float)) or isinstance(threshold, bool):
        raise ConfigError("embeddings.similarity_threshold must be a number")
    if not 0 <= float(threshold) <= 1:
        raise ConfigError("embeddings.similarity_threshold must be between 0 and 1")

    scoring = config.get("scoring")
    if not isinstance(scoring, dict):
        raise ConfigError("The scoring section must be a mapping")
    if scoring.get("backend") != "ollama":
        raise ConfigError("scoring.backend must be ollama")
    for key in ("model", "ollama_url"):
        if not isinstance(scoring.get(key), str) or not scoring[key].strip():
            raise ConfigError(f"scoring.{key} must be a non-empty string")
    timeout = scoring.get("timeout_seconds")
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise ConfigError("scoring.timeout_seconds must be a positive number")

    return config
