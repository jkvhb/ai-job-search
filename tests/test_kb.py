# -*- coding: utf-8 -*-
import glob
import io
import json
import log
import os
import unittest
from unittest import mock

import app
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
    # 默认 definition 刻意不含任何被测术语的字（曾用「把省略的成分补全」，含「省略」，
    # 把 Task 7 的 q="省略" 计数断言污染成假绿；默认值必须与所有术语无关）
    n = {"id": "x", "term": "省略恢复", "definition": "默认定义",
         "plain_explanation": "一句话说不完整，靠上下文补齐", "category": "NLP",
         "layer": 0, "sources": [], "timeline": [], "related": [],
         "interview_questions": [], "confidence": "ai-generated"}
    n.update(kw)
    return n


def _src(url, title="t", snippet="s", date="2024"):
    return {"title": title, "url": url, "snippet": snippet, "date": date, "accessed": "2026-10-08"}


def _log_events(name):
    """读出隔离目录里的事件日志（IsolatedCase 已把 log.LOG_FILE 指到临时目录）"""
    path = log.LOG_FILE
    if not os.path.exists(path):
        return []
    return [e for e in (json.loads(l) for l in store.read_text(path).splitlines() if l.strip())
            if e.get("event") == name]


def _real_jd_ids(card):
    """卡片里「算数」的来源岗位 id：只认有 id 的 dict（与 kb._add_jd 同一把尺子）"""
    return [x.get("id") for x in kb._as_list(card.get("from_jds"))
            if isinstance(x, dict) and x.get("id")]


def _consistent_multi_jd(kb_data):
    """按「算数的来源岗位」重算 multi_jd：stats() 必须与它一致，否则两处口径打架"""
    return sum(1 for n in kb_data["nodes"] if len(_real_jd_ids(n)) > 1)


