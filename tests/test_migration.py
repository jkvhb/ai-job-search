# -*- coding: utf-8 -*-
"""`身份迁移.py`（岗位身份：报告文件名 → JD 稳定 ID）的测试。

夹具思路：造**三份同一 JD 文本**的分析 + **一份不同**的 + **一份没有 JD 原文**的
（正是用户真实数据的形状：统本三件套 / 语音那一对 / 示例无原文）。
期望：5 份活跃分析 → **3 个 JD**（前 3 份并成 1 个、第 4 份自成 1 个、第 5 份自成 1 个）。

所有测试都在 `IsolatedCase` 的隔离根里跑，**绝不碰真实 data/**。
"""
import contextlib
import glob
import hashlib
import importlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from unittest import mock

import jdid
import kb
import store

from tests.base import IsolatedCase

CLI = importlib.import_module("身份迁移")

# 同一份 JD 的两次粘贴：SHORT 是 LONG 的截断（真实验测形状 0.965；jobdup 的测试里钉过）
LONG_JD = ("数据质检验收把控：严格按评测标准对大模型语音通话数据质检验收；"
           "熟悉ASR与TTS评测流程；能定位Bad Case并做归因分析；有标注规范落地经验；"
           "每周输出质量周报，推动标注工具迭代与规范落地。")
SHORT_JD = ("数据质检验收把控：严格按评测标准对大模型语音通话数据质检验收；"
            "熟悉ASR与TTS评测流程；能定位Bad Case并做归因分析。")
OTHER_JD = ("负责抖音电商直播运营，策划带货活动，管理达人矩阵与坑位排期，"
            "跟进直播间转化率与退货率，每周输出复盘报告并复盘投放ROI。")
# 迁移完成之后新加的一份分析（**另一份 JD**）→ 表头必须是 `3 → 4（新增 1 个）`
NEW_JD = ("负责图像标注平台的质检规则设计与抽检方案，跟踪标注员一致性（Kappa），"
          "输出返修闭环报告并推动标注工具迭代。")


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


def write_report(path, data):
    """造一份能被 `kb.read_report_data` 读出来的报告（模板骨架 + REPORT_DATA）。"""
    store.write_text(path, "<html><body><script>\nconst REPORT_DATA = %s;\n</script></body></html>"
                     % json.dumps(data, ensure_ascii=False))


def _report_data(title, salary, category, location, education, score, company=""):
    return {"job_title": title, "company": company, "job_category": category,
            "location": location, "salary": salary, "education": education,
            "score": score, "verdict": "建议投递", "resume": "周汉钦-母版",
            "source": "粘贴文本", "application": {"status": "未投递", "outcome_reason": "",
                                              "outcome_at": ""}}


class CliTestMixin:
    """跑 CLI / 读结果的小工具（几套夹具共用，**不自己建夹具**）。"""

    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", ["身份迁移.py"] + argv):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = CLI.main()
        return code, out.getvalue()

    def jobs_by_id(self):
        return {j["id"]: j for j in store.load_jobs()}

    def jds_by_id(self):
        return {j["id"]: j for j in jdid.load_jds()["jds"]}


class MigrationTestBase(CliTestMixin, IsolatedCase):
    """三份同一 JD（A/B/C）+ 一份不同（D）+ 一份无原文（E）；外加知识库与一条面试记录。"""

    def setUp(self):
        IsolatedCase.setUp(self)
        reports, jd_dir = store.p_path("reports"), store.p_path("jd")
        for sub in ("reports", "jd"):
            os.makedirs(store.p_path(sub), exist_ok=True)

        # A/B/C：同一份 JD 的三次分析（B、C 是 A 的截断粘贴）
        for jid, date, text in (("分析A", "2026-09-01 10:00", LONG_JD),
                                ("分析B", "2026-09-02 10:00", SHORT_JD),
                                ("分析C", "2026-09-03 10:00", SHORT_JD)):
            store.upsert_job({"id": jid, "report": jid + ".html", "created_at": date,
                              "job_title": "语音/文本评测", "company": "",
                              "job_category": "数据标注/质检", "location": "北京",
                              "salary": "13-26K", "score": 70.0, "verdict": "建议投递",
                              "source_type": "粘贴文本", "jd_text_file": jid + ".txt",
                              "resume": "周汉钦-母版", "status": "未投递"})
            store.write_text(os.path.join(jd_dir, jid + ".txt"), text)
            write_report(os.path.join(reports, jid + ".html"),
                         _report_data("语音/文本评测", "13-26K", "数据标注/质检", "北京",
                                      "本科", 70.0))

        # D：一份完全不同的 JD
        store.upsert_job({"id": "分析D", "report": "分析D.html", "created_at": "2026-09-04 10:00",
                          "job_title": "电商直播运营", "company": "某电商公司",
                          "job_category": "AI数据运营", "location": "上海", "salary": "10-15K",
                          "score": 55.0, "jd_text_file": "分析D.txt", "resume": "周汉钦-母版",
                          "status": "未投递"})
        store.write_text(os.path.join(jd_dir, "分析D.txt"), OTHER_JD)
        write_report(os.path.join(reports, "分析D.html"),
                     _report_data("电商直播运营", "10-15K", "AI数据运营", "上海", "本科", 55.0,
                                  company="某电商公司"))

        # E：没有 JD 原文（真实数据里的「示例」那条）→ 自成 1 个 JD
        store.upsert_job({"id": "分析E", "report": "分析E.html", "created_at": "2026-09-05 10:00",
                          "job_title": "语音/文本评测（示例）", "company": "某 AI 科技公司（示例）",
                          "job_category": "数据标注/质检", "location": "北京", "salary": "13–26K",
                          "score": 80.4, "jd_text_file": "", "resume": "周汉钦-母版",
                          "status": "未投递"})
        write_report(os.path.join(reports, "分析E.html"),
                     _report_data("语音/文本评测（示例）", "13–26K", "数据标注/质检", "北京",
                                  "本科", 80.4, company="某 AI 科技公司（示例）"))

        # 知识库：卡 1 同时提到 A / B / C（迁移后必须并成**一条** JD 引用）
        kb.absorb([{"term": "语音识别", "layer": 0, "sources": [], "timeline": [],
                    "related": [], "interview_questions": []}],
                  {"id": "分析A", "job_title": "语音/文本评测", "company": "", "date": "2026-09-01"})
        kb.absorb([{"term": "语音识别", "layer": 0, "sources": [], "timeline": [],
                    "related": [], "interview_questions": []}],
                  {"id": "分析B", "job_title": "语音/文本评测", "company": "", "date": "2026-09-02"})
        kb.absorb([{"term": "语音识别", "layer": 0, "sources": [], "timeline": [],
                    "related": [], "interview_questions": []}],
                  {"id": "分析C", "job_title": "语音/文本评测", "company": "", "date": "2026-09-03"})
        kb.absorb([{"term": "质量周报", "layer": 0, "sources": [], "timeline": [],
                    "related": [], "interview_questions": []}],
                  {"id": "分析D", "job_title": "电商直播运营", "company": "某电商公司",
                   "date": "2026-09-04"})

        # 面试记录：job_id 现在指向分析 id
        store.save_interview_json("iv1", "status.json", {"state": "done", "job_id": "分析A"})


