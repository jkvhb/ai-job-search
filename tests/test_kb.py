# -*- coding: utf-8 -*-
import glob
import json
import os
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


class DuplicateTest(IsolatedCase):
    def _seed(self, terms):
        kb.absorb([_node(term=t) for t in terms], {"id": "j1"})

    def test_detects_containment_pair(self):
        self._seed(["省略恢复", "省略识别及恢复"])
        dups = kb.find_duplicates(store.load_knowledge())
        self.assertEqual(len(dups), 1)
        self.assertEqual({dups[0]["a"], dups[0]["b"]}, {"省略恢复", "省略识别及恢复"})

    def test_detects_tail_word_variant(self):
        self._seed(["根因分析", "根因分析方法"])
        self.assertTrue(kb.find_duplicates(store.load_knowledge()))

    def test_does_not_pair_unrelated_terms(self):
        self._seed(["Transformer 架构", "抽样检验", "幻觉", "标注规范"])
        for d in kb.find_duplicates(store.load_knowledge()):
            self.assertNotEqual({d["a"], d["b"]}, {"transformer架构", "抽样检验"})

    def test_short_terms_do_not_generate_prefix_noise(self):
        self._seed(["抽样", "抽取", "抽检"])
        self.assertEqual(kb.find_duplicates(store.load_knowledge()), [])

    def test_two_char_terms_never_become_subsequence_pairs(self):
        # 「插入式」分支要求短词 ≥3 字：2 字词极易互相成为有序子序列，
        # 没有这道门槛会刷出一堆假配对（这条测试就是钉住这道门槛的）
        self._seed(["抽样", "抽检", "抽取", "标注", "标准", "指标", "幻觉", "错觉"])
        self.assertEqual(kb.find_duplicates(store.load_knowledge()), [])

    def test_subsequence_variant_is_detected(self):
        # 「省略恢复」不是「省略识别及恢复」的连续子串（中间插了「识别及」），
        # 但每个字按序都能对上 —— 这是本领域真实存在的重复来源
        self._seed(["省略恢复", "省略识别及恢复"])
        dups = kb.find_duplicates(store.load_knowledge())
        self.assertEqual(len(dups), 1)
        self.assertEqual(dups[0]["reason"], "插入式")

    def test_limit_is_respected(self):
        self._seed(["概念%d号" % i for i in range(40)])
        self.assertLessEqual(len(kb.find_duplicates(store.load_knowledge())), 20)


class ManualMergeTest(IsolatedCase):
    def test_merge_moves_sources_and_records_alias(self):
        kb.absorb([_node(term="省略恢复", sources=[_src("https://a")])], {"id": "j1"})
        kb.absorb([_node(term="省略识别及恢复", sources=[_src("https://b")])], {"id": "j2"})
        ok, msg = kb.merge_nodes("省略恢复", "省略识别及恢复")
        self.assertTrue(ok, msg)
        cards = store.load_knowledge()["nodes"]
        self.assertEqual(len(cards), 1)
        self.assertEqual(len(cards[0]["sources"]), 2)
        self.assertIn("省略识别及恢复", cards[0]["aliases"])
        self.assertEqual([j["id"] for j in cards[0]["from_jds"]], ["j1", "j2"])

    def test_merge_keeps_target_progress(self):
        kb.absorb([_node(term="A词", sources=[_src("https://a")])], {"id": "j1"})
        kb.absorb([_node(term="B词", sources=[_src("https://b")])], {"id": "j2"})
        kb.set_state("A词", "已掌握")
        kb.set_state("B词", "待学习")
        kb.merge_nodes("A词", "B词")
        self.assertEqual(store.load_knowledge()["nodes"][0]["state"], "已掌握")

    def test_merge_rejects_same_id(self):
        kb.absorb([_node(term="A词")], {"id": "j1"})
        ok, msg = kb.merge_nodes("A词", "A词")
        self.assertFalse(ok)
        self.assertIn("自己", msg)

    def test_merge_rejects_unknown_id(self):
        kb.absorb([_node(term="A词")], {"id": "j1"})
        ok, msg = kb.merge_nodes("A词", "不存在的词")
        self.assertFalse(ok)
        self.assertEqual(len(store.load_knowledge()["nodes"]), 1)

    def test_merge_is_idempotent_when_target_gone(self):
        kb.absorb([_node(term="A词")], {"id": "j1"})
        kb.absorb([_node(term="B词")], {"id": "j2"})
        kb.merge_nodes("A词", "B词")
        ok, _ = kb.merge_nodes("A词", "B词")
        self.assertFalse(ok)

    def test_entry_points_accept_raw_terms_and_ids_alike(self):
        kb.absorb([_node(term="Transformer 架构")], {"id": "j1"})
        # 归一化 id
        self.assertTrue(kb.set_state("transformer架构", "学习中")[0])
        # 原始术语（含大小写/空格差异）
        self.assertTrue(kb.set_state("Transformer 架构", "已掌握")[0])
        self.assertEqual(store.load_knowledge()["nodes"][0]["state"], "已掌握")
        # merge 同理
        kb.absorb([_node(term="省略识别及恢复")], {"id": "j2"})
        ok, msg = kb.merge_nodes("Transformer 架构", "省略识别及恢复")
        self.assertTrue(ok, msg)


