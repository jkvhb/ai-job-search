# -*- coding: utf-8 -*-
"""知识管线：① 拆解 ② 头脑风暴 ③ 搜索 ④ 校验生成"""
import json
import re
from concurrent.futures import ThreadPoolExecutor

import llm
import log
import search as search_mod
import store

MAX_CORE = 15
MAX_RELATED_PER_NODE = 3
MAX_TIMELINES = 30   # 时间线最贵（每节点一次模型调用），设上限兜底
MAX_SEARCHES = 40    # 每次分析最多搜这么多次（核心概念优先），控制 Tavily 额度消耗

# 节点上 `search_skipped` 的取值：**超出 `max_searches` 上限、本次一次都没搜**。
# 这个键必须落在**节点**上（不只落在统计里）：报告要逐条分辨"联网搜了但一条都没留下"
# 与"压根没联网"—— 两者都是 `sources == []`，只看结果分不出来，而对后者说「已联网补全」
# 就是**说假话**（必修①）。报告模板里读的是字面量 `n.search_skipped === 'capped'`。
SEARCH_SKIPPED_CAPPED = "capped"

# 本工具只做 AI 领域岗位（大模型数据标注 / AI 数据运营 / 标注管理 / AI 产品经理）。
# 领域护栏只放在**搜回来之后**（filter_relevant），查询本身不做领域锚定。

# ---------------------------------------------------------------------------
# 为什么查询不加领域词（2026-10-08 实测，共 20 次真实 Tavily 搜索）——勿再"优化"回去
#
# 指标 =「既切题（标题/摘要字面含术语）又属本领域」的结果数，5 个真实术语 × 4 种写法：
#   术语              A {t} 是什么   B {t} 大模型 AI
#   省略恢复               3/3              0/3
#   裁决权限               0/3              0/3
#   抽样检验               1/3              0/3
#   Transformer 架构       3/3              2/3
#   幻觉                   1/3              3/3
#   合计                  8/15             5/15
# 机理：查询里只要出现「大模型 AI」，Tavily 就返回泛 AI 页面
# （如「15款大模型透明度测评」「视觉模型推进图像复原」），与术语毫无关系；
# 而 filter_relevant 只验领域、不验切题 —— 这些页面会顺利通过过滤并被标成「有来源」，
# 比原来那条 Microsoft 的错**更危险**（伪装的错来源，看起来可信）。
# 补充实测：引号变体 C `"{t}" 是什么` ≡ A、D `"{t}" 大模型 AI` ≡ B（逐条结果完全相同，引号无效）；
# 用定义消歧的策略 E（`{术语} + {定义前20字}`）只有 2/9，**比 A 更差**。
# 结论：不存在好的领域锚定查询。**能过滤掉的错才是安全的错** ——
# 查询保持裸术语（{term} 是什么），让 filter_relevant 去挡（它是唯一护栏）。
# ---------------------------------------------------------------------------

# 强关键词：无歧义的 AI 词（含 AI 缩写与专有名词），命中即保留
# 中英并重：英文结果标题里常只有 machine learning / deep learning 这类词，
# 只放中文关键词会把正确来源误杀（实测：Transformer (deep learning) 被误丢）。
DOMAIN_KEYWORDS = (
    # 中文
    "人工智能", "大模型", "大语言模型", "语言模型", "自然语言", "机器学习",
    "深度学习", "强化学习", "神经网络", "多模态", "计算机视觉", "ai", "llm",
    "微调", "数据集", "语料", "prompt", "token",
    # 英文（"fine-tun" 是前缀，可同时命中 fine-tune / fine-tuning）
    "machine learning", "deep learning", "neural", "nlp",
    "language model", "large language model", "dataset", "annotation",
    "artificial intelligence", "inference", "fine-tun", "embedding", "chatbot",
    "classifier", "benchmark", "corpus", "supervised", "reinforcement",
    # 专有名词 / 缩写（无歧义的 AI 领域词，加了只会少误杀）
    "gpt", "openai", "chatgpt", "bert", "diffusion", "rag", "agent",
    "ocr", "asr", "tts",
)

