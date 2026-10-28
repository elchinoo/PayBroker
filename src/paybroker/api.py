import contextlib
import sys
import threading
import time
from pathlib import Path

import psycopg
from apgkit import insights as insights_mod
from apgkit.auth import Principal, TokenError
from apgkit.auth.rp import safe_return_to
from apgkit.guard import SecurityGuard
from apgkit.web import is_https, mount_kit_static, public_url
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from . import __version__, auth, cards, core, cors, merchants, ml, payments, routing, rules, vault, worker
from . import metrics as m
from .config import get_settings
from .core import COOKIE, CSRF_HEADER, fed, local
from .db import close_pool, connect, merchant_conn, system_conn
from .mcp_server import mcp

STATIC = Path(__file__).resolve().parent / "static"
_announced = False
_stop = threading.Event()
_threads: list[threading.Thread] = []

mcp_app = mcp.streamable_http_app(streamable_http_path="/", host="0.0.0.0")


def _announce_setup_code() -> None:
    global _announced
    if not _announced:
        _announced = True
        print("\n=== PayBroker first-run setup ===\n"
              "Open the app in your browser and enter this one-time code to create the admin password:\n"
              f"    {local.setup_code()}\n"
              "(It changes every time the server restarts until setup is complete.)\n", file=sys.stderr, flush=True)


def _startup() -> None:
    """Seed connectors and default rules, unlock the vault if a platform secret exists, announce the setup code."""
    try:
        with connect(retries=2) as conn:
            routing.seed_connectors(conn)
            rules.seed_defaults(conn)
            if fed.enabled and not vault.is_initialized(conn):
                core.vault.initialize_with_platform_secret(conn)   # single sign-on only: no admin password to wrap the key with
                conn.commit()
            vault.try_unlock_with_platform_secret(conn)
            if local.needs_setup(conn) and not fed.enabled:
                _announce_setup_code()
    except Exception:  # noqa: BLE001 - the database may be asleep; everything retries on the first request
        pass


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    _stop.clear()
    _startup()
    role = get_settings().role
    if role in {"worker", "all"}:
        t = threading.Thread(target=worker.loop, args=(_stop,), daemon=True, name="paybroker-worker")
    else:
        t = threading.Thread(target=worker.flush_loop, args=(_stop,), daemon=True, name="paybroker-flush")
    t.start()
    _threads.append(t)
    try:
        async with mcp_app.router.lifespan_context(mcp_app):
            yield
    finally:
        _stop.set()
        for th in _threads:
            th.join(timeout=3)
        _threads.clear()
        close_pool()


# ---- authentication ------------------------------------------------------------------------------------------
# The mechanics (local admin, operator tokens, federated JWTs, sessions, rate limiting) are agentpg-kit's `Authenticator`;
# here we only name the permission each route needs. Merchant API keys are a separate kind of principal.
_LEGACY = {"read": "payments:read", "full": "settings:manage", "metrics": "metrics:scrape"}


def _can(p: Principal, permission: str) -> bool:
    """`p.can`, except that the provisioning scope counts for services and tokens only: a person's token (sub_type user) that
    happens to carry `paybroker.admin` gets what their roles give and nothing from that scope."""
    if p.kind == "user" and core.ADMIN_SCOPE in p.scopes:
        held = core.APP_AUTH.permissions_for(roles=p.roles, scopes=p.scopes - {core.ADMIN_SCOPE})
        return "*" in held or permission in held
    return p.can(permission)


def require(*perms: str):
    """Operator dependency: the caller needs ANY of the permissions (or an old scope name: read, full, metrics).
    A merchant key never passes: it carries only payment permissions, and these routes ask for operator ones."""
    wanted = [_LEGACY.get(p, p) for p in (perms or ("payments:read",))]

    def dep(request: Request) -> Principal:
        p = core.auth.authenticate(request)
        if p is None:
            raise HTTPException(401, "authentication required")
        if p.kind == "merchant" or not any(_can(p, w) for w in wanted):
            raise HTTPException(403, f"this credential cannot do that (it needs one of: {', '.join(wanted)})")
        return p

    return dep


