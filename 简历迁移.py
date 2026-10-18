# -*- coding: utf-8 -*-
"""简历迁移（维护脚本）：把**盘上已有的简历文件**登记成版本库（`resumes.json`）的第一版，
并把老分析指向它。

为什么需要它：阶段 4C 之前，简历只有"一个会被截断覆盖的文件"，没有任何版本记录
（`resumes.json` 不存在），于是报告里"这份分析用的是哪一版简历"无从查起。
这份脚本把**已经存在的那份简历**补登成 `rv_0001`，并给已经跑完的分析补上 `resume_id`。
**文件一个都不动**（不移动、不改名、不删除）。

用法：
  python 简历迁移.py --试跑     # **严格只读**：打印对照表，一个字节都不写
  python 简历迁移.py --执行     # 先整目录备份，再迁移（失败 → 从备份恢复）

----------------------------------------------------------------- 推定依据（规格 §7）
**有证据，不是猜**。用户真实数据的实测形状：

    简历文件 resumes/周汉钦-母版.md  2455 字节  改于 2026-09-18 23:06:24
    6 次分析的创建时间：2026-09-19 14:29 / 14:50 / 15:06 / 21:18 / 2026-10-03 00:30 / 2026-10-08 16:40
    （`jobs.json` 6 条记录，其中 1 条 `merged_into` 死记录；`resumes.json` 当时不存在）

**文件改于所有分析之前**，而"保存简历会更新文件时间" → 这 6 次分析读到的就是当前这份内容。
所以：登记为 `rv_0001`（`at` 取**文件 mtime**、`chosen=true`、`rev=1`、`root_id`=自己），
并给这 6 条补 `resume_id`。

注意：**这是"推定"，不是记录里写着的**（旧分析本来就没有 `resume_id` 这个字段）：
脚本把它印在对照表里、写进日志，界面也要照规格 §8.5 标出来。**绝不假装是当时的记录。**
（也正因为是推定：一旦盘上那份文件的内容与已有版本记录**对不上**（程序外改过），
"这几次分析用的是当前这份内容"这条依据就跟着失效 → 本脚本**什么都不写**，如实报告。）

安全保证：
  - `--试跑` 严格只读：不写库、不建 `resumes.json`、不记日志、不取备份、不动简历文件
  - `--执行` **有改动时**第一步是 `data/profiles/<key>/` 的整目录备份（带时间戳，**保留不删**；
    拷备份时跳过旧备份目录与 `.bak-` 留档，备份里不再套备份）；
    任一步失败 → 用备份恢复整目录，并把迁移新建的文件删掉（备份里没有的东西一律不留下）。
    **算完计划确认一个字节都不会变时不建备份、也不写任何文件**：备份是回滚的**前提**，
    没有改动就无从回滚；而整目录备份会把 `interviews/` 下的原始录音（单文件上限 500MB）
    一起拷一遍 —— 无事可做的一轮再拷贝一次，只会让磁盘只增不减（审计 §2 P0-12）
  - **简历文件不移动、不改名、不删除**；报告 / `jd/` 原文 / `knowledge.json` / 面试记录**都不碰**
  - 幂等：已有版本记录按 `name`（文件名去扩展名）认领，**不重复建、不撞号**；
    已经有 `resume_id` 的分析**绝不覆盖**；第二次执行三个文件字节级不变

只 import 本任务用到的模块（`resumelib` / `store` / `log`），**绝不 import `app.py`**
（那是服务，脚本不能拖起 Web 依赖）。
"""
import functools
import hashlib
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 备份 / 恢复四件套的**唯一一份实现**（批 5 §7.2）：`简历迁移.py` 与 `身份迁移.py` 共用，
# 不再各抄一遍（抄漏一处就是体检 §2 P0-14）。
import 迁移脚手架 as 脚手架  # noqa: E402

import log        # noqa: E402
import resumelib  # noqa: E402
import store      # noqa: E402

# 备份目录名前缀（`data/profiles/简历迁移备份_<时间戳>/`）。**唯一一份定义**：
# 备份命名与「拷备份时跳过旧备份」两处都用它（与 `身份迁移.py` 的 BACKUP_PREFIX 同一做法，
# 但**不能共用**：那次备份的是 JD 身份，这次备份的是简历版本，用户要能分开看）。
BACKUP_PREFIX = "简历迁移备份"