def _consistent_jd_ids(kb_data):
    """all_jds() 应该返回的岗位 id 全集（同一把尺子）"""
    return sorted({i for n in kb_data["nodes"] for i in _real_jd_ids(n)})


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

    def test_corrupt_file_survives_when_backup_rename_fails(self):
        # 改名失败还照写，等于把用户的积累用空库顶掉 —— 宁可这次不沉淀
        store.ensure_profile()
        path = store.knowledge_path()
        store.write_text(path, "{ this is not json")
        # 用 patch.object 而不是直接给 kb.os.replace 赋值：后者是**进程级**改动 stdlib 的 os 模块，
        # 断言中途抛错就永久留在原地，会污染之后的每一个测试
        with mock.patch.object(kb.os, "replace", side_effect=OSError("rename failed")):
            added, merged = kb.absorb([_node()], {"id": "j1"})
        self.assertEqual((added, merged), (0, 0))
        self.assertEqual(store.read_text(path), "{ this is not json")   # 坏文件原样保留
        self.assertEqual(glob.glob(path + ".corrupt-*"), [])            # 没有半成品备份
        self.assertTrue(_log_events("kb.corrupt_backup_failed"))

    def test_two_quarantines_in_the_same_second_do_not_overwrite_each_other(self):
        # 备份名原先只到秒：同秒两次隔离会互相覆盖，只留下一个备份
        store.ensure_profile()
        path = store.knowledge_path()
        for i in (1, 2):
            store.write_text(path, "{ 坏文件 %d" % i)
            kb.absorb([_node()], {"id": "j%d" % i})
        backups = glob.glob(path + ".corrupt-*")
        self.assertEqual(len(backups), 2)
        self.assertEqual(sorted(store.read_text(x) for x in backups),
                         ["{ 坏文件 1", "{ 坏文件 2"])

    def test_quarantine_sequence_yields_unique_values(self):
        # 原实现是 `_QUARANTINE_SEQ += 1`（LOAD/ADD/STORE 三步）：ThreadingHTTPServer 下两个
        # 请求线程可能读到同一个旧值，备份名相撞 → 先备份的坏文件被后一个覆盖掉。
        # 现在是 C 层原子自增的计数器：连续取值必须两两不同
        seq = [next(kb._QUARANTINE_SEQ) for _ in range(5)]
        self.assertEqual(len(set(seq)), len(seq))

    def test_absorb_does_not_wipe_an_existing_live_kb(self):
        # 反向确认：正常库（能解析）绝不被隔离，absorb 之后内容只增不减
        kb.absorb([_node(term="A词")], {"id": "j1"})
        self.assertEqual(glob.glob(store.knowledge_path() + ".corrupt-*"), [])
        kb.absorb([_node(term="B词")], {"id": "j2"})
        self.assertEqual(sorted(n["term"] for n in store.load_knowledge()["nodes"]),
                         ["A词", "B词"])

    def test_jd_without_id_is_not_recorded(self):
        # _add_jd 的**入参**守卫：没有 id 的岗位不该产生任何来源记录
        kb.absorb([_node()], {"job_title": "没有 id 的岗位"})
        card = store.load_knowledge()["nodes"][0]
        self.assertEqual(card["from_jds"], [])
        self.assertEqual(kb.stats(store.load_knowledge())["multi_jd"], 0)
        self.assertEqual(kb.all_jds(store.load_knowledge()), [])

    def test_phantom_jd_without_id_is_cleaned_on_next_absorb(self):
        # 只压入参守卫是**假绿**：历史/手改的卡片里已经存在的无 id dict 也必须在下一次 absorb
        # 时被清掉。旧实现只过滤「非 dict」，这条幻影会留在 from_jds 里 → 统计口径打架。
        store.ensure_profile()
        store.save_knowledge({"nodes": [{"id": "省略恢复", "term": "省略恢复", "state": "待学习",
                                         "from_jds": [{"job_title": "幻影"}]}]})
        kb.absorb([_node()], {"id": "j1", "job_title": "岗1"})
        d = store.load_knowledge()
        self.assertEqual([j.get("id") for j in d["nodes"][0]["from_jds"]], ["j1"])   # 幻影被清掉
        self.assertEqual(kb.stats(d)["multi_jd"], 0)
        self.assertEqual([j["id"] for j in kb.all_jds(d)], ["j1"])

    def test_multi_jd_agrees_with_all_jds_when_history_has_id_less_entries(self):
        # stats()["multi_jd"] 与 all_jds() 必须同一把尺子：幻影项（无 id）既不该算进 multi_jd、
        # 也不该出现在岗位下拉里，否则前端会显示「被 2 个以上岗位提到」却找不到那个岗位
        store.ensure_profile()
        store.save_knowledge({"nodes": [
            {"id": "省略恢复", "term": "省略恢复", "state": "待学习",
             "from_jds": [{"job_title": "幻影"}]},
            {"id": "抽样检验", "term": "抽样检验", "state": "待学习",
             "from_jds": [{"id": "j9", "job_title": "岗9"}, {"id": "j8", "job_title": "岗8"}]},
        ]})
        kb.absorb([_node(), _node(term="抽样检验")], {"id": "j1", "job_title": "岗1"})
        d = store.load_knowledge()
        self.assertEqual([j.get("id") for j in d["nodes"][0]["from_jds"]], ["j1"])
        self.assertEqual(kb.stats(d)["multi_jd"], _consistent_multi_jd(d))     # 口径一致
        self.assertEqual([j["id"] for j in kb.all_jds(d)], _consistent_jd_ids(d))
        self.assertEqual(kb.stats(d)["multi_jd"], 1)                           # 只有「抽样检验」被多岗提到
        self.assertEqual([j["id"] for j in kb.all_jds(d)], ["j1", "j8", "j9"])


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
        # 原来只否定 4 个术语里的一对，负例强度太弱（另外 5 对悄悄配上了也照样绿）
        # → 断言这 4 个术语两两都不成对
        self._seed(["Transformer 架构", "抽样检验", "幻觉", "标注规范"])
        self.assertEqual(kb.find_duplicates(store.load_knowledge()), [])

    def test_non_string_ids_do_not_raise(self):
        # knowledge.json 是用户可手改的：`"id": 5` 曾让 `5 in "abc"` 抛 TypeError，
        # 顺着 /api/knowledge 变成 500
        kb_data = {"nodes": [{"id": 5, "term": "手改的坏卡"}, {"id": None},
                             {"id": {}}, {"id": []}, {"id": "abc", "term": "正常卡"}]}
        self.assertIsInstance(kb.find_duplicates(kb_data), list)
        self.assertIsInstance(kb.find_duplicates({"nodes": [{"id": 1}, {"id": 2}]}), list)
        self.assertEqual(kb.find_duplicates({"nodes": [{"id": 5}, {"id": "abc"}]}), [])

    def test_non_string_ids_are_ignored_but_string_ones_still_compared(self):
        kb_data = {"nodes": [{"id": 5}, {"id": "省略恢复"}, {"id": "省略识别及恢复"}]}
        dups = kb.find_duplicates(kb_data)
        self.assertEqual(len(dups), 1)
        self.assertEqual({dups[0]["a"], dups[0]["b"]}, {"省略恢复", "省略识别及恢复"})

    def test_dup_reason_type_guard(self):
        # 双保险：即使有人绕过 find_duplicates 直接调 _dup_reason，也不能抛
        self.assertIsNone(kb._dup_reason(5, "abc"))
        self.assertIsNone(kb._dup_reason("abc", 5))
        self.assertIsNone(kb._dup_reason(None, None))

    def test_synonym_suffix_rule_pairs_same_stem_after_stripping_tail_word(self):
        # 「训练体系 / 训练机制」：去掉后缀词后词干同为「训练」，且互相不包含
        # （所以走的是「同义后缀」分支，不是「包含」分支）
        self._seed(["训练体系", "训练机制"])
        dups = kb.find_duplicates(store.load_knowledge())
        self.assertEqual(len(dups), 1)
        self.assertEqual(dups[0]["reason"], "同义后缀")

    def test_synonym_suffix_rule_needs_same_stem(self):
        # 同一条规则的负例：后缀词相同、去掉之后词干不同 → 不成对
        self._seed(["训练体系", "评估体系"])
        self.assertEqual(kb.find_duplicates(store.load_knowledge()), [])

    def test_prefix_and_suffix_similarity_rule(self):
        # 「首尾相近」在真实语料从未触发过 —— 这条规则此前完全没有测试。
        # 这两个术语前 4 字与后 2 字都相同、互相不包含，只能由「首尾相近」命中
        self._seed(["数据标注处理流程", "数据标注审核流程"])
        dups = kb.find_duplicates(store.load_knowledge())
        self.assertEqual(len(dups), 1)
        self.assertEqual(dups[0]["reason"], "首尾相近")

    def test_short_terms_are_excluded_from_prefix_similarity(self):
        # 门槛是「两个都 ≥6 字」：4 字词首尾同字极易互相配对，必须被挡住
        self._seed(["机器学习", "机器算法"])
        self.assertEqual(kb.find_duplicates(store.load_knowledge()), [])

    def test_prefix_similarity_needs_both_prefix_and_suffix(self):
        # 前缀 4 字相同但尾字不同，且互相不包含 → 不成对（门槛是 p>=4 且 s>=2）
        self._seed(["模型微调策略", "模型微调方案"])
        self.assertEqual(kb.find_duplicates(store.load_knowledge()), [])

    def test_containment_length_gap_over_four_is_not_a_pair(self):
        # 「包含」分支的长度差门槛 ≤4：差了 5 个字就不是同一概念的写法差异
        self._seed(["机器学习", "机器学习算法与模型"])
        self.assertEqual(kb.find_duplicates(store.load_knowledge()), [])

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
        ok, msg = kb.merge_nodes("A词", "B词")
        # 先钉住「合并真的发生了」：只断 state 的话，一个完全不合并的 no-op 实现也能通过
        self.assertTrue(ok, msg)
        self.assertEqual(len(store.load_knowledge()["nodes"]), 1)
        self.assertEqual(store.load_knowledge()["nodes"][0]["state"], "已掌握")

    def test_merge_rejects_same_id(self):
        kb.absorb([_node(term="A词")], {"id": "j1"})
        ok, msg = kb.merge_nodes("A词", "A词")
        self.assertFalse(ok)
        self.assertIn("自己", msg)
        self.assertEqual(len(store.load_knowledge()["nodes"]), 1)

    def test_merge_rejects_unknown_id(self):
        kb.absorb([_node(term="A词")], {"id": "j1"})
        # 先确认 keep 侧确实存在且能命中：否则「找不到 keep」与「找不到 drop」分不开，
        # 一个把 keep 也查丢的 no-op 实现照样能让本用例通过
        self.assertTrue(kb.set_state("A词", "学习中")[0])
        ok, msg = kb.merge_nodes("A词", "不存在的词")
        self.assertFalse(ok)
        # 拒绝必须是「什么都没发生」：合并照做但返回失败同样是错的
        cards = store.load_knowledge()["nodes"]
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["term"], "A词")
        self.assertEqual(cards[0]["aliases"], [])

    def test_merge_is_idempotent_when_target_gone(self):
        kb.absorb([_node(term="A词")], {"id": "j1"})
        kb.absorb([_node(term="B词")], {"id": "j2"})
        ok, msg = kb.merge_nodes("A词", "B词")
        self.assertTrue(ok, msg)          # 第一次必须真的合并掉（否则第二次的 False 是假象）
        # 钉住「第一次真的动过库」：只断第二次返回 False 的话，一个什么都不做的 no-op 也照样绿
        self.assertEqual([n["term"] for n in store.load_knowledge()["nodes"]], ["A词"])
        ok, _ = kb.merge_nodes("A词", "B词")
        self.assertFalse(ok)
        self.assertEqual(len(store.load_knowledge()["nodes"]), 1)

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


