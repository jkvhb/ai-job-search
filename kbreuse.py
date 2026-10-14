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

# ASCII 括号内容（`词错率（WER）` / `评测标准(Rubric)`）。**只认 ASCII 开头**：
# 中文括号是解释性补充（`响应延迟（首字延迟）`），当别名会让 `Case（案例）沉淀`
# 贡献出泛化的 `案例`，任何叫「案例」的节点都会误命中（spec §5）。
_ASCII_PAREN = re.compile(r"[（(]\s*([A-Za-z0-9][A-Za-z0-9 .,\-]*?)\s*[)）]")


def _paren_keys(term):
    """术语里 **ASCII** 括号内容的归一化键（`词错率（WER）` → `["wer"]`）。"""
    out = []
    for m in _ASCII_PAREN.finditer(str(term or "")):
        k = kb.normalize_id(m.group(1))
        if k and k not in out:
            out.append(k)
    return out


def _keys_of_term(term):
    """一个术语的全部查找键：去噪全名 + 每个 ASCII 括号内容（去重、保序）。

    **口径必须沿用 `kb.normalize_id`**（NFKC + 去标点空白 + 小写），不许另造一套 ——
    否则报告里的节点名与库里的卡片 id 会对不上（spec §5）。
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
    """
    missing = []
    if not str((card or {}).get("definition") or "").strip():
        missing.append("definition")
    if not str((card or {}).get("plain_explanation") or "").strip():
        missing.append("plain_explanation")
    if not (card or {}).get("sources"):
        missing.append("sources")
    if not (card or {}).get("timeline"):
        missing.append("timeline")
    reusable = not any(m in ("definition", "plain_explanation", "sources") for m in missing)
    return reusable, missing
