# -*- coding: utf-8 -*-
"""JD 级事实缓存（`jds.json` 每条 JD 记录里的 `jd_facts`）：知识地图**每个 JD 只算一次**。

**修的是什么**（用户真实日志）：

    analyze.knowledge_done  {"ms": 206206, "nodes": 53}    ← 知识地图：206 秒、53 次搜索
    analyze.success         {"ms": 236186}                 ← 一次分析总计 236 秒

知识地图占一次分析 **87% 的时间与全部 ~53 次 Tavily 搜索额度**，而它是**纯 JD 级**的
（"这个岗位要会什么"，与哪份简历无关）。`knowledge.build_for_jd(jd_text, cfg)` 本来就只吃
JD 文本，所以「同一个 JD 只算一次、换简历直接复用」是白捡的收益 —— 而且**模型看到的输入
一个字不变**（只缓存知识地图，不拆主调用）。

**三级回退（顺序是契约，见规格 §5）**：

    ① 缓存（`jd_facts.knowledge`）            → `source="cache"`，0 秒 0 搜索
    ② 播种（该 JD **自己**已有分析的报告里那份）→ `source="seeded:<报告id>"`，0 秒 0 搜索
    ③ 真算（调用方注入的 `builder`）           → `source="computed"`，206 秒 + 53 次搜索

**铁律**：
  1. **绝不 import `knowledge`**：真算那一步由调用方通过 `builder=` 注入（生产里 app.py 传
     `knowledge.build_for_jd`）。否则测试会真的联网搜索、烧掉用户的搜索额度。
  2. 写 `jds.json` 一律持 `jdid._JD_LOCK`（本项目真实发生过并发丢更新），且读-改-写是**一个**
     临界区。本模块**只**拿这一把锁，绝不反向去碰 `kb._KB_LOCK`
     （锁顺序恒为 `_JD_LOCK` → `_KB_LOCK`）。
  3. **播种只认「该 JD 自己的分析」**（`jdid.analyses_of`）：读不出来（文件缺失 / 坏 JSON /
     没有 REPORT_DATA / 知识地图为空）一律**静默**返回 `(False, None)`，由调用方退回真算。
     **绝不猜、绝不用别的 JD 的知识地图、绝不写一份空缓存冒充「已缓存」。**
  4. 缓存 `build_for_jd` 的**原始输出**，不从知识库重建：知识库是**跨 JD 累积**的
     （`sources` 会并入别的岗位、节点会被合并、`aliases` 会增长），拿它重建出来的已经不是
     「这个 JD 当时的视图」了。知识库那边照旧由 `kb.absorb` 累积，两者用途不同。
  5. 磁盘是唯一真相源：每次读都重新解析 `jds.json`，所以调用方（比如 `kb.absorb`）就地改动
     返回的节点列表也不会污染缓存。
  6. **同一个 JD 的并发请求只许真算一次**（单飞）：真算是 206 秒 + ~53 次搜索额度，而服务是
     `ThreadingHTTPServer` —— 双击 / 双标签页 / 重试都会让两个线程同时看到「没有缓存」。
     等待者**绝不各算一遍**，也不许死等：领导者失败时它自己再算一次。
     单飞登记表由 `_INFLIGHT_LOCK` 护着，**绝不用 `_JD_LOCK` 跨 `builder`**
     （那会让所有 JD 的写入一起停 206 秒）。等待者拿到领导者的结果后按 **`cache`** 报
     （它没有重新搜索），日志里用 `analyze.knowledge_joined` 分辨「这是等待者」。

数据模型与接口见 `docs/superpowers/specs/2026-10-12-知识地图缓存-design.md` §3 / §5。
"""
import os
import threading
import time
from datetime import datetime

import jdid
import kb
import log
import store

FACTS_KEY = "jd_facts"
NOW_FMT = "%Y-%m-%d %H:%M"          # 与 `jdid.upsert_jd` 的 created_at 同一格式（分钟粒度）

SOURCE_CACHE = "cache"              # 复用缓存
SOURCE_COMPUTED = "computed"        # 真算
SEEDED_PREFIX = "seeded:"           # 播种：`seeded:<报告id>`

