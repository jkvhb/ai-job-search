# -*- coding: utf-8 -*-
"""jdfacts（JD 级事实缓存：知识地图只算一次）的测试。

**这块的价值来自用户真实日志**：

    analyze.knowledge_done  {"ms": 206206, "nodes": 53}    ← 知识地图：206 秒、53 次搜索
    analyze.success         {"ms": 236186}                 ← 一次分析总计 236 秒

知识地图占一次分析 87% 的时间与**全部** ~53 次 Tavily 搜索额度，而它是**纯 JD 级**的
（"这个岗位要会什么"，与哪份简历无关）。所以「同一个 JD 只算一次、换简历直接复用」就是全部收益。

**本文件的三条硬纪律**（每一条都对应本项目抓到过的假绿）：
1. 「复用了缓存」**必须同时**断言注入的 builder 调用次数为 **0** —— 只断言"返回了节点"是恒真的；
2. 「播种」同理：断言没调 builder、`source == "seeded:<报告id>"`、且节点与报告里那份**逐字相同**；
3. 落盘断言一律看**原始 jds.json**（`_raw_jd`），不看 `get()` 的返回值 —— 只有真写进文件才算数
   （「加字段型」变异会被读侧净化，只有钉住落盘那一行才抓得住）。

**绝不联网 / 绝不真调模型**：真算那一步由测试注入的假 builder 承担；生产里由 app.py 注入
`knowledge.build_for_jd`（本模块刻意不 import knowledge，见 `ModuleHygieneTest`）。
"""
import ast
import json
import os
import re
import unittest
from unittest import mock

import jdfacts
import jdid
import kb
import log
import store

from tests.base import IsolatedCase

JD_A = "本科_甲公司_标注_文本_北京_13-26K_0001"
JD_B = "硕士_乙公司_评测_语音_上海_20-30K_0002"
CFG = {"max_searches": 3, "search_providers": []}


# ---------------------------------------------------------------- 夹具
def _nodes(tag):
    """一份带哨兵字符串的假知识地图：逐字比对时任何一处不同都会露出来（不是空列表）。"""
    return [
        {"id": "n-%s-1" % tag, "term": "语音识别（%s）" % tag, "layer": 0,
         "sources": [{"title": "来源-%s" % tag, "url": "https://example.invalid/%s" % tag}],
         "timeline": [{"year": "2020", "text": "时间线-%s" % tag}],
         "related": ["相关-%s" % tag], "interview_questions": ["问题-%s" % tag],
         "confidence": "verified"},
        {"id": "n-%s-2" % tag, "term": "幻觉（%s）" % tag, "layer": 1,
         "sources": [], "timeline": [], "related": [], "interview_questions": [],
         "confidence": "guess"},
    ]


