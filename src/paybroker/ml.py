"""Tier 2: real-time scoring with a gradient-boosted tree model (target under 35 ms; in practice a fraction of one).

The reference design exports XGBoost to ONNX Runtime. This sample exports the same trees to a small JSON file and walks them
in pure Python: no native dependency, nothing to deploy beside the app, and it also yields per-feature contributions for
explanations. The model in `models/fraud_gbdt.json` is trained on SYNTHETIC data by `scripts/train_model.py`; it shows the
mechanics and is not a model of real fraud. Replace the file (same format) to use your own.
"""
import json
import math
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


class Model:
    def __init__(self, doc: dict):
        if doc.get("features") != list(VECTOR_FIELDS):
            raise ValueError("the model was trained on different features than this version of the code")
        self.base_margin = float(doc["base_margin"])
        self.trees = doc["trees"]
        self.version = doc.get("version", "unknown")
        self.trained_on = doc.get("trained_on", "")
        self.names = doc["features"]

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
        p = 1.0 / (1.0 + math.exp(-margin))
        score = max(0, min(1000, round(p * 1000)))
        if not explain:
            return score, []
        ranked = sorted(((self.names[i], c) for i, c in enumerate(contrib)), key=lambda kv: -kv[1])
        return score, [(n, c) for n, c in ranked if c > 0.05]


@lru_cache(maxsize=1)
def load(path: str | None = None) -> Model:
    return Model(json.loads(Path(path or MODEL_PATH).read_text()))


def reasons_from(contribs: list[tuple[str, float]], limit: int = 4) -> list[str]:
    out: list[str] = []
    for name, _ in contribs:
        code = REASONS.get(name)
        if code and code not in out:
            out.append(code)
        if len(out) == limit:
            break
    return out