# 弱关键词：同名或通用词，**单独命中不算数**（"transformer" 也是电力变压器、
# 「模型」也可以是时装模特、「训练」也可以是体育训练）。只命中弱关键词时，
# 必须看有没有否决词：命中否决词 → 丢弃；没有 → 保留（避免把正确来源误杀）。
WEAK_KEYWORDS = (
    "transformer", "模型", "训练", "算法", "推理", "对话", "语音",
    "评测", "标注", "对齐", "智能", "生成",
)

# 否决词：同名跨领域信号（电力变压器等）。只对「仅命中弱关键词」的结果生效；
# 命中强关键词的结果一律保留，否决词不会误杀真正的 AI 来源。
# 前 6 个中文 + 后 5 个英文来自实测列表；末尾两个是同一漏洞的英文侧补强
# （维基百科词条 Transformer 的摘要靠 electrical engineering / windings 才判得出是电力件）。
VETO_KEYWORDS = (
    "电力", "变压器", "输电", "变电", "绕组", "电工",
    "electrical transformer", "power transformer", "voltage", "kva", "electric power",
    "electrical engineering", "winding",
)

# 纯 ASCII 且 ≤4 字母的短词必须按词边界匹配，否则会子串误命中：
# available / email / retail 里的 "ai"，storage / paragraph 里的 "rag"。
# 词边界 = 左右都不是英文字母；允许复数 s（LLMs / RAGs 要能命中）。
# 注意 "AI标注" 仍命中：中文字符不算英文字母。
_ASCII_SHORT_MAX = 4
_ASCII_BOUNDARY_RE = {
    k: re.compile(r"(?<![a-z])%s(?:s)?(?![a-z])" % re.escape(k))
    for k in set(DOMAIN_KEYWORDS) | set(WEAK_KEYWORDS) | set(VETO_KEYWORDS)
    if k.isascii() and k.isalpha() and len(k) <= _ASCII_SHORT_MAX
}


def _hit(blob, kw):
    """blob 已 lower()；返回该关键词是否命中"""
    rx = _ASCII_BOUNDARY_RE.get(kw)
    return rx.search(blob) is not None if rx else kw in blob


def build_query(node):
    """只拼「{术语} 是什么」，**刻意不加领域词**（理由见文件顶部实测记录）。

    领域护栏由 filter_relevant 承担：它只验领域，不验切题 —— 所以查询越"干净"越好。
    """
    term = (node.get("term") or "").strip()
    return ("%s 是什么" % term).strip()


def filter_relevant(sources):
    """强/弱关键词两级 + 否决词：宁可无来源，也不给错来源

    - 命中强关键词（DOMAIN_KEYWORDS）→ 保留
    - 只命中弱关键词（WEAK_KEYWORDS）→ 命中否决词（VETO_KEYWORDS）则丢弃，否则保留
    - 一个字都没命中 → 丢弃（不是本领域）

    歧义术语（如「裁决权限」）可能因此一个来源都留不下 → 节点标 ai-generated，
    这是"宁可不给来源，也不给错来源"的预期行为，不是 bug。
    """
    keep = []
    for s in (sources or []):
        if not isinstance(s, dict):
            continue
        blob = "%s %s" % (s.get("title") or "", s.get("snippet") or "")
        blob = blob.lower()
        if any(_hit(blob, k) for k in DOMAIN_KEYWORDS):
            keep.append(s)
        elif any(_hit(blob, k) for k in WEAK_KEYWORDS):
            if not any(_hit(blob, k) for k in VETO_KEYWORDS):
                keep.append(s)
    return keep


