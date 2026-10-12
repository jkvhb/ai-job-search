# -*- coding: utf-8 -*-
"""jdid（JD 稳定身份：生成 / 校验 / 改 ID / JD 库）的测试。

**本块修的是根因**：岗位身份以前是「报告文件名」，里面嵌了日期与分数，于是
「换个简历重分析」必然生成新身份 → 知识库 `from_jds` 越叠越多 →
`multi_jd`（复习优先级的**乘性**因子）算错。用户真实数据里 6 个概念因此虚高。

夹具思路沿用 `tests/test_jobdup.py`：`IsolatedCase` 同时重定向 `store.DATA_ROOT`
与 `log.LOG_FILE`，所以这些测试碰不到用户的真实 data/。

**断言粒度刻意收紧**（本项目已累计 11+ 次假绿）：ID 字符串一律**逐字比全文**；
「重复被拒」与「改名被拒」**同时**断言「返回了错误说明」且「库里的东西一个字节没动」。
"""
import os
import unittest
from unittest import mock

import jdid
import kb
import store

from tests.base import IsolatedCase


class BuildIdTest(IsolatedCase):
    def test_full_fields_make_seven_segments(self):
        f = {"education": "本科", "company": "字节跳动", "duty": "标注",
             "domain": "文本", "city": "北京", "salary": "6-8K"}
        self.assertEqual(jdid.build_id(f, 1), "本科_字节跳动_标注_文本_北京_6-8K_0001")

    def test_missing_fields_become_unknown_placeholders(self):
        f = {"education": None, "company": "", "duty": "标注",
             "domain": None, "city": "北京", "salary": None}
        got = jdid.build_id(f, 7)
        self.assertEqual(got, "【未知学历】_【未知单位】_标注_【未知领域】_北京_【未知薪资】_0007")

    def test_slash_in_duty_is_cleaned_so_it_can_be_a_filename(self):
        got = jdid.build_id({"duty": "数据标注/质检"}, 2)
        self.assertNotIn("/", got)
        self.assertIn("数据标注质检", got)
        # 补强（计划外）：四段未知 + 清洗后的职责 + 补零编号，逐字比全文
        self.assertEqual(got, "【未知学历】_【未知单位】_数据标注质检_【未知领域】_【未知城市】_【未知薪资】_0002")

    def test_seq_is_always_four_digits(self):
        self.assertTrue(jdid.build_id({"duty": "x"}, 12).endswith("_0012"))
        self.assertTrue(jdid.build_id({"duty": "x"}, 12345).endswith("_12345"))   # 溢出不加长错误

    def test_salary_keeps_its_own_hyphen(self):
        # 这是分隔符改用 _ 的原因：13-26K 里的 - 不能和字段分隔符混淆
        got = jdid.build_id({"salary": "13-26K", "city": "北京"}, 1)
        self.assertEqual(jdid.parse_id(got)[0]["salary"], "13-26K")

    def test_every_illegal_filename_char_is_cleaned(self):
        """补强（计划外）：`BAD_CHARS` 里的字符一个都不能漏进 ID（ID 会进文件名）。"""
        got = jdid.build_id({"education": "本\\科", "company": '字:节*跳?动"<集>团|',
                             "duty": "标/注", "domain": "文:本", "city": "北*京",
                             "salary": "6-8K", }, 1)
        for ch in '\\/:*?"<>|':
            self.assertNotIn(ch, got, "ID 里不该有非法字符 %r：%s" % (ch, got))
        # 每段的内容被清洗后仍逐字可读（`集>团` 去掉 `>` 后是 `集团`）
        self.assertEqual(got, "本科_字节跳动集团_标注_文本_北京_6-8K_0001")

    def test_whitespace_is_removed(self):
        self.assertEqual(jdid.build_id({"city": "北 京\t", "duty": "标 注"}, 3),
                         "【未知学历】_【未知单位】_标注_【未知领域】_北京_【未知薪资】_0003")

    def test_bad_input_never_raises(self):
        """补强（计划外）：fields 是 None / 非字典、字段是数字/负数/列表、
        seq 是字符串或负数 —— 全都不该抛异常。"""
        self.assertEqual(jdid.build_id(None, 1),
                         "【未知学历】_【未知单位】_【未知职责】_【未知领域】_【未知城市】_【未知薪资】_0001")
        self.assertEqual(jdid.build_id("不是字典", 1).count(jdid.SEP), 6)
        self.assertTrue(jdid.build_id({"duty": "x"}, None).endswith("_0000"))
        self.assertTrue(jdid.build_id({"duty": "x"}, -3).endswith("_0000"))
        self.assertTrue(jdid.build_id({"duty": "x"}, "7").endswith("_0007"))
        # 数字/列表也会被 str() 后清洗（`[1, 2]` 里的空格同样被去掉）
        self.assertEqual(jdid.build_id({"duty": -5, "city": [1, 2], "salary": 6}, 1),
                         "【未知学历】_【未知单位】_-5_【未知领域】_[1,2]_6_0001")


