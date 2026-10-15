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
import traceback

import kb
import llm
import log
import store

# 一次模型判定最多带多少条库内条目；超了**分块**（`_chunks`），每块单独判一次。
# 「分块」而不是「截断」是**故意**的：截断会让清单尾部那些概念**永远判不到**
# （少命中且看不出来），而分块的代价只是多花几次模型调用。
MAX_JUDGE_TERMS = 400

JUDGE_SYSTEM = """你是知识库管理员。下面给你两份清单：
A. 我的知识库里**已有**的条目（card_id + 术语 + 一行定义）
B. 新岗位需要、但名字没能直接对上的一些知识点（node + 术语 + 一行定义）

请判断：B 里每一条，是不是**就是** A 里的某一条（同一个概念的不同叫法）。

只输出 JSON：{"pairs":[{"node":"<B 里的 node 原文>","card_id":"<A 里的 card_id>"}]}

硬性要求：
- `card_id` **必须是 A 里出现过的原样字符串**；不确定就**不要输出这一对**
- **只输出你确信是同一个概念的**；宁可少配对，也不要猜
- 一个 `card_id` 最多被一条 `node` 认领
- 全部中文，字符串内部禁止英文双引号与裸换行"""

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
         记录过的 `aliases`。查这一层按**键的真实来源**判 `abbrev` / `head` / `alias`。

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
        # 派生键 -> "abbrev" | "head" | "alias"（M-3：归属按**键的真实来源**判）
        self.key_source = {}
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
            # 头名键**单独记 `head`**（Minor④，终审实测）：节点 `词错率` 命中库里
            # `词错率（WER）` 靠的是**去掉括号后的头名**，而节点自己**一个括号都没有** ——
            # 报成 `abbrev` 时报告会对用户说「括号缩写命中」，那句话让人莫名其妙（也说不通）。
            # 两个档位必须分得开，因为它们是两条不同的线索：
            #   * `abbrev` = 卡侧 term 的 **ASCII 括号内容**（`词错率（WER）` 贡献 `wer`）；
            #   * `head`   = 卡侧 term **去掉括号后的头名**（同一张卡贡献 `词错率`）。
            for k in _paren_keys(c.get("term")):
                if k:
                    derived.append((k, "abbrev"))
            head = _head_key(c.get("term"))
            if head:
                derived.append((head, "head"))
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
          * `abbrev` —— 通过**卡侧**的括号缩写命中的（如节点 `WER` ↔ 库里 `词错率（WER）`）
          * `head`   —— 通过**卡侧的"去括号头名"**命中的（如节点 `词错率` ↔ 库里 `词错率（WER）`）。
            与 `abbrev` 分开是**必须**的：这条线索的节点侧**没有括号**，报 `abbrev` 时报告会说
            「括号缩写命中」—— 用户看不出这是怎么认出来的（Minor④）。
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
                    # 这里如实报 `abbrev`：能走到这一行的 `k` 只可能来自 `_keys_of_term`
                    # 的**括号键**（`_head_key(节点)` 不在那张清单里），所以线索确实是
                    # "节点里的括号缩写正好就是那张卡的精确名" —— 与 `head` 不是一回事。
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


# 从卡片整条复制到节点上的字段。**刻意不含 `related`**：
# 它的 `{id}` 指向**上一次分析**的节点（`store.slug`），复制到本次报告里就是断链
# （本次节点的 related 由模型为这个 JD 头脑风暴产生，指向的 id 都在本次报告里）。
#
# ⚠️ **契约：这几个字段在本次运行内是"只读"的。**
# 它们一旦落到报告节点上，本次运行就**绝不再原地改它们**（下游全是重绑定新对象：
# ③ 会重写 `definition` / `plain_explanation`，④ 会重写 `timeline`，`kb.absorb` 也是整条重算）。
# 为什么要有这条契约：复制出来的东西与卡片**不能共享同一个可变对象** —— 就地把共享的
# dict 改脏再 `kb.absorb`，**盘上那张库卡真的会被改脏**（复查反证过）。所以下面所有形状类
# 字段都走 `kb` 自己的清洗函数（它们每次都返回**全新的** dict/str，见 `_CLEAN_FIELDS`），
# 深拷贝那一层因此是多余的（不另写一套）。
# 但它挡不到另一头：**脏值本来就在节点上**、随后被 `kb.absorb` 抄进库 —— 那条只能靠这条契约。
_COPY_FIELDS = ("definition", "plain_explanation", "category", "sources", "timeline",
                "interview_questions", "state")
