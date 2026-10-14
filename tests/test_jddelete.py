# -*- coding: utf-8 -*-
"""阶段 D：**真删除一个 JD**（`jdid` 的 `jds.json` 记录 + 它的分析 + 报告文件 + JD 原文）。

用户的决定原话是「**连报告与 JD 原文一起真删**」——所以这个功能的语义**就是不可恢复**。
工具能做的是：动手之前**逐条列清单**、**两次确认**、**先备份到带时间戳的目录**，
并把备份路径**如实**告诉用户（绝不让人以为"磁盘上真没了"）。

本文件钉住的契约（每一条都是硬要求）：

1. **预演只读**：`preview_purge` 逐条列出将删的东西，跑完前后**逐文件指纹不变**；
2. **清单不漏**：该 JD 的**每一条**分析（id + 简历名 + 分数）、**每一个**报告文件、
   JD 原文文件、知识库 `from_jds` 命中数、面试记录命中数；算不出来的如实说，**绝不猜**；
3. **真删**：`jds.json` 无该 JD、`jobs.json` 无它的分析、报告与原文文件**真的没了**；
   **别的 JD 的记录 / 报告 / 原文逐字未变**（反假绿：整条比较，不是数个数）；
4. **引用清理只清引用**：知识库命中卡的 `from_jds` 去掉它、**卡片还在**；
   面试记录 `job_id` 清空、**记录还在**；
5. **失败回滚**：注入删除失败 → **逐字节**还原（含已经被删掉的那个文件）+ 明确报错，绝不半删；
6. **边界**：删不存在的 JD → 明确错误（不是崩）；没分析 → 照样能删；
   报告文件缺失 → 如实说明（advisories）而不是静默跳过。
"""
import hashlib
import json
import os
import unittest
from unittest import mock

import app
import jddelete
import jdid
import store
from tests.base import IsolatedCase

DIR_REPORTS = "reports"
DIR_JD = "jd"


def _report_html(title):
    return ("<html><body><script>\nconst REPORT_DATA = %s;\n</script></body></html>"
            % json.dumps({"job_title": title}, ensure_ascii=False))

# ---- 夹具常量：两个 JD，各自的分析、报告、原文、截图 ----
JD_A = "本科_甲单位_标注质检_文本_北京_13-26K_0001"
JD_B = "硕士_乙单位_语音评测_语音_上海_20-30K_0002"
A1, A2 = "20261001_甲岗_66分", "20261005_甲岗_72分"
B1 = "20261006_乙岗_88分"
JD_A_TEXT = "统本标注（视频标注）：负责数据质检验收，熟悉 ASR 与 TTS 评测流程。" * 3
JD_B_TEXT = "语音/文本评测：负责语音数据质检与 Bad Case 归因。" * 3


def _sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _tree(root, skip=("logs",)):
    """目录下每个文件的**相对路径 → 内容指纹**（整棵树的指纹）。

    默认跳过 `logs/`：回滚路径**故意**写一条失败日志（那是审计，不是数据），
    把它算进"逐字节还原"里会让这条断言变成「日志没写」的假红。
    """
    out = {}
    for dirpath, _dirs, files in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        if any(rel_dir == s or rel_dir.startswith(s + os.sep) for s in skip):
            continue
        for fn in sorted(files):
            p = os.path.join(dirpath, fn)
            out[os.path.relpath(p, root)] = _sha(p)
    return out


class _PurgeCase(IsolatedCase):
    """两个 JD 的隔离夹具：绝不碰真实 `data/`（继承 `IsolatedCase` 已把根指到临时目录）。"""

    def setUp(self):
        super().setUp()
        store.ensure_profile()
        self._reports = store.p_path("reports")
        self._jd = store.p_path("jd")
        # ---- JD 记录（A 有原文 + 截图；B 有原文，用来做反假绿对照）----
        jdid.upsert_jd({"id": JD_A, "job_title": "甲岗（数据质检）", "company": "甲单位",
                        "location": "北京", "salary": "13-26K", "status": "未投递",
                        "jd_text_file": JD_A + ".txt", "jd_image": JD_A + ".jpg"})
        jdid.upsert_jd({"id": JD_B, "job_title": "乙岗（语音评测）", "company": "乙单位",
                        "location": "上海", "salary": "20-30K", "status": "已投",
                        "jd_text_file": JD_B + ".txt"})
        # ---- 分析记录（A 两条，B 一条）----
        app._save_analysis({"id": A1, "jd_id": JD_A, "report": A1 + ".html", "score": 66.0,
                            "resume": "简历甲", "created_at": "2026-10-01 09:00"})
        app._save_analysis({"id": A2, "jd_id": JD_A, "report": A2 + ".html", "score": 72.0,
                            "resume": "简历乙", "created_at": "2026-10-05 09:00"})
        app._save_analysis({"id": B1, "jd_id": JD_B, "report": B1 + ".html", "score": 88.0,
                            "resume": "简历甲", "created_at": "2026-10-06 09:00"})
        # ---- 报告文件（每份都带得出 REPORT_DATA，这样"读得出分数"那条才不是恒真）----
        for name, title in ((A1 + ".html", "甲岗"), (A2 + ".html", "甲岗"), (B1 + ".html", "乙岗")):
            store.write_text(os.path.join(self._reports, name), _report_html(title))
        # ---- JD 原文 + A 的截图 ----
        store.write_text(os.path.join(self._jd, JD_A + ".txt"), JD_A_TEXT)
        store.write_text(os.path.join(self._jd, JD_A + ".jpg"), "JPEG-A-BYTES")
        store.write_text(os.path.join(self._jd, JD_B + ".txt"), JD_B_TEXT)
        # ---- 知识库：一张卡同时来自 A 与 B（清引用时**卡片必须留着**）----
        store.save_knowledge({"nodes": [
            {"id": "rlhf", "term": "RLHF", "state": "未掌握", "from_jds": [
                {"id": JD_A, "job_title": "甲岗（数据质检）", "date": "2026-10-01"},
                {"id": JD_B, "job_title": "乙岗（语音评测）", "date": "2026-10-06"}]},
            {"id": "asr", "term": "ASR", "state": "已掌握", "from_jds": [
                {"id": JD_A, "job_title": "甲岗（数据质检）", "date": "2026-10-05"}]},
            {"id": "tts", "term": "TTS", "state": "未掌握", "from_jds": [
                {"id": JD_B, "job_title": "乙岗（语音评测）", "date": "2026-10-06"}]},
        ]})
        # ---- 面试记录：一条挂在 A 上（job_id 要被清空、记录要留着），一条挂在 B 上 ----
        store.save_interview_json("iv_0001", "status.json",
                                  {"state": "done", "job_id": JD_A, "title": "甲岗一面"})
        store.save_interview_json("iv_0002", "status.json",
                                  {"state": "done", "job_id": JD_B, "title": "乙岗一面"})

    # ---- 断言小工具 ----
    def _report(self, name):
        return os.path.join(self._reports, name)

    def _jd_file(self, name):
        return os.path.join(self._jd, name)

    def _card(self, cid):
        return [n for n in store.load_knowledge()["nodes"] if n.get("id") == cid][0]

    def _analysis_ids(self):
        return [j.get("id") for j in store.load_jobs()]

    def _advisories_text(self, preview):
        return "\n".join(str(a.get("message") or "") for a in preview["advisories"])