EVENT_JOINED = "analyze.knowledge_joined"        # 等待者加入了别人「在飞」的那次真算
EVENT_SEED_SKIPPED = "analyze.knowledge_seed_skipped"   # 临界区里发现已有可用缓存 → 放弃播种写


# ---------------------------------------------------------------- 单飞（同一个 JD 只算一次）
_INFLIGHT_LOCK = threading.Lock()   # 只护着下面这张登记表；**绝不**跨 `builder` 持有
_INFLIGHT = {}                      # jd_id -> _Flight：这个 JD 上正在「真算」的那一次


class _Flight(object):
    """一次「真算」的登记。`done` 给等待者发信号；`nodes` / `error` 是那次的结果。

    第二个线程**等它算完直接拿结果**，绝不各算一遍。领导者失败（builder 抛异常）时
    `error` 会被填上、`done` **照样**会被置上：等待者醒来发现没有结果就自己再去算一次 ——
    **绝不死等**。

    这里**没有** `source`：等待者一律按「复用」（`SOURCE_CACHE`）报，不读领导者的来源
    —— 理由见 `_compute_once` 里那段产品裁定（报了 `computed` 用户会以为又烧了一遍额度）。
    """
    __slots__ = ("done", "nodes", "error")

    def __init__(self):
        self.done = threading.Event()
        self.nodes = None
        self.error = None


def _join_inflight(flight):
    """等同一个 JD 上正在真算的那个线程；返回等了多久（秒）。

    **单独一个函数是给测试留的同步缝**：替换它，测试就能确定性地知道「等待者已经进入了
    等待」，不必靠 sleep 去猜（sleep 型的并发测试既可能假绿也可能假红）。
    """
    t0 = time.monotonic()
    flight.done.wait()
    return time.monotonic() - t0


def _enter_flight(jd_id):
    """登记「本线程要真算这个 JD」；已经有线程在算 → `(None, 那份登记)`。"""
    with _INFLIGHT_LOCK:
        flight = _INFLIGHT.get(jd_id)
        if flight is None:
            flight = _Flight()
            _INFLIGHT[jd_id] = flight
            return flight, None
        return None, flight


def _leave_flight(jd_id, flight):
    """注销登记。结果（`done`/`nodes`）**先写好**，所以这中间到的线程会直接拿到刚算好的那份。"""
    with _INFLIGHT_LOCK:
        if _INFLIGHT.get(jd_id) is flight:
            del _INFLIGHT[jd_id]


def _now():
    """当前时间。**测试用它注入时钟**（真实实现只有分钟粒度，不注入就会靠巧合恒真）。"""
    return datetime.now().strftime(NOW_FMT)


def _text(value):
    """取非空字符串；其它（None / 数字 / 列表）一律当缺失 → None。"""
    if not isinstance(value, str):
        return None
    v = value.strip()
    return v or None


def _as_nodes(value):
    """知识地图的「节点列表」形态：`list` 直接用；`{"nodes": [...]}` 取 `nodes`；其它 → None。

    容忍 dict 形态是因为旧报告/手写数据里出现过（`kb.absorb_reports` 同样两种都认）。
    """
    if isinstance(value, dict):
        value = value.get("nodes")
    return value if isinstance(value, list) else None


def _facts_of(rec):
    """记录里的 `jd_facts`（缺失 / 不是字典 → `{}`，用户可手改这个文件）。"""
    facts = rec.get(FACTS_KEY) if isinstance(rec, dict) else None
    return facts if isinstance(facts, dict) else {}


# ---------------------------------------------------------------- 读
def get(jd_id):
    """读该 JD 的 `jd_facts`。缺失 / 垃圾（不是 dict）→ `{}`；**岗位不存在 → LookupError**。

    返回的是**副本**：调用方改它不会污染库（磁盘才是真相源）。
    """
    rec = jdid.find_jd(jd_id)
    if rec is None:
        raise LookupError("JD 库里没有这个岗位：%r" % (jd_id,))
    return dict(_facts_of(rec))


def knowledge_of(jd_id):
    """只取缓存的知识地图；没有（缺字段 / 空列表 / 垃圾）→ None。"""
    return _as_nodes(get(jd_id).get("knowledge")) or None