class DeriveFieldsTest(IsolatedCase):
    def test_derives_from_report_data(self):
        f = jdid.derive_fields({"education": "本科", "company": "", "job_category": "数据标注/质检",
                                "location": "北京", "salary": "13-26K", "job_title": "语音/文本评测"})
        self.assertEqual(f["education"], "本科")
        self.assertIsNone(f["company"])            # 空 → None → 由 build_id 填【未知单位】
        self.assertEqual(f["city"], "北京")
        self.assertEqual(f["salary"], "13-26K")
        self.assertEqual(f["domain"], "语音")       # 标题里有"语音"
        # 补强（计划外）：职责原样带出（清洗是 build_id 的活），键名齐全
        self.assertEqual(f["duty"], "数据标注/质检")
        self.assertEqual(sorted(f), ["city", "company", "domain", "duty", "education", "salary"])

    def test_domain_falls_back_to_unknown(self):
        f = jdid.derive_fields({"job_title": "某某岗位", "job_category": "其他"})
        self.assertIsNone(f["domain"])

    def test_education_falls_back_to_hard_gates(self):
        f = jdid.derive_fields({"education": "",
                                "hard_gates": [{"name": "学历", "jd": "本科"}]})
        self.assertEqual(f["education"], "本科")

    def test_city_falls_back_to_address_and_is_not_the_whole_address(self):
        """城市 ← location 或 address。address 常是「成都武侯区瑞鑫时代大厦-A座」，
        取首段（到第一个区/路/号一类分隔）比整串当城市合理 —— 整串根本进不了 ID 的可读性。"""
        f = jdid.derive_fields({"address": "成都武侯区瑞鑫时代大厦-A座"})
        self.assertEqual(f["city"], "成都")

    def test_domain_rules_are_keyword_rules_over_title_keywords_and_jd_text(self):
        """补强（计划外）：领域没有现成字段，必须真的从 标题 / keywords / JD 原文 里推。"""
        self.assertEqual(jdid.derive_fields({"job_title": "视频标注AI训练标注师"})["domain"], "视频")
        self.assertEqual(jdid.derive_fields({"keywords": ["多模态", "语音"]})["domain"], "多模态")
        self.assertEqual(jdid.derive_fields({"jd_text": "负责大模型Agent评测"})["domain"], "Agent")
        self.assertEqual(jdid.derive_fields({"job": {"text": "图像质量标注"}})["domain"], "图片")
        self.assertIsNone(jdid.derive_fields({})["domain"])

    def test_hard_gate_education_ignores_non_education_rows(self):
        f = jdid.derive_fields({"hard_gates": [{"name": "经验", "jd": "3年"},
                                               {"name": "学历", "jd": "大专"}]})
        self.assertEqual(f["education"], "大专")

    def test_missing_everything_gives_all_none(self):
        f = jdid.derive_fields({})
        self.assertEqual(sorted(f), ["city", "company", "domain", "duty", "education", "salary"])
        for k, v in f.items():
            self.assertIsNone(v, "%s 应该是 None，实际 %r" % (k, v))


