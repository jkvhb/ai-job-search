# -*- coding: utf-8 -*-
"""Task 2：把 JD 级知识地图缓存接进 `app.do_analyze`，并在报告里**诚实标注来源**。

**为什么是这批断言**（用户真实日志）：知识地图占一次分析 87% 的时间（206 秒 / 53 次搜索），
而它是**纯 JD 级**的。接上缓存之后「换一份简历重分析」必须真的 **0 次 builder、0 次搜索**，
并且报告里要能看出这份地图是**复用**还是**新算**（含「这份缓存生成于」的时间）。

**四条硬纪律**（每条都对应本项目抓到过的假绿）：
1. 「复用了」必须同时断言注入的 builder **调用次数 0** —— 只断言「报告里有知识节点」是恒真的；
2. **顺序是契约**：`jdfacts.resolve` 对不存在的 JD 抛 `LookupError`（在真算之前），所以
   `do_analyze` 必须**先落 JD 身份、再 resolve**。这条用「builder 被调时盘上已有这条 JD 记录」
   + 「resolve 一次 `LookupError` 都没抛」两条一起钉住 —— 只断言前者会被「把 LookupError
   吞掉再重试」的写法蒙过去（实测踩到：那条路下盘上确实有记录了）；
3. 报告里的来源标记要断言**那个元素自己的文本**（`#knowMeta`），绝不写「整页 HTML 里有没有
   某个字符串」—— 那句话本来就在模板源码里；
4. 模板那一行用 **Node 探针**单独验（真跑模板的 JS）：三种 source 的文案 + 缺失时不显示 +
   `esc()` 生效。**绝不真调模型 / 绝不真搜索**：三方入口全部注入假实现并计数。
"""
import base64
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

import app
import jdfacts
import jdid
import kb
import log
import store

from tests import web_probe
from tests.base import IsolatedCase

JD_A = "本科_甲公司_标注_文本_北京_13-26K_0001"
JD_B = "硕士_乙公司_评测_语音_上海_20-30K_0002"
CACHE_TIME = "2026-10-03 00:35"


def _nodes(tag):
    """带哨兵字符串的假知识地图：两个 JD / 两次分析的产物互相之间绝不会碰巧相同。"""
    return [{"id": "n-%s-1" % tag, "term": "语音识别（%s）" % tag, "definition": "定义（%s）" % tag,
             "layer": 0, "sources": [], "timeline": [], "related": [], "interview_questions": [],
             "confidence": "guess"}]


