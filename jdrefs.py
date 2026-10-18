# -*- coding: utf-8 -*-
"""**引用点单一真源**：一处登记「谁指向一个岗位」，只读扫描 + 逐项重写。

**修的是什么**（体检报告 §8.1）：「指向岗位的引用点」这份清单原先散在 **4 个模块**里各列一遍
（`jdid.rename_jd` / `jddelete._plan`+`_purge_locked` / `jobdup.merge_jobs` / `身份迁移.py`），
实测已经**漏了两处**，而这两处一旦漏就是"盘上孤儿"：

    `report_state/<报告id>.json`   ← 删完 JD 之后它还在盘上，预演里也不列
    `interviews/<面试id>/feedback.json`   ← 同上（反哺标记）

所以本模块的价值**不在重构本身**，而在「**以后再也不会漏**」：新增一个"指向岗位"的产物时，
只要它经过 `store` 的路径构造器，`tests/test_jdrefs.py::GuardTest` 就会**红**，
报错里直接告诉你「要么在 `KINDS` 里登记，要么加进 `WHITELIST` 并写明理由」。

## 全量清点（8 条，含今天漏掉的两条）

| # | 引用点 | kind | 谁写 | count 的含义 |
|---|---|---|---|---|
| 1 | `jds.json` 的 `jds[].id`（**本体**） | `jds` | `jdid` | 1（本体那一条记录） |
| 2 | `jobs.json` 的 `jd_id` | `jobs` | `app` / `jdid` | 命中的台账记录条数 |
| 3 | `knowledge.json` 的 `nodes[].from_jds[].id` | `kb` | `kb` | 命中的**卡片数** |
| 4 | `interviews/*/status.json` 的 `job_id` | `iv_status` | `interview` | 每条 Ref = 一个文件（1） |
| 5 | `report_state/<报告id>.json`（**今天漏的**） | `report_state` | `app`（报告勾选） | 每条 Ref = 一个文件（1） |
| 6 | `interviews/*/feedback.json` 的 jd 键（**今天漏的**） | `iv_feedback` | `interview` | 每条 Ref = 一个文件（1） |
| 7 | `jobs.json` 的 `merged_into`（悬空引用） | `merged_into` | `jobdup` | 命中的台账记录条数（**处理属批 7.2**） |
| 8 | `reports/*.html` 内嵌 JD 数据、`jd/<id>.txt` 原文 | —— | `app` | **策略上不重写** ⇒ 进 `WHITELIST` 并写明理由 |

## 硬约束（照着验收，别只看注释）

1. **`refs_of_jd` 只读**，且一律走 `store` 的**严格读**：**「读不出来」≠「这份 JD 没被引用」**。
   读不出来的每一处都**如实上报**（`Ref.err` 非空、`count = -1`），要用"抛错"那条路的调用方
   走 `refs_of_jd_or_raise`。**绝不允许**把"读不出来"当成"没有引用"——那正是本项目今天两次
   事故的形态（`load_knowledge()` 把坏文件静默归成空库 ⇒ "没有卡引用它"与"库读不出来"不可区分）。
2. **`rewrite_jd_refs` 写盘前每一项都过既有的 `*_safe_to_write` 口径**：任何一处读不出来 ⇒
   **整次拒绝**（`JdRefsRefused`，一个字节都不写），**不是跳过那一项**。落盘中途失败 ⇒
   用原像**逐字节**回滚（`read_bytes` / `write_bytes`，文本模式会换换行符）。
3. **锁序不变**：本模块**自己绝不取** `_JD_LOCK` / `_MERGE_LOCK` / `_KB_LOCK`（那些是调用方
   持有的外层锁，回头去要就是死锁形状）；写 `knowledge.json` 只走 `kb._with_kb_write`
   （它内部持 `_KB_LOCK`）。⚠️ 本模块**不 import** `jdid` / `jddelete` / `jobdup` / `app`
   —— 第 2~5 步里它们会 import 本模块，反向 import 会成环。

## 口径（哪些字面相等、哪些去空白）

* `jobs` 的"属于哪个 JD"用 **`jd_id or id`**（与 `jddelete._jd_id_of_record` / `app._jd_id_of_row`
  同一把尺子）：只认 `jd_id` 的话，**迁移前**形态的记录会"一条都列不出来"（少列最贵）。
* `kb` 的 `from_jds[].id` 用**去空白比**（与 `jddelete` 的预演/清理同一把尺子）。
  ⚠️ 重指变换**只有一份实现**（批 7.1）：`kb.repoint_refs_in(data, old, new, strip=...)`。
  本模块传 `strip=True`（**去空白比**，与 `_detect_kb` 同一把尺子 —— detect 与 rewrite 用
  两把尺子就会"预演列出 3 条、真改 2 条"）；`kb` 的三个窄接口传各自的**历史口径**
  （`purge_jd_refs` 去空白比；`repoint_jd_refs` / `merge_jd_refs` 逐字比）。判据由调用方
  传入是**刻意的**：两边各被既有用例钉着，顺手统一任何一边都会改变"手改出的 `" jd1 "`
  算不算同一条引用"这个对外行为。
* `jds` / `iv_status` 用**逐字比**（与 `jdid` / `jobdup` 的既有实现逐字一致）。
* `report_state` 只按**文件是否存在**判（它的"引用"是**文件名**=报告 id，不是内容）⇒ 这一处
  **不存在**"读不出来 ⇒ 当成没有引用"的洞：文件坏不坏都不影响"它在不在、属不属于这个岗位"。

## 不做什么（划清边界）

* **不**改报告/原文的命名策略（`reports/*.html`、`jd/<id>.txt` 仍按 id 命名，白名单里写明理由）；
* **不**引入数据库/索引文件（保持"JSON + 磁盘即真相"）；
* **不**顺手修 `merged_into` 悬空（那是批 7.2，独立一步 + 独立测试）；
* **不**接线任何调用方（第 1 步只加本模块 + 守卫测试，一个调用方都不动）。
"""
import ast
import glob
import os
from collections import namedtuple

