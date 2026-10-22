"""Train the Tier 2 fraud model and export it as the JSON trees `ml.py` walks (needs: `uv pip install -e ".[train]"`).

Used by `scripts/train_model.py` (`make train`). Sources, any mix:

* synthetic rows from `synth.py` (default: 1,000,000, the project's original class mix scaled up);
* an export of captured training data (`GET /api/admin/training/export`, CSV or NDJSON), labeled rows only;
* a public dataset through an adapter (`datasets.py`): TabFormer, Sparkov or PaySim.

The model is XGBoost (depth-3 trees, log loss) trained on 80 % of the rows and scored on a 20 % holdout that is time-ordered for
sources with timestamps (the newest rows) and stratified-random for synthetic ones. The report (`training_report.json`, next to the
model) holds PR-AUC, ROC-AUC, recall at 1 % false positives and the score distribution of each source's holdout, and the table of
named scenarios from `scenarios.py`. Read the report's `warnings`: synthetic holdouts say little about real fraud.
"""
import argparse
import csv
import json
import math
import sys
import time
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np

from . import __version__, datasets, fraud, ml, scenarios, synth
from .features import FEATURE_HASH, VECTOR_FIELDS

DEFAULT_ROWS = 1_000_000
MAX_REAL_ROWS = 4_000_000
MIN_ROWS = 500
DEFAULT_PARAMS = {"objective": "binary:logistic", "max_depth": 3, "eta": 0.1, "min_child_weight": 15, "lambda": 5, "subsample": 0.9,
                  "colsample_bytree": 0.9, "base_score": 0.5, "tree_method": "hist"}
SOURCES = ("chargeback", "case", "aging")


class TrainError(ValueError):
    pass


# ---- metrics (numpy only, weight aware) -----------------------------------------------------------------------------
def _curve(y: np.ndarray, p: np.ndarray, w: np.ndarray):
    order = np.argsort(-p, kind="stable")
    y, p, w = y[order], p[order], w[order]
    tp, fp = np.cumsum(w * y), np.cumsum(w * (1 - y))
    last = np.r_[np.flatnonzero(np.diff(p) != 0), len(p) - 1]           # one point per distinct score: ties are not an order
    return tp[last], fp[last]


def metrics(y: np.ndarray, p: np.ndarray, w: np.ndarray | None = None) -> dict:
    """PR-AUC (average precision), ROC-AUC and recall at 1 % false-positive rate; `w` are sample weights (the negatives
    that were down-sampled count for more). Probabilities in [0, 1]."""
    y = y.astype(np.float64)
    w = np.ones(len(y)) if w is None else w.astype(np.float64)
    pos, neg = float((w * y).sum()), float((w * (1 - y)).sum())
    out = {"rows": int(len(y)), "weighted_rows": round(float(w.sum()), 1), "fraud_rate": round(pos / (pos + neg), 5) if pos + neg else None}
    if pos == 0 or neg == 0:
        return out | {"pr_auc": None, "roc_auc": None, "recall_at_1pct_fpr": None, "note": "needs both classes in the holdout"}
    tp, fp = _curve(y, p, w)
    recall, fpr, precision = tp / pos, fp / neg, tp / (tp + fp)
    trapz = getattr(np, "trapezoid", None) or np.trapz
    ok = np.flatnonzero(fpr <= 0.01)
    return out | {"pr_auc": round(float(np.sum(np.diff(np.r_[0.0, recall]) * precision)), 5),
                  "roc_auc": round(float(trapz(np.r_[0.0, recall], np.r_[0.0, fpr])), 5),
                  "recall_at_1pct_fpr": round(float(recall[ok[-1]]) if len(ok) else 0.0, 5)}


