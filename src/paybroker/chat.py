"""One chat call to an OpenAI-compatible provider (non-streaming)."""
from . import vault
from .accounts import ChatConfig
from .llm_http import LLMError, answer_text, error_detail, post_json


def chat(cfg: ChatConfig, messages: list[dict], max_tokens: int = 700, temperature: float = 0.1, retries: int = 3) -> dict:
    try:
        key = cfg.api_key()
    except vault.VaultLocked:
        raise
    except vault.VaultError as exc:
        raise LLMError(str(exc)) from exc
    body = {"model": cfg.model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature}
    resp = post_json(cfg.base_url, "/chat/completions", key, body, trusted=cfg.trusted, retries=retries)
    if resp.status_code >= 400:
        raise LLMError(f"chat endpoint returned HTTP {resp.status_code}: {error_detail(resp, key)}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise LLMError("unexpected response from the chat endpoint (no choices[0].message.content)") from exc
    return {"text": answer_text(data, max_tokens), "usage": data.get("usage") if isinstance(data, dict) else None}
