terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.0"
    }
  }
}

provider "aws" {
  region = "eu-west-2"

  default_tags {
    tags = {
      Project = "exception-platform"
    }
  }

  variable "alert_email" {
  type        = string
  description = "Email address for operational alerts"
}
}

resource "random_id" "suffix" {
  byte_length = 4
}

resource "aws_s3_bucket" "uploads" {
  bucket        = "exception-platform-uploads-${random_id.suffix.hex}"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "uploads" {
  bucket                  = aws_s3_bucket.uploads.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "uploads" {
  bucket = aws_s3_bucket.uploads.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

data "aws_caller_identity" "current" {}

# ---------- Queues ----------
resource "aws_sqs_queue" "dlq" {
  name                      = "exception-platform-dlq"
  message_retention_seconds = 1209600 # 14 days
}

resource "aws_sqs_queue" "uploads" {
  name                       = "exception-platform-uploads"
  visibility_timeout_seconds = 180 # 6x the Lambda timeout
  message_retention_seconds  = 345600

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = 3
  })
}

resource "aws_sqs_queue_policy" "uploads" {
  queue_url = aws_sqs_queue.uploads.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "s3.amazonaws.com" }
      Action    = "sqs:SendMessage"
      Resource  = aws_sqs_queue.uploads.arn
      Condition = {
        ArnLike      = { "aws:SourceArn" = aws_s3_bucket.uploads.arn }
        StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id }
      }
    }]
  })
}

# ---------- S3 -> SQS ----------
resource "aws_s3_bucket_notification" "uploads" {
  bucket = aws_s3_bucket.uploads.id

  queue {
    queue_arn     = aws_sqs_queue.uploads.arn
    events        = ["s3:ObjectCreated:*"]
    filter_prefix = "incoming/"
    filter_suffix = ".csv"
  }

  depends_on = [aws_sqs_queue_policy.uploads]
}

# ---------- Lambda ----------
data "archive_file" "processor" {
  type        = "zip"
  source_dir  = "${path.module}/../src/processor"
  output_path = "${path.module}/processor.zip"
}

resource "aws_cloudwatch_log_group" "processor" {
  name              = "/aws/lambda/exception-platform-processor"
  retention_in_days = 14
}

resource "aws_iam_role" "processor" {
  name = "exception-platform-processor-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "processor" {
  name = "processor-least-privilege"
  role = aws_iam_role.processor.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.processor.arn}:*"
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "${aws_s3_bucket.uploads.arn}/incoming/*"
      },
      {
        Effect   = "Allow"
        Action   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
        Resource = aws_sqs_queue.uploads.arn
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem"]
        Resource = [aws_dynamodb_table.transactions.arn, aws_dynamodb_table.exceptions.arn]
      }
    ]
  })
}

resource "aws_lambda_function" "processor" {
  function_name    = "exception-platform-processor"
  role             = aws_iam_role.processor.arn
  runtime          = "python3.12"
  handler          = "handler.lambda_handler"
  filename         = data.archive_file.processor.output_path
  source_code_hash = data.archive_file.processor.output_base64sha256
  timeout          = 30
  memory_size      = 256

  environment {
    variables = {
      TRANSACTIONS_TABLE = aws_dynamodb_table.transactions.name
      EXCEPTIONS_TABLE   = aws_dynamodb_table.exceptions.name
    }
  }

  depends_on = [aws_cloudwatch_log_group.processor]
}

resource "aws_lambda_event_source_mapping" "processor" {
  event_source_arn        = aws_sqs_queue.uploads.arn
  function_name           = aws_lambda_function.processor.arn
  batch_size              = 5
  function_response_types = ["ReportBatchItemFailures"]
}

# ---------- Outputs ----------
output "bucket_name" {
  value = aws_s3_bucket.uploads.id
}

output "dlq_url" {
  value = aws_sqs_queue.dlq.id
}

resource "aws_dynamodb_table" "transactions" {
  name         = "exception-platform-transactions"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "transaction_id"

  attribute {
    name = "transaction_id"
    type = "S"
  }
}

resource "aws_dynamodb_table" "exceptions" {
  name         = "exception-platform-exceptions"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "record_id"
  range_key    = "rule"

  attribute {
    name = "record_id"
    type = "S"
  }

  attribute {
    name = "rule"
    type = "S"
  }
}

