# -*- coding: utf-8 -*-
import ast
import datetime
import glob
import io
import json
import log
import os
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

import app
import jdid
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
    """卡片里「算数」的来源岗位 id：只认 id 是**非空字符串**的 dict（与 kb._add_jd / kb.all_jds 同一把尺子）"""
    return [x["id"] for x in kb._as_list(card.get("from_jds"))
            if isinstance(x, dict) and isinstance(x.get("id"), str) and x["id"]]


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

    def test_non_string_jd_ids_are_cleaned_and_stats_agrees_with_all_jds(self):
        # 手改成**真值但非字符串**的 id（`{"id": 5}` / `{"id": [1]}`）：旧实现的历史项过滤只挡「非 dict」，
        # 这两条会被 stats()["multi_jd"] 按 raw 长度数进去，all_jds() 却看不见它们 ——
        # 前端于是显示「被 2 个以上岗位提到」却在下拉里找不到对应岗位。两处必须同一把尺子。
        store.ensure_profile()
        store.save_knowledge({"nodes": [
            {"id": "省略恢复", "term": "省略恢复", "state": "待学习",
             "from_jds": [{"id": 5, "job_title": "数字 id"}, {"id": [1], "job_title": "列表 id"},
                          {"id": "j9", "job_title": "岗9"}]},
            {"id": "抽样检验", "term": "抽样检验", "state": "待学习",
             "from_jds": [{"id": 5, "job_title": "数字 id"}, {"id": [1], "job_title": "列表 id"}]},
        ]})
        kb.absorb([_node(), _node(term="抽样检验")], {"id": "j1", "job_title": "岗1"})
        d = store.load_knowledge()
        self.assertEqual([j.get("id") for j in d["nodes"][0]["from_jds"]], ["j9", "j1"])  # 非字符串 id 清掉
        self.assertEqual([j.get("id") for j in d["nodes"][1]["from_jds"]], ["j1"])        # 幻影清掉，只剩真岗位
        self.assertEqual(kb.stats(d)["multi_jd"], _consistent_multi_jd(d))                # 口径一致
        self.assertEqual(kb.stats(d)["multi_jd"], 1)                       # 只有第 1 张卡真被多岗提到
        self.assertEqual([j["id"] for j in kb.all_jds(d)], _consistent_jd_ids(d))
        self.assertEqual([j["id"] for j in kb.all_jds(d)], ["j1", "j9"])

    def test_reabsorbing_an_already_recorded_jd_still_cleans_non_string_ids(self):
        # 幂等分支：同一个岗位再 absorb 一次时，旧实现清洗完发现 id 已存在就直接 return，
        # 手改进去的非字符串 id 会留在原地 —— 库依旧是「数得着、选不到」的状态。
        store.ensure_profile()
        store.save_knowledge({"nodes": [
            {"id": "省略恢复", "term": "省略恢复", "state": "待学习",
             "from_jds": [{"id": 5, "job_title": "数字 id"}, {"id": "j1", "job_title": "岗1"}]},
        ]})
        kb.absorb([_node()], {"id": "j1", "job_title": "岗1"})
        d = store.load_knowledge()
        self.assertEqual([j.get("id") for j in d["nodes"][0]["from_jds"]], ["j1"])
        self.assertEqual(kb.stats(d)["multi_jd"], 0)                       # 唯一一张卡只有 1 个真岗位
        self.assertEqual(kb.stats(d)["multi_jd"], _consistent_multi_jd(d))
        self.assertEqual([j["id"] for j in kb.all_jds(d)], ["j1"])

    def test_non_string_jd_id_argument_is_not_recorded(self):
        # 入参守卫也要字符串：merge_nodes() 会把 drop 卡里手改过的 from_jds **原样**喂进 _add_jd，
        # 非字符串 id 一旦落盘就是 stats() 数得着、all_jds() 看不见的幻影。
        kb.absorb([_node()], {"id": 5, "job_title": "数字 id"})
        kb.absorb([_node()], {"id": [1], "job_title": "列表 id"})
        card = store.load_knowledge()["nodes"][0]
        self.assertEqual(card["from_jds"], [])
        self.assertEqual(kb.stats(store.load_knowledge())["multi_jd"], 0)
        self.assertEqual(kb.all_jds(store.load_knowledge()), [])

    def test_merge_does_not_carry_non_string_jd_ids_from_the_dropped_card(self):
        # 同一条不变式走手动合并分支：merge_nodes 会把 drop 卡的 from_jds **原样**喂进 _add_jd，
        # 手改成 {"id": 5} 的幻影不能借合并钻进 keep 卡
        store.ensure_profile()
        store.save_knowledge({"nodes": [
            {"id": "省略恢复", "term": "省略恢复", "state": "待学习",
             "from_jds": [{"id": "j1", "job_title": "岗1"}]},
            {"id": "省略识别及恢复", "term": "省略识别及恢复", "state": "待学习",
             "from_jds": [{"id": 5, "job_title": "数字 id"}, {"id": "j2", "job_title": "岗2"}]},
        ]})
        ok, _ = kb.merge_nodes("省略恢复", "省略识别及恢复")
        self.assertTrue(ok)
        d = store.load_knowledge()
        self.assertEqual([j.get("id") for j in d["nodes"][0]["from_jds"]], ["j1", "j2"])
        self.assertEqual(kb.stats(d)["multi_jd"], _consistent_multi_jd(d))
        self.assertEqual([j["id"] for j in kb.all_jds(d)], ["j1", "j2"])

    def test_ensure_kb_filters_phantom_from_jds_in_place(self):
        # I4：手改出的非字符串 id 是「stats 数得着、all_jds 选不到」的幻影。
        # 清洗放在 ensure_kb 这唯一咽喉点上 → stats / list_nodes / all_jds 三处口径同时归位。
        kb_data = {"nodes": [{
            "id": "省略恢复", "term": "省略恢复", "state": "待学习",
            "from_jds": [{"id": "j1", "job_title": "岗1"}, {"id": 5},
                         {"id": None}, {"id": ""}, {"id": [1]}, "junk"],
        }]}
        out = kb.ensure_kb(kb_data)
        # 原地过滤：不只是返回值干净，传进去的那个 dict 也已经被改干净
        self.assertEqual([j["id"] for j in kb_data["nodes"][0]["from_jds"]], ["j1"])
        self.assertEqual([j["id"] for j in out["nodes"][0]["from_jds"]], ["j1"])
        # 不是 list 时按空处理（绝不抛异常）
        self.assertEqual(kb.ensure_kb({"nodes": [{"id": "卡", "from_jds": "j1"}]})["nodes"][0]["from_jds"], [])
        self.assertEqual(kb.ensure_kb({"nodes": [{"id": "卡", "from_jds": None}]})["nodes"][0]["from_jds"], [])
        # 没有 from_jds 字段的节点会被补上空列表（不是缺字段）—— 调用方读 n["from_jds"] 也安全
        self.assertEqual(kb.ensure_kb({"nodes": [{"id": "卡"}]})["nodes"][0]["from_jds"], [])

    def test_ensure_kb_filter_makes_stats_list_and_sort_agree(self):
        # I4 的三处口径一致性：修复前 stats 数 3、排序按 3 排、徽章显示 3，而下拉里只有 1 个岗位
        store.ensure_profile()
        store.save_knowledge({"nodes": [
            {"id": "幻影卡", "term": "幻影卡", "state": "待学习",
             "from_jds": [{"id": 5}, {"id": "j1", "job_title": "岗1"}, {"id": [1]}, "junk"]},
            {"id": "单岗卡", "term": "单岗卡", "state": "待学习",
             "from_jds": [{"id": "j1", "job_title": "岗1"}]},
            {"id": "真多岗卡", "term": "真多岗卡", "state": "待学习",
             "from_jds": [{"id": "j1", "job_title": "岗1"}, {"id": "j2", "job_title": "岗2"}]},
            {"id": "怪卡", "term": "怪卡", "state": "待学习", "from_jds": {"id": "j9"}},
        ]})
        # 先看磁盘上的原始内容（read_json 走的是 store.load_knowledge 同一条解析路径，
        # 但**不经过** ensure_kb）—— 证明清洗真的发生了，而不是一开始就没写进去。
        # 注意 ["junk"] 是字符串不是 dict，本来就不算一条 from_jds 项。
        raw_nodes = store.read_json(store.knowledge_path())["nodes"]
        self.assertEqual(len(raw_nodes[0]["from_jds"]), 4)
        self.assertEqual(len(store.load_knowledge()["nodes"][3]["from_jds"]), 1)   # 怪卡：from_jds 是 dict
        d = store.load_knowledge()
        self.assertEqual(kb.stats(d)["multi_jd"], 1)                      # 只有「真多岗卡」算多岗位
        # **副作用实证**：上面这一行 kb.stats(d) 内部只是调了 ensure_kb，就地把 d 自己的节点改写了
        self.assertEqual(len(d["nodes"][0]["from_jds"]), 1)
        self.assertEqual(len(d["nodes"][3]["from_jds"]), 0)
        # 排序键是 (-岗位数, -来源数, 术语)：清掉幻影后「幻影卡」与「单岗卡」同为 1 个岗位，
        # 靠术语定序（Python sort 稳定，但不能依赖输入顺序 —— 那正是本条要钉住的口径）
        self.assertEqual([n["id"] for n in kb.list_nodes(d, sort="mentions")],
                         ["真多岗卡", "单岗卡", "幻影卡", "怪卡"])
        # 反证：如果 ensure_kb 没清幻影，「幻影卡」会以 3 个岗位排到最前面
        self.assertEqual([j["id"] for j in kb.all_jds(d)], ["j1", "j2"])
        # 序列化给前端的节点（/api/knowledge 走的就是 list_nodes）里也不含幻影
        for n in kb.list_nodes(d):
            self.assertEqual([j.get("id") for j in n["from_jds"]],
                             _real_jd_ids(n))

    def test_absorb_with_zero_absorbable_terms_does_not_quarantine_or_write(self):
        # I1：坏库 + 「零可吸收节点」的调用。修复前 absorb 会先 _quarantine_corrupt_kb 把坏文件
        # 改名走、再 save_knowledge 写回空库 —— 用户卡片从可见库消失，只剩一个 UI 从不提示的
        # .corrupt-* 文件。现在必须在隔离之前就 return，文件与目录都原样不动。
        store.ensure_profile()
        path = store.knowledge_path()
        store.write_text(path, "{ 坏文件")
        self.assertEqual(kb.absorb([_node(term=""), _node(term="（）·、。"), _node(term="   ")]), (0, 0))
        self.assertEqual(store.read_text(path), "{ 坏文件")
        self.assertEqual(glob.glob(path + ".corrupt-*"), [])

    def test_absorb_with_zero_absorbable_terms_does_not_wipe_a_valid_live_kb(self):
        # 同一个早退在「库是好的」时同样是纯 no-op：文件连 mtime 都不该动。
        # 注意「!!! 纯标点」**不是**零可吸收 —— normalize_id 只去标点，中文会留下（结果是「纯标点」）。
        # 真正的零可吸收是「去标点后什么都不剩」与「根本不是 dict/字符串术语」这两类。
        store.ensure_profile()
        path = store.knowledge_path()
        kb.absorb([_node(term="省略恢复")], {"id": "j1"})
        before = store.read_text(path)
        mtime = os.path.getmtime(path)
        self.assertEqual(kb.absorb([]), (0, 0))
        self.assertEqual(kb.absorb([_node(term="（）·、。"), _node(term="   "),
                                    "junk", 5, None, {}]), (0, 0))
        self.assertEqual(store.read_text(path), before)
        self.assertEqual(os.path.getmtime(path), mtime)
        self.assertEqual(len(store.load_knowledge()["nodes"]), 1)

    def test_non_string_term_cannot_fabricate_a_card(self):
        # str(5) → "5"、str(None) → "none" 都非空，只靠 normalize_id 的话 {"term": 5} 会凭空造出
        # 一张 id 为 "5" 的垃圾卡；而 find_duplicates / 前端都把非字符串 id 当幻影跳过 ——
        # 那就成了「有卡片却查不到也合并不了」的死节点。术语必须是字符串才算可吸收。
        store.ensure_profile()
        self.assertEqual(kb.absorb([{"term": 5}, {"term": None}, {"term": [1]},
                                    {"term": 3.5}, {"term": True}]), (0, 0))
        self.assertEqual(store.load_knowledge()["nodes"], [])
        # 反向确认：字符串术语照常吸收（守卫不能把正常值一起挡掉）
        self.assertEqual(kb.absorb([{"term": "省略恢复"}]), (1, 0))
        self.assertEqual([n["id"] for n in store.load_knowledge()["nodes"]], ["省略恢复"])

    def test_absorb_still_records_a_jd_when_only_some_nodes_are_absorbable(self):
        # 反向确认：早退守卫不能把「有货 + 有垃圾」的正常调用一起挡掉
        added, merged = kb.absorb([_node(term=""), _node(term="省略恢复")], {"id": "j1"})
        self.assertEqual((added, merged), (1, 0))
        card = store.load_knowledge()["nodes"][0]
        self.assertEqual(card["term"], "省略恢复")
        self.assertEqual([j["id"] for j in card["from_jds"]], ["j1"])


class ConcurrentWriteTest(IsolatedCase):
    """C1：knowledge.json 的读-改-写必须串行。

    真实故障形态（实测过）：分析要 20–60 秒，且只禁用 #goBtn —— 用户完全可以在知识库 tab 里
    点掌握度。于是「分析结尾的 absorb」与「set_state」两个线程会同时 load→改→save，
    后写完的把先写完的整个覆盖掉（审查者 120 轮丢 31 次，我 120 轮丢 99 次）。

    这条测试同时钉住三件事：① 每一轮 state 改动都没丢 ② 每次读回的文件都能解析 ③ 节点数符合预期。
    """

    THREADS = 4
    ROUNDS = 12

    def test_concurrent_absorb_and_set_state_never_lose_updates_or_corrupt_the_file(self):
        store.ensure_profile()
        path = store.knowledge_path()
        barrier = threading.Barrier(self.THREADS)
        errors = []

        def worker(t):
            try:
                for r in range(self.ROUNDS):
                    barrier.wait(timeout=30)
                    added, _ = kb.absorb([_node(term="概念-%d-%d" % (t, r))],
                                         {"id": "j%d" % t, "job_title": "岗%d" % t})
                    if added != 1:
                        errors.append("线程 %d 第 %d 轮 absorb 报告 added=%d" % (t, r, added))
                    ok, msg = kb.set_state("概念-%d-%d" % (t, r), "已掌握")
                    if not ok:
                        errors.append("线程 %d 第 %d 轮 set_state 失败：%s" % (t, r, msg))
            except Exception as e:                 # 异常必须带回主线程，否则会被 unittest 吞掉
                errors.append("线程 %d 抛异常：%r" % (t, e))

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(self.THREADS)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=120)

        self.assertEqual(errors, [])
        # ② 文件必须还能解析（去掉任一保护都会出现半截文件 → read_json 归 None）
        raw = store.read_text(path)
        try:
            parsed = json.loads(raw)
        except ValueError as e:
            self.fail("并发写之后 knowledge.json 无法解析：%s" % e)
        # ③ 节点数符合预期
        self.assertEqual(len(parsed["nodes"]), self.THREADS * self.ROUNDS)
        # ① 每一轮的 state 改动都没有丢
        states = {n["id"]: n.get("state") for n in parsed["nodes"]}
        missing = [k for k, v in states.items() if v != "已掌握"]
        self.assertEqual(missing, [])
        self.assertEqual(len(states), self.THREADS * self.ROUNDS)
        self.assertEqual(len(store.load_knowledge()["nodes"]), self.THREADS * self.ROUNDS)


