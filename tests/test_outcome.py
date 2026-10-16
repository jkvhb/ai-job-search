# -*- coding: utf-8 -*-
"""outcome.py —— 结果记录 + 归因：终态门槛、证据过滤、用户原因不被覆盖。

铁律：
  * **测试绝不真调模型** —— chat 一律注入假的（约定 `chat(prompt, cfg)`，与 interview.analyze 一致）
  * **绝不碰真实 data/** —— 全部继承 IsolatedCase（同时重定向 store.DATA_ROOT 与 log.LOG_FILE）
  * to_study **只列不建卡** —— 用户明确要求新词手动入库（怕知识库被灌爆）
"""
import json
import os
import unittest
from unittest import mock

import kb
import outcome
import store

from tests.base import IsolatedCase


def fake_chat(text):
    """注入约定与 interview.analyze 一致：`chat(prompt, cfg)` —— 两参数。"""
    return lambda prompt, cfg: text


def _job(job_id="j1", **kw):
    """每个用例先造出这个岗位 —— save_result 对不存在的岗位会抛 LookupError。

    （真实的流程里岗位总是先存在：结果是你在台账里对已有的岗位记录的。）
    """
    store.upsert_job(dict({"id": job_id, "job_title": "AI训练标注师",
                           "job_category": "数据标注/质检"}, **kw))


class ResultFieldsTest(IsolatedCase):
    def test_save_result_writes_status_reason_and_time(self):
        _job()
        outcome.save_result("j1", "已拒", "HR 说岗位预算冻结了")
        app_rec = outcome.application_of("j1")
        self.assertEqual(app_rec["status"], "已拒")
        self.assertEqual(app_rec["outcome_reason"], "HR 说岗位预算冻结了")
        self.assertTrue(app_rec["outcome_at"])

    def test_rejects_unknown_status(self):
        _job()
        with self.assertRaises(ValueError):
            outcome.save_result("j1", "胡说", "x")

    def test_rejects_unknown_job(self):
        with self.assertRaises(LookupError):
            outcome.save_result("没有这个岗位", "已拒", "x")

    def test_user_reason_survives_attribution(self):
        _job()
        outcome.save_result("j1", "已拒", "我自己写的原因")
        outcome.attribute("j1", cfg={}, chat=fake_chat('{"verdict_summary":"总评"}'))
        self.assertEqual(outcome.application_of("j1")["outcome_reason"], "我自己写的原因")

    def test_junk_fields_are_tolerated(self):
        _job(application={"status": 5, "outcome_ai": "不是字典"})
        a = outcome.application_of("j1")
        self.assertIsInstance(a.get("outcome_ai"), (dict, type(None)))


class TerminalGateTest(IsolatedCase):
    def test_only_terminal_states_can_be_attributed(self):
        for st in ("未投递", "已投", "待回复", "面试中"):
            with self.subTest(st=st):
                _job()
                outcome.save_result("j1", st, "")
                with self.assertRaises(outcome.NotTerminal):
                    outcome.attribute("j1", cfg={}, chat=fake_chat("{}"))

    def test_terminal_states_pass_the_gate(self):
        for st in outcome.TERMINAL:
            with self.subTest(st=st):
                _job()
                outcome.save_result("j1", st, "")
                self.assertTrue(outcome.can_attribute("j1"))


class NeverRecordedGateTest(IsolatedCase):
    """没记过状态的岗位**也要拦** —— 终态门槛是严格版，没有例外。

    计划原稿里的 EvidenceFilterTest 直接对裸岗位归因、与本条冲突；**裁定按严格语义**（判据只有
    `_recorded_status(job) in TERMINAL`，缺失/非法一律当非终态）。理由：畸形 / 手改 / 遗留记录若
    被放行，AI 就会给一个「从未记录过结果」的岗位写「你为什么没拿到 offer」——
    正是本设计要防的「没有依据却言之凿凿」。用户看到「还没有结果」→ 去记录结果 → 才能归因，
    这恰好就是「先记结果，再复盘」的流程顺序。
    """

    def test_a_job_with_no_recorded_result_is_gated(self):
        _job()
        self.assertFalse(outcome.can_attribute("j1"))
        with self.assertRaises(outcome.NotTerminal) as cm:
            outcome.attribute("j1", cfg={}, chat=fake_chat("{}"))
        msg = str(cm.exception)
        self.assertIn("还没有结果", msg)      # Task 2 的 400 文案靠这句
        self.assertNotIn("未投递", msg)        # 不该谎报一个用户从没填过的状态

    def test_recording_a_non_terminal_status_also_closes_the_gate(self):
        _job()
        outcome.save_result("j1", "已投", "")
        self.assertFalse(outcome.can_attribute("j1"))
        with self.assertRaises(outcome.NotTerminal) as cm:
            outcome.attribute("j1", cfg={}, chat=fake_chat("{}"))
        self.assertIn("当前状态：已投", str(cm.exception))     # 记过就说真实状态

    def test_junk_status_is_gated_like_never_recorded(self):
        # 状态是垃圾类型（5）：与「没记过」等价 → 一律拦；展示默认值不参与判门
        _job(application={"status": 5})
        self.assertEqual(outcome.application_of("j1")["status"], "未投递")   # 只是展示默认值
        self.assertFalse(outcome.can_attribute("j1"))
        with self.assertRaises(outcome.NotTerminal):
            outcome.attribute("j1", cfg={}, chat=fake_chat("{}"))


