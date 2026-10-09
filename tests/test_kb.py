# -*- coding: utf-8 -*-
import glob
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

    def test_source_without_snippet_survives(self):
        # 旧报告只存了 title/url，缺 snippet 键也不能丢掉整条来源
        kb.absorb([_node(sources=[{"title": "旧报告", "url": "https://old", "date": "2023"}])],
                  {"id": "j1"})
        sources = store.load_knowledge()["nodes"][0]["sources"]
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["snippet"], "")
        self.assertEqual(sources[0]["date"], "2023")

    def test_source_missing_accessed_is_backfilled(self):
        kb.absorb([_node(sources=[{"title": "t", "url": "https://a"}])], {"id": "j1"})
        self.assertEqual(store.load_knowledge()["nodes"][0]["sources"][0]["accessed"], "")

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


class MergeFieldsTest(IsolatedCase):
    def test_same_term_merges_instead_of_adding(self):
        kb.absorb([_node(sources=[_src("https://a")])], {"id": "j1", "job_title": "岗1"})
        added, merged = kb.absorb([_node(sources=[_src("https://b")])], {"id": "j2", "job_title": "岗2"})
        self.assertEqual((added, merged), (0, 1))
        cards = store.load_knowledge()["nodes"]
        self.assertEqual(len(cards), 1)
        self.assertEqual(len(cards[0]["sources"]), 2)
        self.assertEqual([j["id"] for j in cards[0]["from_jds"]], ["j1", "j2"])

    def test_from_jds_is_idempotent(self):
        kb.absorb([_node()], {"id": "j1"})
        kb.absorb([_node()], {"id": "j1"})
        kb.absorb([_node()], {"id": "j1"})
        self.assertEqual(len(store.load_knowledge()["nodes"][0]["from_jds"]), 1)
        self.assertEqual(len(store.load_knowledge()["nodes"][0]["sources"]), 0)

    def test_case_and_spacing_variants_are_same_card(self):
        kb.absorb([_node(term="Transformer 架构")], {"id": "j1"})
        kb.absorb([_node(term="transformer架构")], {"id": "j2"})
        cards = store.load_knowledge()["nodes"]
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["term"], "Transformer 架构")   # 保留首次写法

    def test_user_progress_is_never_overwritten(self):
        kb.absorb([_node()], {"id": "j1"})
        kb.set_state("省略恢复", "已掌握")
        kb.absorb([_node(), _node(term="省略恢复")], {"id": "j2"})
        card = store.load_knowledge()["nodes"][0]
        self.assertEqual(card["state"], "已掌握")

    def test_asked_count_never_decreases(self):
        kb.absorb([_node()], {"id": "j1"})
        d = store.load_knowledge()
        d["nodes"][0]["asked_count"] = 3
        store.save_knowledge(d)
        kb.absorb([_node()], {"id": "j2"})
        self.assertEqual(store.load_knowledge()["nodes"][0]["asked_count"], 3)

    def test_last_outcome_never_overwritten(self):
        kb.absorb([_node()], {"id": "j1"})
        d = store.load_knowledge()
        d["nodes"][0]["last_outcome"] = "已面试"
        store.save_knowledge(d)
        kb.absorb([_node()], {"id": "j2"})
        self.assertEqual(store.load_knowledge()["nodes"][0]["last_outcome"], "已面试")

    def test_null_progress_fields_are_backfilled(self):
        # knowledge.json 是用户可编辑的：显式 null 不能靠 setdefault 修掉
        kb.absorb([_node()], {"id": "j1"})
        d = store.load_knowledge()
        d["nodes"][0].update({"state": None, "last_outcome": None, "asked_count": None})
        store.save_knowledge(d)
        kb.absorb([_node()], {"id": "j2"})
        card = store.load_knowledge()["nodes"][0]
        self.assertEqual(card["state"], kb.DEFAULT_STATE)
        self.assertEqual(card["last_outcome"], kb.DEFAULT_OUTCOME)
        self.assertEqual(card["asked_count"], 0)

    def test_progress_fields_with_real_values_are_untouched(self):
        kb.absorb([_node()], {"id": "j1"})
        d = store.load_knowledge()
        d["nodes"][0].update({"state": "已掌握", "last_outcome": "已面试", "asked_count": "3"})
        store.save_knowledge(d)
        kb.absorb([_node()], {"id": "j2"})
        card = store.load_knowledge()["nodes"][0]
        self.assertEqual(card["state"], "已掌握")
        self.assertEqual(card["last_outcome"], "已面试")
        self.assertEqual(card["asked_count"], 3)     # 字符串数字也顺手修好

    def test_timeline_keeps_richer(self):
        kb.absorb([_node(timeline=[{"year": "2017", "text": "a"}])], {"id": "j1"})
        kb.absorb([_node(timeline=[{"year": "2017", "text": "a"}, {"year": "2020", "text": "b"}])],
                  {"id": "j2"})
        self.assertEqual(len(store.load_knowledge()["nodes"][0]["timeline"]), 2)

    def test_timeline_not_downgraded_by_shorter(self):
        kb.absorb([_node(timeline=[{"year": "2017", "text": "a"}, {"year": "2020", "text": "b"}])],
                  {"id": "j1"})
        kb.absorb([_node(timeline=[{"year": "2017", "text": "a"}])], {"id": "j2"})
        self.assertEqual(len(store.load_knowledge()["nodes"][0]["timeline"]), 2)

    def test_timeline_equal_length_keeps_existing(self):
        # 长度相等时保留原有，绝不用新节点覆盖（换掉会丢用户已看过的内容）
        kb.absorb([_node(timeline=[{"year": "2017", "text": "a"}])], {"id": "j1"})
        kb.absorb([_node(timeline=[{"year": "1999", "text": "z"}])], {"id": "j2"})
        self.assertEqual(store.load_knowledge()["nodes"][0]["timeline"],
                         [{"year": "2017", "text": "a"}])

    def test_definition_not_overwritten_by_shorter(self):
        kb.absorb([_node(definition="这是一段很长的完整定义内容")], {"id": "j1"})
        kb.absorb([_node(definition="短")], {"id": "j2"})
        self.assertEqual(store.load_knowledge()["nodes"][0]["definition"], "这是一段很长的完整定义内容")

    def test_definition_upgraded_by_longer(self):
        kb.absorb([_node(definition="短")], {"id": "j1"})
        kb.absorb([_node(definition="更长更完整的定义内容在这里")], {"id": "j2"})
        self.assertEqual(store.load_knowledge()["nodes"][0]["definition"], "更长更完整的定义内容在这里")

    def test_layer_takes_min(self):
        kb.absorb([_node(layer=1)], {"id": "j1"})
        kb.absorb([_node(layer=0)], {"id": "j2"})
        self.assertEqual(store.load_knowledge()["nodes"][0]["layer"], 0)

    def test_negative_layer_cannot_pin_the_card(self):
        # min 是单调的：不夹下界的话 -3 写进去就再也回不来
        kb.absorb([_node(layer=-3)], {"id": "j1"})
        self.assertEqual(store.load_knowledge()["nodes"][0]["layer"], 0)
        kb.absorb([_node(layer=1)], {"id": "j2"})
        self.assertEqual(store.load_knowledge()["nodes"][0]["layer"], 0)

    def test_category_keeps_first_nonempty(self):
        kb.absorb([_node(category="NLP")], {"id": "j1"})
        kb.absorb([_node(category="其他")], {"id": "j2"})
        self.assertEqual(store.load_knowledge()["nodes"][0]["category"], "NLP")

    def test_related_union_by_id(self):
        kb.absorb([_node(related=[{"id": "a", "relation": "属于"}])], {"id": "j1"})
        kb.absorb([_node(related=[{"id": "a", "relation": "对比"}, {"id": "b", "relation": "前置"}])],
                  {"id": "j2"})
        rel = store.load_knowledge()["nodes"][0]["related"]
        self.assertEqual([r["id"] for r in rel], ["a", "b"])
        self.assertEqual(rel[0]["relation"], "属于")

    def test_interview_questions_union_with_cap(self):
        kb.absorb([_node(interview_questions=["q%d" % i for i in range(6)])], {"id": "j1"})
        kb.absorb([_node(interview_questions=["q%d" % i for i in range(6, 12)])], {"id": "j2"})
        qs = store.load_knowledge()["nodes"][0]["interview_questions"]
        self.assertEqual(len(qs), kb.MAX_QUESTIONS)
        self.assertEqual(qs[0], "q0")

    def test_confidence_upgrades_when_sources_arrive(self):
        kb.absorb([_node(sources=[])], {"id": "j1"})
        self.assertEqual(store.load_knowledge()["nodes"][0]["confidence"], "ai-generated")
        kb.absorb([_node(sources=[_src("https://a")])], {"id": "j2"})
        self.assertEqual(store.load_knowledge()["nodes"][0]["confidence"], "verified")

    def test_last_seen_updates_and_first_seen_stays(self):
        kb.absorb([_node()], {"id": "j1"})
        first = store.load_knowledge()["nodes"][0]["first_seen"]
        kb.absorb([_node()], {"id": "j2"})
        card = store.load_knowledge()["nodes"][0]
        self.assertEqual(card["first_seen"], first)
        self.assertTrue(card["last_seen"])