import kb
import log
import store

# `Ref`：一处引用点的只读探测结果。
#   kind  —— `KINDS` 里的登记名（如 `report_state`）
#   path  —— 那一处的**具体路径**（逐文件的 kind 是一条一个文件；聚合的 kind 是那份 JSON）
#   count —— 命中条数；**`-1` = 读不出来、根本不知道有几条**（绝不是 0！0 = 确认没有引用）
#   how   —— 一句给用户看的"这是哪一处引用"（与登记表里的说明同源）
#   err   —— 非空 = 这一处**读不出来**（如实上报；调用方**不许**当成"没有引用"）
# ⚠️ 设计文档里写的是四字段；`err` 是第 5 个字段（附录 A 的硬约束 1 要求"带上 err 的结果"），
# 带默认值所以 `Ref(kind, path, count, how)` 这种老写法照样能用。
Ref = namedtuple("Ref", "kind path count how err")
Ref.__new__.__defaults__ = ("",)

# `Kind`：一种引用点的登记项。
#   products  —— 它落在哪些「构造 profile 内路径」的产物上（守卫测试按它比对）
#   how       —— 给用户看的一句话（`Ref.how` 就是它）
#   detect(jd_id, scan) -> [Ref]     只读扫描
#   rewrite(old, new, refs, key) -> (改了几条, 原因)   逐项重写（`None` = 本步不重写）
#   preflight(key) -> (可写, 原因)    写盘前的判据（一律用既有的 `*_safe_to_write` 口径）
#   deferred  —— True = 本步**刻意不处理**（结果里报 `ok=False`+理由）；False 且 `rewrite=None`
#                = **策略上无需重写**（结果里报 `ok=True`，理由在 `note` 里）
Kind = namedtuple("Kind", "products how detect rewrite preflight deferred note")
Kind.__new__.__defaults__ = (None, None, False, "")


class JdRefsUnreadable(RuntimeError):
    """**有一处引用点读不出来** ⇒ 不知道它指向谁 ⇒ 不许当成"它没引用这个岗位"。

    继承 `RuntimeError`：全仓库「前置条件不满足就中止」用的都是它
    （见 `store.ProfileUnreadable` 的说明），既有的兜底接得住并如实回话。
    """


class JdRefsRefused(RuntimeError):
    """**整次拒绝**：写盘前发现任何一处读不出来 ⇒ 一个字节都不写（不是"跳过那一项"）。"""


class JdRefsWriteFailed(RuntimeError):
    """落盘途中失败（已用原像逐字节回滚；回滚也没成的话文案里会点名哪份没还原）。"""


# ---------------------------------------------------------------- 小工具
def _text(value):
    """非空字符串才算内容（用户可手改 JSON：`5` / `[]` / `None` 一律当没有）。"""
    return value.strip() if isinstance(value, str) else ""


def _jds_path(key=None):
    """`jds.json` 的路径。**不 import `jdid`**（第 3 步它会 import 本模块）：与
    `jdid._jds_path` 是同一份解析（都是 `store.p_path("jds.json", key=key)`）。"""
    return store.p_path("jds.json", key=key)


def jd_of_record(rec):
    """一条台账记录挂在哪个 JD 下：`jd_id` 优先，迁移前的记录退回 `id`。

    判据与 `jddelete._jd_id_of_record` / `app._jd_id_of_row` **同一口径**（本项目最容易写错的
    一处）：只认 `jd_id` 的话，迁移前的数据会"一条分析都列不出来" —— 那正是"少列"最贵的形态
    （用户对着"只有 0 条引用"的清单按了确认）。

    **公开**：合并（`jobdup`）要在**同一次台账事务**里既重指 `jd_id`、又标 `merged_into`，
    它调 `repoint_jobs_in`（下面），判据仍然只有这一份。
    """
    if not isinstance(rec, dict):
        return ""
    return _text(rec.get("jd_id")) or _text(rec.get("id"))


# 一条 `from_jds` 项指向的岗位 id：**实现只有一份** —— `kb.ref_id(x, strip)`（批 7.1 把两份
# 逐行相同的副本收成了一份，本模块**不再**留同名同义的小包装：留一个"转发壳子"会让人以为
# 这里可以独立演化，而那正是"两份实现迟早漂移"的开头）。`strip` 的来历见 `kb.ref_id`
# 与模块文档的「口径」一节（本模块用去空白比，与 `_detect_kb` 的 detect 同一把尺子）。


def _strict(reader, path):
    """严格读 + 「盘上有没有可丢的字节」这道闸门。

    **0 字节 / 根本不存在的文件不算"读不出来"**：里面一个字节都没有，也就没有引用可指
    （那是合法的"第一次"）。其余一律把原因如实带出去 —— 这一步是硬约束 1 的落点。
    """
    data, err = reader(path)
    if err and not store.file_has_bytes_to_lose(path):
        return data, ""
    return data, err


def _interview_files(filename, key=None):
    """`interviews/<面试id>/<filename>` 的全部路径（排序 ⇒ 输出稳定、测试可复现）。"""
    return sorted(glob.glob(os.path.join(store.interviews_dir(key), "*", filename)))


def _interview_unreadable_err(filename, key=None):
    """扫一遍这类面试文件，返回**第一个**"读不出来"的原因（非空 = 不知道它指向谁）。

    判据与 `jdid._interview_status_paths` / `jobdup._interview_status_files` **逐字同一把尺子**：
    不是 dict（含 JSON 坏掉、被 GBK 写坏）**且有字节** → 读不出来。面试记录是每个面试目录
    一个文件，漏一份没有任何集中信号 —— 所以这里宁多不少。
    """
    err = ""
    for path in _interview_files(filename, key):
        data = store.read_json(path, None)
        if isinstance(data, dict):
            continue
        if store.file_has_bytes_to_lose(path):
            err = err or ("面试记录 `%s` 读不出来（有字节、拿不到它的 `%s`）" % (path, filename))
    return err


