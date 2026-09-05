from __future__ import annotations

import json
import hashlib
import re
from pathlib import Path
from typing import Any

from .models import (
    ArtistString,
    CharacterProfile,
    DrawPreset,
    LastGeneration,
    UserDrawProfile,
    utc_now_text,
)

DEFAULT_PRESET = DrawPreset(
    name="default",
    positive_prefix="",
    negative_prompt=(
        "lowres, artistic error, film grain, scan artifacts, worst quality, "
        "bad quality, jpeg artifacts, very displeasing, chromatic aberration, "
        "dithering, halftone, screentone, multiple views, logo, too many watermarks, "
        "negative space, blank page, bad anatomy, bad hands, extra digits"
    ),
    params={},
)

_ARTIST_PREFIX_RE = re.compile(r"\bartists?\s*:\s*", re.IGNORECASE)
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


def normalize_key(value: str) -> str:
    lowered = value.casefold().strip()
    lowered = re.sub(r"\s+", " ", lowered)
    return lowered


def normalize_novelai_prompt_syntax(content: str) -> str:
    """Preserve NovelAI syntax and convert accidental SD-style weights back."""
    text = str(content or "").strip()
    if not text:
        return ""

    text = _ARTIST_PREFIX_RE.sub("artist:", text)
    text = _convert_sd_weight_segments_to_nai(text)
    text = re.sub(r"\s*,\s*", ", ", text)
    text = re.sub(r",\s*,+", ", ", text)
    return text.strip(" ,")


def normalize_artist_string_content(content: str) -> str:
    return normalize_novelai_prompt_syntax(content)


def _convert_sd_weight_segments_to_nai(text: str) -> str:
    result: list[str] = []
    index = 0
    while index < len(text):
        if text[index] != "(":
            result.append(text[index])
            index += 1
            continue

        end = _find_matching_paren(text, index)
        if end < 0:
            result.append(text[index])
            index += 1
            continue

        original = text[index : end + 1]
        converted = _convert_single_sd_weight_segment(text[index + 1 : end])
        result.append(converted or original)
        index = end + 1
    return "".join(result)


