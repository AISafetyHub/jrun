"""Service settings, read from the environment (see .env.example)."""

import os
from dataclasses import dataclass, field
from pathlib import Path

CATEGORY_NAMES = ("dedicated", "public", "idle")


def _csv(value: str | None) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


@dataclass
class Settings:
    # Project sets (names) the service will submit into; required.
    project_sets: list[str]
    # Optional allowlist of caller aliases; empty means any verified user.
    users: list[str] = field(default_factory=list)
    # Queue categories the service submits (default: public only).
    categories: list[str] = field(default_factory=lambda: ["public"])
    data_dir: Path = Path("/data")
    host: str = "0.0.0.0"
    port: int = 8080
    jrun_bin: str = "jrun"
    # Seconds a verified caller token is trusted before re-checking.
    identity_ttl: int = 300
    # Timeout for one jrun invocation (a submit runs several airsctl calls).
    jrun_timeout: int = 900

    @classmethod
    def from_env(cls, env=None) -> "Settings":
        env = os.environ if env is None else env
        project_sets = _csv(env.get("JRUN_SERVE_PROJECT_SETS"))
        if not project_sets:
            raise ValueError("JRUN_SERVE_PROJECT_SETS is required (comma-separated "
                             "project set names the service may submit into)")
        categories = [c.lower() for c in _csv(env.get("JRUN_SERVE_CATEGORIES"))] or ["public"]
        bad = [c for c in categories if c not in CATEGORY_NAMES]
        if bad:
            raise ValueError(f"JRUN_SERVE_CATEGORIES: unknown categories {', '.join(bad)}")
        if not (env.get("AIRS_AK") and env.get("AIRS_SK")):
            raise ValueError("AIRS_AK and AIRS_SK (the submitting account's key) are required")
        return cls(
            project_sets=project_sets,
            users=_csv(env.get("JRUN_SERVE_USERS")),
            categories=categories,
            data_dir=Path(env.get("JRUN_SERVE_DATA") or "/data"),
            host=env.get("JRUN_SERVE_HOST") or "0.0.0.0",
            port=int(env.get("JRUN_SERVE_PORT") or 8080),
            jrun_bin=env.get("JRUN_BIN") or "jrun",
            identity_ttl=int(env.get("JRUN_SERVE_IDENTITY_TTL") or 300),
            jrun_timeout=int(env.get("JRUN_SERVE_JRUN_TIMEOUT") or 900),
        )