def score_distribution(y: np.ndarray, p: np.ndarray, w: np.ndarray | None = None) -> dict:
    """How the holdout spreads over the 0-1000 score: deciles and the three decision bands (approve below 300, challenge
    with 3-D Secure to 699, decline from 700), with the fraud rate inside each band and the share of all fraud it holds."""
    w = np.ones(len(y)) if w is None else w
    s = np.rint(p * 1000).astype(np.int32)
    total, fraud_total = float(w.sum()), float((w * y).sum())
    deciles = [round(float(w[(s >= lo) & (s < (lo + 100 if lo < 900 else 1001))].sum() / total), 5) for lo in range(0, 1000, 100)]
    bands = {}
    for name, lo, hi in (("approve", 0, fraud.LOW), ("challenge", fraud.LOW, fraud.HIGH), ("decline", fraud.HIGH, 1001)):
        m = (s >= lo) & (s < hi)
        weight, fr = float(w[m].sum()), float((w * y)[m].sum())
        bands[name] = {"share": round(weight / total, 5), "fraud_rate": round(fr / weight, 5) if weight else None,
                       "share_of_fraud": round(fr / fraud_total, 5) if fraud_total else None}
    return {"deciles_0_to_1000": deciles, "bands": bands}


# ---- reading an export ----------------------------------------------------------------------------------------------
def load_export(path: str | Path, sources: tuple[str, ...] = SOURCES) -> tuple[np.ndarray, np.ndarray, np.ndarray, dict]:
    """Labeled rows of an export (CSV or NDJSON), streamed line by line: (x, y, created_at epoch seconds, provenance).
    Rows with another feature layout (feature_hash) or a label source not in `sources` are skipped and counted."""
    path = Path(path)
    if not path.is_file():
        raise TrainError(f"no such file: {path}")
    xs, ys, ts = [], [], []
    skipped = {"unlabeled": 0, "other_feature_layout": 0, "label_source_filtered": 0, "unparseable": 0}
    kinds: dict[str, int] = {}
    names = list(VECTOR_FIELDS)

    def take(feats, label, src, created, fhash):
        if label in (None, ""):
            skipped["unlabeled"] += 1
        elif fhash not in (None, "", FEATURE_HASH):
            skipped["other_feature_layout"] += 1
        elif src not in sources:
            skipped["label_source_filtered"] += 1
        else:
            try:
                xs.append([float(v) for v in feats])
                ys.append(int(float(label)))
                ts.append(datetime.fromisoformat(created).timestamp() if created else math.nan)
                kinds[src] = kinds.get(src, 0) + 1
            except (ValueError, TypeError):
                skipped["unparseable"] += 1

    with path.open(newline="", encoding="utf-8") as fh:
        first = fh.readline()
        fh.seek(0)
        if first.startswith("{"):
            for line in fh:
                if not line.strip():
                    continue
                try:
                    doc = json.loads(line)
                except ValueError:
                    skipped["unparseable"] += 1
                    continue
                if "_meta" in doc:
                    if doc["_meta"].get("features") != names:
                        raise TrainError("the export was made with a different feature list than this version of PayBroker")
                    continue
                f = doc.get("features") or {}
                take([f.get(n) for n in names], doc.get("label"), doc.get("label_source"), doc.get("created_at"), doc.get("feature_hash"))
        else:
            reader = csv.DictReader(line for line in fh if not line.startswith("#"))
            if reader.fieldnames is None or any(n not in reader.fieldnames for n in names):
                raise TrainError("the CSV lacks the feature columns " + ", ".join(n for n in names if n not in (reader.fieldnames or [])))
            for row in reader:
                take([row[n] for n in names], row.get("label"), row.get("label_source"), row.get("created_at"), row.get("feature_hash"))
    if not xs:
        raise TrainError(f"{path.name} holds no usable labeled rows ({skipped})")
    y = np.array(ys, dtype=np.int8)
    prov = {"kind": "export", "file": path.name, "rows_used": len(y), "fraud_rows": int(y.sum()), "skipped": skipped,
            "label_sources": kinds, "real_features": names, "synthetic_features": [], "approximate_features": []}
    return np.array(xs, dtype=np.float32), y, np.array(ts, dtype=np.float64), prov