# 简历文件的扩展名（与 `app._RESUME_EXTS` 同一口径：只收文本）。
RESUME_EXTS = (".md", ".txt")

USAGE = """用法：
  python 简历迁移.py --试跑     # 只打印对照表（严格只读，一个字节都不写）
  python 简历迁移.py --执行     # 有改动时先整目录备份，再迁移（失败 → 从备份恢复；无改动则不备份）
"""

# 测试注入点（生产恒为空）：`--执行` 每写一个文件后把它的相对名 append 进来；
# 测试往里面放一个「打开即抛错」的对象，就能在**事务中途**制造写盘失败，
# 从而验证「失败 → 从备份恢复」。生产路径永远不 append，等于零开销。
_FAIL_AFTER_WRITE = []

# 版本记录的键序（与 `resumelib.create_version` 落盘的形状**逐字**一致：
# 同一个文件被两条代码路径写下不同的键序，人打开看会觉得"是不是被人改过"）。
_VERSION_KEYS = ("id", "name", "root_id", "parent_id", "rev", "at", "size", "sha256", "file",
                 "mother_file", "chosen", "ai_diff", "ai_diff_error", "note", resumelib.REMOVED)


def _name_of_file(filename):
    """文件名去扩展名（与 `resumelib` 记录里的 `name`、`app` 的 `unregistered` 同一口径）"""
    return os.path.splitext(store.safe_name(filename))[0]


def _resume_key(value):
    """分析记录里的 `resume` 值 → 与 `name` 可比的口径（多写了个 `.md` 也认）"""
    return os.path.splitext(store.safe_name(value))[0]


def _resume_files():
    """盘上真实的简历文件 `[(文件名, 绝对路径)]`。

    **不含** `resumes/快照/` 下的母版快照（那是版本库自己的东西，不是"用户的一份简历"），
    也不递归：`resumes/` 这一层的文件就是简历（旧约定，打包分享等照旧）。
    """
    d = store.p_path("resumes")
    if not os.path.isdir(d):
        return []
    out = []
    for fn in sorted(os.listdir(d)):
        p = os.path.join(d, fn)
        if os.path.isfile(p) and fn.lower().endswith(RESUME_EXTS):
            out.append((fn, p))
    return out


def _mtime_text(path):
    """文件 mtime 的**秒级**原文（对照表里给人看的证据）"""
    return datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d %H:%M:%S")


def _at_of(path):
    """版本记录的 `at`：**取文件 mtime**（与 `resumelib._AT_FMT` 同一格式）"""
    return datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d %H:%M")


def _sha_file(path):
    """磁盘**字节**的 sha256（只用于对照：与文本口径不一致时如实提示，不当指纹存库）"""
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _doc_state():
    """读 `resumes.json` 的**原始**形状：`(version, 记录列表, 文件是否存在)`。

    为什么不走 `resumelib.load_resumes()`：那个会把「没有 id / 名字」的记录**过滤掉**，
    我们照它写回去就等于**静默删掉**用户手改坏的那几条。这里原样取、原样写回。

    「**有内容但解析不出来**」→ 直接中止（同 `身份迁移._build_knowledge` 那条铁律）：
    拿一份空库顶掉用户手改坏的文件，就是**静默丢数据**。
    ⚠️ "有没有内容"必须问 `store.file_has_bytes_to_lose()`（**看原始字节，且只认 0 字节**）——
    老写法用 `store.read_text(path, "").strip()`，而非 UTF-8 文件解码会失败并被兜成空串，
    于是"有内容但读不出来"被误判成"空文件"，迁移照跑、把用户记录顶掉。
    中途换成 `content_bytes_present()` 也只修了一半：它按 `strip()` 判"有没有可见内容"，
    于是一个只有 `b"\r\n   \r\n"` 的版本库仍被判成"空" ⇒ 迁移照跑、写一份空库上去（P0-6）。
    现在只有"不存在"或"真的是 0 字节"才算没东西可丢。
    """
    path = store.p_path(resumelib.RESUMES_FILE)
    raw = store.read_json(path, None)
    if raw is None and store.file_has_bytes_to_lose(path):
        raise RuntimeError("resumes.json 有内容但解析失败：已放弃本次迁移，"
                           "绝不把它写成空库（先修好或移开这个文件再跑）")
    if not isinstance(raw, dict):
        return 1, [], os.path.exists(path)
    items = raw.get("resumes")
    # 口径：本模块历史上用的是裸 `int()`（即 `bool_ok=True`）。取值实现只剩 store 这一份
    # （结构钉子 `tests/test_store.py::SingleImplementationTest`），所以口径在调用点显式传。
    return (store.as_int(raw.get("version"), 1, bool_ok=True) or 1,
            list(items) if isinstance(items, list) else [], True)


