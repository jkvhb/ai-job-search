# -*- coding: utf-8 -*-
"""`简历迁移.py`（把盘上已有的简历文件登记成 `rv_0001`、把老分析指向它）的测试。

夹具与**用户真实数据的形状同构**（规格 §7 的实测形状）：
  - 简历文件 `resumes/周汉钦-母版.md`，mtime = **2026-09-18 23:06:24**（早于全部 6 次分析）
  - 6 条分析（真实的 id / created_at / `resume` 值，其中 1 条是 `merged_into` 死记录）
  - 外加 2 条**对不上**的：`resume="别的简历"`（盘上没有这份文件）、`resume=""`
  - 外加 1 条**已经有 `resume_id`** 的：绝不能被覆盖（幂等 + 不覆盖别人的账）

全部用例都在 `IsolatedCase` 的隔离根里跑，**绝不碰真实 `data/`**，也绝不真调模型。
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
from datetime import datetime
from unittest import mock

import resumelib
import store

from tests.base import IsolatedCase

CLI = importlib.import_module("简历迁移")

RESUME_NAME = "周汉钦-母版"
RESUME_TEXT = ("周汉钦 · 简历（夹具）\n\n## 技能\n数据标注质检 / ASR 评测 / Bad Case 归因\n\n"
               "## 项目经历\n语音通话 Agent 质检，返修率降低 40%\n")
RESUME_MTIME = "2026-09-18 23:06:24"          # 规格 §7 实测的文件修改时间
RESUME_AT = "2026-09-18 23:06"                # 版本记录的 `at`（与 resumelib._AT_FMT 同口径）
MTIME_TS = datetime.strptime(RESUME_MTIME, "%Y-%m-%d %H:%M:%S").timestamp()

# 真实 jobs.json 的 6 条分析：(id, created_at, merged_into)
REAL_JOBS = (
    ("20261008_语音文本评测_72分", "2026-10-08 16:40", "20261003_语音文本评测_75分"),
    ("20261003_语音文本评测_75分", "2026-10-03 00:30", ""),
    ("20260919_统本标注（视频标注AI训练标注师）_69分", "2026-09-19 21:18", ""),
    ("20260919_统本标注（视频标注）_69分", "2026-09-19 14:50", ""),
    ("示例_语音文本评测_80分", "2026-09-19 15:06", ""),
    ("20260919_统本标注（视频标注）_66分", "2026-09-19 14:29", ""),
)
MATCHING_IDS = [j[0] for j in REAL_JOBS]
MERGED_ID = "20261008_语音文本评测_72分"
MISMATCHED_ID = "别的简历_某岗位_50分"        # resume=别的简历（盘上没有这份文件）
EMPTY_RESUME_ID = "无名简历_某岗位_40分"       # resume=""
ALREADY_ID = "已经钉过版本_某岗位_60分"        # 已经有 resume_id，不许覆盖

SECTION_FILES = "文件 → 版本"
SECTION_POINT = "会被指向 rv_0001 的分析（逐条）"
SECTION_SKIP = "不会被处理的分析（不硬塞 resume_id）"
SECTION_ALREADY = "已有 resume_id 的分析（保持原样）"
SECTION_TAIL = "本脚本只写"


def fingerprint(root):
    """整个数据根的指纹：{相对路径: sha256 或 '<目录>'}（新增 / 删除 / 改动都逃不掉）"""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for d in dirnames:
            out[os.path.relpath(os.path.join(dirpath, d), root)] = "<目录>"
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            with open(p, "rb") as f:
                out[os.path.relpath(p, root)] = hashlib.sha256(f.read()).hexdigest()
    return out


def count_files(directory, exts):
    try:
        names = os.listdir(directory)
    except OSError:
        return 0
    return len([f for f in names if f.lower().endswith(exts)])


def section(text, start, end):
    """对照表里两个标题之间的那一段。

    断言只在这一段里查 —— 绝不写"整页里有没有某个词"（那会把一个词在别处的出现当成通过）。
    """
    i = text.index(start)
    j = text.index(end, i + len(start))
    return text[i:j]


class MigrationCase(IsolatedCase):
    """真实形状夹具：1 份简历 + 6 条匹配分析（1 条已合并）+ 2 条对不上 + 1 条已有版本号。"""

    def setUp(self):
        IsolatedCase.setUp(self)
        for sub in ("resumes", "reports", "jd", "logs"):
            os.makedirs(store.p_path(sub), exist_ok=True)
        # 简历文件：**LF** 写（与仓库里真实的简历文件一致；磁盘字节 == 文本的 UTF-8 字节）
        self.resume_path = os.path.join(store.p_path("resumes"), RESUME_NAME + ".md")
        with open(self.resume_path, "w", encoding="utf-8", newline="") as f:
            f.write(RESUME_TEXT)
        os.utime(self.resume_path, (MTIME_TS, MTIME_TS))

        jobs = []
        for jid, created, merged in REAL_JOBS:
            rec = {"id": jid, "report": jid + ".html", "created_at": created,
                   "job_title": "语音/文本评测", "job_category": "数据标注/质检",
                   "location": "北京", "salary": "13-26K", "score": 70.0, "verdict": "建议投递",
                   "jd_text_file": jid + ".txt", "resume": RESUME_NAME, "status": "未投递",
                   "notes": ""}
            if merged:
                rec["merged_into"] = merged
            jobs.append(rec)
        jobs.append({"id": MISMATCHED_ID, "report": MISMATCHED_ID + ".html",
                     "created_at": "2026-09-20 10:00", "resume": "别的简历", "score": 50.0,
                     "status": "未投递"})
        jobs.append({"id": EMPTY_RESUME_ID, "report": EMPTY_RESUME_ID + ".html",
                     "created_at": "2026-09-21 10:00", "resume": "", "score": 40.0,
                     "status": "未投递"})
        jobs.append({"id": ALREADY_ID, "report": ALREADY_ID + ".html",
                     "created_at": "2026-11-01 10:00", "resume": RESUME_NAME, "score": 60.0,
                     "resume_id": "rv_0009", "status": "未投递"})
        store.save_jobs(jobs)

        # 报告 / jd 原文：用来钉「文件数不变」（夹具只造前三条 + 一份别人的报告）
        for i, jid in enumerate(MATCHING_IDS[:3]):
            store.write_text(os.path.join(store.p_path("reports"), jid + ".html"),
                             "<html><body>报告 %d</body></html>" % i)
            store.write_text(store.p_path("jd", jid + ".txt"), "JD 原文 %d" % i)
        store.write_text(os.path.join(store.p_path("reports"), "别人的报告.html"), "<html></html>")

    # ---- 小工具 ----
    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", ["简历迁移.py"] + argv):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = CLI.main()
        return code, out.getvalue(), err.getvalue()

    def jobs_by_id(self):
        return {j["id"]: j for j in store.load_jobs()}

    def disk_bytes(self, path):
        with open(path, "rb") as f:
            return f.read()

    def resume_bytes(self):
        return self.disk_bytes(self.resume_path)

    def listing(self, sub, exts=None):
        names = sorted(os.listdir(store.p_path(sub)))
        return [n for n in names if exts is None or n.lower().endswith(exts)]

    def backups(self):
        return sorted(glob.glob(os.path.join(store.profiles_root(), CLI.BACKUP_PREFIX + "_*")))

    def expected_version_keys(self):
        return ["ai_diff", "ai_diff_error", "at", "chosen", "file", "id", "mother_file", "name",
                "note", "parent_id", "removed", "rev", "root_id", "sha256", "size"]

    def write_resume_file(self, text, path=None):
        """直接写简历文件（LF，磁盘字节 == 文本字节），并把它 mtime 钉成规格 §7 那个时刻。"""
        p = path or self.resume_path
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.utime(p, (MTIME_TS, MTIME_TS))
        return p


class DryRunTest(MigrationCase):
    """① `--试跑` 必须**严格只读**（逐文件 sha256 指纹一致），且对照表逐条列对人。"""

    def test_dry_run_writes_nothing_and_prints_the_mapping(self):
        before = fingerprint(store.DATA_ROOT)
        code, text, _err = self.run_cli(["--试跑"])

        self.assertEqual(code, 0)
        self.assertEqual(fingerprint(store.DATA_ROOT), before)          # 一个字节都没写
        self.assertFalse(os.path.exists(store.p_path("resumes.json")))  # 也没建版本库
        self.assertEqual(self.backups(), [])                           # 连备份都没有

        # 「文件 → 版本」那一块：at / size / sha256 / chosen / rev / root_id 逐项对得上
        files_block = section(text, SECTION_FILES, SECTION_POINT)
        self.assertIn("resumes/%s.md  →  rv_0001" % RESUME_NAME, files_block)
        self.assertIn("at=%s（取文件 mtime %s）" % (RESUME_AT, RESUME_MTIME), files_block)
        self.assertIn("size=%d 字节" % len(RESUME_TEXT.encode("utf-8")), files_block)
        self.assertIn("sha256=%s" % hashlib.sha256(RESUME_TEXT.encode("utf-8")).hexdigest(),
                      files_block)
        self.assertIn("chosen=true", files_block)
        self.assertIn("root_id=rv_0001（自己）", files_block)
        self.assertIn("rev=1", files_block)
        self.assertIn("不移动、不改名、不删除", text)

        # 「会被指向 rv_0001 的分析」：**逐条**列 id 与 resume 值（6 条全在里面）
        point_block = section(text, SECTION_POINT, SECTION_SKIP)
        for jid in MATCHING_IDS:
            self.assertIn("%s | resume=%s" % (jid, RESUME_NAME), point_block)
        self.assertIn("共 6 条", point_block)
        self.assertIn(MERGED_ID, point_block)          # 死记录也在（它同样是那次分析的那一版）
        self.assertIn("merged_into", point_block)      # 但如实标出它是已合并的死记录
        self.assertNotIn(MISMATCHED_ID, point_block)
        self.assertNotIn(EMPTY_RESUME_ID, point_block)
        self.assertNotIn(ALREADY_ID, point_block)

        # 「不会被处理的分析」：对不上的、空 resume 的（各自理由写清楚）
        skip_block = section(text, SECTION_SKIP, SECTION_ALREADY)
        self.assertIn(MISMATCHED_ID, skip_block)
        self.assertIn("resume=别的简历", skip_block)
        self.assertIn("与简历文件名对不上", skip_block)
        self.assertIn(EMPTY_RESUME_ID, skip_block)
        self.assertIn("resume 为空", skip_block)
        self.assertNotIn(ALREADY_ID, skip_block)

        # 「已有 resume_id 的分析」：保持原样（绝不覆盖）
        already_block = section(text, SECTION_ALREADY, SECTION_TAIL)
        self.assertIn(ALREADY_ID, already_block)
        self.assertIn("rv_0009", already_block)
        for jid in MATCHING_IDS:
            self.assertNotIn(jid, already_block)       # 它们还没有 resume_id

        # 立论依据（规格 §7）必须写在输出里，且**说明这是推定**
        self.assertIn("有证据，不是猜", text)
        self.assertIn(RESUME_MTIME, text)              # 文件时间
        for created in ("2026-09-19 14:29", "2026-09-19 14:50", "2026-09-19 15:06",
                        "2026-09-19 21:18", "2026-10-03 00:30", "2026-10-08 16:40"):
            self.assertIn(created, text)               # 6 次分析的时间
        self.assertIn("推定", text)
        self.assertIn("未改动任何文件", text)
        # 只写这两个文件，别的都不碰
        self.assertIn("本次 --执行 将改动：resumes.json 是", text)
        self.assertIn("jobs.json 是（6 条分析补 resume_id）", text)
        self.assertIn("不碰 knowledge.json", text)

    def test_dry_run_reports_mtime_as_the_evidence_verbatim(self):
        """`at` 取的是**文件 mtime**：改一下 mtime，表里的 at 必须跟着变（证明不是写死的）。"""
        other = datetime.strptime("2026-05-01 08:09:10", "%Y-%m-%d %H:%M:%S").timestamp()
        os.utime(self.resume_path, (other, other))
        _code, text, _err = self.run_cli(["--试跑"])
        self.assertIn("at=2026-05-01 08:09（取文件 mtime 2026-05-01 08:09:10）", text)

    def test_bad_args_print_usage_and_write_nothing(self):
        before = fingerprint(store.DATA_ROOT)
        for bad in ([], ["--执行", "extra"], ["--乱写"]):
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(sys, "argv", ["简历迁移.py"] + bad):
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = CLI.main()
            self.assertEqual(code, 2, bad)
            self.assertIn("--试跑", err.getvalue())
            self.assertIn("--执行", err.getvalue())
        self.assertEqual(fingerprint(store.DATA_ROOT), before)
        self.assertFalse(os.path.exists(store.p_path("resumes.json")))


class ExecuteTest(MigrationCase):
    """② `--执行`：登记 `rv_0001` + 6 条匹配分析指向它；对不上的不硬塞；文件一个不动。"""

    def execute(self):
        before = {
            "resume_bytes": self.resume_bytes(),
            "resumes_listing": self.listing("resumes"),
            "reports_listing": self.listing("reports"),
            "jd_listing": self.listing("jd"),
            "jobs": {j["id"]: dict(j) for j in store.load_jobs()},
        }
        code, text, err = self.run_cli(["--执行"])
        return code, text, err, before

    def test_rv_0001_is_registered_with_the_file_mtime_and_the_file_fingerprint(self):
        code, _text, _err, _before = self.execute()
        self.assertEqual(code, 0)
        doc = resumelib.load_resumes()
        self.assertEqual(len(doc["resumes"]), 1)
        rec = doc["resumes"][0]
        self.assertEqual(sorted(rec), self.expected_version_keys())
        self.assertEqual(rec["id"], "rv_0001")
        self.assertEqual(rec["name"], RESUME_NAME)
        self.assertEqual(rec["root_id"], "rv_0001")        # 独立根 = 自己
        self.assertIsNone(rec["parent_id"])
        self.assertEqual(rec["rev"], 1)
        self.assertTrue(rec["chosen"])                     # 母简历
        self.assertFalse(rec["removed"])
        self.assertEqual(rec["at"], RESUME_AT)             # == 文件 mtime
        self.assertEqual(rec["at"], datetime.fromtimestamp(
            os.path.getmtime(self.resume_path)).strftime("%Y-%m-%d %H:%M"))
        self.assertEqual(rec["size"], os.path.getsize(self.resume_path))
        self.assertEqual(rec["size"], len(RESUME_TEXT.encode("utf-8")))
        self.assertEqual(rec["sha256"], hashlib.sha256(RESUME_TEXT.encode("utf-8")).hexdigest())
        self.assertEqual(rec["file"], "resumes/%s.md" % RESUME_NAME)
        self.assertIsNone(rec["mother_file"])              # 没快照（它就是最新版）
        self.assertEqual(rec["ai_diff"], {})
        self.assertEqual(rec["ai_diff_error"], "")
        self.assertEqual(rec["note"], "")
        # 用**生产读路径**复核：库自己认这条记录（sha 对得上 → latest），并且它是母简历
        text_of, path, kind = resumelib.read_version("rv_0001")
        self.assertEqual(kind, "latest")
        self.assertEqual(text_of, RESUME_TEXT)
        self.assertEqual(path, self.resume_path)
        self.assertEqual(resumelib.mother_of("rv_0001")["id"], "rv_0001")
        self.assertEqual(resumelib.current_of("rv_0001")["id"], "rv_0001")

    def test_all_six_matching_analyses_point_at_rv_0001_including_the_merged_dead_record(self):
        code, _text, _err, _before = self.execute()
        self.assertEqual(code, 0)
        jobs = self.jobs_by_id()
        for jid in MATCHING_IDS:                            # ← 逐条，不是"有一条对就算"
            self.assertEqual(jobs[jid]["resume_id"], "rv_0001", "分析 %s 没指向 rv_0001" % jid)
        self.assertEqual(jobs[MERGED_ID]["merged_into"],
                         "20261003_语音文本评测_75分")     # 合并标记还在
        self.assertEqual(jobs[MERGED_ID]["resume_id"], "rv_0001")

    def test_analyses_that_do_not_match_are_never_stuffed(self):
        self.execute()
        jobs = self.jobs_by_id()
        self.assertNotIn("resume_id", jobs[MISMATCHED_ID], "resume 对不上也被硬塞了版本号")
        self.assertNotIn("resume_id", jobs[EMPTY_RESUME_ID], "resume 为空的也被硬塞了版本号")
        # 已经有版本号的那条**逐字未变**（绝不覆盖）
        self.assertEqual(jobs[ALREADY_ID]["resume_id"], "rv_0009")

    def test_only_the_resume_id_field_changes_on_the_matching_records(self):
        """其余字段一个都不许动（连键顺序都不动：resume_id 插在 resume 后面）。"""
        _code, _text, _err, before = self.execute()
        jobs = self.jobs_by_id()
        for jid in MATCHING_IDS + [MISMATCHED_ID, EMPTY_RESUME_ID, ALREADY_ID]:
            old, new = before["jobs"][jid], jobs[jid]
            added = {"resume_id"} if jid in MATCHING_IDS else set()
            self.assertEqual(sorted(new), sorted(set(old) | added), jid)
            for key, value in old.items():
                self.assertEqual(new[key], value, "分析 %s 的 %s 被改了" % (jid, key))
        # 键顺序：`resume_id` 紧跟在 `resume` 后面（人打开 jobs.json 一眼能对上）
        keys = list(jobs[MATCHING_IDS[0]])
        self.assertEqual(keys[keys.index("resume") + 1], "resume_id")

    def test_no_resume_report_or_jd_file_is_moved_renamed_or_deleted(self):
        code, _text, _err, before = self.execute()
        self.assertEqual(code, 0)
        self.assertEqual(self.listing("resumes"), before["resumes_listing"])
        self.assertEqual(self.resume_bytes(), before["resume_bytes"])     # 逐字节没动
        self.assertEqual(count_files(store.p_path("resumes"), (".md", ".txt")), 1)
        self.assertEqual(self.listing("reports"), before["reports_listing"])
        self.assertEqual(self.listing("jd"), before["jd_listing"])
        self.assertEqual(self.listing("reports"), sorted(
            [jid + ".html" for jid in MATCHING_IDS[:3]] + ["别人的报告.html"]))

    def test_a_backup_of_the_whole_profile_is_kept(self):
        code, text, _err, before = self.execute()
        self.assertEqual(code, 0)
        backups = self.backups()
        self.assertEqual(len(backups), 1)
        self.assertIn(backups[0], text)
        b = backups[0]
        # 备份里是**迁移前**的形状：简历文件逐字节相同、jobs.json 里还没有 resume_id
        self.assertEqual(self.disk_bytes(os.path.join(b, "resumes", RESUME_NAME + ".md")),
                         before["resume_bytes"])
        self.assertFalse(os.path.exists(os.path.join(b, "resumes.json")))
        backed = json.loads(store.read_text(os.path.join(b, "jobs.json")))
        for rec in backed:
            if rec.get("id") in before["jobs"] and "resume_id" in before["jobs"][rec["id"]]:
                continue                       # 夹具里本来就带着版本号的那条（不是本次补的）
            self.assertNotIn("resume_id", rec, "备份里的 %s 已经有 resume_id 了" % rec.get("id"))
        self.assertEqual(sorted(os.listdir(os.path.join(b, "reports"))),
                         before["reports_listing"])
        self.assertEqual(sorted(os.listdir(os.path.join(b, "jd"))), before["jd_listing"])

    def test_the_run_is_logged_once_with_counts_only(self):
        self.execute()
        path = os.path.join(store.p_path("logs"), "events.jsonl")
        recs = [json.loads(l) for l in store.read_text(path).splitlines() if l.strip()]
        mine = [r for r in recs if r["event"] == "resume.migration_done"]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["data"]["versions"], 1)
        self.assertEqual(mine[0]["data"]["analyses"], 6)
        self.assertEqual(mine[0]["data"]["skipped"], 2)
        self.assertEqual(mine[0]["data"]["conflicts"], 0)
        # 日志里只有计数与名字，**没有简历正文**（选项集合逐项钉住，不是"整行里搜一下"）
        self.assertEqual(sorted(mine[0]["data"]),
                         ["analyses", "backup", "conflicts", "profile", "skipped", "versions"])


class ExistingVersionTest(MigrationCase):
    """已经登记过的版本：**沿用、不重复建**；内容与记录不符时不动它（规格 §8.2）。"""

    def test_an_already_registered_file_is_reused_not_duplicated(self):
        other = self.write_resume_file("旧简历正文",
                                       os.path.join(store.p_path("resumes"), "旧简历.md"))
        old = resumelib.create_version("旧简历", "旧简历正文")
        self.assertEqual(old["id"], "rv_0001")

        code, text, _err = self.run_cli(["--执行"])
        self.assertEqual(code, 0)
        recs = resumelib.load_resumes()["resumes"]
        self.assertEqual([r["id"] for r in recs], ["rv_0001", "rv_0002"])   # 新的顺延，不撞号
        self.assertEqual(recs[0]["sha256"], resumelib.sha256_of("旧简历正文"))  # 老记录原样
        self.assertEqual(recs[0]["name"], "旧简历")
        self.assertEqual(recs[1]["name"], RESUME_NAME)
        self.assertIn("rv_0002", text)
        self.assertTrue(os.path.exists(other))

    def test_a_record_whose_content_sha_does_not_match_is_left_alone_and_reported(self):
        """记录与磁盘内容不符（程序外改过）→ **不新建版本、也不给分析补 resume_id**（§8.2）。

        理由：这一版的内容已经不是记录里那一份了，"这 6 次分析用的是当前这份文件"这条推定
        也就跟着失效 —— 宁可什么都不写、如实报告，也绝不写一句自己都不信的 `resume_id`。
        """
        self.write_resume_file("现在是别的内容了")           # 程序外改过（mtime 仍是旧时刻）
        store.write_json(store.p_path("resumes.json"), {"version": 1, "resumes": [{
            "id": "rv_0001", "name": RESUME_NAME, "root_id": "rv_0001", "parent_id": None,
            "rev": 1, "at": RESUME_AT, "size": 10, "sha256": "0" * 64,
            "file": "resumes/%s.md" % RESUME_NAME, "mother_file": None, "chosen": True,
            "ai_diff": {}, "ai_diff_error": "", "note": "", "removed": False}]})
        before = store.read_text(store.p_path("resumes.json"))

        code, text, _err = self.run_cli(["--执行"])
        self.assertEqual(code, 0)
        self.assertIn("内容与记录不符", text)
        self.assertEqual(store.read_text(store.p_path("resumes.json")), before)  # 一字未动
        self.assertEqual([r["id"] for r in resumelib.load_resumes()["resumes"]], ["rv_0001"])
        for jid in MATCHING_IDS:
            self.assertNotIn("resume_id", self.jobs_by_id()[jid],
                             "内容对不上还给 %s 补了 resume_id" % jid)


    def test_a_corrupt_resumes_json_is_never_overwritten_with_an_empty_library(self):
        """`resumes.json`「有内容但解析失败」→ **中止**（同 `身份迁移` 那条铁律）。

        拿一份空库顶掉用户手改坏的文件就是静默丢数据；这里连备份都不建（计划阶段就退了）。
        """
        store.write_text(store.p_path("resumes.json"), "{ 这不是 JSON")
        before = fingerprint(store.DATA_ROOT)
        code, _text, err = self.run_cli(["--执行"])
        self.assertEqual(code, 1)
        self.assertIn("解析失败", err)
        self.assertEqual(fingerprint(store.DATA_ROOT), before)   # 一个字节都没写
        code, _text, err = self.run_cli(["--试跑"])
        self.assertEqual(code, 1)
        self.assertIn("解析失败", err)
        self.assertEqual(fingerprint(store.DATA_ROOT), before)


class IdempotenceTest(MigrationCase):
    """③ 二次执行：`resumes.json` / `jobs.json` / `events.jsonl` 字节级不变。"""

    def test_second_execute_changes_nothing_at_all(self):
        self.assertEqual(self.run_cli(["--执行"])[0], 0)
        self.assertEqual(len(self.backups()), 1)
        logs = os.path.join(store.p_path("logs"), "events.jsonl")
        snap = {
            "resumes.json": self.disk_bytes(store.p_path("resumes.json")),
            "jobs.json": self.disk_bytes(store.p_path("jobs.json")),
            "events.jsonl": self.disk_bytes(logs),
        }
        code, _text, _err = self.run_cli(["--执行"])
        self.assertEqual(code, 0)
        self.assertEqual(self.disk_bytes(store.p_path("resumes.json")), snap["resumes.json"])
        self.assertEqual(self.disk_bytes(store.p_path("jobs.json")), snap["jobs.json"])
        self.assertEqual(self.disk_bytes(logs), snap["events.jsonl"])   # 无事发生 → 不写审计行
        self.assertEqual(len(self.backups()), 2)                        # 备份照常加一份
        self.assertEqual([r["id"] for r in resumelib.load_resumes()["resumes"]], ["rv_0001"])
        jobs = self.jobs_by_id()
        self.assertEqual(jobs[MISMATCHED_ID].get("resume_id"), None)
        self.assertEqual(jobs[MATCHING_IDS[0]]["resume_id"], "rv_0001")

    def test_dry_run_after_migration_says_nothing_will_change(self):
        self.assertEqual(self.run_cli(["--执行"])[0], 0)
        before = fingerprint(store.DATA_ROOT)
        code, text, _err = self.run_cli(["--试跑"])
        self.assertEqual(code, 0)
        self.assertEqual(fingerprint(store.DATA_ROOT), before)
        self.assertIn("resumes/%s.md  →  rv_0001（已登记，沿用）" % RESUME_NAME, text)
        self.assertIn("本次 --执行 将改动：resumes.json 否", text)
        self.assertIn("jobs.json 否", text)
        point_block = section(text, SECTION_POINT, SECTION_SKIP)
        self.assertIn("（无）", point_block)                          # 没有要指的了
        already_block = section(text, SECTION_ALREADY, SECTION_TAIL)
        for jid in MATCHING_IDS:
            self.assertIn(jid, already_block)                        # 都已有 resume_id


class RollbackTest(MigrationCase):
    """④ 注入写盘失败 → 从备份恢复（**逐字节**，含"把已经改过的那份还原回去"）。"""

    def _with_existing_version(self):
        """让 `resumes.json` **本来就有内容**：迁移必须改它 → 失败回滚才有东西可验。"""
        self.write_resume_file("旧简历正文", os.path.join(store.p_path("resumes"), "旧简历.md"))
        return resumelib.create_version("旧简历", "旧简历正文")

    def test_write_failure_on_the_second_file_restores_the_first_one_byte_for_byte(self):
        old = self._with_existing_version()
        before = {
            "resumes.json": self.disk_bytes(store.p_path("resumes.json")),
            "jobs.json": self.disk_bytes(store.p_path("jobs.json")),
            "resume_bytes": self.resume_bytes(),
            "listing": self.listing("resumes"),
        }
        real = store.write_json
        calls = {"n": 0}
        boom = OSError("disk full (注入)")

        def flaky(path, obj):
            calls["n"] += 1
            if calls["n"] == 2:        # resumes.json 已改（rv_0002 写进去了），jobs.json 炸
                raise boom
            return real(path, obj)

        with mock.patch.object(store, "write_json", side_effect=flaky):
            code, text, err = self.run_cli(["--执行"])

        self.assertEqual(calls["n"], 2)
        self.assertEqual(code, 1)
        self.assertIn("已用备份恢复", text)
        self.assertIn("disk full (注入)", err)
        # 逐字节还原：rv_0002 必须被抹掉，jobs.json 一字未变
        self.assertEqual(self.disk_bytes(store.p_path("resumes.json")), before["resumes.json"])
        self.assertEqual(self.disk_bytes(store.p_path("jobs.json")), before["jobs.json"])
        self.assertEqual([r["id"] for r in resumelib.load_resumes()["resumes"]], [old["id"]])
        self.assertEqual(self.resume_bytes(), before["resume_bytes"])
        self.assertEqual(self.listing("resumes"), before["listing"])
        self.assertEqual(glob.glob(os.path.join(store.p_path(), ".tmp-*")), [])   # 不留临时文件
        self.assertEqual(len(self.backups()), 1)                                  # 备份保留不删
        self.assertTrue(os.path.exists(os.path.join(self.backups()[0], "resumes.json")))

    def test_write_failure_on_the_first_file_changes_nothing(self):
        before = fingerprint(store.profile_dir())
        with mock.patch.object(store, "write_json", side_effect=OSError("disk full (注入)")):
            code, text, _err = self.run_cli(["--执行"])
        self.assertEqual(code, 1)
        self.assertIn("已用备份恢复", text)
        self.assertEqual(fingerprint(store.profile_dir()), before)
        self.assertFalse(os.path.exists(store.p_path("resumes.json")))


if __name__ == "__main__":
    unittest.main()
