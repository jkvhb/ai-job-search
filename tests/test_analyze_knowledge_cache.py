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
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import app
import jdfacts
import jdid
import kb
import log
import store

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


# ---------------------------------------------------------------- 模板：Node 探针
NODE = shutil.which("node")

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


@unittest.skipUnless(NODE, "本机没有 Node，跳过模板那一行的渲染探针")
class KnowledgeMetaTemplateTest(IsolatedCase):
    """模板那一行：三种 source 的文案 + 缺失/垃圾时不显示 + `esc()` 生效。"""

    TERM = "语音识别"

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
        """`at` 缺失/垃圾时**不能显示错话**（不能说「复用 X 生成的那份」而 X 是空的）。"""
        for at in (None, "", "   ", 5, []):
            with self.subTest(at=at):
                got = self._html({"reused": True, "at": at, "source": "cache"})
                self.assertIsNotNone(got)
                self.assertTrue(got.startswith("📚 知识地图：复用该岗位"),
                                "at 不可用时的文案说错了：%r" % (got,))
                self.assertNotIn("生成的那份", got)


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


if __name__ == "__main__":
    unittest.main()
