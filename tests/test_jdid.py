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
import glob
import json
import os
import unittest
from unittest import mock

import jdid
import jdrefs
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


class NormalizeFieldsTest(IsolatedCase):
    def test_education_keeps_only_one_short_tier(self):
        for raw, want in [("统招公办本科及以上", "本科"), ("本科", "本科"),
                          ("大专及以上", "大专"), ("硕士", "硕士"),
                          ("博士研究生", "博士"), ("不限", "不限"), ("学历不限", "不限"),
                          ("本科及以上，硕士优先", "硕士"),   # 取出现的最高档
                          ("中专或高中", "中专")]:
            with self.subTest(raw=raw):
                self.assertEqual(jdid._normalize_education(raw), want)

    def test_education_that_cannot_be_pinned_stays_none(self):
        for raw in ("", "  ", None, "看能力", 5, ["本科"]):
            with self.subTest(raw=raw):
                self.assertIsNone(jdid._normalize_education(raw))

    def test_salary_keeps_only_the_range(self):
        for raw, want in [("6-8K（入职五险，提及一金7k+）", "6-8K"),
                          ("6-8K（一金7k+）", "6-8K"),
                          ("13-26K", "13-26K"),
                          ("13–26K", "13-26K"),          # 全角破折号 → 半角
                          ("20—30K", "20-30K"),
                          ("6至8K", "6-8K")]:
            with self.subTest(raw=raw):
                self.assertEqual(jdid._normalize_salary(raw), want)

    def test_salary_without_a_range_stays_none(self):
        for raw in ("", "面议", "20K", "13薪", None, 8):
            with self.subTest(raw=raw):
                self.assertIsNone(jdid._normalize_salary(raw))

    def test_company_prefers_the_shorter_of_bracket_and_prefix(self):
        for raw, want in [("四川金沙人力资源开发管理有限公司（金沙人力）", "金沙人力"),
                          ("金沙人力（四川金沙人力资源开发管理有…）", "金沙人力"),
                          ("某 AI 科技公司（示例）", "示例"),
                          ("（示例）", "示例")]:
            with self.subTest(raw=raw):
                self.assertEqual(jdid._normalize_company(raw), want)

    def test_company_without_brackets_is_truncated_to_twelve_chars(self):
        self.assertEqual(jdid._normalize_company("四川金沙人力资源开发管理有限公司"),
                         "四川金沙人力资源开发管理")          # 16 → 剥后缀后正好 12
        self.assertEqual(jdid._normalize_company("字节跳动"), "字节跳动")   # 短的照旧
        # 截断不能留半个括号（未闭合的括号也要砍在它前面）
        self.assertEqual(jdid._normalize_company("某某某某某某某某某某（简称"),
                         "某某某某某某某某某某")

    def test_legal_suffix_is_stripped_before_truncating(self):
        """**这条规则存在的理由**：不留「半个后缀」。

        名字**超过** 12 字时最明显：`北京字节跳动网络技术有限公司`（14 字）不剥后缀、
        直接截断到 12 字就会切出 `北京字节跳动网络技术有限` —— 半个 `有限公司`，
        用户每天看这个身份串，一眼就是坏的。
        """
        raw = "北京字节跳动网络技术有限公司"
        self.assertEqual(len(raw), 14)
        self.assertEqual(raw[:12], "北京字节跳动网络技术有限")      # 不剥就是这个坏结果
        self.assertEqual(jdid._normalize_company(raw), "北京字节跳动网络技术")
        # 其余形态
        self.assertEqual(jdid._normalize_company("北京字节跳动科技有限公司"), "北京字节跳动科技")
        self.assertEqual(jdid._normalize_company("北京某某科技有限公司"), "北京某某科技")
        self.assertEqual(jdid._normalize_company("广州某某信息技术有限责任公司"), "广州某某信息技术")
        self.assertEqual(jdid._normalize_company("某某某某某某某某某某有限公司"), "某某某某某某某某某某")

    def test_legal_suffix_stripping_tries_the_longest_first(self):
        """`集团有限公司` 必须先于 `公司` 匹配，否则会剥出 `某某集团有限`。"""
        self.assertEqual(jdid._normalize_company("某某集团有限公司"), "某某")
        self.assertEqual(jdid._normalize_company("某某集团"), "某某")
        self.assertEqual(jdid._normalize_company("某某股份有限公司"), "某某")

    def test_stripping_to_an_empty_company_falls_back_to_not_stripping(self):
        """后缀剥完**为空**时必须退回不剥：否则用户填过的公司名变成 `【未知单位】`。"""
        for raw in ("有限公司", "公司", "集团", "有限责任公司"):
            with self.subTest(raw=raw):
                self.assertEqual(jdid._normalize_company(raw), raw)
        self.assertEqual(jdid.normalize_fields({"company": "有限公司"})["company"], "有限公司")
        self.assertEqual(jdid.build_id(jdid.normalize_fields({"company": "有限公司"}), 1),
                         "【未知学历】_有限公司_【未知职责】_【未知领域】_【未知城市】_【未知薪资】_0001")

    def test_bracketed_company_uses_the_shorter_side_when_the_bracket_is_at_the_end(self):
        """括号**在结尾** → 取更短一侧（括号里是简称 / 注解）。"""
        self.assertEqual(jdid._normalize_company("四川金沙人力资源开发管理有限公司（金沙人力）"),
                         "金沙人力")
        self.assertEqual(jdid._normalize_company("某 AI 科技公司（示例）"), "示例")

    def test_midfix_bracket_is_a_qualifier_and_gets_dropped(self):
        """括号**后面还有字** → 括号里是 `（中国）`/`（北京）` 这类**限定词**，整段去掉。

        这条修的是一个真 bug：`阿里巴巴（中国）有限公司` 与 `腾讯（中国）有限公司`
        若都按「取更短一侧」走，会双双归成 `中国` → 两个不同的公司同一段 → 撞 ID →
        用户看到莫名其妙的「ID 已被占用」。
        """
        self.assertEqual(jdid._normalize_company("阿里巴巴（中国）有限公司"), "阿里巴巴")
        self.assertEqual(jdid._normalize_company("腾讯（中国）有限公司"), "腾讯")
        self.assertEqual(jdid._normalize_company("百度（中国）有限公司"), "百度")
        # 去掉括号后外面还带别的后缀形态：**我的判断** —— `某公司分公司` 再剥 `分公司`，
        # 留 `某公司`（只认 `公司` 会剥成 `某公司分`，吊着一个 `分`，所以 `分公司` 也进了后缀表）
        self.assertEqual(jdid._normalize_company("某公司（北京）分公司"), "某公司")
        self.assertEqual(jdid._normalize_company("四川金沙（成都）分公司"), "四川金沙")
        # 多个限定词括号一起去掉
        self.assertEqual(jdid._normalize_company("阿里巴巴（中国）（北京）有限公司"), "阿里巴巴")
        # 中缀括号 + 结尾括号混在一起：限定词与注解都不要，剩下的当全名剥后缀
        self.assertEqual(jdid._normalize_company("某公司（北京）分公司（简称）"), "某公司")

    def test_two_different_companies_never_share_a_company_segment(self):
        """**本次的核心不变量**：两家不同的公司，公司段必须不同。

        没有这条，`（中国）` 那类限定词就会把不同公司压成同一段，其余段再相同就撞 ID。
        """
        companies = [
            "阿里巴巴（中国）有限公司",
            "腾讯（中国）有限公司",
            "百度（中国）有限公司",
            "四川金沙人力资源开发管理有限公司（金沙人力）",
            "某 AI 科技公司（示例）",
            "北京字节跳动科技有限公司",
            "北京字节跳动网络技术有限公司",
            "某公司（北京）分公司",
        ]
        segs = [jdid._normalize_company(c) for c in companies]
        self.assertEqual(len(set(segs)), len(companies),
                         "两家不同的公司拿到了同一个公司段：%s" % segs)
        # 其余段相同的前提下，整条 ID 也必须不同（否则用户会看到「ID 已被占用」）
        ids = [jdid.build_id({"company": s, "duty": "标注", "city": "北京"}, 1) for s in segs]
        self.assertEqual(len(set(ids)), len(companies), "整条 ID 撞了：%s" % ids)

    def test_the_four_agreed_examples_stay_the_same(self):
        """父代理裁决时点名的 4 个例子：加了限定词判据之后**结果一个都不能变**。"""
        self.assertEqual(jdid._normalize_company("四川金沙人力资源开发管理有限公司（金沙人力）"),
                         "金沙人力")
        self.assertEqual(jdid._normalize_company("金沙人力（四川金沙人力资源开发管理有…）"),
                         "金沙人力")
        self.assertEqual(jdid._normalize_company("某 AI 科技公司（示例）"), "示例")
        self.assertEqual(jdid._normalize_company("阿里巴巴（中国）有限公司"), "阿里巴巴")

    def test_bare_qualifier_bracket_no_longer_collapses_two_companies(self):
        """**上一轮的缺口，本轮已修**：括号在**结尾**、里面是纯限定词时也要忽略。

        `阿里巴巴（中国）` 与 `腾讯（中国）` 按「取更短一侧」会双双变成 `中国`
        （本轮之前它就是被钉住的那个缺口）→ 两个不同公司同一段 → 撞 ID。
        """
        self.assertEqual(jdid._normalize_company("阿里巴巴（中国）"), "阿里巴巴")
        self.assertEqual(jdid._normalize_company("腾讯（中国）"), "腾讯")
        self.assertNotEqual(jdid._normalize_company("阿里巴巴（中国）"),
                            jdid._normalize_company("腾讯（中国）"))

    def test_region_names_count_as_qualifiers(self):
        """大区名（华东/华南/华北/华中/西南/西北/东北）与城市名同样是纯限定词。"""
        self.assertEqual(jdid._normalize_company("阿里巴巴（华东）"), "阿里巴巴")
        self.assertEqual(jdid._normalize_company("腾讯（华南）"), "腾讯")
        self.assertEqual(jdid._normalize_company("百度（西南）有限公司"), "百度")
        # 整串就是那个限定词时不能忽略（否则空段）——见下面的守卫测试
        self.assertEqual(jdid._normalize_company("（华东）"), "华东")

    def test_qualifier_ignoring_never_produces_an_empty_segment(self):
        """**空段守卫**：忽略限定词后括号外什么都不剩 → 退回不忽略。

        公司名就叫 `（北京）` 时，若照「忽略限定词」走会得到空串 → `build_id` 只好填
        `【未知单位】`，等于把用户填过的公司名丢掉。
        """
        for raw, want in [("（北京）", "北京"), ("（中国）", "中国"), ("（华东）", "华东"),
                          ("（成都）", "成都")]:
            with self.subTest(raw=raw):
                self.assertEqual(jdid._normalize_company(raw), want)
        # 整条 ID 里也必须真的是那个公司名，不是【未知单位】
        self.assertEqual(jdid.build_id(jdid.normalize_fields({"company": "（北京）"}), 1),
                         "【未知学历】_北京_【未知职责】_【未知领域】_【未知城市】_【未知薪资】_0001")

    def test_company_empty_stays_none(self):
        for raw in ("", "   ", None, 7):
            with self.subTest(raw=raw):
                self.assertIsNone(jdid._normalize_company(raw))

    def test_normalize_fields_touches_only_the_three_long_segments(self):
        got = jdid.normalize_fields({"education": "统招公办本科及以上", "company": "",
                                     "duty": "数据标注/质检", "domain": "视频",
                                     "city": "成都", "salary": "6-8K（一金7k+）"})
        self.assertEqual(got, {"education": "本科", "company": None, "duty": "数据标注/质检",
                               "domain": "视频", "city": "成都", "salary": "6-8K"})


