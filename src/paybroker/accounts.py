"""LLM provider accounts (OpenAI-compatible endpoints) for Tier 3 triage and rule suggestions. Keys are encrypted at rest."""
from dataclasses import dataclass
from urllib.parse import urlsplit

from . import settings_store as store
from . import vault
from .config import get_settings
from .netguard import check_url

SETTING = "triage_account"
PRESETS = [
    {"id": "openai", "name": "OpenAI", "base_url": "https://api.openai.com/v1", "chat_model": ""},
    {"id": "openrouter", "name": "OpenRouter", "base_url": "https://openrouter.ai/api/v1", "chat_model": ""},
    {"id": "anthropic", "name": "Anthropic (Claude)", "base_url": "https://api.anthropic.com/v1", "chat_model": "",
     "note": "Uses Anthropic's OpenAI-compatibility endpoint, which they describe as not for production."},
    {"id": "custom", "name": "Custom (OpenAI-compatible)", "base_url": "", "chat_model": ""},
]
PROVIDER_IDS = {p["id"] for p in PRESETS}
ENV = "env"     # the built-in account made from LLM_BASE_URL / LLM_API_KEY[_FILE] / LLM_CHAT_MODEL (never stored, never editable)
TEST_PROMPT = "Reply with the single word OK. Keep any reasoning very short."
TEST_MAX_TOKENS = 64        # a thinking model needs room to finish its reasoning and still answer
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class AccountError(ValueError):
    pass


@dataclass(frozen=True)
class ChatConfig:
    base_url: str
    model: str
    label: str
    key_enc: str | None = None
    trusted: bool = False         # True only for the operator-set environment account (needs no vault)
    key_plain: str = ""

    def api_key(self) -> str:
        return vault.decrypt(self.key_enc) if self.key_enc else self.key_plain


def _env_url_problem(url: str) -> str | None:
    """The environment URL is set by the operator, so (like TrustDesk) it is trusted and skips the private-address check
    (the shared gateway is another sandbox's URL). It must still be a plain https base URL; http only for localhost."""
    try:
        parts = urlsplit(url)
        host, _ = parts.hostname, parts.port
    except ValueError:
        return "LLM_BASE_URL is not a valid URL"
    if parts.scheme not in {"http", "https"} or not host:
        return "LLM_BASE_URL must start with https://"
    if parts.username or parts.password:
        return "LLM_BASE_URL must not contain credentials"
    if parts.query or parts.fragment:
        return "LLM_BASE_URL must not contain a query string or fragment"
    if parts.scheme == "http" and host.lower() not in _LOCAL_HOSTS:
        return "LLM_BASE_URL must use https:// (plain http is only accepted for localhost)"
    return None


def env_account() -> dict | None:
    """The operator-set environment configuration, shown read-only in the UI. The key is never part of it."""
    s = get_settings()
    if not s.llm_base_url:
        return None
    out = {"id": ENV, "name": "Environment (server settings)", "provider": ENV, "base_url": s.llm_base_url,
           "has_key": bool(s.llm_api_key), "api_key_last4": None, "chat_model": s.llm_chat_model or None, "read_only": True}
    problem = _env_url_problem(s.llm_base_url)
    if problem:
        out["error"] = problem
    return out


_PUBLIC = "id, name, provider, base_url, (api_key_enc IS NOT NULL) AS has_key, api_key_last4, chat_model, created_at, updated_at"


def list_accounts(conn) -> list[dict]:
    return conn.execute(f"SELECT {_PUBLIC} FROM llm_account ORDER BY name").fetchall()


def get_account(conn, account_id: int) -> dict:
    row = conn.execute(f"SELECT {_PUBLIC} FROM llm_account WHERE id=%s", (account_id,)).fetchone()
    if not row:
        raise AccountError("account not found")
    return row


def _clean(value: str | None) -> str | None:
    return (value or "").strip() or None


def _check_name(name: str) -> None:
    if name.strip().lower() == ENV:
        raise AccountError('the name "env" is reserved for the environment account')


def _need_vault(conn) -> None:
    if not vault.is_initialized(conn):
        raise AccountError("set an admin password first (Settings > Security): provider keys are stored encrypted under it")


