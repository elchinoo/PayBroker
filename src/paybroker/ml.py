"""Tier 2: real-time scoring with a gradient-boosted tree model (target under 35 ms; in practice a fraction of one).

The reference design exports XGBoost to ONNX Runtime. This sample exports the same trees to a small JSON file and walks them
in pure Python: no native dependency, nothing to deploy beside the app, and it also yields per-feature contributions for
explanations. The model in `models/fraud_gbdt.json` is trained on SYNTHETIC data by `scripts/train_model.py`; it shows the
mechanics and is not a model of real fraud.

Which model scores: an operator can upload a model (same JSON format, validated by `validate`) and activate it without
rebuilding the image; `current(conn)` returns the active uploaded model, re-checked at most every `REFRESH_SECONDS` so every
instance follows within about 30 s, and falls back to the bundled file when none is active. Rolling back is activating the
previous version.
"""
import json
import math
import threading
import time
from functools import lru_cache
from pathlib import Path

from .features import VECTOR_FIELDS

MODEL_PATH = Path(__file__).resolve().parent / "models" / "fraud_gbdt.json"
# Which feature moving the score maps to which reason code shown to the merchant and the analyst.
REASONS = {
    "amount_log": "LARGE_AMOUNT", "amount_vs_avg_ratio": "AMOUNT_ANOMALY", "user_tx_1h": "HIGH_VELOCITY_USER",
    "user_tx_24h": "HIGH_VELOCITY_USER", "user_sum_24h_log": "HIGH_SPEND_24H", "distinct_cards_7d": "MANY_CARDS_ON_ACCOUNT",
    "ip_tx_1h": "HIGH_VELOCITY_IP", "device_tx_1h": "HIGH_VELOCITY_DEVICE", "card_tx_1h": "HIGH_VELOCITY_CARD",
    "chargebacks": "PRIOR_CHARGEBACKS", "device_linked_chargebacks": "DEVICE_LINKED_TO_CHARGEBACK",
    "impossible_travel": "IMPOSSIBLE_TRAVEL_DETECTED", "new_device_for_account": "NEW_DEVICE_FOR_ACCOUNT",
    "checkout_seconds_log": "FAST_CHECKOUT", "pasted_card": "PASTED_CARD_NUMBER", "country_mismatch": "IP_COUNTRY_MISMATCH",
    "account_age_hours_log": "NEW_ACCOUNT",
}

# Features whose reason code means a LOW value (a new account, a fast checkout); every other feature is risky when high.
LOW_IS_RISKY = frozenset({"checkout_seconds_log", "account_age_hours_log"})

# Limits for an uploaded model. They bound the work of one score (trees x depth) so a model cannot slow authorization.
MAX_MODEL_BYTES = 2 * 1024 * 1024
MAX_TREES = 500
MAX_DEPTH = 8
MAX_NODES_PER_TREE = 2 ** (MAX_DEPTH + 1) - 1
MAX_ABS_VALUE = 1e4
REFRESH_SECONDS = 30.0
TREE_KEYS = ("f", "t", "l", "r", "v", "e")


class ModelError(ValueError):
    """An uploaded model that is malformed, too large or built on different features."""


def _number(v, what: str, *, bound: float = MAX_ABS_VALUE) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or abs(v) > bound:
        raise ModelError(f"{what} must be a finite number no larger than {bound:g}")
    return float(v)


def _index(v, what: str, low: int, high: int) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not low <= v <= high:
        raise ModelError(f"{what} must be an integer between {low} and {high}")
    return v


