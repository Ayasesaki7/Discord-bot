from __future__ import annotations

import unittest
from unittest.mock import Mock

from music.cog import Music
from music.search import SEARCH_URL, query_variants, relevance, search_songs


def track(mid="one", name="晴天", artist="周杰伦", title=None):
    return {"id": 97773, "mid": mid, "name": name, "title": title or name,
            "singer": [{"name": artist}], "album": {"mid": "albumMID", "name": "叶惠美"},
            "file": {"media_mid": "mediaMID"}, "interval": 269}


def response(*songs):
    return {"code": 0, "req_1": {"code": 0, "data": {"code": 0, "body": {"song": {"list": list(songs)}}}}}


class QueryTests(unittest.TestCase):
    def test_natural_request_with_artist_and_book_title(self):
        variants = query_variants("帮我播放一下陈奕迅的《喜帖街》，谢谢")
        self.assertIn("陈奕迅 喜帖街", variants)
        self.assertTrue(variants[0].startswith("帮我播放"))
        self.assertLessEqual(len(variants), 3)

    def test_symbol_titles_and_versions_are_not_destroyed(self):
        for query in ("AC/DC - Back In Black", "Love Story (Taylor's Version)", "A+B & C", "给我一个理由忘记", "放生", "谢谢"):
            with self.subTest(query=query):
                self.assertEqual(query_variants(query)[0], query)

    def test_fullwidth_symbols(self):
        self.assertIn("晴天 周杰伦", query_variants("　《晴天》　－　周杰伦　"))

    def test_empty_controls_and_oversized_queries(self):
        for query in ("", "\n\t\x00", "x" * 1001, None):
            self.assertEqual(query_variants(query), [])

    def test_order_artist_and_version(self):
        exact = track(title="晴天 (Live)")
        wrong_artist = track(artist="翻唱歌手", title="晴天 (Live)")
        wrong_version = track()
        self.assertGreater(relevance(exact, "周杰伦 晴天 Live"), relevance(wrong_artist, "周杰伦 晴天 Live"))
        self.assertGreater(relevance(exact, "周杰伦 晴天 Live"), relevance(wrong_version, "周杰伦 晴天 Live"))

    def test_no_latin_substring_boost(self):
        self.assertEqual(relevance(track(name="It", artist="X"), "Without You"), 0)

    def test_exact_symbol_title_outranks_stripped_title(self):
        request = Mock(return_value=response(track("wrong", name="AB"), track("right", name="A+B")))
        self.assertEqual(search_songs(request, "A+B")[0]["mid"], "right")


