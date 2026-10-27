"""Operator API: dashboard, payments, fraud cases, rules and suggestions, blacklist, merchants and their keys, connectors,
LLM accounts, demo traffic. Every route names the permission it needs (see core.PERMISSIONS)."""
import time
from typing import Literal

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

from . import (
    accounts,
    auth,
    cases,
    demo,
    insights,
    maintenance,
    merchants,
    origins,
    rules,
    suggest,
    triage_limits,
    vault,
)
from .chat import chat as run_chat
from .db import connect
from .llm_http import LLMError
from .netguard import UnsafeURL


class _B(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TriageLimitsBody(_B):
    """Bounds live in triage_limits.BOUNDS; they are repeated here so the API documents them and answers 422 before touching the database."""
    rpm: int | None = Field(None, ge=0, le=600)
    daily_cap: int | None = Field(None, ge=0, le=1_000_000)
    group_minutes: int | None = Field(None, ge=0, le=1440)
    pause_minutes: int | None = Field(None, ge=1, le=1440)


class ResolveBody(_B):
    decision: str = Field(max_length=30)
    note: str | None = Field(None, max_length=1000)


class ChargebackBody(_B):
    amount: int | None = Field(None, ge=1, le=99_999_999)
    reason: str = Field("", max_length=200)
    reason_code: str | None = Field(None, max_length=20, description="network reason code, e.g. 10.4 or 4837; fraud codes label the payment as fraud")


class RuleBody(_B):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field("", max_length=300)
    definition: dict
    action: str = Field(pattern="^(decline|challenge)$")


class RulePatch(_B):
    enabled: bool


class DecideBody(_B):
    approve: bool


class BlacklistBody(_B):
    kind: str = Field(pattern="^(ip|device|card_fingerprint|bin|email_domain)$")
    value: str = Field(min_length=1, max_length=200)
    reason: str = Field("", max_length=200)


class MerchantBody(_B):
    name: str = Field(min_length=1, max_length=100)
    settings: dict | None = None
    allowed_origins: list[str] | None = Field(None, max_length=origins.MAX_ORIGINS, description="exact browser origins, e.g. https://shop.example.com")


class MerchantPatch(_B):
    name: str | None = Field(None, max_length=100)
    status: str | None = Field(None, pattern="^(active|suspended)$")
    settings: dict | None = None
    allowed_origins: list[str] | None = Field(None, max_length=origins.MAX_ORIGINS, description="replaces the whole list; [] removes browser access")


class WebhookBody(_B):
    url: str | None = Field(None, max_length=500)


class KeyBody(_B):
    kind: str = Field(pattern="^(secret|publishable)$")
    name: str = Field("", max_length=60)


class ConnectorPatch(_B):
    enabled: bool


class AccountBody(_B):
    name: str = Field(min_length=1, max_length=60)
    provider: str = Field(max_length=30)
    base_url: str = Field(max_length=300)
    api_key: str | None = Field(None, max_length=500)
    clear_key: bool = False
    chat_model: str | None = Field(None, max_length=100)


class TriageAccountBody(_B):
    account_id: int | Literal["env"] | None = None


class TestBody(_B):
    target: Literal["chat"] = "chat"


class ResetBody(_B):
    confirm: str = Field(max_length=100, description=f'the exact phrase "{maintenance.PHRASE}"')
    include_training_data: bool = True
    include_rule_suggestions: bool = True


class DemoBody(_B):
    count: int = Field(60, ge=1, le=500)
    seed: int = Field(7, ge=0, le=10**9)


def _who(p) -> str:
    """The audit label of a caller: a service is named by its client id (stable), a person by their name."""
    return p.subject[:60] if p.kind == "service" else p.actor


def build_router(require, forget_merchants, public_url) -> APIRouter:
    r = APIRouter(prefix="/api")
    read, review, manage_rules = Depends(require("payments:read")), Depends(require("cases:review")), Depends(require("rules:manage"))
    merch, settings = Depends(require("merchants:manage")), Depends(require("settings:manage"))
    wipe = Depends(require("data:reset"))

    # ---- dashboard and payments ------------------------------------------------------------------------------
    @r.get("/overview", dependencies=[read])
    def overview(days: int = Query(7, ge=1, le=90)):
        with connect() as conn:
            out = insights.overview(conn, days)
            conn.rollback()
        return out

    @r.get("/payments", dependencies=[read])
    def payments(status: str | None = Query(None, max_length=20), merchant_id: str | None = Query(None, max_length=60),
                 min_score: int | None = Query(None, ge=0, le=1000), decision: str | None = Query(None, pattern="^(APPROVE|CHALLENGE|DECLINE)$"),
                 q: str | None = Query(None, max_length=80), limit: int = Query(25, ge=1, le=100), offset: int = Query(0, ge=0, le=100_000)):
        with connect() as conn:
            rows = insights.search_payments(conn, status=status, merchant_id=merchant_id, min_score=min_score, decision=decision, q=q,
                                            limit=limit, offset=offset)
            conn.rollback()
        return {"items": rows}

    @r.get("/payments/{payment_id}", dependencies=[read])
    def payment(payment_id: str):
        with connect() as conn:
            out = insights.payment_detail(conn, payment_id)
            conn.rollback()
        if out is None:
            raise HTTPException(404, "payment not found")
        return out

    @r.post("/payments/{payment_id}/chargeback", dependencies=[review])
    def chargeback(payment_id: str, body: ChargebackBody):
        with connect() as conn:
            try:
                return cases.record_chargeback(conn, payment_id, body.amount, body.reason, body.reason_code) | {"ok": True}
            except cases.CaseError as exc:
                raise HTTPException(422, str(exc)) from exc
            finally:
                conn.commit()

    # ---- fraud cases -----------------------------------------------------------------------------------------
    @r.get("/cases", dependencies=[read])
    def list_cases(status: str = Query("open", pattern="^(open|resolved|all)$")):
        with connect() as conn:
            rows = insights.list_cases(conn, None if status == "all" else status)
            conn.rollback()
        return {"items": rows}

    @r.post("/cases/{case_id}/resolve")
    def resolve_case(case_id: int, body: ResolveBody, p=review):
        with connect() as conn:
            try:
                return cases.resolve(conn, case_id, body.decision, body.note, p.actor)
            except cases.CaseError as exc:
                conn.rollback()
                raise HTTPException(422, str(exc)) from exc

    # ---- rules, suggestions, blacklist -------------------------------------------------------------------------
    @r.get("/rules", dependencies=[read])
    def list_rules():
        with connect() as conn:
            out = {"items": insights.list_rules(conn), "fields": sorted(rules.FIELDS), "operators": sorted(rules.OPS)}
            conn.rollback()
        return out

    @r.post("/rules", status_code=201)
    def create_rule(body: RuleBody, p=manage_rules):
        try:
            rules.validate(body.definition)
        except rules.RuleError as exc:
            raise HTTPException(422, str(exc)) from exc
        with connect() as conn:
            try:
                row = conn.execute("INSERT INTO fraud_rule (name, description, definition, action, created_by) VALUES (%s,%s,%s,%s,%s) "
                                   "RETURNING id", (body.name.strip(), body.description, Jsonb(body.definition), body.action,
                                                    p.actor)).fetchone()
            except psycopg.errors.UniqueViolation as exc:
                conn.rollback()
                raise HTTPException(409, "a rule with that name already exists") from exc
            conn.commit()
        rules.invalidate()
        return {"id": row["id"]}

    @r.patch("/rules/{rule_id}", dependencies=[manage_rules])
    def patch_rule(rule_id: int, body: RulePatch):
        with connect() as conn:
            row = conn.execute("UPDATE fraud_rule SET enabled=%s WHERE id=%s RETURNING id", (body.enabled, rule_id)).fetchone()
            conn.commit()
        if not row:
            raise HTTPException(404, "rule not found")
        rules.invalidate()
        return {"id": rule_id, "enabled": body.enabled}

    @r.delete("/rules/{rule_id}", dependencies=[manage_rules])
    def delete_rule(rule_id: int):
        with connect() as conn:
            row = conn.execute("DELETE FROM fraud_rule WHERE id=%s RETURNING id", (rule_id,)).fetchone()
            conn.commit()
        if not row:
            raise HTTPException(404, "rule not found")
        rules.invalidate()
        return {"deleted": rule_id}

    @r.get("/suggestions", dependencies=[read])
    def list_suggestions(status: str = Query("pending", pattern="^(pending|approved|rejected|all)$")):
        with connect() as conn:
            out = insights.list_suggestions(conn, None if status == "all" else status)
            conn.rollback()
        return {"items": out}

    @r.post("/suggestions/run", dependencies=[manage_rules])
    def run_suggestions():
        with connect() as conn:
            try:
                return suggest.run(conn)
            except accounts.AccountError as exc:
                raise HTTPException(422, str(exc)) from exc

    @r.post("/suggestions/{suggestion_id}/decide")
    def decide_suggestion(suggestion_id: int, body: DecideBody, p=manage_rules):
        with connect() as conn:
            try:
                return suggest.decide(conn, suggestion_id, body.approve, p.actor)
            except rules.RuleError as exc:
                conn.rollback()
                raise HTTPException(422, str(exc)) from exc

    @r.get("/blacklist", dependencies=[read])
    def blacklist():
        with connect() as conn:
            rows = conn.execute("SELECT id, kind, value, reason, source, created_at FROM blacklist ORDER BY id DESC LIMIT 500").fetchall()
            conn.rollback()
        return {"items": rows}

    @r.post("/blacklist", status_code=201, dependencies=[review])
    def add_blacklist(body: BlacklistBody):
        with connect() as conn:
            row = conn.execute("INSERT INTO blacklist (kind, value, reason) VALUES (%s,%s,%s) ON CONFLICT (kind, value) DO NOTHING RETURNING id",
                               (body.kind, body.value.strip(), body.reason)).fetchone()
            conn.commit()
        if not row:
            raise HTTPException(409, "that entry is already blocked")
        rules.invalidate()
        return {"id": row["id"]}

    @r.delete("/blacklist/{entry_id}", dependencies=[review])
    def delete_blacklist(entry_id: int):
        with connect() as conn:
            row = conn.execute("DELETE FROM blacklist WHERE id=%s RETURNING id", (entry_id,)).fetchone()
            conn.commit()
        if not row:
            raise HTTPException(404, "entry not found")
        rules.invalidate()
        return {"deleted": entry_id}

    # ---- merchants ---------------------------------------------------------------------------------------------
    def _merchant_call(fn):
        try:
            return fn()
        except merchants.NotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        except merchants.Duplicate as exc:
            raise HTTPException(409, str(exc)) from exc
        except (merchants.MerchantError, vault.VaultError, UnsafeURL) as exc:
            raise HTTPException(422, str(exc)) from exc

    @r.get("/merchants", dependencies=[read])
    def list_merchants(name: str | None = Query(None, min_length=1, max_length=100, description="only the merchant with exactly this name")):
        with connect() as conn:
            out = merchants.listing(conn, name)
            conn.rollback()
        return {"items": out, "defaults": merchants.DEFAULTS}

    @r.post("/merchants", status_code=201)
    def create_merchant(body: MerchantBody, p=merch):
        def run():
            with connect() as conn:
                return merchants.create(conn, body.name, body.settings, body.allowed_origins, actor=_who(p))
        return _merchant_call(run)

    @r.get("/merchants/{merchant_id}", dependencies=[read])
    def get_merchant(merchant_id: str):
        def run():
            with connect() as conn:
                out = merchants.get(conn, merchant_id)
                out["keys"] = auth.list_merchant_keys(conn, merchant_id)
                conn.rollback()
                return out
        return _merchant_call(run)

    @r.patch("/merchants/{merchant_id}")
    def patch_merchant(merchant_id: str, body: MerchantPatch, p=merch):
        def run():
            with connect() as conn:
                out = merchants.update(conn, merchant_id, name=body.name, status=body.status, settings=body.settings,
                                       allowed_origins=body.allowed_origins, actor=_who(p))
            forget_merchants()
            return out
        return _merchant_call(run)

    @r.put("/merchants/{merchant_id}/webhook")
    def set_webhook(merchant_id: str, body: WebhookBody, p=merch):
        def run():
            with connect() as conn:
                row, secret = merchants.set_webhook(conn, merchant_id, body.url, actor=_who(p))
            forget_merchants()
            return {"merchant": row, "signing_secret": secret}
        return _merchant_call(run)

    @r.post("/merchants/{merchant_id}/keys", status_code=201)
    def create_key(merchant_id: str, body: KeyBody, p=merch):
        def run():
            with connect() as conn:
                key_id, raw = merchants.new_key(conn, merchant_id, body.kind, body.name, actor=_who(p))
            return {"id": key_id, "key": raw, "kind": body.kind}
        return _merchant_call(run)

    @r.delete("/merchants/{merchant_id}/keys/{key_id}")
    def revoke_key(merchant_id: str, key_id: int, p=merch):
        with connect() as conn:
            ok = auth.revoke_merchant_key(conn, merchant_id, key_id, actor=_who(p))
        forget_merchants()
        if not ok:
            raise HTTPException(404, "key not found or already revoked")
        return {"revoked": key_id}

    @r.get("/webhook-events", dependencies=[read])
    def webhook_events(merchant_id: str | None = Query(None, max_length=60), limit: int = Query(50, ge=1, le=200)):
        with connect() as conn:
            rows = conn.execute("SELECT event_id, merchant_id, type, status, attempts, last_code, created_at, delivered_at FROM webhook_event "
                                "WHERE (%s::text IS NULL OR merchant_id=%s) ORDER BY created_at DESC LIMIT %s",
                                (merchant_id, merchant_id, limit)).fetchall()
            conn.rollback()
        return {"items": rows}

    # ---- connectors ----------------------------------------------------------------------------------------------
    @r.get("/connectors", dependencies=[read])
    def connectors():
        with connect() as conn:
            rows = conn.execute("SELECT name, label, enabled, currencies, regions, fee_bps, fixed_fee_cents, base_latency_ms, base_auth_rate, "
                                "auth_ewma, latency_ewma, attempts, successes FROM psp_connector ORDER BY name").fetchall()
            conn.rollback()
        return {"items": rows}

    @r.patch("/connectors/{name}", dependencies=[merch])
    def patch_connector(name: str, body: ConnectorPatch):
        from . import routing
        with connect() as conn:
            row = conn.execute("UPDATE psp_connector SET enabled=%s WHERE name=%s RETURNING name", (body.enabled, name)).fetchone()
            conn.commit()
        if not row:
            raise HTTPException(404, "connector not found")
        routing.invalidate()
        return {"name": name, "enabled": body.enabled}

    # ---- LLM accounts (Tier 3 and rule suggestions) --------------------------------------------------------------
    @r.get("/accounts", dependencies=[settings])
    def list_accounts():
        with connect() as conn:
            out = {"items": accounts.list_accounts(conn), "env": accounts.env_account(), "presets": accounts.PRESETS,
                   "triage_account": accounts.triage_account(conn)}
            conn.rollback()
        return out

    def _account_call(fn):
        try:
            return fn()
        except (accounts.AccountError, UnsafeURL, vault.VaultError, LLMError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @r.post("/accounts", status_code=201, dependencies=[settings])
    def create_account(body: AccountBody):
        def run():
            with connect() as conn:
                return {"id": accounts.create(conn, name=body.name, provider=body.provider, base_url=body.base_url, api_key=body.api_key,
                                              chat_model=body.chat_model)}
        return _account_call(run)

    @r.put("/accounts/{account_id}", dependencies=[settings])
    def update_account(account_id: int, body: AccountBody):
        def run():
            with connect() as conn:
                accounts.update(conn, account_id, name=body.name, provider=body.provider, base_url=body.base_url, api_key=body.api_key,
                                clear_key=body.clear_key, chat_model=body.chat_model)
            return {"ok": True}
        return _account_call(run)

    @r.delete("/accounts/{account_id}", dependencies=[settings])
    def delete_account(account_id: int):
        def run():
            with connect() as conn:
                accounts.delete(conn, account_id)
            return {"deleted": account_id}
        return _account_call(run)

    @r.post("/accounts/{account}/test", dependencies=[settings])
    def test_account(account: str, body: TestBody):
        """One tiny real chat call so a wrong URL, key or model shows up now. The error text is the provider's own reason, redacted."""
        if account != accounts.ENV and not account.isdigit():
            raise HTTPException(422, "invalid account")
        started = time.monotonic()
        try:
            with connect() as conn:
                cfg = accounts.chat_config_for(conn, account if account == accounts.ENV else int(account))
                conn.rollback()
            reply = run_chat(cfg, [{"role": "user", "content": accounts.TEST_PROMPT}], max_tokens=accounts.TEST_MAX_TOKENS)
        except vault.VaultLocked as exc:
            raise HTTPException(423, str(exc)) from exc
        except LLMError as exc:
            raise HTTPException(502, str(exc)) from exc
        except (accounts.AccountError, UnsafeURL, vault.VaultError) as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"ok": True, "ms": int((time.monotonic() - started) * 1000), "reply": reply["text"][:60]}

    @r.put("/triage-account", dependencies=[settings])
    def set_triage(body: TriageAccountBody):
        def run():
            with connect() as conn:
                accounts.set_triage_account(conn, body.account_id)
            return {"triage_account": body.account_id}
        return _account_call(run)

    # ---- AI triage limits -------------------------------------------------------------------------------------------
    @r.get("/admin/triage/settings", dependencies=[settings])
    def get_triage_limits():
        with connect() as conn:
            out = triage_limits.settings(conn)
            conn.rollback()
        return out

    @r.put("/admin/triage/settings")
    def put_triage_limits(body: TriageLimitsBody, p=settings):
        try:
            with connect() as conn:
                return triage_limits.update_settings(conn, actor=p.actor, **body.model_dump())
        except triage_limits.LimitsError as exc:
            raise HTTPException(422, str(exc)) from exc

    # ---- demo traffic ----------------------------------------------------------------------------------------------
    @r.post("/demo/traffic", dependencies=[settings])
    def demo_traffic(body: DemoBody, request: Request):
        with connect() as conn:
            return demo.run(conn, body.count, body.seed, public_url(request))

    # ---- maintenance: wipe the transaction data, keep the configuration (admin only) --------------------------------
    @r.get("/admin/data/reset", dependencies=[wipe])
    def reset_preview():
        with connect() as conn:
            return maintenance.preview(conn)

    @r.post("/admin/data/reset")
    def reset_data(body: ResetBody, p=wipe):
        try:
            maintenance.check_phrase(body.confirm)
            out = maintenance.reset(actor=p.actor, include_training_data=body.include_training_data,
                                    include_rule_suggestions=body.include_rule_suggestions)
        except maintenance.ResetBusy as exc:
            raise HTTPException(409, str(exc)) from exc
        except maintenance.ResetError as exc:
            raise HTTPException(422, str(exc)) from exc
        forget_merchants()
        return out

    return r
