# -*- coding: utf-8 -*-
import glob
import io
import json
import log
import os
import threading
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
