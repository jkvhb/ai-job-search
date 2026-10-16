# -*- coding: utf-8 -*-
"""jdfacts（JD 级事实缓存：知识地图只算一次）的测试。

**这块的价值来自用户真实日志**：

    analyze.knowledge_done  {"ms": 206206, "nodes": 53}    ← 知识地图：206 秒、53 次搜索
    analyze.success         {"ms": 236186}                 ← 一次分析总计 236 秒

知识地图占一次分析 87% 的时间与**全部** ~53 次 Tavily 搜索额度，而它是**纯 JD 级**的
（"这个岗位要会什么"，与哪份简历无关）。所以「同一个 JD 只算一次、换简历直接复用」就是全部收益。

**本文件的四条硬纪律**（每一条都对应本项目抓到过的假绿）：
1. 「复用了缓存」**必须同时**断言注入的 builder 调用次数为 **0** —— 只断言"返回了节点"是恒真的；
2. 「播种」同理：断言没调 builder、`source == "seeded:<报告id>"`、且节点与报告里那份**逐字相同**；
3. 落盘断言一律看**原始 jds.json**（`_raw_jd`），不看 `get()` 的返回值 —— 只有真写进文件才算数
   （「加字段型」变异会被读侧净化，只有钉住落盘那一行才抓得住）。
4. **并发测试必须用 `Event` 做确定性同步，绝不能 sleep**：服务是 ThreadingHTTPServer，双击 /
   双标签页 / 重试都会让同一个 JD 被两个线程同时 resolve（= 2×206 秒 + 2×53 次搜索）。
   sleep 型的并发测试既可能假绿（第二个线程其实没赶上窗口）也可能假红（机器快慢），
   所以「等待者已经就位」由**事件**（注入的 `_join_inflight` 探针）报告，不靠猜。

**绝不联网 / 绝不真调模型**：真算那一步由测试注入的假 builder 承担；生产里由 app.py 注入
`knowledge.build_for_jd`（本模块刻意不 import knowledge，见 `ModuleHygieneTest`）。
"""
import ast
import json
import os
import re
import threading
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
class GetAndStoreTest(IsolatedCase):
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

    def test_store_writes_through_to_the_real_file(self):
        jdid.upsert_jd({"id": JD_A})
        with mock.patch.object(jdfacts, "_now", return_value="2026-10-03 00:35"):
            jdfacts._store(JD_A, _nodes("seed"), "computed")
        facts = _raw_facts(JD_A)                                  # ← 落盘那一行，不是读侧
        self.assertEqual(_jdump(facts["knowledge"]), _jdump(_nodes("seed")))
        self.assertEqual(facts["knowledge_at"], "2026-10-03 00:35")
        self.assertEqual(facts["knowledge_source"], "computed")
        self.assertEqual(jdfacts.get(JD_A)["knowledge_at"], "2026-10-03 00:35")
        self.assertEqual(_jdump(jdfacts.knowledge_of(JD_A)), _jdump(_nodes("seed")))

    def test_store_keeps_the_first_generated_at(self):
        """**首次生成时间保留**：knowledge_at 是「这份知识地图第一次生成于何时」。

        覆盖掉它就等于把「这份地图有多旧」这条信息弄丢（复用时要展示给用户看）。
        时钟必须**注入**：真实实现是分钟粒度，同一分钟内两次写入本来就是同一个字符串，
        不注入的话这条断言会靠巧合恒真（正是本项目抓过的假绿形态）。

        ⚠️ 只有**旧缓存是非空列表**时那条时间戳才叫「某份地图的生成时间」才值得保留
        （见下面两条：空列表 / 孤儿时间戳一律不沿用）。
        """
        jdid.upsert_jd({"id": JD_A, "jd_facts": {"knowledge": _nodes("第一次"),
                                                "knowledge_at": "2020-01-01 08:00"}})
        with mock.patch.object(jdfacts, "_now", return_value="2026-11-01 12:00"):
            jdfacts._store(JD_A, _nodes("第二次"), "seeded:r9")
        facts = _raw_facts(JD_A)
        self.assertEqual(facts["knowledge_at"], "2020-01-01 08:00")     # 旧的时间没被动
        self.assertEqual(_jdump(facts["knowledge"]), _jdump(_nodes("第二次")))   # 内容换了
        self.assertEqual(facts["knowledge_source"], "seeded:r9")
        # 第二次：先写一次（t1），再写一次（t2）→ knowledge_at 必须还是 t1
        jdid.upsert_jd({"id": JD_B})
        with mock.patch.object(jdfacts, "_now", return_value="2026-10-03 00:35"):
            jdfacts._store(JD_B, _nodes("first"), "computed")
        with mock.patch.object(jdfacts, "_now", return_value="2026-12-31 23:59"):
            jdfacts._store(JD_B, _nodes("second"), "computed")
        self.assertEqual(_raw_facts(JD_B)["knowledge_at"], "2026-10-03 00:35")
        self.assertEqual(_jdump(_raw_facts(JD_B)["knowledge"]), _jdump(_nodes("second")))

    def test_a_saved_map_never_inherits_a_timestamp_from_an_empty_map(self):
        """空列表**不是可用的缓存**（既有约定，有测试钉住）→ 它的时间戳也不该被沿用。

        不修的话：builder 返回 `[]` 落盘成 `knowledge: []` + 一个时间戳；下次真算出一份
        **非空**地图时却沿用那份空地图的时间戳 → 报告里的「生成于」是错的时间（而且旧得离谱）。
        """
        jdid.upsert_jd({"id": JD_A, "jd_facts": {"knowledge": [], "knowledge_at": "2020-01-01 08:00"}})
        with mock.patch.object(jdfacts, "_now", return_value="2026-11-01 12:00"):
            jdfacts._store(JD_A, _nodes("新"), "computed")
        facts = _raw_facts(JD_A)
        self.assertEqual(facts["knowledge_at"], "2026-11-01 12:00",
                         "新地图沿用了那份空地图的时间戳（报告会显示错的时间）")
        self.assertEqual(_jdump(facts["knowledge"]), _jdump(_nodes("新")))
        self.assertEqual(facts["knowledge_source"], "computed")

    def test_a_saved_map_never_inherits_an_orphan_timestamp(self):
        """`knowledge_at` 有值但**没有**地图（用户手改 / 迁移留下的孤儿）→ 同样不沿用。"""
        jdid.upsert_jd({"id": JD_A, "jd_facts": {"knowledge_at": "2020-01-01 08:00"}})
        with mock.patch.object(jdfacts, "_now", return_value="2026-11-01 12:00"):
            jdfacts._store(JD_A, _nodes("新"), "computed")
        self.assertEqual(_raw_facts(JD_A)["knowledge_at"], "2026-11-01 12:00")

    def test_store_stamps_a_time_only_when_there_was_none(self):
        jdid.upsert_jd({"id": JD_A})
        with mock.patch.object(jdfacts, "_now", return_value="2026-10-03 00:35"):
            self.assertEqual(jdfacts._store(JD_A, _nodes("x"), "computed")["knowledge_at"],
                             "2026-10-03 00:35")

    def test_store_keeps_other_facts_keys(self):
        """只动三个键：`seeded_at`（以及用户/将来加的键）必须原样留着。"""
        jdid.upsert_jd({"id": JD_A, "jd_facts": {"seeded_at": "2026-09-01 10:00", "别的": [1]}})
        jdfacts._store(JD_A, _nodes("x"), "computed")
        facts = _raw_facts(JD_A)
        self.assertEqual(facts["seeded_at"], "2026-09-01 10:00")
        self.assertEqual(facts["别的"], [1])
        self.assertEqual(facts["knowledge_source"], "computed")

    def test_store_accepts_the_nodes_dict_form(self):
        """容错：`{"nodes": [...]}`（旧报告/手写数据里出现过这种形态，kb 也认）→ 存成列表。"""
        jdid.upsert_jd({"id": JD_A})
        jdfacts._store(JD_A, {"nodes": _nodes("dict")}, "computed")
        self.assertEqual(_jdump(_raw_facts(JD_A)["knowledge"]), _jdump(_nodes("dict")))

    def test_store_of_an_unknown_jd_raises_and_creates_no_file(self):
        with self.assertRaises(LookupError):
            jdfacts._store(JD_A, _nodes("x"), "computed")
        self.assertFalse(os.path.exists(store.p_path("jds.json")),
                         "给不存在的岗位存缓存时居然凭空造出了一个 jds.json")

    def test_store_rejects_garbage_without_writing(self):
        jdid.upsert_jd({"id": JD_A})
        before = store.read_text(store.p_path("jds.json"), None)
        for junk in ("不是列表", 5, None, {"没有nodes": 1}, {"nodes": "不是列表"}):
            with self.subTest(junk=junk):
                with self.assertRaises(ValueError):
                    jdfacts._store(JD_A, junk, "computed")
                self.assertEqual(store.read_text(store.p_path("jds.json"), None), before)

    def test_store_rejects_a_missing_source_instead_of_writing_an_empty_one(self):
        """`knowledge_source` 是契约的一部分（报告照着它写来源标记）→ **不许静默写成 ""**。

        审查 Nit：`_text(source) or ""` 会把「调用方传错了」这种 bug 悄悄落盘成一个空来源。
        """
        jdid.upsert_jd({"id": JD_A})
        before = store.read_text(store.p_path("jds.json"), None)
        for junk in (None, "", "   ", 5, [], {}):
            with self.subTest(source=junk):
                with self.assertRaises(ValueError):
                    jdfacts._store(JD_A, _nodes("x"), junk)
                self.assertEqual(store.read_text(store.p_path("jds.json"), None), before)
        # 反向对照：合法来源照旧写得进去
        jdfacts._store(JD_A, _nodes("x"), " seeded:r1 ")
        self.assertEqual(_raw_facts(JD_A)["knowledge_source"], "seeded:r1")

    def test_store_allows_an_empty_list_but_it_is_not_a_usable_cache(self):
        """空列表是合法的「真算结果」（这个 JD 没抽出知识点），但不是可用的缓存。"""
        jdid.upsert_jd({"id": JD_A})
        jdfacts._store(JD_A, [], "computed")
        self.assertEqual(_raw_facts(JD_A)["knowledge"], [])
        self.assertIsNone(jdfacts.knowledge_of(JD_A))

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
            jdfacts._store(JD_A, _nodes("x"), "computed")
        self.assertTrue(seen, "整个写路径居然没读过 jds.json，这条测试就没验证到东西")
        self.assertEqual(seen, [True], "读 jd_facts 时没有持 _JD_LOCK：%s" % seen)


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
        # 两个字段**不是**一回事：`seeded_at` 是「这次播种发生在何时」，
        # `knowledge_at` 是「这份地图**真正生成**于何时」= 那份报告的 `created_at`。
        # 写成同一个时间的话，报告里的小字会显示一个**晚于报告本身**的生成日期。
        self.assertEqual(facts["seeded_at"], "2026-10-20 09:00")
        self.assertEqual(facts["knowledge_at"], "2026-10-03 00:30",
                         "播种把 knowledge_at 写成了「播种那一刻」而不是报告的生成时间")
        self.assertNotEqual(facts["knowledge_at"], facts["seeded_at"],
                            "两个字段退化成一个了（报告里会显示晚于报告本身的日期）")
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

    def test_a_stale_timestamp_without_a_map_does_not_survive_a_seed(self):
        """有 `knowledge_at` 但地图是**空列表** → 那不是「某份地图的生成时间」，不沿用。

        播种必须给出这份地图**真正**的生成时间（那份报告的 `created_at`），
        既不是那个孤儿时间戳、也不是「播种那一刻」。
        """
        jdid.upsert_jd({"id": JD_A, "jd_facts": {"knowledge": [], "knowledge_at": "2020-01-01 08:00"}})
        _analysis("r1", JD_A, "2026-10-01 09:00")
        _write_report("r1.html", {"report_id": "r1", "knowledge": _nodes("report")})
        with mock.patch.object(jdfacts, "_now", return_value="2026-11-01 12:00"):
            self.assertEqual(jdfacts.seed_from_reports(JD_A), (True, "r1"))
        facts = _raw_facts(JD_A)
        self.assertEqual(facts["knowledge_at"], "2026-10-01 09:00")     # ← 报告真正生成的时间
        self.assertEqual(facts["seeded_at"], "2026-11-01 12:00")        # ← 播种那一刻

    def test_seeding_never_overwrites_a_usable_cache(self):
        """**播种写守卫**：进临界区后重判「此刻已经有可用缓存」→ 放弃这次播种写。

        窄窗口：A 的真算刚落盘、B 的播种才进临界区。没有这道守卫的话，B 会把 knowledge
        换成旧报告那份、source 换成 `seeded:<id>`，而 `knowledge_at` 保留 A 的时间 →
        **内容与时间戳对不上**（报告拿 A 的时间显示 B 的内容）。
        """
        jdid.upsert_jd({"id": JD_A})
        jdfacts._store(JD_A, _nodes("computed"), "computed")     # A 的真算已经落盘
        _analysis("r1", JD_A, "2026-10-01 09:00")
        _write_report("r1.html", {"report_id": "r1", "knowledge": _nodes("report")})
        before = _raw_facts(JD_A)
        self.assertEqual(jdfacts.seed_from_reports(JD_A), (False, None),
                         "已经有真算落盘了，播种还报告「播成功」")
        facts = _raw_facts(JD_A)
        self.assertEqual(_jdump(facts["knowledge"]), _jdump(_nodes("computed")),
                         "播种用旧报告覆盖了刚落盘的真算")
        self.assertEqual(facts["knowledge_source"], "computed")
        self.assertEqual(facts["knowledge_at"], before["knowledge_at"])
        self.assertNotIn("seeded_at", facts)
        self.assertEqual(_events("analyze.knowledge_seeded"), [])
        self.assertEqual(len(_events("analyze.knowledge_seed_skipped")), 1)
        # 反向对照：同一份报告在**没有**缓存时照样播得进去
        # （证明上面那个 False 不是因为「报告根本读不出来」）
        jdid.upsert_jd({"id": JD_B})
        _analysis("rB", JD_B, "2026-10-01 09:00")
        _write_report("rB.html", {"report_id": "rB", "knowledge": _nodes("report")})
        self.assertEqual(jdfacts.seed_from_reports(JD_B), (True, "rB"))
        self.assertEqual(_raw_facts(JD_B)["knowledge_source"], "seeded:rB")

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
        # 播种那份地图的生成时间是**报告自己的**（`replaced_at` 读的就是落盘的 knowledge_at）
        self.assertEqual(_raw_facts(JD_A)["knowledge_at"], "2026-10-01 09:00")
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
        self.assertEqual(evs[0]["data"]["replaced_at"], "2026-10-01 09:00")
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

    def test_an_empty_map_does_not_lend_its_timestamp_to_the_next_compute(self):
        """空地图不算可用缓存（既有约定）→ 它落盘的时间戳也不许被下一份**非空**地图沿用。

        不修的话：第二次真算出来的地图会挂着那份空地图的生成时间，报告里显示错的时间。
        """
        self._new_jd()
        with mock.patch.object(jdfacts, "_now", return_value="2026-10-03 00:35"):
            self.assertEqual(jdfacts.resolve(JD_A, "JD 原文-A", CFG, builder=_Builder([])),
                             ([], "computed"))
        self.assertEqual(_raw_facts(JD_A)["knowledge_at"], "2026-10-03 00:35")
        with mock.patch.object(jdfacts, "_now", return_value="2026-11-01 12:00"):
            nodes, source = jdfacts.resolve(JD_A, "JD 原文-A", CFG, builder=_Builder(_nodes("second")))
        self.assertEqual((source, _jdump(nodes)), ("computed", _jdump(_nodes("second"))))
        self.assertEqual(_raw_facts(JD_A)["knowledge_at"], "2026-11-01 12:00",
                         "新地图沿用了那份空地图的时间戳（报告会显示错的生成时间）")

    def test_a_cache_that_appears_while_seeding_is_not_overwritten(self):
        """**窄窗口**（审查原文）：`resolve` 在第①级看到「没缓存」，但播种写进临界区之前
        另一个线程刚把真算落盘了 —— 这次播种写必须**放弃**。

        这里用「读报告」这一步注入那次并发写入（它是播种路径上必然发生的一步）：
        确定性，不靠 sleep。没有守卫的话 source 会变成 `seeded:r1`、内容变成旧报告那份、
        而 `knowledge_at` 还是真算那份的时间 → 内容与时间戳对不上。
        """
        self._new_jd()
        _analysis("r1", JD_A, "2026-10-01 09:00")
        _write_report("r1.html", {"report_id": "r1", "knowledge": _nodes("report")})
        real_read = kb.read_report_data

        def read_then_race(path):
            data = real_read(path)
            jdfacts._store(JD_A, _nodes("computed"), "computed")   # 另一个线程刚算完
            return data

        b = _Builder(_nodes("绝不该出现"))
        with mock.patch.object(kb, "read_report_data", read_then_race):
            nodes, source = jdfacts.resolve(JD_A, "JD 原文-A", CFG, builder=b)
        self.assertEqual(b.calls, [], "播种路径居然调了 builder（= 真搜索）")
        self.assertEqual(source, "cache", "播种写覆盖了刚落盘的真算：%r" % (source,))
        self.assertEqual(_jdump(nodes), _jdump(_nodes("computed")))
        facts = _raw_facts(JD_A)
        self.assertEqual(_jdump(facts["knowledge"]), _jdump(_nodes("computed")))
        self.assertEqual(facts["knowledge_source"], "computed")
        self.assertNotIn("seeded_at", facts)
        self.assertEqual(len(_events("analyze.knowledge_seed_skipped")), 1)

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