def _map_nodes(nodes, worker, max_workers=1):
    """对每个节点执行 worker(node)。max_workers>1 时并发——
    每个节点是独立网络 I/O，串行会把总耗时乘以节点数。
    默认 1（串行）：保证注入假实现的测试结果确定、可复现。
    """
    if max_workers and max_workers > 1 and len(nodes) > 1:
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            list(ex.map(worker, nodes))
    else:
        for n in nodes:
            worker(n)


def _kb_reuse(n):
    """节点的 `kb_reuse` 标记（没有就回空 dict）。**一律当成不可信数据**：用户可手改。"""
    r = (n or {}).get("kb_reuse")
    return r if isinstance(r, dict) else {}


def _already_reused(n):
    """这个节点已经从知识库"够格复用"了 → ③ 该跳过它。"""
    return bool(_kb_reuse(n).get("reusable"))


def _timeline_already_reused(n):
    """时间线也是从库里拿的（`got == "all"`）→ ④ 该跳过它。"""
    r = _kb_reuse(n)
    return bool(r.get("reusable")) and r.get("got") == "all"


EXTRACT_SYSTEM = """你是资深 AI 领域技术面试官 + 知识拆解专家。
从岗位 JD 中抽出候选人**必须掌握**的核心知识点，供其学习备考。

只输出 JSON：{"nodes":[{"id":"英文短标识","term":"术语中文名","definition":"专业定义(60字内)",
"plain_explanation":"通俗解释：用大白话+一个具体例子，让完全外行也能听懂",
"category":"核心概念|前置知识"}]}

硬性要求：
- 12~15 个，宁少勿滥，必须是本岗位真正会考的概念
- 字符串内部禁止英文双引号（用中文引号「」），禁止裸换行
- 全部中文"""


def _call(chat, cfg, messages, **kw):
    """统一调用约定：chat(base_url, api_key, model, messages, **kw)"""
    tm = cfg["text_model"]
    opts = {"json_mode": True, "temperature": tm.get("temperature")}
    opts.update(kw)
    return chat(tm["base_url"], tm["api_key"], tm["model"], messages, **opts)


def normalize_node(n, layer=0):
    return {
        "id": (n.get("id") or "").strip() or store.slug(n.get("term") or "node"),
        "term": (n.get("term") or "").strip(),
        "definition": (n.get("definition") or "").strip(),
        "plain_explanation": (n.get("plain_explanation") or "").strip(),
        "category": (n.get("category") or "核心概念").strip(),
        "layer": layer,
        "timeline": [],
        "related": [],
        "sources": [],
        "confidence": "ai-generated",
        "interview_questions": [],
        "state": "待学习",
        "last_outcome": "未面试",
    }


def extract_nodes(jd_text, cfg, chat=None):
    """第 ① 步：从 JD 抽出核心知识点（失败时记日志并返回空列表，不抛）"""
    chat = chat or llm.call_openai_compatible
    try:
        reply = _call(chat, cfg, [
            {"role": "system", "content": EXTRACT_SYSTEM},
            {"role": "user", "content": "岗位 JD：\n%s\n\n请拆出 12~15 个核心知识点，输出 JSON。" % jd_text},
        ])
        data = llm.parse_json_reply(reply)
    except Exception as e:
        log.log_exc("knowledge.extract_error", e)
        return []
    raw = data.get("nodes")
    if not isinstance(raw, list):
        log.log_event("knowledge.extract_bad_shape", level="warn", got=type(raw).__name__)
        return []
    nodes = [normalize_node(n, 0) for n in raw if isinstance(n, dict) and n.get("term")]
    log.log_event("knowledge.extract_done", count=len(nodes))
    return nodes[:MAX_CORE]


def merge_sources(a, b):
    """按 url 去重合并来源"""
    seen, out = set(), []
    for s in list(a or []) + list(b or []):
        u = (s or {}).get("url")
        if u and u not in seen:
            seen.add(u)
            out.append(s)
    return out


