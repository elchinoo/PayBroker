"""The key/value settings store lives in agentpg-kit; this module keeps the old import path and adds row MACs for the keys in
`rowmac.AUTHORITY_KEYS` (the ones that choose a destination or change behaviour). Those are signed on write; a stored value whose MAC
is missing or wrong is treated as not set (logged once, counted) while the vault is unlocked. Every other key is the kit's plain store."""
from apgkit import settings_store as _kit

from . import rowmac

delete = _kit.delete


def get(conn, key: str) -> str | None:
    if key not in rowmac.AUTHORITY_KEYS:
        return _kit.get(conn, key)
    return rows(conn, key).get(key)


def put(conn, key: str, value: str) -> None:
    _kit.put(conn, key, value)
    if key in rowmac.AUTHORITY_KEYS:
        rowmac.seal(conn, "app_setting", "key=%s", (key,))


def rows(conn, *keys: str) -> dict[str, str]:
    """The stored values of `keys` that may be used: an authority key without a valid MAC is left out, as if it were not set."""
    found = conn.execute("SELECT key, value, mac FROM app_setting WHERE key = ANY(%s)", (list(keys),)).fetchall()
    return {r["key"]: r["value"] for r in found if r["key"] not in rowmac.AUTHORITY_KEYS or rowmac.verify_row("app_setting", r)}