# 6 份真实报告的字段（逐字抄自 `data/profiles/default/reports/*.html` 里的 REPORT_DATA）。
# 归一化 + build_id 出来的完整 ID 见 REAL_IDS（**手算出来的期望值，不是从实现里抄的**）。
REAL_REPORTS = [
    ("20260919_统本标注（视频标注AI训练标注师）_69分",
     {"job_title": "统本标注（视频标注/AI训练标注师）",
      "company": "四川金沙人力资源开发管理有限公司（金沙人力）",
      "job_category": "数据标注/质检", "location": "成都",
      "salary": "6-8K（入职五险，提及一金7k+）", "education": "统招公办本科及以上"}),
    ("20260919_统本标注（视频标注）_66分",
     {"job_title": "统本标注（视频标注）",
      "company": "四川金沙人力资源开发管理有限公司",
      "job_category": "数据标注/质检", "location": "成都",
      "salary": "6-8K（一金7k+）", "education": "统招公办本科及以上"}),
    ("20260919_统本标注（视频标注）_69分",
     {"job_title": "统本标注（视频标注）",
      "company": "金沙人力（四川金沙人力资源开发管理有…）",
      "job_category": "数据标注/质检", "location": "成都",
      "salary": "6-8K（入职五险，一金7k+）", "education": "统招公办本科及以上"}),
    ("20261003_语音文本评测_75分",
     {"job_title": "语音/文本评测", "company": "",
      "job_category": "数据标注/质检", "location": "北京",
      "salary": "13-26K", "education": "本科"}),
    ("20261008_语音文本评测_72分",
     {"job_title": "语音/文本评测", "company": "",
      "job_category": "数据标注/质检", "location": "北京",
      "salary": "13-26K", "education": "本科"}),
    ("示例_语音文本评测_80分",
     {"job_title": "语音/文本评测", "company": "某 AI 科技公司（示例）",
      "job_category": "数据标注/质检", "location": "北京",
      "salary": "13–26K", "education": "本科"}),
]

