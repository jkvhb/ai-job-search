# -*- coding: utf-8 -*-
"""知识库（阶段 3.1）：跨岗位累积与复习。

与 knowledge.py 的分工：
  knowledge.py —— 从一份 JD **生成**知识（调模型、调搜索，有额度成本）
  kb.py        —— 把生成出来的知识**跨岗位累积**、查询、复习（纯数据运算，零额度、可离线）

三条铁律：
  1. 用户学习进度（state / last_outcome / asked_count）绝不被合并覆盖 —— 合并方向永远是
     「你的操作 → 库」，绝不「库 → 覆盖你」
  2. 合并幂等：同一份报告吸收两次，结果相同（from_jds / sources 都不翻倍）
  3. sources 完整保留 —— 用户明确强调「来源尤其关键」，知识库里必须能点进原文
"""
import log
import os
import re
import store
import unicodedata
from datetime import datetime

# 本模块的配置面：下面这些常量供后续任务（来源清洗 / 合并 / 查询 / 回填）使用，
# 提前集中声明是为了让契约可见，不是未使用的死代码。
# **导入只写当前用到的**（log / os / re / store / unicodedata / datetime）；glob / json 到真正
# 用到它们的那个任务再加，否则会被代码质量审查判为未使用导入。
KB_VERSION = 1
SNIPPET_LIMIT = 400      # knowledge.json 里的摘要截断长度（完整摘要仍在报告快照里）
MAX_QUESTIONS = 8        # 面试题并集上限
STATE_VALUES = ("待学习", "学习中", "已掌握")
DEFAULT_STATE = "待学习"
DEFAULT_OUTCOME = "未面试"

# 去掉这些后缀后若完全相同，则两个术语疑似同一概念（仅提示，绝不自动合并）
_TAIL_WORDS = ("体系", "机制", "方法", "流程", "策略", "规范", "标准", "系统")

# 标点/空白（含全角）：身份归一用
_PUNCT = re.compile(r"[\s\u3000·・、,，.。;；:：!！?？\"'“”‘’()（）\[\]【】<>《》/\\|_\-—－+*#~`]+")

REPORT_MARK = "const REPORT_DATA = "


def normalize_id(term):
    """术语 → 知识库身份键：NFKC 归一、小写、去标点与空白。

    这是跨岗位认定「同一个概念」的唯一依据。空/纯标点返回空串（调用方应跳过，避免垃圾卡）。

    为什么要先 NFKC：中文输入法极易打出全角字符（ＡＩ／（１）），而全角与半角是**同一个概念**
    的两种写法。不归一的话 "ＡＩ标注" 与 "AI标注" 会算成两张卡，违背「一个概念 = 一张卡」。
    """
    if not isinstance(term, str):
        return ""
    return _PUNCT.sub("", unicodedata.normalize("NFKC", term)).lower()


