"""Worker configuration guards."""

from __future__ import annotations

import pytest

from pmp_worker.settings import ChaosSettings, WorkerSettings


@pytest.fixture(autouse=True)
def _base_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_PASSWORD", "secret")
    for var in (
        "CHAOS_CRASH_AFTER_COPY",
        "CHAOS_CRASH_BEFORE_OFFSET_COMMIT",
        "CHAOS_DELAY_MS",
        "WORKER_LEASE_SECONDS",
    ):
        monkeypatch.delenv(var, raising=False)


def test_chaos_is_off_by_default() -> None:
    chaos = ChaosSettings()

    assert not chaos.any_enabled
    assert not chaos.crash_after_copy
    assert chaos.delay_ms == 0


@pytest.mark.parametrize("value", ["1", "true", "True", "yes", "on"])
def test_chaos_switches_accept_shell_booleans(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("CHAOS_CRASH_AFTER_COPY", value)

    assert ChaosSettings().crash_after_copy is True
    assert ChaosSettings().any_enabled


def test_worker_id_defaults_to_the_container_hostname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOSTNAME", "worker-abc123")

    assert WorkerSettings().worker_id == "worker-abc123"


def test_lease_default_is_short_because_it_is_heartbeaten() -> None:
    """The lease only bounds recovery from a *dead* worker: a live one renews
    it. Keeping it short is what makes crash recovery quick."""
    assert WorkerSettings().lease_seconds <= 120


def test_max_attempts_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKER_MAX_ATTEMPTS", "0")
    with pytest.raises(ValueError):
        WorkerSettings()