class AtomicWriteTest(IsolatedCase):
    """C1 的第二道防线：store.write_json 原子写。

    即使将来还有别的交错路径（或多进程），也不能再产生「无法解析」的文件；
    进程被 kill 时也不能留下截断成半截的 knowledge.json。
    """

    def test_write_json_replaces_target_atomically(self):
        store.ensure_profile()
        path = store.knowledge_path()
        store.write_json(path, {"version": 1, "nodes": [{"id": "a"}]})
        store.write_json(path, {"version": 1, "nodes": [{"id": "b"}]})
        self.assertEqual(store.read_json(path), {"version": 1, "nodes": [{"id": "b"}]})
        # 保留原有格式：ensure_ascii=False + indent=2（中文不转义、两空格缩进）
        raw = store.read_text(path)
        self.assertIn('"id": "b"', raw)
        self.assertIn("\n  ", raw)
        self.assertNotIn("\\u", raw)

    def test_dump_failure_leaves_the_old_file_intact_and_no_temp_junk(self):
        store.ensure_profile()
        path = store.knowledge_path()
        good = {"version": 1, "nodes": [{"id": "省略恢复", "term": "省略恢复"}]}
        store.write_json(path, good)
        before = store.read_text(path)
        d = os.path.dirname(path)
        real_dump = json.dump
        calls = {"n": 0}

        def flaky_dump(obj, fp, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                real_dump(obj, fp, **kw)          # 先往临时文件里写一份完整内容……
                raise RuntimeError("模拟写到一半磁盘/进程挂了")
            return real_dump(obj, fp, **kw)

        with mock.patch.object(json, "dump", flaky_dump):
            with self.assertRaises(RuntimeError):
                store.write_json(path, {"version": 1, "nodes": [{"id": "会被丢掉的新内容"}]})

        self.assertEqual(store.read_text(path), before)    # 原文件分毫未动
        self.assertEqual(store.read_json(path), good)
        # 临时文件必须被清理干净，目录里不能留垃圾
        leftovers = [f for f in os.listdir(d) if f.startswith(".tmp-")]
        self.assertEqual(leftovers, [])

    def test_serialization_failure_also_leaves_no_temp_junk(self):
        store.ensure_profile()
        path = store.knowledge_path()
        store.write_json(path, {"version": 1, "nodes": []})
        before = store.read_text(path)
        with self.assertRaises(TypeError):
            store.write_json(path, {"bad": {1, 2, 3}})     # set 不可 JSON 序列化
        self.assertEqual(store.read_text(path), before)
        self.assertEqual([f for f in os.listdir(os.path.dirname(path)) if f.startswith(".tmp-")], [])


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
        # 守的是「首尾相近」的两个 ≥6 字门槛：这批 2 字词共享首尾字，没有门槛会被刷成一片假配对。
        # （与「插入式」的 len(short)>=3 门槛无关 —— 那条由 test_two_char_term_inside_a_long_term_is_not_a_pair 钉住。）
        self._seed(["抽样", "抽取", "抽检"])
        self.assertEqual(kb.find_duplicates(store.load_knowledge()), [])

    def test_two_char_terms_never_become_subsequence_pairs(self):
        # 这一批负例守住的是「同一个字在两个 2 字词里都出现」这类噪音不成对。
        # **注意它守不住 len(short)>=3 那道门槛**：两个长度都是 2 的不同术语永远不可能互为
        # 有序子序列（长度 2 是长度 2 的子序列 ⟺ 两者相等，而相等在 _dup_reason 开头就被排除了），
        # 所以删掉门槛这批照样绿。真正钉门槛的是下面 test_two_char_term_inside_a_long_term_is_not_a_pair。
        self._seed(["抽样", "抽检", "抽取", "标注", "标准", "指标", "幻觉", "错觉"])
        self.assertEqual(kb.find_duplicates(store.load_knowledge()), [])

    def test_two_char_term_inside_a_long_term_is_not_a_pair(self):
        # 「插入式」分支的 len(short) >= 3 门槛。这里是**真能红**的负例：
        # 「幻觉」2 字，每个字都按序出现在「幻象觉知」里（跳过了中间的「象」），长度差 2 ≤ 4，
        # 互相又不是连续子串、去后缀词后词干也不同 —— 删掉门槛就会被判成「插入式」。
        # 上面那批 2 字两两配对的负例与这道门槛无关，所以必须单列一条（否则门槛被删也是全绿）。
        self._seed(["幻觉", "幻象觉知"])
        self.assertEqual(kb.find_duplicates(store.load_knowledge()), [])

    def test_subsequence_variant_is_detected(self):
        # 「省略恢复」不是「省略识别及恢复」的连续子串（中间插了「识别及」），
        # 但每个字按序都能对上 —— 这是本领域真实存在的重复来源
        self._seed(["省略恢复", "省略识别及恢复"])
        dups = kb.find_duplicates(store.load_knowledge())
        self.assertEqual(len(dups), 1)
        self.assertEqual(dups[0]["reason"], "插入式")

    def test_limit_is_respected(self):
        # 必须是等号：40 个「概念N号」里前 20 对能配满 20 条，assertLessEqual 连恒返回 [] 的
        # 实现都能绿（假绿）。
        self._seed(["概念%d号" % i for i in range(40)])
        self.assertEqual(len(kb.find_duplicates(store.load_knowledge())), 20)


# ---------------------------------------------------------------- 批 6.3：find_duplicates 的剪枝
# 为什么要有这一组：`find_duplicates` 是纯 O(N²) 两两比较，**库很干净也不提前返回**
# （体检 §6 实测：145 卡 0.03s ／ 500→0.38s ／ 1000→1.52s ／ **2000→5.71s**）。
# 剪枝是纯性能改动，所以本组的**主判据是"判据不许变"**（逐条相同），性能只作次判据。
#
# 夹具字符池：本领域真实术语里的**词素**（两字词），一个术语 = 3 个词素（6 字）。
# 真实 145 卡里 4~6 字占 ~2/3、用到 ~296 个不同的字；这份池子 110 个词素 = 220 个字，
# 量级接近。合成夹具其实比真实库**散**（真实术语共享更多词素 ⇒ 索引键碰撞更多），
# 所以它并不占便宜：实测候选对占全量对的 ~8%（真实 145 卡上 4.7%）。
_FX_MORPHEMES = (
    "省略", "识别", "恢复", "根因", "分析", "抽样", "检验", "幻觉", "标注", "规范",
    "流程", "策略", "体系", "机制", "方法", "标准", "系统", "数据", "模型", "推理",
    "训练", "评估", "优化", "微调", "对齐", "蒸馏", "提示", "向量", "检索", "排序",
    "嵌入", "注意", "力度", "损失", "正则", "编码", "解码", "缓存", "并发", "事务",
    "索引", "分区", "副本", "容灾", "降级", "熔断", "限流", "幂等", "一致", "依赖",
    "注入", "反射", "代理", "装饰", "适配", "观察", "工厂", "单例", "迭代", "生成",
    "闭包", "作用", "原型", "继承", "多态", "封装", "抽象", "接口", "实现", "对象",
    "实例", "属性", "字段", "约束", "校验", "断言", "覆盖", "分支", "循环", "异常",
    "捕获", "堆栈", "断点", "调试", "监控", "告警", "指标", "采样", "聚合", "分组",
    "窗口", "滑动", "延迟", "吞吐", "压力", "容量", "扩缩", "预热", "灰度", "回滚",
    "发布", "版本", "升级", "兼容", "弃用", "迁移", "备份", "演练", "故障", "复盘",
)


def _fx_lcg(state):
    """自带的确定性伪随机（**不用 `random` 模块**：不依赖它内部算法跨版本稳定）。"""
    return (state * 1103515245 + 12345) % (2 ** 31)


def _fx_terms(n):
    """前 n 个夹具术语。**前缀一致**：n=50 的序列就是 n=2000 序列的前 50 个。"""
    out, seen, state = [], set(), 20261015
    m = len(_FX_MORPHEMES)
    while len(out) < n:
        state = _fx_lcg(state)
        a = _FX_MORPHEMES[state % m]
        state = _fx_lcg(state)
        b = _FX_MORPHEMES[state % m]
        state = _fx_lcg(state)
        c = _FX_MORPHEMES[state % m]
        t = a + b + c
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


def _fx_variant_terms(n):
    """在 `_fx_terms(n)` 上**按固定下标注入**已知重复（四种判据都覆盖到）。

    注入规则（`i` 是 37 的倍数、`base` 是前一张卡）：
      `base + 方法` ⇒ 包含/同义后缀；`base[:2] + 的 + base[2:]` ⇒ 插入式；
      `base[:-2] + 机制` ⇒ 同义后缀；其余原样（等于 base ⇒ 不成对，做对照）。
    """
    ts = _fx_terms(n)
    for i in range(37, n, 37):
        base = ts[i - 1]
        k = i % 4
        if k == 0:
            ts[i] = base + "方法"
        elif k == 1:
            ts[i] = base[:2] + "的" + base[2:]
        elif k == 2:
            ts[i] = base[:-2] + "机制"
        else:
            ts[i] = base
    return ts


def _fx_kb(terms):
    return {"version": 1, "nodes": [{"id": t, "term": t} for t in terms]}


def _fx_triples(dups):
    return [(d["a"], d["b"], d["reason"]) for d in dups]


def _reference_find_duplicates(kb_data, limit=20):
    """**改前**的实现：逐字照抄剪枝之前 `kb.find_duplicates` 的那圈两两比较。

    等价性用例拿它当对照物（它是"判据"的独立副本，不是被测量对象）。保留它的另一个用处是
    钉住**顺序**与 `limit` 的既有语义（够了就 return —— 包含 `limit=0` 那个怪语义）。
    """
    ids = [n.get("id") for n in kb.ensure_kb(kb_data)["nodes"]
           if isinstance(n.get("id"), str) and n.get("id")]
    out = []
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            reason = kb._dup_reason(ids[i], ids[j])
            if reason:
                out.append({"a": ids[i], "b": ids[j], "reason": reason})
                if len(out) >= limit:
                    return out
    return out


# ---- 期望值：全部由**改前**实现跑出来并存死（"先跑出改前的结果存成期望值"）----
# 手工夹具：四种判据 + 单字 id + 非字符串/空 id + 重复 id + term 与 id 不一致的卡。
_FX_TRICKY_KBS = {"version": 1, "nodes": [
    {"id": "机器学习", "term": "机器学习"},
    # ⚠️ 下面这张卡**故意让 term 与 id 不一致**（用户手改过的库）：判据只吃 id，
    # 所以索引键也只能按 id 建 —— 谁把键建成 term 或整张卡的序列化，这一对就会消失。
    {"id": "机器学习算法", "term": "手改过的别名（term 与 id 不一致）"},
    {"id": "省略恢复", "term": "省略恢复"},
    {"id": "省略识别及恢复", "term": "省略识别及恢复"},
    {"id": "训练体系", "term": "训练体系"},
    {"id": "训练机制", "term": "训练机制"},
    # 「猫体系 / 猫机制」：词干只有 1 个字，**没有任何共享的字对** ⇒ 只可能靠"同义后缀"键命中
    {"id": "猫体系", "term": "猫体系"},
    {"id": "猫机制", "term": "猫机制"},
    {"id": "数据标注处理流程", "term": "数据标注处理流程"},
    {"id": "数据标注审核流程", "term": "数据标注审核流程"},
    # 单字 id：与它成对必须靠"短卡兜底"（差 4 字以内）；差 5 字的「猫科动物类」不许成对
    {"id": "猫", "term": "猫"},
    {"id": "猫科动物", "term": "猫科动物"},
    {"id": "猫科动物类", "term": "猫科动物类"},
    {"id": 5, "term": "非字符串 id"},
    {"id": None, "term": "None id"},
    {"id": {}, "term": "字典 id"},
    {"id": [], "term": "列表 id"},
    {"id": "", "term": "空 id"},
    {"id": "重复卡", "term": "重复卡"},
    {"id": "重复卡", "term": "同一 id 的第二张（a==b 不成对）"},
]}

# 改前实现在 _FX_TRICKY_KBS 上的**全量**结果（limit 取大）：10 条，顺序也是它的顺序。
_FX_TRICKY_DUPS = [
    ("机器学习", "机器学习算法", "包含"),
    ("省略恢复", "省略识别及恢复", "插入式"),
    ("训练体系", "训练机制", "同义后缀"),
    ("猫体系", "猫机制", "同义后缀"),
    ("猫体系", "猫", "包含"),
    ("猫机制", "猫", "包含"),
    ("数据标注处理流程", "数据标注审核流程", "首尾相近"),
    ("猫", "猫科动物", "包含"),
    ("猫", "猫科动物类", "包含"),
    ("猫科动物", "猫科动物类", "包含"),
]

# 40 张「概念N号」：配对远多于默认 limit=20 ⇒ 钉住"截断的是**扫描顺序的前 20 条**"。
_FX_LIMIT40_KBS = _fx_kb(["概念%d号" % i for i in range(40)])
_FX_LIMIT40_DUPS = [
    ("概念0号", "概念10号", "插入式"),
    ("概念0号", "概念20号", "插入式"),
    ("概念0号", "概念30号", "插入式"),
    ("概念1号", "概念10号", "插入式"),
    ("概念1号", "概念11号", "插入式"),
    ("概念1号", "概念12号", "插入式"),
    ("概念1号", "概念13号", "插入式"),
    ("概念1号", "概念14号", "插入式"),
    ("概念1号", "概念15号", "插入式"),
    ("概念1号", "概念16号", "插入式"),
    ("概念1号", "概念17号", "插入式"),
    ("概念1号", "概念18号", "插入式"),
    ("概念1号", "概念19号", "插入式"),
    ("概念1号", "概念21号", "插入式"),
    ("概念1号", "概念31号", "插入式"),
    ("概念2号", "概念12号", "插入式"),
    ("概念2号", "概念20号", "插入式"),
    ("概念2号", "概念21号", "插入式"),
    ("概念2号", "概念22号", "插入式"),
    ("概念2号", "概念23号", "插入式"),
]

_FX_DIRTY200_DUPS = [
    ("多态演练优化", "多态的演练优化", "插入式"),
    ("对象优化演练", "对象优化演练方法", "包含"),
    ("故障向量副本", "故障的向量副本", "插入式"),
]
_FX_DIRTY1000_DUPS = [
    ("多态演练优化", "多态的演练优化", "插入式"),
    ("对象优化演练", "对象优化演练方法", "包含"),
    ("故障向量副本", "故障的向量副本", "插入式"),
    ("属性系统堆栈", "属性系统堆栈方法", "包含"),
    ("适配接口正则", "适配的接口正则", "插入式"),
    ("数据指标排序", "数据指标排序方法", "包含"),
    ("注入回滚备份", "注入的回滚备份", "插入式"),
    ("缓存对齐微调", "缓存对齐微调方法", "包含"),
    ("训练反射限流", "训练的反射限流", "插入式"),
    ("分区限流单例", "分区限流单例方法", "包含"),
    ("向量嵌入滑动", "向量的嵌入滑动", "插入式"),
    ("闭包迁移迭代", "闭包迁移迭代方法", "包含"),
    ("告警闭包分区", "告警的闭包分区", "插入式"),
]
# 2000 卡（干净夹具）上的两条意外同义后缀配对 —— 也是"旧实现不可能提前返回"的反证（2 < limit）。
_FX_CLEAN2000_DUPS = [
    ("作用一致规范", "作用一致标准", "同义后缀"),
    ("调试升级策略", "调试升级机制", "同义后缀"),
]


class FindDuplicatesPruningTest(IsolatedCase):
    """批 6.3：`find_duplicates` 的 O(N²) 剪枝（**判据与顺序一字不改，只少比较**）。

    剪枝思路（细节与证明见 `kb._block_keys` 的 docstring）：先用**一次线性扫描**给每张卡建
    索引键，只对"键与别人撞上"的卡两两比较。判据完全没动 —— 候选集是原判据的**超集**，
    所以结果逐条相同；`limit` 的截断顺序也不变（仍按 (i, j) 升序）。

    **本组用例的"红"法（诚实说明）**：等价性用例在改前**是绿的** —— 因为期望值就是从改前
    实现冻出来的，"行为不变"本来就该绿。改前真正红的是两条 2000 卡用例：
    旧实现要比较 2000×1999/2 = 1,999,000 次、实测 5.8~6.1s，两条断言都过不去。
    """

    def test_pruned_result_matches_the_reference_on_small_fixtures(self):
        """N=50/200：剪枝结果与**改前实现**逐条相同（含顺序）。"""
        for n in (50, 200):
            for terms in (_fx_terms(n), _fx_variant_terms(n)):
                data = _fx_kb(terms)
                self.assertEqual(kb.find_duplicates(data, limit=10 ** 9),
                                 _reference_find_duplicates(data, limit=10 ** 9),
                                 "剪枝改变了判据（N=%d）" % n)

    def test_pruned_result_matches_the_frozen_expected_on_medium_fixtures(self):
        """N=200/1000/2000：与**改前跑出来存死的期望值**逐条相同（顺序也相同）。

        N=1000/2000 不再跑对照实现（全量两两要 1.5s / 6s，白等），改用冻死的期望值 ——
        它们同样是"改前的结果"，只是提前算好写在这里。
        """
        cases = [
            (_fx_kb(_fx_variant_terms(200)), _FX_DIRTY200_DUPS),
            (_fx_kb(_fx_variant_terms(1000)), _FX_DIRTY1000_DUPS),
            (_fx_kb(_fx_terms(2000)), _FX_CLEAN2000_DUPS),
        ]
        for data, expected in cases:
            self.assertEqual(_fx_triples(kb.find_duplicates(data, limit=10 ** 9)), expected)
        # 干净夹具上一条都不许多出来（否则"2000 卡"那条的耗时就没有判别力了）
        self.assertEqual(_fx_triples(kb.find_duplicates(_fx_kb(_fx_terms(1000)), limit=10 ** 9)), [])

    def test_tricky_cards_still_pair_up_exactly_as_before(self):
        """四种判据 + 单字 id + 非字符串/空 id + term 与 id 不一致：11 条期望值一字不差。"""
        self.assertEqual(_fx_triples(kb.find_duplicates(_FX_TRICKY_KBS, limit=10 ** 9)),
                         _FX_TRICKY_DUPS)
        # 默认 limit=20 时上面这 10 条还装得下 ⇒ 与全量相同
        self.assertEqual(_fx_triples(kb.find_duplicates(_FX_TRICKY_KBS)), _FX_TRICKY_DUPS)

    def test_limit_still_truncates_the_same_scan_order_prefix(self):
        """`limit` 截断的必须是**扫描顺序的前 limit 条**（剪枝不许改变 (i, j) 的遍历顺序）。"""
        self.assertEqual(_fx_triples(kb.find_duplicates(_FX_LIMIT40_KBS)), _FX_LIMIT40_DUPS)
        self.assertEqual(len(kb.find_duplicates(_FX_LIMIT40_KBS)), 20)
        # 既有语义里 limit=0 不是"不限量"（`len(out) >= 0` 恒真 ⇒ 只出一条）——照旧
        self.assertEqual(_fx_triples(kb.find_duplicates(_FX_LIMIT40_KBS, limit=0)),
                         _FX_LIMIT40_DUPS[:1])
        self.assertEqual(_fx_triples(kb.find_duplicates(_FX_LIMIT40_KBS, limit=1)),
                         _FX_LIMIT40_DUPS[:1])

    def test_pruning_cuts_the_number_of_pairwise_comparisons(self):
        """2000 卡：剪枝必须真的**少比较**（确定性判据，不靠墙钟）。

        改前实测：`_dup_reason` 被调 1,999,000 次（全量对）；剪枝后 159,031 次（~8%）。
        阈值取"全量对的 1/4"（500k）：留 3 倍余量，但"把剪枝改回全量两两"必红。

        ⚠️ 为什么墙钟断言**不在这条**里：`mock.patch.object(wraps=...)` 每次调用要走一层
        MagicMock（实测 159k 次 = +1.9s），带上它测耗时就成了在测 mock 的开销 ——
        所以耗时单独一条、不带 spy（见下一条）。
        """
        data = _fx_kb(_fx_terms(2000))
        with mock.patch.object(kb, "_dup_reason", wraps=kb._dup_reason) as spy:
            dups = kb.find_duplicates(data, limit=10 ** 9)
        self.assertEqual(_fx_triples(dups), _FX_CLEAN2000_DUPS, "剪枝改变了判据")
        total_pairs = 2000 * 1999 // 2
        self.assertLess(len(_FX_CLEAN2000_DUPS), 20,
                        "反证：这张夹具上旧实现不可能提前返回（命中数 < limit），比较次数才有判别力")
        self.assertLess(spy.call_count, total_pairs // 4,
                        "剪枝没起作用：_dup_reason 调了 %d 次（全量 %d 次）"
                        % (spy.call_count, total_pairs))

    def test_2000_cards_stay_under_the_wall_clock_budget(self):
        """次判据（宽松，别当唯一证据）：2000 卡剪枝后墙钟 < 2s。

        改前实测 5.79~6.12s（干净库也不提前返回），剪枝后本机 ~0.62s ⇒ 3 倍余量。
        这是纯墙钟断言，所以**不带 spy**（理由见上一条）。
        """
        data = _fx_kb(_fx_terms(2000))
        t0 = time.perf_counter()
        dups = kb.find_duplicates(data, limit=10 ** 9)
        elapsed = time.perf_counter() - t0
        self.assertEqual(_fx_triples(dups), _FX_CLEAN2000_DUPS, "剪枝改变了判据")
        self.assertLess(elapsed, 2.0,
                        "2000 张卡剪枝后仍要 %.2fs（改前实测 5.8~6.1s）" % elapsed)


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

    def __init__(self, path, method="GET", body=None):
        self.path = path
        self.command = method
        payload = b"" if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.rfile = io.BytesIO(payload)
        # Content-Length 必须与 payload 一致：_json_body() 按它读，不一致就会读空/读残
        self.headers = {"Content-Length": str(len(payload))}
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


def _post(path, body):
    h = _SandboxedHandler(path, method="POST", body=body)
    h.do_POST()
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

    def test_zero_absorbable_terms_does_not_touch_a_corrupt_kb_file(self):
        # I1 的第二形态：报告/回填传进来的节点全是空术语（analyze 那条路径有 if know_nodes 守卫，
        # 但 absorb_reports / CLI 回填没有）。absorb 自己必须在隔离之前早退。
        store.ensure_profile()
        path = store.knowledge_path()
        store.write_text(path, "{ 坏文件")
        self.assertEqual(kb.absorb([_node(term=""), _node(term="、、。")], {"id": "j1"}), (0, 0))
        self.assertEqual(store.read_text(path), "{ 坏文件")
        self.assertEqual(glob.glob(path + ".corrupt-*"), [])


class KnowledgeWriteRouteTest(IsolatedCase):
    """I5：/api/knowledge/state 与 /api/knowledge/merge 是前端仅有的两条写路径，此前零覆盖。

    用与 KnowledgeRouteTest 同一套沙箱化 handler（不接套接字、直接调 do_POST），
    DATA_ROOT / LOG_FILE 由 IsolatedCase 指向临时目录。
    """

    def _seed(self):
        store.ensure_profile()
        store.save_knowledge({"nodes": [
            {"id": "省略恢复", "term": "省略恢复", "state": "待学习", "last_outcome": "未面试",
             "asked_count": 0, "from_jds": [{"id": "j1", "job_title": "岗1"}]},
            {"id": "省略识别及恢复", "term": "省略识别及恢复", "state": "待学习", "last_outcome": "未面试",
             "asked_count": 0, "from_jds": [{"id": "j2", "job_title": "岗2"}]},
        ]})

    def test_set_state_route_200_and_persists(self):
        self._seed()
        code, body = _post("/api/knowledge/state", {"id": "省略恢复", "state": "已掌握"})
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        # 不只是看响应：库内的数据必须真的变了，而且另一张卡一点没动
        cards = {n["id"]: n for n in store.load_knowledge()["nodes"]}
        self.assertEqual(cards["省略恢复"]["state"], "已掌握")
        self.assertEqual(cards["省略识别及恢复"]["state"], "待学习")
        self.assertTrue(_log_events("kb.state_set"))

    def test_set_state_route_accepts_raw_term(self):
        # 前端传的是节点 id，但接口契约允许原始术语（内部 normalize_id）
        self._seed()
        code, body = _post("/api/knowledge/state", {"id": "省略恢复（全角括号）", "state": "学习中"})
        self.assertEqual(code, 400)                    # 归一化后对不上，如实报错而不是假装成功
        self.assertFalse(body["ok"])
        code, body = _post("/api/knowledge/state", {"id": "省略恢复", "state": "学习中"})
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(store.load_knowledge()["nodes"][0]["state"], "学习中")

    def test_set_state_route_rejects_bad_state(self):
        self._seed()
        code, body = _post("/api/knowledge/state", {"id": "省略恢复", "state": "已精通"})
        self.assertEqual(code, 400)
        self.assertFalse(body["ok"])
        self.assertIn("状态不合法", body["error"])
        self.assertEqual(store.load_knowledge()["nodes"][0]["state"], "待学习")   # 库没被改

    def test_set_state_route_rejects_unknown_id(self):
        self._seed()
        code, body = _post("/api/knowledge/state", {"id": "不存在的卡", "state": "已掌握"})
        self.assertEqual(code, 400)
        self.assertFalse(body["ok"])
        self.assertIn("找不到该知识点", body["error"])

    def test_set_state_route_tolerates_missing_fields(self):
        # 请求体缺 id/state 时不能抛（do_POST 外层 try 会兜成 500，但这是客户端错误，该是 400）
        self._seed()
        code, body = _post("/api/knowledge/state", {})
        self.assertEqual(code, 400)
        self.assertFalse(body["ok"])

    def test_merge_route_200_and_merges_for_real(self):
        self._seed()
        code, body = _post("/api/knowledge/merge", {"keep_id": "省略恢复", "drop_id": "省略识别及恢复"})
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["message"], body["error"])          # 成功时两者同文案
        cards = store.load_knowledge()["nodes"]
        self.assertEqual(len(cards), 1)                            # drop 卡真的被移除了
        self.assertEqual(cards[0]["id"], "省略恢复")
        self.assertIn("省略识别及恢复", cards[0]["aliases"])
        self.assertEqual([j["id"] for j in cards[0]["from_jds"]], ["j1", "j2"])
        self.assertTrue(_log_events("kb.merge_done"))

    def test_merge_route_rejects_identical_ids(self):
        self._seed()
        code, body = _post("/api/knowledge/merge", {"keep_id": "省略恢复", "drop_id": "省略恢复"})
        self.assertEqual(code, 400)
        self.assertFalse(body["ok"])
        self.assertIn("不能和自己合并", body["error"])
        self.assertEqual(len(store.load_knowledge()["nodes"]), 2)  # 拒绝时绝不动库

    def test_merge_route_rejects_unknown_id(self):
        self._seed()
        code, body = _post("/api/knowledge/merge", {"keep_id": "省略恢复", "drop_id": "不存在的卡"})
        self.assertEqual(code, 400)
        self.assertFalse(body["ok"])
        self.assertIn("找不到要合并的知识点", body["error"])
        self.assertEqual(len(store.load_knowledge()["nodes"]), 2)

    def test_merge_route_rejects_empty_ids(self):
        self._seed()
        code, body = _post("/api/knowledge/merge", {})
        self.assertEqual(code, 400)
        self.assertFalse(body["ok"])
        self.assertIn("两个知识点都要选", body["error"])