def _as_int(v, default=0):
    """容错取整数：'1' → 1；'abc'/None/[] → default（绝不抛异常）"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _today():
    return datetime.now().strftime("%Y-%m-%d")


def empty_kb():
    return {"version": KB_VERSION, "nodes": []}


def ensure_kb(kb):
    """把任意输入（含损坏结构）规整成可用知识库，绝不抛异常。"""
    if not isinstance(kb, dict):
        return empty_kb()
    nodes = kb.get("nodes")
    if not isinstance(nodes, list):
        nodes = []
    return {"version": kb.get("version") or KB_VERSION,
            "nodes": [n for n in nodes if isinstance(n, dict)]}


def _as_list(v):
    return v if isinstance(v, list) else []


def clean_sources(src):
    """清洗来源：只留 dict 且 url 非空；摘要截断；按 url 去重（保留信息更全的一条）。

    历史数据里 sources 可能缺 snippet/date（旧报告只存了 title/url），必须容忍缺失，
    绝不可因此丢掉整条来源。
    """
    out = []
    seen = {}
    for s in _as_list(src):
        if not isinstance(s, dict):
            continue
        url = str(s.get("url") or "").strip()
        if not url:
            continue
        item = {
            "title": str(s.get("title") or "").strip(),
            "url": url,
            "date": str(s.get("date") or "").strip(),
            "accessed": str(s.get("accessed") or "").strip(),
            "snippet": str(s.get("snippet") or "").strip()[:SNIPPET_LIMIT],
        }
        old = seen.get(url)
        if old is None:
            seen[url] = item
            out.append(item)
            continue
        if len(item["snippet"]) > len(old["snippet"]):
            old["snippet"] = item["snippet"]
        for k in ("title", "date", "accessed"):
            if not old[k] and item[k]:
                old[k] = item[k]
    return out


def clean_timeline(tl):
    out = []
    for t in _as_list(tl):
        if not isinstance(t, dict):
            continue
        text = str(t.get("text") or "").strip()
        if not text:
            continue
        out.append({"year": str(t.get("year") or "").strip(), "text": text})
    return out


def clean_related(rel):
    out = []
    seen = set()
    for r in _as_list(rel):
        if not isinstance(r, dict):
            continue
        rid = str(r.get("id") or "").strip()
        if not rid or rid in seen:
            continue
        seen.add(rid)
        out.append({"id": rid, "relation": str(r.get("relation") or "").strip()})
    return out


def clean_questions(qs):
    out = []
    for q in _as_list(qs):
        q = str(q or "").strip()
        if q and q not in out:
            out.append(q)
    return out[:MAX_QUESTIONS]


def _add_jd(card, jd):
    """记录「这个知识点被哪个岗位提到过」，按 job_id 去重（幂等的关键）"""
    if not isinstance(jd, dict) or not jd.get("id"):
        return
    # 顺手清掉历史垃圾项（字符串/数字/None）：留着会被统计、回填与前端当成岗位记录踩到
    frm = [x for x in _as_list(card.get("from_jds")) if isinstance(x, dict)]
    if any(x.get("id") == jd["id"] for x in frm):
        return
    frm.append({"id": jd["id"], "job_title": jd.get("job_title") or "",
                "company": jd.get("company") or "", "date": jd.get("date") or _today()})
    card["from_jds"] = frm


def _new_card(node, jd):
    term = str(node.get("term") or "").strip()
    card = {
        "id": normalize_id(term),
        "term": term,
        "aliases": [],
        "definition": str(node.get("definition") or "").strip(),
        "plain_explanation": str(node.get("plain_explanation") or "").strip(),
        "category": str(node.get("category") or "").strip(),
        "layer": max(0, _as_int(node.get("layer"))),
        "sources": clean_sources(node.get("sources")),
        "timeline": clean_timeline(node.get("timeline")),
        "related": clean_related(node.get("related")),
        "interview_questions": clean_questions(node.get("interview_questions")),
        "from_jds": [],
        "state": DEFAULT_STATE,
        "last_outcome": DEFAULT_OUTCOME,
        "asked_count": 0,
        "first_seen": _today(),
        "last_seen": _today(),
    }
    _add_jd(card, jd)
    _recompute_confidence(card)
    return card


def _recompute_confidence(card):
    card["confidence"] = "verified" if _as_list(card.get("sources")) else "ai-generated"


def _merge_into(card, node):
    """把新节点的内容并进已有卡片。

    **用户进度（state / last_outcome / asked_count）一律不动** —— 见文件头铁律 1。
    """
    card["sources"] = clean_sources(_as_list(card.get("sources")) + _as_list(node.get("sources")))

    tl_old = clean_timeline(card.get("timeline"))
    tl_new = clean_timeline(node.get("timeline"))
    card["timeline"] = tl_new if len(tl_new) > len(tl_old) else tl_old

    # 定义/通俗解释：不用更短的值覆盖现有内容（保留更全的）
    for key in ("definition", "plain_explanation"):
        new_val = str(node.get(key) or "").strip()
        if len(new_val) > len(str(card.get(key) or "").strip()):
            card[key] = new_val

    if not str(card.get("category") or "").strip():
        card["category"] = str(node.get("category") or "").strip()

    # 下界夹 0：min 是单调的，一旦让 -3 落盘，之后再来合法值也永远回不来
    card["layer"] = min(max(0, _as_int(card.get("layer"))), max(0, _as_int(node.get("layer"))))

    card["related"] = clean_related(_as_list(card.get("related")) + _as_list(node.get("related")))
    card["interview_questions"] = clean_questions(
        _as_list(card.get("interview_questions")) + _as_list(node.get("interview_questions")))

    # 补齐可能缺失/损坏的进度字段，但绝不覆盖已有真值。
    # 这里必须用 or 而不是 setdefault：knowledge.json 是用户可手改的，显式的 null 骗得过
    # setdefault，却会让状态筛选与进度按钮全部落空、让复习调度的 +1 直接抛 TypeError。
    # _as_int 顺带修掉 "3" 这类字符串写法。
    card["state"] = card.get("state") or DEFAULT_STATE
    card["last_outcome"] = card.get("last_outcome") or DEFAULT_OUTCOME
    card["asked_count"] = _as_int(card.get("asked_count"))
    card.setdefault("first_seen", _today())
    card["last_seen"] = _today()


def _quarantine_corrupt_kb():
    """损坏的 knowledge.json 先改名备份，绝不当空库静默覆盖。

    store.read_json 把「解析失败」和「文件为空」都归成 None，而 absorb 末尾会把内存里的库写回
    文件 —— 一次坏读就等于清空用户全部积累。判据必须是「文件有内容 **且** 解析失败」：
    正常的空库 {"nodes": []} 解析成功，绝不会被误备份走。
    """
    path = store.knowledge_path()
    if store.read_json(path, None) is not None:
        return
    if not store.read_text(path, "").strip():
        return                       # 文件不存在或为空：没有可丢的数据
    backup = "%s.corrupt-%s" % (path, datetime.now().strftime("%Y%m%d%H%M%S"))
    try:
        os.replace(path, backup)
    except OSError as e:
        log.log_event("kb.corrupt_backup_failed", level="warn", error=str(e))
        return
    log.log_event("kb.corrupt_backup", level="warn", backup=os.path.basename(backup))


def absorb(nodes, jd=None, kb=None):
    """把一次分析（或一份报告）的知识点并入知识库。返回 (added, merged)。

    幂等：同一个 jd['id'] 重复吸收不会重复计数 from_jds，来源也不会翻倍。

    写盘之前先保住「读不出来」的旧文件（见 _quarantine_corrupt_kb）—— 本函数末尾一定会
    save_knowledge，不先备份的话坏文件会被内存里的空库永久顶掉。
    """
    _quarantine_corrupt_kb()
    kb = ensure_kb(kb if kb is not None else store.load_knowledge())
    index = {n.get("id"): n for n in kb["nodes"] if n.get("id")}
    added = merged = 0
    for node in _as_list(nodes):
        if not isinstance(node, dict):
            continue
        nid = normalize_id(str(node.get("term") or "").strip())
        if not nid:
            continue                      # 空术语/纯标点 → 跳过，不产生垃圾卡
        card = index.get(nid)
        if card is None:
            card = _new_card(node, jd)
            kb["nodes"].append(card)
            index[nid] = card
            added += 1
        else:
            _merge_into(card, node)
            _add_jd(card, jd)
            _recompute_confidence(card)
            merged += 1
    kb["version"] = KB_VERSION
    store.save_knowledge(kb)
    return added, merged


def set_state(node_id, state, kb=None):
    """只改学习状态，不碰任何其它字段。返回 (ok, msg)。

    node_id 既可以是身份键，也可以是术语写法：先过 normalize_id 抹平大小写/全角/标点差异
    （对已经归一化的 id 是 no-op），这样前端传 id、脚本或人传术语都能用。
    """
    if state not in STATE_VALUES:
        return False, "状态不合法：%s" % state
    node_id = normalize_id(node_id)
    kb = ensure_kb(kb if kb is not None else store.load_knowledge())
    for n in kb["nodes"]:
        if n.get("id") == node_id:
            n["state"] = state
            store.save_knowledge(kb)
            log.log_event("kb.state_set", id=node_id, state=state)
            return True, ""
    return False, "找不到该知识点"


def _strip_tail(t):
    for w in _TAIL_WORDS:
        if t.endswith(w) and len(t) > len(w):
            return t[: -len(w)]
    return t


def _is_subsequence(short, long_):
    """short 的字符是否**按顺序**都出现在 long_ 里（允许中间插入别的字符）。

    用于识别「插入式变体」：`省略恢复` 与 `省略识别及恢复` —— 前者并不是后者的连续子串
    （中间插了「识别及」），但每个字都按序出现。
    """
    it = iter(long_)
    return all(ch in it for ch in short)


def _dup_reason(a, b):
    """两个身份键疑似同一概念的原因；不是则 None。"""
    if not a or not b or a == b:
        return None
    if a in b or b in a:
        return "包含" if abs(len(a) - len(b)) <= 4 else None
    # 插入式变体：不是连续子串，但按序都能对上。
    # 短词门槛 ≥3 字是必要的：2 字词极易互相成为子序列，会刷出一堆假配对。
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) >= 3 and abs(len(a) - len(b)) <= 4 and _is_subsequence(short, long_):
        return "插入式"
    if _strip_tail(a) == _strip_tail(b):
        return "同义后缀"
    # 首尾相近：加长度门槛，否则短术语（4 字以内）会共享首尾字产生大量假配对
    if len(a) >= 6 and len(b) >= 6:
        n = min(len(a), len(b))
        p = 0
        while p < n and a[p] == b[p]:
            p += 1
        s = 0
        while s < n - p and a[-1 - s] == b[-1 - s]:
            s += 1
        if p >= 4 and s >= 2:
            return "首尾相近"
    return None


def find_duplicates(kb, limit=20):
    """启发式找出可能指同一概念的术语对。**只提示，绝不自动合并**（错并难发现）。"""
    ids = [n.get("id") for n in ensure_kb(kb)["nodes"] if n.get("id")]
    out = []
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            reason = _dup_reason(ids[i], ids[j])
            if reason:
                out.append({"a": ids[i], "b": ids[j], "reason": reason})
                if len(out) >= limit:
                    return out
    return out


def merge_nodes(keep_id, drop_id, kb=None):
    """手动合并：把 drop 并入 keep，drop 的写法记进 aliases 后移除。返回 (ok, msg)。

    两个参数既可以是身份键，也可以是术语写法（先过 normalize_id；对已归一化的 id 是 no-op）——
    否则「A词」这类带大写的术语会被判成「找不到」，让人以为知识点丢了。
    keep 的学习进度保持不变（合并方向永远是「你的进度 → 保留」，不用 drop 覆盖）。
    """
    keep_id, drop_id = normalize_id(keep_id), normalize_id(drop_id)
    if not keep_id or not drop_id:
        return False, "两个知识点都要选"
    if keep_id == drop_id:
        return False, "不能和自己合并"
    kb = ensure_kb(kb if kb is not None else store.load_knowledge())
    index = {n.get("id"): n for n in kb["nodes"] if n.get("id")}
    keep, drop = index.get(keep_id), index.get(drop_id)
    if keep is None or drop is None:
        return False, "找不到要合并的知识点"

    _merge_into(keep, drop)

    aliases = _as_list(keep.get("aliases"))
    for a in [drop.get("term")] + _as_list(drop.get("aliases")):
        a = str(a or "").strip()
        if a and a not in aliases:
            aliases.append(a)
    keep["aliases"] = aliases
    for jd in _as_list(drop.get("from_jds")):
        _add_jd(keep, jd)
    _recompute_confidence(keep)

    kb["nodes"] = [n for n in kb["nodes"] if n.get("id") != drop_id]
    store.save_knowledge(kb)
    log.log_event("kb.merge_done", keep=keep_id, drop=drop_id, aliases=len(aliases))
    return True, "已把「%s」并入「%s」" % (drop.get("term"), keep.get("term"))