class ResilienceTest(IsolatedCase):
    def test_malformed_node_fields_do_not_raise(self):
        weird = {"term": "怪节点", "sources": "not-a-list", "timeline": {"a": 1},
                 "related": 42, "interview_questions": None, "layer": "abc",
                 "definition": None, "plain_explanation": 7}
        added, merged = kb.absorb([weird], {"id": "j1"})
        self.assertEqual((added, merged), (1, 0))
        card = store.load_knowledge()["nodes"][0]
        self.assertEqual(card["sources"], [])
        self.assertEqual(card["timeline"], [])
        self.assertEqual(card["related"], [])

    def test_sources_containing_non_dict_are_ignored(self):
        kb.absorb([_node(sources=["junk", 5, _src("https://ok"), {"url": ""}])], {"id": "j1"})
        self.assertEqual(len(store.load_knowledge()["nodes"][0]["sources"]), 1)

    def test_corrupt_knowledge_json_returns_empty_without_crash(self):
        store.ensure_profile()
        store.write_text(store.knowledge_path(), "{ this is not json")
        self.assertEqual(store.load_knowledge(), {"nodes": []})
        added, _ = kb.absorb([_node()], {"id": "j1"})
        self.assertEqual(added, 1)          # 损坏后能从空库继续

    def test_corrupt_knowledge_json_is_backed_up_not_overwritten(self):
        # 一次坏读不能等于清空用户全部积累：先改名备份，再从空库继续
        store.ensure_profile()
        path = store.knowledge_path()
        store.write_text(path, "{ this is not json")
        added, _ = kb.absorb([_node()], {"id": "j1"})
        self.assertEqual(added, 1)
        backups = glob.glob(path + ".corrupt-*")
        self.assertEqual(len(backups), 1)
        self.assertEqual(store.read_text(backups[0]), "{ this is not json")
        self.assertEqual(store.load_knowledge()["nodes"][0]["term"], "省略恢复")

    def test_valid_knowledge_json_is_never_backed_up(self):
        # 正常空库 {"nodes": []} 解析成功，绝不能被误当成损坏文件备份走
        store.ensure_profile()
        store.save_knowledge({"nodes": []})
        kb.absorb([_node()], {"id": "j1"})
        self.assertEqual(glob.glob(store.knowledge_path() + ".corrupt-*"), [])

    def test_junk_entries_in_from_jds_are_cleaned(self):
        # 历史垃圾项（非 dict）不该永久留在卡上给统计/回填/前端踩
        store.ensure_profile()
        store.save_knowledge({"nodes": [{"id": "省略恢复", "term": "省略恢复",
                                         "from_jds": ["junk", 5, None]}]})
        kb.absorb([_node()], {"id": "j1"})
        frm = store.load_knowledge()["nodes"][0]["from_jds"]
        self.assertEqual(len(frm), 1)
        self.assertEqual([j.get("id") for j in frm if isinstance(j, dict)], ["j1"])

    def test_knowledge_json_with_wrong_shape_is_recovered(self):
        store.ensure_profile()
        store.write_json(store.knowledge_path(), {"nodes": "nope"})
        self.assertEqual(store.load_knowledge(), {"nodes": []})

    def test_layer_string_is_tolerated(self):
        kb.absorb([_node(layer="1")], {"id": "j1"})
        self.assertEqual(store.load_knowledge()["nodes"][0]["layer"], 1)
