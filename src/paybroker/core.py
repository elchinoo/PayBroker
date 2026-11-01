"""This app's instances of the shared blocks (agentpg-kit), and the authorization model of PayBroker.

Permissions are what the code checks (`cases:review`); roles (given to people by the identity provider) and scopes (given to
services and local API tokens) are mapped onto them here. Merchant API keys are a separate kind of principal that can only
reach the payment API (`payments:write`, `vault:tokenize`), never an operator route.
"""
import dataclasses
import threading

from apgkit import metrics as metrics_mod
from apgkit.auth import AppAuth, Authenticator, LocalAuth, from_env
from apgkit.auth.rp import RelyingParty
from apgkit.db import Database
from apgkit.llm import LLMClient
from apgkit.netguard import NetGuard
from apgkit.vault import Vault

from . import __version__
from .config import bind_host, get_settings, pool_max, read_secret

APP = "paybroker"
COOKIE = "pb_session"
CSRF_HEADER = "x-paybroker-csrf"
SECRET_PREFIX, PUBLISHABLE_PREFIX = "sk_test_", "pk_test_"
ADMIN_SCOPE = "paybroker.admin"

db = Database(lambda: get_settings().database_url, vector=True, pool_max=pool_max())


class _RowMacVault(Vault):
    """The kit's vault, plus: when it is created with the admin password (a standalone install setting its first password, or after
    `reset-admin`) the rows that exist at that moment are signed, because no key existed before and nothing could have carried a MAC.
    Sealed boot does NOT do this (the vault is created at the first unseal): there `paybroker adopt-rowmacs` is the explicit, deliberate
    step, so a database whose vault records were wiped cannot launder planted rows by being re-initialised."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._sealing = threading.local()

    def initialize(self, conn, password: str) -> None:
        super().initialize(conn, password)
        if not getattr(self._sealing, "on", False):
            from . import rowmac
            rowmac.adopt_tables(conn)                  # in the caller's transaction: it commits right after

    def unlock_sealed(self, conn, passphrase):
        self._sealing.on = True
        try:
            return super().unlock_sealed(conn, passphrase)
        finally:
            self._sealing.on = False


vault = _RowMacVault("PAYBROKER")
netguard = NetGuard("PAYBROKER_ALLOW_PRIVATE_URLS")
llm = LLMClient(netguard, vault)
metrics = metrics_mod.Metrics(APP, __version__)

ALL = frozenset({"*"})
PERMISSIONS = {
    "payments:read": "see payments, fraud scores, cases, rules and analytics",
    "cases:review": "resolve flagged transactions and record chargebacks",
    "rules:manage": "create, edit, activate fraud rules and decide on suggestions",
    "merchants:manage": "merchants, their keys and webhooks, payment connectors",
    "settings:manage": "LLM accounts, operator tokens, the vault password",
    "training:export": "see training-data statistics and export the captured examples (admin only; the export holds derived features and labels)",
    "model:manage": "capture and retention settings, label-aging, upload, activate and roll back fraud models (admin only)",
    "data:reset": "wipe the transaction data and start fresh, keeping the configuration (admin only; cannot be undone)",
    "mcp:use": "use the MCP server",
    "audit:read": "read the tamper-evident audit ledger and verify its chain (admin only)",
    "metrics:scrape": "scrape /metrics",
    # merchant API keys
    "payments:write": "authorize, capture, void and refund payments (merchant secret key)",
    "vault:tokenize": "exchange a test card for a token (merchant publishable or secret key)",
}
ROLE_PERMISSIONS = {
    "admin": ALL,
    "risk_manager": frozenset({"payments:read", "cases:review", "rules:manage", "mcp:use"}),
    "analyst": frozenset({"payments:read", "cases:review", "mcp:use"}),
    "operator": frozenset({"payments:read", "merchants:manage", "mcp:use"}),
    "viewer": frozenset({"payments:read", "mcp:use"}),
}
SCOPE_PERMISSIONS = {
    "read": frozenset({"payments:read", "mcp:use"}), "metrics": frozenset({"metrics:scrape"}), "full": ALL,
    "paybroker.read": frozenset({"payments:read", "mcp:use"}), "paybroker.metrics": frozenset({"metrics:scrape"}),
    # provisioning by another service (ecoctl): merchants, their keys, webhooks and origins, and nothing else. Only service
    # tokens may hold it (api.require ignores it on a person's token).
    ADMIN_SCOPE: frozenset({"merchants:manage", "payments:read"}),
}
fed = from_env("PAYBROKER", APP)
if fed.rp is not None:
    # The kit decides "confidential or public client" by reading the client secret at import, which with sealed boot does not exist
    # yet. Rebuild the relying party so the secret is read at use time (empty still means a public client, PKCE only).
    fed = dataclasses.replace(fed, rp=RelyingParty(fed.issuer, fed.client_id, lambda: read_secret("PAYBROKER_OIDC_CLIENT_SECRET")))
APP_AUTH = AppAuth(APP, fed.client_id, ROLE_PERMISSIONS, SCOPE_PERMISSIONS)


def _on_reset(conn) -> None:
    conn.execute("UPDATE llm_account SET api_key_enc = NULL, api_key_last4 = NULL")
    conn.execute("UPDATE merchant SET webhook_secret_enc = NULL")
    conn.execute("DELETE FROM app_setting WHERE key = 'card_fingerprint_key_enc'")    # unreadable now: a new key (new fingerprints) on next use
    from . import rowmac
    rowmac.clear(conn)       # the data key is gone, no row MAC can verify: `paybroker adopt-rowmacs` signs afresh


local = LocalAuth(APP_AUTH, env_prefix="PAYBROKER", token_prefix="pbt_", token_scopes={"read", "metrics", "full"}, vault=vault,
                  on_reset=_on_reset)
# bearer_resolvers is filled in by auth.py (merchant keys need the cache and the pool that live there)
auth = Authenticator(APP_AUTH, local, db, cookie_name=COOKIE, verifier=fed.verifier, bearer_resolvers={},
                     bind_host=bind_host())
public_url_env = "PAYBROKER_PUBLIC_URL"