# ---------------------------------------------------------------- 迁移计划（只读）
def plan_migration():
    """算出整轮迁移的全部内容，**一个字节都不落盘**（`--试跑` 与 `--执行` 共用）。"""
    version, raw_items, had_doc = _doc_state()
    known_names = {}                                # name → 已有记录（含已移出、含坏形状）
    for rec in raw_items:
        if not isinstance(rec, dict):
            continue
        name = store.safe_name(rec.get("name"))
        if name and name not in known_names:
            known_names[name] = rec

    items = []
    for fn, path in _resume_files():
        name = _name_of_file(fn)
        text = store.read_text(path, "")
        item = {
            "file_name": fn,
            "rel": "resumes/%s" % fn,
            "name": name,
            "path": path,
            "size": os.path.getsize(path),
            "mtime": _mtime_text(path),
            "at": _at_of(path),
            "sha256": resumelib.sha256_of(text),          # 文本口径：与 read_version 核对的一致
            "file_sha256": _sha_file(path),
            "kind": "new",
            "version": None,
            "conflict": None,
        }
        old = known_names.get(name)
        if old is None:
            items.append(item)
            continue
        expected = str(old.get("sha256") or "")
        if expected and expected != item["sha256"]:
            # 内容与记录不符（程序外改过，规格 §8.2）→ **不新建、不改动**，
            # 并且"这几次分析用的是当前这份内容"这条推定同时失效 → 那几条分析也不补
            item["kind"] = "conflict"
            item["conflict"] = {"expected": expected, "actual": item["sha256"]}
        else:
            item["kind"] = "existing"
            item["version"] = old
        items.append(item)

    doc = {"version": version, "resumes": list(raw_items)}
    new_versions = []
    used = list(raw_items)
    for item in items:
        if item["kind"] != "new":
            continue
        # 版本 id 的格式只有 `resumelib` 一份定义（顺延现有最大号，绝不撞号）
        vid = resumelib._next_id(used)              # noqa: SLF001
        rec = {"id": vid, "name": item["name"], "root_id": vid, "parent_id": None, "rev": 1,
               "at": item["at"], "size": item["size"], "sha256": item["sha256"],
               "file": item["rel"], "mother_file": None,
               # 单版本族谱里，它自己就是这一族的母简历（"选定继续优化"的目标）
               "chosen": True,
               "ai_diff": {}, "ai_diff_error": "", "note": "", resumelib.REMOVED: False}
        rec = {k: rec[k] for k in _VERSION_KEYS}
        used.append(rec)
        item["version"] = rec
        new_versions.append(rec)
        doc["resumes"].append(rec)

    id_by_name = {item["name"]: resumelib._clean_id((item["version"] or {}).get("id"))
                  for item in items if item["kind"] in ("new", "existing")}
    # 已有记录里的 id 可能是手改坏的（`""` / 带路径分隔符）→ **绝不把它当成版本号写进 jobs.json**
    # （那既是一句假话，又会让下一次分析因为"版本不存在"被 400 挡掉）
    id_by_name = {name: vid for name, vid in id_by_name.items() if vid}
    conflict_names = {item["name"] for item in items if item["kind"] == "conflict"}

    jobs = store.load_jobs()
    point, already, skipped = [], [], []
    for rec in jobs:
        if not isinstance(rec, dict):
            continue
        row = {"id": str(rec.get("id") or ""), "resume": rec.get("resume"),
               "resume_id": str(rec.get("resume_id") or ""),
               "created_at": str(rec.get("created_at") or ""),
               "merged_into": str(rec.get("merged_into") or "")}
        if row["resume_id"]:
            # 已经有版本号（新写的分析 / 别的迁移）→ **绝不覆盖**（幂等的前提）
            already.append(row)
            continue
        key = _resume_key(row["resume"])
        if key and key in conflict_names:
            row["why"] = "该简历内容与版本库记录不符（程序外改过）→ 不硬塞"
            skipped.append(row)
        elif key and key in id_by_name:
            row["resume_id"] = id_by_name[key]
            point.append(row)
        else:
            row["why"] = ("resume 为空" if not key
                          else "resume 值与简历文件名对不上（resumes/ 里没有这份文件）")
            skipped.append(row)

    # 引用完整性：`jobs.json` 里新补的 `resume_id` 必须指向**真的写下去**的那条版本记录
    new_id_by_row = {row["id"]: row["resume_id"] for row in point}
    jobs_doc = [(_with_resume_id(rec, new_id_by_row[str(rec.get("id") or "")])
                 if isinstance(rec, dict) and str(rec.get("id") or "") in new_id_by_row else rec)
                for rec in jobs]

    return {
        "profile": store.current_profile(),
        "dir": store.profile_dir(),
        "had_doc": had_doc,
        "version": version,
        "before_records": len([r for r in raw_items if isinstance(r, dict)]),
        "new_versions": new_versions,
        "items": items,
        "jobs_total": len([r for r in jobs if isinstance(r, dict)]),
        "jobs_merged": len([r for r in jobs if isinstance(r, dict)
                            and str(r.get("merged_into") or "").strip()]),
        "point": point,
        "already": already,
        "skipped": skipped,
        "conflicts": [item for item in items if item["kind"] == "conflict"],
        "resumes_doc": doc,
        "jobs_doc": jobs_doc,
        "changed_resumes": bool(new_versions),
        "changed_jobs": bool(point),
    }