class ProgressUnionTest(IsolatedCase):
    """手动合并两张「用户卡」时，学习进度取并集而不是只保 keep 的（否则误选方向即静默丢进度）。

    注意：这不是放松「用户进度神圣」—— _merge_into（AI 节点并入你的卡）里 keep 的进度仍然
    绝不被覆盖，铁律 1 只针对「库 → 覆盖你」，这里是两张你自己的卡。
    """

    def _seed(self, term, jd_id, state=None, asked=None, outcome=None, first_seen=None):
        kb.absorb([_node(term=term)], {"id": jd_id})
        d = store.load_knowledge()
        card = [n for n in d["nodes"] if n["id"] == kb.normalize_id(term)][0]
        if state is not None:
            card["state"] = state
        if asked is not None:
            card["asked_count"] = asked
        if outcome is not None:
            card["last_outcome"] = outcome
        if first_seen is not None:
            card["first_seen"] = first_seen
        store.save_knowledge(d)

    def test_merge_unions_progress_so_drop_progress_is_not_lost(self):
        # 审查实测的原始故障：keep「待学习/未面试/0」+ drop「已掌握/已面试/5」→ 合并后全丢
        self._seed("A词", "j1")
        self._seed("B词", "j2", state="已掌握", asked=5, outcome="已面试")
        ok, msg = kb.merge_nodes("A词", "B词")
        self.assertTrue(ok, msg)
        card = store.load_knowledge()["nodes"][0]
        self.assertEqual(card["state"], "已掌握")
        self.assertEqual(card["last_outcome"], "已面试")
        self.assertEqual(card["asked_count"], 5)

    def test_merge_does_not_reset_keep_progress_when_drop_is_blank(self):
        # 只补不回退：drop 的进度是空值时，绝不能把 keep 已有的真实结果冲成默认值
        self._seed("A词", "j1", state="已掌握", asked=3, outcome="已面试")
        self._seed("B词", "j2")
        self.assertTrue(kb.merge_nodes("A词", "B词")[0])
        card = store.load_knowledge()["nodes"][0]
        self.assertEqual(card["state"], "已掌握")
        self.assertEqual(card["last_outcome"], "已面试")
        self.assertEqual(card["asked_count"], 3)

    def test_merge_normalizes_junk_last_outcome_instead_of_copying_it(self):
        # knowledge.json 可手改：drop 的 last_outcome 是 5 这类非法值时应回落默认，
        # 绝不能原样写到本该干净的 keep 卡上（旧实现原样拷贝，keep 就变成了 5）
        self._seed("A词", "j1")
        self._seed("B词", "j2", outcome=5)
        self.assertTrue(kb.merge_nodes("A词", "B词")[0])
        self.assertEqual(store.load_knowledge()["nodes"][0]["last_outcome"], kb.DEFAULT_OUTCOME)

    def test_merge_strips_last_outcome_whitespace(self):
        # 归一：合法值两侧的空白要吃掉，否则状态筛选与「已面试」的判等全部落空
        self._seed("A词", "j1")
        self._seed("B词", "j2", outcome="  已面试  ")
        self.assertTrue(kb.merge_nodes("A词", "B词")[0])
        self.assertEqual(store.load_knowledge()["nodes"][0]["last_outcome"], "已面试")

    def test_merge_takes_earliest_first_seen(self):
        # first_seen = 「这个概念最早什么时候出现」，并集口径下应取更早的一侧
        self._seed("A词", "j1", first_seen="2026-10-08")
        self._seed("B词", "j2", first_seen="2026-01-01")
        self.assertTrue(kb.merge_nodes("A词", "B词")[0])
        self.assertEqual(store.load_knowledge()["nodes"][0]["first_seen"], "2026-01-01")

    def test_merge_takes_earliest_first_seen_the_other_way_round(self):
        self._seed("A词", "j1", first_seen="2026-01-01")
        self._seed("B词", "j2", first_seen="2026-10-08")
        self.assertTrue(kb.merge_nodes("A词", "B词")[0])
        self.assertEqual(store.load_knowledge()["nodes"][0]["first_seen"], "2026-01-01")

    def test_merge_still_accumulates_aliases_and_from_jds(self):
        # 进度并集只补不回退：来源/别名等仍按原语义累加
        self._seed("A词", "j1", state="已掌握", asked=5, outcome="已面试")
        self._seed("B词", "j2")
        self.assertTrue(kb.merge_nodes("A词", "B词")[0])
        card = store.load_knowledge()["nodes"][0]
        self.assertEqual(card["term"], "A词")
        self.assertIn("B词", card["aliases"])
        self.assertEqual(sorted(j["id"] for j in card["from_jds"]), ["j1", "j2"])


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
        # 断言返回的术语集合，不断言条数：条数会被「别的卡恰好也命中」污染（默认 definition 就害过一次）
        self.assertEqual({n["term"] for n in kb.list_nodes(store.load_knowledge(), q="省略")},
                         {"省略恢复"})
        self.assertEqual(len(kb.list_nodes(store.load_knowledge(), q="统计抽样")), 1)
        self.assertEqual(len(kb.list_nodes(store.load_knowledge(), q="不存在的词")), 0)

    def test_search_tolerates_non_string_q(self):
        # q 直接来自 URL 参数：123 / bytes 不能让 AttributeError / TypeError 冒成 500
        kb_data = store.load_knowledge()
        for bad in (123, b"x", None, [], object()):
            self.assertIsInstance(kb.list_nodes(kb_data, q=bad), list)
        self.assertEqual({n["term"] for n in kb.list_nodes(kb_data, q=123)}, set())

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

    def test_one_bad_report_does_not_abort_the_whole_backfill(self):
        # 循环内没有 per-report try 时，一份报告写盘失败会让后面所有报告都吸不进来
        self._write_report("20260901_岗C_60分.html", {
            "report_id": "20260901_岗C_60分", "job_title": "岗C",
            "knowledge": [_node(term="坏报告里的词")]})
        self._write_report("20260902_岗D_70分.html", {
            "report_id": "20260902_岗D_70分", "job_title": "岗D",
            "knowledge": [_node(term="好报告里的词")]})
        real_absorb = kb.absorb

        def boom(nodes, jd=None, kb_=None):
            if jd and jd.get("id") == "20260901_岗C_60分":
                raise OSError("disk full")
            return real_absorb(nodes, jd, kb_)

        kb.absorb = boom
        try:
            files, added, merged = kb.absorb_reports()
        finally:
            kb.absorb = real_absorb
        self.assertEqual((files, added, merged), (1, 1, 0))
        self.assertEqual([n["term"] for n in store.load_knowledge()["nodes"]],
                         ["好报告里的词"])
        self.assertTrue(_log_events("kb.import_report_error"))

    def test_skipped_old_format_reports_are_logged_at_info_level(self):
        # 旧格式报告是可预期情况：不能打 warn（用户在日志页看到的「异常」里混着正常跳过）
        self._write_report("20260901_岗C_60分.html", {"report_id": "20260901_岗C_60分"})
        files, added, _ = kb.absorb_reports()
        self.assertEqual((files, added), (0, 0))
        res = _log_events("kb.import_skip")
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["level"], "info")
        self.assertEqual(res[0]["data"]["file"], "20260901_岗C_60分.html")

    def test_skipped_old_format_report_without_data_marker_is_info_too(self):
        store.ensure_profile()
        store.write_text(os.path.join(store.p_path("reports"), "旧报告.html"), "<html>no data")
        self.assertEqual(kb.absorb_reports()[0], 0)
        res = _log_events("kb.import_skip")
        self.assertTrue(res)
        self.assertTrue(all(e["level"] == "info" for e in res))
        self.assertFalse(_log_events("kb.import_report_error"))

    def test_report_is_not_counted_done_when_kb_quarantine_fails(self):
        # absorb 早退（坏库 + 改名备份失败）返回 (0, 0)：那不是「这份报告处理成功」，done 不能 +1
        store.ensure_profile()
        path = store.knowledge_path()
        store.write_text(path, "{ 坏文件")
        self._write_report("20261008_岗A_80分.html", {
            "report_id": "20261008_岗A_80分", "job_title": "岗A",
            "knowledge": [_node(term="省略恢复")]})
        with mock.patch.object(kb.os, "replace", side_effect=OSError("rename failed")):
            files, added, merged = kb.absorb_reports()
        self.assertEqual((files, added, merged), (0, 0, 0))
        self.assertEqual(store.read_text(path), "{ 坏文件")     # 坏文件原样保留
        self.assertEqual(glob.glob(path + ".corrupt-*"), [])


