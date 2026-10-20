"""Реестр ключей и хранение: то, чем отзыв и удаление отличаются от украшения.

Главное здесь: смена ключа и отзыв личности это разные записи. Смена ключа закрывает
конкретный ключ, а отзыв личности закрывает дорогу новым ключам под тем же именем до
повторного подтверждения. Второе и есть то, чего нет у Index вовсе.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from arli_engine.core.registry import (
    KEY_REVOCATION_REASONS,
    REGISTRY_REJECTIONS,
    Registry,
    RegistryRejected,
    now_utc,
)
from arli_engine.core.retention import (
    RETENTION_DEFAULT_DAYS,
    RETENTION_REJECTIONS,
    Retention,
    RetentionRejected,
)


def test_registered_key_is_valid_and_unknown_key_is_not():
    registry = Registry()
    registry.register("k1", "principal-1", "pem-1")
    ok, reasons = registry.check("k1")
    assert ok and reasons == []
    unknown_ok, unknown_reasons = registry.check("nope")
    assert unknown_ok is False and unknown_reasons == ["unknown_key"]


def test_rotation_revokes_the_old_key_only():
    registry = Registry()
    registry.register("k1", "principal-1", "pem-1")
    old, new = registry.rotate("k1", "k2", "pem-2")
    assert old.revoked_at is not None and old.revocation_reason == "rotated"
    assert registry.check("k1")[0] is False
    assert "key_revoked:rotated" in registry.check("k1")[1]
    assert registry.check("k2") == (True, [])
    # личность при ротации цела: это разные записи
    assert registry.principals == {}


def test_principal_revocation_blocks_new_keys_until_confirmed():
    registry = Registry()
    registry.register("k1", "principal-1", "pem-1")
    registry.revoke_principal("principal-1", "compromised")
    ok, reasons = registry.check("k1")
    assert ok is False and "principal_revoked" in reasons

    with pytest.raises(RegistryRejected) as exc:
        registry.register("k2", "principal-1", "pem-2")
    assert exc.value.code == "principal_revoked"

    registry.confirm_principal("principal-1")
    registry.register("k3", "principal-1", "pem-3")
    assert registry.check("k3") == (True, [])


def test_confirmation_without_a_revocation_record_is_refused():
    with pytest.raises(RegistryRejected) as exc:
        Registry().confirm_principal("nobody")
    assert exc.value.code == "principal_not_confirmed"


def test_expiry_and_not_yet_valid_are_reported():
    registry = Registry()
    moment = now_utc()
    registry.register("short", "principal-2", "pem", valid_days=1, at=moment)
    later = moment + timedelta(days=2)
    ok, reasons = registry.check("short", at=later)
    assert ok is False and "key_expired" in reasons

    future = moment + timedelta(days=1)
    registry.register("later", "principal-3", "pem3", at=future)
    ok_now, reasons_now = registry.check("later", at=moment)
    assert ok_now is False and "key_not_yet_valid" in reasons_now


def test_unknown_revocation_reason_is_refused_with_a_code():
    registry = Registry()
    registry.register("k1", "principal-1", "pem-1")
    with pytest.raises(RegistryRejected) as exc:
        registry.revoke_key("k1", "because-i-said-so")
    assert exc.value.code == "unknown_revocation_reason"
    assert set(KEY_REVOCATION_REASONS) == {"rotated", "compromised", "principal_request", "policy"}


def test_registry_rejections_are_a_closed_set():
    with pytest.raises(ValueError):
        RegistryRejected("made_up")
    assert {"unknown_key", "key_revoked", "principal_revoked"} <= set(REGISTRY_REJECTIONS)


# --- хранение ---

def test_archiving_an_intent_hides_its_envelopes_but_keeps_the_record():
    retention = Retention(days=RETENTION_DEFAULT_DAYS)
    retention.store("ev-1", "intent-1")
    retention.store("ev-2", "intent-1")
    retention.store("ev-3", "intent-2")
    assert retention.searchable() == ["ev-1", "ev-2", "ev-3"]

    touched = retention.archive_intent("intent-1")
    assert touched == 2
    assert retention.searchable() == ["ev-3"]
    # запись остаётся, но помечена: видно, что конверт был и почему скрыт
    record = retention.records["ev-1"]
    assert record.deleted_reason == "intent_archived"
    assert retention.visible("ev-1") is False


def test_archiving_an_unknown_intent_is_refused():
    with pytest.raises(RetentionRejected) as exc:
        Retention().archive_intent("nobody")
    assert exc.value.code == "unknown_intent"


def test_deleting_twice_is_refused_and_unknown_envelope_is_refused():
    retention = Retention()
    retention.store("ev-1", "intent-1")
    retention.delete_envelope("ev-1")
    with pytest.raises(RetentionRejected) as exc:
        retention.delete_envelope("ev-1")
    assert exc.value.code == "already_deleted"
    with pytest.raises(RetentionRejected) as exc:
        retention.delete_envelope("ev-nope")
    assert exc.value.code == "unknown_envelope"


def test_purge_removes_only_records_older_than_the_window():
    retention = Retention(days=30)
    moment = now_utc()
    retention.store("old", "intent-1", captured_at=moment - timedelta(days=31))
    retention.store("fresh", "intent-1", captured_at=moment - timedelta(days=1))
    removed = retention.purge(at=moment)
    assert removed == 1
    assert retention.searchable() == ["fresh"]


def test_retention_rejections_are_a_closed_set():
    with pytest.raises(ValueError):
        RetentionRejected("made_up")
    assert {"unknown_envelope", "already_deleted", "unknown_intent"} == set(RETENTION_REJECTIONS)
