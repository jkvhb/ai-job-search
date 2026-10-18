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


def _quarantine_corrupt_kb(path=None):
    """损坏的 knowledge.json 先改名备份，绝不当空库静默覆盖。返回 True = 可以继续写盘。

    判据收归 `store.knowledge_safe_to_write()`（唯一实现）。**老判据为什么是错的**：
    它用 `store.read_text(path, "").strip()` 判断"有没有内容"，而非 UTF-8 文件
    **解码失败会被兜成空串** → "有内容但读不出来"（比如 GBK 写坏的库）被误判成"空文件"，
    于是不备份、直接写盘，用户的整个库就被内存里的空库顶掉了（实测复现过）。

    改名失败时返回 False（调用方 absorb 会放弃这次写盘）：宁可这次不沉淀，
    也绝不能让坏文件被内存里的空库顶掉。

    `path` 是给 `_with_kb_write(key=...)` 那条**带配置面**的路用的（默认 None = 当前档）。
    """
    path = path or store.knowledge_path()
    ok, reason = store.knowledge_safe_to_write(path)
    if ok:
        return True
    # 文件名带自增序号：同秒内两次隔离（或两个进程）绝不能互相覆盖备份
    seq = next(_QUARANTINE_SEQ)
    backup = "%s.corrupt-%s-%d-%04d" % (path, datetime.now().strftime("%Y%m%d%H%M%S"),
                                       os.getpid(), seq)
    try:
        os.replace(path, backup)
    except OSError as e:
        log.log_event("kb.corrupt_backup_failed", level="warn", error=str(e))
        return False
    log.log_event("kb.corrupt_backup", level="warn", backup=os.path.basename(backup),
                  reason=reason[:200])
    return True


_KB_WRITE_REFUSED = ("知识库读不出来，本次没有写入（文件原样未动、本次也没有留档；"
                     "先修好库再操作）")

#: **留档自己失败**（改名失败：磁盘满 / 文件被占用）时用这句。
#: 不能与上面那句混用：那句说的是"我们**决定**不碰你的文件、所以没有 `.corrupt-*`"，
#: 这句说的是"我们**试着**留档但没成功" —— 两件事、用户要做的动作也不同（一个去修库，
#: 一个还要腾磁盘）。混用会让用户照着"已按 .corrupt-* 留档"去找一份并不存在的备份。
_KB_BACKUP_FAILED = ("知识库读不出来，坏文件留档也失败，本次没有写入（文件原样未动；"
                     "先修好库、腾出磁盘再操作）")


def _with_kb_write(fn, refuse_when=None, write_when_unreadable=True, key=None):
    """knowledge.json 唯一写入口：持 `_KB_LOCK` → load → 改 → save，写盘前先隔离读不出来的旧文件。

    `fn(data)` 返回调用方要的返回值。
    `refuse_when(data) -> str | None` 是可选的**入参守卫**，返回非空字符串 = "这次没什么可写的"
    （如"找不到该知识点"）。`write_when_unreadable=False` 表示这个入口在**库读不出来时一律不写**
    （`set_state` / `record_review` / `merge_nodes`：它们写的**就是库里那张已有的卡**；
    `record_interview` 同理 —— 它虽然无条件 save，但坏库下 save 下去的只是一份空库，
    见该函数 docstring）。
    `key` 是**配置面**（`store.knowledge_path(key)` 的那一个）：三个跨模块窄接口
    （`purge_jd_refs` / `repoint_jd_refs` / `merge_jd_refs`）要把它透传进来 ——
    否则一次带 key 的调用会**静默写到另一个档**去。其余入口都不带 key（默认 None = 当前档）。

    返回 `(是否真的调用过 fn, fn 的返回值或守卫给的原因)`：

      * `(False, 守卫给的原因 / _KB_WRITE_REFUSED / _KB_BACKUP_FAILED)` —— 没写。
      * `(True, result)` —— fn 跑过了；库读不出来时坏文件已先被改名留档（这次写入不丢用户的卡）。

    **为什么必须抽出来（P0-1）**：`absorb` 早就接了哨兵，而另外五个写入口各写各的样板。
    `record_interview` 那条漏了哨兵 —— 实测把 GBK 写坏的库（300 字节、用户 145 张卡）换成
    36 字节空库、`.corrupt-*` 一个都没有。其余四条当时"数据不丢"只是**副作用**：读到空库 →
    找不到目标卡 → 恰好没走到 save。那不是契约，任何一次"顺手把 save 挪到前面 / 加一句无条件
    save"都会立刻复现同一个事故。把哨兵放在**唯一入口**上，就不可能再有"哪条路忘了装闸门"。

    **库读不出来时写不写，按入口的语义分两类**（`load_knowledge()` 给的是空库，
    "空库"与"库真的空了"分不开 —— 所以"库里有没有这张卡"这个判据在此时整个失效）：

      * 写的**就是库里那张已有的卡**（`write_when_unreadable=False`）：目标无从判断，就不写。
        一次点空了的调用不该让用户的文件消失（哪怕留下备份）。如实报 `_KB_WRITE_REFUSED`，
        调用方/界面就能说"库读不出来，先去修库" —— 比"静默干了半件事"有用得多。
        `record_interview` 也归这一类：它无条件 save，但坏库下**一张卡都命中不到**、
        save 下去的只是一份空库 ⇒ 这次反哺一个字没记下、用户的卡还从活库消失了
        （2026-10-15 的修正，见该函数 docstring）。
      * 写的是**用户这次的操作**（`absorb` 的沉淀、`upsert_check` 的勾）：坏文件先逐字节留档，
        然后照常写新库。放弃写入等于让用户刚做完的操作静默丢失，而留档之后"用户的卡会被空库
        顶掉"这件事已经不成立了（P0-1 的修法正是这个）。
        （这两个调用方目前都把留档失败的那句话**吞掉**，各报自己的外形：`absorb` → `(0, 0)`、
        `upsert_check` → `(False, False, None)`；`_KB_BACKUP_FAILED` 因此只出现在日志里。）

    **守卫的位置**：
      * 库**读得出来** → 先问守卫（这是 `absorb` 那条"零可吸收节点先返回"的一般化）：
        一次"点空了"的调用本该什么都不做，那就**一个字节都别动**（连隔离都不做）。顺序反过来时，
        这种调用会把好库隔离走再写回一份空库 —— 用户看到"点一下，知识库没了（多了个 .corrupt-*）"。
      * 库**读不出来** → 守卫拿到的 `data` 是空库、判据失效，**所以不拿它做决定**（隔离之后
        读回来的也还是空文件，同样不可用）。要不要写只由 `write_when_unreadable` 决定。

    **为什么新库必须重新 `load()`**（不接收调用方传进来的库对象）：隔离会把坏文件**改名走**，
    此刻盘上已经没有 knowledge.json 了；拿一份外部传进来的内存副本当底本，等于把坏文件被改名
    之前的旧内容又写回去 —— 那正好是 P0-1 要修的那件事。代价是每次写多一次读盘（几毫秒）。
    """
    with _KB_LOCK:
        kb_path = store.knowledge_path(key)
        read_ok = store.knowledge_safe_to_write(kb_path)[0]
        if read_ok and refuse_when is not None:
            because = refuse_when(ensure_kb(store.load_knowledge(key)))
            if because:
                return False, because              # 没什么可写的：连隔离都不做，文件原样不动
        if not read_ok and not write_when_unreadable:
            log.log_event("kb.write_refused", level="warn", reason="corrupt_kb_unreadable")
            return False, _KB_WRITE_REFUSED
        if not _quarantine_corrupt_kb(kb_path):
            # 坏文件留档失败：宁可这次不沉淀，也绝不能让用户的卡被内存里的空库顶掉。
            # 用 `_KB_BACKUP_FAILED`（"试着留档但没成功"）而不是 `_KB_WRITE_REFUSED`
            # （"决定不碰你的文件、所以没有 .corrupt-*"）—— 两句说的事不一样。
            # 注意：`write_when_unreadable=False` 的四个入口走不到这里（库读不出来时上面
            # 那行已经返回了；库读得出来时 `_quarantine_corrupt_kb()` 只会返回 True），
            # 所以这句话目前只出现在日志里（两个 (b) 调用方各自吞掉它、报自己的外形）。
            log.log_event("kb.write_refused", level="warn", reason="corrupt_backup_failed")
            return False, _KB_BACKUP_FAILED
        return True, fn(ensure_kb(store.load_knowledge()))


