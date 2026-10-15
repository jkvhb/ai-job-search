# -*- coding: utf-8 -*-
"""jobdup（JD 原文判重 / 体检 / 合并）的测试。

夹具思路：造岗位 + 它对应的 `jd/<id>.txt`（判定只认 JD 原文，不认标题/公司）。
    SHORT 是 LONG 的**截断**（去掉最后一句），复现用户真实数据的形状：
    「同一份 JD 的另一次粘贴，其中一份是截断的」。
"""
import contextlib
import glob
import hashlib
import importlib
import io
import json
import os
import sys
import unittest
from unittest import mock

import jobdup
import kb
import store

from tests.base import IsolatedCase

CLI = importlib.import_module("岗位去重")


def _fingerprint(root):
    """整个数据根的指纹：{相对路径: sha256 或 '<目录>'}（新增/删除/改动都逃不掉）"""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for d in dirnames:
            out[os.path.relpath(os.path.join(dirpath, d), root)] = "<目录>"
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            with open(p, "rb") as f:
                out[os.path.relpath(p, root)] = hashlib.sha256(f.read()).hexdigest()
    return out


class ContainmentTest(IsolatedCase):
    def test_a_truncated_paste_of_the_same_jd_is_a_duplicate(self):
        """复现实测形状：长文本 + 它的一段摘抄（**不是前缀**，长度差约 40%）。"""
        long_ = "数据质检验收把控：严格按评测标准对大模型语音通话数据质检验收；" \
                "熟悉ASR与TTS评测流程；能定位Bad Case并做归因分析；有标注规范落地经验。"
        short = "数据质检验收把控：严格按评测标准对大模型语音通话数据质检验收；" \
                "熟悉ASR与TTS评测流程；能定位Bad Case并做归因分析。"     # 去掉最后一句 ≈ 截断
        self.assertGreaterEqual(jobdup.containment(short, long_), 0.85)
        self.assertTrue(jobdup.is_duplicate(short, long_))

    def test_unrelated_jds_are_not_duplicates(self):
        a = "负责抖音电商直播运营，策划带货活动，管理达人矩阵与坑位排期。"
        b = "负责大模型语音数据标注质检，制定标注规范，输出一致性报告与返修方案。"
        self.assertLess(jobdup.containment(a, b), 0.3)
        self.assertFalse(jobdup.is_duplicate(a, b))

    def test_unrelated_long_jds_score_far_below_the_threshold(self):
        """**计划外补充**：上面那条的两段其实只有 27 / 31 字，**是被「过短不判」挡掉的**
        （containment 直接 0.0）—— 它并没有验证「无关 JD 的相似度本来就低」。

        这里换成两条**都过得了门槛**（≥40 字、≥30 个不同 bigram）的无关 JD，
        实测包含度 0.098（只有「负责 / 输出 / 跟进 / 报告」4 个 bigram 撞上）。
        """
        a = "负责抖音电商直播运营，策划带货活动，管理达人矩阵与坑位排期，跟进直播间的转化率与退货率，输出复盘报告。"
        b = "负责大模型语音数据标注质检，制定标注规范，输出一致性报告与返修方案，跟进标注团队的产能与准确率。"
        self.assertGreaterEqual(len(jobdup.bigrams(jobdup.normalize(a))), jobdup.MIN_BIGRAMS)
        self.assertGreaterEqual(len(jobdup.bigrams(jobdup.normalize(b))), jobdup.MIN_BIGRAMS)
        self.assertLess(jobdup.containment(a, b), 0.3)
        self.assertFalse(jobdup.is_duplicate(a, b))

    def test_truncated_paste_shorter_than_half_still_matches(self):
        """**计划外补充**：钉住「为什么用包含度而不是 Jaccard」这个决策（实测，别改回去）。

        同一份 JD 的另一份粘贴只有其中一段：包含度 1.0（短的那段整段都在长文本里），
        而 Jaccard 只有 0.55 —— 用常见的 0.8 阈值会**漏报**。
        （用户真实数据里同一个形状：包含度 0.9647 / Jaccard 0.5734。）
        """
        long_ = ("数据质检验收把控：严格按评测标准对大模型语音通话数据质检验收；"
                 "熟悉ASR与TTS评测流程；能定位Bad Case并做归因分析；有标注规范落地经验。"
                 "另外负责标注团队排期与新人带教，每周输出质量周报，并推动标注工具迭代与规范落地。")
        short = ("数据质检验收把控：严格按评测标准对大模型语音通话数据质检验收；"
                 "熟悉ASR与TTS评测流程；能定位Bad Case并做归因分析。")
        A = jobdup.bigrams(jobdup.normalize(short))
        B = jobdup.bigrams(jobdup.normalize(long_))
        jaccard = len(A & B) / len(A | B)
        self.assertEqual(jobdup.containment(short, long_), 1.0)   # 短的那段整段都在长文本里
        self.assertLess(jaccard, 0.85)                            # Jaccard 会被长度差稀释
        self.assertLess(jaccard, 0.8)                             # 连常见的 0.8 阈值也过不了
        self.assertTrue(jobdup.is_duplicate(short, long_))

    def test_very_short_texts_never_match(self):
        self.assertFalse(jobdup.is_duplicate("质检", "质检"))          # 过短，直接不判
        self.assertEqual(jobdup.containment("", "任何"), 0.0)
        self.assertEqual(jobdup.containment("   ", "x"), 0.0)

    def test_punctuation_and_whitespace_do_not_matter(self):
        # 计划原文是 12 字的短句 a/b 再 `a * 3` 放大，注释说「放大到门槛以上再比」。
        # 但保守门槛里的「bigram < 30」看的是**不同 bigram 的个数**（分块产物是集合），
        # 重复同一段 10 字文本无论乘几倍都只有 10 个不同 bigram（实测 a*3 / a*4 / a*5 都是 10）
        # → 永远过不了门槛，与那句注释的意图**不可能同时成立**。这里改用一段真正足够长的
        # 正文（>40 字、>30 个不同 bigram），左右只差标点与空白，验证的仍是同一个性质。
        a = "负责数据质检，严格按评测标准对大模型语音通话数据质检；熟悉ASR与TTS评测流程，能定位Bad Case并归因。"
        b = "负责数据质检 严格按评测标准对大模型语音通话数据质检\n熟悉ASR与TTS评测流程 能定位Bad Case并归因"
        self.assertEqual(jobdup.normalize(a), jobdup.normalize(b))
        self.assertEqual(jobdup.containment(a, b), 1.0)   # 标点/空白归零后完全相同
        self.assertTrue(jobdup.is_duplicate(a, b))


