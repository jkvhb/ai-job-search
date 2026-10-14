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
# ⚠️ **当前全仓没有任何地方引用它**（Task 5 的 judge 才会用）—— 如实记在这里，
# 免得后人以为"已经有一道闸门在生效"。
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
    单字符 / 纯数字**不是缩写，是噪音** —— 它会让任意两个含该字符的概念互相认错。
    下面是**构造复现（预防的由来，不是线上发生过的事）**：若不加这道闸门，
    `选项（A）` 与 `分组（a）` 会共享键 `a`，而卡片 `{"term": "A"}` 的 id 恰好就是 `a`，
    于是节点 `选项（A）` 会把 `A` 的内容**悄悄塞进报告里**（spec §2 禁止的失败形态：
    认错且看不出来）。纯数字同理：`第（2）轮` 与 `版本（2）` 会共享键 `2`。
    **真库现状（只读扫描，2026 年本次改动时）**：145 张卡，既无 1 字符卡 id，
    `by_key` 也没有 len<2 的键 —— 所以这道闸门与 `Index` 里的同一条尺子都是**预防性**的
    （用户手改 knowledge.json、或 `merge_nodes` 把单字符 term 写进 `aliases` 时才会出现）。
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


# 「去掉括号内容后的全名」用：中英文括号都认，且只要成对就整段去掉（嵌套时留外层）。
_HEAD_PAREN = re.compile(r"[（(][^)）]*[)）]")


def _head_key(term):
    """**头名键**：术语去掉括号内容后的全名归一化结果（`词错率（WER）` → `词错率`）。

    为什么要有这一类（M-4，直接决定"能不能真正复用"）：真库 8 张带括号的卡里，
    **7 张用中文头名查不到**（`词错率` / `平均意见分` / `打断` / `评测标准` / `字错误率` /
    `声学回声消除` / `语音端点检测` 全 miss）—— 而报告里的节点天然写中文头名。
    查不到的后果是 `absorb` 认不出该合并，**新建一张重复卡**（库里越攒越多同一概念）。

    它是**派生键**（第二层），且**不含**归一化后的全名：`kb.normalize_id(term)` 始终是
    第一层的精确名，所以头名键再多也**遮蔽不了**任何卡自己的名字（见 `Index.__init__`）。
    """
    t = str(term or "")
    if not _HEAD_PAREN.search(t):
        return ""                       # 没有括号 → 头名就是全名，第一层已经有了
    return kb.normalize_id(_HEAD_PAREN.sub("", t))


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