# 形状类字段 → 复用 `kb` 已有的清洗函数（**不另写一套**，M-6）：
#   * `clean_sources`：只留 `dict` 且 `url` 非空，补齐 title/date/accessed/snippet（新 dict）；
#   * `clean_timeline`：只留带 `text` 的 dict，收成 `{year,text}`（新 dict）；
#   * `clean_questions`：只留非空字符串、去重、按 `kb.MAX_QUESTIONS` 截断。
# 于是 `definition=5` / `timeline={"a": 1}` / `interview_questions="abc"` 这类手改出来的
# 奇形值不会原样塞进报告节点（旧代码只有 list 被拷贝，其余原样赋值）。
# 剩下没列在这里的字段是**文本类**，一律走 `str(...).strip()`（与 `kb._new_card` 同一口径）。
_CLEAN_FIELDS = {"sources": kb.clean_sources,
                 "timeline": kb.clean_timeline,
                 "interview_questions": kb.clean_questions}
# `term` 单独处理（取规范名 + 记原写法），`layer` 取 min，`confidence` 直接写 verified。


def _origin_of(card):
    """这张卡**最早**的 `from_jds` 条目 → `{"job_title","date"}`（报告标注用）。

    **先过滤掉空 date 再取最早**：`min` 比的是字符串，手改出来的 `date: ""` 是最小的字符串，
    会**抢赢所有真日期**，于是 origin 变成空的（M-7 —— 报告里那句「最早来自《…》· 日期」
    会整个丢掉，还看不出来）。
    一条真日期都没有时（全是 `""` / 空白）：没有"最早"可比 → **保序退回第一条** ——
    `from_jds` 是按加入顺序追加的，所以第一条就是最早入库的那一次；`date` 留空，不编造日期。
    """
    rows = [x for x in ((card or {}).get("from_jds") or []) if isinstance(x, dict)]
    if not rows:
        return {"job_title": "", "date": ""}
    dated = [x for x in rows if str(x.get("date") or "").strip()]
    first = min(dated, key=lambda x: str(x.get("date") or "")) if dated else rows[0]
    return {"job_title": str(first.get("job_title") or ""), "date": str(first.get("date") or "")}


def _align_term(node, card, matched_by="", reason=""):
    """**身份对齐**：把节点的 `term` 换成库里的规范名，返回节点**原来的**写法。

    这件事**不是内容覆盖，是身份对齐** —— 所以它**只要命中就跑**
    （够格 / 不够格 / 被禁选挡下只对齐身份，三条支路都一样）：
    `kb.absorb` 是按 `kb.normalize_id(node["term"].strip())` 找卡的（kb.py:373），节点名不改，
    写回时就找不到库里那张卡 → **新建一张重复卡**（spec §7 要防的"知识库越长越乱"）。

    真缺陷复现（本轮修掉的那条）：库里「语音识别」（缺来源）+ 节点「ASR」被模型判定命中 →
    旧行为下 `node["term"]` 仍是 `"ASR"` → `kb.absorb` 给出 `added=1, merged=0`，
    卡片数 1 → 2（「语音识别」+「asr」）。

    刻意**不动** `node["id"]`：报告里的关联概念图按它连线，改它会打断 `related`（spec §7）。
    `card["term"]` 为空（手改坏）时保留节点自己的写法，绝不把 `term` 变成空串 ——
    空名节点写回时连"可吸收"都不算（`kb.absorb` 第 359-361 行）。

    **留痕**（必修③）：改名是"库里那张卡为什么是这个写法"的唯一线索 —— 卡内容**没有版本
    历史**（`kb._merge_into` 的"更长者胜"会直接把 definition 换成另一个概念的文本），
    所以改名的这一刻必须落一条**可查**的事件。字段：`card_id` / `from` / `to` /
    `matched_by`（怎么命中的）+ `reason`（哪条支路改的：`reused` 够格整条复用 /
    `incomplete` 命中但不够格 / `card_taken` 被禁选或一卡一条挡下、只对齐身份）。
    只在**真的改了**（原写法非空、且与规范名字面不同）时记 —— 否则日志会被每一次
    "命中同名卡"刷满，真出问题时反而查不到。
    ⚠️ `from` 是 Python 关键字，只能靠 `**{...}` 传（`log.log_event` 收 `**kwargs`）。
    """
    original = str(node.get("term") or "")
    node["term"] = str(card.get("term") or original)
    if original and original != node["term"]:
        log.log_event("kbreuse.term_aligned",
                      card_id=kb.normalize_id(card.get("term")),
                      matched_by=str(matched_by or ""), reason=str(reason or ""),
                      **{"from": original, "to": node["term"]})
    return original


