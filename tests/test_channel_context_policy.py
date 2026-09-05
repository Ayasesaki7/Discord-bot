from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from chat.agent.context_policy import (
    ChannelContextPolicy,
    ChannelContextPolicyStore,
    PASSIVE_HISTORY_BATCH_VERSION,
)


class ChannelContextPolicyStoreTests(unittest.TestCase):
    def test_settings_and_usage_are_scoped_by_guild_and_channel(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = ChannelContextPolicyStore(root, default_history_messages=200)

            first = store.update_policy(
                100,
                200,
                history_messages=300,
                token_budget=40_000,
                overflow_strategy="drop_oldest",
            )
            store.record_usage(100, 200, input_tokens=12_345, output_tokens=321)

            self.assertEqual(first.policy.history_messages, 300)
            self.assertEqual(store.get(100, 200).last_input_tokens, 12_345)
            self.assertEqual(store.get(100, 201).policy.history_messages, 200)
            self.assertIsNone(store.get(101, 200).last_input_tokens)

            reloaded = ChannelContextPolicyStore(root, default_history_messages=200)
            state = reloaded.get(100, 200)
            self.assertEqual(state.policy.token_budget, 40_000)
            self.assertEqual(state.policy.overflow_strategy, "drop_oldest")
            self.assertEqual(state.last_output_tokens, 321)

    def test_import_watermark_is_persisted_without_message_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = ChannelContextPolicyStore(root)
            store.record_import(
                100,
                200,
                message_count=300,
                through_message_id=999,
                input_tokens=50_000,
                output_tokens=700,
            )

            payload = json.loads(store.path.read_text(encoding="utf-8"))
            serialized = json.dumps(payload, ensure_ascii=False)
            self.assertNotIn("message_text", serialized)
            self.assertEqual(
                payload["channels"]["100:200"]["imported_message_count"],
                300,
            )
            self.assertEqual(store.get(100, 200).imported_through_message_id, 999)
            self.assertEqual(store.get(100, 200).observed_through_message_id, 999)
            self.assertTrue(store.get(100, 200).passive_history_initialized)
            self.assertEqual(
                store.get(100, 200).passive_history_version,
                PASSIVE_HISTORY_BATCH_VERSION,
            )

            store.record_observed(100, 200, through_message_id=1_200)
            store.record_observed(100, 200, through_message_id=1_100)
            reloaded = ChannelContextPolicyStore(root)
            self.assertEqual(
                reloaded.get(100, 200).observed_through_message_id,
                1_200,
            )

            store.clear_runtime_usage(100, 200)
            cleared = store.get(100, 200)
            self.assertEqual(cleared.imported_message_count, 0)
            self.assertIsNone(cleared.imported_through_message_id)
            self.assertIsNone(cleared.observed_through_message_id)
            self.assertFalse(cleared.passive_history_initialized)
            self.assertEqual(cleared.passive_history_version, 0)

    def test_legacy_live_injection_watermark_requires_one_batch_rescan(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = root / 'chat' / 'agent' / 'data' / 'channel_contexts.json'
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps(
                    {
                        'version': 1,
                        'channels': {
                            '100:200': {
                                'history_messages': 300,
                                'token_budget': 80_000,
                                'overflow_strategy': 'compress',
                                'observed_through_message_id': 999,
                                'passive_history_initialized': True,
                            }
                        },
                    }
                ),
                encoding='utf-8',
            )

            state = ChannelContextPolicyStore(root).get(100, 200)

            self.assertTrue(state.passive_history_initialized)
            self.assertEqual(state.passive_history_version, 0)

    def test_policy_rejects_unsafe_sizes(self) -> None:
        with self.assertRaises(ValueError):
            ChannelContextPolicy(history_messages=0)
        with self.assertRaises(ValueError):
            ChannelContextPolicy(token_budget=80_001)
        with self.assertRaises(ValueError):
            ChannelContextPolicy(overflow_strategy="erase_everything")  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
