# Architecture

Two views of the system, following the C4 model: system context (who uses it and what it talks to) and containers (the deployable pieces inside it).

## Level 1: System context

```mermaid
flowchart TB
    uploader["Uploader<br/>Submits transaction CSV files"]
    reviewer["Reviewer<br/>Approves or rejects flagged items"]
    operator["Operator<br/>Receives operational alerts"]

    platform["<b>Exception and Reconciliation Platform</b><br/>Validates transaction files, flags exceptions,<br/>records reviewer decisions with an audit trail"]

    email["Email<br/>Alert delivery"]

    uploader -->|Uploads CSV files| platform
    reviewer -->|Lists and reviews exceptions via API| platform
    platform -->|Sends alerts| email
    email -->|Notifies| operator
```

## Level 2: Containers

```mermaid
flowchart TB
    uploader["Uploader"]
    reviewer["Reviewer"]
    operator["Operator"]

    subgraph aws["AWS account (eu-west-2)"]
        subgraph intake["Intake"]
            s3["<b>S3 bucket</b><br/>[Storage]<br/>incoming/*.csv<br/>private, SSE-S3"]
            q["<b>Upload queue</b><br/>[SQS]<br/>visibility timeout 180s"]
            dlq["<b>Dead-letter queue</b><br/>[SQS]<br/>after 3 failed receives"]
            proc["<b>Processor</b><br/>[Lambda, Python 3.12]<br/>validate, deduplicate,<br/>record exceptions"]
        end

        subgraph state["State"]
            tx[("<b>transactions</b><br/>[DynamoDB]")]
            ex[("<b>exceptions</b><br/>[DynamoDB]")]
            au[("<b>audit</b><br/>[DynamoDB]<br/>append-only")]
        end

        subgraph review["Review"]
            api["<b>HTTP API</b><br/>[API Gateway]<br/>IAM auth, throttled"]
            rev["<b>Reviewer</b><br/>[Lambda, Python 3.12]<br/>list, review, audit"]
        end

        subgraph ops["Operations"]
            cw["<b>Alarms</b><br/>[CloudWatch]"]
            sns["<b>Alert topic</b><br/>[SNS]"]
        end
    end

    uploader -->|PUT CSV| s3
    s3 -->|ObjectCreated event| q
    q -->|batch of 5| proc
    q -.->|poison messages| dlq
    proc -->|conditional writes| tx
    proc -->|conditional writes| ex

    reviewer -->|SigV4-signed HTTPS| api
    api --> rev
    rev -->|read, status update| ex
    rev -->|atomic transaction| au

    dlq -.->|depth metric| cw
    q -.->|age metric| cw
    proc -.->|error metric| cw
    rev -.->|error metric| cw
    cw --> sns
    sns -->|email| operator
```

## Key flows

**Ingestion.** An uploader drops a CSV in `incoming/`. S3 emits an event to the upload queue. The processor reads the file, validates each row, and writes clean rows to `transactions` and violations to `exceptions` using conditional writes, so redelivery and re-uploads are safe. A file that fails entirely (for example, wrong headers) is retried and, after three attempts, moved to the dead-letter queue, which raises an alarm.

**Review.** A reviewer sends a signed request to the HTTP API. API Gateway verifies the caller's AWS identity and passes it to the reviewer Lambda. A decision updates the exception and appends an audit event in one DynamoDB transaction. Both succeed or neither does, and a second decision on the same exception is rejected.

## Trust boundaries

- The S3 bucket and queues are private. Only the processor role can read from `incoming/` and the queue.
- The only public entry point is the HTTP API, and every route requires a valid AWS signature.
- The reviewer role cannot read S3 or SQS. The processor role cannot touch the audit table.