class JdPurgePreviewTest(_PurgeCase):
    """`preview_purge` = **只读**预演：逐条列出将删的东西，一个字节都不落盘。"""

    def test_every_analysis_is_listed_with_its_resume_and_score(self):
        p = jddelete.preview_purge(JD_A)
        self.assertTrue(p["found"])
        # ① 该 JD 的**每一条**分析都在（id / 简历名 / 分数逐条对上）
        self.assertEqual([a["id"] for a in p["analyses"]], [A1, A2])
        self.assertEqual([a["resume"] for a in p["analyses"]], ["简历甲", "简历乙"])
        self.assertEqual([a["score"] for a in p["analyses"]], [66.0, 72.0])
        # ② 别的 JD 的分析**一条都不许出现**（少列是坑，多列是数据损失）
        self.assertNotIn(B1, [a["id"] for a in p["analyses"]])

    def test_every_report_file_and_the_jd_source_are_listed(self):
        p = jddelete.preview_purge(JD_A)
        names = [f["name"] for f in p["files"]]
        self.assertEqual(sorted(f["name"] for f in p["files"] if f["kind"] == "analysis_report"),
                         [A1 + ".html", A2 + ".html"])
        self.assertIn(JD_A + ".txt", names)                 # JD 原文
        self.assertIn(JD_A + ".jpg", names)                 # 原截图
        self.assertNotIn(JD_B + ".txt", names)              # 别的 JD 的原文不许进清单
        # 每条文件都说得清它是干什么的（前端要能一条一条念给用户听）
        for f in p["files"]:
            self.assertIn(f["kind"], ("analysis_report", "jd_text", "jd_image"))
            self.assertIsInstance(f["exists"], bool)

    def test_reference_hits_are_counted_per_caller(self):
        p = jddelete.preview_purge(JD_A)
        # 知识库：A 命中 2 条引用（rlhf 与 asr 两张卡），其中 1 张卡还有别的来源
        self.assertEqual(p["refs"]["knowledge"]["hits"], 2)
        self.assertEqual([c["id"] for c in p["refs"]["knowledge"]["cards"]], ["rlhf", "asr"])
        self.assertEqual(p["refs"]["knowledge"]["cards_kept"], 2)   # 卡片**都会留着**
        # 面试记录：1 条挂在 A 上
        self.assertEqual(p["refs"]["interviews"]["hits"], 1)
        self.assertEqual([r["id"] for r in p["refs"]["interviews"]["records"]], ["iv_0001"])

    def test_missing_report_file_is_stated_not_silently_skipped(self):
        os.remove(self._report(A2 + ".html"))            # 盘上故意少一个报告
        p = jddelete.preview_purge(JD_A)
        self.assertIn(A2 + ".html", [f["name"] for f in p["files"]])   # 清单里照旧列它
        f = [x for x in p["files"] if x["name"] == A2 + ".html"][0]
        self.assertFalse(f["exists"])
        self.assertIn(A2 + ".html", self._advisories_text(p))
        self.assertIn("不在盘上", self._advisories_text(p))

    def test_preview_is_strictly_read_only(self):
        before = _tree(store.profile_dir())
        jddelete.preview_purge(JD_A)
        jddelete.preview_purge(JD_B)
        after = _tree(store.profile_dir())
        # 逐文件指纹不变（不只比文件数：改了一个字节也要红）
        self.assertEqual(after, before)

    def test_unknown_jd_is_reported_not_guessed(self):
        p = jddelete.preview_purge("根本没有这个 ID_0001")
        self.assertFalse(p["found"])
        self.assertFalse(p["can_purge"])
        self.assertIn("没有", p["not_found"] or p["reason"] or "")
        self.assertEqual(p["analyses"], [])
        self.assertEqual(p["files"], [])

    def test_an_orphan_analysis_without_a_jd_record_is_still_found(self):
        """迁移还没跑的数据形态：分析记录在、`jds.json` 里没有这条（身份 = 分析 id）。"""
        jd_id = "没有JD记录的孤儿_0007"
        app._save_analysis({"id": jd_id, "report": "orphan.html", "score": 50.0,
                            "resume": "简历丙", "created_at": "2026-09-09 09:00"})
        store.write_text(os.path.join(self._reports, "orphan.html"), "<html>orphan</html>")
        p = jddelete.preview_purge(jd_id)
        self.assertTrue(p["found"])
        self.assertFalse(p["jd_exists"])                 # JD 记录不存在 —— 如实说
        self.assertEqual([a["id"] for a in p["analyses"]], [jd_id])
        self.assertIn("没有独立的 JD 记录", json.dumps(p, ensure_ascii=False))

    def test_a_legacy_analysis_carrying_its_own_jd_files_is_listed_too(self):
        """**隔离副本实跑抓到的真 bug**：迁移前的记录把 JD 原文/截图留在**分析记录**上。

        `jds.json` 不存在时（= 用户真实数据现在的形态）只认 JD 记录的 `jd_text_file`
        会让预演**只列报告**，`jd/` 下的原文与截图留在盘上 —— 那就是"少列"：
        用户以为自己连原文一起删了。这条断言的就是那三份文件（报告 + 原文 + 截图）都在清单里。
        """
        jd_id = "迁移前的岗位_0008"
        # ⚠️ 必须**直接写 `jobs.json`**，不能走 `app._save_analysis`：那个入口按阶段 4A 的
        # 契约把 JD 级字段（`jd_text_file` / `jd_image`）**剥掉**，造不出"迁移前"的形状。
        store.save_jobs(store.load_jobs() + [
            {"id": jd_id, "report": "old.html", "score": 61.0, "resume": "简历丁",
             "created_at": "2026-09-01 09:00",
             "jd_text_file": jd_id + ".txt", "jd_image": jd_id + ".jpg"}])
        store.write_text(os.path.join(self._reports, "old.html"), "<html>old</html>")
        store.write_text(os.path.join(self._jd, jd_id + ".txt"), "迁移前的 JD 原文")
        store.write_text(os.path.join(self._jd, jd_id + ".jpg"), "JPEG-OLD")
        p = jddelete.preview_purge(jd_id)
        self.assertFalse(p["jd_exists"])                       # 没有独立 JD 记录 —— 如实说
        names = [f["name"] for f in p["files"]]
        self.assertIn("old.html", names)
        self.assertIn(jd_id + ".txt", names, "分析记录上的 JD 原文没被列出来（少列）")
        self.assertIn(jd_id + ".jpg", names, "分析记录上的原截图没被列出来（少列）")
        res = jddelete.purge_jd(jd_id)
        self.assertTrue(res["ok"], res)
        for name in ("old.html", jd_id + ".txt", jd_id + ".jpg"):
            where = DIR_REPORTS if name.endswith(".html") else DIR_JD
            self.assertFalse(os.path.exists(os.path.join(store.p_path(where), name)), name)

    def test_a_merged_analysis_of_this_jd_is_listed_too(self):
        """`merged_into` 的标记记录也挂在这个 JD 下（台账不显示，但它指着这个 JD）→ 必须列出来。"""
        jdid_data = jdid.load_jds()
        rec = {"id": "merged-1", "jd_id": JD_A, "report": A1 + ".html", "score": 60.0,
               "resume": "旧简历", "created_at": "2026-09-01 09:00", "merged_into": A2}
        jobs = store.load_jobs()
        jobs.append(rec)
        store.save_jobs(jobs)
        p = jddelete.preview_purge(JD_A)
        self.assertIn("merged-1", [a["id"] for a in p["analyses"]])
        self.assertEqual(len(jdid_data["jds"]), 2)


