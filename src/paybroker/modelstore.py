"""Uploaded fraud models: validated, stored in the database with their version, notes and uploader, one marked active.

The bundled file (`models/fraud_gbdt.json`) is always the fallback: with no uploaded model active it scores. Activating an
uploaded model, or going back to an earlier one (rollback), takes effect in this process at once and in the other instances
within `ml.REFRESH_SECONDS`. Models are never edited, only added, activated and (when inactive) deleted.
"""
import hashlib
import json
import math
import re

from psycopg.types.json import Jsonb

from . import features, ml

VERSION_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
ENVELOPE_SLACK = 16 * 1024


class ModelStoreError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def parse_upload(raw: bytes, query_notes: str = "", query_version: str | None = None, query_activate: bool = False):
    """The request body is either the model file itself or an envelope {"model": {...}, "notes", "version", "activate"}.
    Returns (model document, notes, version or None, activate)."""
    if len(raw) > ml.MAX_MODEL_BYTES + ENVELOPE_SLACK:
        raise ModelStoreError(413, "request body too large")

    def refuse(token):
        raise ModelStoreError(422, f"{token} is not allowed in a model")
    try:
        body = json.loads(raw, parse_constant=refuse)
    except ModelStoreError:
        raise
    except (ValueError, RecursionError) as exc:
        raise ModelStoreError(422, "the upload is not valid JSON") from exc
    if not isinstance(body, dict):
        raise ModelStoreError(422, "the upload must be a JSON object")
    if "trees" in body or "features" in body:
        doc, notes, version, activate = body, query_notes, query_version, query_activate
    else:
        extra = set(body) - {"model", "notes", "version", "activate"}
        if extra or "model" not in body:
            raise ModelStoreError(422, "send the model file itself or {model, notes?, version?, activate?}")
        doc, notes, version, activate = body["model"], body.get("notes") or "", body.get("version"), bool(body.get("activate"))
    if not isinstance(notes, str) or len(notes) > 1000:
        raise ModelStoreError(422, "notes must be text of at most 1000 characters")
    if version is not None and (not isinstance(version, str) or not VERSION_RE.match(version)):
        raise ModelStoreError(422, "version must be 1 to 64 letters, digits, dots, dashes or underscores")
    declared = doc.get("version") if isinstance(doc, dict) else None
    if declared is not None and (not isinstance(declared, str) or not VERSION_RE.match(declared)):
        raise ModelStoreError(422, "the model's own version must be 1 to 64 letters, digits, dots, dashes or underscores")
    try:
        ml.validate(doc)
        if _fingerprint(doc)[1] > ml.MAX_MODEL_BYTES:
            raise ModelStoreError(413, f"the model is larger than {ml.MAX_MODEL_BYTES // 1024} KB")
        _check_reference(doc)
    except ml.ModelError as exc:
        raise ModelStoreError(422, str(exc)) from exc
    return doc, notes.strip(), version, activate


def _check_reference(doc: dict) -> None:
    """If the file carries reference vectors (as scripts/train_model.py writes), the trees must reproduce them: it catches a
    corrupted or hand-edited upload before it scores anything."""
    ref = doc.get("reference")
    if ref is None:
        return
    if not isinstance(ref, list) or len(ref) > 100:
        raise ml.ModelError("reference must be a list of at most 100 {x, score} items")
    model = ml.Model(doc)
    for item in ref:
        x, want = (item.get("x"), item.get("score")) if isinstance(item, dict) else (None, None)
        if (not isinstance(x, list) or len(x) != len(features.VECTOR_FIELDS) or isinstance(want, bool) or not isinstance(want, int)
                or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in x)):
            raise ml.ModelError("each reference item needs a 17-number x and an integer score")
        if model.score([float(v) for v in x], explain=False)[0] != want:
            raise ml.ModelError("the trees do not reproduce the file's own reference scores; the model looks corrupted or edited")


def _fingerprint(doc: dict) -> tuple[str, int]:
    blob = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest(), len(blob)


_LIST = ("id, version, notes, uploaded_by, uploaded_at, activated_at, size_bytes, tree_count, sha256, active, "
         "doc->'trained_on' AS trained_on, doc->'metrics' AS metrics")


def add(conn, doc: dict, *, notes: str, version: str | None, uploaded_by: str) -> dict:
    sha, size = _fingerprint(doc)
    declared = str(doc.get("version", ""))
    version = version or (declared if VERSION_RE.match(declared) else f"uploaded-{sha[:8]}")
    if version == ml.load().version:
        raise ModelStoreError(409, "that version name belongs to the bundled model; choose another")
    row = conn.execute(
        "INSERT INTO model_version (version, notes, uploaded_by, size_bytes, tree_count, sha256, doc) VALUES (%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (version) DO NOTHING RETURNING id",
        (version, notes, uploaded_by[:60], size, len(doc["trees"]), sha, Jsonb(doc))).fetchone()
    if not row:
        conn.rollback()
        raise ModelStoreError(409, f"a model with version {version!r} already exists; versions are never overwritten")
    conn.commit()
    return get_meta(conn, row["id"])


def get_meta(conn, model_id: int) -> dict:
    row = conn.execute(f"SELECT {_LIST} FROM model_version WHERE id=%s", (model_id,)).fetchone()
    conn.rollback()
    if not row:
        raise ModelStoreError(404, "no such model")
    return row


def get_doc(conn, model_id: int) -> dict:
    row = conn.execute("SELECT doc FROM model_version WHERE id=%s", (model_id,)).fetchone()
    conn.rollback()
    if not row:
        raise ModelStoreError(404, "no such model")
    return row["doc"]


def overview(conn) -> dict:
    rows = conn.execute(f"SELECT {_LIST} FROM model_version ORDER BY id DESC").fetchall()
    conn.rollback()
    bundled = ml.load()
    active = next((r for r in rows if r["active"]), None)
    return {"active": {"source": "uploaded", "id": active["id"], "version": active["version"]} if active
            else {"source": "bundled", "id": None, "version": bundled.version},
            "bundled": {"version": bundled.version, "trees": len(bundled.trees), "trained_on": bundled.trained_on},
            "items": rows, "limits": {"max_bytes": ml.MAX_MODEL_BYTES, "max_trees": ml.MAX_TREES, "max_depth": ml.MAX_DEPTH},
            "refresh_seconds": ml.REFRESH_SECONDS}


def activate(conn, model_id: int) -> dict:
    row = conn.execute("SELECT id FROM model_version WHERE id=%s FOR UPDATE", (model_id,)).fetchone()
    if not row:
        conn.rollback()
        raise ModelStoreError(404, "no such model")
    conn.execute("UPDATE model_version SET active=false WHERE active AND id<>%s", (model_id,))
    conn.execute("UPDATE model_version SET active=true, activated_at=now() WHERE id=%s AND NOT active", (model_id,))
    conn.commit()
    ml.invalidate()
    return get_meta(conn, model_id)


def use_bundled(conn) -> dict:
    conn.execute("UPDATE model_version SET active=false WHERE active")
    conn.commit()
    ml.invalidate()
    return {"active": {"source": "bundled", "version": ml.load().version}}


def delete(conn, model_id: int) -> None:
    row = conn.execute("SELECT active FROM model_version WHERE id=%s FOR UPDATE", (model_id,)).fetchone()
    if not row:
        conn.rollback()
        raise ModelStoreError(404, "no such model")
    if row["active"]:
        conn.rollback()
        raise ModelStoreError(409, "this model is active; activate another (or the bundled one) first")
    conn.execute("DELETE FROM model_version WHERE id=%s", (model_id,))
    conn.commit()