class ValidateIdTest(IsolatedCase):
    def test_duplicate_is_rejected(self):
        jdid.upsert_jd({"id": "本科_字节_标注_文本_北京_6-8K_0001"})
        err = jdid.validate_id("本科_字节_标注_文本_北京_6-8K_0001", jdid.load_jds()["jds"])
        self.assertIsNotNone(err)
        self.assertIn("占用", err)

    def test_duplicate_rejection_returns_error_and_leaves_library_untouched(self):
        """补强（计划外）：**同时**断言「返回了错误说明」与「库里的东西没变」。

        只断言 assertIsNotNone(err) 是假绿高危位：一个只会挑刺（对任何输入都返回错误）
        的实现照样过，而「库里被动过」也照样过。
        """
        jdid.upsert_jd({"id": "本科_字节_标注_文本_北京_6-8K_0001", "seq": 1, "company": "字节"})
        before = store.read_text(store.p_path("jds.json"), None)
        err = jdid.validate_id("本科_字节_标注_文本_北京_6-8K_0001", jdid.load_jds()["jds"])
        self.assertIsNotNone(err)
        self.assertIn("占用", err)
        self.assertEqual(store.read_text(store.p_path("jds.json"), None), before)
        self.assertEqual(jdid.load_jds()["jds"][0]["company"], "字节")

    def test_excluding_self_allows_unchanged_id(self):
        jdid.upsert_jd({"id": "本科_字节_标注_文本_北京_6-8K_0001"})
        self.assertIsNone(jdid.validate_id("本科_字节_标注_文本_北京_6-8K_0001",
                                           jdid.load_jds()["jds"],
                                           exclude_id="本科_字节_标注_文本_北京_6-8K_0001"))

    def test_a_fresh_id_is_accepted(self):
        """补强（计划外）：反向对照 —— 一个没被占用的合法 ID 必须过。

        没有这条，`validate_id` 写成「永远返回错误」也会让上面几条「被拒」测试全绿。
        """
        jdid.upsert_jd({"id": "本科_字节_标注_文本_北京_6-8K_0001"})
        self.assertIsNone(jdid.validate_id("硕士_腾讯_评测_语音_上海_20-30K_0002",
                                           jdid.load_jds()["jds"]))

    def test_bad_segment_count_and_empty_segment_are_rejected(self):
        for bad in ("只有两_段", "本科__标注_文本_北京_6-8K_0001", "本科_字节_标注_文本_北京_6-8K"):
            with self.subTest(bad=bad):
                self.assertIsNotNone(jdid.validate_id(bad, []))

    def test_anything_build_id_produces_must_validate(self):
        """**契约**：`build_id` 生成什么，`validate_id` 就必须接受什么。

        这条是拿**真实数据**钉出来的：真实公司名带全角括号
        （`四川金沙人力资源开发管理有限公司（金沙人力）`）、真实薪资带全角括号与逗号
        （`6-8K（入职五险，提及一金7k+）`）。校验若用「只允许中英文/数字/-/_/【】」的白名单，
        自动生成的 ID 就存不进去 —— 任务 2 的接线会在用户真实数据上直接崩。
        """
        real = [
            {"education": "本科", "company": "", "job_category": "数据标注/质检",
             "location": "北京", "salary": "13-26K", "job_title": "语音/文本评测"},
            {"education": "本科", "company": "四川金沙人力资源开发管理有限公司（金沙人力）",
             "job_category": "数据标注/质检", "location": "成都",
             "salary": "6-8K（入职五险，提及一金7k+）", "job_title": "统本标注（视频标注/AI训练标注师）"},
            {"education": "本\\科", "company": '字:节*跳?动"<集>团|', "job_category": "数据标注/质检",
             "location": "北京", "salary": "13-26K", "job_title": "语音/文本评测"},
            {},
        ]
        for data in real:
            with self.subTest(data=data):
                jd_id = jdid.build_id(jdid.derive_fields(data), 1)
                self.assertIsNone(jdid.validate_id(jd_id, []), "build_id 的产物必须能过校验：%s" % jd_id)
                # 再走一遍解析 → 拼接，必须完全回到原样（否则存进库的会和台账显示的不一样）
                fields, seq = jdid.parse_id(jd_id)
                self.assertEqual(jdid.build_id(fields, seq), jd_id)

    def test_illegal_characters_in_a_segment_are_rejected(self):
        """补强（计划外）：手改 ID 时任何一段都不能带 `/` 等文件名非法字符。"""
        for bad in ("本科_字节_标注_文本_北/京_6-8K_0001",
                    '本科_字节_标注_文*本_北京_6-8K_0001',
                    "本科_字节_标注_文本_北京_6-8K_\t0001"):
            with self.subTest(bad=bad):
                self.assertIsNotNone(jdid.validate_id(bad, []))

    def test_whitespace_is_rejected_with_its_own_message(self):
        """空白单独给说明（它会被 build_id 洗掉，不拦就是「显示 A 存 B」）。"""
        err = jdid.validate_id("本科_字节_标注_文本_北京_6-8K_ 0001", [])
        self.assertIsNotNone(err)
        self.assertIn("空白", err)

    def test_error_text_explains_what_is_wrong(self):
        """补强（计划外）：错误说明要能告诉用户缺什么（前端直接展示给用户手改）。"""
        self.assertIn("7", jdid.validate_id("只有两_段", []))
        self.assertIn("岗位", jdid.validate_id("只有两_段", []))

    def test_unknown_placeholder_segment_is_allowed(self):
        """规格 §4：允许把某段改成 `【未知xx】`。"""
        self.assertIsNone(jdid.validate_id("本科_【未知单位】_标注_文本_北京_6-8K_0001", []))


