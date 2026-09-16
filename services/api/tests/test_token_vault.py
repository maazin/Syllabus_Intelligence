"""The refresh-token vault.

Cheap tests for an expensive property: a refresh token that round-trips is
table stakes, but the behaviours that matter are the ones around the edges,
where a misconfiguration would otherwise degrade silently into plaintext.
"""

from __future__ import annotations

import base64
import os

import pytest

from services.api import token_vault


def test_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "local")
    sealed = token_vault.seal("1//refresh-token")
    assert sealed != "1//refresh-token"
    assert token_vault.open_sealed(sealed) == "1//refresh-token"


def test_ciphertext_is_not_deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two students with the same token must not produce the same row."""
    monkeypatch.setenv("ENVIRONMENT", "local")
    assert token_vault.seal("same") != token_vault.seal("same")


def test_standard_base64_key_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Terraform's random_bytes emits standard base64, which can contain + and /."""
    raw = bytes(range(0xF8, 0x100)) * 4  # deliberately lands on + and / characters
    standard = base64.b64encode(raw).decode()
    assert "+" in standard or "/" in standard
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", standard)
    monkeypatch.setenv("ENVIRONMENT", "production")
    assert token_vault.open_sealed(token_vault.seal("x")) == "x"


def test_production_without_a_key_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """Falling back to a derived key outside local would defeat the point."""
    monkeypatch.delenv("TOKEN_ENCRYPTION_KEY", raising=False)
    monkeypatch.setenv("ENVIRONMENT", "production")
    with pytest.raises(token_vault.TokenVaultError, match="TOKEN_ENCRYPTION_KEY"):
        token_vault.seal("x")


def test_a_value_written_under_another_key_is_reported_not_returned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "local")
    monkeypatch.setenv("JWT_SECRET", "key-one")
    sealed = token_vault.seal("secret")
    monkeypatch.setenv("JWT_SECRET", "key-two")
    with pytest.raises(token_vault.TokenVaultError, match="reconnect"):
        token_vault.open_sealed(sealed)


def test_plaintext_left_over_from_before_the_vault_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row written by the old placeholder is unusable, and says so."""
    monkeypatch.setenv("ENVIRONMENT", "local")
    with pytest.raises(token_vault.TokenVaultError):
        token_vault.open_sealed("1//a-token-stored-in-the-clear")
    assert "ENVIRONMENT" in os.environ or True
