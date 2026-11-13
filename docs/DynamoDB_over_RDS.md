# ADR 0001: Use DynamoDB for state instead of RDS PostgreSQL

## Context

The platform stores three kinds of data: ingested transactions, exceptions with review status, and an append-only audit log. The access patterns are narrow and key-based:

- Look up a transaction by `transaction_id` (idempotency check)
- Write an exception once per `(record_id, rule)` and never overwrite a reviewed one
- List exceptions by status (low volume, human-paced)
- Read an exception's audit events in time order

Requirements that shape the choice: writes must be idempotent under at-least-once delivery, a review decision and its audit event must be atomic, idle cost should be near zero, and operational overhead should be minimal.

## Decision

Use **DynamoDB with on-demand capacity**, as three tables: `transactions`, `exceptions` and `audit`.

## Options considered

**RDS PostgreSQL.** Strong relational model, constraints, joins and ad-hoc SQL. Against it: an always-on instance has a baseline cost even when idle; it needs patching and backup management; Lambda connections need pooling or a proxy; and Lambdas must run inside the VPC to reach it, which adds networking cost and complexity.

**DynamoDB.** Conditional writes (`attribute_not_exists`) give idempotency without locks. `TransactWriteItems` gives the atomic review-plus-audit write. On-demand pricing matches spiky, low-volume file processing, and there is no server to patch.

## Consequences

**Good**
- Idempotency and atomic review are enforced by the database, not by application locking.
- No idle cost, no instance to maintain.
- Lambdas stay outside a VPC, which keeps the architecture simpler.

**Trade-offs**
- Listing exceptions by status currently uses a table scan. Acceptable at this volume; a global secondary index on `status` is the fix at scale.
- No ad-hoc SQL or joins. Reporting would need an export to a relational or analytical store.
- Items are limited to 400 KB, which is ample for this data.

## Revisit when

Reviewers need rich filtering or reporting across entities, or volume grows to where scans and secondary indexes no longer cover the query needs.