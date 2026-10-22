"""Dataset adapters, the synthetic generator, evaluation and the training script. Needs numpy and xgboost (the `train` extra).

The adapters are tested ONLY on invented rows: a few hand-written ones whose features are worked out by hand below, and tiny
generated files in tests/fixtures (see make_fixtures.py). No real dataset is used or downloaded."""
import gzip
import json
import math
import subprocess
import sys
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("xgboost")

from paybroker import datasets, features, ml, scenarios, synth, trainer  # noqa: E402

FIX = Path(__file__).parent / "fixtures"
ROOT = Path(__file__).resolve().parents[1]
IDX = {n: i for i, n in enumerate(features.VECTOR_FIELDS)}

TABFORMER_HEADER = "User,Card,Year,Month,Day,Time,Amount,Use Chip,Merchant Name,Merchant City,Merchant State,Zip,MCC,Errors?,Is Fraud?\n"
TABFORMER_ROWS = """0,0,2020,1,1,08:00,$10.00,Swipe Transaction,111,Springfield,IL,62701,5411,,No
0,0,2020,1,1,08:20,$20.00,Chip Transaction,112,Springfield,IL,62701,5411,,No
0,1,2020,1,1,08:40,$30.00,Online Transaction,113,ONLINE,,,4121,,No
0,0,2020,1,1,09:10,$40.00,Swipe Transaction,114,Dallas,TX,75001,5411,,Yes
0,0,2020,1,1,09:15,-$5.00,Swipe Transaction,115,Dallas,TX,75001,5411,,No
0,0,2020,1,3,08:00,"$100.00",Swipe Transaction,116,Dallas,TX,75001,5411,,No
1,0,2020,1,1,12:00,"$1,234.50",Swipe Transaction,117,Boston,MA,02101,5999,,No
"""


def write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text)
    return p


def run_adapter(name, path, **kw):
    a = datasets.Adapter(name, path, **kw)
    chunks = list(a.chunks())
    return a, np.vstack([c.x for c in chunks]), np.concatenate([c.y for c in chunks]), np.concatenate([c.ts for c in chunks])


def col(x, name):
    return x[:, IDX[name]]


# ---- TabFormer ---------------------------------------------------------------------------------------------------
def test_tabformer_features_worked_out_by_hand(tmp_path):
    a, x, y, ts = run_adapter("tabformer", write(tmp_path, "t.csv", TABFORMER_HEADER + TABFORMER_ROWS))
    assert len(y) == 6 and a.skipped == {"refund_or_zero": 1} and y.tolist() == [0, 0, 0, 1, 0, 0]
    assert col(x, "amount_log").tolist() == pytest.approx([math.log1p(v) for v in (10, 20, 30, 40, 100, 1234.5)], rel=1e-6)
    assert col(x, "user_tx_1h").tolist() == [0, 1, 2, 2, 0, 0]               # prior payments of the user within the last hour
    assert col(x, "user_tx_24h").tolist() == [0, 1, 2, 3, 0, 0]
    assert col(x, "user_sum_24h_log").tolist() == pytest.approx([math.log1p(v) for v in (0, 10, 30, 60, 0, 0)], rel=1e-6)
    assert col(x, "distinct_cards_7d").tolist() == [1, 1, 1, 2, 2, 1]        # never below 1: the card being used counts
    assert col(x, "amount_vs_avg_ratio").tolist() == pytest.approx([1.0, 2.0, 2.0, 2.0, 4.0, 1.0])
    assert col(x, "card_tx_1h").tolist() == [0, 1, 0, 1, 0, 0]                # per (user, card)
    assert col(x, "impossible_travel").tolist() == [0, 0, 0, 1, 0, 0]         # Springfield IL at 08:20, Dallas TX at 09:10
    assert ts[1] - ts[0] == 1200 and ts[3] - ts[2] == 1800


def test_tabformer_online_payments_never_count_as_travel_and_dates_are_utc(tmp_path):
    rows = ("0,0,2020,1,1,08:00,$10.00,Swipe Transaction,1,Springfield,IL,62701,5411,,No\n"
            "0,0,2020,1,1,08:30,$10.00,Online Transaction,2,ONLINE,,,4121,,No\n"
            "0,0,2020,1,1,09:00,$10.00,Swipe Transaction,3,Springfield,IL,62701,5411,,No\n")
    _, x, _, ts = run_adapter("tabformer", write(tmp_path, "t.csv", TABFORMER_HEADER + rows))
    assert col(x, "impossible_travel").tolist() == [0, 0, 0]
    assert ts[0] == 1577865600.0                                              # 2020-01-01 08:00 UTC