class QueryTest(IsolatedCase):
    def setUp(self):
        super().setUp()
        kb.absorb([
            _node(term="省略恢复", category="NLP", layer=0, sources=[_src("https://a")],
                  definition="把省略成分补全", interview_questions=["q"]),
            _node(term="抽样检验", category="数据标注/质检", layer=1, sources=[],
                  definition="统计抽样"),
            _node(term="Kappa系数", category="数据标注/质检", layer=1, sources=[_src("https://b")],
                  definition="标注一致性强度的度量"),
        ], {"id": "j1", "job_title": "岗位一"})
        kb.absorb([_node(term="省略恢复", sources=[_src("https://c")])],
                  {"id": "j2", "job_title": "岗位二"})

    def test_stats_shape(self):
        s = kb.stats(store.load_knowledge())
        self.assertEqual(s["total"], 3)
        self.assertEqual(s["verified"], 2)
        self.assertEqual(s["multi_jd"], 1)
        self.assertEqual(s["sources"], 3)
        self.assertEqual(s["by_state"]["待学习"], 3)

    def test_stats_on_empty_kb(self):
        s = kb.stats({"nodes": []})
        self.assertEqual(s["total"], 0)
        self.assertEqual(s["by_state"], {"待学习": 0, "学习中": 0, "已掌握": 0})

    def test_search_matches_term_and_definition(self):
        self.assertEqual(len(kb.list_nodes(store.load_knowledge(), q="省略")), 1)
        self.assertEqual(len(kb.list_nodes(store.load_knowledge(), q="统计抽样")), 1)
        self.assertEqual(len(kb.list_nodes(store.load_knowledge(), q="不存在的词")), 0)

    def test_filter_by_state_and_category(self):
        kb.set_state("省略恢复", "已掌握")
        self.assertEqual(len(kb.list_nodes(store.load_knowledge(), state="已掌握")), 1)
        self.assertEqual(len(kb.list_nodes(store.load_knowledge(), category="数据标注/质检")), 2)

    def test_filter_by_jd(self):
        self.assertEqual(len(kb.list_nodes(store.load_knowledge(), jd_id="j2")), 1)

    def test_sort_by_mentions_puts_multi_jd_first(self):
        out = kb.list_nodes(store.load_knowledge(), sort="mentions")
        self.assertEqual(out[0]["term"], "省略恢复")

    def test_sort_by_term_is_deterministic(self):
        out = kb.list_nodes(store.load_knowledge(), sort="term")
        self.assertEqual([n["term"] for n in out], sorted(n["term"] for n in out))

    def test_unknown_sort_falls_back_to_mentions(self):
        self.assertEqual(len(kb.list_nodes(store.load_knowledge(), sort="bogus")), 3)


