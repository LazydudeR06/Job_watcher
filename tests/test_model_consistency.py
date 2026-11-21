from pathlib import Path
import re

import yaml


ROOT = Path(__file__).resolve().parents[1]
MODEL_NAME = "llama3.2:3b"


def test_config_and_readme_use_the_same_language_model() -> None:
    """Keep the configured scoring model and README install command in sync."""
    config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert config["scoring"]["model"] == MODEL_NAME
    assert re.search(
        rf"(?m)^\s*ollama\s+pull\s+{re.escape(MODEL_NAME)}\s*$",
        readme,
        re.IGNORECASE,
    )