REAL_IDS = [
    "本科_金沙人力_数据标注质检_视频_成都_6-8K_0001",
    "本科_四川金沙人力资源开发管理_数据标注质检_视频_成都_6-8K_0002",
    "本科_金沙人力_数据标注质检_视频_成都_6-8K_0003",
    "本科_【未知单位】_数据标注质检_语音_北京_13-26K_0004",
    "本科_【未知单位】_数据标注质检_语音_北京_13-26K_0005",
    "本科_示例_数据标注质检_语音_北京_13-26K_0006",
]


class RealReportIdTest(IsolatedCase):
    """拿**全部 6 份真实报告**的字段跑派生 + 归一化 + 拼接，逐条比全文。"""

    def test_each_real_report_makes_the_expected_id(self):
        self.assertEqual(len(REAL_REPORTS), len(REAL_IDS))
        for seq, ((name, data), want) in enumerate(zip(REAL_REPORTS, REAL_IDS), 1):
            with self.subTest(report=name):
                self.assertEqual(jdid.build_id(jdid.derive_fields(data), seq), want)

    def test_every_real_id_stays_under_forty_chars(self):
        """可读性底线：ID 要当文件名、要在台账里显示。一条超标就说明归一化没做到位。"""
        for seq, ((name, data), want) in enumerate(zip(REAL_REPORTS, REAL_IDS), 1):
            with self.subTest(report=name):
                jd_id = jdid.build_id(jdid.derive_fields(data), seq)
                self.assertLessEqual(len(jd_id), 40, "ID 太长了（%d 字）：%s" % (len(jd_id), jd_id))
                self.assertEqual(len(jd_id), len(want))

    def test_no_real_id_contains_a_bracket_or_illegal_char(self):
        for seq, (name, data) in enumerate(REAL_REPORTS, 1):
            with self.subTest(report=name):
                jd_id = jdid.build_id(jdid.derive_fields(data), seq)
                for ch in "（()）" + jdid.BAD_CHARS:
                    self.assertNotIn(ch, jd_id)
                self.assertIsNone(jdid.validate_id(jd_id, []))

    def test_the_suffix_strip_changes_none_of_the_six_real_ids(self):
        """**零影响证据**：剥法定后缀前后，6 份真实报告的归一化单位与整条 ID 都逐字不变。

        真实数据里唯一没有括号的那条（`四川金沙人力资源开发管理有限公司`）剥掉 `有限公司` 后
        正好是 12 字，与「不剥直接截断到 12 字」的结果完全相同 —— 所以这条改进对真实数据
        一个字符都没动（期望值就是上一版冻结的 `REAL_IDS`）。
        """
        want_companies = ["金沙人力", "四川金沙人力资源开发管理", "金沙人力",
                          None, None, "示例"]
        for seq, ((name, data), want_id, want_co) in enumerate(
                zip(REAL_REPORTS, REAL_IDS, want_companies), 1):
            with self.subTest(report=name):
                fields = jdid.derive_fields(data)
                self.assertEqual(fields["company"], want_co)
                self.assertEqual(jdid.build_id(fields, seq), want_id)
        # 那条无括号的真实公司名：剥后缀与「不剥直接截断」得到同一个值
        self.assertEqual(jdid._strip_legal_suffix("四川金沙人力资源开发管理有限公司"),
                         "四川金沙人力资源开发管理")
        self.assertEqual("四川金沙人力资源开发管理有限公司"[:12],
                         "四川金沙人力资源开发管理")

    def test_the_two_analyses_of_the_same_real_jd_now_share_one_identity(self):
        """**这就是本块要修的根因**：`20261003` 与 `20261008` 是同一份 JD 的两次分析。

        旧方案里它们的身份是报告文件名（`…_75分` / `…_72分`），必然不同 → 知识库
        `from_jds` 越叠越多、`multi_jd` 虚高（真实数据里 6 个概念中招）。
        新方案下两份分析派生出的字段**完全相同**，只差全局编号 → 同一份 JD 一个身份。
        """
        f1 = jdid.derive_fields(REAL_REPORTS[3][1])
        f2 = jdid.derive_fields(REAL_REPORTS[4][1])
        self.assertEqual(f1, f2)
        id1 = jdid.build_id(f1, 1)
        id2 = jdid.build_id(f2, 1)
        self.assertEqual(id1, id2)


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

    def test_the_ledger_is_written_through_the_transaction(self):
        """回归钉：台账 `jd_id` 那一段必须走 `store.update_jobs`（计划 2.4）。

        改回"把第 764 行那份快照改好、第 7 步整份 `save_jobs`"就红 —— 改名窗口里挤进来的
        一次分析落盘（`upsert_job`）会被整份顶掉（报告还在盘上，台账里却看不到那次分析）。
        """
        self._fixture()
        calls = []
        real = store.update_jobs

        def watch(fn, key=None):
            calls.append(True)
            return real(fn, key)

        with mock.patch.object(store, "update_jobs", watch):
            jdid.rename_jd(self.OLD, self.NEW)

        self.assertEqual(len(calls), 1,
                         "改名没有走 `store.update_jobs`：台账又变成跨多次调用的读-改-写了")
        self.assertEqual(store.load_jobs()[0]["jd_id"], self.NEW)

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


    # ---------- P0-8：知识库**读不出来**时拒绝整次改名（判据与 `kb.repoint_jd_refs` 同一份） ----------
    def _break_knowledge_base_non_utf8(self):
        """把 `knowledge.json` 写成一个**真的非 UTF-8**（GBK，含中文）的坏文件，返回那串字节。

        ⚠️ 夹具**必须含中文**：纯 ASCII 的 GBK 与 UTF-8 逐字节相同，那就不是坏文件，
        `knowledge_safe_to_write()` 会判"读得出来" → 这条测试**假绿**（本项目 11+ 次假绿的常见形态）。
        所以下面先自证：它解不出 UTF-8、但解得出 GBK（内容真的在里面）。
        """
        raw = json.dumps({"version": 1, "nodes": [
            {"id": "yuyinshibie", "term": "语音识别",
             "from_jds": [{"id": self.OLD, "job_title": "旧岗位"}]}]},
            ensure_ascii=False).encode("gbk")
        try:
            raw.decode("utf-8")
            raise AssertionError("夹具没坏：这串字节居然能按 UTF-8 解出来")
        except UnicodeDecodeError:
            pass
        self.assertIn("语音识别", raw.decode("gbk"))          # 自证：内容确实在里面
        with open(store.knowledge_path(), "wb") as f:
            f.write(raw)
        return raw

    def test_an_unreadable_knowledge_base_refuses_the_rename_and_changes_nothing(self):
        """P0-8：知识库**有内容但读不出来** → 拒绝整次改名，别处的引用一个字节都不许动。

        老行为：`load_knowledge()` 把坏文件静默归成 `{"nodes": []}` → `kb.repoint_jd_refs` 命中 0 →
        `if kb_touched:` 为假 → **一字不写**，而 `rename_jd` 照样成功返回。
        于是"没有卡引用它"与"库读不出来"**不可区分**：jds.json / 台账 / 面试记录都改指了新 ID，
        知识库里那些卡的 `from_jds` 仍指向旧 ID —— 用户把库修好的那一刻，同一个岗位成了两种身份。
        """
        self._fixture()
        raw_kb = self._break_knowledge_base_non_utf8()
        jds_before = store.read_bytes(store.p_path("jds.json"))
        jobs_before = store.read_bytes(store.jobs_path())
        iv_before = store.read_bytes(store.interview_file("iv1", "status.json"))
        with self.assertRaises(RuntimeError) as cm:
            jdid.rename_jd(self.OLD, self.NEW)
        msg = str(cm.exception)
        self.assertIn("知识库", msg)                      # 说清坏在哪
        self.assertIn("拒绝", msg)                        # 说清这次改名没做
        # 逐字节比：不是"读回来看看"，而是四个文件全是原样
        self.assertEqual(store.read_bytes(store.p_path("jds.json")), jds_before)
        self.assertEqual(store.read_bytes(store.jobs_path()), jobs_before)
        self.assertEqual(store.read_bytes(store.interview_file("iv1", "status.json")), iv_before)
        self.assertEqual(store.read_bytes(store.knowledge_path()), raw_kb)
        # 半成品状态也不许有：旧 ID 还在、新 ID 没有凭空出现
        self.assertIsNotNone(jdid.find_jd(self.OLD))
        self.assertIsNone(jdid.find_jd(self.NEW))
        self.assertEqual(store.load_jobs()[0]["jd_id"], self.OLD)

    def test_a_wrong_shaped_knowledge_base_also_refuses(self):
        """有字节、JSON 也合法但**结构不对**（`[]`）→ 同样拒绝：里面可能正躺着那些引用。"""
        self._fixture()
        with open(store.knowledge_path(), "wb") as f:
            f.write(b"[]")
        with self.assertRaises(RuntimeError):
            jdid.rename_jd(self.OLD, self.NEW)
        self.assertEqual(jdid.load_jds()["jds"][0]["id"], self.OLD)     # 库没动

    def test_a_readable_knowledge_base_keeps_the_existing_behaviour(self):
        """对照组（P0-8）：库**读得出来** → 闸门不许改变既有行为，卡上的引用要改指新 ID。"""
        self._fixture()
        before = store.load_knowledge()["nodes"]
        self.assertEqual([x["id"] for n in before for x in n["from_jds"]], [self.OLD])
        self.assertEqual(jdid.rename_jd(self.OLD, self.NEW), self.NEW)
        frm = store.load_knowledge()["nodes"][0]["from_jds"]
        self.assertEqual([x["id"] for x in frm], [self.NEW])
        self.assertEqual(store.load_jobs()[0]["jd_id"], self.NEW)
        self.assertEqual(store.load_interview_json("iv1", "status.json")["job_id"], self.NEW)

    def test_a_missing_knowledge_base_is_not_a_reason_to_refuse(self):
        """闸门不许**误伤**：没有 knowledge.json → 照改（"读不出来 **或** 根本没有一个字节"才可写）。

        这条与上面那条是一对：判据必须是「读得出来 或 0 字节」，不是"文件不存在才算空"
        也不是"非空就拒绝" —— 少了它，把判据写成 `os.path.exists()` 之类的实现也会绿。
        """
        jdid.upsert_jd({"id": self.OLD, "seq": 1, "company": ""})
        store.upsert_job({"id": "a1", "jd_id": self.OLD, "resume": "母版",
                          "score": 75.0, "report": "a1.html"})
        self.assertFalse(os.path.exists(store.knowledge_path()))
        self.assertEqual(jdid.rename_jd(self.OLD, self.NEW), self.NEW)
        self.assertEqual(store.load_jobs()[0]["jd_id"], self.NEW)
        self.assertFalse(os.path.exists(store.knowledge_path()))    # 没卡提到它 → 一个字节都不写

    def test_a_zero_byte_knowledge_base_is_not_a_reason_to_refuse(self):
        """0 字节的 `knowledge.json` 是合法的"第一次"（没有可丢的字节）→ 照改。"""
        self._fixture()
        with open(store.knowledge_path(), "wb") as f:
            f.write(b"")
        self.assertEqual(jdid.rename_jd(self.OLD, self.NEW), self.NEW)
        self.assertEqual(jdid.load_jds()["jds"][0]["id"], self.NEW)

    def test_a_read_failure_at_snapshot_time_leaves_the_bytes_untouched(self):
        """快照时刻**读不出来**（被锁/权限）→ 整体中止，`jds.json` 一个字节都不许动。

        这是"原像按字节读"那条修复**唯一可达**的触发点：老实现 `read_text(..., None)`
        把"读失败"静默兜成 `None`，而 `None` 在回滚里表示"这个文件本来不存在" →
        回滚把 285 字节的台账写成 `{}`（2 字节）——**用户的 JD 库被清空，连备份里也没有原文**。
        （静态的"GBK jds.json"反而不可达：那会让 `load_jds` 返回空库，`rename_jd` 在
        "库里没有这个 ID"就退出，一个字节都没写。）
        """
        self._fixture()
        path = store.p_path("jds.json")
        with open(path, "rb") as f:
            raw = f.read()
        self.assertNotEqual(raw, b"{}", "夹具前提：台账本身不是空结构")
        real = store.read_bytes

        def flaky(p):
            if p == path:
                raise PermissionError(13, "拒绝访问")
            return real(p)

        with mock.patch.object(store, "read_bytes", flaky):
            with self.assertRaises(ValueError):
                jdid.rename_jd(self.OLD, self.NEW)

        with open(path, "rb") as f:
            self.assertEqual(f.read(), raw, "中止时台账必须逐字节不变")


