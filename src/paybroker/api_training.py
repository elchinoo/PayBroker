"""Operator API for the model lifecycle and its training data (admin only: `training:export` and `model:manage`).

Training data: settings, statistics, a streaming export, label-aging and purge. Models: upload, list, download, activate,
roll back, delete. Every route names its permission; both belong to the admin role alone (see core.ROLE_PERMISSIONS)."""
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from . import ml, modelstore, training_data
from .db import connect


class _B(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SettingsBody(_B):
    capture: bool | None = None
    retention_days: int | None = Field(None, ge=1, le=training_data.MAX_RETENTION_DAYS)
    max_rows: int | None = Field(None, ge=1000, le=training_data.MAX_ROWS_LIMIT)


class AgingBody(_B):
    days: int = Field(ge=1, le=3650)


def build_router(require) -> APIRouter:
    r = APIRouter(prefix="/api/admin")
    export_or_manage = Depends(require("training:export", "model:manage"))
    export_perm, manage = Depends(require("training:export")), Depends(require("model:manage"))

    def _guard(fn):
        try:
            return fn()
        except training_data.TrainingError as exc:
            raise HTTPException(422, str(exc)) from exc
        except modelstore.ModelStoreError as exc:
            raise HTTPException(exc.status, exc.message) from exc

    # ---- training data ----------------------------------------------------------------------------------------
    @r.get("/training/settings", dependencies=[export_or_manage])
    def get_settings():
        with connect() as conn:
            out = training_data.settings(conn, fresh=True)
            conn.rollback()
        return out

    @r.put("/training/settings", dependencies=[manage])
    def put_settings(body: SettingsBody):
        def run():
            with connect() as conn:
                return training_data.update_settings(conn, capture=body.capture, retention_days=body.retention_days,
                                                     max_rows=body.max_rows)
        return _guard(run)

    @r.get("/training/stats", dependencies=[export_or_manage])
    def get_stats():
        with connect() as conn:
            return training_data.stats(conn) | {"settings": training_data.settings(conn, fresh=True)}

    @r.get("/training/export")
    def export(format: str = Query("csv", pattern="^(csv|ndjson)$"), labeled: bool = Query(False), since: str | None = Query(None, max_length=40),
               limit: int | None = Query(None, ge=1, le=50_000_000), p=export_perm):
        since_dt = _guard(lambda: training_data.parse_since(since))

        def stream():
            with connect() as conn:
                yield from training_data.export_rows(conn, format, labeled, since_dt, p.actor, limit)
        media = "text/csv; charset=utf-8" if format == "csv" else "application/x-ndjson"
        name = f"paybroker-training-{date.today().isoformat()}.{format}"
        return StreamingResponse(stream(), media_type=media, headers={
            "Content-Disposition": f'attachment; filename="{name}"', "X-PayBroker-Feature-Hash": training_data.features.FEATURE_HASH})

    @r.post("/training/label-aging")
    def label_aging(body: AgingBody, p=manage):
        def run():
            with connect() as conn:
                return training_data.label_aging(conn, body.days)
        out = _guard(run)
        training_data.log.info("label-aging by %s: %s", p.actor, out)
        return out

    @r.post("/training/purge", dependencies=[manage])
    def purge():
        with connect() as conn:
            return training_data.purge(conn, max_batches=200)

    # ---- models -----------------------------------------------------------------------------------------------
    @r.get("/models", dependencies=[manage])
    def list_models():
        with connect() as conn:
            return modelstore.overview(conn)

    @r.post("/models", status_code=201)
    async def upload_model(request: Request, notes: str = Query("", max_length=1000), version: str | None = Query(None, max_length=64),
                           activate: bool = Query(False), p=manage):
        raw = b""
        async for chunk in request.stream():
            raw += chunk
            if len(raw) > ml.MAX_MODEL_BYTES + 16 * 1024:
                return JSONResponse({"detail": "request body too large"}, status_code=413)

        def run():
            doc, n, v, act = modelstore.parse_upload(raw, notes, version, activate)
            with connect() as conn:
                row = modelstore.add(conn, doc, notes=n, version=v, uploaded_by=p.actor)
                if act:
                    row = modelstore.activate(conn, row["id"])
                return row
        return await run_in_threadpool(lambda: _guard(run))

    @r.get("/models/{model_id}", dependencies=[manage])
    def download_model(model_id: int):
        with connect() as conn:
            return _guard(lambda: modelstore.get_doc(conn, model_id))

    @r.post("/models/use-bundled", dependencies=[manage])
    def use_bundled():
        with connect() as conn:
            return modelstore.use_bundled(conn)

    @r.post("/models/{model_id}/activate", dependencies=[manage])
    def activate_model(model_id: int):
        with connect() as conn:
            return _guard(lambda: modelstore.activate(conn, model_id))

    @r.delete("/models/{model_id}", dependencies=[manage])
    def delete_model(model_id: int):
        with connect() as conn:
            _guard(lambda: modelstore.delete(conn, model_id))
        return {"deleted": model_id}

    return r