class ReviewFieldsTest(IsolatedCase):
    def test_new_card_has_review_defaults(self):
        kb.absorb([_node(term="甲")], {"id": "j1"})
        c = store.load_knowledge()["nodes"][0]
        self.assertEqual(c["review_count"], 0)
        self.assertEqual(c["last_reviewed_at"], "")
        self.assertEqual(c["last_result"], "")

    def test_existing_card_without_fields_gets_defaults_on_read(self):
        store.save_knowledge({"version": 1, "nodes": [
            {"id": "甲", "term": "甲", "sources": [], "from_jds": []}]})
        c = kb.ensure_kb(store.load_knowledge())["nodes"][0]
        self.assertEqual(c["review_count"], 0)
        self.assertEqual(c["last_reviewed_at"], "")
        self.assertEqual(c["last_result"], "")

    def test_junk_review_fields_are_tolerated(self):
        store.save_knowledge({"version": 1, "nodes": [
            {"id": "甲", "term": "甲", "review_count": "3", "last_reviewed_at": 5,
             "last_result": {"x": 1}, "sources": [], "from_jds": []}]})
        c = kb.ensure_kb(store.load_knowledge())["nodes"][0]
        self.assertEqual(c["review_count"], 3)          # 字符串数字能被修好
        self.assertEqual(c["last_reviewed_at"], "")     # 非字符串一律当"没复习过"
        self.assertEqual(c["last_result"], "")          # 非法结果值一律清空


class ReviewQueueTest(IsolatedCase):
    def _seed(self):
        kb.absorb([
            _node(term="待学多岗", layer=0),
            _node(term="已掌握", layer=0),
            _node(term="学习中", layer=0),
        ], {"id": "j1"})
        kb.absorb([_node(term="待学多岗")], {"id": "j2"})     # 让它是"被 2 个岗位提到"
        kb.set_state("已掌握", "已掌握")
        kb.set_state("学习中", "学习中")

    def test_ordering_weights_learning_state_and_jd_count(self):
        self._seed()
        q = [n["term"] for n in kb.review_queue(store.load_knowledge())]
        self.assertEqual(q[0], "待学多岗")                    # 待学习 + 2 个岗位
        self.assertLess(q.index("学习中"), q.index("已掌握"))  # 学习中的权重高于已掌握

    def test_score_and_reason_are_returned(self):
        self._seed()
        first = kb.review_queue(store.load_knowledge())[0]
        self.assertGreater(first["score"], 0)
        self.assertIn("待学习", first["reason"])
        self.assertIn("2 个岗位", first["reason"])

    def test_never_reviewed_ranks_above_recently_reviewed(self):
        kb.absorb([_node(term="甲"), _node(term="乙")], {"id": "j1"})
        d = store.load_knowledge()
        for n in d["nodes"]:
            if n["term"] == "乙":
                n["last_reviewed_at"] = kb._today()
        store.save_knowledge(d)
        q = [n["term"] for n in kb.review_queue(store.load_knowledge())]
        self.assertEqual(q[0], "甲")

    def test_limit_is_respected_and_order_is_deterministic(self):
        kb.absorb([_node(term="概念%02d" % i) for i in range(30)], {"id": "j1"})
        a = [n["term"] for n in kb.review_queue(store.load_knowledge(), limit=10)]
        b = [n["term"] for n in kb.review_queue(store.load_knowledge(), limit=10)]
        self.assertEqual(len(a), 10)
        self.assertEqual(a, b)                                # 同分按术语升序，稳定

    def test_weight_ordering_is_not_a_term_tie_break_accident(self):
        """「学习中」必须**严格高于**「已掌握」—— 不能让术语升序替权重蒙对答案。

        上面 test_ordering_weights_learning_state_and_jd_count 里两张卡同分（都是 1 个岗位、
        都没复习过），而 tie-break 是术语升序 + 「学」(U+5B66) 恰好排在「已」(U+5DF2) 前面，
        所以把 _REVIEW_WEIGHT 改成都相等时它**依然全绿**（实测假绿）。
        这里让「已掌握」那张卡的术语排在最前做对照：只有权重真的起作用，它才会掉到后面。
        """
        kb.absorb([_node(term="A 已掌握对照"), _node(term="B 学习中对照")], {"id": "j1"})
        kb.set_state("A 已掌握对照", "已掌握")
        kb.set_state("B 学习中对照", "学习中")
        q = [n["term"] for n in kb.review_queue(store.load_knowledge())]
        self.assertEqual(q[0], "B 学习中对照")

    def test_empty_kb_is_fine(self):
        self.assertEqual(kb.review_queue({"nodes": []}), [])


def _days_ago(n):
    """n 天前的日期串（`%Y-%m-%d`）——按**当下**算，不用写死日期。"""
    return (datetime.date.today() - datetime.timedelta(days=n)).strftime("%Y-%m-%d")


def _reason_card(term="甲", **kw):
    """只为 `kb._review_reason` 造一张卡：只摆理由文案会读的字段，其余给默认值。"""
    c = {"term": term, "state": "待学习", "from_jds": [], "asked_count": 0,
         "review_count": 0, "last_reviewed_at": "", "last_result": "",
         "last_outcome": "未面试"}
    c.update(kw)
    return c


