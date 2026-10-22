"""Adapters that turn public fraud datasets into PayBroker's 17 features, streamed in chunks (needs numpy).

    python scripts/train_model.py --dataset tabformer --path card_transaction.v1.csv

An adapter reads the file in chunks of `chunk_rows` records and keeps only small per-entity state (the last 30 days of one
user's payments), never the file: the 24 million rows of IBM TabFormer pass through in constant memory (the training script
then keeps a bounded, negative-downsampled sample; see trainer.py). The files are NOT included or downloaded by this project.

What each dataset can and cannot give (column names are the ones its documentation lists; the adapters read columns by
header name, so their order does not matter, and ignore every column not named below):

TabFormer (IBM "credit card transactions", ~24 M rows)
    User, Card, Year, Month, Day, Time, Amount, Use Chip, Merchant Name, Merchant City, Merchant State, Zip, MCC, Errors?, Is Fraud?
    Used: User, Card, Year, Month, Day, Time (HH:MM), Amount ("$134.09"; negative amounts are refunds and are skipped),
    Merchant City, Merchant State, Use Chip (only to tell online from in-person), Is Fraud? ("Yes"/"No").
    Real: amount_log, amount_vs_avg_ratio, user_tx_1h, user_tx_24h, user_sum_24h_log, distinct_cards_7d, card_tx_1h.
    Approximate: impossible_travel (two in-person payments within 2 h in different Merchant State and City: there are no
    coordinates in this file). Everything else is completed from the generator. The file is ordered by user, then time.

Sparkov (Kaggle "Credit Card Transactions Fraud Detection", ~1.3 M rows in fraudTrain.csv; also fraudTest.csv)
    trans_date_trans_time, cc_num, merchant, category, amt, first, last, gender, street, city, state, zip, lat, long, city_pop,
    job, dob, trans_num, unix_time, merch_lat, merch_long, is_fraud (plus an unnamed row-number column)
    Used: unix_time (else trans_date_trans_time), cc_num, amt, merch_lat, merch_long, is_fraud. Names, street, dob and job are
    never read. Real: amount_log, amount_vs_avg_ratio, user_tx_1h, user_tx_24h, user_sum_24h_log, card_tx_1h,
    distinct_cards_7d (one card per customer, so always 1), impossible_travel (merchant locations of consecutive payments
    imply more than 900 km/h).

PaySim (Kaggle synthetic mobile-money data, ~6.3 M rows)
    step, type, amount, nameOrig, oldbalanceOrg, newbalanceOrig, nameDest, oldbalanceDest, newbalanceDest, isFraud, isFlaggedFraud
    Used: step (one hour each), amount, nameOrig, isFraud. The balance columns are NEVER read: PaySim's fraud is a transfer
    that empties the origin account, so oldbalanceOrg, newbalanceOrg, oldbalanceDest and newbalanceDest give the label away
    (and isFlaggedFraud is a rule's output); a model fed them looks superb and learns nothing usable. `type` is not one of the
    17 features. Real: amount_log, amount_vs_avg_ratio, user_tx_1h, user_tx_24h, user_sum_24h_log. nameOrig rarely repeats, so
    most velocity values are 0; the dataset is weak evidence about velocity. Fraud happens only in TRANSFER and CASH_OUT.

Features a dataset cannot provide (IP and device velocity, checkout time, pasted card, country mismatch, chargebacks, new
device, account age) are drawn from the synthetic generator for the row's class (`--fill class`, the default), or from ordinary
traffic for every row (`--fill neutral`). Class-conditioned fills make those columns informative by construction, so a model
trained on completed data scores better than it would on real fraud; the training report therefore also scores the holdout
with the completed columns replaced by neutral draws ("real_features_only").
"""
import csv
import gzip
import math
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from . import synth
from .features import VECTOR_FIELDS

IDX = {n: i for i, n in enumerate(VECTOR_FIELDS)}
H1, H24, D7, D30 = 3600, 86400, 7 * 86400, 30 * 86400
TRAVEL_WINDOW = 7200
TRAVEL_KMH = 900.0
MAX_ENTITIES = 2_000_000
CHUNK_ROWS = 100_000


class DatasetError(ValueError):
    pass


@dataclass
class Spec:
    name: str
    required: tuple[str, ...]
    optional: tuple[str, ...]
    real: tuple[str, ...]
    approximate: tuple[str, ...] = ()
    never_read: tuple[str, ...] = ()
    notes: str = ""

    def synthetic(self) -> list[str]:
        have = set(self.real) | set(self.approximate)
        return [n for n in VECTOR_FIELDS if n not in have]


