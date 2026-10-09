# -*- coding: utf-8 -*-
import unittest

import kb
import store

from tests.base import IsolatedCase


class NormalizeIdTest(unittest.TestCase):
    def test_lowercases_and_strips_spaces(self):
        self.assertEqual(kb.normalize_id("Transformer 架构"), "transformer架构")

    def test_strips_chinese_and_ascii_punctuation(self):
        self.assertEqual(kb.normalize_id("省略恢复（Ellipsis）"), "省略恢复ellipsis")
        self.assertEqual(kb.normalize_id("A/B、C·D"), "abcd")

    def test_folds_fullwidth_latin(self):
        self.assertEqual(kb.normalize_id("ＡＢＣ"), "abc")
        self.assertEqual(kb.normalize_id("ＡＩ标注"), kb.normalize_id("AI标注"))

    def test_folds_fullwidth_digits_and_parens(self):
        self.assertEqual(kb.normalize_id("（１）"), "1")
        self.assertEqual(kb.normalize_id("ＫＢ＿ｖ２"), "kbv2")

    def test_empty_and_punctuation_only_become_empty(self):
        self.assertEqual(kb.normalize_id(""), "")
        self.assertEqual(kb.normalize_id("   "), "")
        self.assertEqual(kb.normalize_id("（）·、。"), "")
        self.assertEqual(kb.normalize_id(None), "")
        self.assertEqual(kb.normalize_id(123), "")


class EnsureKbTest(unittest.TestCase):
    def test_non_dict_becomes_empty(self):
        for bad in (None, [], "x", 3):
            self.assertEqual(kb.ensure_kb(bad), {"version": kb.KB_VERSION, "nodes": []})

    def test_missing_nodes_becomes_empty_list(self):
        self.assertEqual(kb.ensure_kb({"version": 1}), {"version": 1, "nodes": []})

    def test_drops_non_dict_nodes(self):
        out = kb.ensure_kb({"nodes": [{"id": "a"}, "junk", 5, None]})
        self.assertEqual(len(out["nodes"]), 1)


def _node(**kw):
    n = {"id": "x", "term": "省略恢复", "definition": "把省略的成分补全",
         "plain_explanation": "一句话说不完整，靠上下文补齐", "category": "NLP",
         "layer": 0, "sources": [], "timeline": [], "related": [],
         "interview_questions": [], "confidence": "ai-generated"}
    n.update(kw)
    return n


def _src(url, title="t", snippet="s", date="2024"):
    return {"title": title, "url": url, "snippet": snippet, "date": date, "accessed": "2026-10-08"}


class AbsorbTest(IsolatedCase):
    def test_new_term_creates_card(self):
        added, merged = kb.absorb([_node(sources=[_src("https://a")])],
                                 {"id": "job1", "job_title": "标注", "company": "X"})
        self.assertEqual((added, merged), (1, 0))
        card = store.load_knowledge()["nodes"][0]
        self.assertEqual(card["id"], "省略恢复")
        self.assertEqual(card["term"], "省略恢复")
        self.assertEqual(card["state"], "待学习")
        self.assertEqual(card["confidence"], "verified")
        self.assertEqual([j["id"] for j in card["from_jds"]], ["job1"])
        self.assertEqual(len(card["sources"]), 1)

    def test_sources_dedupe_by_url(self):
        kb.absorb([_node(sources=[_src("https://a"), _src("https://a", title="dup")])], {"id": "j1"})
        self.assertEqual(len(store.load_knowledge()["nodes"][0]["sources"]), 1)

    def test_duplicate_url_keeps_richer_snippet_and_fills_blanks(self):
        kb.absorb([_node(sources=[{"title": "", "url": "https://a", "snippet": "short"}])], {"id": "j1"})
        kb.absorb([_node(sources=[{"title": "标题", "url": "https://a", "snippet": "much longer snippet",
                                   "date": "2024"}])], {"id": "j2"})
        s = store.load_knowledge()["nodes"][0]["sources"][0]
        self.assertEqual(s["snippet"], "much longer snippet")
        self.assertEqual(s["title"], "标题")
        self.assertEqual(s["date"], "2024")

    def test_source_without_url_is_dropped(self):
        kb.absorb([_node(sources=[{"title": "no url"}, {"url": "  "}, _src("https://ok")])], {"id": "j1"})
        self.assertEqual(len(store.load_knowledge()["nodes"][0]["sources"]), 1)

    def test_snippet_truncated(self):
        kb.absorb([_node(sources=[_src("https://a", snippet="x" * 1000)])], {"id": "j1"})
        s = store.load_knowledge()["nodes"][0]["sources"][0]
        self.assertEqual(len(s["snippet"]), kb.SNIPPET_LIMIT)

    def test_empty_term_is_skipped(self):
        added, _ = kb.absorb([_node(term=""), _node(term="（）")], {"id": "j1"})
        self.assertEqual(added, 0)
        self.assertEqual(store.load_knowledge()["nodes"], [])

    def test_non_dict_node_is_skipped(self):
        added, _ = kb.absorb(["junk", None, 3], {"id": "j1"})
        self.assertEqual(added, 0)
