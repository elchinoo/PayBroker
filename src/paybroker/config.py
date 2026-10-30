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
    llm_base_url: str       # the shared LLM gateway (OpenAI-compatible `<gateway>/v1`), injected by ecoctl at deploy time
    llm_api_key: str
    llm_chat_model: str


def get_settings() -> Settings:
    role = os.environ.get("PAYBROKER_ROLE", "all").lower()
    return Settings(
        database_url=os.environ.get("DATABASE_URL") or os.environ.get("DB_EXTERNAL_DSN")
        or os.environ.get("PSQL_CONNECTION_STRING") or "",
        api_key=read_secret("PAYBROKER_API_KEY", "/etc/paybroker/api_key"),
        public_url=os.environ.get("PAYBROKER_PUBLIC_URL", "").rstrip("/"),
        role=role if role in {"api", "worker", "all"} else "all",
        pool_max=int(os.environ.get("PAYBROKER_POOL_MAX", "20")),
        # Same names and meaning as TrustDesk and PagerLens: the environment contract of the ecosystem.
        llm_base_url=os.environ.get("LLM_BASE_URL", "").rstrip("/"),
        llm_api_key=read_secret("LLM_API_KEY", "/etc/paybroker/llm_api_key"),
        llm_chat_model=os.environ.get("LLM_CHAT_MODEL", ""),
    )


def bind_host() -> str:
    """The address `paybroker serve` binds to: PAYBROKER_HOST, else HOST, else all interfaces. The kit decides from this whether
    PAYBROKER_ALLOW_ANONYMOUS may be honoured (loopback only)."""
    return os.environ.get("PAYBROKER_HOST") or os.environ.get("HOST") or "0.0.0.0"