class JdPurgeExecuteTest(_PurgeCase):
    """真删：备份 + 删记录 + 删文件 + 清引用；别人的东西**逐字未变**。"""

    def _other_fingerprints(self):
        """别人那一份的完整快照：B 的 JD 记录 / B 的分析记录 / B 的报告 / B 的原文 / 卡片 / 面试。"""
        jd_b = jdid.find_jd(JD_B)
        rec_b = [j for j in store.load_jobs() if j.get("id") == B1][0]
        return {
            "jd_record": json.dumps(jd_b, ensure_ascii=False, sort_keys=True),
            "analysis": json.dumps(rec_b, ensure_ascii=False, sort_keys=True),
            "report": store.read_text(self._report(B1 + ".html"), None),
            "jd_text": store.read_text(self._jd_file(JD_B + ".txt"), None),
            "card_tts": json.dumps(self._card("tts"), ensure_ascii=False, sort_keys=True),
            "interview": json.dumps(
                store.load_interview_json("iv_0002", "status.json", None),
                ensure_ascii=False, sort_keys=True),
        }

    def test_purge_removes_records_files_and_clears_references_only(self):
        other_before = self._other_fingerprints()
        # 删除前把要删的文件的**原始字节**留一份（用来核对备份是不是逐字节相同）
        raw_before = {os.path.join(w, n): open(os.path.join(store.p_path(w), n), "rb").read()
                      for w, n in ((DIR_REPORTS, A1 + ".html"), (DIR_REPORTS, A2 + ".html"),
                                   (DIR_JD, JD_A + ".txt"), (DIR_JD, JD_A + ".jpg"))}
        res = jddelete.purge_jd(JD_A)
        self.assertTrue(res["ok"], res)
        # ① `jds.json` 里没有它了、B 还在
        self.assertIsNone(jdid.find_jd(JD_A))
        self.assertIsNotNone(jdid.find_jd(JD_B))
        # ② `jobs.json` 里没有它的分析，B 的还在
        self.assertEqual(self._analysis_ids(), [B1])
        # ③ 报告与 JD 原文（含截图）**真的没了**
        for name in (A1 + ".html", A2 + ".html"):
            self.assertFalse(os.path.exists(self._report(name)), name)
        for name in (JD_A + ".txt", JD_A + ".jpg"):
            self.assertFalse(os.path.exists(self._jd_file(name)), name)
        # ④ 备份目录里有**逐字节相同**的副本，而且路径回给了用户
        backup = res["backup"]
        self.assertTrue(os.path.isdir(backup), backup)
        for rel, raw in raw_before.items():
            with open(os.path.join(backup, rel), "rb") as f:
                self.assertEqual(f.read(), raw, "备份里的 %s 与删除前不一致" % rel)
        # 同一句话也要能逐字复现（不只比 sha：JSON 报告里的转义/换行都要一模一样）
        self.assertEqual(store.read_text(os.path.join(backup, "reports", A1 + ".html"), None),
                         _report_html("甲岗"))
        self.assertEqual(store.read_text(os.path.join(backup, "jd", JD_A + ".txt"), None),
                         JD_A_TEXT)
        # ⑤ 知识库：命中卡**还在**、`from_jds` 里没有 A 了、**别的来源没被顺手删掉**
        rlhf = self._card("rlhf")
        self.assertEqual([x["id"] for x in rlhf["from_jds"]], [JD_B])
        asr = self._card("asr")
        self.assertEqual(asr["from_jds"], [])            # 只清引用，卡片本身保留
        self.assertEqual(self._card("tts")["from_jds"][0]["id"], JD_B)
        # ⑥ 面试记录：挂 A 的那条 `job_id` 清空、**记录还在**；B 的没动
        iv1 = store.load_interview_json("iv_0001", "status.json", None)
        self.assertEqual(iv1["job_id"], "")
        self.assertEqual(iv1["title"], "甲岗一面")
        # ⑦ 反假绿：别人的东西**逐字未变**（整条比较，不是数个数）
        self.assertEqual(self._other_fingerprints(), other_before)

    def test_purge_a_jd_with_no_analyses_still_works(self):
        jd_id = "本科_丙单位_运营_文本_广州_9-12K_0009"
        jdid.upsert_jd({"id": jd_id, "job_title": "丙岗", "jd_text_file": jd_id + ".txt"})
        store.write_text(os.path.join(self._jd, jd_id + ".txt"), "丙岗 JD 原文")
        res = jddelete.purge_jd(jd_id)
        self.assertTrue(res["ok"], res)
        self.assertIsNone(jdid.find_jd(jd_id))
        self.assertFalse(os.path.exists(self._jd_file(jd_id + ".txt")))
        self.assertEqual(len(self._analysis_ids()), 3)   # 一条分析都没被顺手删掉

    def test_purging_a_missing_jd_is_a_clear_error(self):
        before = _tree(store.profile_dir())
        with self.assertRaises(jddelete.JdPurgeError) as ctx:
            jddelete.purge_jd("根本不存在_0001")
        self.assertIn("没有", str(ctx.exception))
        # 什么都没动（`store.save_jobs` 是「新的在前」，所以顺序与夹具的写入顺序相反）
        self.assertEqual(sorted(self._analysis_ids()), sorted([A1, A2, B1]))
        self.assertEqual(_tree(store.profile_dir()), before)

    def test_a_shared_report_file_is_not_deleted_out_from_under_another_jd(self):
        """两条分析引同一份报告：删 A 时不能把 B 还在用（且**还看得见**）的那份删掉。

        判据是「**还有别的分析记录引用它**」——不是"还有别人的报告文件"，所以这份报告
        仍然会出现在预演清单里（用户该知道它本来要删），但执行时**跳过**并如实说明。
        """
        jobs = store.load_jobs()
        for j in jobs:
            if j.get("id") == B1:
                j["report"] = A1 + ".html"
        store.save_jobs(jobs)
        p = jddelete.preview_purge(JD_A)
        self.assertIn(A1 + ".html", [f["name"] for f in p["files"]
                                     if f["kind"] == "analysis_report"])
        res = jddelete.purge_jd(JD_A)
        self.assertTrue(res["ok"], res)
        # B 还指着它 → 必须留在盘上，且要如实告诉用户"这份留下了"
        self.assertTrue(os.path.exists(self._report(A1 + ".html")),
                        "把别的 JD 还在用的报告删了 —— 那条分析的报告链接会 404")
        self.assertIn(A1 + ".html", [f["name"] for f in res["skipped_files"]])
        self.assertIn("别的 JD", "\n".join(str(s.get("why") or "") for s in res["skipped_files"]))
        # A 那份**没被别人用**的报告照旧被删（不是"因为有一份要留就全都不删"）
        self.assertFalse(os.path.exists(self._report(A2 + ".html")))