def merchant_auth(permission: str):
    """Merchant dependency: a secret key, or where allowed a publishable key. Cookies and operator credentials never count."""

    def dep(request: Request) -> Principal:
        p = core.auth.authenticate(request, allow_cookie=False)
        if p is None or p.kind != "merchant":
            if p is None:
                raise HTTPException(401, "a merchant API key is required (Authorization: Bearer sk_test_...)")
            raise HTTPException(403, "this endpoint takes a merchant API key; operator credentials cannot make payments")
        if not p.can(permission):
            raise HTTPException(403, f"this key cannot do that (it is a {'publishable' if 'publishable' in p.scopes else 'secret'} key)")
        return p

    return dep


def _mid(p: Principal) -> str:
    return p.extra["merchant_id"]


def _client(request: Request) -> str:
    return core.auth.client(request)


def _db_unavailable() -> HTTPException:
    return core.auth.db_unavailable()


class McpGuard:
    """MCP never accepts browser cookies or merchant keys; the credential must carry `mcp:use`."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/mcp"):
            return await self.app(scope, receive, send)
        if scope["path"] == "/mcp":
            scope["path"] = scope["raw_path"] = "/mcp/"   # a mount redirect could point at http:// behind a TLS proxy
        request = Request(scope, receive)
        try:
            principal = core.auth.authenticate(request, allow_cookie=False)
        except HTTPException as exc:
            return await JSONResponse({"error": exc.detail}, status_code=exc.status_code)(scope, receive, send)
        if principal is None:
            return await JSONResponse({"error": "authentication required"}, status_code=401)(scope, receive, send)
        if principal.kind == "merchant" or not principal.can("mcp:use"):
            return await JSONResponse({"error": "this credential cannot use MCP"}, status_code=403)(scope, receive, send)
        await self.app(scope, receive, send)


app = FastAPI(title="PayBroker", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(McpGuard)


class _Guard(SecurityGuard):
    """The kit's guard caps operator bodies at 64 KB. A model upload is bigger (a retrained model is tens of KB, an uploaded one
    may be up to ml.MAX_MODEL_BYTES), so exactly that one route gets its own, larger cap; everything else keeps the default."""

    UPLOAD = "/api/admin/models"

    def __init__(self, app, **kw):
        super().__init__(app, **kw)
        self._upload = SecurityGuard(app, **{**kw, "max_body": ml.MAX_MODEL_BYTES + 16 * 1024})

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"] == "POST" and scope["path"] == self.UPLOAD:
            return await self._upload(scope, receive, send)
        return await super().__call__(scope, receive, send)


app.add_middleware(_Guard, csrf_header=CSRF_HEADER, csrf_prefixes=("/api/",), body_prefixes=("/api/", "/v1/", "/3ds/"),
                   observe=core.metrics.observe)


def _origin_listed(origin: str) -> bool:
    with system_conn() as conn:       # a CORS preflight carries no credential: it asks across all merchants
        ok = merchants.origin_listed(conn, origin)
        conn.rollback()
    return ok


_origins = cors.OriginLookup(_origin_listed)
app.add_middleware(cors.BrowserCors, lookup=_origins)   # outermost: answers a preflight before any other layer sees it
app.mount("/mcp", mcp_app)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
mount_kit_static(app)


@app.get("/healthz")
def healthz():
    return {"status": "ok", "version": __version__}


@app.get("/readyz")
def readyz():
    try:
        with connect(retries=1) as conn:
            conn.execute("SELECT 1")
        return {"status": "ready"}
    except Exception:  # noqa: BLE001 - never echo connection details
        return JSONResponse({"status": "waking-database"}, status_code=503)


@app.get("/metrics", dependencies=[Depends(require("metrics:scrape"))])
def prometheus_metrics():
    body, ctype = m.render()
    return Response(body, media_type=ctype)


@app.get("/api/insights", dependencies=[Depends(require("settings:manage"))])
def database_insights(order: str = Query("total", max_length=10), limit: int = Query(20, ge=1, le=100)):
    """What EDB's query statistics, tuner and index advisor know about this database (admin only: statements show the workload).
    Each part degrades to {"available": false, "reason": ...} on a database without the extension or privilege."""
    order = order if order in ("total", "mean", "calls") else "total"
    with connect() as conn:
        out = insights_mod.insights_payload(conn, order, limit)
        conn.rollback()
    return out


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


# ---- merchant API (/v1) -------------------------------------------------------------------------------------------
class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CardBody(_Body):
    number: str = Field(max_length=25)
    exp_month: int = Field(ge=1, le=12)
    exp_year: int = Field(ge=0, le=2100)
    cvc: str | None = Field(None, max_length=4)


class PaymentMethod(_Body):
    type: str = Field(pattern="^card$")
    card_token: str = Field(min_length=8, max_length=80)
    cvv_check: bool = True
    cardholder_name: str | None = Field(None, max_length=100, description="name on the card; compared with customer.name (NAME_MISMATCH signal), never stored")


class Customer(_Body):
    id: str = Field(min_length=1, max_length=64)
    name: str | None = Field(None, max_length=100, description="the customer's account name; compared with the cardholder name (NAME_MISMATCH signal), never stored")
    email: str | None = Field(None, max_length=254)
    phone: str | None = Field(None, max_length=30)
    ip_address: str = Field(max_length=45)
    account_age_hours: float | None = Field(None, ge=0, le=1_000_000, description="how old the customer's account is, if the merchant knows")


class Telemetry(_Body):
    device_fingerprint: str | None = Field(None, max_length=80)
    user_agent: str | None = Field(None, max_length=300)
    checkout_duration_ms: int | None = Field(None, ge=0, le=86_400_000)
    pasted_card_number: bool = False
    ip_country: str | None = Field(None, min_length=2, max_length=2)


class Shipping(_Body):
    line1: str | None = Field(None, max_length=200)
    city: str | None = Field(None, max_length=100)
    state: str | None = Field(None, max_length=50)
    postal_code: str | None = Field(None, max_length=20)
    country: str | None = Field(None, min_length=2, max_length=2)


class AuthorizeBody(_Body):
    merchant_id: str | None = Field(None, max_length=64)
    idempotency_key: str = Field(min_length=8, max_length=128)
    amount: int = Field(ge=1, le=99_999_999)
    currency: str = Field(pattern="^[A-Za-z]{3}$")
    payment_method: PaymentMethod
    customer: Customer
    telemetry: Telemetry = Telemetry()
    shipping_address: Shipping | None = None
    capture_method: str | None = Field(None, pattern="^(manual|automatic)$")
    return_url: str | None = Field(None, max_length=500, description="where the hosted 3-D Secure page sends the customer back; on an allowed origin")


class AmountBody(_Body):
    amount: int | None = Field(None, ge=1, le=99_999_999)


def _merchant(conn, merchant_id: str) -> dict:
    return merchants.get(conn, merchant_id)


_merchant_cache: dict[str, tuple[dict, float]] = {}


def merchant_cached(conn, merchant_id: str) -> dict:
    hit = _merchant_cache.get(merchant_id)
    if hit and time.monotonic() - hit[1] < 5:
        return hit[0]
    row = merchants.get(conn, merchant_id)
    conn.commit()
    _merchant_cache[merchant_id] = (row, time.monotonic())
    return row


def forget_merchants() -> None:
    _merchant_cache.clear()
    _origins.clear()
    auth.forget_keys()


def _paying(fn):
    """Run a payments.* call, mapping PaymentError to its HTTP status."""
    try:
        return fn()
    except payments.PaymentError as exc:
        return JSONResponse(exc.body(), status_code=exc.status)
    except cards.CardError as exc:
        return JSONResponse({"error": {"code": "INVALID_CARD", "message": str(exc)}}, status_code=422)
    except (psycopg.OperationalError, RuntimeError) as exc:
        raise _db_unavailable() from exc


@app.exception_handler(payments.PaymentError)
def _payment_error(_: Request, exc: payments.PaymentError):
    return JSONResponse(exc.body(), status_code=exc.status)


def tokenize_auth(request: Request, p: Principal = Depends(merchant_auth("vault:tokenize"))) -> Principal:
    """A browser (a request with an Origin) may tokenize only with a publishable key, and only from an origin the key's own merchant
    lists. Passing records the origin for the CORS middleware, which then adds the headers; failing answers 403 without any. A caller
    with no Origin header (a server, curl) is not a browser and is not affected."""
    origin = request.headers.get("origin")
    if origin is None:
        return p
    try:
        with merchant_conn(_mid(p)) as conn:
            listed = merchant_cached(conn, _mid(p))["allowed_origins"]
    except (psycopg.OperationalError, RuntimeError) as exc:
        raise _db_unavailable() from exc
    if "publishable" not in p.scopes:
        raise payments.PaymentError(403, "origin_not_allowed", "a secret key must never be used from a browser: use a publishable key")
    if origin not in listed:
        raise payments.PaymentError(403, "origin_not_allowed", "this origin is not in the merchant's allowed origins")
    request.state.cors_origin = origin
    return p


@app.post("/v1/vault/tokens", status_code=201)
def tokenize(body: CardBody, p: Principal = Depends(tokenize_auth)):
    """Exchange a TEST card for a token. Meant to be called from the customer's browser with a publishable key (CORS: see cors.py)."""
    def run():
        with merchant_conn(_mid(p)) as conn:
            return cards.tokenize(conn, _mid(p), body.number, body.exp_month, body.exp_year, body.cvc)
    return _paying(run)