def _unreadable(kind, path, err):
    """「读不出来」那一条：`count = -1`（未知）**不是** 0（没有引用），并带上原因。"""
    return Ref(kind, path, -1, KINDS[kind].how, err)


class _Scan(object):
    """一次扫描里共享的**严格读**：`jobs.json` 只读一次（三条 kind 共用一份快照）。

    为什么要共享：`jobs` / `report_state` / `merged_into` 三条 kind 都要读台账，各读一遍的话
    它们量的可能是**两个时刻**的台账（中间挤进一次 upsert），预演出来的清单自相矛盾。
    **但"读不出来"仍然每一条 kind 各自如实上报**（见三个 detect 函数里那三处 `jobs_err`）：
    台账读不出来时，`merged_into` 有没有命中、报告 id 是哪几个，**都是未知**，不许报成"没有"。
    """

    __slots__ = ("key", "jobs", "jobs_err")

    def __init__(self, key=None):
        self.key = key
        self.jobs, self.jobs_err = _strict(store.read_jobs_strict, store.jobs_path(key))


def _report_ids_of(rows, jd_id):
    """这个 JD 的各条分析对应的**报告 id**（报告文件名去掉 `.html`），去重、保持出现顺序。

    与 `jdfacts._report_of` 同一口径：`report` 字段优先，空了才退回 `<分析 id>.html`
    （迁移前的约定就是"分析 id == 报告文件名"，那时记录里没有 `report` 字段）。
    报告 id 就是 `store.report_state_path()` 的入参、前端 `body["report"][:-5]` 的那一个东西。
    """
    out, seen = [], set()
    for rec in rows:
        if jd_of_record(rec) != jd_id:
            continue
        name = store.safe_name(rec.get("report"))
        if not name:
            aid = _text(rec.get("id"))
            name = (aid + ".html") if aid else ""
        rid = name[:-5] if name.endswith(".html") else name
        if rid and rid not in seen:
            seen.add(rid)
            out.append(rid)
    return out


# ---------------------------------------------------------------- 只读探测（每个 kind 一份）
def _detect_jds(jd_id, scan):
    """#1 本体：`jds.json` 里有没有 id == jd_id 的记录。"""
    path = _jds_path(scan.key)
    data, err = _strict(store.read_jds_strict, path)
    if err:
        return [_unreadable("jds", path, err)]
    for rec in data.get("jds") or []:
        if isinstance(rec, dict) and rec.get("id") == jd_id:
            return [Ref("jds", path, 1, KINDS["jds"].how)]
    return []


def _detect_jobs(jd_id, scan):
    """#2 `jobs.json` 的 `jd_id`（口径 `jd_id or id`）。"""
    path = store.jobs_path(scan.key)
    if scan.jobs_err:
        return [_unreadable("jobs", path, scan.jobs_err)]
    n = len([r for r in scan.jobs if jd_of_record(r) == jd_id])
    return [Ref("jobs", path, n, KINDS["jobs"].how)] if n else []


def _detect_kb(jd_id, scan):
    """#3 知识库 `nodes[].from_jds[].id`（去空白比）。count = 命中的**卡片数**。"""
    path = store.knowledge_path(scan.key)
    data, err = _strict(store.read_knowledge_strict, path)
    if err:
        return [_unreadable("kb", path, err)]
    n = 0
    for node in data.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        if any(kb.ref_id(x, True) == jd_id for x in (node.get("from_jds") or [])):
            n += 1
    return [Ref("kb", path, n, KINDS["kb"].how)] if n else []


def _scan_interview_json(kind, filename, jd_id, scan, hit):
    """逐文件的面试 JSON：命中的一条 Ref 一个文件；读不出来的那条**如实上报**。"""
    out = []
    for path in _interview_files(filename, scan.key):
        data = store.read_json(path, None)
        if isinstance(data, dict):
            if hit(data):
                out.append(Ref(kind, path, 1, KINDS[kind].how))
            continue
        if store.file_has_bytes_to_lose(path):
            out.append(_unreadable(kind, path,
                                   "面试记录 `%s` 读不出来（有字节、拿不到它的 `%s`）"
                                   % (path, filename)))
    return out


def _detect_iv_status(jd_id, scan):
    """#4 面试记录 `status.json` 的 `job_id`（逐字比，与 `jdid` / `jobdup` 一致）。"""
    return _scan_interview_json("iv_status", "status.json", jd_id, scan,
                                lambda d: d.get("job_id") == jd_id)


def _detect_report_state(jd_id, scan):
    """#5（**今天漏的**）`report_state/<报告id>.json`。

    它里面的内容没有 jd id —— 指过来的是**文件名**（报告 id），而报告 id 由"这个 JD 的各条
    分析记录"决定（`jobs[].report`）。所以台账读不出来时，这一处**也是未知**（不许报成"没有"）。
    """
    room = store.report_state_dir(scan.key)
    if scan.jobs_err:
        return [_unreadable("report_state", room,
                            "%s（读不出台账就无法确认这个岗位的分析报告 id ⇒ "
                            "也就不知道它有几个报告勾选文件）" % scan.jobs_err)]
    out = []
    for rid in _report_ids_of(scan.jobs, jd_id):
        path = store.report_state_path(rid, scan.key)
        if path and os.path.isfile(path):
            out.append(Ref("report_state", path, 1, KINDS["report_state"].how))
    return out


def _detect_iv_feedback(jd_id, scan):
    """#6（**今天漏的**）`feedback.json` 里**以 jd id 为键**的条目。

    键就是 `interview._feedback_id(jd_id)`（= `str(jd_id or "")`），条目内层的 `jd_id` 只是副本，
    重写时一并跟过去（`_rewrite_iv_feedback`）。
    """
    return _scan_interview_json("iv_feedback", "feedback.json", jd_id, scan,
                                lambda d: jd_id in d)


