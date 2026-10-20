"""Key registry: rotation and revocation, with principal revocation separate from key change.

Why it has to work exactly like this and not more simply: Index has no revocation
field at all in the code, and rotating a key there just makes the card invalid. So a
principal is never revoked there, and after a leak a forged card lives on with the old
key until a manual cleanup.

There are two separate records here:

- a key record: when it was created, when it is valid, when it was revoked and why;
- a principal record: whether it was revoked, when and why.

Principal revocation blocks new keys under the same identifier until a fresh
confirmation. Without that, revocation would be decoration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

# Key revocation reasons. A closed list: a revocation record either carries one of
# these codes or is not created at all.
KEY_REVOCATION_REASONS = ("rotated", "compromised", "principal_request", "policy")

REGISTRY_REJECTIONS = (
    "unknown_key",
    "key_revoked",
    "key_expired",
    "key_not_yet_valid",
    "principal_revoked",
    "principal_not_confirmed",
    "unknown_revocation_reason",
)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class RegistryRejected(RuntimeError):
    """A registry operation was rejected. The code is from the closed list REGISTRY_REJECTIONS."""

    def __init__(self, code: str, detail: str = "") -> None:
        if code not in REGISTRY_REJECTIONS:
            raise ValueError(f"{code!r} is not in REGISTRY_REJECTIONS")
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass
class KeyRecord:
    kid: str
    principal_id: str
    public_pem: str
    created_at: datetime
    valid_from: datetime
    valid_until: datetime | None = None
    revoked_at: datetime | None = None
    revocation_reason: str | None = None

    def to_json(self) -> dict:
        return {
            "kid": self.kid,
            "principal_id": self.principal_id,
            "created_at": self.created_at.isoformat(),
            "valid_from": self.valid_from.isoformat(),
            "valid_until": self.valid_until.isoformat() if self.valid_until else None,
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else None,
            "revocation_reason": self.revocation_reason,
        }


@dataclass
class PrincipalRevocation:
    """Principal revocation: a separate record, not a side effect of a key change."""

    principal_id: str
    revoked_at: datetime
    reason: str
    confirmed_again_at: datetime | None = None

    @property
    def active(self) -> bool:
        return self.confirmed_again_at is None


@dataclass
class Registry:
    keys: dict[str, KeyRecord] = field(default_factory=dict)
    principals: dict[str, PrincipalRevocation] = field(default_factory=dict)

    # --- keys ---
    def register(
        self,
        kid: str,
        principal_id: str,
        public_pem: str,
        *,
        valid_days: int | None = None,
        at: datetime | None = None,
    ) -> KeyRecord:
        moment = at or now_utc()
        revocation = self.principals.get(principal_id)
        if revocation is not None and revocation.active:
            raise RegistryRejected(
                "principal_revoked", f"{principal_id} was revoked at {revocation.revoked_at.isoformat()}"
            )
        if kid in self.keys:
            raise RegistryRejected("unknown_key", f"key {kid!r} is already registered")
        record = KeyRecord(
            kid=kid,
            principal_id=principal_id,
            public_pem=public_pem,
            created_at=moment,
            valid_from=moment,
            valid_until=moment + timedelta(days=valid_days) if valid_days else None,
        )
        self.keys[kid] = record
        return record

    def rotate(
        self,
        kid: str,
        new_kid: str,
        new_public_pem: str,
        *,
        reason: str = "rotated",
        at: datetime | None = None,
    ) -> tuple[KeyRecord, KeyRecord]:
        """Rotate a key: the old one is revoked, the new one is registered. The principal is untouched."""
        old = self._require_key(kid)
        if reason not in KEY_REVOCATION_REASONS:
            raise RegistryRejected("unknown_revocation_reason", f"reason={reason!r}")
        moment = at or now_utc()
        old.revoked_at = moment
        old.revocation_reason = reason
        new = self.register(new_kid, old.principal_id, new_public_pem, at=moment)
        return old, new

    def revoke_key(self, kid: str, reason: str, *, at: datetime | None = None) -> KeyRecord:
        if reason not in KEY_REVOCATION_REASONS:
            raise RegistryRejected("unknown_revocation_reason", f"reason={reason!r}")
        record = self._require_key(kid)
        record.revoked_at = at or now_utc()
        record.revocation_reason = reason
        return record

    def _require_key(self, kid: str) -> KeyRecord:
        record = self.keys.get(kid)
        if record is None:
            raise RegistryRejected("unknown_key", f"key {kid!r} is not registered")
        return record

    # --- principals ---
    def revoke_principal(self, principal_id: str, reason: str, *, at: datetime | None = None) -> PrincipalRevocation:
        if reason not in KEY_REVOCATION_REASONS:
            raise RegistryRejected("unknown_revocation_reason", f"reason={reason!r}")
        record = PrincipalRevocation(
            principal_id=principal_id, revoked_at=at or now_utc(), reason=reason
        )
        self.principals[principal_id] = record
        return record

    def confirm_principal(self, principal_id: str, *, at: datetime | None = None) -> PrincipalRevocation:
        """A fresh confirmation lifts the revocation: this is a separate action, not automation."""
        record = self.principals.get(principal_id)
        if record is None:
            raise RegistryRejected("principal_not_confirmed", f"{principal_id} has no revocation record")
        record.confirmed_again_at = at or now_utc()
        return record

    # --- checks ---
    def check(self, kid: str, *, at: datetime | None = None) -> tuple[bool, list[str]]:
        """Whether the key is valid now. Returns the result and the reasons for refusal."""
        moment = at or now_utc()
        record = self.keys.get(kid)
        if record is None:
            return False, ["unknown_key"]
        reasons: list[str] = []
        if record.revoked_at is not None:
            reasons.append(f"key_revoked:{record.revocation_reason}")
        if record.valid_from > moment:
            reasons.append("key_not_yet_valid")
        if record.valid_until is not None and record.valid_until <= moment:
            reasons.append("key_expired")
        revocation = self.principals.get(record.principal_id)
        if revocation is not None and revocation.active:
            reasons.append("principal_revoked")
        return (not reasons), reasons

    def to_json(self) -> dict:
        return {
            "keys": [record.to_json() for record in self.keys.values()],
            "principals": [
                {
                    "principal_id": r.principal_id,
                    "revoked_at": r.revoked_at.isoformat(),
                    "reason": r.reason,
                    "active": r.active,
                    "confirmed_again_at": r.confirmed_again_at.isoformat() if r.confirmed_again_at else None,
                }
                for r in self.principals.values()
            ],
        }