class DryRunTest(MigrationTestBase):
    """① `--试跑` 必须**严格只读**（逐文件 sha256 指纹一致）。"""

    def test_dry_run_writes_nothing_and_prints_the_mapping(self):
        before = fingerprint(store.DATA_ROOT)
        code, text = self.run_cli(["--试跑"])
        self.assertEqual(code, 0)
        self.assertEqual(fingerprint(store.DATA_ROOT), before)      # 一个字节都没写
        self.assertFalse(os.path.exists(store.p_path("jds.json")))   # 没建 JD 库
        # 对照表要点（细到个体）
        self.assertIn("活跃分析 5 份 → 3 个 JD", text)
        self.assertIn("分析A", text)
        self.assertIn("分析B", text)
        self.assertIn("分析C", text)
        self.assertIn("分析D", text)
        self.assertIn("分析E", text)
        self.assertIn("报告文件不改名、不删除", text)
        self.assertIn("跳过了 1 个", text)
        self.assertIn("未改动任何文件", text)

    def test_bad_args_return_usage_and_write_nothing(self):
        before = fingerprint(store.DATA_ROOT)
        for bad in ([], ["--执行", "extra"], ["--乱写"]):
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(sys, "argv", ["身份迁移.py"] + bad):
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = CLI.main()
            self.assertEqual(code, 2, bad)
            self.assertIn("--试跑", err.getvalue())      # 用法里点名了两种模式
            self.assertIn("--执行", err.getvalue())
        self.assertEqual(fingerprint(store.DATA_ROOT), before)
        self.assertFalse(os.path.exists(store.p_path("jds.json")))

    def test_dry_run_groups_by_fingerprint_not_by_report_filename(self):
        _code, text = self.run_cli(["--试跑"])
        blocks = self._jd_blocks(text)
        # 具体哪几份归到一组：个体粒度
        group_a = [b for b in blocks if "分析A" in b]
        group_d = [b for b in blocks if "分析D" in b]
        group_e = [b for b in blocks if "分析E" in b]
        self.assertEqual(len(group_a), 1, "分析A 必须只出现在一个 JD 块里：\n" + text)
        self.assertEqual(len(group_d), 1)
        self.assertEqual(len(group_e), 1)
        group_a, group_d, group_e = group_a[0], group_d[0], group_e[0]
        self.assertIn("分析B", group_a)                   # A/B/C 同一份 JD → 同一块
        self.assertIn("分析C", group_a)
        self.assertIn("归组依据: 指纹", group_a)
        self.assertNotIn("分析D", group_a)
        self.assertNotIn("分析E", group_a)
        self.assertNotIn("分析A", group_d)
        self.assertNotIn("分析E", group_d)
        self.assertIn("缺 JD 原文", group_e)
        # 三个 JD 的 id 互不相同
        heads = [b.splitlines()[0] for b in blocks]
        self.assertEqual(len({h.split("→")[1].strip() for h in heads}), 3)

    @staticmethod
    def _jd_blocks(text):
        """把对照表切成「一个 JD 一块」（头行是 `JD <序号>  →  <id>`）"""
        blocks, cur = [], None
        for line in text.splitlines():
            if line.startswith("JD ") and line[3:4].isdigit() and "→" in line:
                if cur is not None:
                    blocks.append("\n".join(cur))
                cur = [line]
            elif cur is not None:
                if line.strip():
                    cur.append(line)
                else:
                    blocks.append("\n".join(cur))
                    cur = None
        if cur is not None:
            blocks.append("\n".join(cur))
        return blocks


class DryRunAfterColumnTest(MigrationTestBase):
    """P1-2：试跑表的 `after` 列必须量的是**计划执行后的真实结果**。

    这张表是用户决定「要不要执行」的**唯一依据**。旧实现有两个毛病：
      ① `render_table` 自己再读一次盘取 before 值 ⇒ 计划与表可能量的是**两个时刻**的文件
         （另一个进程刚执行完 / 用户手改过），before 那一列于是与计划无关；
      ② 真实数据里那两行打「知识库引用 2 → 2」「multi_jd 5 → 5」（distinct id 数恰好不变），
         看不出这次会改指 **97 个知识点** —— 表格等于没说这次迁移会做什么。
    """

    @staticmethod
    def _row(text, label):
        for line in text.splitlines():
            if line.strip().startswith(label):
                return line.strip()
        raise AssertionError("对照表里没有 %s 这一行：\n%s" % (label, text))

    def test_the_before_values_come_from_the_plan_not_from_a_fresh_read_of_disk(self):
        """计划算好之后盘上再被动过（另一次执行 / 另一个进程 / 用户手改）也不许影响这一行。"""
        plan = CLI.plan_migration()
        data = store.load_knowledge()
        for node in data["nodes"]:
            for item in node.get("from_jds") or []:
                item["id"] = "本科_别处改过_数据标注质检_文本_北京_13-26K_0001"
        store.save_knowledge(data)

        text = CLI.render_table(plan)

        # 前值必须是**计划那一份**（4 条旧 id / 1 个 multi_jd），不是盘上那份被改过的
        self.assertIn("知识库引用（distinct 来源 id）4 → 2", text)
        self.assertIn("multi_jd 1 → 0", text)

    def test_the_after_column_equals_what_executing_really_produces(self):
        """`after` 唯一的意义：它必须等于**真执行之后实测出来**的值。"""
        _code, table = self.run_cli(["--试跑"])
        table_after_kb = int(re.search(r"→\s*(\d+)", self._row(table, "知识库引用")).group(1))
        table_after_multi = int(re.search(r"→\s*(\d+)", self._row(table, "multi_jd")).group(1))

        code, _text = self.run_cli(["--执行"])
        self.assertEqual(code, 0)

        data = store.load_knowledge()
        self.assertEqual(table_after_kb, CLI._from_jds_ids(data))
        self.assertEqual(table_after_multi, kb.stats(data)["multi_jd"])

    def test_the_table_says_how_many_knowledge_nodes_this_run_repoints(self):
        """distinct id 数可能是 2 → 2（真实数据就是这样），必须另说**改指了多少个知识点**。"""
        _code, table = self.run_cli(["--试跑"])
        self.assertIn("本次改指 2 个知识点的引用", self._row(table, "知识库引用"))


