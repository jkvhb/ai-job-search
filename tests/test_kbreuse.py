# -*- coding: utf-8 -*-
"""阶段 E：知识库优先复用 —— 纯函数测试（匹配 / 够格 / 判定校验 / 填充）。"""
import json
import unittest

import kb
import kbreuse
import store
from tests.base import IsolatedCase


class KeysTest(IsolatedCase):
    def test_full_name_is_normalised_with_the_kb_rule(self):
        """键必须沿用 `kb.normalize_id` 的口径，**不许另造一套**。"""
        self.assertEqual(kbreuse._keys_of_term("词错率（WER）")[0], "词错率wer")
        self.assertEqual(kbreuse._keys_of_term(" ASR ")[0], "asr")

    def test_ascii_parenthetical_becomes_an_extra_key(self):
        self.assertIn("vad", kbreuse._keys_of_term("语音端点检测（VAD）"))

    def test_chinese_parenthetical_does_not_become_a_key(self):
        """**中文括号不建键** —— 它是解释性补充，当别名会误命中。

        反例：`Case（案例）沉淀` 若能贡献出 `案例`，任何叫「案例」的节点都会错认成它。

        ⚠️ **单汉字与多汉字都要有**，因为它们挡的是**两种不同的变异**：
        - **单汉字** `（案）`：挡"只把首字符放宽成 `.`"那种窄变异 ——
          多汉字时那种变异会因后面的字符类仍要求 ASCII 而**整个匹配回退失败**，
          于是测试照样绿（第一版就是这么写的，**没有牙**）。
        - **多汉字** `（案例）`：挡"把整条 ASCII 限制删掉"（如 `[^)）]+?`）那种宽变异。
        只写一种，另一种变异就能悄悄溜过去。
        """
        self.assertNotIn("案例", kbreuse._keys_of_term("Case（案例）沉淀"))
        self.assertNotIn("案", kbreuse._keys_of_term("Case（案）沉淀"))

    def test_a_single_character_parenthetical_is_not_a_key(self):
        """**单字符 / 纯数字的括号内容不算缩写** —— 它会制造泛化键，让两个不相干的概念互相认错。

        反例（实测）：`选项（A）` 与 `分组（a）` 会共享键 `a`；
        而卡片 `{"term": "A"}` 的 id 恰好就是 `a` —— 节点 `选项（A）` 会把 `A` 的内容
        悄悄塞进报告里（spec §2 明令禁止的失败形态：认错且看不出来）。
        """
        self.assertEqual(kbreuse._keys_of_term("选项（A）"), ["选项a"])
        self.assertEqual(kbreuse._keys_of_term("分组（a）"), ["分组a"])
        self.assertNotIn("a", kbreuse._keys_of_term("选项（A）"))
        self.assertEqual(kbreuse._keys_of_term("第（2）轮"), ["第2轮"])     # 纯数字也不建
        # 够格的缩写一个都不能少
        self.assertIn("wer", kbreuse._keys_of_term("词错率（WER）"))
        self.assertIn("vad", kbreuse._keys_of_term("语音端点检测（VAD）"))
        self.assertIn("3d", kbreuse._keys_of_term("三维重建（3D）"))

    def test_a_parenthetical_key_uses_the_kb_normalisation_rule(self):
        """**括号侧**的键也必须走 `kb.normalize_id`（NFKC + 去标点空白 + 小写）。

        自造一套口径（比如只 `.lower()`）会把 `评测（a.b）` 建成 `a.b`，
        而库里卡片 id 是 `ab` —— 报告的节点与卡片**对不上**（spec §5）。
        「够格才算缩写」那条只管**长度与字母**，不替代归一化：`（a.b）` 归一化后是
        `ab`，长度 2 且有字母，照样建键。
        """
        keys = kbreuse._keys_of_term("评测（a.b）")
        self.assertIn("ab", keys)
        self.assertNotIn("a.b", keys)

    def test_nested_parentheticals_only_keep_the_inner_ascii_run(self):
        """嵌套括号的**真实行为**（钉住形状，不钉理想）。

        `术语（WER（x））`：外层从 `（` 到 `WER` 就撞上中文括号 `（`，**整段**过不了
        ASCII 闸门 → 外层 `wer` 丢；内层 `（x）` 单独匹配上。够格才算缩写之后，
        单字符 `x` 也被拒 → 只剩全名键。方向安全：**少命中，不错命中**。
        """
        self.assertEqual(kbreuse._keys_of_term("术语（WER（x））"), ["术语werx"])

    def test_a_chinese_only_parenthetical_is_not_a_key(self):
        """括号内容**整段都是中文**时不建键（判据在括号内容上，与括号本身无关）。

        ⚠️ 别读成「中文括号里的英文缩写也不建键」—— 恰恰相反：
        `语音端点检测（VAD）` 用的就是**中文括号**，它**要**建 `vad` 键（见上上条）。
        这条挡的是"括号内容本身是中文解释"那类：`响应延迟（首字延迟）` 若能贡献出
        `首字延迟`，任何叫「首字延迟」的节点都会错认成它。

        同样单/多字各一，理由见上一条。
        """
        self.assertNotIn("首字延迟", kbreuse._keys_of_term("响应延迟（首字延迟）"))
        self.assertNotIn("首", kbreuse._keys_of_term("响应延迟（首）"))

    def test_keys_are_deduplicated_and_keep_order(self):
        """去重与保序。

        ⚠️ **两级去重，别混为一谈**：
        - `_keys_of_term` 去重**"全名键"与"括号键"之间**的重复 ——
          它只在**术语本身就是那个括号**时可达：`（WER）` 的 `normalize_id` 正好也是 `wer`，
          于是第二把键会被拦下。**下面第一条断言就是给这个分支补牙的**（去掉它那层去重
          就会得到 `["wer", "wer"]`）。
        - `_paren_keys` 自己也去重（`（WER）（WER）` → 只剩一把 `wer`），但它对**当前唯一的
          调用方 `_keys_of_term` 不产生行为差异**（后者会再去重），所以下面这些断言
          **并没有**钉住它 —— 它只是让返回值自身干净，将来的 `Index.by_key.setdefault`
          也不在乎重复。
        """
        self.assertEqual(kbreuse._keys_of_term("（WER）"), ["wer"])
        self.assertEqual(kbreuse._keys_of_term("（ASR）"), ["asr"])
        self.assertEqual(kbreuse._keys_of_term("AB（WER）CD（WER）"),
                         ["abwercdwer", "wer"])


