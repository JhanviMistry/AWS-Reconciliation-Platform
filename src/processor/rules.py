from datetime import date
from decimal import Decimal, InvalidOperation

ALLOWED_CURRENCIES = {"GBP", "USD", "EUR"}
HIGH_VALUE_THRESHOLD = Decimal("10000")


def validate_row(row, today=None):
    """Return a list of (rule, detail) violations. Empty list means clean."""
    today = today or date.today()
    issues = []

    for field in ("transaction_id", "account_id"):
        if not (row.get(field) or "").strip():
            issues.append((f"missing_{field}", f"{field} is empty"))

    amount = None
    raw_amount = (row.get("amount") or "").strip()
    try:
        amount = Decimal(raw_amount)
        if not amount.is_finite():
            raise InvalidOperation
    except InvalidOperation:
        amount = None
        issues.append(("invalid_amount", f"cannot parse amount '{raw_amount}'"))

    if amount is not None and abs(amount) > HIGH_VALUE_THRESHOLD:
        issues.append(("high_value", f"amount {amount} exceeds {HIGH_VALUE_THRESHOLD}"))

    currency = (row.get("currency") or "").strip().upper()
    if currency not in ALLOWED_CURRENCIES:
        issues.append(("unsupported_currency", f"currency '{currency}' not allowed"))

    raw_date = (row.get("booked_at") or "").strip()
    try:
        booked = date.fromisoformat(raw_date)
        if booked > today:
            issues.append(("future_dated", f"booked_at {raw_date} is in the future"))
    except ValueError:
        issues.append(("invalid_date", f"cannot parse booked_at '{raw_date}'"))

    return issues