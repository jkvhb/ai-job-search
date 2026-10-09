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
import glob
import itertools
import json
import log
import os
import re
import store
import threading
import unicodedata
from datetime import datetime

# 本模块的配置面：下面这些常量供后续任务（来源清洗 / 合并 / 查询 / 回填）使用，
# 提前集中声明是为了让契约可见，不是未使用的死代码。
# **导入只写当前用到的**（glob / json / log / os / re / store / unicodedata / datetime）——
# 到真正用到它们的那个任务再加，否则会被代码质量审查判为未使用导入。
KB_VERSION = 1
SNIPPET_LIMIT = 400      # knowledge.json 里的摘要截断长度（完整摘要仍在报告快照里）
MAX_QUESTIONS = 8        # 面试题并集上限
STATE_VALUES = ("待学习", "学习中", "已掌握")
DEFAULT_STATE = "待学习"
DEFAULT_OUTCOME = "未面试"

# 自测结果只有两种：答上了 / 没答上。用常量而不是裸字符串，避免前端/路由/存储三处拼写漂移。
RESULT_UP = "答上了"
RESULT_DOWN = "没答上"
_RESULTS = (RESULT_UP, RESULT_DOWN)

# 掌握度排序：手动合并两张「用户卡」时用 max 取更靠后的那个状态（见 merge_nodes）
_STATE_RANK = {s: i for i, s in enumerate(STATE_VALUES)}

# 去掉这些后缀后若完全相同，则两个术语疑似同一概念（仅提示，绝不自动合并）
_TAIL_WORDS = ("体系", "机制", "方法", "流程", "策略", "规范", "标准", "系统")

# 标点/空白（含全角）：身份归一用
_PUNCT = re.compile(r"[\s\u3000·・、,，.。;；:：!！?？\"'“”‘’()（）\[\]【】<>《》/\\|_\-—－+*#~`]+")

REPORT_MARK = "const REPORT_DATA = "

# 隔离备份名的自增序号：只到秒会互相覆盖，加 pid 也挡不住「同一进程同一秒内两次隔离」。
# 用 itertools.count 而不是 `+= 1`：后者是 LOAD/ADD/STORE 三步，在 ThreadingHTTPServer 下
# 两个请求线程可能读到同一个旧值，导致备份名相撞、先备份的文件被后一个覆盖掉。
_QUARANTINE_SEQ = itertools.count(1)

# knowledge.json 的读-改-写必须整体串行。本模块的每个写路径都是
# 「load_knowledge → 改内存 → save_knowledge」三步，而这三步之间是**没有**保护的：
# absorb 要 7.5–24ms、set_state 要 10–15ms，ThreadingHTTPServer 下「分析结尾的 absorb」
# 与「用户在知识库 tab 点掌握度」完全会撞上（分析要 20–60 秒且只禁用 #goBtn）。
# 撞上的后果是丢更新：后写完的那个线程把先写线程的改动整个覆盖掉（实测 120 轮丢 31–99 次）。
#
# 用 RLock 而不是 Lock：absorb_reports() 在循环里调 absorb()，将来若有别的写路径互相调用，
# 普通 Lock 会直接死锁；RLock 让同一线程可重入。风格照 log.py 的 _LOG_LOCK。
# store.write_json 的原子写是第二道防线（保证文件不会被读成半截），
# 这道锁才是第一道：保证「读到的旧内容」不会把别人的改动盖掉。
_KB_LOCK = threading.RLock()


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


def _normalise_review_fields(card):
    """补/修复习三字段。容错：非 int → _as_int；非 str → 空串；结果值非法 → 空串。

    为什么放在 ensure_kb 这条咽喉上：knowledge.json 是用户可手改的（也可能来自旧版本），
    缺字段的卡片一进 review_queue / record_review 就会 KeyError 或把 "+1" 算成字符串拼接。
    和 from_jds 清洗同一个理由 —— 所有查询与序列化都经过这里，只改一处就全口径归位。
    """
    card["review_count"] = _as_int(card.get("review_count"))
    dt = card.get("last_reviewed_at")
    card["last_reviewed_at"] = dt.strip() if isinstance(dt, str) else ""
    r = card.get("last_result")
    card["last_result"] = r.strip() if isinstance(r, str) and r.strip() in _RESULTS else ""
    return card