def test_tabformer_completed_features_are_flagged_and_real_ones_are_kept(tmp_path):
    a, x, y, _ = run_adapter("tabformer", write(tmp_path, "t.csv", TABFORMER_HEADER + TABFORMER_ROWS), seed=3)
    prov = a.provenance()
    assert prov["real_features"] == list(datasets.SPECS["tabformer"].real) and prov["approximate_features"] == ["impossible_travel"]
    synthetic = prov["synthetic_features"]
    assert set(synthetic) | set(prov["real_features"]) | set(prov["approximate_features"]) == set(features.VECTOR_FIELDS)
    assert {"ip_tx_1h", "device_tx_1h", "checkout_seconds_log", "pasted_card", "country_mismatch", "chargebacks",
            "device_linked_chargebacks", "new_device_for_account", "account_age_hours_log"} == set(synthetic)
    assert (col(x, "checkout_seconds_log") > 0).all()                         # completed, not left at zero
    assert prov["synthetic_fill"] == "class" and prov["rows_used"] == 6 and prov["fraud_rows"] == 1 and prov["file"] == "t.csv"


# ---- Sparkov -----------------------------------------------------------------------------------------------------
SPARKOV_HEADER = ("trans_date_trans_time,cc_num,merchant,category,amt,first,last,gender,street,city,state,zip,lat,long,city_pop,job,dob,trans_num,"
                  "unix_time,merch_lat,merch_long,is_fraud\n")


def sparkov_row(cc, t, amt, mlat, mlon, fraud=0, first="Ann", last="Invented"):
    return (f"2020-09-13 12:26:40,{cc},fraud_Shop,grocery_pos,{amt},{first},{last},F,1 Fake St,Springfield,IL,62701,40.0,-75.0,5000,Job,1980-01-01,"
            f"abc{t},{t},{mlat},{mlon},{fraud}\n")


def test_sparkov_velocity_and_impossible_travel(tmp_path):
    t0 = 1_600_000_000
    rows = (sparkov_row(111, t0, "10.00", 40.0, -75.0) + sparkov_row(111, t0 + 600, "30.00", 45.0, -75.0, fraud=1)      # 555 km in 10 min
            + sparkov_row(111, t0 + 1200, "20.00", 45.01, -75.01) + sparkov_row(222, t0 + 1300, "5.00", 10.0, 10.0))
    a, x, y, _ = run_adapter("sparkov", write(tmp_path, "s.csv", SPARKOV_HEADER + rows))
    assert y.tolist() == [0, 1, 0, 0]
    assert col(x, "impossible_travel").tolist() == [0, 1, 0, 0]               # the third is 1.4 km from the second: not travel
    assert col(x, "user_tx_1h").tolist() == [0, 1, 2, 0] and col(x, "card_tx_1h").tolist() == [0, 1, 2, 0]
    assert col(x, "distinct_cards_7d").tolist() == [1, 1, 1, 1]
    assert col(x, "amount_vs_avg_ratio").tolist() == pytest.approx([1.0, 3.0, 1.0, 1.0])     # 20 against the average of 10 and 30 is 1
    assert "first" in a.provenance()["columns_never_read"] and "dob" in a.provenance()["columns_never_read"]


def test_sparkov_never_reads_personal_columns(tmp_path):
    t0 = 1_600_000_000
    one = SPARKOV_HEADER + sparkov_row(111, t0, "10.00", 40, -75, first="Ann", last="One") + sparkov_row(111, t0 + 60, "20.00", 40, -75)
    two = SPARKOV_HEADER + sparkov_row(111, t0, "10.00", 40, -75, first="Zed", last="Other") + sparkov_row(111, t0 + 60, "20.00", 40, -75)
    a = run_adapter("sparkov", write(tmp_path, "a.csv", one), seed=1)
    b = run_adapter("sparkov", write(tmp_path, "b.csv", two), seed=1)
    assert np.array_equal(a[1], b[1])
    assert not {"first", "last", "street", "dob", "job", "gender", "trans_num"} & set(a[0].provenance()["columns_read"])