class RenameUsesJdRefsTest(IsolatedCase):
    """批 7.1 第 3 步：`rename_jd` 的引用重指改走 `jdrefs.refs_of_jd` + `rewrite_jd_refs`。

    改名原先自己 repoint 三处（`jds.json` / 知识库 `from_jds` / 面试 `status.json` / 台账），
    于是**漏了两处**：`report_state/<报告id>.json`（按报告 id 命名，改名不需要动它，但**必须
    列出来**）与 `interviews/<面试id>/feedback.json`（反哺幂等标记，**键就是旧 JD id**）——
    后者不改的话，改完名之后那个键就指向一个不存在的岗位，下一次反哺重试会把同一次面试
    重复计进复习队列。

    这一组钉三件事：
      ① 改名后 `refs_of_jd(旧)` **为空**（悬空 0 条）、`refs_of_jd(新)` 覆盖全部六类；
      ② `feedback.json` 的键与条目里的 `jd_id` 副本都跟过去；
      ③ 读不出来的那处引用（`feedback.json` 坏掉）⇒ **拒绝整次改名**、一个字节都不动。
    """

    OLD = RenameJdTest.OLD
    NEW = RenameJdTest.NEW
    SIX = ["jds", "jobs", "kb", "iv_status", "report_state", "iv_feedback"]

    def _fixture(self):
        """在 `RenameJdTest` 的夹具上补齐**此前漏掉的两处**（报告勾选 / 反哺标记）。"""
        RenameJdTest._fixture(self)
        store.save_report_state("a1", {"要会 ASR": True})       # 报告 id = `a1.html` 去掉后缀
        store.save_interview_json("iv1", "feedback.json",
                                  {self.OLD: {"applied": True, "jd_id": self.OLD}})

    def _kinds(self, jd_id):
        return sorted(r.kind for r in jdrefs.refs_of_jd(jd_id))

    def test_after_the_rename_nothing_points_at_the_old_id(self):
        self._fixture()
        self.assertEqual(self._kinds(self.OLD), sorted(self.SIX),
                         "夹具前提：六类引用要齐（否则这条断言恒真）")
        jdid.rename_jd(self.OLD, self.NEW)
        self.assertEqual(jdrefs.refs_of_jd(self.OLD), [],
                         "改完还有东西指着旧 id（悬空引用）")
        self.assertEqual(self._kinds(self.NEW), sorted(self.SIX),
                         "新 id 没有覆盖全部引用点")
        # 逐项的真实内容：不是"扫出来对"而是"盘上那份对"
        fb = store.load_interview_json("iv1", "feedback.json")
        self.assertEqual(list(fb), [self.NEW], "feedback.json 的键还停着旧 id")
        self.assertEqual(fb[self.NEW]["jd_id"], self.NEW, "条目里那份 `jd_id` 副本没跟过去")
        self.assertTrue(fb[self.NEW]["applied"], "反哺的幂等标记丢了（会重复计进复习队列）")
        # 报告勾选文件按**报告 id** 命名 ⇒ 改名不需要动它，但它在清单里（上面六类已断言）
        self.assertTrue(os.path.isfile(store.report_state_path("a1")),
                        "报告勾选文件被顺手删/改名了")
        self.assertEqual(store.load_jobs()[0]["jd_id"], self.NEW)

    def test_an_unreadable_feedback_file_refuses_the_rename_and_changes_nothing(self):
        """**读不出来 ≠ 没有引用**：坏掉的 `feedback.json` ⇒ 拒绝整次改名（一个字节都不动）。"""
        self._fixture()
        path = store.interview_file("iv1", "feedback.json")
        with open(path, "wb") as f:                  # 真实的非 UTF-8 字节（不是 mock）
            f.write(json.dumps({"jd": "中文"}, ensure_ascii=False).encode("gbk"))
        raw = store.read_bytes(path)
        jds_before = store.read_bytes(store.p_path("jds.json"))
        jobs_before = store.read_bytes(store.jobs_path())
        with self.assertRaises(RuntimeError) as cm:
            jdid.rename_jd(self.OLD, self.NEW)
        self.assertIn("读不出来", str(cm.exception))
        self.assertIn("拒绝", str(cm.exception))
        self.assertEqual(store.read_bytes(path), raw, "坏的那份原样留着")
        self.assertEqual(store.read_bytes(store.p_path("jds.json")), jds_before)
        self.assertEqual(store.read_bytes(store.jobs_path()), jobs_before)
        self.assertEqual(store.load_jobs()[0]["jd_id"], self.OLD)

    def test_a_legacy_record_identified_by_its_own_id_follows_the_rename_too(self):
        """**迁移前形态**的记录（身份 = `id`，没有 `jd_id`）也跟到新 id。

        判据是 `jdrefs` 的 `jd_id or id`（与删除预演同一把尺子）：只认 `jd_id` 的话，
        改完名之后这条记录的 `_jd_of_record` 仍等于旧 id ⇒ `refs_of_jd(旧)` 非空（悬空 1 条）。
        """
        jdid.upsert_jd({"id": self.OLD, "seq": 1, "company": ""})
        store.save_jobs([{"id": self.OLD, "report": "legacy.html", "score": 60.0,
                          "created_at": "2026-10-01 09:00"}])
        self.assertNotIn("jd_id", store.load_jobs()[0])
        jdid.rename_jd(self.OLD, self.NEW)
        self.assertEqual(store.load_jobs()[0]["jd_id"], self.NEW)
        self.assertEqual([r.kind for r in jdrefs.refs_of_jd(self.OLD)], [],
                         "迁移前形态的记录没跟上改名（旧 id 还悬着）")


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