def _jdump(value):
    """逐字比较用：键序无关、中文不转义。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _events(name):
    """隔离根日志里某个事件的全部记录。"""
    return [r for r in log.read_logs(500)[0] if r.get("event") == name]


def _report_data_on_disk(report):
    """从**落盘的那份报告**里读回 REPORT_DATA（与 kb.read_report_data 同一口径）。"""
    path = os.path.join(store.p_path("reports"), report)
    return kb.read_report_data(path)


class _Builder:
    """注入的假 builder：只计数、绝不联网、每次回一份**新副本**。"""

    def __init__(self, nodes=None, watch=None):
        self.calls = []
        self.nodes = _nodes("computed") if nodes is None else nodes
        self.watch = watch

    def __call__(self, jd_text, cfg):
        if self.watch is not None:
            self.watch()
        self.calls.append((jd_text, cfg))
        return json.loads(json.dumps(self.nodes, ensure_ascii=False))


class _AnalyzeCase(IsolatedCase):
    """沙箱化一次真实的分析流程：三方入口全部是计数替身，绝不联网。"""

    LONG = ("数据质检验收把控：严格按评测标准对大模型语音通话数据质检验收；"
            "熟悉ASR与TTS评测流程；能定位Bad Case并做归因分析；有标注规范落地经验。")
    REPLY = json.dumps({
        "job_title": "语音数据质检", "score": 91.5, "company": "", "verdict": "建议投递",
        "job_category": "数据标注/质检", "location": "北京", "salary": "13-26K",
        "scores": [{"name": "匹配度", "score": 91.5, "weight": 10}],
    }, ensure_ascii=False)

    RESUME = "我的简历.md"

    def setUp(self):
        super().setUp()
        store.ensure_profile()
        store.write_text(os.path.join(store.p_path("resumes"), self.RESUME), "候选人简历正文")
        self._patch(app.llm, "call_openai_compatible", lambda *a, **kw: self.REPLY)
        # 真 repair_json 会走网络。干净 JSON 走不到它，但它必须被挡住 —— 挡住了才说明
        # 「模型替身真的被用上了」，而不是悄悄换了一条路。
        self._patch(app.llm, "repair_json",
                    lambda *a, **kw: (_ for _ in ()).throw(AssertionError("测试里不该真去修 JSON")))
        self._patch(app.search_mod, "fetch_link",
                    lambda *a, **kw: (_ for _ in ()).throw(AssertionError("测试里不该真抓链接")))

    def _patch(self, obj, name, value):
        p = mock.patch.object(obj, name, value)
        p.start()
        self.addCleanup(p.stop)

    def _inject_builder(self, nodes=None, watch=None):
        """把 `knowledge.build_for_jd`（唯一真花钱的那一步）换成计数替身。

        统计报告里出现：**注入点仍然是 `app.knowledge.build_for_jd`**（不是 jdfacts 内部）——
        它正是生产里 do_analyze 传给 jdfacts.resolve 的那个 builder。
        """
        b = _Builder(nodes, watch=watch)
        self._patch(app.knowledge, "build_for_jd", b)
        return b

    def _resume(self, name):
        """按名字准备一份简历（测试里会用好几个名字：换简历重分析是这块的全部场景）。"""
        path = os.path.join(store.p_path("resumes"), name)
        if not os.path.exists(path):
            store.write_text(path, "候选人简历正文（%s）" % name)
        return name

    def _run(self, jd_text=None, resume=None, **extra):
        body = {"jd_text": jd_text if jd_text is not None else self.LONG,
                "resume_name": self._resume(resume or self.RESUME), "source_type": "粘贴文本"}
        body.update(extra)
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        h = _sandbox_handler("POST", "/api/analyze", raw,
                             {"Content-Length": str(len(raw)),
                              "Content-Type": "application/json; charset=utf-8"})
        self.assertEqual(h._status, 200, "分析请求不该失败：%r" % (h._body,))
        self.assertTrue(isinstance(h._body, dict) and h._body.get("ok"), h._body)
        return h._body["report"]

    def _raw(self, **body):
        """发一次 `/api/analyze`，**不做任何断言**（失败场景要自己看状态码与 body）。"""
        payload = {"jd_text": body.pop("jd_text", self.LONG),
                   "resume_name": self._resume(body.pop("resume_name", self.RESUME)),
                   "source_type": "粘贴文本"}
        payload.update(body)
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return _sandbox_handler("POST", "/api/analyze", raw,
                                {"Content-Length": str(len(raw)),
                                 "Content-Type": "application/json; charset=utf-8"})

    def _data_of(self, report):
        data = _report_data_on_disk(report)
        self.assertIsInstance(data, dict, "报告 %s 里抽不出 REPORT_DATA" % report)
        return data

    def _meta_of(self, report):
        meta = self._data_of(report).get("knowledge_meta")
        self.assertIsInstance(meta, dict, "报告 %s 里没有 knowledge_meta（来源标记那行读不到来源）"
                              % report)
        return meta

    def _seed_a_report_with_knowledge(self, jd_id=JD_A, report="20261003_岗A_75分.html",
                                      nodes=None):
        """造一个「该 JD 自己已有分析、且报告里有知识地图」的局面（播种路径的入口）。"""
        jdid.upsert_jd({"id": jd_id, "job_title": "语音数据质检", "jd_text_file": jd_id + ".txt"})
        store.write_text(store.p_path("jd", jd_id + ".txt"), self.LONG)     # 覆盖时要用它比对相似度
        store.upsert_job({"id": report[:-5], "jd_id": jd_id, "resume": "旧简历", "score": 75.0,
                          "report": report, "created_at": "2026-10-03 00:30"})
        store.write_text(os.path.join(store.p_path("reports"), report),
                         '<!doctype html><html><body><script>\n%s%s\n</script></body></html>'
                         % (kb.REPORT_MARK,
                            json.dumps({"report_id": report[:-5],
                                        "knowledge": nodes if nodes is not None else _nodes("report")},
                                       ensure_ascii=False)))


def _sandbox_handler(method, path, body: bytes, headers=None):
    """与 tests/test_app.py::_sandbox_handler 同一套写法：不监听端口、不接套接字。"""
    h = app.Handler.__new__(app.Handler)
    h.path = path
    h.command = method
    h.request_version = "HTTP/1.1"
    h.close_connection = True
    h.rfile = io.BytesIO(body)
    h.wfile = io.BytesIO()
    h.headers = dict(headers or {})
    h._status, h._body, h._ctype, h._sent_file = None, None, "", None
    h._send = lambda code, obj=None, ctype="application/json; charset=utf-8", raw=False: (
        setattr(h, "_status", code), setattr(h, "_body", obj), setattr(h, "_ctype", ctype), obj)[-1]
    h._send_file = lambda p, ctype: (setattr(h, "_status", 200), setattr(h, "_sent_file", p), None)[-1]
    (app.Handler.do_GET if method == "GET" else app.Handler.do_POST)(h)
    return h


# ---------------------------------------------------------------- 三条路径 + 刷新
class AnalyzeKnowledgeCacheTest(_AnalyzeCase):
    """`do_analyze` 的三级回退接线：复用 / 播种 / 真算 / 强制刷新。"""

    def test_reuse_the_second_analysis_of_the_same_jd_calls_the_builder_zero_times(self):
        """**本块的核心验收**：同一 JD 第二次分析（换了简历）→ builder 0 次、搜索 0 次。"""
        b = self._inject_builder(_nodes("first"))
        r1 = self._run(resume="第一版.md")
        self.assertEqual(len(b.calls), 1, "第一次分析就该真算一次")
        self.assertEqual(self._meta_of(r1)["source"], "computed")

        b2 = self._inject_builder(_nodes("绝不该出现"))
        r2 = self._run(resume="第二版.md", allow_duplicate=True)
        self.assertEqual(b2.calls, [], "第二次分析又调了 builder（= 又烧了 53 次搜索）")
        # 报告里的知识地图与第一次**逐字相同**（缓存的是 build_for_jd 的原始输出）
        d1, d2 = _report_data_on_disk(r1), _report_data_on_disk(r2)
        self.assertEqual(_jdump(d2["knowledge"]), _jdump(d1["knowledge"]))
        self.assertEqual(_jdump(d2["knowledge"]), _jdump(_nodes("first")))
        # 来源标注 + 日志
        meta = self._meta_of(r2)
        self.assertEqual(meta["reused"], True)
        self.assertEqual(meta["source"], "cache")
        # 报告是两份（每份分析一个），JD 只有一个
        self.assertNotEqual(r1, r2)
        jds = jdid.load_jds()["jds"]
        self.assertEqual(len(jds), 1)
        reused = _events("analyze.knowledge_reused")
        self.assertEqual(len(reused), 1)
        self.assertEqual(reused[0]["data"]["jd"], jds[0]["id"])
        self.assertEqual(reused[0]["data"]["nodes"], 1)

    def test_the_second_report_says_which_cache_generation_it_reuses(self):
        """报告里的 `at` 必须是**这份缓存生成于**（真实实现拿 jdfacts 的 knowledge_at）。"""
        with mock.patch("jdfacts._now", return_value=CACHE_TIME):
            self._inject_builder(_nodes("first"))
            r1 = self._run()
        meta1 = self._meta_of(r1)
        self.assertEqual(meta1["source"], "computed")
        self.assertEqual(meta1["at"], CACHE_TIME)
        self.assertEqual(meta1["reused"], False)

        self._inject_builder(_nodes("绝不该出现"))
        r2 = self._run(resume="第二版.md", allow_duplicate=True)
        meta2 = self._meta_of(r2)
        self.assertEqual(meta2["source"], "cache")
        self.assertEqual(meta2["reused"], True)
        self.assertEqual(meta2["at"], CACHE_TIME, "复用时显示的生成时间不是缓存那份的")

    def test_the_at_shape_production_sends_matches_the_template_guard(self):
        """**生产与测试同一个口径**：报告里的 `at` 要么是真字符串、要么干脆没有。

        为什么必须断在**落盘的那份报告**上：模板里 `typeof m.at === 'string'` 那道守卫只有在
        生产**不**把 `5` / `{}` 之流 `str()` 成 `"5"` / `"{'a': 1}"` 时才有意义。`str()` 一上，
        守卫就形同虚设 —— 报告会说出「复用该岗位 **5** 生成的那份」这种**不真实**的话
        （`tests/...::test_a_cache_meta_without_a_usable_at_still_says_reuse` 喂原始类型，
        生产到不了那个形状，所以那条测试的不变量在生产里根本不成立）。

        用户手改坏 `jds.json`（`knowledge_at: 5`）是真会发生的：那时生产必须**不送** `at`，
        由模板退回「复用该岗位已缓存的那份」—— 而不是把 `5` 当日期显示。
        """
        with mock.patch("jdfacts._now", return_value=CACHE_TIME):
            self._inject_builder(_nodes("first"))
            r1 = self._run()
        at = self._meta_of(r1).get("at")
        self.assertTrue(at is None or isinstance(at, str),
                        "生产送出的 `at` 不是字符串也不是缺失：%r" % (at,))
        self.assertEqual(at, CACHE_TIME)

        # 手改坏缓存时间戳（用户真会这么干）→ 重分析走复用，`at` 必须**缺失**
        jd_id = jdid.load_jds()["jds"][0]["id"]
        facts = dict(jdid.find_jd(jd_id)["jd_facts"])
        facts["knowledge_at"] = 5
        jdid.upsert_jd({"id": jd_id, "jd_facts": facts})
        self._inject_builder(_nodes("绝不该出现"))
        r2 = self._run(resume="第二版.md", allow_duplicate=True)
        meta2 = self._meta_of(r2)
        self.assertEqual(meta2["source"], "cache")
        self.assertNotIn("at", meta2,
                         "生产把非字符串的 knowledge_at 送进了报告（模板守卫形同虚设）：%r" % (meta2,))
        need_node()   # 缺 Node → 红（1.10③）：这半句渲染断言原先被 `if NODE:` 静默跳过
        got = _know_meta_text(self._data_of(r2))
        # 阶段 E 之后这一行还会追加一句「本次没有重跑「库优先」」（`kb_reuse.not_run`），
        # 所以这里断言**那半句真话还在**，而不是整行等于它。
        self.assertIn("📚 知识地图：复用该岗位已缓存的那份（本次没有重新搜索）", got,
                      "`5` 被当成日期显示出来了（说了不真实的话）：%r" % (got,))
        self.assertNotIn("生成的那份", got,
                         "`at` 不可用时不能编一句「X 生成的那份」：%r" % (got,))

    def test_seeding_from_the_jds_own_report_does_not_call_the_builder(self):
        """该 JD 已有报告里有知识地图 → 免费播种，builder **0 次**，source == seeded:<报告id>。

        这里带 `overwrite=JD_A`：把新分析挂到那份**有报告的**已有 JD 上（否则新建的 JD 是
        另一个身份，播种当然播不到 —— 那不是被测路径）。
        """
        self._seed_a_report_with_knowledge(JD_A)
        b = self._inject_builder(_nodes("绝不该出现"))
        report = self._run(overwrite=JD_A)
        self.assertEqual(b.calls, [], "播种路径居然调了 builder（= 真搜索）")
        data = self._data_of(report)
        self.assertEqual(_jdump(data["knowledge"]), _jdump(_nodes("report")))
        meta = data["knowledge_meta"]
        self.assertEqual(meta["source"], "seeded:20261003_岗A_75分")
        self.assertEqual(meta["reused"], True)
        evs = _events("analyze.knowledge_seeded")
        self.assertEqual([e["data"]["report"] for e in evs], ["20261003_岗A_75分"])
        self.assertEqual([e["data"]["jd"] for e in evs], [JD_A])

    def test_a_new_jd_computes_once_and_says_so(self):
        """新 JD 第一次 → builder 1 次、source == computed、`at` 就是这次生成的。"""
        with mock.patch("jdfacts._now", return_value=CACHE_TIME):
            b = self._inject_builder(_nodes("brand-new"))
            report = self._run()
        self.assertEqual(len(b.calls), 1)
        self.assertEqual(b.calls[0][0], self.LONG)          # 喂给 builder 的是 JD 原文
        data = _report_data_on_disk(report)
        self.assertEqual(_jdump(data["knowledge"]), _jdump(_nodes("brand-new")))
        meta = data["knowledge_meta"]
        self.assertEqual((meta["reused"], meta["source"], meta["at"]),
                         (False, "computed", CACHE_TIME))
        computed = _events("analyze.knowledge_computed")
        self.assertEqual(len(computed), 1)
        self.assertEqual(computed[0]["data"]["refresh"], False)

    def test_refresh_jd_facts_recomputes_even_when_a_cache_exists(self):
        """`refresh_jd_facts: true` → 即使有缓存也重算，并把「这份生成于」推到现在。"""
        with mock.patch("jdfacts._now", return_value=CACHE_TIME):
            self._inject_builder(_nodes("旧的"))
            first = self._run()
        self.assertEqual(self._meta_of(first)["at"], CACHE_TIME)

        # 把第一次分析的事件日志清掉：这条要断的是**第二次**（刷新）那一次的事件，
        # 不清的话「刷新时算了几次」会被第一次的 computed 混进来（那不是假绿，是假红）。
        store.write_text(log.LOG_FILE, "")
        with mock.patch("jdfacts._now", return_value="2026-11-01 12:00"):
            b2 = self._inject_builder(_nodes("刷新的"))
            report = self._run(resume="第二版.md", allow_duplicate=True,
                               refresh_jd_facts=True)
        self.assertEqual(len(b2.calls), 1, "刷新没有透传给 jdfacts.resolve（永远走缓存了）")
        data = _report_data_on_disk(report)
        self.assertEqual(_jdump(data["knowledge"]), _jdump(_nodes("刷新的")))
        meta = data["knowledge_meta"]
        self.assertEqual((meta["reused"], meta["source"], meta["at"]),
                         (False, "computed", "2026-11-01 12:00"))
        computed = _events("analyze.knowledge_computed")
        self.assertEqual([e["data"]["refresh"] for e in computed], [True])
        # 刷新之后**再分析一次**：又回到复用（不是每次都真算）
        b3 = self._inject_builder(_nodes("绝不该出现"))
        report3 = self._run(resume="第三版.md", allow_duplicate=True)
        self.assertEqual(b3.calls, [])
        self.assertEqual(self._meta_of(report3)["source"], "cache")

    def test_another_jd_does_not_reuse_the_first_cache(self):
        """反向对照：另一个 JD 不会误用别人的缓存（否则「复用」就是恒真）。"""
        self._inject_builder(_nodes("A 的"))
        self._run()
        b_b = self._inject_builder(_nodes("B 的"))
        report = self._run(jd_text=("负责语音评测集建设：设计 TTS/ASR 评测方案，"
                                    "分析 Bad Case 并输出改进建议，维护标注规范文档。"))
        self.assertEqual(len(b_b.calls), 1, "B 直接拿了 A 的缓存（builder 一次都没调）")
        self.assertEqual(self._meta_of(report)["source"], "computed")
        self.assertEqual(_jdump(_report_data_on_disk(report)["knowledge"]), _jdump(_nodes("B 的")))

    def test_the_knowledge_base_still_absorbs_on_the_reuse_path(self):
        """*不要动知识库那条路*：复用时 `kb.absorb` 照旧累积（含 from_jds 指向 JD id）。"""
        self._inject_builder(_nodes("first"))
        r1 = self._run()
        card = [n for n in store.load_knowledge()["nodes"]
                if kb.normalize_id(n.get("term")) == kb.normalize_id("语音识别（first）")]
        self.assertEqual(len(card), 1)
        jd_id = jdid.load_jds()["jds"][0]["id"]
        self.assertEqual([x["id"] for x in card[0]["from_jds"]], [jd_id])
        self.assertNotIn(r1[:-5], [x["id"] for x in card[0]["from_jds"]])
        # 第二次（复用路径）再来一遍：库没崩、卡还在（同 JD 不该被记成两个岗位）
        self._run(resume="第二版.md", allow_duplicate=True)
        cards = [n for n in store.load_knowledge()["nodes"]
                 if kb.normalize_id(n.get("term")) == kb.normalize_id("语音识别（first）")]
        self.assertEqual(len(cards), 1)
        self.assertEqual([x["id"] for x in cards[0]["from_jds"]], [jd_id])


# ---------------------------------------------------------------- 失败处理不变
class AnalyzeKnowledgeFailureTest(_AnalyzeCase):
    """真算失败仍然要「报告能出 + 明确标注失败」，**绝不静默变空**。"""

    def _boom(self, jd_text, cfg):
        raise RuntimeError("模型/搜索炸了")

    def test_a_failing_builder_reports_the_error_and_still_writes_the_report(self):
        self._patch(app.knowledge, "build_for_jd", self._boom)
        report = self._run()                     # 报告照出（不抛到路由层）
        data = _report_data_on_disk(report)
        self.assertEqual(data["knowledge"], [])
        self.assertEqual(data["knowledge_error"], "模型/搜索炸了")
        errs = _events("analyze.knowledge_error")
        self.assertEqual(len(errs), 1)
        self.assertIn("模型/搜索炸了", errs[0]["data"]["error"])
        # 「管线失败」不能伪装成「真的没有知识点」：没有来源标记
        self.assertFalse(data.get("knowledge_meta"))
        # 真算**没有**开始过（不是「算了但结果丢了」：`resolve` 在 builder 之前就挂了）
        self.assertEqual(_events("analyze.knowledge_computed"), [])

    def test_an_empty_knowledge_map_is_still_a_successful_compute(self):
        """真的算出来是空的 → 是「computed」，不是 error（两条路必须分得开）。"""
        b = self._inject_builder([])
        report = self._run()
        data = _report_data_on_disk(report)
        self.assertEqual(data["knowledge"], [])
        self.assertNotIn("knowledge_error", data)
        self.assertEqual(data["knowledge_meta"]["source"], "computed")
        self.assertEqual(_events("analyze.knowledge_error"), [])
        self.assertEqual(len(b.calls), 1, "空结果也得是真算出来的（不是没算就报空）")
        # 空列表不算「可用的缓存」→ evaluate 再分析一次还是真算（绝不把空当缓存复用）
        self._inject_builder([])
        self._run(resume="第二版.md", allow_duplicate=True)
        self.assertEqual(len(_events("analyze.knowledge_computed")), 2)


# ---------------------------------------------------------------- 顺序契约
class ResolveOrderTest(_AnalyzeCase):
    """**顺序是契约**：先定 JD 身份（落盘那条 JD 记录），再 `jdfacts.resolve`。

    规格 Task 1 交接：`resolve` 对不存在的 JD 抛 `LookupError`（在任何真算之前 —— 否则
    强制刷新会在一个不存在的岗位上空烧 53 次搜索）。所以 `do_analyze` 必须先落身份。

    断言方式刻意不是「抛了异常」：那种断言会被「把 LookupError 吞掉、落完身份再重试一次」
    的写法蒙过去（实测踩到：那条路下盘上确实有记录了）。这里同时钉两样：
      * builder 被调**那一刻**回头看盘，这条 JD 记录必须已经在 `jds.json` 里；
      * `resolve` 从没抛过 `LookupError`（吞异常重试那条路会留下痕迹）。
    """

    def test_the_jd_identity_is_written_before_the_knowledge_map_is_computed(self):
        """**顺序是契约**（规格 Task 1 交接）：先定 JD 身份（落盘那条 JD 记录），再 `resolve`。

        `resolve` 对不存在的 JD 抛 `LookupError` —— 所以「先 resolve 再落身份」必然要吞掉那个
        异常、落完身份重试一次才出得来报告。这里就把那条路钉死：**落身份时一次都没抛**
        （`analyze.error` 一条都没有）+ builder 被调那一刻盘上已经有这条 JD。
        """
        seen = {}
        thrown = []
        real_resolve = jdfacts.resolve

        def spy(jd_id, jd_text, cfg, refresh=False, builder=None):
            try:
                return real_resolve(jd_id, jd_text, cfg, refresh=refresh, builder=builder)
            except Exception as e:
                thrown.append((jd_id, type(e).__name__))
                raise

        self._patch(jdfacts, "resolve", spy)
        jdid.save_jds({"version": 1, "seq": 0, "jds": []})

        def watch():
            seen["jds_at_build_time"] = [j.get("id") for j in jdid.load_jds()["jds"]]

        self._inject_builder(_nodes("first"), watch=watch)
        report = self._run()
        self.assertIn("jds_at_build_time", seen, "builder 一次都没被调用，这条测试没验证到东西")
        self.assertEqual(len(seen["jds_at_build_time"]), 1,
                         "builder 被调时盘上还没有这条 JD 记录（身份没先定下来）")
        self.assertEqual(seen["jds_at_build_time"][0], jdid.load_jds()["jds"][0]["id"])
        self.assertEqual(thrown, [], "resolve 先跑在了身份之前（靠吞 LookupError + 重试蒙过去的）：%r"
                         % (thrown,))
        self.assertEqual(_events("analyze.error"), [])
        self.assertEqual(len(self._data_of(report)["knowledge"]), len(_nodes("first")),
                         "报告里的知识地图不是这一份（刷新/播种串了路）")
        facts = jdfacts.get(jdid.load_jds()["jds"][0]["id"])
        self.assertEqual(_jdump(facts["knowledge"]), _jdump(_nodes("first")))

    def test_the_builder_is_not_called_when_the_jd_identity_cannot_be_established(self):
        """身份定不下来（build_id 拿不到字段）→ 一个 builder 都不该跑（更没有 53 次搜索）。"""
        b = self._inject_builder(_nodes("绝不该出现"))
        body = {"jd_text": self.LONG, "resume_name": self.RESUME}
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        with mock.patch.object(jdid, "build_id", side_effect=RuntimeError("身份炸了")):
            h = _sandbox_handler("POST", "/api/analyze", raw,
                                 {"Content-Length": str(len(raw)),
                                  "Content-Type": "application/json; charset=utf-8"})
        self.assertEqual(h._status, 200)
        self.assertFalse(h._body.get("ok"))
        self.assertEqual(b.calls, [], "身份都没定下来就真算知识地图了")


# ---------------------------------------------------------------- 孤儿 JD（崩溃窗口的善后）
class AnalyzeOrphanJdTest(_AnalyzeCase):
    """异常落在「落 `jd/<id>.txt`」与「写 JD 记录」之间时，绝不留下一条卡死用户的孤儿。

    孤儿长这样：`jds.json` 里只有 `id/seq/created_at/updated_at/id_history` —— **没有
    `jd_facts`**，也**没有任何分析挂在它下面**。它在台账里根本看不见，但 `_similar_saved_jd`
    会通过盘上那份 `<id>.txt` 命中它（similarity 1.0、`job_title=""`、`score=None`）→
    用户重提同一份 JD 只会拿到 **409**，而前端**没有** `allow_duplicate` 入口 → 看到
    「失败：未知错误」且无法继续。提交前同一局面下没有 `jds.json` 记录，重试本可成功。

    善后的代价（**为什么只删「本次刚建、且一无所有」的那一条**）：只删 `jds.json` 里那一条
    记录，**绝不删任何文件**（用户贴过的原文照旧留在 `jd/` 下）；**绝不动**不是本次刚建的
    记录，也绝不删「已经有分析挂在下面」的记录 —— 那已经是用户的台账了。孤儿留在盘上的
    代价只是下一次同 JD 分析会换个新身份（多一份报告），比「用户被卡死」小得多。
    """

    OTHER = "j-别的岗位"
    OTHER_TEXT = ("负责线下门店的陈列与库存盘点：每周巡店、核对进销存、"
                  "输出盘点差异报告并与仓配协同处理异常。")

    def _other_jd(self):
        """一条**正常的**别的 JD（含分析记录与原文）：清理时一条都不许少。"""
        jdid.upsert_jd({"id": self.OTHER, "job_title": "陈列盘点", "seq": 7,
                        "jd_text_file": self.OTHER + ".txt"})
        store.write_text(store.p_path("jd", self.OTHER + ".txt"), self.OTHER_TEXT)
        store.upsert_job({"id": "j-别的分析", "jd_id": self.OTHER, "resume": "旧简历",
                          "score": 60.0, "report": "j-别的分析.html",
                          "created_at": "2026-10-01 09:00"})
        return jdid.find_jd(self.OTHER)

    def _crash_while_writing_the_jd_record(self):
        """在「原文已落盘、JD 记录还没写」那一刻炸 —— 审查者实测的那个窗口。

        **知识地图那一步也失败**（`builder` 抛异常）：这正是审查者实测到的孤儿形状
        （`id/seq/created_at/updated_at/id_history`，没有 `jd_facts`）。知识地图成功的话记录上
        会有 `jd_facts` —— 那种按裁定**不删**（不是「什么也没落上」，见 `_discard_orphan_jd`）。
        """
        self._patch(app.knowledge, "build_for_jd",
                    lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("知识地图炸了")))
        with mock.patch.object(app, "_jd_record_of",
                               side_effect=RuntimeError("写 JD 记录之前炸了")):
            h = self._raw()
        self.assertEqual(h._status, 200, "路由层该把它当普通失败回 200：%r" % (h._body,))
        self.assertFalse(h._body.get("ok"), h._body)
        self.assertIn("写 JD 记录之前炸了", str(h._body.get("error")))
        return h

    def _orphan_id(self):
        """孤儿那条 JD 的 id。记录清掉之后就只剩事件日志认得它。"""
        ident = _events("analyze.jd_identity")
        self.assertEqual(len(ident), 1, "没有 analyze.jd_identity 事件，这条测试没验证到东西")
        return ident[0]["data"]["jd"]

    def test_the_reviewers_repro_is_exactly_this_shape(self):
        """先把**审查者实测的那个局面**原样钉住（关掉善后）：孤儿就是那个形状。

        没有这条，「修好了」就只是一个说法 —— 它证明：孤儿真的长成
        `id/seq/created_at/updated_at/id_history`（没有 `jd_facts`、没有分析）。

        ⚠️ 阶段 4C 起，「重提同一份 JD」**不再 409**：查重粒度改成 (JD 指纹 × 简历版本)，
        而一条分析都没有的孤儿**没有任何版本可比** → 不判重复（规格 §6.2「宁可多建，也不误拦」）。
        这条断言因此改成钉**新行为**：重提照常分析、并且**复用**那个孤儿身份（不另建一个 JD）。
        善后仍然必要 —— 不是为了让重提能过，而是别把一条没有 `jd_facts`、没有分析的半成品记录
        永久的留在 `jds.json` 里（它占着 JD id，还会被 `_similar_saved_jd` 认作"已有的那个岗位"）。
        """
        self._other_jd()
        self._patch(app, "_discard_orphan_jd", lambda jd_id: False)   # 关掉善后，看原始局面
        self._crash_while_writing_the_jd_record()
        orphan_id = self._orphan_id()
        orphan = jdid.find_jd(orphan_id)
        self.assertIsInstance(orphan, dict, "审查者说的孤儿没复现出来")
        self.assertEqual(sorted(orphan), ["created_at", "id", "id_history", "seq", "updated_at"],
                         "孤儿形状与审查者报告的不一样：%r" % (sorted(orphan),))
        self.assertEqual(orphan["id"], orphan_id)
        # 重提同一份 JD：不再 409（阶段 4C 的粒度下没有可比的版本）→ 照常分析
        h = self._raw()
        self.assertEqual(h._status, 200, h._body)
        self.assertNotIn("duplicate", h._body)
        # 而且**复用**那个孤儿身份：没有因此多出一个 JD
        self.assertEqual(sorted(j["id"] for j in jdid.load_jds()["jds"]),
                         sorted([self.OTHER, orphan_id]))

    def test_a_crash_between_the_text_and_the_record_leaves_no_orphan(self):
        other = self._other_jd()
        self._crash_while_writing_the_jd_record()
        orphan_id = self._orphan_id()
        self.assertNotEqual(orphan_id, self.OTHER)

        # ① 本次刚建的那条：清干净了
        self.assertIsNone(jdid.find_jd(orphan_id), "孤儿 JD 记录还在 jds.json 里")
        cleaned = _events("analyze.jd_orphan_cleaned")
        self.assertEqual([e["data"]["jd"] for e in cleaned], [orphan_id],
                         "清理没有留下证据（日志），事后无从查起")
        # ② 反假绿：**别的 JD 记录一条都没少**（逐字比较整条记录，不只是数个数）
        self.assertEqual([j["id"] for j in jdid.load_jds()["jds"]], [self.OTHER])
        self.assertEqual(_jdump(jdid.find_jd(self.OTHER)), _jdump(other))
        self.assertEqual([j["id"] for j in store.load_jobs()], ["j-别的分析"])
        # ③ 「绝不删文件」：只删 `jds.json` 里那一条，用户的原文照旧在盘上
        self.assertTrue(os.path.exists(os.path.join(store.p_path("jd"), orphan_id + ".txt")),
                        "清理孤儿时把用户的原文文件删了")

    def test_the_same_jd_can_be_submitted_again_after_the_crash(self):
        """**用户可见的效果**：修掉之后重提同一份 JD **不再 409**，而是照常重新分析。"""
        self._other_jd()
        self._crash_while_writing_the_jd_record()
        b = self._inject_builder(_nodes("second"))
        report = self._run()                       # `_run` 自己断言 200 + ok（409 会在这里红）
        self.assertEqual(len(b.calls), 1, "重提没有真的重新跑一次分析")
        self.assertEqual(_jdump(self._data_of(report)["knowledge"]), _jdump(_nodes("second")))
        self.assertEqual(len(jdid.load_jds()["jds"]), 2, "别的岗位 + 这次新建的，一条都不能少")

    def test_an_existing_jd_record_is_never_touched(self):
        """**绝不动不是本次刚建的记录**：重提一个已有 JD 时炸在同一个窗口 → 它原样保留。"""
        jdid.upsert_jd({"id": JD_A, "job_title": "语音数据质检", "seq": 3,
                        "jd_text_file": JD_A + ".txt"})
        store.write_text(store.p_path("jd", JD_A + ".txt"), self.LONG)
        store.upsert_job({"id": "j-旧分析", "jd_id": JD_A, "resume": "旧简历", "score": 75.0,
                          "report": "j-旧分析.html", "created_at": "2026-10-03 00:30"})
        before = jdid.find_jd(JD_A)
        self._inject_builder(_nodes("已有岗位的新分析"))
        with mock.patch.object(app, "_jd_record_of", side_effect=RuntimeError("炸在写记录之前")):
            h = self._raw(allow_duplicate=True)     # 命中相似 JD：不带它会在查重那一步先 409
        self.assertEqual(h._status, 200)
        self.assertFalse(h._body.get("ok"), h._body)

        after = jdid.find_jd(JD_A)
        self.assertIsNotNone(after, "本次**没有**新建 JD，却把已有那条记录删了")
        self.assertEqual(after["job_title"], "语音数据质检")
        self.assertEqual(after["jd_text_file"], JD_A + ".txt")
        self.assertEqual(_jdump(after.get("id_history")), _jdump(before.get("id_history")))
        self.assertEqual([j["id"] for j in jdid.analyses_of(JD_A)], ["j-旧分析"])
        self.assertEqual(_events("analyze.jd_orphan_cleaned"), [],
                         "清掉了不是本次刚建的记录")

    def test_a_pre_existing_empty_jd_record_is_never_touched(self):
        """**绝不动不是本次刚建的记录**：判据只能是「这条 id 是本次这次调用建的吗」。

        一条已经存在、同样没有 `jd_facts`、也没有分析的 JD 记录（迁移残留 / 用户手建的）
        在**同一个崩溃窗口**里必须原样保留 —— 否则「看起来像孤儿就删」会把用户的记录删掉。
        """
        jdid.upsert_jd({"id": JD_B, "seq": 9, "job_title": "", "jd_text_file": JD_B + ".txt"})
        store.write_text(store.p_path("jd", JD_B + ".txt"), self.LONG)   # 相似度 1.0 → 复用它
        before = jdid.find_jd(JD_B)
        self.assertFalse(before.get("jd_facts"), "这条记录居然有 jd_facts（没测到该测的东西）")
        self.assertEqual(jdid.analyses_of(JD_B), [])
        self._patch(app.knowledge, "build_for_jd",
                    lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("知识地图炸了")))
        with mock.patch.object(app, "_jd_record_of", side_effect=RuntimeError("炸在写记录之前")):
            h = self._raw(allow_duplicate=True)      # 命中已有 JD_B → 走复用分支（不是本次建的）
        self.assertEqual(h._status, 200)
        self.assertFalse(h._body.get("ok"), h._body)

        after = jdid.find_jd(JD_B)
        self.assertIsNotNone(after, "把一条不是本次刚建的记录当成孤儿删了")
        self.assertEqual(_jdump(after), _jdump(before))
        self.assertEqual(_events("analyze.jd_orphan_cleaned"), [])

    def test_a_fresh_jd_with_an_analysis_on_it_is_kept_even_without_facts(self):
        """守卫②：本次刚建、**已经有分析挂在它下面**的记录绝不删（那已经是用户的台账了）。"""
        self._patch(app.knowledge, "build_for_jd",
                    lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("知识地图炸了")))
        real_save = app._save_analysis

        def save_then_boom(rec, key=None):
            real_save(rec, key)                 # 分析记录**真的写下去了**，然后才炸
            raise RuntimeError("分析记录写完之后炸了")

        self._patch(app, "_save_analysis", save_then_boom)
        h = self._raw()
        self.assertEqual(h._status, 200)
        self.assertFalse(h._body.get("ok"), h._body)

        jds = jdid.load_jds()["jds"]
        self.assertEqual(len(jds), 1, "把一条已经有分析挂着的 JD 记录删了")
        self.assertFalse(jds[0].get("jd_facts"), "这条记录居然有 jd_facts（没测到守卫②）")
        self.assertEqual(len(jdid.analyses_of(jds[0]["id"])), 1)
        self.assertEqual(_events("analyze.jd_orphan_cleaned"), [])


# ---------------------------------------------------------------- 并发：等待者按「复用」报
class AnalyzeConcurrentKnowledgeTest(_AnalyzeCase):
    """父亲的裁定：并发的第二个请求**等**领导者的那 206 秒（额度只烧一次），
    而它报告的那一行必须说「复用…（本次没有重新搜索）」—— 它确实没有重新搜索。

    为什么要在 `/api/analyze` 这一层再测一遍（`jdfacts` 那层已经测过等待者的返回值）：
    报告里那行小字读的是 `knowledge_meta`，它是**路由层**用
    `source != SOURCE_COMPUTED` 算出来的 —— jdfacts 返回什么，用户就看到什么。

    两次请求的分析结果只差 `score` → 报告文件名不同。同分同标题的话第二份报告会覆盖第一份，
    「第二个请求的报告」读出来可能是任意一份（那是真正的假绿）。
    """

    def _prime(self):
        """先跑一次把 JD 身份 + 原文 + 一条分析落好；知识地图**故意算成空的**。

        空列表既不算可用缓存（`_as_nodes([]) or None`），也播种不出东西（`if not nodes:
        continue`）—— 于是并发那两个请求必然走到 ③ 真算（单飞那一步）。用一份非空地图的话，
        第二个请求会从「该 JD 自己的报告」里**播种**成功，单飞根本不会被碰到（这是假绿）。
        """
        self._inject_builder([])
        self._run(resume="第零版.md")
        jd_id = jdid.load_jds()["jds"][0]["id"]
        self.assertIsNone(jdfacts.knowledge_of(jd_id),
                          "缓存还在：两个请求都会走缓存，这条测试就看不到单飞")
        self.assertEqual(_events("analyze.knowledge_seeded"), [],
                         "播种成功了：两个请求根本不会被单飞拦住")
        return jd_id

    def _score_by_resume(self):
        def fake_llm(base_url, api_key, model, messages, json_mode=False, **kw):
            score = 80.0 if "第二版" in messages[-1]["content"] else 91.5
            return json.dumps({"job_title": "语音数据质检", "score": score, "company": "",
                               "verdict": "建议投递", "job_category": "数据标注/质检",
                               "location": "北京", "salary": "13-26K",
                               "scores": [{"name": "匹配度", "score": score, "weight": 10}]},
                              ensure_ascii=False)
        self._patch(app.llm, "call_openai_compatible", fake_llm)

    def test_the_second_concurrent_request_reports_reuse_and_logs_the_join(self):
        jd_id = self._prime()
        store.write_text(log.LOG_FILE, "")           # 只断并发这一段的事件
        entered, release, waiting = threading.Event(), threading.Event(), threading.Event()
        calls = []

        def builder(jd_text, cfg):
            calls.append(threading.current_thread().name)
            entered.set()
            self.assertTrue(release.wait(30), "测试自己没放行 builder")
            return json.loads(json.dumps(_nodes("once"), ensure_ascii=False))

        self._patch(app.knowledge, "build_for_jd", builder)
        self._score_by_resume()
        results, errors = {}, {}

        def worker(name, resume):
            try:
                results[name] = self._raw(resume_name=resume, allow_duplicate=True)
            except BaseException as e:                # noqa: BLE001 - 线程里的异常要能被断言看到
                errors[name] = e

        real_join = jdfacts._join_inflight

        def watch_join(flight):
            waiting.set()
            return real_join(flight)

        with mock.patch.object(jdfacts, "_join_inflight", watch_join):
            t1 = threading.Thread(target=worker, args=("first", "第一版.md"), name="first")
            t1.start()
            self.assertTrue(entered.wait(30), "第一个请求没进 builder：%r / %r" % (errors, results))
            t2 = threading.Thread(target=worker, args=("second", "第二版.md"), name="second")
            t2.start()
            self.assertTrue(waiting.wait(30), "第二个请求没有进入单飞等待（它自己又去算了一遍）")
            release.set()
            t1.join(60)
            t2.join(60)
        self.assertFalse(t1.is_alive() or t2.is_alive(), "有请求没跑完（死等）")
        self.assertEqual(errors, {})
        self.assertEqual(calls, ["first"], "同一个 JD 被真算了 %d 次" % len(calls))

        h1, h2 = results["first"], results["second"]
        self.assertEqual((h1._status, h2._status), (200, 200), (h1._body, h2._body))
        self.assertTrue(h1._body.get("ok") and h2._body.get("ok"), (h1._body, h2._body))
        r1, r2 = h1._body["report"], h2._body["report"]
        self.assertNotEqual(r1, r2, "两个请求写出了同一份报告，读回来的可能不是各自那份")
        meta1, meta2 = self._meta_of(r1), self._meta_of(r2)
        self.assertEqual((meta1["source"], meta1["reused"]), ("computed", False))
        # ★ 等待者：它确实没有重新搜索 → 必须按**复用**报（写「本次新算」会让用户以为烧了两次额度）
        self.assertEqual((meta2["source"], meta2["reused"]), ("cache", True),
                         "等待者被报成了「本次新算」：%r" % (meta2,))
        self.assertEqual(_jdump(self._data_of(r2)["knowledge"]), _jdump(_nodes("once")))
        # 日志里仍然分辨得出「这是等待者」（不是缓存命中）
        joined = _events("analyze.knowledge_joined")
        self.assertEqual(len(joined), 1, "等待者没留下 knowledge_joined（分不清等来的还是缓存）")
        self.assertEqual(joined[0]["data"]["jd"], jd_id)
        self.assertEqual(len(_events("analyze.knowledge_computed")), 1)
        self.assertEqual(len(_events("analyze.knowledge_reused")), 0,
                         "等待者走的是单飞等待，不该记成缓存命中")


# ---------------------------------------------------------------- 模板：Node 探针
NODE = shutil.which("node")

#: 缺 Node 时这**一批**前端断言的覆盖面（供失败信息说清代价，别让人以为只是少跑一条）。
_NODE_LOSS = ("这两条用例的渲染断言（三种 source 的文案 + 缺失时不显示 + 模板里 `at` "
              "不可用时的半句）会静默消失 —— 报告模板标注那一批就没有任何断言了")


def need_node():
    """缺 Node **响亮失败**，绝不静默跳过（1.10③）。

    为什么改成红：原先这里（以及本模块 :275 那处内联 `if NODE:`、`test_app.py` 的
    `_need_node`）在缺 Node 的机器上把整批「真跑页面 JS」的断言变成 `SkipTest` / 直接不执行 ——
    全量测试照样打印 OK，而**报告模板标注那一批根本没有被断言过**。那种绿是假绿：
    改动模板的 JS 不会红，测试不再保护它。缺环境就该是红的，并且要能一眼看出缺什么、
    丢了哪些覆盖（Node 是开发机依赖：装一次即可，不是"这条测试不稳定"）。
    """
    if NODE is None:
        raise AssertionError(
            "本环境缺 Node：`node` 不在 PATH 上，这批「真跑 web/ 模板 JS」的前端断言不能静默跳过。\n"
            "  · 丢掉的覆盖：%s\n"
            "  · 修法：装 Node 并确保 `node` 在 PATH 上（`node --version` 能跑）后重跑本批。\n"
            "  · 若确实要在无 Node 的环境跑：**不要**把它改回 skip —— 那等于把这条不变量删掉；"
            "请显式说明这次没验前端，并让 CI 在有 Node 的机器上跑这一批。"
            % _NODE_LOSS)

# 探针：真跑模板里的 JS（最小 DOM 垫片），只回 `#main` 的 innerHTML，调用方再从里面取
# **那一个元素**（#knowMeta）的文本。
#
# 两个必须踩过才知道的坑（都踩了）：
#   ① 页面从 **stdin** 读，脚本**按数据那行的锚点切**，不按「最后一个 <script>」找 ——
#      报告里注释中就写着字面量 `</script>`（避免 `<\\/` 转义），按标签找会切错段落，
#      于是一个模板函数都没跑，看起来像「模板没渲染」的假红。
#   ② 模板以 `boot()` 结尾（async：先 await 服务端状态，再 render()）→ 必须**反复让出事件循环**
#      等它渲染完，读一次恒空（同样是假红）。
# 输出一律 base64：避免 Windows 控制台编码把中文吃掉（那种失败看起来像「文案不对」）。
PROBE = r"""
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync(0, 'utf8');          // 0 = stdin
const ANCHOR = 'const REPORT_DATA = ';
const a = src.indexOf(ANCHOR);
if (a < 0) throw new Error('探针拿不到页面：找不到 ' + ANCHOR);
const open = src.lastIndexOf('<script>', a);
const i = (open < 0) ? a : (open + '<script>'.length);
const j = src.indexOf('\n', a);                   // 数据那行到此为止（模板脚本在同一 <script> 里）
const js = src.slice(i, j + 1);