class EvidenceFilterTest(IsolatedCase):
    def test_reasons_without_evidence_are_dropped(self):
        _job()
        outcome.save_result("j1", "已拒", "")      # 归因的前提：先记结果（终态）
        reply = json.dumps({"possible_reasons": [
            {"reason": "有依据的", "evidence": "面试里问了你 Kappa，你答成了准确率", "confidence": "高"},
            {"reason": "没依据的", "evidence": "", "confidence": "高"},
            {"reason": "缺字段的", "confidence": "低"},
        ]}, ensure_ascii=False)
        out = outcome.attribute("j1", cfg={}, chat=fake_chat(reply))
        self.assertEqual([r["reason"] for r in out["possible_reasons"]], ["有依据的"])

    def test_missing_controllable_stays_unjudged(self):
        """模型漏标 controllable → **None（未判断）**，绝不默认成「可控」。

        默认 True 是在没有依据的情况下宣称「这事你能控制」——与「没有 evidence 的原因要丢弃」
        是同一类错误。报告模板应把 None 单列成「未判断」。
        """
        _job()
        outcome.save_result("j1", "已拒", "")
        reply = json.dumps({"possible_reasons": [{"reason": "没标可控性的", "evidence": "面试里的某个点"}]},
                           ensure_ascii=False)
        out = outcome.attribute("j1", cfg={}, chat=fake_chat(reply))
        self.assertIsNone(out["possible_reasons"][0]["controllable"])
        self.assertIsNone(out["possible_reasons"][0].get("controllable"))

    def test_controllable_is_kept_when_the_model_says_so(self):
        # 反向对照：显式标了就必须原样保留，否则「漏标→None」可以靠「永远 None」蒙过
        _job()
        outcome.save_result("j1", "已拒", "")
        reply = json.dumps({"possible_reasons": [
            {"reason": "可控的", "evidence": "证据甲", "controllable": True},
            {"reason": "不可控的", "evidence": "证据乙", "controllable": False},
        ]}, ensure_ascii=False)
        out = outcome.attribute("j1", cfg={}, chat=fake_chat(reply))
        self.assertEqual([r["controllable"] for r in out["possible_reasons"]], [True, False])

    def test_all_dropped_is_marked_honestly(self):
        _job()
        outcome.save_result("j1", "已拒", "")
        reply = json.dumps({"possible_reasons": [{"reason": "泛泛而谈"}]}, ensure_ascii=False)
        out = outcome.attribute("j1", cfg={}, chat=fake_chat(reply))
        self.assertEqual(out["possible_reasons"], [])
        self.assertTrue(out["reasons_all_dropped"])          # 报告据此如实说明

    def test_missing_sections_get_empty_defaults(self):
        _job()
        outcome.save_result("j1", "已拒", "")
        out = outcome.attribute("j1", cfg={}, chat=fake_chat('{"verdict_summary":"只有总评"}'))
        self.assertEqual(out["actions"], [])
        self.assertEqual(out["to_study"], [])
        self.assertEqual(out["not_controllable"], [])

    def test_to_study_never_creates_cards(self):
        _job()
        outcome.save_result("j1", "已拒", "")
        before = len(store.load_knowledge()["nodes"])
        reply = json.dumps({"to_study": [{"term": "分层抽样", "definition": "按类别比例抽"}]},
                           ensure_ascii=False)
        outcome.attribute("j1", cfg={}, chat=fake_chat(reply))
        self.assertEqual(len(store.load_knowledge()["nodes"]), before)   # 绝不自动建卡


