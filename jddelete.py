# -*- coding: utf-8 -*-
"""阶段 D：**真删除一个 JD**（`jds.json` 记录 + 它的分析 + 报告文件 + JD 原文）。

用户的决定（原话选项）是「**连报告与 JD 原文一起真删**」——所以本模块的语义
**就是不可恢复**。工具能做、也必须做的三件事：

1. **先列清单**（`preview_purge`，**严格只读**）：逐条列出将要删掉的东西 ——
   该 JD 的每一条分析（id + 简历名 + 分数）、每一个报告文件、JD 原文（与截图）、
   将被清理的引用（知识库 `from_jds` 命中几条、面试记录几条）。
   **算不出来就说算不出来**（`advisories`），**绝不猜、绝不少列** ——
   少列 = 用户不知道自己正在删什么。
2. **先备份，再删**（`purge_jd`）：动手之前把要动的文件**逐字节**拷进
   `data/profiles/删除备份_<时间戳>/`，**核对 sha256** 之后才开始删；
   并把备份路径**回给用户**（界面必须如实说：功能语义不可恢复，但盘上留了一份备份）。
3. **失败回滚**：任何一步炸了，就用备份把文件**逐字节**恢复（含已经被删掉的那些），
   再写回三份 JSON —— **绝不半删**。

**为什么身份有两套口径**（这是本模块最容易写错的一处）：
`jobs.json` 的记录在迁移**之后**才有 `jd_id`（= 稳定 JD id）；迁移**之前**它的身份就是
`id`（= 报告文件名），而知识库 `from_jds` 指的正是这个值。用户真实数据现在就是**迁移前**
的形态（`jds.json` 不存在、6 条旧形状分析、知识库 97 条引用指向报告 id）。
所以一条记录「属于哪个 JD」的判据是 `jd_id or id`（与 `app._jd_id_of_row` 同一口径）——
只认 `jd_id` 的话，迁移前的数据会「一条分析都列不出来」，而 `preview_purge` 会
**开开心心地把 JD 原文删掉、把分析留在盘上**（正是"少列"最坏的形态）。

**铁律**：
  1. 只 import `jdid` / `kb` / `log` / `store`，**绝不 import `app`**（app 依赖本模块；
     反过来 import 会成环，也会把服务依赖拖进脚本）。
  2. 锁顺序恒为 `jdid._JD_LOCK` → `kb._KB_LOCK`（与 `jdid` / `身份迁移.py` 同一约定，
     反向会死锁）。
  3. **不删别人的东西**：只删「属于这个 JD 的分析」的报告、只删该 JD 的原文/截图；
     还有别的分析在引用的报告文件**留在盘上**并如实说明（`skipped_files`）。
  4. 备份路径是**数据根下的兄弟目录**（`data/profiles/删除备份_<ts>/`），
     与 `身份迁移.py` 的 `迁移备份_<ts>` 并列，绝不落在用户手里 80 个文件的 `data/` 里。
  5. 备份**只增不减**：回滚用它，成功之后也留着（用户确认无误后自行删除）。
"""
import glob
import hashlib
import json
import os
import shutil
from datetime import datetime

import jdid
import kb
import log
import store

# 备份目录名前缀（`data/profiles/删除备份_<时间戳>/`）。**唯一一份定义**：
# 命名与「回滚从哪读」两处都用它，绝不会写歪成两个说法。
BACKUP_PREFIX = "删除备份"

# 报告/原文文件在备份里的相对目录（与 profile 目录下的真实目录同名）
DIR_REPORTS = "reports"
DIR_JD = "jd"
# 事务会**整份重写**的 JSON / 面试记录在备份里的相对目录（见 `_copy_state_into_backup`）
DIR_STATE = "_state"

# 测试注入点（生产恒为 0）：`purge_jd` 每**成功删掉一个文件**就 +1 计数，
# 到 `_FAIL_AFTER` 时抛错 —— 用来在**事务中途**制造失败，验证「失败 → 逐字节回滚（含被删的文件）」。
# 0 = 不注入。生产路径永远看不到它被置非零。
_FAIL_AFTER = 0


class JdPurgeError(Exception):
    """删除被拒绝 / 失败（失败时**已经回滚**）。`str(e)` 就是给用户看的那句话。

    `code` 是**机器可读**的那一份，供路由决定 HTTP 状态码：`not_found`（404）、
    `stale`（409：用户确认的清单已经变了）、`backup_failed`（500：**一个字节都没动**）、
    `failed`（500：已回滚）、`rollback_failed`（500：回滚也失败，要人工介入）。
    **不靠 `str(e)` 里有没有某个词**判状态码 —— 文案改一个字就悄悄错一个状态码。

    `backup`（只有 `failed` / `rollback_failed` 才挂）：这次删除用的备份目录。
    `rollback_failed` 时它是**唯一的自救入口**，路由要把它原样回给前端 ——
    前端据 `code` 分开说话：`backup_failed` 是"一个字节都没动"，
    `rollback_failed` 是"数据可能不是原样，请手工从 `backup` 恢复"。**两者不能混成一句话。**
    """

    def __init__(self, message, code="failed"):
        super().__init__(message)
        self.code = code