def create(conn, *, name: str, provider: str, base_url: str, api_key: str | None, chat_model: str | None) -> int:
    name = name.strip()
    _check_name(name)
    if provider not in PROVIDER_IDS:
        raise AccountError("unknown provider")
    base_url = check_url(base_url)
    enc = last4 = None
    if _clean(api_key):
        _need_vault(conn)
        enc, last4 = vault.encrypt(api_key.strip()), api_key.strip()[-4:]
    if conn.execute("SELECT 1 FROM llm_account WHERE lower(name)=lower(%s)", (name,)).fetchone():
        raise AccountError("an account with that name already exists")
    row = conn.execute("INSERT INTO llm_account (name, provider, base_url, api_key_enc, api_key_last4, chat_model) "
                       "VALUES (%s,%s,%s,%s,%s,%s) RETURNING id", (name, provider, base_url, enc, last4, _clean(chat_model))).fetchone()
    conn.commit()
    return row["id"]


def update(conn, account_id: int, *, name: str, provider: str, base_url: str, api_key: str | None, clear_key: bool,
           chat_model: str | None) -> None:
    get_account(conn, account_id)
    if provider not in PROVIDER_IDS:
        raise AccountError("unknown provider")
    _check_name(name)
    name, base_url = name.strip(), check_url(base_url)
    if conn.execute("SELECT 1 FROM llm_account WHERE lower(name)=lower(%s) AND id<>%s", (name, account_id)).fetchone():
        raise AccountError("an account with that name already exists")
    sets = ["name=%s", "provider=%s", "base_url=%s", "chat_model=%s", "updated_at=now()"]
    args: list = [name, provider, base_url, _clean(chat_model)]
    if _clean(api_key):
        _need_vault(conn)
        sets += ["api_key_enc=%s", "api_key_last4=%s"]
        args += [vault.encrypt(api_key.strip()), api_key.strip()[-4:]]
    elif clear_key:
        sets += ["api_key_enc=NULL", "api_key_last4=NULL"]
    conn.execute(f"UPDATE llm_account SET {', '.join(sets)} WHERE id=%s", (*args, account_id))
    conn.commit()


def delete(conn, account_id: int) -> None:
    get_account(conn, account_id)
    conn.execute("DELETE FROM llm_account WHERE id=%s", (account_id,))
    if store.get(conn, SETTING) == str(account_id):
        store.delete(conn, SETTING)
    conn.commit()


def chat_config_for(conn, ref: int | str) -> ChatConfig:
    if ref == ENV:
        s, env = get_settings(), env_account()
        if not env:
            raise AccountError("the environment configuration is not set (LLM_BASE_URL)")
        if env.get("error"):
            raise AccountError(env["error"])
        if not s.llm_chat_model:
            raise AccountError("the environment configuration has no chat model (set LLM_CHAT_MODEL)")
        return ChatConfig(base_url=s.llm_base_url, model=s.llm_chat_model, label="Environment", trusted=True, key_plain=s.llm_api_key)
    account_id = int(ref)
    row = conn.execute("SELECT * FROM llm_account WHERE id=%s", (account_id,)).fetchone()
    if not row:
        raise AccountError("account not found")
    if not row["chat_model"]:
        raise AccountError("this account has no chat model configured")
    return ChatConfig(base_url=row["base_url"], model=row["chat_model"], label=row["name"], key_enc=row["api_key_enc"])


def triage_account(conn) -> int | str | None:
    """The account used for Tier 3: the chosen one (an id, or "env"), else the environment account when it can chat, else the
    only stored account that has a chat model."""
    chosen = store.get(conn, SETTING)
    env = env_account()
    env_usable = bool(env and env["chat_model"] and not env.get("error"))
    if chosen and (chosen != ENV or env_usable):      # a chosen "env" that is no longer configured falls back to the default
        return ENV if chosen == ENV else int(chosen)
    if env_usable:
        return ENV
    usable = [a for a in list_accounts(conn) if a["chat_model"]]
    return usable[0]["id"] if len(usable) == 1 else None


def set_triage_account(conn, account_id: int | str | None) -> None:
    if account_id is None:
        store.delete(conn, SETTING)
    else:
        ref = ENV if account_id == ENV else int(account_id)
        chat_config_for(conn, ref)
        store.put(conn, SETTING, str(ref))
    conn.commit()