class RenameRollbackByteExactTest(IsolatedCase):
    """改 ID 失败回滚时，`jds.json` 必须**逐字节**还原。

    老实现用 `read_text` / `write_text` 做快照与回滚：文本模式下换行会被转换
    （Windows 写文件时 `\\n` → `\\r\\n`），于是一份 LF 的 `jds.json`
    回滚之后变成 CRLF —— **回滚改变了文件内容**，那不叫还原。
    """

    OLD = RenameJdTest.OLD
    NEW = RenameJdTest.NEW

    def _fixture(self):
        """复用 `RenameJdTest` 的夹具（不继承那个类：否则它的用例会被再跑一遍）。"""
        RenameJdTest._fixture(self)

    def test_rollback_restores_lf_jds_bytes_exactly(self):
        self._fixture()                       # RenameJdTest 既有的夹具
        path = store.p_path("jds.json")
        raw = store.read_text(path).replace("\r\n", "\n").encode("utf-8")
        with open(path, "wb") as f:
            f.write(raw)

        real_write_json = store.write_json
        iv_path = store.interview_file("iv1", "status.json")

        def flaky(p, obj, *a, **kw):
            if p == iv_path:
                raise OSError("注入的写失败")
            return real_write_json(p, obj, *a, **kw)

        with mock.patch.object(store, "write_json", flaky):
            with self.assertRaises(ValueError):
                jdid.rename_jd(self.OLD, self.NEW)

        with open(path, "rb") as f:
            self.assertEqual(f.read(), raw, "回滚必须逐字节还原（LF 不许被换成 CRLF）")