def _text(value):
    """非空字符串才算内容（用户可手改 JSON，5 / [] / None 一律当没有）。"""
    return value.strip() if isinstance(value, str) else ""


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _jd_id_of_record(rec):
    """一条分析记录的 JD 身份：`jd_id` 优先，迁移前退回 `id`（与 `app._jd_id_of_row` 同口径）。"""
    if not isinstance(rec, dict):
        return ""
    return _text(rec.get("jd_id")) or _text(rec.get("id"))


def _jobs_or_error():
    """`(记录列表, 读不出来的原因)`。**原因非空 = 这份台账不可信，绝不能当成"空"**。

    为什么必须自己读、不能直接用 `store.load_jobs()`：那个函数在**任何**读/解析失败时都
    返回 `[]`（`store.read_json` 吞掉所有异常）。而删除流程接下来会用
    `store.save_jobs(kept)` 把 `jobs.json` **整份覆盖**成它读到的那个列表 ——
    于是"读不出来"会被落成一份 `[]`：**所有岗位的所有分析记录一次性没了**。
    更糟的是 `jobs.json` 原先不在备份里，回滚也救不回来（不可恢复）。

    触发条件是现实的：用户手改 JSON（本模块处处假设"用户可手改"）、非 UTF-8 保存
    （本项目已经在别的脚本上栽过一次 GBK）、Windows 上杀毒软件短暂锁文件、磁盘/IO 错误。

    "文件根本不存在"与"文件读不出来"是**两件相反的事**：前者空台账是对的，
    后者必须让整次删除**停下来**。
    """
    path = store.jobs_path()
    if not os.path.exists(path):
        return [], ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return [], "台账文件 `jobs.json` 读不出来：%s" % e
    if not isinstance(data, list):
        return [], ("台账文件 `jobs.json` 不是一份列表（是 %s）—— 它坏了，"
                    "而删除会把它整份覆盖掉" % type(data).__name__)
    return [r for r in data if isinstance(r, dict)], ""


def _readable_jobs_or_raise():
    """删除**真正落盘**之前用：读不出来就抛 `unreadable`，宁可拒绝也绝不覆盖成空。"""
    rows, err = _jobs_or_error()
    if err:
        raise JdPurgeError("%s。**拒绝删除**：继续下去会把整份台账覆盖成空，"
                           "所有岗位的分析记录都会没掉。请先把 `jobs.json` 修好"
                           "（或从别处恢复）再删。" % err, code="unreadable")
    return rows


def _digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _fingerprint_of(plan):
    """执行确认用的指纹：把"将要删什么"压成一个短串。

    预演与执行之间数据变了（多了/少了一条分析、换了一份报告）→ 指纹变 →
    `purge_jd` 拒绝执行。用户确认的是**那一份**清单，不是另一份。

    ⚠️ `plan` 既可能是 `_plan` 的内部形状，也可能是摊平后的 `preview_purge` 形状：
    前者是 `"analyses"`/`"files"`/`"kb_cards"`/`"interviews"`，后者把引用摆成
    `refs.knowledge.cards`。两种都认 —— 它们只是同一份数据的两种投影。
    """
    def pick(*names):
        for n in names:
            v = plan.get(n) if isinstance(plan, dict) else None
            if v:
                return v
        return []

    refs = (plan.get("refs") or {}) if isinstance(plan, dict) else {}
    cards = pick("kb_cards") or ((refs.get("knowledge") or {}).get("cards") or [])
    interviews = pick("interviews") or ((refs.get("interviews") or {}).get("records") or [])
    parts = ["jd=" + str(plan.get("id") or "")]
    parts += ["a=" + str(a.get("id") or "") + ":" + str(a.get("report") or "")
              for a in pick("analyses", "_analyses")]
    parts += ["f=" + str(f.get("where") or "") + "/" + str(f.get("name") or "")
              for f in pick("files", "_files")]
    parts += ["k=" + str(c.get("id") or "") + ":" + str(c.get("hits") or 0) for c in cards]
    parts += ["i=" + str(r.get("id") or "") for r in interviews]
    return _digest("\n".join(parts))


# ---------------------------------------------------------------- 只读：读现状
def _analyses_of(jd_id, rows=None):
    """**属于这个 JD 的全部**分析记录（含 `merged_into` 的标记记录），按时间升序。

    为什么要把已合并的也列出来：它们也是这个 JD 下的记录（台账不显示，但删除后
    `jobs.json` 里会留下指向一个不存在的 JD 的孤儿记录）—— 「少列」在这里同样致命。
    """
    if rows is None:
        rows, _err = _jobs_or_error()
    rows = [j for j in rows if _jd_id_of_record(j) == jd_id]
    return sorted(rows, key=lambda r: (str(r.get("created_at") or ""), str(r.get("id") or "")))


def _report_name(rec):
    """一条分析报告的文件名（走 `safe_name`，挡越级路径）。没有 → 空串。"""
    return store.safe_name(rec.get("report")) if isinstance(rec, dict) else ""


def _referenced_by_survivors(jd_id, rows=None):
    """**不属于这个 JD** 的分析记录所引用的报告文件名集合（= 留着的东西还要用的那份）。"""
    if rows is None:
        rows, _err = _jobs_or_error()
    out = set()
    for j in rows:
        if _jd_id_of_record(j) == jd_id:
            continue
        name = _report_name(j)
        if name:
            out.add(name)
    return out


