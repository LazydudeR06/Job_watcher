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
        if not os.getenv("ADZUNA_APP_ID") or not os.getenv("ADZUNA_APP_KEY"):
            raise ConfigError("ADZUNA_APP_ID and ADZUNA_APP_KEY are required when Adzuna is enabled")

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
