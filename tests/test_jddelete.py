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