def _fill_from_card(node, card, matched_by=""):
    """把库里那份**整条**覆盖到节点上，返回节点**原来的**写法（`term_original`）。

    三处刻意不动：
      * `node["id"]` —— 报告里的关联概念图按它连线，改它会打断 `related`；
      * `node["related"]` —— 见 `_COPY_FIELDS` 上面的注释；
      * `node["kb_reuse"]` —— 由调用方 `plan_nodes` 写（这里只管内容）。

    `matched_by` 只用来给 `_align_term` 的留痕事件带上"怎么命中的"（默认空串 = 不留痕字段）。
    """
    for k in _COPY_FIELDS:
        if k not in card:
            continue                        # 卡里没有的字段保持节点自己的值
        v = card[k]
        clean = _CLEAN_FIELDS.get(k)
        if clean is not None:
            node[k] = clean(v)              # 形状类：kb 自己的尺子 + 全新对象
        else:
            node[k] = str(v or "").strip()  # 文本类：奇形值（5 / [1] / None）收口成字符串
    # `layer` 走 `kb._as_int` 兜底（I-1）：`knowledge.json` 是用户可手改的，
    # 一张 `layer: "abc"` / `[1]` 的卡会让 `int(...)` 抛出去 —— 那不是"悄悄认错"，
    # 而是**整步崩**：Task 5 接上后整次分析的知识地图退化成 `knowledge_error`，
    # 其余几十个节点的复用机会一起丢。下界夹 0 与 `kb._new_card` / `kb._merge_into` 同一口径
    # （min 是单调的：一旦让 -3 落盘，之后再来合法值也永远回不来）。
    node["layer"] = min(max(0, kb._as_int(node.get("layer"))),
                        max(0, kb._as_int(card.get("layer"))))
    # `"ai-generated"` 那条支路**不可达**：本函数只在够格时被调用，而"够格"的定义里
    # 就要求「至少一条带 url 的 dict 来源」（`_card_completeness`）—— 直接写死 `verified`。
    # 刻意不写 `"verified" if card.get("sources") else ...`：那是死分支，而且它用的是
    # truthy，不是 completeness 那把"有 url 的 dict"的尺子，两把尺子迟早会漂移。
    node["confidence"] = "verified"
    # 身份对齐与"不够格"那一支**同一处实现**（I-1）：两条支路各写一遍迟早就漂移，
    # 而这里漂移的代价正是本轮修掉的那条真缺陷（写回新建重复卡）。
    # 返回值就是节点**原来的**写法（`_align_term` 唯一一处算它的地方）。
    return _align_term(node, card, matched_by=matched_by, reason="reused")