_JDS_OLD_1 = "本科_字节跳动_数据标注_文本_北京_6-8K_0001"
_JDS_OLD_2 = "本科_腾讯_语音评测_语音_深圳_13-26K_0002"
_JDS_NEW = "本科_新单位_数据质检_文本_上海_6-8K_0003"


def _gbk_jds_bytes():
    """坏 `jds.json` 的夹具字节：GBK + 中文、里面**真有** 2 条记录。

    ⚠️ 夹具**必须含中文**：纯 ASCII 的 GBK 与 UTF-8 逐字节相同 —— 那就不是坏文件，
    `jds_safe_to_write()` 会判"读得出来" ⇒ 用这条夹具的用例会**假绿**
    （本项目 11+ 次假绿的常见形态）。所以这里先自证：解不出 UTF-8、但解得出 GBK。
    """
    raw = json.dumps({"version": 1, "seq": 7, "jds": [
        {"id": _JDS_OLD_1, "company": "字节跳动", "job_title": "数据标注"},
        {"id": _JDS_OLD_2, "company": "腾讯", "job_title": "语音评测"}]},
        ensure_ascii=False).encode("gbk")
    try:
        raw.decode("utf-8")
        raise AssertionError("夹具没坏：这串字节居然能按 UTF-8 解出来")
    except UnicodeDecodeError:
        pass
    if "字节跳动" not in raw.decode("gbk"):
        raise AssertionError("夹具里没有中文内容：GBK 与 UTF-8 就没区别了")
    if len(json.loads(raw.decode("gbk"))["jds"]) != 2:
        raise AssertionError("夹具里应当有 2 条记录")
    return raw


def _write_gbk_jds():
    raw = _gbk_jds_bytes()
    path = store.p_path("jds.json")
    with open(path, "wb") as f:
        f.write(raw)
    return path, raw