class FindDuplicatesTest(IsolatedCase):
    """夹具：造岗位 + 它对应的 jd/<id>.txt。"""

    LONG = ("数据质检验收把控：严格按评测标准对大模型语音通话数据质检验收；"
            "熟悉ASR与TTS评测流程；能定位Bad Case并做归因分析；有标注规范落地经验。")
    SHORT = ("数据质检验收把控：严格按评测标准对大模型语音通话数据质检验收；"
             "熟悉ASR与TTS评测流程；能定位Bad Case并做归因分析。")

    def _job(self, job_id, created_at, text, title="语音/文本评测"):
        store.upsert_job({"id": job_id, "job_title": title, "created_at": created_at,
                          "jd_text_file": job_id + ".txt"})
        store.write_text(store.p_path("jd", job_id + ".txt"), text)

    def test_pairs_use_the_earlier_job_as_keep(self):
        self._job("20261003_语音文本评测_75分", "2026-10-03 00:30", self.SHORT)
        self._job("20261008_语音文本评测_72分", "2026-10-08 16:40", self.LONG)
        pairs, _skipped = jobdup.find_duplicate_jobs()
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]["keep"], "20261003_语音文本评测_75分")   # 更早的那条
        self.assertEqual(pairs[0]["drop"], "20261008_语音文本评测_72分")
        self.assertGreaterEqual(pairs[0]["score"], 0.85)

    def test_unrelated_jobs_are_not_paired(self):
        self._job("j-a", "2026-10-01 00:00", "负责抖音电商直播运营，策划带货活动与达人矩阵。")
        self._job("j-b", "2026-10-02 00:00", "负责大模型语音数据标注质检，输出一致性报告。")
        pairs, _ = jobdup.find_duplicate_jobs()
        self.assertEqual(pairs, [])

    def test_already_merged_jobs_are_not_paired_again(self):
        self._job("k", "2026-10-01 00:00", self.LONG)
        self._job("d", "2026-10-02 00:00", self.SHORT)
        jobdup.merge_jobs("k", "d")
        pairs, _ = jobdup.find_duplicate_jobs()
        self.assertEqual(pairs, [])                     # d 已 merged_into，不再参与

    def test_jobs_without_jd_text_are_skipped_and_counted(self):
        self._job("k", "2026-10-01 00:00", self.LONG)
        store.upsert_job({"id": "没有原文", "job_title": "岗", "created_at": "2026-10-02 00:00",
                          "jd_text_file": ""})       # 没有 jd 文件
        pairs, skipped = jobdup.find_duplicate_jobs()
        self.assertEqual(pairs, [])
        self.assertEqual(skipped, 1)


class FindDuplicatesOrderTest(IsolatedCase):
    """**计划外补充**：钉住 pairs 的**顺序**（不是只断言「非空 / 条数」）。

    三份同一 JD 的粘贴（用户真实场景：同一份 JD 反复重分析）→ 3 对，
    期望按 (keep 时间, keep id, drop 时间, drop id) 升序稳定输出。
    """

    LONG = FindDuplicatesTest.LONG
    SHORT = FindDuplicatesTest.SHORT

    def test_pairs_are_sorted_by_keep_then_drop(self):
        for i, date, text in (("a", "2026-10-01 00:00", self.LONG),
                              ("b", "2026-10-02 00:00", self.SHORT),
                              ("c", "2026-10-03 00:00", self.SHORT)):
            store.upsert_job({"id": i, "job_title": "语音/文本评测", "created_at": date})
            store.write_text(store.p_path("jd", i + ".txt"), text)
        pairs, skipped = jobdup.find_duplicate_jobs()
        self.assertEqual(skipped, 0)
        self.assertEqual([(p["keep"], p["drop"]) for p in pairs],
                         [("a", "b"), ("a", "c"), ("b", "c")])
        # 具体值：同一份 JD 的截断对，包含度应显著高于阈值（实测形状 0.965 量级）
        self.assertEqual(len(pairs), 3)
        for p in pairs:
            self.assertGreaterEqual(p["score"], 0.85)


class MergeTest(IsolatedCase):
    def _two_jobs(self):
        store.upsert_job({"id": "keep", "job_title": "语音/文本评测", "created_at": "2026-10-03 00:30",
                          "jd_text_file": "keep.txt", "report": "keep.html", "score": 75.0})
        store.upsert_job({"id": "drop", "job_title": "语音/文本评测", "created_at": "2026-10-08 16:40",
                          "jd_text_file": "drop.txt", "report": "drop.html", "score": 72.0})
        for i in ("keep", "drop"):
            store.write_text(store.p_path("jd", i + ".txt"),
                             FindDuplicatesTest.LONG if i == "keep" else FindDuplicatesTest.SHORT)
            store.write_text(os.path.join(store.p_path("reports"), i + ".html"), "<html>" + i)

    def test_merge_repoints_from_jds_without_adding_duplicates(self):
        self._two_jobs()
        # keep 与 drop 同时出现在同一张卡上 → 合并后**只应剩一条** keep
        kb.absorb([{"term": "语音识别", "layer": 0, "sources": [], "timeline": [],
                    "related": [], "interview_questions": []}], {"id": "keep"})
        kb.absorb([{"term": "语音识别", "layer": 0, "sources": [], "timeline": [],
                    "related": [], "interview_questions": []}], {"id": "drop"})
        jobdup.merge_jobs("keep", "drop")
        card = store.load_knowledge()["nodes"][0]
        ids = [j["id"] for j in card["from_jds"]]
        self.assertEqual(ids, ["keep"])                  # 去重，不是 ["keep","keep"]

    def test_merge_repoints_interview_job_id(self):
        self._two_jobs()
        store.save_interview_json("iv1", "status.json", {"state": "done", "job_id": "drop"})
        jobdup.merge_jobs("keep", "drop")
        self.assertEqual(store.load_interview_json("iv1", "status.json")["job_id"], "keep")

    def test_merge_marks_drop_but_never_deletes(self):
        self._two_jobs()
        jobdup.merge_jobs("keep", "drop")
        jobs = {j["id"]: j for j in store.load_jobs()}
        self.assertIn("drop", jobs)                      # 记录还在
        self.assertEqual(jobs["drop"].get("merged_into"), "keep")
        self.assertTrue(os.path.exists(os.path.join(store.p_path("reports"), "drop.html")))  # 报告没删
        self.assertTrue(os.path.exists(store.p_path("jd", "drop.txt")))

    def test_merge_is_idempotent(self):
        self._two_jobs()
        jobdup.merge_jobs("keep", "drop")
        before = store.read_text(store.p_json_jobs()) if hasattr(store, "p_json_jobs") else None
        jobdup.merge_jobs("keep", "drop")                # 再来一次
        jobs = {j["id"]: j for j in store.load_jobs()}
        self.assertEqual(jobs["drop"].get("merged_into"), "keep")   # 仍指向 keep，无二次改动

    def test_merge_backs_up_before_writing(self):
        self._two_jobs()
        jobdup.merge_jobs("keep", "drop")
        backups = glob.glob(store.p_path("jobs.json") + ".bak-*")
        self.assertTrue(backups)                         # 备份存在且保留
        self.assertTrue(glob.glob(store.p_path("knowledge.json") + ".bak-*"))


