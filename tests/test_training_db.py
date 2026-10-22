"""Training-data capture, labels, retention, export, and the model lifecycle (upload, activate, roll back) against Postgres."""
import csv
import io
import json
import time
from datetime import timedelta

import pytest

from tests.conftest import needs_db

pytestmark = needs_db

from paybroker import demo, features, ml, modelstore, training_data  # noqa: E402
from paybroker.db import connect  # noqa: E402
from tests.test_db import CSRF, KEY, bearer, pay, tokenize  # noqa: E402

CAPTURE = {"capture": True}


@pytest.fixture
def op(monkeypatch):
    monkeypatch.setenv("PAYBROKER_API_KEY", KEY)
    return {"Authorization": f"Bearer {KEY}"}


@pytest.fixture
def shop(client, op):
    """A merchant with a secret and a publishable key: (merchant_id, secret headers, publishable headers)."""
    mid = client.post("/api/merchants", json={"name": "Training Shop"}, headers=op).json()["id"]
    sk = client.post(f"/api/merchants/{mid}/keys", json={"kind": "secret", "name": "server"}, headers=op).json()["key"]
    pk = client.post(f"/api/merchants/{mid}/keys", json={"kind": "publishable", "name": "browser"}, headers=op).json()["key"]
    return mid, bearer(sk), bearer(pk)


def pay_n(client, sk, tok, key, i, **extra):
    """A payment from its own customer, IP and device, so the velocity rules and the model see an ordinary shopper."""
    body = {"customer": f"cus_n{i}", "ip": f"198.51.100.{20 + i}", "telemetry": {"device_fingerprint": f"dev_n{i}", "checkout_duration_ms": 40_000}}
    body.update(extra)
    return pay(client, sk, tok, f"train-{key}-{i}", **body)
FIELDS = list(features.VECTOR_FIELDS)


def capture_on(client, op, **extra):
    r = client.put("/api/admin/training/settings", json={"capture": True, **extra}, headers=op)
    assert r.status_code == 200, r.text
    return r.json()


def rows():
    with connect() as conn:
        out = conn.execute("SELECT * FROM training_example ORDER BY id").fetchall()
        conn.rollback()
    return out


def leaf_model(value: float, version: str) -> dict:
    return {"version": version, "features": FIELDS, "base_margin": 0.0, "notes": "x",
            "trees": [{"f": [0], "t": [0.0], "l": [-1], "r": [-1], "v": [value], "e": [value]}]}


def upload(client, op, doc, **params):
    return client.post("/api/admin/models", content=json.dumps(doc), params=params,
                       headers={**op, "Content-Type": "application/json"})


# ---- capture ---------------------------------------------------------------------------------------------------
def test_capture_is_off_by_default_and_stores_nothing(client, shop, op):
    _, sk, pk = shop
    assert pay(client, sk, tokenize(client, pk), "cap-off-1").status_code == 200
    assert rows() == []
    s = client.get("/api/admin/training/settings", headers=op).json()
    assert s["capture"] is False and s["retention_days"] == 180 and s["max_rows"] == 5_000_000 and s["capture_source"] == "default"


def test_capture_stores_one_row_per_scored_payment_without_pii(client, shop, op):
    _, sk, pk = shop
    capture_on(client, op)
    tok = tokenize(client, pk)
    first = pay(client, sk, tok, "cap-on-1", customer="cus_secret_name", ip="198.51.100.77").json()
    pay(client, sk, tok, "cap-on-1", customer="cus_secret_name", ip="198.51.100.77")        # an idempotent replay scores nothing new
    pay(client, sk, tokenize(client, pk), "cap-on-2")
    got = rows()
    assert len(got) == 2
    row = got[0]
    assert set(row) == {"id", "created_at", "payment_id", "features", "feature_hash", "model_version", "score", "decision", "label",
                        "label_source", "labeled_at"}
    assert row["payment_id"] == first["payment_id"] and len(row["features"]) == 17 and row["feature_hash"] == features.FEATURE_HASH
    assert row["label"] is None and row["label_source"] is None and row["labeled_at"] is None and row["decision"] == "APPROVE"
    with connect() as conn:
        fa = conn.execute("SELECT feature_vector, tier2_ml_score, model_version FROM fraud_assessment ORDER BY created_at LIMIT 1").fetchone()
        dump = json.dumps(got, default=str)
        conn.rollback()
    assert row["score"] == fa["tier2_ml_score"] and row["model_version"] == fa["model_version"] == ml.load().version
    assert row["features"] == pytest.approx(list(fa["feature_vector"].to_list()), abs=1e-3)
    for secret in ("cus_secret_name", "198.51.100.77", "dev_1", "a@example.com", "4242424242424242", "4242", "example.com"):
        assert secret not in dump, secret