@app.post("/v1/payments/authorize")
def authorize(body: AuthorizeBody, request: Request, p: Principal = Depends(merchant_auth("payments:write"))):
    if body.merchant_id and body.merchant_id != _mid(p):
        return JSONResponse({"error": {"code": "MERCHANT_MISMATCH", "message": "merchant_id does not belong to this API key"}}, status_code=403)
    data = body.model_dump(mode="json")
    data["currency"] = data["currency"].upper()
    data.pop("merchant_id", None)
    url = public_url(request, core.public_url_env)

    def run():
        with merchant_conn(_mid(p)) as conn:
            status, out = payments.authorize(conn, merchant_cached(conn, _mid(p)), data, url)
        return JSONResponse(out, status_code=status)
    return _paying(run)


@app.get("/v1/payments/{payment_id}")
def get_payment(payment_id: str, request: Request, p: Principal = Depends(merchant_auth("payments:write"))):
    def run():
        with merchant_conn(_mid(p)) as conn:
            tx, fa, ch = payments.load(conn, payment_id, _mid(p))
            conn.rollback()
        return JSONResponse(payments.render(tx, fa, ch, public_url(request, core.public_url_env)))
    return _paying(run)


@app.get("/v1/payments")
def list_payments(limit: int = Query(20, ge=1, le=100), status: str | None = Query(None, max_length=20),
                  p: Principal = Depends(merchant_auth("payments:write"))):
    with merchant_conn(_mid(p)) as conn:
        rows = conn.execute(
            "SELECT payment_id, status, amount, currency, fraud_score, psp_selected, created_at FROM transaction "
            "WHERE merchant_id=%s AND (%s::text IS NULL OR status=%s) ORDER BY created_at DESC LIMIT %s",
            (_mid(p), status, status, limit)).fetchall()
        conn.rollback()
    return {"items": rows}