def _names_referenced_by_others(jd_id, key):
    """**别的 JD** 显式点名的 `jd_text_file` / `jd_image` 文件名集合。

    报告一直有 `_referenced_by_survivors` 这道闸门（别的 JD 还在用就不删），
    但 `jd/` 原文与截图**原先没有** —— 两个 JD 的记录点同一个 `jd_text_file` 时，
    删 A 会把 B 还在用的原文删掉（"不删别人的东西"是文件头铁律 3）。
    判据与报告**刻意一致**：还有别的 JD 在引用它 → 留着并如实说明。

    两个来源都要看：迁移后的 JD 记录（`jds.json`）与迁移前的分析记录（`jobs.json`）。
    **只看显式点名的字段**：不把 `<id>.txt` 约定算进来，否则"约定名"会互相保护，
    反而让本该删掉的原文留下来。
    """
    out = set()
    data = jdid.load_jds()
    for rec in ((data.get("jds") if isinstance(data, dict) else None) or []):
        if not isinstance(rec, dict) or _jd_id_of_record(rec) == jd_id:
            continue
        name = store.safe_name(rec.get(key))
        if name:
            out.add(name)
    rows, _err = _jobs_or_error()
    for rec in rows:
        if _jd_id_of_record(rec) == jd_id:
            continue
        name = store.safe_name(rec.get(key))
        if name:
            out.add(name)
    return out


def _kb_hits(jd_id):
    """知识库里 `from_jds[].id == jd_id` 的卡片：**只读**（绝不顺手 `ensure_kb` 改库）。"""
    data = store.load_knowledge()
    out = []
    for node in (data.get("nodes") if isinstance(data, dict) else []) or []:
        if not isinstance(node, dict):
            continue
        hits = [x for x in (node.get("from_jds") or [])
                if isinstance(x, dict) and _text(x.get("id")) == jd_id]
        if hits:
            out.append({"id": str(node.get("id") or ""), "term": str(node.get("term") or ""),
                        "hits": len(hits),
                        "other_sources": len([x for x in (node.get("from_jds") or [])
                                              if isinstance(x, dict)
                                              and _text(x.get("id")) != jd_id])})
    return out


def _interview_hits(jd_id):
    """面试记录里 `job_id == jd_id` 的文件：**只读**。返回 `[{"id","path","title"}]`。"""
    out = []
    for path in sorted(glob.glob(store.interview_file("*", "status.json"))):
        st = store.read_json(path, None)
        if isinstance(st, dict) and _text(st.get("job_id")) == jd_id:
            out.append({"id": os.path.basename(os.path.dirname(path)),
                        "path": path,
                        "title": str(st.get("title") or st.get("job_title") or "")})
    return out


def _file_entry(where, name, kind, note=""):
    path = os.path.join(store.p_path(where), name)
    return {"where": where, "name": name, "kind": kind, "note": note,
            "exists": os.path.isfile(path)}