class SearchTests(unittest.TestCase):
    def test_formal_endpoint_full_metadata_and_one_request(self):
        request = Mock(return_value=response(track()))
        result = search_songs(request, "《晴天》 - 周杰伦")
        self.assertEqual(result[0]["album"]["mid"], "albumMID")
        request.assert_called_once()
        args, kwargs = request.call_args
        self.assertEqual(args[0], SEARCH_URL)
        self.assertEqual(kwargs["method"], "POST")
        self.assertEqual(kwargs["timeout"], 10)
        api = kwargs["data"]["req_1"]
        self.assertEqual(api["method"], "DoSearchForQQMusicDesktop")
        self.assertEqual(api["param"]["search_type"], 0)
        self.assertEqual(api["param"]["query"], "《晴天》 - 周杰伦")

    def test_empty_raw_then_cleaned_query(self):
        request = Mock(side_effect=[response(), response(track(name="喜帖街", artist="陈奕迅"))])
        result = search_songs(request, "帮我播放一下陈奕迅的《喜帖街》，谢谢")
        self.assertEqual(result[0]["name"], "喜帖街")
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args.kwargs["data"]["req_1"]["param"]["query"], "陈奕迅 喜帖街")

    def test_relevance_sorts_and_dedupes_mid_not_title(self):
        request = Mock(return_value=response(track("cover", artist="别人"), track("correct"), track("correct"), track("edition")))
        result = search_songs(request, "晴天 周杰伦", limit=3)
        self.assertEqual([s["mid"] for s in result], ["correct", "edition", "cover"])

    def test_at_most_three_empty_queries(self):
        request = Mock(return_value=response())
        self.assertEqual(search_songs(request, "请播放一下《A+B》，谢谢"), [])
        self.assertEqual(request.call_count, 3)

    def test_network_error_does_not_fan_out(self):
        request = Mock(side_effect=TimeoutError())
        with self.assertRaises(TimeoutError):
            search_songs(request, "请播放一下《A+B》，谢谢")
        self.assertEqual(request.call_count, 1)

    def test_risk_response_or_bad_schema_is_not_empty_results(self):
        for result in ({"code": 2000}, {"code": 0, "req_1": {"code": 1000}},
                       {"code": 0, "req_1": {"code": 0, "data": {}}},
                       {"code": 0, "req_1": {"code": 0, "data": {"code": 1000}}}):
            request = Mock(return_value=result)
            with self.subTest(result=result), self.assertRaises(RuntimeError):
                search_songs(request, "请播放一下《A+B》，谢谢")
            self.assertEqual(request.call_count, 1)

    def test_later_failure_keeps_existing_candidates(self):
        request = Mock(side_effect=[response(track()), TimeoutError()])
        self.assertEqual(search_songs(request, "请播放一下《A+B》，谢谢")[0]["mid"], "one")
        self.assertEqual(request.call_count, 2)

    def test_ignores_invalid_rows_without_discarding_valid_results(self):
        request = Mock(return_value=response(None, {}, {"mid": 123, "name": "invalid"}, track()))
        self.assertEqual(len(search_songs(request, "晴天")), 1)

    def test_no_network_for_invalid_or_zero_limit(self):
        request = Mock()
        self.assertEqual(search_songs(request, " "), [])
        self.assertEqual(search_songs(request, "晴天", limit=0), [])
        request.assert_not_called()

    def test_symbol_query_is_encoded_in_json_not_url(self):
        query = "A&B + C/D #1"
        request = Mock(return_value=response(track(name=query)))
        search_songs(request, query)
        self.assertEqual(request.call_args.args[0], SEARCH_URL)
        self.assertEqual(request.call_args.kwargs["data"]["req_1"]["param"]["query"], query)


class MusicIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.music = object.__new__(Music)
        self.music._get_song_detail = Mock(side_effect=AssertionError("No N+1 calls"))
        self.music._qq_request_json = Mock(return_value=response(track(title="晴天 (Live)")))

    def test_picker_and_agent_use_same_search(self):
        picker = self.music._search_song_candidates("晴天 周杰伦 Live")
        agent = self.music._search_song("晴天 周杰伦 Live")
        self.assertEqual(picker[0], agent)
        self.assertEqual(agent["name"], "晴天 (Live)")
        self.assertEqual(agent["id"], "97773")
        self.assertEqual(agent["media_mid"], "mediaMID")
        self.assertEqual(agent["dt"], 269000)
        self.assertIn("albumMID", agent["al"]["picUrl"])
        self.music._get_song_detail.assert_not_called()
        self.assertEqual(self.music._qq_request_json.call_count, 2)

    def test_bad_metadata_does_not_hide_other_candidates(self):
        malformed = track("bad")
        malformed["interval"] = "not a number"
        self.music._qq_request_json.return_value = response(malformed, track())
        self.assertEqual(len(self.music._search_song_candidates("晴天")), 1)
        self.assertEqual(self.music._search_song("晴天")["mid"], "one")

    def test_network_failure_returns_no_song_without_detail_retries(self):
        self.music._qq_request_json.side_effect = TimeoutError()
        self.assertIsNone(self.music._search_song("晴天 周杰伦"))
        self.music._qq_request_json.assert_called_once()
        self.music._get_song_detail.assert_not_called()


if __name__ == "__main__":
    unittest.main()
