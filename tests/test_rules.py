import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "processor"))

from rules import validate_row

TODAY = date(2026, 11, 2)


def row(**overrides):
    base = {"transaction_id": "T1", "account_id": "A1", "amount": "10.00",
            "currency": "GBP", "booked_at": "2026-10-01"}
    base.update(overrides)
    return base


def rules_for(r):
    return {rule for rule, _ in validate_row(r, today=TODAY)}


def test_clean_row():
    assert validate_row(row(), today=TODAY) == []

def test_invalid_amount():
    assert rules_for(row(amount="abc")) == {"invalid_amount"}

def test_nan_is_invalid():
    assert rules_for(row(amount="NaN")) == {"invalid_amount"}

def test_high_value():
    assert rules_for(row(amount="-15000")) == {"high_value"}

def test_unsupported_currency():
    assert rules_for(row(currency="XYZ")) == {"unsupported_currency"}

def test_future_date():
    assert rules_for(row(booked_at="2027-01-01")) == {"future_dated"}

def test_invalid_date():
    assert rules_for(row(booked_at="01/10/2026")) == {"invalid_date"}

def test_multiple_issues():
    assert rules_for(row(account_id="", booked_at="2027-01-01")) == {"missing_account_id", "future_dated"}