def plan_nodes(nodes, index, call=None, error=""):
    """就地做"库优先"：确定性匹配 →（可选）模型判定 → 填充 → 统计。

    返回统计（报告标注与日志都用它），并把 `kb_reuse` 写在每个**命中**的节点上。
    `call` 是**已经绑好 cfg 的模型调用函数**（`call(messages) -> reply 文本`）；
    `call=None` = 只做确定性匹配（不花模型调用），这也是模型判定不可用时的降级形态。

    `index=None` = **调用方没给库**（不是"库是空的"）：两者在行为上都退化成"全部走联网"，
    但语义完全不同 —— 所以调用方必须把 `build_index` 那条 `err` 通过 `error=` 如实传进来。
    不传的话报告里那个 `knowledge_error` 就是空串，读报告的人分不清「库里暂时没有这条」
    与「库读不出来（这次的复用全都白瞎了）」—— 那是一行 ⚠ 的差别（M-4）。
    `error` 非空时**不阻止**分析：能命中的照样复用，它只是如实转达进 `stats`。

    **同一个节点对象**在 `nodes` 里出现两次时按 `id(n)` 只算一次（M-5）：否则
    `matched` / `reusable` / `timeline_reused` 各 +2，而且第二遍会把第一遍写好的
    `term_original` **抹成 `""`**（改名记录丢失）。注意去重按**对象身份**，
    不是按名字 —— 两个内容相同的真节点是两条，都要处理。

    **三条支路的差别只有"要不要拿库里的内容"（I-1 / 必修②）**：
      * 够格 → `_fill_from_card`：改名 + **整条内容覆盖**；
      * 不够格 → 只 `_align_term` 改名，内容一个字不动（definition / sources / timeline
        全归 ③④ 去跑）；
      * **被禁选 / 一卡一条挡下**（`aligned_only`）→ 同样只改名，内容一个字不动。
    改名这三条支路**都要做**，因为它决定 `kb.absorb` 写回时认不认得出原来那张卡
    （见 `_align_term`）。

    ⚠️ `searched` 在这里是**推测值**（`total - reusable`）：本函数只知道"复用省下了几次"，
    **不知道** `knowledge.attach_sources` 的 `max_searches` 上限。真值由 ③ 算完写回来
    （`build_for_jd` 把 `attach_sources(stats=reuse.stats)` 原地更新）—— 报告里读到的必须是
    真值，否则会对"超出上限、根本没搜"的卡说「已联网补全」（必修①）。

    统计口径：`matched` = **拿到 `kb_reuse` 标记的节点数**（够格 + 不够格 + 只对齐身份），
    于是 `matched == reusable + enriched + aligned_only` —— 报告里那句
    「命中 M 条（其中 E 条不够格、A 条与其它节点指向同一张卡）」才自洽。
    """
    nodes = list(nodes or [])
    uniq, seen = [], set()
    for n in nodes:
        if id(n) in seen:
            continue
        seen.add(id(n))
        uniq.append(n)
    nodes = uniq
    stats = {"total": len(nodes), "matched": 0, "reusable": 0, "timeline_reused": 0,
             "enriched": 0, "aligned_only": 0,
             "searched": len(nodes), "knowledge_error": str(error or "")}
    idx = index if index is not None else Index([])
    if len(idx) == 0:
        # 空库直接早退：省掉那次模型判定（本来也没什么可判的）。
        return stats
    found = {}      # id(node) -> (card, matched_by)
    occupied = set()   # 确定性匹配已经占用的 card_id（判定层的**禁选集合**，I-1）
    for n in nodes:
        card, how = idx.lookup(n.get("term"))
        if card is not None:
            found[id(n)] = (card, how)
            cid = kb.normalize_id(card.get("term"))
            if cid:
                occupied.add(cid)
    # 只对齐身份、不抄内容的那些节点：id(node) -> (card, matched_by, reason)
    align_only = {}
    rest = [n for n in nodes if id(n) not in found]
    if call is not None and rest:
        dropped = {}          # `judge` 的出参：模型判成同一概念、但这次没采纳的配对
        pairs = judge(rest, idx, call, forbidden=occupied, dropped=dropped)
        # 填充前的**第二道收口**（I-1）：判定命中不许拿已被确定性占用的卡，判定命中之间
        # 也互不共享一张卡（先出现的赢）。它与 `judge` 里的 `forbidden` **不是**重复的摆设：
        # 两处各有独立的测试盯着（`test_the_plan_gate_drops_a_judged_card_that_the_exact_hit_already_took`
        # 与 `test_the_judge_itself_drops_a_forbidden_card_and_keeps_the_legal_one`），
        # 别因为"看起来像兜底"就删掉任何一处。
        # ⚠️ **确定性同名共享一张卡是允许的**（spec §5：同名是"确凿同一"，
        # `test_two_nodes_resolving_to_one_card_both_get_reused`）—— 所以被禁的只是
        # "判定命中去拿别人已经占上的卡"，不是"一张卡只能填一个节点"。
        for n in rest:
            cid = pairs.get(str(n.get("term") or ""))
            if not cid:
                continue
            card = idx.cards.get(cid)
            if card is None:
                continue            # 判定层已经校验过，这里是"别处的 bug 也别炸"的一层
            if cid in occupied:
                # 这张卡已经归别人了 → **只对齐身份**，内容一个字不动（见下面的长注释）
                align_only[id(n)] = (card, "assisted", "card_taken")
                continue
            found[id(n)] = (card, "assisted")
            occupied.add(cid)
        # **被禁选丢掉的那条也要做身份对齐**（必修②，真缺陷）：`judge` 把"模型**确实**判成了
        # 同一概念、但这次没采纳"的配对放进 `dropped`（禁选卡 / 一卡一条，见 `judge`）。
        # 只"丢掉不采纳"而不对齐的话，那条节点的 `term` 保持 JD 里的写法 →
        # `kb.absorb` 按归一化名找不到库里那张卡 → **新建重复卡**
        # （终审实测两张：`语音识别` + `asr`、`泛化能力` + `模型泛化`）。
        # ⚠️ 只对齐 `term`，内容**一个字节都不动**（不调 `_fill_from_card`）：那张卡的内容
        # 已经归另一个节点了，这条照旧联网、内容由 ③④ 产出。
        # ⚠️ 于是报告里会出现"**两条同名节点**"（一条是库里的、一条是本次生成的）——
        # **这是可接受的**，不是 bug：写回时它们并进同一张卡（`kb.absorb` 按归一化名找卡）。
        # 别为了"报告好看"把后者改名或者干脆丢掉对齐 —— 那会正好把重复卡放回来。
        for n in rest:
            if id(n) in found or id(n) in align_only:
                continue                     # 它已经拿到了更好的归宿（合法配对优先）
            cid = dropped.get(str(n.get("term") or ""))
            card = idx.cards.get(cid) if cid else None
            if card is None:
                continue                     # 判定层已经校验过卡在库里，这里是兜底
            align_only[id(n)] = (card, "assisted", "card_taken")
    for n in nodes:
        only = align_only.get(id(n))
        if only is not None:
            card, how, why = only
            original = _align_term(n, card, matched_by=how, reason=why)
            stats["matched"] += 1
            stats["aligned_only"] += 1
            # 内容一个字不动 ⇒ 复用资格一律 False（③④ 照旧要跑，`_already_reused` 也是读它）
            _reusable, missing = _card_completeness(card)
            _mark_reuse(n, card, how, False, missing, original, aligned_only=True)
            continue
        hit = found.get(id(n))
        if hit is None:
            continue
        card, how = hit
        reusable, missing = _card_completeness(card)
        stats["matched"] += 1
        original = ""
        if reusable:
            original = _fill_from_card(n, card, how)
            stats["reusable"] += 1
            if card.get("timeline"):
                stats["timeline_reused"] += 1
        else:
            stats["enriched"] += 1
            # **不够格也要做身份对齐**（I-1，真缺陷）：改名不是内容覆盖，只要命中就该做。
            # 不做的话写回会新建一张重复卡（`kb.absorb` 按 `normalize_id(term)` 找卡）。
            # 内容（definition / sources / timeline / …）一个字都不动 —— 那是 ③④ 的事。
            original = _align_term(n, card, matched_by=how, reason="incomplete")
        _mark_reuse(n, card, how, reusable, missing, original)
    stats["searched"] = stats["total"] - stats["reusable"]
    return stats


