# -*- coding: utf-8 -*-
"""阶段 E：知识库优先复用 —— 测试（匹配 / 够格 / 判定校验 / 填充 / 写回安全的强不变量）。"""
import copy
import json
import os
import threading
import unittest
from unittest import mock

import kb
import kbreuse
import log
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

        **构造复现（预防的由来，不是线上发生过的事）**：`选项（A）` 与 `分组（a）` 会共享键
        `a`；而卡片 `{"term": "A"}` 的 id 恰好就是 `a` —— 节点 `选项（A）` 会把 `A` 的内容
        悄悄塞进报告里（spec §2 明令禁止的失败形态：认错且看不出来）。
        真库此刻没有 1 字符卡 id、`by_key` 也没有 len<2 的键（只读扫描过），
        所以这条闸门是**预防性**的：它挡的是将来（用户手改 / `merge_nodes` 写入）那种形状。
        """
        self.assertEqual(kbreuse._keys_of_term("选项（A）"), ["选项a"])
        self.assertEqual(kbreuse._keys_of_term("分组（a）"), ["分组a"])
        self.assertNotIn("a", kbreuse._keys_of_term("选项（A）"))
        self.assertEqual(kbreuse._keys_of_term("第（2）轮"), ["第2轮"])     # 纯数字也不建
        # **两位数字也必须拒**：`第（2）轮` 的括号内容归一后是单字符 `2`，它**先**被长度闸门
        # 挡下（`len(k) >= 2` 不成立）—— 于是"纯数字被拒"这件事根本没被测到。
        # `第（12）轮` 归一后是 `12`（长度 2），它只能被**字母闸门**拦下：
        # 去掉字母闸门，`12` 就会变成键，任何含 `12` 的概念都会互相认错（spec §2 禁止的失败形态）。
        self.assertEqual(kbreuse._keys_of_term("第（12）轮"), ["第12轮"])
        self.assertNotIn("12", kbreuse._keys_of_term("第（12）轮"))
        self.assertEqual(kbreuse._keys_of_term("第（123）轮"), ["第123轮"])
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


class IndexTest(IsolatedCase):
    def test_lookup_by_exact_normalised_name(self):
        idx = kbreuse.Index([{"id": "语音识别", "term": "语音识别", "definition": "d"}])
        card, how = idx.lookup(" 语音识别 ")
        self.assertIsNotNone(card)
        self.assertEqual(how, "exact")

    def test_lookup_by_ascii_parenthetical_both_directions(self):
        """库里叫 `词错率（WER）`、节点叫 `WER` → 命中；反之亦然（spec §5）。"""
        idx = kbreuse.Index([{"id": "词错率wer", "term": "词错率（WER）", "definition": "d"}])
        card, how = idx.lookup("WER")
        self.assertIsNotNone(card, "库里那份的 ASCII 括号要能当别名被查到")
        self.assertEqual(how, "abbrev")

        idx2 = kbreuse.Index([{"id": "asr", "term": "ASR", "definition": "d"}])
        card2, how2 = idx2.lookup("语音识别（ASR）")
        self.assertIsNotNone(card2, "节点自己的 ASCII 括号也要能查到库里的项")
        self.assertEqual(how2, "abbrev")

    def test_lookup_by_recorded_alias(self):
        idx = kbreuse.Index([{"id": "语音识别", "term": "语音识别",
                              "aliases": ["ASR"], "definition": "d"}])
        card, how = idx.lookup("asr")
        self.assertIsNotNone(card)
        self.assertEqual(how, "alias")

    def test_card_side_alias_key_is_reported_as_alias_not_abbrev(self):
        """**卡侧 `aliases` 命中的键归 `alias`，卡侧括号贡献的键才归 `abbrev`**（M-3）。

        节点 `语音识别（ASR）` 查库里的卡 `{term: 语音识别, aliases: ["ASR"]}`：
        两边都"带 ASR"，但**那把键的真实来源是卡侧的 `aliases`** —— `alias` 这一档
        （spec §5 里明确的来源区分：括号缩写 vs 卡片记录过的别名）不能被 `abbrev` 遮蔽，
        否则报告无法如实说明"我是靠哪条线索认出来的"。

        为什么这不是"键归属随便挑一个"：`abbrev` 的判据是**卡侧 term 里的 ASCII 括号**，
        这张卡的 term `语音识别` 里一个括号都没有 —— 报 `abbrev` 就是**编造**了一个来源。
        """
        idx = kbreuse.Index([{"id": "语音识别", "term": "语音识别",
                              "aliases": ["ASR"], "definition": "d"}])
        card, how = idx.lookup("语音识别（ASR）")
        self.assertIsNotNone(card)
        self.assertEqual(how, "alias", "键来自卡侧 aliases，不是卡侧括号")

    def test_lookup_by_the_head_name_without_parentheticals(self):
        """**去掉括号内容后的全名**也要能查到（M-4，直接提高复用率的那个）。

        真库 8 张带括号的卡里 **7 张用"中文头名"查不到**（`词错率` / `平均意见分` /
        `打断` / `评测标准` / `字错误率` / `声学回声消除` / `语音端点检测` 全 miss），
        后果是 `absorb` 认不出该合并，**新建重复卡**。

        中文括号那类（`响应延迟（首字延迟）`）也要能中 —— 它不贡献 ASCII 缩写键
        （括号内容是中文，见上），但**头名键**照样建。

        ⚠️ Minor④：档位是 **`head`**，不是 `abbrev` —— 节点 `词错率` 里**一个括号都没有**，
        报 `abbrev` 时报告会说「括号缩写命中」（用户看不出这条是怎么认出来的）。
        下面同时钉住**另一侧**：靠卡侧**括号内容**命中的仍然是 `abbrev`（两个档位不许混）。
        """
        idx = kbreuse.Index([{"id": "词错率wer", "term": "词错率（WER）", "definition": "d"}])
        card, how = idx.lookup("词错率")
        self.assertIsNotNone(card, "中文头名要能查到带括号的那张卡")
        self.assertEqual(card["id"], "词错率wer")
        self.assertEqual(how, "head", "去括号的头名命中要报 head（不是 abbrev）")
        # 反向：真的走**括号缩写键**（节点叫 `WER`）的仍然是 abbrev —— 档位没被一起改坏
        card_a, how_a = idx.lookup("WER")
        self.assertIsNotNone(card_a, "夹具前提：括号缩写也要能中")
        self.assertEqual((card_a["id"], how_a), ("词错率wer", "abbrev"))

        idx2 = kbreuse.Index([{"id": "响应延迟首字延迟", "term": "响应延迟（首字延迟）",
                               "definition": "d"}])
        card2, how2 = idx2.lookup("响应延迟")
        self.assertIsNotNone(card2, "中文括号那类也要建头名键")
        self.assertEqual(card2["id"], "响应延迟首字延迟")
        self.assertEqual(how2, "head")

    def test_an_exact_name_always_beats_an_earlier_derived_key(self):
        """**精确名优先于派生键** —— 与"谁先落盘"无关（I-1）。

        反例（复查实测）：`[词错率（WER）, WER]` 查 `"WER"` 会返回「词错率（WER）」/abbrev
        —— 那把 `wer` 键是**更早那张卡的括号**贡献的，`setdefault` 让先落盘的那张赢；
        倒序才回 `WER`/exact。

        真库此刻已有 **8 把键**（`wer` / `vad` / `bargein` / `rubric` / `mos` / `kappa` /
        `cer` / `aec`）被派生键占住 —— 以后入库一张 term 就叫 `MOS`/`VAD`/`CER` 的卡
        （本领域极常见）就会被**静默遮蔽**：查 `MOS` 拿到的是别人，且看不出来。
        """
        a = {"id": "甲", "term": "词错率（WER）", "definition": "d"}
        b = {"id": "乙", "term": "WER", "definition": "d"}
        for cards in ([a, b], [b, a]):
            card, how = kbreuse.Index(cards).lookup("WER")
            self.assertEqual(card["id"], "乙", "WER 就是那张卡的精确名")
            self.assertEqual(how, "exact")

    def test_aliases_that_are_not_a_list_never_throw_and_never_shorten_keys(self):
        """`aliases` 必须收口（I-2）—— 本函数的存在意义是"失败要降级、**绝不抛**"。

        两种真实坏法：
        - `{"aliases": 5}` / `True` / `{"a": 1}`：直接 `for a in (... or [])` 迭代
          → 数字/布尔**抛 `TypeError`**（`bool` 不是 iterable），dict 则迭代出**键**；
        - `{"aliases": "ASR语音标注"}`：字符串被**逐字符**拆键 → 单字符泛化键
          （`a` / `s` / `r` / `语` …）从**别名这道门**重现 —— Task 2b 刚在括号侧堵掉它。
          下游后果与括号侧一模一样：任意含 `a` 的概念互相认错（spec §2 禁止的失败形态）。

        生产可达：`kb.absorb({'term': 'A'})` + `kb.merge_nodes` 真能把 `['A']` 写进 `aliases`。
        """
        for bad in (5, True, "ASR语音标注", {"a": 1}, None, ["ASR"]):
            idx = kbreuse.Index([{"id": "语音识别", "term": "语音识别",
                                  "aliases": bad, "definition": "d"}])
            self.assertEqual(len(idx), 1)
            self.assertTrue(all(len(k) >= 2 for k in idx.by_key),
                            "别名侧不许出现单字符泛化键：%r" % (bad,))
        # 正常的列表别名照样建键（收口不能把好路径一起收掉）
        idx = kbreuse.Index([{"id": "语音识别", "term": "语音识别",
                              "aliases": ["ASR", "语音转文字"], "definition": "d"}])
        self.assertEqual(idx.lookup("asr")[1], "alias")
        self.assertEqual(idx.lookup("语音转文字")[1], "alias")

    def test_no_short_key_ever_lands_in_by_key(self):
        """不变量：`by_key` 里**没有任何 len < 2 的键**（构造库）。

        构造库里故意放最坏的形状：单字符 term 的卡（`{"term": "A"}` 的 id 就是 `a`）、
        单字符别名。精确名可以短（它在第一层 `cards` 里，是**用户自己的卡**，短名无害 ——
        查 `a` 只会查到那张卡自己），但**派生键**一旦短了就是泛化键。
        """
        idx = kbreuse.Index([
            {"id": "a", "term": "A", "definition": "d"},
            {"id": "语音识别", "term": "语音识别", "aliases": ["A", "x", "ASR"], "definition": "d"},
            {"id": "词错率wer", "term": "词错率（WER）", "definition": "d"},
            {"id": "响应延迟首字延迟", "term": "响应延迟（首字延迟）", "definition": "d"},
        ])
        self.assertEqual([k for k in idx.by_key if len(k) < 2], [])
        self.assertIn("a", idx.cards, "精确名（哪怕 1 个字符）仍在第一层")

    def test_lookup_misses_cleanly(self):
        idx = kbreuse.Index([{"id": "语音识别", "term": "语音识别", "definition": "d"}])
        self.assertEqual(idx.lookup("完全不相干的概念"), (None, ""))

    def test_a_card_without_a_term_is_skipped(self):
        idx = kbreuse.Index([{"id": "", "term": "", "definition": "d"},
                             {"id": "a", "term": "A", "definition": "d"}])
        self.assertEqual(len(idx), 1, "没有名字的卡片进不了目录")

    def test_non_dict_cards_are_skipped_instead_of_exploding(self):
        """知识库文件是用户可手改的：`nodes` 里混进字符串/数字/None 时不许把整次分析炸掉。"""
        idx = kbreuse.Index(["不是卡片", 5, None, [], {"id": "a", "term": "A", "definition": "d"}])
        self.assertEqual(len(idx), 1)
        self.assertEqual(idx.cards["a"]["id"], "a")

    def test_a_contested_key_keeps_the_first_card(self):
        """**同 id 去重**：两张同 term 的卡只进一张，第一张胜出（M-5 补的真牙）。

        ⚠️ 这条以前写着"同一把键被两张卡占用时先到先得" —— **那句是假的**：
        两张卡 term 相同 → `normalize_id(term)` 是同一把 id，第二张压根没机会争键。
        复查用变异证明过：删掉 `cid in self.cards` 那道去重，全量仍绿（`Ran 951 tests ... OK`），
        因为 `self.cards[cid]` 用**同一个 key** 覆盖，语义上仍是第一张的键值，
        `lookup` 拿到的还能是第二张卡（下面的 `winner` 断言就是钉这个的）。
        真正钉"先到先得"的是下一条 `..._between_two_different_terms_...`。
        """
        idx = kbreuse.Index([{"id": "first", "term": "同名", "definition": "d"},
                             {"id": "second", "term": "同名", "definition": "d"}])
        self.assertEqual(len(idx), 1, "同一个 id（同名归一后同键）只能进一张")
        card, _ = idx.lookup("同名")
        self.assertEqual(card["id"], "first")
        self.assertEqual(idx.cards[kb.normalize_id("同名")]["id"], "first",
                         "目录里留下的必须是**第一张**那张卡（不是第二个同 id 的对象）")

    def test_a_contested_key_between_two_different_terms_keeps_the_first_card(self):
        """**为什么需要两条测试**：「先到先得」这条规则要**两条**才钉得住，缺一条就会假绿。

        - `test_a_contested_key_keeps_the_first_card` 用的是**同一个 term**（两张卡都叫「同名」）：
          它们的 `normalize_id(term)` 是同一把 id，第二张卡在"同一个 id 只进一次"那一步
          （`if not cid or cid in self.cards: continue`）就被丢掉了，**根本没争到键** ——
          它钉的其实是**「同 id 去重」**，不是「先到先得」。实测：把
          `by_key.setdefault(k, c)` 改成 `by_key[k] = c`，全量仍是 `Ran 951 tests ... OK`，
          它照样绿（**它当时是因为另一个原因绿的**）。
        - **这条**钉的才是「先到先得」：两个**不同** term、**同一把派生键**、
          且**第二张卡的名字不是那把键**。
          卡 A 的 `词错率（WER）` 与卡 B 的 `字错误率（WER）` 都贡献出 `wer`；两张卡的 id
          不同（`词错率wer` / `字错误率wer`）→ 都进目录，`wer` 这把派生键同时被两张卡占着。
          这时行为才是**可观测**的：改成 `[k] = c` 之后卡 B 会顶掉卡 A，
          `by_key["wer"]` 与 `lookup("WER")` 都从 `first`/`abbrev` 变成 `second`/`abbrev`（实测红）。

        ⚠️ **本条的构造在 I-1 之后必须换过**：原来的第二张卡 term 就叫 `WER`，于是它既是
        "派生键的争夺者"、又是"精确名"。I-1 要求**精确名永远赢**，`lookup("WER")` 因此
        从 `first`/`abbrev` 变成 `second`/`exact` —— 那是**故意**的（见
        `test_an_exact_name_always_beats_an_earlier_derived_key`），但那样一来这条测试
        钉的就不再是"层内先到先得"，而变成"跨层优先级"了（结论相反，会让测试自相矛盾）。
        把第二张卡换成同样带 `（WER）` 的另一张卡，冲突就**只在第二层内部**：
        两层规则各测各的，互不遮蔽。
        """
        idx = kbreuse.Index([{"id": "first", "term": "词错率（WER）", "definition": "d"},
                             {"id": "second", "term": "字错误率（WER）", "definition": "d"}])
        self.assertEqual(len(idx.cards), 2, "两张卡 term 不同，都该进目录")
        self.assertEqual(idx.by_key["wer"]["id"], "first", "wer 这把派生键被两张卡占着")
        card, how = idx.lookup("WER")
        self.assertEqual(card["id"], "first", "同一把**派生**键被两张卡占用时先到先得")
        self.assertEqual(how, "abbrev")

    def test_index_from_store_reads_the_real_knowledge_file(self):
        store.save_knowledge({"nodes": [{"id": "语音识别", "term": "语音识别",
                                         "definition": "d", "plain_explanation": "p",
                                         "sources": [{"url": "u"}]}]})
        idx, err = kbreuse.build_index()
        self.assertEqual(err, "")
        self.assertEqual(len(idx), 1)

    def test_index_from_a_corrupt_store_reports_instead_of_pretending_empty(self):
        with open(store.knowledge_path(), "wb") as f:
            f.write(b'{"nodes": [ <<<CORRUPT>>>')
        idx, err = kbreuse.build_index()
        self.assertEqual(len(idx), 0)
        self.assertTrue(err, "坏知识库必须给出原因（报告要如实标 ⚠）")

    def test_explicit_cards_never_touch_the_disk(self):
        """`build_index(cards=[...])` 走**显式传参**那条路：`err==""`、**一次盘都不读**。

        为什么这条要有牙：磁盘上放一份**坏** knowledge.json。显式传参若偷偷去读盘，
        要么读出来的东西顶掉传进来的卡片（本次分析静默换了知识库），
        要么把坏文件的 err 带回来（报告凭空多一个 ⚠）。两种都是"看起来没事"的错。
        顺手钉住"只读"：这条路上知识库文件字节**一字不动**。
        """
        corrupt = b'{"nodes": [ <<<CORRUPT>>>'
        with open(store.knowledge_path(), "wb") as f:
            f.write(corrupt)
        path = store.knowledge_path()
        with open(path, "rb") as f:
            before = f.read()

        cards = [{"id": "语音识别", "term": "语音识别", "definition": "d"}]
        idx, err = kbreuse.build_index(cards=cards)
        self.assertEqual(err, "", "显式传参这条路不该碰盘，也就带不回读盘错误")
        self.assertEqual(len(idx), 1)
        self.assertEqual(idx.lookup("语音识别")[0]["id"], "语音识别")

        with open(path, "rb") as f:
            after = f.read()
        self.assertEqual(before, after, "只读：知识库文件字节不许变")

    def test_explicit_empty_list_is_not_the_same_as_reading_the_disk(self):
        """`cards=[]` 是**显式空库**（"这次没有可复用的"），不是"去读盘"。

        若把 `if cards is not None` 写成 `if cards`，空列表会掉进读盘分支 ——
        于是"显式空库"会变成"读真库"，调用方（测试/离线复用）拿到的东西完全不同。
        """
        store.save_knowledge({"nodes": [{"id": "语音识别", "term": "语音识别", "definition": "d"}]})
        idx, err = kbreuse.build_index(cards=[])
        self.assertEqual(err, "")
        self.assertEqual(len(idx), 0, "显式空列表就是空目录")


class BuildIndexLockTest(IsolatedCase):
    """批 2 · 2.3（体检 §5 P3-2）：`build_index` 是**新加的无锁读者**。

    为什么它不是"只读所以无所谓"：`kb.absorb` 持 `kb._KB_LOCK` **整份重写** knowledge.json
    （`store.write_json` → 写临时文件 → `os.replace`）。Windows 上 `os.replace` 撞上
    别人打开着的同名文件会抛 `WinError 5`（CPython 的 `open` 不带 `FILE_SHARE_DELETE`），
    而 `kb.absorb` 只在边界记一条日志 ⇒ **用户刚沉淀的卡静默丢失**（本项目最恨的假话）。

    选 **(a) 读取时持 `kb._KB_LOCK`**，理由见 `kbreuse.build_index` 的 docstring。
    """

    def test_reading_the_knowledge_base_holds_the_kb_lock_and_releases_it(self):
        """读盘必须**在 `kb._KB_LOCK` 里**发生（判据与 `test_jdid.py` 那条同源：`_is_owned()`）。

        ⚠️ 断言的是"**读盘那一刻**"锁被持着，而不是"函数外面碰过锁"：变异成
        `with kb._KB_LOCK: pass` 之后再去读盘，这条照样红（`seen` 里是 `False`）。
        """
        store.save_knowledge({"nodes": [{"id": "语音识别", "term": "语音识别", "definition": "d"}]})
        seen = []
        real = store.read_knowledge_strict

        def spy(*a, **kw):
            seen.append(kb._KB_LOCK._is_owned())      # noqa: SLF001 —— 与 test_jdid 同一判据
            return real(*a, **kw)

        with mock.patch.object(store, "read_knowledge_strict", spy):
            idx, err = kbreuse.build_index()
        self.assertEqual(err, "")
        self.assertEqual(len(idx), 1)
        self.assertEqual(seen, [True],
                         "读 knowledge.json 时没有持 `kb._KB_LOCK`：写者的 `os.replace` 会与它"
                         "并发，Windows 上抛 WinError 5，用户的沉淀静默丢失")
        self.assertFalse(kb._KB_LOCK._is_owned(), "只读临界区必须退出去，别把锁留在手里")  # noqa: SLF001

    def test_explicit_cards_never_take_the_lock(self):
        """`cards=[...]` 那条路**不碰盘、也不碰锁**：它不该为一个内存对象去挡住写者。

        （"不读盘"由 `test_explicit_cards_never_touch_the_disk` 钉着；这条钉"不取锁"。）
        """
        seen = []
        real = store.read_knowledge_strict
        with mock.patch.object(store, "read_knowledge_strict",
                               lambda *a, **kw: (seen.append(1), real(*a, **kw))[1]):
            idx, err = kbreuse.build_index(cards=[{"id": "a", "term": "A", "definition": "d"}])
        self.assertEqual((err, len(idx), seen), ("", 1, []))

    def test_an_unreadable_knowledge_base_is_reported_and_never_pretends_to_be_empty(self):
        """「读不出来」**不许**当成「空库」 —— 那是本项目最恨的假话。

        变异取证（本批第 2 条）：把 `build_index` 里那句 `return Index([]), err` 改成
        `return Index([]), ""`（"读不出来"当成"没有"），**这条必须红**。它逐条钉住四条出口：
          ① 返回的原因非空（调用方要把它写进报告的 ⚠）；
          ② 原因里点得出是哪个文件（用户照着这句话才知道去修哪个）；
          ③ 落一条 `kbreuse.kb_unreadable` 的 warn 日志（事后查得到）；
          ④ 经 `Reuse.plan` 之后 `stats["knowledge_error"]` 仍带着那句话 —— 报告里那句
             「库读不出来」的**唯一**来源就是它；
        另外钉住"只读"：坏文件一个字节都不许动（读者绝不隔离/改名/写盘）。
        """
        store.ensure_profile()
        path = store.knowledge_path()
        with open(path, "wb") as f:
            f.write(b'{"nodes": [ <<<CORRUPT>>>')
        before = store.read_bytes(path)

        idx, err = kbreuse.build_index()               # cards=None → 真的去读盘
        self.assertEqual(len(idx), 0, "坏库拿不出可复用的卡（这次全部走联网）")
        self.assertTrue(err, "读不出来必须如实给出原因，**不许**静默成空库")
        self.assertIn("knowledge.json", err, "原因要点出文件，否则用户不知道去修哪个")

        evs = [r for r in log.read_logs(200)[0] if r.get("event") == "kbreuse.kb_unreadable"]
        self.assertEqual(len(evs), 1, "库读不出来必须留一条日志：%r" % (evs,))
        self.assertEqual(str(evs[0].get("level") or "").lower(), "warn")

        stats = kbreuse.Reuse({}).plan([])             # 真跑一次"库优先"
        self.assertEqual(stats["knowledge_error"], err, "原因必须一路带到报告统计里")
        self.assertEqual(store.read_bytes(path), before, "只读：坏文件一个字节都不许动")

    # ---- 并发（这一条才是"不再丢数据"的牙）----
    #: 种进库里的卡数。**库文件够大是关键**：读一次要在几毫秒里一直握着那个文件句柄，
    #: 无锁读者才有可命中的窗口（小库的窗口窄到几乎撞不上，测试会变成假绿）。
    SEED_CARDS = 150
    ROUNDS = 40

    def _seed_a_big_kb(self):
        nodes = [{"id": "种子%04d" % i, "term": "种子概念%04d" % i,
                  "definition": "定义" * 100, "plain_explanation": "解释" * 100,
                  "sources": [{"url": "https://example.invalid/%d" % i}]}
                 for i in range(self.SEED_CARDS)]
        store.save_knowledge({"version": kb.KB_VERSION, "nodes": nodes})
        size = os.path.getsize(store.knowledge_path())
        self.assertGreater(size, 100000, "夹具前提：库文件太小，读窗口窄到测不出竞态（%d 字节）" % size)

    def test_a_concurrent_absorb_never_loses_a_card_and_never_raises(self):
        """一个线程反复 `kb.absorb`、另一个线程反复 `build_index` ⇒ **零异常 + 零丢失**。

        交错用 `threading.Event` 真制造（不是 `sleep` 赌）：两个线程都卡在同一个发令 `Event`
        上，一放行就同时开跑；写者跑完 N 轮后置 `done`，读者**自由循环**到那一刻为止。

        ⚠️ 读者**故意不逐轮对齐**（不用 `Barrier`）—— 这不是风格问题，是**这条测试有没有牙**的问题。
        `Barrier` 会把两条线程的相位锁死（每轮都从同一时刻起步、跑同样的工作量），于是写者的
        `os.replace` 每一轮都落在读窗口**之外**。**实测**（探针：与这里完全同构，只是把读者换成
        无锁替身，`SEED_CARDS=150` / `ROUNDS=40`）：

            无锁 + Event    errors=1（writer:PermissionError(13, '拒绝访问。')） missing=40  ← 旧实现红
            无锁 + Barrier  errors=0                                      missing=0   ← 旧实现**绿**！
            持锁 + Event    errors=0                                      missing=0

        读者单次耗时 2.04 ms（文件句柄在整个 `json` 解析期间都开着）。也就是说：用 `Barrier` 写
        这条用例，**旧实现会全绿地通过** —— 那正是本项目最怕的"测试看起来有牙、其实没有"。
        自由循环让相位自然漂移，读者在整个跑动期间有相当比例的时间"文件开着"。

        ⚠️ **旧实现（无锁读）的红色形态是异常，不是丢数据**：`kb.absorb` 里
        `store.write_json` 的 `os.replace` 撞上读者打开着的 knowledge.json →
        `PermissionError: [WinError 5]` → `_with_kb_write` 不吞（这是对的：吞掉就是骗用户
        "已沉淀"）→ absorb 抛出；而**那张刚沉淀的卡只在没被改名过去的临时文件里** ⇒ 一起丢。
        于是下面的 `errors` 与 `missing` 会**同时**红（谁先红看时序）。
        """
        store.ensure_profile()
        self._seed_a_big_kb()
        self.assertIn(self.tmp, store.knowledge_path(), "探针必须落在临时根里")

        errors = []
        reads = []                        # 读者每次看到的卡数（钉"永远不是空库/残库"）
        start = threading.Event()         # 发令枪：两条线程同一刻起跑
        done = threading.Event()          # 写者跑完 N 轮 → 读者收工

        def writer():
            try:
                start.wait(30)
                for i in range(self.ROUNDS):
                    kb.absorb([{"term": "并发概念-%d" % i, "definition": "本次沉淀",
                                "plain_explanation": "并发写入", "layer": 2,
                                "sources": [{"url": "https://example.invalid/c%d" % i}]}],
                              {"id": "jd-conc", "job_title": "并发岗"})
            except Exception as e:            # 异常必须带回主线程，否则会被 unittest 吞掉
                errors.append("absorb 线程抛异常：%r" % (e,))
            finally:
                done.set()                    # 写者即使是异常退出，读者也必须能收工

        def reader():
            try:
                start.wait(30)
                while not done.is_set():
                    idx, err = kbreuse.build_index()
                    if err:
                        errors.append("build_index 报读不出来：%r" % (err,))
                        return
                    reads.append(len(idx))
            except Exception as e:
                errors.append("build_index 线程抛异常：%r" % (e,))

        tw = threading.Thread(target=writer, name="kbreuse-conc-absorb", daemon=True)
        tr = threading.Thread(target=reader, name="kbreuse-conc-read", daemon=True)
        tw.start()
        tr.start()
        start.set()
        tw.join(180)
        tr.join(180)
        self.assertEqual([t.name for t in (tw, tr) if t.is_alive()], [],
                         "并发用例没跑完（挂住了）—— 很可能是锁序/死锁")
        self.assertGreaterEqual(len(reads), 1, "夹具前提：读者至少真读过一次")
        self.assertEqual(errors, [],
                         "并发读写抛异常了（旧实现的形态就是 WinError 5，"
                         "而它把用户刚沉淀的卡一起带走）")
        have = {n.get("term") for n in store.load_knowledge()["nodes"]}
        missing = sorted({"并发概念-%d" % i for i in range(self.ROUNDS)} - have)
        self.assertEqual(missing, [], "absorb 沉淀的卡片丢了：%r" % (missing,))
        self.assertGreaterEqual(min(reads), self.SEED_CARDS,
                                "读者读到过一次空库/残库（把「读不出来」当成「没有」？）："
                                "最少的一次只有 %d 张" % min(reads))


class JudgeTermsTest(IsolatedCase):
    """`terms_for_judge()` 是 Task 5 的 judge 喂模型的清单，之前**完全没测试**。"""

    def test_it_returns_exactly_the_three_fields_with_a_truncated_definition(self):
        long_def = "定" * 41
        idx = kbreuse.Index([{"id": "语音识别", "term": "语音识别",
                              "definition": long_def, "aliases": ["ASR"]}])
        items = idx.terms_for_judge()
        self.assertEqual(len(items), len(idx))
        item = items[0]
        self.assertEqual(set(item), {"card_id", "term", "definition"},
                         "只带三个字段（省 token），别把整张卡塞给模型")
        self.assertEqual(item["term"], "语音识别")
        self.assertEqual(len(item["definition"]), 40, "41 字输入截成 40")
        self.assertEqual(item["definition"], "定" * 40)

    def test_card_id_is_the_kb_normalisation_of_the_term(self):
        """`card_id` 必须是 `kb.normalize_id(term)` —— Task 5 用回传的 card_id 取卡。

        口径不一致（比如塞原始 term 或小写 id）→ 模型回传的 card_id 在目录里**取不到卡**，
        而失败形态是"悄悄退化成新建一张重复卡"，看不出来。
        """
        idx = kbreuse.Index([{"id": "x", "term": " 语音识别（ASR） ", "definition": "d"},
                             {"id": "y", "term": "词错率（WER）", "definition": "d"}])
        by_term = {i["term"]: i for i in idx.terms_for_judge()}
        self.assertEqual(len(by_term), 2)
        for term, item in by_term.items():
            self.assertEqual(item["card_id"], kb.normalize_id(term))
            self.assertIn(item["card_id"], idx.cards, "card_id 必须能在目录里取到那张卡")
            self.assertIs(idx.cards[item["card_id"]], idx.lookup(term)[0])

    def test_missing_definition_becomes_an_empty_string_not_the_word_none(self):
        idx = kbreuse.Index([{"id": "x", "term": "X", "definition": None}])
        self.assertEqual(idx.terms_for_judge()[0]["definition"], "")


class PlanNodesTest(IsolatedCase):
    def _node(self, term, **kw):
        n = {"id": store.slug(term), "term": term, "definition": "本次写的定义",
             "plain_explanation": "本次写的通俗解释", "category": "核心概念", "layer": 0,
             "sources": [], "timeline": [], "related": [], "interview_questions": [],
             "confidence": "ai-generated", "state": "待学习"}
        n.update(kw)
        return n

    def _card(self, term="语音识别", **kw):
        c = {"id": kb.normalize_id(term), "term": term,
             "definition": "库里的定义", "plain_explanation": "库里的通俗解释",
             "category": "核心概念", "layer": 0,
             "sources": [{"title": "维基", "url": "https://w"}],
             "timeline": [{"year": "2010", "text": "库里的时间线"}],
             "related": [{"id": "别的节点", "relation": "相关"}],
             "interview_questions": ["会怎么问"], "state": "已掌握",
             "from_jds": [{"id": "老岗位", "job_title": "语音/文本评测", "date": "2026-10-03"}]}
        c.update(kw)
        return c

    def test_a_reusable_hit_is_copied_wholesale_and_marked(self):
        node = self._node("语音识别")
        card = self._card()
        stats = kbreuse.plan_nodes([node], kbreuse.Index([card]))
        self.assertEqual(stats["reusable"], 1)
        self.assertEqual(node["definition"], "库里的定义", "整条用库里那份（spec 决策 5）")
        self.assertEqual(node["plain_explanation"], "库里的通俗解释")
        # 形状类字段走 **kb 自己的清洗函数**（M-6）：`clean_sources` 会把缺失的
        # date/accessed/snippet 补成空串、丢掉没 url 的项 —— 所以这里比的是**清洗后**的形状，
        # 而不是卡片里那份原样。口径只有一套（kb），报告里读到的与写回库里的因此一致。
        self.assertEqual(node["sources"], kb.clean_sources(card["sources"]))
        self.assertEqual(node["timeline"], kb.clean_timeline(card["timeline"]))
        self.assertEqual(node["state"], "已掌握")
        self.assertEqual(node["confidence"], "verified")
        r = node["kb_reuse"]
        self.assertTrue(r["reusable"])
        self.assertEqual(r["card_id"], "语音识别")
        self.assertEqual(r["matched_by"], "exact")
        self.assertEqual(r["got"], "all", "库里有时间线 → 时间线也不用跑")
        self.assertEqual(r["term_original"], "", "同名时原写法留空")
        self.assertEqual(r["origin"], {"job_title": "语音/文本评测", "date": "2026-10-03"})

    def test_a_hit_without_a_timeline_is_reused_but_needs_the_timeline(self):
        node = self._node("语音识别")
        kbreuse.plan_nodes([node], kbreuse.Index([self._card(timeline=[])]))
        self.assertTrue(node["kb_reuse"]["reusable"])
        self.assertEqual(node["kb_reuse"]["got"], "sources", "缺时间线 → ④ 还要跑一次补算")
        self.assertEqual(node["timeline"], [])
        self.assertIn("timeline", node["kb_reuse"]["missing"])

    def test_an_incomplete_card_is_matched_but_not_reused_and_content_is_untouched(self):
        node = self._node("语音识别")
        stats = kbreuse.plan_nodes([node], kbreuse.Index([self._card(sources=[])]))
        self.assertEqual(stats["matched"], 1)
        self.assertEqual(stats["reusable"], 0)
        self.assertEqual(stats["enriched"], 1, "命中但不够格 → 联网并把结果写回那张卡")
        self.assertFalse(node["kb_reuse"]["reusable"])
        self.assertIn("sources", node["kb_reuse"]["missing"])
        self.assertEqual(node["kb_reuse"]["got"], "",
                         "不够格 ⇒ 什么都没拿到，③④ 全部照旧要跑（M-9 补的牙）")
        self.assertEqual(node["definition"], "本次写的定义", "不够格时**不许改内容**，让 ③④ 去跑")
        self.assertEqual(node["sources"], [])

    def test_a_different_name_gets_renamed_to_the_canonical_term(self):
        """身份对齐（spec §7）：名字必须改成库里的规范名，否则写回会新建重复卡。

        ⚠️ **夹具必须让它们真的能命中**（第一版计划在这里错过）：确定性匹配**不可能**知道
        「ASR」=「语音识别」，所以这里给卡片记一个 `aliases: ["ASR"]` ——
        于是节点「ASR」能通过**别名**命中「语音识别」，才走到了"改名"这一步。
        """
        node = self._node("ASR")
        kbreuse.plan_nodes([node], kbreuse.Index([self._card(term="语音识别", aliases=["ASR"])]))
        self.assertEqual(node["kb_reuse"]["matched_by"], "alias")
        self.assertEqual(node["term"], "语音识别")
        self.assertEqual(node["kb_reuse"]["term_original"], "ASR")
        self.assertEqual(node["kb_reuse"]["card_term"], "语音识别")

    def test_the_node_id_is_not_touched(self):
        """`id` 是报告里关联概念图用的，改它会打断 `related` 引用（spec §7）。

        先断言"确实命中了"，否则这条会因为没命中而**假绿**。
        """
        node = self._node("ASR")
        before = node["id"]
        kbreuse.plan_nodes([node], kbreuse.Index([self._card(term="语音识别", aliases=["ASR"])]))
        self.assertIn("kb_reuse", node, "没命中就没有'改不改 id'这回事（假绿）")
        self.assertEqual(node["term"], "语音识别", "夹具前提：这条确实走了改名")
        self.assertEqual(node["id"], before)

    def test_related_is_not_copied_from_the_card(self):
        """卡片的 `related[].id` 指向**上一次分析**的节点，复制过来在本次报告里是断链。"""
        node = self._node("语音识别")
        kbreuse.plan_nodes([node], kbreuse.Index([self._card()]))
        self.assertEqual(node["related"], [], "不复制 related（spec §7）")

    def test_a_miss_is_left_completely_alone(self):
        node = self._node("完全不相干的概念")
        stats = kbreuse.plan_nodes([node], kbreuse.Index([self._card()]))
        self.assertEqual(stats, {"total": 1, "matched": 0, "reusable": 0,
                                 "timeline_reused": 0, "enriched": 0, "aligned_only": 0,
                                 "searched": 1, "knowledge_error": ""})
        self.assertNotIn("kb_reuse", node)

    def test_two_nodes_resolving_to_one_card_both_get_reused(self):
        """确定性同名就是同一个概念，两个都复用（spec §5）。"""
        a, b = self._node("语音识别"), self._node("语音识别")
        b["id"] = "另一个-id"
        stats = kbreuse.plan_nodes([a, b], kbreuse.Index([self._card()]))
        self.assertEqual(stats["reusable"], 2)
        self.assertEqual(a["definition"], "库里的定义")
        self.assertEqual(b["definition"], "库里的定义")

    def test_stats_count_what_will_still_be_searched(self):
        nodes = [self._node("语音识别"), self._node("词错率（WER）"), self._node("没见过的概念")]
        idx = kbreuse.Index([self._card(), self._card(term="词错率（WER）", timeline=[])])
        stats = kbreuse.plan_nodes(nodes, idx)
        self.assertEqual(stats["total"], 3)
        self.assertEqual(stats["matched"], 2)
        self.assertEqual(stats["reusable"], 2, "缺时间线也算 reusable")
        self.assertEqual(stats["timeline_reused"], 1, "只有库里带时间线的那条省掉 ④")
        self.assertEqual(stats["searched"], 1, "没命中的那条才是真要联网的")

    def test_an_empty_index_leaves_everything_alone(self):
        node = self._node("语音识别")
        stats = kbreuse.plan_nodes([node], kbreuse.Index([]))
        self.assertEqual(stats["searched"], 1)
        self.assertNotIn("kb_reuse", node)
        self.assertEqual(node["definition"], "本次写的定义")

    def test_an_assisted_match_fills_the_node_and_marks_the_method(self):
        """模型判定出来的命中，走的是与确定性命中**同一条**填充路径，只是 `matched_by` 不同。

        断言批次**镜像**确定性命中那条（`term_original` / `origin` / `got` / `sources` /
        `confidence`），才算证明"同一条填充路径"—— 只断言"有个东西填了 term+definition"
        证明不了这件事（上一个复查点名的缺口）。
        """
        node = self._node("ASR")
        card = self._card()
        idx = kbreuse.Index([card])

        def call(messages):
            return json.dumps({"pairs": [{"node": "ASR", "card_id": "语音识别"}]})

        stats = kbreuse.plan_nodes([node], idx, call=call)
        r = node["kb_reuse"]
        self.assertEqual(r["matched_by"], "assisted")
        self.assertEqual(node["term"], "语音识别", "assisted 也要做身份对齐")
        self.assertEqual(r["card_id"], "语音识别")
        self.assertEqual(r["card_term"], "语音识别")
        self.assertEqual(r["term_original"], "ASR", "改名确实发生过 → 记下 JD 原本的写法")
        self.assertEqual(node["definition"], "库里的定义")
        self.assertEqual(node["plain_explanation"], "库里的通俗解释")
        self.assertEqual(node["sources"], kb.clean_sources(card["sources"]), "同一条清洗路径（M-6）")
        self.assertEqual(node["timeline"], kb.clean_timeline(card["timeline"]))
        self.assertEqual(node["confidence"], "verified")
        self.assertEqual(node["state"], "已掌握")
        self.assertEqual(r["got"], "all", "库里有时间线 → 时间线也不用跑")
        self.assertTrue(r["reusable"])
        self.assertEqual(r["missing"], [])
        self.assertEqual(r["origin"], {"job_title": "语音/文本评测", "date": "2026-10-03"})
        self.assertEqual(stats["reusable"], 1)
        self.assertEqual(stats["timeline_reused"], 1)
        self.assertEqual(stats["searched"], 0)


class BrokenShapeTest(IsolatedCase):
    """`knowledge.json` **是用户可手改的** —— 手改坏的一张卡不许让整次分析的知识地图崩掉（I-1）。

    崩的代价不是"悄悄认错"，而是**整步崩**：Task 5 接上后，一张 `layer: "abc"` 的卡会让
    `plan_nodes` 抛出去、整次分析退化成 `knowledge_error`，**其余几十个节点的复用机会一起丢**。
    项目自己就把这个形状当真：`store.as_int(..., bool_ok=True)`（kb 那把尺子，`kb.py:289` / `:301`）
    就是为它写的，`tests/test_kb.py:306-314` 明确把 `"layer": "abc"` 当"不许抛"的形状。
    （批 5 之前这里写的是 `kb._as_int`（`kb.py:83-88`）—— 那份副本已收归 store，注释跟着改；
     第四轮又把 `interview.py` / `简历迁移.py` 两份漏网的副本也收了，见
     `tests/test_store.py::SingleImplementationTest`。）
    """

    def _node(self, term="语音识别", **kw):
        n = {"id": store.slug(term), "term": term, "definition": "本次写的定义",
             "plain_explanation": "本次写的通俗解释", "category": "核心概念", "layer": 4,
             "sources": [], "timeline": [], "related": [], "interview_questions": [],
             "confidence": "ai-generated", "state": "待学习"}
        n.update(kw)
        return n

    def _card(self, **kw):
        c = {"id": "语音识别", "term": "语音识别", "definition": "库里的定义",
             "plain_explanation": "库里的通俗解释", "category": "核心概念", "layer": 0,
             "sources": [{"url": "https://w"}], "timeline": [{"year": "2010", "text": "t"}],
             "from_jds": [{"id": "j1", "job_title": "岗位一", "date": "2025-01-02"}]}
        c.update(kw)
        return c

    def test_a_broken_layer_never_throws_and_is_floored_at_zero(self):
        """`layer` 走 `store.as_int(..., bool_ok=True)` 兜底 —— `int("abc")` / `int([1])` 都会把整步炸掉。"""
        for bad in ("abc", [1], None, {}, "3.7", float("nan"), -9):
            node = self._node()
            card = self._card(layer=bad)
            stats = kbreuse.plan_nodes([node], kbreuse.Index([card]))     # 不许抛
            self.assertEqual(stats["reusable"], 1)
            self.assertEqual(node["layer"], 0,
                             "坏 layer 按 store.as_int(bool_ok=True) 兜底成 0（下界也夹 0）：%r" % (bad,))

    def test_a_missing_layer_behaves_like_zero(self):
        node = self._node()
        card = self._card()
        del card["layer"]
        kbreuse.plan_nodes([node], kbreuse.Index([card]))
        self.assertEqual(node["layer"], 0, "字段缺失 = 0，与 kb 的口径一致")

    def test_a_broken_layer_on_the_node_side_also_never_throws(self):
        for bad in ("abc", [1], None, {}):
            node = self._node(layer=bad)
            card = self._card(layer=3)
            kbreuse.plan_nodes([node], kbreuse.Index([card]))             # 不许抛
            self.assertEqual(node["layer"], 0, "节点侧的坏 layer 同样兜底成 0：%r" % (bad,))

    def test_the_layer_is_the_minimum_of_the_node_and_the_card(self):
        """取 **min**（与 `kb._merge_into` 同一条规则）：把库里那条并到本次节点上时，
        层级取两者更靠前的那一个 —— 取 max 会把已经学会的知识点重新推回"深层"。
        两边各让一次（1/3 与 3/1），否则"谁小谁赢"这件事测不到（M-9 补的承重牙）。
        """
        for node_layer, card_layer, want in ((1, 3, 1), (3, 1, 1), (0, 2, 0), (2, 0, 0),
                                             (-5, 2, 0), (5, -9, 0)):
            node = self._node(layer=node_layer)
            kbreuse.plan_nodes([node], kbreuse.Index([self._card(layer=card_layer)]))
            self.assertEqual(node["layer"], want,
                             "节点 layer=%r、卡 layer=%r 时该是 %r"
                             % (node_layer, card_layer, want))

    def test_a_float_layer_is_truncated_like_store_as_int(self):
        node = self._node(layer=5)
        kbreuse.plan_nodes([node], kbreuse.Index([self._card(layer=3.7)]))
        self.assertEqual(node["layer"], 3, "`store.as_int(3.7, bool_ok=True)` == 3（截断，不四舍五入）")


class CopyContractTest(IsolatedCase):
    """M-2/M-6：复制到报告节点上的那几个字段**在本次运行内只读**，且形状要按 kb 的口径收口。"""

    def _node(self, term="语音识别", **kw):
        n = {"id": store.slug(term), "term": term, "definition": "本次写的定义",
             "plain_explanation": "本次写的通俗解释", "category": "核心概念", "layer": 0,
             "sources": [], "timeline": [], "related": [], "interview_questions": [],
             "confidence": "ai-generated", "state": "待学习"}
        n.update(kw)
        return n

    def _card(self, term="语音识别", **kw):
        c = {"id": kb.normalize_id(term), "term": term,
             "definition": "库里的定义", "plain_explanation": "库里的通俗解释",
             "category": "核心概念", "layer": 0,
             "sources": [{"title": "维基", "url": "https://w"}],
             "timeline": [{"year": "2010", "text": "库里的时间线"}],
             "interview_questions": ["会怎么问"], "state": "已掌握",
             "from_jds": [{"id": "j1", "job_title": "岗位一", "date": "2025-01-02"}]}
        c.update(kw)
        return c

    def test_the_copied_items_are_fresh_objects_not_shared_with_the_card(self):
        """**里面的 dict 不许与卡片共享**（M-2）。

        `list(v)` 只隔开列表本身，元素还是同一批 dict —— 今天不会真出事（下游都是重绑定），
        但那是"碰巧成立"：复查反证过，就地把共享的那个 dict 改脏、再 `kb.absorb`，
        **盘上那张卡真的会被改脏**。所以这里就地改节点、断言卡片一字不动。
        """
        node = self._node()
        card = self._card()
        kbreuse.plan_nodes([node], kbreuse.Index([card]))
        self.assertIsNot(node["sources"], card["sources"], "列表本身是新对象")
        self.assertIsNot(node["sources"][0], card["sources"][0],
                         "里面的 dict 也必须是新对象（M-2 的牙就在这一行）")
        self.assertIsNot(node["timeline"][0], card["timeline"][0])
        self.assertIsNot(node["interview_questions"], card["interview_questions"])
        node["sources"][0]["title"] = "就地把节点改脏"
        node["timeline"][0]["text"] = "就地把节点改脏"
        node["interview_questions"].append("就地把节点改脏")
        self.assertEqual(card["sources"][0]["title"], "维基", "就地改节点不许污染库里那张卡")
        self.assertEqual(card["timeline"][0]["text"], "库里的时间线")
        self.assertEqual(card["interview_questions"], ["会怎么问"])

    def test_odd_shaped_card_fields_are_cleaned_not_copied_raw(self):
        """`definition=5` / `timeline={"a": 1}` / `interview_questions="abc"` 这类**奇形值**不许
        原样落进报告节点（M-6）—— 形状类复用 `kb.clean_*`，文本类走 `str()`，**不另写一套**。
        """
        card = self._card(definition=5, plain_explanation={"a": 1}, category=[1],
                          state=None, timeline={"a": 1}, interview_questions="abc")
        node = self._node()
        kbreuse.plan_nodes([node], kbreuse.Index([card]))
        self.assertTrue(node["kb_reuse"]["reusable"], "夹具前提：这张卡够格（有真 url）")
        self.assertEqual(node["definition"], "5", "文本类走 str()")
        self.assertIsInstance(node["plain_explanation"], str)
        self.assertIsInstance(node["category"], str)
        self.assertIsInstance(node["state"], str)
        self.assertEqual(node["timeline"], [], "形状类复用 kb.clean_timeline：dict 不是时间线")
        self.assertEqual(node["interview_questions"], [],
                         "形状类复用 kb.clean_questions：字符串不是问题列表")
        self.assertIsInstance(node["sources"], list)

    def test_a_card_with_a_bad_source_shape_is_cleaned_by_the_kb_rule(self):
        """`sources` 走 `kb.clean_sources`：没 url 的项被丢掉，留下的补齐固定形状。"""
        card = self._card(sources=[{"url": "https://w", "title": "标题"},
                                   {"title": "没有 url"}, "不是 dict", None])
        node = self._node()
        kbreuse.plan_nodes([node], kbreuse.Index([card]))
        self.assertEqual(node["sources"], [{"title": "标题", "url": "https://w",
                                            "date": "", "accessed": "", "snippet": ""}])

    def test_a_reused_hit_is_always_verified(self):
        """M-3：`confidence` 的 `ai-generated` 支路**不可达** —— `_fill_from_card` 只在
        `reusable` 为真时被调用，而够格的定义里本来就要求「≥1 条带 url 的来源」。
        旧代码 `"verified" if card.get("sources") else "ai-generated"` 是**死分支**，
        而且它用的是 truthy，不是 completeness 那把"有 url 的 dict"的尺子（两把尺子会漂移）。
        所以直接写 `"verified"` 并在这里钉住：需要留意的只是**反过来**那一半 ——
        命中但不够格时**一个字都不许动**（③④ 跑完由 `absorb` 重算）。
        """
        node = self._node()
        kbreuse.plan_nodes([node], kbreuse.Index([self._card()]))
        self.assertTrue(node["kb_reuse"]["reusable"], "夹具前提")
        self.assertEqual(node["confidence"], "verified")
        node2 = self._node()
        kbreuse.plan_nodes([node2], kbreuse.Index([self._card(sources=[])]))
        self.assertFalse(node2["kb_reuse"]["reusable"], "夹具前提")
        self.assertEqual(node2["confidence"], "ai-generated", "不够格时不许动 confidence")


class OriginTest(IsolatedCase):
    """`origin` = 这张卡**最早**来自哪个岗位（M-7 + M-9 的承重牙）。"""

    def _node(self, term="语音识别"):
        return {"id": store.slug(term), "term": term, "definition": "本次写的定义",
                "plain_explanation": "本次写的通俗解释", "category": "核心概念", "layer": 0,
                "sources": [], "timeline": [], "related": [], "interview_questions": [],
                "confidence": "ai-generated", "state": "待学习"}

    def _card(self, from_jds):
        return {"id": "语音识别", "term": "语音识别", "definition": "库里的定义",
                "plain_explanation": "库里的通俗解释", "category": "核心概念", "layer": 0,
                "sources": [{"url": "https://w"}], "timeline": [], "from_jds": from_jds}

    def _origin(self, from_jds):
        node = self._node()
        kbreuse.plan_nodes([node], kbreuse.Index([self._card(from_jds)]))
        return node["kb_reuse"]["origin"]

    def test_the_earliest_jd_wins(self):
        """取**最早**那条 —— 夹具必须有 2–3 条 `from_jds`，否则"early"根本测不到（M-9 的承重牙）。

        真库每张卡都攒着好几个岗位（`from_jds` 按加入顺序追加），只给一条时
        `min` 与 `max` 的变异**都会绿** —— 上一轮就是这么漏掉的。
        """
        self.assertEqual(self._origin([
            {"id": "j3", "job_title": "最近的岗位", "date": "2026-10-03"},
            {"id": "j2", "job_title": "中间的岗位", "date": "2025-05-05"},
            {"id": "j1", "job_title": "最早的岗位", "date": "2024-01-02"},
        ]), {"job_title": "最早的岗位", "date": "2024-01-02"})

    def test_an_empty_date_never_beats_a_real_one(self):
        """手改出 `date: ""` 时，它**抢赢所有真日期**（空串是最小的字符串）→ origin 变空（M-7）。"""
        self.assertEqual(self._origin([
            {"id": "j3", "job_title": "最近的岗位", "date": "2026-10-03"},
            {"id": "j1", "job_title": "手改出来的空日期", "date": ""},
            {"id": "j2", "job_title": "最早的岗位", "date": "2024-01-02"},
        ]), {"job_title": "最早的岗位", "date": "2024-01-02"})
        self.assertEqual(self._origin([
            {"id": "j1", "job_title": "手改出来的空日期", "date": ""},
            {"id": "j2", "job_title": "全是空白日期", "date": "   "},
            {"id": "j3", "job_title": "有真日期", "date": "2024-01-02"},
        ]), {"job_title": "有真日期", "date": "2024-01-02"})

    def test_no_date_at_all_falls_back_to_the_first_row(self):
        """一条真日期都没有时：没有"最早"可比 → **保序退回第一条**（`from_jds` 是按加入顺序追加的，
        所以第一条就是最早入库的那次），`date` 留空 —— 不编造任何日期。
        """
        self.assertEqual(self._origin([
            {"id": "j1", "job_title": "第一条", "date": ""},
            {"id": "j2", "job_title": "第二条", "date": "   "},
        ]), {"job_title": "第一条", "date": ""})

    def test_missing_or_broken_from_jds_is_an_empty_origin(self):
        self.assertEqual(self._origin([]), {"job_title": "", "date": ""})
        self.assertEqual(self._origin([None, 5, "不是 dict"]), {"job_title": "", "date": ""})
        self.assertEqual(self._origin([{"id": "j1"}]), {"job_title": "", "date": ""})


class PlanNodesContractTest(IsolatedCase):
    """M-4 / M-5 / M-9：库读不出来要**说出来**；同一个节点对象不许被算两遍；空库不花模型调用。"""

    def _node(self, term="语音识别", **kw):
        n = {"id": store.slug(term), "term": term, "definition": "本次写的定义",
             "plain_explanation": "本次写的通俗解释", "category": "核心概念", "layer": 0,
             "sources": [], "timeline": [], "related": [], "interview_questions": [],
             "confidence": "ai-generated", "state": "待学习"}
        n.update(kw)
        return n

    def _card(self, term="语音识别", **kw):
        c = {"id": kb.normalize_id(term), "term": term,
             "definition": "库里的定义", "plain_explanation": "库里的通俗解释",
             "category": "核心概念", "layer": 0,
             "sources": [{"url": "https://w"}], "timeline": [{"year": "2010", "text": "t"}],
             "from_jds": [{"id": "j1", "job_title": "岗位一", "date": "2025-01-02"}]}
        c.update(kw)
        return c

    def test_a_knowledge_error_is_carried_into_the_stats(self):
        """`knowledge_error` 以前是**死字段**（恒 `""`），调用方分不清"库是空的"与"库读不出来"（M-4）。"""
        node = self._node()
        stats = kbreuse.plan_nodes([node], kbreuse.Index([]), error="读不出来：坏文件")
        self.assertEqual(stats["knowledge_error"], "读不出来：坏文件")
        self.assertNotIn("kb_reuse", node)

    def test_the_error_reaches_the_stats_even_when_cards_were_planned(self):
        node = self._node()
        stats = kbreuse.plan_nodes([node], kbreuse.Index([self._card()]),
                                   error="读不出来：坏文件")
        self.assertEqual(stats["knowledge_error"], "读不出来：坏文件")
        self.assertEqual(stats["reusable"], 1, "带着 err 也照样把能复用的复用掉")

    def test_no_error_means_the_empty_string(self):
        stats = kbreuse.plan_nodes([self._node()], kbreuse.Index([]))
        self.assertEqual(stats["knowledge_error"], "")

    def test_index_none_is_not_dressed_up_as_a_healthy_empty_base(self):
        """`index=None` 不许被静默当成"库是空的" —— 那条 `err` 是**唯一**的区分依据（M-4）。"""
        node = self._node()
        stats = kbreuse.plan_nodes([node], None, error="读不出来：坏文件")
        self.assertEqual(stats["knowledge_error"], "读不出来：坏文件",
                         "调用方必须能区分『库是空的』与『库读不出来』")
        self.assertEqual(stats["searched"], 1)
        self.assertNotIn("kb_reuse", node)

    def test_the_same_node_object_listed_twice_is_counted_once(self):
        """同一个节点对象出现两次时：统计各 +2、第二遍还把 `term_original` 抹成 `""`（M-5）。"""
        node = self._node("ASR")
        card = self._card(term="语音识别", aliases=["ASR"])
        stats = kbreuse.plan_nodes([node, node], kbreuse.Index([card]))
        self.assertEqual(stats["total"], 1, "同一个对象就是一个节点")
        self.assertEqual(stats["matched"], 1)
        self.assertEqual(stats["reusable"], 1)
        self.assertEqual(stats["timeline_reused"], 1)
        self.assertEqual(stats["searched"], 0)
        self.assertEqual(node["kb_reuse"]["term_original"], "ASR",
                         "第二遍不许把第一遍写好的改名记录抹掉")

    def test_two_distinct_nodes_with_the_same_term_are_still_two(self):
        """去重按 **`id(n)`（是不是同一个对象）**，不是按名字 —— 两个真节点都要算。"""
        a, b = self._node(), self._node()
        self.assertIsNot(a, b)
        stats = kbreuse.plan_nodes([a, b], kbreuse.Index([self._card()]))
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["reusable"], 2)

    def test_an_empty_index_never_calls_the_judge(self):
        """**空库直接早退，一次模型调用都不花**（M-9 的承重牙）。

        这是"省一次模型调用"的闸门，而且空库时本来也没什么可判的。上一轮的变异
        （去掉早退）全量绿 —— 因为当时只用假 `call` 计数，而 `call` 要经 `judge` 才被用到；
        这里直接盯 `judge` 自己被没被叫到，两个方向都钉住（叫了 / 没叫）。
        """
        node = self._node()
        seen = []
        forbidden_seen = []

        def fake_judge(rest, index, call, forbidden=None, dropped=None):
            seen.append(list(rest))
            forbidden_seen.append(set(forbidden or ()))
            return {}

        with mock.patch.object(kbreuse, "judge", fake_judge):
            stats = kbreuse.plan_nodes([node], kbreuse.Index([]), call=lambda m: "{}")
        self.assertEqual(seen, [], "空库不许调判定（那次模型调用必须省掉）")
        self.assertEqual(stats["searched"], 1)

        # 反向：非空库 + 有未命中的节点 + 给了 call → 必须走判定，否则上面那条会假绿
        with mock.patch.object(kbreuse, "judge", fake_judge):
            kbreuse.plan_nodes([node], kbreuse.Index([self._card(term="别的概念")]),
                               call=lambda m: "{}")
        self.assertEqual(len(seen), 1, "有未命中节点时必须走判定")
        self.assertEqual([len(r) for r in seen], [1], "只把**没命中的**那些交给判定")
        self.assertEqual(forbidden_seen, [set()], "没有确定性命中时禁选集合是空的（不是「缺参」）")

    def test_a_call_without_any_unmatched_node_does_not_reach_the_judge(self):
        """全都确定性命中了 → 也没有可判的东西，同样省掉那次调用。"""
        node = self._node()
        seen = []

        def fake_judge(rest, index, call, forbidden=None, dropped=None):
            seen.append(list(rest))
            return {}

        with mock.patch.object(kbreuse, "judge", fake_judge):
            kbreuse.plan_nodes([node], kbreuse.Index([self._card()]), call=lambda m: "{}")
        self.assertEqual(seen, [])


class CrossBoundaryTest(IsolatedCase):
    """I-1（**真实的认错面**）："一卡一条"必须跨过「确定性 / 判定」这条边界。

    构造复现：同一张卡同时被「同名命中」的节点**和**「模型判定命中」的节点认领 →
    两个节点被改成同一个 `term`、装着一模一样的内容（而其中一个概念其实没被认出来）。
    另外 `node:""` 能一次认领**多个**空名节点（实测两个空名节点会被填上同一张卡）。

    三道闸门，**各自**都有牙（缺任何一道，对应的那条就红）：
      1. `judge(..., forbidden=…)`：确定性已占用的 `card_id` 作为禁选集合交给判定；
      2. 空名节点（`kb.normalize_id(term)` 为空）**不参与判定**；
      3. 填充前再收口一次：判定命中**不许**拿已被确定性占用的卡（也互不共享）。
    """

    def _node(self, term, **kw):
        n = {"id": store.slug(term or "空名"), "term": term, "definition": "本次写的定义",
             "plain_explanation": "本次写的通俗解释", "category": "核心概念", "layer": 0,
             "sources": [], "timeline": [], "related": [], "interview_questions": [],
             "confidence": "ai-generated", "state": "待学习"}
        n.update(kw)
        return n

    def _card(self, term="语音识别", **kw):
        c = {"id": kb.normalize_id(term), "term": term,
             "definition": "库里的定义", "plain_explanation": "库里的通俗解释",
             "category": "核心概念", "layer": 0,
             "sources": [{"url": "https://w"}], "timeline": [{"year": "2010", "text": "t"}],
             "from_jds": [{"id": "j1", "job_title": "岗位一", "date": "2025-01-02"}]}
        c.update(kw)
        return c

    @staticmethod
    def _reply(obj):
        return lambda messages: json.dumps(obj, ensure_ascii=False)

    # ---------------- 闸门 1 + 3：确定性已占用的卡不许再落到判定命中那条
    def test_an_exact_hit_and_a_judged_hit_never_share_one_card(self):
        """一条精确名节点 + 一条被模型判成同卡的节点 → 库里的内容**只填进一条**。

        另一条**一个字节的内容都不许改**（definition / sources / timeline / confidence
        全是本次的）—— 它去联网。但它的 `term` **必须被对齐**成库里的规范名
        （必修②：不对齐的话写回 `kb.absorb` 会新建一张重复卡）。

        ⚠️ 这条改动的可见后果：报告里会出现**两条同名节点**（一条复用、一条本次生成），
        这是**可接受的**（写回并进同一张卡），别当成 bug。
        """
        exact, judged = self._node("语音识别"), self._node("ASR")
        idx = kbreuse.Index([self._card()])
        stats = kbreuse.plan_nodes([exact, judged], idx, call=self._reply(
            {"pairs": [{"node": "ASR", "card_id": "语音识别"}]}))
        self.assertIn("kb_reuse", exact, "夹具前提：精确名那条确实命中了")
        self.assertEqual(exact["kb_reuse"]["matched_by"], "exact")
        self.assertEqual(exact["definition"], "库里的定义")
        r = judged["kb_reuse"]
        self.assertTrue(r["aligned_only"], "被判回的那条只能对齐身份")
        self.assertFalse(r["reusable"], "内容没从库里拿 → 不许说自己复用了")
        self.assertEqual(judged["term"], "语音识别", "身份必须对齐（否则写回新建重复卡）")
        self.assertEqual(r["term_original"], "ASR", "改名确实发生过 → 记下 JD 原本的写法")
        self.assertEqual(judged["definition"], "本次写的定义", "没被认领的那条内容一个字都不许动")
        self.assertEqual(judged["sources"], [], "本次的来源是本次的（它照旧要联网）")
        self.assertEqual(judged["confidence"], "ai-generated")
        self.assertEqual(stats["matched"], 2,
                         "matched = 拿到 kb_reuse 标记的节点数（含只对齐身份那条），"
                         "报告里 reused + enriched + aligned_only 才能对上它")
        self.assertEqual(stats["reusable"], 1)
        self.assertEqual(stats["aligned_only"], 1)
        self.assertEqual(stats["searched"], 1, "被判回的那条照旧联网")

    def test_the_plan_gate_drops_a_judged_card_that_the_exact_hit_already_took(self):
        """闸门 3 的**独立**牙：判定层就算无视 `forbidden` 照报这张卡，填充前也必须拦下。

        这条把 `judge` 换成"不认 forbidden 的坏判定"（模拟判定层自己漏了），
        所以它红的唯一原因就是 `plan_nodes` 那道收口被拿掉了。
        **拦下的含义是"不拿它的内容"**（`aligned_only` + 内容原样），不是"什么都不做"：
        身份对齐照样要做（必修②），否则写回新建重复卡。
        """
        exact, judged = self._node("语音识别"), self._node("ASR")
        idx = kbreuse.Index([self._card()])

        def bad_judge(rest, index, call, forbidden=None, dropped=None):
            return {"ASR": "语音识别"}          # 明知已被占用照报（禁选集合形同虚设）

        with mock.patch.object(kbreuse, "judge", bad_judge):
            kbreuse.plan_nodes([exact, judged], idx, call=lambda m: "{}")
        self.assertIn("kb_reuse", exact)
        self.assertEqual(exact["definition"], "库里的定义")
        self.assertTrue(judged["kb_reuse"]["aligned_only"],
                        "填充前那道收口没拦住已被确定性占用的卡（内容被抢走了）")
        self.assertEqual(judged["definition"], "本次写的定义", "内容一个字都不许动")
        self.assertEqual(judged["term"], "语音识别", "但身份要对齐")

    def test_the_judge_itself_drops_a_forbidden_card_and_keeps_the_legal_one(self):
        """闸门 1 的**独立**牙（判定层的单元测试）：禁选卡丢弃，同一条回复里合法的照活。

        同时钉住**必修②**的那个出参：丢掉的配对**不是凭空消失**，它必须出现在 `dropped`
        里（否则调用方失去身份对齐的机会 → 写回新建重复卡）。
        """
        idx = kbreuse.Index([self._card(), self._card(term="词错率（WER）")])
        dropped = {}
        out = kbreuse.judge(self._nodes_for_judge("ASR", "词错率别名"), idx,
                            self._reply({"pairs": [
                                {"node": "ASR", "card_id": "语音识别"},          # 禁选 → 丢
                                {"node": "词错率别名", "card_id": "词错率wer"}]}),  # 合法 → 活
                            forbidden={"语音识别"}, dropped=dropped)
        self.assertEqual(out, {"词错率别名": "词错率wer"}, "禁选卡必须丢，合法的必须活")
        self.assertEqual(dropped, {"ASR": "语音识别"},
                         "被禁选丢掉的配对要如实报给调用方（它靠这个做身份对齐）")

    def test_the_judge_reports_the_loser_when_two_nodes_claim_one_card(self):
        """必修②的另一半：**一卡一条**的内层去重（两条节点认领同一张卡，先出现的赢）。

        输的那条同样要进 `dropped` —— 否则它写回时会新建一张重复卡
        （终审实测：`泛化能力` + `模型泛化` 这类"同一张卡被两条节点认领"）。
        """
        idx = kbreuse.Index([self._card()])
        dropped = {}
        out = kbreuse.judge(self._nodes_for_judge("ASR", "语音识别的别名"), idx,
                            self._reply({"pairs": [
                                {"node": "ASR", "card_id": "语音识别"},
                                {"node": "语音识别的别名", "card_id": "语音识别"}]}),
                            dropped=dropped)
        self.assertEqual(out, {"ASR": "语音识别"}, "一卡一条：先出现的赢")
        self.assertEqual(dropped, {"语音识别的别名": "语音识别"}, "输的那条也要报出来（用来对齐身份）")

    def test_a_card_id_from_another_chunk_never_lands_in_dropped(self):
        """**不在这一块**的 `card_id` 连 `dropped` 都不进：模型在发明概念/记错了块，
        拿它对齐等于按幻觉改名（比"少对齐一次"危险得多）。"""
        idx = kbreuse.Index([{"id": "甲", "term": "甲概念", "definition": "d"},
                             {"id": "乙", "term": "乙概念", "definition": "d"}])
        dropped = {}
        out = kbreuse.judge([{"id": "n", "term": "别人", "definition": "d"}], idx,
                            self._reply({"pairs": [{"node": "别人", "card_id": "不存在的卡"}]}),
                            dropped=dropped)
        self.assertEqual((out, dropped), ({}, {}))

    @staticmethod
    def _nodes_for_judge(*terms):
        return [{"id": store.slug(t), "term": t, "definition": "d"} for t in terms]

    # ---------------- 闸门 2：空名节点不参与判定
    def test_two_empty_named_nodes_are_never_claimed_by_the_judge(self):
        """两个 `term=""` 的节点 + 一次判定 → **都不被认领**（`node:""` 不能一次领两张卡）。

        ⚠️ 断言必须落在**第一个**空名节点上：只断言第二个的话，"一张卡只填一条"那道收口
        会让它假绿（第一个被填、第二个没填，看起来"没被认领"）。
        """
        a, b = self._node(""), self._node("")
        b["id"] = "另一个空名-id"
        idx = kbreuse.Index([self._card()])
        stats = kbreuse.plan_nodes([a, b], idx, call=self._reply(
            {"pairs": [{"node": "", "card_id": "语音识别"}]}))
        self.assertNotIn("kb_reuse", a, "空名节点参与了判定（`node:空串` 认领了卡）")
        self.assertNotIn("kb_reuse", b, "空名节点参与了判定")
        self.assertEqual(a["definition"], "本次写的定义")
        self.assertEqual(stats["matched"], 0)
        self.assertEqual(stats["reusable"], 0)
        self.assertEqual(stats["searched"], 2)

        # 判定层自己也不许把空名节点列进 B 清单（那是"问了也不该问"）
        asked = []

        def spy_call(messages):
            asked.append(json.loads(messages[-1]["content"])["B"])
            return json.dumps({"pairs": []})

        self.assertEqual(kbreuse.judge([a], idx, spy_call), {})
        self.assertEqual(asked, [], "一条能问的都没有时不该发起模型调用")


class JudgeTest(IsolatedCase):
    """模型判定的输入校验：**只允许在库里已有项里选**，其余一律丢弃（spec §6）。"""

    def _cards(self):
        return [{"id": "语音识别", "term": "语音识别", "definition": "把语音转文字"},
                {"id": "词错率wer", "term": "词错率（WER）", "definition": "错字比例"}]

    def _nodes(self, *terms):
        return [{"id": store.slug(t), "term": t, "definition": "d"} for t in terms]

    def _reply(self, obj):
        return lambda messages: json.dumps(obj, ensure_ascii=False)

    def test_a_valid_pair_is_accepted(self):
        idx = kbreuse.Index(self._cards())
        out = kbreuse.judge(self._nodes("ASR"), idx, self._reply(
            {"pairs": [{"node": "ASR", "card_id": "语音识别"}]}))
        self.assertEqual(out, {"ASR": "语音识别"})

    def test_a_card_id_that_is_not_in_the_index_is_dropped(self):
        """模型**不许发明概念**：库里没有的 card_id 直接丢弃 —— 但**同一条回复里**合法的照活。

        ⚠️ 必须**混一条合法对**进来：全是坏元素的回复区分不了「丢坏的」与「整条全丢」，
        一个"见到坏元素就 `return {}`"的实现会让它假绿（而那个实现在生产上几乎全废：
        模型 5 对里 1 对胡诌 → 5 对全丢）。
        """
        idx = kbreuse.Index(self._cards())
        out = kbreuse.judge(self._nodes("ASR", "语音转写"), idx, self._reply(
            {"pairs": [{"node": "ASR", "card_id": "库里根本没有这个概念"},
                       {"node": "语音转写", "card_id": "语音识别"}]}))
        self.assertEqual(out, {"语音转写": "语音识别"},
                         "坏元素只丢它自己，合法的那些必须活下来（逐条过滤，spec §6）")

    def test_a_node_term_that_was_not_asked_about_is_dropped(self):
        """没问过的节点丢弃 —— 同样混一条合法对，逐条过滤。"""
        idx = kbreuse.Index(self._cards())
        out = kbreuse.judge(self._nodes("ASR"), idx, self._reply(
            {"pairs": [{"node": "我没问过的节点", "card_id": "语音识别"},
                       {"node": "ASR", "card_id": "词错率wer"}]}))
        self.assertEqual(out, {"ASR": "词错率wer"}, "合法的那条活下来")

    def test_a_non_dict_element_only_drops_that_element(self):
        """`pairs` 里单个元素不是 dict → **只丢那一条**（逐条过滤，spec §6）。"""
        idx = kbreuse.Index(self._cards())
        out = kbreuse.judge(self._nodes("ASR"), idx,
                            self._reply({"pairs": [1, "x", None, {},
                                                   {"node": "ASR", "card_id": "语音识别"}]}))
        self.assertEqual(out, {"ASR": "语音识别"},
                         "非 dict 元素只丢它自己，合法的必须活下来")

    def test_the_same_node_reported_twice_keeps_the_first_card(self):
        """同一个 `node` 被报了两张卡 → **第一个赢**（顺序稳定，绝不随机；M-5）。"""
        idx = kbreuse.Index(self._cards())
        out = kbreuse.judge(self._nodes("ASR"), idx, self._reply({"pairs": [
            {"node": "ASR", "card_id": "语音识别"},
            {"node": "ASR", "card_id": "词错率wer"}]}))
        self.assertEqual(out, {"ASR": "语音识别"}, "先出现的那张卡赢（后者不许覆盖前者）")

    def test_malformed_json_yields_nothing_at_all(self):
        idx = kbreuse.Index(self._cards())
        out = kbreuse.judge(self._nodes("ASR"), idx, lambda messages: "这不是 JSON")
        self.assertEqual(out, {}, "判不出来就当没命中，绝不猜")

    def test_a_wrong_shape_yields_nothing(self):
        idx = kbreuse.Index(self._cards())
        nodes = self._nodes("ASR")
        for bad in ({"pairs": "x"}, {"pairs": [1, 2]}, {"没有-pairs": []}, [], None):
            with self.subTest(bad=bad):
                self.assertEqual(kbreuse.judge(nodes, idx, self._reply(bad)), {})

    def test_two_nodes_claiming_one_card_keeps_only_the_first(self):
        """模型说"两个**不同**名字的节点都等于同一张卡"，多半是它认错了一个（spec §6）。"""
        idx = kbreuse.Index(self._cards())
        out = kbreuse.judge(self._nodes("ASR", "语音转写"), idx, self._reply({"pairs": [
            {"node": "ASR", "card_id": "语音识别"},
            {"node": "语音转写", "card_id": "语音识别"}]}))
        self.assertEqual(out, {"ASR": "语音识别"}, "只认第一个，其余去联网")

    def test_a_raising_call_is_swallowed_and_logged(self):
        """模型不可用 → 等于"没命中"，绝不因此让分析做不出来（spec §9），而且**要留痕**。

        ⚠️ 原来的用例**名字说 logged、却一条日志断言都没有** —— 名字里的承诺必须有牙：
        事件名 / level / 原文 / 块序号与总块数（"5 块坏 1 块"与"全坏"必须分得开，M-4）。
        """
        idx = kbreuse.Index(self._cards())

        def boom(messages):
            raise RuntimeError("模型炸了")

        self.assertEqual(kbreuse.judge(self._nodes("ASR"), idx, boom), {})
        evs = [r for r in log.read_logs(200)[0] if r.get("event") == "kbreuse.judge_error"]
        self.assertEqual(len(evs), 1, "模型炸了必须记一条 kbreuse.judge_error：%r" % (evs,))
        self.assertEqual(evs[0]["level"], "error")
        self.assertIn("模型炸了", evs[0]["data"]["error"])
        self.assertEqual(evs[0]["data"].get("chunk"), 1, "要记清是**哪一块**坏的")
        self.assertEqual(evs[0]["data"].get("chunks"), 1, "也要记总块数")

    def test_a_long_exception_message_is_truncated_before_it_lands_in_the_log(self):
        """HTTP 报错常带**整段 prompt / 回复** → `str(异常)` 截断后再落盘（M-4）。"""
        idx = kbreuse.Index(self._cards())
        huge = "回显：" + ("很长的回复" * 200)

        def boom(messages):
            raise RuntimeError(huge)

        self.assertEqual(kbreuse.judge(self._nodes("ASR"), idx, boom), {})
        evs = [r for r in log.read_logs(200)[0] if r.get("event") == "kbreuse.judge_error"]
        self.assertEqual(len(evs), 1)
        self.assertLessEqual(len(evs[0]["data"]["error"]), 300,
                             "异常原文原样落盘了（可能藏进整段用户内容）")
        self.assertTrue(evs[0]["data"]["error"], "截断不等于不记")

    def test_a_fenced_json_reply_is_parsed(self):
        """模型爱用 ```json 围栏 —— 走**同一个** `llm.parse_json_reply` 才不会整块白丢（M-8）。"""
        idx = kbreuse.Index(self._cards())
        reply = '```json\n{"pairs": [{"node": "ASR", "card_id": "语音识别"}]}\n```'
        self.assertEqual(kbreuse.judge(self._nodes("ASR"), idx, lambda m: reply),
                         {"ASR": "语音识别"})

    def test_a_reply_that_is_not_even_a_string_is_handled(self):
        idx = kbreuse.Index(self._cards())
        self.assertEqual(kbreuse.judge(self._nodes("ASR"), idx, lambda messages: None), {})
        self.assertEqual(kbreuse.judge(self._nodes("ASR"), idx, lambda messages: 5), {})

    def test_a_huge_library_is_chunked_and_still_judged(self):
        """超过 `MAX_JUDGE_TERMS` 时分块；分块后仍能判定（spec §6 规模守卫）。"""
        cards = [{"id": "c%d" % i, "term": "概念%d" % i, "definition": "d"}
                 for i in range(kbreuse.MAX_JUDGE_TERMS + 5)]
        cards.append({"id": "语音识别", "term": "语音识别", "definition": "d"})
        idx = kbreuse.Index(cards)
        seen = {"chunks": 0}

        def call(messages):
            seen["chunks"] += 1
            if "语音识别" in messages[-1]["content"]:
                return json.dumps({"pairs": [{"node": "ASR", "card_id": "语音识别"}]})
            return json.dumps({"pairs": []})

        out = kbreuse.judge(self._nodes("ASR"), idx, call)
        self.assertGreater(seen["chunks"], 1, "超限必须分块，不许悄悄截断")
        self.assertEqual(out, {"ASR": "语音识别"})

    def test_a_card_id_from_another_chunk_is_dropped(self):
        """`allowed` 必须是**当前这一块**的清单，不是全部（spec §6）—— 这条是"必须属于当前块"
        唯一有意义的断言（小库里 `allowed == 全部`，所以它必须真的分块才有牙）。

        同一条回复里：**别块的** card_id 必须丢，**本块的**合法对必须活 —— 一次钉住两个方向。
        """
        cards = [{"term": "块一概念%03d" % i, "definition": "d"}
                 for i in range(kbreuse.MAX_JUDGE_TERMS)]
        cards.append({"term": "块二概念", "definition": "d"})       # 落在第二块
        idx = kbreuse.Index(cards)
        self.assertEqual(len(idx), kbreuse.MAX_JUDGE_TERMS + 1)
        asked = []

        def call(messages):
            payload = json.loads(messages[-1]["content"])
            ids = {c["card_id"] for c in payload["A"]}
            asked.append(len(ids))
            if "块一概念000" in ids:            # 第一块：本块 + 别块各报一条
                return json.dumps({"pairs": [
                    {"node": "块二节点", "card_id": "块一概念000"},   # 本块的合法对 → 必须活
                    {"node": "块一节点", "card_id": "块二概念"}]},    # 别块的卡 → 必须丢
                    ensure_ascii=False)
            return json.dumps({"pairs": []})

        out = kbreuse.judge(self._nodes("块一节点", "块二节点"), idx, call)
        self.assertEqual(asked, [kbreuse.MAX_JUDGE_TERMS, 1], "夹具前提：真的是两块")
        self.assertEqual(out, {"块二节点": "块一概念000"},
                         "别块的 card_id 没被丢掉（`allowed` 用了全部 terms）")

    def test_the_chunk_size_is_exactly_max_judge_terms(self):
        """块大小语义：401 张 → **恰好 2 次**、每块最多 400 条（不是 401 次、也不是 1 次）。

        `MAX_JUDGE_TERMS` 是**硬编码常量**（当前没有配置入口），所以这里只钉行为、不加配置。
        """
        cards = [{"term": "概念%04d" % i, "definition": "d"}
                 for i in range(kbreuse.MAX_JUDGE_TERMS + 1)]
        idx = kbreuse.Index(cards)
        sizes = []

        def call(messages):
            sizes.append(len(json.loads(messages[-1]["content"])["A"]))
            return json.dumps({"pairs": []})

        self.assertEqual(kbreuse.judge(self._nodes("ASR"), idx, call), {})
        self.assertEqual(sizes, [kbreuse.MAX_JUDGE_TERMS, 1],
                         "块大小语义变了（每块最多 MAX_JUDGE_TERMS 条）")

    def test_no_nodes_means_no_call(self):
        """没有可问的节点 → **一次模型都不调**（req 1 的另一半：省额度）。"""

        def must_not_call(messages):
            raise AssertionError("没有可问的节点时不该发起模型调用")

        self.assertEqual(kbreuse.judge([], kbreuse.Index(self._cards()), must_not_call), {})

    def test_a_non_positive_chunk_size_never_escapes_the_judge(self):
        """`_chunks(size<=0)` 必须兜底成 1：`0` → `ValueError` 会**掀掉整步知识地图**（M-6）。

        它**不**在 `judge` 的那个 `try` 里（try 罩的是模型调用），所以这条必须单独钉。
        """
        self.assertEqual(list(kbreuse._chunks([1, 2, 3], 0)), [[1], [2], [3]])
        self.assertEqual(list(kbreuse._chunks([1, 2, 3], -5)), [[1], [2], [3]])
        with mock.patch.object(kbreuse, "MAX_JUDGE_TERMS", 0):
            out = kbreuse.judge(self._nodes("ASR"), kbreuse.Index(self._cards()),
                                self._reply({"pairs": [{"node": "ASR", "card_id": "语音识别"}]}))
        self.assertEqual(out, {"ASR": "语音识别"}, "块大小被兜成 1 之后照样要能判")

    def test_no_candidate_library_means_no_call(self):
        """库里没有条目时**一次模型都不调**（省额度）。"""

        def must_not_call(messages):
            raise AssertionError("空库不该发起模型调用")

        self.assertEqual(kbreuse.judge(self._nodes("ASR"), kbreuse.Index([]), must_not_call), {})


class ReuseTest(IsolatedCase):
    """阶段 E 的**执行者**：`Reuse(cfg).plan(nodes)` = 建目录 → 匹配（含一次模型判定）→ 统计。

    **头号契约是不许抛**（spec §9）：知识库读不出来 / 索引建不起来 / 模型判定失败，
    都必须如实落进 `stats`，让分析照常做完（联网兜底）。所以每条失败路径都同时盯着三件事：
    「不抛」「如实说」「节点没被复用」。
    """

    def _node(self, term="语音识别"):
        return {"id": store.slug(term), "term": term, "definition": "本次写的定义",
                "plain_explanation": "本次写的通俗解释", "category": "核心概念", "layer": 0,
                "sources": [], "timeline": [], "related": [], "interview_questions": [],
                "confidence": "ai-generated", "state": "待学习"}

    def _card(self, term="语音识别", **kw):
        c = {"id": kb.normalize_id(term), "term": term, "definition": "库里的定义",
             "plain_explanation": "库里的通俗解释", "category": "核心概念", "layer": 0,
             "sources": [{"url": "https://w"}], "timeline": [{"year": "2010", "text": "t"}],
             "from_jds": [{"id": "j1", "job_title": "岗位一", "date": "2025-01-02"}]}
        c.update(kw)
        return c

    def _cfg(self, **tm):
        model = {"base_url": "https://b/v1", "api_key": "k", "model": "m", "temperature": 0.3}
        model.update(tm)
        return {"text_model": model}

    def test_a_readable_base_is_planned_and_marked_used(self):
        """库读得出来：`used` 为真、节点被整条填上、`stats` 就是 `plan` 的返回值。"""
        store.save_knowledge({"nodes": [self._card()]})
        node = self._node()
        reuse = kbreuse.Reuse({})
        stats = reuse.plan([node])
        self.assertTrue(reuse.used, "跑过就要记下来 —— 报告据此区分「跑了但库里是空的」与「根本没跑」")
        self.assertEqual(stats["reusable"], 1)
        self.assertEqual(stats["knowledge_error"], "")
        self.assertEqual(node["definition"], "库里的定义", "够格的整条复用（spec 决策 5）")
        self.assertEqual(node["kb_reuse"]["matched_by"], "exact")
        self.assertEqual(reuse.stats, stats, "报告读的就是 `self.stats` 那份（同一个统计）")

    def test_a_broken_base_never_throws_and_is_reported(self):
        """知识库坏掉：`plan` **不抛**、如实写 `knowledge_error`、节点**没被复用**（退回联网）。"""
        with open(store.knowledge_path(), "wb") as f:
            f.write(b'{"nodes": [ <<<CORRUPT>>>')
        node = self._node()
        reuse = kbreuse.Reuse({})
        stats = reuse.plan([node])                      # 不许抛
        self.assertTrue(reuse.used, "库坏了也是「跑过」——报告要标的是那条 error，不是 cached")
        self.assertTrue(stats["knowledge_error"], "坏库必须如实说（报告要标 ⚠，spec §9）")
        self.assertIn("knowledge.json", stats["knowledge_error"], "原因里要说清是哪个文件")
        self.assertEqual(stats["reusable"], 0)
        self.assertEqual(stats["searched"], 1, "一条都没复用 → 这次照旧联网")
        self.assertNotIn("kb_reuse", node, "库读不出来时不许假装命中")
        self.assertEqual(node["definition"], "本次写的定义", "不许动节点内容")

    def test_a_config_without_a_usable_text_model_skips_the_judge_only(self):
        """`cfg` 里没有可用的文本模型 → `_judge_caller` 返回 `None` → **只做确定性匹配**。

        确定性匹配是**免费**的那一半，必须照跑（少了它这项功能就没意义了）；
        被省掉的只有那次模型判定。
        """
        for cfg in (None, {}, {"text_model": {}}, {"text_model": None},
                    {"text_model": {"base_url": "b", "api_key": "k"}},        # 缺 model
                    {"text_model": {"base_url": "b", "model": "m"}},          # 缺 api_key
                    {"text_model": {"api_key": "k", "model": "m"}}):           # 缺 base_url
            self.assertIsNone(kbreuse._judge_caller(cfg), "不该给出可用的调用器：%r" % (cfg,))
        self.assertTrue(callable(kbreuse._judge_caller(self._cfg())),
                        "配好了文本模型时**必须**给出调用器（否则模型判定永远跑不到）")

        store.save_knowledge({"nodes": [self._card()]})
        node = self._node()
        seen = []

        def spy(*a, **kw):
            seen.append(a)
            return {}

        with mock.patch.object(kbreuse, "judge", spy):
            stats = kbreuse.Reuse({}).plan([node])
        self.assertEqual(seen, [], "没有可用文本模型时不许调判定")
        self.assertEqual(stats["reusable"], 1, "确定性命中照样复用")
        self.assertEqual(node["definition"], "库里的定义")

    def test_a_configured_text_model_gets_one_judge_call_that_can_rescue_a_miss(self):
        """配好文本模型时，那次判定要**真的发生**，并且能救回确定性匹配认不出的同义写法。

        节点「ASR」与库里那张卡（term「语音识别」、无 aliases）确定性**不可能**对上 ——
        只有模型判定能把它认出来。所以这条同时是 `Reuse.plan` 里 `call=...` 的牙：
        不传 `call`，这里 `reusable` 就会掉到 0（实测该变异全量红）。
        """
        store.save_knowledge({"nodes": [self._card()]})
        node = self._node("ASR")
        seen = []

        def fake_chat(base_url, api_key, model, messages, **kw):
            seen.append((base_url, api_key, model, messages, kw))
            return json.dumps({"pairs": [{"node": "ASR", "card_id": "语音识别"}]},
                              ensure_ascii=False)

        with mock.patch.object(kbreuse.llm, "call_openai_compatible", fake_chat):
            stats = kbreuse.Reuse(self._cfg()).plan([node])
        self.assertEqual(stats["reusable"], 1, "配好的模型判定必须被用来认同义写法")
        self.assertEqual(node["kb_reuse"]["matched_by"], "assisted")
        self.assertEqual(node["term"], "语音识别", "assisted 也要做身份对齐（spec §7）")
        self.assertEqual(len(seen), 1, "一次分析只判一次")
        self.assertEqual(seen[0][:3], ("https://b/v1", "k", "m"), "走的是 cfg 里那个文本模型")
        self.assertEqual(seen[0][4].get("temperature"), 0.3, "温度沿用配置")
        self.assertTrue(seen[0][4].get("json_mode"),
                        "判定也是「只要 JSON」的调用 —— 与 knowledge._call 同一套（json_mode=True）")
        self.assertIn("知识库管理员", seen[0][3][0]["content"])

    def test_a_raising_model_call_is_swallowed_into_the_stats(self):
        """模型判定失败（网络/额度/返回畸形）→ **绝不抛**，等于"没命中"，这次退回联网。"""
        store.save_knowledge({"nodes": [self._card()]})
        node = self._node("ASR")

        def boom(*a, **kw):
            raise RuntimeError("模型炸了")

        with mock.patch.object(kbreuse.llm, "call_openai_compatible", boom):
            stats = kbreuse.Reuse(self._cfg()).plan([node])      # 不许抛
        self.assertEqual(stats["knowledge_error"], "")
        self.assertEqual(stats["reusable"], 0, "判不出来就当没命中，绝不猜")
        self.assertEqual(stats["searched"], 1)
        self.assertNotIn("kb_reuse", node)

    def test_a_missing_temperature_falls_back_to_a_default_and_never_passes_none(self):
        """`temperature` 缺失/为 `None`/为 `0` 时一律退回 0.3 —— 与 `knowledge._call` 同一条口径。

        往模型接口送 `None` 是另一套语义（"不带这个参数"），而这个字段在配置里是可选装饰；
        口径是 `float(tm.get("temperature") or 0.3)`（既有写法就这么写，不另造一套）。
        """
        store.save_knowledge({"nodes": [self._card(term="别的概念")]})     # 造一个"未命中"让判定跑起来
        seen = {}

        def fake_chat(base_url, api_key, model, messages, **kw):
            seen.update(kw)
            return json.dumps({"pairs": []})

        for extra in ({}, {"temperature": None}, {"temperature": 0}, {"temperature": "0.3"}):
            tm = {"base_url": "b", "api_key": "k", "model": "m"}
            tm.update(extra)
            seen.clear()
            with mock.patch.object(kbreuse.llm, "call_openai_compatible", fake_chat):
                kbreuse.Reuse({"text_model": tm}).plan([self._node("没见过")])
            want = 0.3      # 四种输入都该送 0.3（"0.3" 转成 float）
            self.assertEqual(seen.get("temperature"), want,
                             "temperature=%r 时该送出 %r" % (extra, want))
            self.assertIsInstance(seen.get("temperature"), float, "不许把 None / int 原样送出去")


class _WriteBackCase(IsolatedCase):
    """写回安全两个测试类的**公共夹具**（不是测试类：这里没有 `test_*` 方法）。

    夹具一律走生产 API 造数据：`kb.absorb` 种卡（形状因此与线上完全一致，含
    `clean_sources` 补出来的 5 个键、`confidence`、`first_seen/last_seen`）、
    `kb.merge_nodes` 造别名、`kb.record_review` / `kb.record_interview` 造用户进度
    —— **手写 dict 造不出这些形状**，而"逐字节不变"这种断言最怕夹具自己形状不对。
    """

    JD_OLD = {"id": "老岗位", "job_title": "语音数据质检", "company": "老公司", "date": "2026-10-03"}
    JD_NEW = {"id": "新岗位", "job_title": "语音算法工程师", "company": "新公司", "date": "2026-10-15"}

    def _seed_node(self, term, **kw):
        """喂给**真** `kb.absorb` 的一份"知识节点"（形状照 `knowledge.build_for_jd` 的产出）。"""
        n = {"term": term, "definition": "库里的定义", "plain_explanation": "库里的通俗",
             "category": "核心概念", "layer": 0,
             "sources": [{"title": "维基", "url": "https://w"}],
             "timeline": [{"year": "2010", "text": "库里的时间线"}],
             "related": [], "interview_questions": ["会怎么问"]}
        n.update(kw)
        return n

    def _seed(self, term="语音识别", **kw):
        """用真 `kb.absorb` 种一张卡，返回**从盘上重新读出来**的那张（与内存里那份不是同一对象）。"""
        kb.absorb([self._seed_node(term, **kw)], self.JD_OLD)
        cid = kb.normalize_id(term)
        cards = [c for c in store.load_knowledge()["nodes"] if c.get("id") == cid]
        self.assertEqual(len(cards), 1, "夹具前提：库里正好一张 %s" % cid)
        return cards[0]

    def _hand_card(self, term, **kw):
        """**手改形态**的 knowledge.json 卡片：`term` 可以带尾随空格。

        整个文件里**只有这一处**不走真 `kb.absorb` —— 真路径会把 term `.strip()`
        （`kb._new_card` 第 239 行），造不出"卡片 term 带尾随空格"这种用户手改出来的形态；
        而 `kb.absorb` 第 373 行读的恰恰是 `node["term"].strip()`。卡片其余字段照生产形态写。
        """
        c = {"id": kb.normalize_id(term), "term": term, "aliases": [],
             "definition": "库里的定义", "plain_explanation": "库里的通俗",
             "category": "核心概念", "layer": 0,
             "sources": [{"title": "维基", "url": "https://w", "date": "", "accessed": "", "snippet": ""}],
             "timeline": [{"year": "2010", "text": "库里的时间线"}],
             "related": [], "interview_questions": [],
             "from_jds": [dict(self.JD_OLD)],
             "state": "已掌握", "last_outcome": "答上了", "asked_count": 1,
             "review_count": 3, "last_reviewed_at": "2026-10-01", "last_result": "答上了",
             "first_seen": "2026-10-03", "last_seen": "2026-10-03", "confidence": "verified"}
        c.update(kw)
        return c

    def _seed_full_card(self):
        """一张**生产形态**的满卡：内容 + 别名（真 `merge_nodes`）+ 用户进度（真 `record_review`）。

        用户的进度字段不是编出来的：`record_review` 写 state / review_count / last_reviewed_at /
        last_result，`record_interview` 写 asked_count / last_outcome / last_interview_jd。
        """
        self._seed("语音识别")
        self._seed("语音辨识")
        ok, msg = kb.merge_nodes("语音识别", "语音辨识")     # 真路径：旧写法记进 aliases
        self.assertTrue(ok, msg)
        kb.record_review("语音识别", kb.RESULT_UP)
        kb.record_interview(self.JD_OLD["id"], asked=["语音识别"], good=["语音识别"])
        cards = store.load_knowledge()["nodes"]
        self.assertEqual(len(cards), 1, "夹具前提：drop 卡已被并走，只剩一张")
        return cards[0]

    def _node(self, term="语音识别", **kw):
        """本次分析产出的一个知识点节点（形状照 knowledge 的产出）。

        `related` 为空表是**刻意**的：`_COPY_FIELDS` 不含 `related`（kbreuse.py:276-289），
        复用节点保留本次自己的 related —— 而 `kb._merge_into` 第 294 行会把它并进库里那张卡。
        但这条**与"复用"无关**：② 头脑风暴跑在 `reuse.plan` **之前**（knowledge.py:390-393），
        所以"不复用的同一次分析"会把**同一批** related 并进**同一张**卡（`kb.clean_related`
        第 192-203 行按 id 去重）。夹具取空表，是为了把这条逐字节不变量只指向**复用引入的差异**：
        related 的并集复用与不复用都一样会发生，掺进来只会盖住真正的信号。
        """
        n = {"id": store.slug(term), "term": term, "definition": "本次写的定义",
             "plain_explanation": "本次写的通俗解释", "category": "核心概念", "layer": 0,
             "sources": [], "timeline": [], "related": [], "interview_questions": [],
             "confidence": "ai-generated", "state": "待学习"}
        n.update(kw)
        return n

    @staticmethod
    def _frozen(card, skip=("from_jds", "last_seen")):
        """整张卡的**逐字节**快照（保留字段顺序 → 多一个键、挪一次顺序都会露出来）。"""
        return json.dumps({k: v for k, v in card.items() if k not in skip}, ensure_ascii=False)


class WriteBackSafetyTest(_WriteBackCase):
    """写回安全 —— spec §7 的**强不变量**：复用并写回之后，库里那张卡**只该多一条 `from_jds`、
    只该动 `last_seen`**，其余每个字段逐字节不变。

    为什么非要用**真的 `kb.absorb`**：这条不变量横跨两个模块的缝 —— `kbreuse` 负责"认出是哪张卡、
    把库里的内容抄进节点"，`kb` 负责"合并进库"。任何一侧的替身都会把缝糊掉，而"复用一次就把
    库里那张卡改脏"正是这条缝上的事故。

    生产路径（`app.do_analyze`，app.py:1365-1427）：`jdfacts.resolve(builder=_builder_with_reuse(reuse))`
    里 `reuse.plan(nodes)`（= `plan_nodes`）就地填节点、写 `kb_reuse` 标记 → ③④ 只对**没复用**的
    节点补内容 → 最后 `kb.absorb(know_nodes, {JD id, ...})`。本类的用例就是这条路径的后半段
    （plan → absorb），不驱动 HTTP。

    **逐条对照的真实实现**（行号 = 本仓库当前 `kb.py` / `kbreuse.py`，按字段列出）：
      * `sources`：并集，`kb.clean_sources` 按 url 去重（kb.py:276）—— 复用节点带的是卡里那份
        （`_fill_from_card` 用 `kb.clean_sources` 抄过去，kbreuse.py:288-299、335），并集 = 原样；
      * `timeline`：**只取更长的**（kb.py:278-280）→ 等长保留原值；
      * `definition` / `plain_explanation`：**严格更长才换**（kb.py:283-286）→ 等长不换；
      * `category`：仅当卡里为空才补（kb.py:288-289）→ 非空时一个字不动；
      * `layer`：`min`（kb.py:292）。复用侧 `_fill_from_card` 第 343-344 行先把节点的 layer
        夹成 `min(节点的, 卡的)` —— ⚠️ **若 JD 这次把这个概念归到更基础的层**（节点的 layer
        比卡小），`min` 会把卡的 layer **拉低**：这是 `kb._merge_into` 自己的规则，与"复用"无关
        （同一概念不复用地重算一次，同样会拉低）。临时探针实测：卡的 layer 2 + 复用节点 layer 0
        → 写回后卡的 layer = 0，且除 `from_jds`/`last_seen` 外**只有** `layer` 这一个字段变了。
        夹具因此取等值 layer，前提就写在这里；
        ⚠️ **注释不算钉住**（M-1）：这两条规则各自有一条显式测试 ——
        `test_the_layer_is_the_kb_minimum_rule_not_a_reuse_side_effect`（且两头都验：
        复用 / 不复用同一条规则）；
      * `related` / `interview_questions`：并集（kb.py:294-296）—— 见 `_node` 的注释；
        ⚠️ `related` 的并集同样有显式测试（M-1）：
        `test_related_is_unioned_by_the_kb_rule_and_the_reuse_side_copies_none`；
      * 用户进度 `state` / `last_outcome` / `asked_count`：**只补空、绝不覆盖真值**（kb.py:302-304），
        这是 kb.py 文件头铁律 1；
      * `aliases` / `term` / `id` / `first_seen`：`_merge_into`（kb.py:271-306）压根不碰
        （`first_seen` 只有第 305 行的 `setdefault` 补缺，真卡早就有）；
      * `confidence`：kb.py:383 的 `_recompute_confidence` 会重算（有来源 → verified），
        对本来就有来源的卡是同一个值；
      * `from_jds`：按 id 去重追加一条（kb.py:215-235）；`last_seen` 更新（kb.py:306）。
    """

    def test_reuse_writeback_changes_only_the_refs_and_last_seen(self):
        """不变量 1：够格复用（`got == "all"`）写回后，库里那张卡**只有** `from_jds` 多一条、
        `last_seen` 更新 —— 其余每个字段**逐字节不变**（spec §7 / §11 第 5 条）。"""
        self._seed_full_card()
        # 把 last_seen 改成旧日期：不变量说它**允许**变 —— 夹具必须让它真的变过，那句话才不是空话
        data = store.load_knowledge()
        data["nodes"][0]["last_seen"] = "2020-01-01"
        store.save_knowledge(data)
        before = copy.deepcopy(store.load_knowledge()["nodes"][0])

        node = self._node()
        # 节点自带一份"本次搜到/本次算的"（步 ③④ 的产出）：够格复用后必须被库里那份整条替换
        node["sources"] = [{"title": "本次搜到的", "url": "https://this-run"}]
        node["timeline"] = [{"year": "2026", "text": "本次算的"}]

        stats = kbreuse.plan_nodes([node], kbreuse.Index(store.load_knowledge()["nodes"]))
        self.assertEqual(stats["matched"], 1, "夹具前提：命中")
        self.assertEqual(stats["reusable"], 1, "夹具前提：够格复用")
        self.assertEqual(node["kb_reuse"]["got"], "all", "夹具前提：卡里有时间线 → got=all")

        added, merged = kb.absorb([node], self.JD_NEW)          # ← 真的 kb.absorb，不是替身
        self.assertEqual((added, merged), (0, 1), "够格复用的写回是「并进那张卡」，不是新建")

        cards = store.load_knowledge()["nodes"]
        self.assertEqual(len(cards), 1, "卡片数不变（不变量 2）")
        after = cards[0]

        # ① 逐字段：除 from_jds / last_seen 之外，一个都不许不同（哪个字段归哪条规则见类 docstring）
        changed = sorted(k for k in set(before) | set(after)
                         if k not in ("from_jds", "last_seen") and before.get(k) != after.get(k))
        self.assertEqual(changed, [], "这些字段被写回改了：%s" % changed)
        # ② 逐字节：连**字段顺序**与"多出来的键"也不许变（比逐字段更硬，挡"悄悄加一个字段"）
        self.assertEqual(self._frozen(after), self._frozen(before), "整张卡（除那两处）必须逐字节不变")
        # ③ 用户进度字段单独再钉：不只要"没变"，还要"就是原来那些值"（spec §10.4 第二条）
        self.assertEqual(
            (after["state"], after["last_outcome"], after["asked_count"],
             after["review_count"], after["last_reviewed_at"], after["last_result"],
             after.get("last_interview_jd")),
            ("已掌握", "答上了", 1, 1, kb._today(), kb.RESULT_UP, self.JD_OLD["id"]),
            "用户进度字段必须原样留着（kb.py:302-304 + 文件头铁律 1）")
        # ④ 允许变的两处：from_jds 追加一条（kb._add_jd）、last_seen 更新（kb.py:306）
        self.assertEqual([j["id"] for j in after["from_jds"]],
                         [self.JD_OLD["id"], self.JD_NEW["id"]], "只多这一条，且追加在尾部")
        self.assertEqual(after["from_jds"][:-1], before["from_jds"], "旧的来源岗位条目原样保留")
        self.assertEqual(after["from_jds"][-1], dict(self.JD_NEW),
                         "记的是这次那个岗位（id / job_title / company / date 四项）")
        self.assertEqual(before["last_seen"], "2020-01-01")
        self.assertEqual(after["last_seen"], kb._today(), "last_seen 允许变，而且必须被更新")

    def test_the_node_progress_never_overwrites_the_card_progress(self):
        """`kb._merge_into` 第 302-304 行**只补空、绝不覆盖已有真值**（kb.py 文件头铁律 1）。

        ⚠️ **为什么夹具要让两边相反**：在"够格复用"那条路上，`_fill_from_card` 已经把卡里的
        `state` 抄到节点上（kbreuse.py:288-289 的 `_COPY_FIELDS` 含 `state`），两边必然相同 ——
        覆盖了也看不出来。只有节点自带一份与库里**相反**的进度时，"永不触碰"这句话才有牙；
        生产上这份相反进度来自"命中但不够格"那条路（那条路上 `_fill_from_card` 不跑）。
        """
        self._seed("语音识别", sources=[], timeline=[])
        node = self._node(state="已掌握", last_outcome="答上了", asked_count=9,
                          definition="本次定义", plain_explanation="本次通俗")
        added, merged = kb.absorb([node], self.JD_NEW)
        self.assertEqual((added, merged), (0, 1), "库里已有这张卡 → 并进去")
        after = store.load_knowledge()["nodes"][0]
        self.assertEqual(after["state"], "待学习", "节点自称「已掌握」也不许覆盖库里的状态")
        self.assertEqual(after["last_outcome"], "未面试", "同理（kb.py:300 的 or 只补空）")
        self.assertEqual(after["asked_count"], 0, "同理（kb.py:301 只做 store.as_int 归一，不取值）")
        self.assertEqual(after["definition"], "库里的定义", "更短的定义不许顶掉库里的（kb.py:279-283）")

    def test_an_incomplete_hit_is_completed_in_place_and_never_duplicated(self):
        """不变量 3：命中但**不够格**（缺来源）时写回是"就地补全"，逐条对照 `kb._merge_into`。

        生产上这条对应"③④ 照样联网，搜到的东西并进原来那张卡"（spec §1 决策 3 / §3）。
        """
        self._seed("语音识别", definition="库里的定义（很长的那一版）", plain_explanation="库里的通俗",
                   sources=[], timeline=[])
        before = copy.deepcopy(store.load_knowledge()["nodes"][0])
        reusable, missing = kbreuse._card_completeness(before)
        self.assertFalse(reusable, "夹具前提：缺来源 → 不够格")
        self.assertIn("sources", missing)
        self.assertIn("timeline", missing)

        node = self._node(definition="短定义",                                    # 比库里的**短**
                          plain_explanation="本次搜到的通俗解释，比库里的那句长得多",  # **严格更长**
                          state="已掌握", last_outcome="答上了", asked_count=9)      # 与库里相反
        stats = kbreuse.plan_nodes([node], kbreuse.Index(store.load_knowledge()["nodes"]))
        self.assertEqual((stats["matched"], stats["reusable"], stats["enriched"]), (1, 0, 1))
        self.assertEqual(node["kb_reuse"]["got"], "", "不够格 → 没有 got=all（报告不许说「整条复用」）")
        self.assertEqual(node["definition"], "短定义",
                         "不够格时 `_fill_from_card` 不跑 → 节点内容一个字没动")

        # 模拟 ③④ 联网之后拿到的东西（形状与生产一致）
        node["sources"] = [{"title": "新来源", "url": "https://new"}]
        node["timeline"] = [{"year": "2024", "text": "本次补算"}]

        added, merged = kb.absorb([node], self.JD_NEW)
        self.assertEqual((added, merged), (0, 1), "不够格的写回必须**并进那张已有卡**，不是新建")
        cards = store.load_knowledge()["nodes"]
        self.assertEqual(len(cards), 1, "卡片数不变（没有重复卡）")
        after = cards[0]
        self.assertEqual((after["id"], after["term"]), (before["id"], before["term"]), "还是原来那张卡")

        # sources：并集（kb.py:276）—— 原来一条没有 → 等于这次搜到的（走 kb 自己的清洗尺子）
        self.assertEqual(after["sources"], kb.clean_sources(node["sources"]))
        self.assertEqual([s["url"] for s in after["sources"]], ["https://new"], "搜到的来源填上了")
        self.assertEqual(after["confidence"], "verified", "有来源了 → kb.py:383 重算 confidence")
        # timeline：**只取更长的**（kb.py:278-280）→ 新的更长，写进去
        self.assertEqual(after["timeline"], [{"year": "2024", "text": "本次补算"}])
        # definition：**严格更长才换**（kb.py:283-286）→ 本次的更短，留库里的
        self.assertEqual(after["definition"], before["definition"], "更短的定义不许顶掉库里的")
        # plain_explanation：同一条规则的另一半 —— 本次的严格更长 → 换
        self.assertEqual(after["plain_explanation"], node["plain_explanation"])
        self.assertNotEqual(after["plain_explanation"], before["plain_explanation"])
        # category：库里非空 → 一个字不动（kb.py:288-289）
        self.assertEqual(after["category"], before["category"])
        # 进度字段：**永不触碰**（kb.py:302-304）—— 节点那份相反进度不许覆盖
        self.assertEqual((after["state"], after["last_outcome"], after["asked_count"]),
                         ("待学习", "未面试", 0), "库里的进度是库里的（kb.py 铁律 1）")
        # from_jds：+1（kb.py:215-235）
        self.assertEqual([j["id"] for j in after["from_jds"]],
                         [self.JD_OLD["id"], self.JD_NEW["id"]])

    def test_a_miss_creates_exactly_one_new_card_with_one_reference(self):
        """不变量 4：**没命中**的写回新建一张卡，`from_jds` 恰好一条；库里别的卡一个字都不动。"""
        self._seed("语音识别")                    # 库里有一张**不相干**的卡（空索引会早退，验不到"没命中"）
        untouched_before = copy.deepcopy(store.load_knowledge()["nodes"][0])

        node = self._node("梯度裁剪")
        node["sources"] = [{"title": "新来源", "url": "https://new"}]
        node["timeline"] = [{"year": "2019", "text": "本次算的"}]
        stats = kbreuse.plan_nodes([node], kbreuse.Index(store.load_knowledge()["nodes"]))
        self.assertEqual((stats["matched"], stats["reusable"], stats["searched"]), (0, 0, 1))
        self.assertNotIn("kb_reuse", node, "没命中就没有复用标记（报告不许说查过库还命中了）")

        added, merged = kb.absorb([node], self.JD_NEW)
        self.assertEqual((added, merged), (1, 0), "没命中 → 新建一张卡")
        cards = store.load_knowledge()["nodes"]
        self.assertEqual(len(cards), 2)
        new = [c for c in cards if c["id"] == kb.normalize_id("梯度裁剪")]
        self.assertEqual(len(new), 1, "新卡身份键 = 术语归一化（kb._new_card 第 241 行）")
        new = new[0]
        self.assertEqual(len(new["from_jds"]), 1, "from_jds **恰好一条**")
        self.assertEqual(new["from_jds"][0], dict(self.JD_NEW))
        self.assertEqual(new["term"], "梯度裁剪")
        self.assertEqual(new["sources"], kb.clean_sources(node["sources"]))
        self.assertEqual(new["timeline"], kb.clean_timeline(node["timeline"]))
        self.assertEqual((new["state"], new["last_outcome"]), ("待学习", "未面试"),
                         "新卡的进度是默认值（kb._new_card 第 253-255 行）")
        self.assertEqual((new["asked_count"], new["review_count"]), (0, 0))
        self.assertEqual((new["first_seen"], new["last_seen"]), (kb._today(), kb._today()))
        self.assertEqual(new["confidence"], "verified", "有来源 → verified（kb.py:267-268）")
        # 原来那张卡**整张**（连 last_seen 都不排除）没被碰 —— 没命中的写回与它无关
        old = [c for c in cards if c["id"] == kb.normalize_id("语音识别")][0]
        self.assertEqual(self._frozen(old, skip=()), self._frozen(untouched_before, skip=()),
                         "没命中的写回不许碰别的卡（连 last_seen 都不许）")

    def test_the_layer_is_the_kb_minimum_rule_not_a_reuse_side_effect(self):
        """M-1 第一条：`layer` 取 **min** 是 `kb._merge_into`（kb.py:292）**自己的**规则，
        **不是**"复用把库里的卡改脏"。

        上一轮那条逐字节不变量（`test_reuse_writeback_changes_only_the_refs_and_last_seen`）
        的夹具取了**等值** layer，于是这条规则被夹具绕开了 —— 只有注释，没有牙。
        这里把它显式钉住，并且**两头都验**（复用 / 不复用同一条规则），
        证明它是 kb 的合并行为，而不是复用引入的差异。
        """
        # ① 复用路径：卡的 layer 更大（2）、本次节点更小（0）→ 写回后取 min = 0
        self._seed("语音识别", layer=2)
        data = store.load_knowledge()
        data["nodes"][0]["last_seen"] = "2020-01-01"
        store.save_knowledge(data)
        before = copy.deepcopy(store.load_knowledge()["nodes"][0])
        self.assertEqual(before["layer"], 2, "夹具前提：库里的层更大")
        node = self._node(layer=0)
        stats = kbreuse.plan_nodes([node], kbreuse.Index(store.load_knowledge()["nodes"]))
        self.assertEqual((stats["matched"], stats["reusable"]), (1, 1), "夹具前提：够格复用")

        added, merged = kb.absorb([node], self.JD_NEW)
        self.assertEqual((added, merged), (0, 1))
        after = store.load_knowledge()["nodes"][0]
        self.assertEqual(after["layer"], 0, "layer 取 min（kb.py:292）：卡的 2 与节点的 0 取小的")
        changed = sorted(k for k in set(before) | set(after)
                         if k not in ("from_jds", "last_seen") and before.get(k) != after.get(k))
        self.assertEqual(changed, ["layer"],
                         "复用路径上**唯一**被改的内容字段就是 layer（其余见不变量 1 那条）")

        # ② 这条规则与复用**无关**：同一个概念不复用地重算一次（直接喂真 `kb.absorb` 一个节点，
        #    整步 `plan_nodes` 都不跑），kb 的 min 照样把它拉低。
        self._seed("梯度裁剪", layer=3)
        raw = self._seed_node("梯度裁剪", layer=1)      # 形状与 knowledge 的产出一致
        added, merged = kb.absorb([raw], self.JD_NEW)
        self.assertEqual((added, merged), (0, 1))
        cards = {c["id"]: c for c in store.load_knowledge()["nodes"]}
        self.assertEqual(cards[kb.normalize_id("梯度裁剪")]["layer"], 1,
                         "不复用也一样取 min → 这条 min 属于 kb，不属于复用")

    def test_related_is_unioned_by_the_kb_rule_and_the_reuse_side_copies_none(self):
        """M-1 第二条：`related` 会**并集**（kb.py:294），且并进去的是**节点自己**那份 ——
        `_COPY_FIELDS`（kbreuse.py:288）刻意不含 `related`。前提同样是"这是 kb 的规则"。

        上一轮的夹具取了空 related 并只写注释（见 `_node` 的 docstring），这条把规则钉住：
        并集的是节点那份、卡的旧值也留着（这里卡里是空的，所以并集 == 节点那份）。
        """
        rel = [{"id": "另一个概念的节点", "relation": "先修"}]
        # ① 复用路径：节点自己的 related 在写回时并进库里那张卡
        self._seed("语音识别")
        before = copy.deepcopy(store.load_knowledge()["nodes"][0])
        self.assertEqual(before["related"], [], "夹具前提：库里那张卡的 related 是空的")
        node = self._node(related=copy.deepcopy(rel))
        stats = kbreuse.plan_nodes([node], kbreuse.Index(store.load_knowledge()["nodes"]))
        self.assertEqual(stats["reusable"], 1, "夹具前提：够格复用")
        self.assertEqual(node["related"], rel,
                         "复用**不复制**卡里的 related —— 节点留着本次自己那份")

        added, merged = kb.absorb([node], self.JD_NEW)
        self.assertEqual((added, merged), (0, 1))
        after = store.load_knowledge()["nodes"][0]
        self.assertEqual(after["related"], kb.clean_related(rel), "related 并集（kb.py:294）")

        # ② 与复用无关：不复用地重算同一概念，节点那份 related 一样会被并进去
        self._seed("梯度裁剪")
        raw = self._seed_node("梯度裁剪", related=copy.deepcopy(rel))
        kb.absorb([raw], self.JD_NEW)
        cards = {c["id"]: c for c in store.load_knowledge()["nodes"]}
        self.assertEqual(cards[kb.normalize_id("梯度裁剪")]["related"], kb.clean_related(rel),
                         "不复用也一样并集 → 这条并集属于 kb，不属于复用")

    def test_the_reused_node_carries_the_library_sources_not_this_search(self):
        """不变量 5：复用节点上的来源**就是库里那份**（值相等），本次搜索的东西一条都不许留下。

        为什么要单独钉：报告读的是**节点**上的 `sources`，而"库里的来源"与"本次搜到的来源"
        长得一模一样 —— 混进去看不出来（spec §10.4 / §11 第 3 条）。
        """
        self._seed("语音识别")
        before = copy.deepcopy(store.load_knowledge()["nodes"][0])
        node = self._node()
        node["sources"] = [{"title": "本次搜到的", "url": "https://this-run"}]
        node["timeline"] = [{"year": "2026", "text": "本次算的"}]

        stats = kbreuse.plan_nodes([node], kbreuse.Index(store.load_knowledge()["nodes"]))
        self.assertEqual(stats["reusable"], 1, "夹具前提：够格复用")
        self.assertEqual(node["sources"], before["sources"], "来源 = 库里那份（值相等即可）")
        self.assertEqual([s["url"] for s in node["sources"]], ["https://w"])
        self.assertNotIn("https://this-run", [s.get("url") for s in node["sources"]],
                         "本次搜索的东西不许留在复用节点里")
        self.assertEqual(node["timeline"], before["timeline"], "时间线同理：本次那份被整条替掉")

        kb.absorb([node], self.JD_NEW)
        self.assertEqual(node["sources"], store.load_knowledge()["nodes"][0]["sources"],
                         "写回之后节点与库里的来源仍然一致（不变量 5 的原话）")


class WriteBackIdentityTest(_WriteBackCase):
    """不变量 2：写回**必须认出库里原来那张卡**（卡片数不变），而"认出"的尺子是
    `kb.normalize_id`（kb.py:70-80），**不是**字面相等。

    ⚠️ **为什么"必须用 normalize_id"这句话要单独钉**：写回的落点判据在 `kb.absorb` 第 373 行
    —— `normalize_id(node["term"].strip())` 与索引里的 `card["id"]` 比。下面四种**真实存在**的
    形态里，节点名与卡片 id **字面不等**，归一化后却相等，而 `kb.absorb` 在这四种形态下都是
    `added=0, merged=1`（并进那张卡）。拿字面比去写断言会**误报**（把"并进了原来那张卡"
    报成"多出一张重复卡"）：
      * **大小写**：`kb.normalize_id` 第 80 行的 `.lower()`；
      * **全角**：同行的 `unicodedata.normalize("NFKC", …)`；
      * **尾随空格**：`kb.absorb` 第 373 行先 `.strip()` 再归一；
      * **头名键命中**：`kbreuse._fill_from_card` 第 350 行把节点改名成**卡里的规范名**，
        而那张卡的 `id` 是"全名去掉括号"的归一化结果（`kb._new_card` 第 241 行）——
        所以节点名（`词错率（WER）`）与卡片 id（`词错率wer`）字面永远不等。
    """

    def _write_back_and_check(self, case, term, jd_term, seeded=True):
        """种一张卡 → 够格复用 → **真** `kb.absorb` 写回 → 断言只有原来那张卡动了。"""
        if seeded:
            self._seed(term)                 # 真路径种卡（生产形态）
        else:
            store.save_knowledge({"nodes": [self._hand_card(term)]})
        before = copy.deepcopy(store.load_knowledge()["nodes"])
        self.assertEqual(len(before), 1, "%s：夹具前提是库里只有一张卡" % case)

        node = self._node(jd_term)
        stats = kbreuse.plan_nodes([node], kbreuse.Index(store.load_knowledge()["nodes"]))
        self.assertEqual(stats["matched"], 1, "%s：夹具前提是命中" % case)
        self.assertEqual(node["kb_reuse"]["got"], "all", "%s：夹具前提是够格复用" % case)

        added, merged = kb.absorb([node], self.JD_NEW)
        self.assertEqual((added, merged), (0, 1),
                         "%s：写回要并进那张卡（按字面找卡会新建一张重复卡）" % case)

        after = store.load_knowledge()["nodes"]
        self.assertEqual(len(after), len(before), "%s：卡片数前后相同" % case)
        self.assertEqual(len(after), 1, "%s：库里还是那一张卡，没有重复卡" % case)
        # **身份必须用归一化口径比**（四种形态见类 docstring）
        same = [c for c in after
                if kb.normalize_id(c.get("term")) == kb.normalize_id(node["term"])]
        self.assertEqual(len(same), 1,
                         "%s：写回必须落在原来那张卡上（按 kb.normalize_id 找；字面比会误报）" % case)
        self.assertEqual(after[0]["id"], before[0]["id"], "%s：卡片的 id 一个字没动" % case)
        self.assertEqual(after[0]["term"], before[0]["term"], "%s：卡片的 term 一个字没动" % case)
        self.assertEqual([j["id"] for j in after[0]["from_jds"]],
                         [self.JD_OLD["id"], self.JD_NEW["id"]],
                         "%s：那张卡只多了一条 from_jds" % case)
        # 这条用例的前提，也是"字面比会误报"的直接证据：节点名与卡片 id **字面不等**、
        # 归一化后相等 —— 而 `kb.absorb` 第 373 行正是用后者找卡的。
        self.assertNotEqual(node["term"], before[0]["id"],
                            "%s：夹具前提是字面不等（否则这条用例测不到归一化）" % case)
        self.assertEqual(kb.normalize_id(node["term"]), before[0]["id"],
                         "%s：归一化后必须相等（kb.absorb 第 373 行的落点判据）" % case)

    def test_identity_holds_when_only_the_letter_case_differs(self):
        """大小写：库里 `ASR`（id `asr`）、JD 里写 `asr`。"""
        self._write_back_and_check("大小写", "ASR", "asr")

    def test_identity_holds_when_only_the_width_differs(self):
        """全角：库里 `ＡＳＲ`（id `asr`）、JD 里写 `ASR`。"""
        self._write_back_and_check("全角", "ＡＳＲ", "ASR")

    def test_identity_holds_when_only_a_trailing_space_differs(self):
        """尾随空格：库里那张卡的 `term` 带尾随空格（手改过的 knowledge.json）、`id` 已是归一化形态。"""
        self._write_back_and_check("尾随空格", "ASR ", "ASR", seeded=False)

    def test_identity_holds_when_the_hit_comes_from_the_head_name_key(self):
        """头名键命中：库里 `词错率（WER）`（id `词错率wer`）、JD 写头名 `词错率`。"""
        self._write_back_and_check("头名键", "词错率（WER）", "词错率")


class IncompleteHitIdentityTest(_WriteBackCase):
    """I-1（**真缺陷**）：身份对齐（把节点 term 换成库里的规范名）要覆盖**所有命中**，
    不只是够格那一支。

    缺口（本轮之前）：改名只写在 `_fill_from_card` 里，而它**只在够格时**跑 → 命中但不够格
    （缺来源 / 缺通俗解释）时节点名原样留着。于是 `kb.absorb` 按
    `normalize_id(node["term"].strip())` 找卡（kb.py:373）找不到 → **新建一张重复卡**。
    实测复现：库里 1 张「语音识别」（缺来源）+ 节点「ASR」被模型判定命中
    → `matched=1 / reusable=0 / enriched=1`、`node["term"]` 仍是 `"ASR"` → 卡片数 1 → 2
    （「语音识别」+「asr」）。这正是 spec §7 要防的"知识库越长越乱"。

    边界（**这一条不许被这次改动突破**）：
      * 内容（`definition` / `plain_explanation` / `sources` / `timeline` / `category` /
        `state` / `confidence` …）**一个字节都不许改** —— 它们仍归 ③④ 去跑；
      * `node["id"]` 不许动（报告里的关联概念图按它连线）。
    """

    def _judge_call(self, pairs):
        """一次"模型判定命中"的调用（`call(messages) -> reply 文本`）。"""
        return lambda messages: json.dumps({"pairs": pairs}, ensure_ascii=False)

    def _hit(self, term="ASR", card_id="语音识别"):
        """跑一次 `plan_nodes`（节点 `term` 由模型判定命中 `card_id`），返回 `(stats, 节点)`。"""
        node = self._node(term)
        stats = kbreuse.plan_nodes([node], kbreuse.Index(store.load_knowledge()["nodes"]),
                                   call=self._judge_call([{"node": term, "card_id": card_id}]))
        return stats, node

    def test_a_judged_incomplete_hit_is_renamed_and_does_not_duplicate_the_card(self):
        """核心那条：库里「语音识别」**缺来源** + 节点「ASR」被判定命中 → 改名为规范名；
        然后**真** `kb.absorb(node)` → `added=0, merged=1`、**卡片数不变**。

        （这就是上一轮那个"写就是红"的探针，现在要它绿。）
        """
        self._seed("语音识别", sources=[], timeline=[])
        before = copy.deepcopy(store.load_knowledge()["nodes"][0])
        reusable, missing = kbreuse._card_completeness(before)
        self.assertFalse(reusable, "夹具前提：库里那张卡缺来源 → 不够格")
        self.assertIn("sources", missing)

        stats, node = self._hit("ASR", "语音识别")
        self.assertEqual((stats["matched"], stats["reusable"], stats["enriched"]), (1, 0, 1),
                         "夹具前提：命中但不够格（要联网并把结果写回那张卡）")
        self.assertEqual(node["kb_reuse"]["matched_by"], "assisted", "夹具前提：模型判定命中")
        self.assertFalse(node["kb_reuse"]["reusable"], "夹具前提：不够格")
        self.assertEqual(node["term"], "语音识别", "命中就要做身份对齐（不够格也一样）")
        self.assertEqual(node["kb_reuse"]["term_original"], "ASR",
                         "改名确实发生过 → 记下 JD 原本的写法")
        self.assertEqual(node["kb_reuse"]["card_term"], "语音识别")

        added, merged = kb.absorb([node], self.JD_NEW)      # ← 真的 kb.absorb
        self.assertEqual((added, merged), (0, 1), "改名之后写回必须并进原来那张卡，不是新建")
        cards = store.load_knowledge()["nodes"]
        self.assertEqual(len(cards), 1, "卡片数不变（1 → 2 就是新建了一张重复卡）")
        self.assertEqual(cards[0]["id"], before["id"], "还是原来那张卡")
        self.assertEqual([j["id"] for j in cards[0]["from_jds"]],
                         [self.JD_OLD["id"], self.JD_NEW["id"]], "只多了一条 from_jds")

    def test_a_width_only_difference_is_renamed_and_the_original_literal_is_recorded(self):
        """归一化后同名、**字面不同**（库 `ＡＳＲ` 全角 / JD 写 `ASR`）→ 也要改名，
        `term_original` 记**原字面**，写回仍然落在原来那张卡上（卡片数不变）。"""
        self._seed("ＡＳＲ", sources=[], timeline=[])
        before = copy.deepcopy(store.load_knowledge()["nodes"][0])
        self.assertEqual(before["id"], "asr", "夹具前提：卡片身份键 = 归一化后的 asr")

        node = self._node("ASR")
        stats = kbreuse.plan_nodes([node], kbreuse.Index(store.load_knowledge()["nodes"]))
        self.assertEqual((stats["matched"], stats["reusable"]), (1, 0), "夹具前提：命中但不够格")
        self.assertEqual(node["kb_reuse"]["matched_by"], "exact", "归一化后同名 → exact")
        self.assertEqual(node["term"], "ＡＳＲ", "字面不同也要换成库里的规范名")
        self.assertEqual(node["kb_reuse"]["term_original"], "ASR", "记的是**原字面**")

        added, merged = kb.absorb([node], self.JD_NEW)
        self.assertEqual((added, merged), (0, 1))
        cards = store.load_knowledge()["nodes"]
        self.assertEqual(len(cards), 1, "卡片数不变")
        self.assertEqual((cards[0]["id"], cards[0]["term"]), (before["id"], before["term"]),
                         "库里那张卡一个字没动")

    def test_an_incomplete_hit_changes_only_the_term(self):
        """回归（原约束**不变**）：不够格时内容**一个字节都不许改** —— 只有 `term` 被对齐。

        `definition` / `plain_explanation` / `sources` / `timeline` 仍归 ③④ 去跑。
        """
        self._seed("语音识别", sources=[], timeline=[])
        node = self._node("ASR")                  # sources / timeline 都是空的（③④ 还没跑）
        before = copy.deepcopy(node)

        kbreuse.plan_nodes([node], kbreuse.Index(store.load_knowledge()["nodes"]),
                           call=self._judge_call([{"node": "ASR", "card_id": "语音识别"}]))
        self.assertEqual(node["term"], "语音识别", "夹具前提：这条确实改了名（否则下面是假绿）")
        changed = sorted(k for k in set(before) | set(node)
                         if k not in ("term", "kb_reuse") and before.get(k) != node.get(k))
        self.assertEqual(changed, [], "不够格时除 term 外一个字段都不许动：%s" % changed)
        # 逐字节（比逐字段更硬：多一个键、挪一次顺序都露出来）
        self.assertEqual(self._frozen(node, skip=("kb_reuse",)),
                         self._frozen(dict(before, term="语音识别"), skip=("kb_reuse",)),
                         "整条节点（除 term / kb_reuse）必须逐字节不变")
        self.assertEqual(node["definition"], "本次写的定义", "内容归 ③④，不许被库里那份顶掉")
        self.assertEqual(node["sources"], [], "本次的来源是本次的（不够格 = 没有来源可复用）")
        self.assertEqual(node["confidence"], "ai-generated",
                         "不够格没有整条复用 → 不许标 verified（那是够格那支的事）")

    def test_a_node_the_exact_hit_already_took_is_aligned_and_does_not_duplicate(self):
        """★ 必修②：**被禁选挡掉**的那条也要做身份对齐（内容一个字节不动）。

        终审实测（`语音识别` + `asr`）：库里那张卡被**同名命中**的节点占上之后，判定层
        把 `asr` 判给同一张卡的配对**丢掉了**（一卡一条）；旧行为下那条节点既不采纳也
        **不改名** → `kb.absorb` 按归一化名找不到那张卡 → **新建一张重复卡**。
        """
        self._seed("语音识别")                     # 满卡（够格）→ 同名那条整条复用
        before = copy.deepcopy(store.load_knowledge()["nodes"][0])
        exact, judged = self._node("语音识别"), self._node("ASR")
        stats = kbreuse.plan_nodes([exact, judged], kbreuse.Index(store.load_knowledge()["nodes"]),
                                   call=self._judge_call([{"node": "ASR", "card_id": "语音识别"}]))
        self.assertEqual((stats["reusable"], stats["aligned_only"]), (1, 1), "夹具前提")
        r = judged["kb_reuse"]
        self.assertTrue(r["aligned_only"], "被禁选挡掉的那条没被标记成'只对齐身份'")
        self.assertFalse(r["reusable"], "它没从库里拿内容 → 不许说复用")
        self.assertEqual(judged["term"], "语音识别", "身份必须对齐（否则写回新建重复卡）")
        self.assertEqual(r["term_original"], "ASR", "改名确实发生过 → 记下 JD 原本的写法")
        self.assertEqual(r["matched_by"], "assisted")
        # 内容**仍是本次生成的**（③④ 的活）：一个字节都没从库里抄
        self.assertEqual(judged["definition"], "本次写的定义")
        self.assertEqual(judged["plain_explanation"], "本次写的通俗解释")
        self.assertEqual(judged["sources"], [])
        self.assertEqual(judged["timeline"], [])
        self.assertEqual(judged["confidence"], "ai-generated")

        added, merged = kb.absorb([exact, judged], self.JD_NEW)     # ← 真的 kb.absorb
        self.assertEqual(added, 0, "新建了重复卡（旧行为：added=1 且卡片数 1 → 2）")
        self.assertEqual(merged, 2, "两条节点都并进同一张卡")
        cards = store.load_knowledge()["nodes"]
        self.assertEqual(len(cards), 1, "卡片数不变（1 → 2 就是新建了一张重复卡）")
        self.assertEqual(cards[0]["id"], before["id"], "还是原来那张卡")
        self.assertEqual([j["id"] for j in cards[0]["from_jds"]],
                         [self.JD_OLD["id"], self.JD_NEW["id"]], "只多了一条 from_jds")

    def test_two_nodes_judged_to_one_card_still_land_on_one_card(self):
        """★ 必修②的另一半：两条节点被判成**同一张卡**（一卡一条，先出现的赢）。

        输的那条也要只对齐身份 —— 终审实测的 `泛化能力` + `模型泛化` 正是这个形态。
        这里让那张卡**不够格**（缺来源），因为这是最坏的一种组合：两条都要联网、都要写回。
        """
        self._seed("语音识别", sources=[], timeline=[])
        first, second = self._node("ASR"), self._node("语音识别的说法")
        stats = kbreuse.plan_nodes([first, second], kbreuse.Index(store.load_knowledge()["nodes"]),
                                   call=self._judge_call([
                                       {"node": "ASR", "card_id": "语音识别"},
                                       {"node": "语音识别的说法", "card_id": "语音识别"}]))
        self.assertEqual((stats["enriched"], stats["aligned_only"]), (1, 1), "夹具前提")
        self.assertEqual([first["term"], second["term"]], ["语音识别", "语音识别"],
                         "两条都要对齐到规范名")
        self.assertEqual((first["kb_reuse"].get("aligned_only"),
                          second["kb_reuse"].get("aligned_only")),
                         (None, True), "先出现的那个是正常命中，后一个是'只对齐身份'")
        self.assertEqual(second["kb_reuse"]["term_original"], "语音识别的说法")

        added, merged = kb.absorb([first, second], self.JD_NEW)
        self.assertEqual(added, 0, "写回新建了重复卡")
        self.assertEqual(len(store.load_knowledge()["nodes"]), 1, "卡片数不变")

    def test_identity_alignment_is_logged_for_later_audit(self):
        """★ 必修③留痕：改名必须落一条**可查**的事件。

        为什么非要留痕：卡内容**没有版本历史** —— 判定认错（模型说 A 就是 B）时
        `kb._merge_into` 的"更长者胜"会把 definition 换成另一个概念的文本，而用户事后
        只能靠这条日志回答"库里那张卡为什么是这个写法"。
        """
        self._seed("语音识别", sources=[], timeline=[])
        node = self._node("ASR")
        kbreuse.plan_nodes([node], kbreuse.Index(store.load_knowledge()["nodes"]),
                           call=self._judge_call([{"node": "ASR", "card_id": "语音识别"}]))
        ev = [r for r in log.read_logs(200)[0] if r.get("event") == "kbreuse.term_aligned"]
        self.assertTrue(ev, "身份对齐没留下任何日志（库里那张卡的写法以后查不到）")
        d = ev[-1]["data"]
        self.assertEqual((d.get("card_id"), d.get("from"), d.get("to")),
                         ("语音识别", "ASR", "语音识别"))
        self.assertEqual(d.get("matched_by"), "assisted", "要说清是靠哪条线索认出来的")
        self.assertEqual(d.get("reason"), "incomplete", "也要说清是哪条支路改的名")

        # 反向：**同名命中**（名字没变）不许留痕 —— 否则日志会被每一次命中刷满，
        # 真出问题时反而查不到（"没改名却留痕"这条要靠它钉住）。
        same = self._node("语音识别")
        kbreuse.plan_nodes([same], kbreuse.Index(store.load_knowledge()["nodes"]))
        again = [r for r in log.read_logs(200)[0] if r.get("event") == "kbreuse.term_aligned"]
        self.assertEqual(len(again), len(ev), "没改名却留了痕（日志会被刷满）")

    def test_the_reusable_branch_still_renames_and_copies_wholesale(self):
        """回归：**够格**那一支的 `term_original` / 内容覆盖口径**一字不变**（别改坏了）。

        同一条路径上用真 `kb.absorb` 走一遍：改名记录照旧、内容整条用库里那份、写回不新建卡。
        """
        self._seed("语音识别")                     # 满卡：有来源 + 时间线
        card = copy.deepcopy(store.load_knowledge()["nodes"][0])
        node = self._node("ASR")
        node["sources"] = [{"title": "本次搜到的", "url": "https://this-run"}]
        node["timeline"] = [{"year": "2026", "text": "本次算的"}]

        stats = kbreuse.plan_nodes([node], kbreuse.Index(store.load_knowledge()["nodes"]),
                                   call=self._judge_call([{"node": "ASR", "card_id": "语音识别"}]))
        self.assertEqual((stats["matched"], stats["reusable"]), (1, 1), "夹具前提：够格复用")
        self.assertEqual(node["kb_reuse"]["matched_by"], "assisted")
        self.assertEqual(node["kb_reuse"]["term_original"], "ASR", "改名记录口径不变")
        self.assertEqual(node["kb_reuse"]["got"], "all")
        self.assertEqual(node["term"], card["term"], "改成库里的规范名")
        self.assertEqual(node["definition"], card["definition"], "整条用库里那份")
        self.assertEqual(node["plain_explanation"], card["plain_explanation"])
        self.assertEqual(node["sources"], kb.clean_sources(card["sources"]),
                         "本次搜到的被整条替掉（不变量 5）")
        self.assertEqual(node["timeline"], kb.clean_timeline(card["timeline"]))
        self.assertEqual(node["confidence"], "verified")
        self.assertEqual(node["state"], card["state"])

        added, merged = kb.absorb([node], self.JD_NEW)
        self.assertEqual((added, merged), (0, 1), "写回仍然并进原来那张卡")
        self.assertEqual(len(store.load_knowledge()["nodes"]), 1)


class MismatchedCardIdentityEndToEndTest(_WriteBackCase):
    """P0-7 端到端：库里那张卡的 `id` **不等于**归一化 `term` 时，复用与写回是同一把尺子。

    实测（报告 §2 P0-7）：库里 `{"id": "yuyinshibie", "term": "ASR"}`（手改 / 别的写入者造成）
    → `kbreuse.Index.lookup("ASR")` 命中、`plan_nodes` 身份对齐也做了，但 `kb.absorb`
    仍 `added=1` → 库变成 `['yuyinshibie', 'asr']` **两张同概念卡**。
    根因：`kb.absorb` 按 `card["id"]` 建索引，`kbreuse` 按 `normalize_id(term)` 建索引。

    这条用例把**整条复用路径**串起来跑：认出是哪张卡（kbreuse）→ 写回（kb.absorb）→
    卡片数不变。单测里只钉一半的话，"两边又分叉"可以悄悄回来。
    """

    def test_reuse_then_write_back_does_not_create_a_second_card(self):
        # 库里那张卡 id ≠ 归一化 term（真 `merge_nodes` 造不出这种形态，只能手写盘）
        store.save_knowledge({"nodes": [dict(self._hand_card("ASR"), id="yuyinshibie")]})
        card = store.load_knowledge()["nodes"][0]
        self.assertNotEqual(card["id"], kb.normalize_id(card["term"]),
                            "夹具前提：这张卡的 id 与归一化 term 本来就不一致")

        node = self._node("ASR")                       # 同一次分析又见到这个概念
        index = kbreuse.Index(store.load_knowledge()["nodes"])
        hit, how = index.lookup("ASR")                 # 返回 (卡, 命中线索)
        self.assertEqual((hit["id"], how), ("yuyinshibie", "exact"),
                         "kbreuse 认出的是同一张卡（按归一化 term）")

        stats = kbreuse.plan_nodes([node], index)      # 同名命中，不需要模型判定
        self.assertEqual(stats["matched"], 1, "夹具前提：命中库里那张卡")

        added, merged = kb.absorb([node], self.JD_NEW)  # ← 真的 kb.absorb
        self.assertEqual((added, merged), (0, 1),
                         "写回必须并进原来那张卡（只按 id 建索引时会新建一张 'asr' 重复卡）")
        after = store.load_knowledge()["nodes"]
        self.assertEqual(len(after), 1, "库变成两张同概念卡就是 P0-7 复发")
        self.assertEqual(after[0]["id"], "yuyinshibie", "还是原来那张卡")
        self.assertEqual([j["id"] for j in after[0]["from_jds"]], [self.JD_OLD["id"], self.JD_NEW["id"]],
                         "只多了一条 from_jds（不是新建的卡）")


# ---------------------------------------------------------------------------------
# 只读真库的**人工闸门**（1.10①）。故意不做成 `unittest` 用例。
#
# 原先是 `RealKbTest.test_the_real_knowledge_base_also_has_no_short_derived_key`：
# 它临时把全局 `store.DATA_ROOT` 指回**用户真实库**、跑完再指回临时目录，有三个问题：
#   1. **无锁改全局**：那一刻只要有别的线程活着（本项目真出过 app/interview 后台线程写盘
#      的事故），它就会**写进用户真实 data/** —— 测试绝不该有这条通路；
#   2. **断言绑在本机数据上**：把库挪走、或库里某个时刻有 1 字符卡 id，全量测试就红，
#      而那与**代码**有没有问题无关（数据问题该用数据闸门看，不该让全量测试变红）；
#   3. 它还操作 `store._KB_BACKUP_DONE` —— 该属性在 `store.py` 里**根本不存在**
#      （`git show HEAD:store.py` 搜不到），是纯死代码（`getattr` 拿到 None 就整段跳过）。
#
# ⚠️ 不变量**没有被删**：`by_key` 无 len<2 键这件事仍由构造库那条强用例钉着
# （`test_no_short_key_ever_lands_in_by_key`，构造了单字符 term / 单字符别名 / 两位数
# 等最坏形状；`_keys_of_term` 的闸门另有 KeysTest 那批钉住）。这里保留的是**另外半件事**：
# 「用户此刻盘上那份真库也干净」—— 它是**数据体检**，不是单元测试，所以走人工入口：
#
#     python -B -m tests.test_kbreuse
#
# 照样会红（非 0 退出码），但**不会**在每次 `unittest discover` 里碰用户真实数据。
def check_real_kb_has_no_short_derived_key():
    """只读用户真实知识库，返回 `(ok, 说明)`。**绝不写盘、绝不改任何全局路径。**"""
    index, err = kbreuse.build_index()     # cards=None → store.read_knowledge_strict，只读
    if err:
        return False, "真库读不出来（只读）：%s" % err
    if len(index) <= 0:
        return False, ("真库是空的 —— 这条闸门没牙；先确认 DATA_ROOT 指向的是真库：%s"
                       % store.DATA_ROOT)
    bad = [k for k in index.by_key if len(k) < 2]
    if bad:
        return False, ("真库的派生键里有 len<2 的键：%r（用户手改 knowledge.json、或 "
                       "merge_nodes 写入单字符别名时，两个不相干的概念就会互相认错）"
                       % (bad[:20],))
    return True, ("真库干净：%d 张卡，by_key 共 %d 把键，无 len<2 的键（只读检查，未写盘）"
                  % (len(index), len(index.by_key)))


if __name__ == "__main__":
    # 人工入口：**不** `unittest.main()` —— 本模块的用例仍由 `unittest discover` 跑，
    # 这里只跑上面那道「只读真库」的闸门。失败 → 非 0 退出（人工巡检/CI 都能当红用）。
    print("[check] DATA_ROOT=%s" % store.DATA_ROOT)
    _ok, _why = check_real_kb_has_no_short_derived_key()
    print("[%s] %s" % ("OK" if _ok else "FAIL", _why))
    raise SystemExit(0 if _ok else 1)