def _with_resume_id(rec, version_id):
    """给一条分析补 `resume_id`，**其余键与顺序一字不动**（插在 `resume` 后面）。

    为什么插在 `resume` 后面：`app.ANALYSIS_FIELDS` 里两者就是挨着的，人打开 `jobs.json`
    一眼能对上"这一版用的是哪一份简历"；改键序也会让"其余字段一字未动"的对照变得难做。
    """
    out = {}
    for key, value in rec.items():
        out[key] = value
        if key == "resume" and "resume_id" not in rec:
            out["resume_id"] = version_id
    if "resume_id" not in out:
        out["resume_id"] = version_id
    return out


# ---------------------------------------------------------------- 对照表
def _evidence_rows(plan, item):
    """会被指向这一版的分析（含"早就有版本号、号就是这一版"的那些）—— 用来印推定依据。"""
    name = item["name"]
    vid = (item["version"] or {}).get("id")
    rows = [r for r in plan["point"] if _resume_key(r["resume"]) == name]
    rows += [r for r in plan["already"]
             if _resume_key(r["resume"]) == name and r["resume_id"] == vid]
    return sorted(rows, key=lambda r: (r["created_at"], r["id"]))


def render_table(plan, executing=False):
    """「文件 → 版本」对照表（给人逐行看的）。返回整段文本。

    `executing=True` 时表头/表尾说明**下面就要真动手了**：老实现把"以上只是试跑，
    **未改动任何文件**"印在 `--执行` 的输出里，紧接着就是"迁移完成"—— 连着读很容易被理解成
    "这次什么都没改"（与 `身份迁移.py` 同一处误读）。表的内容一个字不变，只换这两句的口气。
    """
    lines = []
    # 批 5 §7.1：本脚本是**一次性**迁移工具，本机**已经跑过了**（`rv_0001` 已登记、6 条老分析
    # 已经指过去）。再跑一次什么也不会改，只会往 `data/profiles/` 里再堆一份整目录备份 ——
    # 这行"开场提示"就是为了挡住"顺手再跑一次"。
    # 这是**唯一**允许的输出变化（§7.2）。措辞里**不用** `⚠` / `✗` 之类的非 GBK 字符：
    # cp936 控制台 print 它们就是 `UnicodeEncodeError`（本项目为此专门换掉过 `⚠` / `✗`）。
    lines.append("【提醒】本脚本是一次性迁移工具，本机已完成迁移；重复执行只会重复备份，"
                 "除非确有必要请勿再跑。")
    lines.append("简历迁移 · %s" % ("改动预览（下面开始执行：有改动才建备份）" if executing
                                    else "试跑（严格只读，一个字节都不写）"))
    lines.append("profile: %s" % plan["profile"])
    lines.append("目录: %s" % plan["dir"])
    if plan["had_doc"]:
        lines.append("版本库 resumes.json: 已有 %d 条记录" % plan["before_records"])
    else:
        lines.append("版本库 resumes.json: 不存在（将新建）")
    lines.append("简历文件 %d 份 | 分析记录 %d 条（其中已合并 merged_into %d 条）"
                 % (len(plan["items"]), plan["jobs_total"], plan["jobs_merged"]))
    lines.append("")
    lines.append("推定依据（规格 §7 —— 有证据，不是猜）：")
    for item in plan["items"]:
        rows = _evidence_rows(plan, item)
        times = " / ".join(r["created_at"] for r in rows) or "（无）"
        lines.append("  %s 改于 %s" % (item["rel"], item["mtime"]))
        lines.append("  %d 条分析全在文件时间之后（%s）" % (len(rows), times))
    lines.append("  保存简历会更新文件时间 → 文件既然早于所有分析，这几次分析用的就是当前这份内容")
    lines.append("  注意：所以 resume_id 是**推定**：旧分析本来就没有这个字段"
                 "（规格 §8.5，界面要把「推定」标出来）")
    lines.append("")
    lines.append("文件 → 版本（**不移动、不改名、不删除**任何简历文件）")
    for item in plan["items"]:
        if item["kind"] == "new":
            rec = item["version"]
            lines.append("  %s  →  %s" % (item["rel"], rec["id"]))
            lines.append("    name=%s | rev=%d | root_id=%s（自己） | chosen=true | file=%s"
                         % (rec["name"], rec["rev"], rec["root_id"], rec["file"]))
            lines.append("    at=%s（取文件 mtime %s） | size=%d 字节 | sha256=%s"
                         % (rec["at"], item["mtime"], rec["size"], item["sha256"]))
        elif item["kind"] == "existing":
            rec = item["version"]
            lines.append("  %s  →  %s（已登记，沿用）" % (item["rel"], rec.get("id")))
            lines.append("    记录里的 sha256 与磁盘一致（%s）；size=%d 字节"
                         % (item["sha256"], item["size"]))
        else:
            lines.append("  %s  →  **不登记**（注意：内容与记录不符，程序外改过）" % item["rel"])
            lines.append("    记录里的 sha256=%s" % item["conflict"]["expected"])
            lines.append("    磁盘上实测     sha256=%s" % item["conflict"]["actual"])
            lines.append("    → 不新建版本、也不给分析补 resume_id（规格 §8.2：如实提示，不假装一致）")
        if item["file_sha256"] != item["sha256"]:
            lines.append("    注意：该文件的磁盘字节与文本口径不一致（CRLF/编码转换？）："
                         "文件 %s / 文本 %s" % (item["file_sha256"], item["sha256"]))
    lines.append("")
    lines.append("会被指向 rv_0001 的分析（逐条）" if len(plan["items"]) == 1
                 else "会被指向各自版本的分析（逐条）")
    if not plan["point"]:
        lines.append("  （无）匹配的分析都已经有 resume_id")
    for row in plan["point"]:
        lines.append("  %s | resume=%s | created_at=%s" % (row["id"], row["resume"] or "（空）",
                                                          row["created_at"] or "无日期"))
    if plan["point"]:
        lines.append("  —— 共 %d 条（含已合并 merged_into 的死记录 %d 条：它同样是那几次分析"
                     "用的那一版，照样补；台账本就不显示它们）"
                     % (len(plan["point"]), len([r for r in plan["point"] if r["merged_into"]])))
    lines.append("")
    lines.append("不会被处理的分析（不硬塞 resume_id）")
    if not plan["skipped"]:
        lines.append("  （无）")
    for row in plan["skipped"]:
        lines.append("  %s | resume=%s | created_at=%s | 理由: %s"
                     % (row["id"], row["resume"] or "（空）", row["created_at"] or "无日期",
                        row["why"]))
    lines.append("")
    lines.append("已有 resume_id 的分析（保持原样）")
    if not plan["already"]:
        lines.append("  （无）")
    for row in plan["already"]:
        lines.append("  %s | resume=%s | resume_id=%s | created_at=%s"
                     % (row["id"], row["resume"] or "（空）", row["resume_id"],
                        row["created_at"] or "无日期"))
    lines.append("")
    lines.append("本次 --执行 将改动：resumes.json %s | jobs.json %s"
                 % ("是（新增 %d 条版本记录 %s）"
                    % (len(plan["new_versions"]),
                       "、".join(r["id"] for r in plan["new_versions"]))
                    if plan["changed_resumes"] else "否（已登记，不新建）",
                    "是（%d 条分析补 resume_id）" % len(plan["point"])
                    if plan["changed_jobs"] else "否（匹配的分析都已有 resume_id）"))
    lines.append("本脚本只写 resumes.json 与 jobs.json 的 resume_id 这一处字段；"
                 "不碰 knowledge.json / jds.json / 报告 / jd/ / 面试记录。")
    lines.append("简历文件：不移动、不改名、不删除（%d 份 .md/.txt 就地保留）。"
                 % len(plan["items"]))
    lines.append("")
    if executing:
        # 表还没写盘是真的，但它紧接着就要写了（与 `身份迁移.py` 同一处误读的修法）。
        lines.append("以上是**改动预览**（这张表本身还没有写任何文件）；下面开始执行 —— "
                     "有改动才建整目录备份，没有改动会如实告诉你「无需改动、未建备份」。")
    else:
        lines.append("以上只是试跑，**未改动任何文件**（加 --执行 才迁移；有改动时先整目录备份）。")
    return "\n".join(lines)