def _plan(jd_id):
    """**只读**地算出一份完整删除计划（内部形状；对外由 `preview_purge` 摊平）。

    `unreadable` 非空 = `jobs.json` 在盘上却读不出来（**不能当成"这个 JD 没有分析"**）。
    这时 `found=True` 但 `can_purge=False`：用户要看得到"读不出来"这件事本身，
    而不是一句误导性的"找不到这个岗位"。
    """
    jd_id = _text(jd_id)
    jd = jdid.find_jd(jd_id) if jd_id else None
    rows, unreadable = _jobs_or_error()
    analyses = _analyses_of(jd_id, rows) if jd_id else []
    if jd is None and not analyses:
        if unreadable:
            return {"id": jd_id, "jd": None, "found": True, "unreadable": unreadable,
                    "analyses": [], "files": [], "kb_cards": [], "interviews": [],
                    "advisories": [{"kind": "unreadable_jobs", "message": unreadable}]}
        return {"id": jd_id, "jd": None, "found": False, "analyses": [], "files": [],
                "kb_cards": [], "interviews": []}

    files, seen, advisories = [], set(), []
    if unreadable:
        advisories.append({"kind": "unreadable_jobs", "message": unreadable})
    # ① 每一条分析的报告文件（**逐条**列，哪怕两条分析引同一份也只列一次并说明）
    survivors = _referenced_by_survivors(jd_id, rows)
    for rec in analyses:
        name = _report_name(rec)
        if not name:
            advisories.append({"kind": "no_report_name", "message":
                               "分析「%s」的记录里没有报告文件名 —— 没有文件要删（记录照删）"
                               % (rec.get("id") or "")})
            continue
        if (DIR_REPORTS, name) in seen:
            advisories.append({"kind": "shared_report", "message":
                               "报告「%s」被这个 JD 下的多条分析共同引用，只删一次" % name})
            continue
        seen.add((DIR_REPORTS, name))
        entry = _file_entry(DIR_REPORTS, name, "analysis_report")
        if name in survivors:
            entry["kept"] = True
            entry["why"] = "还有**别的 JD** 的分析在引用这份报告：留着它，绝不删别人的东西"
            advisories.append({"kind": "kept_shared_report", "message":
                               "报告「%s」还有别的 JD 的分析在用 —— **保留不删**（否则那条分析的"
                               "报告链接会 404）" % name})
        elif not entry["exists"]:
            advisories.append({"kind": "missing_report", "message":
                               "报告文件「%s」**不在盘上**（可能已被手工删掉）—— 预演里照旧列出它，"
                               "不是静默跳过" % name})
        files.append(entry)
    # ② JD 原文 + 原截图。**三个来源，缺一不可**：
    #    * `jds.json` 的 JD 记录（迁移**之后**的形状：JD 级字段搬到了那里）；
    #    * **分析记录自己**（迁移**之前**的形状：`jd_text_file` / `jd_image` 还在 `jobs.json`
    #      的那条记录上）；
    #    * `jd/<JD id>.txt` 这条**约定**（字段为空时原文仍然按它命名，与 `app._jd_text_path`
    #      同一口径 —— 那个函数就有这个兜底，本模块原先没有，于是两边对"这个 JD 的原文叫
    #      什么"有两种答案）。
    #    只认前两个来源的话，字段为空（`app.py` 写演示数据时就是 `""`）的老记录会
    #    **只列报告、不列原文** —— 那正是"少列"：用户以为删干净了，`jd/` 下的原文还躺在盘上
    #    （本项目对"少列"的代价说得很清楚：用户不知道自己删了什么）。
    for key, kind, label in (("jd_text_file", "jd_text", "JD 原文"),
                             ("jd_image", "jd_image", "原截图")):
        # 约定名只对 JD 原文成立（截图没有 `<id>.jpg` 这条约定）。
        fallback = (jd_id + ".txt") if key == "jd_text_file" else ""
        others = _names_referenced_by_others(jd_id, key)
        for name in _jd_file_names_of(jd, analyses, key, fallback=fallback):
            if (DIR_JD, name) in seen:
                continue
            seen.add((DIR_JD, name))
            entry = _file_entry(DIR_JD, name, kind, note=label)
            if name in others:
                entry["kept"] = True
                entry["why"] = "还有**别的 JD** 的记录点名了这份文件：留着它，绝不删别人的东西"
                advisories.append({"kind": "kept_shared_jd_file", "message":
                                   "%s「%s」还有别的 JD 的记录在用 —— **保留不删**"
                                   % (label, name)})
            elif not entry["exists"]:
                advisories.append({"kind": "missing_jd_file", "message":
                                   "%s文件「%s」**不在盘上** —— 预演里照旧列出它，不是静默跳过"
                                   % (label, name)})
            files.append(entry)
    return {"id": jd_id, "jd": jd, "found": True, "unreadable": unreadable,
            "analyses": analyses, "files": files,
            "kb_cards": _kb_hits(jd_id), "interviews": _interview_hits(jd_id),
            "advisories": advisories}