# ---------------------------------------------------------------- ④ 单飞（同一个 JD 只许算一次）
class SingleFlightTest(IsolatedCase):
    """服务是 `ThreadingHTTPServer`：双击 / 双标签页 / 重试会让同一个 JD 被并发 resolve。

    一次真算 = 206 秒 + ~53 次搜索额度，所以**同一个 JD 同一时刻只许算一次**，
    第二个线程等它算完**直接拿它的结果**。

    **为什么这里的同步全是 `Event`**：sleep 型的并发测试既可能假绿（第二个线程其实没赶上
    窗口、悄悄走了缓存，看起来也"只算了一次"）也可能假红（机器快慢）。本类里的确定性来自：
      * 领导者被 builder 里的 `release` 卡住 —— 它跑完之前盘上**没有**缓存（第①级必然落空）；
      * 等待者「进入等待」由注入的 `_join_inflight` 探针报告（那是个真同步点，不是猜的）；
      * 所以「两个线程同时 resolve」是被事件钉住的，不是靠时间差碰运气。
    """

    # 超时类用例的**有界等待**：正确实现在 0.5 秒内就退出了，这里给 5 秒余量。
    # ⚠️ 它的作用不是「等它回来」，而是「回来后断言它必须已经结束」—— 旧实现（无限 wait）上
    # 等待者永远不结束，红就出在 assertFalse(is_alive) 那一行，而不会把整个套件拖死。
    HANG_GRACE_SECONDS = 5.0

    def _new_jd(self, jd_id=JD_A):
        jdid.upsert_jd({"id": jd_id, "seq": 1})
        return jd_id

    def _run_pair(self, builder, jd_id=JD_A):
        """两个线程并发 `resolve` 同一个 JD；返回 (结果, 异常, builder 调用序列)。

        先起 t1（它会卡在 builder 里），等到**确认 t2 已经进入单飞等待**才放行 t1。
        """
        self._new_jd(jd_id)
        results, errors, calls = {}, {}, []
        entered, release, waiting = threading.Event(), threading.Event(), threading.Event()

        def wrapped(jd_text, cfg):
            calls.append(threading.current_thread().name)
            return builder(jd_text, cfg)

        def worker(name):
            try:
                results[name] = jdfacts.resolve(jd_id, "JD 原文-A", CFG, builder=wrapped)
            except BaseException as e:                     # noqa: BLE001 - 失败也要能被断言看到
                errors[name] = e

        real_join = jdfacts._join_inflight

        def watch_join(flight):
            waiting.set()
            return real_join(flight)

        self._entered, self._release = entered, release
        with mock.patch.object(jdfacts, "_join_inflight", watch_join):
            t1 = threading.Thread(target=worker, args=("t1",), name="t1")
            t1.start()
            self.assertTrue(entered.wait(30),
                            "第一个线程没进 builder（t1 的异常 / 结果：%r / %r）" % (errors, results))
            t2 = threading.Thread(target=worker, args=("t2",), name="t2")
            t2.start()
            self.assertTrue(waiting.wait(30),
                            "第二个线程没有进入单飞等待（它自己又去算了一遍）")
            release.set()
            t1.join(30)
            t2.join(30)
        self.assertFalse(t1.is_alive() or t2.is_alive(), "有线程没跑完（死等了）")
        # 登记必须注销干净：漏一份就会让这个 JD 后面的请求一直去等一个不会再有结果的信号
        self.assertEqual(jdfacts._INFLIGHT, {}, "单飞登记没注销干净")
        return results, errors, calls

    def test_two_threads_on_one_jd_compute_once_and_share_the_result(self):
        def builder(jd_text, cfg):
            self._entered.set()
            self.assertTrue(self._release.wait(30), "测试自己没放行 builder")
            return json.loads(json.dumps(_nodes("once"), ensure_ascii=False))

        results, errors, calls = self._run_pair(builder)
        self.assertEqual(errors, {})
        self.assertEqual(calls, ["t1"],
                         "同一个 JD 被真算了 %d 次（= 烧了 %d 份 206 秒 + 搜索额度）"
                         % (len(calls), len(calls)))
        n1, s1 = results["t1"]
        n2, s2 = results["t2"]
        # 等待者拿的是**领导者刚算出来的那份**（不是空地图、不是又一次真算）；但它的 `source`
        # 按**复用**报：那一行小字的全部意义是「本次有没有重新搜索」，而等待者确实没有重新
        # 搜索 —— 报「本次新算」会让用户以为烧了两遍 53 次搜索额度（而那正是这一行要传达的
        # 信息）。「这是等待者」由下面的 `analyze.knowledge_joined` 事件分辨。
        self.assertEqual((s1, s2), ("computed", "cache"),
                         "第二个线程没按「等待复用」报：%r" % ((s1, s2),))
        self.assertEqual(s2, jdfacts.SOURCE_CACHE)
        self.assertEqual(_jdump(n2), _jdump(n1), "两个线程拿到的不是同一份知识地图")
        self.assertEqual(_jdump(n1), _jdump(_nodes("once")))
        self.assertIsNot(n1, n2,
                         "两个线程共享了同一个列表对象：谁就地改一下（kb.absorb 就会）就污染另一个")
        facts = _raw_facts(JD_A)                                    # ← 真落盘的那一行
        self.assertEqual(_jdump(facts["knowledge"]), _jdump(_nodes("once")))
        self.assertEqual(facts["knowledge_source"], "computed")
        self.assertEqual(len(_events("analyze.knowledge_computed")), 1)
        joined = _events("analyze.knowledge_joined")
        self.assertEqual(len(joined), 1, "等待者没有走「加入在飞的那一次」这条路")
        self.assertEqual(joined[0]["data"]["jd"], JD_A)
        self.assertEqual(joined[0]["data"]["nodes"], 2)

    def test_eight_threads_on_one_jd_compute_once_and_share_one_result(self):
        """并发 **8** 个线程请求同一个 JD ⇒ builder 只被调 **1** 次、8 个线程拿到**同一份**结果。

        2 个线程那条钉的是「等待者」这一条路，这条钉的是**规模**：7 个等待者同时挂上来时，
        登记表的「先来者当领导、后来者等它」必须仍然只算一次（真算 = 206 秒 + ~53 次搜索额度）。
        """
        total = 8
        self._new_jd()
        calls, results, errors, waits = [], {}, {}, []
        entered, release, all_waiting = threading.Event(), threading.Event(), threading.Event()

        def builder(jd_text, cfg):
            calls.append(threading.current_thread().name)
            entered.set()
            self.assertTrue(release.wait(30), "测试自己没放行 builder")
            return json.loads(json.dumps(_nodes("shared"), ensure_ascii=False))

        def worker(i):
            name = "w%d" % i
            try:
                results[name] = jdfacts.resolve(JD_A, "JD 原文-A", CFG, builder=builder)
            except BaseException as e:                     # noqa: BLE001 - 异常也要能被断言看到
                errors[name] = e

        real_join = jdfacts._join_inflight

        def watch_join(flight):
            waits.append(threading.current_thread().name)
            if len(waits) >= total - 1:
                all_waiting.set()
            return real_join(flight)

        threads = [threading.Thread(target=worker, args=(i,), name="w%d" % i, daemon=True)
                   for i in range(total)]
        with mock.patch.object(jdfacts, "_join_inflight", watch_join):
            threads[0].start()
            self.assertTrue(entered.wait(30), "领导者没进 builder（这条测试没验证到东西）")
            for t in threads[1:]:
                t.start()
            # 7 个等待者必须**都**已经等在那次在飞的真算上，才放行领导者（否则会有线程晚到、
            # 在登记注销之后自己抢领导权 → 真算两次，测试就变成靠时间差碰运气了）。
            self.assertTrue(all_waiting.wait(30),
                            "只有 %d/%d 个线程进入了单飞等待" % (len(waits), total - 1))
            release.set()
            for t in threads:
                t.join(30)
        self.assertEqual([t.name for t in threads if t.is_alive()], [], "有线程没跑完（死等了）")
        self.assertEqual(jdfacts._INFLIGHT, {}, "单飞登记没注销干净")
        self.assertEqual(errors, {}, "有线程抛异常了")
        self.assertEqual(sorted(results), sorted("w%d" % i for i in range(total)))
        self.assertEqual(calls, ["w0"],
                         "同一个 JD 被真算了 %d 次（= 烧了 %d 份 206 秒 + 搜索额度）"
                         % (len(calls), len(calls)))
        payloads = {}
        for name in sorted(results):
            nodes, source = results[name]
            payloads[name] = _jdump(nodes)
            self.assertEqual(source, "computed" if name == "w0" else jdfacts.SOURCE_CACHE,
                             "%s 的来源报错了：%r" % (name, source))
        self.assertEqual(set(payloads.values()), {_jdump(_nodes("shared"))},
                         "8 个线程拿到的不是同一份知识地图：%r" % (payloads,))
        self.assertEqual(len({id(nodes) for nodes, _ in results.values()}), total,
                         "有线程共享了同一个列表对象：谁就地改一下（kb.absorb 就会）就污染别人")
        self.assertEqual(len(_events("analyze.knowledge_joined")), total - 1)
        self.assertEqual(len(_events("analyze.knowledge_computed")), 1)

    def test_a_waiting_thread_does_not_hang_when_the_leader_fails(self):
        """领导者**失败**（builder 抛异常）时，等待者**绝不死等**。

        选定的行为：失败的那份登记被摘掉，等待者醒来发现没有结果 → 自己抢领导权再算一次。
        这里钉住三样：领导者确实抛了 RuntimeError（不是静默成功）、等待者拿到了真结果、
        builder 一共被调 **2** 次（不是死等不动，也不是「只算一次就返回一份空地图」）。
        """
        calls = []

        def builder(jd_text, cfg):
            calls.append(1)
            if len(calls) == 1:                       # 领导者：卡到测试确认等待者就位，再炸
                self._entered.set()
                self.assertTrue(self._release.wait(30), "测试自己没放行 builder")
                raise RuntimeError("模型/搜索炸了")
            return json.loads(json.dumps(_nodes("after-failure"), ensure_ascii=False))

        results, errors, threaded_calls = self._run_pair(builder)
        self.assertEqual(len(calls), 2)
        self.assertIsInstance(errors.get("t1"), RuntimeError,
                              "领导者居然没失败（这条测试没验证到东西）：%r" % (errors,))
        self.assertNotIn("t2", errors, "等待者被领导者的失败带死了：%r" % (errors.get("t2"),))
        self.assertEqual(threaded_calls, ["t1", "t2"], "等待者没有自己再算一次")
        n2, s2 = results["t2"]
        self.assertEqual(s2, "computed")
        self.assertEqual(_jdump(n2), _jdump(_nodes("after-failure")))
        facts = _raw_facts(JD_A)
        self.assertEqual(_jdump(facts["knowledge"]), _jdump(_nodes("after-failure")))
        self.assertEqual(facts["knowledge_source"], "computed")
        # 失败那次**没有**写盘（既不写空缓存，也不留半截）
        self.assertEqual(len(_events("analyze.knowledge_computed")), 1)
        # ⚠️ 「领导者**失败**」与「领导者**卡死**」是两条路，绝不许混：
        # 前者等待者自己再算一次（上面钉住了 builder 被调 2 次），后者等待者**如实报错**。
        self.assertEqual(_events(jdfacts.EVENT_JOIN_TIMEOUT), [],
                         "失败的领导者被误判成「卡死超时」了（那是另一条路）")

    # ---- 超时（体检 §5 P3-4）：领导者**卡死**时等待者绝不无限挂起 ----
    def test_a_waiter_gives_up_with_an_error_when_the_leader_never_returns(self):
        """`builder` 永不返回 ⇒ 等待者必须在 `INFLIGHT_WAIT_SECONDS` 内退出并**如实报错**。

        ⚠️ 这是体检 §5 P3-4 的真实病：服务是 `ThreadingHTTPServer`（每请求一线程），
        `Event.wait()` 没有上限时，一个卡死的领导者会让**所有**等待者永久占住线程 ——
        一次卡死慢慢把线程耗光（而且它们永远等不到任何结果）。

        ⚠️ 断言形状必须是「有界 join 之后线程**必须**已经结束」，**不能**写成「等它回来」：
        旧实现上等待者会一直挂，写成等它就变成测试自己挂住（那既不是红、也拿不到证据）。

        ⚠️ 选定的降级是**抛错**（`InflightTimeout`）而不是「等不到就自己再算一遍」：
        `builder` 永不返回时「自己算」会再进一次同样不封顶的调用 —— 照样占住线程（等于没修），
        还会再烧一遍 ~53 次搜索额度；抛错则被 app.py 如实写进报告的 `knowledge_error`。
        """
        self._new_jd()
        calls, errors, results = [], {}, {}
        entered, release = threading.Event(), threading.Event()

        def builder(jd_text, cfg):
            calls.append(threading.current_thread().name)
            entered.set()
            release.wait(30)                              # 断言窗口内**永不返回**
            return json.loads(json.dumps(_nodes("never"), ensure_ascii=False))

        def worker(name):
            try:
                results[name] = jdfacts.resolve(JD_A, "JD 原文-A", CFG, builder=builder)
            except BaseException as e:                     # noqa: BLE001 - 异常本身就是要断言的对象
                errors[name] = e

        with mock.patch.object(jdfacts, "INFLIGHT_WAIT_SECONDS", 0.5):
            t1 = threading.Thread(target=worker, args=("t1",), name="t1", daemon=True)
            t2 = threading.Thread(target=worker, args=("t2",), name="t2", daemon=True)
            try:
                t1.start()
                self.assertTrue(entered.wait(30), "领导者没进 builder（这条测试没验证到东西）")
                t2.start()
                t2.join(self.HANG_GRACE_SECONDS)
                # ↓ 旧实现 / 变异「把 wait 的超时去掉」红在**这一行**：等待者到现在还挂着
                self.assertFalse(t2.is_alive(),
                                 "等待者在超时（0.5 秒）之后仍然挂住：无限 wait 会一直占着线程")
            finally:
                release.set()                              # 放行领导者（否则它一直卡在 builder 里）
                t1.join(10)
        self.assertEqual(jdfacts._INFLIGHT, {}, "超时之后留下了「永久占位」的在飞登记")
        self.assertEqual(calls, ["t1"], "等待者没去等，而是自己另算了一遍（那会再烧 53 次搜索）")
        self.assertNotIn("t2", results,
                         "等待者返回了一份「结果」：那是**说假话** —— 它根本没拿到任何结果")
        self.assertIsInstance(errors.get("t2"), jdfacts.InflightTimeout,
                              "等待者不是「如实报错」退出的：%r" % (errors.get("t2"),))
        self.assertIn("0.5", str(errors["t2"]), "报错里没说清等了多久就放弃了")
        timeouts = _events(jdfacts.EVENT_JOIN_TIMEOUT)
        self.assertEqual(len(timeouts), 1, "超时没有留下日志（真出事时没法诊断）")
        self.assertEqual(timeouts[0]["data"]["jd"], JD_A)

    def test_a_request_arriving_after_the_timeout_still_computes(self):
        """超时之后**新来的**请求必须能正常开始算 —— 不是永远去等一个已经死掉的登记。

        做法：领导者卡死 → 等待者超时退出（这一步该把死登记注销掉）→ 再来一个请求，
        它必须**自己真算一次**并拿到结果。判据是「builder 又被调了一次」而**不是**「它很快
        返回了」：只断言「没挂住」的话，一份永远等不到结果的死登记也能让新请求**立刻抛错**
        通过 —— 那不是「正常开始」，那正是这条要防的病。
        """
        self._new_jd()
        calls, errors = [], {}
        entered, release = threading.Event(), threading.Event()

        def hung(jd_text, cfg):
            calls.append(threading.current_thread().name)
            entered.set()
            release.wait(30)                              # 领导者卡死（收尾时才放行）
            return json.loads(json.dumps(_nodes("stale"), ensure_ascii=False))

        def worker(name):
            try:
                jdfacts.resolve(JD_A, "JD 原文-A", CFG, builder=hung)
            except BaseException as e:                     # noqa: BLE001 - 超时就是要断言的对象
                errors[name] = e

        fresh = _Builder(_nodes("fresh"))                  # 新来的请求注入的 builder（会计数）
        with mock.patch.object(jdfacts, "INFLIGHT_WAIT_SECONDS", 0.5):
            leader = threading.Thread(target=worker, args=("leader",), name="leader", daemon=True)
            waiter = threading.Thread(target=worker, args=("waiter",), name="waiter", daemon=True)
            try:
                leader.start()
                self.assertTrue(entered.wait(30), "领导者没进 builder（这条测试没验证到东西）")
                waiter.start()
                waiter.join(self.HANG_GRACE_SECONDS)
                self.assertFalse(waiter.is_alive(), "等待者没在超时内退出（无限挂起）")
                self.assertIsInstance(errors.get("waiter"), jdfacts.InflightTimeout)
                self.assertEqual(jdfacts._INFLIGHT, {},
                                 "超时之后留下了「永久占位」的在飞登记：后来者会永远等一个死掉的"
                                 "领导者（那份登记再也不会有人注销）")
                try:
                    nodes, source = jdfacts.resolve(JD_A, "JD 原文-A", CFG, builder=fresh)
                except BaseException as e:
                    # ↓ 变异「超时后不清理登记」红在**这一行**：新请求还在等那个死条目
                    self.fail("超时之后新来的请求没能开始算（还在等那个死掉的登记）：%r" % (e,))
            finally:
                release.set()
                leader.join(10)
        self.assertEqual(calls, ["leader"], "等待者不该去调 builder（它只是在等）")
        self.assertEqual(len(fresh.calls), 1, "新来的请求没有真的开始算")
        self.assertEqual(source, jdfacts.SOURCE_COMPUTED)
        self.assertEqual(_jdump(nodes), _jdump(_nodes("fresh")))
        self.assertEqual(jdfacts._INFLIGHT, {}, "收尾后还留着在飞登记")

    def test_the_builder_is_called_outside_the_jd_lock(self):
        """真算（206 秒）**绝不能**抱着 `_JD_LOCK`：那会让**所有 JD 的写入**一起停 206 秒。

        判据：builder 被调的那一刻那把锁不归本线程（`_store` 那次「读 → 改 → 写」仍然必须在
        锁内，由 `GetAndSaveTest.test_read_modify_write_happens_under_the_jd_lock` 钉着）。
        """
        self._new_jd()
        owned = []

        def builder(jd_text, cfg):
            owned.append(jdid._JD_LOCK._is_owned())
            return _nodes("x")

        jdfacts.resolve(JD_A, "JD 原文-A", CFG, builder=builder)
        self.assertTrue(owned, "_builder 一次都没被调，这条测试没验证到东西")
        self.assertEqual(owned, [False],
                         "builder 是在 _JD_LOCK 里被调的：所有 JD 的写入都会被它堵 206 秒")