class ExecuteTest(MigrationTestBase):
    """③ `--执行` 之后：无悬空引用 / 报告数不变 / 知识库与面试记录改指 JD id / JD 数 == 分组数。"""

    def _execute(self):
        reports_before = sorted(os.path.basename(p)
                                for p in glob.glob(os.path.join(store.p_path("reports"), "*.html")))
        code, text = self.run_cli(["--执行"])
        return code, text, reports_before

    def test_every_analysis_has_a_jd_id_that_exists(self):
        code, _text, _ = self._execute()
        self.assertEqual(code, 0)
        jobs = store.load_jobs()
        self.assertEqual(len(jobs), 5)
        jds = self.jds_by_id()
        # 活跃分析：条条必须有 jd_id 且指向真实存在的 JD（无悬空引用）
        for j in jobs:
            self.assertTrue(str(j.get("jd_id") or ""), "分析 %s 没有 jd_id" % j.get("id"))
            self.assertIn(j["jd_id"], jds, "悬空 jd_id：%s" % j["jd_id"])
        # 逐条钉住「迁移后 jobs.json 只剩下这 6 个键」（JD 级字段整体搬走）
        for j in jobs:
            self.assertEqual(sorted(j), ["created_at", "id", "jd_id", "report", "resume", "score"],
                             "分析 %s 的键不对：%s" % (j["id"], sorted(j)))
        # A/B/C 必须**同一个** jd_id（个体粒度，不是「有 id 就算」）
        by_id = self.jobs_by_id()
        self.assertEqual(by_id["分析A"]["jd_id"], by_id["分析B"]["jd_id"])
        self.assertEqual(by_id["分析A"]["jd_id"], by_id["分析C"]["jd_id"])
        self.assertNotEqual(by_id["分析A"]["jd_id"], by_id["分析D"]["jd_id"])
        self.assertNotEqual(by_id["分析A"]["jd_id"], by_id["分析E"]["jd_id"])

    def test_jd_level_fields_do_not_stay_in_jobs_json(self):
        """JD 级字段只存 `jds.json`（两个文件各存一份是本项目反复栽的坑）。"""
        self._execute()
        for j in store.load_jobs():
            for key in ("job_title", "company", "salary", "location", "status", "verdict",
                        "notes", "jd_text_file", "source_type", "source_link",
                        "job_category", "address", "hr_name", "phone", "outcome_at",
                        "outcome_reason", "outcome_ai"):
                self.assertNotIn(key, j, "jobs.json 里还留着 JD 级字段 %s：%s" % (key, j.get("id")))
        jd = self.jds_by_id()[self.jobs_by_id()["分析A"]["jd_id"]]
        self.assertEqual(jd["job_category"], "数据标注/质检")     # 原始值，不是 ID 里的归一化值
        self.assertEqual(jd["salary"], "13-26K")
        self.assertEqual(jd["location"], "北京")
        self.assertEqual(jd["created_at"], "2026-09-01 10:00")
        self.assertEqual(jd["migrated_from"], ["分析A", "分析B", "分析C"])
        self.assertEqual(jd["id_history"], [])
        # ID 的六段与 JD 级字段是**两套值**：ID 归一化（`示例` / `13-26K`），
        # JD 级字段保留原始表述（`某 AI 科技公司（示例）`）——混在一起就是用户看到被截短的名字
        jd_e = self.jds_by_id()[self.jobs_by_id()["分析E"]["jd_id"]]
        self.assertEqual(jd_e["company"], "某 AI 科技公司（示例）")
        self.assertEqual(jd_e["salary"], "13–26K")               # 全角破折号，原样保留
        self.assertIn("示例", jd_e["id"])
        self.assertNotIn("某 AI 科技公司（示例）", jd_e["id"])

    def test_group_count_matches_fingerprint_groups(self):
        self._execute()
        self.assertEqual(len(jdid.load_jds()["jds"]), 3)
        self.assertEqual(jdid.load_jds()["seq"], 3)
        # ID 是 7 段、编号 4 位，末段编号 == 记录的 seq（逐条，不是只看总数）
        for jd in jdid.load_jds()["jds"]:
            fields, seq = jdid.parse_id(jd["id"])
            self.assertEqual(seq, jd["seq"])
            self.assertEqual(fields["city"], jd["location"] or "【未知城市】")
            self.assertEqual(jd["id"], jdid.build_id(fields, seq))     # 自洽：能原样拼回去

    def test_report_files_are_untouched(self):
        code, _text, reports_before = self._execute()
        self.assertEqual(code, 0)
        reports_after = sorted(os.path.basename(p)
                               for p in glob.glob(os.path.join(store.p_path("reports"), "*.html")))
        self.assertEqual(reports_after, reports_before)
        for name in reports_before:
            self.assertTrue(os.path.exists(os.path.join(store.p_path("reports"), name)))
        # JD 原文也不改名（jd_text_file 跟着指向它）
        for jid in ("分析A", "分析B", "分析C", "分析D"):
            self.assertTrue(os.path.exists(store.p_path("jd", jid + ".txt")),
                            "JD 原文 %s.txt 不见了" % jid)

    def test_knowledge_references_point_to_jd_ids(self):
        self._execute()
        data = store.load_knowledge()
        jds = self.jds_by_id()
        nodes = {n["term"]: n for n in data["nodes"]}
        self.assertEqual(len(nodes), 2)
        # 细到个体：三条旧引用并成一条，值就是那个 JD 的 id
        frm = nodes["语音识别"]["from_jds"]
        self.assertEqual(len(frm), 1, "A/B/C 应并成 1 条引用：%s" % frm)
        self.assertIn(frm[0]["id"], jds)
        self.assertEqual(frm[0]["id"], self.jobs_by_id()["分析A"]["jd_id"])
        self.assertEqual(frm[0]["job_title"], "语音/文本评测")
        other = nodes["质量周报"]["from_jds"][0]["id"]
        self.assertIn(other, jds)
        self.assertEqual(other, self.jobs_by_id()["分析D"]["jd_id"])
        # 全库无悬空：每条引用都必须在 jds.json 里
        for n in data["nodes"]:
            for x in n.get("from_jds") or []:
                self.assertIn(x["id"], jds, "知识库悬空引用：%s" % x)

    def test_knowledge_reference_with_no_title_gets_one_from_the_jd(self):
        """引用项缺 `job_title` / `company` 时要**从 JD 记录补上**（另一条独立分支）。"""
        kb.absorb([{"term": "无标题引用", "layer": 0, "sources": [], "timeline": [],
                    "related": [], "interview_questions": []}], {"id": "分析D"})
        data = store.load_knowledge()                       # 清掉那条引用的展示字段
        for node in data["nodes"]:
            if node["term"] == "无标题引用":
                for item in node["from_jds"]:
                    item["job_title"] = ""
                    item["company"] = ""
        store.save_knowledge(data)
        self._execute()
        node = [n for n in store.load_knowledge()["nodes"] if n["term"] == "无标题引用"][0]
        item = node["from_jds"][0]
        jd = self.jds_by_id()[self.jobs_by_id()["分析D"]["jd_id"]]
        self.assertEqual(item["id"], jd["id"])
        self.assertEqual(item["job_title"], "电商直播运营")     # 从 JD 记录补回来
        self.assertEqual(item["company"], "某电商公司")

    def test_interview_job_id_points_to_a_jd_id(self):
        self._execute()
        st = store.load_interview_json("iv1", "status.json")
        self.assertEqual(st["job_id"], self.jobs_by_id()["分析A"]["jd_id"])
        self.assertIn(st["job_id"], self.jds_by_id())

    def test_backup_directory_is_created_and_kept(self):
        self._execute()
        backups = glob.glob(os.path.join(store.profiles_root(), "迁移备份_*"))
        self.assertEqual(len(backups), 1)
        self.assertTrue(os.path.isdir(backups[0]))
        self.assertTrue(os.path.exists(os.path.join(backups[0], "jobs.json")))
        self.assertTrue(os.path.exists(os.path.join(backups[0], "knowledge.json")))
        self.assertFalse(os.path.exists(os.path.join(backups[0], "jds.json")))  # 迁移前它不存在
        # 备份里是**迁移前**的内容，且没被就地改动
        before_jobs = json.loads(store.read_text(os.path.join(backups[0], "jobs.json")))
        self.assertEqual(len(before_jobs), 5)
        self.assertTrue(all("jd_id" not in j for j in before_jobs))

    def test_backup_never_contains_a_previous_backup(self):
        """备份里**不套备份**（拷一次就多 MB 级、再跑就滚雪球）。

        ⚠ 实测澄清：真布局下备份落在 `data/profiles/迁移备份_<ts>/`，它是 profile 目录的
        **兄弟**，本来就拷不进去（我在隔离副本上实测过：新备份顶层没有 `迁移备份_*`）。
        这条钉的是**不变量** —— 只要备份根将来挪进 profile，或用户手工把旧备份搬进来，
        也绝不会把备份拷成备份。
        """
        old = os.path.join(store.profile_dir(), "迁移备份_20200101_000000")
        os.makedirs(old)
        store.write_text(os.path.join(old, "jobs.json"), "旧备份，不该被拷进新备份")

        code, _text, _reports = self._execute()

        self.assertEqual(code, 0)
        backups = glob.glob(os.path.join(store.profiles_root(), "迁移备份_*"))
        self.assertEqual(len(backups), 1)
        new = backups[0]
        self.assertFalse(os.path.exists(os.path.join(new, "迁移备份_20200101_000000")),
                         "新备份里套了上一次的备份：%s" % sorted(os.listdir(new)))
        # 闸门只挡备份：正常内容一个都不能少（别做成「什么都不拷」而假绿）
        self.assertTrue(os.path.exists(os.path.join(new, "jobs.json")))
        self.assertTrue(os.path.exists(os.path.join(new, "knowledge.json")))
        self.assertTrue(os.path.exists(os.path.join(new, "reports", "分析A.html")))
        self.assertTrue(os.path.isdir(os.path.join(new, "jd")))
        # 旧备份原地不动、内容一个字节没动（**绝不删除**）
        self.assertTrue(os.path.isdir(old))
        self.assertEqual(store.read_text(os.path.join(old, "jobs.json")), "旧备份，不该被拷进新备份")

    def test_backup_does_not_carry_store_backup_files_away(self):
        """`store` 会给 `jobs.json` / `knowledge.json` 留 `<文件>.bak-<时间戳>`（真实 profile 里就有）。

        整目录备份**不该把留档的留档再拷一份**：每跑一次迁移就多一份，profile 无上限增长。
        `简历迁移.py` 的同名闸门加了这一条，两处必须同一口径（骨架抄两遍已经漏出一次差异）。
        """
        keep = os.path.join(store.profile_dir(),
                            "jobs.json" + store.BACKUP_SUFFIX + "20200101_000000")
        store.write_text(keep, "旧留档，不该被拷进新备份")
        # 反假绿：先证明夹具里**真的有**留档可挡（否则这条测试什么也没钉住）
        self.assertTrue(glob.glob(os.path.join(store.profile_dir(), "*.bak-*")))

        code, _text, _reports = self._execute()

        self.assertEqual(code, 0)
        backups = glob.glob(os.path.join(store.profiles_root(), "迁移备份_*"))
        self.assertEqual(len(backups), 1)
        new = backups[0]
        copied = [os.path.relpath(os.path.join(d, n), new)
                  for d, _dn, fns in os.walk(new) for n in fns]
        self.assertEqual([p for p in copied if store.BACKUP_SUFFIX in p], [],
                         "新备份里套了 store 的留档：%s" % copied)
        # 闸门只挡留档：正常内容一个都不能少（别做成「什么都不拷」的假绿）
        self.assertIn("jobs.json", copied)
        self.assertIn("knowledge.json", copied)
        self.assertIn(os.path.join("reports", "分析A.html"), copied)
        # 留档原地不动、一个字节没动（**绝不删除**）
        self.assertEqual(store.read_text(keep), "旧留档，不该被拷进新备份")