def test_capture_is_decided_by_setting_then_environment(client, shop, op, monkeypatch):
    _, sk, pk = shop
    monkeypatch.setenv("PAYBROKER_CAPTURE_TRAINING_DATA", "true")
    training_data.invalidate()
    pay(client, sk, tokenize(client, pk), "cap-env-1")
    assert len(rows()) == 1                                           # the environment alone switches it on
    assert client.get("/api/admin/training/settings", headers=op).json()["capture_source"] == "environment"
    client.put("/api/admin/training/settings", json={"capture": False}, headers=op)      # a stored setting wins over the environment
    pay(client, sk, tokenize(client, pk), "cap-env-2")
    assert len(rows()) == 1
    assert client.get("/api/admin/training/settings", headers=op).json()["capture_source"] == "setting"


def test_settings_are_validated_and_admin_only(client, op, shop):
    for bad in ({"retention_days": 0}, {"retention_days": 99999}, {"max_rows": 5}, {"capture": "maybe"}, {"unknown": 1}):
        assert client.put("/api/admin/training/settings", json=bad, headers=op).status_code == 422, bad
    ok = client.put("/api/admin/training/settings", json={"retention_days": 30, "max_rows": 10_000}, headers=op).json()
    assert ok["retention_days"] == 30 and ok["max_rows"] == 10_000 and ok["capture"] is False
    read = client.post("/api/tokens", json={"name": "ro", "scope": "read"}, headers=op).json()["token"]
    assert client.put("/api/admin/training/settings", json=CAPTURE, headers=bearer(read)).status_code == 403
    assert client.get("/api/admin/training/settings", headers=bearer(read)).status_code == 403
    assert client.put("/api/admin/training/settings", json=CAPTURE, headers=bearer(shop[1]["Authorization"][7:])).status_code == 403


# ---- labels ----------------------------------------------------------------------------------------------------
def _flagged(client, op):
    capture_on(client, op)
    with connect() as conn:
        demo.run(conn, 150, seed=3)
    return client.get("/api/cases", headers=op).json()["items"]


def _row(payment_id):
    return next(r for r in rows() if r["payment_id"] == payment_id)


def test_case_resolution_labels_confirmed_fraud_and_false_positives(client, op):
    cases = _flagged(client, op)
    assert len(cases) >= 3
    fraud_case, fp_case, kyc_case = cases[0], cases[1], cases[2]
    assert all(_row(c["payment_id"])["label"] is None for c in cases[:3])
    client.post(f"/api/cases/{fraud_case['id']}/resolve", json={"decision": "PERMANENT_BLOCK"}, headers=op)
    client.post(f"/api/cases/{fp_case['id']}/resolve", json={"decision": "DISMISS_ALERT"}, headers=op)
    client.post(f"/api/cases/{kyc_case['id']}/resolve", json={"decision": "ALLOW_WITH_KYC"}, headers=op)
    a, b, c = (_row(x["payment_id"]) for x in (fraud_case, fp_case, kyc_case))
    assert (a["label"], a["label_source"]) == (1, "case") and a["labeled_at"] is not None
    assert (b["label"], b["label_source"]) == (0, "case")
    assert c["label"] is None and c["label_source"] is None          # KYC asserts neither fraud nor innocence: no guess
    assert sum(1 for r in rows() if r["label"] is not None) == 2