class CompletenessTest(IsolatedCase):
    def _card(self, **kw):
        c = {"id": "x", "term": "X", "definition": "定义", "plain_explanation": "通俗",
             "sources": [{"url": "https://a"}], "timeline": [{"year": "2020", "text": "t"}]}
        c.update(kw)
        return c

    def test_a_full_card_is_reusable(self):
        reusable, missing = kbreuse._card_completeness(self._card())
        self.assertTrue(reusable)
        self.assertEqual(missing, [])

    def test_sources_are_required(self):
        reusable, missing = kbreuse._card_completeness(self._card(sources=[]))
        self.assertFalse(reusable, "没有真实来源链接就不算够格（spec §1 决策 2）")
        self.assertIn("sources", missing)

    def test_sources_must_be_a_list_holding_a_dict_with_a_non_empty_url(self):
        """够格要的是「**列表里至少一个 dict，且它的 `url` 非空**」，**不是 truthy**。

        实测（修前）：`[{}]` / `[None]` / `"abc"` / `{"url": "u"}` **全部**被判成够格 ——
        可它们一条能点开的链接都没有；`{"url": "u"}` 甚至连列表都不是（形状错）。
        spec §1 决策 2 要的是「≥1 条**真实来源链接**」。
        """
        for bad in ([{}], [None], "abc", {"url": "u"},
                    [{"x": 1}], [{"url": ""}], [{"url": "   "}], [None, {}]):
            reusable, missing = kbreuse._card_completeness(self._card(sources=bad))
            self.assertFalse(reusable, "形状不对的来源不算：%r" % (bad,))
            self.assertIn("sources", missing, "形状不对的来源不算：%r" % (bad,))
        reusable, missing = kbreuse._card_completeness(
            self._card(sources=[{"url": "https://w"}]))
        self.assertTrue(reusable, "带真 url 的列表仍然够格")
        self.assertNotIn("sources", missing)

    def test_definition_and_plain_explanation_are_required(self):
        for key in ("definition", "plain_explanation"):
            reusable, missing = kbreuse._card_completeness(self._card(**{key: "  "}))
            self.assertFalse(reusable, key)
            self.assertIn(key, missing)

    def test_a_missing_timeline_does_not_disqualify_but_is_listed(self):
        """`timeline` **不参与够格判定**（缺了照样复用，只是要补算）。"""
        reusable, missing = kbreuse._card_completeness(self._card(timeline=[]))
        self.assertTrue(reusable, "缺时间线不该让卡片失去复用资格")
        self.assertIn("timeline", missing)