class MergeIdempotenceTest(IsolatedCase):
    """**计划外补充**：幂等不能只断言「没抛异常」——第二次必须**一个文件都不动**。"""

    def _two_jobs(self):
        store.upsert_job({"id": "keep", "job_title": "语音/文本评测", "created_at": "2026-10-03 00:30"})
        store.upsert_job({"id": "drop", "job_title": "语音/文本评测", "created_at": "2026-10-08 16:40"})
        store.write_text(store.p_path("jd", "keep.txt"), FindDuplicatesTest.LONG)
        store.write_text(store.p_path("jd", "drop.txt"), FindDuplicatesTest.SHORT)

    def test_second_merge_changes_nothing_on_disk(self):
        self._two_jobs()
        jobdup.merge_jobs("keep", "drop")
        jobs1 = store.read_text(store.jobs_path())
        n_jobs_bak = len(glob.glob(store.jobs_path() + ".bak-*"))
        second = jobdup.merge_jobs("keep", "drop")
        self.assertEqual(store.read_text(store.jobs_path()), jobs1)      # 台账字节级不变
        self.assertEqual(len(glob.glob(store.jobs_path() + ".bak-*")), n_jobs_bak)  # 也没多备份
        self.assertEqual([j["id"] for j in store.load_jobs()], ["drop", "keep"])
        self.assertEqual({j["id"]: j.get("merged_into") for j in store.load_jobs()},
                         {"drop": "keep", "keep": None})
        self.assertTrue(second.get("noop"), "第二次调用必须报告 noop")
        # 这个夹具里没有任何卡片提到 drop/keep → 知识库应当**连文件都没被创建**
        self.assertFalse(os.path.exists(store.knowledge_path()))


class MergeCorruptKbTest(IsolatedCase):
    """**计划外补充**：knowledge.json 损坏时绝不写成空库（本项目真实丢过 97 张卡）。

    判据走 `store.knowledge_safe_to_write()`（唯一实现）。
    """

    def test_corrupt_kb_aborts_merge_and_rolls_back(self):
        store.upsert_job({"id": "keep", "job_title": "k", "created_at": "2026-10-03 00:30"})
        store.upsert_job({"id": "drop", "job_title": "d", "created_at": "2026-10-08 16:40"})
        store.write_text(store.p_path("jd", "keep.txt"), FindDuplicatesTest.LONG)
        store.write_text(store.p_path("jd", "drop.txt"), FindDuplicatesTest.SHORT)
        broken = "{\"nodes\": [{\"id\": \"x\"}  <<< 半截"
        store.write_text(store.knowledge_path(), broken)
        jobs_before = store.read_text(store.jobs_path())

        res = jobdup.merge_jobs("keep", "drop")

        self.assertFalse(res["ok"])
        self.assertEqual(store.read_text(store.knowledge_path()), broken)   # 坏文件原样保留
        self.assertEqual(store.read_text(store.jobs_path()), jobs_before)  # 台账已回滚
        self.assertEqual({j["id"]: j.get("merged_into") for j in store.load_jobs()},
                         {"drop": None, "keep": None})