def empty_kb():
    return {"version": KB_VERSION, "nodes": []}


def ensure_kb(kb):
    """把任意输入（含损坏结构）规整成可用知识库，绝不抛异常。

    **注意：这个函数有副作用 —— 它原地过滤 `from_jds`。**
    每个节点的 `from_jds` 里「id 不是非空字符串」的项会被**就地丢掉**（直接改传进来的 dict，
    不是返回一份副本）。之所以选在这里做，是因为它**唯一咽喉点**：stats() / list_nodes() /
    all_jds() / find_duplicates() 以及 /api/knowledge 序列化给前端的节点**全部**经过这里，
    只改一处就能让四处口径同时归位。

    不清洗会怎样：knowledge.json 是用户可手改的，手改出 `from_jds:[{"id":5},{"id":"j1"}]` 后
    会出现「stats 数 2 个岗位、排序按 2 排、前端徽章显示 2 个，而筛选下拉里只有 1 个」的幻影。
    过滤后 all_jds() 那段只认字符串 id 的逻辑退化为第二道防线（保留，防止有人绕过 ensure_kb）。
    """
    if not isinstance(kb, dict):
        return empty_kb()
    nodes = kb.get("nodes")
    if not isinstance(nodes, list):
        nodes = []
    nodes = [n for n in nodes if isinstance(n, dict)]
    for n in nodes:
        # from_jds 不是 list 时按空处理；顺手把原地清洗结果写回节点（这就是副作用本身）
        n["from_jds"] = [x for x in _as_list(n.get("from_jds"))
                         if isinstance(x, dict) and isinstance(x.get("id"), str) and x["id"]]
        # 复习三字段也在同一个咽喉点补齐/修坏：与 from_jds 同样「原地改」的副作用
        _normalise_review_fields(n)
    return {"version": kb.get("version") or KB_VERSION, "nodes": nodes}


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
    """记录「这个知识点被哪个岗位提到过」，按 job_id 去重（幂等的关键）。

    历史项和传进来的 `jd` 都按**同一把尺子**过滤：只有 id 是**非空字符串**的 dict 才算一条来源岗位。
    没有 id 的、以及真值但非字符串的 id（`{"id": 5}` / `{"id": [1]}`）留着会被 stats()["multi_jd"]
    计入，而 all_jds() 又会跳过它 —— 同一个库两处口径打架，前端会显示出「被 2 个以上岗位提到」
    却在下拉里找不到对应岗位的幻影。

    入参那道守卫也必须过字符串关：merge_nodes() 会把 drop 卡（用户可手改的 knowledge.json）里的
    from_jds **原样**喂进来，只压历史项等于给幻影留了后门。
    """
    if not isinstance(jd, dict) or not isinstance(jd.get("id"), str) or not jd["id"]:
        return
    # 顺手清掉历史垃圾项（字符串/数字/列表/None，以及没有 id 的 dict）
    frm = [x for x in _as_list(card.get("from_jds"))
           if isinstance(x, dict) and isinstance(x.get("id"), str) and x["id"]]
    # 先落盘清洗结果再去重：这个岗位早就记过时下面会 return，手改进去的垃圾不能因此留在原地
    if not any(x.get("id") == jd["id"] for x in frm):
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
        "review_count": 0,
        "last_reviewed_at": "",
        "last_result": "",
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
    """损坏的 knowledge.json 先改名备份，绝不当空库静默覆盖。返回 True = 可以继续写盘。

    store.read_json 把「解析失败」和「文件为空」都归成 None，而 absorb 末尾会把内存里的库写回
    文件 —— 一次坏读就等于清空用户全部积累。判据必须是「文件有内容 **且** 解析失败」：
    正常的空库 {"nodes": []} 解析成功，绝不会被误备份走。

    改名失败时返回 False（调用方 absorb 会放弃这次写盘）：宁可这次不沉淀，
    也绝不能让坏文件被内存里的空库顶掉。
    """
    path = store.knowledge_path()
    if store.read_json(path, None) is not None:
        return True
    if not store.read_text(path, "").strip():
        return True                  # 文件不存在或为空：没有可丢的数据
    # 文件名带自增序号：同秒内两次隔离（或两个进程）绝不能互相覆盖备份
    seq = next(_QUARANTINE_SEQ)
    backup = "%s.corrupt-%s-%d-%04d" % (path, datetime.now().strftime("%Y%m%d%H%M%S"),
                                       os.getpid(), seq)
    try:
        os.replace(path, backup)
    except OSError as e:
        log.log_event("kb.corrupt_backup_failed", level="warn", error=str(e))
        return False
    log.log_event("kb.corrupt_backup", level="warn", backup=os.path.basename(backup))
    return True


