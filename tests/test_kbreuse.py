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

    def test_a_chinese_parenthetical_is_not_a_key_even_with_latin_inside(self):
        """中文括号里即便是英文缩写，也**不建键**（只认 ASCII 开头的括号内容）。

        同样单/多字各一，理由见上一条。
        """
        self.assertNotIn("首字延迟", kbreuse._keys_of_term("响应延迟（首字延迟）"))
        self.assertNotIn("首", kbreuse._keys_of_term("响应延迟（首）"))

    def test_keys_are_deduplicated_and_keep_order(self):
        """同一把键重复出现时只留一个（保序）。

        ⚠️ **别拿 `WER（WER）` 当例子**：`kb.normalize_id("WER（WER）")` 是 `"werwer"`
        （括号只是被去掉，不是被替换），所以 `_keys_of_term` 会给 `["werwer", "wer"]` ——
        那不是"去重"。真正能验证去重的是"**同一个括号缩写出现两次**"。
        """
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