def _action(name: str):
    def endpoint(payment_id: str, request: Request, body: AmountBody | None = None, p: Principal = Depends(merchant_auth("payments:write"))):
        url = public_url(request, core.public_url_env)

        def run():
            with merchant_conn(_mid(p)) as conn:
                mer = merchant_cached(conn, _mid(p))
                if name == "void":
                    out = payments.void(conn, mer, payment_id, url)
                else:
                    out = getattr(payments, name)(conn, mer, payment_id, body.amount if body else None, url)
            return JSONResponse(out)
        return _paying(run)
    return endpoint


app.post("/v1/payments/{payment_id}/capture")(_action("capture"))
app.post("/v1/payments/{payment_id}/void")(_action("void"))
app.post("/v1/payments/{payment_id}/refund")(_action("refund"))


# ---- hosted 3-D Secure page (simulated issuer) -----------------------------------------------------------------------
class ThreeDSBody(_Body):
    outcome: str = Field(pattern="^(succeeded|failed)$")


@app.get("/3ds/{challenge_id}")
def threeds_page(challenge_id: str):
    return FileResponse(STATIC / "3ds.html")


@app.get("/api/3ds/{challenge_id}")
def threeds_info(challenge_id: str):
    with connect() as conn:
        row = conn.execute(
            "SELECT c.status, c.expires_at, t.amount, t.currency, m.name AS merchant FROM three_ds_challenge c "
            "JOIN transaction t ON t.id=c.transaction_id JOIN merchant m ON m.id=t.merchant_id WHERE c.id=%s", (challenge_id,)).fetchone()
    if not row:
        raise HTTPException(404, "no such challenge")
    return row