class ReviewReasonTest(IsolatedCase):
    """`kb._review_reason` 的**完整映射表**（体检 **P1-3**：理由文案必须与卡上的事实一致）。

    背景：老实现让 `_days_since()` 一个数同时干两件事 —— **评分**（久未复习加成封顶 30 天）
    与**说话**。于是 `min(天, 30) >= 30` 既可能是「从没复习过」，也可能是「复习过 45 天」，
    文案一律写成「还没复习过」：`last_reviewed_at=45 天前` + `review_count=3` 的卡会显示
    「待学习 · 还没复习过」这句**假话**（体检报告 §3 P1-3 的实跑证据）。

    这里钉的是**每一支**的**具体文案**（不是 `assertIsInstance(str)` 那种没牙的断言）：
    从没复习 / 今天复习过 / 1~29 天 / 恰好 30 天 / 45 天 / 有次数没日期 / 日期读不出来 /
    日期在未来 / 空 state / 非法 state / asked_count 为负 / last_outcome 各种值 / 坏数据。
    """

    def _reasons(self, *cards):
        """卡片写进隔离库 → 走**真实读路径**（`ensure_kb` 归一 → `review_queue`）→ {术语: 理由}。

        为什么不直接调 `_review_reason(card)`：那会绕开 `ensure_kb` 这道咽喉
        （「非字符串日期一律当空」等归一都发生在那里），而线上/界面上真正跑的就是这条路径。
        """
        nodes = []
        for c in cards:
            c = dict(c)
            n = {"id": c["term"], "term": c["term"], "sources": [], "from_jds": []}
            n.update(c)
            nodes.append(n)
        store.save_knowledge({"version": 1, "nodes": nodes})
        return {n["term"]: n["reason"]
                for n in kb.review_queue(store.load_knowledge(), limit=0)}

    # ---------- 复习时间这一支（P1-3 的被告状分支） ----------

    def test_never_reviewed_says_never_reviewed(self):
        self.assertEqual(self._reasons(_reason_card())["甲"], "待学习 · 还没复习过")

    def test_reviewed_45_days_ago_is_not_never_reviewed(self):
        """P1-3 的被告状组合：复习过 30 天以上，绝不许说成「还没复习过」。"""
        r = self._reasons(_reason_card(review_count=3, last_reviewed_at=_days_ago(45)))["甲"]
        self.assertEqual(r, "待学习 · 45 天没复习")
        self.assertNotIn("还没复习过", r)

    def test_29_and_30_days_ago_report_the_real_number(self):
        """30 天是评分的**加成上限**，不是「从没复习过」的判据 —— 29/30 都要如实说天数。"""
        got = self._reasons(_reason_card(term="甲", last_reviewed_at=_days_ago(29)),
                            _reason_card(term="乙", last_reviewed_at=_days_ago(30)))
        self.assertEqual(got["甲"], "待学习 · 29 天没复习")
        self.assertEqual(got["乙"], "待学习 · 30 天没复习")

    def test_reviewed_today(self):
        self.assertEqual(self._reasons(_reason_card(last_reviewed_at=kb._today()))["甲"],
                         "待学习 · 今天复习过")

    def test_review_count_without_date_is_admitted_not_denied(self):
        """有自测次数、日期却是空的（手改坏/旧数据）→ 说次数，别替它说「还没复习过」。

        `_normalise_review_fields` 把非字符串的 `last_reviewed_at` 一律归成空串，
        所以「review_count=3 + last_reviewed_at=5」这个**真实可达**的组合必须走这一支。
        """
        got = self._reasons(_reason_card(review_count=3, last_reviewed_at=5))["甲"]
        self.assertEqual(got, "待学习 · 复习过 3 次（没记日期）")
        self.assertNotIn("还没复习过", got)

    def test_unparseable_date_is_admitted_not_guessed(self):
        """日期串读不出来 → 如实说读不出来，绝不猜成「还没复习过」或「今天复习过」。"""
        got = self._reasons(_reason_card(last_reviewed_at="2026-13-45"))["甲"]
        self.assertEqual(got, "待学习 · 上次复习日期读不出来（2026-13-45）")

    def test_future_date_is_not_reported_as_today(self):
        """日期在未来（手改 / 改过系统时钟）→ 不许说「今天复习过」（那是假话）。"""
        tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        got = self._reasons(_reason_card(last_reviewed_at=tomorrow))["甲"]
        self.assertEqual(got, "待学习 · 上次复习日期在未来（%s）" % tomorrow)

    # ---------- state 这一支 ----------

    def test_bad_state_is_not_dressed_up_as_a_known_state(self):
        """非法 state（手改出「精通」/ 5）→ 说「状态未知」，不许冒充满分权重最高的「待学习」。

        `_review_score` 对非法 state 用的是 `_REVIEW_WEIGHT.get(state, 1)` → weight=1（最低），
        文案要是写「待学习」就等于宣称它按最高权重 3 排的。
        """
        got = self._reasons(_reason_card(term="甲", state="精通"),
                            _reason_card(term="乙", state=5))
        self.assertEqual(got["甲"], "状态未知（精通） · 还没复习过")
        self.assertEqual(got["乙"], "状态未知（5） · 还没复习过")

    def test_count_zero_with_a_corrupt_date_is_still_about_the_date(self):
        """`review_count==0` + 坏日期 → 说的是**日期读不出来**，不是「还没复习过」。

        两个字段必须各按各的事实说：日期坏了就报日期坏了（那一支还能被修好），
        不能因为「次数是 0」就把「日期读不出来」这句真话吞掉。
        """
        got = self._reasons(_reason_card(review_count=0, last_reviewed_at="昨天"))["甲"]
        self.assertEqual(got, "待学习 · 上次复习日期读不出来（昨天）")

    def test_negative_review_count_is_not_printed_as_a_count(self):
        """`review_count=-3`（手改坏）→ 不许写「复习过 -3 次」（负的自测次数不存在）。

        `_review_age_text` 的判据是 `count > 0`，所以负值落回同一支「还没复习过」——
        与事实一致：没有过任何一次自测。
        """
        got = self._reasons(_reason_card(review_count=-3))["甲"]
        self.assertEqual(got, "待学习 · 还没复习过")

    def test_whitespace_only_date_is_treated_as_absent(self):
        """`"   "` 经 `_normalise_review_fields` 的 `strip()` 成了空串 → 走「还没复习过」，
        不许把它当成一个读不出来的日期印成「上次复习日期读不出来（ ）」。"""
        got = self._reasons(_reason_card(review_count=0, last_reviewed_at="   "))["甲"]
        self.assertEqual(got, "待学习 · 还没复习过")

    def test_wording_is_not_capped_at_30_days_but_the_score_is(self):
        """**不封顶与封顶的分工**：45 天前复习的卡，**文案**说 45 天，**评分**仍按上限 30 天算。

        这正是 P1-3 的根因 —— `_days_since()` 一个数同时干两件事。修法是把它们分开：
        说话走 `_review_age_text`（不封顶），评分继续走 `_days_since`（封顶 30，旧行为不变）。
        所以这里同时钉两个数：文案「45 天没复习」，且 score 与「从没复习过」那张卡**完全相等**
        （都是封顶到 30 天）。谁要是为了改文案顺手动了 `_days_since`，这条就会红。
        """
        store.save_knowledge({"version": 1, "nodes": [
            {"id": "甲", "term": "甲", "sources": [], "from_jds": [],
             "last_reviewed_at": _days_ago(45), "review_count": 3},
            {"id": "乙", "term": "乙", "sources": [], "from_jds": []}]})
        q = {n["term"]: n for n in kb.review_queue(store.load_knowledge(), limit=0)}
        self.assertEqual(q["甲"]["reason"], "待学习 · 45 天没复习")
        self.assertEqual(q["乙"]["reason"], "待学习 · 还没复习过")
        self.assertEqual(q["甲"]["score"], q["乙"]["score"],
                         "久未复习的加成封顶 30 天：45 天前复习过的卡与从没复习过的卡同分")
        # 两张卡同分时按术语升序（「乙」U+4E59 < 「甲」U+7532），顺序确定
        self.assertEqual([n["term"] for n in kb.review_queue(store.load_knowledge(), limit=0)],
                         ["乙", "甲"])

    def test_missing_state_is_said_as_the_same_ruler_the_score_uses(self):
        """空/缺失 state 与 `_review_score` **同一把尺子**：评分按 DEFAULT_STATE=待学习 算（权重 3），
        所以文案也说「待学习」—— 判据与文案对齐，而不是各说各的。"""
        got = self._reasons(_reason_card(term="甲", state=""),
                            _reason_card(term="乙", state=None))
        self.assertEqual(got["甲"], "待学习 · 还没复习过")
        self.assertEqual(got["乙"], "待学习 · 还没复习过")

    # ---------- asked_count / last_outcome / 岗位数 ----------

    def test_negative_asked_count_is_not_turned_into_a_claim(self):
        """asked_count=-3（手改坏）→ 不许写「面试被问过 -3 次」（没人被问过负数次）。"""
        got = self._reasons(_reason_card(term="甲", asked_count=-3),
                            _reason_card(term="乙", asked_count=2))
        self.assertEqual(got["甲"], "待学习 · 还没复习过")
        self.assertEqual(got["乙"], "待学习 · 面试被问过 2 次 · 还没复习过")

    def test_last_outcome_values_do_not_leak_into_review_wording(self):
        """`last_outcome` 是**面试结果**，不是自测记录：各种取值都不许搅动复习理由。

        （它与 `asked_count` 是两回事：`_record_interview_in` 里 weak/good 只写 `last_outcome`、
        不加 `asked_count`，所以「asked_count=0 + last_outcome=没答上」是真会出现的组合；
        反正理由里一个字都不提面试结果，就不会说假话。）
        """
        values = ("未面试", "答上了", "没答上", 5, None, "")
        cards = [_reason_card(term="t%d" % i, last_outcome=v)
                 for i, v in enumerate(values)]
        got = self._reasons(*cards)
        for i, v in enumerate(values):
            self.assertEqual(got["t%d" % i], "待学习 · 还没复习过",
                             "last_outcome=%r 不许改变复习理由" % (v,))

    def test_jd_clause_only_when_really_more_than_one_valid_jd(self):
        """「被 N 个岗位提到」与 `all_jds` 同一把尺子：只有**非空字符串 id** 才算一个岗位。"""
        got = self._reasons(
            _reason_card(term="甲", from_jds=[{"id": "j1"}, {"id": "j2"}]),
            _reason_card(term="乙", from_jds=[{"id": "j1"}]),
            _reason_card(term="丙", from_jds=[{"id": "j1"}, {"id": 5}, {"id": ""}]))
        self.assertEqual(got["甲"], "待学习 · 被 2 个岗位提到 · 还没复习过")
        self.assertEqual(got["乙"], "待学习 · 还没复习过")
        self.assertEqual(got["丙"], "待学习 · 还没复习过")

    def test_full_reason_matches_the_documented_example(self):
        """README 给用户看的例子必须真的能由实现产出（四段全齐、顺序一致）。"""
        got = self._reasons(_reason_card(
            term="甲", from_jds=[{"id": "j1"}, {"id": "j2"}], asked_count=1,
            review_count=2, last_reviewed_at=_days_ago(21)))["甲"]
        self.assertEqual(got, "待学习 · 被 2 个岗位提到 · 面试被问过 1 次 · 21 天没复习")


class RecordReviewTest(IsolatedCase):
    def _one(self, state):
        kb.absorb([_node(term="甲")], {"id": "j1"})
        if state != "待学习":
            kb.set_state("甲", state)
        return "甲"

    def test_answer_up_sets_mastered(self):
        nid = self._one("待学习")
        ok, node = kb.record_review(nid, kb.RESULT_UP)
        self.assertTrue(ok)
        self.assertEqual(node["state"], "已掌握")
        self.assertEqual(node["review_count"], 1)
        self.assertEqual(node["last_reviewed_at"], kb._today())
        self.assertEqual(node["last_result"], "答上了")

    def test_answer_down_from_mastered_goes_to_learning(self):
        nid = self._one("已掌握")
        ok, node = kb.record_review(nid, kb.RESULT_DOWN)
        self.assertTrue(ok)
        self.assertEqual(node["state"], "学习中")
        self.assertEqual(node["last_result"], "没答上")

    def test_answer_down_from_learning_goes_to_not_started(self):
        nid = self._one("学习中")
        ok, node = kb.record_review(nid, kb.RESULT_DOWN)
        self.assertTrue(ok)
        self.assertEqual(node["state"], "待学习")

    def test_answer_down_from_not_started_stays_put(self):
        nid = self._one("待学习")
        ok, node = kb.record_review(nid, kb.RESULT_DOWN)
        self.assertTrue(ok)
        self.assertEqual(node["state"], "待学习")

    def test_review_count_accumulates(self):
        nid = self._one("待学习")
        kb.record_review(nid, kb.RESULT_DOWN)
        kb.record_review(nid, kb.RESULT_UP)
        self.assertEqual(kb.review_queue(store.load_knowledge())[0]["review_count"], 2)

    def test_rejects_bad_result_and_unknown_id(self):
        nid = self._one("待学习")
        self.assertFalse(kb.record_review(nid, "随便")[0])
        self.assertFalse(kb.record_review("不存在", kb.RESULT_UP)[0])

    def test_accepts_raw_term_and_normalised_id_alike(self):
        self._one("待学习")
        self.assertTrue(kb.record_review("甲", kb.RESULT_UP)[0])


class UpsertCheckTest(IsolatedCase):
    def test_creates_card_when_missing(self):
        ok, created, card = kb.upsert_check("品牌新概念", "已掌握",
                                            {"definition": "定义", "category": "核心概念"})
        self.assertTrue(ok)
        self.assertTrue(created)
        self.assertEqual(card["state"], "已掌握")
        self.assertEqual(card["definition"], "定义")
        self.assertEqual(len(store.load_knowledge()["nodes"]), 1)

    def test_updates_existing_card_without_creating(self):
        kb.absorb([_node(term="甲")], {"id": "j1"})
        ok, created, card = kb.upsert_check("甲", "已掌握")
        self.assertTrue(ok)
        self.assertFalse(created)
        self.assertEqual(card["state"], "已掌握")
        self.assertEqual(len(store.load_knowledge()["nodes"]), 1)

    def test_defined_metadata_does_not_overwrite_existing_richer_content(self):
        kb.absorb([_node(term="甲", definition="很长很完整的原定义内容")], {"id": "j1"})
        kb.upsert_check("甲", "学习中", {"definition": "短"})
        self.assertEqual(store.load_knowledge()["nodes"][0]["definition"], "很长很完整的原定义内容")

    def test_accepts_normalised_id_too(self):
        kb.upsert_check("Transformer 架构", "已掌握")
        ok, created, card = kb.upsert_check("transformer架构", "学习中")
        self.assertTrue(ok)
        self.assertFalse(created)
        self.assertEqual(card["state"], "学习中")

    def test_rejects_bad_state_and_empty_term(self):
        self.assertFalse(kb.upsert_check("甲", "随便")[0])
        self.assertFalse(kb.upsert_check("", "已掌握")[0])
        self.assertFalse(kb.upsert_check("（）", "已掌握")[0])

    def test_uncheck_maps_to_not_learned(self):
        kb.upsert_check("甲", "已掌握")
        ok, _, card = kb.upsert_check("甲", "待学习")
        self.assertTrue(ok)
        self.assertEqual(card["state"], "待学习")


class ReportStateRouteTest(IsolatedCase):
    """打钩持久化的两条 HTTP 路由（POST /api/report_state/read | write）。

    用已有的沙箱化 handler（不接套接字）直接调 do_POST；DATA_ROOT/LOG_FILE 已在 IsolatedCase 里隔离。
    走路由而不是直调 store：路由层的 report_id 校验（400）与 know 映射只有在这一层才测得到。
    """

    def _post(self, path, body):
        return _post(path, body)[1]

    def _post_raw(self, path, body):
        return _post(path, body)

    def test_read_returns_checks_and_know_states(self):
        kb.absorb([_node(term="省略恢复")], {"id": "j1"})
        kb.set_state("省略恢复", "已掌握")
        store.save_report_state("r1", {"gate:0": True})
        body = self._post("/api/report_state/read", {"report_id": "r1", "terms": ["省略恢复", "没见过"]})
        self.assertTrue(body["ok"])
        self.assertEqual(body["checks"], {"gate:0": True})
        self.assertEqual(body["know"]["省略恢复"], "已掌握")
        self.assertEqual(body["know"]["没见过"], "待学习")

    def test_write_saves_both_kinds_and_upserts(self):
        body = self._post("/api/report_state/write", {
            "report_id": "r1",
            "checks": {"gate:1": True},
            "know": {"新概念": {"state": "已掌握", "definition": "定义"}}})
        self.assertTrue(body["ok"])
        self.assertEqual(body["created"], 1)
        self.assertEqual(store.load_report_state("r1")["checks"], {"gate:1": True})
        self.assertEqual(store.load_knowledge()["nodes"][0]["state"], "已掌握")

    def test_bad_report_id_is_rejected(self):
        # 判据与 store.report_state_path 一致：safe_name 为空 → 400（'' / '.' / '..'）
        for bad in ("", "..", "."):
            code, body = self._post_raw("/api/report_state/write", {"report_id": bad, "checks": {"a": True}})
            self.assertEqual(code, 400, "report_id=%r 应被拒" % bad)
            self.assertFalse(body["ok"])
        d = store.report_state_dir()
        self.assertEqual(os.listdir(d) if os.path.isdir(d) else [], [])   # 拒了就不该落盘

    def test_traversal_shaped_report_id_is_sanitised_not_escaped(self):
        # '../evil' 形态：store.safe_name 取 basename 削成 'evil'（与 tests/test_store.py 同一口径），
        # 所以路由放行 —— 但落盘路径必须仍在 report_state 目录内，绝不越级。
        code, body = self._post_raw("/api/report_state/write", {"report_id": "../evil", "checks": {"a": True}})
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(os.listdir(store.report_state_dir()), ["evil.json"])
        self.assertEqual(store.load_report_state("../evil")["checks"], {"a": True})

    def test_read_with_bad_report_id_is_rejected(self):
        code, _ = self._post_raw("/api/report_state/read", {"report_id": ""})
        self.assertEqual(code, 400)

    def test_write_rejects_bad_know_state(self):
        """**契约更新（P1-12：消灭假 ok）**：这一项没落盘时，路由**不许**回 `ok: True`。

        旧断言钉的是**要消灭的那个旧契约**：`/api/report_state/write` 以前在
        `kb.upsert_check` 返回 `ok=False` 时只把它**不计入 `saved`**、随后**无条件**回
        `{"ok": True}` —— 前端（web/report_template.html:292 的 `if(!r || r.ok !== true)`）
        于是显示「已保存 ✓」并 keep 住勾选框，而用户那一项**根本没落盘**（关掉程序就没了）。
        现在路由改成「**有失败就绝不回 `ok: True`**」（见 app.py 里该分支的注释与
        tests/test_app.py 的 `ReportStateWriteOkTest`），所以这里改钉新契约：
        `ok is False` + 能看出**是哪一项、什么原因**，而不是一句笼统的失败。

        （前端把状态拼错时不能静默吞掉：这一项不建卡、`saved` 为 0。）
        """
        code, body = self._post_raw("/api/report_state/write",
                                    {"report_id": "r1", "know": {"新概念": {"state": "已精通"}}})
        self.assertEqual(code, 200, "整条写请求本身成功，只是这一项没写进去")
        # 有失败就不许回假 ok：旧契约在这里回 True，用户以为存上了
        self.assertIs(body["ok"], False, "这一项真的没落盘，不许回 ok: True")
        self.assertEqual(body["failed"], 1, "要如实报出有几项没存上")
        self.assertIn("已精通", body["error"], "错误里要能看出是哪一项、什么原因")
        self.assertIn("新概念", body["error"], "错误里要能看出是哪一项、什么原因")
        self.assertEqual(body["saved"], 0)                # checks 为空、know 的 state 非法 → 一个都没存
        self.assertEqual(store.load_knowledge()["nodes"], [])