def absorb(nodes, jd=None, kb=None):
    """把一次分析（或一份报告）的知识点并入知识库。返回 (added, merged)。

    幂等：同一个 jd['id'] 重复吸收不会重复计数 from_jds，来源也不会翻倍。

    写盘之前先保住「读不出来」的旧文件（见 _quarantine_corrupt_kb）—— 本函数末尾一定会
    save_knowledge，不先备份的话坏文件会被内存里的空库永久顶掉。备份都失败时直接放弃本次吸收。

    **零可吸收节点时在隔离之前就返回 (0, 0)**：一个节点都没有的调用（报告知识点全是空术语/
    纯标点）本来就没有任何东西可沉淀，却会走完隔离 + save_knowledge —— 于是「库文件坏 + 本次
    零知识点」的组合会先把用户的库改名走，再写回一份空库，等于「吸收了个寂寞还把库清空」。
    判断必须在 _quarantine_corrupt_kb() **之前**做，否则坏文件已经被改名走了。
    """
    # 先筛出真正可吸收的节点（术语是非空字符串、normalize_id 后也非空），再决定做不做隔离与写盘。
    # 这里必须整节点保留（不是只留 term）：_new_card / _merge_into 还要读 definition / sources /
    # timeline / related / interview_questions 等字段，只留 term 会把它们全丢掉。
    #
    # 为什么要 isinstance(term, str) 这道类型关（不能只靠 normalize_id）：`str(5)` → `"5"`、
    # `str(None)` → `"None"` 都是**非空**的合法身份键，`{"term": 5}` 会凭空造出一张 id 为 "5"、
    # term 为 "5" 的垃圾卡；而 find_duplicates() 与前端本来就把非字符串 id 当幻影跳过 ——
    # 那会造出「有卡片、却查不到也合并不了」的死节点。非字符串术语一律不算可吸收。
    absorbable = [n for n in _as_list(nodes)
                  if isinstance(n, dict) and isinstance(n.get("term"), str)
                  and normalize_id(n["term"].strip())]
    if not absorbable:
        return 0, 0                      # 零可吸收节点：连隔离都不做，文件原样不动

    # 整段「检查坏文件 → load → 改内存 → save」必须串行：见 _KB_LOCK 的注释
    with _KB_LOCK:
        if not _quarantine_corrupt_kb():
            return 0, 0
        kb = ensure_kb(kb if kb is not None else store.load_knowledge())
        index = {n.get("id"): n for n in kb["nodes"] if n.get("id")}
        added = merged = 0
        for node in absorbable:
            nid = normalize_id(node["term"].strip())   # 上面已保证 term 是字符串且非空
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

    参数可以是**身份键**也可以是**原始术语**（内部先 normalize_id，对已归一化的 id 是 no-op）。
    """
    if state not in STATE_VALUES:
        return False, "状态不合法：%s" % state
    node_id = normalize_id(node_id)
    # load → 改 → save 整段串行：不加锁会被并发的 absorb 用「它读到的旧库」覆盖掉这次掌握度
    with _KB_LOCK:
        kb = ensure_kb(kb if kb is not None else store.load_knowledge())
        for n in kb["nodes"]:
            if n.get("id") == node_id:
                n["state"] = state
                store.save_knowledge(kb)
                log.log_event("kb.state_set", id=node_id, state=state)
                return True, ""
        return False, "找不到该知识点"


_REVIEW_WEIGHT = {"待学习": 3, "学习中": 2, "已掌握": 1}
_REVIEW_AGE_CAP = 30        # 久未复习的加成上限（天），防止"从没看过"无限压过一切


def _valid_jds(card):
    """只算「有非空字符串 id」的来源岗位 —— 与 all_jds / stats 同一把尺子。"""
    return [j for j in _as_list(card.get("from_jds"))
            if isinstance(j, dict) and isinstance(j.get("id"), str) and j["id"]]


def _days_since(date_str):
    try:
        d = datetime.strptime(str(date_str), "%Y-%m-%d").date()
    except Exception:
        return _REVIEW_AGE_CAP              # 从没复习过（或格式坏了）→ 按最久算
    return max(0, (datetime.now().date() - d).days)


def _review_score(card):
    """优先级 = 状态权重 × (1 + 被多少岗位提到) × (1 + 久未复习加成)。

    全部是乘性：任何一个维度为 0 都不会把分数压成 0（未掌握但只被 1 个岗位提到，仍应排在
    已掌握且刚复习过的前面）。
    """
    state = str(card.get("state") or DEFAULT_STATE)
    weight = _REVIEW_WEIGHT.get(state, 1)
    jd_n = len(_valid_jds(card))
    age = min(_days_since(card.get("last_reviewed_at")), _REVIEW_AGE_CAP)
    return weight * (1 + jd_n) * (1 + age / 7.0)


def _review_reason(card):
    """给人看的排序理由 —— 黑箱排序会让人不信任它。"""
    state = str(card.get("state") or DEFAULT_STATE)
    parts = [state]
    jd_n = len(_valid_jds(card))
    if jd_n > 1:
        parts.append("被 %d 个岗位提到" % jd_n)
    age = _days_since(card.get("last_reviewed_at"))
    parts.append("还没复习过" if age >= _REVIEW_AGE_CAP else "%d 天没复习" % age)
    return " · ".join(parts)


def review_queue(kb, limit=20):
    """按优先级排出的自测队列。每项 = 节点副本 + score + reason。"""
    nodes = list(ensure_kb(kb)["nodes"])
    out = []
    for n in nodes:
        item = dict(n)
        item["score"] = round(_review_score(n), 3)
        item["reason"] = _review_reason(n)
        out.append(item)
    out.sort(key=lambda x: (-x["score"], str(x.get("term") or "")))
    return out[:limit] if limit else out


def record_review(node_id, result, kb=None):
    """记录一次自测结果并推进状态。返回 (ok, 节点或 None)。

    答上了 → 直接「已掌握」；没答上 → 退一档（已掌握→学习中→待学习，待学习留在原地）。
    退档而不是清零：答错一次不该把「学习中」打回原点，但也不该留在原位骗自己。

    load → 改 → save 整段进 _KB_LOCK：与 absorb / set_state 同类，无锁并发会丢更新。
    """
    if result not in _RESULTS:
        return False, None
    nid = normalize_id(node_id)
    if not nid:
        return False, None
    with _KB_LOCK:
        data = ensure_kb(kb if kb is not None else store.load_knowledge())
        for n in data["nodes"]:
            if n.get("id") == nid:
                if result == RESULT_UP:
                    n["state"] = "已掌握"
                else:
                    rank = _STATE_RANK.get(str(n.get("state")), 0)
                    n["state"] = STATE_VALUES[max(0, rank - 1)]
                n["review_count"] = _as_int(n.get("review_count")) + 1
                n["last_reviewed_at"] = _today()
                n["last_result"] = result
                store.save_knowledge(data)
                log.log_event("kb.review_recorded", id=nid, result=result, state=n["state"])
                return True, n
    return False, None


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
    """两个身份键疑似同一概念的原因；不是则 None。

    先做类型防御：knowledge.json 是用户可手改的，`"id": 5` 这类非字符串键会让
    `5 in "abc"` 直接抛 TypeError —— 那会顺着 find_duplicates 冒到 /api/knowledge 变成 500，
    违反 ensure_kb 一族「绝不抛异常」的契约。
    """
    if not isinstance(a, str) or not isinstance(b, str):
        return None
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
    """启发式找出可能指同一概念的术语对。**只提示，绝不自动合并**（错并难发现）。

    只取字符串 id：知识库可被用户手改出 `"id": 5`，非字符串既无法比较大小写与包含关系，
    也会在 `5 in "abc"` 处抛 TypeError。
    """
    ids = [n.get("id") for n in ensure_kb(kb)["nodes"]
           if isinstance(n.get("id"), str) and n.get("id")]
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

    参数可以是**身份键**也可以是**原始术语**（内部先 normalize_id，对已归一化的 id 是 no-op）。

    学习进度**取并集**（见下方注释）：手动合并的是两张「用户卡」，两张卡的进度都是用户的真实
    作答结果，选错方向（把已掌握的并进待学习的）不该静默丢掉复习进度。这与 _merge_into 的
    「只保 keep 进度」并不矛盾 —— 那条铁律守的是「AI 新节点不许覆盖你的进度」，这里两边都是你。
    """
    keep_id, drop_id = normalize_id(keep_id), normalize_id(drop_id)
    if not keep_id or not drop_id:
        return False, "两个知识点都要选"
    if keep_id == drop_id:
        return False, "不能和自己合并"
    # load → 改两张卡 → save 整段串行：不加锁时并发的 absorb/set_state 会把这次合并整个覆盖掉
    with _KB_LOCK:
        return _merge_nodes_locked(keep_id, drop_id, kb)