@app.post("/api/3ds/{challenge_id}/complete")
def threeds_complete(challenge_id: str, body: ThreeDSBody, request: Request):
    """TEST MODE: the page lets the tester pick the outcome. A real integration receives the issuer's result instead."""
    url = public_url(request, core.public_url_env)

    def run():
        with connect() as conn:
            out = payments.complete_3ds(conn, challenge_id, body.outcome, url)
        return JSONResponse(out)
    return _paying(run)


# ---- operator: session, setup, sign-in, tokens ---------------------------------------------------------------------
class SetupBody(_Body):
    code: str = Field(max_length=64)
    password: str = Field(max_length=256)


class LoginBody(_Body):
    password: str | None = Field(None, max_length=256)
    api_key: str | None = Field(None, max_length=512)


class UnlockBody(_Body):
    password: str = Field(max_length=256)


class PasswordBody(_Body):
    current: str | None = Field(None, max_length=256)
    new: str = Field(max_length=256)


class TokenBody(_Body):
    name: str = Field(min_length=1, max_length=60)
    scope: str = Field(pattern="^(read|metrics|full)$")


def _start_session(conn, request: Request, response: Response, **who) -> None:
    response.set_cookie(COOKIE, local.create_session(conn, **who), httponly=True, samesite="lax" if who else "strict",
                        secure=is_https(request), max_age=12 * 3600, path="/")


def _guard_attempts(request: Request) -> str:
    client = _client(request)
    if core.auth.limiter.limited(client):
        raise HTTPException(429, "too many failed attempts; wait a minute")
    return client


@app.get("/api/session")
def session_state(request: Request):
    """Public: tells the UI which screen to show. Reveals no secrets."""
    principal = None
    try:
        principal = core.auth.authenticate(request)
        with connect(retries=1) as conn:
            setup = local.needs_setup(conn) and not fed.enabled
            v = vault.status(conn)
            has_password = local.admin_configured(conn)
    except HTTPException:
        raise
    except (psycopg.Error, RuntimeError) as exc:
        raise _db_unavailable() from exc
    if setup:
        _announce_setup_code()
    state = "setup_required" if setup else "logged_in" if principal and principal.kind != "merchant" else "logged_out"
    out = {"state": state, "vault": v, "has_admin_password": has_password, "version": __version__,
           "password_login": has_password and (fed.local_login or not fed.enabled),
           "key_login": local.static_key_configured() and (fed.local_login or not fed.enabled),
           "sso": {"issuer": fed.issuer, "login_url": "/auth/login"} if fed.enabled else None}
    if principal and state == "logged_in":
        out["user"] = {"name": principal.name, "email": principal.email, "source": principal.source, "roles": sorted(principal.roles),
                       "permissions": ["*"] if "*" in principal.permissions else sorted(principal.permissions)}
    return out


@app.get("/auth/login")
def sso_login(request: Request, next: str | None = None):
    if not fed.enabled:
        raise HTTPException(404, "single sign-on is not configured")
    try:
        with connect(retries=1) as conn:
            url = fed.rp.begin(conn, public_url(request, core.public_url_env) + "/auth/callback", next)
    except psycopg.Error as exc:
        raise _db_unavailable() from exc
    except Exception:  # noqa: BLE001 - the identity provider may be unreachable
        return RedirectResponse("/?sso_error=the identity provider is unavailable", status_code=303)
    return RedirectResponse(url, status_code=303)


@app.get("/auth/callback")
def sso_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if not fed.enabled:
        raise HTTPException(404, "single sign-on is not configured")
    if error or not code or not state:
        return RedirectResponse("/?sso_error=the sign-in was cancelled or failed", status_code=303)
    try:
        with connect(retries=1) as conn:
            claims, back, id_token = fed.rp.finish(conn, public_url(request, core.public_url_env) + "/auth/callback", code, state)
            roles = [r for r in claims.get("roles", []) if isinstance(r, str)]
            if not core.APP_AUTH.permissions_for(roles=roles):
                return RedirectResponse("/?sso_error=your account has no role in PayBroker; ask an administrator", status_code=303)
            resp = RedirectResponse(safe_return_to(back), status_code=303)
            _start_session(conn, request, resp, subject=str(claims["sub"]), name=str(claims.get("name") or ""),
                           email=str(claims.get("email") or ""), roles=roles, source="oidc", id_token_hint=id_token)
            return resp
    except TokenError as exc:
        return RedirectResponse("/?sso_error=" + str(exc)[:120], status_code=303)