def _mark_reuse(node, card, how, reusable, missing, original, aligned_only=False):
    """把"这次到底拿到了什么"写在节点上（`node["kb_reuse"]`）—— **口径只有这一处**。

    三条支路（够格 / 不够格 / 只对齐身份）共用它，理由与 `_align_term` 一样：各写一遍迟早
    漂移，而这里漂移的代价是报告说假话（报告逐字读这些字段）。

    字段口径（报告 `kReuseLine` 与测试都按它读）：
      * `card_id` / `card_term` —— 命中的那张卡（`card_id` 是归一化后的身份键）；
      * `term_original` —— **只在真的改名时才记**（M-8）：
          - 名字与规范名**字面**相同时留空串 —— 否则报告里会冒出一句多余的
            「JD 里写作 语音识别」；
          - 够格 / 不够格 / 只对齐三条支路都记，判据只有"改名有没有真的发生"这一条：
            不够格与只对齐时也改名了（I-1 / 必修②），所以这里**不再**掺 `reusable` ——
            那会把一个真发生过的改名记成没发生（报告与事实相反）。
      * `reusable` —— **这条节点有没有拿到库里那份内容**。只对齐身份那条一律 `False`
        （它的内容全归 ③④，也必须继续联网：`knowledge._already_reused` 就读这个字段）；
      * `missing` —— 库里那张卡缺什么（够格判定用的那三个字段 + timeline）；
      * `got` —— `all` / `sources` / `""`；
      * `origin` —— 这张卡最早来自哪个岗位；
      * `aligned_only` —— **只有**只对齐身份那条才有这个键（值 `True`）。报告靠它把
        "与另一条节点指向同一张卡"与"库里内容不够格"分开说 —— 后者对一张**内容齐全**
        的卡是假话（它没什么"不够格"的，只是这张卡已经归别人了）。
    """
    node["kb_reuse"] = {
        "card_id": kb.normalize_id(card.get("term")),
        "card_term": str(card.get("term") or ""),
        "term_original": (original if (original
                                       and original != str(card.get("term") or "")) else ""),
        "matched_by": how,
        "reusable": bool(reusable),
        "missing": list(missing),
        "got": ("all" if (reusable and card.get("timeline")) else
                ("sources" if reusable else "")),
        "origin": _origin_of(card),
    }
    if aligned_only:
        node["kb_reuse"]["aligned_only"] = True
    return node["kb_reuse"]