def test_fraud_coded_chargeback_labels_and_beats_a_case_decision(client, shop, op):
    _, sk, pk = shop
    capture_on(client, op)
    ids = [pay_n(client, sk, tokenize(client, pk), f"cb-{i}", i).json()["payment_id"] for i in range(5)]
    cb = lambda i, **b: client.post(f"/api/payments/{ids[i]}/chargeback", json=b, headers=op)  # noqa: E731
    assert cb(0, reason="stolen", reason_code="10.4").status_code == 200
    assert cb(1, reason="item not received", reason_code="13.1").status_code == 200
    assert cb(2, reason="fraud").status_code == 200
    assert cb(3, reason="customer unhappy").status_code == 200
    assert cb(4, reason_code="4837").status_code == 200
    got = {r["payment_id"]: (r["label"], r["label_source"]) for r in rows()}
    assert got[ids[0]] == (1, "chargeback") and got[ids[2]] == (1, "chargeback") and got[ids[4]] == (1, "chargeback")
    assert got[ids[1]] == (None, None) and got[ids[3]] == (None, None)    # a dispute that is not fraud proves nothing either way
    with connect() as conn:
        assert conn.execute("SELECT reason_code FROM chargeback WHERE payment_id=%s", (ids[0],)).fetchone()["reason_code"] == "10.4"
        # a weaker source never replaces a stronger one, a stronger one replaces a weaker one
        assert training_data.set_label(conn, ids[0], 0, "case") is False and training_data.set_label(conn, ids[0], 0, "aging") is False
        assert training_data.set_label(conn, ids[1], 0, "aging") is True and training_data.set_label(conn, ids[1], 0, "case") is True
        assert training_data.set_label(conn, ids[1], 1, "chargeback") is True
        conn.commit()
    assert _row(ids[1])["label"] == 1 and _row(ids[0])["label"] == 1


def _age(payment_ids, days):
    with connect() as conn:
        conn.execute("UPDATE training_example SET created_at = now() - make_interval(days => %s) WHERE payment_id = ANY(%s)", (days, payment_ids))
        conn.commit()


def test_label_aging_is_explicit_and_only_labels_approved_undisputed_old_payments(client, shop, op):
    _, sk, pk = shop
    capture_on(client, op)
    ok_old = pay_n(client, sk, tokenize(client, pk), "age-1", 1, capture_method="automatic").json()["payment_id"]
    ok_new = pay_n(client, sk, tokenize(client, pk), "age-2", 2, capture_method="automatic").json()["payment_id"]
    disputed = pay_n(client, sk, tokenize(client, pk), "age-3", 3, capture_method="automatic").json()["payment_id"]
    uncaptured = pay_n(client, sk, tokenize(client, pk), "age-4", 4).json()["payment_id"]       # authorized only
    declined = pay_n(client, sk, tokenize(client, pk, "4000000000000002"), "age-5", 5, capture_method="automatic")
    declined_id = declined.json()["error"]["payment_id"]
    client.post(f"/api/payments/{disputed}/chargeback", json={"reason": "not fraud, a dispute"}, headers=op)
    _age([ok_old, disputed, uncaptured, declined_id], 100)
    time.sleep(0.01)
    assert all(r["label"] is None for r in rows())                    # nothing is labeled by waiting alone
    assert client.post("/api/admin/training/label-aging", json={"days": 0}, headers=op).status_code == 422
    read = client.post("/api/tokens", json={"name": "ro", "scope": "read"}, headers=op).json()["token"]
    assert client.post("/api/admin/training/label-aging", json={"days": 90}, headers=bearer(read)).status_code == 403
    out = client.post("/api/admin/training/label-aging", json={"days": 90}, headers=op)
    assert out.status_code == 200 and out.json()["labeled_legitimate"] == 1
    assert (_row(ok_old)["label"], _row(ok_old)["label_source"]) == (0, "aging")
    for pid in (ok_new, disputed, uncaptured, declined_id):
        assert _row(pid)["label"] is None, pid                         # too young, disputed, never captured, never approved
    assert client.post("/api/admin/training/label-aging", json={"days": 90}, headers=op).json()["labeled_legitimate"] == 0
    assert client.post("/api/admin/training/label-aging", json={"days": 90, "x": 1}, headers=op).status_code == 422


def test_aging_skips_payments_with_an_open_case(client, op):
    cases = _flagged(client, op)
    open_ids = [c["payment_id"] for c in cases]
    _age(open_ids, 400)
    with connect() as conn:
        conn.execute("UPDATE transaction SET status='CAPTURED' WHERE payment_id = ANY(%s)", (open_ids,))   # even if it were captured
        conn.commit()
    assert client.post("/api/admin/training/label-aging", json={"days": 30}, headers=op).json()["labeled_legitimate"] == 0


# ---- retention and the row cap -----------------------------------------------------------------------------------
def _insert(n, age_days=0):
    with connect() as conn:
        for i in range(n):
            conn.execute("INSERT INTO training_example (payment_id, features, feature_hash, model_version, score, decision, created_at) "
                         "VALUES (%s,%s,%s,'v',10,'APPROVE', now() - make_interval(days => %s, secs => %s))",
                         (f"pay_x_{age_days}_{i}_{time.time_ns()}", [0.5] * 17, features.FEATURE_HASH, age_days, n - i))
        conn.commit()