@app.post("/api/setup")
def setup(body: SetupBody, request: Request, response: Response):
    client = _guard_attempts(request)
    try:
        with connect(retries=1) as conn:
            try:
                local.complete_setup(conn, body.code, body.password)
            except PermissionError as exc:
                core.auth.limiter.record(client)
                raise HTTPException(403, str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            _start_session(conn, request, response)
    except (psycopg.Error, RuntimeError) as exc:
        raise _db_unavailable() from exc
    return {"ok": True}


@app.post("/api/login")
def login(body: LoginBody, request: Request, response: Response):
    client = _guard_attempts(request)
    try:
        with connect(retries=1) as conn:
            ok = False
            if body.password is not None:
                ok = local.login_password(conn, body.password)
            elif body.api_key is not None and local.static_key_matches(body.api_key):
                ok = True
                vault.try_unlock_with_platform_secret(conn)
            if not ok:
                core.auth.limiter.record(client)
                raise HTTPException(401, "invalid credentials")
            _start_session(conn, request, response)
    except (psycopg.Error, RuntimeError) as exc:
        raise _db_unavailable() from exc
    return {"ok": True}


@app.post("/api/logout")
def logout(request: Request, response: Response):
    out: dict = {"ok": True}
    try:
        with connect(retries=1) as conn:
            row = local.delete_session(conn, request.cookies.get(COOKIE))
        if row and row["source"] == "oidc" and fed.enabled:
            with contextlib.suppress(Exception):
                out["logout_url"] = fed.rp.logout_url(row["id_token_hint"], public_url(request, core.public_url_env) + "/")
    except (psycopg.Error, RuntimeError):
        pass  # the cookie is cleared regardless; the session row expires on its own
    response.delete_cookie(COOKIE, path="/")
    return out


@app.post("/api/vault/unlock", dependencies=[Depends(require("settings:manage"))])
def unlock_vault(body: UnlockBody, request: Request):
    client = _guard_attempts(request)
    with connect() as conn:
        if not vault.is_initialized(conn):
            raise HTTPException(409, "no secrets are stored yet; set an admin password first")
        if not vault.unlock_with_password(conn, body.password):
            core.auth.limiter.record(client)
            raise HTTPException(403, "wrong password")   # 403, not 401: the session itself is still valid
        conn.commit()
    return {"ok": True}


@app.post("/api/admin-password", dependencies=[Depends(require("settings:manage"))])
def admin_password(body: PasswordBody, request: Request):
    client = _guard_attempts(request)
    with connect() as conn:
        try:
            if local.admin_configured(conn):
                if body.current is None:
                    raise HTTPException(422, "current password is required")
                local.change_password(conn, body.current, body.new)
            else:
                local.set_admin_password(conn, body.new)
        except PermissionError as exc:
            core.auth.limiter.record(client)
            raise HTTPException(403, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        conn.execute("DELETE FROM web_session")
        conn.commit()
    return {"ok": True, "signed_out_everywhere": True}


@app.get("/api/tokens", dependencies=[Depends(require("settings:manage"))])
def list_tokens():
    with connect() as conn:
        return local.list_tokens(conn)


@app.post("/api/tokens", dependencies=[Depends(require("settings:manage"))])
def create_token(body: TokenBody):
    with connect() as conn:
        token_id, raw = local.create_token(conn, body.name.strip(), body.scope)
    return {"id": token_id, "token": raw, "scope": body.scope, "name": body.name.strip()}


@app.delete("/api/tokens/{token_id}", dependencies=[Depends(require("settings:manage"))])
def revoke_token(token_id: int):
    with connect() as conn:
        if not local.revoke_token(conn, token_id):
            raise HTTPException(404, "token not found or already revoked")
    return {"revoked": token_id}


from . import api_admin, api_training  # noqa: E402  (need `app`, `require`, `forget_merchants` defined above)

app.include_router(api_admin.build_router(require, forget_merchants, lambda request: public_url(request, core.public_url_env)))
app.include_router(api_training.build_router(require))