def _detect_merged_into(jd_id, scan):
    """#7 悬空引用：`jobs[].merged_into == jd_id`（**登记但本步不处理**，见批 7.2）。"""
    path = store.jobs_path(scan.key)
    if scan.jobs_err:
        return [_unreadable("merged_into", path, scan.jobs_err)]
    n = len([r for r in scan.jobs
             if isinstance(r, dict) and _text(r.get("merged_into")) == jd_id])
    return [Ref("merged_into", path, n, KINDS["merged_into"].how)] if n else []


# ---------------------------------------------------------------- 写盘前的判据（一律用既有口径）
def _preflight_jds(key):
    """`jds.json` 能不能安全写 —— 用 `store.jds_safe_to_write`（唯一的"读不出来≠空"判据）。"""
    return store.jds_safe_to_write(_jds_path(key))


def _preflight_jobs(key):
    return store.jobs_safe_to_write(store.jobs_path(key))


def _preflight_kb(key):
    return store.knowledge_safe_to_write(store.knowledge_path(key))


def _preflight_interview(filename):
    """面试目录下的 JSON 没有 `*_safe_to_write` 那一对 —— 判据照 `jdid` / `jobdup` 同一把尺子。"""

    def _check(key):
        err = _interview_unreadable_err(filename, key)
        return (not err), err

    return _check


# ---------------------------------------------------------------- 逐项重写
def _write_interview_json(path, obj):
    """面试目录下 JSON 的**唯一落盘口**（单点，便于测试注入一次写失败）。"""
    store.write_json(path, obj)


def _rewrite_jds(old, new, refs, key):
    """#1 本体：改 `jds[].id`，旧值推进 `id_history`（与 `jdid.rename_jd` 同一形状）。

    ⚠️ 只动**一条**记录（与 `jdid.rename_jd` 一致：重复 id 是用户手改出来的病态数据，
    本模块不替它猜哪条该改）。写盘**不**做 `jdid.save_jds` 那层过滤（丢掉没有 id 的记录）：
    本模块不 import `jdid`（会成环），而且"顺手过滤"就是把用户的记录吃掉。
    覆盖前照旧留一份 `.bak-`（`store.snapshot_before_overwrite`，与 `jdid.save_jds` 同口径）。
    """
    path = refs[0].path
    data, err = store.read_jds_strict(path)
    if err:
        return 0, "JD 库 `%s` 在动手前变得读不出来了（预检之后被改坏/被锁）：%s" % (path, err)
    for rec in data.get("jds") or []:
        if not isinstance(rec, dict) or rec.get("id") != old:
            continue
        hist = rec.get("id_history")
        hist = [x for x in hist if x != old] if isinstance(hist, list) else []
        rec["id"] = new
        rec["id_history"] = hist + [old]
        store.snapshot_before_overwrite(path)
        store.write_json(path, data)
        return 1, ""
    return 0, ""


def repoint_jobs_in(rows, old, new, include_legacy=True):
    """原地把 `rows` 里挂在 `old` 名下的记录的 `jd_id` 改指 `new`，返回改了几条（**不落盘**）。

    "判断一条记录挂在哪个 JD 下"只有一份实现（`jd_of_record`：`jd_id or id`）—— 本函数是它的
    唯一一处"改"的落点：
      * `_rewrite_jobs`（改名那条路）在 `store.update_jobs` 的临界区里调它（`include_legacy=True`：
        改名要把**所有**指向旧 id 的地方改干净，迁移前形态的记录靠 `id` 当身份，也得跟过去，
        否则改完 `refs_of_jd(旧)` 仍然非空 —— 旧 id 悬着）；
      * `jobdup._mark_drop_merged`（合并）在**它自己的**那一次台账事务里调它
        （`include_legacy=False`：合并只改**显式写着 `jd_id == old`** 的那些记录）。
        ⚠️ 为什么合并**不**退回 `id`：`merged_into` 那条记录（`id == drop`）按
        `身份迁移.py` 的策略 A **保留原样**（不加 `jd_id`）—— 它是历史标记、台账里不显示；
        给它补一个 `jd_id` 会与"已合并记录不挂到新 JD 上"这条既有决定冲突
        （`tests/test_migration.py::MergedRecordTest` 钉着）。而真·引用（知识库 / 面试
        `status.json` / `feedback.json`）由本模块其余 kind 覆盖，一条都不会漏。

    `merged_into` 字段**不在这里碰**：那是 #7（悬空引用），处理属批 7.2。
    """
    changed = 0
    for rec in rows:
        if not isinstance(rec, dict):
            continue
        if include_legacy:
            if jd_of_record(rec) != old:
                continue
        elif rec.get("jd_id") != old:
            continue
        if rec.get("jd_id") != new:
            rec["jd_id"] = new
            changed += 1
    return changed


def _rewrite_jobs(old, new, refs, key):
    """#2 `jobs.json` 的 `jd_id`（口径 `jd_id or id` ⇒ 迁移前的记录也会被指过去）。

    **读-改-写整段收进 `store.update_jobs`**（与 `jdid.rename_jd` / `jobdup` 原来的做法一致，
    计划 2.4）：判据作用在**刚读出来**的那份台账上；一条都没改 → 传 `None` 回去
    （**一个字节都不写**，也不多留一份 `.bak-`）。写盘前的严格读只用来把"读不出来"如实
    报成原因（`update_jobs` 内部用的是宽容的 `load_jobs`，读不出来会给空台账 —— 那正是
    "当成没有引用"的形态，所以必须先问严格读）。

    `merged_into` 字段**原样留着**：它是 #7 那一处（悬空引用），处理属批 7.2 ——
    本函数只把记录"挂在哪个 JD"改对，绝不替 7.2 做决定。
    """
    _rows, err = store.read_jobs_strict(store.jobs_path(key))
    if err:
        return 0, "台账 `jobs.json` 在动手前变得读不出来了：%s" % err

    def _fn(rows):
        changed = repoint_jobs_in(rows, old, new)
        return (rows if changed else None), changed

    return store.update_jobs(_fn, key), ""