class ReviewRouteTest(IsolatedCase):
    """自测队列的两条 HTTP 路由（GET / 记录 POST /api/knowledge/review）。"""

    def _get(self, path):
        return _get(path)[1]

    def _post(self, path, body):
        return _post(path, body)[1]

    def _post_raw(self, path, body):
        return _post(path, body)

    def _get_raw(self, path):
        return _get(path)

    def test_queue_returns_score_and_reason(self):
        kb.absorb([_node(term="甲")], {"id": "j1"})
        body = self._get("/api/knowledge/review?limit=5")
        self.assertTrue(body["ok"])
        self.assertEqual(body["queue"][0]["term"], "甲")
        self.assertIn("reason", body["queue"][0])
        self.assertIn("stats", body)

    def test_queue_honours_limit_and_default(self):
        kb.absorb([_node(term="概念%02d" % i) for i in range(30)], {"id": "j1"})
        self.assertEqual(len(self._get("/api/knowledge/review")["queue"]), 20)      # 默认 20，不是全量
        self.assertEqual(len(self._get("/api/knowledge/review?limit=50")["queue"]), 30)

    def test_queue_survives_unexpected_error(self):
        # /api/knowledge/review 是 do_GET 里第二处可能抛的读取路径，而 do_GET 没有外层 try：
        # 不兜底就会裸断连接（客户端只看到 RemoteDisconnected，连 500 都没有）。
        kb.absorb([_node(term="甲")], {"id": "j1"})
        with mock.patch.object(kb, "review_queue", side_effect=RuntimeError("boom")):
            code, body = self._get_raw("/api/knowledge/review")
        self.assertEqual(code, 500)
        self.assertFalse(body["ok"])
        self.assertIn("复习队列读取失败", body["error"])
        self.assertTrue(_log_events("kb.review_api_error"))

    def test_record_advances_state(self):
        kb.absorb([_node(term="甲")], {"id": "j1"})
        body = self._post("/api/knowledge/review", {"id": "甲", "result": "答上了"})
        self.assertTrue(body["ok"])
        self.assertEqual(body["node"]["state"], "已掌握")

    def test_record_rejects_bad_result(self):
        kb.absorb([_node(term="甲")], {"id": "j1"})
        code, _ = self._post_raw("/api/knowledge/review", {"id": "甲", "result": "瞎写"})
        self.assertEqual(code, 400)

    def test_record_rejects_unknown_node(self):
        kb.absorb([_node(term="甲")], {"id": "j1"})
        code, _ = self._post_raw("/api/knowledge/review", {"id": "不存在的卡", "result": "答上了"})
        self.assertEqual(code, 400)


class InterviewFeedbackTest(IsolatedCase):
    def _seed(self):
        kb.absorb([_node(term="RLHF"), _node(term="语音识别"),
                   _node(term="上下文窗口"), _node(term="模型")], {"id": "j1"})

    def test_hits_are_deterministic_and_skip_two_char_terms(self):
        self._seed()
        text = "我们聊了 RLHF 和语音识别，也提到上下文窗口。模型这个词出现了很多次。"
        hits = [n["term"] for n in kb.interview_hits(text, store.load_knowledge())]
        self.assertIn("RLHF", hits)
        self.assertIn("语音识别", hits)
        self.assertNotIn("模型", hits)                    # 2 字词不参与

    def test_two_char_terms_need_repeated_mentions(self):
        self._seed()
        once = kb.interview_hits("模型", store.load_knowledge())
        thrice = kb.interview_hits("模型 模型 模型", store.load_knowledge())
        self.assertEqual(once, [])
        self.assertEqual([n["term"] for n in thrice], ["模型"])

    def test_record_interview_updates_asked_count_and_outcome(self):
        self._seed()
        kb.record_interview("j1", asked=["RLHF"], weak=["RLHF"], good=["语音识别"])
        cards = {n["term"]: n for n in store.load_knowledge()["nodes"]}
        self.assertEqual(cards["RLHF"]["asked_count"], 1)
        self.assertEqual(cards["RLHF"]["last_outcome"], "没答上")
        self.assertEqual(cards["语音识别"]["last_outcome"], "答上了")

    def test_weak_concept_already_mastered_is_downgraded(self):
        self._seed()
        kb.set_state("RLHF", "已掌握")
        kb.record_interview("j1", weak=["RLHF"])
        cards = {n["term"]: n for n in store.load_knowledge()["nodes"]}
        self.assertEqual(cards["RLHF"]["state"], "学习中")     # 你以为会了，其实不会

    def test_weak_concept_from_not_started_is_not_promoted(self):
        self._seed()
        kb.record_interview("j1", weak=["上下文窗口"])
        cards = {n["term"]: n for n in store.load_knowledge()["nodes"]}
        self.assertEqual(cards["上下文窗口"]["state"], "待学习")

    def test_unknown_terms_are_ignored_not_created(self):
        self._seed()
        before = len(store.load_knowledge()["nodes"])
        kb.record_interview("j1", asked=["从没见过的词"], weak=["从没见过的词"])
        self.assertEqual(len(store.load_knowledge()["nodes"]), before)   # 不自动建卡

    def test_asked_count_raises_review_priority(self):
        kb.absorb([_node(term="甲概念"), _node(term="乙概念")], {"id": "j1"})
        d = store.load_knowledge()
        for n in d["nodes"]:
            if n["term"] == "乙概念":
                n["asked_count"] = 3
        store.save_knowledge(d)
        q = [n["term"] for n in kb.review_queue(store.load_knowledge())]
        self.assertEqual(q[0], "乙概念")

    def test_reason_mentions_interview(self):
        kb.absorb([_node(term="甲概念")], {"id": "j1"})
        d = store.load_knowledge(); d["nodes"][0]["asked_count"] = 2
        store.save_knowledge(d)
        self.assertIn("面试", kb.review_queue(store.load_knowledge())[0]["reason"])

    def test_asked_count_beats_the_alphabetical_tie_break(self):
        """「被问过」因子必须**真的**参与打分，不能靠术语升序蒙对答案。

        上面 test_asked_count_raises_review_priority 里被提到的是「乙概念」，而 tie-break 是术语
        升序 + 「乙」(U+4E59) 恰好排在「甲」(U+7532) 前面 —— 把 (1 + asked) 因子整个删掉，
        那张卡**依然排第一**，用例照样绿（实测假绿）。这里把被问过的换成字母序靠后的「甲概念」做对照：
        两张卡状态/岗位数/复习时间完全相同，只有因子能让它翻到前面。
        """
        kb.absorb([_node(term="甲概念"), _node(term="乙概念")], {"id": "j1"})
        d = store.load_knowledge()
        for n in d["nodes"]:
            if n["term"] == "甲概念":
                n["asked_count"] = 3
        store.save_knowledge(d)
        q = [n["term"] for n in kb.review_queue(store.load_knowledge())]
        self.assertEqual(q[0], "甲概念")
        # 反证：按术语升序（同分时的 tie-break）「乙概念」本排在前面，只有因子能把它压下去
        self.assertLess("乙概念", "甲概念")


def _assert_really_non_utf8(case, raw, label=""):
    """夹具自证：这份字节**必须真的不是 UTF-8**。

    ⚠️ 这条自证是必需品，不是洁癖：ASCII 内容的 GBK 与 UTF-8 逐字节相同 ——
    夹具里不带中文时，`encode("gbk")` 写出来的其实是一份**合法 UTF-8** 文件，
    于是"坏库"根本不存在，整条坏库用例会在**什么都没测到**的情况下假绿
    （2026-10-15 主线程正是栽在这上面，报了一次假警报）。
    所以凡是要造"读不出来"的文件，先过这一关。
    """
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return
    case.fail("夹具没坏（这些字节能按 UTF-8 解码）：%s；GBK 夹具里必须含中文。字节=%r"
              % (label, raw[:200]))


class KbAbsorbNonUtf8Test(IsolatedCase):
    """GBK 写坏的库 + 一次 absorb → **用户的卡必须还在**（在被备份走的那份里）。

    这是本任务的核心回归：老实现下用户的整个库会被内存里的空库顶掉
    （实测：卡全没了，而且连备份都没有）。
    """

    def _cards(self):
        return [{"id": "语音识别", "term": "语音识别", "definition": "把语音转文字",
                 "plain_explanation": "通俗", "layer": 0,
                 "sources": [{"url": "https://w"}], "timeline": [], "related": [],
                 "interview_questions": [], "confidence": "verified",
                 "state": "待学习", "from_jds": []}]

    def test_absorb_quarantines_a_non_utf8_library_instead_of_wiping_it(self):
        path = store.knowledge_path()
        raw = json.dumps({"nodes": self._cards()}, ensure_ascii=False).encode("gbk")
        _assert_really_non_utf8(self, raw, "KbAbsorbNonUtf8Test")
        with open(path, "wb") as f:
            f.write(raw)
        node = {"id": "新概念", "term": "新概念", "definition": "d", "plain_explanation": "p",
                "layer": 0, "sources": [{"url": "u"}], "timeline": [], "related": [],
                "interview_questions": [], "confidence": "verified", "state": "待学习"}
        kb.absorb([node], {"id": "新岗位", "job_title": "新岗位", "company": "",
                           "date": "2026-10-15"})

        # ① 用户的字节必须被**完整**备份下来（能原样恢复他原来的卡）
        folder = os.path.dirname(path)
        backups = [f for f in os.listdir(folder) if ".corrupt-" in f]
        self.assertEqual(len(backups), 1, "坏库必须先被改名备份，绝不能直接覆盖")
        with open(os.path.join(folder, backups[0]), "rb") as f:
            self.assertEqual(f.read(), raw, "备份必须逐字节等于原文")
        with open(os.path.join(folder, backups[0]), encoding="gbk") as f:
            self.assertIn("语音识别", f.read(), "用户的卡必须能从这个备份里恢复")
        # ② 本次照样沉淀成功（库坏了也不能不干活）
        cards = store.load_knowledge()["nodes"]
        self.assertIn("新概念", [n.get("term") for n in cards])
        self.assertEqual(len(cards), 1, "新库只该有这次沉淀的那张卡（用户那张在原文件里）")


def _raw_card(term, cid=None, **kw):
    """一张**原始形状**的卡片（照用户手改 knowledge.json 的样子写盘）。

    刻意不走 `kb.absorb`：本类要造的是「盘上文件读不出来」这种形态，必须由测试自己写字节。
    `cid` 与 `term` 故意可以不一致 —— 那正是 P0-7 的形态（id ≠ normalize_id(term)）。
    """
    c = {"id": cid if cid is not None else term, "term": term,
         "definition": "库里的定义", "plain_explanation": "库里的通俗", "category": "核心概念",
         "layer": 0, "sources": [], "timeline": [], "related": [], "interview_questions": [],
         "confidence": "ai-generated", "from_jds": [],
         "state": "待学习", "last_outcome": "未面试", "asked_count": 0,
         "review_count": 0, "last_reviewed_at": "", "last_result": ""}
    c.update(kw)
    return c


def _state_of(card_id):
    """从盘上读某张卡的 state（断言用；卡不存在就返回 None）。"""
    for c in store.load_knowledge()["nodes"]:
        if c.get("id") == card_id:
            return c.get("state")
    return None


def _first_diff(a, b):
    """两段字节**第一个**不同的下标（全同返回 None）。只为把诊断串写具体。"""
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return i
    return None if len(a) == len(b) else min(len(a), len(b))


class KbQuarantineBackupByteForByteTest(IsolatedCase):
    """P0-1 需求三：`absorb` 走 (b)「留档后写」时，`.corrupt-*` 必须是**跑之前那份文件的逐字节副本**。

    为什么单独立一条（而不是靠"备份存在 + 活库能读 + 卡数对"就够）：留档是用户那 145 张卡
    **唯一**的存身之处 —— 这次写入已经把活库换成了一份新库。留档只要不是逐字节副本，
    哪怕只是"按文本读进来再写出去"这种看起来最无害的往返（解码失败被兜成空串 / 换行被规范化 /
    凭空多个 BOM），用户的卡就**永久**没了，而上面那些断言**全都还是绿的**。
    所以这条钉子必须直接比字节。
    """

    def _cards(self):
        return [_raw_card("语音识别"), _raw_card("语音辨识", cid="yuyinshibian"),
                _raw_card("Transformer 架构", category="模型结构")]

    def test_absorb_backup_is_a_byte_for_byte_copy_of_the_file_before_the_call(self):
        path = store.knowledge_path()
        raw = json.dumps({"version": 1, "nodes": self._cards()},
                         ensure_ascii=False).encode("gbk")
        _assert_really_non_utf8(self, raw, "KbQuarantineBackupByteForByteTest")
        with open(path, "wb") as f:
            f.write(raw)
        before = open(path, "rb").read()      # 「跑之前那份文件内容」——当场重读，不是复用上面那个变量
        self.assertEqual(before, raw, "夹具前提：盘上就是这份 GBK 字节")

        kb.absorb([_raw_card("新概念")], {"id": "j1", "job_title": "新岗位"})

        folder = os.path.dirname(path)
        backups = [f for f in os.listdir(folder) if ".corrupt-" in f]
        self.assertEqual(len(backups), 1, "这次 absorb 必须正好留档一份：%r" % (backups,))
        after = open(os.path.join(folder, backups[0]), "rb").read()
        self.assertEqual(after, before,
                         "留档不是逐字节副本 —— 用户的卡就永久没了：before=%d after=%d 首个不同位置=%s"
                         % (len(before), len(after), _first_diff(before, after)))
        # 反向：这条用例必须真的走在 (b)「留档后写」上（不然"备份恰好等于原文"是白捡的）
        self.assertTrue(store.knowledge_safe_to_write()[0], "新库必须读得出来")
        self.assertIn("新概念", [c.get("term") for c in store.load_knowledge()["nodes"]],
                      "本次沉淀必须照旧生效（(b) 是'留档后写'，不是'拒绝'）")