# ---------------------------------------------------------------- 写
def _store(jd_id, nodes, source, at=None, seeded_at=None, only_if_no_cache=False):
    """在 `jdid._JD_LOCK` 里「读 `jd_facts` → 改 → 写回」，返回落盘后的那份 `jd_facts`。

    `knowledge_at` 是**这份知识地图的生成时间**：
      * 给了 `at`（强制刷新 = 本次时间；播种 = 那份报告**真正生成**的时间）→ 用它；
      * 否则只在**旧缓存是非空列表**时沿用旧时间戳 —— 空列表不是可用缓存，
        沿用它的时间戳会让新算出来的地图显示错的时间（报告里「生成于」那行）。

    `only_if_no_cache=True`（播种用）：进临界区后**重判**一次，此刻已经有可用缓存就
    **放弃这次写入**并返回 `None` —— 播种绝不用旧报告覆盖更新的那份（那会让内容与
    时间戳对不上：内容来自旧报告、时间来自刚算完的那次）。
    """
    got = _as_nodes(nodes)
    if got is None:
        raise ValueError("知识地图必须是节点列表（或 {\"nodes\": [...]}）：%r" % (nodes,))
    src = _text(source)
    if src is None:
        raise ValueError("knowledge_source 必须是非空字符串：%r" % (source,))
    with jdid._JD_LOCK:
        rec = jdid.find_jd(jd_id)
        if rec is None:
            raise LookupError("JD 库里没有这个岗位：%r" % (jd_id,))
        facts = dict(_facts_of(rec))
        old = _as_nodes(facts.get("knowledge"))      # 覆盖**之前**的旧缓存
        if only_if_no_cache and old:
            return None                              # 此刻已经有可用缓存了：放弃这次写入
        facts["knowledge"] = got
        facts["knowledge_source"] = src
        if _text(at):
            facts["knowledge_at"] = _text(at)
        elif old:
            facts["knowledge_at"] = _text(facts.get("knowledge_at")) or _now()
        else:
            facts["knowledge_at"] = _now()
        if seeded_at:
            facts["seeded_at"] = seeded_at
        jdid.upsert_jd({"id": rec.get("id"), FACTS_KEY: facts})
        return facts


# ---------------------------------------------------------------- 播种
def _report_of(analysis):
    """一条分析记录的报告文件 → `(绝对路径, 报告id)`；定位不了 → `(None, None)`。

    **报告 id 就是报告文件名去掉 `.html`**：与 `app.render_report` 写进 REPORT_DATA 的
    `report_id`、`store.report_state_path()` 的入参、前端 `body["report"][:-5]` 是同一个东西。
    记录里没有 `report` 时退回 `<分析id>.html`（迁移前的约定就是「分析 id == 报告文件名」）——
    这是定位**它自己的**报告，不是猜别人的。
    """
    name = _text(analysis.get("report")) or ""
    if not name:
        aid = _text(analysis.get("id"))
        name = (aid + ".html") if aid else ""
    name = store.safe_name(name)          # 取 basename：`../evil.html` 出不了 reports 目录
    if not name:
        return None, None
    rid = name[:-5] if name.endswith(".html") else name
    return os.path.join(store.p_path("reports"), name), rid