BRAIN_SYSTEM = """你是知识网络构建专家。给定一个核心概念，列出候选人还需要了解的 2~3 个**关联知识点**。

只输出 JSON：{"related":[{"id":"英文短标识","term":"术语","definition":"专业定义(50字内)",
"plain_explanation":"通俗解释+例子","relation":"前置|对比|属于|应用"}]}

硬性要求：
- 2~3 个，必须与给定概念直接相关，不要凑数
- 字符串内部禁止英文双引号，禁止裸换行
- 全部中文"""


def brainstorm(core_nodes, cfg, chat=None):
    """第 ② 步：为每个核心知识点扩出第 1 层关联（并回填 core.related）"""
    if not core_nodes:
        return []
    chat = chat or llm.call_openai_compatible
    new_nodes, seen = [], {n.get("id") for n in core_nodes if n.get("id")}
    for node in core_nodes:
        try:
            reply = _call(chat, cfg, [
                {"role": "system", "content": BRAIN_SYSTEM},
                {"role": "user", "content": "核心概念：%s\n专业定义：%s\n\n请列出 2~3 个关联知识点。"
                                            % (node.get("term", ""), node.get("definition", ""))},
            ])
            data = llm.parse_json_reply(reply)
            related = data.get("related")
            if not isinstance(related, list):
                log.log_event("knowledge.brainstorm_bad_shape", level="warn",
                              term=node.get("term"), got=type(related).__name__)
                continue
            for r in related[:MAX_RELATED_PER_NODE]:
                if not isinstance(r, dict) or not r.get("term"):
                    continue
                child = normalize_node(r, 1)
                node.setdefault("related", []).append(
                    {"id": child["id"], "relation": (r.get("relation") or "相关").strip()})
                if child["id"] not in seen:
                    seen.add(child["id"])
                    new_nodes.append(child)
        except Exception as e:
            log.log_exc("knowledge.brainstorm_error", e, term=node.get("term"))
            continue
    log.log_event("knowledge.brainstorm_done", core=len(core_nodes), expanded=len(new_nodes))
    return new_nodes


TIMELINE_SYSTEM = """你是技术史专家。基于给出的**参考资料**，为该概念梳理「前世今生」时间线。

只输出 JSON：{"timeline":[{"year":"年份或年月","text":"发生了什么(30字内)"}]}

硬性要求：
- 4~6 条，按时间正序
- **只写参考资料里能支撑的事实**；资料不足就少写几条，绝不编造
- 字符串内部禁止英文双引号，禁止裸换行
- 全部中文"""