def test_retention_purges_old_rows_and_the_cap_purges_oldest_first(client, op):
    _insert(5, age_days=200)
    _insert(8, age_days=1)
    with connect() as conn:
        out = training_data.purge(conn, retention_days=180, max_rows=100)
        assert out["expired"] == 5 and out["over_cap"] == 0
        assert conn.execute("SELECT count(*) AS n FROM training_example").fetchone()["n"] == 8
        keep = [r["id"] for r in conn.execute("SELECT id FROM training_example ORDER BY created_at DESC LIMIT 3")]
        out = training_data.purge(conn, retention_days=180, max_rows=3)
        assert out["over_cap"] == 5
        assert [r["id"] for r in conn.execute("SELECT id FROM training_example ORDER BY created_at DESC")] == keep
        conn.rollback()


def test_purge_work_is_bounded_per_call_and_the_endpoint_finishes_the_job(client, op):
    _insert(30, age_days=500)
    with connect() as conn:
        out = training_data.purge(conn, retention_days=180, max_rows=100, batch=7, max_batches=2)
        assert out["expired"] == 14 and out["batches"] == 2
        assert conn.execute("SELECT count(*) AS n FROM training_example").fetchone()["n"] == 16
        conn.rollback()
    client.put("/api/admin/training/settings", json={"retention_days": 180}, headers=op)
    r = client.post("/api/admin/training/purge", headers=op)
    assert r.status_code == 200 and r.json()["expired"] == 16 and rows() == []
    read = client.post("/api/tokens", json={"name": "ro", "scope": "read"}, headers=op).json()["token"]
    assert client.post("/api/admin/training/purge", headers=bearer(read)).status_code == 403


def test_the_worker_loop_applies_retention_even_with_capture_off(client, op):
    import threading

    from paybroker import worker
    _insert(3, age_days=400)
    _insert(2, age_days=1)
    stop = threading.Event()
    t = threading.Thread(target=worker.loop, args=(stop, 0.05), daemon=True)
    t.start()
    try:
        deadline = time.time() + 10
        while time.time() < deadline and len(rows()) != 2:
            time.sleep(0.1)
    finally:
        stop.set()
        t.join(5)
    assert len(rows()) == 2 and client.get("/api/admin/training/settings", headers=op).json()["capture"] is False


# ---- statistics and export ----------------------------------------------------------------------------------------
def _seed_export(client, shop, op):
    _, sk, pk = shop
    capture_on(client, op)
    ids = [pay_n(client, sk, tokenize(client, pk), f"exp-{i}", i, capture_method="automatic").json()["payment_id"] for i in range(4)]
    client.post(f"/api/payments/{ids[0]}/chargeback", json={"reason_code": "10.4"}, headers=op)
    _age([ids[1]], 100)
    client.post("/api/admin/training/label-aging", json={"days": 90}, headers=op)
    return ids


def test_stats_count_rows_labels_fraud_rate_and_model_versions(client, shop, op):
    ids = _seed_export(client, shop, op)
    s = client.get("/api/admin/training/stats", headers=op).json()
    assert s["rows"] == 4 and s["labeled"] == 2 and s["fraud"] == 1 and s["fraud_rate"] == 0.5
    assert s["label_sources"] == {"chargeback": 1, "aging": 1}
    pm = s["per_model_version"]
    assert len(pm) == 1 and pm[0]["model_version"] == ml.load().version and pm[0]["rows"] == 4 and pm[0]["labeled"] == 2
    assert s["feature_hash"] == features.FEATURE_HASH and s["features"] == FIELDS and s["settings"]["capture"] is True
    assert ids


def test_export_csv_has_a_commented_header_the_features_and_the_labels(client, shop, op):
    ids = _seed_export(client, shop, op)
    r = client.get("/api/admin/training/export", headers=op)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert r.headers["x-paybroker-feature-hash"] == features.FEATURE_HASH and "attachment" in r.headers["content-disposition"]
    lines = r.text.splitlines()
    comments = [ln for ln in lines if ln.startswith("#")]
    assert any("features: " + ", ".join(FIELDS) in ln for ln in comments) and any(features.FEATURE_HASH in ln for ln in comments)
    assert any(ml.load().version in ln for ln in comments)
    table = list(csv.DictReader(io.StringIO("\n".join(ln for ln in lines if not ln.startswith("#")))))
    assert len(table) == 4 and all(f in table[0] for f in FIELDS) and table[0]["payment_id"] == ids[0]
    assert {t["label"] for t in table} == {"1", "0", ""}
    assert table[0]["label"] == "1" and table[0]["label_source"] == "chargeback"
    assert list(table[0]) == [*training_data.SAMPLE_COLUMNS, *FIELDS]            # exactly these columns: no identity of any kind


