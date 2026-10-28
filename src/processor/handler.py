import csv
import json
import logging
import urllib.parse

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client("s3")

REQUIRED_COLUMNS = {"transaction_id", "account_id", "amount", "currency", "booked_at"}


def process_file(bucket, key):
    obj = s3.get_object(Bucket=bucket, Key=key)
    lines = (line.decode("utf-8") for line in obj["Body"].iter_lines())
    reader = csv.DictReader(lines)

    missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
    if missing:
        raise ValueError(f"{key}: missing columns {sorted(missing)}")

    count = sum(1 for _ in reader)
    logger.info(json.dumps({"event": "file_processed", "bucket": bucket, "key": key, "rows": count}))


def lambda_handler(event, context):
    failures = []

    for sqs_record in event["Records"]:
        message_id = sqs_record["messageId"]
        try:
            body = json.loads(sqs_record["body"])
            # S3 sends a test message when notifications are first configured
            for s3_record in body.get("Records", []):
                bucket = s3_record["s3"]["bucket"]["name"]
                key = urllib.parse.unquote_plus(s3_record["s3"]["object"]["key"])
                process_file(bucket, key)
        except Exception:
            logger.exception(json.dumps({"event": "processing_failed", "message_id": message_id}))
            failures.append({"itemIdentifier": message_id})

    return {"batchItemFailures": failures}