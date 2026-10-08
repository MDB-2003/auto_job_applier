"""Validated environment configuration without import-time side effects."""
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
import os


@dataclass(frozen=True)
class Settings:
    database_path: Path
    log_level: str = "INFO"

    @classmethod
    def load(cls, environ: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if environ is None else environ
        path = env.get("JOB_AGENT_DATABASE_PATH", "data/job_agent.sqlite3").strip()
        level = env.get("JOB_AGENT_LOG_LEVEL", "INFO").strip().upper()
        if not path or path == ":memory:" or "://" in path:
            raise ValueError("JOB_AGENT_DATABASE_PATH must be a local file path")
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("Invalid JOB_AGENT_LOG_LEVEL")
        return cls(Path(path).expanduser(), level)