def test_sparkov_falls_back_to_the_date_column_without_unix_time(tmp_path):
    text = ("trans_date_trans_time,cc_num,amt,is_fraud\n2020-09-13 12:00:00,1,10.0,0\n2020-09-13 12:30:00,1,10.0,0\n")
    _, x, _, ts = run_adapter("sparkov", write(tmp_path, "s.csv", text))
    assert ts[1] - ts[0] == 1800 and col(x, "user_tx_1h").tolist() == [0, 1]


# ---- PaySim ------------------------------------------------------------------------------------------------------
PAYSIM_HEADER = "step,type,amount,nameOrig,oldbalanceOrg,newbalanceOrig,nameDest,oldbalanceDest,newbalanceDest,isFraud,isFlaggedFraud\n"


def test_paysim_never_uses_the_balance_columns_that_leak_the_label(tmp_path):
    rows = ["1,TRANSFER,100.0,C1,100.0,0.0,C9,0.0,0.0,1,0", "1,CASH_OUT,50.0,C1,50.0,0.0,C8,0.0,0.0,1,0", "2,PAYMENT,10.0,C2,900.0,890.0,M1,0,0,0,0"]
    same = [r.replace(",100.0,0.0,", ",777.0,555.0,").replace(",50.0,0.0,", ",1.0,2.0,") for r in rows]
    a, xa, ya, _ = run_adapter("paysim", write(tmp_path, "a.csv", PAYSIM_HEADER + "\n".join(rows) + "\n"), seed=2)
    b, xb, yb, _ = run_adapter("paysim", write(tmp_path, "b.csv", PAYSIM_HEADER + "\n".join(same) + "\n"), seed=2)
    assert np.array_equal(xa, xb) and ya.tolist() == yb.tolist() == [1, 1, 0]       # changing every balance changes nothing
    prov = a.provenance()
    for leaked in ("oldbalanceOrg", "newbalanceOrig", "oldbalanceDest", "newbalanceDest", "isFlaggedFraud"):
        assert leaked in prov["columns_never_read"] and leaked not in prov["columns_read"]
    assert col(xa, "user_tx_1h").tolist() == [0, 1, 0] and col(xa, "user_sum_24h_log")[1] == pytest.approx(math.log1p(100.0), rel=1e-6)
    assert "distinct_cards_7d" in prov["synthetic_features"] and "user_tx_1h" in prov["real_features"]


# ---- every adapter: streaming, errors, ordering, fill ---------------------------------------------------------------
@pytest.mark.parametrize("name", ["tabformer", "sparkov", "paysim"])
def test_chunk_size_never_changes_the_result_and_chunks_are_bounded(name):
    path = FIX / f"{name}_tiny.csv"
    big = datasets.Adapter(name, path, chunk_rows=10_000, seed=4)
    small = datasets.Adapter(name, path, chunk_rows=7, seed=4)
    cb, cs = list(big.chunks()), list(small.chunks())
    assert len(cb) == 1 and len(cs) > 5 and max(len(c.y) for c in cs) <= 7
    real = big.real_mask
    assert np.array_equal(np.vstack([c.x for c in cb])[:, real], np.vstack([c.x for c in cs])[:, real])     # the real columns are chunk-independent
    assert big.used == small.used > 300 and 0 < big.fraud < big.used


@pytest.mark.parametrize("name", ["tabformer", "sparkov", "paysim"])
def test_fixtures_map_to_finite_17_feature_rows_with_both_classes(name):
    a, x, y, ts = run_adapter(name, FIX / f"{name}_tiny.csv")
    assert x.shape[1] == 17 and x.dtype == np.float32 and np.isfinite(x).all() and set(y.tolist()) == {0, 1} and len(ts) == len(y)
    assert (x[:, [IDX["amount_log"]]] > 0).all() and (col(x, "amount_vs_avg_ratio") <= 20).all() and (col(x, "distinct_cards_7d") >= 1).all()
    assert a.provenance()["rows_read"] >= a.provenance()["rows_used"]


def test_the_adapter_streams_a_file_far_larger_than_its_chunk(tmp_path):
    t0 = 1_600_000_000
    path = tmp_path / "big.csv"
    with path.open("w") as fh:
        fh.write(SPARKOV_HEADER)
        for i in range(20_000):
            fh.write(sparkov_row(i % 500, t0 + i * 30, "12.50", 40.0, -75.0))
    a = datasets.Adapter("sparkov", path, chunk_rows=1000)
    sizes = [len(c.y) for c in a.chunks()]
    assert len(sizes) == 20 and max(sizes) == 1000 and sum(sizes) == 20_000
    assert len(a.windows.state) <= 500                                        # state is per entity, never per row