def _seed(jd_id):
    """播种的实体：写进去了就返回**报告id**；读不出来 → `None`（绝不猜、绝不崩）。

    只回一个值就够：唯一调用方 `seed_from_reports` 要知道的正是「写成功了没有、是哪份报告」，
    而写成功时节点列表**必然非空**（下面 `if not nodes: continue` 保证了这一点）。
    回一份调用方随即丢掉的节点列表，只会让人以为"外面还要用这份节点"。
    """
    want = _text(jd_id)
    if not want or jdid.find_jd(want) is None:
        return None
    # created_at 升序；坏报告**跳过继续找**，不能因为最新那份没知识就放弃播种
    for analysis in jdid.analyses_of(want):
        path, rid = _report_of(analysis)
        if not path or not rid:
            continue
        try:
            data = kb.read_report_data(path)      # 文件缺失 / 坏 JSON → None（它绝不抛）
            nodes = _as_nodes(data.get("knowledge")) if isinstance(data, dict) else None
        except Exception:
            nodes = None                          # 任何意外都当「读不出来」，静默走真算
        if not nodes:
            continue
        facts = _store(want, nodes, SEEDED_PREFIX + rid,
                       # `knowledge_at` 是这份地图**真正生成**的时间 = 那份分析记录的
                       # `created_at`；`seeded_at` 才是「这次播种发生在何时」。写成同一个时间
                       # 的话，报告里的小字会显示一个**晚于报告本身**的生成日期。
                       at=_text(analysis.get("created_at")), seeded_at=_now(),
                       only_if_no_cache=True)
        if facts is None:
            # 临界区里发现已经有可用缓存（窄窗口：另一个线程刚把真算落盘）→ 放弃这次播种写。
            # 调用方（`resolve`）会再看一眼缓存：那时那份缓存比任何旧报告都新。
            log.log_event(EVENT_SEED_SKIPPED, jd=want, report=rid)
            return None
        log.log_event("analyze.knowledge_seeded", jd=want, report=rid, nodes=len(nodes))
        return rid
    return None


def seed_from_reports(jd_id):
    """从**该 JD 自己已有分析**的报告里读 `REPORT_DATA.knowledge` 播种。

    返回 `(True, 报告id)` 或 `(False, None)`。读不出来（文件缺失 / 坏 JSON / 没有 REPORT_DATA /
    知识地图为空或垃圾）一律**静默**返回 `(False, None)`：不抛异常、不写空缓存、
    更不用别的 JD 的知识地图 —— 由调用方退回真算那条路。

    **播种写守卫**：进 `_JD_LOCK` 临界区后重判一次，此刻已经有可用缓存（比如另一个线程刚
    真算完）就**放弃这次写入**、同样返回 `(False, None)` —— 绝不用旧报告覆盖更新的那份。
    """
    rid = _seed(jd_id)
    if rid:
        return (True, rid)
    return (False, None)


# ---------------------------------------------------------------- 编排
def _reuse(jd_id, cached):
    """① 缓存命中（或等待者拿到了刚落盘的那份）：记事件、返回 `(节点, "cache")`。"""
    log.log_event("analyze.knowledge_reused", jd=jd_id, nodes=len(cached),
                  at=_text(get(jd_id).get("knowledge_at")) or "")
    return cached, SOURCE_CACHE


def _compute(jd_id, jd_text, cfg, builder, refresh):
    """③ 真算一次并落盘。**单飞登记由调用方负责**（同一个 JD 同一时刻只走一个）。"""
    replaced_at = (_text(get(jd_id).get("knowledge_at")) or "") if refresh else ""
    # ⚠️ `builder(...)` 是**实参**：它在 `_store` 取 `_JD_LOCK` 之前就求值完了。
    # 真算要 206 秒，抱着 `_JD_LOCK` 去算就等于让**所有 JD 的写入**一起停 206 秒。
    computed = builder(jd_text, cfg)
    facts = _store(jd_id, computed, SOURCE_COMPUTED, at=_now() if refresh else None)
    nodes = facts["knowledge"]
    log.log_event("analyze.knowledge_computed", jd=jd_id, nodes=len(nodes),
                  refresh=bool(refresh), replaced_at=replaced_at)
    return nodes, SOURCE_COMPUTED