class MergeUnreadableInterviewStatusTest(IsolatedCase):
    """面试 `status.json` **读不出来** ≠ 它没引用 drop（需求一，jobdup 侧）。

    缺陷形状（批 1 独立复查实测）：造一份内容里 `job_id` 就是 `drop_id` 的坏 `status.json`
    → `_interview_status_files` 里 `store.read_json(path, None)` 把「读不出来」当成
    「这份没引用我」→ 返回 `[]`，合并照样成功 → 那条面试记录的 `job_id` 指向**已被合并掉的**
    岗位（悬空引用，而且全程静默）。

    策略（与 `jdid._interview_status_paths` 同一份口径）：**拒绝这次合并**，且闸门装在
    **任何留档 / 写盘之前** —— 拒绝时用户数据一个字节都没动。为什么不选"只如实上报、照样合并"：
    drop 的 `merged_into` 一写下去那条面试记录就永久指向一个已被合并掉的身份，
    而这件事**事先就能判定**；按本项目「不知道 ≠ 安全」的口径就该停下（与坏知识库、
    坏 JD 库那两条闸门同规格）。

    断言为什么不是"整树指纹"：拒绝**要**记一条 `merge_rejected` 日志（与 `jdid` 同理），
    所以钉的是用户数据逐字节未变 + **一份 `.bak-` 都没多**（后者同时钉住"闸门在留档之前"）。
    """

    def _two_jobs(self):
        store.upsert_job({"id": "keep", "job_title": "语音/文本评测", "created_at": "2026-10-03 00:30",
                          "jd_text_file": "keep.txt", "report": "keep.html", "score": 75.0})
        store.upsert_job({"id": "drop", "job_title": "语音/文本评测", "created_at": "2026-10-08 16:40",
                          "jd_text_file": "drop.txt", "report": "drop.html", "score": 72.0})
        store.write_text(store.p_path("jd", "keep.txt"), FindDuplicatesTest.LONG)
        store.write_text(store.p_path("jd", "drop.txt"), FindDuplicatesTest.SHORT)

    def _broken_status(self, interview_id, job_id):
        """造一份 GBK 的 `interviews/<id>/status.json`（内容里的 `job_id` 就是 `job_id`）。"""
        path = store.interview_file(interview_id, "status.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        raw = json.dumps({"state": "done", "job_id": job_id, "note": "语音评测跟进记录"},
                         ensure_ascii=False).encode("gbk")
        try:
            raw.decode("utf-8")
            raise AssertionError("夹具没坏：这串字节居然能按 UTF-8 解出来")
        except UnicodeDecodeError:
            pass
        store.write_bytes(path, raw)
        return path, raw

    def _baks(self):
        """隔离根里全部 `.bak-` 的相对路径（拒绝时**一份都不该多**）。"""
        return sorted(os.path.relpath(p, self.tmp)
                      for p in glob.glob(os.path.join(self.tmp, "**", "*.bak-*"), recursive=True))

    def test_an_unreadable_status_refuses_the_merge_and_touches_nothing(self):
        self._two_jobs()
        iv_path, raw = self._broken_status("iv_bad", "drop")
        _assert_isolated(self, iv_path, store.jobs_path(), store.knowledge_path())
        jobs_before = store.read_bytes(store.jobs_path())
        baks_before = self._baks()

        res = jobdup.merge_jobs("keep", "drop")

        self.assertFalse(res["ok"], "读不出来的面试记录存在时绝不能报合并成功：%r" % res["reason"])
        self.assertIn("读不出来", res["reason"])
        self.assertEqual(store.read_bytes(store.jobs_path()), jobs_before, "台账一个字节都不该动")
        self.assertIsNone({j["id"]: j for j in store.load_jobs()}["drop"].get("merged_into"),
                          "drop 不许被标 merged_into（那就是把引用指向已合并掉的岗位）")
        self.assertEqual(store.read_bytes(iv_path), raw, "坏的那份原样留着")
        self.assertEqual(self._baks(), baks_before,
                         "拒绝必须发生在留档之前：一份 `.bak-` 都不该多")

    def test_a_readable_status_for_another_job_does_not_block_the_merge(self):
        """对照组（反"有文件就拒"）：**读得出来但没引用 drop** 的那份不该挡住合并。"""
        self._two_jobs()
        store.save_interview_json("iv_other", "status.json", {"state": "done", "job_id": "别的岗位"})

        res = jobdup.merge_jobs("keep", "drop")

        self.assertTrue(res["ok"], res["reason"])
        self.assertEqual(store.load_interview_json("iv_other", "status.json")["job_id"], "别的岗位")

    def test_an_unreadable_status_for_another_job_also_refuses(self):
        """判据是「读不出来」而不是「内容里恰好有 drop_id」（与 `jdid` 那条同一口径）。"""
        self._two_jobs()
        iv_path, raw = self._broken_status("iv_other", "别的岗位_标注_文本_上海_6-8K_0009")
        jobs_before = store.read_bytes(store.jobs_path())

        res = jobdup.merge_jobs("keep", "drop")

        self.assertFalse(res["ok"])
        self.assertIn("读不出来", res["reason"])
        self.assertEqual(store.read_bytes(store.jobs_path()), jobs_before)
        self.assertEqual(store.read_bytes(iv_path), raw)

    def test_a_status_that_becomes_unreadable_after_the_gate_fails_loudly(self):
        """同源读取**不能只改一半**：闸门通过后、写盘阶段那次再读读不出来 → 失败并回滚。

        `_merge_jobs_locked` 有两处读面试记录：① 收集文件清单（闸门）② 写之前重新读内容。
        老实现②用的是 `read_json(path, None)` + `if isinstance(st, dict)` —— 读不出来就**静默跳过**，
        于是闸门白装、那条面试记录仍指向 drop，而合并报成功（同一条病的另一半）。
        """
        self._two_jobs()
        store.save_interview_json("iv1", "status.json", {"state": "done", "job_id": "drop"})
        iv_path = store.interview_file("iv1", "status.json")
        real = store.read_json
        calls = {"n": 0}

        def flaky(path, default=None):
            if path == iv_path:
                calls["n"] += 1
                if calls["n"] == 2:          # ① 闸门那次读得到；② 写盘那次读不出来
                    return default
            return real(path, default)

        with mock.patch.object(store, "read_json", side_effect=flaky):
            res = jobdup.merge_jobs("keep", "drop")

        self.assertEqual(calls["n"], 2, "夹具前提：这个路径必须真的被读两次（闸门 + 写前复查）")
        self.assertFalse(res["ok"], "写盘阶段读不出来时必须失败，不许静默跳过那条面试记录")
        self.assertEqual(store.load_interview_json("iv1", "status.json")["job_id"], "drop",
                         "回滚后那条面试记录还指向 drop（没有被改一半）")
        self.assertIsNone({j["id"]: j for j in store.load_jobs()}["drop"].get("merged_into"))