class IdempotenceTest(MigrationTestBase):
    """④ 跑两次，第二次一个字节都不改（jds.json / jobs.json / knowledge.json 字节级相同）。"""

    def test_second_execute_changes_nothing(self):
        self.assertEqual(self.run_cli(["--执行"])[0], 0)
        snap = {name: store.read_text(store.p_path(name))
                for name in ("jds.json", "jobs.json", "knowledge.json")}
        backups_before = glob.glob(os.path.join(store.profiles_root(), "迁移备份_*"))
        code, _text = self.run_cli(["--执行"])
        self.assertEqual(code, 0)
        for name, text in snap.items():
            self.assertEqual(store.read_text(store.p_path(name)), text, "%s 被改动了" % name)
        self.assertEqual(len(glob.glob(os.path.join(store.profiles_root(), "迁移备份_*"))),
                         len(backups_before) + 1)      # 备份照常加一份，但库内容不变
        self.assertEqual(len(jdid.load_jds()["jds"]), 3)   # 没重复建 JD
        self.assertEqual(jdid.load_jds()["seq"], 3)        # seq 也没再涨

    def test_dry_run_header_after_migration_says_no_new_jds(self):
        """**表头不能说假话**：二次执行的预览曾写成 `3 → 6`（实际 3、且执行确实幂等）。

        这张表是用户决定「要不要执行」的**唯一依据**。
        """
        _c, first = self.run_cli(["--试跑"])
        self.assertIn("JD 数 0 → 3（新增 3 个）", first)
        self.assertNotIn("已迁移过", first)
        first_ids = DryRunTest._jd_blocks(first)
        self.assertEqual(len(first_ids), 3)

        self.assertEqual(self.run_cli(["--执行"])[0], 0)

        _c2, second = self.run_cli(["--试跑"])
        self.assertIn("JD 数 3 → 3（已迁移过，无新增）", second)
        self.assertNotIn("3 → 6", second)
        self.assertNotIn("0 → 3", second)
        # 列出来的还是同样那三个 id（顺序也一样）
        heads = [b.splitlines()[0] for b in DryRunTest._jd_blocks(second)]
        self.assertEqual(heads, [b.splitlines()[0] for b in first_ids])
        # 每个 JD 都标出「沿用」——用户能看出表头为什么是 3 → 3
        self.assertEqual(second.count("这个 JD 已存在（沿用原 id/原编号）"), 3)

    def test_dry_run_header_counts_only_new_jds_when_some_already_exist(self):
        """迁移后新加一份**另一份 JD** 的分析 → `3 → 4（新增 1 个）`；老三个标「沿用」。"""
        self.assertEqual(self.run_cli(["--执行"])[0], 0)
        store.upsert_job({"id": "分析F", "report": "分析F.html", "created_at": "2026-10-01 10:00",
                          "job_title": "图像标注质检", "company": "某图像公司",
                          "job_category": "数据标注/质检", "location": "深圳", "salary": "12-18K",
                          "score": 66.0, "jd_text_file": "分析F.txt", "resume": "周汉钦-母版",
                          "status": "未投递"})
        store.write_text(store.p_path("jd", "分析F.txt"), NEW_JD)
        write_report(os.path.join(store.p_path("reports"), "分析F.html"),
                     _report_data("图像标注质检", "12-18K", "数据标注/质检", "深圳", "本科", 66.0,
                                  company="某图像公司"))

        before = fingerprint(store.DATA_ROOT)
        code, text = self.run_cli(["--试跑"])

        self.assertEqual(code, 0)
        self.assertEqual(fingerprint(store.DATA_ROOT), before)      # 仍然严格只读
        self.assertIn("JD 数 3 → 4（新增 1 个）", text)
        blocks = DryRunTest._jd_blocks(text)
        self.assertEqual(len(blocks), 4)
        new_block = [b for b in blocks if "分析F" in b]
        self.assertEqual(len(new_block), 1, "分析F 必须只出现在一个 JD 块里：\n" + text)
        self.assertNotIn("已存在", new_block[0])                     # 新的那个不该标「沿用」
        self.assertIn("分析F | 2026-10-01 10:00", new_block[0])
        self.assertEqual(text.count("这个 JD 已存在（沿用原 id/原编号）"), 3)


