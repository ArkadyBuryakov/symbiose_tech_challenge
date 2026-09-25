"""Worker configuration."""

from __future__ import annotations

import os
import socket

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from pmp_common.config import DbSettings, KafkaSettings, S3Settings, ServiceSettings

__all__ = ["ChaosSettings", "WorkerSettings", "get_settings"]


class ChaosSettings(BaseSettings):
    """Deliberate failure injection, for demonstrating the recovery paths.

    Every switch is off by default and logged loudly when on. They exist so the
    at-least-once guarantees can be *shown* rather than asserted — see the
    ``chaos-*`` Make targets.
    """

    model_config = SettingsConfigDict(env_prefix="chaos_", extra="ignore", frozen=True)

    crash_after_copy: bool = Field(
        default=False,
        description="Exit hard after copying to the publish bucket but before the DB commit.",
    )
    crash_before_offset_commit: bool = Field(
        default=False,
        description="Exit hard after the DB commit but before committing the Kafka offset.",
    )
    delay_ms: int = Field(default=0, ge=0, description="Sleep this long inside each job.")

    @property
    def any_enabled(self) -> bool:
        return self.crash_after_copy or self.crash_before_offset_commit or self.delay_ms > 0


class WorkerSettings(ServiceSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    service_name: str = "worker"
    # The worker serves only probes and metrics, never application traffic.
    http_port: int = Field(default=9100, validation_alias="HTTP_PORT")

    db: DbSettings = Field(default_factory=DbSettings)
    kafka: KafkaSettings = Field(default_factory=KafkaSettings)
    s3: S3Settings = Field(default_factory=S3Settings)
    chaos: ChaosSettings = Field(default_factory=ChaosSettings)

    # --- job execution ----------------------------------------------------
    worker_id: str = Field(
        default_factory=lambda: os.environ.get("HOSTNAME") or socket.gethostname(),
        validation_alias="WORKER_ID",
        description="Identifies the lease holder; must be unique per process.",
    )
    lease_seconds: int = Field(
        default=60,
        ge=10,
        validation_alias="WORKER_LEASE_SECONDS",
        description=(
            "How long a claim is trusted without a heartbeat. Kept short because "
            "LeaseHeartbeat renews it while the job runs, so this is only the "
            "delay before a *dead* worker's job is recovered. (KAFKA_MAX_POLL_INTERVAL_MS "
            "must instead exceed the longest job including its retry backoffs.)"
        ),
    )
    max_attempts: int = Field(default=5, ge=1, le=50, validation_alias="WORKER_MAX_ATTEMPTS")
    backoff_base_seconds: float = Field(
        default=2.0, ge=0.1, validation_alias="WORKER_BACKOFF_BASE_SECONDS"
    )
    backoff_max_seconds: float = Field(
        default=60.0, ge=1.0, validation_alias="WORKER_BACKOFF_MAX_SECONDS"
    )
    poll_timeout_seconds: float = Field(
        default=1.0, ge=0.1, validation_alias="WORKER_POLL_TIMEOUT_SECONDS"
    )
    shutdown_grace_seconds: float = Field(
        default=25.0, ge=1.0, validation_alias="WORKER_SHUTDOWN_GRACE_SECONDS"
    )
    hash_chunk_bytes: int = Field(
        default=8 * 1024 * 1024, ge=64 * 1024, validation_alias="WORKER_HASH_CHUNK_BYTES"
    )

    # --- background loops -------------------------------------------------
    outbox_interval_seconds: float = Field(
        default=1.0, ge=0.1, validation_alias="WORKER_OUTBOX_INTERVAL_SECONDS"
    )
    outbox_batch: int = Field(default=100, ge=1, le=1000, validation_alias="WORKER_OUTBOX_BATCH")
    reconciler_interval_seconds: float = Field(
        default=30.0, ge=1.0, validation_alias="WORKER_RECONCILER_INTERVAL_SECONDS"
    )
    stuck_pending_seconds: int = Field(
        default=60, ge=5, validation_alias="WORKER_STUCK_PENDING_SECONDS"
    )
    reconciler_batch: int = Field(
        default=50, ge=1, le=500, validation_alias="WORKER_RECONCILER_BATCH"
    )


def get_settings() -> WorkerSettings:
    return WorkerSettings()