def test_adapters_refuse_bad_input_with_readable_errors(tmp_path):
    with pytest.raises(datasets.DatasetError, match="unknown dataset"):
        datasets.Adapter("nope", tmp_path / "x.csv")
    with pytest.raises(datasets.DatasetError, match="no such file"):
        list(datasets.Adapter("tabformer", tmp_path / "missing.csv").chunks())
    with pytest.raises(datasets.DatasetError, match="lacks the column"):
        list(datasets.Adapter("tabformer", write(tmp_path, "a.csv", "User,Card\n1,2\n")).chunks())
    with pytest.raises(datasets.DatasetError, match="empty"):
        list(datasets.Adapter("paysim", write(tmp_path, "e.csv", "")).chunks())
    with pytest.raises(datasets.DatasetError, match="unix_time"):
        list(datasets.Adapter("sparkov", write(tmp_path, "s.csv", "cc_num,amt,is_fraud\n1,2,0\n")).chunks())
    with pytest.raises(datasets.DatasetError, match="fill"):
        datasets.Adapter("paysim", tmp_path / "x.csv", fill="mystery")


def test_bad_rows_are_skipped_and_counted_not_fatal(tmp_path):
    text = PAYSIM_HEADER + "1,PAYMENT,10.0,C1,0,0,C2,0,0,0,0\nxx,PAYMENT,10.0,C1,0,0,C2,0,0,0,0\n1,PAYMENT\n1,PAYMENT,abc,C1,0,0,C2,0,0,0,0\n1,PAYMENT,0,C1,0,0,C2,0,0,0,0\n2,PAYMENT,5,C1,0,0,C2,0,0,maybe,0\n"
    a, x, _, _ = run_adapter("paysim", write(tmp_path, "p.csv", text))
    assert len(x) == 1 and a.skipped == {"unparseable": 3, "wrong_column_count": 1, "refund_or_zero": 1}


def test_gzip_files_and_column_order_and_bom(tmp_path):
    header = "Is Fraud?,Amount,Time,Day,Month,Year,Card,User\n"                 # a different column order, as some copies have
    text = "﻿" + header + "No,$10.00,08:00,1,1,2020,0,0\nYes,$20.00,08:20,1,1,2020,0,0\n"
    p = tmp_path / "t.csv.gz"
    with gzip.open(p, "wt", encoding="utf-8") as fh:
        fh.write(text)
    _, x, y, _ = run_adapter("tabformer", p)
    assert y.tolist() == [0, 1] and col(x, "user_tx_1h").tolist() == [0, 1]


def test_late_records_are_counted_and_old_entities_age_out():
    w = datasets.Windows()
    w.observe("u", 1000.0, 5.0)
    w.observe("u", 500.0, 5.0)                                                # arrives late
    assert w.out_of_order == 1
    n1, n24, s24, cards, avg, _ = w.observe("u", 1000.0 + 40 * 86400, 1.0)    # forty days later: nothing is left but the new payment
    assert (n1, n24, s24, avg) == (0, 0, 0.0, 0.0) and cards == 0
    small = datasets.Windows(max_entities=2)
    for k in "abc":
        small.observe(k, 1.0, 1.0)
    assert len(small.state) == 2 and small.evicted == 1


def test_fill_modes_keep_real_columns_and_differ_only_in_completed_ones():
    path = FIX / "tabformer_tiny.csv"
    _, xc, yc, _ = run_adapter("tabformer", path, fill="class", seed=5)
    _, xn, yn, _ = run_adapter("tabformer", path, fill="neutral", seed=5)
    real = datasets.Adapter("tabformer", path).real_mask
    assert np.array_equal(xc[:, real], xn[:, real]) and yc.tolist() == yn.tolist()
    fraud_rows = yc == 1
    c, n = col(xc, "checkout_seconds_log")[fraud_rows].mean(), col(xn, "checkout_seconds_log")[fraud_rows].mean()
    assert c < n - 0.3                                                         # class fill gives fraud rows a hurried checkout, neutral does not