class RestoreTest(MigrationTestBase):
    """⑤ 注入写盘失败 → 用备份恢复（三个文件字节级还原）。

    注入点是 `store.write_json` 的**第二次**调用（第一次写 `jds.json` 成功、第二次写
    `jobs.json` 炸）—— 失败落在「已经有文件被改过」之后，才能验证「从备份恢复」这条路，
    而不是「还没开始就退出」。`save_jobs` 打不着：本脚本走的是 `store.write_json` 原子写。
    """

    def test_injected_write_failure_restores_from_backup(self):
        original = {name: store.read_text(store.p_path(name))
                    for name in ("jobs.json", "knowledge.json", "jds.json")
                    if os.path.exists(store.p_path(name))}
        original[os.path.join("interviews", "iv1", "status.json")] = \
            store.read_text(store.interview_file("iv1", "status.json"))
        self.assertFalse(os.path.exists(store.p_path("jds.json")))

        boom = OSError("disk full (注入)")
        real = store.write_json
        calls = {"n": 0}

        def flaky(path, obj):
            calls["n"] += 1
            if calls["n"] == 2:                      # jds.json 已落盘，jobs.json 炸
                raise boom
            return real(path, obj)

        with mock.patch.object(store, "write_json", side_effect=flaky):
            code, text = self.run_cli(["--执行"])

        self.assertEqual(calls["n"], 2)
        self.assertEqual(code, 1)
        self.assertIn("已用备份恢复", text)
        # 迁移新建的 jds.json 必须被清掉（它不在备份里）
        self.assertFalse(os.path.exists(store.p_path("jds.json")))
        for rel, content in original.items():
            self.assertEqual(store.read_text(store.p_path(rel)), content,
                             "%s 没被还原" % rel)
        st = store.load_interview_json("iv1", "status.json")
        self.assertEqual(st["job_id"], "分析A")               # 面试记录也还原
        self.assertEqual(self.jobs_by_id()["分析A"].get("jd_id"), None)
        self.assertEqual(glob.glob(os.path.join(store.p_path(), ".tmp-*")), [])   # 不留临时文件
        # 备份保留不删
        backups = glob.glob(os.path.join(store.profiles_root(), "迁移备份_*"))
        self.assertEqual(len(backups), 1)
        self.assertTrue(os.path.exists(os.path.join(backups[0], "jobs.json")))

    def test_failure_does_not_leave_a_temp_backup_root_behind(self):
        with mock.patch.object(store, "write_json", side_effect=OSError("disk full (注入)")):
            self.run_cli(["--执行"])
        self.assertEqual(glob.glob(os.path.join(store.DATA_ROOT, ".迁移临时*")), [])