# ---------------------------------------------------------------- 备份 / 恢复
# 批 5 §7.2：这一段原先在本脚本与 `身份迁移.py` 里**各抄一遍**（`_dir_stats` / `_human_size`
# / `_backup_note` / `_profile_files` / `_ignore_previous_backups` / `_backup_profile` /
# `_restore_profile`）。
# 【订正】此处原先还写着「两边唯一的差别是 `_restore_profile` 收尾那圈多一句
# `if dirpath == root: continue`（本脚本有、`身份迁移.py` 没有）」—— **这是假的**：
# 重构前两份脚本**都已有** `if dirpath == root: continue` 与 `.bak-` 那道闸门，两份
# `_restore_profile` **代码逐行一致**（AST 归一化后只差 docstring / 注释）。体检 §2 P0-14
# 那颗雷（回滚时把 profile 根目录 `rmdir` 掉，用户下一步就是重试，等于「失败路径上再丢一次」）
# 是**更早的一次修复**里拆掉的，不是这次重构补上的差值 —— 换句话说，**两道保护此前双方就都有**。
# 这次重构统一的是**实现**（订正口径与 `迁移脚手架.py` 顶部那段一致，以那份为准）。
# 抄两遍的真正代价不是「代码丑」，而是**下一次改一处、漏一处**；所以现在**唯一一份实现**在
# `迁移脚手架.py`，这里只剩绑定：
# `functools.partial` 把本脚本的备份前缀绑成第一个位置参数，于是调用形状一个字没变
# （`_backup_profile()` / `_restore_profile(backup, had_files)`，以及 `copytree(ignore=...)`
# 按 `(dirpath, names)` 两参调用）—— `execute_migration` 与既有测试都照原名调用。
# 备份前缀仍**各脚本一份**（`简历迁移备份` / `迁移备份`：用户要能分开看哪次备份属于哪件事），
# 但**命名规则**（`<前缀>_<YYYYmmdd_HHMMSS>`、同秒重名兜底）只有脚手架里 `backup_dir_name` 一处。
_backup_note = 脚手架.backup_note
_profile_files = 脚手架.profile_files
_restore_profile = 脚手架.restore_profile
_ignore_previous_backups = functools.partial(脚手架.ignore_previous_backups, BACKUP_PREFIX)
_backup_profile = functools.partial(脚手架.backup_profile, BACKUP_PREFIX)