class DegradedAttributionTest(IsolatedCase):
    def test_without_interview_records_it_says_so(self):
        _job()
        outcome.save_result("j1", "已拒", "预算冻结")
        out = outcome.attribute("j1", cfg={}, chat=fake_chat('{"verdict_summary":"s"}'))
        self.assertEqual(out["interview_count"], 0)
        self.assertTrue(out["degraded_no_interview"])        # 报告据此说明"没看到面试过程"

    def test_with_interview_records_it_feeds_them(self):
        _job()
        outcome.save_result("j1", "已拒", "")
        store.save_interview_json("iv1", "analysis.json", {"weak_concepts": [{"term": "Kappa"}]})
        store.save_interview_json("iv1", "status.json", {"state": "done", "job_id": "j1"})
        seen = {}

        def chat(prompt, cfg):
            seen["prompt"] = prompt
            return '{"verdict_summary":"s"}'

        outcome.attribute("j1", cfg={}, chat=chat)
        self.assertIn("Kappa", seen["prompt"])               # 面试复盘真被喂进去了


# ---------------------------------------------------------------- 以下为计划外的加固用例
class LedgerSyncTest(IsolatedCase):
    """台账顶层字段与 application 子字典必须是同一件事的两种写法，绝不各说各话。"""

    def test_terminal_is_a_subset_of_the_existing_statuses(self):
        # 状态词表已经存在（store.STATUSES），outcome 绝不另造一套
        for st in outcome.TERMINAL:
            self.assertIn(st, store.STATUSES)

    def test_save_result_also_updates_the_ledger_status(self):
        # 真实 jobs.json 是**扁平**的（status/notes 在顶层），台账行内徽章与按状态筛选读的就是它。
        # 不镜像的话：复盘面板刚存下「已拒」，台账里还是「未投递」——同一件事两个说法。
        _job(notes="我自己写的备注")
        outcome.save_result("j1", "已拒", "预算冻结")
        job = store.find_job("j1")
        self.assertEqual(job["status"], "已拒")
        self.assertEqual(job["application"]["status"], "已拒")
        self.assertEqual(job["notes"], "我自己写的备注")     # 只写该写的，别的字段一个字不动

    def test_state_reads_fall_back_to_the_ledger_fields(self):
        # 老岗位记录里没有 application 子字典：状态/备注要能从顶层读到，不能凭空归零
        _job(status="面试中", notes="已有备注")
        app_rec = outcome.application_of("j1")
        self.assertEqual(app_rec["status"], "面试中")
        self.assertEqual(app_rec["notes"], "已有备注")
        self.assertFalse(outcome.can_attribute("j1"))        # 面试中不是终态

    def test_junk_status_is_not_trusted_as_terminal(self):
        # application.status = "已拒" 但顶层台账说「面试中」→ 以台账为准，绝不误开归因
        _job(status="面试中", application={"status": "已拒"})
        self.assertEqual(outcome.application_of("j1")["status"], "面试中")
        with self.assertRaises(outcome.NotTerminal):
            outcome.attribute("j1", cfg={}, chat=fake_chat("{}"))


