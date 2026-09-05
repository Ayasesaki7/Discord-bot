from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class DrawPreset:
    name: str
    positive_prefix: str = ""
    negative_prompt: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    updated_at: str = field(default_factory=utc_now_text)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DrawPreset":
        params = data.get("params")
        return cls(
            name=str(data.get("name") or "default"),
            positive_prefix=str(data.get("positive_prefix") or ""),
            negative_prompt=str(data.get("negative_prompt") or ""),
            params=dict(params) if isinstance(params, dict) else {},
            updated_at=str(data.get("updated_at") or utc_now_text()),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ArtistString:
    name: str
    content: str
    updated_at: str = field(default_factory=utc_now_text)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ArtistString":
        return cls(
            name=str(data.get("name") or "default"),
            content=str(data.get("content") or ""),
            updated_at=str(data.get("updated_at") or utc_now_text()),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class CharacterProfile:
    name: str
    work: str = ""
    aliases: list[str] = field(default_factory=list)
    traits: list[str] = field(default_factory=list)
    prompt_tags: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    confidence: float = 0.0
    updated_at: str = field(default_factory=utc_now_text)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CharacterProfile":
        return cls(
            name=str(data.get("name") or ""),
            work=str(data.get("work") or ""),
            aliases=[str(item) for item in data.get("aliases", []) if str(item).strip()],
            traits=[str(item) for item in data.get("traits", []) if str(item).strip()],
            prompt_tags=[str(item) for item in data.get("prompt_tags", []) if str(item).strip()],
            sources=[str(item) for item in data.get("sources", []) if str(item).strip()],
            confidence=float(data.get("confidence") or 0.0),
            updated_at=str(data.get("updated_at") or utc_now_text()),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class LastGeneration:
    user_request: str
    prompt: str
    negative_prompt: str
    model: str
    params: dict[str, Any] = field(default_factory=dict)
    preset_name: str = "default"
    artist_names: list[str] = field(default_factory=list)
    character_names: list[str] = field(default_factory=list)
    image_message_url: str = ""
    created_at: str = field(default_factory=utc_now_text)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LastGeneration":
        params = data.get("params")
        return cls(
            user_request=str(data.get("user_request") or ""),
            prompt=str(data.get("prompt") or ""),
            negative_prompt=str(data.get("negative_prompt") or ""),
            model=str(data.get("model") or ""),
            params=dict(params) if isinstance(params, dict) else {},
            preset_name=str(data.get("preset_name") or "default"),
            artist_names=[str(item) for item in data.get("artist_names", []) if str(item).strip()],
            character_names=[str(item) for item in data.get("character_names", []) if str(item).strip()],
            image_message_url=str(data.get("image_message_url") or ""),
            created_at=str(data.get("created_at") or utc_now_text()),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class UserDrawProfile:
    user_id: int
    default_preset: str = "default"
    active_artist: str = ""
    presets: dict[str, DrawPreset] = field(default_factory=dict)
    artist_strings: dict[str, ArtistString] = field(default_factory=dict)
    last_generation: LastGeneration | None = None
    scoped_last_generations: dict[str, LastGeneration] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, user_id: int, data: dict[str, Any]) -> "UserDrawProfile":
        presets_payload = data.get("presets")
        artists_payload = data.get("artist_strings")
        last_payload = data.get("last_generation")
        scoped_last_payload = data.get("scoped_last_generations")
        profile = cls(
            user_id=user_id,
            default_preset=str(data.get("default_preset") or "default"),
            active_artist=str(data.get("active_artist") or ""),
            presets={},
            artist_strings={},
            last_generation=(
                LastGeneration.from_dict(last_payload)
                if isinstance(last_payload, dict)
                else None
            ),
            scoped_last_generations={},
        )
        if isinstance(presets_payload, dict):
            for name, payload in presets_payload.items():
                if isinstance(payload, dict):
                    preset = DrawPreset.from_dict({"name": name, **payload})
                    profile.presets[preset.name] = preset
        if isinstance(artists_payload, dict):
            for name, payload in artists_payload.items():
                if isinstance(payload, dict):
                    artist = ArtistString.from_dict({"name": name, **payload})
                    profile.artist_strings[artist.name] = artist
        if isinstance(scoped_last_payload, dict):
            for scope_key, payload in scoped_last_payload.items():
                if isinstance(payload, dict):
                    profile.scoped_last_generations[str(scope_key)] = LastGeneration.from_dict(payload)
        return profile

    def to_dict(self) -> dict[str, Any]:
        return {
            "default_preset": self.default_preset,
            "active_artist": self.active_artist,
            "presets": {
                name: preset.to_dict()
                for name, preset in self.presets.items()
            },
            "artist_strings": {
                name: artist.to_dict()
                for name, artist in self.artist_strings.items()
            },
            "last_generation": (
                self.last_generation.to_dict()
                if self.last_generation is not None
                else None
            ),
            "scoped_last_generations": {
                scope_key: generation.to_dict()
                for scope_key, generation in self.scoped_last_generations.items()
            },
        }