# ---- the generator ---------------------------------------------------------------------------------------------------
def test_synthetic_generator_is_vectorised_reproducible_and_mixes_the_classes():
    x1, y1 = synth.generate(np.random.default_rng(9), 50_000)
    x2, y2 = synth.generate(np.random.default_rng(9), 50_000)
    assert x1.shape == (50_000, 17) and x1.dtype == np.float32 and np.array_equal(x1, x2) and np.array_equal(y1, y2)
    assert 0.08 < y1.mean() < 0.11 and abs(sum(synth.MIX.values()) - 1) < 1e-9
    assert sum(synth.counts(1_000_003).values()) == 1_000_003
    assert np.isfinite(x1).all() and (x1[:, IDX["amount_log"]] > 0).all() and x1.max() < 1e3 and x1.min() >= 0   # no row left unwritten
    busy = synth.rows(np.random.default_rng(1), 5000, "busy_shopper")
    assert busy[:, IDX["user_tx_1h"]].max() >= 6 and (busy[:, IDX["checkout_seconds_log"]] > 2).mean() > 0.95        # busy, not hurried
    testing = synth.rows(np.random.default_rng(1), 5000, "card_testing")
    assert testing[:, IDX["distinct_cards_7d"]].mean() > 4 and testing[:, IDX["pasted_card"]].mean() > 0.8
    with pytest.raises(ValueError):
        synth.rows(np.random.default_rng(1), 5, "mystery")


def test_default_training_set_is_a_million_rows():
    assert trainer.DEFAULT_ROWS == 1_000_000
    assert trainer.parser().parse_args([]).synthetic_rows is None


# ---- metrics ---------------------------------------------------------------------------------------------------------
def test_metrics_on_known_rankings_ties_and_weights():
    y, p = np.array([1, 1, 0, 0]), np.array([0.9, 0.8, 0.3, 0.1])
    m = trainer.metrics(y, p)
    assert (m["pr_auc"], m["roc_auc"], m["recall_at_1pct_fpr"]) == (1.0, 1.0, 1.0)
    worst = trainer.metrics(y, np.array([0.1, 0.2, 0.8, 0.9]))
    assert worst["roc_auc"] == 0.0 and worst["recall_at_1pct_fpr"] == 0.0
    tied = trainer.metrics(y, np.full(4, 0.5))
    assert tied["roc_auc"] == 0.5 and tied["recall_at_1pct_fpr"] == 0.0
    mixed = trainer.metrics(np.array([1, 0, 1, 0]), np.array([0.9, 0.8, 0.7, 0.1]))
    assert mixed["roc_auc"] == 0.75 and mixed["pr_auc"] == pytest.approx(0.8333, abs=1e-3)
    heavy = trainer.metrics(np.array([1, 0, 1, 0]), np.array([0.9, 0.8, 0.7, 0.1]), np.array([1, 1, 1, 10.0]))
    assert heavy["roc_auc"] == pytest.approx(1 - 1 / 22, abs=1e-4) and heavy["fraud_rate"] == pytest.approx(2 / 13, abs=1e-4)
    assert trainer.metrics(np.array([1, 1]), np.array([0.5, 0.6]))["pr_auc"] is None


def test_score_distribution_covers_every_row_in_three_bands():
    y = np.array([0, 0, 1, 1, 0, 1])
    p = np.array([0.05, 0.299, 0.5, 0.7, 0.9, 0.2])
    d = trainer.score_distribution(y, p)
    assert sum(d["deciles_0_to_1000"]) == pytest.approx(1.0, abs=1e-4)
    assert {b: v["share"] for b, v in d["bands"].items()} == pytest.approx({"approve": 0.5, "challenge": 1 / 6, "decline": 1 / 3}, abs=1e-4)
    assert d["bands"]["decline"]["fraud_rate"] == 0.5 and d["bands"]["approve"]["share_of_fraud"] == pytest.approx(1 / 3, abs=1e-3)


def test_collector_keeps_every_fraud_row_and_reweights_the_rest_within_its_cap():
    rng = np.random.default_rng(3)
    col_ = trainer.Collector(2000, rng)
    for i in range(20):
        y = (np.arange(1000) % 50 == 0).astype(np.int8)                       # 2 % fraud
        col_.add(np.zeros((1000, 17), dtype=np.float32), y, np.full(1000, float(i)))
    x, y, ts, w = col_.result()
    assert len(y) <= 2000 + 200 and y.sum() == 20 * 20 and (w[y == 1] == 1).all() and col_.rate < 1 and col_.seen == 20_000
    assert w.sum() == pytest.approx(20_000, rel=0.15)                          # the weights still add up to the rows that were seen
    assert (np.diff(ts) >= 0).all()                                             # order (time) survives