def _jd_file_names_of(jd, analyses, key, fallback=""):
    """该 JD 的 `jd_text_file` / `jd_image` 文件名（去重、保持出现顺序）。

    JD 记录里有就用它的；**同时还收**分析记录上的同名键（迁移前的老记录把 JD 级字段留在
    `jobs.json` 上）；最后再加上 `fallback`（`jd/<JD id>.txt` 这条约定名）。
    只要有一个来源点到，这个文件就**必须**出现在预演清单里 —— 少列比多列危险得多。
    """
    out, seen = [], set()
    names = [store.safe_name((src or {}).get(key)) for src in [jd] + list(analyses)]
    names.append(store.safe_name(fallback))
    for name in names:
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def preview_purge(jd_id):
    """**只读**预演：逐条列出删这个 JD 会动到的记录、文件与引用。一个字节都不落盘。

    返回给前端 / 路由的形状（键名即契约）：

    ```
    { "ok": true, "id": "<JD id>", "found": true, "jd_exists": true,
      "can_purge": true, "unreadable": "", "fingerprint": "<12 位>", "not_found": "",
      "jd_record": {...} | null,
      "analyses": [{"id","resume","score","report","created_at","merged"}],
      "files":    [{"name","where","kind","exists","kept","note","why"}],
      "refs": {"knowledge": {"hits": 2, "cards": [{id,term,hits,other_sources}],
                             "cards_kept": 2, "note": "只清引用，卡片保留"},
               "interviews": {"hits": 1, "records": [{id,title}]}},
      "summary": {"analyses": 2, "files": 3, "reports": 2, "kb_hits": 2, "interviews": 1},
      "advisories": [{"kind","message"}],
      "not_found": "..."}          # 找不到时非空（给用户看的那句话）
    ```

    **`can_purge=false` 只有一个原因**：`unreadable` 非空 —— `jobs.json` 在盘上却读不出来。
    这时**必须拒绝删除**：删除会用 `save_jobs()` 把整份台账覆盖成它读到的那个列表，
    而"读不出来"被当成空就会把**所有岗位的所有分析记录**一次抹掉（且不可回滚）。
    前端见到 `can_purge !== true` 就要停手，别拿这份"看起来只有 0 条分析"的清单往下走。

    **算不出来的就说算不出来**：读不出报告名、文件不在盘上、报告被别的 JD 共用，
    都进 `advisories`，绝不静默吞掉。
    """
    plan = _plan(jd_id)
    jd_id = plan["id"]
    if not plan["found"]:
        reason = ("JD 库里没有这个 ID，也没有挂在它下面的分析：%s"
                  % (jd_id or "（空 id）"))
        return {"ok": True, "id": jd_id, "found": False, "jd_exists": False,
                "can_purge": False, "unreadable": "", "fingerprint": "",
                "not_found": reason, "reason": reason,
                "jd_record": None, "analyses": [], "files": [],
                "refs": {"knowledge": {"hits": 0, "cards": [], "cards_kept": 0, "note": ""},
                         "interviews": {"hits": 0, "records": []}},
                "summary": {"analyses": 0, "files": 0, "reports": 0, "kb_hits": 0,
                            "interviews": 0},
                "advisories": []}
    advisories = list(plan["advisories"])
    if plan["jd"] is None:
        advisories.append({"kind": "no_jd_record", "message":
                           "这个 ID **没有独立的 JD 记录**（`jds.json` 里找不到）："
                           "它落在「迁移前」的数据形态上，身份就是分析记录自己的 id。"
                           "下面列的是**按这个身份能找到的全部东西**。"})
    analyses = [{"id": str(r.get("id") or ""),
                 "resume": str(r.get("resume") or ""),
                 "score": r.get("score"),
                 "report": _report_name(r),
                 "created_at": str(r.get("created_at") or ""),
                 "merged": bool(_text(r.get("merged_into")))}
                for r in plan["analyses"]]
    files = [{"name": f["name"], "where": f["where"], "kind": f["kind"],
              "exists": f["exists"], "kept": bool(f.get("kept")),
              "note": f.get("note") or "", "why": f.get("why") or ""}
             for f in plan["files"]]
    kb_cards = plan["kb_cards"]
    interviews = plan["interviews"]
    unreadable = str(plan.get("unreadable") or "")
    return {
        "ok": True, "id": jd_id, "found": True, "jd_exists": plan["jd"] is not None,
        # 台账读不出来 → **明确不许删**。前端必须看这个字段（`can_purge !== true` 就停手），
        # 否则它会照着这份"看起来只有 0 条分析"的清单走下去，把整份台账覆盖成空。
        "can_purge": not unreadable, "unreadable": unreadable,
        "reason": "", "not_found": "",
        "fingerprint": _fingerprint_of(plan),
        "jd_record": plan["jd"],
        "analyses": analyses,
        "files": files,
        "refs": {"knowledge": {
                     "hits": sum(c["hits"] for c in kb_cards),
                     "cards": kb_cards, "cards_kept": len(kb_cards),
                     "note": "只清 `from_jds` 里指向它的条目，**卡片本身保留**（知识是通用的）"},
                 "interviews": {"hits": len(interviews), "records": interviews,
                                "note": "只把 `job_id` 清空，**面试记录本身保留**"}},
        "summary": {"analyses": len(analyses),
                    "files": len(files),
                    "reports": len([f for f in files if f["kind"] == "analysis_report"]),
                    "kb_hits": sum(c["hits"] for c in kb_cards),
                    "interviews": len(interviews)},
        "advisories": advisories,
        # ---- 内部键（带下划线）：`purge_jd` 的指纹与删除都只读它们，绝不读摊平后的那份 ----
        "_analyses": plan["analyses"],
        "_files": plan["files"],
        "_kb_cards": kb_cards,
        "_interviews": interviews,
    }


