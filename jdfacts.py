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

数据模型与接口见 `docs/superpowers/specs/2026-10-12-知识地图缓存-design.md` §3 / §5。
"""
import os
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


def needs_compute(jd_id, refresh=False):
    """「要真算吗」：有缓存且没要求刷新 → False；其余（无缓存 / 强制刷新）→ True。

    它**只回答缓存够不够**：没有缓存但能播种时同样是 True —— 播种是 `resolve` 的第 ② 级，
    不在这里判（否则「读报告」的判定会散成两套）。
    """
    return bool(refresh) or knowledge_of(jd_id) is None


# ---------------------------------------------------------------- 写
def _store(jd_id, nodes, source, at=None, seeded_at=None):
    """在 `jdid._JD_LOCK` 里「读 `jd_facts` → 改 → 写回」，返回落盘后的那份 `jd_facts`。

    `knowledge_at` 是**这份知识地图的生成时间**：已有就保留（只有「强制刷新」会显式传 `at`
    覆盖它 —— 用户明确要了一份新的，报告里的「生成于」就该跟着走）。
    """
    got = _as_nodes(nodes)
    if got is None:
        raise ValueError("知识地图必须是节点列表（或 {\"nodes\": [...]}）：%r" % (nodes,))
    with jdid._JD_LOCK:
        rec = jdid.find_jd(jd_id)
        if rec is None:
            raise LookupError("JD 库里没有这个岗位：%r" % (jd_id,))
        facts = dict(_facts_of(rec))
        facts["knowledge"] = got
        facts["knowledge_source"] = _text(source) or ""
        facts["knowledge_at"] = _text(at) or _text(facts.get("knowledge_at")) or _now()
        if seeded_at:
            facts["seeded_at"] = seeded_at
        jdid.upsert_jd({"id": rec.get("id"), FACTS_KEY: facts})
        return facts


def save_knowledge(jd_id, nodes, source):
    """写 `jd_facts.knowledge` + `knowledge_at`（**首次生成时间，已存在则保留**）+ `knowledge_source`。

    岗位不存在 → LookupError（绝不凭空造一条 JD 记录）。
    """
    return _store(jd_id, nodes, source)


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
    """播种的实体：`(节点列表, 报告id)`；读不出来 → `(None, None)`（绝不猜、绝不崩）。"""
    want = _text(jd_id)
    if not want or jdid.find_jd(want) is None:
        return None, None
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
        _store(want, nodes, SEEDED_PREFIX + rid, seeded_at=_now())
        log.log_event("analyze.knowledge_seeded", jd=want, report=rid, nodes=len(nodes))
        return nodes, rid
    return None, None


def seed_from_reports(jd_id):
    """从**该 JD 自己已有分析**的报告里读 `REPORT_DATA.knowledge` 播种。

    返回 `(True, 报告id)` 或 `(False, None)`。读不出来（文件缺失 / 坏 JSON / 没有 REPORT_DATA /
    知识地图为空或垃圾）一律**静默**返回 `(False, None)`：不抛异常、不写空缓存、
    更不用别的 JD 的知识地图 —— 由调用方退回真算那条路。
    """
    nodes, rid = _seed(jd_id)
    if nodes and rid:
        return (True, rid)
    return (False, None)


# ---------------------------------------------------------------- 编排
def resolve(jd_id, jd_text, cfg, refresh=False, builder=None):
    """**唯一编排入口**：① 缓存 → ② 播种 → ③ 真算（三级回退）。返回 `(nodes, source)`。

    顺序是契约（规格 §5）：缓存命中就**不播种也不算**；缓存没命中才试播种；播种不到才真算。

    `builder(jd_text, cfg)` 由调用方注入（生产里是 `knowledge.build_for_jd`）；本模块不 import
    knowledge，所以测试里永远不会真联网搜索。`refresh=True` 跳过①②直接重算，并把
    `knowledge_at` 改成本次时间（用户明确要了一份新的）。
    """
    if jdid.find_jd(jd_id) is None:
        # 身份先校验：否则「强制刷新」会在一个不存在的岗位上空烧 53 次搜索，最后才写盘失败
        raise LookupError("JD 库里没有这个岗位：%r" % (jd_id,))

    if not refresh:
        cached = knowledge_of(jd_id)
        if cached:
            log.log_event("analyze.knowledge_reused", jd=jd_id, nodes=len(cached),
                          at=_text(get(jd_id).get("knowledge_at")) or "")
            return cached, SOURCE_CACHE
        ok, rid = seed_from_reports(jd_id)
        if ok:
            seeded = knowledge_of(jd_id)      # 从**落盘的那份**取，保证返回的与缓存逐字一致
            if seeded:
                return seeded, SEEDED_PREFIX + rid

    if builder is None:
        raise ValueError("真算知识地图需要调用方注入 builder（生产里是 knowledge.build_for_jd）")
    replaced_at = (_text(get(jd_id).get("knowledge_at")) or "") if refresh else ""
    facts = _store(jd_id, builder(jd_text, cfg), SOURCE_COMPUTED,
                   at=_now() if refresh else None)
    nodes = facts["knowledge"]
    log.log_event("analyze.knowledge_computed", jd=jd_id, nodes=len(nodes),
                  refresh=bool(refresh), replaced_at=replaced_at)
    return nodes, SOURCE_COMPUTED