def _rewrite_kb(old, new, refs, key):
    """#3 知识库引用：**只走** `kb._with_kb_write`（内部持 `_KB_LOCK`，坏库哨兵也只有那一份）。

    本模块自己**绝不**取 `_KB_LOCK`（硬约束 3：锁序恒为"调用方的上层锁 → kb 的锁"）。
    ⚠️ 重指变换只有**一份实现**（批 7.1）：`kb.repoint_refs_in(..., strip=True)` ——
    本模块用去空白比，与 `_detect_kb` 同一把尺子（否则"预演列出 3 条、真改 2 条"）。
    """
    def _fn(data):
        touched, _repointed, _deduped = kb.repoint_refs_in(data, old, new, strip=True)
        if touched:
            store.save_knowledge(data, key)
        return touched

    invoked, result = kb._with_kb_write(_fn, write_when_unreadable=False, key=key)
    if not invoked:
        return 0, "知识库读不出来，**一个字节都没写**：%s" % (result,)
    return result, ""


def _rewrite_iv_status(old, new, refs, key):
    """#4 面试记录 `status.json` 的 `job_id` → 新 id。"""
    changed = 0
    for ref in refs:
        data = store.read_json(ref.path, None)
        if not isinstance(data, dict):
            return 0, ("面试记录 `%s` 在动手前变得读不出来了（预检之后被改坏/被锁）"
                       % ref.path)
        if data.get("job_id") != old:
            continue
        data["job_id"] = new
        _write_interview_json(ref.path, data)
        changed += 1
    return changed, ""


def _rewrite_iv_feedback(old, new, refs, key):
    """#6 面试记录 `feedback.json`：把**以旧 id 为键**的条目改指新 id。

    两种形态都处理：
      * 新键还没有 → 整条搬过去（保留 `applied` / `at`），并把条目里那份 `jd_id` 副本也改掉；
      * 新键已经有了（同一场面试里两种身份各记了一次）→ **保留新的那条**，但 `applied`
        只要有一边为真就必须为真 —— 否则"已反哺过"这条信息会静默丢失，
        下一次重试就会把同一次面试**重复计进复习队列**（`kb.record_interview` 会把 asked_count +1）。
    """
    changed = 0
    for ref in refs:
        data = store.read_json(ref.path, None)
        if not isinstance(data, dict):
            return 0, ("面试记录 `%s` 在动手前变得读不出来了（预检之后被改坏/被锁）"
                       % ref.path)
        if old not in data:
            continue
        entry = data.pop(old)
        kept = data.get(new)
        if kept is None:
            data[new] = entry
            kept = entry
        elif isinstance(kept, dict) and isinstance(entry, dict) and entry.get("applied"):
            kept["applied"] = True
        if isinstance(kept, dict):
            kept["jd_id"] = new               # 条目内层那份副本也要跟过去
        _write_interview_json(ref.path, data)
        changed += 1
    return changed, ""


# ---------------------------------------------------------------- 登记表（**单一真源**）
# 新增一个"指向岗位"的产物：**只在这里加一行**（`GuardTest` 会告诉你漏了哪一个）。
KINDS = {
    "jds": Kind(
        products=("jds.json",),
        how="`jds.json` 的 `jds[].id`（**本体**：改名时改 id 本身，旧值进 `id_history`）",
        detect=_detect_jds, rewrite=_rewrite_jds, preflight=_preflight_jds,
        note="本体。"),

    "jobs": Kind(
        products=("jobs.json",),
        how="`jobs.json` 的 `jd_id`（口径 `jd_id or id`：迁移前的记录身份就是 `id`）",
        detect=_detect_jobs, rewrite=_rewrite_jobs, preflight=_preflight_jobs,
        note="每条分析挂在哪个 JD 下。"),

    "kb": Kind(
        products=("knowledge.json",),
        how="`knowledge.json` 的 `nodes[].from_jds[].id`（去空白比；count = 命中卡片数）",
        detect=_detect_kb, rewrite=_rewrite_kb, preflight=_preflight_kb,
        note="只改引用条目，**卡片本身保留**（知识是通用的）。"),

    "iv_status": Kind(
        products=("interviews/status.json",),
        how="`interviews/<面试id>/status.json` 的 `job_id`（逐字比）",
        detect=_detect_iv_status, rewrite=_rewrite_iv_status,
        preflight=_preflight_interview("status.json"),
        note="只改 `job_id`，面试记录本身保留。"),

    "report_state": Kind(
        products=("report_state",),
        how="`report_state/<报告id>.json`（报告 id 来自该 JD 各条分析的 `report` 字段）",
        detect=_detect_report_state,
        # 它的"引用"是**文件名**（报告 id），而报告文件不改名 ⇒ 改名/合并时无需重写。
        # 删除时由 `jddelete` 连带清理（第 2 步 + 批 7.2），所以这里**登记 + 只读探测**，
        # 不是"漏了"：`refs_of_jd` 现在会把它们如实列进预演。
        rewrite=None, preflight=None,
        note="**策略上无需重写**：它按报告 id（文件名）定位，而报告文件跨改名/合并都不改名。"),

    "iv_feedback": Kind(
        products=("interviews/feedback.json",),
        how="`interviews/<面试id>/feedback.json` 里以 jd id 为键的条目（含条目内层的 `jd_id` 副本）",
        detect=_detect_iv_feedback, rewrite=_rewrite_iv_feedback,
        preflight=_preflight_interview("feedback.json"),
        note="反哺幂等标记：改指新 id，绝不把它丢了（丢了会重复计进复习队列）。"),

    "merged_into": Kind(
        products=("jobs.json",),
        how="`jobs.json` 的 `merged_into`（**悬空引用**：指向被并入的那个岗位）",
        detect=_detect_merged_into, rewrite=None, preflight=None, deferred=True,
        note="**未处理**：悬空引用的修法属**批 7.2**（独立一步 + 独立测试）；本步只登记 + 只读探测。"),
}