def test_export_filters_labeled_since_limit_and_ndjson(client, shop, op):
    _seed_export(client, shop, op)
    labeled = client.get("/api/admin/training/export?labeled=true", headers=op).text
    assert len([r for r in csv.DictReader(io.StringIO("\n".join(ln for ln in labeled.splitlines() if not ln.startswith("#"))))]) == 2
    nd = client.get("/api/admin/training/export?format=ndjson&labeled=true", headers=op)
    assert nd.headers["content-type"].startswith("application/x-ndjson")
    docs = [json.loads(ln) for ln in nd.text.splitlines()]
    assert docs[0]["_meta"]["features"] == FIELDS and docs[0]["_meta"]["feature_hash"] == features.FEATURE_HASH
    assert docs[0]["_meta"]["model_versions"] == [ml.load().version] and docs[0]["_meta"]["labeled_only"] is True
    assert len(docs) == 3 and set(docs[1]["features"]) == set(FIELDS) and docs[1]["label"] in (0, 1)
    assert "payment_id" in docs[1] and "customer" not in json.dumps(docs[1])
    tomorrow = (time.strftime("%Y-%m-%d", time.gmtime(time.time() + 86400)))
    assert len(client.get(f"/api/admin/training/export?format=ndjson&since={tomorrow}", headers=op).text.splitlines()) == 1   # meta only
    assert len(client.get("/api/admin/training/export?format=ndjson&since=2020-01-01", headers=op).text.splitlines()) == 5
    assert len(client.get("/api/admin/training/export?format=ndjson&limit=2", headers=op).text.splitlines()) == 3
    assert client.get("/api/admin/training/export?format=xml", headers=op).status_code == 422
    assert client.get("/api/admin/training/export?since=yesterday-ish", headers=op).status_code == 422


def test_export_skips_rows_built_with_another_feature_layout(client, shop, op):
    _seed_export(client, shop, op)
    with connect() as conn:
        conn.execute("UPDATE training_example SET feature_hash='oldlayout000' WHERE id = (SELECT min(id) FROM training_example)")
        conn.commit()
    assert len(client.get("/api/admin/training/export?format=ndjson", headers=op).text.splitlines()) == 4


def test_export_and_stats_need_the_admin_permission(client, shop, op):
    _seed_export(client, shop, op)
    _, sk, pk = shop
    read = client.post("/api/tokens", json={"name": "ro", "scope": "read"}, headers=op).json()["token"]
    full = client.post("/api/tokens", json={"name": "full", "scope": "full"}, headers=op).json()["token"]
    for path in ("/api/admin/training/export", "/api/admin/training/stats", "/api/admin/models"):
        assert client.get(path).status_code == 401, path
        assert client.get(path, headers=bearer(read)).status_code == 403, path
        assert client.get(path, headers=sk).status_code == 403, path                       # a merchant key
        assert client.get(path, headers=pk).status_code == 403, path
        assert client.get(path, headers=bearer(full)).status_code == 200, path


def test_export_streams_in_bounded_memory_chunks(client, op):
    _insert(250)
    with connect() as conn:
        parts = list(training_data.export_rows(conn, "csv", False, None, "tester", itersize=10))
    assert len(parts) >= 3 and b"".join(parts).count(b"\n") >= 250


def test_an_export_trains_a_model(client, shop, op, tmp_path):
    np = pytest.importorskip("numpy")
    from paybroker import trainer
    _seed_export(client, shop, op)
    for fmt in ("csv", "ndjson"):
        path = tmp_path / f"e.{fmt}"
        path.write_text(client.get(f"/api/admin/training/export?format={fmt}", headers=op).text)
        x, y, ts, prov = trainer.load_export(path)
        assert x.shape == (2, 17) and sorted(y.tolist()) == [0, 1] and prov["skipped"]["unlabeled"] == 2 and not np.isnan(ts).any()
        assert trainer.load_export(path, ("aging",))[1].tolist() == [0]