const store = {};
const mk = (id) => ({ id, innerHTML: '', textContent: '', value: '', style: {}, dataset: {},
  classList: { add(){}, remove(){}, toggle(){}, contains(){ return false; } },
  addEventListener(){}, removeEventListener(){}, querySelector(){ return null; },
  querySelectorAll(){ return []; }, getContext(){ return null; },
  appendChild(){}, removeChild(){}, insertBefore(){}, focus(){}, click(){},
  setAttribute(){}, getAttribute(){ return null; } });
const g = id => (store[id] || (store[id] = mk(id)));
for (const id of ['main', 'hero', 'heroScore', 'heroVerdict', 'foot', 'toc', 'offline', 'hint'])
  g(id);
const document = {
  getElementById: id => store[id] || null,
  querySelector: () => null,
  querySelectorAll: () => [],
  createElement: () => mk(''),
  addEventListener() {}, documentElement: {}, body: mk('body')
};
const window = { devicePixelRatio: 1, scrollY: 0, innerWidth: 1200, addEventListener() {},
  removeEventListener() {}, print() {} };
window.window = window;
const localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
const location = { href: 'file:///probe.html', origin: 'null', reload() {} };
const navigator = {};
const ctx = { document, window, localStorage, location, navigator, Promise, queueMicrotask,
  fetch: () => Promise.reject(new Error('probe: no network')),
  setTimeout, clearTimeout, console, JSON, Math, Object, Array, String, Number, Date,
  requestAnimationFrame: () => 0, cancelAnimationFrame() {}, reportError() {} };

