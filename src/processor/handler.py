import csv
import hashlib
import json
import logging
import os
import urllib.parse
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

from rules import validate_row

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")
transactions = dynamodb.Table(os.environ["TRANSACTIONS_TABLE"])
exceptions = dynamodb.Table(os.environ["EXCEPTIONS_TABLE"])

REQUIRED_COLUMNS = {"transaction_id", "account_id", "amount", "currency", "booked_at"}


def now():
    return datetime.now(timezone.utc).isoformat()


def clean_row(row):
    return {k: (row.get(k) or "").strip() for k in sorted(REQUIRED_COLUMNS)}


def row_hash(row):
    return hashlib.sha256(json.dumps(clean_row(row), sort_keys=True).encode()).hexdigest()


def is_conditional_failure(err):
    return err.response["Error"]["Code"] == "ConditionalCheckFailedException"


def record_exception(record_id, rule, detail, source_key, row_number):
    """Idempotent: never overwrites an existing exception (and later, its review status)."""
    try:
        exceptions.put_item(
            Item={
                "record_id": record_id,
                "rule": rule,
                "detail": detail,
                "source_key": source_key,
                "row_number": row_number,
                "status": "open",
                "created_at": now(),
            },
            ConditionExpression="attribute_not_exists(record_id)",
        )
    except ClientError as err:
        if not is_conditional_failure(err):
            raise


def process_row(row, row_number, key, stats):
    tid = (row.get("transaction_id") or "").strip()
    record_id = tid or f"{key}#row{row_number}"
    issues = validate_row(row)
    digest = row_hash(row)

    if tid:
        existing = transactions.get_item(Key={"transaction_id": tid}).get("Item")
        if existing:
            if existing["row_hash"] == digest:
                stats["duplicates"] += 1
            else:
                stats["conflicts"] += 1
                record_exception(
                    record_id, "conflicting_duplicate",
                    "same transaction_id seen earlier with different content", key, row_number,
                )
            return

    # Exceptions are written BEFORE the transaction record. The transaction record
    # is the commit marker: if we crash in between, the retry redoes the (idempotent)
    # exception writes instead of mistaking the row for an already-handled duplicate.
    for rule, detail in issues:
        record_exception(record_id, rule, detail, key, row_number)

    if tid:
        try:
            transactions.put_item(
                Item={
                    "transaction_id": tid,
                    "row_hash": digest,
                    "status": "exception" if issues else "clean",
                    "source_key": key,
                    "row_number": row_number,
                    "ingested_at": now(),
                    "data": clean_row(row),
                },
                ConditionExpression="attribute_not_exists(transaction_id)",
            )
        except ClientError as err:
            if not is_conditional_failure(err):
                raise
            stats["duplicates"] += 1  # lost a race with a concurrent invocation
            return

    stats["flagged" if issues else "clean"] += 1


def process_file(bucket, key):
    obj = s3.get_object(Bucket=bucket, Key=key)
    lines = (line.decode("utf-8") for line in obj["Body"].iter_lines())
    reader = csv.DictReader(lines)

    missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
    if missing:
        raise ValueError(f"{key}: missing columns {sorted(missing)}")

    stats = {"rows": 0, "clean": 0, "flagged": 0, "duplicates": 0, "conflicts": 0}
    for row_number, row in enumerate(reader, start=2):
        stats["rows"] += 1
        process_row(row, row_number, key, stats)

    logger.info(json.dumps({"event": "file_processed", "bucket": bucket, "key": key, **stats}))


def lambda_handler(event, context):
    failures = []

    for sqs_record in event["Records"]:
        message_id = sqs_record["messageId"]
        try:
            body = json.loads(sqs_record["body"])
            for s3_record in body.get("Records", []):
                bucket = s3_record["s3"]["bucket"]["name"]
                key = urllib.parse.unquote_plus(s3_record["s3"]["object"]["key"])
                process_file(bucket, key)
        except Exception:
            logger.exception(json.dumps({"event": "processing_failed", "message_id": message_id}))
            failures.append({"itemIdentifier": message_id})

    return {"batchItemFailures": failures}