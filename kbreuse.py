# -*- coding: utf-8 -*-
"""阶段 E：**知识库优先复用** —— 报告要的知识点，先问本地知识库。

单一职责：给一个知识点，回答"库里有没有？是哪一张？能不能直接用？"，以及"该往库里补什么"。
**它不写盘**：真正的写回一律走 `app.do_analyze` 里已有的 `kb.absorb`
（那条路已经被测试盯得很牢，不另开一条）。

铁律：
  1. **不 import `knowledge`**（`knowledge` 反向依赖本模块提供的注入对象，成环）；
     它需要的东西（一个 `call(messages)->reply 文本` 函数）由调用方注入。
  2. **不做字面相似度匹配**（`difflib` / 编辑距离一律不引入）：实测错例明确
     （「风险识别」会被认成「语音识别」），而认错的形态是"报告里塞进另一个概念的内容
     且看不出来"——那正是本项目最不能出的错（spec §2、§5）。
  3. 读知识库**只读**，且必须区分"读不出来"与"没有"（`store.read_knowledge_strict`）。
"""
import json
import re

import kb
import log
import store

# 一次模型判定最多带多少条库内条目；超了分块，分块后仍超 → 退回确定性匹配
# （少命中，但**绝不**悄悄截断清单然后假装判过）。
MAX_JUDGE_TERMS = 400

# ASCII 括号内容（`词错率（WER）` / `评测标准(Rubric)`）。判据在**括号内容**上，
# 与括号本身是不是中文括号无关：`语音端点检测（VAD）` 用中文括号，照样建 `vad` 键。
# 要求**整段**字符都落在 ASCII 字符类里（不是"只认开头"）：所以 `模型（abc中文）` **不建**键。
# 这是**故意**的方向 —— 少建键只会少命中（退回一次联网搜索），而**绝不会**错命中
# （把另一个概念的内容塞进报告，spec §2/§5 最不能出的错）。
#
# 归一化（`kb.normalize_id` 的 NFKC）**跑在这道 ASCII 闸门之后**：全角的 `（ＷＥＲ）`
# 里 `Ｗ` 不在字符类里 → **不建** `wer` 键；半角 `（WER）` 建。同样方向安全：
# 全名键（`_keys_of_term` 里对整条术语做的 `kb.normalize_id`）照样把 `词错率（ＷＥＲ）`
# 归一成 `词错率wer`，少的只是一把别名键，不会错命中。
_ASCII_PAREN = re.compile(r"[（(]\s*([A-Za-z0-9][A-Za-z0-9 .,\-]*?)\s*[)）]")

# 「够格才算缩写」用的字母闸门（见 `_paren_keys`）。
_ASCII_LETTER = re.compile(r"[A-Za-z]")


def _paren_keys(term):
    """术语里 **ASCII** 括号内容的归一化键（`词错率（WER）` → `["wer"]`）。

    **够格才算缩写**：归一化后必须 **长度 ≥ 2 且至少含一个 ASCII 字母**。
    单字符 / 纯数字**不是缩写，是噪音** —— 它会让任意两个含该字符的概念互相认错：
    实测 `选项（A）` 与 `分组（a）` 共享键 `a`，而卡片 `{"term": "A"}` 的 id 恰好就是 `a`，
    于是节点 `选项（A）` 会把 `A` 的内容**悄悄塞进报告里**（spec §2 禁止的失败形态：
    认错且看不出来）。纯数字同理：`第（2）轮` 与 `版本（2）` 共享键 `2`。
    `WER` / `ASR` / `VAD` / `3D` / `GPT4` 通过；`A` / `a` / `2` / `12` 被拒。

    键的口径一律沿用 `kb.normalize_id`（这里没有第二套），长度与字母闸门跑在归一化**之后**。
    """
    out = []
    for m in _ASCII_PAREN.finditer(str(term or "")):
        k = kb.normalize_id(m.group(1))
        if len(k) >= 2 and _ASCII_LETTER.search(k) and k not in out:
            out.append(k)
    return out


def _keys_of_term(term):
    """一个术语的全部查找键：去噪全名 + 每个 ASCII 括号内容（去重、保序）。

    **口径必须沿用 `kb.normalize_id`**（NFKC + 去标点空白 + 小写），不许另造一套 ——
    **两侧都算**：整条术语（第一把键）与括号内容（`_paren_keys`）都要走它。
    括号侧自造一套（哪怕只是"小写 + 去空白"）就会漏掉 NFKC 与去标点：
    `评测（a.b）` 会建成 `a.b`，而库里卡片 id 是 `ab` —— 报告的节点与卡片对不上（spec §5）。
    """
    out = []
    for k in [kb.normalize_id(term)] + _paren_keys(term):
        if k and k not in out:
            out.append(k)
    return out


def _card_completeness(card):
    """`(reusable, missing)`：够格复用 = **定义 + 通俗解释 + ≥1 条真实来源链接**。

    `timeline` **不参与够格判定**：它是锦上添花（缺了照样能复用，只是要补算），
    但会出现在 `missing` 里 —— 报告要知道"这条的时间线是不是本次补的"。

    前提：`card` 是 **dict**（调用方只把 dict 卡片交进来）。非 dict 的真值会在第一行
    `(card or {}).get` 上抛 `AttributeError`，那是**故意的** —— 形状错的卡片是上游的 bug，
    崩出来比"静默判成不够格、然后悄悄少复用它"好（后者正是本项目最忌讳的失败形态）。
    """
    missing = []
    if not str((card or {}).get("definition") or "").strip():
        missing.append("definition")
    if not str((card or {}).get("plain_explanation") or "").strip():
        missing.append("plain_explanation")
    # sources 够格的判据是「**列表里至少一个 dict，且它的 url 非空**」，不是 truthy：
    # 实测 `[{}]` / `[None]` / `"abc"` / `{"url": "u"}` 全是真值，却一条能点开的链接都没有
    # （`{"url": "u"}` 连列表都不是，是形状错）。spec §1 决策 2 要的是「≥1 条真实来源链接」。
    sources = (card or {}).get("sources")
    has_url = isinstance(sources, list) and any(
        isinstance(s, dict) and str(s.get("url") or "").strip() for s in sources)
    if not has_url:
        missing.append("sources")
    if not (card or {}).get("timeline"):
        missing.append("timeline")
    reusable = not any(m in ("definition", "plain_explanation", "sources") for m in missing)
    return reusable, missing