# ---- model lifecycle ---------------------------------------------------------------------------------------------
def test_upload_validates_stores_and_does_not_activate_by_itself(client, op):
    r = upload(client, op, leaf_model(20.0, "always-decline"), notes="all declines")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["version"] == "always-decline" and body["active"] is False and body["tree_count"] == 1 and body["notes"] == "all declines"
    assert body["uploaded_by"] and body["size_bytes"] > 10 and len(body["sha256"]) == 64
    listing = client.get("/api/admin/models", headers=op).json()
    assert listing["active"]["source"] == "bundled" and listing["bundled"]["trees"] == 70 and len(listing["items"]) == 1
    assert upload(client, op, leaf_model(20.0, "always-decline")).status_code == 409           # versions are never overwritten
    assert upload(client, op, leaf_model(1.0, ml.load().version)).status_code == 409           # nor may they shadow the bundled one
    again = client.post("/api/admin/models", json={"model": leaf_model(3.0, "x"), "version": "renamed", "notes": "n"}, headers=op)
    assert again.status_code == 201 and again.json()["version"] == "renamed"
    assert client.get(f"/api/admin/models/{body['id']}", headers=op).json()["trees"][0]["v"] == [20.0]


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(features=FIELDS[::-1]), lambda d: d.update(features=FIELDS[:-1]), lambda d: d.update(trees=[]),
    lambda d: d["trees"][0].update(f=[99], l=[1], r=[2], v=[0], e=[0], t=[0]), lambda d: d.update(base_margin="x"),
    lambda d: d.update(trees=d["trees"] * (ml.MAX_TREES + 1)),
])
def test_upload_refuses_malformed_models(client, op, mutate):
    doc = leaf_model(1.0, "bad")
    mutate(doc)
    assert upload(client, op, doc).status_code == 422
    assert client.get("/api/admin/models", headers=op).json()["items"] == []


def test_upload_refuses_malicious_oversized_and_non_json_bodies(client, op):
    hdr = {**op, "Content-Type": "application/json"}
    for raw in (b"not json", b'{"base_margin": NaN, "trees": [], "features": []}', b"[]", b"{" * 5000, b"\xff\xfe\x00"):
        assert client.post("/api/admin/models", content=raw, headers=hdr).status_code in (422, 400), raw[:20]
    big = leaf_model(1.0, "big")
    big["padding"] = "x" * (ml.MAX_MODEL_BYTES + 50_000)                    # over the request cap: refused before it is read
    assert client.post("/api/admin/models", content=json.dumps(big), headers=hdr).status_code == 413
    mid = leaf_model(1.0, "mid")
    mid["padding"] = "x" * (ml.MAX_MODEL_BYTES + 100)                       # inside the request cap, over what a model may weigh
    assert client.post("/api/admin/models", content=json.dumps(mid), headers=hdr).status_code == 413
    fine = leaf_model(1.0, "fine-size")
    fine["padding"] = "x" * (ml.MAX_MODEL_BYTES - 1000)                     # a large but legal upload is accepted
    assert client.post("/api/admin/models", content=json.dumps(fine), headers=hdr).status_code == 201
    assert client.post("/api/admin/models", content=b"x" * 70_000, headers=hdr).status_code == 422       # the upload cap is not the 64 KB one
    assert client.post("/api/tokens", content=b"x" * 70_000, headers=hdr).status_code == 413              # which stays for everything else
    assert upload(client, op, leaf_model(1.0, "../../etc/passwd")).status_code == 422
    assert upload(client, op, leaf_model(1.0, "ok-version"), version="has space").status_code == 422
    assert client.post("/api/admin/models", content=json.dumps(leaf_model(1.0, "csrf")), headers={"Content-Type": "application/json", **CSRF}).status_code == 401


def test_model_endpoints_need_model_manage_and_csrf_for_browsers(client, shop, op):
    read = client.post("/api/tokens", json={"name": "ro", "scope": "read"}, headers=op).json()["token"]
    assert upload(client, bearer(read), leaf_model(1.0, "nope")).status_code == 403
    assert upload(client, shop[1], leaf_model(1.0, "nope2")).status_code == 403
    mid = upload(client, op, leaf_model(1.0, "keepme")).json()["id"]
    for method, path in (("post", f"/api/admin/models/{mid}/activate"), ("post", "/api/admin/models/use-bundled"),
                         ("delete", f"/api/admin/models/{mid}"), ("get", f"/api/admin/models/{mid}")):
        assert getattr(client, method)(path, headers=bearer(read)).status_code == 403, path
    assert client.post(f"/api/admin/models/{mid}/activate").status_code == 403                   # a browser write needs the CSRF header
    assert client.post(f"/api/admin/models/{mid}/activate", headers=CSRF).status_code == 401     # and then a session