class ExistingJdIdClaimTest(CliTestMixin, IsolatedCase):
    """P0-11：job 记录上**已经写好**的 `jd_id` 必须被认领（真实数据就是这个形状）。

    真实数据（`data/profiles/default/`）：`jobs.json` 里
    `20261014_大模型标注项目团队管理（ASRTTS方向）_75分` 的 `jd_id` 已经是
    `本科_泰盈科技集团_标注管理_多模态_成都_12-20K_0001`，而 `jds.json` 里**已经有**这条 JD
    （它的 `jd_text_file` 也是按 JD id 命名的，所以按 `jd/<分析 id>.txt` 找不到原文）。
    旧实现只用 `migrated_from` / 派生 id / 旧 id 认领 ⇒ 这一组被判成「缺 JD 原文」自成一组、
    另造一张 `…_000N`：同一个岗位在台账/知识库/报告里出现**两种身份**
    （审计 §2 P0-11 在真实数据副本上实跑确认）。
    """

    JD_ID = "本科_泰盈科技集团_标注管理_多模态_成都_12-20K_0001"
    JOB = "20261014_大模型标注项目团队管理（ASRTTS方向）_75分"

    def setUp(self):
        IsolatedCase.setUp(self)
        for sub in ("reports", "jd"):
            os.makedirs(store.p_path(sub), exist_ok=True)
        # JD 库里已经有这条 JD（程序自建 / 上一轮迁移建的），JD 级字段齐全
        jdid.save_jds({"version": 1, "seq": 1, "jds": [{
            "id": self.JD_ID, "seq": 1, "created_at": "2026-10-14 15:03",
            "updated_at": "2026-10-14 15:03", "id_history": [], "jd_facts": {},
            "job_title": "大模型标注项目团队管理（ASR/TTS方向）", "job_category": "标注管理",
            "location": "成都", "salary": "12-20K", "company": "泰盈科技集团股份有限公司",
            "status": "未投递", "verdict": "建议投递",
            "jd_text_file": self.JD_ID + ".txt", "jd_image": self.JD_ID + ".jpg"}]})
        store.write_text(store.p_path("jd", self.JD_ID + ".txt"), LONG_JD)
        store.write_text(store.p_path("jd", self.JD_ID + ".jpg"), "假截图（只需要文件存在）")
        # job 记录就是「迁移后」的形状：只有分析级字段 + jd_id（真实数据里就是这样）
        store.upsert_job({"id": self.JOB, "jd_id": self.JD_ID, "report": self.JOB + ".html",
                          "created_at": "2026-10-14 15:03", "score": 75.0,
                          "resume": "周汉钦-母版", "resume_id": "rv_0001"})
        # 报告里的写法与 JD 记录**不同**（JD 记录才是身份：报告那份不许反过来覆盖它）
        write_report(os.path.join(store.p_path("reports"), self.JOB + ".html"),
                     _report_data("大模型标注（报告侧写法）", "12-20K（含绩效）", "标注管理",
                                  "成都", "本科", 75.0, company="泰盈科技集团股份有限公司"))
        # 知识库的两半（真实形状）：一部分引用指向 JD id，另一部分还指向报告文件名 id
        kb.absorb([{"term": "ASR语音标注", "layer": 0, "sources": [], "timeline": [],
                    "related": [], "interview_questions": []}],
                  {"id": self.JD_ID, "job_title": "大模型标注项目团队管理（ASR/TTS方向）",
                   "company": "泰盈科技集团股份有限公司", "date": "2026-10-14"})
        kb.absorb([{"term": "大模型标注", "layer": 0, "sources": [], "timeline": [],
                    "related": [], "interview_questions": []}],
                  {"id": self.JOB, "job_title": "大模型标注项目团队管理（ASRTTS方向）",
                   "date": "2026-10-14"})

    def knowledge_ref_ids(self):
        return {x["id"] for n in store.load_knowledge()["nodes"]
                for x in (n.get("from_jds") or [])}

    def test_execute_claims_the_existing_jd_id_and_creates_no_second_card(self):
        before = fingerprint(store.DATA_ROOT)
        code, text = self.run_cli(["--试跑"])
        self.assertEqual(code, 0)
        self.assertEqual(fingerprint(store.DATA_ROOT), before)        # 试跑仍然严格只读
        self.assertIn("JD 数 1 → 1（已迁移过，无新增）", text)
        self.assertIn("这个 JD 已存在（沿用原 id/原编号）", text)

        code, _text = self.run_cli(["--执行"])
        self.assertEqual(code, 0)

        jds = jdid.load_jds()
        # ① JD 条数不变（旧实现会变成 2：另一张 `…_0002`）
        self.assertEqual([r["id"] for r in jds["jds"]], [self.JD_ID], jds["jds"])
        self.assertEqual(jds["seq"], 1)
        # ② 那条 job 的 `jd_id` 仍指向原 id
        self.assertEqual(self.jobs_by_id()[self.JOB]["jd_id"], self.JD_ID)
        # ③ 知识库 `from_jds` 指向的 id 仍在 `jds.json` 里（无悬空引用）
        self.assertEqual(self.knowledge_ref_ids(), {self.JD_ID})

    def test_the_existing_jd_fields_win_over_the_report_side_ones(self):
        """已有 JD 记录里的字段优先——「只补不覆盖」必须在**认领**这条路上也生效。"""
        self.run_cli(["--执行"])

        jds = jdid.load_jds()["jds"]
        self.assertEqual([r["id"] for r in jds], [self.JD_ID], "旧实现会另造一张 `…_0002`")
        jd = self.jds_by_id()[self.JD_ID]
        # JD 记录里已有的值不许被报告侧那份覆盖（真实数据里两处写法本来就不同）
        self.assertEqual(jd["job_title"], "大模型标注项目团队管理（ASR/TTS方向）")
        self.assertEqual(jd["salary"], "12-20K")
        self.assertEqual(jd["jd_text_file"], self.JD_ID + ".txt")
        self.assertEqual(jd["jd_image"], self.JD_ID + ".jpg")
        self.assertEqual(jd["created_at"], "2026-10-14 15:03")
        # 已有 JD 缺的字段照旧从报告/台账补齐（别做成「整个 JD 都不动」的假绿）
        self.assertEqual(jd["source_type"], "粘贴文本")
        # 认领的出处要记下来（幂等的依据）
        self.assertEqual(jd["migrated_from"], [self.JOB])

    def test_the_table_shows_the_fields_that_will_actually_be_written(self):
        """试跑表里「JD 级字段（原始值，进 jds.json 用）」这一行也必须是**已有 JD 那份**。"""
        _code, text = self.run_cli(["--试跑"])
        self.assertIn("标题=大模型标注项目团队管理（ASR/TTS方向）", text)
        self.assertNotIn("标题=大模型标注（报告侧写法）", text)