def _chunks(seq, size):
    """按 `size` 切块。**`size <= 0` 一律兜底成 1**（M-6）。

    为什么非要兜这一下：`size=0` 时 `range(0, n, 0)` 抛 `ValueError`，而这个函数**不在**
    `judge` 那个 `try` 里（try 罩的是模型调用）—— 它会一路掀掉整步知识地图：
    Task 5 接上之后，整次分析的知识地图退化成 `knowledge_error`，几十个节点的复用机会
    一起丢。负数更坏：一个 chunk 都不产出，看起来"判过了"其实**一条都没问**。
    当前唯一的 `size` 来源是模块常量 `MAX_JUDGE_TERMS`（硬编码、没有配置入口），
    所以这是**预防性**的兜底 —— 但代价极低，而漏掉的代价是整步崩。
    """
    size = max(1, int(size))
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def judge(nodes, index, call, forbidden=None, dropped=None):
    """让模型判一次"这些知识点是不是库里已有的某个概念"。

    返回 `{节点 term 原文: card_id}`。**逐条校验**，任何可疑的一律丢弃：
      * `card_id` 必须真实存在于**这一批（这一块）**清单里（**不许发明概念**）；
      * `node` 必须是这次问过的节点，且它的 `kb.normalize_id(term)` **非空** ——
        空名节点不参与判定（I-1：否则 `node:""` 能**一次认领多个**空名节点，
        两个空名节点会被填上同一张卡）；
      * **一张卡只认一条**、**一条节点只认一张卡**：先出现的赢（顺序稳定，绝不随机）。
        模型说"两个不同名字都等于同一张卡"时，多半认错了一个（spec §6）；
      * `forbidden` 里的 `card_id`（**确定性匹配已经占用**的那些）一律丢弃 ——
        "一卡一条"必须跨过「确定性 / 判定」这条边界（I-1）：否则同一张卡会同时落到
        「同名命中」与「模型判定命中」两个节点上，两个节点被改成同一个 `term`、
        装着一模一样的内容，而其中一个概念其实没被认出来；
      * 调用抛异常 / `llm.parse_json_reply` 失败 / **顶层**形状不对 → **这一块**
        一条都不用（等同"没命中"），记日志（含**块序号与总块数**，且异常原文**截断**后
        再落盘 —— HTTP 报错常带整段 prompt/回复）。**某一块坏了只作废那一块**，
        其余块照常生效（`continue`），绝不"整步退回确定性匹配"。

    `dropped`（**可选出参**，必修②）：传进来一个 dict，本函数把"模型**确实**判成了同一概念、
    但这次**没采纳**"的配对照样原地写进去（`{节点 term 原文: card_id}`），给调用方做
    **只对齐身份、不抄内容**。两类被丢弃的配对都要报：
      * 禁选卡（`forbidden`）—— 采纳会破坏"一卡一条"；
      * 一卡一条的内层去重（同一张卡被两条节点认领，先出现的赢）。
    **不报的代价**（本轮修掉的真缺陷）：那条节点的 `term` 保持 JD 里的写法 →
    `kb.absorb` 按归一化名找不到库里那张卡 → **新建重复卡**（终审实测：`语音识别` + `asr`）。
    **不在这一块的** `card_id`（模型在发明概念/记错了块）**不报** —— 那不是库里的卡，
    拿它对齐等于按幻觉改名。

    为什么是出参而不是把它并进返回值：返回值 `{term: card_id}` 是**既有契约**（一批测试直接
    断言它等于某个 dict），改成 `(out, dropped)` 会把所有调用点拖进这次改动。
    与 `knowledge.attach_sources` 的 `stats` 出参同一个理由。

    ⚠️ 已知无上限（M-7，如实记一句）：B 清单（要问的节点）**每一块都重发整份**，
    块数越多重复越多。当前不做优化 —— 生产上节点 ≤ 60、块数通常为 1，收益为零。
    """
    nodes = list(nodes or [])
    terms = index.terms_for_judge() if index is not None else []
    if not terms or not nodes:
        return {}
    banned = {str(x) for x in (forbidden or [])}
    wanted = {}
    for n in nodes:
        t = str(n.get("term") or "")
        if not kb.normalize_id(t):
            continue            # 空名节点不参与判定（I-1）
        wanted.setdefault(t, n)
    if not wanted:
        return {}               # 一条能问的都没有 → 别白花那次模型调用
    out = {}
    chunks = list(_chunks(terms, MAX_JUDGE_TERMS))
    for i, chunk in enumerate(chunks):
        payload = {"A": chunk,
                   "B": [{"node": t, "term": t,
                          "definition": str((n.get("definition") or ""))[:40]}
                         for t, n in wanted.items()]}
        try:
            reply = call([{"role": "system", "content": JUDGE_SYSTEM},
                          {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}])
            # 与 `knowledge._call` 那条路**同一个** helper（容 ```json 围栏）——
            # 用裸 `json.loads` 时，一句带围栏的回复会让**整块白丢**（M-8）。
            # 校验闸门一条都不放松：下面逐条校验的还是同一批（parse 只负责把文本读成 dict）。
            data = llm.parse_json_reply(reply)
        except Exception as e:
            # 手动落一条 `log_exc` 形状的记录（error + traceback），但**截断**异常原文：
            # `log.log_exc` 会把 `str(异常)` 原样写进去，而 HTTP 报错常带整段 prompt/回复。
            log.log_event("kbreuse.judge_error", level="error",
                          error=str(e)[:300], traceback=traceback.format_exc()[-2000:],
                          chunk=i + 1, chunks=len(chunks))
            continue
        pairs = data.get("pairs")
        if not isinstance(pairs, list):
            log.log_event("kbreuse.judge_bad_shape", level="warn",
                          got=type(pairs).__name__, chunk=i + 1, chunks=len(chunks))
            continue
        # `allowed` 是**当前这一块**的清单（减掉确定性已占用的）——不是全部 terms：
        # 小库里两者恰好相同，所以这条要有牙就必须真的分块（见 `test_a_card_id_from_another_chunk_is_dropped`）。
        chunk_ids = {c["card_id"] for c in chunk}
        allowed = chunk_ids - banned
        for p in pairs:
            if not isinstance(p, dict):
                continue
            node_term = str(p.get("node") or "")
            cid = str(p.get("card_id") or "")
            # `cid` 必须在**这一块**的清单里：模型不许发明概念，也不该拿着别块的 id
            # （那种连"判过"都不算 —— 拿它对齐等于按幻觉改名，所以**不进 `dropped`**）。
            if node_term not in wanted or cid not in chunk_ids:
                continue
            if node_term in out:
                continue          # 一条节点只认一张卡：**先出现的赢**（M-5）
            if cid in allowed and cid not in out.values():
                out[node_term] = cid
                continue
            # 到这里 = "模型**确实**判成了同一概念、但这次没采纳"（禁选卡 / 一卡一条）。
            # 采纳会破坏"一卡一条"（spec §5/§6）；**直接丢掉又会让调用方失去身份对齐的机会**
            # —— 那条节点的 term 保持 JD 写法，写回时 `kb.absorb` 认不出库里那张卡，
            # **新建重复卡**（必修② 实测：`语音识别` + `asr`）。所以如实报给 `dropped`，
            # 由 `plan_nodes` 只对齐身份、内容一个字不动。
            if dropped is not None:
                dropped.setdefault(node_term, cid)
    return out


