"""Synthetic training data for the Tier 2 model (needs numpy: `uv pip install -e ".[train]"`).

The generator invents ordinary customers next to the attack patterns the broker is meant to catch (account takeover, card
testing, synthetic identities, chargeback-linked devices), with label noise. It exists so the sample has a model to run and so
that real datasets, which lack some of the 17 features, can be completed (see `datasets.py`); it says nothing about real
fraud. Everything is vectorised and written chunk by chunk into float32 arrays, so a million rows take seconds and about
70 MB.

Ordinary traffic includes two kinds a naive generator forgets and a model then punishes:
* the busy shopper: one customer paying several times in an hour, so the user, card, IP and device counts rise TOGETHER with
  an otherwise ordinary checkout (a busy day, a basket split in two, a retry after a decline);
* the shared network: an office or mobile carrier putting many different customers behind one IP address.
Without them the model learns that any velocity is fraud.
"""
import math

import numpy as np

from .features import VECTOR_FIELDS

IDX = {name: i for i, name in enumerate(VECTOR_FIELDS)}
LEGIT_KINDS = ("legit", "busy_shopper", "shared_network", "benign_anomaly")
FRAUD_KINDS = ("ato", "card_testing", "synthetic_identity", "chargeback_link")
# Shares of the total. The original mix (60000 / 5000 / 1500 / 2000 / 1000 / 900 of 70400 rows) is kept: 7.7 % fraud, and the
# ordinary share is split into plain, busy-shopper and shared-network traffic.
MIX = {
    "legit": 0.7823, "busy_shopper": 0.0500, "shared_network": 0.0200, "benign_anomaly": 0.0710,
    "ato": 0.0213, "card_testing": 0.0284, "synthetic_identity": 0.0142, "chargeback_link": 0.0128,
}
LABEL_NOISE = 0.02
MILD_SHARE = 0.4
CHUNK = 250_000


def _lognormal(rng, median: float, sigma: float, n: int) -> np.ndarray:
    return rng.lognormal(math.log(median), sigma, n)


def rows(rng: np.random.Generator, n: int, kind: str) -> np.ndarray:
    """n feature rows of one kind, shape (n, 17), float32."""
    d = {k: np.zeros(n) for k in VECTOR_FIELDS}
    f = rng.random

    def amount(median, sigma):
        return np.log1p(_lognormal(rng, median, sigma, n))

    d["checkout_seconds_log"] = np.log1p(_lognormal(rng, 60, 0.6, n))
    d["account_age_hours_log"] = np.log1p(rng.exponential(2500, n) + 30)
    d["amount_log"] = amount(45, 0.9)
    d["amount_vs_avg_ratio"] = np.clip(rng.lognormal(0, 0.45, n), 0.1, 20)
    d["user_tx_1h"] = rng.poisson(0.25, n)
    d["user_tx_24h"] = d["user_tx_1h"] + rng.poisson(0.6, n)
    d["distinct_cards_7d"] = np.maximum(1, rng.poisson(0.15, n) + 1)
    d["ip_tx_1h"] = rng.poisson(0.3, n)
    d["device_tx_1h"] = rng.poisson(0.3, n)
    d["card_tx_1h"] = rng.poisson(0.2, n)
    d["user_sum_24h_log"] = np.log1p(d["user_tx_24h"] * _lognormal(rng, 45, 0.8, n))
    d["pasted_card"] = (f(n) < 0.03).astype(float)
    d["country_mismatch"] = (f(n) < 0.03).astype(float)
    d["new_device_for_account"] = (f(n) < 0.04).astype(float)
    if kind == "busy_shopper":
        # k earlier payments in the hour by the same customer on the same card, IP and device (0 to ~12, mostly 1 to 5)
        k = np.minimum(rng.poisson(2.4, n) + (f(n) < 0.35), 14).astype(float)
        d["user_tx_1h"] = k
        d["card_tx_1h"] = np.maximum(0, k - (f(n) < 0.12))
        d["ip_tx_1h"] = k + rng.poisson(0.2, n)
        d["device_tx_1h"] = np.maximum(0, k - (f(n) < 0.05))
        d["user_tx_24h"] = k + rng.poisson(1.5, n)
        d["user_sum_24h_log"] = np.log1p(d["user_tx_24h"] * _lognormal(rng, 45, 0.7, n))
        d["checkout_seconds_log"] = np.log1p(_lognormal(rng, 45, 0.55, n))     # a returning customer is a little quicker
        d["amount_vs_avg_ratio"] = np.clip(rng.lognormal(0, 0.5, n), 0.1, 20)
    elif kind == "shared_network":
        d["ip_tx_1h"] = rng.poisson(12, n) + 2           # many different customers behind one address; each one ordinary
    elif kind == "benign_anomaly":
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
        d["checkout_seconds_log"] = np.log1p(_lognormal(rng, 14, 0.4, n))
        d["pasted_card"] = (f(n) < 0.4).astype(float)
        d["user_tx_1h"] = rng.poisson(1.2, n)
    elif kind == "card_testing":
        d["amount_log"] = amount(2.5, 0.5)
        d["ip_tx_1h"] = rng.poisson(14, n) + 3
        d["card_tx_1h"] = rng.poisson(3, n) + 1
        d["user_tx_1h"] = rng.poisson(6, n) + 1
        d["user_tx_24h"] = d["user_tx_1h"] + rng.poisson(4, n)
        d["distinct_cards_7d"] = rng.poisson(5, n) + 2
        d["checkout_seconds_log"] = np.log1p(_lognormal(rng, 3, 0.5, n))
        d["pasted_card"] = (f(n) < 0.9).astype(float)
        d["account_age_hours_log"] = np.log1p(rng.exponential(30, n))
        d["device_tx_1h"] = rng.poisson(8, n)
    elif kind == "synthetic_identity":
        d["account_age_hours_log"] = np.log1p(rng.exponential(12, n))
        d["amount_log"] = amount(900, 0.6)
        d["distinct_cards_7d"] = rng.poisson(2.5, n) + 2
        d["device_linked_chargebacks"] = rng.poisson(1.0, n)
        d["country_mismatch"] = (f(n) < 0.45).astype(float)
        d["checkout_seconds_log"] = np.log1p(_lognormal(rng, 20, 0.4, n))
    elif kind == "chargeback_link":
        d["device_linked_chargebacks"] = rng.poisson(2, n) + 1
        d["chargebacks"] = rng.poisson(0.8, n)
        d["amount_log"] = amount(250, 0.8)
        d["new_device_for_account"] = (f(n) < 0.3).astype(float)
    elif kind != "legit":
        raise ValueError(f"unknown kind {kind!r}")
    return np.column_stack([d[k] for k in VECTOR_FIELDS]).astype(np.float32)