class JdPurgeRollbackTest(_PurgeCase):
    """失败必须**逐字节**回滚（含已经被删掉的文件），绝不半删。"""

    def _whole_tree(self):
        return _tree(store.profile_dir())

    def test_a_delete_failure_restores_every_file_byte_for_byte(self):
        before = self._whole_tree()
        jddelete._FAIL_AFTER = 1                     # 删掉第 1 个文件之后就炸
        try:
            with self.assertRaises(jddelete.JdPurgeError) as ctx:
                jddelete.purge_jd(JD_A)
        finally:
            jddelete._FAIL_AFTER = 0
        self.assertIn("回滚", str(ctx.exception))
        # ① 被删掉的那个文件回来了
        self.assertTrue(os.path.exists(self._report(A1 + ".html")))
        # ② 三份 JSON 与全树**逐字节**回到原样（不只比数量）
        self.assertEqual(self._whole_tree(), before)

    def test_a_failure_late_in_the_run_restores_the_files_removed_before_it(self):
        before = self._whole_tree()
        jddelete._FAIL_AFTER = 4                     # 第 4 个文件（原文/截图那批）之后炸
        try:
            with self.assertRaises(jddelete.JdPurgeError):
                jddelete.purge_jd(JD_A)
        finally:
            jddelete._FAIL_AFTER = 0
        self.assertEqual(self._whole_tree(), before)

    def test_a_failure_after_the_records_are_written_restores_the_records(self):
        before = self._whole_tree()
        jddelete._FAIL_AFTER = 6                     # 记录已经落盘之后才炸
        try:
            with self.assertRaises(jddelete.JdPurgeError):
                jddelete.purge_jd(JD_A)
        finally:
            jddelete._FAIL_AFTER = 0
        self.assertEqual(_tree(store.profile_dir()), before)
        self.assertIsNotNone(jdid.find_jd(JD_A))
        self.assertEqual(sorted(self._analysis_ids()), sorted([A1, A2, B1]))

    def test_a_rollback_restores_an_lf_only_json_file_byte_for_byte(self):
        """LF 换行的 JSON 也要**逐字节**回来。

        这条用例存在的理由：`_rollback` 用 `store.read_text` / `write_text` 还原三份 JSON，
        而这两个函数走 **Python 的换行符自动转换**（`open(..., "w")` 在 Windows 上把 `\n`
        写成 `\r\n`）。于是一份 **LF-only** 的文件经"读→写"往返后 sha 就变了 ——
        「逐字节回滚」对它**不成立**。

        程序自己写的文件都是 CRLF，所以真实数据暂时碰不到；但用户手改、编辑器设置、
        别的机器检出、或 `git` 的 `core.autocrlf` 都可能留下一份 LF 的文件 ——
        而回滚正是"数据必须与动手之前一模一样"的最后一道保险。
        """
        path = store.jobs_path()
        lf = store.read_text(path, "").encode("utf-8")
        self.assertNotIn(b"\r\n", lf, "夹具没做成 LF-only，这条用例就没在测东西")
        with open(path, "wb") as f:
            f.write(lf)
        before = _sha(path)

        jddelete._FAIL_AFTER = 1                     # 删掉第 1 个文件之后就炸 → 触发回滚
        try:
            with self.assertRaises(jddelete.JdPurgeError):
                jddelete.purge_jd(JD_A)
        finally:
            jddelete._FAIL_AFTER = 0
        self.assertEqual(_sha(path), before,
                         "回滚后 jobs.json 不是逐字节原样（换行被 read_text/write_text 改了）")


    def test_a_failed_rollback_carries_its_own_code_and_the_backup_path(self):
        """回滚**也**失败时：错误要带上 `rollback_failed` 与备份目录。

        前端据 `code` 分开说话：`failed` → "已逐字节回滚"；`rollback_failed` →
        "**数据可能不是原样**，请手工从 `backup` 恢复"。两者混成一句话
        （"已回滚：回滚也失败了"）会让用户以为没事 —— 那是本功能最不能出的错。
        所以 `backup` 必须是**结构化字段**，不能只埋在给用户看的文案里。
        """
        jddelete._FAIL_AFTER = 1                     # 触发事务中途失败 → 走回滚
        try:
            with mock.patch.object(jddelete, "_rollback", return_value=False):
                with self.assertRaises(jddelete.JdPurgeError) as ctx:
                    jddelete.purge_jd(JD_A)
        finally:
            jddelete._FAIL_AFTER = 0
        err = ctx.exception
        self.assertEqual(err.code, "rollback_failed")
        self.assertTrue(getattr(err, "backup", ""),
                        "回滚失败却没把备份目录给出来 —— 那用户就没法自救了")
        self.assertTrue(os.path.isdir(err.backup), err.backup)
        self.assertIn("回滚也失败了", str(err))
        self.assertNotIn("已从备份逐字节回滚", str(err),
                         "回滚失败还说'已逐字节回滚'是自相矛盾的")


    def test_a_rollback_never_unlinks_a_file_it_failed_to_read(self):
        """留底时**读不出来**的文件，回滚绝不能删它。

        原来的写法把"读不出来"和"原来没有"合并成一个 `None`，于是回滚会对 `jobs.json`
        执行 `os.remove` —— **用一次读失败换掉用户整份台账**（杀毒软件锁文件、权限、
        磁盘 IO 错误都会走到这里）。
        """
        path = store.jobs_path()
        with open(path, "rb") as f:
            before = f.read()
        ok = jddelete._rollback("", {path: (True, None)}, {}, [], {})
        self.assertTrue(os.path.exists(path), "回滚把一份「读不出来」的 jobs.json 删掉了")
        with open(path, "rb") as f:
            self.assertEqual(f.read(), before)
        self.assertFalse(ok, "没把握还原却报了「回滚成功」")

    def test_a_rollback_removes_a_json_file_that_did_not_exist_before(self):
        """反方向也要对：留底时**确实不存在**的文件，回滚要把它删掉（别留个假的）。"""
        path = store.jobs_path()
        os.remove(path)
        store.write_text(path, "[]")                 # 事务把它造出来了
        ok = jddelete._rollback("", {path: (False, None)}, {}, [], {})
        self.assertTrue(ok)
        self.assertFalse(os.path.exists(path), "留底时不存在，回滚后却还在盘上")


