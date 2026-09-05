from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(slots=True)
class FortuneRecord:
    user_id: int
    period_id: str
    generated_at: str
    reset_at: str
    summary: str
    luck_score: int
    sign: str
    omen: str
    lucky_color: str
    lucky_direction: str
    lucky_time: str
    suitable: list[str]
    avoid: list[str]
    poem: str
    detail: str

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "FortuneRecord":
        return cls(
            user_id=int(data["user_id"]),
            period_id=str(data["period_id"]),
            generated_at=str(data["generated_at"]),
            reset_at=str(data["reset_at"]),
            summary=str(data["summary"]),
            luck_score=int(data["luck_score"]),
            sign=str(data["sign"]),
            omen=str(data["omen"]),
            lucky_color=str(data["lucky_color"]),
            lucky_direction=str(data["lucky_direction"]),
            lucky_time=str(data["lucky_time"]),
            suitable=[str(item) for item in data.get("suitable", [])],
            avoid=[str(item) for item in data.get("avoid", [])],
            poem=str(data["poem"]),
            detail=str(data["detail"]),
        )

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class FortuneStorage:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load_all(self) -> dict[int, FortuneRecord]:
        if not self.path.exists():
            return {}

        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

        if not isinstance(raw, dict):
            return {}

        records: dict[int, FortuneRecord] = {}
        for user_id_text, payload in raw.items():
            if not isinstance(payload, dict):
                continue
            try:
                user_id = int(user_id_text)
                records[user_id] = FortuneRecord.from_dict(payload)
            except (KeyError, TypeError, ValueError):
                continue
        return records

    def save_all(self, records: dict[int, FortuneRecord]) -> None:
        payload = {
            str(user_id): record.to_dict()
            for user_id, record in records.items()
        }
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
