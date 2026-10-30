"""This app's instances of the shared blocks (agentpg-kit), and the authorization model of PayBroker.

Permissions are what the code checks (`cases:review`); roles (given to people by the identity provider) and scopes (given to
services and local API tokens) are mapped onto them here. Merchant API keys are a separate kind of principal that can only
reach the payment API (`payments:write`, `vault:tokenize`), never an operator route.
"""
from apgkit import metrics as metrics_mod
from apgkit.auth import AppAuth, Authenticator, LocalAuth, from_env
from apgkit.db import Database
from apgkit.llm import LLMClient
from apgkit.netguard import NetGuard
from apgkit.vault import Vault

from . import __version__
from .config import bind_host, get_settings

APP = "paybroker"
COOKIE = "pb_session"
CSRF_HEADER = "x-paybroker-csrf"
SECRET_PREFIX, PUBLISHABLE_PREFIX = "sk_test_", "pk_test_"
ADMIN_SCOPE = "paybroker.admin"

db = Database(lambda: get_settings().database_url, vector=True, pool_max=get_settings().pool_max)
vault = Vault("PAYBROKER")
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
APP_AUTH = AppAuth(APP, fed.client_id, ROLE_PERMISSIONS, SCOPE_PERMISSIONS)


def _on_reset(conn) -> None:
    conn.execute("UPDATE llm_account SET api_key_enc = NULL, api_key_last4 = NULL")
    conn.execute("UPDATE merchant SET webhook_secret_enc = NULL")
    conn.execute("DELETE FROM app_setting WHERE key = 'card_fingerprint_key_enc'")    # unreadable now: a new key (new fingerprints) on next use


local = LocalAuth(APP_AUTH, env_prefix="PAYBROKER", token_prefix="pbt_", token_scopes={"read", "metrics", "full"}, vault=vault,
                  on_reset=_on_reset)
# bearer_resolvers is filled in by auth.py (merchant keys need the cache and the pool that live there)
auth = Authenticator(APP_AUTH, local, db, cookie_name=COOKIE, verifier=fed.verifier, bearer_resolvers={},
                     bind_host=bind_host())
public_url_env = "PAYBROKER_PUBLIC_URL"
