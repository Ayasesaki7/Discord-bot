from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass
class PunishmentRecord:
    punishment_id: str
    action: str
    user_id: int
    user_label: str
    moderator_id: int
    moderator_label: str
    reason: str
    created_at: str
    revoked_at: str | None = None
    revoked_by_id: int | None = None
    revoked_by_label: str | None = None
    revoke_reason: str | None = None


class PunishmentStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> list[PunishmentRecord]:
        if not self.path.is_file():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            return []

        records: list[PunishmentRecord] = []
        if not isinstance(raw, list):
            return records
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                records.append(PunishmentRecord(**item))
            except TypeError:
                continue
        return records

    def save(self, records: list[PunishmentRecord]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = [asdict(record) for record in records]
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + '\n',
            encoding='utf-8',
        )

    def append(self, record: PunishmentRecord) -> None:
        records = self.load()
        records.append(record)
        self.save(records)

    def find_active_ban(self, user_id: int) -> PunishmentRecord | None:
        for record in reversed(self.load()):
            if (
                record.user_id == user_id
                and record.action == 'ban'
                and record.revoked_at is None
            ):
                return record
        return None

    def revoke_active_ban(
        self,
        user_id: int,
        *,
        moderator_id: int,
        moderator_label: str,
        reason: str,
    ) -> PunishmentRecord | None:
        records = self.load()
        for record in reversed(records):
            if (
                record.user_id == user_id
                and record.action == 'ban'
                and record.revoked_at is None
            ):
                record.revoked_at = datetime.now(UTC).isoformat()
                record.revoked_by_id = moderator_id
                record.revoked_by_label = moderator_label
                record.revoke_reason = reason
                self.save(records)
                return record
        return None
