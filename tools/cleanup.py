from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


_MUSIC_SUFFIXES = {
    '.mp3', '.m4a', '.webm', '.opus', '.ogg', '.wav', '.flac',
    '.part', '.ytdl', '.tmp',
}
_MAX_CANDIDATES = 10_000


@dataclass(slots=True)
class CleanupProtection:
    protected_music_names: set[str] = field(default_factory=set)
    busy_groups: set[str] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class CleanupCandidate:
    kind: str
    path: Path
    relative_path: str
    size: int
    modified_at: float
    is_directory: bool = False


@dataclass(slots=True)
class CleanupReport:
    candidates: list[CleanupCandidate]
    deleted_count: int = 0
    freed_bytes: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def candidate_bytes(self) -> int:
        return sum(candidate.size for candidate in self.candidates)

    def to_dict(self, *, include_items: int = 50) -> dict[str, object]:
        return {
            'candidateCount': len(self.candidates),
            'candidateBytes': self.candidate_bytes,
            'deletedCount': self.deleted_count,
            'freedBytes': self.freed_bytes,
            'failures': list(self.failures[:20]),
            'items': [
                {
                    'kind': candidate.kind,
                    'path': candidate.relative_path,
                    'bytes': candidate.size,
                    'directory': candidate.is_directory,
                }
                for candidate in self.candidates[:include_items]
            ],
            'truncated': len(self.candidates) > include_items,
        }