def validate(doc) -> dict:
    """Check a model document before it is stored or used. Raises ModelError; returns a small summary.

    Beyond the feature list this guarantees that walking every tree terminates (a child always has a higher index than its
    parent), that each node has one parent, that thresholds and values are finite, and that one score is bounded work
    (at most MAX_TREES trees of depth MAX_DEPTH). Extra keys (metrics, notes, reference vectors) are allowed."""
    if not isinstance(doc, dict):
        raise ModelError("the model must be a JSON object")
    if doc.get("features") != list(VECTOR_FIELDS):
        raise ModelError("the model was trained on different features than this version of the code "
                         f"(expected exactly: {', '.join(VECTOR_FIELDS)})")
    _number(doc.get("base_margin"), "base_margin")
    trees = doc.get("trees")
    if not isinstance(trees, list) or not 1 <= len(trees) <= MAX_TREES:
        raise ModelError(f"trees must be a list of 1 to {MAX_TREES} trees")
    n_features, max_depth, nodes = len(VECTOR_FIELDS), 0, 0
    for ti, tree in enumerate(trees):
        where = f"tree {ti}"
        if not isinstance(tree, dict) or any(not isinstance(tree.get(k), list) for k in TREE_KEYS):
            raise ModelError(f"{where} must hold the lists {', '.join(TREE_KEYS)}")
        size = len(tree["f"])
        if not 1 <= size <= MAX_NODES_PER_TREE or any(len(tree[k]) != size for k in TREE_KEYS):
            raise ModelError(f"{where}: the node lists must have equal length between 1 and {MAX_NODES_PER_TREE}")
        parents = [0] * size
        for n in range(size):
            left, right = tree["l"][n], tree["r"][n]
            _number(tree["v"][n], f"{where} node {n} value")
            _number(tree["e"][n], f"{where} node {n} expected value")
            if left == -1 and right == -1:
                continue
            _index(tree["f"][n], f"{where} node {n} feature", 0, n_features - 1)
            _number(tree["t"][n], f"{where} node {n} threshold", bound=1e12)
            for child in (left, right):
                _index(child, f"{where} node {n} child", n + 1, size - 1)
                parents[child] += 1
        if size > 1 and any(c != 1 for c in parents[1:]) or parents[0] != 0:
            raise ModelError(f"{where}: every node except the root needs exactly one parent")
        depth = [0] * size
        for n in range(size):
            if tree["l"][n] != -1:
                depth[tree["l"][n]] = depth[tree["r"][n]] = depth[n] + 1
        if max(depth) > MAX_DEPTH:
            raise ModelError(f"{where} is deeper than {MAX_DEPTH} levels")
        max_depth, nodes = max(max_depth, max(depth)), nodes + size
    version = doc.get("version")
    if version is not None and not isinstance(version, str):
        raise ModelError("version must be a string")
    return {"trees": len(trees), "nodes": nodes, "max_depth": max_depth}


def parse(raw: bytes | str) -> dict:
    """JSON text to a validated model document (NaN and Infinity are refused, not just out-of-range numbers)."""
    def refuse(token):
        raise ModelError(f"{token} is not allowed in a model")
    if len(raw) > MAX_MODEL_BYTES:
        raise ModelError(f"the model is larger than {MAX_MODEL_BYTES // 1024} KB")
    try:
        doc = json.loads(raw, parse_constant=refuse)
    except (ValueError, RecursionError) as exc:
        if isinstance(exc, ModelError):
            raise
        raise ModelError("the model is not valid JSON") from exc
    validate(doc)
    return doc