class CliTest(IsolatedCase):
    """**计划外补充**：`岗位去重.py` 这个交付物本身的测试。

    - 无参数：**严格只读**（对整个数据根取 sha256 指纹，新增 / 改动 / 删除都会被抓到）
    - `--合并`：跑完整合并链路并打印 multi_jd 变化（在隔离数据根里跑，绝不碰真实数据）
    """

    def _fixture(self):
        for i, date, text in (("k", "2026-10-01 00:00", FindDuplicatesTest.LONG),
                              ("d", "2026-10-02 00:00", FindDuplicatesTest.SHORT)):
            store.upsert_job({"id": i, "job_title": "语音/文本评测", "created_at": date})
            store.write_text(store.p_path("jd", i + ".txt"), text)

    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", ["岗位去重.py"] + argv):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = CLI.main()
        return code, out.getvalue()

    def test_no_args_is_strictly_read_only_and_lists_the_pair(self):
        self._fixture()
        before = _fingerprint(store.DATA_ROOT)
        code, text = self._run([])
        self.assertEqual(code, 0)
        self.assertEqual(_fingerprint(store.DATA_ROOT), before)   # 指纹逐项相同 = 完全没写
        self.assertIn("keep  k | 2026-10-01 00:00", text)
        self.assertIn("drop  d | 2026-10-02 00:00", text)
        self.assertIn("--合并 k d", text)

    def test_bad_args_return_usage_and_write_nothing(self):
        self._fixture()
        before = _fingerprint(store.DATA_ROOT)
        for bad in (["--合并", "k"], ["--合并", "k", "d", "x"], ["--乱写"]):
            code, _ = self._run(bad)
            self.assertEqual(code, 2, bad)
        self.assertEqual(_fingerprint(store.DATA_ROOT), before)

    def test_merge_mode_merges_and_prints_multi_jd_change(self):
        self._fixture()
        for jid in ("k", "d"):            # 造一张同时来自 k 与 d 的卡 → multi_jd = 1
            kb.absorb([{"term": "语音识别", "layer": 0, "sources": [], "timeline": [],
                        "related": [], "interview_questions": []}], {"id": jid})
        self.assertEqual(kb.stats(store.load_knowledge())["multi_jd"], 1)

        code, text = self._run(["--合并", "k", "d"])

        self.assertEqual(code, 0)
        self.assertIn("合并前：知识库 1 个概念，其中 multi_jd 1 个", text)
        self.assertIn("包含度 1.000", text)
        self.assertIn("multi_jd 1 → 0", text)
        self.assertEqual({j["id"]: j.get("merged_into") for j in store.load_jobs()},
                         {"d": "k", "k": None})
        self.assertEqual([x["id"] for x in store.load_knowledge()["nodes"][0]["from_jds"]], ["k"])
        self.assertTrue(glob.glob(store.jobs_path() + ".bak-*"))      # 备份留在盘上
        self.assertTrue(glob.glob(store.knowledge_path() + ".bak-*"))

    def test_merge_mode_reports_missing_job_with_exit_1(self):
        self._fixture()
        code, text = self._run(["--合并", "k", "不存在"])
        self.assertEqual(code, 1)
        self.assertIn("找不到", text)
        self.assertEqual({j["id"]: j.get("merged_into") for j in store.load_jobs()},
                         {"d": None, "k": None})                      # 一条都没被标记


class MergeRollbackTest(IsolatedCase):
    """**计划外补充**：写台账那一步失败时，必须用备份把**已经改掉**的知识库与面试记录还原回去。

    用 patch 把 `store.save_jobs` 打成必炸 —— 失败点落在「知识库已改指、面试记录已改指」之后。
    备份若是在改动**之后**才取的，回滚就会把改过的状态当原状写回去，这条会红。
    """

    def test_failure_after_kb_and_interview_steps_rolls_everything_back(self):
        store.upsert_job({"id": "keep", "job_title": "k", "created_at": "2026-10-03 00:30"})
        store.upsert_job({"id": "drop", "job_title": "d", "created_at": "2026-10-08 16:40"})
        store.write_text(store.p_path("jd", "keep.txt"), FindDuplicatesTest.LONG)
        store.write_text(store.p_path("jd", "drop.txt"), FindDuplicatesTest.SHORT)
        for jid in ("keep", "drop"):
            kb.absorb([{"term": "语音识别", "layer": 0, "sources": [], "timeline": [],
                        "related": [], "interview_questions": []}], {"id": jid})
        store.save_interview_json("iv1", "status.json", {"state": "done", "job_id": "drop"})
        iv_path = store.interview_file("iv1", "status.json")
        kb_before = store.read_text(store.knowledge_path())
        iv_before = store.read_text(iv_path)
        jobs_before = store.read_text(store.jobs_path())

        with mock.patch.object(store, "save_jobs", side_effect=OSError("disk full")):
            res = jobdup.merge_jobs("keep", "drop")

        self.assertFalse(res["ok"])
        self.assertTrue(res["rolled_back"])
        self.assertEqual(store.read_text(store.knowledge_path()), kb_before)   # from_jds 回到 [keep, drop]
        self.assertEqual(store.read_text(iv_path), iv_before)                 # job_id 回到 drop
        self.assertEqual(store.read_text(store.jobs_path()), jobs_before)     # 台账没被标记
        self.assertEqual([x["id"] for x in store.load_knowledge()["nodes"][0]["from_jds"]],
                         ["keep", "drop"])
        self.assertEqual(store.load_interview_json("iv1", "status.json")["job_id"], "drop")
        self.assertIsNone({j["id"]: j for j in store.load_jobs()}["drop"].get("merged_into"))


class MergeRollbackNonUtf8Test(IsolatedCase):
    """回滚必须**逐字节**还原，哪怕原文件是非 UTF-8。

    老实现 `_snapshot` 是 `write_text(backup, read_text(path, ""))`：GBK 文件被兜成 `""`，
    **快照里存的就是空** → 回滚把用户的文件写成空的，连备份里也没有原文。
    """

    def test_rollback_restores_non_utf8_kb_bytes_exactly(self):
        store.upsert_job({"id": "keep", "job_title": "k", "created_at": "2026-10-03 00:30"})
        store.upsert_job({"id": "drop", "job_title": "d", "created_at": "2026-10-08 16:40"})
        store.write_text(store.p_path("jd", "keep.txt"), FindDuplicatesTest.LONG)
        store.write_text(store.p_path("jd", "drop.txt"), FindDuplicatesTest.SHORT)
        # 知识库用 **GBK** 写（模拟被非 UTF-8 工具写坏）：里面有用户的卡
        raw = json.dumps({"version": 1, "nodes": [
            {"id": "语音识别", "term": "语音识别", "definition": "把语音转文字",
             "plain_explanation": "通俗", "sources": [{"url": "https://w"}],
             "from_jds": [{"id": "keep", "job_title": "k", "date": "2026-10-03"}]}]},
            ensure_ascii=False).encode("gbk")
        with open(store.knowledge_path(), "wb") as f:
            f.write(raw)

        with mock.patch.object(store, "save_jobs", side_effect=OSError("disk full")):
            res = jobdup.merge_jobs("keep", "drop")

        self.assertFalse(res["ok"])
        self.assertTrue(res["rolled_back"])
        with open(store.knowledge_path(), "rb") as f:
            self.assertEqual(f.read(), raw,
                             "回滚必须逐字节还原 —— 不许把 GBK 的知识库写成空的")