# ---------- Audit table ----------
resource "aws_dynamodb_table" "audit" {
  name         = "exception-platform-audit"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "exception_key"
  range_key    = "event_at"

  attribute {
    name = "exception_key"
    type = "S"
  }

  attribute {
    name = "event_at"
    type = "S"
  }
}

# ---------- Reviewer Lambda ----------
data "archive_file" "reviewer" {
  type        = "zip"
  source_dir  = "${path.module}/../src/reviewer"
  output_path = "${path.module}/reviewer.zip"
}

resource "aws_cloudwatch_log_group" "reviewer" {
  name              = "/aws/lambda/exception-platform-reviewer"
  retention_in_days = 14
}

resource "aws_iam_role" "reviewer" {
  name = "exception-platform-reviewer-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "reviewer" {
  name = "reviewer-least-privilege"
  role = aws_iam_role.reviewer.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.reviewer.arn}:*"
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:Scan", "dynamodb:UpdateItem"]
        Resource = aws_dynamodb_table.exceptions.arn
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem", "dynamodb:Query"]
        Resource = aws_dynamodb_table.audit.arn
      }
    ]
  })
}

resource "aws_lambda_function" "reviewer" {
  function_name    = "exception-platform-reviewer"
  role             = aws_iam_role.reviewer.arn
  runtime          = "python3.12"
  handler          = "handler.lambda_handler"
  filename         = data.archive_file.reviewer.output_path
  source_code_hash = data.archive_file.reviewer.output_base64sha256
  timeout          = 10
  memory_size      = 256

  environment {
    variables = {
      EXCEPTIONS_TABLE = aws_dynamodb_table.exceptions.name
      AUDIT_TABLE      = aws_dynamodb_table.audit.name
    }
  }

  depends_on = [aws_cloudwatch_log_group.reviewer]
}

# ---------- HTTP API with IAM auth ----------
resource "aws_apigatewayv2_api" "reviewer" {
  name          = "exception-platform-api"
  protocol_type = "HTTP"
}

resource "aws_apigatewayv2_integration" "reviewer" {
  api_id                 = aws_apigatewayv2_api.reviewer.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.reviewer.invoke_arn
  payload_format_version = "2.0"
}

locals {
  review_routes = ["GET /exceptions", "GET /audit", "POST /review"]
}

resource "aws_apigatewayv2_route" "reviewer" {
  for_each           = toset(local.review_routes)
  api_id             = aws_apigatewayv2_api.reviewer.id
  route_key          = each.value
  target             = "integrations/${aws_apigatewayv2_integration.reviewer.id}"
  authorization_type = "AWS_IAM"
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.reviewer.id
  name        = "$default"
  auto_deploy = true

  default_route_settings {
    throttling_burst_limit = 10
    throttling_rate_limit  = 5
  }
}

resource "aws_lambda_permission" "apigw" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.reviewer.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.reviewer.execution_arn}/*/*"
}

output "api_url" {
  value = aws_apigatewayv2_api.reviewer.api_endpoint
}

# ---------- Alerting ----------
resource "aws_sns_topic" "alerts" {
  name = "exception-platform-alerts"
}

resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

resource "aws_cloudwatch_metric_alarm" "dlq_not_empty" {
  alarm_name          = "exception-platform-dlq-not-empty"
  alarm_description   = "A file failed processing 3 times and is in the dead-letter queue"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.dlq.name }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "queue_backlog" {
  alarm_name          = "exception-platform-queue-backlog"
  alarm_description   = "Oldest message in the upload queue has waited over 10 minutes"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateAgeOfOldestMessage"
  dimensions          = { QueueName = aws_sqs_queue.uploads.name }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 5
  threshold           = 600
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
}

locals {
  monitored_lambdas = {
    processor = aws_lambda_function.processor.function_name
    reviewer  = aws_lambda_function.reviewer.function_name
  }
}

resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  for_each            = local.monitored_lambdas
  alarm_name          = "exception-platform-${each.key}-errors"
  alarm_description   = "The ${each.key} Lambda crashed, timed out or failed to start"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = each.value }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
}