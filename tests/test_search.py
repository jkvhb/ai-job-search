# -*- coding: utf-8 -*-
import os
import shutil
import tempfile
import unittest

import log
import search
import store


class ProviderFallbackTest(unittest.TestCase):
    def setUp(self):
        # 隔离：search() 全失败时会调 log.log_event，必须重定向到临时目录，
        # 否则会往用户的真实 data/profiles/.../logs/events.jsonl 里追加记录。
        self.tmp = tempfile.mkdtemp()
        self._old_data = store.DATA_ROOT
        self._old_logfile = log.LOG_FILE
        store.DATA_ROOT = self.tmp
        store.set_current_profile("default")
        log.LOG_FILE = os.path.join(store.profile_dir(), "logs", "events.jsonl")

    def tearDown(self):
        store.DATA_ROOT = self._old_data
        log.LOG_FILE = self._old_logfile
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_uses_first_provider_when_ok(self):
        calls = []

        def fake_http(url, payload, key, timeout):
            calls.append(url)
            return {"results": [{"title": "T", "url": "https://x", "content": "c"}]}

        res = search.search("rlhf", providers=[
            {"name": "tavily", "base_url": "https://a", "api_key": "k1"},
            {"name": "bocha", "base_url": "https://b", "api_key": "k2"},
        ], http=fake_http)
        self.assertEqual(len(calls), 1)
        self.assertEqual(res[0]["url"], "https://x")

    def test_falls_back_to_second_on_failure(self):
        calls = []

        def fake_http(url, payload, key, timeout):
            calls.append(url)
            if "a" in url:
                raise RuntimeError("quota exceeded")
            return {"results": [{"title": "T2", "url": "https://y", "content": "c2"}]}

        res = search.search("rlhf", providers=[
            {"name": "tavily", "base_url": "https://a", "api_key": "k1"},
            {"name": "bocha", "base_url": "https://b", "api_key": "k2"},
        ], http=fake_http)
        self.assertEqual(len(calls), 2)
        self.assertEqual(res[0]["url"], "https://y")

    def test_returns_empty_when_all_fail(self):
        def bad(url, payload, key, timeout):
            raise RuntimeError("down")

        res = search.search("x", providers=[{"name": "t", "base_url": "https://a", "api_key": "k"}],
                            http=bad)
        self.assertEqual(res, [])

    def test_skips_provider_without_key(self):
        calls = []

        def fake_http(url, payload, key, timeout):
            calls.append(key)
            return {"results": []}

        search.search("x", providers=[
            {"name": "a", "base_url": "https://a", "api_key": ""},
            {"name": "b", "base_url": "https://b", "api_key": "kb"},
        ], http=fake_http)
        self.assertEqual(calls, ["kb"])

    def test_normalizes_result_fields(self):
        def fake_http(url, payload, key, timeout):
            return {"results": [{"title": "T", "url": "U", "content": "C"}]}

        res = search.search("x", providers=[{"name": "t", "base_url": "https://a", "api_key": "k"}],
                            http=fake_http)
        self.assertEqual(set(res[0].keys()), {"title", "url", "snippet", "date"})


if __name__ == "__main__":
    unittest.main()
