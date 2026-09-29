from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from chat.agent.dsh_runtime import _read_dsh_session_snapshot, _split_dsh_recovery_messages


def message(seq, text, kind="user/message", op="append"):
    content = {"content": [{"type": "text", "text": text}]}
    data = {"message": content} if kind == "assistant/message" else content
    return {"seq": seq, "type": kind, "surfaceOp": op, "data": data}


def compact(seq, start, end, text):
    return [
        {"seq": seq, "type": "compaction/summary", "data": {
            "summary": [{"type": "text", "text": text}],
            "shadowedRange": {"start": start, "end": end},
        }},
        message(seq + 1, "native checkpoint wrapper", op={"op": "replace", "start": start, "end": end}),
    ]


class DshRetentionTests(unittest.TestCase):
    def test_legacy_recovery_is_migrated_without_truncating_to_old_112k_cap(self):
        body = "中文" * 90000
        frame = "\n".join(("[Recovered post-checkpoint DSH delta; host-authored boundary]", body, "[End recovered post-checkpoint DSH delta]"))
        before = self.snapshot([message(0, frame)])
        self.assertTrue(before.has_oversized_recovery)
        self.assertEqual(before.post_compaction_delta, body)
        parts = _split_dsh_recovery_messages(frame)
        self.assertEqual(len(parts), 45)
        self.assertTrue(all(len(part) < 4500 for part in parts))
        after = self.snapshot([message(i, part) for i, part in enumerate(parts)])
        self.assertFalse(after.has_oversized_recovery)
        self.assertEqual(after.post_compaction_delta.replace("\n\n", ""), body)

    def test_shadowed_legacy_frame_does_not_trigger_another_migration(self):
        frame = "\n".join(("[Recovered post-checkpoint DSH delta; host-authored boundary]", "中" * 20000, "[End recovered post-checkpoint DSH delta]"))
        result = self.snapshot([message(0, frame), message(1, "recent"), *compact(2, 0, 0, "facts")])
        self.assertFalse(result.has_oversized_recovery)
        self.assertEqual(result.latest_compaction_summary, "facts")

    def snapshot(self, events, **kwargs):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session_id = "s_" + "a" * 40
            target = root / session_id / "session.jsonl"
            target.parent.mkdir()
            target.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
            original = target.read_bytes()
            result = _read_dsh_session_snapshot(root, session_id, kwargs.pop("max_summary_chars", 120_000), **kwargs)
            self.assertEqual(target.read_bytes(), original)
            return result

    def test_retained_tail_before_summary_is_recovered(self):
        result = self.snapshot([
            message(0, "summarized old head"), message(1, "recent question"),
            message(2, "recent answer", "assistant/message"),
            *compact(3, 0, 0, "old facts"), message(5, "newest question"),
        ])
        self.assertEqual(result.latest_compaction_summary, "old facts")
        self.assertEqual(result.post_compaction_event_count, 3)
        self.assertNotIn("summarized old head", result.post_compaction_delta)
        self.assertNotIn("native checkpoint wrapper", result.post_compaction_delta)
        self.assertLess(result.post_compaction_delta.index("recent question"), result.post_compaction_delta.index("recent answer"))
        self.assertIn("newest question", result.post_compaction_delta)

    def test_repeated_replacement_uses_surface_positions_not_seq_ranges(self):
        # After first compaction: [5, 2, 3]; seq 5 precedes seq 2!
        result = self.snapshot([
            message(0, "old A"), message(1, "old B"),
            message(2, "middle"), message(3, "keep this tail"),
            *compact(4, 0, 1, "first checkpoint"), message(6, "latest"),
            *compact(7, 5, 2, "second checkpoint"),
        ])
        self.assertEqual(result.latest_compaction_summary, "second checkpoint")
        self.assertNotIn("middle", result.post_compaction_delta)
        self.assertIn("keep this tail", result.post_compaction_delta)
        self.assertIn("latest", result.post_compaction_delta)

    def test_summary_without_committed_replace_cannot_discard_tail(self):
        result = self.snapshot([message(0, "keep A"), message(1, "keep B"), compact(2, 0, 1, "uncommitted")[0]])
        self.assertEqual(result.latest_compaction_summary, "")
        self.assertIn("keep A", result.post_compaction_delta)
        self.assertIn("keep B", result.post_compaction_delta)

    def test_invalid_replacement_preserves_existing_surface(self):
        result = self.snapshot([message(0, "keep"), *compact(1, 99, 100, "bad")])
        self.assertIn("keep", result.post_compaction_delta)
        self.assertEqual(result.latest_compaction_summary, "")

    def test_pruned_tool_result_is_not_added_twice_or_treated_as_summary(self):
        tool = {"seq": 1, "type": "tool/result", "surfaceOp": "append", "data": {"name": "web_search"}}
        rewrite = {**tool, "seq": 3, "surfaceOp": {"op": "replace", "start": 1, "end": 1}}
        result = self.snapshot([message(0, "question"), tool, {"seq": 2, "type": "compaction/prune"}, rewrite])
        self.assertEqual(result.post_compaction_delta.count("web_search"), 1)
        self.assertEqual(result.latest_compaction_summary, "")

    def test_non_surface_chunks_and_auxiliary_answers_never_enter_memory(self):
        hidden = message(1, "auxiliary hidden answer", "assistant/message")
        hidden.pop("surfaceOp")
        result = self.snapshot([message(0, "visible"), hidden])
        self.assertNotIn("auxiliary hidden answer", result.post_compaction_delta)

    def test_full_recent_messages_no_longer_have_4000_or_40000_char_caps(self):
        text = "recent text " * 6000
        result = self.snapshot([message(0, "old"), message(1, text), *compact(2, 0, 0, "summary")])
        self.assertIn(text.strip(), result.post_compaction_delta)
        self.assertEqual(result.post_compaction_dropped_count, 0)

    def test_total_recovery_cap_still_keeps_newest_content(self):
        result = self.snapshot([message(0, "A" * 200), message(1, "B" * 200)], max_delta_chars=100)
        self.assertEqual(result.post_compaction_delta, "B" * 100)
        self.assertEqual(result.post_compaction_dropped_count, 1)

    def test_recovered_frame_remains_until_actually_shadowed(self):
        frame = "\n".join([
            "[Recovered same-channel memory checkpoint; host-authored boundary]",
            "This is factual conversation memory recovered from the previous DSH session.",
            "inherited summary", "[End recovered memory checkpoint]",
            "[Recovered post-checkpoint DSH delta; host-authored boundary]",
            "These are bounded user/assistant records written after the checkpoint.",
            "inherited recent fact", "[End recovered post-checkpoint DSH delta]",
        ])
        result = self.snapshot([message(0, frame), message(1, "recent fact"), *compact(2, 0, 0, "updated summary")])
        self.assertEqual(result.latest_compaction_summary, "updated summary")
        self.assertNotIn("inherited recent fact", result.post_compaction_delta)
        self.assertIn("recent fact", result.post_compaction_delta)

    def test_large_summary_is_not_silently_limited_to_30000_characters(self):
        text = "summarized facts " * 2500
        result = self.snapshot([message(0, "old"), message(1, "tail"), *compact(2, 0, 0, text)])
        self.assertEqual(result.latest_compaction_summary, text.strip())

    def test_rebuild_framing_does_not_accumulate_as_conversation(self):
        frame = "\n".join([
            "[Recovered same-channel memory checkpoint; host-authored boundary]",
            "This is factual conversation memory recovered from the previous DSH session.",
            "Historical claims about API providers, quotas, rate limits, ...",
            "actual summary", "[End recovered memory checkpoint]",
            "[Recovered post-checkpoint DSH delta; host-authored boundary]",
            "These are bounded recent user/assistant records not covered by the checkpoint.",
            "Do not infer the current API provider, quota, rate-limit state, ...",
            "actual recent fact", "[End recovered post-checkpoint DSH delta]",
        ])
        result = self.snapshot([message(0, frame)])
        self.assertEqual(result.latest_compaction_summary, "actual summary")
        self.assertEqual(result.post_compaction_delta, "actual recent fact")


if __name__ == "__main__":
    unittest.main()