# ---- training end to end -----------------------------------------------------------------------------------------------
def train(tmp_path, *extra, name="m.json"):
    out = tmp_path / name
    args = trainer.parser().parse_args(["--out", str(out), "--rounds", "8", "--threads", "1", *extra])
    args.label_sources = list(trainer.SOURCES)
    report = trainer.train(args, log=None)
    return out, report


def test_synthetic_training_writes_a_loadable_model_and_a_report(tmp_path):
    out, report = train(tmp_path, "--synthetic-rows", "20000")
    doc = json.loads(out.read_text())
    assert ml.validate(doc)["trees"] == 8 and doc["features"] == list(features.VECTOR_FIELDS)
    model = ml.Model(doc)                                                       # loadable by ml.py with the same feature check
    assert doc["reference"] and all(model.score(r["x"], explain=False)[0] == r["score"] for r in doc["reference"])
    assert isinstance(doc["trained_on"], dict) and doc["trained_on"]["sources"][0]["kind"] == "synthetic"
    assert doc["trained_on"]["sources"][0]["rows_used"] == 20000
    rep = json.loads((tmp_path / "training_report.json").read_text())
    h = rep["holdout"]["synthetic"]
    assert {"pr_auc", "roc_auc", "recall_at_1pct_fpr", "fraud_rate", "score_distribution"} <= set(h) and 0.5 < h["roc_auc"] <= 1
    assert set(h["score_distribution"]["bands"]) == {"approve", "challenge", "decline"}
    assert rep["data"]["rows_train"] + rep["data"]["rows_holdout"] == 20000 and 0.15 < rep["data"]["rows_holdout"] / 20000 < 0.25
    assert rep["data"]["split"]["synthetic"].startswith("stratified") and rep["timing_seconds"]["total"] > 0
    assert len(rep["scenarios"]) == len(scenarios.SCENARIOS) and report["model"]["trees"] == 8
    assert modelstore_accepts(doc)


def modelstore_accepts(doc):
    from paybroker import modelstore
    modelstore.parse_upload(json.dumps({"model": doc}).encode())
    return True


def test_the_default_hyperparameters_keep_the_bundled_shape():
    args = trainer.parser().parse_args([])
    assert (args.rounds, args.max_depth, args.eta, args.min_child_weight, args.reg_lambda, args.holdout) == (70, 3, 0.1, 15, 5, 0.2)
    assert Path(args.out) == ml.MODEL_PATH


@pytest.mark.parametrize("name", ["tabformer", "sparkov", "paysim"])
def test_training_from_a_dataset_fixture_mixed_with_synthetic(tmp_path, name):
    out, rep = train(tmp_path, "--dataset", name, "--path", str(FIX / f"{name}_tiny.csv"), "--mix-synthetic", "0.4", "--chunk-rows", "50", name=f"{name}.json")
    doc = json.loads(out.read_text())
    src = {s["kind"]: s for s in doc["trained_on"]["sources"]}
    ds, syn = src["dataset"], src["synthetic"]
    assert ds["name"] == name and ds["rows_kept"] == ds["rows_used"] and ds["synthetic_fill"] == "class"
    assert set(ds["real_features"]) == set(datasets.SPECS[name].real) and ds["synthetic_features"]
    assert syn["rows_used"] / (syn["rows_used"] + ds["rows_kept"]) == pytest.approx(0.4, abs=0.01)         # the synthetic share asked for
    assert name in doc["trained_on"]["summary"] and "completed from the synthetic generator" in doc["trained_on"]["summary"]
    h = rep["holdout"]
    assert f"dataset:{name}" in h and f"dataset:{name}:real_features_only" in h and "synthetic" in h and "all" in h
    assert h[f"dataset:{name}"]["split"].startswith("time-ordered") and h["synthetic"]["split"].startswith("stratified")
    assert any("completed from the synthetic generator" in w or "completed" in w for w in rep["warnings"])
    ml.validate(doc)


