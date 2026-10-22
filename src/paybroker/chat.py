"""One chat call to an OpenAI-compatible provider (non-streaming)."""
from . import vault
from .accounts import ChatConfig
from .llm_http import LLMError, error_detail, post_json


def chat(cfg: ChatConfig, messages: list[dict], max_tokens: int = 700, temperature: float = 0.1) -> dict:
    try:
        key = cfg.api_key()
    except vault.VaultLocked:
        raise
    except vault.VaultError as exc:
        raise LLMError(str(exc)) from exc
    body = {"model": cfg.model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature}
    resp = post_json(cfg.base_url, "/chat/completions", key, body, trusted=False)
    if resp.status_code >= 400:
        raise LLMError(f"chat endpoint returned HTTP {resp.status_code}: {error_detail(resp, key)}")
    try:
        data = resp.json()
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise LLMError("unexpected response from the chat endpoint (no choices[0].message.content)") from exc
    if not isinstance(text, str) or not text.strip():
        raise LLMError("the chat endpoint returned an empty answer")
    return {"text": text.strip(), "usage": data.get("usage")}