class KbWriteEntryPointsCorruptLibraryTest(IsolatedCase):
    """P0-1 的**五个写入口**统一契约：库读不出来时要么**逐字节不动**（拒绝），要么**留档后写**。

    背景（报告 §2 P0-1，主线程实测）：`record_interview` 会把整库换成空库**且不留备份**
    （隔离根里把 knowledge.json 写成 GBK → 300 字节变 36 字节、卡片 0、`.corrupt-*` 0 个）。
    其余四个写入口当时"数据不丢"靠的是**副作用**（读到空库 → 没找到目标 → 不保存），
    不是显式契约 —— 那条路随时会被一次"顺手加个 save"改掉，所以这里把它钉成契约。

    两个形态（每个入口必须落在其中一个上，且必须**写死在表里**）：

      (a) **拒绝、连留档都不做**：库读不出来就放弃这次写入，盘上的坏文件**逐字节不动**
          （留给用户自己修）。选它的理由见 `REFUSING_ENTRIES` 的注释（写的**就是库里那张
          已有的卡**，而"这张卡在不在"在坏库下整个失效）。
      (b) **留档后写**：`absorb`（沉淀用户这次分析出的知识点）与 `upsert_check`（用户刚点的勾）
          —— 对这俩来说"放弃写入"等于让用户刚做完的操作静默丢失。所以先
          `_quarantine_corrupt_kb()` 把坏文件逐字节改名留档，再往新库写
          （副本逐字节性由 `KbQuarantineBackupByteForByteTest` 与下面 (b) 分支各自钉住）。

    ⚠️ `record_interview` 原来是 (b)，本轮改成 **(a)**（P0-1 的后续）：它虽然"无条件 save"，
    但写的**就是库里那些已有的卡**（asked_count / last_outcome / 降级）—— 坏库下
    `_record_interview_in` 一张卡都命中不到、却照样 save 一份空库。结果是**这次反哺一个字都没记下**，
    而用户 145 张卡从活库消失（只剩 `.corrupt-*`）。留档不丢字节，但用户打开界面看到"知识库 0 张卡"，
    还得自己去找那个备份文件；拒绝则让用户修好库之后**重试这次反哺**
    （`interview._apply_feedback` 只在真写进库之后才落 feedback 标记，专门为这一支留的）。
    """

    #: (说明, 调用)
    #: ⚠️ 这五个入口的形态**不是**"统一策略"，而是按各自语义分两类，且**逐条写死在
    #: `REFUSING_ENTRIES` 里**（改了哪一条、这条用例就在哪一条上红）：
    #:   * (a) 拒绝：写的**就是库里那张已有的卡**（`set_state` / `record_review` / `merge_nodes`）
    #:     或**读不出库就只有空库可写**（`record_interview`）。"目标卡在不在"这个判据在坏库下
    #:     整个失效（`load_knowledge()` 给的是空库），所以一律不写、且**连隔离都不做** ——
    #:     盘上那个坏文件**一个字节都不动**，用户还能自己用记事本把它另存为 UTF-8 救回来。
    #:   * (b) 留档后写：`absorb` / `upsert_check` 写的是**用户这次的操作**，放弃等于静默丢失；
    #:     所以先把坏文件逐字节留档，再往新库写。
    #: 两类的反面（读得出来时照旧干活）由
    #: `test_a_corrupt_library_is_quarantined_and_a_readable_one_still_writes` 覆盖。
    ENTRIES = (
        ("set_state", lambda: kb.set_state("语音识别", "已掌握")),
        ("record_review", lambda: kb.record_review("语音识别", kb.RESULT_UP)),
        ("record_interview",
         lambda: kb.record_interview("j1", asked=["语音识别"], weak=["语音识别"])),
        ("upsert_check", lambda: kb.upsert_check("语音识别", "已掌握")),
        ("merge_nodes", lambda: kb.merge_nodes("语音识别", "语音辨识")),
    )

    #: 每个入口的 fixture：`(term, id)` 列表。**id 故意与归一化 term 不同**（P0-7 那种手改库）
    #: —— 顺手证明"按归一化 term 找卡"与"按 id 找卡"两把钥匙在坏库路径上也没丢。
    FIXTURES = {
        "set_state": [("语音识别", "yuyinshibie")],
        "record_review": [("语音识别", "yuyinshibie")],
        "record_interview": [("语音识别", "yuyinshibie")],
        "upsert_check": [("语音识别", "yuyinshibie")],
        "merge_nodes": [("语音识别", "yuyinshibie"), ("语音辨识", "yuyinshibian")],
    }

    def _write_gbk_kb(self, cards):
        raw = json.dumps({"version": 1, "nodes": cards}, ensure_ascii=False).encode("gbk")
        _assert_really_non_utf8(self, raw, "KbWriteEntryPointsCorruptLibraryTest 夹具")
        with open(store.knowledge_path(), "wb") as f:
            f.write(raw)
        return raw

    def _fixture_cards(self, name):
        return [_raw_card(term, cid=cid) for term, cid in self.FIXTURES[name]]

    def _backups(self):
        folder = os.path.dirname(store.knowledge_path())
        return sorted(f for f in os.listdir(folder) if ".corrupt-" in f)

    def _fresh_root(self):
        """把 store.DATA_ROOT 指到一个**全新**的临时根（返回该根）。

        每个入口都要新根：上一个入口留下的 `.corrupt-*` 会污染下一个的备份计数。
        用 mock.patch（不是手工赋值）—— 断言失败提前抛错时还能靠它还原，
        否则后面的用例会接着往这个临时根里写，"隔离"就名存实亡了。
        """
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        patcher = mock.patch.object(store, "DATA_ROOT", tmp)
        patcher.start()
        self.addCleanup(patcher.stop)
        return tmp

    def _kv(self, text):
        return dict(part.split("=", 1) for part in text.split() if "=" in part)

    def _assert_no_backup_claim(self, msg, kv):
        """(a) 的口子**不许声称"已留档"**：这条路上 `_quarantine_corrupt_kb()` 压根没被调用，
        盘上没有任何 `.corrupt-*` —— 用户照着"坏文件已按 .corrupt-* 留档"去找备份只会扑空，
        而那正是本次要修的"说的和做的不一样"。（如实说"本次没有留档"是可以的、也是有用的。）
        """
        self.assertNotIn(".corrupt-", str(msg), "(a) 拒绝时不该声称已按 .corrupt-* 留档 " + kv)

    #: (a) **拒绝写入、连留档都不做**的四个入口（`_with_kb_write(..., write_when_unreadable=False)`）。
    #: 前三个写的**就是库里那张已有的卡**（"这张卡在不在"在坏库下整个失效 → 判不了就不写）；
    #: `record_interview` 虽然"无条件 save"，但坏库下它一张卡都命中不到、save 下去的只有一份空库
    #: （用户 145 张卡从活库消失、这次反哺一个字都没记下）⇒ 同样归入"拒绝、文件逐字节不动"。
    #: 另外两个（`absorb` / `upsert_check`）写的是**用户这次的操作**，坏文件留档后照常写
    #: （见 `kb._with_kb_write`），所以不在这张表里。
    REFUSING_ENTRIES = ("set_state", "record_review", "record_interview", "merge_nodes")

    def test_every_writer_refuses_to_wipe_a_corrupt_library(self):
        """P0-1 的核心回归：库读不出来时，**五个写入口都不许把用户的 GBK 字节换成空库**。

        实测（报告 §2 P0-1）：`record_interview` 把 GBK 写坏的库换成 36 字节空库、`.corrupt-*`
        一个都没有。另外四个当时"数据不丢"靠的是**副作用**（读到空库 → 找不到目标卡 → 恰好
        没走到 save），不是契约 —— 这里把它钉成契约。

        每个入口选 (a) 还是 (b) 及其理由（**同一入口的行为写死在表里**）：
          * (a) **拒绝、连留档都不做**（`REFUSING_ENTRIES`）：`set_state` / `record_review` /
            `merge_nodes` 写的**就是库里那张已有的卡**，而库读不出来时"有没有这张卡"整个
            无从判断 —— 一次点空了的操作不该让用户的文件消失（哪怕留下了备份）；
            `record_interview` 虽然"无条件 save"，但坏库下它一张卡都命中不到、save 下去的
            只是一份空库（145 张卡从活库消失，而这次反哺一个字都没记下）⇒ 同样归入拒绝。
            所以一个字节都不动（连隔离都不做），并如实报"库读不出来"（`kb._KB_WRITE_REFUSED`）。
          * (b) **留档后写**：`absorb`（沉淀这次分析出的知识点）、`upsert_check`（用户刚点的勾）。
            这两个入口的目标不是"库里已有的某张卡"，而是"用户刚做的这次操作"；放弃写入
            等于让用户的操作静默丢失。所以先 `_quarantine_corrupt_kb()` 把坏文件**逐字节**留档，
            再往新库写（用户原来的字节一个不少地躺在 `.corrupt-*` 里）。

        ⚠️ fixture 那张卡的 `id` 故意 ≠ 归一化 `term`（`yuyinshibie` / 语音识别，P0-7 那种手改库），
        顺手证明两把钥匙（按 id、按归一化 term）在坏库路径上也没丢。
        """
        for name, call in self.ENTRIES:
            with self.subTest(entry=name):
                tmp = self._fresh_root()
                store.ensure_profile()
                self.assertIn(tmp, store.knowledge_path(), "探针必须落在临时根里")
                raw = self._write_gbk_kb(self._fixture_cards(name))
                before = open(store.knowledge_path(), "rb").read()
                self.assertEqual(before, raw, "夹具前提：盘上就是这份 GBK 字节")

                result = call()

                # 诊断串带 kv：assertTrue 的 msg 会被 unittest 转义成 %-编码，
                # 直接把判据塞进 msg 里（照 test_jdid 的既有做法）
                after = open(store.knowledge_path(), "rb").read()
                kv = "entry=%s before=%d after=%d backups=%d result=%r" % (
                    name, len(before), len(after), len(self._backups()), result)
                if name in self.REFUSING_ENTRIES:
                    # (a) 拒绝：坏文件逐字节不动，连备份都不做
                    self.assertEqual(after, before, "坏库被动过 " + kv)
                    self.assertEqual(self._backups(), [], "拒绝写入的调用不该隔离用户文件 " + kv)
                    if name == "set_state":
                        self.assertFalse(result[0], "读不出库却报告成功 " + kv)
                        self.assertIn("读", result[1], "没如实说清原因 " + kv)
                        self._assert_no_backup_claim(result[1], kv)
                    elif name == "record_review":
                        self.assertEqual(result, (False, None), "外形不对 " + kv)
                    elif name == "record_interview":
                        # `(0, 0, 0)` 是**合法结果**（这次面试一个知识点都没提到），调用方
                        # 没法拿它区分"写成了"与"压根没写" ⇒ 没写时必须是 None（interview.py 靠它
                        # 决定不落 feedback 标记、好让用户修好库之后能重试这次反哺）
                        self.assertIsNone(result, "必须返回那个'没写'信号 None " + kv)
                    else:                               # merge_nodes
                        self.assertFalse(result[0], "读不出库却报告成功 " + kv)
                        self.assertIn("读", result[1], "没如实说清原因 " + kv)
                        self._assert_no_backup_claim(result[1], kv)
                else:
                    # (b) 留档后写：坏文件被整体改名，备份逐字节等于原文
                    backups = self._backups()
                    self.assertEqual(len(backups), 1, "坏库没被留档 " + kv)
                    backup = os.path.join(os.path.dirname(store.knowledge_path()), backups[0])
                    self.assertEqual(open(backup, "rb").read(), before,
                                     "备份不是逐字节原文 " + kv)
                    # 新库必须读得出来（不然"留档"就只是把用户的库改名走）
                    self.assertTrue(store.knowledge_safe_to_write()[0], "新库读不出来 " + kv)
                    # 只剩 `upsert_check` 还留在这条 (b) 支上
                    self.assertEqual(result[0], True, kv)
                    self.assertEqual(result[1], True, "应当新建那张卡 " + kv)
                    self.assertEqual([c.get("id") for c in store.load_knowledge()["nodes"]],
                                     ["语音识别"], kv)
                # 收尾：把临时根还回去，免得下一个入口/下一条用例接着往这里写
                mock.patch.stopall()

    def test_a_failed_quarantine_refuses_all_five_writes_and_keeps_the_file(self):
        """**哨兵自己失败**时（改名失败：磁盘满 / 文件被占用）：五个入口一个字节都不许写。

        这是 `_with_kb_write` 里 `if not _quarantine_corrupt_kb(): return False, _KB_BACKUP_FAILED`
        那条路。没有这条用例的话，"哨兵返回 False 时照样继续写"这个变异可以让其它用例全绿 ——
        那正是 P0-1 事故本身（坏文件留不住档时，内存里的空库照样写下去）。

        ⚠️ 四个 (a) 入口（`REFUSING_ENTRIES`）在**哨兵之前**就返回了，所以对它们来说这个
        mock 其实是空转 —— 但它们仍然必须给出可分辨的"没写"信号，这一半断言照旧有效
        （顺带证明：哨兵挂了也不会让 (a) 入口改口说"写成了"）。真正走到这里的只有
        (b) 的 `upsert_check`。
        """
        for name, call in self.ENTRIES:
            with self.subTest(entry=name):
                tmp = self._fresh_root()
                store.ensure_profile()
                self.assertIn(tmp, store.knowledge_path(), "探针必须落在临时根里")
                path = store.knowledge_path()
                raw = self._write_gbk_kb(self._fixture_cards(name))
                before = open(path, "rb").read()
                with mock.patch.object(kb, "_quarantine_corrupt_kb", return_value=False):
                    result = call()
                kv = "entry=%s before=%d after=%d result=%r" % (
                    name, len(before), os.path.getsize(path), result)
                # ① 坏文件必须原样留着（既不改名、也不被空库顶掉）
                self.assertEqual(open(path, "rb").read(), before, "哨兵失败却动了盘 " + kv)
                self.assertEqual(self._backups(), [], "哨兵失败了还留下了备份 " + kv)
                # ② 五个入口都必须让调用方看得出"没写"，并且不许声称"已留档"（这次确实没有）
                if name == "set_state":
                    self.assertFalse(result[0], "哨兵失败却报告成功 " + kv)
                    self.assertIn("读", result[1], "没如实说清原因是'库读不出来' " + kv)
                    self._assert_no_backup_claim(result[1], kv)
                elif name == "record_review":
                    self.assertEqual(result, (False, None), "外形不对 " + kv)
                elif name == "record_interview":
                    self.assertIsNone(result, "必须给一个可分辨的失败信号 " + kv)
                elif name == "upsert_check":
                    self.assertEqual(result, (False, False, None), "外形不对 " + kv)
                else:                                   # merge_nodes
                    self.assertFalse(result[0], "哨兵失败却报告成功 " + kv)
                    self.assertIn("读", result[1], "没如实说清原因是'库读不出来' " + kv)
                    self._assert_no_backup_claim(result[1], kv)
                mock.patch.stopall()

    def test_a_corrupt_library_is_quarantined_and_a_readable_one_still_writes(self):
        """反向：库读得出来时，五个入口照旧干活（守卫不能把正常写入一起挡掉）。

        没有这条用例，"干脆永不写盘"那种"修法"也能让上面那些坏库用例全绿。
        """
        calls = (
            ("set_state", lambda: kb.set_state("语音识别", "学习中")),
            ("record_review", lambda: kb.record_review("语音识别", kb.RESULT_UP)),
            ("record_interview", lambda: kb.record_interview("j1", asked=["语音识别"])),
            ("upsert_check", lambda: kb.upsert_check("新概念", "已掌握")),
            ("merge_nodes", lambda: kb.merge_nodes("语音识别", "语音辨识")),
        )
        for name, call in calls:
            with self.subTest(entry=name):
                tmp = self._fresh_root()
                store.ensure_profile()
                self.assertIn(tmp, store.knowledge_path(), "探针必须落在临时根里")
                # 夹具走**真的** kb.absorb：形状与线上完全一致
                kb.absorb([_raw_card("语音识别"), _raw_card("语音辨识")], {"id": "j0"})
                self.assertEqual(sorted(c["id"] for c in store.load_knowledge()["nodes"]),
                                 ["语音识别", "语音辨识"], "夹具前提：两张卡")
                result = call()
                kv = "entry=%s result=%r" % (name, result)
                self.assertEqual(self._backups(), [], "库是好的，不该有任何隔离备份 " + kv)
                if name == "set_state":
                    self.assertTrue(result[0], kv)
                    self.assertEqual(_state_of("语音识别"), "学习中", kv)
                elif name == "record_review":
                    self.assertTrue(result[0], kv)
                    self.assertEqual(_state_of("语音识别"), "已掌握", kv)
                elif name == "record_interview":
                    self.assertEqual(result, (1, 0, 0), kv)
                elif name == "upsert_check":
                    self.assertEqual((result[0], result[1]), (True, True), kv)
                    self.assertEqual(_state_of("新概念"), "已掌握", kv)
                else:                                   # merge_nodes
                    self.assertTrue(result[0], kv)
                    ids = sorted(c["id"] for c in store.load_knowledge()["nodes"])
                    self.assertEqual(ids, ["语音识别"], "drop 卡没被并走 " + kv)
                mock.patch.stopall()

    def test_a_readable_library_still_writes_for_all_five(self):
        """反向确认：守卫不能把正常写入一起挡掉（读得出来时五个入口照旧干活）。

        同时钉住"库可读时行为一字不变"这条最容易被顺手改坏的断言 —— 少了它，
        任何"干脆永不写盘"的变异都能让上面那些坏库用例全绿。
        """
        store.ensure_profile()
        kb.absorb([_raw_card("语音识别")], {"id": "j0"})
        kb.absorb([_raw_card("语音辨识")], {"id": "j0"})
        self.assertTrue(kb.set_state("语音识别", "学习中")[0])
        ok, node = kb.record_review("语音识别", kb.RESULT_UP)
        self.assertTrue(ok)
        self.assertEqual(node["state"], "已掌握")
        self.assertEqual(kb.record_interview("j1", asked=["语音识别"]), (1, 0, 0))
        ok, created, card = kb.upsert_check("新概念", "已掌握")
        self.assertEqual((ok, created), (True, True))
        self.assertEqual(card["id"], "新概念")
        ok, msg = kb.merge_nodes("语音识别", "语音辨识")
        self.assertTrue(ok, msg)
        cards = store.load_knowledge()["nodes"]
        self.assertEqual(sorted(c["id"] for c in cards), ["新概念", "语音识别"])
        self.assertEqual(self._backups(), [], "库是好的，不该有任何隔离备份")


class KbAbsorbFindsCardsByIdAndNormalizedTermTest(IsolatedCase):
    """P0-7：`kb.absorb` 与 `kbreuse` 必须用**同一把尺子**找卡。

    实测（报告 §2 P0-7）：库里 `{"id": "yuyinshibie", "term": "ASR"}`（手改 / 别的写入者
    造成 id ≠ 归一化 term）→ `kbreuse.Index.lookup("ASR")` 命中、身份对齐也做了，
    但 `kb.absorb` 仍 `added=1` → 库变成 `['yuyinshibie', 'asr']` 两张同概念卡。
    根因：`kb.absorb` 按 `card["id"]` 建索引，`kbreuse` 按 `normalize_id(term)` 建索引。
    """

    def _mismatched(self):
        return _raw_card("ASR", cid="yuyinshibie", state="已掌握",
                         definition="自动语音识别", from_jds=[{"id": "老岗位", "job_title": "老岗"}])

    def test_absorb_merges_into_a_card_whose_id_differs_from_its_term(self):
        store.ensure_profile()
        store.save_knowledge({"version": 1, "nodes": [self._mismatched()]})
        added, merged = kb.absorb([_raw_card("ASR", definition="本次的定义")], {"id": "新岗位"})
        # 不修的话这里是 (1, 0) 且卡片数 1 → 2
        self.assertEqual((added, merged), (0, 1), "按归一化 term 建索引才能认出这张卡")
        cards = store.load_knowledge()["nodes"]
        self.assertEqual(len(cards), 1, "不许新建同概念的第二张卡")
        self.assertEqual(cards[0]["id"], "yuyinshibie", "还是原来那张卡")

    def test_the_mismatched_card_is_still_found_by_its_own_id(self):
        """反向：按 id 找卡这条路**不许**被"改成只按归一化 term"的修法弄丢。"""
        store.ensure_profile()
        store.save_knowledge({"version": 1, "nodes": [self._mismatched()]})
        added, merged = kb.absorb([_raw_card("yuyinshibie", definition="本次的定义")],
                                  {"id": "新岗位"})
        self.assertEqual((added, merged), (0, 1), "id 也该是一把能找到这张卡的钥匙")
        self.assertEqual(len(store.load_knowledge()["nodes"]), 1)

    def test_the_card_id_still_wins_when_both_keys_point_somewhere(self):
        """id 优先（setdefault）：库里 id 与 term 指向两张卡时，按 id 找到的必须是第一张。"""
        store.ensure_profile()
        store.save_knowledge({"version": 1, "nodes": [
            _raw_card("语音识别", cid="kuayuyinshibie"),
            _raw_card("kuayuyinshibie", cid="yuyinshibie"),
        ]})
        added, merged = kb.absorb([_raw_card("kuayuyinshibie")], {"id": "新岗位"})
        self.assertEqual((added, merged), (0, 1))
        cards = store.load_knowledge()["nodes"]
        self.assertEqual(len(cards), 2, "合并只该并进一张，另一张原样留着")