class JdsWriteRefusesUnreadableLibraryTest(IsolatedCase):
    """本轮实测的**两条丢数据路**：`jds.json` 有字节却读不出来时，三个写入口都必须拒绝。

    config / jobs / knowledge 三份早就有「读不出来就别写」的判据与严格读，**jds 这一侧是
    唯一的缺口**：`load_jds()` 的契约是"绝不抛异常"（读失败 → 空库），而 `save_jds` 是
    **整份覆盖** ⇒ 一次读失败就等于整个 JD 库被内存里那份空库顶掉。
    隔离探针实测（299 字节 / 2 条记录的 GBK 库）：
      - `upsert_jd(一条新记录)` → 盘上 272 字节、**只剩新那一条**（旧 2 条被清、`.bak-` 0 份）
      - `next_seq()`           → 盘上 47 字节、`{"version":1,"seq":1,"jds":[]}`（**全清**、0 份）

    所以断言是三条一起：**逐字节未变**、**记录条数仍是 2**、**连 `.bak-` 都没多出来**。
    """

    def _assert_refused(self, call, layer):
        path, raw = _write_gbk_jds()
        folder = os.path.dirname(path)
        before = sorted(os.listdir(folder))
        with self.assertRaises(RuntimeError) as cm:
            call()
        msg = str(cm.exception)
        self.assertIn("jds.json", msg, "错误说明里要点名是哪个文件")
        self.assertIn(layer, msg, "要说清是哪一层的哪件事（%s）" % layer)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), raw, "拒绝写盘时 JD 库必须逐字节未变")
        with open(path, "rb") as f:
            self.assertEqual(len(json.loads(f.read().decode("gbk"))["jds"]), 2,
                             "不许被覆盖成「这一条 + 读出来的空库」（「没有多出半条」）")
        self.assertEqual(sorted(os.listdir(folder)), before,
                         "守卫先于留档：连 .bak- / .tmp- 都不该多出来")

    def test_upsert_jd_refuses_and_keeps_every_byte(self):
        self._assert_refused(lambda: jdid.upsert_jd({"id": _JDS_NEW, "company": "新单位"}),
                             "upsert_jd")

    def test_next_seq_refuses_and_keeps_every_byte(self):
        self._assert_refused(jdid.next_seq, "next_seq")

    def test_save_jds_refuses_and_keeps_every_byte(self):
        self._assert_refused(
            lambda: jdid.save_jds({"version": 1, "seq": 9, "jds": [{"id": _JDS_NEW}]}),
            "save_jds")

    # ---------- 守卫必须**在这一层**，不能只靠下游兜底 ----------
    def test_upsert_jd_refuses_before_the_write_step(self):
        """把写盘那一步换成哨兵：走到它就算这次 upsert 没拦住（老实现正是"读空库 → 写回"）。

        `save_jds` 自己也有守卫（与 `store.save_jobs` 同一做法：每个入口各自拦），
        但这一层的判据不能省：读进来的那份空库在语义上已经把"读不出来"当成了"库里没有"。
        """
        _write_gbk_jds()
        with mock.patch.object(jdid, "save_jds", side_effect=AssertionError("不该走到写盘")):
            with self.assertRaises(RuntimeError) as cm:
                jdid.upsert_jd({"id": _JDS_NEW})
        self.assertIn("upsert_jd", str(cm.exception))

    def test_next_seq_refuses_before_the_write_step(self):
        _write_gbk_jds()
        with mock.patch.object(jdid, "save_jds", side_effect=AssertionError("不该走到写盘")):
            with self.assertRaises(RuntimeError) as cm:
                jdid.next_seq()
        self.assertIn("next_seq", str(cm.exception))

    def test_save_jds_refuses_before_the_write_step(self):
        _write_gbk_jds()
        with mock.patch.object(store, "write_json", side_effect=AssertionError("不该走到写盘")):
            with self.assertRaises(RuntimeError) as cm:
                jdid.save_jds({"version": 1, "seq": 9, "jds": [{"id": _JDS_NEW}]})
        self.assertIn("save_jds", str(cm.exception))

    # ---------- 对照组：不存在 / 真的是 0 字节 = 合法的"第一次" ----------
    def test_a_missing_library_is_created(self):
        path = store.p_path("jds.json")
        self.assertFalse(os.path.exists(path))
        jdid.upsert_jd({"id": _JDS_OLD_1, "company": "字节跳动"})
        self.assertEqual([r["id"] for r in jdid.load_jds()["jds"]], [_JDS_OLD_1])
        self.assertEqual(glob.glob(path + ".bak-*"), [], "第一次写没有旧内容可留")

    def test_a_zero_byte_library_is_created_and_gets_no_backup(self):
        path = store.p_path("jds.json")
        with open(path, "wb") as f:
            f.write(b"")
        self.assertEqual(jdid.next_seq(), 1)
        self.assertEqual(glob.glob(path + ".bak-*"), [], "0 字节没有可留的旧内容")
        jdid.upsert_jd({"id": _JDS_OLD_1})
        self.assertEqual([r["id"] for r in jdid.load_jds()["jds"]], [_JDS_OLD_1])

    def test_save_jds_writes_a_missing_or_zero_byte_library(self):
        path = store.p_path("jds.json")
        jdid.save_jds({"version": 1, "seq": 0, "jds": []})              # 不存在
        self.assertEqual(store.read_json(path)["jds"], [])
        with open(path, "wb") as f:
            f.write(b"")
        jdid.save_jds({"version": 1, "seq": 5, "jds": [{"id": _JDS_OLD_1}]})   # 0 字节
        self.assertEqual(jdid.load_jds()["seq"], 5)
        self.assertEqual([r["id"] for r in jdid.load_jds()["jds"]], [_JDS_OLD_1])

    # ---------- 判据必须问"按 key 指定的那个路径" ----------
    def test_the_guard_asks_the_keyed_path_not_the_default_one(self):
        """两个方向都钉死：别拿默认档的库去判一个按 key 指定的文件，也别反过来误伤。"""
        raw = _gbk_jds_bytes()
        alice = store.p_path("jds.json", key="alice")
        dflt = store.p_path("jds.json")

        # 方向一：默认档坏着，alice 的库是新写的 ⇒ 写 alice 必须照旧成功
        with open(dflt, "wb") as f:
            f.write(raw)
        jdid.save_jds({"version": 1, "seq": 1, "jds": [{"id": _JDS_OLD_1}]}, key="alice")
        jdid.upsert_jd({"id": _JDS_OLD_2}, key="alice")
        self.assertEqual(len(jdid.load_jds("alice")["jds"]), 2,
                         "判据问错了路径（拿默认档的坏库挡住了 alice 的写）")

        # 方向二：默认档修好，alice 的库坏着 ⇒ 写 alice 必须拒，默认档一个字节不动
        store.write_json(dflt, {"version": 1, "seq": 0, "jds": [{"id": _JDS_OLD_1}]})
        with open(alice, "wb") as f:
            f.write(raw)
        with self.assertRaises(RuntimeError) as cm:
            jdid.upsert_jd({"id": _JDS_NEW}, key="alice")
        self.assertIn("jds.json", str(cm.exception))
        with open(alice, "rb") as f:
            self.assertEqual(f.read(), raw, "alice 的库逐字节未变")
        self.assertEqual(len(jdid.load_jds("default")["jds"]), 1, "默认档一个字节都不该动")


class SaveJdsBackupTest(IsolatedCase):
    """覆盖前留档：`.bak-<时间戳>`（内容 = **覆盖前**那一份，逐字节）+ 只留最新 3 份。

    为什么 jds.json 也要：`save_jds` 是**整份覆盖**，`upsert_jd` / `next_seq` 每次抓岗位都走它
    —— 覆盖前那一份是事后唯一能救回来的东西。轮转复用 `store.snapshot_before_overwrite`
    （因此自动拿到 `store.BACKUP_KEEP` = 3 的上限，profile 目录不会无上限长大）。
    """

    def _path(self):
        return store.p_path("jds.json")

    def test_overwriting_a_library_with_content_keeps_a_timestamped_backup(self):
        jdid.save_jds({"version": 1, "seq": 1, "jds": [{"id": _JDS_OLD_1, "company": "旧"}]})
        old = store.read_bytes(self._path())
        jdid.save_jds({"version": 1, "seq": 2, "jds": [{"id": _JDS_OLD_1, "company": "新"}]})

        backups = glob.glob(self._path() + ".bak-*")
        self.assertEqual(len(backups), 1, "覆盖有内容的 JD 库必须留下一份 .bak-<时间戳>")
        self.assertEqual(store.read_bytes(backups[0]), old, "备份必须逐字节等于覆盖前那一份")
        self.assertEqual(jdid.load_jds()["jds"][0]["company"], "新")

    def test_the_first_write_leaves_no_backup(self):
        self.assertEqual(glob.glob(self._path() + ".bak-*"), [])
        jdid.save_jds({"version": 1, "seq": 1, "jds": [{"id": _JDS_OLD_1}]})
        self.assertEqual(glob.glob(self._path() + ".bak-*"), [],
                         "第一次写没有旧内容可留，别造垃圾备份")

    def test_writing_five_times_keeps_exactly_the_three_newest(self):
        path = self._path()
        raws = []
        for i in range(1, 6):
            jdid.save_jds({"version": 1, "seq": i,
                           "jds": [{"id": _JDS_OLD_1, "company": "第%d版" % i}]})
            raws.append(store.read_bytes(path))

        baks = glob.glob(path + ".bak-*")
        self.assertEqual(len(baks), 3,
                         "连写 5 次只该留 3 份 .bak-，实际：%s"
                         % sorted(os.path.basename(b) for b in baks))
        self.assertEqual(sorted(store.read_bytes(b) for b in baks), sorted(raws[1:4]),
                         "留下的必须是**最近 3 次**覆盖前的样子（按内容断言，不是只看数量）")
        self.assertNotIn(raws[0], [store.read_bytes(b) for b in baks],
                         "最旧的那一份（第 1 次写进去的样子）必须被删掉")
        self.assertEqual(store.read_bytes(path), raws[4], "当前库仍是最后一次写进去的那份")

    def test_upsert_jd_also_leaves_a_backup(self):
        """真实路径：第一次抓岗位留不下东西；第二次（覆盖同一 ID）要留下一份覆盖前的样子。"""
        jdid.upsert_jd({"id": _JDS_OLD_1, "company": "字节"})
        self.assertEqual(glob.glob(self._path() + ".bak-*"), [])
        jdid.upsert_jd({"id": _JDS_OLD_1, "company": "字节跳动"})
        baks = glob.glob(self._path() + ".bak-*")
        self.assertEqual(len(baks), 1)
        self.assertEqual(json.loads(store.read_bytes(baks[0]).decode("utf-8"))["jds"][0]["company"],
                         "字节")