def attach_sources(nodes, providers, search=None, max_results=5, max_workers=1,
                   max_searches=MAX_SEARCHES, stats=None):
    """第 ③ 步：为知识点查真实来源；只留本领域结果，查不到/全被过滤 → ai-generated

    三层节制（都为了「不给错来源」+「不浪费 Tavily 额度」）：
    - 次数上限：核心节点（layer 0）优先，超出的直接跳过、不发起搜索
    - 候选条数：max_results=5（Tavily 按**请求**计费、不按条数，多拿候选是纯赚；
      反正 filter_relevant 会筛掉跑偏的，过滤后留下的更多）
    - 相关性过滤：强/弱关键词 + 否决词判定，非本领域一律丢弃（sources=[] 且标 ai-generated）

    `stats`（**可选出参**）是"这次到底搜了几次"的出口：传进来一个 dict，本函数原地写两个字段
    （`build_for_jd` 把 `reuse.stats` 传进来，报告里读的就是它）：
      * `searched` —— **真的发起过几次**搜索 = 「未被复用、且在上限之内」的节点数。
        ⚠️ **不是** `total - reusable`：那个推测值只算"复用省下的"，没算上限截断掉的，
        于是报告会**多报**联网条数（终审实测：10 个节点 + 上限 2 → 报告说「本次联网 4 条」，
        实际只搜了 2 次）。
      * `skipped_by_cap` —— **超出 `max_searches`、本来会搜、本次一次都没搜**的条数。
        口径故意**不含**"本来就跳过搜索的复用节点"：那些不是被上限坑的，把它们算进来
        会让报告说"它们没联网"（读的人会以为复用节点被上限挤掉了）。
    ⚠️ 它与日志里那个 `capped` **不是同一个数**：日志的 `capped` 是"被上限挤出去的总条数"
    （含复用节点，等于 `len(nodes) - len(selected)`），报告要的是前者，所以两个都记。
    为什么用出参而不是改返回值：返回值 `nodes` 是既有契约（`build_for_jd` 与一批测试都直接
    拿它当节点列表用），改成 `(nodes, stats)` 会把所有调用点一起拖进这次改动。
    """
    search = search or search_mod.search

    # 核心优先 + 上限；max_searches 为 0/None 时视为不限制
    # sorted 稳定：同层内保持原顺序，而核心节点（layer 0）本就在列表前面
    ordered = sorted(nodes, key=lambda n: n.get("layer", 0) or 0)
    selected = ordered[:max_searches] if max_searches else nodes
    keep_ids = {id(n) for n in selected}

    def one(n):
        if _already_reused(n):
            # 已从知识库整条复用（来源链接来自库、本次没联网）→ **绝不**再搜一次。
            # 置信度已在复用那一步按"有没有来源"定好，这里不动。
            return
        if id(n) not in keep_ids:
            n["sources"] = []
            n["confidence"] = "ai-generated"
            # 把"没联网"这件事写在节点上（见 `SEARCH_SKIPPED_CAPPED`）：报告必须能分清
            # 它与"联网了但一条都没留下"，否则会对这条卡说「已联网补全」（必修①）。
            n["search_skipped"] = SEARCH_SKIPPED_CAPPED
            return
        try:
            raw = search(build_query(n), providers=providers, max_results=max_results)
        except Exception as e:
            log.log_exc("knowledge.search_error", e, term=n.get("term"))
            raw = []
        if not isinstance(raw, list):
            raw = []
        kept = filter_relevant(raw)
        n["sources"] = kept
        n["confidence"] = "verified" if kept else "ai-generated"
        if raw and not kept:
            log.log_event("knowledge.source_filtered", level="warn",
                          term=n.get("term"), dropped=len(raw))

    # 复用节点虽也在 selected 里、却**根本没搜** → 先数出来，否则日志的 searched 会多报
    # （日志是本项目的取证材料，多报就是说了不真的话）。确定性计算，不需要锁。
    skipped = sum(1 for n in selected if _already_reused(n))
    _map_nodes(nodes, one, max_workers)
    # 真值一律**确定性算**、不在 worker 里累加：`max_workers>1` 时每个 worker 跑在各自线程，
    # `+=` 不是原子的；而这两个数就是 `one` 的两条分支判据本身，算出来与它逐条一致
    # （重复对象出现两次时也一致：`one` 会被调两次，这里也数两次）。
    searched = sum(1 for n in nodes if id(n) in keep_ids and not _already_reused(n))
    skipped_by_cap = sum(1 for n in nodes if id(n) not in keep_ids and not _already_reused(n))
    log.log_event("knowledge.sources_done",
                  verified=sum(1 for n in nodes if n["confidence"] == "verified"),
                  total=len(nodes), searched=searched,
                  capped=max(0, len(nodes) - len(selected)), kb_skipped=skipped,
                  skipped_by_cap=skipped_by_cap)
    if isinstance(stats, dict):
        stats["searched"] = searched
        stats["skipped_by_cap"] = skipped_by_cap
    return nodes