def _write_json(path, obj):
    """原子写一个 JSON，并触发 `_FAIL_AFTER_WRITE` 注入点（生产恒为空）。"""
    store.write_json(path, obj)
    if _FAIL_AFTER_WRITE:
        probe = _FAIL_AFTER_WRITE[0]
        if probe is not None:
            raise getattr(probe, "boom", OSError("注入的写盘失败"))


# ---------------------------------------------------------------- 执行
def execute_migration():
    """算计划 → **有改动才**备份 → 迁移 → 落盘；失败从备份恢复。返回进程退出码。

    **备份是有条件的**（审计 §2 P0-12）：计划里 `changed_resumes` / `changed_jobs` 都是空时
    → **不建备份目录、一个文件都不写**（备份是回滚的**前提**，没有改动就无从回滚；而整目录
    备份会把 `interviews/` 下的原始录音一起拷一遍）。算出来真的有改动时**照旧无条件备份** ——
    这条安全底线不因为"改动很小"而放松。
    """
    plan = plan_migration()
    print(render_table(plan, executing=True))
    print("")
    changed = bool(plan["changed_resumes"] or plan["changed_jobs"])
    if not changed:
        # 与 `身份迁移.py` 同一口径：无事发生的一轮不建备份、连审计行都不写（真幂等）。
        print("本次无需改动：版本库与分析都已经登记过（本次**未建备份** —— 备份只为回滚改动而建）。")
        print("没有改动任何文件；简历文件没移动、没改名、没删除。")
        return 0
    root = store.profile_dir()
    had_files = _profile_files(root)
    try:
        backup = _backup_profile()
    except BaseException as e:
        sys.stderr.write("错误：备份失败，**没有改动任何文件**：%s\n" % e)
        return 1
    print("已备份整目录：%s" % _backup_note(backup))
    log.LOG_FILE = os.path.join(store.profile_dir(), "logs", "events.jsonl")
    try:
        # 顺序是契约：**先写版本库、再写引用它的 jobs.json**。
        # 中间崩了最坏只留一条没人引用的版本记录（用户删掉即可）；
        # 反过来先写 jobs.json 就会留下"指向一个不存在的版本"的假话。
        if plan["changed_resumes"]:
            with resumelib._LOCK:                  # noqa: SLF001 —— 与 resumelib 的读写持同一把锁
                _write_json(store.p_path(resumelib.RESUMES_FILE), plan["resumes_doc"])
        if plan["changed_jobs"]:
            _write_json(store.jobs_path(), plan["jobs_doc"])
    except BaseException as e:
        import traceback
        restored = False
        try:
            _restore_profile(backup, had_files)
            restored = True
        except BaseException as e2:                # 回滚本身不能再把原始异常吞掉
            sys.stderr.write("错误：恢复也失败了：%s\n" % e2)
        sys.stderr.write("错误：迁移失败：%s\n%s\n" % (e, traceback.format_exc()))
        print("错误：迁移失败：%s（%s）"
              % (e, "已用备份恢复 " + backup if restored else "恢复失败，请手工从 %s 恢复" % backup))
        return 1
    # `changed` 在上面已经算过（无改动的话早就带备份一起返回了），这里只会是有改动的那条路。
    # 无事发生就不写审计行：这样"再跑一次"连 events.jsonl 都一个字节不变（真幂等）。
    try:
        log.log_event("resume.migration_done", profile=plan["profile"],
                      versions=len(plan["new_versions"]), analyses=len(plan["point"]),
                      skipped=len(plan["skipped"]), conflicts=len(plan["conflicts"]),
                      backup=os.path.basename(backup))
    except Exception:
        pass                                       # 日志写不进去不该让一次成功的迁移算失败
    print("")
    print("迁移完成：新增 %d 条版本记录（%s）| %d 条分析补上 resume_id | 跳过 %d 条 | 冲突 %d 条"
          % (len(plan["new_versions"]),
             "、".join(r["id"] for r in plan["new_versions"]) or "无",
             len(plan["point"]), len(plan["skipped"]), len(plan["conflicts"])))
    print("简历文件没移动、没改名、没删除；备份保留在 %s" % _backup_note(backup))
    print("注意：老分析的 resume_id 是**推定**（依据：简历文件 mtime 早于这几次分析），"
          "不是当时的记录。")
    return 0


# ---------------------------------------------------------------- CLI
def main():
    args = [a for a in sys.argv[1:] if a.strip()]
    if args not in (["--试跑"], ["--执行"]):
        sys.stderr.write(USAGE)
        return 2
    try:
        if args == ["--试跑"]:
            print(render_table(plan_migration()))
            return 0
        return execute_migration()
    except RuntimeError as e:
        # 「库读不出来」这类**前置**失败：计划阶段就中止了，一个字节都没写（绝不半改）
        sys.stderr.write("错误：%s\n" % e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