# ---- collecting real rows within a memory budget --------------------------------------------------------------------
class Collector:
    """Holds the rows of a streamed source within `cap` rows. When full it keeps every fraud row and halves the legitimate
    ones (and doubles their sample weight), so rates stay right: standard practice for data where fraud is a fraction of a
    percent. Rows are NOT shuffled, so timestamps keep their meaning."""

    def __init__(self, cap: int, rng: np.random.Generator):
        self.cap, self.rng, self.rate = cap, rng, 1.0
        self.x: list = []
        self.y: list = []
        self.ts: list = []
        self.w: list = []
        self.rows = 0
        self.seen = 0

    def add(self, x, y, ts) -> None:
        self.seen += len(y)
        w = np.ones(len(y))
        if self.rate < 1.0:
            keep = (y == 1) | (self.rng.random(len(y)) < self.rate)
            x, y, ts, w = x[keep], y[keep], ts[keep], np.where(y[keep] == 1, 1.0, 1.0 / self.rate)
        self.x.append(x)
        self.y.append(y)
        self.ts.append(ts)
        self.w.append(w)
        self.rows += len(y)
        while self.rows > self.cap and self._halve():
            pass

    def _halve(self) -> bool:
        if not any((y == 0).any() for y in self.y):
            return False
        self.rate /= 2
        for i in range(len(self.y)):
            keep = (self.y[i] == 1) | (self.rng.random(len(self.y[i])) < 0.5)
            self.w[i] = np.where(self.y[i] == 0, self.w[i] * 2.0, self.w[i])[keep]
            self.x[i], self.y[i], self.ts[i] = self.x[i][keep], self.y[i][keep], self.ts[i][keep]
        self.rows = sum(len(y) for y in self.y)
        return True

    def result(self):
        if not self.rows:
            raise TrainError("the source produced no rows")
        return np.concatenate(self.x), np.concatenate(self.y), np.concatenate(self.ts), np.concatenate(self.w)


# ---- the model ------------------------------------------------------------------------------------------------------
def flatten(node: dict, out: dict) -> int:
    """Depth-first flatten of one xgboost JSON tree into parallel arrays (index = node id in `out`); children always have a
    higher index than their parent, which `ml.validate` relies on."""
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


def export_trees(bst) -> list[dict]:
    trees = []
    for dump in bst.get_dump(dump_format="json", with_stats=True):
        out = {k: [] for k in ("f", "t", "l", "r", "v", "e", "cover")}
        flatten(json.loads(dump), out)
        out.pop("cover")
        trees.append(out)
    return trees


def _split(x, y, ts, w, frac: float, rng):
    """(train index, holdout index). Time-ordered by weight when timestamps exist, else stratified random."""
    if ts is not None and not np.isnan(ts).any():
        order = np.argsort(ts, kind="stable")
        cum = np.cumsum(w[order])
        cut = int(np.searchsorted(cum, (1 - frac) * cum[-1], side="right"))
        return order[:cut], order[cut:], "time-ordered: the newest rows are the holdout"
    train, hold = [], []
    for cls in (0, 1):
        idx = np.flatnonzero(y == cls)
        rng.shuffle(idx)
        k = int(len(idx) * (1 - frac))
        train.append(idx[:k])
        hold.append(idx[k:])
    return np.concatenate(train), np.concatenate(hold), "stratified random by label"


def _names():
    return [f"f{i}" for i in range(len(VECTOR_FIELDS))]