def attach_timeline(nodes, cfg, chat=None, max_workers=1, max_timelines=MAX_TIMELINES):
    """第 ④ 步：基于来源写「前世今生」时间线（无来源的跳过，不编造）

    超出 max_timelines 的有来源节点不再调用模型：它们保留 sources / verified，
    只是 timeline 为空（不伪造），并在日志里以 capped 计数。
    """
    chat = chat or llm.call_openai_compatible
    sourced = [n for n in nodes if n.get("sources")]
    # sorted 稳定：同层内保持原顺序，而核心节点（layer 0）本就在列表前面
    selected = sorted(sourced, key=lambda n: n.get("layer") or 0)[:max_timelines]
    capped = len(sourced) - len(selected)

    def one(n):
        if _timeline_already_reused(n):
            # 时间线也是复用的 → 不再调模型（这是复用省下的第二笔）
            return
        if not n.get("sources"):
            return
        try:
            refs = "\n".join(
                "- %s | %s | %s" % (str(s.get("title") or ""),
                                    str(s.get("snippet") or "")[:200],
                                    str(s.get("url") or ""))
                for s in n["sources"][:5] if isinstance(s, dict))
            reply = _call(chat, cfg, [
                {"role": "system", "content": TIMELINE_SYSTEM},
                {"role": "user", "content": "概念：%s\n\n参考资料：\n%s\n\n请输出时间线 JSON。"
                                            % (n.get("term", ""), refs)},
            ])
            data = llm.parse_json_reply(reply)
            tl = data.get("timeline")
            if not isinstance(tl, list):
                n["timeline"] = []
                return
            n["timeline"] = [{"year": str(t.get("year", "")).strip(), "text": (t.get("text") or "").strip()}
                             for t in tl if isinstance(t, dict) and (t.get("text") or "").strip()]
        except Exception as e:
            log.log_exc("knowledge.timeline_error", e, term=n.get("term"))
            n["timeline"] = []

    _map_nodes(selected, one, max_workers)
    log.log_event("knowledge.timeline_done", with_timeline=sum(1 for n in nodes if n["timeline"]),
                  capped=capped)
    return nodes


def build_for_jd(jd_text, cfg, core_limit=MAX_CORE, reuse=None, search=None, chat=None):
    """编排：① 拆解 → ② 头脑风暴 →（新增）库优先复用 → ③ 搜索 → ④ 时间线

    `reuse`（可选）是一个带 `.plan(nodes) -> stats` 的对象（生产里是 `kbreuse.Reuse`）。
    **不传 = 今天的行为**（纯联网）—— 刻意如此：新能力默认关闭，由 `app.do_analyze` 显式打开。
    """
    core = extract_nodes(jd_text, cfg, chat=chat)[:core_limit]
    expanded = brainstorm(core, cfg, chat=chat)
    all_nodes = core + expanded
    stats = reuse.plan(all_nodes) if reuse is not None else None
    # ③ 的真实搜索次数**写回** `reuse.stats`（`app.do_analyze` 报告里读的就是那个 dict）。
    # `plan_nodes` 给的 `searched` 是**推测值**（`total - reusable`）：它只知道"复用省下了
    # 几次"，**不知道** `max_searches` 上限。上限截断与真实次数只有 ③ 知道，所以在这里收口 ——
    # 不收口的话报告会对"超出上限、根本没搜"的卡说「已联网补全」（必修①，终审实测）。
    # `Reuse.plan` 里 `self.stats` 与这里的 `stats` 是**同一个对象**，原地更新即可；
    # 万一别的 `reuse` 实现每次返回新 dict，也优先写它自己那个 `.stats`（报告读的是它）。
    sink = stats
    own = getattr(reuse, "stats", None) if reuse is not None else None
    if isinstance(own, dict):
        sink = own
    attach_sources(all_nodes, cfg.get("search_providers") or [],
                   search=search, max_results=5, max_workers=6,
                   max_searches=int(cfg.get("max_searches") or MAX_SEARCHES),
                   stats=sink)
    attach_timeline(all_nodes, cfg, chat=chat, max_workers=6)
    log.log_event("knowledge.build_done", core=len(core), expanded=len(expanded),
                  verified=sum(1 for n in all_nodes if n["confidence"] == "verified"),
                  kb_reused=(stats or {}).get("reusable", 0),
                  kb_matched=(stats or {}).get("matched", 0))
    return all_nodes