def _judge_caller(cfg):
    """把"调一次文本模型"包成 `judge` 要的 `call(messages) -> 文本`。

    `cfg` 里没有可用的文本模型（`base_url` / `api_key` / `model` 缺一）→ 返回 `None`：
    调用方（`plan_nodes`）会**只做确定性匹配** —— 那是免费的那一半，照跑；
    被省掉的只有这次模型判定（省一次额度，也不因为没配模型就报错）。

    调用方式与 `knowledge._call` **完全同一套**（`llm.call_openai_compatible`，就是那里
    `chat or …` 的默认值）：本模块不 import `knowledge`（成环，见模块 docstring），
    所以这里直接落到同一个底层函数上，绝不另造一条模型调用路径。

    `temperature` 走 `float(tm.get("temperature") or 0.3)`（与 `knowledge._call` 的口径一致）：
    往接口送 `None` 是"不带这个参数"的另一套语义，而这个字段在配置里是可选装饰。

    `json_mode=True` 也照 `knowledge._call`：判定是**只要 JSON** 的调用，而 `llm` 在不支持
    `response_format` 时会自动降级重试，所以这里不需要自己兜。
    """
    tm = (cfg or {}).get("text_model") or {}
    if not (tm.get("base_url") and tm.get("api_key") and tm.get("model")):
        return None

    def call(messages):
        return llm.call_openai_compatible(tm["base_url"], tm["api_key"], tm["model"], messages,
                                          json_mode=True,
                                          temperature=float(tm.get("temperature") or 0.3))
    return call


