# -*- coding: utf-8 -*-
import json
import unittest
import urllib.request
from unittest import mock

import search

from tests.base import IsolatedCase


class ProviderFallbackTest(IsolatedCase):
    # search() 全失败时会调 log.log_event，IsolatedCase 会把 store/log 重定向到临时目录，
    # 否则会往用户的真实 data/profiles/.../logs/events.jsonl 里追加记录。
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


class RegistryUrlTest(IsolatedCase):
    """`REGISTRY` 里**真发出去的那个请求**长什么样（P5 §7.2 合并 `_http_tavily`/`_http_bocha`）。

    为什么非要这一条：上面所有既有测试都注入假 `http`，`REGISTRY` 里的真实现
    **一行都没被走过** —— 把两个源合并成一个带参数的实现时，后缀写错
    （`/v1/web_search`）、方法写成 GET、`Bearer` 丢了，全量照样绿。
    这里只把 `urlopen` 换掉，断言 Request 本身（不联网）。
    """

    def _capture(self):
        seen = {}

        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return b'{"results": []}'

        def fake_urlopen(req, timeout=None):
            seen["url"] = req.full_url
            seen["headers"] = dict(req.header_items())
            seen["method"] = req.get_method()
            seen["timeout"] = timeout
            seen["data"] = req.data
            return FakeResp()

        return seen, fake_urlopen

    def test_each_provider_posts_to_its_own_suffix(self):
        for base, name, suffix in (("https://api.example.com", "tavily", "/search"),
                                   ("https://api.example.com/", "tavily", "/search"),
                                   ("https://api.example.com/", "bocha", "/v1/web-search")):
            seen, fake_urlopen = self._capture()
            with mock.patch.object(urllib.request, "urlopen", fake_urlopen):
                raw = search.REGISTRY[name](base, {"query": "q", "max_results": 5}, "KEY", 7)
            self.assertEqual(raw, {"results": []}, "应答要按 JSON 解出来")
            self.assertEqual(seen["url"], "https://api.example.com" + suffix,
                             "base_url 末尾的 / 要去掉，后缀由各自的源钉住")
            self.assertEqual(seen["method"], "POST")
            self.assertEqual(seen["headers"].get("Authorization"), "Bearer KEY")
            # `header_items()` 的键是 capitalize 过的（`Content-Type` → `Content-type`）
            self.assertEqual(seen["headers"].get("Content-type"), "application/json")
            self.assertEqual(seen["timeout"], 7, "timeout 必须透传")
            self.assertEqual(json.loads(seen["data"].decode("utf-8")),
                             {"query": "q", "max_results": 5})


if __name__ == "__main__":
    unittest.main()