# ---------------------------------------------------------------- 只读扫描
def refs_of_jd(jd_id, key=None):
    """全仓扫描「这份 JD 被谁引用」（**纯读**，一个字节都不落盘）。

    返回 `[Ref, ...]`，顺序 = `KINDS` 的登记顺序（输出稳定、可复现）：
      * 命中的 kind 各自一条（逐文件的 kind 一个文件一条）；
      * **读不出来的那一处也一定在列表里**（`err` 非空、`count = -1`）—— 这是硬约束 1，
    调用方要么自己看 `err`，要么用 `refs_of_jd_or_raise` 走"抛错"那条路。
      * 读不出来的文件**一个都不会被跳过**：宁可多说一句，绝不"看起来只有 0 条引用"。

    ⚠️ 空 id 直接 `ValueError`：返回空列表会被读成"没有引用"，而空 id 的真相是"不知道在问谁"。
    """
    want = _text(jd_id)
    if not want:
        raise ValueError("JD id 不能为空（空 id 不猜：返回空列表会被读成「没有引用」）")
    scan = _Scan(key)
    out = []
    for name in KINDS:
        out.extend(KINDS[name].detect(want, scan))
    return out


def unreadable_refs(refs):
    """`refs` 里**读不出来**的那些（`err` 非空）。空列表 = 这次扫描的每一条都可信。"""
    return [r for r in refs if r.err]


def refs_of_jd_or_raise(jd_id, key=None):
    """`refs_of_jd` 的**严格版**：任一处读不出来就抛 `JdRefsUnreadable`。

    给"少列比报错贵得多"的调用方用（删除预演就是这种）：它宁可这次失败，
    也绝不拿着"看起来正常"的短清单往下走。
    """
    refs = refs_of_jd(jd_id, key)
    bad = unreadable_refs(refs)
    if bad:
        raise JdRefsUnreadable(_refused_message(_text(jd_id), bad))
    return refs


def _refused_message(jd_id, bad):
    lines = ["有 %d 处引用点读不出来：**拒绝**按一份读不全的清单动 `%s`（一个字节都没写）。"
             % (len(bad), jd_id or "（空 id）")]
    for ref in bad:
        lines.append("  - [%s] %s" % (ref.kind, ref.err))
    lines.append("「读不出来」= **不知道它指向谁** —— 照改会留下「jds.json 指向新 id、"
                 "这些引用还指着旧 id」的两种身份，而且全程静默。"
                 "请先把它们修好（或从别处恢复、把它们移开）；"
                 "0 字节 / 根本不存在的文件**不算**读不出来。")
    return "\n".join(lines)


# ---------------------------------------------------------------- 逐项重写
def _ordered_kinds(refs):
    """`refs` 里出现过的 kind，按 `KINDS` 的登记顺序（输出稳定）。"""
    return [name for name in KINDS if any(r.kind == name for r in refs)]


def _rollback(before):
    """把改动过的文件**逐字节**还原。返回没能还原的文件名（空 = 全部还原成功）。

    一律走 `read_bytes` / `write_bytes`：文本模式在 Windows 上会把 `\\n` 写成 `\\r\\n`，
    那样"逐字节还原"就是假的（本项目在 `jddelete` 上踩过同一个坑）。
    没还原成的**如实带回**：宁可让调用方看到"回滚没完成，请手工检查"，也绝不假称已还原。
    """
    failed = []
    for path, raw in reversed(before):
        try:
            if raw is None:
                if os.path.exists(path):
                    os.remove(path)
            else:
                store.write_bytes(path, raw)
        except Exception:                     # noqa: BLE001 —— 回滚失败必须如实带回，不能中断整轮
            failed.append(os.path.basename(path))
    return failed