class GeneratedArtifactCleaner:
    """Delete only known ATRI-generated cache and rotated-log artifacts."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()
        self.music_root = (self.project_root / 'music_cache').resolve()
        self.bilibili_root = (self.project_root / 'bilibili_cache').resolve()
        self.douyin_root = (self.project_root / 'douyin_cache').resolve()
        self.log_root = (self.project_root / 'logs').resolve()

    def preview(
        self,
        *,
        minimum_age_hours: int = 24,
        protection: CleanupProtection | None = None,
        now: float | None = None,
    ) -> CleanupReport:
        age_hours = min(max(int(minimum_age_hours), 1), 24 * 365)
        cutoff = (time.time() if now is None else float(now)) - age_hours * 3600
        safe = protection or CleanupProtection()
        candidates: list[CleanupCandidate] = []

        if 'music' not in safe.busy_groups:
            candidates.extend(self._music_candidates(cutoff, safe.protected_music_names))
        if 'bilibili' not in safe.busy_groups:
            candidates.extend(
                self._temporary_directory_candidates(
                    root=self.bilibili_root,
                    prefix='bilibili_',
                    kind='bilibili_temp',
                    cutoff=cutoff,
                )
            )
        if 'douyin' not in safe.busy_groups:
            candidates.extend(
                self._temporary_directory_candidates(
                    root=self.douyin_root,
                    prefix='douyin_',
                    kind='douyin_temp',
                    cutoff=cutoff,
                )
            )
        candidates.extend(self._log_candidates(cutoff))
        candidates.sort(key=lambda item: (item.modified_at, item.relative_path))
        return CleanupReport(candidates=candidates[:_MAX_CANDIDATES])

    def clean(
        self,
        *,
        minimum_age_hours: int = 24,
        protection: CleanupProtection | None = None,
        now: float | None = None,
    ) -> CleanupReport:
        report = self.preview(
            minimum_age_hours=minimum_age_hours,
            protection=protection,
            now=now,
        )
        for candidate in report.candidates:
            try:
                if not self._candidate_is_still_safe(candidate):
                    report.failures.append(f'{candidate.relative_path}: safety check changed')
                    continue
                if candidate.is_directory:
                    shutil.rmtree(candidate.path)
                else:
                    candidate.path.unlink(missing_ok=True)
                report.deleted_count += 1
                report.freed_bytes += candidate.size
            except OSError as exc:
                report.failures.append(
                    f'{candidate.relative_path}: {exc.__class__.__name__}'
                )
        return report

    def _music_candidates(
        self,
        cutoff: float,
        protected_names: set[str],
    ) -> list[CleanupCandidate]:
        if not self.music_root.is_dir() or self.music_root.is_symlink():
            return []
        protected = {str(name).casefold() for name in protected_names}
        output: list[CleanupCandidate] = []
        for path in self.music_root.iterdir():
            if not path.is_file() or path.is_symlink():
                continue
            if path.suffix.casefold() not in _MUSIC_SUFFIXES:
                continue
            if path.name.casefold() in protected or path.stem.casefold() in protected:
                continue
            try:
                stats = path.stat()
            except OSError:
                continue
            if stats.st_mtime > cutoff:
                continue
            output.append(self._candidate('music_cache', path, stats.st_size, stats.st_mtime))
        return output

    def _temporary_directory_candidates(
        self,
        *,
        root: Path,
        prefix: str,
        kind: str,
        cutoff: float,
    ) -> list[CleanupCandidate]:
        if not root.is_dir() or root.is_symlink():
            return []
        output: list[CleanupCandidate] = []
        for path in root.iterdir():
            if not path.name.startswith(prefix) or not path.is_dir() or path.is_symlink():
                continue
            details = self._safe_directory_details(path)
            if details is None:
                continue
            size, newest_mtime = details
            if newest_mtime > cutoff:
                continue
            output.append(self._candidate(kind, path, size, newest_mtime, is_directory=True))
        return output

    def _log_candidates(self, cutoff: float) -> list[CleanupCandidate]:
        output: list[CleanupCandidate] = []
        if self.log_root.is_dir() and not self.log_root.is_symlink():
            f2_logs = [
                path
                for path in self.log_root.glob('f2-*.log')
                if path.is_file() and not path.is_symlink()
            ]
            newest_log = max(f2_logs, key=lambda path: path.stat().st_mtime, default=None)
            for path in f2_logs:
                if path == newest_log:
                    continue
                try:
                    stats = path.stat()
                except OSError:
                    continue
                if stats.st_mtime <= cutoff:
                    output.append(self._candidate('rotated_log', path, stats.st_size, stats.st_mtime))

        # Active bot.log and bot.err.log are deliberately excluded. Only
        # timestamped/rotated siblings created by ATRI startup tooling qualify.
        for pattern in ('bot.log.*', 'bot.err.log.*'):
            for path in self.project_root.glob(pattern):
                if not path.is_file() or path.is_symlink():
                    continue
                try:
                    stats = path.stat()
                except OSError:
                    continue
                if stats.st_mtime <= cutoff:
                    output.append(self._candidate('rotated_log', path, stats.st_size, stats.st_mtime))
        return output

    def _candidate(
        self,
        kind: str,
        path: Path,
        size: int,
        modified_at: float,
        *,
        is_directory: bool = False,
    ) -> CleanupCandidate:
        resolved = path.resolve()
        relative = resolved.relative_to(self.project_root).as_posix()
        return CleanupCandidate(
            kind=kind,
            path=resolved,
            relative_path=relative,
            size=max(int(size), 0),
            modified_at=float(modified_at),
            is_directory=is_directory,
        )

    @staticmethod
    def _safe_directory_details(path: Path) -> tuple[int, float] | None:
        try:
            newest = path.stat().st_mtime
            size = 0
            for current_root, directory_names, filenames in os.walk(path, followlinks=False):
                current = Path(current_root)
                for name in list(directory_names):
                    child = current / name
                    if child.is_symlink():
                        return None
                    newest = max(newest, child.stat().st_mtime)
                for name in filenames:
                    child = current / name
                    if child.is_symlink():
                        return None
                    stats = child.stat()
                    size += stats.st_size
                    newest = max(newest, stats.st_mtime)
            return size, newest
        except OSError:
            return None

    def _candidate_is_still_safe(self, candidate: CleanupCandidate) -> bool:
        path = candidate.path
        if path.is_symlink() or not path.exists():
            return False
        try:
            path.relative_to(self.project_root)
        except ValueError:
            return False

        if candidate.kind == 'music_cache':
            return (
                path.parent == self.music_root
                and path.is_file()
                and path.suffix.casefold() in _MUSIC_SUFFIXES
            )
        if candidate.kind == 'bilibili_temp':
            return (
                path.parent == self.bilibili_root
                and path.is_dir()
                and path.name.startswith('bilibili_')
                and self._safe_directory_details(path) is not None
            )
        if candidate.kind == 'douyin_temp':
            return (
                path.parent == self.douyin_root
                and path.is_dir()
                and path.name.startswith('douyin_')
                and self._safe_directory_details(path) is not None
            )
        if candidate.kind == 'rotated_log':
            return (
                path.is_file()
                and (
                    (path.parent == self.log_root and path.name.startswith('f2-') and path.suffix == '.log')
                    or (
                        path.parent == self.project_root
                        and path.name.startswith(('bot.log.', 'bot.err.log.'))
                    )
                )
            )
        return False


def collect_cleanup_protection(bot: Any) -> CleanupProtection:
    protection = CleanupProtection()

    music = bot.get_cog('Music') if hasattr(bot, 'get_cog') else None
    if music is not None:
        for stem, lock in dict(getattr(music, 'download_locks', {})).items():
            if getattr(lock, 'locked', lambda: False)():
                protection.protected_music_names.add(str(stem))
                protection.busy_groups.add('music')
        for queue_data in dict(getattr(music, 'queues', {})).values():
            if not isinstance(queue_data, dict):
                continue
            songs: list[object] = [queue_data.get('current'), queue_data.get('priority_next')]
            raw_queue = queue_data.get('queue')
            if isinstance(raw_queue, list):
                songs.extend(raw_queue)
            for song in songs:
                if not isinstance(song, dict):
                    continue
                local_path = song.get('local_path')
                if local_path:
                    protection.protected_music_names.add(Path(str(local_path)).name)

    for cog_name, group in (
        ('BilibiliVideoCog', 'bilibili'),
        ('DouyinVideoCog', 'douyin'),
    ):
        cog = bot.get_cog(cog_name) if hasattr(bot, 'get_cog') else None
        lock = getattr(cog, 'download_lock', None)
        if lock is not None and getattr(lock, 'locked', lambda: False)():
            protection.busy_groups.add(group)
    return protection