def _find_matching_paren(text: str, start: int) -> int:
    depth = 0
    for index in range(start, len(text)):
        char = text[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
    return -1


def _convert_single_sd_weight_segment(inner: str) -> str:
    tag, separator, weight = inner.rpartition(":")
    if not separator:
        return ""
    tag = tag.strip()
    weight = weight.strip()
    if not tag or not _NUMBER_RE.fullmatch(weight):
        return ""
    return f"{weight}::{tag} ::"


class DrawStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _load_payload(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"version": 3, "users": {}, "artist_users": {}, "characters": {}}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"version": 3, "users": {}, "artist_users": {}, "characters": {}}
        if not isinstance(raw, dict):
            return {"version": 3, "users": {}, "artist_users": {}, "characters": {}}
        raw.setdefault("version", 1)
        raw.setdefault("users", {})
        raw.setdefault("artist_users", {})
        raw.setdefault("characters", {})
        if not isinstance(raw["users"], dict):
            raw["users"] = {}
        if not isinstance(raw["characters"], dict):
            raw["characters"] = {}
        if not isinstance(raw["artist_users"], dict):
            raw["artist_users"] = {}
        return raw

    def _save_payload(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def get_user(self, user_id: int, scope_key: str) -> UserDrawProfile:
        payload = self._load_payload()
        users = payload.get("users", {})
        storage_key = self._profile_storage_key(user_id, scope_key)
        raw_user = users.get(storage_key) if isinstance(users, dict) else None
        if isinstance(raw_user, dict):
            profile = UserDrawProfile.from_dict(user_id, raw_user)
        else:
            profile = UserDrawProfile(user_id=user_id)
        artist_profile = self._get_global_artist_profile(payload, user_id)
        profile.artist_strings = artist_profile.artist_strings
        profile.active_artist = artist_profile.active_artist
        self.ensure_default_preset(profile)
        for artist in profile.artist_strings.values():
            artist.content = normalize_artist_string_content(artist.content)
        return profile

    def save_user(self, profile: UserDrawProfile, scope_key: str) -> None:
        self.ensure_default_preset(profile)
        payload = self._load_payload()
        payload["version"] = max(int(payload.get("version") or 1), 3)
        users = payload.setdefault("users", {})
        scoped_payload = profile.to_dict()
        scoped_payload["active_artist"] = ""
        scoped_payload["artist_strings"] = {}
        users[self._profile_storage_key(profile.user_id, scope_key)] = scoped_payload
        artist_users = payload.setdefault("artist_users", {})
        artist_users[str(profile.user_id)] = {
            "active_artist": profile.active_artist,
            "artist_strings": {
                name: artist.to_dict()
                for name, artist in profile.artist_strings.items()
            },
        }
        self._save_payload(payload)

    def _get_global_artist_profile(
        self,
        payload: dict[str, Any],
        user_id: int,
    ) -> UserDrawProfile:
        artist_users = payload.get("artist_users")
        raw_global = (
            artist_users.get(str(user_id))
            if isinstance(artist_users, dict)
            else None
        )
        if isinstance(raw_global, dict):
            return UserDrawProfile.from_dict(user_id, raw_global)

        # Version 1/2 stored artist strings inside every conversation profile.
        # Merge those records lazily so existing saved strings immediately act
        # as user-wide settings without exposing any conversation text.
        merged = UserDrawProfile(user_id=user_id)
        users = payload.get("users")
        suffix = f":user_{int(user_id)}"
        if not isinstance(users, dict):
            return merged
        for storage_key, raw_profile in users.items():
            if not isinstance(storage_key, str) or not isinstance(raw_profile, dict):
                continue
            if not (storage_key.endswith(suffix) or storage_key == str(user_id)):
                continue
            legacy = UserDrawProfile.from_dict(user_id, raw_profile)
            for name, artist in legacy.artist_strings.items():
                existing = merged.artist_strings.get(name)
                if existing is None or artist.updated_at >= existing.updated_at:
                    merged.artist_strings[name] = artist
            if legacy.active_artist in merged.artist_strings:
                merged.active_artist = legacy.active_artist
        if merged.active_artist not in merged.artist_strings:
            merged.active_artist = ""
        return merged

    @staticmethod
    def _scope_namespace(scope_key: str) -> str:
        normalized = str(scope_key or "").strip()
        if not normalized:
            raise ValueError("draw memory scope cannot be empty")
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]

    @classmethod
    def _profile_storage_key(cls, user_id: int, scope_key: str) -> str:
        return f"scope_{cls._scope_namespace(scope_key)}:user_{int(user_id)}"

    def ensure_default_preset(self, profile: UserDrawProfile) -> None:
        if "default" not in profile.presets:
            profile.presets["default"] = DEFAULT_PRESET
        if profile.default_preset not in profile.presets:
            profile.default_preset = "default"

    def upsert_artist_string(
        self,
        user_id: int,
        scope_key: str,
        *,
        name: str,
        content: str,
        make_active: bool = True,
    ) -> ArtistString:
        profile = self.get_user(user_id, scope_key)
        safe_name = normalize_key(name) or "default"
        artist = ArtistString(
            name=safe_name,
            content=normalize_artist_string_content(content),
            updated_at=utc_now_text(),
        )
        profile.artist_strings[safe_name] = artist
        if make_active:
            profile.active_artist = safe_name
        self.save_user(profile, scope_key)
        return artist

    def upsert_preset(
        self,
        user_id: int,
        scope_key: str,
        *,
        name: str,
        positive_prefix: str,
        negative_prompt: str = "",
        params: dict[str, Any] | None = None,
        make_default: bool = False,
    ) -> DrawPreset:
        profile = self.get_user(user_id, scope_key)
        safe_name = normalize_key(name) or "default"
        preset = DrawPreset(
            name=safe_name,
            positive_prefix=positive_prefix.strip(),
            negative_prompt=negative_prompt.strip(),
            params=dict(params or {}),
            updated_at=utc_now_text(),
        )
        profile.presets[safe_name] = preset
        if make_default:
            profile.default_preset = safe_name
        self.save_user(profile, scope_key)
        return preset

    def set_default_preset(self, user_id: int, scope_key: str, preset_name: str) -> bool:
        profile = self.get_user(user_id, scope_key)
        safe_name = normalize_key(preset_name)
        if safe_name not in profile.presets:
            return False
        profile.default_preset = safe_name
        self.save_user(profile, scope_key)
        return True

    def set_active_artist(self, user_id: int, scope_key: str, artist_name: str) -> bool:
        profile = self.get_user(user_id, scope_key)
        safe_name = normalize_key(artist_name)
        if safe_name not in profile.artist_strings:
            return False
        profile.active_artist = safe_name
        self.save_user(profile, scope_key)
        return True

    def save_last_generation(
        self,
        user_id: int,
        scope_key: str,
        generation: LastGeneration,
    ) -> None:
        profile = self.get_user(user_id, scope_key)
        profile.last_generation = generation
        self.save_user(profile, scope_key)

    def get_last_generation(
        self,
        user_id: int,
        scope_key: str,
    ) -> LastGeneration | None:
        profile = self.get_user(user_id, scope_key)
        scoped = profile.scoped_last_generations.get(scope_key)
        if scoped is not None:
            return scoped
        return None

    def save_scoped_last_generation(
        self,
        user_id: int,
        scope_key: str,
        generation: LastGeneration,
    ) -> None:
        profile = self.get_user(user_id, scope_key)
        profile.scoped_last_generations[scope_key] = generation
        profile.last_generation = generation
        self.save_user(profile, scope_key)

    def find_character(self, name: str, scope_key: str) -> CharacterProfile | None:
        key = normalize_key(name)
        if not key:
            return None
        payload = self._load_payload()
        characters = payload.get("characters", {})
        namespace = self._scope_namespace(scope_key)
        scoped_key = f"scope_{namespace}:{key}"
        raw = characters.get(scoped_key) if isinstance(characters, dict) else None
        if isinstance(raw, dict):
            return CharacterProfile.from_dict(raw)
        scoped_prefix = f"scope_{namespace}:"
        candidates = (
            (
                candidate
                for candidate_key, candidate in characters.items()
                if str(candidate_key).startswith(scoped_prefix)
            )
            if isinstance(characters, dict)
            else []
        )
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            profile = CharacterProfile.from_dict(candidate)
            aliases = [profile.name, *profile.aliases]
            if any(normalize_key(alias) == key for alias in aliases):
                return profile
        return None

    def save_character(self, profile: CharacterProfile, scope_key: str) -> None:
        key = normalize_key(profile.name)
        if not key:
            return
        payload = self._load_payload()
        payload["version"] = max(int(payload.get("version") or 1), 2)
        characters = payload.setdefault("characters", {})
        scoped_prefix = f"scope_{self._scope_namespace(scope_key)}:"
        characters[f"{scoped_prefix}{key}"] = profile.to_dict()
        for alias in profile.aliases:
            alias_key = normalize_key(alias)
            if alias_key:
                characters.setdefault(f"{scoped_prefix}{alias_key}", profile.to_dict())
        self._save_payload(payload)