def train(args, log=print) -> dict:
    import xgboost as xgb

    t_start = time.perf_counter()
    rng = np.random.default_rng(args.seed)
    timing: dict[str, float] = {}
    parts: list[dict] = []          # each: name, x, y, w, ts|None, real_mask|None, provenance
    warnings: list[str] = []

    # -- real sources
    if args.from_export:
        t0 = time.perf_counter()
        x, y, ts, prov = load_export(args.from_export, tuple(args.label_sources))
        timing["read_export"] = round(time.perf_counter() - t0, 2)
        parts.append({"name": "export", "x": x, "y": y, "w": np.ones(len(y)), "ts": ts, "mask": None, "prov": prov})
        warnings.append("Export rows carry the scores and decisions of the model that was live: a model trained on them alone "
                        "learns to imitate its own past decisions (feedback loop) and sees outcomes only for payments that "
                        "were allowed through. Mix in synthetic or external data and review label sources before trusting it.")
        if prov["label_sources"].get("aging"):
            warnings.append("Some labels are 'aging' (no dispute after N days = legitimate): they cover only approved payments and "
                            "may include fraud that was not yet disputed.")
    if args.dataset:
        t0 = time.perf_counter()
        adapter = datasets.Adapter(args.dataset, args.path, seed=args.seed, chunk_rows=args.chunk_rows, fill=args.fill,
                                   max_rows=args.read_limit)
        col = Collector(args.max_real_rows, rng)
        for chunk in adapter.chunks():
            col.add(chunk.x, chunk.y, chunk.ts)
            if log and adapter.read and adapter.read % (args.chunk_rows * 20) < args.chunk_rows:
                log(f"  read {adapter.read:,} rows of {args.dataset} (kept {col.rows:,})")
        x, y, ts, w = col.result()
        prov = adapter.provenance() | {"rows_kept": int(len(y)), "negative_sample_rate": round(col.rate, 6)}
        timing["read_dataset"] = round(time.perf_counter() - t0, 2)
        parts.append({"name": f"dataset:{args.dataset}", "x": x, "y": y, "w": w, "ts": ts, "mask": adapter.real_mask, "prov": prov})
        if args.fill == "class":
            warnings.append(f"The columns {', '.join(prov['synthetic_features'])} were completed from the synthetic generator, "
                            "conditioned on the label: they make the model look better on this dataset than it would on real "
                            "fraud. See holdout 'real_features_only' for the score when they carry no signal.")
        if args.dataset == "paysim":
            warnings.append("PaySim's balance columns leak its label and were not used; its fraud is only TRANSFER and CASH_OUT.")

    # -- synthetic rows
    real_rows = sum(len(p["y"]) for p in parts)
    if args.synthetic_rows is not None and args.mix_synthetic is not None:
        raise TrainError("use either --synthetic-rows or --mix-synthetic, not both")
    if parts:
        if args.mix_synthetic is not None:
            if not 0 < args.mix_synthetic < 1:
                raise TrainError("--mix-synthetic is a fraction between 0 and 1 (the synthetic share of all rows)")
            n_syn = round(real_rows * args.mix_synthetic / (1 - args.mix_synthetic))
        else:
            n_syn = args.synthetic_rows or 0
    else:
        n_syn = DEFAULT_ROWS if args.synthetic_rows is None else args.synthetic_rows
    if n_syn:
        t0 = time.perf_counter()
        x, y = synth.generate(rng, n_syn)
        timing["generate_synthetic"] = round(time.perf_counter() - t0, 2)
        parts.append({"name": "synthetic", "x": x, "y": y, "w": np.ones(len(y)), "ts": None, "mask": None,
                      "prov": {"kind": "synthetic", "rows_used": n_syn, "fraud_rows": int(y.sum()), "generator": "paybroker.synth",
                               "class_mix": synth.MIX, "label_noise": synth.LABEL_NOISE, "real_features": [],
                               "synthetic_features": list(VECTOR_FIELDS), "approximate_features": []}})
        if log:
            log(f"generated {n_syn:,} synthetic rows in {timing['generate_synthetic']} s ({int(y.sum()):,} labelled fraud)")
    if not parts:
        raise TrainError("nothing to train on: give --synthetic-rows, --from-export or --dataset")
    if sum(float(p["w"].sum()) for p in parts) < MIN_ROWS:
        raise TrainError(f"too few rows to train and evaluate a model (fewer than {MIN_ROWS})")

    # -- split
    splits = []
    for p in parts:
        tr, ho, how = _split(p["x"], p["y"], p["ts"], p["w"], args.holdout, rng)
        if not len(ho) or not len(tr):
            raise TrainError(f"{p['name']}: too few rows for a {args.holdout:.0%} holdout")
        splits.append((p, tr, ho, how))
    x_train = np.concatenate([p["x"][tr] for p, tr, _, _ in splits])
    y_train = np.concatenate([p["y"][tr] for p, tr, _, _ in splits])
    w_train = np.concatenate([p["w"][tr] for p, tr, _, _ in splits])
    if y_train.min() == y_train.max():
        raise TrainError("the training rows contain only one class")

    # -- train
    names = _names()
    params = {**DEFAULT_PARAMS, "max_depth": args.max_depth, "eta": args.eta, "min_child_weight": args.min_child_weight,
              "lambda": args.reg_lambda, "seed": args.seed, "nthread": args.threads, "eval_metric": "aucpr"}
    t0 = time.perf_counter()
    dtrain = xgb.DMatrix(x_train, y_train, weight=w_train, feature_names=names)
    hold_sets = []
    for p, _, ho, _ in splits:
        hold_sets.append((p, ho, xgb.DMatrix(p["x"][ho], p["y"][ho], weight=p["w"][ho], feature_names=names)))
    bst = xgb.train(params, dtrain, num_boost_round=args.rounds, evals=[(hold_sets[-1][2], "holdout")],
                    verbose_eval=max(1, args.rounds // 4) if log else False)
    timing["train"] = round(time.perf_counter() - t0, 2)
    del dtrain

    # -- evaluate
    t0 = time.perf_counter()
    holdout, neutral_rng = {}, np.random.default_rng(args.seed + 1)
    xh, yh, wh, pred_all = [], [], [], []
    for (p, _, ho, how), (_, _, dho) in zip(splits, hold_sets, strict=True):
        pr = bst.predict(dho)
        y_ho, w_ho = p["y"][ho], p["w"][ho]
        holdout[p["name"]] = metrics(y_ho, pr, w_ho) | {"split": how, "score_distribution": score_distribution(y_ho, pr, w_ho)}
        if p["mask"] is not None:      # the same rows with every completed column replaced by ordinary-traffic values
            cols = [i for i in range(len(VECTOR_FIELDS)) if not p["mask"][i]]
            xn = p["x"][ho].copy()
            xn[:, cols] = synth.complete_columns(neutral_rng, np.zeros(len(ho), dtype=np.int8), cols)
            prn = bst.predict(xgb.DMatrix(xn, feature_names=names))
            holdout[p["name"] + ":real_features_only"] = metrics(y_ho, prn, w_ho) | {
                "note": "completed columns replaced by ordinary-traffic draws: what the real columns alone achieve",
                "real_features": list(p["prov"]["real_features"]) + list(p["prov"].get("approximate_features", []))}
        xh.append(p["x"][ho]); yh.append(y_ho); wh.append(w_ho); pred_all.append(pr)   # noqa: E702
    if len(splits) > 1:
        yy, ww, pp = np.concatenate(yh), np.concatenate(wh), np.concatenate(pred_all)
        holdout["all"] = metrics(yy, pp, ww) | {"score_distribution": score_distribution(yy, pp, ww)}
    timing["evaluate"] = round(time.perf_counter() - t0, 2)

    # -- export
    trees = export_trees(bst)
    today = date.today().isoformat()
    kinds = "+".join(p["name"].replace("dataset:", "") for p in parts)
    total_rows = sum(len(p["y"]) for p in parts)
    version = args.version or f"{kinds}-{total_rows // 1000}k-{today}"
    summary = "; ".join(_describe(p["prov"]) for p in parts)
    doc = {"version": version, "trained_on": {"summary": summary, "sources": [p["prov"] for p in parts]},
           "features": list(VECTOR_FIELDS), "base_margin": 0.0, "trees": trees,
           "metrics": {k: {m: v[m] for m in ("pr_auc", "roc_auc", "recall_at_1pct_fpr") if m in v}
                       for k, v in holdout.items()},
           "training": {"created_at": datetime.now(UTC).isoformat(timespec="seconds"), "paybroker": __version__, "rounds": args.rounds,
                        "params": {k: v for k, v in params.items() if k not in {"nthread"}}, "train_rows": int(len(y_train)),
                        "holdout_fraction": args.holdout}}
    # reference vectors: lets the tests prove the JSON walk reproduces xgboost exactly, without xgboost installed
    model = ml.Model({**doc, "reference": []})
    ref_x = np.concatenate([a[:40] for a in xh])[:80]
    ref_p = bst.predict(xgb.DMatrix(ref_x, feature_names=names))
    ref = [{"x": [float(v) for v in r], "score": int(round(s * 1000))} for r, s in zip(ref_x, ref_p, strict=True)]
    ref = [r for r in ref if model.score(r["x"], explain=False)[0] == r["score"]][:12]
    doc["reference"] = ref
    summary_info = ml.validate(doc)
    scen = scenarios.evaluate(ml.Model(doc))
    timing["total"] = round(time.perf_counter() - t_start, 2)
    out_path = Path(args.out)
    report = {"version": version, "created_at": doc["training"]["created_at"], "command": " ".join(sys.argv[1:]) or "(defaults)",
              "feature_hash": FEATURE_HASH, "timing_seconds": timing,
              "data": {"sources": [p["prov"] for p in parts], "rows_total": total_rows, "rows_train": int(len(y_train)),
                       "rows_holdout": int(sum(len(ho) for _, _, ho, _ in splits)),
                       "split": {p["name"]: how for p, _, _, how in splits}},
              "params": params, "rounds": args.rounds, "xgboost": xgb.__version__, "numpy": np.__version__,
              "model": {"file": out_path.name, **summary_info, "reference_vectors": len(ref)},
              "holdout": holdout, "scenarios": scen, "warnings": warnings}
    if not args.dry_run:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(doc, separators=(",", ":")))
        report["model"]["size_bytes"] = out_path.stat().st_size
        report_path = Path(args.report) if args.report else out_path.with_name("training_report.json")
        report_path.write_text(json.dumps(report, indent=2, default=float) + "\n")
    return report


def _describe(prov: dict) -> str:
    if prov["kind"] == "synthetic":
        return f"{prov['rows_used']:,} synthetic rows (paybroker.synth, invented attack patterns, label noise {prov['label_noise']:.0%})"
    if prov["kind"] == "export":
        return f"{prov['rows_used']:,} labeled rows captured from PayBroker ({prov['file']}; labels: {prov['label_sources']})"
    return (f"{prov['rows_kept']:,} rows of the {prov['name']} dataset ({prov['file']}); real features: "
            f"{', '.join(prov['real_features'] + prov['approximate_features'])}; completed from the synthetic generator ({prov['synthetic_fill']} fill): "
            f"{', '.join(prov['synthetic_features'])}")


def print_report(report: dict, out=print) -> None:
    t = report["timing_seconds"]
    out(f"rows: {report['data']['rows_train']:,} train / {report['data']['rows_holdout']:,} holdout; "
        f"time: {t.get('generate_synthetic', 0)} s generate, {t['train']} s train, {t['total']} s total")
    for name, m in report["holdout"].items():
        if m.get("pr_auc") is None:
            out(f"  holdout {name}: {m.get('note', 'n/a')}")
            continue
        out(f"  holdout {name}: PR-AUC {m['pr_auc']}  ROC-AUC {m['roc_auc']}  recall@1%FPR {m['recall_at_1pct_fpr']}  fraud rate {m['fraud_rate']}")
        bands = m.get("score_distribution", {}).get("bands")
        if bands:
            out("    bands: " + ", ".join(f"{b} {v['share']:.1%} (fraud inside {v['fraud_rate']})" for b, v in bands.items()))
    out("scenarios:")
    for s in report["scenarios"]:
        out(f"  {s['score']:5d} {'ok ' if s['ok'] else 'BAD'} {s['scenario']}")
    for w in report["warnings"]:
        out(f"WARNING: {w}")
    m = report["model"]
    out(f"model: {m['trees']} trees, depth {m['max_depth']}, {m.get('size_bytes', 0) // 1024} KB")


def parser() -> argparse.ArgumentParser:
    default_out = Path(ml.MODEL_PATH)
    p = argparse.ArgumentParser(prog="train_model.py", description="Train the Tier 2 fraud model and export it as JSON trees.")
    g = p.add_argument_group("data")
    g.add_argument("--synthetic-rows", type=int, default=None, metavar="N",
                   help=f"synthetic rows to generate (default {DEFAULT_ROWS:,}; with a real source: default 0)")
    g.add_argument("--mix-synthetic", type=float, default=None, metavar="FRACTION",
                   help="with a real source: the synthetic share of all rows, e.g. 0.3 (instead of --synthetic-rows)")
    g.add_argument("--from-export", metavar="FILE", help="an export of captured training data (.csv or .ndjson), labeled rows only")
    g.add_argument("--label-sources", default=",".join(SOURCES), help="export label sources to keep (default: chargeback,case,aging)")
    g.add_argument("--dataset", choices=sorted(datasets.SPECS), help="a public dataset to read through its adapter")
    g.add_argument("--path", metavar="FILE", help="the dataset file (.csv or .csv.gz); required with --dataset")
    g.add_argument("--fill", choices=("class", "neutral"), default="class",
                   help="how to complete features a dataset lacks: from the label's class (default) or ordinary traffic")
    g.add_argument("--max-real-rows", type=int, default=MAX_REAL_ROWS,
                   help=f"memory cap on dataset rows kept; legitimate rows are down-sampled and weighted (default {MAX_REAL_ROWS:,})")
    g.add_argument("--read-limit", type=int, default=None, metavar="N", help="read only the first N rows of the dataset (for trials)")
    g.add_argument("--chunk-rows", type=int, default=datasets.CHUNK_ROWS)
    m = p.add_argument_group("model")
    m.add_argument("--rounds", type=int, default=70)
    m.add_argument("--max-depth", type=int, default=3)
    m.add_argument("--eta", type=float, default=0.1)
    m.add_argument("--min-child-weight", type=float, default=15)
    m.add_argument("--reg-lambda", type=float, default=5)
    m.add_argument("--holdout", type=float, default=0.2, help="holdout fraction (default 0.2)")
    m.add_argument("--seed", type=int, default=20261022)
    m.add_argument("--threads", type=int, default=4)
    o = p.add_argument_group("output")
    o.add_argument("--out", default=str(default_out), help="model file to write (default: the bundled model)")
    o.add_argument("--report", default=None, help="report file (default: training_report.json next to the model)")
    o.add_argument("--version", default=None, help="model version name (default: sources, rows and date)")
    o.add_argument("--dry-run", action="store_true", help="train and report, write nothing")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    args.label_sources = [s.strip() for s in args.label_sources.split(",") if s.strip()]
    if bad := [s for s in args.label_sources if s not in SOURCES]:
        raise SystemExit(f"unknown label source(s): {', '.join(bad)} (choose from {', '.join(SOURCES)})")
    if bool(args.dataset) != bool(args.path):
        raise SystemExit("--dataset and --path go together")
    if not 0.05 <= args.holdout <= 0.5:
        raise SystemExit("--holdout must be between 0.05 and 0.5")
    try:
        report = train(args)
    except (TrainError, datasets.DatasetError) as exc:
        raise SystemExit(f"error: {exc}") from exc
    print_report(report)
    if not args.dry_run:
        print(f"wrote {args.out}")
    return 0