class Index:
    """知识库的"名字 → 卡片"目录。一次分析建一次，之后**只读**。

    **分两层**（I-1 —— 这是"能不能真正复用"的关键）：
      1. `by_name`：卡片的**精确名** = `kb.normalize_id(term)`。查这一层命中一律算 `exact`。
      2. `by_key` ：**派生键** = term 里的 ASCII 括号缩写 / 头名（去括号后的全名）/ 卡片
         记录过的 `aliases`。查这一层按来源判 `abbrev` / `alias`。

    为什么必须分层（反例 · 复查实测）：一张扁平表下，同一把键既可能来自"某张卡的**全名**"，
    也可能来自"另一张**更早**的卡的**括号**"，`setdefault` 一视同仁 → 返回先落盘的那张。
    `[词错率（WER）, WER]` 查 `"WER"` 会拿到「词错率（WER）」/abbrev，**倒序才**拿到 `WER`/exact。
    真库此刻已有 **8 把键**（`wer` / `vad` / `bargein` / `rubric` / `mos` / `kappa` / `cer` /
    `aec`）被派生键占住 —— 以后入库一张 term 就叫 `MOS`/`VAD`/`CER` 的卡（本领域极常见）
    会被**静默遮蔽**：查 `MOS` 拿到别人，而且看不出来。分层之后精确名**永远**赢。

    **层内**仍是先到先得（`setdefault`，顺序稳定，绝不随机）：派生键之间互相撞，
    谁先落盘谁赢 —— 这条没有变，也不该变。
    """

    def __init__(self, cards=None):
        self.cards = {}       # card_id -> card
        self.by_name = {}     # kb.normalize_id(term) -> card（**精确名**，第一层）
        self.by_key = {}      # 其余派生键（ASCII 括号 / 头名 / aliases），第二层（先到先得）
        self.key_source = {}  # 派生键 -> "abbrev" | "alias"（M-3：归属按**键的真实来源**判）
        for c in (cards or []):
            if not isinstance(c, dict):
                continue
            cid = kb.normalize_id(c.get("term"))
            if not cid or cid in self.cards:
                continue
            self.cards[cid] = c
            # 第一层内部：先到先得（同 id 的第二张在上面就被去重挡掉了）
            self.by_name.setdefault(cid, c)
            # 第二层：**不含 cid 自己**（精确名不参与派生层的争抢）。
            # 键的**来源**同时记下来 —— `abbrev` / `alias` 的区分判据是键从哪来，
            # 不是"两边看起来像不像"（M-3：卡侧 aliases 贡献的键必须报 alias，
            # 否则 alias 这一档会被 abbrev 遮蔽，报告说不清是靠哪条线索认出来的）。
            #
            # aliases 必须走 `kb._as_list` 收口（I-2）：直接迭代 `(c.get("aliases") or [])`
            # ① `{"aliases": 5}` / `True` 抛 `TypeError`（本函数的存在意义就是"失败要降级、
            #   绝不抛"）；② `"ASR语音标注"` 被**逐字符**拆键 → 单字符泛化键从**别名这道门**
            #   重现（Task 2b 刚在括号侧堵掉它），任意含 `a` 的概念互相认错（spec §2 禁止的
            #   失败形态）。实测确认 `kb._as_list` 对**字符串**返回 `[]`（把字符串当"不是列表"），
            #   正是我们要的口径：宁可不建键（少命中，退回联网），绝不逐字符拆（会错命中）。
            #   生产可达：`kb.absorb({'term': 'A'})` + `kb.merge_nodes` 真能把 `['A']` 写进
            #   `aliases`。
            derived = []
            for k in list(_paren_keys(c.get("term"))) + [_head_key(c.get("term"))]:
                if k:
                    derived.append((k, "abbrev"))
            for a in kb._as_list(c.get("aliases")):
                k = kb.normalize_id(a)
                if k:
                    derived.append((k, "alias"))
            for k, src in derived:
                # **派生键一律要有 2 个字符**（与括号侧的"够格才算缩写"同一条尺子）：
                # 单字符派生键是泛化键。精确名哪怕 1 个字符也留在第一层 —— 它是用户自己的卡，
                # 查 `a` 只会查到那张卡自己，且第一层谁都不遮蔽。
                if k == cid or len(k) < 2:
                    continue
                # 先到先得：同一把键被两张卡占着时保留第一张（顺序稳定，绝不随机）
                self.by_key.setdefault(k, c)
                # 归属也先到先得：同一把键被"卡 A 的括号"和"卡 B 的 aliases"同时占着时，
                # 报出来的必须是**真正生效那张卡**的来源（`by_key` 里留下的那张）。
                self.key_source.setdefault(k, src)

    def __len__(self):
        return len(self.cards)

    def terms_for_judge(self):
        """喂给模型判定用的"库里已有什么"清单（只带 id / 名字 / 一行定义，省 token）。"""
        return [{"card_id": cid, "term": str(c.get("term") or ""),
                 "definition": str(c.get("definition") or "")[:40]}
                for cid, c in self.cards.items()]

    def lookup(self, term):
        """`(卡片 或 None, 怎么命中的)`。

        `matched_by` 的**精确定义**（别用"键等于全名"这种代理判断 —— 那会把
        "节点叫 `WER`、库里叫 `词错率（WER）`"错报成 `exact`）：
          * `exact`  —— **两边全名去噪后完全相同**（`normalize_id(term) == normalize_id(card.term)`）
          * `abbrev` —— 通过**卡侧**的括号缩写 / 头名命中的（如节点 `WER` ↔ 库里 `词错率（WER）`）
          * `alias`  —— 通过卡片**记录过的** `aliases` 命中的

        **先查精确名那一层**（命中即 `exact`），未中再过派生键那一层 —— 顺序由
        `__init__` 的两层结构保证，与"谁先落盘"无关（I-1）。

        ⚠️ 第二层**也要回查 `by_name`**，这不是走回头路：节点 `语音识别（ASR）` 的
        **括号键** `asr` 正好就是库里那张卡的**精确名**（`card.term == "ASR"`）。
        只查 `by_key` 会让"节点带括号、库里用缩写当全名"这个方向整个 miss
        （库里那边 `asr` 是第一层，压根不在第二层）—— spec §5 要的"**双向**命中"就断了。
        回查到的仍是**那张卡自己的名字**，所以只可能更准，不可能认错。
        """
        full = kb.normalize_id(term)
        # 第一层：精确名。节点叫 `WER`、库里也有一张就叫 `WER` 的卡 → 命中就是它，
        # 哪怕另一张更早的卡里有 `（WER）` 这样的括号（那只是**派生**键）。
        if full:
            card = self.by_name.get(full)
            if card is not None:
                return card, "exact"
        # 第二层：派生键（括号 / 头名 / aliases）；归属按建索引时记下的**键来源**判。
        for k in _keys_of_term(term):
            card = self.by_key.get(k)
            if card is not None:
                return card, self.key_source.get(k, "alias")
            if k != full:
                card = self.by_name.get(k)      # 节点的括号键 == 库里那张卡的精确名
                if card is not None:
                    return card, "abbrev"
        return None, ""


def build_index(cards=None):
    """建目录。`cards=None` 时从磁盘**严格只读**读知识库。

    返回 `(Index, 原因)`：原因非空 = 知识库读不出来。这时**不阻止分析**，
    只是这次全部走联网，并由调用方把原因如实带进报告标注（spec §9）。
    """
    if cards is not None:
        return Index(cards), ""
    data, err = store.read_knowledge_strict()
    if err:
        log.log_event("kbreuse.kb_unreadable", level="warn", error=err)
        return Index([]), err
    return Index(data.get("nodes") or []), ""