def _merge_nodes_locked(keep_id, drop_id, kb):
    """merge_nodes 的主体，**调用方必须已持有 _KB_LOCK**（拆出来只是为了让锁的范围一眼可见）。

    入参的 keep_id / drop_id 已 normalize、且已排除「空 id」与「自己和自己合并」。
    """
    kb = ensure_kb(kb if kb is not None else store.load_knowledge())
    index = {n.get("id"): n for n in kb["nodes"] if n.get("id")}
    keep, drop = index.get(keep_id), index.get(drop_id)
    if keep is None or drop is None:
        return False, "找不到要合并的知识点"

    # 进度并集在 _merge_into **之后**取（计划原文顺序）：_merge_into 只写 keep、从不写 drop
    # （对 drop 只有 clean_*(node.get(...)) 这类读取），所以「先合并内容」与「先取进度快照」
    # 结果完全相同；放在后面只是让「合并内容」与「合并进度」两段连起来读。
    _merge_into(keep, drop)

    # 手动合并两张「用户卡」时进度要取并集：否则误选合并方向就会静默丢掉复习进度
    keep["asked_count"] = max(_as_int(keep.get("asked_count")), _as_int(drop.get("asked_count")))
    if _STATE_RANK.get(str(drop.get("state")), 0) > _STATE_RANK.get(str(keep.get("state")), 0):
        keep["state"] = drop["state"]
    # last_outcome 取值也要归一：knowledge.json 是用户可手改的，`"last_outcome": 5` 这类非法值
    # 只该回落默认（keep 已由 _merge_into 补齐），绝不能原样落到本该干净的 keep 卡上。
    # state 有 rank 守卫、asked_count 过了 _as_int，这里同样必须过一道类型关。
    outcome = drop.get("last_outcome")
    outcome = outcome.strip() if isinstance(outcome, str) else ""
    if outcome and outcome != DEFAULT_OUTCOME:
        keep["last_outcome"] = outcome
    fs = str(drop.get("first_seen") or "")
    if fs and (not keep.get("first_seen") or fs < str(keep.get("first_seen"))):
        keep["first_seen"] = fs

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