class JdPurgeSafetyTest(_PurgeCase):
    """**拒绝删除**的两条底线：台账读不出来就不许动手；备份必须够用。"""

    def test_an_unreadable_ledger_blocks_the_purge_instead_of_wiping_it(self):
        """`jobs.json` 读不出来 → **拒绝删除**，绝不把它覆盖成 `[]`。

        这是本阶段最严重的一处：`store.load_jobs()` 在任何读/解析失败时都返回 `[]`，
        而删除的第 2c 步会用 `save_jobs(kept_jobs)` 把整份台账**覆盖**成它读到的那个列表
        —— 于是"读不出来"会被落成一份空台账：**所有岗位的所有分析记录一次性没了**，
        而且 `jobs.json` 原先不在备份里，回滚也救不回来。

        触发条件很现实：用户手改 JSON（本模块处处假设可手改）、非 UTF-8 保存
        （本项目已在别的脚本上栽过 GBK）、Windows 上杀毒软件短暂锁文件。
        """
        bad = b'{"not": "a list"}'
        with open(store.jobs_path(), "wb") as f:
            f.write(bad)
        # ① 预演：如实说读不出来，并且**明说不能删**（前端据 `can_purge` 停手）
        p = jddelete.preview_purge(JD_A)
        self.assertTrue(p["found"], p)
        self.assertFalse(p["can_purge"], p)
        self.assertTrue(p["unreadable"], p)
        self.assertTrue(any(a["kind"] == "unreadable_jobs" for a in p["advisories"]), p)
        self.assertEqual(p["analyses"], [], "读不出来时不该假装知道有几条分析")
        # ② 执行：拒绝，且是**可判别的**拒绝（不是靠文案猜）
        with self.assertRaises(jddelete.JdPurgeError) as ctx:
            jddelete.purge_jd(JD_A)
        self.assertEqual(ctx.exception.code, "unreadable")
        # ③ 台账一个字节都没动（既没被覆盖成 []，也没被删）
        with open(store.jobs_path(), "rb") as f:
            self.assertEqual(f.read(), bad)
        # ④ 报告与原文也一个都没动 —— 拒绝必须发生在**任何**破坏之前
        self.assertTrue(os.path.exists(self._report(A1 + ".html")))
        self.assertTrue(os.path.exists(self._jd_file(JD_A + ".txt")))

    def test_a_non_utf8_ledger_also_blocks_the_purge(self):
        """GBK 存过的 `jobs.json` 同样必须**拒绝**，而不是静默当成空台账。"""
        with open(store.jobs_path(), "wb") as f:
            f.write('{"id": "甲岗"}'.encode("gbk"))
        with self.assertRaises(jddelete.JdPurgeError) as ctx:
            jddelete.purge_jd(JD_A)
        self.assertEqual(ctx.exception.code, "unreadable")
        self.assertEqual(store.load_jobs(), [], "夹具前提：这份台账对 store 来说读出来是空的")

    def test_the_backup_carries_the_json_files_and_a_manifest(self):
        """备份必须**够用**：事务会整份重写三份 JSON，备份里就得有它们的原样副本。

        否则进程被 kill（`except` 根本没机会跑，回滚无从谈起）时用户手里什么都没有；
        而 `rollback_failed` 那句"请手工从备份恢复"也就成了**做不到的话**。
        """
        with open(store.jobs_path(), "rb") as f:
            jobs_before = f.read()
        with open(store.knowledge_path(), "rb") as f:
            kb_before = f.read()
        res = jddelete.purge_jd(JD_A)
        self.assertTrue(res["ok"], res)
        bak = res["backup"]
        state = os.path.join(bak, jddelete.DIR_STATE)
        with open(os.path.join(state, "jobs.json"), "rb") as f:
            self.assertEqual(f.read(), jobs_before, "备份里的 jobs.json 不是删除前的原样")
        with open(os.path.join(state, "knowledge.json"), "rb") as f:
            self.assertEqual(f.read(), kb_before)
        # 面试记录（事务会改 `job_id`）也在备份里，且**按原目录结构**放，不会互相覆盖
        iv = os.path.join(state, "interviews", "iv_0001", "status.json")
        self.assertTrue(os.path.isfile(iv), "面试记录没进备份：%s" % iv)
        # manifest 要说清"删的是哪个 JD"、每个文件的 sha256 要对得上
        with open(os.path.join(bak, "manifest.json"), encoding="utf-8") as f:
            man = json.load(f)
        self.assertEqual(man["jd_id"], JD_A)
        rels = [e["path"] for e in man["files"]]
        for want in ("jobs.json", "knowledge.json", "jds.json"):
            self.assertTrue(any(r.endswith(want) for r in rels), "%s 不在 manifest 里：%r" % (want, rels))
        self.assertTrue(rels, "manifest 是空的")
        for e in man["files"]:
            with open(os.path.join(bak, e["path"]), "rb") as f:
                self.assertEqual(hashlib.sha256(f.read()).hexdigest(), e["sha256"], e["path"])