def rewrite_jd_refs(old_id, new_id, refs=None, key=None):
    """把指向 `old_id` 的引用逐项改指 `new_id`。返回 `[(kind, ok, err), ...]`（按登记顺序）。

    **`refs`**（可选）是**显式指定改哪几种**：给了就只动这些 kind（合并/删除各只要一部分）；
    ⚠️ 漏一种 = 那一处**不会被改**（本模块绝不自作主张补齐）—— 想全改就别传（自己重扫一遍）。
    传进来的 refs 若来自**另一个时刻**，那一份是否还新鲜由调用方负责（本模块只按它指定的
    kind 去改，并在改之前**重新**过一遍 `*_safe_to_write`）。

    **整次成功或整次拒绝**（硬约束 2）：
      * 任一处 `err` 非空、或预检（`*_safe_to_write` / 面试文件读不出来）不过 ⇒ 抛
        `JdRefsRefused`，**一个字节都不写**（不是"跳过坏的那一项"——那会留下两种身份）；
      * 落盘中途失败 ⇒ 用原像**逐字节**回滚后抛 `JdRefsWriteFailed`（回滚也没成的话文案里
        会点名哪一份没还原）。

    `ok` 的含义（**没有"半成功"这回事**，所以返回值只可能是"全 True"或抛错）：
      * `ok=True` + `err=""` —— 这一项该改的改了；
      * `ok=True` + 这一项**按策略无需重写**（如 `report_state`：它按报告 id 定位，报告不改名）
        —— 理由在 `KINDS[kind].note` 与模块文档里；
      * `ok=False` + `err=理由` —— 这一项本步**刻意不处理**（`merged_into`，批 7.2）。
    改了几条走 `jdrefs.rewrite_done` 日志（`changed`），调用方也能重扫一遍 `refs_of_jd` 自己核。
    """
    old = _text(old_id)
    new = _text(new_id)
    if not old or not new:
        raise ValueError("旧 id 与新 id 都要给（空 id 不猜）")
    if old == new:
        return []                             # 幂等：本来就一样 ⇒ 一个文件都不动
    if refs is None:
        refs = refs_of_jd(old, key)
    bad = unreadable_refs(refs)
    if bad:
        log.log_event("jdrefs.rewrite_refused", level="warn", old=old, new=new,
                      kinds=[r.kind for r in bad], reason="unreadable")
        raise JdRefsRefused(_refused_message(old, bad))

    kinds = _ordered_kinds(refs)
    if not kinds:
        return []                             # 没有任何引用命中 ⇒ 一个字都不写
    writable = [k for k in kinds if KINDS[k].rewrite is not None]

    # ---- 预检：写盘前每一项都过既有的 `*_safe_to_write` 口径（坏文件 ⇒ 整次拒绝）----
    blocked = []
    for kind in writable:
        ok, err = KINDS[kind].preflight(key)
        if not ok:
            blocked.append((kind, err))
    if blocked:
        log.log_event("jdrefs.rewrite_refused", level="warn", old=old, new=new,
                      kinds=[k for k, _ in blocked],
                      reason="；".join(e for _, e in blocked))
        lines = ["有 %d 处引用点写不得（读不出来 / 结构不对）：**整次拒绝**改写 `%s`，"
                 "一个字节都没写。" % (len(blocked), old)]
        for kind, err in blocked:
            lines.append("  - [%s] %s" % (kind, err))
        lines.append("**不是跳过坏的那一项**：那会留下「jds.json 指向新 id、别的引用还指着旧 id」"
                     "的两种身份（用户看不到任何提示）。请先把它修好（或从别处恢复、把它移开）再改。")
        raise JdRefsRefused("\n".join(lines))

    # ---- 原像 + 落盘（失败逐字节回滚）----
    before = [(p, store.read_bytes(p)) for p in [r.path for r in refs
                                                 if not r.err and r.kind in writable]]
    changed_by = []
    try:
        for kind in writable:
            changed, err = KINDS[kind].rewrite(old, new, [r for r in refs if r.kind == kind], key)
            if err:
                raise JdRefsWriteFailed("改写 `%s` 失败：%s" % (kind, err))
            changed_by.append((kind, changed))
    except BaseException as e:
        failed = _rollback(before)
        log.log_exc("jdrefs.rewrite_failed", e, old=old, new=new,
                    kinds=writable, rolled_back=not failed, not_restored=failed)
        if isinstance(e, JdRefsWriteFailed):
            raise
        how = ("已用原像逐字节回滚" if not failed
               else ("**回滚未完成**！没能还原：%s（请手工检查这几份文件）" % "、".join(failed)))
        raise JdRefsWriteFailed("改写引用失败：%s（%s）" % (e, how))

    log.log_event("jdrefs.rewrite_done", old=old, new=new,
                  kinds=[k for k, _ in changed_by], changed=sum(c for _k, c in changed_by))
    out = []
    for kind in kinds:
        if kind in writable:
            out.append((kind, True, ""))
        else:
            out.append((kind, not KINDS[kind].deferred, KINDS[kind].note))
    return out


# ---------------------------------------------------------------- 守卫：新增产物不许漏
# 扫哪些文件：**显式名单**（不是目录通配）—— 目录通配会把别人的临时脚本也扫进来，
# 而这个守卫要的是"我们自己的代码里，每个产物都有着落"。新增模块要显式加进来。
# `log.LOG_FILE` 除外（日志不是产物，且它只追加）；`web/*` 除外（前端构造不出盘上产物）。
GUARD_FILES = ("app.py", "store.py", "kb.py", "interview.py", "jdid.py", "jddelete.py",
               "jobdup.py", "jdfacts.py", "kbreuse.py", "knowledge.py", "outcome.py",
               "resumelib.py", "迁移脚手架.py", "身份迁移.py", "简历迁移.py",
               "报告体检.py", "岗位去重.py", "打包分享.py", "jdrefs.py", "log.py")

# 「profile 根目录」这个特殊产物名（`p_path()` / `profile_dir()` 无参时只拿到根）。
_PROFILE_ROOT = "(profile 根)"

# 路径构造器：属性名 → 产物。`p_path` 特殊（看第一个位置参数的字面量）。
_PRODUCT_BY_ATTR = {
    "jobs_path": "jobs.json",
    "knowledge_path": "knowledge.json",
    "config_path": "config.json",
    "report_state_dir": "report_state",
    "report_state_path": "report_state",
    "interviews_dir": "interviews",
    "interview_dir": "interviews",
    "_jds_path": "jds.json",
}
# 第二个位置参数是**文件名**的那几个（`interviews/<面试id>/<文件名>`）。
_INTERVIEW_NAME_ARGS = ("interview_file", "load_interview_json", "save_interview_json")

