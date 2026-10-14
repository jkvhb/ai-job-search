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


if __name__ == "__main__":
    unittest.main()
