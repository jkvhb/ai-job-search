# -*- coding: utf-8 -*-
"""阶段 E：知识库优先复用 —— 纯函数测试（匹配 / 够格 / 判定校验 / 填充）。"""
import unittest
from unittest import mock

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
                                 "timeline_reused": 0, "enriched": 0, "searched": 1,
                                 "knowledge_error": ""})
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


class BrokenShapeTest(IsolatedCase):
    """`knowledge.json` **是用户可手改的** —— 手改坏的一张卡不许让整次分析的知识地图崩掉（I-1）。

    崩的代价不是"悄悄认错"，而是**整步崩**：Task 5 接上后，一张 `layer: "abc"` 的卡会让
    `plan_nodes` 抛出去、整次分析退化成 `knowledge_error`，**其余几十个节点的复用机会一起丢**。
    项目自己就把这个形状当真：`kb._as_int`（`kb.py:83-88`）就是为它写的，
    `tests/test_kb.py:300-309` 明确把 `"layer": "abc"` 当"不许抛"的形状。
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
        """`layer` 走 `kb._as_int` 兜底 —— `int("abc")` / `int([1])` 都会把整步炸掉。"""
        for bad in ("abc", [1], None, {}, "3.7", float("nan"), -9):
            node = self._node()
            card = self._card(layer=bad)
            stats = kbreuse.plan_nodes([node], kbreuse.Index([card]))     # 不许抛
            self.assertEqual(stats["reusable"], 1)
            self.assertEqual(node["layer"], 0,
                             "坏 layer 按 kb._as_int 兜底成 0（下界也夹 0）：%r" % (bad,))

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

    def test_a_float_layer_is_truncated_like_kb_as_int(self):
        node = self._node(layer=5)
        kbreuse.plan_nodes([node], kbreuse.Index([self._card(layer=3.7)]))
        self.assertEqual(node["layer"], 3, "`kb._as_int(3.7)` == 3（截断，不四舍五入）")


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

        def fake_judge(rest, index, call):
            seen.append(list(rest))
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

    def test_a_call_without_any_unmatched_node_does_not_reach_the_judge(self):
        """全都确定性命中了 → 也没有可判的东西，同样省掉那次调用。"""
        node = self._node()
        seen = []

        def fake_judge(rest, index, call):
            seen.append(list(rest))
            return {}

        with mock.patch.object(kbreuse, "judge", fake_judge):
            kbreuse.plan_nodes([node], kbreuse.Index([self._card()]), call=lambda m: "{}")
        self.assertEqual(seen, [])