class KbSetStateAndReviewUseOneRulerTest(IsolatedCase):
    """P0-8：`set_state` / `record_review` 的**守卫**与**主体**必须是同一把尺子。

    实测（批 1 独立复查）：库里有 `{"id": "yuyinshibie", "term": "ASR"}`（手改 / 别的写入者
    造成 id ≠ 归一化 term）时
      * 守卫 `refuse_when` 用 `_card_index`（id + 归一化 term 双键）→ 判定"卡在"
      * 主体 `_set_state_in` / `_record_review_in` 只用 `n.get("id")` → 判定"卡不在"
    ⇒ `kb.set_state("ASR", "已掌握")` 回 `(False, "找不到该知识点")`：**卡就在库里、文案却是假的**，
    而且用户点的掌握度**静默没生效**（同形态下 `kb.absorb` 早已能命中，P0-7）。

    这些用例只钉"守卫与主体同尺"这一件事：命中之后改的仍必须是**原来那张卡**，
    **不建新卡**（卡片数不变），真的不存在时仍如实说找不到。
    """

    def _mismatched(self, **kw):
        return _raw_card("ASR", cid="yuyinshibie", **kw)

    def _seed(self, cards):
        store.ensure_profile()
        # 探针必须落在 IsolatedCase 给的临时根里（绝不碰真实 data/，跑前跑后基线一致）
        self.assertIn(self.tmp, store.knowledge_path(), "探针必须落在临时根里")
        store.save_knowledge({"version": 1, "nodes": cards})
        return cards

    def test_set_state_finds_a_card_whose_id_differs_from_its_term(self):
        self._seed([self._mismatched()])
        ok, msg = kb.set_state("ASR", "已掌握")
        # 不修的话这里是 (False, "找不到该知识点")，而那张卡就在库里
        self.assertTrue(ok, "卡就在库里，不该报失败：%r" % (msg,))
        self.assertNotIn("找不到该知识点", msg, "这是假话（卡就在库里）：%r" % (msg,))
        cards = store.load_knowledge()["nodes"]
        self.assertEqual(len(cards), 1, "这条入口不建卡：卡片数必须不变")
        self.assertEqual(cards[0]["id"], "yuyinshibie", "改的必须还是原来那张卡")
        self.assertEqual(cards[0]["term"], "ASR", "不许顺手改术语")
        self.assertEqual(_state_of("yuyinshibie"), "已掌握", "用户的掌握度必须真的落盘")

    def test_record_review_finds_a_card_whose_id_differs_from_its_term(self):
        self._seed([self._mismatched()])
        ok, node = kb.record_review("ASR", kb.RESULT_UP)
        # 不修的话这里是 (False, None)：这次自测被静默吞掉
        self.assertTrue(ok, "卡就在库里，不该报失败")
        self.assertIsNotNone(node, "命中了就该回传那张卡")
        self.assertEqual(node["id"], "yuyinshibie", "回传的必须是库里那张卡")
        cards = store.load_knowledge()["nodes"]
        self.assertEqual(len(cards), 1, "这条入口不建卡：卡片数必须不变")
        after = cards[0]
        self.assertEqual(after["state"], "已掌握")
        self.assertEqual(after["review_count"], 1, "这次自测必须真的记下来（不许静默吞掉）")
        self.assertEqual(after["last_reviewed_at"], kb._today())
        self.assertEqual(after["last_result"], kb.RESULT_UP)

    def test_the_mismatched_card_is_still_found_by_its_own_id(self):
        """反向：按 id 找卡这条路**不许**被"改成只按归一化 term"的修法弄丢。"""
        self._seed([self._mismatched()])
        self.assertTrue(kb.set_state("yuyinshibie", "已掌握")[0])
        self.assertEqual(_state_of("yuyinshibie"), "已掌握")
        self.assertTrue(kb.record_review("yuyinshibie", kb.RESULT_UP)[0])
        self.assertEqual(_state_of("yuyinshibie"), "已掌握")
        self.assertEqual(len(store.load_knowledge()["nodes"]), 1)

    def test_a_plain_card_behaves_exactly_as_before(self):
        """对照组：id 恰好等于归一化 term 的普通卡，行为一字不变。"""
        self._seed([_raw_card("语音识别")])
        self.assertTrue(kb.set_state("语音识别", "学习中")[0])
        self.assertEqual(_state_of("语音识别"), "学习中")
        self.assertTrue(kb.record_review("语音识别", kb.RESULT_UP)[0])
        self.assertEqual(_state_of("语音识别"), "已掌握")
        self.assertEqual(len(store.load_knowledge()["nodes"]), 1)

    def test_a_really_missing_term_is_still_reported_as_missing(self):
        """对照组：真的不存在 → 仍如实说"找不到该知识点"，且不许建卡。"""
        self._seed([self._mismatched()])
        ok, msg = kb.set_state("压根没有的概念", "已掌握")
        self.assertFalse(ok, "不存在的概念不该报成功")
        self.assertIn("找不到该知识点", msg, "真的不存在时才说这句：%r" % (msg,))
        self.assertFalse(kb.record_review("压根没有的概念", kb.RESULT_UP)[0])
        self.assertEqual(len(store.load_knowledge()["nodes"]), 1, "找不到时不许建卡")


class KbSetStateCorruptLibraryHonestyTest(IsolatedCase):
    """需求 4：库读不出来时，`set_state` 必须说"库读不出来"，**不许**说成"找不到该知识点"。

    后者是假话：盘上那份 GBK 字节里那张卡明明在（只是整个库读不出来）——
    用户照着"找不到该知识点"会以为卡丢了 / 术语打错了，而真正该做的是先去修库。
    （同款口径的既有断言见 `KbWriteEntryPointsCorruptLibraryTest._assert_no_backup_claim` 家族。）
    """

    def _on_disk(self):
        """盘上那份字节；文件已经不在了就返回 None（用 with 读，不留下未关闭的句柄）。"""
        path = store.knowledge_path()
        if not os.path.exists(path):
            return None
        with open(path, "rb") as f:
            return f.read()

    def _write_gbk_kb(self):
        store.ensure_profile()
        raw = json.dumps({"version": 1, "nodes": [_raw_card("语音识别", cid="yuyinshibie")]},
                         ensure_ascii=False).encode("gbk")
        _assert_really_non_utf8(self, raw, "KbSetStateCorruptLibraryHonestyTest 夹具")
        self.assertIn(self.tmp, store.knowledge_path(), "探针必须落在临时根里")
        with open(store.knowledge_path(), "wb") as f:
            f.write(raw)
        return raw

    def test_set_state_says_the_library_is_unreadable_not_the_card_missing(self):
        raw = self._write_gbk_kb()
        self.assertEqual(self._on_disk(), raw, "夹具前提：盘上就是这份 GBK 字节")
        ok, msg = kb.set_state("语音识别", "已掌握")
        # 诊断串自己不许抛异常（否则真正的判据被一句 FileNotFoundError 顶掉、红在不该红的行上）
        after = self._on_disk()
        kv = "before=%d after=%s msg=%r" % (len(raw), "文件不在了" if after is None else len(after), msg)
        self.assertFalse(ok, "读不出库却报告成功 " + kv)
        self.assertNotIn("找不到该知识点", msg,
                         "库读不出来时说这句是假话（卡就在盘上那份字节里）" + kv)
        self.assertIn("读", msg, "没如实说清原因是'库读不出来' " + kv)
        self.assertIsNotNone(after, "坏库被弄没了（连隔离备份都不是）" + kv)
        self.assertEqual(after, raw, "坏库被动过 " + kv)


# ======================================================================
# 批 2 · 2.1：`_KB_LOCK` 跨模块缺口（体检 §5 P3-3）
#
# 三个调用方（`jddelete` / `jdid` / `jobdup`）原先**各自**在 kb 之外做
# 「load_knowledge → 改 from_jds → save_knowledge」，并各自解析 `kb._KB_LOCK`
# （= 三份重复的 `_repoint_kb`）。现在这段读-改-写收进 kb 的三个窄接口，
# 保护它的锁与它同处一个模块（都以 `_with_kb_write` 为准）。
#
# 这一段钉三件事：
#   ① 三个接口的对外行为（含坏库时"拒绝写入、一个字节都不动"）；
#   ② **并发不丢更新** —— 分析（absorb）与"删岗位 / 改 ID"同时发生，一条沉淀都不许丢；
#   ③ **不出现互相等待** —— 新接口只许取内层锁 `_KB_LOCK`，绝不回头去取
#      `_JD_LOCK` / `_MERGE_LOCK`（锁序铁律：外层 → `_KB_LOCK` → 文件写）。
# ======================================================================


def _ref_card(cid, term, refs):
    """一张带 `from_jds` 的卡：三个接口只改这个字段，**卡片本身必须留着**。"""
    return {"id": cid, "term": term, "from_jds": [dict(r) for r in refs]}


def _ref_ids(card):
    """一张卡引用的岗位 id（原样，不去空白 —— 正是为了看清接口动了哪一条）。"""
    return [x.get("id") for x in (card.get("from_jds") or [])]


def _read_raw(path):
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return f.read()


def _corrupt_backups(path):
    folder = os.path.dirname(path)
    return sorted(f for f in os.listdir(folder) if ".corrupt-" in f)


class PurgeJdRefsTest(IsolatedCase):
    """`kb.purge_jd_refs(jd_id)` = 删岗位时清引用：**只清 `from_jds` 条目、卡片留着**。

    这是 `jddelete._strip_references` 里知识库那一半的唯一实现（原来那一份已删）。
    判据与它**逐字同一把尺子**：条目 id 去首尾空白后相等（用户手改出的 `" jd-a "`
    也算同一条引用）。
    """

    def setUp(self):
        super().setUp()
        store.ensure_profile()
        store.save_knowledge({"version": kb.KB_VERSION, "nodes": [
            _ref_card("rlhf", "RLHF", [{"id": "jd-a", "job_title": "甲岗"},
                                       {"id": "jd-b", "job_title": "乙岗"}]),
            _ref_card("asr", "ASR", [{"id": "jd-a", "job_title": "甲岗"}]),
            _ref_card("tts", "TTS", [{"id": "jd-b", "job_title": "乙岗"}]),
        ]})
        self.path = store.knowledge_path()
        self.assertIn(self.tmp, self.path, "探针必须落在临时根里")

    def _card(self, cid):
        return [n for n in store.load_knowledge()["nodes"] if n.get("id") == cid][0]

    def test_only_the_matching_entries_go_away_and_every_card_stays(self):
        changed, err = kb.purge_jd_refs("jd-a")
        self.assertEqual(err, "")
        self.assertEqual(changed, 2, "命中的卡片数应当是 2（rlhf / asr）")
        self.assertEqual(_ref_ids(self._card("rlhf")), ["jd-b"])
        self.assertEqual(_ref_ids(self._card("asr")), [])
        self.assertEqual(len(store.load_knowledge()["nodes"]), 3, "卡片一张都不许少")
        # 没提到 jd-a 的卡：连它那条引用的 job_title 都原样留着
        self.assertEqual(self._card("tts")["from_jds"], [{"id": "jd-b", "job_title": "乙岗"}])

    def test_a_library_that_never_mentions_the_id_is_not_rewritten(self):
        before = _read_raw(self.path)
        self.assertEqual(kb.purge_jd_refs("jd-none"), (0, ""))
        self.assertEqual(_read_raw(self.path), before, "没有可清的引用时不该白重写整份库")

    def test_a_whitespace_padded_id_is_the_same_reference(self):
        """`" jd-a "` 与 `jd-a` 是同一条引用（照 `jddelete._text()` 的既有尺子）。"""
        store.save_knowledge({"version": kb.KB_VERSION, "nodes": [
            _ref_card("padded", "P", [{"id": " jd-a "}])]})
        changed, err = kb.purge_jd_refs("jd-a")
        self.assertEqual((changed, err), (1, ""))
        self.assertEqual(_ref_ids(self._card("padded")), [])

    def test_an_empty_id_touches_nothing(self):
        before = _read_raw(self.path)
        self.assertEqual(kb.purge_jd_refs(""), (0, ""))
        self.assertEqual(kb.purge_jd_refs(None), (0, ""))
        self.assertEqual(_read_raw(self.path), before)


class RepointJdRefsTest(IsolatedCase):
    """`kb.repoint_jd_refs(old, new)` = 改 ID 时把引用重指过去（`jdid.rename_jd` 用）。

    计数口径照 `jdid._repoint_kb`：返回**被改动的卡片数**（`jdid` 记的 `kb_nodes`）。
    判据也照它：**逐字比较**条目 id（不去空白 —— 去空白是 `purge_jd_refs` 那一半的尺子，
    收口时把三处的差异参数化、不顺手统一口径）。
    """

    def setUp(self):
        super().setUp()
        store.ensure_profile()
        store.save_knowledge({"version": kb.KB_VERSION, "nodes": [
            _ref_card("a1", "A1", [{"id": "old", "job_title": "旧岗", "date": "2026-10-01"}]),
            _ref_card("a2", "A2", [{"id": "old"}, {"id": "new", "job_title": "新岗"}]),
            _ref_card("a3", "A3", [{"id": "other"}]),
        ]})
        self.path = store.knowledge_path()
        self.assertIn(self.tmp, self.path, "探针必须落在临时根里")

    def _card(self, cid):
        return [n for n in store.load_knowledge()["nodes"] if n.get("id") == cid][0]

    def test_the_old_entry_is_repointed_in_place_and_keeps_its_own_fields(self):
        changed, err = kb.repoint_jd_refs("old", "new")
        self.assertEqual(err, "")
        self.assertEqual(changed, 2, "两张卡提到 old（a1 / a2）")        # 原位改 id：这条引用自己的 job_title / date 是它自己的身份，不许丢
        self.assertEqual(self._card("a1")["from_jds"],
                         [{"id": "new", "job_title": "旧岗", "date": "2026-10-01"}])
        # 卡上本来就有 new 的项时：保留它、去掉 old 那一条（否则同一个岗位被数两次）
        self.assertEqual(_ref_ids(self._card("a2")), ["new"])
        self.assertNotIn("old", _ref_ids(self._card("a3")))

    def test_two_old_entries_without_a_new_one_become_a_single_one(self):
        store.save_knowledge({"version": kb.KB_VERSION, "nodes": [
            _ref_card("dup", "D", [{"id": "old"}, {"id": "old"}])]})
        self.assertEqual(kb.repoint_jd_refs("old", "new"), (1, ""))
        self.assertEqual(_ref_ids(self._card("dup")), ["new"])

    def test_a_library_that_never_mentions_the_old_id_is_not_rewritten(self):
        before = _read_raw(self.path)
        self.assertEqual(kb.repoint_jd_refs("nobody", "new"), (0, ""))
        self.assertEqual(_read_raw(self.path), before, "没有命中的引用时不该白重写整份库")

    def test_the_id_comparison_is_verbatim_like_jdid_used_to_be(self):
        """`" old "` **不是** `old`：这一半的判据与 `jobdup` / `jdid` 原来逐字一致。"""
        store.save_knowledge({"version": kb.KB_VERSION, "nodes": [
            _ref_card("padded", "P", [{"id": " old "}])]})
        self.assertEqual(kb.repoint_jd_refs("old", "new"), (0, ""))
        self.assertEqual(_ref_ids(self._card("padded")), [" old "])


