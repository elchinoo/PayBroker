import os
from dataclasses import dataclass

from apgkit.config import read_secret  # noqa: F401  (kept importable from here)


@dataclass(frozen=True)
class Settings:
    database_url: str
    api_key: str            # optional static full-access operator key
    public_url: str
    role: str               # api | worker | all
    pool_max: int


def get_settings() -> Settings:
    role = os.environ.get("PAYBROKER_ROLE", "all").lower()
    return Settings(
        database_url=os.environ.get("DATABASE_URL") or os.environ.get("DB_EXTERNAL_DSN")
        or os.environ.get("PSQL_CONNECTION_STRING") or "",
        api_key=read_secret("PAYBROKER_API_KEY", "/etc/paybroker/api_key"),
        public_url=os.environ.get("PAYBROKER_PUBLIC_URL", "").rstrip("/"),
        role=role if role in {"api", "worker", "all"} else "all",
        pool_max=int(os.environ.get("PAYBROKER_POOL_MAX", "20")),
    )