def absorb(nodes, jd=None, kb=None):
    """把一次分析（或一份报告）的知识点并入知识库。返回 (added, merged)。

    幂等：同一个 jd['id'] 重复吸收不会重复计数 from_jds，来源也不会翻倍。

    写盘之前先保住「读不出来」的旧文件（见 _quarantine_corrupt_kb）—— 本函数末尾一定会
    save_knowledge，不先备份的话坏文件会被内存里的空库永久顶掉。备份都失败时直接放弃本次吸收。

    **零可吸收节点时在隔离之前就返回 (0, 0)**：一个节点都没有的调用（报告知识点全是空术语/
    纯标点）本来就没有任何东西可沉淀，却会走完隔离 + save_knowledge —— 于是「库文件坏 + 本次
    零知识点」的组合会先把用户的库改名走，再写回一份空库，等于「吸收了个寂寞还把库清空」。
    判断必须在 _quarantine_corrupt_kb() **之前**做，否则坏文件已经被改名走了。

    哨兵与 load→save 这段整体由 `_with_kb_write` 承担（P0-1：五个写入口共用同一条闸门）。

    `kb` 参数**保留但已不再使用**（P0-1 的后续）：P0-1 要的就是"写盘用的底本必须是**当场从盘上读
    回来的**"，而不是调用方手里那份可能已经过期的内存副本 —— 拿旧副本当底本正是"坏文件被改名走
    之后又把旧内容写回去"的来源。签名不改是为了不惊动既有调用方（本函数是模块里被外部用得最多
    的一个）。其余四个写入口的 `kb=None` 参数一并删掉了（仓库里 0 个调用方在传）。
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

    def _absorb_into(kb):
        # P0-7：索引必须**同时**收 id 与归一化 term。kbreuse 是按 normalize_id(term) 找卡的，
        # 只按 card["id"] 建索引的话，一张 id 与 term 不一致的卡（手改 / 别的写入者造成，
        # 实测形态 `{"id": "yuyinshibie", "term": "ASR"}`）在这里会"找不到"
        # → 明明复用了旧卡却又新建一张同概念卡（added=1，卡片数 1 → 2）。
        # 先 id 后 term、用 setdefault：**id 优先**，与 `kbreuse.Index` 的
        # `by_name[normalize_id(term)]` + 既有 id 索引合起来是同一把尺子。
        index = _card_index(kb)
        added = merged = 0
        for node in absorbable:
            nid = normalize_id(node["term"].strip())   # 上面已保证 term 是字符串且非空
            card = index.get(nid)
            if card is None:
                card = _new_card(node, jd)
                kb["nodes"].append(card)
                index[nid] = card
                index.setdefault(card.get("id"), card)
                added += 1
            else:
                _merge_into(card, node)
                _add_jd(card, jd)
                _recompute_confidence(card)
                merged += 1
        kb["version"] = KB_VERSION
        store.save_knowledge(kb)
        return added, merged

    invoked, result = _with_kb_write(_absorb_into)
    if not invoked:
        return 0, 0                       # 坏文件留档失败：照旧"什么都没吸收"
    return result


def _card_index(data):
    """知识库的「身份键 → 卡」索引：**同时**收 `id` 与 `normalize_id(term)`，`id` 优先。

    P0-7：`kbreuse.Index` 是按 `normalize_id(term)` 找卡的，`kb.absorb` 却只按 `card["id"]`
    建索引 —— 一张 id 与 term 不一致的卡（手改 / 别的写入者造成，实测
    `{"id": "yuyinshibie", "term": "ASR"}`）在 absorb 这里会"找不到"，于是明明复用了旧卡
    又新建一张同概念卡。两把尺子必须合成一把：**两张键都收，先 id 后 term（`setdefault`）**。

    `id` 优先的理由：id 是这张卡的**真身份**（报告里的关联概念图、`from_jds` 都按它连线），
    term 只是显示名。两者冲突时以 id 为准，与 `kbreuse.Index` 两层键里"精确名永远赢"同源。
    """
    index = {}
    for n in data["nodes"]:
        nid = n.get("id")
        if nid:
            index.setdefault(nid, n)
        term_key = normalize_id(n.get("term"))
        if term_key:
            index.setdefault(term_key, n)
    return index


# ---------- 跨模块的窄接口：清引用 / 重指引用（P3-3）----------
#
# 为什么要有这三个：`jddelete`（删岗位）、`jdid`（改 ID）、`jobdup`（合并）原先**各自**
# 在 kb 之外做「load_knowledge → 改 from_jds → save_knowledge」，并各自解析 `kb._KB_LOCK`
# —— 三份重复的 `_repoint_kb`（体检 §7.2）。读-改-写收进这里之后：
#   * 保护它的锁与它同处一个模块（外面不必、也不该再碰 `kb._KB_LOCK`）；
#   * 坏库哨兵、归一化、留档只有 `_with_kb_write` 那一份实现（P0-1）；
#   * 三处调用方的差异（计数口径 / 判据是否去空白）在这里**参数化**，不再留第二份实现。
#
# 锁序铁律（见文件头）：这三个接口**只**取内层锁 `_KB_LOCK`，绝不回头去取
# `_JD_LOCK` / `_MERGE_LOCK` —— 那些是调用方已经持着的**外层**锁，回头去要就是死锁形状。

def ref_id(x, strip):
    """一条 `from_jds` 项指向的岗位 id（非 dict / 缺 id / id 不是字符串 → 空串）。

    `strip` 不是洁癖而是**口径**，而且它是**参数**（不是"这里忘了统一"）：三处调用方历史上
    用的判据不同 —— `jddelete` / `jdrefs` 去首尾空白（它们必须先列清单、再按同一把尺子改，
    否则手改出的 `" jd1 "` 会"预演列出、真改不动"），`jdid` / `jobdup` 的窄接口**逐字比**
    （既有用例 `tests/test_kb.py:2996` 钉着这一点）。收口只搬家、不顺手把两种判据统一成
    一种 —— 那会改变用户手改出来的 `" jd1 "` 算不算同一条引用。

    本函数是**唯一一份**条目 id 取值实现（批 7.1：`jdrefs` 原来也有一份一模一样的副本）。
    """
    if not isinstance(x, dict):
        return ""
    v = x.get("id")
    if not isinstance(v, str):
        return ""
    return v.strip() if strip else v


def _purge_refs_in(data, jd_id):
    """原地删掉每张卡 `from_jds` 里指向 jd_id 的条目。返回被改动的**卡片数**（不落盘）。

    判据与 `jddelete._strip_references` 原来那一份逐字同一把尺子（去首尾空白）；
    `from_jds` 不是 list、条目不是 dict 一律跳过；**卡片本身留着**（"只清引用"是规格硬要求）。
    """
    touched = 0
    for node in _as_list(data.get("nodes")):
        if not isinstance(node, dict):
            continue
        frm = node.get("from_jds")
        if not isinstance(frm, list):
            continue
        kept = [x for x in frm if ref_id(x, True) != jd_id]
        if len(kept) != len(frm):
            node["from_jds"] = kept
            touched += 1
    return touched


def repoint_refs_in(data, old_id, new_id, strip=False):
    """原地把每张卡 `from_jds` 里指向 old 的条目重指到 new。返回
    `(被改动的卡片数, 原位重指的条目数, 去掉的重复条目数)`（不落盘）。

    语义与 `jdid._repoint_kb` / `jobdup._repoint_kb` **逐字同一套**：卡上本来就有 new 项时
    保留它、去掉 old 那一条（否则同一个 JD 会被数两次）；否则把 old 项**原位改 id**
    （保留它自己的 job_title / company / date）。

    ## ⚠️ 这是**唯一一份**"重指"变换的实现（批 7.1）

    `jdrefs._repoint_kb_in` 原来是与本函数**逐行相同**的第二份副本（体检 §8.1 点名的
    "同一套变换两份实现"）。现在它被删掉，`jdrefs` 直接调本函数 —— 变换只可能在这里漂移一次。

    ## 判据是**参数**（`strip`），不是"这里统一成一种"

    两边历史上的尺子不同，而且**各自都被既有用例钉着**（对向钉死，谁赢都会红一条）：
      * `strip=True` —— **去首尾空白比**。`jdrefs`（预演 + 改写）与 `jddelete` 用这把尺子：
        手改出来的 `" jd1 "` 也算同一条引用。`jdrefs` **必须**用它：它的 detect 与 rewrite
        是同一把尺子，否则会"预演列出 3 条、真改 2 条"，而改名/合并之后那个
        `" 旧 id "` 就成了一条永远指不到的悬空引用（`tests/test_jdrefs.py:400` 钉着）。
      * `strip=False`（默认）—— **逐字比**。`repoint_jd_refs` / `merge_jd_refs` 用这把尺子，
        与它们收口之前的 `jdid._repoint_kb` / `jobdup._repoint_kb` 逐字一致
        （`tests/test_kb.py:2996` 钉着）。
    换句话说：**实现统一了，口径按调用方传** —— 这是刻意的，别把它"顺手统一"掉：
    改任何一边都会改变"用户手改出的 `" jd1 "` 算不算同一条引用"这个对外行为。

    旧实现里 `jobdup` 对每个有 `from_jds` 列表的卡都无条件回写 `node["from_jds"] = out`，
    `jdid` 只在真的改了才回写 —— 落盘内容完全相同（没命中的卡 `out` 与旧列表逐项一致），
    这里取 `jdid` 那种（不白改字典、也不白造一份列表）。
    """
    touched = repointed = deduped = 0
    for node in _as_list(data.get("nodes")):
        if not isinstance(node, dict):
            continue
        frm = node.get("from_jds")
        if not isinstance(frm, list):
            continue
        has_new = any(ref_id(x, strip) == new_id for x in frm)
        out, changed = [], False
        for x in frm:
            if ref_id(x, strip) == old_id:
                if has_new:
                    changed = True          # 已经有 new 项 → 去掉这个重复项
                    deduped += 1
                    continue
                x = dict(x)               # 只改一份副本，不动传进来的那些 dict
                x["id"] = new_id
                has_new = True
                changed = True
                repointed += 1
            out.append(x)
        if changed:
            node["from_jds"] = out
            touched += 1
    return touched, repointed, deduped


def purge_jd_refs(jd_id, key=None):
    """删岗位时清掉知识库里指向它的引用。返回 `(被改动的卡片数, err)`。

    `jddelete.purge_jd` 的唯一知识库写路径（原来那份 `_strip_references` 已删）。
    **只清引用、卡片留着**；没有卡片提到它就**一个字节都不写**（别白重写整份库）。

    库读不出来时`(0, _KB_WRITE_REFUSED)` —— **拒绝写入、连留档都不做**，与
    `jddelete` 本来的行为一致（"坏库不会被覆盖、坏着也不丢数据"）。
    """
    want = str(jd_id or "").strip()
    if not want:
        # 空 id 不做任何事（旧路径也到不了这里：`purge_jd` 在空 id 上就报 not_found 了）。
        # 而且"去空白后为空"的判据会把 `{"id": "   "}` 这类条目也算成同一条 —— 不猜。
        return 0, ""

    def _fn(data):
        touched = _purge_refs_in(data, want)
        if touched:
            store.save_knowledge(data, key)
        return touched

    invoked, result = _with_kb_write(_fn, write_when_unreadable=False, key=key)
    if not invoked:
        return 0, result                   # 库读不出来：一个字都没写，原因如实带回
    return result, ""


def repoint_jd_refs(old_id, new_id, key=None):
    """改 ID（`jdid.rename_jd`）时把知识库里 old 的引用重指到 new。返回 `(被改动的卡片数, err)`。

    计数口径是**卡片数**（`jdid` 记的 `kb_nodes`），与它原来的 `_repoint_kb` 一致。
    没有卡片提到 old 就一个字节都不写；库读不出来时 `(0, _KB_WRITE_REFUSED)`。

    ⚠️ `old` / `new` 先 `strip()`（调用方 `jdid` / `jobdup` 本来就这么规整），但**条目一侧
    是逐字比**（见 `_ref_id`）。
    """
    old = str(old_id or "").strip()
    new = str(new_id or "").strip()
    if not old or not new or old == new:
        # 无事可做。`rename_jd` 在 old == new 时自己就提前返回了；这里再兜一层：
        # 把"没给 id"当成"没有引用命中"是唯一说得通的口径，绝不拿空串去匹配一堆空 id。
        return 0, ""

    def _fn(data):
        touched, _repointed, _deduped = repoint_refs_in(data, old, new)
        if touched:
            store.save_knowledge(data, key)
        return touched

    invoked, result = _with_kb_write(_fn, write_when_unreadable=False, key=key)
    if not invoked:
        return 0, result
    return result, ""


def merge_jd_refs(drop_id, keep_id, key=None):
    """合并（`jobdup.merge_jobs`）时把被并掉的 drop 的引用重指到 keep。
    返回 `(原位重指的条目数, 去掉的重复条目数, err)`。

    变换与 `repoint_jd_refs` **同一份核心遍历**（只留一份实现），差别只在**计数口径**：
    `jobdup` 的对外字段是 `kb_repointed` / `kb_deduplicated`，数的是 `from_jds`
    **条目**而不是卡片 —— 那是 `merge_jobs` 的返回值与 `jobdup.merge_done` 日志的形状，
    收口时不许改。两个计数都是 0 时一个字节都不写；库读不出来时 `(0, 0, _KB_WRITE_REFUSED)`。
    """
    drop = str(drop_id or "").strip()
    keep = str(keep_id or "").strip()
    if not drop or not keep or drop == keep:
        return 0, 0, ""                    # 同上：无事可做

    def _fn(data):
        _touched, repointed, deduped = repoint_refs_in(data, drop, keep)
        if repointed or deduped:
            store.save_knowledge(data, key)
        return repointed, deduped

    invoked, result = _with_kb_write(_fn, write_when_unreadable=False, key=key)
    if not invoked:
        return 0, 0, result
    return result[0], result[1], ""


def set_state(node_id, state):
    """只改学习状态，不碰任何其它字段。返回 (ok, msg)。

    参数可以是**身份键**也可以是**原始术语**（内部先 normalize_id，对已归一化的 id 是 no-op）。
    **找卡与守卫是同一把尺子**（`_card_index`：id + 归一化 term 双键、id 优先，P0-8）——
    一张 id ≠ 归一化 term 的卡（手改 / 别的写入者造成，如实测的
    `{"id": "yuyinshibie", "term": "ASR"}`）两把钥匙都开得开。

    **不建卡**：找不到就返回 (False, "找不到该知识点")。库读不出来时同样返回 False，
    但理由不同（见 `_with_kb_write` 的 `_KB_WRITE_REFUSED`）—— 调用方据 msg 就能如实回报。
    这两句话分别对应"卡**真的**不在"与"库读不出来"，不许混用（假话 + 静默吞掉用户的掌握度）。

    哨兵与 load→save 这段整体由 `_with_kb_write` 承担（P0-1）。**守卫要在哨兵之前**：
    "状态不合法"是纯粹的入参问题，不该因为一次失败调用就把好库隔离走。
    """
    if state not in STATE_VALUES:
        return False, "状态不合法：%s" % state
    node_id = normalize_id(node_id)
    # load → 改 → save 整段串行：不加锁会被并发的 absorb 用「它读到的旧库」覆盖掉这次掌握度
    invoked, result = _with_kb_write(
        lambda kb: _set_state_in(kb, node_id, state),
        refuse_when=lambda kb: None if _card_index(kb).get(node_id) else "找不到该知识点",
        write_when_unreadable=False)
    if not invoked:
        return False, result or _KB_WRITE_REFUSED
    return result


def _set_state_in(kb, node_id, state):
    """`set_state` 的主体（调用方已持锁、已过入参守卫、已隔离坏库）。

    P0-8：找卡必须与守卫（`refuse_when`）**同一把尺子** —— `_card_index`（id + 归一化 term
    双键、id 优先）。此前这里只比 `n.get("id")`，于是库里 `{"id": "yuyinshibie", "term": "ASR"}`
    这种卡（手改 / 别的写入者造成 id ≠ 归一化 term）上：守卫说"卡在"、主体说"卡不在" ⇒
    `set_state("ASR", ...)` 回一句**假话**（"找不到该知识点"）外加**静默吞掉**用户的掌握度。
    命中之后改的仍必须是**库里那张原卡**：本入口不建卡（卡片数是守卫与主体共同守住的）。
    """
    card = _card_index(kb).get(node_id)
    if card is None:
        return False, "找不到该知识点"
    card["state"] = state
    store.save_knowledge(kb)
    # 日志记卡片的**真 id**（命中可能是走归一化 term 那把钥匙，此时 node_id 只是术语）
    log.log_event("kb.state_set", id=card.get("id") or node_id, state=state)
    return True, ""


_REVIEW_WEIGHT = {"待学习": 3, "学习中": 2, "已掌握": 1}
_REVIEW_AGE_CAP = 30        # 久未复习的加成上限（天），防止"从没看过"无限压过一切


def _valid_jds(card):
    """只算「有非空字符串 id」的来源岗位 —— 与 all_jds / stats 同一把尺子。"""
    return [j for j in _as_list(card.get("from_jds"))
            if isinstance(j, dict) and isinstance(j.get("id"), str) and j["id"]]


def _days_since(date_str):
    """距上次复习多少天，封顶 `_REVIEW_AGE_CAP` —— **只给评分用，不许拿来说话**。

    它把「从没复习过」「日期格式坏了」「日期在未来」「真的过了很久」四种情况兜成同一个数
    （30）。对**评分**这是对的（久未复习加成要封顶，`_review_score` 的旧行为不变），
    但拿它去写文案就必然说假话 —— 体检 **P1-3** 的「复习过 45 天却说从没复习过」就是这么来的。
    文案走 `_parse_review_date()` + `_review_age_text()`（见 `_review_reason` 的映射表）。
    """
    try:
        d = datetime.strptime(str(date_str), "%Y-%m-%d").date()
    except Exception:
        return _REVIEW_AGE_CAP              # 从没复习过（或格式坏了）→ 按最久算
    return max(0, (datetime.now().date() - d).days)


def _parse_review_date(date_str):
    """`last_reviewed_at` → `date`；解析不出来返回 **None**（不兜底、不猜）。

    判据必须与 `_days_since` 分开：那个函数把「没写过 / 格式坏了 / 超过 30 天」抹平成同一个
    数（30，评分用的封顶），说话时就必须能分辨它们，否则又回到 P1-3 那句假话。
    """
    try:
        return datetime.strptime(str(date_str), "%Y-%m-%d").date()
    except Exception:
        return None


def _review_score(card):
    """优先级 = 状态权重 × (1 + 被多少岗位提到) × (1 + 久未复习加成) × (1 + 被面试问过几次)。

    全部是乘性：任何一个维度为 0 都不会把分数压成 0（未掌握但只被 1 个岗位提到，仍应排在
    已掌握且刚复习过的前面）。

    为什么把 asked_count 也算进来：这份库是**为真实面试准备**的，被面试官真问过的概念，
    复习价值远高于只在 JD 里出现过的（ask=0 时因子为 1，与旧行为完全一致，老数据不会被搅动）。
    """
    state = str(card.get("state") or DEFAULT_STATE)
    weight = _REVIEW_WEIGHT.get(state, 1)
    jd_n = len(_valid_jds(card))
    age = min(_days_since(card.get("last_reviewed_at")), _REVIEW_AGE_CAP)
    asked = _as_int(card.get("asked_count"))
    # 被真实面试问过 → 优先复习（asked=0 时因子为 1，与旧行为一致）
    return weight * (1 + jd_n) * (1 + age / 7.0) * (1 + asked)


def _review_age_text(raw, count):
    """「多久没复习」那一句 —— 每一支都必须与事实对得上（见 `_review_reason` 的映射表）。

    `raw` 是**已经过 `_normalise_review_fields` 归一**的 `last_reviewed_at`
    （非字符串一律成了空串；非空字符串原样保留，可能是"昨天"、"2026-13-45" 这类）。
    """
    if not raw:
        if count > 0:
            # 有自测次数、没日期：两个字段打架（手改坏 / 旧版本数据），只报事实、不选边 ——
            # 老实现这里一律说「还没复习过」，而卡上明明写着 review_count=3。
            # **报得出的事实就报**（次数），报不出的（哪天看的）如实说没记，不给「不确定」那种
            # 什么都不说的空话 —— 用户至少知道这张卡被自测过几次。
            return "复习过 %d 次（没记日期）" % count
        return "还没复习过"
    day = _parse_review_date(raw)
    if day is None:
        return "上次复习日期读不出来（%s）" % raw[:20]   # 信息不足 → 如实说，别猜
    days = (datetime.now().date() - day).days
    if days < 0:
        # 日期在未来（手改 / 改过系统时钟）：算不出"多久没复习"，但绝不说成「今天复习过」
        return "上次复习日期在未来（%s）" % raw[:20]
    if days == 0:
        return "今天复习过"
    # **不封顶**：`_REVIEW_AGE_CAP` 是评分的加成上限，不是说话的上限 —— 45 天就说 45 天
    return "%d 天没复习" % days


def _review_reason(card):
    """给人看的排序理由 —— 黑箱排序会让人不信任它。

    **每一句都必须与卡上的事实对得上**（体检 **P1-3**：老实现把「复习过 30 天以上」说成
    「还没复习过」）。判据与文案的完整映射表：

    | 卡上的事实 | 文案 |
    |---|---|
    | `state` 在 `STATE_VALUES` 里 | 原样印状态 |
    | `state` 为空 / 缺失 | 「待学习」—— 与 `_review_score` 的 `or DEFAULT_STATE` **同一把尺子**（权重 3） |
    | `state` 非空但不在枚举里（如「精通」/5） | 「状态未知（X）」—— 评分对它按 `_REVIEW_WEIGHT.get(state, 1)` ＝ 1 算，不许冒充已知状态 |
    | `_valid_jds` 数 > 1 | 「被 N 个岗位提到」（≤1 时省略；与 `all_jds` 同一把尺子） |
    | `asked_count` > 0 | 「面试被问过 N 次」（≤0 / 坏值一律不提，免得写出"被问过 -3 次"） |
    | 没日期 + `review_count` == 0 | 「还没复习过」 |
    | 没日期 + `review_count` > 0 | 「复习过 N 次（没记日期）」—— 两个字段打架，只报事实、不选边 |
    | 日期串读不出来 | 「上次复习日期读不出来（X）」—— 信息不足，如实说 |
    | 日期在未来 | 「上次复习日期在未来（X）」—— 算不出来，绝不说「今天复习过」 |
    | 日期是今天 | 「今天复习过」 |
    | 日期在 1 天前或更早 | 「N 天没复习」—— **不封顶**，45 天就说 45 天 |

    为什么不复用 `_days_since()`：见它的 docstring —— 它把三种不同的情况兜成同一个封顶值，
    适合评分、不适合说话。评分继续用它（旧行为不变），文案走 `_parse_review_date()`。

    `last_outcome` / `last_result` 不参与本文案：它们分别是「面试结果」与「上次自测结果」，
    与"多久没复习"无关，写进来只会多一句与排序无关的话（也就多一个说假话的机会）。
    映射表钉在 `tests/test_kb.py::ReviewReasonTest`，每一支都有具体文案断言。
    """
    state = str(card.get("state") or DEFAULT_STATE)
    # 合法状态原样印；非法状态评的是 weight=1（最低），所以不许印成「待学习」那种满权重的说法
    parts = [state if state in _STATE_RANK else "状态未知（%s）" % state[:20]]
    jd_n = len(_valid_jds(card))
    if jd_n > 1:
        parts.append("被 %d 个岗位提到" % jd_n)
    asked = _as_int(card.get("asked_count"))
    if asked > 0:
        parts.append("面试被问过 %d 次" % asked)
    raw = card.get("last_reviewed_at")
    raw = raw.strip() if isinstance(raw, str) else ""
    parts.append(_review_age_text(raw, _as_int(card.get("review_count"))))
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


def record_review(node_id, result):
    """记录一次自测结果并推进状态。返回 (ok, 节点或 None)。

    答上了 → 直接「已掌握」；没答上 → 退一档（已掌握→学习中→待学习，待学习留在原地）。
    退档而不是清零：答错一次不该把「学习中」打回原点，但也不该留在原位骗自己。

    参数可以是身份键也可以是原始术语（内部先 normalize_id）；**找卡与守卫是同一把尺子**
    （`_card_index`：id + 归一化 term 双键、id 优先，P0-8），免得守卫放行了主体却说找不到、
    把用户这次自测静默吞掉。

    **不建卡**：找不到就返回 (False, None)。库读不出来时同样返回 (False, None) ——
    盘上的坏文件一个字节都不动（调用方要更细的原因，看 `kb._KB_WRITE_REFUSED`）。

    load → 改 → save 整段进 _with_kb_write：与 absorb / set_state 同类，无锁并发会丢更新。
    """
    if result not in _RESULTS:
        return False, None
    nid = normalize_id(node_id)
    if not nid:
        return False, None
    invoked, outcome = _with_kb_write(
        lambda data: _record_review_in(data, nid, result),
        refuse_when=lambda data: None if _card_index(data).get(nid) else "找不到该知识点",
        write_when_unreadable=False)
    if not invoked:
        return False, None
    return outcome


def _record_review_in(data, nid, result):
    """`record_review` 的主体（调用方已持锁、已过入参守卫、已隔离坏库）。

    P0-8：找卡与守卫共用 `_card_index`（id + 归一化 term 双键、id 优先）—— 理由同
    `_set_state_in`：两把尺子会让守卫放行、主体却"找不到"，用户的这次自测被静默吞掉。
    命中后回传的就是**库里那张卡**本身（调用方拿它更新界面），卡片数不变（本入口不建卡）。
    """
    card = _card_index(data).get(nid)
    if card is None:
        return False, None
    if result == RESULT_UP:
        card["state"] = "已掌握"
    else:
        rank = _STATE_RANK.get(str(card.get("state")), 0)
        card["state"] = STATE_VALUES[max(0, rank - 1)]
    card["review_count"] = _as_int(card.get("review_count")) + 1
    card["last_reviewed_at"] = _today()
    card["last_result"] = result
    store.save_knowledge(data)
    log.log_event("kb.review_recorded", id=card.get("id") or nid, result=result, state=card["state"])
    return True, card


# ---------- 面试（阶段 3.4）：确定性命中 + 结果回写 ----------
HIT_MIN_LEN = 3          # 术语 ≥3 字：出现即命中
HIT_REPEAT_2CHAR = 3     # 2 字词太宽泛：要出现 ≥3 次才算被问到


def interview_hits(text, kb=None):
    """确定性命中：知识库里哪些概念的术语出现在这段转写里。

    不靠模型判 —— 确定性、可测、零额度，也不会因为模型漏读而漏记。
    2 字词（「模型」「标注」）在口语里遍地都是，单次出现不算数。
    """
    blob = str(text or "").lower()
    if not blob:
        return []
    out = []
    for n in ensure_kb(kb if kb is not None else store.load_knowledge())["nodes"]:
        term = str(n.get("term") or "").strip()
        if not term:
            continue
        t = term.lower()
        if len(t) >= HIT_MIN_LEN:
            if t in blob:
                out.append(n)
        elif blob.count(t) >= HIT_REPEAT_2CHAR:
            out.append(n)
    return out


def record_interview(jd_id, asked=None, weak=None, good=None):
    """把一次面试的结果写回知识库。返回 (hits, weak, downgraded) 计数。

    **顺序**：先 +1（asked_count），再写 last_outcome，最后做「已掌握 → 学习中」的降级
    —— 保证降级不会被后续写回覆盖掉。
    **找不到的术语直接忽略，绝不自动建卡**（新词由用户在复盘报告里手动加入）。

    ⚠️ **库读不出来时拒绝写入、连留档都不做**（`write_when_unreadable=False`）：
    盘上那个坏文件**逐字节不动**，本函数返回 `None`。

    **为什么是 (a) 拒绝，而不是像 `absorb` / `upsert_check` 那样 (b) 留档后写**：本函数写的
    **就是库里那些已有的卡**（asked_count / last_outcome / 降级），而库读不出来时"哪张卡被问过"
    整个无从判断 —— `load_knowledge()` 给的是空库，`_record_interview_in` 于是一张卡都命中不到，
    却照样走到 `store.save_knowledge(data)`（本函数无条件 save）：**这次反哺一个字都没记下**，
    而用户的 145 张卡从**活库**消失（只在 `.corrupt-*` 里）。留档没丢字节，但用户打开界面看到的是
    "知识库 0 张卡"，还得自己去找那份备份文件 —— 纯亏。拒绝则让用户修好库之后**重试这次反哺**。

    （2026-10-15 修正：本函数原先是 (b)，`_with_kb_write` 的 docstring 里那套"写的是用户这次
    的操作 ⇒ 留档后写"的分类对它并不成立 —— 它写的是库里已有的卡，只是恰好无条件 save。）

    **库读不出来时返回 `None`**（不是 `(0, 0, 0)`）：`(0, 0, 0)` 是**合法结果**
    （这次面试一个知识点都没提到），调用方没法拿它区分"写成了"与"压根没写"。
    `interview._apply_feedback` 依赖这个区分：它只在**真写进库之后**才落 feedback 标记，
    否则那次反哺永远补不回来（标记已落盘、重试会被幂等挡掉）。

    哨兵与 load→save 这段整体由 `_with_kb_write` 承担（P0-1：五个写入口共用同一条闸门）。
    """
    invoked, result = _with_kb_write(
        lambda data: _record_interview_in(data, jd_id, asked, weak, good),
        write_when_unreadable=False)
    if not invoked:
        return None                      # 没写：调用方必须能分辨，绝不能报一个假 (0, 0, 0)
    return result


def _record_interview_in(data, jd_id, asked, weak, good):
    """`record_interview` 的主体（调用方已持锁、已隔离坏库）。"""
    index = {}
    for n in data["nodes"]:
        nid = n.get("id")
        if nid:
            index.setdefault(nid, n)
        term_key = normalize_id(n.get("term"))
        if term_key:
            index.setdefault(term_key, n)          # 与 absorb 同一把尺子（P0-7）
    hits = weak_n = downgraded = 0
    for term in _as_list(asked):
        card = index.get(normalize_id(term))
        if card is None:
            continue
        card["asked_count"] = _as_int(card.get("asked_count")) + 1
        hits += 1
    for term, outcome in [(t, "没答上") for t in _as_list(weak)] + \
                         [(t, "答上了") for t in _as_list(good)]:
        card = index.get(normalize_id(term))
        if card is None:
            continue
        card["last_outcome"] = outcome
        card["last_interview_jd"] = str(jd_id or "")
    for term in _as_list(weak):
        card = index.get(normalize_id(term))
        if card is None:
            continue
        weak_n += 1
        if str(card.get("state")) == "已掌握":
            card["state"] = "学习中"          # 你以为会了，其实不会
            downgraded += 1
    store.save_knowledge(data)
    log.log_event("kb.interview_recorded", jd=jd_id, hits=hits,
                  weak=weak_n, downgraded=downgraded)
    return hits, weak_n, downgraded


def upsert_check(term, state, meta=None):
    """报告里勾选/取消一个知识点：不存在就建卡，存在就只改 state。

    返回 (ok, created, card)。**只改 state 与"填空"，绝不用更短的定义覆盖已有内容**
    —— 沿用 _merge_into 的既有原则。

    为什么要自动建卡：报告里的勾是**用户已经做出的判断**。概念在知识库里还没有卡时
    直接拒绝，勾就丢了（正是「关掉程序勾就没了」的复发）。宁可多一张卡，也不能丢用户的勾。

    ⚠️ **库读不出来时走的是 (b)「留档后写」，不是 (a)「拒绝」**（`_with_kb_write` 的默认
    `write_when_unreadable=True`）：先 `_quarantine_corrupt_kb()` 把坏文件**逐字节**改名成
    `.corrupt-*`，再把用户这个勾写进新库。

    **为什么选 (b)**（而不是像 `set_state` / `merge_nodes` 那样一个字节都不动）：本函数写的
    是**用户这次的操作**（刚点下的那个勾），不是"库里已有的某张卡"。选 (a) 也有一条站得住的
    理由 —— 库读不出来时"目标卡在不在"这个判据整个失效，而且**报告里的勾可以在用户修好库
    之后重新点一次**（勾还在报告页上，重不重试由用户决定）；但项目的既有取舍是
    **绝不丢掉用户刚做完的操作**：留档之后"用户的卡被空库顶掉"这件事已经不成立了
    （那些字节一个不少地躺在 `.corrupt-*` 里），所以宁可留档后写。
    （`absorb` 同属这一类；`record_interview` 原本也在这里，2026-10-15 改成了 (a) ——
    它写的其实是"库里那些已有的卡"，坏库下 save 下去的只是一份空库，见该函数 docstring。）

    库读不出来**且留档也失败**时返回 `(False, False, None)`：与"术语不合法 / state 不合法"
    同一个外形，调用方（路由）本来就把 ok=False 当"这次没勾上"处理。

    load → 改 → save 整段进 _with_kb_write：与 absorb / set_state 同类，无锁并发会丢更新。
    """
    if state not in STATE_VALUES:
        return False, False, None
    nid = normalize_id(term)
    if not nid:
        return False, False, None
    meta = meta if isinstance(meta, dict) else {}
    invoked, result = _with_kb_write(
        lambda data: _upsert_check_in(data, nid, term, state, meta))
    if not invoked:
        return False, False, None
    return result


def _upsert_check_in(data, nid, term, state, meta):
    """`upsert_check` 的主体（调用方已持锁、已过入参守卫、已隔离坏库）。"""
    for n in data["nodes"]:
        if n.get("id") == nid:
            n["state"] = state
            for key in ("definition", "plain_explanation", "category"):
                new_val = str(meta.get(key) or "").strip()
                if len(new_val) > len(str(n.get(key) or "").strip()):
                    n[key] = new_val
            store.save_knowledge(data)
            return True, False, n
    card = _new_card({"term": str(term).strip(),
                      "definition": str(meta.get("definition") or "").strip(),
                      "plain_explanation": str(meta.get("plain_explanation") or "").strip(),
                      "category": str(meta.get("category") or "").strip()}, None)
    card["state"] = state
    _normalise_review_fields(card)
    data["nodes"].append(card)
    store.save_knowledge(data)
    log.log_event("kb.check_created", id=nid, state=state)
    return True, True, card


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


# ---------------------------------------------------------------- find_duplicates 的剪枝索引
# 批 6.3：原来是**无条件**两两比较（O(N²)），库很干净也不提前返回 —— 145 卡 0.03s、
# 1000 卡 1.52s、2000 卡 5.71s，而 /api/knowledge 每次打开知识库 tab 都全量重算。
# 剪枝只回答"这张卡**可能**跟谁重复"：候选集必须是真配对的**超集**，撞上之后仍由
# `_dup_reason` 说了算 ⇒ 判据、结果、顺序、`limit` 截断语义一个字都不用改。
#
# `_MAX_LEN_GAP = 4`：`_dup_reason` 里「包含」「插入式」两处长度差门槛都是 4。
_MAX_LEN_GAP = 4
# 短串的一个相邻字对，在长串里两字之间最多夹进 4 个插入字 ⇒ 距离 ≤ 1 + 4 = 5。
_PAIR_MAX_GAP = _MAX_LEN_GAP + 1
# 「同义后缀」的词干键让"单字 id"的兜底池半径还可能被后缀词撑大（词干 1 字 + 最长后缀词）
_TAIL_MAX_LEN = max(len(w) for w in _TAIL_WORDS)


def _block_keys(s):
    """`s` **若与别的卡重复就必然与对方共享**的索引键（`find_duplicates` 的剪枝索引）。

    ⚠️ 方向不能反：这里给的是"真配对 ⇒ 共享某键"的**必要条件**，所以宁可多给键（多比较几对），
    也绝不能漏键（漏一个键就会漏掉一对真重复 —— 那正是"剪枝改变了判据"）。

    为什么这几类键够用（`d = |len(长) - len(短)|`；下面用到的三种判据都要求 `d ≤ 4`）：

      ① 「包含」：短的是长的**连续子串** ⇒ 短串的每个相邻字对在长串里也**相邻**（距离 1）。
      ② 「插入式」：短的是长的**子序列**且 `d ≤ 4` ⇒ 长串 = 短串 + 最多 4 个插入字，于是短串的
         每个相邻字对在长串里的距离 ≤ 1 + 4 = 5。两边都生成"距离 1..5 的有序字对"，
         所以短串那一对（距离 1）与长串那一对（距离 ≤5）是**同一个键值**。
         单看"距离 1"是不够的：插入式配对常常一个相邻字对都不剩
         （「省略恢复」vs「省略识别及恢复」里 `略|恢` 就被「识别及」拆开了，靠 `省|恢` 这类
         距离 ≤5 的字对才撞得上）。
      ③ 「同义后缀」：`_strip_tail` 之后的词干相同 ⇒ 直接把词干当键。
         **这一条不能省**：词干可能只剩 1 个字（「猫体系」/「猫机制」），两边没有任何共享字对。
      ④ 「首尾相近」：要求前 4 字相同 ⇒ 至少共享一个相邻字对（已被 ①②的字对键覆盖）。

    边界：长度 ≤ 1 的 id 没有任何字对，也没有词干可用，由调用方的"单字兜底"逐对比较
    （见 `_candidate_pairs`）——那种卡在真实库里是畸形数据，逐对比一遍最省心也最不容易错。
    """
    keys = set()
    n = len(s)
    for gap in range(1, _PAIR_MAX_GAP + 1):
        for i in range(n - gap):
            keys.add(("字对", s[i], s[i + gap]))
    keys.add(("词干", _strip_tail(s)))
    return keys


def _candidate_pairs(ids):
    """可能重复的下标对 `(i, j)`（`i < j`，按升序给出）—— **一次线性扫描**建索引得来。

    键只被一张卡持有的那些卡直接跳过：一个键只出现一次 ⇒ 它不可能与任何卡重复
    （这正是批 6.3 要的剪枝；库很干净时几乎全部卡片都被跳过，于是"干净库"是**快**的，
    而不是"照样把 N² 跑完"）。
    """
    n = len(ids)
    buckets = {}
    for i, s in enumerate(ids):
        for key in _block_keys(s):
            bucket = buckets.get(key)
            if bucket is None:
                buckets[key] = [i]
            else:
                bucket.append(i)
    cand = set()
    for bucket in buckets.values():
        if len(bucket) < 2:
            continue
        for x in range(len(bucket)):
            ix = bucket[x]
            for y in range(x + 1, len(bucket)):
                iy = bucket[y]
                cand.add(ix * n + iy if ix < iy else iy * n + ix)
    # 单字 id（长度 ≤ 1）的兜底：它没有字对键，只可能靠「包含」与长度 ≤ 1+4 的卡成对
    # （「同义后缀」那条被词干键覆盖了：词干只剩 1 字 ⇒ 对方长度 ≤ 1 + 最长后缀词 = 3，也在这个池子里）。
    # 这类卡极少，逐个跟"短卡池"比一遍即可 —— 宁可多比，绝不漏对。
    singles = [i for i, s in enumerate(ids) if len(s) <= 1]
    if singles:
        pool_radius = max(_MAX_LEN_GAP + 1, 1 + _TAIL_MAX_LEN)
        short_pool = [i for i, s in enumerate(ids) if len(s) <= pool_radius]
        for i in singles:
            for j in short_pool:
                if i != j:
                    cand.add(i * n + j if i < j else j * n + i)
    return sorted(cand)


def find_duplicates(kb, limit=20):
    """启发式找出可能指同一概念的术语对。**只提示，绝不自动合并**（错并难发现）。

    只取字符串 id：知识库可被用户手改出 `"id": 5`，非字符串既无法比较大小写与包含关系，
    也会在 `5 in "abc"` 处抛 TypeError。

    批 6.3 起**先剪枝再两两比较**（原来是纯 O(N²)，库很干净也不提前返回）：先按 `_block_keys`
    建一次线性索引，只比较"索引键与别人撞上"的卡。判据与顺序**一字不改**：
    候选集是 `_dup_reason` 真配对的超集，遍历次序仍是 (i, j) 升序，所以 `limit` 截断的
    仍是同样的前 limit 条（含 `limit=0` 时只出一条这个既有怪语义）。
    """
    ids = [n.get("id") for n in ensure_kb(kb)["nodes"]
           if isinstance(n.get("id"), str) and n.get("id")]
    out = []
    n = len(ids)
    for packed in _candidate_pairs(ids):
        i, j = divmod(packed, n)
        reason = _dup_reason(ids[i], ids[j])
        if reason:
            out.append({"a": ids[i], "b": ids[j], "reason": reason})
            if len(out) >= limit:
                return out
    return out


def merge_nodes(keep_id, drop_id):
    """手动合并：把 drop 并入 keep，drop 的写法记进 aliases 后移除。返回 (ok, msg)。

    参数可以是**身份键**也可以是**原始术语**（内部先 normalize_id，对已归一化的 id 是 no-op）。

    学习进度**取并集**（见下方注释）：手动合并的是两张「用户卡」，两张卡的进度都是用户的真实
    作答结果，选错方向（把已掌握的并进待学习的）不该静默丢掉复习进度。这与 _merge_into 的
    「只保 keep 进度」并不矛盾 —— 那条铁律守的是「AI 新节点不许覆盖你的进度」，这里两边都是你。

    ⚠️ **两张卡都在时本函数一定会 save**（没有别的出口）—— P0-1 盯的就是这种形状：
    save 之前必须有闸门。整段走 `_with_kb_write`，而它属于 **(a) 拒绝**那一类
    （`write_when_unreadable=False`）：库读不出来时连哨兵都到不了，盘上一个字节不动。

    两张卡缺一张时（含"库读不出来 ⇒ 读成空库"）**连隔离都不做**，原样返回
    "找不到要合并的知识点"、盘上一个字节不动（`refuse_when`）。库读不出来时由
    `write_when_unreadable=False` 那条提前返回 `(False, _KB_WRITE_REFUSED)`
    （"文件原样未动、本次也没有留档"）：调用方（路由）本来就按 ok=False 如实回报，
    这句 msg 还会说清是"库读不出来"，用户据此知道该去修库、而不是去核对卡片。
    本入口到不了"留档失败"那一支 —— 库读不出来时它已经提前返回，库读得出来时哨兵只会返回 True。
    """
    keep_id, drop_id = normalize_id(keep_id), normalize_id(drop_id)
    if not keep_id or not drop_id:
        return False, "两个知识点都要选"
    if keep_id == drop_id:
        return False, "不能和自己合并"
    # load → 改两张卡 → save 整段串行：不加锁时并发的 absorb/set_state 会把这次合并整个覆盖掉。
    # refuse_when：两张卡缺一张就什么都不写（连隔离都不做）—— 与 _merge_nodes_locked 里原来的
    # "找不到要合并的知识点"完全同一个判据、同一句人话，只是提到隔离之前判。
    invoked, refusal = _with_kb_write(
        lambda kb: _merge_nodes_locked(kb, keep_id, drop_id),
        refuse_when=lambda kb: None if (_card_index(kb).get(keep_id)
                                        and _card_index(kb).get(drop_id))
        else "找不到要合并的知识点",
        write_when_unreadable=False)
    if not invoked:
        return False, refusal or _KB_WRITE_REFUSED
    return refusal


def _merge_nodes_locked(kb, keep_id, drop_id):
    """merge_nodes 的主体，**由 `_with_kb_write` 调用**（锁、哨兵与"两张卡在不在"的守卫都在那里）。

    到这一步两张卡一定都在（`_card_index` 是同一个判据），所以这里是直取而不是再判一次缺失。
    入参的 keep_id / drop_id 已 normalize、且已排除「空 id」与「自己和自己合并」。
    """
    index = _card_index(kb)
    keep, drop = index[keep_id], index[drop_id]

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
