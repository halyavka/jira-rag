"""YAML config loader with environment variable interpolation."""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

from jira_rag.config.schema import AppConfig

_ENV_VAR_PATTERN = re.compile(r"\$\{(\w+)(?::([^}]*))?\}")


def _interpolate(value: str) -> str:
    def replacer(match: re.Match) -> str:
        var_name = match.group(1)
        default = match.group(2)
        env_value = os.environ.get(var_name)
        if env_value is not None:
            return env_value
        if default is not None:
            return default
        raise ValueError(
            f"Environment variable '{var_name}' is not set and no default provided"
        )

    return _ENV_VAR_PATTERN.sub(replacer, value)


def _walk(data):
    if isinstance(data, dict):
        return {k: _walk(v) for k, v in data.items()}
    if isinstance(data, list):
        return [_walk(item) for item in data]
    if isinstance(data, str):
        return _interpolate(data)
    return data


def load_config(path: str | Path = "config.yaml") -> AppConfig:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(config_path) as f:
        raw = yaml.safe_load(f)

    return AppConfig.model_validate(_walk(raw))