SPECS = {
    "tabformer": Spec(
        "tabformer", ("User", "Card", "Year", "Month", "Day", "Time", "Amount", "Is Fraud?"),
        ("Use Chip", "Merchant City", "Merchant State"),
        real=("amount_log", "amount_vs_avg_ratio", "user_tx_1h", "user_tx_24h", "user_sum_24h_log", "distinct_cards_7d", "card_tx_1h"),
        approximate=("impossible_travel",),
        notes="negative amounts (refunds) are skipped; travel is two in-person payments within 2 h in different state and city"),
    "sparkov": Spec(
        "sparkov", ("cc_num", "amt", "is_fraud"), ("unix_time", "trans_date_trans_time", "merch_lat", "merch_long"),
        real=("amount_log", "amount_vs_avg_ratio", "user_tx_1h", "user_tx_24h", "user_sum_24h_log", "card_tx_1h", "distinct_cards_7d",
              "impossible_travel"),
        never_read=("first", "last", "street", "dob", "job", "gender", "trans_num"),
        notes="needs unix_time or trans_date_trans_time; impossible_travel needs merch_lat and merch_long"),
    "paysim": Spec(
        "paysim", ("step", "amount", "nameOrig", "isFraud"), (),
        real=("amount_log", "amount_vs_avg_ratio", "user_tx_1h", "user_tx_24h", "user_sum_24h_log"),
        never_read=("oldbalanceOrg", "newbalanceOrig", "oldbalanceDest", "newbalanceDest", "isFlaggedFraud", "type", "nameDest"),
        notes="balance columns leak the label and are never read"),
}


@dataclass
class Chunk:
    x: np.ndarray        # float32 (n, 17), every column filled
    y: np.ndarray        # int8
    ts: np.ndarray       # float64 epoch seconds (a PaySim step is one hour after the epoch)
    real_mask: np.ndarray = field(default=None)   # bool (17,): True where the column comes from the dataset (incl. approximate)


class _State:
    """The last 30 days of one entity's payments, newest at the right."""
    __slots__ = ("events", "sum30", "last_ts", "geo")

    def __init__(self):
        self.events: deque = deque()          # (ts, amount, card)
        self.sum30 = 0.0
        self.last_ts = -math.inf
        self.geo = None                       # TabFormer: (ts, state, city); Sparkov: (ts, lat, lon)


class Windows:
    """Rolling per-entity aggregates for a stream of payments, matching the broker's features: counts and sums of the PRIOR
    payments in the last hour and 24 hours, distinct cards in 7 days and the average amount over 30 days. Records of one entity
    should arrive in time order; the occasional late record is counted in `out_of_order` and treated as if it were on time.
    At most `max_entities` entities are tracked (the oldest-created is dropped first, counted in `evicted`)."""

    def __init__(self, max_entities: int = MAX_ENTITIES):
        self.state: dict = {}
        self.max_entities = max_entities
        self.out_of_order = 0
        self.evicted = 0

    def _get(self, key) -> _State:
        st = self.state.get(key)
        if st is None:
            if len(self.state) >= self.max_entities:
                self.state.pop(next(iter(self.state)))
                self.evicted += 1
            st = self.state[key] = _State()
        return st

    def observe(self, key, ts: float, amount: float, card=0):
        """Features of this payment from the PRIOR ones, then remember it.
        Returns (n1h, n24h, sum24h, cards7d, avg30, state)."""
        st = self._get(key)
        if ts < st.last_ts:
            self.out_of_order += 1
            ts = st.last_ts
        ev = st.events
        while ev and ts - ev[0][0] > D30:
            st.sum30 -= ev.popleft()[1]
        n1 = n24 = 0
        s24 = 0.0
        cards = set()
        for t, a, c in reversed(ev):
            age = ts - t
            if age > D7:
                break
            cards.add(c)
            if age <= H24:
                n24 += 1
                s24 += a
                if age <= H1:
                    n1 += 1
        avg30 = st.sum30 / len(ev) if ev else 0.0
        ev.append((ts, amount, card))
        st.sum30 += amount
        st.last_ts = ts
        return n1, n24, s24, len(cards), avg30, st


_DAY_CACHE: dict = {}


def _epoch_day(y: int, m: int, d: int) -> float:
    key = (y, m, d)
    v = _DAY_CACHE.get(key)
    if v is None:
        v = _DAY_CACHE[key] = datetime(y, m, d, tzinfo=UTC).timestamp()
    return v


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 12742.0 * math.asin(min(1.0, math.sqrt(a)))


def _money(text: str) -> float:
    return float(text.replace("$", "").replace(",", "").strip())


def _label(text: str) -> int:
    t = text.strip().lower()
    if t in {"yes", "1", "true", "1.0"}:
        return 1
    if t in {"no", "0", "false", "0.0"}:
        return 0
    raise ValueError(text)