class Model:
    def __init__(self, doc: dict):
        if doc.get("features") != list(VECTOR_FIELDS):
            raise ValueError("the model was trained on different features than this version of the code")
        self.base_margin = float(doc["base_margin"])
        self.trees = doc["trees"]
        self.version = str(doc.get("version", "unknown"))
        self.trained_on = doc.get("trained_on", "")      # a sentence, or a dict naming the sources and which features were real
        self.names = doc["features"]
        self._risk = self._risk_sides()

    def _risk_sides(self) -> list[tuple[bool, float] | None]:
        """For each feature, where the values that justify its reason code lie: (True, t) for values >= t, (False, t) for values < t.

        Which way a feature is risky comes from what its reason code says (LOW_IS_RISKY: a new account, a fast checkout; every
        other one is risky when high). t is the nearest threshold among the model's splits on the feature that send that side to
        the higher expected margin: the lowest for "high", the highest for "low". The trees are fitted on noise and also hold
        splits that rank the other way, which is why a direction taken from the model alone is not trusted. When the model has no
        such split the feature can never be a reason (t is infinite); None when it never splits on the feature at all."""
        splits: list[list[tuple[float, float]]] = [[] for _ in self.names]      # per feature: (threshold, ev(right) - ev(left))
        for t in self.trees:
            for n, left in enumerate(t["l"]):
                if left != -1:
                    splits[t["f"][n]].append((t["t"][n], t["e"][t["r"][n]] - t["e"][left]))
        out: list[tuple[bool, float] | None] = []
        for name, rows in zip(self.names, splits, strict=True):
            if not rows:
                out.append(None)
            elif name in LOW_IS_RISKY:
                out.append((False, max((thr for thr, d in rows if d < 0), default=-math.inf)))
            else:
                out.append((True, min((thr for thr, d in rows if d > 0), default=math.inf)))
        return out

    def _on_risk_side(self, i: int, value: float) -> bool:
        side = self._risk[i]
        return side is not None and (value >= side[1] if side[0] else value < side[1])

    def score(self, x: list[float], explain: bool = True) -> tuple[int, list[tuple[str, float]]]:
        margin = self.base_margin
        contrib = [0.0] * len(x)
        for t in self.trees:
            feat, thr, left, right, val, ev = t["f"], t["t"], t["l"], t["r"], t["v"], t["e"]
            n = 0
            while left[n] != -1:
                nxt = left[n] if x[feat[n]] < thr[n] else right[n]
                if explain:
                    contrib[feat[n]] += ev[nxt] - ev[n]
                n = nxt
            margin += val[n]
        p = 1.0 / (1.0 + math.exp(-max(-50.0, min(50.0, margin))))
        score = max(0, min(1000, round(p * 1000)))
        if not explain:
            return score, []
        ranked = sorted(((i, c) for i, c in enumerate(contrib)), key=lambda kv: -kv[1])
        # A reason needs a real push (attribution above 0.05) AND a value on the risky side of the model's splits for that feature:
        # a new customer with 0 cards still gets a small positive attribution from the splits at 3, 4 and 5, but is no risk.
        return score, [(self.names[i], c) for i, c in ranked if c > 0.05 and self._on_risk_side(i, x[i])]


@lru_cache(maxsize=1)
def load(path: str | None = None) -> Model:
    """The bundled model (or the file at `path`)."""
    return Model(json.loads(Path(path or MODEL_PATH).read_text()))


# ---- the active model: uploaded and activated by an operator, else the bundled one -----------------------------------
_lock = threading.Lock()
_active: dict = {"model": None, "id": None, "sha": None, "checked": 0.0}


def invalidate() -> None:
    """Forget the cached choice (called after an activation in this process, and by tests)."""
    with _lock:
        _active.update(model=None, id=None, sha=None, checked=0.0)


def current(conn=None) -> Model:
    """The model that scores payments. With a connection, the active uploaded model is looked up at most every 30 s; a failed
    lookup keeps what was cached, so a database hiccup never changes the model in use or breaks authorization."""
    if conn is not None and time.monotonic() - _active["checked"] >= REFRESH_SECONDS:
        refresh(conn)
    return _active["model"] or load()


def refresh(conn) -> None:
    try:
        with conn.transaction():          # a savepoint: a failure here must not abort the payment's transaction
            row = conn.execute("SELECT id, sha256 FROM model_version WHERE active").fetchone()
            doc = None
            if row and (row["id"], row["sha256"]) != (_active["id"], _active["sha"]):
                doc = conn.execute("SELECT doc FROM model_version WHERE id=%s", (row["id"],)).fetchone()["doc"]
        with _lock:
            _active["checked"] = time.monotonic()
            if not row:
                _active.update(model=None, id=None, sha=None)
            elif doc is not None:
                validate(doc)                   # defence in depth: the row could have been written outside the API
                _active.update(model=Model(doc), id=row["id"], sha=row["sha256"])
    except Exception:  # noqa: BLE001 - keep serving the model already in use
        with _lock:
            _active["checked"] = time.monotonic()


def active_id() -> int | None:
    return _active["id"]


def reasons_from(contribs: list[tuple[str, float]], limit: int = 4) -> list[str]:
    out: list[str] = []
    for name, _ in contribs:
        code = REASONS.get(name)
        if code and code not in out:
            out.append(code)
        if len(out) == limit:
            break
    return out