class AllJdsTest(IsolatedCase):
    def test_unhashable_and_non_string_ids_do_not_raise(self):
        # 用户手改的 knowledge.json：`{"id": [1]}` 曾让 all_jds() 抛
        # TypeError: unhashable type: 'list'，顺着 /api/knowledge 让连接裸断
        kb_data = {"nodes": [
            {"id": "卡1", "term": "卡1", "from_jds": [{"id": [1], "job_title": "列表 id"}]},
            {"id": "卡2", "term": "卡2", "from_jds": [{"id": {"a": 1}}, {"id": 5}]},
            {"id": {"a": 1}, "term": "怪节点", "from_jds": [{"id": "j1", "job_title": "岗1"}]},
            {"id": 5, "term": "数字节点",
             "from_jds": ["junk", {"id": None}, {"job_title": "无 id"}]},
        ]}
        self.assertEqual([j["id"] for j in kb.all_jds(kb_data)], ["j1"])

    def test_string_ids_are_still_collected_and_sorted(self):
        # 反向确认：跳过非字符串 id 不等于把岗位下拉清空
        kb_data = {"nodes": [{"id": "卡", "from_jds": [{"id": "j2", "job_title": "岗2"},
                                                       {"id": "j1"}]}]}
        self.assertEqual(kb.all_jds(kb_data),
                         [{"id": "j1", "title": "j1"}, {"id": "j2", "title": "岗2"}])