def test_activation_changes_what_scores_and_the_version_is_recorded(client, shop, op):
    _, sk, pk = shop
    capture_on(client, op)
    declining = upload(client, op, leaf_model(20.0, "declines-all")).json()["id"]
    approving = upload(client, op, leaf_model(-20.0, "approves-all")).json()["id"]
    r1 = pay_n(client, sk, tokenize(client, pk), "act-1", 1)
    assert r1.status_code == 200
    assert client.post(f"/api/admin/models/{declining}/activate", headers=op).json()["active"] is True
    r2 = pay_n(client, sk, tokenize(client, pk), "act-2", 2)
    assert r2.status_code == 403 and r2.json()["error"]["fraud_assessment"]["score"] == 1000
    assert client.get("/api/admin/models", headers=op).json()["active"] == {"source": "uploaded", "id": declining, "version": "declines-all"}
    assert client.post(f"/api/admin/models/{approving}/activate", headers=op).status_code == 200     # a new model replaces the active one
    items = {m["version"]: m["active"] for m in client.get("/api/admin/models", headers=op).json()["items"]}
    assert items == {"declines-all": False, "approves-all": True}
    r3 = pay_n(client, sk, tokenize(client, pk), "act-3", 3)
    assert r3.status_code == 200 and r3.json()["fraud_assessment"]["score"] == 0
    # roll back to the earlier version, then to the bundled file
    client.post(f"/api/admin/models/{declining}/activate", headers=op)
    assert pay_n(client, sk, tokenize(client, pk), "act-4", 4).status_code == 403
    assert client.post("/api/admin/models/use-bundled", headers=op).json()["active"]["source"] == "bundled"
    r5 = pay_n(client, sk, tokenize(client, pk), "act-5", 5)
    assert r5.status_code == 200 and 0 < r5.json()["fraud_assessment"]["score"] < 300
    by_payment = {r["payment_id"]: r["model_version"] for r in rows()}
    versions = [by_payment[p] for p in sorted(by_payment, key=lambda p: [r["id"] for r in rows() if r["payment_id"] == p])]
    assert versions == [ml.load().version, "declines-all", "approves-all", "declines-all", ml.load().version]
    with connect() as conn:
        stored = [r["model_version"] for r in conn.execute("SELECT model_version FROM fraud_assessment ORDER BY created_at")]
        conn.rollback()
    assert stored == versions
    detail = client.get(f"/api/payments/{r1.json()['payment_id']}", headers=op).json()
    assert detail["fraud"]["model_version"] == ml.load().version
    assert "model_version" not in json.dumps(r1.json())                       # merchant-facing responses are unchanged


def test_other_instances_follow_within_the_refresh_interval(client, shop, op, monkeypatch):
    """Another process activates a model by writing the table; this one notices when its 30 s cache expires."""
    _, sk, pk = shop
    assert upload(client, op, leaf_model(20.0, "elsewhere")).status_code == 201
    pay_n(client, sk, tokenize(client, pk), "ref-1", 1)           # warms the cache with 'no uploaded model'
    with connect() as conn:
        conn.execute("UPDATE model_version SET active=true WHERE version='elsewhere'")
        conn.commit()
    assert pay_n(client, sk, tokenize(client, pk), "ref-2", 2).status_code == 200      # still cached
    monkeypatch.setitem(ml._active, "checked", time.monotonic() - ml.REFRESH_SECONDS - 1)
    assert pay_n(client, sk, tokenize(client, pk), "ref-3", 3).status_code == 403
    with connect() as conn:                                                      # and it follows a rollback the same way
        conn.execute("UPDATE model_version SET active=false")
        conn.commit()
    monkeypatch.setitem(ml._active, "checked", time.monotonic() - ml.REFRESH_SECONDS - 1)
    assert pay_n(client, sk, tokenize(client, pk), "ref-4", 4).status_code == 200


def test_a_broken_active_row_or_database_hiccup_keeps_the_current_model(client, shop, op, monkeypatch):
    _, sk, pk = shop
    mid = upload(client, op, leaf_model(20.0, "fine")).json()["id"]
    client.post(f"/api/admin/models/{mid}/activate", headers=op)
    assert pay_n(client, sk, tokenize(client, pk), "brk-1", 1).status_code == 403
    with connect() as conn:                       # written outside the API with a model the validator would refuse
        conn.execute("INSERT INTO model_version (version, uploaded_by, size_bytes, tree_count, sha256, doc, active) "
                     "SELECT 'evil', 'x', 1, 1, 'z', '{\"features\": [], \"trees\": []}'::jsonb, false")
        conn.execute("UPDATE model_version SET active=false")
        conn.execute("UPDATE model_version SET active=true WHERE version='evil'")
        conn.commit()
    monkeypatch.setitem(ml._active, "checked", 0.0)
    assert pay_n(client, sk, tokenize(client, pk), "brk-2", 2).status_code == 403      # kept the last good model


