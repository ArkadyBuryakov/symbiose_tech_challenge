"""Configuration must fail fast and loudly, not half-start."""

from __future__ import annotations

import pytest

from pmp_common.config import DbSettings, KafkaSettings, S3Settings, read_secret_file


def test_db_password_is_required_in_password_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DB_PASSWORD", raising=False)
    monkeypatch.setenv("DB_AUTH", "password")

    with pytest.raises(ValueError, match="DB_PASSWORD is required"):
        DbSettings()


def test_iam_mode_needs_no_password(monkeypatch: pytest.MonkeyPatch) -> None:
    """The AWS switch is configuration only: DB_AUTH=iam and the password goes away."""
    monkeypatch.delenv("DB_PASSWORD", raising=False)
    monkeypatch.setenv("DB_AUTH", "iam")

    assert DbSettings().auth == "iam"


def test_db_url_percent_encodes_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_PASSWORD", "p@ss/word:1")
    monkeypatch.setenv("DB_USER", "svc user")

    url = DbSettings().url(driver="psycopg", password="p@ss/word:1")

    assert url.startswith("postgresql+psycopg://svc%20user:p%40ss%2Fword%3A1@")
    assert url.endswith("/pmtiles")


def test_db_url_without_a_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_AUTH", "iam")
    assert "@postgres:5432" in DbSettings().url(driver="psycopg", password=None)


def test_s3_static_credentials_must_be_set_together(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "only-half")
    monkeypatch.delenv("S3_SECRET_ACCESS_KEY", raising=False)

    with pytest.raises(ValueError, match="must be set together"):
        S3Settings()


def test_s3_defaults_to_the_aws_credential_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("S3_ENDPOINT", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY"):
        monkeypatch.delenv(var, raising=False)

    settings = S3Settings()

    assert settings.endpoint is None
    assert settings.access_key_id is None


def test_kafka_sasl_mechanism_is_constrained(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KAFKA_SASL_MECHANISM", "plain")

    with pytest.raises(ValueError):
        KafkaSettings()


def test_read_secret_file_reports_the_path(tmp_path: object) -> None:
    with pytest.raises(ValueError, match="cannot read internal JWT key"):
        read_secret_file("/nonexistent/key.pem", what="internal JWT key")


def test_read_secret_file_rejects_an_empty_file(tmp_path) -> None:  # type: ignore[no-untyped-def]
    empty = tmp_path / "empty.pem"
    empty.write_text("   \n")

    with pytest.raises(ValueError, match="is empty"):
        read_secret_file(empty, what="secret")