class AttributionStorageTest(IsolatedCase):
    def test_attribution_is_stored_on_the_job(self):
        _job()
        outcome.save_result("j1", "已拒", "预算冻结")
        self.assertIsNone(outcome.application_of("j1")["outcome_ai"])   # 还没归因：如实回 None
        out = outcome.attribute("j1", cfg={}, chat=fake_chat('{"verdict_summary":"总评"}'))
        self.assertEqual(outcome.application_of("j1")["outcome_ai"], out)   # 真落到 application.outcome_ai
        self.assertEqual(outcome.application_of("j1")["outcome_ai"]["verdict_summary"], "总评")

    def test_application_of_unknown_job_raises_lookup(self):
        with self.assertRaises(LookupError):
            outcome.application_of("没有这个岗位")

    def test_model_order_is_preserved(self):
        """模型给的顺序就是优先级顺序 —— 绝不排序。

        ⚠️ 断言必须让「兜底顺序」（按术语排序）处于**不利位置**：甲(U+7532) > 乙(U+4E59)，
        所以排序兜底会把顺序反过来 —— 一旦有人加了 sort，这条立刻变红。
        """
        _job()
        outcome.save_result("j1", "已拒", "")      # 归因的前提：先记结果（终态）
        reply = json.dumps({"possible_reasons": [
            {"reason": "甲原因", "evidence": "证据甲", "confidence": "高"},
            {"reason": "乙原因", "evidence": "证据乙", "confidence": "中"},
        ], "to_study": [{"term": "甲术语"}, {"term": "乙术语"}]}, ensure_ascii=False)
        out = outcome.attribute("j1", cfg={}, chat=fake_chat(reply))
        self.assertEqual([r["reason"] for r in out["possible_reasons"]], ["甲原因", "乙原因"])
        self.assertEqual([t["term"] for t in out["to_study"]], ["甲术语", "乙术语"])

    def test_only_this_jobs_interviews_are_fed(self):
        _job(job_id="j1")
        _job(job_id="j2")
        outcome.save_result("j1", "已拒", "")
        store.save_interview_json("iv1", "analysis.json", {"weak_concepts": [{"term": "Kappa"}]})
        store.save_interview_json("iv1", "status.json", {"job_id": "j1"})
        # 别的岗位的面试记录：喂进去就是把别人的面试当成你的（绝不能）
        store.save_interview_json("iv9", "analysis.json", {"weak_concepts": [{"term": "别的岗位机密"}]})
        store.save_interview_json("iv9", "status.json", {"job_id": "j2"})
        seen = {}

        def chat(prompt, cfg):
            seen["prompt"] = prompt
            return '{"verdict_summary":"s"}'

        out = outcome.attribute("j1", cfg={}, chat=chat)
        self.assertIn("Kappa", seen["prompt"])
        self.assertNotIn("别的岗位机密", seen["prompt"])
        self.assertEqual(out["interview_count"], 1)
        self.assertFalse(out["degraded_no_interview"])

    def test_to_study_does_not_touch_the_knowledge_file(self):
        _job()
        outcome.save_result("j1", "已拒", "")
        store.save_knowledge({"nodes": [{"id": "kb-existing", "term": "已有概念"}]})
        reply = json.dumps({"to_study": [{"term": "分层抽样", "definition": "按类别比例抽"}]},
                           ensure_ascii=False)
        outcome.attribute("j1", cfg={}, chat=fake_chat(reply))
        raw = store.read_text(store.knowledge_path())
        self.assertIn("已有概念", raw)          # 用户已有的卡一张没少
        self.assertNotIn("分层抽样", raw)        # 新词只列不建卡（入库由用户手动决定）

    def test_bad_json_raises_a_clear_error_and_writes_nothing(self):
        """归因失败 → 不留半截归因（规格 §6）；用户写的原因早已落盘，不会因归因失败而丢。"""
        _job()
        outcome.save_result("j1", "已拒", "预算冻结")
        with mock.patch.object(outcome.llm, "repair_json", side_effect=RuntimeError("修不动")):
            with self.assertRaises(RuntimeError) as cm:
                outcome.attribute("j1", cfg={}, chat=fake_chat("完全不是 JSON"))
        self.assertTrue(str(cm.exception).startswith("模型没有返回可解析的归因结果"),
                        "错误必须出自 outcome 自己的校验口径：%s" % cm.exception)
        self.assertIsNone(outcome.application_of("j1")["outcome_ai"])
        self.assertEqual(outcome.application_of("j1")["outcome_reason"], "预算冻结")

    def test_interviews_are_not_fed_twice_on_repeat_attribution(self):
        """重复归因允许（结果可能变），但每次只喂**一份**面试记录，绝不重复累加。"""
        _job()
        outcome.save_result("j1", "已拒", "")
        store.save_interview_json("iv1", "analysis.json", {"weak_concepts": [{"term": "Kappa"}]})
        store.save_interview_json("iv1", "status.json", {"job_id": "j1"})
        seen = []

        def chat(prompt, cfg):
            seen.append(prompt)
            return '{"verdict_summary":"s"}'

        first = outcome.attribute("j1", cfg={}, chat=chat)
        second = outcome.attribute("j1", cfg={}, chat=chat)
        self.assertEqual(first["interview_count"], 1)
        self.assertEqual(second["interview_count"], 1)
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0].count("Kappa"), seen[1].count("Kappa"))


class ReportRenderTest(IsolatedCase):
    def test_render_report_writes_oc_file_with_kind_outcome(self):
        _job()
        outcome.save_result("j1", "已拒", "预算冻结")
        out = outcome.attribute("j1", cfg={}, chat=fake_chat('{"verdict_summary":"总评"}'))
        fname = outcome.render_report(out, "j1")
        self.assertEqual(fname, "oc_j1.html")                # 前缀与面试复盘的 iv_ 区分开
        path = os.path.join(store.p_path("reports"), fname)
        self.assertTrue(os.path.isfile(path))
        data = kb.read_report_data(path)                     # 独立读盘，不经 outcome 的代码
        self.assertEqual(data["kind"], "outcome")            # Task 2 靠它选 outcome 模板
        self.assertEqual(data["verdict_summary"], "总评")
        self.assertEqual(data["outcome_reason"], "预算冻结")   # 用户原话随报告落盘
        self.assertIn("预算冻结", store.read_text(path))

    def test_render_report_survives_a_deleted_job(self):
        """归因时岗位被删 → 静默降级（少喂一块），报告照样能出，绝不崩。"""
        fname = outcome.render_report({"verdict_summary": "x"}, "已经没了的岗位")
        self.assertEqual(fname, "oc_已经没了的岗位.html")
        self.assertTrue(os.path.isfile(os.path.join(store.p_path("reports"), fname)))


if __name__ == "__main__":
    unittest.main()
