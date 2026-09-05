from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock, Mock, patch

from chat.agent.code_settings import AgentCodeSettings
from chat.agent.web_tools import WebSearchError, WebSearchHost


class WebSearchHostTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    async def _accept_test_sources(
        candidates: list[dict[str, str]],
    ) -> list[dict[str, str]]:
        return [
            {**candidate, "httpStatus": "200"}
            for candidate in candidates
        ]

    async def test_unconfigured_search_model_is_rejected(self) -> None:
        with self.assertRaisesRegex(WebSearchError, "联网搜索设置"):
            await WebSearchHost().search(
                AgentCodeSettings(False, "", "", ""),
                "current news",
            )

    async def test_semantic_recovery_can_force_an_explicit_search(self) -> None:
        settings = AgentCodeSettings(
            True,
            "https://search.example/v1",
            "fake-search-key",
            "search-model",
        )
        completion = AsyncMock(return_value="SEARCH")
        with patch(
            "chat.agent.web_tools.OpenAICompatibleClient.create_chat_completion",
            new=completion,
        ):
            required = await WebSearchHost().should_force_search(
                settings,
                "Please use your live search tool to verify today's result.",
            )

        self.assertTrue(required)
        messages = completion.await_args.args[0]
        self.assertIn("complete meaning", messages[0]["content"])
        self.assertIn("capability questions", messages[0]["content"])

    async def test_semantic_decision_disables_transport_retries(self) -> None:
        settings = AgentCodeSettings(
            True,
            "https://search.example/v1",
            "fake-search-key",
            "search-model",
        )
        client = Mock()
        client.create_chat_completion = AsyncMock(return_value='NO_SEARCH')
        with patch(
            'chat.agent.web_tools.OpenAICompatibleClient',
            return_value=client,
        ) as constructor:
            await WebSearchHost().should_force_search(settings, 'hello')

        config = constructor.call_args.args[0]
        self.assertEqual(config.retry_count, 0)

    async def test_semantic_recovery_keeps_capability_chat_search_free(self) -> None:
        settings = AgentCodeSettings(
            True,
            "https://search.example/v1",
            "fake-search-key",
            "search-model",
        )
        with patch(
            "chat.agent.web_tools.OpenAICompatibleClient.create_chat_completion",
            new=AsyncMock(return_value="NO_SEARCH"),
        ):
            required = await WebSearchHost().should_force_search(
                settings,
                "Do you have a search tool?",
            )

        self.assertFalse(required)

    async def test_semantic_recovery_rejects_ambiguous_classifier_output(self) -> None:
        settings = AgentCodeSettings(
            True,
            "https://search.example/v1",
            "fake-search-key",
            "search-model",
        )
        with patch(
            "chat.agent.web_tools.OpenAICompatibleClient.create_chat_completion",
            new=AsyncMock(return_value="I am not sure"),
        ):
            required = await WebSearchHost().should_force_search(
                settings,
                "Could you maybe look at that?",
            )

        self.assertFalse(required)

    async def test_separate_model_result_requires_and_returns_sources(self) -> None:
        settings = AgentCodeSettings(
            True,
            "https://search.example/v1",
            "fake-search-key",
            "search-model",
            4096,
        )
        with patch(
            "chat.agent.web_tools.OpenAICompatibleClient.create_chat_completion",
            new=AsyncMock(
                return_value=(
                    "Current result. Source: https://example.com/current-report"
                )
            ),
        ), patch.object(
            WebSearchHost,
            "_verify_sources",
            new=AsyncMock(side_effect=self._accept_test_sources),
        ):
            result = await WebSearchHost().search(settings, "current result")

        payload = json.loads(result["content"])
        self.assertEqual(payload["model"], "search-model")
        self.assertEqual(
            payload["sources"][0]["url"],
            "https://example.com/current-report",
        )

    async def test_missing_sources_do_not_discard_search_answer(self) -> None:
        settings = AgentCodeSettings(
            True,
            "https://search.example/v1",
            "fake-search-key",
            "search-model",
        )
        completion = AsyncMock(return_value="A current result with no reliable link.")
        with patch(
            "chat.agent.web_tools.OpenAICompatibleClient.create_chat_completion",
            new=completion,
        ), patch.object(
            WebSearchHost,
            "_verify_sources",
            new=AsyncMock(side_effect=self._accept_test_sources),
        ):
            result = await WebSearchHost().search(settings, "current result")

        self.assertEqual(completion.await_count, 1)
        payload = json.loads(result["content"])
        self.assertEqual(payload["answer"], "A current result with no reliable link.")
        self.assertEqual(payload["sources"], [])
        self.assertEqual(payload["sourceVerification"], "no_verified_sources")

    def test_grounding_metadata_and_inline_urls_are_deduplicated(self) -> None:
        sources = WebSearchHost._collect_sources(
            {
                "attempts": [
                    {
                        "web_sources": [
                            {"url": "https://example.com/a", "title": "A"}
                        ]
                    }
                ]
            },
            "See https://example.com/a and https://example.com/b.",
        )

        self.assertEqual(
            [item["url"] for item in sources],
            ["https://example.com/a", "https://example.com/b"],
        )

    def test_homepages_are_not_accepted_as_specific_sources(self) -> None:
        self.assertFalse(WebSearchHost._is_specific_source_url("https://example.com/"))
        self.assertTrue(
            WebSearchHost._is_specific_source_url("https://example.com/news/story-1")
        )

    def test_redirect_to_a_different_article_is_rejected(self) -> None:
        self.assertFalse(
            WebSearchHost._same_resource_path(
                "https://example.com/2026/news/future-story/31005/",
                "https://example.com/2024/news/unrelated-story/31005/",
            )
        )
        self.assertTrue(
            WebSearchHost._same_resource_path(
                "http://example.com/news/story-1",
                "https://www.example.com/news/story-1/",
            )
        )

    def test_soft_error_page_titles_are_rejected(self) -> None:
        self.assertTrue(WebSearchHost._looks_like_error_title("404 - Page Not Found"))
        self.assertFalse(WebSearchHost._looks_like_error_title("A real technology report"))

    def test_unverified_answer_links_are_removed_before_agent_use(self) -> None:
        cleaned = WebSearchHost._strip_unverified_links(
            "Read [Example](https://example.com/fake) or https://invalid.example/path"
        )

        self.assertIn("Example", cleaned)
        self.assertNotIn("https://", cleaned)


if __name__ == "__main__":
    unittest.main()