class MergeSnapshotReadFailureTest(IsolatedCase):
    """快照时刻**读不出来**（被锁/权限）→ 合并失败，且**不许**留下一个空备份冒充原像。

    夹具与断言照 `MergeRollbackTest`（同一套 keep/drop + 知识库 + 面试记录）。
    注入点在 `jobdup._snapshot`：原像一律按**原始字节**读（`store.read_bytes`）；
    老实现 `read_text(path, "")` 会把"读失败"静默兜成 `""` → 备份写成 0 字节 →
    这一刻的"原像"根本不存在，回滚反而把知识库清成空的（连备份里都没有原文）。
    """

    def test_a_read_failure_at_snapshot_time_leaves_no_empty_backup(self):
        store.upsert_job({"id": "keep", "job_title": "k", "created_at": "2026-10-03 00:30"})
        store.upsert_job({"id": "drop", "job_title": "d", "created_at": "2026-10-08 16:40"})
        store.write_text(store.p_path("jd", "keep.txt"), FindDuplicatesTest.LONG)
        store.write_text(store.p_path("jd", "drop.txt"), FindDuplicatesTest.SHORT)
        for jid in ("keep", "drop"):
            kb.absorb([{"term": "语音识别", "layer": 0, "sources": [], "timeline": [],
                        "related": [], "interview_questions": []}], {"id": jid})
        store.save_interview_json("iv1", "status.json", {"state": "done", "job_id": "drop"})
        kb_path = store.knowledge_path()
        jobs_path = store.jobs_path()
        with open(kb_path, "rb") as f:
            kb_raw = f.read()
        with open(jobs_path, "rb") as f:
            jobs_raw = f.read()
        self.assertNotEqual(kb_raw, b"{}", "夹具前提：知识库本身不是空结构")
        real = store.read_bytes

        def flaky(p):
            if p == kb_path:
                raise PermissionError(13, "拒绝访问")
            return real(p)

        with mock.patch.object(store, "read_bytes", flaky):
            res = jobdup.merge_jobs("keep", "drop")

        self.assertFalse(res["ok"], "快照读不出来时绝不能当成合并成功")
        # 失败点必须**正好落在知识库那一步**（台账的原像已经按字节存好了）：这条同时钉住
        # "原像是按字节读的" —— 若 `_snapshot` 用 `read_text`，`write_bytes` 会在写**台账**
        # 备份时就抛 `a bytes-like object is required`，盘上只会留一个 0 字节的 jobs 备份，
        # 这条断言就会红（实测：这一次的注入根本走不到）。
        #
        # ⚠️ 只数**本次合并自己**留下的那一份：夹具里前面两次 `upsert_job` 也各留了
        # `.bak-<时间戳>`（`store.save_jobs` 的覆盖前留档），所以目录里本来就不止一份 ——
        # 老断言 `len(...) == 1` 数的是"目录里有几份留档"，会被别的写者的留档干扰（实测 2 != 1）。
        # 判据改成"内容逐字节等于合并前那份台账"：这正是本条要保护的东西，而且比数数量更不容易
        # 假绿（若合并漏了台账快照，或者写了个 0 字节的空档，这里都会是 0 份）。
        baks = sorted(glob.glob(jobs_path + ".bak-*"))
        mine = [p for p in baks if store.read_bytes(p) == jobs_raw]
        self.assertEqual(len(mine), 1,
                         "台账的原像应当**恰好**存下 1 份（失败点在它后面那一步）："
                         "目录里的留档 = %s，其中与合并前台账逐字节相同的有 %d 份"
                         % ([os.path.basename(p) for p in baks], len(mine)))
        with open(mine[0], "rb") as f:
            self.assertEqual(f.read(), jobs_raw, "原像必须逐字节等于台账原文，不许是 0 字节空档")
        self.assertEqual(glob.glob(kb_path + ".bak-*"), [],
                         "读不出原文就不许留下空备份（0 字节的「原像」比没有更危险）")
        with open(kb_path, "rb") as f:
            self.assertEqual(f.read(), kb_raw, "整个合并必须中止，知识库逐字节不变")
        self.assertEqual([x["id"] for x in store.load_knowledge()["nodes"][0]["from_jds"]],
                         ["keep", "drop"], "引用一条都没被改跑")


def _assert_isolated(case, *paths):
    """写盘用例的第一步：钉住「这次要写的东西**都在隔离根里**」。

    为什么把它写进用例而不是只靠 `IsolatedCase`：本项目真实出过一次事故（探针没隔离根目录，
    把用户 145 张卡清成 0 张）。隔离根是 `IsolatedCase.setUp` 给的 `case.tmp`（临时目录），
    这里把"目标确实落在它下面"变成一条**每次都会跑的断言**，而不是靠人记得先设 `DATA_ROOT`。
    """
    root = os.path.abspath(case.tmp)
    for p in paths:
        case.assertTrue(os.path.abspath(p).startswith(root + os.sep),
                        "路径不在隔离根里，拒绝跑：%s（隔离根 %s）" % (p, root))