class MergeJdRefsTest(IsolatedCase):
    """`kb.merge_jd_refs(drop, keep)` = 合并时把被并掉的 id 重指到保留 id（`jobdup` 用）。

    变换与 `repoint_jd_refs` **同一份核心遍历**（只留一份实现），差别只在**计数口径**：
    `jobdup` 的对外字段是 `kb_repointed` / `kb_deduplicated`，数的是 `from_jds`
    **条目**（不是卡片）—— 那是既有日志与 `merge_jobs` 返回值的形状，收口时不许改。
    """

    def setUp(self):
        super().setUp()
        store.ensure_profile()
        store.save_knowledge({"version": kb.KB_VERSION, "nodes": [
            _ref_card("m1", "M1", [{"id": "drop", "job_title": "被并岗"}]),
            _ref_card("m2", "M2", [{"id": "drop"}, {"id": "keep", "job_title": "保留岗"}]),
            _ref_card("m3", "M3", [{"id": "keep"}]),
        ]})
        self.assertIn(self.tmp, store.knowledge_path(), "探针必须落在临时根里")

    def test_the_two_counters_are_item_level_like_the_old_jobdup_one(self):
        repointed, deduped, err = kb.merge_jd_refs("drop", "keep")
        self.assertEqual(err, "")
        self.assertEqual((repointed, deduped), (1, 1),
                         "m1 的 drop 条目被重指、m2 的 drop 条目被去重")
        card = [n for n in store.load_knowledge()["nodes"] if n.get("id") == "m1"][0]
        self.assertEqual(_ref_ids(card), ["keep"])

    def test_a_library_that_never_mentions_the_drop_id_is_not_rewritten(self):
        before = _read_raw(store.knowledge_path())
        self.assertEqual(kb.merge_jd_refs("nobody", "keep"), (0, 0, ""))
        self.assertEqual(_read_raw(store.knowledge_path()), before)


class KbRefInterfacesCorruptLibraryTest(IsolatedCase):
    """坏库（有内容却读不出来）→ 三个接口**都拒绝写入、连留档都不做**。

    口径照 `_with_kb_write(..., write_when_unreadable=False)` —— 也正是三处调用方本来的行为：
    `jddelete` 靠"坏库读成空库 → `kb_touched` 保持 0"恰好不写（回归断言见
    `test_jddelete.JdPurgeRollbackTest.test_an_unreadable_knowledge_base_is_reported_but_does_not_block`）；
    `jdid` / `jobdup` 各自在调用前用 `store.knowledge_safe_to_write()` 拒绝整次操作。
    现在这句话由 kb 自己保证，调用方不必再解析私有锁、也不必各自装一遍闸门。
    """

    ENTRIES = (("purge_jd_refs", lambda: kb.purge_jd_refs("jd-a")),
               ("repoint_jd_refs", lambda: kb.repoint_jd_refs("jd-a", "jd-b")),
               ("merge_jd_refs", lambda: kb.merge_jd_refs("jd-a", "jd-b")))

    def setUp(self):
        super().setUp()
        store.ensure_profile()
        self.path = store.knowledge_path()
        self.assertIn(self.tmp, self.path, "探针必须落在临时根里")
        # GBK 字节：非 UTF-8 ⇒ `read_knowledge_strict` 读不出来、`file_has_bytes_to_lose` 为真
        self.raw = json.dumps({"version": 1, "nodes": [{"id": "语音识别", "term": "语音识别"}]},
                              ensure_ascii=False).encode("gbk")
        with self.assertRaises(UnicodeDecodeError):
            self.raw.decode("utf-8")

    def test_every_ref_interface_refuses_and_keeps_the_corrupt_bytes(self):
        for name, call in self.ENTRIES:
            with self.subTest(entry=name):
                with open(self.path, "wb") as f:
                    f.write(self.raw)
                result = call()
                after = _read_raw(self.path)
                kv = "entry=%s before=%d after=%s result=%r" % (
                    name, len(self.raw), "文件不在了" if after is None else len(after), result)
                self.assertIsNotNone(after, "坏库被弄没了 " + kv)
                self.assertEqual(after, self.raw, "坏库被动过 " + kv)
                self.assertEqual(_corrupt_backups(self.path), [],
                                 "拒绝写入的入口不该隔离用户文件 " + kv)
                self.assertEqual(result[0], 0, "坏库时却报告改动了东西 " + kv)
                err = result[-1]
                self.assertTrue(err, "坏库时没给出可分辨的'没写'信号 " + kv)
                self.assertIn("读", str(err), "没如实说清原因是'库读不出来' " + kv)


class KbRefConcurrencyTest(IsolatedCase):
    """P3-3 的牙：分析写库（`absorb`）与"清引用 / 重指引用"**同时发生**时，一条都不许丢。

    失败形态（就是本块要修的）：两边各自 `load → 改 → save`，后写的整份**盖掉**先写的
    —— absorb 刚沉淀的卡片凭空消失，而两边都报成功（用户看不到任何提示）。

    **交错怎么造**（不靠 sleep 赌运气，见 `LOAD_DELAY_SECONDS`）：`threading.Barrier` 保证
    两条线程每一轮**同一瞬间**进入各自的读-改-写；`store.load_knowledge` 上的延迟保证
    "先读的拿到旧快照、另一条的写落在它的读与写之间"这件事**必然发生**一次。
    """

    ROUNDS = 40

    #: 把 **B 那一条线程**的「读 → 写」窗口人为拉宽（`store.load_knowledge` 返回前睡一下）。
    #: 为什么必须"只放慢 B"：`Barrier` 让两条线程同一瞬间起步，**后读的那个必然后写** ——
    #: 两边都放慢的话，后写的永远是 absorb（B 先读先写），丢更新反而被掩盖掉。
    #: 只放慢 B 之后，B 的读远早于它的写，而 A 的整段读-改-写就夹在这个窗口里 ⇒
    #: "A 写成功、B 拿旧快照后写并盖掉它"这件事**必然发生**一次。
    #: 加了锁的实现里这个 sleep 在临界区**里面**（`_with_kb_write` 持着 `_KB_LOCK` 调 fn）
    #: ⇒ A 根本进不来，照样全绿；把锁去掉（变异 #1"回到各自读改写"）就立刻变成静默丢数据。
    LOAD_DELAY_SECONDS = 0.008

    def _seed(self):
        store.ensure_profile()
        store.save_knowledge({"version": kb.KB_VERSION, "nodes": [
            _ref_card("seed", "种子", [{"id": "jd-old", "job_title": "旧岗"}])]})
        self.path = store.knowledge_path()
        self.assertIn(self.tmp, self.path, "探针必须落在临时根里")

    def _run(self, worker_b, name):
        """跑一轮并发：线程 A 反复 absorb（每轮新建一张卡、带 jd-old 引用），
        线程 B 反复做 `worker_b`。返回 (errors, 卡住的线程名)。"""
        errors = []
        barrier = threading.Barrier(2)
        real_load = store.load_knowledge
        real_save = store.save_knowledge
        slow_here = threading.local()       # 只在 B 线程里放慢（见 LOAD_DELAY_SECONDS）
        #: 只把**写盘这一步**串行。理由：Windows 上并发 `os.replace` 同一份文件会直接抛
        #: `PermissionError(13, '拒绝访问。')`（一条**响**的失败），而本用例要抓的是**静默**的
        #: 那一种（两边都报成功、后写的整份盖掉先写的）。串行化这一步之后，丢更新才暴露成本来
        #: 的形态；对加了锁的实现它毫无影响（那一段本来就在 `_KB_LOCK` 里、已经是串行的）。
        save_lock = threading.Lock()

        def slow_load(*a, **kw):
            data = real_load(*a, **kw)
            if getattr(slow_here, "on", False):
                time.sleep(self.LOAD_DELAY_SECONDS)
            return data

        def guarded_save(*a, **kw):
            with save_lock:
                return real_save(*a, **kw)

        def absorber():
            try:
                for i in range(self.ROUNDS):
                    barrier.wait(timeout=60)
                    kb.absorb([_node(term="概念-%d" % i)],
                              {"id": "jd-old", "job_title": "旧岗"})
            except Exception as e:              # 异常必须带回主线程，否则会被 unittest 吞掉
                errors.append("absorb 线程抛异常：%r" % (e,))

        def other():
            slow_here.on = True
            try:
                for _i in range(self.ROUNDS):
                    barrier.wait(timeout=60)
                    worker_b()
            except Exception as e:
                errors.append("%s 线程抛异常：%r" % (name, e))

        ta = threading.Thread(target=absorber, name="kb-ref-absorb", daemon=True)
        tb = threading.Thread(target=other, name="kb-ref-" + name, daemon=True)
        with mock.patch.object(store, "load_knowledge", slow_load), \
                mock.patch.object(store, "save_knowledge", guarded_save):
            ta.start()
            tb.start()
            ta.join(180)
            tb.join(180)
        return errors, [t.name for t in (ta, tb) if t.is_alive()]

    def _assert_no_absorbed_card_is_missing(self, sealed_terms):
        data = store.load_knowledge()
        have = {n.get("term") for n in data["nodes"]}
        missing = sorted(sealed_terms - have)
        self.assertEqual(missing, [],
                         "absorb 沉淀的卡片被并发的整份写盘盖掉了（丢更新）：%r" % (missing,))
        return data

    def test_absorb_and_purge_never_lose_an_absorbed_card(self):
        self._seed()
        errors, stuck = self._run(lambda: kb.purge_jd_refs("jd-old"), "purge")
        self.assertEqual(stuck, [], "并发用例没跑完（很可能被锁序/死锁挂住了）")
        # ① **先看数据丢没丢**（本块的主判据）：收尾清一遍引用（此时已无并发写者），
        #    再数 absorb 沉淀的卡片。丢更新时这里必红，而且红的就是"卡没了"本身。
        settled, err = kb.purge_jd_refs("jd-old")
        self.assertEqual(err, "")
        data = self._assert_no_absorbed_card_is_missing(
            {"概念-%d" % i for i in range(self.ROUNDS)})
        leftover = [n.get("term") for n in data["nodes"] if "jd-old" in _ref_ids(n)]
        self.assertEqual(leftover, [], "清引用没走完全程：还有卡片留着已删岗位的引用")
        self.assertGreaterEqual(settled, 0)          # 只是把它读出来，不假设这一轮该命中几条
        # ② 线程里不许抛异常（放在数据判据**后面**：丢更新才是首要判据）
        self.assertEqual(errors, [])

    def test_absorb_and_repoint_never_lose_an_absorbed_card(self):
        self._seed()
        errors, stuck = self._run(lambda: kb.repoint_jd_refs("jd-old", "jd-new"), "repoint")
        self.assertEqual(stuck, [], "并发用例没跑完（很可能被锁序/死锁挂住了）")
        settled, err = kb.repoint_jd_refs("jd-old", "jd-new")
        self.assertEqual(err, "")
        data = self._assert_no_absorbed_card_is_missing(
            {"概念-%d" % i for i in range(self.ROUNDS)})
        leftover = [n.get("term") for n in data["nodes"] if "jd-old" in _ref_ids(n)]
        self.assertEqual(leftover, [], "重指没走完全程：还有卡片指着旧 id")
        self.assertGreaterEqual(settled, 0)
        self.assertEqual(errors, [])


class KbRefLockOrderRuntimeTest(IsolatedCase):
    """锁序的**运行时**探针：持外层锁的线程与"新接口"之间不许出现互相等待。

    形状就是生产里的那条链：`jdid` / `jddelete` 先拿 `_JD_LOCK`、再拿 `_KB_LOCK`
    （外层 → 内层）。与之并发的新接口只该取 `_KB_LOCK`；**若它反序**
    （先 `_KB_LOCK`、再回头要 `_JD_LOCK`），两条线程就互等：

        线程 A：持 `_JD_LOCK`，等 `_KB_LOCK`
        线程 B（接口）：持 `_KB_LOCK`，等 `_JD_LOCK`     ← 死锁

    ⚠️ 用 `acquire(timeout=...)` 而不是裸 `with`：**探针自己不许把整个套件拖死**
    —— 红要红在断言上，不是把后续用例一起挂住。A 超时即判定失败并**释放** `_JD_LOCK`，
    B 随之能走完，进程干干净净地退出（留下的失败断言就是证据）。
    """

    ACQUIRE_TIMEOUT = 2.0

    def test_a_thread_holding_the_jd_lock_never_waits_forever_on_the_kb_lock(self):
        store.ensure_profile()
        store.save_knowledge({"version": kb.KB_VERSION, "nodes": [
            _ref_card("c1", "C1", [{"id": "old", "job_title": "旧岗"}])]})
        self.assertIn(self.tmp, store.knowledge_path(), "探针必须落在临时根里")
        a_entered, b_entered = threading.Event(), threading.Event()
        got_kb_lock = []
        outcome = []

        def holder_a():
            with jdid._JD_LOCK:
                a_entered.set()
                b_entered.wait(10)                 # 等 B 走进接口（先拿到 _KB_LOCK）
                time.sleep(0.3)
                got = kb._KB_LOCK.acquire(timeout=self.ACQUIRE_TIMEOUT)
                if got:
                    kb._KB_LOCK.release()
                got_kb_lock.append(got)

        def caller_b():
            a_entered.wait(10)
            b_entered.set()
            try:
                outcome.append(kb.repoint_jd_refs("old", "new"))
            except Exception as e:
                outcome.append(e)

        ta = threading.Thread(target=holder_a, name="kb-ref-lockorder-a", daemon=True)
        tb = threading.Thread(target=caller_b, name="kb-ref-lockorder-b", daemon=True)
        ta.start()
        tb.start()
        ta.join(20)
        tb.join(20)
        stuck = [t.name for t in (ta, tb) if t.is_alive()]
        self.assertEqual(stuck, [], "锁序反了：两条线程互相等待，套件差点被拖死")
        self.assertEqual(got_kb_lock, [True],
                         "持 `_JD_LOCK` 的线程等不到 `_KB_LOCK`（超时 %ss）：新接口很可能在持 "
                         "`_KB_LOCK` 时回头去取外层锁 —— 那正是死锁的形状" % self.ACQUIRE_TIMEOUT)
        self.assertEqual(outcome, [(1, "")],
                         "接口没把引用重指过去（或者抛了异常）")


class KbRefLockOrderAstTest(unittest.TestCase):
    """锁序铁律的**结构**断言：`_JOBS/_CONFIG` → `_JD`/`_MERGE` → `_KB` → 文件写。

    为什么要 AST（不是正则）：说明文字里提到 `_JD_LOCK` 是**应该的**（铁律就写在注释里），
    要抓的是**代码里真的去拿它**。两条判据：

      ① `kb.py` 里一个 `_JD_LOCK` / `_MERGE_LOCK` 都不许出现 —— 新接口若在持 `_KB_LOCK`
         时回头去取外层锁，就是死锁的经典形状（外层锁的持有者正等着 `_KB_LOCK`）；
      ② 三个调用方里，`with kb._KB_LOCK:` 的**里面**不许再有取 `_JD_LOCK` / `_MERGE_LOCK`
         的 `with`（反序）。
    """

    OUTER = ("_JD_LOCK", "_MERGE_LOCK")
    CALLERS = ("jdid.py", "jddelete.py", "jobdup.py")

    def _src(self, name):
        src = store.read_text(os.path.join(store.ROOT, name))
        self.assertTrue(src, "读不到 %s？" % name)
        return src

    @staticmethod
    def _names(node):
        """一段 AST 里出现过的锁名（属性名 + 裸名字）。"""
        return ({n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}
                | {n.id for n in ast.walk(node) if isinstance(n, ast.Name)})

    @classmethod
    def _with_locks(cls, with_stmt):
        """这个 `with` 语句**自己**的（不含 body）上下文表达式里出现过的锁名。"""
        names = set()
        for item in with_stmt.items:
            names |= cls._names(item.context_expr)
        return names

    def test_kb_never_reaches_for_an_outer_lock(self):
        used = self._names(ast.parse(self._src("kb.py")))
        self.assertNotIn("_JD_LOCK", used,
                         "kb.py 里去拿 `_JD_LOCK` 了：持 `_KB_LOCK` 时回头取外层锁 = 死锁形状")
        self.assertNotIn("_MERGE_LOCK", used, "kb.py 里去拿 `_MERGE_LOCK` 了（同上）")

    def test_no_caller_takes_an_outer_lock_inside_a_kb_lock_block(self):
        offenders = []
        for name in self.CALLERS:
            tree = ast.parse(self._src(name))
            for outer in [n for n in ast.walk(tree) if isinstance(n, ast.With)]:
                if "_KB_LOCK" not in self._with_locks(outer):
                    continue
                for branch in outer.body:
                    for inner in [n for n in ast.walk(branch) if isinstance(n, ast.With)]:
                        bad = self._with_locks(inner) & set(self.OUTER)
                        if bad:
                            offenders.append("%s:%d 在 `with kb._KB_LOCK` 里取了 %s"
                                             % (name, inner.lineno, sorted(bad)))
        self.assertEqual(offenders, [],
                         "锁序反了（外层锁必须在 `_KB_LOCK` **外面**拿）：%r" % (offenders,))

    def test_the_three_callers_really_do_hold_the_kb_lock(self):
        """反向：上面那条不许变成"没人取过 `_KB_LOCK`"的空断言。"""
        for name in self.CALLERS:
            self.assertIn("kb._KB_LOCK", self._src(name),
                          "%s 已经不再持 `_KB_LOCK` 了？那这条 AST 断言就没验证到东西" % name)
