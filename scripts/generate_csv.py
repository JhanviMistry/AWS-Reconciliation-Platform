"""Generate a synthetic transactions CSV for load testing."""
import argparse
import csv
import random
from datetime import date, timedelta

BAD_KINDS = ["high_value", "invalid_amount", "bad_currency", "future_date", "missing_account"]


def make_row(index, prefix, rng, bad_kind=None):
    booked = date.today() - timedelta(days=rng.randint(1, 90))
    row = {
        "transaction_id": f"{prefix}-{index:07d}",
        "account_id": f"A{rng.randint(100, 999)}",
        "amount": f"{rng.uniform(-5000, 5000):.2f}",
        "currency": rng.choice(["GBP", "USD", "EUR"]),
        "booked_at": booked.isoformat(),
    }
    if bad_kind == "high_value":
        row["amount"] = f"{rng.uniform(10001, 50000):.2f}"
    elif bad_kind == "invalid_amount":
        row["amount"] = "abc"
    elif bad_kind == "bad_currency":
        row["currency"] = "XYZ"
    elif bad_kind == "future_date":
        row["booked_at"] = (date.today() + timedelta(days=30)).isoformat()
    elif bad_kind == "missing_account":
        row["account_id"] = ""
    return row


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=1000)
    parser.add_argument("--bad-rate", type=float, default=0.05)
    parser.add_argument("--prefix", default="LT")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", default="load_test.csv")
    args = parser.parse_args()

    rng = random.Random(args.seed)
    fieldnames = ["transaction_id", "account_id", "amount", "currency", "booked_at"]
    with open(args.out, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for i in range(1, args.rows + 1):
            bad_kind = rng.choice(BAD_KINDS) if rng.random() < args.bad_rate else None
            writer.writerow(make_row(i, args.prefix, rng, bad_kind))
    print(f"wrote {args.rows} rows to {args.out}")


if __name__ == "__main__":
    main()