class Reuse:
    """"库优先"的执行者：**一次分析建一次目录** → 跑匹配（含一次模型判定）→ 返回统计。

    这个类**不许抛**：知识库读不出来、索引建不起来、模型判定失败，
    都必须在 `stats` 里如实反映，让分析照常做完（联网兜底）—— spec §9。
    做到这件事靠的是**下游**：`build_index` 把读盘失败转成 `(空目录, 原因)`、
    `plan_nodes` 不抛、`judge` 把模型失败吞成"没命中"。这里刻意**不加** try/except ——
    真抛出来的话那是别处的 bug，应当响亮地失败（静默兜底会把它藏起来）。

    `used` 记录的是"**这次真的跑过库优先**"：`do_analyze` 靠它区分
    「跑了、库是空的（复用了 0 条）」与「根本没跑（知识地图是从缓存/旧报告拿的）」——
    后者在报告里写"复用了 0 条"就是**说假话**（spec §9）。
    """

    def __init__(self, cfg=None):
        self.cfg = cfg or {}
        self.stats = {}
        self.used = False      # 是否真的跑过（缓存命中时不会跑）

    def plan(self, nodes):
        self.used = True
        index, err = build_index()
        self.stats = plan_nodes(nodes, index, call=_judge_caller(self.cfg), error=err)
        return self.stats
