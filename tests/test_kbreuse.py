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
        """
        idx = kbreuse.Index([{"id": "词错率wer", "term": "词错率（WER）", "definition": "d"}])
        card, how = idx.lookup("词错率")
        self.assertIsNotNone(card, "中文头名要能查到带括号的那张卡")
        self.assertEqual(card["id"], "词错率wer")
        self.assertEqual(how, "abbrev")

        idx2 = kbreuse.Index([{"id": "响应延迟首字延迟", "term": "响应延迟（首字延迟）",
                               "definition": "d"}])
        card2, how2 = idx2.lookup("响应延迟")
        self.assertIsNotNone(card2, "中文括号那类也要建头名键")
        self.assertEqual(card2["id"], "响应延迟首字延迟")
        self.assertEqual(how2, "abbrev")

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
        单字符别名。精确名可以短（它在 `by_name` 里，是**用户自己的卡**，短名无害 ——
        查 `a` 只会查到那张卡自己），但**派生键**一旦短了就是泛化键。
        """
        idx = kbreuse.Index([
            {"id": "a", "term": "A", "definition": "d"},
            {"id": "语音识别", "term": "语音识别", "aliases": ["A", "x", "ASR"], "definition": "d"},
            {"id": "词错率wer", "term": "词错率（WER）", "definition": "d"},
            {"id": "响应延迟首字延迟", "term": "响应延迟（首字延迟）", "definition": "d"},
        ])
        self.assertEqual([k for k in idx.by_key if len(k) < 2], [])
        self.assertIn("a", idx.by_name, "精确名（哪怕 1 个字符）仍在第一层")

    def test_the_real_knowledge_base_also_has_no_short_derived_key(self):
        """同一条不变量在**真库**上（只读）：`by_key` 无 len<2 的键。

        单独成条而不是并进上面那条 —— 并在一起时真库那半截会被前一行断言挡住，
        "真库也干净"就永远没被测到（本次就是踩到这个才拆开的）。
        真库此刻确实干净（145 张卡、无 1 字符卡 id、`by_key` 无短键），
        这条钉的是**将来**（用户手改 knowledge.json 或 merge_nodes 写入单字符别名）。

        ⚠️ 读真库就要**临时退出隔离**：`IsolatedCase` 把 `store.DATA_ROOT` 指到临时目录，
        而临时目录里没有知识库 → `build_index()` 会读到空库，这条断言就变成"0 张卡也绿"
        （实测踩到）。所以这里显式还原真实 `DATA_ROOT` 并清掉 `_KB_BACKUP_DONE`
        （脏文件备份只该发生一次，但这条不读脏文件，清掉是防御性的），**finally 里恢复隔离**。
        """
        wait = getattr(store, "_KB_BACKUP_DONE", None)
        store.DATA_ROOT = self._real_data_root
        if wait is not None:
            store._KB_BACKUP_DONE = False
        try:
            real, err = kbreuse.build_index()
        finally:
            store.DATA_ROOT = self.tmp
            if wait is not None:
                store._KB_BACKUP_DONE = wait
        self.assertEqual(err, "", "真库要读得出来（只读）")
        self.assertTrue(len(real) > 0, "真库不该是空的（否则这条没牙）")
        self.assertEqual([k for k in real.by_key if len(k) < 2], [],
                         "真库的派生键也没有 len<2 的")

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