def stats(kb):
    """知识库统计：总数 / 有来源 / 各状态 / 被多岗位提到 / 来源总数。"""
    nodes = ensure_kb(kb)["nodes"]
    return {
        "total": len(nodes),
        "verified": sum(1 for n in nodes if _as_list(n.get("sources"))),
        "by_state": {s: sum(1 for n in nodes if n.get("state") == s) for s in STATE_VALUES},
        "multi_jd": sum(1 for n in nodes if len(_as_list(n.get("from_jds"))) > 1),
        "sources": sum(len(_as_list(n.get("sources"))) for n in nodes),
    }


def list_nodes(kb, q="", state="", category="", jd_id="", sort="mentions"):
    """查询知识库。默认按「被多少个岗位提到过」降序 —— 提到越多越是岗位刚需。

    q 先 str() 强制转换：查询串直接来自 URL 参数，`q=123` 这类输入不能把 AttributeError
    冒到 /api/knowledge 上。
    """
    nodes = list(ensure_kb(kb)["nodes"])
    q = str(q or "").strip().lower()
    if q:
        nodes = [n for n in nodes
                 if q in str(n.get("term") or "").lower()
                 or q in str(n.get("definition") or "").lower()
                 or q in str(n.get("plain_explanation") or "").lower()]
    if state:
        nodes = [n for n in nodes if n.get("state") == state]
    if category:
        nodes = [n for n in nodes if n.get("category") == category]
    if jd_id:
        nodes = [n for n in nodes if any(isinstance(x, dict) and x.get("id") == jd_id
                                        for x in _as_list(n.get("from_jds")))]
    if sort == "term":
        nodes.sort(key=lambda n: str(n.get("term") or ""))
    elif sort == "recent":
        nodes.sort(key=lambda n: str(n.get("last_seen") or ""), reverse=True)
    else:
        nodes.sort(key=lambda n: (-len(_as_list(n.get("from_jds"))),
                                 -len(_as_list(n.get("sources"))),
                                 str(n.get("term") or "")))
    return nodes


