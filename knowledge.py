# -*- coding: utf-8 -*-
"""知识管线：① 拆解 ② 头脑风暴 ③ 搜索 ④ 校验生成"""
import json

import llm
import log
import search as search_mod
import store

MAX_CORE = 15
MAX_RELATED_PER_NODE = 3

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
    new_nodes, seen = [], {n["id"] for n in core_nodes}
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


def attach_sources(nodes, providers, search=None, max_results=3):
    """第 ③ 步：为每个知识点查真实来源；查不到标 ai-generated"""
    search = search or search_mod.search
    for n in nodes:
        try:
            res = search(n["term"] + " 是什么", providers=providers, max_results=max_results)
        except Exception as e:
            log.log_exc("knowledge.search_error", e, term=n.get("term"))
            res = []
        n["sources"] = res if isinstance(res, list) else []
        n["confidence"] = "verified" if res else "ai-generated"
    log.log_event("knowledge.sources_done",
                  verified=sum(1 for n in nodes if n["confidence"] == "verified"), total=len(nodes))
    return nodes


def attach_timeline(nodes, cfg, chat=None):
    """第 ④ 步：基于来源写「前世今生」时间线（无来源的跳过，不编造）"""
    chat = chat or llm.call_openai_compatible
    for n in nodes:
        if not n.get("sources"):
            continue
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
                continue
            n["timeline"] = [{"year": str(t.get("year", "")).strip(), "text": (t.get("text") or "").strip()}
                             for t in tl if isinstance(t, dict) and (t.get("text") or "").strip()]
        except Exception as e:
            log.log_exc("knowledge.timeline_error", e, term=n.get("term"))
            n["timeline"] = []
    log.log_event("knowledge.timeline_done", with_timeline=sum(1 for n in nodes if n["timeline"]))
    return nodes


def build_for_jd(jd_text, cfg, core_limit=MAX_CORE):
    """编排：① 拆解 → ② 头脑风暴 → ③ 搜索 → ④ 时间线"""
    core = extract_nodes(jd_text, cfg)[:core_limit]
    expanded = brainstorm(core, cfg)
    all_nodes = core + expanded
    attach_sources(all_nodes, cfg.get("search_providers") or [])
    attach_timeline(all_nodes, cfg)
    log.log_event("knowledge.build_done", core=len(core), expanded=len(expanded),
                  verified=sum(1 for n in all_nodes if n["confidence"] == "verified"))
    return all_nodes