# ---------------------------------------------------------------- 备份
def _backup_dir():
    """本次删除的备份目录（带时间戳；同一秒内重名就补微秒）。"""
    root = store.profiles_root()
    os.makedirs(root, exist_ok=True)
    dest = os.path.join(root, BACKUP_PREFIX + "_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
    if os.path.exists(dest):
        dest = os.path.join(root, BACKUP_PREFIX + "_"
                            + datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
    return dest


def _copy_into_backup(entry, dest):
    """把一个待删文件拷进备份（保留相对目录），返回 (相对路径, 原 mtime, sha256) 或 None。

    备份的文件**一个字节都不改**（`copy2` 连 mtime 一起带过去），
    回滚时用这份副本来恢复 —— 所以"备份成功了"这件事必须**核对 sha256**，不靠"cp 没报错"。
    """
    src = os.path.join(store.p_path(entry["where"]), entry["name"])
    rel = os.path.join(entry["where"], entry["name"])
    out = os.path.join(dest, rel)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    shutil.copy2(src, out)
    with open(src, "rb") as f:
        got = hashlib.sha256(f.read()).hexdigest()
    if got != entry["sha"]:
        raise JdPurgeError("备份出的文件与盘上的不一致（sha256 不符）：%s" % rel)
    return rel, os.stat(src).st_mtime, got


def _copy_state_into_backup(dest, snapshots, jd_id):
    """把这次事务会**改写的 JSON / 面试记录**也拷进备份，并写一份 `manifest.json`。

    为什么必须连它们一起备份（原先只备了报告与 JD 原文）：
      1. 事务会**整份重写** `jobs.json` / `jds.json` / `knowledge.json`。备份里没有它们，
         一旦进程被 kill（`except` 根本没机会跑，回滚无从谈起），用户手里就没有任何
         "原样"的副本 —— 而"先备份再删"的前提是**备份得够用**。
      2. `rollback_failed` 的错误文案让用户"手工从备份恢复"，可备份里当时**没有**这三份
         文件：那句话是**做不到的**。
      3. 一份 manifest（相对路径 + sha256 + 删的是哪个 JD）：手工恢复时不必猜哪个文件是哪来的。

    `snapshots` = `[(绝对路径, (留底前存在吗, 原始字节 | None))]`（见 `_snapshot`）；
    只备份**存在且读得出来**的那些 —— 读不出来的不能在备份里凭空造一份假的。
    """
    entries = []
    base = store.profile_dir()
    for path, snap in snapshots:
        existed, raw = snap if isinstance(snap, tuple) else (True, snap)
        if not existed or raw is None:
            continue
        try:
            inner = os.path.relpath(path, base)
        except ValueError:                     # 不同盘符（理论上不会发生）
            inner = os.path.basename(path)
        if inner.startswith(".."):             # 不在 profile 下的，退回文件名，别写到备份外面去
            inner = os.path.basename(path)
        rel = os.path.join(DIR_STATE, inner)
        out = os.path.join(dest, rel)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "wb") as f:
            f.write(raw)
        with open(out, "rb") as f:             # 核对：不靠"写没报错"
            if f.read() != raw:
                raise JdPurgeError("备份出的 %s 与盘上的不一致" % rel)
        entries.append({"path": rel, "sha256": hashlib.sha256(raw).hexdigest()})
    manifest = {"jd_id": jd_id, "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "note": "这次删除动过的 JSON 与面试记录的**原样**副本（手工恢复用；"
                        "报告与 JD 原文在同级的 reports/ 与 jd/ 下）",
                "files": entries}
    with open(os.path.join(dest, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    return entries


# ---------------------------------------------------------------- 删除
def _remove(path):
    """删一个文件。**只删文件，不删目录**（目录留给用户与别的 JD）。"""
    os.remove(path)


def _read_bytes(path):
    """整个文件的**原始字节**；读不到（不存在/不是文件/没权限）→ `None`。

    **为什么不能用 `store.read_text` 来留底**：它走文本模式，Windows 上
    `open(..., "w")` 会把 `\n` 写成 `\r\n`（Python 的换行符自动转换）。
    于是一份 **LF-only** 的文件经"读→写"往返之后 sha 就变了 ——
    `_rollback` 自称的「逐字节还原」对它**不成立**，而回滚正是
    "数据必须与动手之前一模一样"的最后一道保险。所以留底与还原**一律走二进制**。
    """
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def _snapshot(path):
    """留底：`(留底之前它存在吗, 原始字节 | None)`。

    **"读不出来"绝不能和"原来没有"混为一谈**（这是本项目踩过的坑里最贵的一类）：
    回滚对"原来没有"的处理是**把它删掉**，而对"读不出来"必须**碰都不碰** ——
    否则一次读失败（杀毒软件锁文件、权限、IO 错误）就会让回滚把用户的 `jobs.json` 删掉。
    所以这里先问"它在不在"，再去读；读失败时返回 `(True, None)`，回滚据此只报"没把握"，
    绝不动它。
    """
    if not os.path.isfile(path):
        return (False, None)
    try:
        with open(path, "rb") as f:
            return (True, f.read())
    except OSError:
        return (True, None)


def _write_bytes(path, raw):
    """把原始字节写回去（**逐字节**：二进制模式，绝不碰换行符）。"""
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "wb") as f:
        f.write(raw)


def _write_json(path, obj):
    """原子写 JSON，并触发 `_FAIL_AFTER` 注入点（生产恒为 0）。"""
    store.write_json(path, obj)
    _tick()


def _tick():
    """`_FAIL_AFTER` 计数（注入用）。到点抛错 —— 调用方据此回滚。"""
    global _FAIL_AFTER
    if _FAIL_AFTER > 0:
        _FAIL_AFTER -= 1
        if _FAIL_AFTER == 0:
            raise OSError("注入的删除失败（测试用）")


def _strip_references(jd_id):
    """在内存里把知识库与面试记录的引用清掉，返回 `(kb_data, kb_touched, iv_updates)`。

    **只清引用**：知识库卡片保留（只去掉 `from_jds` 里指向它的条目）、
    面试记录保留（只把 `job_id` 清空）—— 这两条都是规格里的硬要求。
    """
    kb_data = store.load_knowledge()
    kb_touched = 0
    for node in (kb_data.get("nodes") if isinstance(kb_data, dict) else []) or []:
        if not isinstance(node, dict):
            continue
        frm = node.get("from_jds")
        if not isinstance(frm, list):
            continue
        kept = [x for x in frm if not (isinstance(x, dict) and _text(x.get("id")) == jd_id)]
        if len(kept) != len(frm):
            node["from_jds"] = kept
            kb_touched += 1
    iv_updates = []
    for hit in _interview_hits(jd_id):
        st = store.read_json(hit["path"], None)
        if isinstance(st, dict):
            st = dict(st)
            st["job_id"] = ""
            iv_updates.append((hit["path"], st))
    return kb_data, kb_touched, iv_updates


def purge_jd(jd_id, expect_fingerprint=""):
    """**真删**：备份 → 删记录 + 删文件 + 清引用；任何一步失败就从备份**逐字节**回滚。

    返回 `{"ok": True, "id", "backup", "removed_files", "skipped_files", "analyses",
             "kb_hits", "interviews"}`；失败抛 `JdPurgeError`（此时**已经回滚**）。

    `expect_fingerprint` 非空时，与当前计划的指纹比对：不一致 → 拒绝执行
    （用户确认的是预演时那份清单）。
    """
    plan = _plan(jd_id)
    jd_id = plan["id"]
    if not plan["found"]:
        raise JdPurgeError("JD 库里没有这个 ID，也没有挂在它下面的分析：%s" % (jd_id or "（空 id）"),
                           code="not_found")
    if expect_fingerprint and expect_fingerprint != _fingerprint_of(plan):
        raise JdPurgeError("清单已经变了（预演之后数据被改过）：拒绝按旧清单删除。"
                           "请重新预演、重新确认。", code="stale")

    # 锁顺序恒为 _JD_LOCK → _KB_LOCK（见文件头铁律 2）。
    with jdid._JD_LOCK:                                       # noqa: SLF001
        # 重新算一次计划：进临界区之前的那份可能已经被别的请求改掉
        plan = _plan(jd_id)
        if not plan["found"]:
            raise JdPurgeError("JD 库里没有这个 ID，也没有挂在它下面的分析：%s" % jd_id,
                               code="not_found")
        if expect_fingerprint and expect_fingerprint != _fingerprint_of(plan):
            raise JdPurgeError("清单已经变了（预演之后数据被改过）：拒绝按旧清单删除。"
                               "请重新预演、重新确认。", code="stale")
        with kb._KB_LOCK:                                     # noqa: SLF001
            return _purge_locked(jd_id, plan)


def _purge_locked(jd_id, plan):
    """临界区内执行：备份 → 删文件 → 删记录 → 清引用（失败统一回滚）。"""
    # ---- 0) 先把"要动的"逐条定下（含 sha256：备份之后核对用）----
    to_delete = []
    skipped = []
    for entry in plan["files"]:
        src = os.path.join(store.p_path(entry["where"]), entry["name"])
        if entry.get("kept"):
            skipped.append({"name": entry["name"], "where": entry["where"],
                            "why": entry.get("why") or "还有别的 JD 的分析在引用它"})
            continue
        if not os.path.isfile(src):
            skipped.append({"name": entry["name"], "where": entry["where"],
                            "why": "文件不在盘上（可能已被手工删掉）—— 记录照删，文件没什么可删"})
            continue
        with open(src, "rb") as f:
            entry = dict(entry, sha=hashlib.sha256(f.read()).hexdigest())
        to_delete.append(entry)

    jd_path, jobs_path = jdid._jds_path(), store.jobs_path()   # noqa: SLF001
    kb_path = store.knowledge_path()
    # 留底一律用 `_snapshot`（**原始字节** + "原来在不在"）：文本模式在 Windows 上会把
    # LF 写成 CRLF，那样"逐字节回滚"就是假的；而"读不出来"又必须与"原来没有"分开
    # （后者回滚要删掉它，前者绝不能碰）—— 两个坑都在 `_snapshot` 的注释里。
    originals = {p: _snapshot(p) for p in (jd_path, jobs_path, kb_path)}
    # **读不出来就拒绝执行**：后面那步 `save_jobs(kept_jobs)` 会把整份台账覆盖掉，
    # 而 `store.load_jobs()` 把"读不出来"报成 `[]` —— 那会把所有岗位的所有分析一次抹掉，
    # 且 `jobs.json` 原先不在备份里，回滚也救不回来（见 `_readable_jobs_or_raise`）。
    jobs_before = _readable_jobs_or_raise()
    target_ids = set(str(r.get("id") or "") for r in plan["analyses"])   # 只用于回报与日志
    iv_before = {}
    for hit in plan["interviews"]:
        iv_before[hit["path"]] = _snapshot(hit["path"])

    # ---- 1) 备份（**动手之前**；逐字节 + 核对 sha256）----
    backup = _backup_dir()
    try:
        os.makedirs(backup, exist_ok=True)
        for entry in to_delete:
            _copy_into_backup(entry, backup)
        # 事务还会**整份重写**这三份 JSON 与面试记录 —— 它们也必须进备份，
        # 否则进程被 kill 时用户手里没有任何"原样"副本，`rollback_failed` 那句
        # "请手工从备份恢复"也根本做不到（见 `_copy_state_into_backup`）。
        _copy_state_into_backup(backup, list(originals.items()) + list(iv_before.items()), jd_id)
    except BaseException as e:
        # 备份这一步失败 = **一个字节都还没动**。把半截备份删掉，绝不留误导性的"备份"。
        shutil.rmtree(backup, ignore_errors=True)
        log.log_exc("jd.purge_backup_failed", e, jd=jd_id)
        if isinstance(e, JdPurgeError):
            raise
        raise JdPurgeError("备份失败，**没有删除任何东西**：%s" % e, code="backup_failed")
    # ---- 2) 事务：删文件 → 删记录 → 清引用 ----
    mtimes = {}
    for entry in to_delete:
        src = os.path.join(store.p_path(entry["where"]), entry["name"])
        mtimes[os.path.join(entry["where"], entry["name"])] = os.stat(src).st_mtime
    removed = []
    try:
        # 2a) 文件（顺序固定：报告在前，JD 原文/截图在后 —— 注入点的步数才有确定含义）
        for entry in to_delete:
            _remove(os.path.join(store.p_path(entry["where"]), entry["name"]))
            removed.append(entry)
            _tick()
        # 2b) `jds.json` 里该 JD 的记录
        data = jdid.load_jds()
        data["jds"] = [r for r in data["jds"] if r.get("id") != jd_id]
        jdid.save_jds(data)
        _tick()
        # 2c) `jobs.json` 里它的分析（**逐条**，只删属于这个 JD 的）。
        # 判据与 `_analyses_of` **同一口径**（`jd_id or id`），**不是**"id 落在一个集合里"：
        # 一个集合判据会有一个要命的边界 —— 本 JD 只要有一条**没有 `id`** 的记录，
        # `target_ids` 里就混进一个空串，于是"id 不在集合里"会把**所有**没有 id 的记录
        # 一起删掉（别的 JD 的、以及根本不属于任何 JD 的）。身份是"这是谁的记录"，
        # 就该用身份来判，别用"id 有没有出现在某个集合里"来代理它。
        kept_jobs = [j for j in jobs_before if _jd_id_of_record(j) != jd_id]
        store.save_jobs(kept_jobs)
        _tick()
        # 2d) 引用：知识库只清 `from_jds` 条目、面试记录只清 `job_id`
        kb_data, kb_touched, iv_updates = _strip_references(jd_id)
        if kb_touched:
            store.save_knowledge(kb_data)
        _tick()
        for path, st in iv_updates:
            store.write_json(path, st)
            _tick()
    except BaseException as e:
        rolled = _rollback(backup, originals, mtimes, to_delete, iv_before)
        log.log_exc("jd.purge_failed", e, jd=jd_id, rolled_back=rolled,
                    backup=os.path.basename(backup))
        failed = JdPurgeError("删除失败：%s（%s）"
                              % (e, "已从备份逐字节回滚" if rolled else
                                 "**回滚也失败了**，请手工从 %s 恢复" % backup),
                              code="failed" if rolled else "rollback_failed")
        # 路由把 `backup` 回给前端：`rollback_failed` 时这句话就是**唯一的自救入口**，
        # 用户必须能看见、能复制 —— 所以它不能只埋在没结构化的文案里。
        failed.backup = backup
        raise failed

    try:
        log.log_event("jd.purged", jd=jd_id, backup=os.path.basename(backup),
                      files=len(removed), skipped=len(skipped),
                      analyses=len(target_ids), kb_nodes=kb_touched,
                      interviews=len(iv_updates))
    except Exception:
        pass                        # 日志写不进去不该让一次已经成功的删除算失败
    return {"ok": True, "id": jd_id, "backup": backup,
            "backup_note": ("功能语义是**不可恢复**：台账记录、分析记录、报告文件与 JD 原文"
                            "都已经从工作目录里删除。工具在磁盘上另留了一份安全备份"
                            "（路径见 backup）—— 那不是「盘上真没了」，只是可回滚的余地。"),
            "removed_files": [{"name": e["name"], "where": e["where"], "kind": e["kind"]}
                              for e in removed],
            "skipped_files": skipped,
            "analyses": sorted(target_ids),
            "kb_hits": len([c for c in plan["kb_cards"]]),
            "kb_nodes": kb_touched,
            "interviews": len(iv_updates)}


def _rollback(backup, originals, mtimes, to_delete, iv_before):
    """从备份把文件**逐字节**放回去，再写回三份 JSON 与面试记录。返回是否成功。

    这是"绝不半删"的落点：文件已经被 `os.remove` 掉了，只能从备份恢复 ——
    所以**备份的内容必须与盘上原样一致**（`_copy_into_backup` 核对过 sha256）。
    mtime 也一起还原：它是「分析用的简历内容比文件新/旧」这类判断的依据。

    JSON 与面试记录一律用 `_write_bytes`（**二进制**）写回，不用 `store.write_text`：
    文本模式会把 LF 改成 CRLF，那样"逐字节"就是假的（见 `_read_bytes`）。

    **"原来没有"才删，"读不出来"碰都不碰**：留底时读失败的文件（`raw is None` 但
    `existed` 为真）我们**没有**可信的原样内容 —— 这时删掉它等于"用一次读失败换掉用户
    的文件"，而把手上这份写进去又是在冒充还原成功。所以两个都不做：只记 `ok=False`，
    让路由回 `rollback_failed`，由人来处理。
    """
    ok = True
    for entry in to_delete:
        rel = os.path.join(entry["where"], entry["name"])
        src, dst = os.path.join(backup, rel), os.path.join(store.p_path(entry["where"]),
                                                           entry["name"])
        try:
            if os.path.isfile(src):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
                if rel in mtimes:
                    os.utime(dst, (mtimes[rel], mtimes[rel]))
            else:
                ok = False
        except Exception:
            ok = False
    for path, snap in list(originals.items()) + list(iv_before.items()):
        existed, raw = snap if isinstance(snap, tuple) else (True, snap)
        try:
            if not existed:
                if os.path.exists(path):
                    os.remove(path)
            elif raw is None:
                ok = False                  # 留底就读不出来 → 不敢动，也不算还原成功
            else:
                _write_bytes(path, raw)
        except Exception:
            ok = False
    return ok