class StateTest(IsolatedCase):
    def test_set_state_updates_only_state(self):
        kb.absorb([_node(sources=[_src("https://a")])], {"id": "j1"})
        before = json.loads(json.dumps(store.load_knowledge()["nodes"][0]))
        ok, _ = kb.set_state("省略恢复", "学习中")
        self.assertTrue(ok)
        after = store.load_knowledge()["nodes"][0]
        self.assertEqual(after["state"], "学习中")
        before["state"] = "学习中"
        self.assertEqual(after, before)

    def test_set_state_rejects_bad_value(self):
        kb.absorb([_node()], {"id": "j1"})
        ok, msg = kb.set_state("省略恢复", "随便")
        self.assertFalse(ok)
        self.assertIn("状态不合法", msg)
        self.assertEqual(store.load_knowledge()["nodes"][0]["state"], "待学习")

    def test_set_state_unknown_id(self):
        ok, msg = kb.set_state("不存在", "已掌握")
        self.assertFalse(ok)
        self.assertIn("找不到", msg)


class ImportReportsTest(IsolatedCase):
    def _write_report(self, name, payload):
        store.ensure_profile()
        path = os.path.join(store.p_path("reports"), name)
        store.write_text(path, "<html><body><script>\n%s%s;\n</script></body></html>"
                         % (kb.REPORT_MARK, json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")))
        return path

    def test_imports_knowledge_from_report(self):
        self._write_report("20261008_岗A_80分.html", {
            "report_id": "20261008_岗A_80分", "job_title": "岗A", "company": "甲公司",
            "knowledge": [_node(term="省略恢复", sources=[_src("https://a")])]})
        files, added, merged = kb.absorb_reports()
        self.assertEqual((files, added, merged), (1, 1, 0))
        card = store.load_knowledge()["nodes"][0]
        self.assertEqual(card["from_jds"][0]["job_title"], "岗A")
        self.assertEqual(card["from_jds"][0]["id"], "20261008_岗A_80分")
        self.assertEqual(card["from_jds"][0]["date"], "2026-10-08")

    def test_import_is_idempotent(self):
        self._write_report("20261008_岗A_80分.html", {
            "report_id": "20261008_岗A_80分", "job_title": "岗A",
            "knowledge": [_node(sources=[_src("https://a")])]})
        kb.absorb_reports()
        kb.absorb_reports()
        cards = store.load_knowledge()["nodes"]
        self.assertEqual(len(cards), 1)
        self.assertEqual(len(cards[0]["from_jds"]), 1)
        self.assertEqual(len(cards[0]["sources"]), 1)

    def test_knowledge_as_dict_shape_is_supported(self):
        self._write_report("20261001_岗B_70分.html", {
            "report_id": "20261001_岗B_70分", "knowledge": {"nodes": [_node(term="幻觉")]}})
        files, added, _ = kb.absorb_reports()
        self.assertEqual((files, added), (1, 1))

    def test_report_without_knowledge_is_skipped(self):
        self._write_report("20260901_岗C_60分.html", {"report_id": "20260901_岗C_60分"})
        files, added, _ = kb.absorb_reports()
        self.assertEqual((files, added), (0, 0))

    def test_unreadable_report_does_not_crash(self):
        store.ensure_profile()
        store.write_text(os.path.join(store.p_path("reports"), "坏报告.html"), "<html>no data")
        files, added, _ = kb.absorb_reports()
        self.assertEqual((files, added), (0, 0))

    def test_empty_folder_is_fine(self):
        store.ensure_profile()
        self.assertEqual(kb.absorb_reports(), (0, 0, 0))