def _jdump(value):
    """逐字比较用：把节点列表序列化成稳定字符串（键序无关、中文不转义）。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _raw_jd(jd_id):
    """**原始 jds.json** 里那条 JD 记录（不经任何读侧净化）。找不到 → None。"""
    data = store.read_json(store.p_path("jds.json"), {})
    for rec in (data.get("jds") if isinstance(data, dict) else None) or []:
        if isinstance(rec, dict) and rec.get("id") == jd_id:
            return rec
    return None


def _raw_facts(jd_id):
    """**原始 jds.json** 里那条记录的 `jd_facts`；没有 → None。"""
    rec = _raw_jd(jd_id)
    if not isinstance(rec, dict):
        return None
    return rec.get("jd_facts")


def _write_report(filename, payload):
    """按 app.render_report 的真实形状写一份报告（REPORT_DATA + JSON）。"""
    path = os.path.join(store.p_path("reports"), filename)
    store.write_text(path, '<!doctype html><html><body><script>\n%s%s\n</script></body></html>'
                     % (kb.REPORT_MARK, json.dumps(payload, ensure_ascii=False)))
    return path


def _analysis(rid, jd_id, created_at, report=None, **extra):
    """往 jobs.json 写一条**分析**记录（该 JD 自己的一次分析）。"""
    rec = {"id": rid, "jd_id": jd_id, "resume": "母版", "score": 70.0,
           "report": (rid + ".html") if report is None else report,
           "created_at": created_at}
    rec.update(extra)
    return store.upsert_job(rec)


def _events(name):
    """隔离根日志里某个事件的全部记录（log.LOG_FILE 由 IsolatedCase 指向临时目录）。"""
    return [r for r in log.read_logs(500)[0] if r.get("event") == name]


class _Builder:
    """注入的假 builder：只计数、绝不联网。生产里这一步是真调模型 + 真搜索。"""

    def __init__(self, nodes=None):
        self.calls = []
        self.nodes = _nodes("computed") if nodes is None else nodes

    def __call__(self, jd_text, cfg):
        self.calls.append((jd_text, cfg))
        # 每次给一份**新副本**：缓存里存的必须是内容，不是这个对象的引用
        return json.loads(json.dumps(self.nodes, ensure_ascii=False))


# ---------------------------------------------------------------- ① 读 / 写 / 容错
class GetAndSaveTest(IsolatedCase):
    def test_get_of_an_unknown_jd_raises_lookup_error(self):
        """岗位不存在 → LookupError（不是静默空字典：那会让调用方以为「这个 JD 没有缓存」）。"""
        for bad in (JD_A, "", None, "  "):
            with self.subTest(jd_id=bad):
                with self.assertRaises(LookupError):
                    jdfacts.get(bad)

    def test_get_tolerates_missing_and_junk_facts(self):
        """`jd_facts` 缺失 / 不是字典（用户可手改这个文件）→ `{}`，绝不抛异常。"""
        jdid.upsert_jd({"id": JD_A})
        self.assertEqual(jdfacts.get(JD_A), {})
        for junk in ("垃圾", [], 5, None, [1, 2]):
            with self.subTest(junk=junk):
                jdid.upsert_jd({"id": JD_A, "jd_facts": junk})
                self.assertEqual(jdfacts.get(JD_A), {})
                self.assertIsNone(jdfacts.knowledge_of(JD_A))

    def test_get_returns_a_copy_so_callers_cannot_mutate_the_library(self):
        jdid.upsert_jd({"id": JD_A, "jd_facts": {"knowledge": _nodes("x"), "knowledge_at": "2026-10-03 00:35"}})
        got = jdfacts.get(JD_A)
        got["knowledge"] = "改坏了"
        got["新键"] = 1
        self.assertEqual(_raw_facts(JD_A)["knowledge"], _nodes("x"))
        self.assertNotIn("新键", _raw_facts(JD_A))

    def test_knowledge_of_is_none_when_there_is_nothing_usable(self):
        jdid.upsert_jd({"id": JD_A})
        self.assertIsNone(jdfacts.knowledge_of(JD_A))
        for junk in ([], "不是列表", {"nodes": "也不是列表"}, 5, None, {}):
            with self.subTest(junk=junk):
                jdid.upsert_jd({"id": JD_A, "jd_facts": {"knowledge": junk}})
                self.assertIsNone(jdfacts.knowledge_of(JD_A))

    def test_save_knowledge_writes_through_to_the_real_file(self):
        jdid.upsert_jd({"id": JD_A})
        with mock.patch.object(jdfacts, "_now", return_value="2026-10-03 00:35"):
            jdfacts.save_knowledge(JD_A, _nodes("seed"), "computed")
        facts = _raw_facts(JD_A)                                  # ← 落盘那一行，不是读侧
        self.assertEqual(_jdump(facts["knowledge"]), _jdump(_nodes("seed")))
        self.assertEqual(facts["knowledge_at"], "2026-10-03 00:35")
        self.assertEqual(facts["knowledge_source"], "computed")
        self.assertEqual(jdfacts.get(JD_A)["knowledge_at"], "2026-10-03 00:35")
        self.assertEqual(_jdump(jdfacts.knowledge_of(JD_A)), _jdump(_nodes("seed")))

    def test_save_knowledge_keeps_the_first_generated_at(self):
        """**首次生成时间保留**：knowledge_at 是「这份知识地图第一次生成于何时」。

        覆盖掉它就等于把「这份地图有多旧」这条信息弄丢（复用时要展示给用户看）。
        时钟必须**注入**：真实实现是分钟粒度，同一分钟内两次写入本来就是同一个字符串，
        不注入的话这条断言会靠巧合恒真（正是本项目抓过的假绿形态）。
        """
        jdid.upsert_jd({"id": JD_A, "jd_facts": {"knowledge_at": "2020-01-01 08:00"}})
        with mock.patch.object(jdfacts, "_now", return_value="2026-11-01 12:00"):
            jdfacts.save_knowledge(JD_A, _nodes("第二次"), "seeded:r9")
        facts = _raw_facts(JD_A)
        self.assertEqual(facts["knowledge_at"], "2020-01-01 08:00")     # 旧的时间没被动
        self.assertEqual(_jdump(facts["knowledge"]), _jdump(_nodes("第二次")))   # 内容换了
        self.assertEqual(facts["knowledge_source"], "seeded:r9")
        # 第二次：先写一次（t1），再写一次（t2）→ knowledge_at 必须还是 t1
        jdid.upsert_jd({"id": JD_B})
        with mock.patch.object(jdfacts, "_now", return_value="2026-10-03 00:35"):
            jdfacts.save_knowledge(JD_B, _nodes("first"), "computed")
        with mock.patch.object(jdfacts, "_now", return_value="2026-12-31 23:59"):
            jdfacts.save_knowledge(JD_B, _nodes("second"), "computed")
        self.assertEqual(_raw_facts(JD_B)["knowledge_at"], "2026-10-03 00:35")
        self.assertEqual(_jdump(_raw_facts(JD_B)["knowledge"]), _jdump(_nodes("second")))

    def test_save_knowledge_stamps_a_time_only_when_there_was_none(self):
        jdid.upsert_jd({"id": JD_A})
        with mock.patch.object(jdfacts, "_now", return_value="2026-10-03 00:35"):
            self.assertEqual(jdfacts.save_knowledge(JD_A, _nodes("x"), "computed")["knowledge_at"],
                             "2026-10-03 00:35")

    def test_save_knowledge_keeps_other_facts_keys(self):
        """只动三个键：`seeded_at`（以及用户/将来加的键）必须原样留着。"""
        jdid.upsert_jd({"id": JD_A, "jd_facts": {"seeded_at": "2026-09-01 10:00", "别的": [1]}})
        jdfacts.save_knowledge(JD_A, _nodes("x"), "computed")
        facts = _raw_facts(JD_A)
        self.assertEqual(facts["seeded_at"], "2026-09-01 10:00")
        self.assertEqual(facts["别的"], [1])
        self.assertEqual(facts["knowledge_source"], "computed")

    def test_save_knowledge_accepts_the_nodes_dict_form(self):
        """容错：`{"nodes": [...]}`（旧报告/手写数据里出现过这种形态，kb 也认）→ 存成列表。"""
        jdid.upsert_jd({"id": JD_A})
        jdfacts.save_knowledge(JD_A, {"nodes": _nodes("dict")}, "computed")
        self.assertEqual(_jdump(_raw_facts(JD_A)["knowledge"]), _jdump(_nodes("dict")))

    def test_save_knowledge_of_an_unknown_jd_raises_and_creates_no_file(self):
        with self.assertRaises(LookupError):
            jdfacts.save_knowledge(JD_A, _nodes("x"), "computed")
        self.assertFalse(os.path.exists(store.p_path("jds.json")),
                         "给不存在的岗位存缓存时居然凭空造出了一个 jds.json")

    def test_save_knowledge_rejects_garbage_without_writing(self):
        jdid.upsert_jd({"id": JD_A})
        before = store.read_text(store.p_path("jds.json"), None)
        for junk in ("不是列表", 5, None, {"没有nodes": 1}, {"nodes": "不是列表"}):
            with self.subTest(junk=junk):
                with self.assertRaises(ValueError):
                    jdfacts.save_knowledge(JD_A, junk, "computed")
                self.assertEqual(store.read_text(store.p_path("jds.json"), None), before)

    def test_save_knowledge_allows_an_empty_list_but_it_is_not_a_usable_cache(self):
        """空列表是合法的「真算结果」（这个 JD 没抽出知识点），但不是可用的缓存。"""
        jdid.upsert_jd({"id": JD_A})
        jdfacts.save_knowledge(JD_A, [], "computed")
        self.assertEqual(_raw_facts(JD_A)["knowledge"], [])
        self.assertIsNone(jdfacts.knowledge_of(JD_A))
        self.assertTrue(jdfacts.needs_compute(JD_A, False))

    def test_read_modify_write_happens_under_the_jd_lock(self):
        """读 `jd_facts` → 改 → 写回是**一个临界区**：不在锁里读就会并发丢更新。

        直接问那把锁「现在归本线程吗」：`jdid.find_jd` 只在 `_store` 的锁内被调一次，
        所以那次记录的必须全是 True（`jdid.upsert_jd` 自己也会加锁，但它挡不住「读」在外面）。
        """
        jdid.upsert_jd({"id": JD_A})
        seen = []
        real_find = jdid.find_jd

        def watch(jd_id, *a, **kw):
            seen.append(jdid._JD_LOCK._is_owned())
            return real_find(jd_id, *a, **kw)

        with mock.patch.object(jdid, "find_jd", watch):
            jdfacts.save_knowledge(JD_A, _nodes("x"), "computed")
        self.assertTrue(seen, "整个写路径居然没读过 jds.json，这条测试就没验证到东西")
        self.assertEqual(seen, [True], "读 jd_facts 时没有持 _JD_LOCK：%s" % seen)


class NeedsComputeTest(IsolatedCase):
    def test_the_four_combinations(self):
        """`needs_compute(jd_id, refresh)` 的四种组合：有/没有缓存 × 刷不刷新。"""
        jdid.upsert_jd({"id": JD_A})
        self.assertTrue(jdfacts.needs_compute(JD_A, False))       # 没缓存，不刷新 → 要算
        self.assertTrue(jdfacts.needs_compute(JD_A, True))        # 没缓存，刷新   → 要算
        jdfacts.save_knowledge(JD_A, _nodes("x"), "computed")
        self.assertFalse(jdfacts.needs_compute(JD_A, False))      # 有缓存，不刷新 → 不用算
        self.assertTrue(jdfacts.needs_compute(JD_A, True))        # 有缓存，刷新   → 要算

    def test_it_answers_only_about_the_cache_not_about_seeding(self):
        """没有缓存但有可播种的报告时它也是 True —— 播种是 resolve 的第 ② 级，不在这里判。"""
        jdid.upsert_jd({"id": JD_A})
        _analysis("r1", JD_A, "2026-10-01 09:00")
        _write_report("r1.html", {"report_id": "r1", "knowledge": _nodes("report")})
        self.assertTrue(jdfacts.needs_compute(JD_A, False))
        self.assertEqual(jdfacts.seed_from_reports(JD_A), (True, "r1"))
        self.assertFalse(jdfacts.needs_compute(JD_A, False))       # 播种之后就有缓存了


# ---------------------------------------------------------------- ② 播种
class SeedFromReportsTest(IsolatedCase):
    def test_seeds_from_the_jds_own_report_and_names_the_report_id(self):
        jdid.upsert_jd({"id": JD_A})
        _analysis("20261003_岗A_75分", JD_A, "2026-10-03 00:30")
        _write_report("20261003_岗A_75分.html",
                      {"report_id": "20261003_岗A_75分", "knowledge": _nodes("report")})
        with mock.patch.object(jdfacts, "_now", return_value="2026-10-20 09:00"):
            got = jdfacts.seed_from_reports(JD_A)
        self.assertEqual(got, (True, "20261003_岗A_75分"))
        facts = _raw_facts(JD_A)                                   # ← 真落盘的那一行
        self.assertEqual(_jdump(facts["knowledge"]), _jdump(_nodes("report")))   # 逐字相同
        self.assertEqual(facts["knowledge_source"], "seeded:20261003_岗A_75分")
        self.assertEqual(facts["seeded_at"], "2026-10-20 09:00")
        self.assertEqual(facts["knowledge_at"], "2026-10-20 09:00")
        self.assertEqual(_jdump(jdfacts.knowledge_of(JD_A)), _jdump(_nodes("report")))

    def test_report_id_is_the_report_filename_stem(self):
        """「报告 id」的既有定义就是**报告文件名去掉 .html**（`store.report_state_path` /
        前端 `body["report"][:-5]` 都用它），不是 REPORT_DATA 里那个字段。"""
        jdid.upsert_jd({"id": JD_A})
        _analysis("a1", JD_A, "2026-10-01 09:00")
        _write_report("a1.html", {"report_id": "别的名字", "knowledge": _nodes("report")})
        self.assertEqual(jdfacts.seed_from_reports(JD_A), (True, "a1"))
        self.assertEqual(_raw_facts(JD_A)["knowledge_source"], "seeded:a1")

    def test_report_without_knowledge_does_not_block_a_good_one(self):
        """坏/空的报告要**跳过继续找**，不能因为最新那份没知识就放弃播种。"""
        jdid.upsert_jd({"id": JD_A})
        _analysis("r_bad", JD_A, "2026-10-01 09:00", report="r_bad.html")   # 文件根本不存在
        _analysis("r_empty", JD_A, "2026-10-02 09:00", report="r_empty.html")
        _write_report("r_empty.html", {"report_id": "r_empty", "knowledge": []})
        _analysis("r_good", JD_A, "2026-10-03 09:00", report="r_good.html")
        _write_report("r_good.html", {"report_id": "r_good", "knowledge": _nodes("good")})
        self.assertEqual(jdfacts.seed_from_reports(JD_A), (True, "r_good"))
        self.assertEqual(_raw_facts(JD_A)["knowledge_source"], "seeded:r_good")
        # 反过来：好报告在前、坏报告在后，也照样播种（顺序只影响「先找到谁」）
        jdid.upsert_jd({"id": JD_B})
        _analysis("r_good2", JD_B, "2026-10-01 09:00", report="r_good2.html")
        _write_report("r_good2.html", {"report_id": "r_good2", "knowledge": _nodes("good2")})
        _analysis("r_bad2", JD_B, "2026-10-02 09:00", report="r_bad2.html")
        self.assertEqual(jdfacts.seed_from_reports(JD_B), (True, "r_good2"))

    def test_every_kind_of_unreadable_report_returns_false_and_writes_nothing(self):
        """读不出来一律**静默** `(False, None)`：不抛、绝不写空缓存（否则下次会被当成"缓存命中"）。"""
        cases = {
            "文件缺失": ("r1", "r1.html", None),
            "没有 REPORT_DATA": ("r2", "r2.html", "PLAIN"),
            "REPORT_DATA 是坏 JSON": ("r3", "r3.html", "BADJSON"),
            "REPORT_DATA 不是对象": ("r4", "r4.html", "LIST"),
            "没有 knowledge 键": ("r5", "r5.html", {"report_id": "r5"}),
            "knowledge 是垃圾": ("r6", "r6.html", {"report_id": "r6", "knowledge": "不是列表"}),
            "knowledge 是空列表": ("r7", "r7.html", {"report_id": "r7", "knowledge": []}),
        }
        for name, (rid, filename, payload) in cases.items():
            with self.subTest(case=name):
                jd_id = JD_A
                jdid.save_jds({"version": 1, "seq": 0, "jds": [{"id": jd_id}]})
                store.save_jobs([])
                _analysis(rid, jd_id, "2026-10-01 09:00", report=filename)
                if payload == "PLAIN":
                    store.write_text(os.path.join(store.p_path("reports"), filename), "<html>旧报告</html>")
                elif payload == "BADJSON":
                    store.write_text(os.path.join(store.p_path("reports"), filename),
                                     "<script>%s{ \"job_title\": ] 这不是 JSON }</script>" % kb.REPORT_MARK)
                elif payload == "LIST":
                    store.write_text(os.path.join(store.p_path("reports"), filename),
                                     "<script>%s[1, 2, 3]</script>" % kb.REPORT_MARK)
                elif payload is not None:
                    _write_report(filename, payload)
                self.assertEqual(jdfacts.seed_from_reports(jd_id), (False, None))
                facts = _raw_facts(jd_id)
                self.assertNotIn("knowledge", facts or {},
                                 "读不出报告时居然往缓存里写了东西：%r" % (facts,))
                self.assertIsNone(jdfacts.knowledge_of(jd_id))

    def test_never_seeds_from_another_jds_report(self):
        """**绝不猜**：B 没有自己的分析时，A 的报告一个字节都不能拿来用。"""
        jdid.upsert_jd({"id": JD_A})
        jdid.upsert_jd({"id": JD_B})
        _analysis("rA", JD_A, "2026-10-01 09:00", report="rA.html")
        _write_report("rA.html", {"report_id": "rA", "knowledge": _nodes("A-report")})
        self.assertEqual(jdfacts.seed_from_reports(JD_B), (False, None))
        self.assertIsNone(_raw_facts(JD_B))
        # B 自己那份报告没知识时，也不能退回去拿 A 的
        _analysis("rB", JD_B, "2026-10-02 09:00", report="rB.html")
        store.write_text(os.path.join(store.p_path("reports"), "rB.html"), "<html>没有快照</html>")
        self.assertEqual(jdfacts.seed_from_reports(JD_B), (False, None))
        self.assertIsNone(_raw_facts(JD_B))
        # 反向对照：A 自己确实播得到（证明上面那个 False 不是因为「谁都播不到」）
        self.assertEqual(jdfacts.seed_from_reports(JD_A), (True, "rA"))

    def test_unknown_jd_or_empty_id_is_false_not_an_exception(self):
        jdid.upsert_jd({"id": JD_A})
        _analysis("rA", JD_A, "2026-10-01 09:00", report="rA.html")
        _write_report("rA.html", {"report_id": "rA", "knowledge": _nodes("A-report")})
        for bad in (JD_B, "", None, "   "):
            with self.subTest(jd_id=bad):
                self.assertEqual(jdfacts.seed_from_reports(bad), (False, None))

    def test_report_field_falls_back_to_the_analysis_id_html_convention(self):
        """迁移前的约定：分析 id == 报告文件名。没有 `report` 字段时按它定位**它自己的**报告。"""
        jdid.upsert_jd({"id": JD_A})
        _analysis("20261001_岗A_70分", JD_A, "2026-10-01 09:00", report="")
        _write_report("20261001_岗A_70分.html",
                      {"report_id": "20261001_岗A_70分", "knowledge": _nodes("conv")})
        self.assertEqual(jdfacts.seed_from_reports(JD_A), (True, "20261001_岗A_70分"))
        self.assertEqual(_jdump(jdfacts.knowledge_of(JD_A)), _jdump(_nodes("conv")))

    def test_a_traversal_shaped_report_field_cannot_escape_the_reports_dir(self):
        jdid.upsert_jd({"id": JD_A})
        _analysis("a1", JD_A, "2026-10-01 09:00", report="../evil.html")
        outside = os.path.join(store.profile_dir(), "evil.html")
        store.write_text(outside, "<script>%s%s</script>"
                         % (kb.REPORT_MARK, json.dumps({"knowledge": _nodes("outside")})))
        self.assertEqual(jdfacts.seed_from_reports(JD_A), (False, None))
        self.assertIsNone(_raw_facts(JD_A))
        self.assertTrue(os.path.exists(outside))          # 也绝不删用户的文件

    def test_dict_form_knowledge_in_a_report_is_accepted(self):
        jdid.upsert_jd({"id": JD_A})
        _analysis("r1", JD_A, "2026-10-01 09:00")
        _write_report("r1.html", {"report_id": "r1", "knowledge": {"nodes": _nodes("dict")}})
        self.assertEqual(jdfacts.seed_from_reports(JD_A), (True, "r1"))
        self.assertEqual(_jdump(jdfacts.knowledge_of(JD_A)), _jdump(_nodes("dict")))

    def test_a_later_seed_does_not_rewrite_an_existing_first_generated_at(self):
        jdid.upsert_jd({"id": JD_A, "jd_facts": {"knowledge_at": "2020-01-01 08:00"}})
        _analysis("r1", JD_A, "2026-10-01 09:00")
        _write_report("r1.html", {"report_id": "r1", "knowledge": _nodes("report")})
        jdfacts.seed_from_reports(JD_A)
        self.assertEqual(_raw_facts(JD_A)["knowledge_at"], "2020-01-01 08:00")

    def test_seeding_only_touches_the_jd_library(self):
        jdid.upsert_jd({"id": JD_A})
        _analysis("r1", JD_A, "2026-10-01 09:00")
        _write_report("r1.html", {"report_id": "r1", "knowledge": _nodes("report")})
        knowledge_before = store.read_text(store.knowledge_path(), None)
        jdfacts.seed_from_reports(JD_A)
        self.assertEqual(store.read_text(store.knowledge_path(), None), knowledge_before)
        self.assertFalse(os.path.exists(store.knowledge_path()))   # 播种不该顺手沉淀知识库


# ---------------------------------------------------------------- ③ 三级回退编排
class ResolveTest(IsolatedCase):
    def _new_jd(self, jd_id=JD_A):
        jdid.upsert_jd({"id": jd_id, "seq": 1})
        return jd_id

    # ---- 第 ③ 级：真算 ----
    def test_third_level_computes_once_and_saves_it(self):
        self._new_jd()
        b = _Builder(_nodes("first"))
        with mock.patch.object(jdfacts, "_now", return_value="2026-10-03 00:35"):
            nodes, source = jdfacts.resolve(JD_A, "JD 原文-A", CFG, builder=b)
        self.assertEqual(source, "computed")
        self.assertEqual(b.calls, [("JD 原文-A", CFG)])            # 参数也逐个钉住
        self.assertEqual(_jdump(nodes), _jdump(_nodes("first")))
        facts = _raw_facts(JD_A)
        self.assertEqual(_jdump(facts["knowledge"]), _jdump(_nodes("first")))
        self.assertEqual(facts["knowledge_source"], "computed")
        self.assertEqual(facts["knowledge_at"], "2026-10-03 00:35")
        evs = _events("analyze.knowledge_computed")
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["data"]["jd"], JD_A)
        self.assertEqual(evs[0]["data"]["refresh"], False)
        self.assertEqual(evs[0]["data"]["replaced_at"], "")
        self.assertEqual(evs[0]["data"]["nodes"], 2)

    def test_first_level_reuse_calls_the_builder_zero_times(self):
        """**本块的核心验收**：同一 JD 第二次分析 → builder（= 真算 + 53 次搜索）调用 **0** 次。"""
        self._new_jd()
        b = _Builder(_nodes("first"))
        n1, s1 = jdfacts.resolve(JD_A, "JD 原文-A", CFG, builder=b)
        self.assertEqual((s1, len(b.calls)), ("computed", 1))
        at1 = _raw_facts(JD_A)["knowledge_at"]
        with mock.patch.object(jdfacts, "_now", return_value="2026-12-01 00:00"):
            n2, s2 = jdfacts.resolve(JD_A, "JD 原文-A", CFG, builder=b)
        self.assertEqual(len(b.calls), 1, "第二次分析又调了一次 builder（= 又烧了 53 次搜索）：%r"
                         % (b.calls,))
        self.assertEqual(s2, "cache")
        self.assertEqual(_jdump(n2), _jdump(n1), "复用的知识地图与第一次不是逐字相同")
        self.assertEqual(_jdump(n2), _jdump(_nodes("first")))
        self.assertEqual(_jdump(jdfacts.knowledge_of(JD_A)), _jdump(n1))
        # 复用时首次生成时间不该被改写（否则「复用 10-03 那份」会显示成今天）
        self.assertEqual(_raw_facts(JD_A)["knowledge_at"], at1)
        evs = _events("analyze.knowledge_reused")
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["data"]["jd"], JD_A)
        self.assertEqual(evs[0]["data"]["nodes"], 2)

    def test_reuse_does_not_seed_even_when_a_seedable_report_exists(self):
        """顺序是契约：① 缓存命中就**不播种**（也不写 seeded_at、不改 knowledge_at）。"""
        self._new_jd()
        b = _Builder(_nodes("first"))
        jdfacts.resolve(JD_A, "JD 原文-A", CFG, builder=b)          # 先有一份缓存
        # 之后才冒出一份「该 JD 自己的、有知识的」旧报告
        _analysis("r1", JD_A, "2026-10-01 09:00")
        _write_report("r1.html", {"report_id": "r1", "knowledge": _nodes("report")})
        n2, s2 = jdfacts.resolve(JD_A, "JD 原文-A", CFG, builder=b)
        self.assertEqual(s2, "cache")                               # 不是 "seeded:r1"
        self.assertEqual(_jdump(n2), _jdump(_nodes("first")))        # 不是报告里那份
        self.assertEqual(len(b.calls), 1)
        facts = _raw_facts(JD_A)
        self.assertNotIn("seeded_at", facts, "复用路径居然去播种了")
        self.assertEqual(facts["knowledge_source"], "computed")
        self.assertEqual(_events("analyze.knowledge_seeded"), [])

    # ---- 第 ② 级：播种 ----
    def test_second_level_seeds_without_calling_the_builder(self):
        self._new_jd()
        _analysis("20261003_岗A_75分", JD_A, "2026-10-03 00:30")
        _write_report("20261003_岗A_75分.html",
                      {"report_id": "20261003_岗A_75分", "knowledge": _nodes("report")})
        b = _Builder(_nodes("不该被调到"))
        nodes, source = jdfacts.resolve(JD_A, "JD 原文-A", CFG, builder=b)
        self.assertEqual(b.calls, [], "播种路径居然调了 builder（= 真搜索）")
        self.assertEqual(source, "seeded:20261003_岗A_75分")
        self.assertEqual(_jdump(nodes), _jdump(_nodes("report")))     # 与报告里那份逐字相同
        facts = _raw_facts(JD_A)
        self.assertEqual(facts["knowledge_source"], "seeded:20261003_岗A_75分")
        self.assertTrue(facts.get("seeded_at"))
        self.assertEqual(_jdump(facts["knowledge"]), _jdump(_nodes("report")))
        evs = _events("analyze.knowledge_seeded")
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["data"]["report"], "20261003_岗A_75分")
        # 第二次分析：直接走缓存（连报告都不用再读）
        n3, s3 = jdfacts.resolve(JD_A, "JD 原文-A", CFG, builder=b)
        self.assertEqual((s3, b.calls), ("cache", []))

    def test_broken_report_degrades_to_a_real_compute_not_to_an_empty_map(self):
        """**退化的正确形态**：坏报告 → 走真算；绝不是「静默变空」、也不是崩。"""
        self._new_jd()
        _analysis("r_bad", JD_A, "2026-10-01 09:00", report="r_bad.html")
        store.write_text(os.path.join(store.p_path("reports"), "r_bad.html"), "<html>没有快照</html>")
        b = _Builder(_nodes("computed"))
        nodes, source = jdfacts.resolve(JD_A, "JD 原文-A", CFG, builder=b)
        self.assertEqual(len(b.calls), 1)
        self.assertEqual(source, "computed")
        self.assertEqual(_jdump(nodes), _jdump(_nodes("computed")))
        self.assertNotEqual(nodes, [])
        self.assertEqual(_jdump(_raw_facts(JD_A)["knowledge"]), _jdump(_nodes("computed")))
        self.assertEqual(_raw_facts(JD_A)["knowledge_source"], "computed")

    def test_another_jd_never_reuses_or_seeds_from_the_first(self):
        """**绝不误用别人的缓存 / 别人的报告**（ID 级隔离）。"""
        self._new_jd(JD_A)
        self._new_jd(JD_B)
        bA = _Builder(_nodes("A"))
        self.assertEqual(jdfacts.resolve(JD_A, "JD 原文-A", CFG, builder=bA)[1], "computed")
        _analysis("rA", JD_A, "2026-10-01 09:00", report="rA.html")
        _write_report("rA.html", {"report_id": "rA", "knowledge": _nodes("A-report")})
        bB = _Builder(_nodes("B"))
        nodes, source = jdfacts.resolve(JD_B, "JD 原文-B", CFG, builder=bB)
        self.assertEqual(len(bB.calls), 1, "B 直接拿了 A 的缓存（builder 一次都没调）")
        self.assertEqual(source, "computed")
        self.assertEqual(_jdump(nodes), _jdump(_nodes("B")))
        self.assertEqual(_jdump(_raw_facts(JD_B)["knowledge"]), _jdump(_nodes("B")))
        self.assertNotEqual(_jdump(_raw_facts(JD_B)["knowledge"]), _jdump(_nodes("A")))
        self.assertNotEqual(_jdump(_raw_facts(JD_B)["knowledge"]), _jdump(_nodes("A-report")))
        # A 的缓存一个字节都没被 B 写坏
        self.assertEqual(_jdump(_raw_facts(JD_A)["knowledge"]), _jdump(_nodes("A")))

    def test_b_is_not_seeded_from_as_report_when_bs_own_report_has_nothing(self):
        """播种侧的 ID 校验：只认「该 JD 自己的分析」。"""
        self._new_jd(JD_A)
        self._new_jd(JD_B)
        _analysis("rA", JD_A, "2026-10-01 09:00", report="rA.html")
        _write_report("rA.html", {"report_id": "rA", "knowledge": _nodes("A-report")})
        _analysis("rB", JD_B, "2026-10-02 09:00", report="rB.html")
        store.write_text(os.path.join(store.p_path("reports"), "rB.html"), "<html>没有快照</html>")
        bB = _Builder(_nodes("B"))
        nodes, source = jdfacts.resolve(JD_B, "JD 原文-B", CFG, builder=bB)
        self.assertEqual((source, len(bB.calls)), ("computed", 1))
        self.assertEqual(_jdump(nodes), _jdump(_nodes("B")))
        self.assertIsNone(_raw_facts(JD_B).get("seeded_at"))

    # ---- 强制刷新 ----
    def test_refresh_ignores_cache_and_seed_and_moves_the_generated_at(self):
        self._new_jd()
        _analysis("r1", JD_A, "2026-10-01 09:00")
        _write_report("r1.html", {"report_id": "r1", "knowledge": _nodes("report")})
        b1 = _Builder(_nodes("seeded-era"))
        with mock.patch.object(jdfacts, "_now", return_value="2026-10-03 00:35"):
            self.assertEqual(jdfacts.resolve(JD_A, "JD 原文-A", CFG, builder=b1)[1], "seeded:r1")
        # 刷新：既不看缓存、也不看报告，直接真算
        b2 = _Builder(_nodes("refreshed"))
        with mock.patch.object(jdfacts, "_now", return_value="2026-11-01 12:00"):
            nodes, source = jdfacts.resolve(JD_A, "JD 原文-A", CFG, refresh=True, builder=b2)
        self.assertEqual((source, len(b2.calls)), ("computed", 1))
        self.assertEqual(_jdump(nodes), _jdump(_nodes("refreshed")))
        facts = _raw_facts(JD_A)
        self.assertEqual(_jdump(facts["knowledge"]), _jdump(_nodes("refreshed")))
        self.assertEqual(facts["knowledge_source"], "computed")
        self.assertEqual(facts["knowledge_at"], "2026-11-01 12:00",
                         "强制刷新后 knowledge_at 还是旧的那份（报告会显示错的时间）")
        evs = _events("analyze.knowledge_computed")
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["data"]["refresh"], True)
        self.assertEqual(evs[0]["data"]["replaced_at"], "2026-10-03 00:35")
        # 刷新后再分析一次：又回到复用（不是每次都真算）
        n4, s4 = jdfacts.resolve(JD_A, "JD 原文-A", CFG, builder=b2)
        self.assertEqual((s4, len(b2.calls), _jdump(n4)), ("cache", 1, _jdump(_nodes("refreshed"))))

    def test_refresh_without_cache_still_computes_and_stamps_one_time(self):
        self._new_jd()
        b = _Builder(_nodes("first"))
        with mock.patch.object(jdfacts, "_now", return_value="2026-10-03 00:35"):
            nodes, source = jdfacts.resolve(JD_A, "JD 原文-A", CFG, refresh=True, builder=b)
        self.assertEqual((source, len(b.calls)), ("computed", 1))
        self.assertEqual(_raw_facts(JD_A)["knowledge_at"], "2026-10-03 00:35")
        self.assertEqual(_events("analyze.knowledge_computed")[0]["data"]["replaced_at"], "")

    # ---- 输入 / 身份校验 ----
    def test_unknown_jd_raises_before_anything_is_computed(self):
        """身份先校验：**绝不**在一个不存在的岗位上空烧 53 次搜索，最后才写盘失败。"""
        b = _Builder()
        for refresh in (False, True):
            with self.subTest(refresh=refresh):
                with self.assertRaises(LookupError):
                    jdfacts.resolve(JD_A, "JD 原文", CFG, refresh=refresh, builder=b)
        self.assertEqual(b.calls, [])
        self.assertFalse(os.path.exists(store.p_path("jds.json")))

    def test_missing_builder_raises_and_writes_nothing(self):
        self._new_jd()
        with self.assertRaises(ValueError):
            jdfacts.resolve(JD_A, "JD 原文-A", CFG, builder=None)
        self.assertNotIn("knowledge", _raw_facts(JD_A) or {})
        self.assertEqual(_events("analyze.knowledge_computed"), [])

    def test_the_three_sources_are_exactly_these_strings(self):
        """报告要照着这三个值写来源标记（Task 2），所以它们本身就是契约。"""
        self._new_jd()
        b = _Builder(_nodes("x"))
        self.assertEqual(jdfacts.resolve(JD_A, "JD", CFG, builder=b)[1], "computed")
        self.assertEqual(jdfacts.resolve(JD_A, "JD", CFG, builder=b)[1], "cache")
        self.assertEqual(jdfacts.SEEDED_PREFIX, "seeded:")
        self.assertEqual(jdfacts.SOURCE_CACHE, "cache")
        self.assertEqual(jdfacts.SOURCE_COMPUTED, "computed")

    def test_a_builder_that_returns_nothing_saves_nothing_usable(self):
        """builder 返回空 → 报告里就是空知识地图（诚实），但**不写**一份假缓存。"""
        self._new_jd()
        b = _Builder([])
        nodes, source = jdfacts.resolve(JD_A, "JD 原文-A", CFG, builder=b)
        self.assertEqual((nodes, source), ([], "computed"))
        self.assertEqual(_raw_facts(JD_A)["knowledge"], [])
        self.assertIsNone(jdfacts.knowledge_of(JD_A))              # 空缓存不算「有缓存」
        self.assertTrue(jdfacts.needs_compute(JD_A, False))

    def test_a_failing_builder_leaves_no_half_written_cache(self):
        self._new_jd()

        def boom(jd_text, cfg):
            raise RuntimeError("模型/搜索炸了")

        with self.assertRaises(RuntimeError):
            jdfacts.resolve(JD_A, "JD 原文-A", CFG, builder=boom)
        self.assertNotIn("knowledge", _raw_facts(JD_A) or {})
        self.assertEqual(_events("analyze.knowledge_computed"), [])

    # ---- 磁盘是唯一真相源 ----
    def test_caller_mutation_of_returned_nodes_cannot_corrupt_the_cache(self):
        self._new_jd()
        b = _Builder(_nodes("first"))
        jdfacts.resolve(JD_A, "JD 原文-A", CFG, builder=b)
        n2, _ = jdfacts.resolve(JD_A, "JD 原文-A", CFG, builder=b)
        n2[0]["term"] = "被调用方改坏了"
        n2.append({"term": "塞进去的"})
        self.assertEqual(_jdump(_raw_facts(JD_A)["knowledge"]), _jdump(_nodes("first")))
        n3, s3 = jdfacts.resolve(JD_A, "JD 原文-A", CFG, builder=b)
        self.assertEqual((s3, _jdump(n3)), ("cache", _jdump(_nodes("first"))))

    def test_resolve_only_writes_the_jd_library(self):
        self._new_jd()
        jdfacts.resolve(JD_A, "JD 原文-A", CFG, builder=_Builder())
        self.assertFalse(os.path.exists(store.jobs_path()))
        self.assertFalse(os.path.exists(store.knowledge_path()))


# ---------------------------------------------------------------- ④ 架构铁律
class ModuleHygieneTest(IsolatedCase):
    def test_it_never_imports_knowledge(self):
        """`knowledge.build_for_jd` 会**真联网搜索**：本模块绝不能 import 它。

        真算那一步由调用方注入（生产里 app.py 传 `knowledge.build_for_jd`），
        这样测试里永远不会发生真搜索 —— 也永远不会烧掉用户的搜索额度。
        """
        src = store.read_text(os.path.join(store.ROOT, "jdfacts.py"))
        self.assertTrue(src)
        self.assertIsNone(re.search(r"^\s*(import|from)\s+knowledge\b", src, re.M))
        self.assertIsNone(re.search(r"^\s*(import|from)\s+search\b", src, re.M))
        self.assertFalse(hasattr(jdfacts, "knowledge"))
        self.assertFalse(hasattr(jdfacts, "search"))

    def test_it_imports_nothing_but_stdlib_and_local_modules(self):
        """零第三方依赖：import 名单必须是白名单内的（多一个就会在用户机器上 ImportError）。"""
        tree = ast.parse(store.read_text(os.path.join(store.ROOT, "jdfacts.py")))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names.add((node.module or "").split(".")[0])
        self.assertTrue(names, "一个 import 都没找到？解析方式不对")
        self.assertEqual(names - {"os", "datetime", "jdid", "kb", "log", "store"}, set())

    def test_it_takes_only_the_jd_lock(self):
        """锁顺序恒为 `_JD_LOCK` → `_KB_LOCK`：本模块**只**碰前者，绝不反向。

        判据用 **AST**（不是正则）：说明文字里提到 `kb._KB_LOCK` 是应该的（铁律就写在文档里），
        要抓的是**代码里真的去拿它**。
        """
        src = store.read_text(os.path.join(store.ROOT, "jdfacts.py"))
        self.assertIn("jdid._JD_LOCK", src)
        tree = ast.parse(src)
        used = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        used |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        self.assertNotIn("_KB_LOCK", used)


if __name__ == "__main__":
    unittest.main()