# ---------------------------------------------------------------- ⑤ 架构铁律
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
        self.assertEqual(names - {"os", "threading", "time", "datetime",
                                  "jdid", "kb", "log", "store"}, set())

    def test_the_inflight_wait_always_passes_a_timeout(self):
        """AST 钉住：`_join_inflight` 里的等待**必须**带超时实参（体检 §5 P3-4）。

        回归形态就是「把 `wait(left)` 改回 `wait()`」—— 无限等会让一个卡死的领导者把所有
        等待者永久占住（服务是 `ThreadingHTTPServer`）。正则看不出来（`wait` 到处都是），
        所以按 AST 找 **`_join_inflight` 函数体内**的 `.wait(` 调用，断言它带了实参。
        """
        tree = ast.parse(store.read_text(os.path.join(store.ROOT, "jdfacts.py")))
        funcs = [n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == "_join_inflight"]
        self.assertEqual(len(funcs), 1, "找不到 _join_inflight（解析方式不对？）")
        waits = [n for n in ast.walk(funcs[0])
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "wait"]
        self.assertTrue(waits, "_join_inflight 里一个等待都没找到（这条测试没验证到东西）")
        for call in waits:
            self.assertTrue(call.args or call.keywords,
                            "_join_inflight 里有不带超时的 wait()：领导者卡死会无限占住线程")

    def test_the_inflight_wait_covers_a_slow_but_healthy_compute(self):
        """超时上限必须**明显大于**一次正常真算（实测最坏 206 秒 / 53 次搜索）。

        定小了 = 把一次正常的慢真算误判成「卡死」→ 给用户报一个**假的失败**（本项目最恨的
        那类错）。这里钉住下界：至少是实测最坏耗时（`analyze.knowledge_done ms=206206`）的
        2 倍；上限值本身可以调，但不许调到会误杀正常真算的量级。
        """
        self.assertIsInstance(jdfacts.INFLIGHT_WAIT_SECONDS, (int, float))
        self.assertGreaterEqual(jdfacts.INFLIGHT_WAIT_SECONDS, 2 * 206)

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
