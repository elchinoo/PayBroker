"""Train the Tier 2 fraud model on SYNTHETIC data and export it as JSON trees.

    uv pip install -e ".[train]" && python scripts/train_model.py

The data generator below invents plausible attack patterns (account takeover, card testing, synthetic identities,
chargeback-linked devices) next to ordinary customers, with label noise. It exists so the sample has a model to run; it
says nothing about real fraud, and a production model must be trained on your own labelled history.
"""
import json
import math
import sys
from datetime import date
from pathlib import Path

import numpy as np
import xgboost as xgb

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from paybroker.features import VECTOR_FIELDS  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "src" / "paybroker" / "models" / "fraud_gbdt.json"
rng = np.random.default_rng(20261022)


def rows(n: int, kind: str) -> np.ndarray:
    d = {k: np.zeros(n) for k in VECTOR_FIELDS}
    f = rng.random

    def amount(median, sigma, size=n):
        return np.log1p(rng.lognormal(math.log(median), sigma, size))

    d["checkout_seconds_log"] = np.log1p(rng.lognormal(math.log(60), 0.6, n))
    d["account_age_hours_log"] = np.log1p(rng.exponential(2500, n) + 30)
    d["amount_log"] = amount(45, 0.9)
    d["amount_vs_avg_ratio"] = np.clip(rng.lognormal(0, 0.45, n), 0.1, 20)
    d["user_tx_1h"] = rng.poisson(0.25, n)
    d["user_tx_24h"] = d["user_tx_1h"] + rng.poisson(0.6, n)
    d["distinct_cards_7d"] = np.maximum(1, rng.poisson(0.15, n) + 1)
    d["ip_tx_1h"] = rng.poisson(0.3, n)
    d["device_tx_1h"] = rng.poisson(0.3, n)
    d["card_tx_1h"] = rng.poisson(0.2, n)
    d["user_sum_24h_log"] = np.log1p(d["user_tx_24h"] * rng.lognormal(math.log(45), 0.8, n))
    d["pasted_card"] = (f(n) < 0.03).astype(float)
    d["country_mismatch"] = (f(n) < 0.03).astype(float)
    d["new_device_for_account"] = (f(n) < 0.04).astype(float)
    if kind == "benign_anomaly":
        d["impossible_travel"] = (f(n) < 0.15).astype(float)
        d["new_device_for_account"] = (f(n) < 0.5).astype(float)
        d["country_mismatch"] = (f(n) < 0.3).astype(float)
        d["amount_log"] = amount(400, 0.8)
        d["amount_vs_avg_ratio"] = np.clip(rng.lognormal(1.0, 0.5, n), 0.1, 20)
    elif kind == "ato":
        d["new_device_for_account"] = np.ones(n)
        d["impossible_travel"] = (f(n) < 0.55).astype(float)
        d["country_mismatch"] = (f(n) < 0.65).astype(float)
        d["amount_log"] = amount(700, 0.7)
        d["amount_vs_avg_ratio"] = np.clip(rng.lognormal(1.8, 0.5, n), 1, 20)
        d["checkout_seconds_log"] = np.log1p(rng.lognormal(math.log(14), 0.4, n))
        d["pasted_card"] = (f(n) < 0.4).astype(float)
        d["user_tx_1h"] = rng.poisson(1.2, n)
    elif kind == "card_testing":
        d["amount_log"] = amount(2.5, 0.5)
        d["ip_tx_1h"] = rng.poisson(14, n) + 3
        d["card_tx_1h"] = rng.poisson(3, n) + 1
        d["user_tx_1h"] = rng.poisson(6, n) + 1
        d["user_tx_24h"] = d["user_tx_1h"] + rng.poisson(4, n)
        d["distinct_cards_7d"] = rng.poisson(5, n) + 2
        d["checkout_seconds_log"] = np.log1p(rng.lognormal(math.log(3), 0.5, n))
        d["pasted_card"] = (f(n) < 0.9).astype(float)
        d["account_age_hours_log"] = np.log1p(rng.exponential(30, n))
        d["device_tx_1h"] = rng.poisson(8, n)
    elif kind == "synthetic_identity":
        d["account_age_hours_log"] = np.log1p(rng.exponential(12, n))
        d["amount_log"] = amount(900, 0.6)
        d["distinct_cards_7d"] = rng.poisson(2.5, n) + 2
        d["device_linked_chargebacks"] = rng.poisson(1.0, n)
        d["country_mismatch"] = (f(n) < 0.45).astype(float)
        d["checkout_seconds_log"] = np.log1p(rng.lognormal(math.log(20), 0.4, n))
    elif kind == "chargeback_link":
        d["device_linked_chargebacks"] = rng.poisson(2, n) + 1
        d["chargebacks"] = rng.poisson(0.8, n)
        d["amount_log"] = amount(250, 0.8)
        d["new_device_for_account"] = (f(n) < 0.3).astype(float)
    return np.column_stack([d[k] for k in VECTOR_FIELDS])


