"""Writes the tiny INVENTED fixture files in this folder (same columns as the public datasets, made-up rows).

    python tests/fixtures/make_fixtures.py

Nothing here comes from a real dataset: names, cards and places are fictional, amounts and times are random with a fixed seed.
The files let the adapters and the training script be tested end to end without downloading anything.
"""
import csv
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
rng = random.Random(20261022)
CITIES = [("Springfield", "IL"), ("Dallas", "TX"), ("Austin", "TX"), ("Portland", "OR"), ("Boston", "MA"), ("Miami", "FL")]


def tabformer(n_users: int = 25, per_user: int = 22) -> None:
    out = []
    start = datetime(2020, 1, 1, tzinfo=UTC)
    for u in range(n_users):
        t = start + timedelta(hours=rng.randint(0, 48))
        home = rng.choice(CITIES)
        for _ in range(per_user):
            t += timedelta(minutes=rng.choice([20, 45, 90, 400, 900, 1500]))
            fraud = rng.random() < 0.07
            city, state = (rng.choice(CITIES) if fraud else home)
            online = rng.random() < 0.2
            amount = rng.lognormvariate(6.2 if fraud else 3.4, 0.6)
            out.append([u, rng.randint(0, 1), t.year, t.month, t.day, f"{t.hour:02d}:{t.minute:02d}", f"${amount:,.2f}" if rng.random() > 0.03 else f"-${amount:.2f}",
                        "Online Transaction" if online else rng.choice(["Swipe Transaction", "Chip Transaction"]), rng.randint(10**8, 10**9),
                        "ONLINE" if online else city, "" if online else state, "" if online else f"{rng.randint(10000, 99999)}",
                        rng.choice([5411, 5812, 4121, 5999]), "", "Yes" if fraud else "No"])
    with (HERE / "tabformer_tiny.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["User", "Card", "Year", "Month", "Day", "Time", "Amount", "Use Chip", "Merchant Name", "Merchant City", "Merchant State", "Zip",
                    "MCC", "Errors?", "Is Fraud?"])
        w.writerows(out)


def sparkov(n_cards: int = 24, per_card: int = 16) -> None:
    out = []
    base = 1_600_000_000
    for c in range(n_cards):
        lat, lon = rng.uniform(30, 47), rng.uniform(-120, -75)
        t = base + rng.randint(0, 86400)
        for _ in range(per_card):
            t += rng.choice([300, 900, 3600, 20000, 90000])
            fraud = rng.random() < 0.06
            mlat, mlon = (lat + rng.uniform(-8, 8), lon + rng.uniform(-8, 8)) if fraud else (lat + rng.uniform(-0.4, 0.4), lon + rng.uniform(-0.4, 0.4))
            amt = rng.lognormvariate(5.6 if fraud else 3.3, 0.5)
            ts = datetime.fromtimestamp(t, UTC)
            out.append([ts.strftime("%Y-%m-%d %H:%M:%S"), 4000000000000000 + c, f"fraud_Shop {rng.randint(1, 40)}", rng.choice(["grocery_pos", "gas_transport", "shopping_net"]),
                        f"{amt:.2f}", "Firstname", "Lastname", "F", "1 Invented Way", "Springfield", "IL", "62701", f"{lat:.4f}", f"{lon:.4f}", 5000, "Invented job",
                        "1980-01-01", f"{rng.getrandbits(64):016x}", t, f"{mlat:.6f}", f"{mlon:.6f}", int(fraud)])
    with (HERE / "sparkov_tiny.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["", "trans_date_trans_time", "cc_num", "merchant", "category", "amt", "first", "last", "gender", "street", "city", "state", "zip", "lat",
                    "long", "city_pop", "job", "dob", "trans_num", "unix_time", "merch_lat", "merch_long", "is_fraud"])
        w.writerows([[i, *r] for i, r in enumerate(out)])


def paysim(n: int = 700) -> None:
    out = []
    for i in range(n):
        step = 1 + i // 12
        fraud = rng.random() < 0.06
        kind = rng.choice(["TRANSFER", "CASH_OUT"]) if fraud else rng.choice(["PAYMENT", "CASH_IN", "TRANSFER", "CASH_OUT", "DEBIT"])
        amount = rng.lognormvariate(10 if fraud else 6, 1.0)
        old = amount * rng.uniform(1, 2)
        orig = f"C{rng.randint(1, 120)}"
        out.append([step, kind, f"{amount:.2f}", orig, f"{old:.2f}", "0.00" if fraud else f"{old - amount:.2f}", f"C{rng.randint(1000, 1100)}", "0.00", "0.00",
                    int(fraud), 0])
    with (HERE / "paysim_tiny.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["step", "type", "amount", "nameOrig", "oldbalanceOrg", "newbalanceOrig", "nameDest", "oldbalanceDest", "newbalanceDest", "isFraud",
                    "isFlaggedFraud"])
        w.writerows(out)


if __name__ == "__main__":
    tabformer()
    sparkov()
    paysim()
    for p in sorted(HERE.glob("*.csv")):
        print(p.name, p.stat().st_size, "bytes")