class RestoreKeepsProfileRootTest(IsolatedCase):
    """P0-14：回滚**只许删掉自己新建的东西**，用户原来的形状（含空目录、含 profile 根）必须留着。

    `简历迁移.py` 的同一段多两道：`if dirpath == root: continue` 与「备份里就有的空目录不删」。
    `身份迁移.py` 的收尾那圈把**所有**空目录都删掉（除了根）⇒ 一次失败的 `--执行` 会把用户
    profile 里原本就有的空目录（`jd/` 这种）抹掉 —— 而此刻脚本**正在返回错误码**，
    用户下一步就是重试，属于「失败路径上再丢一次」。
    """

    def _tear_profile_subdirs(self):
        """把 profile 根下的自动建出来的子目录清掉（造「空目录树」这个最极端的形状）。"""
        for sub in ("resumes", "reports", "jd", "logs"):
            shutil.rmtree(store.p_path(sub), ignore_errors=True)

    def _execute_with_write_failure(self):
        """注入「写盘立刻失败」→ 走备份恢复那条路（必须真的失败了，否则这条测试是空跑）。"""
        with mock.patch.object(store, "write_json", side_effect=OSError("disk full (注入)")), \
                mock.patch.object(sys, "argv", ["身份迁移.py", "--执行"]), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            code = CLI.main()
        self.assertEqual(code, 1)
        self.assertTrue(glob.glob(os.path.join(store.profiles_root(), "迁移备份_*")))

    def test_a_failed_execute_keeps_the_root_and_the_empty_directories_it_found(self):
        """端到端走一次失败：profile 根与它原来的空目录 `jd/` 都必须在。"""
        self.assertTrue(os.path.isdir(store.p_path("jd")), "夹具前提：profile 里本来就有空目录 jd/")
        self.assertEqual(os.listdir(store.p_path("jd")), [], "夹具前提：它是空的")

        self._execute_with_write_failure()

        self.assertTrue(os.path.isdir(store.profile_dir()),
                        "回滚把 profile 根目录删掉了：%s" % store.profile_dir())
        self.assertTrue(os.path.isdir(store.p_path("jd")),
                        "用户原来的空目录 jd/ 被回滚删掉了")

    def test_restore_keeps_the_profile_root_when_the_tree_is_empty(self):
        """最极端的形状：备份是空的、profile 树也是空的 → profile 根不许被 `rmdir` 掉。

        这一步必须直接调 `_restore_profile`：走 `--执行` 的话备份里总带着
        `set_current_profile()` 建出来的那几个子目录，根就永远是"非空"的，
        根目录保护去掉也测不出来（假绿）。
        """
        self._tear_profile_subdirs()
        empty_backup = tempfile.mkdtemp()

        CLI._restore_profile(empty_backup, set())

        self.assertTrue(os.path.isdir(store.profile_dir()),
                        "回滚把 profile 根目录删掉了：%s" % store.profile_dir())


