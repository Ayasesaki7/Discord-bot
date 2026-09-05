from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from tools.cleanup import CleanupProtection, GeneratedArtifactCleaner


class GeneratedArtifactCleanerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        for name in ('music_cache', 'bilibili_cache', 'douyin_cache', 'logs', 'config'):
            (self.root / name).mkdir()
        self.now = 2_000_000.0
        self.old = self.now - 48 * 3600
        self.recent = self.now - 3600

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _write(path: Path, content: bytes, modified_at: float) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        os.utime(path, (modified_at, modified_at))

    def test_preview_only_lists_known_old_generated_artifacts(self) -> None:
        self._write(self.root / 'music_cache' / 'old.mp3', b'a' * 5, self.old)
        self._write(self.root / 'music_cache' / 'recent.mp3', b'b', self.recent)
        self._write(self.root / 'music_cache' / 'notes.txt', b'user', self.old)
        self._write(self.root / 'config' / 'credentials.json', b'secret', self.old)
        self._write(self.root / 'bot.log', b'active', self.old)

        generated = self.root / 'bilibili_cache' / 'bilibili_old'
        self._write(generated / 'video.mp4', b'video', self.old)
        os.utime(generated, (self.old, self.old))
        unrelated = self.root / 'bilibili_cache' / 'manual_files'
        self._write(unrelated / 'video.mp4', b'user-video', self.old)
        os.utime(unrelated, (self.old, self.old))

        cleaner = GeneratedArtifactCleaner(self.root)
        report = cleaner.preview(minimum_age_hours=24, now=self.now)
        paths = {candidate.relative_path for candidate in report.candidates}

        self.assertIn('music_cache/old.mp3', paths)
        self.assertIn('bilibili_cache/bilibili_old', paths)
        self.assertNotIn('music_cache/recent.mp3', paths)
        self.assertNotIn('music_cache/notes.txt', paths)
        self.assertNotIn('config/credentials.json', paths)
        self.assertNotIn('bot.log', paths)
        self.assertNotIn('bilibili_cache/manual_files', paths)

    def test_clean_preserves_active_music_and_newest_f2_log(self) -> None:
        self._write(self.root / 'music_cache' / 'active.mp3', b'active', self.old)
        self._write(self.root / 'music_cache' / 'stale.mp3', b'stale', self.old)
        self._write(self.root / 'logs' / 'f2-old.log', b'old', self.old - 100)
        self._write(self.root / 'logs' / 'f2-newest.log', b'newest', self.old)

        cleaner = GeneratedArtifactCleaner(self.root)
        report = cleaner.clean(
            minimum_age_hours=24,
            protection=CleanupProtection(protected_music_names={'active'}),
            now=self.now,
        )

        self.assertTrue((self.root / 'music_cache' / 'active.mp3').exists())
        self.assertFalse((self.root / 'music_cache' / 'stale.mp3').exists())
        self.assertFalse((self.root / 'logs' / 'f2-old.log').exists())
        self.assertTrue((self.root / 'logs' / 'f2-newest.log').exists())
        self.assertEqual(report.deleted_count, 2)

    def test_busy_video_group_is_not_scanned(self) -> None:
        generated = self.root / 'douyin_cache' / 'douyin_old'
        self._write(generated / 'video.mp4', b'video', self.old)
        os.utime(generated, (self.old, self.old))

        cleaner = GeneratedArtifactCleaner(self.root)
        report = cleaner.preview(
            minimum_age_hours=24,
            protection=CleanupProtection(busy_groups={'douyin'}),
            now=self.now,
        )

        self.assertEqual(report.candidates, [])
        self.assertTrue(generated.exists())