def test_the_dataset_holdout_is_the_newest_rows():
    x = np.zeros((100, 17), dtype=np.float32)
    ts = np.random.default_rng(0).permutation(100).astype(float)
    y = np.zeros(100, dtype=np.int8)
    tr, ho, how = trainer._split(x, y, ts, np.ones(100), 0.2, np.random.default_rng(1))
    assert how.startswith("time-ordered") and ts[tr].max() < ts[ho].min() and len(ho) == 20
    tr, ho, how = trainer._split(x, np.arange(100) % 2, None, np.ones(100), 0.2, np.random.default_rng(1))
    assert how.startswith("stratified") and sorted(np.concatenate([tr, ho]).tolist()) == list(range(100)) and ((np.arange(100) % 2)[ho] == 1).sum() == 10


def test_negative_sampling_keeps_rates_honest_in_training(tmp_path):
    out, rep = train(tmp_path, "--dataset", "paysim", "--path", str(FIX / "paysim_tiny.csv"), "--max-real-rows", "300", "--chunk-rows", "100")
    prov = rep["data"]["sources"][0]
    assert prov["rows_kept"] <= 300 + 60 and prov["negative_sample_rate"] < 1 and prov["rows_read"] == 700
    assert rep["holdout"]["dataset:paysim"]["fraud_rate"] == pytest.approx(prov["fraud_rows"] / prov["rows_used"], abs=0.05)


def export_fixture(tmp_path, fmt):
    rng = np.random.default_rng(11)
    x, y = synth.generate(rng, 600)
    names = list(features.VECTOR_FIELDS)
    p = tmp_path / f"export.{fmt}"
    srcs = ["case", "chargeback", "aging"]
    lines = []
    for i in range(600):
        label = int(y[i]) if i % 10 else None
        row = {"id": i, "created_at": f"2026-01-{1 + i // 30:02d}T10:00:00+00:00", "payment_id": f"pay_{i}", "feature_hash": features.FEATURE_HASH, "model_version": "m",
               "score": 10, "decision": "APPROVE", "label": label, "label_source": None if label is None else srcs[i % 3], "labeled_at": None}
        lines.append((row, x[i]))
    if fmt == "csv":
        import csv
        with p.open("w", newline="") as fh:
            fh.write("# paybroker training export (a fixture)\n# features: " + ", ".join(names) + "\n")
            w = csv.writer(fh)
            w.writerow([*lines[0][0], *names])
            for row, v in lines:
                w.writerow([("" if val is None else val) for val in row.values()] + [f"{float(f):.7g}" for f in v])
    else:
        with p.open("w") as fh:
            fh.write(json.dumps({"_meta": {"features": names, "feature_hash": features.FEATURE_HASH}}) + "\n")
            for row, v in lines:
                fh.write(json.dumps({**row, "features": dict(zip(names, map(float, v), strict=True))}) + "\n")
    return p


@pytest.mark.parametrize("fmt", ["csv", "ndjson"])
def test_training_from_an_export_with_and_without_synthetic_mix(tmp_path, fmt):
    path = export_fixture(tmp_path, fmt)
    x, y, ts, prov = trainer.load_export(path)
    assert len(y) == 540 and prov["skipped"]["unlabeled"] == 60 and sum(prov["label_sources"].values()) == 540 and not np.isnan(ts).any()
    assert len(trainer.load_export(path, ("chargeback",))[1]) == 180 and trainer.load_export(path, ("chargeback",))[3]["skipped"]["label_source_filtered"] == 360
    out, rep = train(tmp_path, "--from-export", str(path), name="e.json")
    assert rep["data"]["rows_total"] == 540 and rep["data"]["split"]["export"].startswith("time-ordered")
    assert any("feedback loop" in w for w in rep["warnings"]) and any("aging" in w for w in rep["warnings"])
    out2, rep2 = train(tmp_path, "--from-export", str(path), "--mix-synthetic", "0.5", name="e2.json")
    assert rep2["data"]["rows_total"] == 1080 and "export" in rep2["holdout"] and "synthetic" in rep2["holdout"] and "all" in rep2["holdout"]
    kinds = [s["kind"] for s in json.loads(out2.read_text())["trained_on"]["sources"]]
    assert kinds == ["export", "synthetic"]