def mild(attack: np.ndarray, share: float = 0.4) -> np.ndarray:
    """Real attacks are not all loud: blend a share of them back towards ordinary behaviour so scores spread out and the
    middle (3-D Secure) band has something to catch."""
    ordinary = rows(len(attack), "legit")
    pick = rng.random(attack.shape) < 0.55
    out = np.where(pick, ordinary, attack)
    keep_loud = rng.random(len(attack)) > share
    return np.where(keep_loud[:, None], attack, out)


def dataset():
    parts = [(rows(60000, "legit"), 0), (rows(5000, "benign_anomaly"), 0), (mild(rows(1500, "ato")), 1),
             (mild(rows(2000, "card_testing")), 1), (mild(rows(1000, "synthetic_identity")), 1),
             (mild(rows(900, "chargeback_link")), 1)]
    x = np.vstack([p[0] for p in parts])
    y = np.concatenate([np.full(len(p[0]), p[1]) for p in parts])
    flip = rng.random(len(y)) < 0.02                      # label noise
    y = np.where(flip, 1 - y, y)
    order = rng.permutation(len(y))
    return x[order], y[order]


def flatten(node: dict, out: dict) -> int:
    """Depth-first flatten of one xgboost JSON tree into parallel arrays (index = node id in `out`)."""
    idx = len(out["f"])
    for k in out:
        out[k].append(0)
    if "leaf" in node:
        out["l"][idx], out["r"][idx], out["v"][idx] = -1, -1, node["leaf"]
        out["e"][idx], out["cover"][idx] = node["leaf"], node.get("cover", 1.0)
        return idx
    kids = {c["nodeid"]: c for c in node["children"]}
    yes, no = kids[node["yes"]], kids[node["no"]]
    out["f"][idx] = int(node["split"][1:])
    out["t"][idx] = node["split_condition"]
    out["cover"][idx] = node.get("cover", 1.0)
    out["l"][idx] = flatten(yes, out)
    out["r"][idx] = flatten(no, out)
    cl, cr = out["cover"][out["l"][idx]], out["cover"][out["r"][idx]]
    out["e"][idx] = (out["e"][out["l"][idx]] * cl + out["e"][out["r"][idx]] * cr) / (cl + cr)
    return idx


def main() -> None:
    x, y = dataset()
    cut = int(len(y) * 0.85)
    names = [f"f{i}" for i in range(x.shape[1])]
    dtrain, dtest = xgb.DMatrix(x[:cut], y[:cut], feature_names=names), xgb.DMatrix(x[cut:], y[cut:], feature_names=names)
    params = {"objective": "binary:logistic", "max_depth": 3, "eta": 0.1, "min_child_weight": 15, "lambda": 5, "subsample": 0.9, "colsample_bytree": 0.9,
              "eval_metric": "aucpr", "seed": 7, "base_score": 0.5, "nthread": 2}
    bst = xgb.train(params, dtrain, num_boost_round=70, evals=[(dtest, "holdout")], verbose_eval=20)
    p = bst.predict(dtest)
    yt = y[cut:]
    scores = np.round(p * 1000)
    low, mid, high = scores < 300, (scores >= 300) & (scores < 700), scores >= 700
    print(f"holdout: fraud rate {yt.mean():.3f}; low {low.mean():.3f} (fraud inside {yt[low].mean():.4f}), "
          f"challenge {mid.mean():.3f} (fraud inside {yt[mid].mean() if mid.any() else 0:.3f}), "
          f"decline {high.mean():.3f} (fraud inside {yt[high].mean() if high.any() else 0:.3f})")
    trees = []
    for dump in bst.get_dump(dump_format="json", with_stats=True):
        out = {k: [] for k in ("f", "t", "l", "r", "v", "e", "cover")}
        flatten(json.loads(dump), out)
        out.pop("cover")
        trees.append(out)
    # reference vectors: lets the tests prove the JSON walk reproduces xgboost exactly, without xgboost installed
    ref = x[cut:cut + 12]
    ref_scores = [int(round(v * 1000)) for v in bst.predict(xgb.DMatrix(ref, feature_names=names))]
    doc = {"version": f"synthetic-{date.today().isoformat()}", "trained_on": "synthetic data (scripts/train_model.py)",
           "features": list(VECTOR_FIELDS), "base_margin": 0.0, "trees": trees,
           "reference": [{"x": [float(v) for v in r], "score": s} for r, s in zip(ref, ref_scores)]}
    OUT.write_text(json.dumps(doc, separators=(",", ":")))
    print(f"wrote {OUT} ({OUT.stat().st_size // 1024} KB, {len(trees)} trees)")


if __name__ == "__main__":
    main()
