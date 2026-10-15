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


def _fill_from_card(node, card):
    """把库里那份**整条**覆盖到节点上，返回节点**原来的**写法（`term_original`）。

    三处刻意不动：
      * `node["id"]` —— 报告里的关联概念图按它连线，改它会打断 `related`；
      * `node["related"]` —— 见 `_COPY_FIELDS` 上面的注释；
      * `node["kb_reuse"]` —— 由调用方 `plan_nodes` 写（这里只管内容）。
    """
    original = str(node.get("term") or "")
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
    node["term"] = str(card.get("term") or original)
    return original


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
             "enriched": 0, "searched": len(nodes), "knowledge_error": str(error or "")}
    idx = index if index is not None else Index([])
    if len(idx) == 0:
        # 空库直接早退：省掉那次模型判定（本来也没什么可判的）。
        return stats
    found = {}      # id(node) -> (card, matched_by)
    for n in nodes:
        card, how = idx.lookup(n.get("term"))
        if card is not None:
            found[id(n)] = (card, how)
    rest = [n for n in nodes if id(n) not in found]
    if call is not None and rest:
        pairs = judge(rest, idx, call)
        for n in rest:
            card = idx.cards.get(pairs.get(str(n.get("term") or "")))
            if card is not None:
                found[id(n)] = (card, "assisted")
    for n in nodes:
        hit = found.get(id(n))
        if hit is None:
            continue
        card, how = hit
        reusable, missing = _card_completeness(card)
        stats["matched"] += 1
        original = ""
        if reusable:
            original = _fill_from_card(n, card)
            stats["reusable"] += 1
            if card.get("timeline"):
                stats["timeline_reused"] += 1
        else:
            stats["enriched"] += 1
        n["kb_reuse"] = {
            "card_id": kb.normalize_id(card.get("term")),
            "card_term": str(card.get("term") or ""),
            # ⚠️ **只在真的改名时才记**（M-8）：
            #   * 名字与规范名相同时留空串 —— 否则报告里会冒出一句多余的
            #     「JD 里写作 语音识别」；
            #   * `reusable` 也为假时留空串 —— **不够格就不改名**（内容一个字都不动，
            #     名字也不是"被改成了规范名"，记下来是编的）。所以这里的 `reusable and`
            #     不是多余条件，而是"改名确实发生过"的一部分。
            "term_original": (original if (reusable and original
                                           and original != str(card.get("term") or "")) else ""),
            "matched_by": how,
            "reusable": bool(reusable),
            "missing": list(missing),
            "got": ("all" if (reusable and card.get("timeline")) else
                    ("sources" if reusable else "")),
            "origin": _origin_of(card),
        }
    stats["searched"] = stats["total"] - stats["reusable"]
    return stats


def _chunks(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def judge(nodes, index, call):
    """让模型判一次"这些知识点是不是库里已有的某个概念"。

    返回 `{节点 term 原文: card_id}`。**逐条校验**，任何可疑的一律丢弃：
      * `card_id` 必须真实存在于**这一批**库里（**不许发明概念**）；
      * `node` 必须是这次问过的节点；
      * **一张卡只认一条**（模型说两个不同名字都等于同一张卡时，多半认错了一个）；
      * 返回畸形 / 调用抛异常 → **一条都不用**（等同"没命中"），记日志。
    """
    nodes = list(nodes or [])
    terms = index.terms_for_judge() if index is not None else []
    if not terms or not nodes:
        return {}
    wanted = {}
    for n in nodes:
        wanted.setdefault(str(n.get("term") or ""), n)
    out = {}
    for chunk in _chunks(terms, MAX_JUDGE_TERMS):
        payload = {"A": chunk,
                   "B": [{"node": t, "term": t,
                          "definition": str((n.get("definition") or ""))[:40]}
                         for t, n in wanted.items()]}
        try:
            reply = call([{"role": "system", "content": JUDGE_SYSTEM},
                          {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}])
            data = json.loads(reply) if isinstance(reply, str) else None
        except Exception as e:
            log.log_exc("kbreuse.judge_error", e)
            continue
        pairs = data.get("pairs") if isinstance(data, dict) else None
        if not isinstance(pairs, list):
            log.log_event("kbreuse.judge_bad_shape", level="warn", got=type(pairs).__name__)
            continue
        allowed = {c["card_id"] for c in chunk}
        for p in pairs:
            if not isinstance(p, dict):
                continue
            node_term = str(p.get("node") or "")
            cid = str(p.get("card_id") or "")
            if node_term not in wanted or cid not in allowed:
                continue
            if cid in out.values():
                continue          # 一张卡只认一条
            out[node_term] = cid
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
    """
    tm = (cfg or {}).get("text_model") or {}
    if not (tm.get("base_url") and tm.get("api_key") and tm.get("model")):
        return None

    def call(messages):
        return llm.call_openai_compatible(tm["base_url"], tm["api_key"], tm["model"], messages,
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
