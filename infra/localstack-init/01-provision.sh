#!/usr/bin/env bash
# LocalStack initialisation script – runs once after LocalStack is ready.
# Creates the S3 bucket, SQS queues (standard + FIFO), DynamoDB tables, and
# EventBridge bus used by the local Docker Compose environment.

set -euo pipefail

REGION=us-east-1
ENDPOINT=http://localhost:4566
AWS="aws --endpoint-url=$ENDPOINT --region=$REGION"

echo "==> Creating S3 bucket: reviewlens-local"
$AWS s3api create-bucket --bucket reviewlens-local 2>/dev/null || true

echo "==> Creating SQS queues"
$AWS sqs create-queue --queue-name check-queue 2>/dev/null || true
$AWS sqs create-queue --queue-name check-queue-dlq 2>/dev/null || true
# ContentBasedDeduplication MUST be enabled: the application enqueues to the
# processing FIFO queue with a MessageGroupId only (one group per dataset) and
# relies on content-based dedup per group (see app.core.queue.enqueue and the
# design's "FIFO ... dedup is content-based per group"). Without it SQS rejects
# every SendMessage that omits an explicit MessageDeduplicationId.
$AWS sqs create-queue --queue-name processing-queue.fifo \
    --attributes FifoQueue=true,ContentBasedDeduplication=true 2>/dev/null || true
$AWS sqs create-queue --queue-name processing-queue-dlq.fifo \
    --attributes FifoQueue=true,ContentBasedDeduplication=true 2>/dev/null || true
$AWS sqs create-queue --queue-name push-queue 2>/dev/null || true
$AWS sqs create-queue --queue-name push-queue-dlq 2>/dev/null || true

echo "==> Creating DynamoDB tables"
$AWS dynamodb create-table \
    --table-name rate-limits \
    --attribute-definitions AttributeName=PK,AttributeType=S \
    --key-schema AttributeName=PK,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST \
    --stream-specification StreamEnabled=false 2>/dev/null || true

# Composite key (check_id HASH + item_id RANGE) to match infra/lib/data-stack.ts:
# one row per check item under the shared check_id partition.
$AWS dynamodb create-table \
    --table-name check-sessions \
    --attribute-definitions \
        AttributeName=check_id,AttributeType=S \
        AttributeName=item_id,AttributeType=S \
    --key-schema \
        AttributeName=check_id,KeyType=HASH \
        AttributeName=item_id,KeyType=RANGE \
    --billing-mode PAY_PER_REQUEST 2>/dev/null || true
$AWS dynamodb update-time-to-live \
    --table-name check-sessions \
    --time-to-live-specification "Enabled=true,AttributeName=ttl" 2>/dev/null || true

$AWS dynamodb create-table \
    --table-name ws-connections \
    --attribute-definitions AttributeName=connection_id,AttributeType=S \
    --key-schema AttributeName=connection_id,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST 2>/dev/null || true

echo "==> Creating EventBridge bus: reviewlens-events"
$AWS events create-event-bus --name reviewlens-events 2>/dev/null || true

echo "==> LocalStack provisioning complete"

# Write a sentinel the compose healthcheck polls, so dependents that wait
# for `localstack: service_healthy` only start AFTER every queue/table/bus
# above exists. Without this, LocalStack reports "running" before these
# init scripts finish, and a consumer can call ReceiveMessage on a
# not-yet-created queue (QueueDoesNotExist) and crash on startup.
touch /tmp/localstack-ready