def all_jds(kb):
    """知识库里出现过的来源岗位（给筛选下拉用）。

    只索引**字符串** id：knowledge.json 是用户可手改的，`{"id": [1]}` 这类不可哈希的 id 会让
    `out[j["id"]]` 直接抛 `TypeError: unhashable type: 'list'` —— 顺着 /api/knowledge 冒出去，
    整个知识库 tab 就打不开了。非字符串 id 既选不了也没法当筛选条件，跳过它们。
    """
    out = {}
    for n in ensure_kb(kb)["nodes"]:
        for j in _as_list(n.get("from_jds")):
            if isinstance(j, dict) and isinstance(j.get("id"), str) and j["id"]:
                out[j["id"]] = j.get("job_title") or j["id"]
    return [{"id": k, "title": v} for k, v in sorted(out.items())]


def all_categories(kb):
    return sorted({str(n.get("category") or "").strip()
                   for n in ensure_kb(kb)["nodes"] if str(n.get("category") or "").strip()})


_REPORT_DATE = re.compile(r"^(\d{4})(\d{2})(\d{2})")


def read_report_data(path):
    """从报告 HTML 里取 REPORT_DATA。读不出（旧格式/被改过）返回 None。

    用 raw_decode 而不是正则：JSON 之后的 JS 代码里也有 `}`，正则容易截错。
    """
    text = store.read_text(path)
    i = text.find(REPORT_MARK)
    if i < 0:
        return None
    try:
        data, _ = json.JSONDecoder().raw_decode(text, i + len(REPORT_MARK))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _report_jd(data, filename):
    """从报告的 REPORT_DATA / 文件名推出「来源岗位」元信息"""
    rid = str(data.get("report_id") or os.path.splitext(filename)[0])
    m = _REPORT_DATE.match(os.path.basename(filename))
    date = "%s-%s-%s" % m.groups() if m else _today()
    return {"id": rid, "job_title": data.get("job_title") or "",
            "company": data.get("company") or "", "date": date}


def absorb_reports():
    """回填：把所有已有报告的知识吸进知识库。纯本地、零 API 额度、幂等。

    返回 (处理成功的报告数, 新增卡片数, 合并次数)。

    每份报告独立 try：一份写盘失败只跳过它，不能中断整轮回填（与 knowledge.py 的
    「每项独立 try」风格一致）。
    """
    folder = store.p_path("reports")
    files = sorted(glob.glob(os.path.join(folder, "*.html"))) if os.path.isdir(folder) else []
    done = added = merged = 0
    for path in files:
        name = os.path.basename(path)
        try:
            data = read_report_data(path)
            if not data:
                # 旧格式报告（早期版本没有 REPORT_DATA）是可预期情况，不是异常
                log.log_event("kb.import_skip", level="info", file=name, reason="读不出 REPORT_DATA")
                continue
            nodes = data.get("knowledge")
            if isinstance(nodes, dict):
                nodes = nodes.get("nodes")
            if not isinstance(nodes, list) or not nodes:
                log.log_event("kb.import_skip", level="info", file=name, reason="报告没有知识点")
                continue
            a, m = absorb(nodes, _report_jd(data, name))
            added += a
            merged += m
            # 只有真的吸收进去才算「这份报告处理成功」：absorb 在库文件损坏、且改名备份也失败时
            # 会早退 return (0, 0)（宁可这次不沉淀也绝不用空库顶掉坏文件），那不是成功、不能计数；
            # 知识点全是空术语的报告同样是 0/0，它确实什么都没沉淀。
            if a or m:
                done += 1
        except Exception as e:
            log.log_exc("kb.import_report_error", e, file=name)
            continue
    log.log_event("kb.import_done", files=done, added=added, merged=merged)
    return done, added, merged