class _SandboxedHandler(app.Handler):
    """不接套接字的 Handler 沙箱：直接调 do_GET 并收下响应。

    为什么必须这么测：Fix ② 的故障形态就是「do_GET 抛异常 → 服务器关掉连接、没有任何 HTTP
    响应」，客户端只看到 RemoteDisconnected，连 500 都没有。这里异常会原样冒出来 ——
    冒出来就等于真实世界里的裸断。DATA_ROOT / LOG_FILE 由 IsolatedCase 指向临时目录。
    """

    def __init__(self, path):
        self.path = path
        self.command = "GET"
        self.rfile = io.BytesIO(b"")      # do_GET 不读 body，但 handler 得有这个属性
        self.headers = {}
        self.wfile = io.BytesIO()
        self.code = None

    def send_response(self, code, message=None):
        self.code = code

    def send_header(self, key, value):
        pass

    def end_headers(self):
        pass

    def body(self):
        raw = self.wfile.getvalue()
        return json.loads(raw.decode("utf-8")) if raw else None


def _get(path):
    h = _SandboxedHandler(path)
    h.do_GET()
    return h.code, h.body()


class KnowledgeRouteTest(IsolatedCase):
    """Fix ② 的路由层：手改坏的 knowledge.json 必须得到结构化响应，绝不能让连接裸断。

    （与 kb.py 的修正同属一次改动，仓库还没有 test_app.py，所以路由测试先落在这里。）
    """

    def test_route_survives_hand_edited_knowledge_json(self):
        store.ensure_profile()
        store.write_json(store.knowledge_path(), {"nodes": [
            {"id": "卡1", "term": "卡1", "from_jds": [{"id": [1]}]},
            {"id": {"a": 1}, "term": "怪卡", "from_jds": [{"id": 5}]},
            {"id": 5, "term": "数字卡", "from_jds": [{"id": "j1", "job_title": "岗1"}]},
        ]})
        code, body = _get("/api/knowledge")
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual([j["id"] for j in body["jds"]], ["j1"])

    def test_route_returns_500_instead_of_dropping_the_connection(self):
        # 第二道防线：即使读取路径以外的地方炸了，也必须回结构化 500 而不是裸断连接
        store.ensure_profile()
        store.save_knowledge({"nodes": []})
        with mock.patch.object(kb, "stats", side_effect=RuntimeError("boom")):
            code, body = _get("/api/knowledge")
        self.assertEqual(code, 500)
        self.assertFalse(body["ok"])
        self.assertIn("知识库读取失败", body["error"])
        self.assertTrue(_log_events("kb.api_error"))