def test_export_loading_refuses_a_different_layout_and_missing_columns(tmp_path):
    path = export_fixture(tmp_path, "ndjson")
    lines = path.read_text().splitlines()
    meta = json.loads(lines[0])
    meta["_meta"]["features"] = meta["_meta"]["features"][::-1]
    path.write_text("\n".join([json.dumps(meta), *lines[1:]]))
    with pytest.raises(trainer.TrainError, match="different feature list"):
        trainer.load_export(path)
    bad = tmp_path / "bad.csv"
    bad.write_text("id,label\n1,1\n")
    with pytest.raises(trainer.TrainError, match="lacks the feature columns"):
        trainer.load_export(bad)
    with pytest.raises(trainer.TrainError, match="no such file"):
        trainer.load_export(tmp_path / "nope.csv")
    old = export_fixture(tmp_path, "csv")
    old.write_text(old.read_text().replace(features.FEATURE_HASH, "oldhash00000"))
    with pytest.raises(trainer.TrainError, match="no usable labeled rows"):
        trainer.load_export(old)


# ---- the command line --------------------------------------------------------------------------------------------------
def test_cli_flags_validate_and_report(tmp_path, capsys):
    out = tmp_path / "cli.json"
    base = ["--out", str(out), "--rounds", "4", "--threads", "1", "--synthetic-rows", "5000"]
    assert trainer.main(base) == 0
    printed = capsys.readouterr().out
    assert "PR-AUC" in printed and "scenarios:" in printed and "wrote" in printed and out.exists() and (tmp_path / "training_report.json").exists()
    assert trainer.main([*base, "--dry-run", "--out", str(tmp_path / "never.json")]) == 0
    assert not (tmp_path / "never.json").exists()
    assert trainer.main([*base, "--report", str(tmp_path / "r.json"), "--version", "my-version"]) == 0
    assert json.loads(out.read_text())["version"] == "my-version" and (tmp_path / "r.json").exists()
    for bad, msg in ((["--dataset", "tabformer"], "--dataset and --path"), (["--path", "x.csv"], "--dataset and --path"),
                     (["--label-sources", "guess"], "unknown label source"), (["--holdout", "0.9"], "--holdout"),
                     (["--synthetic-rows", "100", "--mix-synthetic", "0.3", "--from-export", str(export_fixture(tmp_path, "csv"))], "not both"),
                     (["--from-export", str(tmp_path / "missing.csv")], "no such file"),
                     (["--dataset", "paysim", "--path", str(tmp_path / "missing.csv")], "no such file"),
                     (["--synthetic-rows", "0"], "nothing to train on"), (["--synthetic-rows", "40"], "too few rows")):
        with pytest.raises(SystemExit) as exc:
            trainer.main([*base[:4], *bad])
        assert msg in str(exc.value), bad
    with pytest.raises(SystemExit):
        trainer.main(["--dataset", "unknown", "--path", "x"])
    with pytest.raises(SystemExit) as exc:
        trainer.main(["--mix-synthetic", "0.3", "--dataset", "paysim", "--path", str(FIX / "paysim_tiny.csv"), "--synthetic-rows", "5"])
    assert "not both" in str(exc.value)


def test_make_train_still_works_the_script_runs_and_documents_every_flag(tmp_path):
    script = ROOT / "scripts" / "train_model.py"
    helped = subprocess.run([sys.executable, str(script), "--help"], capture_output=True, text=True, check=True).stdout
    for flag in ("--synthetic-rows", "--from-export", "--mix-synthetic", "--dataset", "--path", "--out", "--dry-run", "--fill", "--label-sources"):
        assert flag in helped, flag
    done = subprocess.run([sys.executable, str(script), "--synthetic-rows", "4000", "--rounds", "3", "--threads", "1", "--out", str(tmp_path / "s.json")],
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert (tmp_path / "s.json").exists() and (tmp_path / "training_report.json").exists()
    makefile = (ROOT / "Makefile").read_text()
    assert "train:     ; .venv/bin/python scripts/train_model.py" in makefile


def test_the_committed_report_matches_the_committed_model():
    report = json.loads((ml.MODEL_PATH.parent / "training_report.json").read_text())
    doc = json.loads(ml.MODEL_PATH.read_text())
    assert report["version"] == doc["version"] and report["feature_hash"] == features.FEATURE_HASH
    assert report["data"]["rows_total"] >= 1_000_000 and report["model"]["trees"] == len(doc["trees"])
    assert all(s["ok"] for s in report["scenarios"]) and report["holdout"]["synthetic"]["roc_auc"] > 0.85
    assert report["model"]["size_bytes"] == ml.MODEL_PATH.stat().st_size
