import json
import os
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import ClientError

dynamodb = boto3.client("dynamodb")
deserializer = TypeDeserializer()

EXCEPTIONS_TABLE = os.environ["EXCEPTIONS_TABLE"]
AUDIT_TABLE = os.environ["AUDIT_TABLE"]

STATUSES = {"open", "approved", "rejected"}
DECISIONS = {"approve": "approved", "reject": "rejected"}


def respond(status, body):
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body, default=str),
    }


def plain(item):
    return {k: deserializer.deserialize(v) for k, v in item.items()}


def list_exceptions(status):
    items = []
    kwargs = {
        "TableName": EXCEPTIONS_TABLE,
        "FilterExpression": "#s = :s",
        "ExpressionAttributeNames": {"#s": "status"},
        "ExpressionAttributeValues": {":s": {"S": status}},
    }
    while len(items) < 100:
        page = dynamodb.scan(**kwargs)
        items += [plain(i) for i in page["Items"]]
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    return items[:100]


def audit_trail(record_id, rule):
    resp = dynamodb.query(
        TableName=AUDIT_TABLE,
        KeyConditionExpression="exception_key = :k",
        ExpressionAttributeValues={":k": {"S": f"{record_id}#{rule}"}},
    )
    return [plain(i) for i in resp["Items"]]


def review(record_id, rule, decision, reason, reviewer):
    new_status = DECISIONS[decision]
    ts = datetime.now(timezone.utc).isoformat()

    dynamodb.transact_write_items(
        TransactItems=[
            {
                "Update": {
                    "TableName": EXCEPTIONS_TABLE,
                    "Key": {"record_id": {"S": record_id}, "rule": {"S": rule}},
                    "UpdateExpression": "SET #s = :new, reviewed_by = :by, reviewed_at = :at, review_reason = :why",
                    "ConditionExpression": "attribute_exists(record_id) AND #s = :open",
                    "ExpressionAttributeNames": {"#s": "status"},
                    "ExpressionAttributeValues": {
                        ":new": {"S": new_status},
                        ":by": {"S": reviewer},
                        ":at": {"S": ts},
                        ":why": {"S": reason},
                        ":open": {"S": "open"},
                    },
                }
            },
            {
                "Put": {
                    "TableName": AUDIT_TABLE,
                    "Item": {
                        "exception_key": {"S": f"{record_id}#{rule}"},
                        "event_at": {"S": ts},
                        "action": {"S": decision},
                        "from_status": {"S": "open"},
                        "to_status": {"S": new_status},
                        "reviewer": {"S": reviewer},
                        "reason": {"S": reason},
                    },
                    "ConditionExpression": "attribute_not_exists(exception_key)",
                }
            },
        ]
    )
    return new_status


def lambda_handler(event, context):
    route = event["routeKey"]
    params = event.get("queryStringParameters") or {}

    try:
        if route == "GET /exceptions":
            status = params.get("status", "open")
            if status not in STATUSES:
                return respond(400, {"error": f"status must be one of {sorted(STATUSES)}"})
            items = list_exceptions(status)
            return respond(200, {"count": len(items), "items": items})

        if route == "GET /audit":
            record_id, rule = params.get("record_id"), params.get("rule")
            if not record_id or not rule:
                return respond(400, {"error": "record_id and rule are required"})
            return respond(200, {"events": audit_trail(record_id, rule)})

        if route == "POST /review":
            try:
                body = json.loads(event.get("body") or "{}")
            except json.JSONDecodeError:
                return respond(400, {"error": "body must be valid JSON"})

            record_id = (body.get("record_id") or "").strip()
            rule = (body.get("rule") or "").strip()
            decision = body.get("decision")
            reason = (body.get("reason") or "").strip()

            if not record_id or not rule:
                return respond(400, {"error": "record_id and rule are required"})
            if decision not in DECISIONS:
                return respond(400, {"error": f"decision must be one of {sorted(DECISIONS)}"})
            if not reason:
                return respond(400, {"error": "reason is required"})

            reviewer = event["requestContext"]["authorizer"]["iam"]["userArn"]
            new_status = review(record_id, rule, decision, reason, reviewer)
            return respond(200, {"record_id": record_id, "rule": rule, "status": new_status})

        return respond(404, {"error": "not found"})

    except ClientError as err:
        if err.response["Error"]["Code"] == "TransactionCanceledException":
            return respond(409, {"error": "exception not found or already reviewed"})
        raise