# -*- coding: utf-8 -*-
import os
import shutil
import tempfile
import unittest

import knowledge
import log
import store


def fake_chat_factory(responses):
    calls = []

    def fake_chat(base_url, api_key, model, messages, **kw):
        calls.append(messages)
        return responses.pop(0)

    fake_chat.calls = calls
    return fake_chat


class _Isolated(unittest.TestCase):
    """隔离：knowledge 内部会调 log_event，必须重定向到临时目录，
    否则会往用户真实的 data/profiles/.../logs/events.jsonl 追加记录。"""

    def setUp(self):
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


class ExtractTest(_Isolated):
    def test_extract_returns_nodes(self):
        chat = fake_chat_factory(['{"nodes":[{"id":"rlhf","term":"RLHF","definition":"d",'
                                  '"plain_explanation":"p","category":"核心概念"}]}'])
        nodes = knowledge.extract_nodes("JD文本", {"text_model": {"base_url": "u", "api_key": "k",
                                                                  "model": "m"}}, chat=chat)
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0]["id"], "rlhf")
        self.assertEqual(nodes[0]["layer"], 0)

    def test_extract_assigns_layer_zero(self):
        chat = fake_chat_factory(['{"nodes":[{"id":"a","term":"A","definition":"d","plain_explanation":"p"}]}'])
        nodes = knowledge.extract_nodes("JD", {"text_model": {"base_url": "u", "api_key": "k", "model": "m"}},
                                        chat=chat)
        self.assertEqual(nodes[0]["layer"], 0)

    def test_extract_handles_empty(self):
        chat = fake_chat_factory(['{"nodes":[]}'])
        self.assertEqual(knowledge.extract_nodes("JD", {"text_model": {"base_url": "u", "api_key": "k",
                                                                       "model": "m"}}, chat=chat), [])


class SourceMergeTest(_Isolated):
    def test_merge_sources_dedupes_by_url(self):
        a = [{"url": "https://x", "title": "T"}]
        b = [{"url": "https://x", "title": "T2"}, {"url": "https://y", "title": "T3"}]
        out = knowledge.merge_sources(a, b)
        self.assertEqual(len(out), 2)
        self.assertEqual({s["url"] for s in out}, {"https://x", "https://y"})


class BrainstormTest(_Isolated):
    def test_brainstorm_adds_layer1_nodes(self):
        chat = fake_chat_factory(['{"related":[{"id":"sft","term":"SFT","definition":"d",'
                                  '"plain_explanation":"p","relation":"前置"}]}'])
        core = [knowledge.normalize_node({"id": "rlhf", "term": "RLHF", "definition": "d",
                                          "plain_explanation": "p"}, 0)]
        nodes = knowledge.brainstorm(core, {"text_model": {"base_url": "u", "api_key": "k",
                                                           "model": "m"}}, chat=chat)
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0]["layer"], 1)
        self.assertEqual(core[0]["related"][0]["id"], "sft")

    def test_brainstorm_skips_when_empty(self):
        self.assertEqual(knowledge.brainstorm([], {"text_model": {"base_url": "u", "api_key": "k",
                                                                 "model": "m"}}), [])