def test_delete_refuses_the_active_model_and_missing_ids(client, op):
    a = upload(client, op, leaf_model(1.0, "a")).json()["id"]
    b = upload(client, op, leaf_model(2.0, "b")).json()["id"]
    client.post(f"/api/admin/models/{a}/activate", headers=op)
    assert client.delete(f"/api/admin/models/{a}", headers=op).status_code == 409
    assert client.delete(f"/api/admin/models/{b}", headers=op).json() == {"deleted": b}
    assert client.delete(f"/api/admin/models/{b}", headers=op).status_code == 404
    assert client.post("/api/admin/models/99999/activate", headers=op).status_code == 404
    assert client.get("/api/admin/models/99999", headers=op).status_code == 404


def test_only_one_model_can_be_active_at_the_database_level(client, op):
    a = upload(client, op, leaf_model(1.0, "a")).json()["id"]
    b = upload(client, op, leaf_model(2.0, "b")).json()["id"]
    import psycopg
    with connect() as conn:
        conn.execute("UPDATE model_version SET active=true WHERE id=%s", (a,))
        conn.commit()
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute("UPDATE model_version SET active=true WHERE id=%s", (b,))
        conn.rollback()


def test_uploading_the_bundled_file_works_and_reproduces_its_reference_scores(client, op):
    doc = json.loads(ml.MODEL_PATH.read_text())
    doc["version"] = "bundled-copy"
    r = upload(client, op, doc, activate="true")
    assert r.status_code == 201 and r.json()["active"] is True and r.json()["tree_count"] == 70
    tampered = json.loads(ml.MODEL_PATH.read_text())
    tampered["version"] = "tampered"
    tampered["base_margin"] += 3.0                                              # the file's own reference scores no longer hold
    assert upload(client, op, tampered).status_code == 422


# ---- upgrade safety ----------------------------------------------------------------------------------------------
def test_migration_002_applies_on_top_of_a_database_that_holds_payments():
    """001 plus data, then 002: nothing is lost, the new column is empty for old rows, and re-running 002 is harmless."""
    from pathlib import Path

    from paybroker import migrate
    sql = {p.name: p.read_text() for p in sorted(Path(migrate.MIGRATIONS).glob("*.sql"))}
    with connect() as conn:
        conn.execute("DROP SCHEMA IF EXISTS mig_upgrade CASCADE")
        conn.execute("CREATE SCHEMA mig_upgrade")
        conn.commit()
        conn.execute("SET search_path = mig_upgrade, public")
        conn.execute(sql["001_init.sql"])
        conn.execute("INSERT INTO merchant (id, name) VALUES ('mch_upgrade', 'Old Shop')")
        tx = conn.execute("INSERT INTO transaction (payment_id, merchant_id, idempotency_key, request_hash, amount, currency, status) "
                          "VALUES ('pay_old','mch_upgrade','k','h',100,'USD','CAPTURED') RETURNING id").fetchone()["id"]
        conn.execute("INSERT INTO fraud_assessment (transaction_id, tier2_ml_score, tier2_feature_snapshot, decision) VALUES (%s, 12, '{}', 'APPROVE')", (tx,))
        conn.execute("INSERT INTO chargeback (payment_id, amount, reason) VALUES ('pay_old', 100, 'old')")
        conn.execute(sql["002_training.sql"])
        conn.execute(sql["002_training.sql"])
        old = conn.execute("SELECT model_version FROM fraud_assessment").fetchone()
        cb = conn.execute("SELECT reason, reason_code FROM chargeback").fetchone()
        assert old["model_version"] is None and cb["reason"] == "old" and cb["reason_code"] is None
        assert conn.execute("SELECT count(*) AS n FROM training_example").fetchone()["n"] == 0
        conn.commit()
        conn.execute("SET search_path = public")
        conn.execute("DROP SCHEMA mig_upgrade CASCADE")
        conn.commit()


def test_retention_timedelta_constant_is_sane():
    assert timedelta(days=training_data.DEFAULT_RETENTION_DAYS).days == 180 and training_data.DEFAULT_MAX_ROWS == 5_000_000
    assert modelstore.VERSION_RE.match("synthetic-1000k-2026-10-22")
