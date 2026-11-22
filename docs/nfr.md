# Non-functional requirements, cost and scaling

Targets below are for the client brief in the README. Where something has been measured it says so. Where it has not, it says that too.

## NFR table

| Category | Requirement | How it is met today | Gap / next step |
|---|---|---|---|
| **Availability** | A single availability-zone failure should not interrupt intake or review | Every component is a regional, managed service (S3, SQS, DynamoDB, Lambda, API Gateway). There are no servers to fail over. | Single region only. No multi-region failover. |
| **Reliability** | No record is lost or double-counted | S3 stores the file durably. SQS buffers events and retries. A dead-letter queue catches files that fail 3 times. Writes are idempotent (conditional writes), so redelivery is safe. Verified by test: duplicate uploads produced zero duplicates. | None known. |
| **Security** | Only authorised, identifiable callers can review; data is protected at rest and in transit | S3 public access blocked, SSE-S3 encryption. API requires SigV4 (IAM). Separate least-privilege roles for the processor and reviewer. Rate limiting on the API. TLS on all AWS endpoints. | AWS-managed keys rather than customer-managed KMS. Lambdas outside a VPC. No bucket policy forcing TLS. No S3 versioning. |
| **Auditability** | Every review decision answers who, when, what and why | Each decision records the verified IAM identity, UTC timestamp, old and new status, and a mandatory reason. The status change and audit event are one atomic transaction. The reviewer role can only add and query audit events (no update or delete permission). | No hash chaining, so tamper-evidence relies on IAM and the conditional put. DynamoDB data-event logging not enabled. |
| **Data retention** | Retention is explicit and per data type | Queues: 4 days (upload queue), 14 days (DLQ). Lambda logs: 14 days. | **Uploaded files and all DynamoDB data are kept indefinitely today.** Add an S3 lifecycle rule and a client-defined retention policy for the audit table. |
| **Recoverability** | Rebuild the system and recover data after a failure | Infrastructure is fully reproducible from Terraform. The original CSV files are the source of truth, and reprocessing them is safe because of idempotency. | **DynamoDB point-in-time recovery is not enabled.** Enable it on `exceptions` and `audit`. Review decisions cannot be recreated from the source files, so these two tables matter most. |
| **Performance** | A typical file completes well inside the Lambda timeout (30 s) | Observed: a 3-row file ran in about 140 ms warm, with a cold start of about 490 ms. | **No load test yet, so no throughput claim is made.** |
| **Scalability** | Handle bursts of uploads without losing files | The queue absorbs bursts. Lambda scales with queue depth. DynamoDB on-demand scales with load. | No reserved concurrency set on the processor. API throttle is deliberately low (5 req/s, burst 10). |
| **Observability** | Operators learn about failures without looking | Structured JSON logs. Four CloudWatch alarms (DLQ not empty, queue backlog, processor errors, reviewer errors) with SNS email, including recovery notifications. | No dashboard. No X-Ray tracing. |
| **Cost** | Near zero when idle; cost grows with use | No always-on resources. Everything is pay-per-use. | See below. |
| **Maintainability** | Safe to change and easy to hand over | All infrastructure in Terraform. Validation rules are separate and unit-tested. Design decisions recorded as ADRs. | No CI pipeline running tests and `terraform validate` on pull requests. |

## Cost

**Idle cost.** There is nothing always-on: no database instance, no NAT gateway, no containers. With no traffic the bill is limited to small storage charges (S3, DynamoDB, log storage).

| Component | Pricing model | What drives the bill |
|---|---|---|
| Lambda (x2) | Per request plus per GB-second | Number of files and rows, duration per file |
| SQS | Per request | Roughly a few requests per file; first million per month free |
| DynamoDB (3 tables) | On-demand, per read and write unit, plus storage | Rows per month, because each row costs about one read and one write |
| S3 | Storage plus requests | Total file size retained |
| API Gateway (HTTP API) | Per request | Reviewer activity, which is low |
| CloudWatch | Alarms, logs ingested and stored | 4 alarms (the first 10 are free), log volume |
| SNS | Per notification | A handful of emails |

**How to produce your estimate.** Pick a scenario and count requests, then price them in the [AWS Pricing Calculator](https://calculator.aws/) for eu-west-2. Example scenario: 1,000 files per month, 5,000 rows each (5 million rows).

- DynamoDB reads: about 5 million `GetItem` calls (eventually consistent, half a read unit each), so about 2.5 million read units
- DynamoDB writes: about 5 million write units for transactions, plus one per exception
- Lambda: about 1,000 invocations (batched), duration measured from a real large file
- SQS: a few thousand requests

I have not priced this in eu-west-2, so no total is stated here. Put your calculator figure in the README once you have it.

## What changes at 10x load

- **Listing exceptions** uses a table scan. Add a global secondary index on `status`.
- **Large files:** stream and split into multiple messages so no single invocation nears the Lambda timeout.
- **Concurrency:** set reserved concurrency on the processor to protect DynamoDB and downstream systems.
- **Reporting:** export to a relational or analytical store if reviewers need rich queries.
- **Cost model:** on-demand is cheapest for spiky, low volume. At sustained high throughput, compare provisioned capacity with auto scaling.
- **Recoverability:** enable point-in-time recovery and S3 versioning before any real data is involved.