class RenameJdTest(IsolatedCase):
    OLD = "本科_【未知单位】_标注_文本_北京_13-26K_0001"
    NEW = "本科_字节跳动_标注_文本_北京_13-26K_0001"

    def _fixture(self):
        """一个 JD + 一条挂在它下面的分析 + 一张 from_jds 指向它的卡 + 一个 job_id 指向它的面试记录。"""
        jdid.upsert_jd({"id": self.OLD, "seq": 1, "company": ""})
        store.upsert_job({"id": "a1", "jd_id": self.OLD, "resume": "母版",
                          "score": 75.0, "report": "a1.html"})
        store.write_text(os.path.join(store.p_path("reports"), "a1.html"), "<html>旧报告</html>")
        kb.absorb([{"term": "语音识别", "layer": 0, "sources": [], "timeline": [],
                    "related": [], "interview_questions": []}], {"id": self.OLD})
        store.save_interview_json("iv1", "status.json", {"state": "done", "job_id": self.OLD})

    def test_rename_updates_history_and_all_references(self):
        self._fixture()
        jdid.rename_jd(self.OLD, self.NEW)
        rec = jdid.load_jds()["jds"][0]
        self.assertEqual(rec["id"], self.NEW)
        self.assertIn(self.OLD, rec["id_history"])
        self.assertEqual(store.load_knowledge()["nodes"][0]["from_jds"][0]["id"], self.NEW)
        self.assertEqual(store.load_interview_json("iv1", "status.json")["job_id"], self.NEW)
        self.assertEqual(store.load_jobs()[0]["jd_id"], self.NEW)          # 分析也跟着改

    def test_rename_updates_each_reference_individually(self):
        """补强（计划外）：三处引用**逐处单独断言**，另外两处故意写坏也不会被掩盖。

        `test_rename_updates_history_and_all_references` 里四处断言共用一份夹具，
        但每条都独立取自己的文件读 —— 这里再把「只改了 jds.json」这类半成品实现钉死：
        逐处断言 + 断言旧值在三处都**不复存在**。
        """
        self._fixture()
        jdid.rename_jd(self.OLD, self.NEW)
        # 1) jds.json
        jds_rec = jdid.load_jds()["jds"][0]
        self.assertEqual(jds_rec["id"], self.NEW)
        self.assertNotEqual(jds_rec["id"], self.OLD)
        self.assertEqual(jds_rec["id_history"], [self.OLD])
        # 2) 知识库 from_jds —— 逐条断言，不是只看「有没有抛异常」
        frm = store.load_knowledge()["nodes"][0]["from_jds"]
        self.assertEqual(len(frm), 1)
        self.assertEqual(frm[0]["id"], self.NEW)
        self.assertNotEqual(frm[0]["id"], self.OLD)
        # 3) 面试记录
        self.assertEqual(store.load_interview_json("iv1", "status.json")["job_id"], self.NEW)
        # 4) 分析记录（jobs.json）
        jobs = store.load_jobs()
        self.assertEqual([j["jd_id"] for j in jobs], [self.NEW])
        self.assertEqual(jobs[0]["id"], "a1")            # 分析自己的 id 不被动

    def test_rename_does_not_touch_existing_report_files(self):
        self._fixture()
        jdid.rename_jd(self.OLD, self.NEW)
        path = os.path.join(store.p_path("reports"), "a1.html")
        self.assertTrue(os.path.exists(path))                              # 文件没被改名
        self.assertEqual(store.read_text(path), "<html>旧报告</html>")     # 内容也没动
        self.assertEqual(store.load_jobs()[0]["report"], "a1.html")        # 记录里的文件名不变

    def test_rename_to_a_taken_id_is_rejected_and_changes_nothing(self):
        self._fixture()
        jdid.upsert_jd({"id": "另一个_JD_标注_文本_上海_6-8K_0002", "seq": 2})
        before = store.load_knowledge()["nodes"][0]["from_jds"][0]["id"]
        self.assertEqual(before, self.OLD)                                 # 断言的是那个非空的旧 id
        with self.assertRaises(ValueError):
            jdid.rename_jd(self.OLD, "另一个_JD_标注_文本_上海_6-8K_0002")
        self.assertEqual(jdid.load_jds()["jds"][0]["id"], self.OLD)        # 没改成
        self.assertEqual(store.load_knowledge()["nodes"][0]["from_jds"][0]["id"], before)  # 引用也没动

    def test_rejection_message_says_occupied_and_nothing_else_moved(self):
        """补强（计划外）：「重复被拒」**同时**断言错误说明与三处引用一个字节没动。"""
        self._fixture()
        jdid.upsert_jd({"id": "另一个_JD_标注_文本_上海_6-8K_0002", "seq": 2})
        kb_before = store.read_text(store.knowledge_path(), None)
        jobs_before = store.read_text(store.jobs_path(), None)
        iv_before = store.read_text(store.interview_file("iv1", "status.json"), None)
        jds_before = store.read_text(store.p_path("jds.json"), None)
        with self.assertRaises(ValueError) as cm:
            jdid.rename_jd(self.OLD, "另一个_JD_标注_文本_上海_6-8K_0002")
        self.assertIn("占用", str(cm.exception))
        self.assertEqual(store.read_text(store.knowledge_path(), None), kb_before)
        self.assertEqual(store.read_text(store.jobs_path(), None), jobs_before)
        self.assertEqual(store.read_text(store.interview_file("iv1", "status.json"), None), iv_before)
        self.assertEqual(store.read_text(store.p_path("jds.json"), None), jds_before)

    def test_rename_to_a_malformed_id_is_rejected_and_changes_nothing(self):
        """补强（计划外）：段数/非法字符照样要拒（手改入口不能绕过校验）。"""
        self._fixture()
        jds_before = store.read_text(store.p_path("jds.json"), None)
        for bad in ("只有两_段", "本科_字节_标注_文本_北/京_13-26K_0001"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    jdid.rename_jd(self.OLD, bad)
                self.assertEqual(jdid.load_jds()["jds"][0]["id"], self.OLD)
        self.assertEqual(store.read_text(store.p_path("jds.json"), None), jds_before)

    def test_rename_of_an_unknown_id_raises_and_changes_nothing(self):
        kb_before = store.read_text(store.knowledge_path(), None)
        with self.assertRaises(ValueError):
            jdid.rename_jd("本科_【未知单位】_标注_文本_北京_13-26K_9999", self.NEW)
        self.assertEqual(store.read_text(store.knowledge_path(), None), kb_before)

    def test_a_failure_midway_rolls_everything_back(self):
        """**落盘中途失败必须回滚**，绝不留「jds.json 改了、引用没改」的半改状态。

        注入方式：写面试记录那一步（jds.json 与知识库都已经写过了）抛异常。
        没有这条，回滚分支就是一段从没被执行过的死代码，而它正是用户数据的安全网。
        """
        self._fixture()
        before_jds = store.read_text(store.p_path("jds.json"), None)
        before_kb = store.read_text(store.knowledge_path(), None)
        before_iv = store.read_text(store.interview_file("iv1", "status.json"), None)
        real_write_json = store.write_json
        iv_path = store.interview_file("iv1", "status.json")

        def flaky(path, obj, *a, **kw):
            if path == iv_path:
                raise OSError("注入的写失败")
            return real_write_json(path, obj, *a, **kw)

        with mock.patch.object(store, "write_json", flaky):
            with self.assertRaises(ValueError):
                jdid.rename_jd(self.OLD, self.NEW)

        self.assertEqual(jdid.load_jds()["jds"][0]["id"], self.OLD)     # jds.json 回去了
        self.assertEqual(store.read_text(store.p_path("jds.json"), None), before_jds)
        self.assertEqual(store.read_text(store.knowledge_path(), None), before_kb)
        self.assertEqual(store.read_text(iv_path, None), before_iv)
        frm = store.load_knowledge()["nodes"][0]["from_jds"]
        self.assertEqual([x["id"] for x in frm], [self.OLD])           # 引用一条都没被改跑

    def test_rename_keeps_a_single_reference_when_both_old_and_new_are_present(self):
        """补强（计划外）：卡上同时指向 old 与 new 时，改完不能出现两条指向同一个 JD 的重复项
        （那会让 `multi_jd` 把同一个岗位数两次 —— 正是本块要修的根因的另一种形态）。"""
        self._fixture()
        store.save_knowledge({"version": 1, "nodes": [{
            "id": "x", "term": "语音识别", "from_jds": [
                {"id": self.OLD, "job_title": "旧"}, {"id": self.NEW, "job_title": "新"}]}]})
        jdid.rename_jd(self.OLD, self.NEW)
        frm = store.load_knowledge()["nodes"][0]["from_jds"]
        self.assertEqual([x["id"] for x in frm], [self.NEW])
        self.assertEqual(frm[0]["job_title"], "新")   # 保留本来就在的那一条


class JdsStoreTest(IsolatedCase):
    def test_roundtrip_and_junk_tolerance(self):
        store.write_json(store.p_path("jds.json"), {"version": 1, "seq": 0, "jds": "不是列表"})
        d = jdid.load_jds()
        self.assertEqual(d["jds"], [])
        self.assertEqual(d["seq"], 0)

    def test_next_seq_increments_and_persists(self):
        self.assertEqual(jdid.next_seq(), 1)
        self.assertEqual(jdid.next_seq(), 2)
        self.assertEqual(jdid.load_jds()["seq"], 2)

    def test_missing_file_gives_an_empty_library(self):
        self.assertEqual(jdid.load_jds(), {"version": 1, "seq": 0, "jds": []})

    def test_junk_records_are_dropped_and_seq_is_coerced(self):
        store.write_json(store.p_path("jds.json"),
                         {"version": 1, "seq": "3", "jds": ["字符串", {"无id": 1}, {"id": "x"}]})
        d = jdid.load_jds()
        self.assertEqual([r["id"] for r in d["jds"]], ["x"])
        self.assertEqual(d["seq"], 3)

    def test_upsert_updates_in_place_and_find_jd_reads_it_back(self):
        jdid.upsert_jd({"id": "本科_字节_标注_文本_北京_6-8K_0001", "company": ""})
        jdid.upsert_jd({"id": "本科_字节_标注_文本_北京_6-8K_0001", "company": "字节跳动"})
        self.assertEqual(len(jdid.load_jds()["jds"]), 1)
        self.assertEqual(jdid.find_jd("本科_字节_标注_文本_北京_6-8K_0001")["company"], "字节跳动")
        self.assertIsNone(jdid.find_jd("不存在_的_id_标注_文本_北京_6-8K_0002"))

    def test_upsert_stamps_created_at_and_ids_are_reachable(self):
        rec = jdid.upsert_jd({"id": "本科_字节_标注_文本_北京_6-8K_0001"})
        self.assertTrue(rec.get("created_at"))
        self.assertEqual(jdid.load_jds()["seq"], 0)     # upsert 不偷偷吃编号

    def test_save_jds_is_atomic_and_keeps_utf8(self):
        jdid.save_jds({"version": 1, "seq": 9, "jds": [{"id": "本科_【未知单位】_标注_文本_北京_6-8K_0001"}]})
        d = jdid.load_jds()
        self.assertEqual(d["seq"], 9)
        self.assertEqual(d["jds"][0]["id"], "本科_【未知单位】_标注_文本_北京_6-8K_0001")
        # 临时文件不能在目录里留垃圾（store.write_json 的约定）
        left = [n for n in os.listdir(store.profile_dir()) if n.startswith(".tmp-")]
        self.assertEqual(left, [])


class AnalysesOfTest(IsolatedCase):
    def test_analyses_of_returns_only_that_jd_sorted_by_created_at(self):
        jdid.upsert_jd({"id": "本科_字节_标注_文本_北京_6-8K_0001"})
        store.upsert_job({"id": "a2", "jd_id": "本科_字节_标注_文本_北京_6-8K_0001",
                          "created_at": "2026-10-08 10:00", "resume": "母版"})
        store.upsert_job({"id": "a1", "jd_id": "本科_字节_标注_文本_北京_6-8K_0001",
                          "created_at": "2026-10-03 09:00", "resume": "母版"})
        store.upsert_job({"id": "b1", "jd_id": "别的_JD_标注_文本_上海_6-8K_0002",
                          "created_at": "2026-10-01 09:00"})
        got = jdid.analyses_of("本科_字节_标注_文本_北京_6-8K_0001")
        self.assertEqual([j["id"] for j in got], ["a1", "a2"])

    def test_jd_id_of_analysis_reads_the_record(self):
        store.upsert_job({"id": "a1", "jd_id": "本科_字节_标注_文本_北京_6-8K_0001"})
        self.assertEqual(jdid.jd_id_of_analysis(store.find_job("a1")),
                         "本科_字节_标注_文本_北京_6-8K_0001")
        self.assertEqual(jdid.jd_id_of_analysis(None), "")
        self.assertEqual(jdid.jd_id_of_analysis({}), "")


class LockingTest(IsolatedCase):
    def test_jd_lock_exists_and_is_reentrant(self):
        """整体互斥那把锁必须是 RLock（写路径可能互相调用，普通 Lock 会死锁）。"""
        self.assertTrue(hasattr(jdid, "_JD_LOCK"))
        with jdid._JD_LOCK:
            with jdid._JD_LOCK:
                pass

    def test_library_write_does_not_wake_the_kb_lock_owner(self):
        """写 jds.json 本身不该去动 knowledge.json（职责分开，kb 是纯数据模块）。"""
        jdid.upsert_jd({"id": "本科_字节_标注_文本_北京_6-8K_0001"})
        self.assertFalse(os.path.exists(store.knowledge_path()))

    def test_kb_does_not_import_jdid(self):
        """架构铁律：`kb.py` 绝不能 import `jdid`（方向不能反）。"""
        import re
        src = store.read_text(os.path.join(store.ROOT, "kb.py"))
        self.assertIsNone(re.search(r"^\s*(import|from)\s+jdid\b", src, re.M))

    def test_rename_actually_holds_the_kb_lock_while_writing_knowledge(self):
        """写 `knowledge.json` 必须持既有那把 `kb._KB_LOCK`（本项目真实丢过更新 / 写坏过文件）。

        在 `store.save_knowledge` 里面直接问「这把锁现在被谁持着」：
        锁没被持有时 `_is_owned()` 是 False，持着时是 True —— 只有真加锁才可能为 True。
        """
        OLD = RenameJdTest.OLD
        NEW = RenameJdTest.NEW
        jdid.upsert_jd({"id": OLD, "seq": 1})
        kb.absorb([{"term": "语音识别", "layer": 0, "sources": [], "timeline": [],
                    "related": [], "interview_questions": []}], {"id": OLD})
        seen = []
        real = store.save_knowledge

        def watch(data, *a, **kw):
            seen.append(kb._KB_LOCK._is_owned())
            return real(data, *a, **kw)

        with mock.patch.object(store, "save_knowledge", watch):
            jdid.rename_jd(OLD, NEW)
        self.assertTrue(seen, "整个流程居然没写 knowledge.json，这条测试就没验证到东西")
        self.assertTrue(all(seen), "写 knowledge.json 时没有持 kb._KB_LOCK：%s" % seen)
        # 注意：这里**不能**用 `_is_owned()` 断言「返回时锁已释放」——
        # 当前线程没持锁时 `_is_owned()` 也是 False，它区分不了「释放了」和「本来就没持」。
        self.assertEqual(store.load_knowledge()["nodes"][0]["from_jds"][0]["id"], NEW)
        self.assertIsNone(jdid.validate_id(NEW, jdid.load_jds()["jds"], exclude_id=NEW))


if __name__ == "__main__":
    unittest.main()
