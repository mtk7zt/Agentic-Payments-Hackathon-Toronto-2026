"""Gateway settings, read once from the environment and checked before anything starts.

Two modes:

- `production` (the default): every adapter must be real. The gateway refuses to start with
  the sandbox rail or seller, without a persistent database, without a strong owner token, or
  with a wildcard CORS origin. Nothing is seeded.
- `sandbox`: for local development, demos and CI. The sandbox rail and seller are allowed and
  the sample sellers are seeded into an empty whitelist. It must be chosen explicitly.

Moving to another host (Supabase, RDS, AWS) changes environment values, never this code.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

RAILS = ("mock", "goat", "qatom")
SELLER_MODES = ("mock", "http")
MIN_OWNER_TOKEN = 32


class ConfigError(RuntimeError):
    """The environment cannot run a safe gateway. The message lists every problem found."""


@dataclass(frozen=True)
class Settings:
    mode: str
    database_url: str
    rail: str
    seller_mode: str
    cors_origins: tuple[str, ...]

    @property
    def sandbox(self) -> bool:
        return self.mode == "sandbox"


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    mode = env.get("MANDATE_MODE", "production").strip().lower()
    problems: list[str] = []
    if mode not in ("production", "sandbox"):
        raise ConfigError(f"MANDATE_MODE must be production or sandbox, not {mode!r}")

    database_url = (env.get("DATABASE_URL") or env.get("MANDATE_DB") or "").strip()
    rail = env.get("MANDATE_RAIL", "mock" if mode == "sandbox" else "").strip().lower()
    seller_mode = env.get("MANDATE_SELLER_MODE", "mock" if mode == "sandbox" else "").strip().lower()
    origins = tuple(o.strip() for o in env.get("MANDATE_CORS_ORIGINS", "http://localhost:3000").split(",") if o.strip())

    if rail not in RAILS:
        problems.append(f"MANDATE_RAIL must be one of {', '.join(RAILS)} (got {rail or 'nothing'})")
    if seller_mode not in SELLER_MODES:
        problems.append(f"MANDATE_SELLER_MODE must be one of {', '.join(SELLER_MODES)} (got {seller_mode or 'nothing'})")
    if "*" in origins:
        problems.append("MANDATE_CORS_ORIGINS cannot be '*' because owner requests carry credentials")

    if mode == "production":
        if not database_url or database_url in (":memory:", "sqlite:///:memory:", "sqlite:///"):
            problems.append("DATABASE_URL must point at a persistent database (for example postgresql://…)")
        if rail == "mock":
            problems.append("MANDATE_RAIL=mock is the sandbox rail; production needs goat or qatom")
        if seller_mode == "mock":
            problems.append("MANDATE_SELLER_MODE=mock is the sandbox seller; production needs http")
        if len(env.get("MANDATE_OWNER_TOKEN", "")) < MIN_OWNER_TOKEN:
            problems.append(f"MANDATE_OWNER_TOKEN must be set and at least {MIN_OWNER_TOKEN} characters")
    elif not database_url:
        database_url = "sqlite:///mandate-sandbox.db"  # sandbox still keeps its data across restarts

    if problems:
        raise ConfigError("Mandate cannot start safely:\n- " + "\n- ".join(problems))
    return Settings(mode, database_url, rail, seller_mode, origins)