class MergeRollbackPrunedBackupTest(IsolatedCase):
    """回滚的**前提**（备份）不见了 → 绝不把目标写成 0 字节，且如实报「回滚未完成」。

    ⚠️ 这正是**上一批加的轮转让它从"不可达"变成"可达"**的那条老雷（第一条铁律：绝不静默丢数据）：
    合并窗口里只要别的写者连写几次 `jobs.json`，`store._prune_backups` 就会把这次合并留下的
    `jobs.json.bak-<ts>` 挤出"最新 3 份"；那一刻 `_restore` 读不到备份（`store.read_bytes`
    返回 `None`）就写 `b""` —— 回滚"成功"了，盘上却是一份 0 字节的台账，用户的岗位记录**永久消失**。
    """

    def _fixture(self):
        store.upsert_job({"id": "keep", "job_title": "k", "created_at": "2026-10-03 00:30"})
        store.upsert_job({"id": "drop", "job_title": "d", "created_at": "2026-10-08 16:40"})
        store.write_text(store.p_path("jd", "keep.txt"), FindDuplicatesTest.LONG)
        store.write_text(store.p_path("jd", "drop.txt"), FindDuplicatesTest.SHORT)
        for jid in ("keep", "drop"):
            kb.absorb([{"term": "语音识别", "layer": 0, "sources": [], "timeline": [],
                        "related": [], "interview_questions": []}], {"id": jid})
        store.save_interview_json("iv1", "status.json", {"state": "done", "job_id": "drop"})
        iv_path = store.interview_file("iv1", "status.json")
        _assert_isolated(self, store.jobs_path(), store.knowledge_path(), iv_path)
        return store.jobs_path(), store.knowledge_path(), iv_path

    @staticmethod
    def _break_the_jobs_snapshot(jobs_path, jobs_before, zero_it=False):
        """弄掉**本次合并**留下的那份 `jobs.json` 原像（内容逐字节等于合并前的台账）。

        两种都真实可达：`zero_it=False` = 被 `store._prune_backups` 挤掉（并发写台账），
        `zero_it=True` = 备份文件被别的东西截成 0 字节。
        """
        for p in glob.glob(jobs_path + ".bak-*"):
            if store.read_bytes(p) != jobs_before:
                continue                       # 别的写者（upsert_job）留的档，内容不是合并前的台账
            if zero_it:
                with open(p, "wb"):
                    pass
            else:
                os.remove(p)
            return
        raise AssertionError("夹具前提不成立：没找到本次合并留下的 jobs.json 原像")

    def test_a_pruned_jobs_snapshot_is_never_restored_as_an_empty_file(self):
        jobs_path, kb_path, iv_path = self._fixture()
        jobs_before = store.read_bytes(jobs_path)
        kb_before = store.read_bytes(kb_path)
        iv_before = store.read_bytes(iv_path)
        self.assertTrue(jobs_before, "夹具前提：台账里本来有内容 —— 只有这时候『写成空』才是丢数据")

        def failing_save_jobs(jobs, key=None):
            # 模拟"合并窗口里另一个写者写台账 → 轮转把我们的原像挤出最新 3 份"，然后这次写盘失败
            self._break_the_jobs_snapshot(jobs_path, jobs_before)
            raise OSError("disk full")

        with mock.patch.object(store, "save_jobs", side_effect=failing_save_jobs):
            res = jobdup.merge_jobs("keep", "drop")

        self.assertFalse(res["ok"])
        self.assertEqual(store.read_bytes(jobs_path), jobs_before,
                         "原像被删掉时，台账必须**一个字节都不动**：写成 b\"\" 就是用户的岗位记录"
                         "永久消失（老实现把 `read_bytes` 的 None 兜成 b\"\"，正是这么干的）")
        self.assertNotEqual(store.read_bytes(jobs_path), b"", "回滚把有内容的台账写成了 0 字节")
        self.assertFalse(res["rolled_back"], "回滚没成，却报了「已回滚」")
        self.assertIn("回滚未完成", res["reason"])
        # 其它文件的原像还在 → 它们仍要如实还原（一个失败不该让整段回滚放弃）
        self.assertEqual(store.read_bytes(kb_path), kb_before)
        self.assertEqual(store.read_bytes(iv_path), iv_before)

    def test_a_zeroed_backup_never_empties_a_target_that_still_has_content(self):
        """备份被截成 0 字节：0 字节的原像只说明「那一刻这里没有内容」，
        **不许**拿它覆盖一份**现在有字节**的台账 —— 那些字节没有任何备份覆盖。"""
        jobs_path, _kb_path, _iv_path = self._fixture()
        jobs_before = store.read_bytes(jobs_path)
        self.assertTrue(jobs_before)

        def failing_save_jobs(jobs, key=None):
            self._break_the_jobs_snapshot(jobs_path, jobs_before, zero_it=True)
            raise OSError("disk full")

        with mock.patch.object(store, "save_jobs", side_effect=failing_save_jobs):
            res = jobdup.merge_jobs("keep", "drop")

        self.assertFalse(res["ok"])
        self.assertEqual(store.read_bytes(jobs_path), jobs_before,
                         "0 字节的备份不许覆盖一份有内容的台账：写回去就是拿一次回滚换掉用户的数据")
        self.assertFalse(res["rolled_back"], "回滚没成，却报了「已回滚」")

    def test_restore_raises_and_never_touches_the_target_when_the_backup_is_missing(self):
        """判据的另一半：`_restore` 的入参里**记账过**的备份不见了 → 抛，不是"当成空的"。"""
        target = store.jobs_path()
        _assert_isolated(self, target)
        store.write_text(target, "{\"keep\": 1}")
        before = store.read_bytes(target)
        missing = target + jobdup.BACKUP_SUFFIX + "20260101_000000_000001"
        with self.assertRaises(RuntimeError) as cm:
            jobdup._restore(target, missing)
        self.assertIn(missing, str(cm.exception), "错误信息要给出路径，否则用户没法自救")
        self.assertEqual(store.read_bytes(target), before,
                         "备份不存在 = 回滚的前提没了：目标一个字节都不许动（尤其不许变成 0 字节）")

    def test_restore_raises_when_the_backup_is_missing_even_if_the_target_is_empty(self):
        """**判据 #1 的单独可观测形态**（目标现在没有字节 → 0 字节守卫不会替它兜住）：
        记账过的备份不见了 = 回滚的前提没了 —— 我们**不知道**原文当时是什么，写 `b""` 是在猜
        （本项目的规矩：把"不知道"当成"安全"正是丢数据的习惯）；而且那还会把"没有这个文件"
        变成盘上一个 0 字节文件。判据 #2 只能挡住"目标有内容"的那一半，所以这条必须独立存在。
        """
        target = store.jobs_path()
        _assert_isolated(self, target)
        missing = target + jobdup.BACKUP_SUFFIX + "20260101_000000_000001"
        self.assertFalse(os.path.exists(target), "夹具前提：目标现在一个字节都没有")
        with self.assertRaises(RuntimeError):
            jobdup._restore(target, missing)
        self.assertFalse(os.path.exists(target),
                         "备份不见了还去写 b\"\"：把「没有这个文件」变成一个 0 字节文件，"
                         "而且是在不知道原文是什么的前提下猜的")

    def test_restore_refuses_a_zero_byte_backup_over_a_target_with_bytes(self):
        """知识库那一路（持 `kb._KB_LOCK` 的分支）同样判据。"""
        target = store.knowledge_path()
        _assert_isolated(self, target)
        store.write_text(target, "{\"nodes\": [{\"id\": \"x\"}]}")
        before = store.read_bytes(target)
        backup = target + jobdup.BACKUP_SUFFIX + "20260101_000000_000001"
        with open(backup, "wb"):
            pass                                   # 备份存在、但读出来是空的
        self.assertEqual(store.read_bytes(backup), b"")
        with self.assertRaises(RuntimeError):
            jobdup._restore(target, backup)
        self.assertEqual(store.read_bytes(target), before,
                         "有内容的知识库不许被 0 字节的备份清空（本项目真实丢过 145 张卡）")

    def test_no_snapshot_for_a_target_is_legal_and_touches_nothing(self):
        """**别把合法的"本次没留档"变成硬错误**：它的形态是"这一对根本不在 `backups` 里"，
        不是"记账了、文件却不见了" —— 所以既不抛、也不动任何文件。"""
        target = store.jobs_path()
        _assert_isolated(self, target)
        store.write_text(target, "{\"keep\": 1}")
        before = store.read_bytes(target)
        self.assertEqual(jobdup._rollback([]), (True, []))
        self.assertEqual(store.read_bytes(target), before)

    def test_a_merge_that_fails_before_any_snapshot_says_nothing_was_touched(self):
        """失败点在任何 `_snapshot` 之前 → 一个文件都没改：说「已回滚」是多说的（备份都没留成）。"""
        jobs_path, _kb_path, _iv_path = self._fixture()
        jobs_before = store.read_bytes(jobs_path)
        with mock.patch.object(jobdup, "_snapshot", side_effect=OSError("备份写不出")):
            res = jobdup.merge_jobs("keep", "drop")
        self.assertFalse(res["ok"])
        self.assertTrue(res["rolled_back"], "没有任何改动时，数据本来就在原样")
        self.assertIn("一个字节都没动", res["reason"])
        self.assertEqual(store.read_bytes(jobs_path), jobs_before)


