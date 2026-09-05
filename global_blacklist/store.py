from __future__ import annotations

import os
import re
from pathlib import Path


GLOBAL_BLACKLIST_ENV_KEY = 'BOT_BLACKLIST_IDS'
OWNER_ENV_KEYS = ('BOT_OWNER_DISCORD_ID', 'ATRI_OWNER_DISCORD_ID')
DEFAULT_OWNER_DISCORD_ID = 0
ID_PATTERN = re.compile(r'\d+')


def parse_user_ids(raw: str) -> set[int]:
    user_ids: set[int] = set()
    for token in ID_PATTERN.findall(raw):
        try:
            user_ids.add(int(token))
        except ValueError:
            continue
    return user_ids


def serialize_user_ids(user_ids: set[int]) -> str:
    return ','.join(str(user_id) for user_id in sorted(user_ids))


def read_global_blacklist_ids() -> set[int]:
    return parse_user_ids(os.getenv(GLOBAL_BLACKLIST_ENV_KEY, ''))


def read_owner_discord_id() -> int:
    for key in OWNER_ENV_KEYS:
        raw = os.getenv(key, '').strip()
        if not raw:
            continue
        try:
            owner_id = int(raw)
            if owner_id > 0:
                return owner_id
        except ValueError:
            continue
    return DEFAULT_OWNER_DISCORD_ID


def is_global_blacklisted(bot: object, user_id: int | None) -> bool:
    if user_id is None:
        return False
    user_ids = getattr(bot, 'global_blacklist_ids', set())
    return user_id in user_ids


def persist_global_blacklist_ids(project_root: Path, user_ids: set[int]) -> None:
    serialized = serialize_user_ids(user_ids)
    os.environ[GLOBAL_BLACKLIST_ENV_KEY] = serialized
    env_path = project_root / '.env'

    if env_path.exists():
        lines = env_path.read_text(encoding='utf-8-sig', errors='replace').splitlines()
    else:
        lines = []

    updated = False
    next_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(f'{GLOBAL_BLACKLIST_ENV_KEY}='):
            next_lines.append(f'{GLOBAL_BLACKLIST_ENV_KEY}={serialized}')
            updated = True
        else:
            next_lines.append(line)

    if not updated:
        if next_lines and next_lines[-1].strip():
            next_lines.append('')
        next_lines.append(f'{GLOBAL_BLACKLIST_ENV_KEY}={serialized}')

    env_path.write_text('\n'.join(next_lines) + '\n', encoding='utf-8')