def mild(rng: np.random.Generator, attack: np.ndarray, share: float = MILD_SHARE) -> np.ndarray:
    """Real attacks are not all loud: blend a share of them back towards ordinary behaviour so scores spread out and the
    middle (3-D Secure) band has something to catch."""
    ordinary = rows(rng, len(attack), "legit")
    pick = rng.random(attack.shape) < 0.55
    out = np.where(pick, ordinary, attack)
    keep_loud = rng.random(len(attack)) > share
    return np.where(keep_loud[:, None], attack, out).astype(np.float32)


def kind_rows(rng: np.random.Generator, n: int, kind: str) -> np.ndarray:
    """Rows of one kind as the training set sees them (attacks softened)."""
    return mild(rng, rows(rng, n, kind)) if kind in FRAUD_KINDS else rows(rng, n, kind)


def counts(total: int) -> dict[str, int]:
    """Rows per kind for a total, largest remainder so the parts add up exactly."""
    weight = sum(MIX.values())
    raw = {k: MIX[k] / weight * total for k in MIX}
    out = {k: int(v) for k, v in raw.items()}
    for k in sorted(raw, key=lambda k: raw[k] - out[k], reverse=True)[: total - sum(out.values())]:
        out[k] += 1
    return out


def generate(rng: np.random.Generator, total: int, chunk: int = CHUNK) -> tuple[np.ndarray, np.ndarray]:
    """`total` labelled rows in random order: (x float32 [total, 17], y int8 [total]).

    Memory: one float32 matrix of the final size plus a chunk of float64 scratch (the kinds are generated `chunk` rows at a
    time straight into it); the shuffle temporarily needs one more copy of x (about 70 MB per million rows)."""
    x = np.empty((total, len(VECTOR_FIELDS)), dtype=np.float32)
    y = np.empty(total, dtype=np.int8)
    pos = 0
    for kind, n in counts(total).items():
        done = 0
        while done < n:
            m = min(chunk, n - done)
            x[pos:pos + m] = kind_rows(rng, m, kind)
            y[pos:pos + m] = 1 if kind in FRAUD_KINDS else 0
            pos += m
            done += m
    assert pos == total, "the class mix must cover every row"
    order = rng.permutation(total)
    x[:] = x[order]
    y[:] = y[order]
    flip = rng.random(total) < LABEL_NOISE                      # label noise
    return x, np.where(flip, 1 - y, y).astype(np.int8)


def complete_columns(rng: np.random.Generator, y: np.ndarray, columns: list[int]) -> np.ndarray:
    """Values for the feature `columns` a real dataset cannot provide, drawn from the generator's distributions for each row's
    class: legitimate rows from ordinary traffic, fraud rows from the attack patterns. NOTE this conditions the filled columns
    on the label, so a model fits them easily: metrics on completed data flatter the model (see the training report)."""
    n = len(y)
    out = np.empty((n, len(columns)), dtype=np.float32)
    legit_idx, fraud_idx = np.flatnonzero(y == 0), np.flatnonzero(y == 1)
    if len(legit_idx):
        out[legit_idx] = _draw(rng, len(legit_idx), LEGIT_KINDS[:3] + ("benign_anomaly",), (0.9, 0.04, 0.02, 0.04))[:, columns]
    if len(fraud_idx):
        share = np.array([MIX[k] for k in FRAUD_KINDS])
        out[fraud_idx] = _draw(rng, len(fraud_idx), FRAUD_KINDS, tuple(share / share.sum()))[:, columns]
    return out


def _draw(rng, n: int, kinds, weights) -> np.ndarray:
    pick = rng.choice(len(kinds), size=n, p=np.array(weights) / np.sum(weights))
    out = np.empty((n, len(VECTOR_FIELDS)), dtype=np.float32)
    for i, kind in enumerate(kinds):
        sel = np.flatnonzero(pick == i)
        if len(sel):
            out[sel] = kind_rows(rng, len(sel), kind)
    return out