class MergedRecordTest(IsolatedCase):
    """**决策证据**：已 `merged_into` 的记录**保留原样**（策略 A），不参与建 JD。

    它本来就该被隐藏（台账过滤掉它），给它新造一个 JD 会凭空多出一个只属于已删记录的卡片。
    代价：它的旧 id 继续存在（知识库/面试记录里指向它的引用会改指它 `merged_into` 的 JD）。
    """

    def setUp(self):
        IsolatedCase.setUp(self)
        os.makedirs(store.p_path("reports"), exist_ok=True)
        for jid, date in (("分析A", "2026-09-01 10:00"), ("分析B", "2026-09-02 10:00")):
            store.upsert_job({"id": jid, "report": jid + ".html", "created_at": date,
                              "job_title": "语音/文本评测", "job_category": "数据标注/质检",
                              "location": "北京", "salary": "13-26K", "score": 70.0,
                              "jd_text_file": jid + ".txt", "status": "未投递"})
            store.write_text(store.p_path("jd", jid + ".txt"), LONG_JD)
            write_report(os.path.join(store.p_path("reports"), jid + ".html"),
                         _report_data("语音/文本评测", "13-26K", "数据标注/质检", "北京", "本科", 70.0))
        jobdup = importlib.import_module("jobdup")
        jobdup.merge_jobs("分析A", "分析B")           # B 标记 merged_into=A
        kb.absorb([{"term": "语音识别", "layer": 0, "sources": [], "timeline": [],
                    "related": [], "interview_questions": []}], {"id": "分析B"})

    def test_merged_record_keeps_its_identity_and_its_own_id_is_not_a_jd(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", ["身份迁移.py", "--执行"]):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = CLI.main()
        text = out.getvalue()
        self.assertEqual(code, 0)
        jobs = {j["id"]: j for j in store.load_jobs()}
        self.assertEqual(jobs["分析B"]["merged_into"], "分析A")
        self.assertNotIn("jd_id", jobs["分析B"], "已合并记录不该被挂到新 JD 上")
        self.assertEqual(len(jdid.load_jds()["jds"]), 1)      # B 不产生自己的 JD
        # 但知识库里指向 B 的引用必须改指 A 所在的 JD（不留悬空引用）
        card = store.load_knowledge()["nodes"][0]
        self.assertEqual([x["id"] for x in card["from_jds"]], [jobs["分析A"]["jd_id"]])
        self.assertIn("保留原样", text)


class ResumeIdSurvivesMigrationTest(MigrationTestBase):
    """修 2：`_build_jobs` 的**硬编码白名单**会把 `resume_id` 静默丢掉。

    分析记录里「这次用的是**哪一版**简历」（`resume_id`，阶段 4C）是 `ANALYSIS_FIELDS` 的一员：
    迁移把 JD 级字段搬去 `jds.json` 时**必须逐字留下它** —— 它既是"报告用的是哪一版"的凭据，
    又是查重的另一半粒度。硬编码白名单漏一个键 = **静默丢用户数据**（`--执行` 至今没在真实
    数据上跑过，所以它是"待触发"的）。
    """

    def setUp(self):
        MigrationTestBase.setUp(self)
        jobs = store.load_jobs()                       # 给 A / B 两条分析钉上版本号（真实形状）
        for j in jobs:
            if j["id"] in ("分析A", "分析B"):
                j["resume_id"] = "rv_00%02d" % (1 if j["id"] == "分析A" else 2)
        store.save_jobs(jobs)

    def test_the_resume_id_survives_the_migration_verbatim(self):
        code, _text = self.run_cli(["--执行"])
        self.assertEqual(code, 0)
        jobs = self.jobs_by_id()
        self.assertEqual(jobs["分析A"]["resume_id"], "rv_0001")      # ← 逐字，不是"还在"就行
        self.assertEqual(jobs["分析B"]["resume_id"], "rv_0002")
        self.assertNotIn("resume_id", jobs["分析D"])                  # 没有的不许凭空补
        for jd in jdid.load_jds()["jds"]:
            self.assertNotIn("resume_id", jd, "分析级字段漏进 jds.json 了")

    def test_the_script_field_list_matches_the_app_one(self):
        """`ANALYSIS_FIELDS` 两处各列一份是既有事实，但**必须逐字一致**（同一口径）。"""
        import app
        self.assertEqual(CLI.ANALYSIS_FIELDS, app.ANALYSIS_FIELDS)


def _gbk_unsafe(value):
    """字符串里**编码不进 GBK（cp936）**的字符 → ``["U+26A0", …]``；都安全 → ``[]``。

    为什么要这一道：Windows 控制台默认 cp936，`print`/`sys.stderr.write` 一个非 GBK 字符
    就是 `UnicodeEncodeError` —— 而那两处恰好都在**失败路径**上（备份失败 / 迁移失败），
    用户看到的会是"迁移脚本自己崩了"，真正的失败原因反而被吃掉。这类崩溃以前真发生过。
    """
    bad = []
    for ch in value:
        try:
            ch.encode("gbk")
        except UnicodeEncodeError:
            bad.append("U+%04X" % ord(ch))
    return bad


def _walk_strings(value, path="<module>"):
    """递归遍历（含容器），产出 ``[(path, str), …]``：字符串常量 + 调用时会 `%` 进去的格式串。

    只走**顶层模块成员**的容器（测试自己塞进去的字符串不算本文档的常量）。
    """
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _walk_strings(k, "%s{%r}" % (path, k))
            yield from _walk_strings(v, "%s{%r}" % (path, k))
    elif isinstance(value, (list, tuple, set)):
        for i, v in enumerate(value):
            yield from _walk_strings(v, "%s[%d]" % (path, i))


def _walk_code_strings(code, path="<module>"):
    """**全部**字符串常量：按树遍历 `code.co_consts`，**函数体内的字面量也在内**。

    为什么必须走 code object 而不是 `dir(CLI)`：`sys.stderr.write("…")` 里那个字面量是
    **函数局部的**常量，`dir(模块)` 根本看不见它 —— 只扫模块属性的话，往失败路径里加回一个
    `✗` 照样能过（第一版就是这么漏的，变异取证时抓到了）。这里连嵌套 code 一起走。
    """
    yield path, code.co_name
    for const in code.co_consts:
        if isinstance(const, str):
            yield "%s/%s" % (path, code.co_name), const
        elif hasattr(const, "co_consts"):          # 嵌套的函数 / 推导式
            yield from _walk_code_strings(const, "%s/%s" % (path, code.co_name))
        elif isinstance(const, (tuple, frozenset)):
            for item in const:
                if isinstance(item, str):
                    yield "%s/%s" % (path, code.co_name), item


class GbkSafetyTest(MigrationTestBase):
    """`身份迁移.py` 必须在 cp936 控制台上**说得出话**（非 GBK 字符会 `UnicodeEncodeError`）。

    上一轮同类修复在 `简历迁移.py`（那里的 `⚠`/`✗` 被换掉）；本文件当时还留着 ——
    而它的非 GBK 字符全在**失败路径**上，正常跑永远碰不到，只有真出事时才崩成
    "脚本自己报错"，把真正的原因盖掉。所以这里钉两层：
    ① 源码与全部字符串常量 GBK 可编码（结构性地保证不再有人加回来）；
    ② 把两条真的失败路径走一遍，断言它不是 `UnicodeEncodeError`。
    """

    SOURCE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "身份迁移.py")

    def test_source_file_is_gbk_encodable(self):
        with io.open(self.SOURCE, encoding="utf-8") as f:
            src = f.read()
        self.assertEqual(_gbk_unsafe(src), [],
                         "源码里有非 GBK 字符（cp936 控制台上 print 出去就是 UnicodeEncodeError）")
        src.encode("gbk")                      # 逐字节过一遍真实的编码器，不看上面的结论

    def test_every_string_constant_is_gbk_encodable(self):
        """**全部**字符串常量（含函数体内的字面量 + `%` 进格式串的那两句）。"""
        unsafe = {}
        seen = 0
        # ① 按 code object 走：函数体内的 `"✗ …"` 也在内（`dir(模块)` 看不见它们）
        with io.open(self.SOURCE, encoding="utf-8") as f:
            tree = compile(f.read(), self.SOURCE, "exec")
        for path, text in _walk_code_strings(tree):
            seen += 1
            bad = _gbk_unsafe(text)
            if bad:
                unsafe["%s = %r" % (path, text)] = bad
        self.assertGreater(seen, 100, "一个常量都没扫到 = 扫描本身坏了（假绿）")
        # ② 模块属性里的容器（USAGE 等）再走一遍：两条路互为兜底
        for name in dir(CLI):
            if name.startswith("__"):
                continue
            value = getattr(CLI, name)
            if callable(value):
                continue
            for path, text in _walk_strings(value, name):
                bad = _gbk_unsafe(text)
                if bad:
                    unsafe["%s = %r" % (path, text)] = bad
        self.assertEqual(unsafe, {}, "这些字符串常量 GBK 编不出来：%s" % unsafe)

    def test_the_constant_scan_actually_sees_function_local_strings(self):
        """反假绿：扫描必须真的能看见**函数体内**的字符串（否则它只是个摆设）。

        `sys.stderr.write("✗ …")` 那种字面量是函数局部常量；扫不到就没资格当证据。
        """
        with io.open(self.SOURCE, encoding="utf-8") as f:
            tree = compile(f.read(), self.SOURCE, "exec")
        found = [t for _p, t in _walk_code_strings(tree) if "备份失败" in t]
        self.assertEqual(len(found), 1, "函数体内的字面量扫不到（扫描口径错了）：%r" % found)
        self.assertIn("错误：", found[0])

    def _run_execute(self):
        """跑一次 `--执行`（argv 必须带模式，否则 main 只回用法码 2 什么都不做）。"""
        with mock.patch.object(sys, "argv", ["身份迁移.py", "--执行"]):
            return CLI.main()

    def test_failure_paths_speak_gbk_not_unicode_error(self):
        """走两条真的失败路径：备份失败 + 事务中途写盘失败（报错文案必须 GBK 安全）。"""
        boom = OSError("disk full (注入)")
        real = store.write_json
        calls = {"n": 0}

        def flaky(path, obj):
            calls["n"] += 1
            if calls["n"] == 2:                # jds.json 已落盘、jobs.json 炸 → 走恢复+报错那一支
                raise boom
            return real(path, obj)

        with mock.patch.object(store, "write_json", side_effect=flaky), \
                contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            code = self._run_execute()
        self.assertEqual(code, 1)
        self.assertEqual(calls["n"], 2, "注入没落在「事务中途」上（第二次写盘）")
        for name, text in (("stdout", out.getvalue()), ("stderr", err.getvalue())):
            bad = _gbk_unsafe(text)
            self.assertEqual(bad, [], "%s 里有非 GBK 字符 %s：%r" % (name, bad, text[:200]))
            text.encode("gbk")                 # 真的过一遍编码器（cp936 控制台就是这一步会炸）
        self.assertIn("迁移失败", out.getvalue() + err.getvalue())

        # 备份失败那一支（同样是 sys.stderr.write）：文案也必须 GBK 安全
        with mock.patch.object(CLI, "_backup_profile", side_effect=OSError("no space (注入)")), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()) as err2:
            code2 = self._run_execute()
        self.assertEqual(code2, 1)
        self.assertEqual(_gbk_unsafe(err2.getvalue()), [], repr(err2.getvalue()))
        err2.getvalue().encode("gbk")
        self.assertIn("备份失败", err2.getvalue())


if __name__ == "__main__":
    unittest.main()