class AnalyzeAbsorbGuardTest(IsolatedCase):
    """Fix ⑤：分析时零知识点就绝不碰库文件。

    否则「库文件损坏 + 本次没抽出任何知识点」的组合会先隔离坏文件、再写回空库 ——
    等于吸收了个寂寞，还把用户已有的积累清空。
    """

    def _run_analyze(self, know_nodes):
        store.ensure_profile()
        store.write_text(os.path.join(store.p_path("resumes"), "简历.md"), "候选人简历")
        payload = {"job_title": "岗A", "company": "甲公司", "score": 80}
        with mock.patch.object(app.llm, "call_openai_compatible", return_value="{}"), \
             mock.patch.object(app.llm, "parse_json_reply", return_value=payload), \
             mock.patch.object(app.knowledge, "build_for_jd", return_value=know_nodes), \
             mock.patch.object(app, "render_report", return_value="20261008_岗A_80分.html"):
            app.do_analyze("一份 JD 文本", None, "简历.md")

    def test_zero_knowledge_does_not_touch_a_corrupt_kb_file(self):
        store.ensure_profile()
        path = store.knowledge_path()
        store.write_text(path, "{ 坏文件")
        self._run_analyze([])
        self.assertEqual(store.read_text(path), "{ 坏文件")     # 坏文件原样留着
        self.assertEqual(glob.glob(path + ".corrupt-*"), [])    # 也没被隔离走

    def test_knowledge_is_still_absorbed_when_present(self):
        # 反向确认：守卫不能把正常沉淀一起挡掉
        self._run_analyze([_node(term="省略恢复")])
        self.assertEqual([n["term"] for n in store.load_knowledge()["nodes"]], ["省略恢复"])