class JdPurgeFileScopeTest(_PurgeCase):
    """文件的**范围**：约定命名的原文要列全；别人还在用的文件不许删。"""

    def test_the_conventional_jd_text_name_is_listed_even_when_no_field_names_it(self):
        """原文按约定叫 `jd/<JD id>.txt`、却**没有任何字段点名它**时，也必须列出来并删掉。

        `app._jd_text_path` 有这条兜底（字段空 → `<JD id>.txt`），本模块原先没有 ——
        两边对"这个 JD 的原文叫什么"有两种答案：预演漏列，删除还会**报成功**而原文留在盘上。
        （`app.py` 给演示数据写的就是 `jd_text_file: ""`，所以这不是假想形状。）
        """
        data = jdid.load_jds()
        for r in data["jds"]:
            if r.get("id") == JD_A:
                r.pop("jd_text_file", None)          # 字段整个没有
        jdid.save_jds(data)
        p = jddelete.preview_purge(JD_A)
        names = [f["name"] for f in p["files"] if f["kind"] == "jd_text"]
        self.assertIn(JD_A + ".txt", names, "漏列了原文：%r" % (p["files"],))
        res = jddelete.purge_jd(JD_A)
        self.assertTrue(res["ok"], res)
        self.assertFalse(os.path.exists(self._jd_file(JD_A + ".txt")),
                         "预演说删、删除也报成功，原文却还在盘上（少列的最坏形态）")

    def test_an_empty_jd_text_file_field_still_finds_the_conventional_name(self):
        """同一个坑的另一半：字段在、但是空串（`app.py` 写演示数据时就是 `""`）。"""
        data = jdid.load_jds()
        for r in data["jds"]:
            if r.get("id") == JD_A:
                r["jd_text_file"] = ""
        jdid.save_jds(data)
        res = jddelete.purge_jd(JD_A)
        self.assertTrue(res["ok"], res)
        self.assertFalse(os.path.exists(self._jd_file(JD_A + ".txt")))

    def test_a_jd_text_file_another_jd_names_is_not_deleted(self):
        """别的 JD **点名**要用的原文/截图，删 A 时不许删掉。

        报告一直有这道闸门（`_referenced_by_survivors`），`jd/` 原文与截图原先没有 ——
        两个 JD 的记录点同一个 `jd_text_file` 时，删 A 会把 B 还在用的原文删掉。
        """
        data = jdid.load_jds()
        for r in data["jds"]:
            if r.get("id") == JD_B:
                r["jd_text_file"] = JD_A + ".txt"     # B 也点名了 A 的那份原文
        jdid.save_jds(data)
        p = jddelete.preview_purge(JD_A)
        entry = [f for f in p["files"] if f["name"] == JD_A + ".txt"][0]
        self.assertTrue(entry["kept"], "没认出别的 JD 还在用这份原文：%r" % (entry,))
        res = jddelete.purge_jd(JD_A)
        self.assertTrue(res["ok"], res)
        self.assertTrue(os.path.exists(self._jd_file(JD_A + ".txt")),
                        "把别的 JD 还在点名的原文删了")
        self.assertIn(JD_A + ".txt", [s["name"] for s in res["skipped_files"]])
        self.assertIn("别的 JD", "\n".join(str(s.get("why") or "") for s in res["skipped_files"]))
        # A 自己的截图**没有**被别人点名 → 照旧删掉（不是"有一份要留就全都不删"）
        self.assertFalse(os.path.exists(self._jd_file(JD_A + ".jpg")))

    def test_a_record_without_an_id_is_not_swept_away(self):
        """删 A 只许删**属于 A 的**记录：没有 `id` 的记录绝不能因此被一网打尽。

        触发形态很具体（我第一版测试就写错了、变异仍绿）：**本 JD 有一条没有 `id` 的记录**
        时，`target_ids` 里就混进一个空串，于是"id 不在集合里"这个判据会把**所有**没有 `id`
        的记录一起删掉 —— 别的 JD 的、以及根本不属于任何 JD 的。

        所以这里两样都要放：属于 A 的无 id 记录（该删）+ 不属于任何 JD 的孤儿（必须留）。
        """
        jobs = store.load_jobs()
        jobs.insert(0, {"jd_id": JD_A, "resume": "A 的无 id 记录",
                        "report": A1 + ".html", "score": 60.0})     # 让空串进 target_ids
        jobs.insert(0, {"resume": "孤儿记录", "report": "孤儿.html", "score": 50.0})
        store.save_jobs(jobs)

        res = jddelete.purge_jd(JD_A)
        self.assertTrue(res["ok"], res)
        left = store.load_jobs()
        # ① 属于 A 的记录（哪怕没有 `id`）**是** A 的分析 → 删掉
        self.assertFalse(any(j.get("resume") == "A 的无 id 记录" for j in left), left)
        # ② 不属于任何 JD 的孤儿记录**必须留下**（它没有任何理由被删）
        self.assertTrue(any(j.get("resume") == "孤儿记录" for j in left),
                        "没有 id 的孤儿记录被顺手删掉了：%r" % (left,))
        # ③ 别人的记录一条都没少
        self.assertEqual([j.get("id") for j in left if j.get("id")], [B1])