# **显式白名单**：扫得到的产物里，**与岗位身份无关**、或者**策略上不重写**的那些。
# 每一条都必须写明理由 —— 白名单不是"免死金牌"，它是"我们想过这件事"的记录。
WHITELIST = {
    _PROFILE_ROOT:
        "profile 根目录本身不是产物（`p_path()` / `profile_dir()` 无参时只拿到根，用来算下面的"
        "路径）；真正的产物各自单独登记或走白名单。",
    "config.json":
        "设置（模型 / 搜索来源 / 搜索额度），内容里**没有** jd id —— 与岗位身份无关。",
    "interviews":
        "面试目录本身（每个子目录按**面试 id** 命名）；带 jd id 的那两份文件单独登记为"
        "`interviews/status.json` 与 `interviews/feedback.json`。",
    "interviews/<动态文件名>":
        "文件名是调用方给的动态值（录音 / 视频 / 上传文件，`store.save_stream`），内容与 jd id "
        "无关；**写死文件名**的那些（status/feedback/transcript/analysis/source）会各自作为"
        "独立产物被抓到。",
    "interviews/analysis.json":
        "一场面试的 7 段复盘（按面试 id 定位），内容里没有 jd id —— 反哺记的是 `feedback.json`"
        "（已登记为 `iv_feedback`）。",
    "interviews/source.txt":
        "面试文字稿原文（按面试 id 定位），与 jd id 无关。",
    "interviews/transcript.json":
        "面试转写（按面试 id 定位），与 jd id 无关。",
    "jd":
        "`jd/<id>.txt` 是 JD **原文**，文件名里带 id —— 但**策略上不重写**（设计 §6）："
        "报告/原文按 id 命名，改名由 `身份迁移` 重建 `jd_text_file` 的指向。"
        "这里显式白名单 + 理由，不是漏了。",
    "logs":
        "`logs/events.jsonl` 是**只追加**的审计流水；历史事件里的 jd 字段是"
        "「当时发生了什么」，改写历史才是丢证据。",
    "reports":
        "`reports/*.html` 内嵌 JD 数据，但**策略上不重写**（设计 §6）：文件名是"
        "「日期_岗位_分数」，改名会让台账里的 `report` 指向 404 —— 改名/删除/合并都只动引用、"
        "不动报告文件。",
    "resumes":
        "简历文件按**简历** id 命名，与岗位身份无关。",
}


def _literal(node):
    """字符串字面量；非字面量（变量 / f-string / 拼接）→ `None`。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _call_attr(node):
    """一个调用节点的"函数名"：`store.p_path(...)` → `p_path`；`p_path(...)` → `p_path`。"""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _is_profile_root_call(node):
    """`store.profile_dir()` / `store.p_path()`（无参）= "profile 根"这个产物。"""
    attr = _call_attr(node)
    if attr == "profile_dir":
        return True
    return attr == "p_path" and not node.args


def _products_in_tree(tree):
    """一棵 AST 里出现的全部「profile 内产物」（**静态**：只看路径构造器的字面量参数）。

    ⚠️ 它的能力边界要如实说：只有"用 `store` 的路径构造器（或 `os.path.join(profile_dir(), "x")`）
    拼出来的**字面量**产物"才看得见。拼成动态名的（`"jd/" + x`）看不见 —— 但那种写法在
    本项目里不存在，且守卫是"报警器"不是"证明器"：它抓的是"新增一个产物却忘了登记"。
    """
    out = set()
    for node in ast.walk(tree):
        attr = _call_attr(node)
        if attr is None:
            continue
        if attr == "profile_dir":
            out.add(_PROFILE_ROOT)
            continue
        if attr == "p_path":
            out.add(_literal(node.args[0]) if node.args else _PROFILE_ROOT)
            continue
        if attr in _PRODUCT_BY_ATTR:
            out.add(_PRODUCT_BY_ATTR[attr])
            continue
        if attr in _INTERVIEW_NAME_ARGS:
            got = _literal(node.args[1]) if len(node.args) > 1 else None
            out.add("interviews/%s" % (got or "<动态文件名>"))
            continue
        if attr == "join" and len(node.args) > 1 and _is_profile_root_call(node.args[0]):
            got = _literal(node.args[1])                # os.path.join(store.profile_dir(), "x")
            if got:
                out.add(got)
    return set(x for x in out if x)                     # 丢掉空串（动态参数解析不出来时）


def scanned_products():
    """扫 `GUARD_FILES`，返回 `{产物: [出现在哪些文件里, ...]}`（**只读** AST，不执行代码）。"""
    found = {}
    for name in GUARD_FILES:
        path = os.path.join(store.ROOT, name)
        with open(path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=name)
        for product in _products_in_tree(tree):
            found.setdefault(product, []).append(name)
    return found


def unregistered_writers():
    """**守卫用**：扫出来的产物里，既没在 `KINDS` 里登记、也没进 `WHITELIST` 的那些（有序）。

    空列表 = 每个"构造 profile 内路径"的产物都有着落 ⇒ `tests/test_jdrefs.py::GuardTest` 绿。
    非空 = **红**（并且要用 `guard_advice()` 把那句"要么登记、要么加白名单并写理由"打出来）。
    """
    registered = set()
    for kind in KINDS.values():
        registered |= set(kind.products)
    return sorted(p for p in scanned_products() if p not in registered and p not in WHITELIST)


def guard_advice(missing):
    """给改代码的人看的修法说明（报错里直接打这一句，别只丢一个产物名）。"""
    where = scanned_products()
    lines = ["有 %d 个「构造 profile 内路径」的产物既没在 `jdrefs.KINDS` 里登记、"
             "也不在 `jdrefs.WHITELIST` 里：" % len(missing)]
    for product in missing:
        lines.append("  - %r（出现在 %s）" % (product, "、".join(where.get(product, ["?"]))))
    lines.append("**要么登记、要么加白名单并写明理由**：")
    lines.append("  ① 内容里会带 jd id ⇒ 在 `jdrefs.KINDS` 里加一个 kind"
                 "（`detect` 只读探测 + `rewrite` 逐项重写 + `how` 一句话），"
                 "并补上它的 `*_safe_to_write` 预检；")
    lines.append("  ② 与岗位身份无关、或**策略上不重写** ⇒ 加进 `jdrefs.WHITELIST`，"
                 "并写一句能说服下一个改代码的人的理由。")
    lines.append("**不许**悄悄漏过去：「指向岗位的引用点」漏一处，删除/改名/合并之后"
                 "它就会变成盘上孤儿（本项目今天已经漏过两处）。")
    return "\n".join(lines)