class SnapshotRotationTest(IsolatedCase):
    """`.bak-` 轮转：**jobdup 自己留的档**也要有上限（原来只有 store 的那几个目标有）。

    为什么必须有：`store._prune_backups` 只在 `store.snapshot_before_overwrite` 里被调用 ——
    即只轮转 `config.json` / `jobs.json`；而 `jobdup._snapshot` 还会给 `knowledge.json`
    与每个 `interviews/<面试id>/status.json` 留一份，**每次合并各留一份、谁都不删**，
    这两处因此无上限增长（`简历迁移` 的整目录备份还会把它们一起拷走）。
    """

    TS = ["20260101_000000_000001", "20260101_000000_000002", "20260101_000000_000003",
          "20260101_000000_000004", "20260101_000000_000005"]

    def _snap_five_times(self, target, content):
        _assert_isolated(self, target)
        store.write_text(target, content)
        before = store.read_bytes(target)
        for ts in self.TS:
            jobdup._snapshot(target, ts, [])
        return before

    def _kept(self, target):
        return sorted(os.path.basename(p) for p in glob.glob(target + ".bak-*"))

    def test_knowledge_backups_keep_only_the_newest_three(self):
        target = store.knowledge_path()
        before = self._snap_five_times(target, "{\"nodes\": []}")
        keep = ["knowledge.json.bak-" + ts for ts in self.TS[-store.BACKUP_KEEP:]]
        self.assertEqual(self._kept(target), keep,
                         "留下的必须是**最新**的 %d 份（不是最旧的那几份）" % store.BACKUP_KEEP)
        self.assertFalse(os.path.exists(target + jobdup.BACKUP_SUFFIX + self.TS[0]),
                         "最旧的那份没被轮转掉 → knowledge.json 的 `.bak-` 还是会无上限增长")
        self.assertEqual(store.read_bytes(target), before, "留档/轮转都不许改目标文件本身")

    def test_interview_status_backups_are_rotated_too(self):
        store.save_interview_json("iv1", "status.json", {"state": "done", "job_id": "d"})
        target = store.interview_file("iv1", "status.json")
        before = self._snap_five_times(target, "{\"state\": \"done\", \"job_id\": \"d\"}")
        keep = ["status.json.bak-" + ts for ts in self.TS[-store.BACKUP_KEEP:]]
        self.assertEqual(self._kept(target), keep,
                         "面试 status.json 的 `.bak-` 也要只留最新 %d 份" % store.BACKUP_KEEP)
        self.assertFalse(os.path.exists(target + jobdup.BACKUP_SUFFIX + self.TS[0]))
        self.assertEqual(store.read_bytes(target), before)

    def test_repeated_merges_do_not_grow_knowledge_backups_without_bound(self):
        """真实路径：`merge_jobs` 每次都给 `knowledge.json` 留一份档 —— 连做 4 次也只留最新 N 份。"""
        _assert_isolated(self, store.knowledge_path(), store.jobs_path())
        store.upsert_job({"id": "keep", "job_title": "k", "created_at": "2026-10-03 00:30"})
        for i in range(4):
            drop = "drop%d" % i
            store.upsert_job({"id": drop, "job_title": "d", "created_at": "2026-10-08 16:40"})
            res = jobdup.merge_jobs("keep", drop)
            self.assertTrue(res["ok"], res["reason"])
        baks = self._kept(store.knowledge_path())
        self.assertEqual(len(baks), store.BACKUP_KEEP,
                         "4 次合并留下的知识库档必须轮转到 %d 份，实际 %s"
                         % (store.BACKUP_KEEP, baks))


if __name__ == "__main__":
    unittest.main()