// 后面只取 `#main`：模板脚本之外不再有别的 <script>，所以模板脚本 = 页面剩余部分。
const rest = src.slice(j + 1);
const end = rest.indexOf('</script>');
try { vm.runInNewContext(js + rest.slice(0, end < 0 ? rest.length : end), ctx); }
catch (e) { process.stderr.write('PROBE_RUN_ERROR: ' + (e && e.stack || String(e)) + '\n'); }

async function flush(rounds){
  for (let k = 0; k < rounds; k++) await new Promise(res => setTimeout(res, 0));
}
(async () => {
  let out = '';
  for (let k = 0; k < 20; k++){
    await flush(4);
    const main = store['main'];
    out = main ? String(main.innerHTML || '') : '';
    if (out) break;
  }
  process.stdout.write(Buffer.from(JSON.stringify(out), 'utf8').toString('base64'));
})();
"""


def _know_meta_text(data):
    """跑模板 JS，只回 `#knowMeta` 那个元素**自己的**文本（没有这个元素 → None）。

    为什么是元素文本而不是整页搜索：整页里本来就有模板源码 —— 「搜整页」等于搜源码。
    """
    html = _render_page(data)
    i = html.find('id="knowMeta"')
    if i < 0:
        return None
    k = html.find('>', i)
    m = html.find('</div>', k)
    return html[k + 1:m]


def _kreuse_text(data, node_id="n1"):
    """取某张卡片上**那行出处**自己的文本（没有这一行 → None）。口径同 `_know_meta_text`。

    一样**不许**在整页里搜字符串（模板源码里就写着那些字，那等于搜源码）。
    """
    html = _render_page(data)
    i = html.find('id="kreuse-%s"' % node_id)
    if i < 0:
        return None
    k = html.find('>', i)
    return html[k + 1:html.find('</div>', k)]


def _render_page(data):
    """跑模板 JS，回 `#main` 的 innerHTML（渲染结果本身）。"""
    tmp = None
    try:
        tmp = tempfile.mkdtemp()
        # ⚠️ 必须是**真模板**填占位符拼出来的页面（与 app.render_report 同一口径）。
        tpl = store.read_text(app.TEMPLATE_PATH)
        payload = (tpl.replace("__TITLE__", "探针")
                      .replace("__REPORT_DATA__",
                               json.dumps(data, ensure_ascii=False).replace("</", "<\\/")))
        script = os.path.join(tmp, "probe.js")
        store.write_text(script, PROBE)
        out = subprocess.run([NODE, script], input=payload.encode("utf-8"),
                             capture_output=True, timeout=60)
        if out.returncode != 0:
            raise AssertionError("Node 探针失败（exit=%s）：%s"
                                 % (out.returncode, out.stderr.decode("utf-8", "replace")[-2000:]))
        html = json.loads(base64.b64decode(out.stdout.decode("ascii").strip()).decode("utf-8"))
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
    if not html:
        raise AssertionError("探针跑完但页面是空的（渲染根本没发生）—— 断言会变成恒真的假绿")
    return html


class KnowledgeMetaTemplateTest(IsolatedCase):
    """模板那一行：三种 source 的文案 + 缺失/垃圾时不显示 + `esc()` 生效。"""

    TERM = "语音识别"

    def setUp(self):
        super().setUp()
        need_node()          # 缺 Node → 红（理由见 `need_node`），**不**静默跳过

    def _html(self, meta):
        return _know_meta_text(self._data(meta))

    def _data(self, meta):
        data = {"job_title": "语音数据质检", "score": 91.5, "verdict": "建议投递",
                "scores": [{"name": "匹配度", "score": 91.5, "weight": 10}],
                "knowledge": [{"id": "n1", "term": self.TERM, "definition": "d", "layer": 0,
                               "sources": [], "timeline": [], "related": [],
                               "interview_questions": [], "confidence": "guess"}]}
        if meta is not None:
            data["knowledge_meta"] = meta
        return data

    def test_cache_source_says_which_generation_it_reuses(self):
        got = self._html({"reused": True, "at": "2026-10-03 00:35", "source": "cache"})
        self.assertIsNotNone(got, "缓存复用时报告里没有来源标记那一行")
        self.assertEqual(got, "📚 知识地图：复用该岗位 2026-10-03 生成的那份（本次没有重新搜索）")

    def test_seeded_source_says_it_came_from_an_existing_report(self):
        got = self._html({"reused": True, "at": "2026-10-20 09:00",
                          "source": "seeded:20261003_岗A_75分"})
        self.assertEqual(got, "📚 知识地图：复用该岗位已有报告里的那份（本次没有重新搜索）")

    def test_computed_source_says_it_was_computed_this_time(self):
        got = self._html({"reused": False, "at": "2026-10-03 00:35", "source": "computed"})
        self.assertEqual(got, "📚 知识地图：本次新算")

    def test_a_report_without_knowledge_meta_shows_no_line(self):
        """**老报告**（没有这个字段）绝不显示那一行 —— 也绝不崩。"""
        data = self._data(None)
        self.assertNotIn("knowledge_meta", data)
        markup = _render_page(data)
        self.assertIn('id="sec-knowledge"', markup, "老报告那一页根本没渲染出知识地图卡片")
        self.assertIn("知识学习地图", markup)
        self.assertNotIn('id="knowMeta"', markup, "老报告被渲染出了来源标记那一行")
        self.assertIsNone(_know_meta_text(data))

    def test_junk_knowledge_meta_shows_no_line(self):
        for junk in ("垃圾", 5, [], {}, {"reused": True}, {"source": 5},
                     {"source": ""}, {"source": "别的模块的标记"}):
            with self.subTest(junk=junk):
                self.assertIsNone(self._html(junk),
                                  "垃圾 knowledge_meta 渲染出了来源标记：%r" % (junk,))

    def test_the_at_value_is_escaped(self):
        """`at` 过 `esc()`：脏数据里的尖括号绝不能变成真标签（也绝不能原样输出）。

        缓存时间戳是 `2026-10-03 00:35`，报告里只显示日期那一段（与规格 §7 的文案一致）；
        形状不对的时间戳**整串照显示** —— 这时才是 `esc()` 真正要挡住脏数据的那条路。
        """
        got = self._html({"reused": True, "at": "<b>x</b>&\"'", "source": "cache"})
        self.assertEqual(got, "📚 知识地图：复用该岗位 &lt;b&gt;x&lt;/b&gt;&amp;&quot;&#39; "
                              "生成的那份（本次没有重新搜索）")

    def test_the_cache_timestamp_shows_only_the_date(self):
        """`2026-10-03 00:35` → 报告里显示 `2026-10-03`（规格 §7 的两处文案都是「日期」）。"""
        got = self._html({"reused": True, "at": "2026-10-03 00:35", "source": "cache"})
        self.assertEqual(got, "📚 知识地图：复用该岗位 2026-10-03 生成的那份（本次没有重新搜索）")

    def test_a_weird_seeded_id_still_renders_the_canonical_wording(self):
        got = self._html({"reused": True, "at": "2026-10-03 00:35",
                          "source": "seeded:<script>alert(1)</script>"})
        self.assertEqual(got, "📚 知识地图：复用该岗位已有报告里的那份（本次没有重新搜索）")
        # 报告页里 seeded 的来源 id 原样搬进页面（`<script>` 已在落盘时转义成 `<\\/script>`）
        self.assertNotIn("<b>", got)

    def test_a_cache_meta_without_a_usable_at_still_says_reuse(self):
        """`at` 缺失/垃圾时**不能显示错话**（不能说「复用 X 生成的那份」而 X 是空的）。

        这条是**模板自己的**脏数据防线（手改坏的 `jds.json` / 旧报告里什么都可能有），
        不是「生产会送出这些形状」的断言：生产只送真字符串或不送 `at`
        （见 `AnalyzeKnowledgeCacheTest.test_the_at_shape_production_sends_matches_the_template_guard`）。
        所以这里喂原始 JSON 类型是有意的 —— 模板必须自己扛住 `5` / `[]` / `"   "`。
        """
        for at in (None, "", "   ", 5, [], {}, True):
            with self.subTest(at=at):
                got = self._html({"reused": True, "at": at, "source": "cache"})
                self.assertIsNotNone(got)
                self.assertTrue(got.startswith("📚 知识地图：复用该岗位"),
                                "at 不可用时的文案说错了：%r" % (got,))
                self.assertNotIn("生成的那份", got)


class KbReuseTemplateTest(IsolatedCase):
    """阶段 E 的报告标注（spec §8）：顶部汇总（①跑过/②没跑/③库坏了）+ 卡片逐条出处（④）。

    **最要命的一条**：`not_run`（知识地图来自缓存 / 种子数据，这次根本没跑「库优先」）
    与「跑了、库是空的（复用 0 条）」是**两件不同的事**，报告里必须分得出来。
    口径同 `_know_meta_text` / `_kreuse_text`：只断言**那个元素自己的文本**。
    """

    def setUp(self):
        super().setUp()
        need_node()          # 缺 Node → 红（理由见 `need_node`），**不**静默跳过

    def _data(self, meta=None, node_extra=None):
        node = {"id": "n1", "term": "语音识别", "definition": "d", "layer": 0,
                "sources": [], "timeline": [], "related": [],
                "interview_questions": [], "confidence": "guess"}
        node.update(node_extra or {})
        data = {"job_title": "语音数据质检", "score": 91.5, "verdict": "建议投递",
                "scores": [{"name": "匹配度", "score": 91.5, "weight": 10}],
                "knowledge": [node]}
        if meta is not None:
            data["knowledge_meta"] = meta
        return data

    def _line(self, stats, source="computed"):
        """顶部那一行：`knowledge_meta.source` 与 `kb_reuse` 在**生产里同源**（都来自 jdfacts）。"""
        return _know_meta_text(self._data({"source": source, "kb_reuse": stats}))

    def _kb(self, **kw):
        """一张**命中**卡片的 `kb_reuse` 标记（形状与 `kbreuse.plan_nodes` 写的一致）。"""
        r = {"card_id": "语音识别", "card_term": "语音识别", "term_original": "",
             "matched_by": "exact", "reusable": True, "missing": [], "got": "all",
             "origin": {"job_title": "语音/文本评测", "date": "2026-10-03"}}
        r.update(kw)
        return r

    # ------------------------------------------------------------ ① 跑过：汇总
    def test_the_summary_reports_reuse_hits_and_searches(self):
        got = self._line({"total": 15, "matched": 5, "reusable": 3, "timeline_reused": 2,
                          "enriched": 2, "searched": 12, "knowledge_error": ""})
        self.assertIsNotNone(got)
        self.assertIn("知识库：复用 3 条（含时间线 2 条）", got)
        self.assertIn("命中 5 条（其中 2 条不够格，已联网补全）", got)
        self.assertIn("本次联网 12 条", got)

    def test_the_summary_never_claims_a_capped_card_went_online(self):
        """★ 必修①：`skipped_by_cap > 0` 时**绝不许**说「已联网补全」。

        终审实测：超出搜索上限、根本没搜的那条卡 `sources` 是空的，而报告仍写「已联网补全」
        —— 那句对它是假话。上限没截断任何一条时才敢接着说那句（那时 `enriched` 条条都搜过）。
        """
        got = self._line({"total": 6, "matched": 2, "reusable": 0, "timeline_reused": 0,
                          "enriched": 2, "searched": 2, "skipped_by_cap": 4,
                          "knowledge_error": ""})
        self.assertIsNotNone(got)
        self.assertNotIn("已联网补全", got, "对没搜的卡说了「已联网补全」（说假话）")
        self.assertIn("其中 2 条不够格", got)
        self.assertIn("本次联网 2 条", got)
        self.assertIn("另有 4 条超出本次搜索上限，本次没有联网", got)

    def test_the_summary_says_which_hits_share_one_card(self):
        """必修②的报告口径：只对齐身份的那些卡片**不许**被说成「不够格」——
        那张卡可能内容齐全，只是已经归另一个节点了。"""
        got = self._line({"total": 5, "matched": 3, "reusable": 1, "timeline_reused": 0,
                          "enriched": 0, "aligned_only": 2, "searched": 2,
                          "knowledge_error": ""})
        self.assertIsNotNone(got)
        self.assertIn("命中 3 条（其中 2 条与其它节点指向同一张卡（内容为本次生成））", got)
        self.assertNotIn("不够格", got)
        self.assertNotIn("已联网补全", got)

    def test_an_old_report_without_the_new_counters_is_unchanged(self):
        """老报告没有 `skipped_by_cap` / `aligned_only` → 文案与改动前**一字不变**。"""
        got = self._line({"total": 15, "matched": 5, "reusable": 3, "timeline_reused": 2,
                          "enriched": 2, "searched": 12, "knowledge_error": ""})
        self.assertEqual(got, "📚 知识地图：本次新算；知识库：复用 3 条（含时间线 2 条）"
                              " · 命中 5 条（其中 2 条不够格，已联网补全） · 本次联网 12 条")

    def test_a_run_against_an_empty_kb_says_zero_reused(self):
        """**跑了、但库是空的** → 如实说「复用 0 条」（这才轮到这句话出场）。"""
        got = self._line({"total": 5, "matched": 0, "reusable": 0, "timeline_reused": 0,
                          "enriched": 0, "searched": 5, "knowledge_error": ""})
        self.assertIn("复用 0 条", got)
        self.assertIn("命中 0 条", got)
        self.assertIn("本次联网 5 条", got)

    # ------------------------------------------------------------ ② 没跑：绝不说 0
    def test_not_run_never_claims_zero_reused(self):
        """★ 没跑「库优先」**绝不许**说成「复用了 0 条」—— 那是两件不同的事。"""
        cases = (
            ("cache", "cache", "缓存", ""),
            ("cache", "cached:20261014_语音数据质检_88分", "缓存", "（cached:20261014_语音数据质检_88分）"),
            ("seeded:20261014_语音数据质检_88分", "seeded:20261014_语音数据质检_88分",
             "种子数据", "（seeded:20261014_语音数据质检_88分）"),
        )
        for meta_src, r_src, how, raw in cases:
            with self.subTest(source=r_src):
                got = self._line({"not_run": True, "source": r_src}, source=meta_src)
                self.assertIsNotNone(got, "not_run 时那一行整个消失了")
                self.assertNotIn("复用 0 条", got, "把「没跑」说成了「复用了 0 条」")
                self.assertNotIn("0 条", got, "not_run 不该报任何条数：%r" % (got,))
                self.assertIn("来自" + how + raw, got)
                self.assertIn("没有重跑「库优先」", got)

    # ------------------------------------------------------------ ③ 库坏了：如实说
    def test_an_unreadable_kb_states_the_reason(self):
        reason = "知识库 knowledge.json 读不出来：Expecting value: line 1 column 1"
        got = self._line({"total": 15, "matched": 0, "reusable": 0, "timeline_reused": 0,
                          "enriched": 0, "searched": 15, "knowledge_error": reason})
        self.assertIsNotNone(got, "库读不出来时那一行整个消失了（假装一切正常）")
        self.assertIn("⚠ 本次没能查本地知识库：" + reason, got)
        self.assertIn("已全部改用联网搜索", got)

    def test_the_unreadable_reason_is_escaped(self):
        got = self._line({"total": 1, "matched": 0, "reusable": 0, "timeline_reused": 0,
                          "enriched": 0, "searched": 1,
                          "knowledge_error": "<img src=x onerror=alert(1)>"})
        self.assertIsNotNone(got)
        self.assertNotIn("<img", got)
        self.assertIn("&lt;img", got)

    # ------------------------------------------------------------ 垃圾 / 老报告
    def test_junk_kb_reuse_is_ignored_not_invented(self):
        """垃圾 / 形状不对的 `kb_reuse` 一律忽略：不崩，也**绝不编一段统计**出来。"""
        for junk in ("垃圾", 5, [], [1], True, {}, {"total": "15"}, {"total": None},
                     {"not_run": "yes"}, {"knowledge_error": 5}, {"reusable": 3}):
            with self.subTest(junk=junk):
                got = self._line(junk)
                self.assertEqual(got, "📚 知识地图：本次新算",
                                 "垃圾 kb_reuse 被渲染成了统计：%r → %r" % (junk, got))

    def test_a_report_without_kb_reuse_is_unchanged(self):
        """老形状（缓存 / 播种 / 新算，没有 `kb_reuse`）→ 文案**一字不变**。"""
        self.assertEqual(_know_meta_text(self._data({"source": "computed"})),
                         "📚 知识地图：本次新算")
        self.assertEqual(
            _know_meta_text(self._data({"source": "cache", "at": "2026-10-03 00:35"})),
            "📚 知识地图：复用该岗位 2026-10-03 生成的那份（本次没有重新搜索）")
        self.assertEqual(
            _know_meta_text(self._data({"reused": True, "source": "seeded:20261003_岗A_75分"})),
            "📚 知识地图：复用该岗位已有报告里的那份（本次没有重新搜索）")

    # ------------------------------------------------------------ ④ 卡片逐条出处
    def test_a_reused_card_shows_its_provenance(self):
        got = _kreuse_text(self._data(node_extra={"kb_reuse": self._kb()}))
        self.assertIsNotNone(got, "够格复用的卡片没有显示出处那一行")
        self.assertIn("来源与时间线来自本地知识库", got)
        self.assertIn("《语音/文本评测》", got)
        self.assertIn("2026-10-03", got)
        self.assertIn("本次没有重新搜索", got)

    def test_the_match_method_is_said_in_plain_chinese(self):
        for code, want in (("exact", "同名命中"), ("alias", "别名命中"),
                           ("abbrev", "括号缩写命中"), ("head", "简写命中"),
                           ("assisted", "AI 判定命中")):
            with self.subTest(matched_by=code):
                got = _kreuse_text(self._data(node_extra={"kb_reuse": self._kb(matched_by=code)}))
                self.assertIsNotNone(got)
                self.assertIn(want, got)
                self.assertNotIn(code, got, "把英文码直接甩给用户了：%r → %r" % (code, got))

    def test_a_head_name_hit_is_not_called_an_abbreviation_hit(self):
        """★ Minor④：靠**卡侧"去括号头名"**命中的（节点 `词错率` ↔ 库里 `词错率（WER）`）
        不许说成「括号缩写命中」—— 那个节点里**一个括号都没有**，用户看了莫名其妙。

        `matched_by` 是新的 `head` 档位；报告里的措辞里也不许出现「括号」。
        """
        got = _kreuse_text(self._data(node_extra={"kb_reuse": self._kb(matched_by="head")}))
        self.assertIsNotNone(got)
        self.assertIn("简写命中", got)
        self.assertNotIn("括号", got, "把'去括号头名'说成了「括号缩写命中」（Minor④）")

    def test_a_capped_card_line_says_it_did_not_go_online(self):
        """★ 必修①：超出搜索上限、**本次没搜**的那条卡不许说「本次仍全部联网搜索补齐」。

        它 `sources` 是空的，正因为它压根没搜 —— 节点上的 `search_skipped` 戳是报告唯一的
        依据（只看 `sources` 分不出"搜了没搜到"与"没搜"）。
        """
        got = _kreuse_text(self._data(node_extra={
            "kb_reuse": self._kb(reusable=False, missing=["sources"], got=""),
            "search_skipped": "capped"}))
        self.assertIsNotNone(got)
        self.assertNotIn("联网搜索补齐", got, "对没联网的卡说「仍全部联网搜索补齐」（说假话）")
        self.assertIn("本次没有联网搜索", got)
        self.assertIn("超出本次搜索上限", got)

    def test_an_insufficient_hit_also_shows_the_original_jd_wording(self):
        """★ 必修③：**不够格**那一支也要渲染 `term_original`。

        判定认错（模型说 A 就是 B）时这条路径会永久改掉库里那张卡的内容，而**卡内容没有
        版本历史** —— 让用户至少看得见"JD 里写作 XX"。
        """
        got = _kreuse_text(self._data(node_extra={"kb_reuse": self._kb(
            reusable=False, missing=["sources"], got="", term_original="ASR",
            matched_by="assisted")}))
        self.assertIsNotNone(got)
        self.assertIn("库里内容不够格", got)
        self.assertIn("JD 里写作「ASR」", got, "不够格那一支没渲染 JD 原本的写法（必修③）")

    def test_an_aligned_only_card_does_not_claim_the_kb_content(self):
        """★ 必修②的报告口径：只对齐身份那条**不是**"不够格"，内容也不是库里的。

        它的卡片与**另一条**节点指向同一张卡（那张卡可能内容齐全），内容全是本次生成的。
        """
        got = _kreuse_text(self._data(node_extra={"kb_reuse": self._kb(
            reusable=False, missing=[], got="", matched_by="assisted",
            term_original="ASR", aligned_only=True)}))
        self.assertIsNotNone(got)
        self.assertNotIn("来自本地知识库", got, "内容不是库里的，这样说就是假话")
        self.assertNotIn("不够格", got, "那张卡只是已经归另一个节点了，不是'内容不够格'")
        self.assertIn("同一张卡", got)
        self.assertIn("这条的内容是本次生成的", got)
        self.assertIn("AI 判定命中", got)
        self.assertIn("JD 里写作「ASR」", got)

    def test_an_aligned_only_card_that_was_capped_says_so(self):
        """两条修一起时的交叉情形：只对齐身份 **且** 超出搜索上限 → 不许说"仍联网"。"""
        got = _kreuse_text(self._data(node_extra={
            "kb_reuse": self._kb(reusable=False, missing=[], got="", aligned_only=True),
            "search_skipped": "capped"}))
        self.assertIsNotNone(got)
        self.assertIn("同一张卡", got)
        self.assertNotIn("联网搜索补齐", got)
        self.assertIn("本次没有联网搜索", got)

    def test_a_reused_card_says_the_original_jd_wording_and_the_timeline_catch_up(self):
        got = _kreuse_text(self._data(node_extra={"kb_reuse": self._kb(
            term_original="ASR", matched_by="assisted", got="sources",
            origin={"job_title": "", "date": ""})}))
        self.assertIsNotNone(got)
        self.assertIn("来源链接来自本地知识库", got)
        self.assertIn("JD 里写作「ASR」", got)
        self.assertIn("时间线为本次补算，已写回知识库", got)
        self.assertNotIn("来源与时间线来自本地知识库", got,
                         "got=sources 时绝不能说时间线也是库里的")

    def test_a_card_that_was_only_enriched_does_not_claim_the_kb(self):
        """命中但**不够格**（本次照旧联网）→ 要说出来，但**不许**说内容来自知识库。"""
        got = _kreuse_text(self._data(node_extra={"kb_reuse": self._kb(
            reusable=False, missing=["sources"], got="")}))
        self.assertIsNotNone(got, "命中但不够格的卡片被整个藏掉了")
        self.assertNotIn("来自本地知识库", got, "内容不是库里的，这样说就是假话")
        self.assertIn("命中", got)
        self.assertIn("不够格", got)
        self.assertIn("来源链接", got)
        self.assertIn("联网", got)

    def test_a_card_without_the_marker_shows_no_reuse_line(self):
        self.assertIsNone(_kreuse_text(self._data()), "老报告 / 联网算的卡片不该有这一行")

    def test_junk_card_marker_shows_no_reuse_line(self):
        for junk in ("垃圾", 5, [], [1], True, {}, {"reusable": "yes"}, {"reusable": None}):
            with self.subTest(junk=junk):
                self.assertIsNone(_kreuse_text(self._data(node_extra={"kb_reuse": junk})),
                                  "垃圾卡片标记渲染出了出处行：%r" % (junk,))

    def test_the_origin_and_the_original_term_are_escaped(self):
        got = _kreuse_text(self._data(node_extra={"kb_reuse": self._kb(
            term_original="<b>ASR</b>", origin={"job_title": "<script>", "date": "2026-10-03"})}))
        self.assertIsNotNone(got)
        self.assertNotIn("<b>", got)
        self.assertNotIn("<script>", got)
        self.assertIn("&lt;b&gt;ASR&lt;/b&gt;", got)
        self.assertIn("&lt;script&gt;", got)


# ---------------------------------------------------------------- 模板铁律
class TemplateHygieneTest(IsolatedCase):
    def test_the_marker_line_only_uses_escaped_values(self):
        """源码级铁律：那一行里插进 HTML 的值必须全部过 `esc()`（`at` / seeded id / 兜底）。"""
        src = store.read_text(app.TEMPLATE_PATH)
        self.assertTrue(src)
        i = src.find("function knowMetaLine")
        self.assertGreater(i, 0, "模板里找不到 knowMetaLine（这一行是实现的一部分）")
        j = src.find("\n}", i)
        body = src[i:j]
        self.assertTrue(body, "knowMetaLine 是空的？")
        self.assertIn("esc(at)", body)
        for needle in ("'<div class=\"know-meta\" id=\"knowMeta\">'", "📚 知识地图：本次新算"):
            self.assertIn(needle, body)
        # 反证：`at` 绝不是直接拼进去的
        self.assertNotIn("+at+", body)


# ================================================================ P2-2 链接协议白名单
#
# 断言的是**渲染出来的** `href`，不是源码里有没有 `esc(`：
# `esc()` 只转 `&<>"'`、**不转义冒号**，所以 `esc("javascript:alert(1)")` 原样落进 href 就是一条
# 可点的"脚本链接"。而这些串是**外部字符串**：`sources[].url` 来自模型/搜索，
# `source_link` 来自台账 JSON（用户手改也算）。
#
# 三个入口（报告来源 / 报告原岗位链接 / 台账原岗位链接）**都要**降级成纯文本：
# 内容仍然看得见（不静默消失），只是点不动。
FAKE_LINKS = (
    ("经典 javascript:", "javascript:alert(1)"),
    ("带协议头的 data:", "data://x"),
    ("data: 内联页面", "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg=="),
    ("vbscript:", "vbscript:msgbox(1)"),
    ("大小写混写", "JaVaScRiPt:alert(1)"),
    ("前导空白", "  javascript:alert(1)"),
    ("file:", "file:///C:/Windows/System32/calc.exe"),
    # 协议相对 URL **本批裁定不放行**：它继承"打开这份文档的协议" —— 报告既可能从
    # `http://127.0.0.1:8000` 打开，也可能被用户另存后从 `file://` 打开，那时 `//host/x`
    # 就变成一条本项目从未评估过的 `file://` 链接。仓库里没有任何生产者会产出协议相对 URL
    # （岗位链接都是模型/搜索给的绝对 http(s) 地址），放行它的收益是 0，所以按"不认识就不点"处理。
    ("协议相对", "//evil.example.com/x"),
)

#: href 里**绝不许**出现的开头（小写比较）。`//` 见上面的裁定。
BAD_HREF_PREFIXES = ("javascript:", "data:", "vbscript:", "file:", "//")

GOOD_URL = "https://example.com/a"


def _anchor_tags(html):
    """渲染结果里真渲染出来的锚点（`<a …>` 整段，含属性）。"""
    return re.findall(r"<a\b[^>]*>", html)


def _hrefs(html):
    """渲染结果里全部锚点的 href 值（不跨标签匹配：`[^>]*?` 出不了这个标签）。"""
    return [m.group(1) for m in re.finditer(r'<a\b[^>]*?\bhref="([^"]*)"', html)]


def _safe_url_body(path):
    """取出某个文件里 `safeUrl()` 的函数体（找不到 → **响亮失败**，别静默返回空串）。"""
    src = store.read_text(path)
    anchor = "function safeUrl(u){"
    i = src.find(anchor)
    if i < 0:
        raise AssertionError("%s 里没有 safeUrl()（吃外部字符串的 href 靠它兜底）" % path)
    self_end = src.find("\n}", i)
    if self_end < 0:
        raise AssertionError("%s 里的 safeUrl() 没有结束（切不出去）" % path)
    body = src[i:self_end]
    if len(body) < 40:
        raise AssertionError("%s 里的 safeUrl() 太短（切错了？）：%r" % (path, body))
    return body


def _report_data_with(sources=None, source_link=None, jd_image=None):
    """一份最小可渲染的报告数据 + 要测的那个外部链接字段。"""
    data = {"job_title": "语音数据质检", "score": 91.5, "verdict": "建议投递",
            "scores": [{"name": "匹配度", "score": 91.5, "weight": 10}],
            "knowledge": [{"id": "n1", "term": "语音识别", "definition": "d", "layer": 0,
                           "sources": list(sources or []), "timeline": [], "related": [],
                           "interview_questions": [], "confidence": "guess"}]}
    if source_link is not None:
        data["source_link"] = source_link
    if jd_image is not None:
        data["jd_image"] = jd_image
    return data


def _ledger_html(row):
    """**真跑**台账页的 `renderJobs()`，回 `#jobList` 的渲染结果。空结果 → 直接报错。"""
    need_node()
    out = web_probe.run_index(
        "JOBS = [__fixture.row];\n"
        "renderJobs();\n"
        "out.html = String(document.querySelector('#jobList').innerHTML);\n",
        {"row": row})
    html = out.get("html") or ""
    if not html:
        raise AssertionError("台账卡片什么都没渲染（渲染没发生 → 断言会变成恒真的假绿）：%r" % (out,))
    return html


class LinkSchemeWhitelistTest(IsolatedCase):
    """P2-2：吃外部字符串的 `href` 只放行绝对 http(s)，其余一律降级成纯文本。"""

    def setUp(self):
        super().setUp()
        need_node()

    def _assert_no_bad_href(self, html, raw):
        """href 里绝不许有伪协议。

        `strip()` 是**必须**的：浏览器解析 href 时会丢掉首尾空白与制表/换行，
        所以 `href="  javascript:alert(1)"` 点下去照样执行 —— 不 strip 就会看漏这一条。
        """
        for href in _hrefs(html):
            self.assertFalse(
                href.strip().lower().startswith(BAD_HREF_PREFIXES),
                "伪协议被渲染成了可点的链接：href=%r（原始输入 %r）" % (href, raw))

    # ---------------------------------------------------------- ① 报告来源链接
    def test_a_fake_scheme_source_url_is_plain_text_instead_of_a_link(self):
        """`sources[].url`（**模型/搜索返回的 URL**）非 http(s) → 不生成 `<a>`，内容仍可见。"""
        for name, url in FAKE_LINKS:
            with self.subTest(scheme=name):
                # 不带 title：来源那一行显示的就是 URL 本身 ⇒ 能直接断言"那串文本仍然看得见"
                html = _render_page(_report_data_with(sources=[{"url": url}]))
                self._assert_no_bad_href(html, url)
                # 这一份数据里 #main 唯一可能的链接就是这条来源 ⇒ 它一个锚点都不该生成
                self.assertEqual(_anchor_tags(html), [],
                                 "非 http(s) 的来源居然生成了锚点：%r" % (_anchor_tags(html),))
                self.assertIn(url.strip(), html, "降级成纯文本时那串内容消失了（用户该看得见）")
        # 有 title 时显示 title（标题照样不许把 href 变成链接）
        html = _render_page(_report_data_with(sources=[{"url": "javascript:alert(1)",
                                                        "title": "某来源"}]))
        self._assert_no_bad_href(html, "javascript:alert(1)")
        self.assertIn("某来源", html, "连来源标题一起消失了")

    # ---------------------------------------------------------- ② 报告原岗位链接
    def test_a_fake_scheme_source_link_is_plain_text_instead_of_a_link(self):
        """`source_link` 非 http(s) → 整条不生成 `<a>`，但那一行文案与地址本身仍然看得见。"""
        for name, url in FAKE_LINKS:
            with self.subTest(scheme=name):
                html = _render_page(_report_data_with(source_link=url))
                self._assert_no_bad_href(html, url)
                self.assertIn("原岗位链接", html, "整条链接被删掉了（该降级成纯文本，不是消失）")
                self.assertIn(url.strip(), html, "降级成纯文本时那串内容消失了（用户该看得见）")
                self.assertIn('title="链接不可用', html,
                              "降级之后没有告诉用户为什么点不动：%r" % (html,))

    # ---------------------------------------------------------- ③ 台账原岗位链接
    def test_the_ledger_original_job_link_degrades_for_a_fake_scheme(self):
        """台账（`web/index.html`）里那条原岗位链接 —— 同一个判据。"""
        for name, url in FAKE_LINKS:
            with self.subTest(scheme=name):
                html = _ledger_html({"id": "j-1", "job_title": "语音数据质检", "source_link": url})
                self._assert_no_bad_href(html, url)
                self.assertIn("原岗位链接", html, "整条链接被删掉了（该降级成纯文本，不是消失）")
                self.assertIn(url.strip(), html, "降级成纯文本时那串内容消失了（用户该看得见）")

    # ---------------------------------------------------------- ④ 对照组：正常链接照旧可点
    def test_a_normal_https_link_is_still_a_real_link(self):
        """**别把正常链接也杀了**：`https://…` 三个入口都仍然渲染成真 `<a href>`。"""
        html = _render_page(_report_data_with(sources=[{"url": GOOD_URL, "title": "官网"}]))
        self.assertIn('<a href="' + GOOD_URL + '"', html, "正常的 https 来源被降级了")

        html = _render_page(_report_data_with(source_link=GOOD_URL))
        self.assertIn('<a class="lnk" href="' + GOOD_URL + '"', html, "正常的 https 原岗位链接被降级了")

        html = _ledger_html({"id": "j-1", "job_title": "语音数据质检", "source_link": GOOD_URL})
        self.assertIn('href="' + GOOD_URL + '"', html, "台账里正常的 https 原岗位链接被降级了")

    # ---------------------------------------------------------- ⑤ 两个文件里的 safeUrl 必须同源
    def test_safe_url_is_the_same_function_in_both_files(self):
        """`safeUrl()` 在两个文件各有一份（自包含是必须的），但**逻辑必须逐字一致**。

        报告模板与台账页是两份独立文件（报告要能单独打开），所以函数不能合并 ——
        代价就是"改一处忘了另一处"。这条把代价钉住：两份的函数体必须一字不差。
        """
        index_body = _safe_url_body(web_probe.WEB_INDEX)
        tpl_body = _safe_url_body(app.TEMPLATE_PATH)
        self.assertEqual(index_body, tpl_body,
                         "两份 safeUrl() 的逻辑不一致了（改一处必须改另一处）")
        self.assertIn("/^https?:\\/\\//i", index_body, "判据不是 http(s) 白名单了？")
        # 提成 safeUrl 之后不该再有 kbSafeUrl 这个旧名字（有两个名字就会两处漂移）
        self.assertNotIn("kbSafeUrl", store.read_text(web_probe.WEB_INDEX))

    # ---------------------------------------------------------- ⑥ target=_blank 必须配 noopener
    def test_every_target_blank_anchor_carries_noopener(self):
        """反制 `window.opener` 劫持：**报告模板那两处**（来源 / 原岗位链接；截图那处也一并补上）。

        两个文件里**每一条** `target="_blank"` 都要有 `rel="noopener` —— 数一下锚点数量，
        免得这条断言因为"某个链接没渲染"而空跑。
        """
        html = _render_page(_report_data_with(sources=[{"url": GOOD_URL, "title": "官网"}],
                                             source_link=GOOD_URL, jd_image="jd1.png"))
        blank = [a for a in _anchor_tags(html) if 'target="_blank"' in a]
        self.assertEqual(len(blank), 3,
                         "报告里带 target=_blank 的锚点数不是 3 条（这条断言会空跑）：%r" % (blank,))
        self.assertEqual([a for a in blank if 'rel="noopener' not in a], [],
                         "报告里 target=_blank 没配 rel=noopener：%r"
                         % ([a for a in blank if 'rel="noopener' not in a],))

        ledger = _ledger_html({"id": "j-1", "source_link": GOOD_URL, "report": "r.html",
                               "jd_image": "jd1.png"})
        lblank = [a for a in _anchor_tags(ledger) if 'target="_blank"' in a]
        self.assertEqual(len(lblank), 3,
                         "台账里带 target=_blank 的锚点数不是 3 条（这条断言会空跑）：%r" % (lblank,))
        self.assertEqual([a for a in lblank if 'rel="noopener' not in a], [],
                         "台账里 target=_blank 没配 rel=noopener：%r"
                         % ([a for a in lblank if 'rel="noopener' not in a],))


if __name__ == "__main__":
    unittest.main()