class JdPurgePreviewPinningTest(_PurgeCase):
    """把审查者**实证过是假绿**的几处钉死（变异能过 = 那条断言其实没在测东西）。"""

    def test_the_preview_carries_the_jd_record_itself(self):
        """预演要带上**这条 JD 记录本体**（需求 1 里"JD 记录"那一句原先没被任何测试钉住）。

        审查者的变异：把 `jd_record` 改成 `None`，**28 个测试照样全绿**。
        """
        p = jddelete.preview_purge(JD_A)
        self.assertIsInstance(p["jd_record"], dict, "预演没带上 JD 记录本体")
        self.assertEqual(p["jd_record"]["id"], JD_A)
        self.assertEqual(p["jd_record"]["job_title"], "甲岗（数据质检）")

    def test_the_found_path_reports_no_not_found_reason(self):
        """找到了就不许带"找不到"的理由（同一句话被两处用，最容易悄悄错）。"""
        found = jddelete.preview_purge(JD_A)
        self.assertEqual(found["not_found"], "")
        self.assertEqual(found["reason"], "")
        missing = jddelete.preview_purge("根本没有这个 ID_0001")
        self.assertEqual(missing["not_found"], missing["reason"])
        self.assertTrue(missing["not_found"])

    def test_the_preview_writes_nothing_anywhere_under_the_profiles_root(self):
        """只读预演不许在**任何**地方落盘 —— 包括备份目录。

        原断言只指纹 `store.profile_dir()`，而备份落在它的**同级**
        （`profiles/删除备份_*`）：一个"预演也会建备份"的实现不会被抓到。
        """
        before = _tree(store.profiles_root())
        jddelete.preview_purge(JD_A)
        jddelete.preview_purge(JD_B)
        jddelete.preview_purge("根本没有这个 ID_0001")
        self.assertEqual(_tree(store.profiles_root()), before)

    def test_every_analysis_carrying_jd_files_contributes_its_own(self):
        """**每条**分析自带的 `jd_text_file` / `jd_image` 都要收（不只第一条）。

        审查者的变异：只读第一条分析的同名字段 —— **18 个测试照样全绿**，
        因为老的"迁移前形状"夹具里恰好只有一条分析带这些字段。
        真实数据里同一个 JD 有多条分析是完全正常的，所以必须逐条都收。
        """
        jd_id = "迁移前的岗位_多条_0011"
        # 直接写 `jobs.json`：`app._save_analysis` 会按 4A 契约剥掉 JD 级字段，造不出这个形状
        store.save_jobs(store.load_jobs() + [
            {"id": jd_id, "report": "m1.html", "score": 60.0, "resume": "简历子",
             "created_at": "2026-09-01 09:00", "jd_text_file": "第一条原文.txt",
             "jd_image": "第一条截图.jpg"},
            {"id": jd_id, "report": "m2.html", "score": 61.0, "resume": "简历丑",
             "created_at": "2026-09-02 09:00", "jd_text_file": "第二条原文.txt"},
            {"id": jd_id, "report": "m3.html", "score": 62.0, "resume": "简历寅",
             "created_at": "2026-09-03 09:00", "jd_image": "第三条截图.jpg"},   # 只有截图
            {"id": jd_id, "report": "m4.html", "score": 63.0, "resume": "简历卯",
             "created_at": "2026-09-04 09:00"},                                # 两个字段都没有
        ])
        for name in ("m1.html", "m2.html", "m3.html", "m4.html"):
            store.write_text(os.path.join(self._reports, name), _report_html("多记录岗"))
        for name in ("第一条原文.txt", "第二条原文.txt", "第一条截图.jpg", "第三条截图.jpg"):
            store.write_text(os.path.join(self._jd, name), "内容-" + name)
        p = jddelete.preview_purge(jd_id)
        names = [f["name"] for f in p["files"]]
        for want in ("第一条原文.txt", "第二条原文.txt", "第一条截图.jpg", "第三条截图.jpg"):
            self.assertIn(want, names, "漏列了 %s：%r" % (want, names))
        # 类型也要对：截图不能因为"读的是原文那一列"就被当成原文
        kinds = {f["name"]: f["kind"] for f in p["files"]}
        self.assertEqual(kinds["第一条原文.txt"], "jd_text")
        self.assertEqual(kinds["第二条原文.txt"], "jd_text")
        self.assertEqual(kinds["第一条截图.jpg"], "jd_image")
        self.assertEqual(kinds["第三条截图.jpg"], "jd_image")
        # 而**每一条**分析的报告也都在（4 份）
        self.assertEqual(sorted(f["name"] for f in p["files"] if f["kind"] == "analysis_report"),
                         ["m1.html", "m2.html", "m3.html", "m4.html"])

    def test_file_kinds_are_reported_per_file(self):
        """每份文件都带自己的 `kind`（前端据此写"JD 原文/原截图/报告文件"）。"""
        p = jddelete.preview_purge(JD_A)
        kinds = {f["name"]: f["kind"] for f in p["files"]}
        self.assertEqual(kinds[A1 + ".html"], "analysis_report")
        self.assertEqual(kinds[JD_A + ".txt"], "jd_text")
        self.assertEqual(kinds[JD_A + ".jpg"], "jd_image")


