#!/usr/bin/env bash
# Create the platform's Kafka topics. Idempotent: re-running is a no-op.
#
# The demo AWS deployment runs this same script against its in-cluster broker
# (deploy/helm/kafka). Against MSK only the authentication flags would change.
set -euo pipefail

BROKERS="${KAFKA_BROKERS:-kafka:9092}"
PARTITIONS=6

# Single-broker dev cluster, so RF=1. On MSK this is 3.
REPLICAS="${TOPIC_REPLICAS:-1}"
# Topic names are constants shared with the services (pmp_common.kafka).

create_topic() {
    local name="$1" partitions="$2" retention_ms="$3"
    if rpk topic describe "$name" --brokers "$BROKERS" >/dev/null 2>&1; then
        echo "topic '$name' already exists"
        return 0
    fi
    echo "creating topic '$name' (partitions=$partitions, rf=$REPLICAS)"
    rpk topic create "$name" \
        --brokers "$BROKERS" \
        --partitions "$partitions" \
        --replicas "$REPLICAS" \
        --topic-config "retention.ms=$retention_ms" \
        --topic-config "cleanup.policy=delete" \
        --topic-config "min.insync.replicas=1"
}

# Partitioned by dataset_id so all work for one dataset is ordered and lands on
# the same consumer, which keeps the per-dataset row lock uncontended.
create_topic publication.requested "$PARTITIONS" 604800000   # 7 days
create_topic publication.results   "$PARTITIONS" 604800000   # 7 days
# Dead letters are kept longer: they exist to be inspected and replayed by hand.
create_topic publication.requested.dlq "$PARTITIONS" 2592000000  # 30 days

echo "--- topics ---"
rpk topic list --brokers "$BROKERS"
echo "kafka-init: done"