class Adapter:
    """Streams one public dataset as chunks of feature rows. Usage: `a = Adapter("sparkov", path)`; iterate `a.chunks()`;
    then `a.provenance()` says what was read, skipped and which features are real, approximate or synthetic."""

    def __init__(self, name: str, path: str | Path, *, seed: int = 0, chunk_rows: int = CHUNK_ROWS, fill: str = "class",
                 max_rows: int | None = None):
        if name not in SPECS:
            raise DatasetError(f"unknown dataset {name!r}; choose one of: {', '.join(SPECS)}")
        if fill not in {"class", "neutral"}:
            raise DatasetError("fill must be class or neutral")
        self.spec, self.path, self.chunk_rows, self.fill, self.max_rows = SPECS[name], Path(path), max(1, chunk_rows), fill, max_rows
        self.rng = np.random.default_rng(seed)
        self.windows = Windows()
        self.cards = Windows()
        self.read = self.used = self.fraud = 0
        self.skipped: dict[str, int] = {}
        self.columns: list[str] = []
        names = set(self.spec.real) | set(self.spec.approximate)
        self.real_mask = np.array([n in names for n in VECTOR_FIELDS])

    # ---- reading ---------------------------------------------------------------------------------------------
    def _open(self):
        if not self.path.is_file():
            raise DatasetError(f"no such file: {self.path}")
        opener = gzip.open if self.path.suffix == ".gz" else open
        return opener(self.path, "rt", newline="", encoding="utf-8-sig", errors="replace")

    def _skip(self, why: str) -> None:
        self.skipped[why] = self.skipped.get(why, 0) + 1

    def _records(self) -> Iterator[list[list[str]]]:
        with self._open() as fh:
            reader = csv.reader(fh)
            header = next(reader, None)
            if not header:
                raise DatasetError("the file is empty")
            lower = {c.strip().lower(): i for i, c in enumerate(header)}
            missing = [c for c in self.spec.required if c.lower() not in lower]
            if missing:
                raise DatasetError(f"{self.spec.name}: the file lacks the column(s) {', '.join(missing)}; found: "
                                   f"{', '.join(c.strip() for c in header[:20])}")
            if self.spec.name == "sparkov" and not {"unix_time", "trans_date_trans_time"} & set(lower):
                raise DatasetError("sparkov: the file needs unix_time or trans_date_trans_time")
            self.col = lower
            self.columns = [c for c in (*self.spec.required, *self.spec.optional) if c.lower() in lower]
            width, block = len(header), []
            for row in reader:
                if self.max_rows is not None and self.read >= self.max_rows:
                    break
                self.read += 1
                if len(row) != width:
                    self._skip("wrong_column_count")
                    continue
                block.append(row)
                if len(block) >= self.chunk_rows:
                    yield block
                    block = []
            if block:
                yield block

    # ---- per dataset: raw rows -> (partial feature matrix, labels, timestamps) ---------------------------------
    def chunks(self) -> Iterator[Chunk]:
        parse = {"tabformer": self._tabformer, "sparkov": self._sparkov, "paysim": self._paysim}[self.spec.name]
        for block in self._records():
            out = parse(block)
            if out is None:
                continue
            x, y, ts = out
            self.used += len(y)
            self.fraud += int(y.sum())
            self._complete(x, y)
            yield Chunk(x=x, y=y, ts=ts, real_mask=self.real_mask)

    def _complete(self, x: np.ndarray, y: np.ndarray) -> None:
        cols = [i for i in range(len(VECTOR_FIELDS)) if not self.real_mask[i]]
        labels = y if self.fill == "class" else np.zeros_like(y)
        x[:, cols] = synth.complete_columns(self.rng, labels, cols)

    def _finish(self, n: int, rows: list[tuple]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """rows: (ts, label, amount, n1, n24, s24, cards, avg30, card_n1, travel)."""
        a = np.array(rows, dtype=np.float64)
        x = np.zeros((n, len(VECTOR_FIELDS)), dtype=np.float32)
        amount, avg = a[:, 2], a[:, 7]
        x[:, IDX["amount_log"]] = np.log1p(amount)
        x[:, IDX["amount_vs_avg_ratio"]] = np.where(avg > 0, np.minimum(amount / np.where(avg > 0, avg, 1.0), 20.0), 1.0)
        x[:, IDX["user_tx_1h"]], x[:, IDX["user_tx_24h"]] = a[:, 3], a[:, 4]
        x[:, IDX["user_sum_24h_log"]] = np.log1p(a[:, 5])
        x[:, IDX["distinct_cards_7d"]] = np.maximum(a[:, 6], 1)
        x[:, IDX["card_tx_1h"]] = a[:, 8]
        x[:, IDX["impossible_travel"]] = a[:, 9]
        return x, a[:, 1].astype(np.int8), a[:, 0]

    def _tabformer(self, block):
        c = self.col
        iu, ic, iy, im, idd, it, ia, ifr = (c["user"], c["card"], c["year"], c["month"], c["day"], c["time"], c["amount"], c["is fraud?"])
        ichip, icity, istate = c.get("use chip"), c.get("merchant city"), c.get("merchant state")
        rows = []
        for r in block:
            try:
                amount = _money(r[ia])
                hh, mm = r[it].split(":")[:2]
                ts = _epoch_day(int(r[iy]), int(r[im]), int(r[idd])) + int(hh) * 3600 + int(mm) * 60
                label = _label(r[ifr])
            except ValueError:
                self._skip("unparseable")
                continue
            if amount <= 0:
                self._skip("refund_or_zero")
                continue
            user, card = r[iu], r[ic]
            n1, n24, s24, cards, avg, st = self.windows.observe(("u", user), ts, amount, card)
            card_n1 = self.cards.observe((user, card), ts, amount)[0]
            travel = 0
            physical = bool(istate is not None and r[istate].strip() and (ichip is None or r[ichip].strip() != "Online Transaction")
                            and (icity is None or r[icity].strip().upper() != "ONLINE"))
            if physical:
                here = (st.last_ts, r[istate].strip(), r[icity].strip() if icity is not None else "")
                if st.geo and ts - st.geo[0] <= TRAVEL_WINDOW and st.geo[1] != here[1] and st.geo[2] != here[2]:
                    travel = 1
                st.geo = here
            rows.append((ts, label, amount, n1, n24, s24, cards, avg, card_n1, travel))
        return self._finish(len(rows), rows) if rows else None

    def _sparkov(self, block):
        c = self.col
        icc, iamt, ify = c["cc_num"], c["amt"], c["is_fraud"]
        iunix, idt = c.get("unix_time"), c.get("trans_date_trans_time")
        ilat, ilon = c.get("merch_lat"), c.get("merch_long")
        rows = []
        for r in block:
            try:
                amount = _money(r[iamt])
                ts = float(r[iunix]) if iunix is not None and r[iunix].strip() else datetime.fromisoformat(r[idt].strip()).replace(tzinfo=UTC).timestamp()
                label = _label(r[ify])
                lat, lon = (float(r[ilat]), float(r[ilon])) if ilat is not None and ilon is not None else (None, None)
            except ValueError:
                self._skip("unparseable")
                continue
            if amount <= 0:
                self._skip("refund_or_zero")
                continue
            n1, n24, s24, cards, avg, st = self.windows.observe(r[icc], ts, amount)
            travel = 0
            if lat is not None:
                if st.geo and 0 <= ts - st.geo[0] <= TRAVEL_WINDOW:
                    km = haversine_km(st.geo[1], st.geo[2], lat, lon)
                    travel = int(km >= 100 and km / max((ts - st.geo[0]) / 3600, 1 / 60) > TRAVEL_KMH)
                st.geo = (ts, lat, lon)
            rows.append((ts, label, amount, n1, n24, s24, cards, avg, n1, travel))   # one card per customer: card = user
        return self._finish(len(rows), rows) if rows else None

    def _paysim(self, block):
        c = self.col
        istep, iamt, iorig, ify = c["step"], c["amount"], c["nameorig"], c["isfraud"]
        rows = []
        for r in block:
            try:
                amount = _money(r[iamt])
                ts = float(int(r[istep])) * 3600.0
                label = _label(r[ify])
            except ValueError:
                self._skip("unparseable")
                continue
            if amount <= 0:
                self._skip("refund_or_zero")
                continue
            n1, n24, s24, _, avg, _st = self.windows.observe(r[iorig], ts, amount)
            rows.append((ts, label, amount, n1, n24, s24, 1, avg, 0, 0))
        return self._finish(len(rows), rows) if rows else None

    # ---- provenance ---------------------------------------------------------------------------------------------
    def provenance(self) -> dict:
        s = self.spec
        return {"kind": "dataset", "name": s.name, "file": self.path.name, "rows_read": self.read, "rows_used": self.used,
                "fraud_rows": self.fraud, "skipped": dict(self.skipped), "out_of_order_records": self.windows.out_of_order,
                "entities_evicted": self.windows.evicted, "columns_read": self.columns,
                "columns_never_read": list(s.never_read), "real_features": list(s.real),
                "approximate_features": list(s.approximate), "synthetic_features": s.synthetic(),
                "synthetic_fill": self.fill, "notes": s.notes}