class JdPurgeFingerprintTest(_PurgeCase):
    """预演与执行之间数据变了 → 拒绝执行（用户确认的是**那份**清单，不是另一份）。"""

    def test_the_fingerprint_changes_when_the_plan_changes(self):
        p1 = jddelete.preview_purge(JD_A)
        __import__("app")._save_analysis({"id": "a3", "jd_id": JD_A, "report": A1 + ".html",
                                          "score": 90.0, "resume": "简历丁",
                                          "created_at": "2026-10-09 09:00"})
        p2 = jddelete.preview_purge(JD_A)
        self.assertNotEqual(p1["fingerprint"], p2["fingerprint"])

    def test_a_stale_fingerprint_is_refused_and_nothing_is_deleted(self):
        p = jddelete.preview_purge(JD_A)
        __import__("app")._save_analysis({"id": "a3", "jd_id": JD_A, "report": A1 + ".html",
                                          "score": 90.0, "resume": "简历丁",
                                          "created_at": "2026-10-09 09:00"})
        with self.assertRaises(jddelete.JdPurgeError) as ctx:
            jddelete.purge_jd(JD_A, expect_fingerprint=p["fingerprint"])
        self.assertIn("清单", str(ctx.exception))
        self.assertIsNotNone(jdid.find_jd(JD_A))
        self.assertTrue(os.path.exists(self._report(A1 + ".html")))


if __name__ == "__main__":
    unittest.main()