def _compute_once(jd_id, jd_text, cfg, builder, refresh):
    """③ 真算的**单飞**入口：同一个 JD 同一时刻只算一次，其它线程等它、直接拿它的结果。

    没有单飞的话，双击 / 双标签页 / 重试会让两个线程同时看到「没有缓存」→ 各烧一遍
    206 秒 + ~53 次搜索额度（服务是 `ThreadingHTTPServer`，全项目没有分析级锁）。
    """
    while True:
        mine, other = _enter_flight(jd_id)
        if mine is None:
            # ---- 等待者：绝不各算一遍 ----
            waited = _join_inflight(other)
            if other.error is not None:
                # 领导者失败了 → **绝不死等**：摘掉那份失败的登记，自己再抢一次领导权去算
                with _INFLIGHT_LOCK:
                    if _INFLIGHT.get(jd_id) is other:
                        del _INFLIGHT[jd_id]
                continue
            nodes = knowledge_of(jd_id)      # 磁盘是唯一真相源：重新解析，不与它共享同一个列表
            if not nodes:
                nodes = list(other.nodes or [])   # 空地图（`[]` 不算可用缓存）：结果就是它那份
            log.log_event(EVENT_JOINED, jd=jd_id, nodes=len(nodes), ms=int(waited * 1000))
            # ⚠️ 按**复用**报，**绝不**报领导者那次算出来的来源（产品裁定）：报告里那一行小字的
            # 全部意义是「**本次有没有重新搜索**」（模板原文：「复用该岗位 <日期> 生成的那份
            # （本次没有重新搜索）」）。等待者确实没有重新搜索 —— 它只是在旁边等领导者的那一次。
            # 报 `computed` 会让用户以为这次又烧了一遍 ~53 次搜索额度，而那正是这一行要传达
            # 的信息。「这是等待者（不是缓存命中）」由上面的 `analyze.knowledge_joined` 分辨。
            return nodes, SOURCE_CACHE

        result = None
        try:
            if not refresh:
                cached = knowledge_of(jd_id)   # 抢到领导权后再判一次：别人可能刚算完并注销了
                if cached:
                    result = _reuse(jd_id, cached)
                    return result
            result = _compute(jd_id, jd_text, cfg, builder, refresh)
            return result
        except BaseException as exc:
            # 让等待者知道「这次没算出来」—— 否则它会把「空地图」当成领导者的结果（等于骗用户）
            mine.error = exc
            raise
        finally:
            if result is not None:
                # 只把「结果」交出去；来源不登记（等待者一律按「复用」报，见上面那段裁定）。
                mine.nodes = result[0]
            mine.done.set()                 # 先把结果放出去（`done` 一置上等待者就会读它）…
            _leave_flight(jd_id, mine)      # …再注销登记


def resolve(jd_id, jd_text, cfg, refresh=False, builder=None):
    """**唯一编排入口**：① 缓存 → ② 播种 → ③ 真算（三级回退）。返回 `(nodes, source)`。

    顺序是契约（规格 §5）：缓存命中就**不播种也不算**；缓存没命中才试播种；播种不到才真算。

    `builder(jd_text, cfg)` 由调用方注入（生产里是 `knowledge.build_for_jd`）；本模块不 import
    knowledge，所以测试里永远不会真联网搜索。`refresh=True` 跳过①②直接重算，并把
    `knowledge_at` 改成本次时间（用户明确要了一份新的）。

    ③ 那一步是**单飞**的：同一个 JD 的并发请求只烧一次（206 秒 + ~53 次搜索额度），
    等待者拿的正是领导者的结果，但按**复用**报（`source="cache"`）—— 它没有重新搜索，
    报「本次新算」会让用户以为又烧了一遍额度（日志里 `analyze.knowledge_joined` 分辨得出）。
    """
    if jdid.find_jd(jd_id) is None:
        # 身份先校验：否则「强制刷新」会在一个不存在的岗位上空烧 53 次搜索，最后才写盘失败
        raise LookupError("JD 库里没有这个岗位：%r" % (jd_id,))

    if not refresh:
        cached = knowledge_of(jd_id)
        if cached:
            return _reuse(jd_id, cached)
        ok, rid = seed_from_reports(jd_id)
        if ok:
            seeded = knowledge_of(jd_id)      # 从**落盘的那份**取，保证返回的与缓存逐字一致
            if seeded:
                return seeded, SEEDED_PREFIX + rid
        # 播种可能**故意没写**（临界区里发现另一个线程刚把真算落盘）→ 再看一眼缓存：
        # 那份比任何旧报告都新，直接复用它（否则又会多烧一次 206 秒）。
        cached = knowledge_of(jd_id)
        if cached:
            return _reuse(jd_id, cached)

    if builder is None:
        raise ValueError("真算知识地图需要调用方注入 builder（生产里是 knowledge.build_for_jd）")
    return _compute_once(jd_id, jd_text, cfg, builder, refresh)
