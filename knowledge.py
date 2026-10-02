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
    """第 ① 步：从 JD 抽出核心知识点"""
    chat = chat or llm.call_openai_compatible
    reply = _call(chat, cfg, [
        {"role": "system", "content": EXTRACT_SYSTEM},
        {"role": "user", "content": "岗位 JD：\n%s\n\n请拆出 12~15 个核心知识点，输出 JSON。" % jd_text},
    ])
    data = llm.parse_json_reply(reply)
    nodes = [normalize_node(n, 0) for n in (data.get("nodes") or []) if (n or {}).get("term")]
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