class RenameJdUnreadableLibraryTest(IsolatedCase):
    """需求二①：坏 `jds.json` 时改名**拒绝**（老行为已经拒了），但原因必须是"读不出来"。

    老行为：`load_jds()` 把坏文件静默归成空库 → 走到 `_bad("JD 库里没有这个 ID：X")` ——
    而那条记录其实**就在盘上**（只是读不出来）。用户照着"没有这个 ID"去看只会更糊涂，
    还很容易顺手"那就新建一个吧"，于是同一个岗位又多出一种身份。
    """

    def test_an_unreadable_library_refuses_and_says_unreadable(self):
        path, raw = _write_gbk_jds()
        with self.assertRaises(RuntimeError) as cm:
            jdid.rename_jd(_JDS_OLD_1, _JDS_NEW)
        msg = str(cm.exception)
        self.assertIn("jds.json", msg)
        self.assertIn("读不出来", msg, "「读不出来」不许说成「没有这个 ID」")
        self.assertNotIn("库里没有这个 ID", msg, "不许照旧报老那句「库里没有这个 ID」")
        self.assertIn("拒绝", msg)                                  # 说清这次改名没做
        with open(path, "rb") as f:
            self.assertEqual(f.read(), raw, "拒绝时 JD 库逐字节未变")
        self.assertEqual(glob.glob(path + ".bak-*"), [], "拒绝时连留档都不该有")
        self.assertFalse(os.path.exists(store.knowledge_path()), "更不该写知识库")

    def test_a_zero_byte_library_still_says_the_id_is_missing(self):
        """对照组：0 字节 = 没有可丢的字节（合法的"第一次"）⇒ 照旧 `ValueError` + "没有这个 ID"。

        闸门不许把"空库"也当成"读不出来"（那会把一次正常的校验错误说成文件坏了）。
        """
        path = store.p_path("jds.json")
        with open(path, "wb") as f:
            f.write(b"")
        with self.assertRaises(ValueError) as cm:
            jdid.rename_jd(_JDS_OLD_1, _JDS_NEW)
        self.assertIn("没有这个 ID", str(cm.exception))


class RenameJdUnreadableInterviewStatusTest(IsolatedCase):
    """需求二②：面试 `status.json` 读不出来 ≠ 它不引用这个 JD。

    老行为：`_interview_status_paths` 用 `store.read_json(path, None)`，读不出来当"没有" ⇒
    这个文件被跳过 ⇒ 改完 ID 之后它的 `job_id` **静默**留着旧 ID（与知识库那条是同一类病）。
    面试记录不像知识库那样只有一个集中入口 —— 它是**每个面试目录一个 status.json**，
    所以要扫一遍并如实上报；"读不出来"就是"不知道它指向谁"，而"不知道"不许当成"安全"。
    """

    def _readable_library(self):
        store.write_json(store.p_path("jds.json"),
                         {"version": 1, "seq": 1, "jds": [{"id": _JDS_OLD_1, "company": "字节跳动"}]})

    def _broken_status(self, interview_id, job_id):
        raw = json.dumps({"state": "done", "job_id": job_id, "title": "语音一面"},
                         ensure_ascii=False).encode("gbk")
        try:
            raw.decode("utf-8")
            raise AssertionError("夹具没坏：这串字节居然能按 UTF-8 解出来")
        except UnicodeDecodeError:
            pass
        path = store.interview_file(interview_id, "status.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(raw)
        return path, raw

    def test_an_unreadable_status_refuses_the_rename_and_changes_nothing(self):
        self._readable_library()
        store.save_interview_json("iv_ok", "status.json",
                                  {"state": "done", "job_id": _JDS_OLD_1, "title": "好的一份"})
        iv_path, raw_iv = self._broken_status("iv_bad", _JDS_OLD_1)
        jds_before = store.read_bytes(store.p_path("jds.json"))

        with self.assertRaises(RuntimeError) as cm:
            jdid.rename_jd(_JDS_OLD_1, _JDS_NEW)
        msg = str(cm.exception)
        self.assertIn("面试", msg)
        self.assertIn("读不出来", msg)
        self.assertIn("拒绝", msg)
        self.assertEqual(store.read_bytes(store.p_path("jds.json")), jds_before,
                         "整次改名没做：JD 库逐字节未变")
        with open(iv_path, "rb") as f:
            self.assertEqual(f.read(), raw_iv, "坏的那份原样留着")
        self.assertEqual(store.load_interview_json("iv_ok", "status.json")["job_id"], _JDS_OLD_1,
                         "好那份也不许先改成新 ID（半改状态）")

    def test_an_unreadable_status_refuses_even_when_it_may_not_reference_the_id(self):
        """判据是"读不出来"，不是"内容里恰好有旧 ID"—— 这里那份坏文件里写的是**别的** job_id。"""
        self._readable_library()
        iv_path, raw_iv = self._broken_status("iv_other", "别的岗位_标注_文本_上海_6-8K_0009")
        with self.assertRaises(RuntimeError) as cm:
            jdid.rename_jd(_JDS_OLD_1, _JDS_NEW)
        self.assertIn("读不出来", str(cm.exception))
        with open(iv_path, "rb") as f:
            self.assertEqual(f.read(), raw_iv)


if __name__ == "__main__":
    unittest.main()