class SourceAndTimelineTest(_Isolated):
    def test_attach_sources_marks_verified(self):
        def fake_search(query, providers=None, **kw):
            return [{"title": "T", "url": "https://x", "snippet": "s", "date": "2022"}]

        nodes = [knowledge.normalize_node({"id": "a", "term": "A", "definition": "d",
                                           "plain_explanation": "p"})]
        out = knowledge.attach_sources(nodes, [{"name": "t", "base_url": "u", "api_key": "k"}],
                                       search=fake_search)
        self.assertEqual(out[0]["confidence"], "verified")
        self.assertEqual(out[0]["sources"][0]["url"], "https://x")

    def test_attach_sources_marks_ai_when_no_result(self):
        def empty_search(query, providers=None, **kw):
            return []

        nodes = [knowledge.normalize_node({"id": "a", "term": "A", "definition": "d",
                                           "plain_explanation": "p"})]
        out = knowledge.attach_sources(nodes, [{"name": "t", "base_url": "u", "api_key": "k"}],
                                       search=empty_search)
        self.assertEqual(out[0]["confidence"], "ai-generated")
        self.assertEqual(out[0]["sources"], [])

    def test_attach_timeline_when_verified(self):
        chat = fake_chat_factory(['{"timeline":[{"year":"2022","text":"ChatGPT 发布"}]}'])
        nodes = [{"id": "a", "term": "A", "definition": "d", "plain_explanation": "p",
                  "sources": [{"title": "T", "url": "https://x", "snippet": "s", "date": ""}],
                  "confidence": "verified", "timeline": [], "related": [], "layer": 0}]
        out = knowledge.attach_timeline(nodes, {"text_model": {"base_url": "u", "api_key": "k",
                                                               "model": "m"}}, chat=chat)
        self.assertEqual(out[0]["timeline"][0]["year"], "2022")


class ResilienceTest(_Isolated):
    def test_brainstorm_survives_non_list_related(self):
        # 合法 JSON 但 related 不是数组 → 不能抛，其他核心点要照常处理
        chat = fake_chat_factory(['{"related":"hello"}',
                                  '{"related":[{"id":"b1","term":"B1","definition":"d",'
                                  '"plain_explanation":"p","relation":"前置"}]}'])
        core = [knowledge.normalize_node({"id": "a", "term": "A", "definition": "d",
                                          "plain_explanation": "p"}, 0),
                knowledge.normalize_node({"id": "b", "term": "B", "definition": "d",
                                          "plain_explanation": "p"}, 0)]
        out = knowledge.brainstorm(core, {"text_model": {"base_url": "u", "api_key": "k",
                                                         "model": "m"}}, chat=chat)
        self.assertEqual([n["id"] for n in out], ["b1"])

    def test_extract_returns_empty_on_unparseable(self):
        chat = fake_chat_factory(["完全不是 JSON"])
        self.assertEqual(knowledge.extract_nodes("JD", {"text_model": {"base_url": "u",
                                                                       "api_key": "k", "model": "m"}},
                                                 chat=chat), [])

    def test_extract_returns_empty_on_non_list_nodes(self):
        chat = fake_chat_factory(['{"nodes":"oops"}'])
        self.assertEqual(knowledge.extract_nodes("JD", {"text_model": {"base_url": "u",
                                                                       "api_key": "k", "model": "m"}},
                                                 chat=chat), [])

    def test_attach_timeline_survives_bad_snippet_type(self):
        chat = fake_chat_factory(['{"timeline":[{"year":"2022","text":"X"}]}'])
        nodes = [{"id": "a", "term": "A", "definition": "d", "plain_explanation": "p",
                  "sources": [{"title": "T", "url": "https://x", "snippet": {"unexpected": "shape"}}],
                  "confidence": "verified", "timeline": [], "related": [], "layer": 0}]
        out = knowledge.attach_timeline(nodes, {"text_model": {"base_url": "u", "api_key": "k",
                                                               "model": "m"}}, chat=chat)
        self.assertEqual(out[0]["timeline"][0]["year"], "2022")

    def test_attach_timeline_skips_sourceless_without_calling_chat(self):
        def must_not_call(*a, **kw):
            raise AssertionError("对没有来源的节点绝不该调用模型")

        nodes = [{"id": "a", "term": "A", "definition": "d", "plain_explanation": "p",
                  "sources": [], "confidence": "ai-generated", "timeline": [], "related": [], "layer": 0}]
        out = knowledge.attach_timeline(nodes, {"text_model": {"base_url": "u", "api_key": "k",
                                                               "model": "m"}}, chat=must_not_call)
        self.assertEqual(out[0]["timeline"], [])


if __name__ == "__main__":
    unittest.main()
