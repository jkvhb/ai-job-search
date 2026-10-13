# -*- coding: utf-8 -*-
"""身份迁移（维护脚本）：把岗位身份从**报告文件名**迁到**稳定的 JD id**（`jds.json`）。

为什么需要它（根因）：
  以前岗位身份 == 报告文件名（`20261003_语音文本评测_75分`）——文件名里嵌了**日期与分数**，
  于是「换个简历 / 重跑一次分析」必然产生一个新身份；知识库 `from_jds` 越叠越多，
  `multi_jd`（被 N 个岗位提到，复习优先级的**乘性**因子）静默算错。
  阶段 3.6 用模糊指纹做了补救（治标），`jdid` 把身份本身改对（治本）。
  这份脚本负责把**已经存在的**数据搬过去：一份分析挂在哪个 JD 下、报告文件不动。

用法：
  python 身份迁移.py --试跑     # **严格只读**：打印「迁移前 → 迁移后」对照表，一个字节都不写
  python 身份迁移.py --执行     # 先整目录备份，再迁移（失败 → 从备份恢复）

安全保证：
  - `--试跑` 严格只读：不写库、不建 `jds.json`、不记日志、不取备份
    （`find_duplicate_jobs` 与 `kb.read_report_data` 都只读）
  - `--执行` **第一步**就是 `data/profiles/<key>/` 的整目录备份（带时间戳，**保留不删**；
    拷备份时跳过旧备份目录，备份里不再套备份）；
    任一步失败 → 用备份恢复整目录，并把迁移新建的文件删掉（备份里没有的东西一律不留下）
  - **报告文件不改名、不删除**；`jd/<旧 id>.txt` 也不改名（`jd_text_file` 字段跟着指过去）
  - 幂等：JD id 由「指纹分组 + 报告派生字段 + 编号」决定，已建过的 JD 按 `migrated_from`
    （旧分析 id）认领，**不重复建、不重复涨 `seq`、不刷新 `updated_at`**

迁移步骤（顺序即契约）：
  1. 备份 `data/profiles/<key>/` 整目录
  2. 按 `jobdup` 指纹把**活跃分析**归组 → 每组一个 JD（**缺 JD 原文的自成一组**，不猜）
  3. 每组 `jdid.derive_fields` + `jdid.build_id`（编号从现有 `seq` 之后顺延）→ 写 `jds.json`
     * JD 级字段取**原始值**（原记录 / `REPORT_DATA`），ID 六段取**归一化值**
     * 被 `--执行` 合并过的记录（`merged_into`）**不另建 JD**（见下）
  4. `jobs.json` 记录只留分析级 + `jd_id`（JD 级字段**整体搬走**，绝不两处各存一份）
  5. 知识库 `from_jds[].id` → JD id；面试记录 `status.json.job_id` → JD id
  6. 报告文件不改名、不删除
  7. 失败 → 从第 1 步的备份恢复

**已 `merged_into` 的记录：保留原样（本脚本选的策略）**——它们本来就是「已合并」标记、
台账里**根本不显示**（`app._active_jobs` 过滤掉），给它们另建一个 JD 会凭空多出一张
只有已删记录才有的卡片；而且那会破坏幂等（每次跑都多一个 JD）。
所以：`merged_into` 的记录**不加 `jd_id`、不参与建 JD**，只把**指向它的引用**
（知识库 / 面试记录）改指它 `merged_into` 那条所在的 JD —— 引用无悬空，且它的旧 id
继续存在（它只是历史标记，不再当身份用）。代价：旧 id 会留在盘上（不是身份，只是标记），
换来的是「不凭空造 JD、跑几次都一样」。

**铁律**：
  1. 只 import 本任务用到的模块（`jobdup` / `jdid` / `kb` / `store` / `log`），
     **绝不 import `app.py`**（那是服务，脚本不能拖起 Web 依赖）。
  2. 写 `knowledge.json` 必须持 `kb._KB_LOCK`（本项目真实丢过 97 张卡）；
     锁顺序恒为 `jdid._JD_LOCK` → `kb._KB_LOCK`（反向会死锁）。
  3. `knowledge.json` 「有内容但解析失败」→ **立刻中止**，绝不用空库顶掉（同 `jobdup._repoint_kb`）。
  4. 一次性原子写：先在内存里把三份新内容全算出来，再逐文件落盘；
     落盘失败用备份恢复（绝不半改）。
"""
import glob
import json
import os
import shutil
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import jdid    # noqa: E402
import jobdup  # noqa: E402
import kb      # noqa: E402
import log     # noqa: E402
import store   # noqa: E402

# JD 级字段（只存 `jds.json`）——与 `app.JD_FIELDS` 同一口径（脚本不 import app，故各列一份）
JD_FIELDS = ("job_title", "job_category", "location", "salary", "company", "hr_name", "phone",
             "address", "status", "verdict", "notes", "jd_text_file", "jd_image", "source_type",
             "source_link", "outcome_at", "outcome_reason", "outcome_ai")
# 分析级字段（只存 `jobs.json`）——与 `app.ANALYSIS_FIELDS` **逐字**一致（同一口径）
ANALYSIS_FIELDS = ("id", "jd_id", "resume", "resume_id", "score", "report", "created_at", "merged_into")

# 报告 → JD 级字段的取值顺序（报告的数据类型比台账记录更全，先取报告的值）
_REPORT_FIRST = ("job_title", "company", "hr_name", "phone", "address", "job_category",
                 "location", "salary", "education", "verdict", "notes", "source_link",
                 "jd_text_file", "jd_image")
# 台账记录优先补齐的字段（用户可能在台账里手改过；`source_type` 报告里叫 `source`）
_RECORD_FIRST = ("status", "outcome_at", "outcome_reason", "outcome_ai", "source_link",
                 "jd_text_file", "jd_image", "source_type", "job_title", "company", "hr_name",
                 "phone", "address", "job_category", "location", "salary", "verdict", "notes",
                 "education")
_JD_IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp")

USAGE = """用法：
  python 身份迁移.py --试跑     # 只打印「迁移前 → 迁移后」对照表（严格只读）
  python 身份迁移.py --执行     # 先整目录备份，再迁移
"""

# 备份目录名前缀（`data/profiles/迁移备份_<时间戳>/`）。**唯一一份定义**：
# 备份命名与「拷备份时跳过旧备份」两处都用它，绝不会写歪成两个说法。
BACKUP_PREFIX = "迁移备份"

# 测试注入点（生产恒为空）：`--执行` 每写一个文件后把它的相对名 append 进来；
# 测试往里面放一个「打开即抛错」的对象，就能在**事务中途**制造写盘失败，
# 从而验证「失败 → 从备份恢复」。生产路径永远不 append，等于零开销。
_FAIL_AFTER_WRITE = []


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _created_key(rec):
    """排序键：**更早**的排前面（与 `jobdup._created_key` 同一语义：日期解析不出排最后）"""
    jid = str(rec.get("id") or "")
    raw = str(rec.get("created_at") or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return (0, datetime.strptime(raw, fmt), "", jid)
        except ValueError:
            pass
    return (1, datetime.min, raw, jid)


def _merged_into(rec):
    """记录上的 `merged_into`（只认非空字符串；手改出的 5 / [] 一律当没合并）"""
    v = rec.get("merged_into") if isinstance(rec, dict) else None
    return v.strip() if isinstance(v, str) else ""


def _clean_id(text):
    return jdid._clean(text)


def _report_data_of(rec):
    """读该分析报告里的 `REPORT_DATA`（读不出 → {}）。`report` 走 `safe_name`，挡越级路径。"""
    name = store.safe_name(rec.get("report"))
    if not name:
        return {}
    path = os.path.join(store.p_path("reports"), name)
    if not os.path.isfile(path):
        return {}
    try:
        data = kb.read_report_data(path)
    except Exception:                     # 用户可手改的报告文件，读坏一份不能拖垮整轮迁移
        data = None
    return data if isinstance(data, dict) else {}


def _report_text(data, *keys):
    """从 REPORT_DATA 里取第一个非空字符串（`source` 是 `source_type` 的报告侧键名）"""
    for key in keys:
        v = data.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _jd_image_for(jd_text_file):
    """有 `jd/<id>.txt` 就顺手认同名的截图（用户真实数据里是 `.jpg`）"""
    name = store.safe_name(jd_text_file)
    if not name:
        return ""
    base, _ext = os.path.splitext(name)
    for ext in _JD_IMAGE_EXT:
        cand = base + ext
        if os.path.isfile(store.p_path("jd", cand)):
            return cand
    return ""


# ---------------------------------------------------------------- 指纹分组（只读）
def _groups_of(active):
    """按 `jobdup` 指纹给**活跃分析**归组（**只读**）。

    返回 `(groups, no_text, too_short)`：
      - `groups`：`[[记录…], …]`，每组 = 同一份 JD；组内按 `created_at` 升序
        （第一份 = `jobdup` 的 `keep` = 时间更早的那条）
      - `no_text`：**缺 JD 原文**的分析（真实性：无原文无法比对 → **各自自成一组**，不猜）
      - `too_short`：有原文但过不了 `jobdup` 保守门槛的分析（同样**自成一组**）

    为什么自己走一遍两两比较而不直接用 `find_duplicate_jobs()`：那个函数给的是**对**
    （且 `skipped` 只数缺原文的），分组要的是**连通类**；判据本身完全复用
    `jobdup._blocks` / `jobdup.THRESHOLD`，一行判定逻辑都没有另抄。
    """
    with_text, no_text, too_short = [], [], []
    for rec in active:
        text = jobdup.jd_text_of(rec.get("id"))
        if not text.strip():
            no_text.append(rec)
            continue
        blocks = jobdup._blocks(text)          # noqa: SLF001 —— 判定参数只有 jobdup 一份
        (with_text if blocks is not None else too_short).append((rec, blocks))

    groups, used = [], set()
    # 起点按时间升序：分组结果与 jobs.json 的排列无关（顺序是契约的一部分）
    order = sorted(range(len(with_text)), key=lambda i: _created_key(with_text[i][0]))
    for i in order:
        if i in used:
            continue
        rec_i, blocks_i = with_text[i]
        used.add(i)
        group = [rec_i]
        for k in order:
            if k in used:
                continue
            rec_k, blocks_k = with_text[k]
            if jobdup._score(blocks_i, blocks_k) >= jobdup.THRESHOLD:   # noqa: SLF001
                used.add(k)
                group.append(rec_k)
        groups.append(sorted(group, key=_created_key))
    groups.sort(key=lambda g: _created_key(g[0]))
    return groups, no_text, too_short


# ---------------------------------------------------------------- JD 记录（原始值 + 归一化 ID）
def _jd_fields_for(records, report, extra_reports=()):
    """一组的 JD 级字段：**原始值**（报告优先，台账补齐），一个空值都不主动写进去。

    `records` 已按 `created_at` 升序；`report` 是**权威那份**（身份字段从它派生）。
    某一段在权威报告里没有时（老模板的报告可能缺 `job_title`），按 `extra_reports` 的
    顺序在**同组其它分析**的报告里找，最后才退到台账记录 —— 同一份 JD 的几次分析
    在字段上是互补的，只认一份会平白丢信息（真实数据里 JD 1 的标题就是这样）。
    多份分析的原始表述不一致时取**最早那份**，结果与排序无关地可复现。
    """
    out = {}
    for key in _REPORT_FIRST:
        v = _report_text(report, key)
        if not v:
            for other in extra_reports:
                v = _report_text(other, key)
                if v:
                    break
        if v:
            out[key] = v
    app = report.get("application") if isinstance(report.get("application"), dict) else {}
    for key, src_key, source in (("outcome_at", "outcome_at", app),
                                 ("outcome_reason", "outcome_reason", app),
                                 ("outcome_ai", "outcome_ai", app)):
        v = source.get(src_key) if isinstance(source, dict) else None
        if isinstance(v, str) and v.strip():
            out[key] = v.strip()
        elif key == "outcome_ai" and isinstance(v, dict) and v:
            out[key] = v
    if not out.get("source_type"):
        src = _report_text(report, "source", "source_type")
        if src:
            out["source_type"] = src
    out.setdefault("status", "未投递")
    for key in _RECORD_FIRST:
        if key in out and out[key] not in ("", None):
            continue
        for rec in records:
            v = rec.get(key)
            if isinstance(v, str) and v.strip():
                out[key] = v.strip()
                break
            if key == "outcome_ai" and isinstance(v, dict) and v:
                out[key] = v
                break
    return out


def _find_group_jd(existing, record):
    """这一组是不是**上次迁移已经建过**的 JD（幂等认领）。

    三条判据依次试：① `migrated_from` 命中（最可靠：我们自己写下的出处）
    ② 按派生字段算出来的 id 已在库里（用户手改过 id 时靠这条）
    ③ 组内某个旧 id 本身就是库里的 JD id（中途失败后重跑的兜底）。
    """
    ids = record["ids"]
    for rec in existing:
        migrated = rec.get("migrated_from")
        if isinstance(migrated, list) and any(x in migrated for x in ids):
            return rec
    derived = _clean_id(record.get("derived_id"))
    if derived:
        for rec in existing:
            if _clean_id(rec.get("id")) == derived:
                return rec
    for rec in existing:
        if rec.get("id") in ids:
            return rec
    return None


# ---------------------------------------------------------------- 迁移计划（只读）
def plan_migration():
    """算出整轮迁移的全部内容，**一个字节都不落盘**（`--试跑` 与 `--执行` 共用）。

    返回：
      `jobs`        —— 迁移后的 `jobs.json`（分析级 + `jd_id`）
      `jds_doc`     —— 迁移后的 `jds.json`
      `knowledge`   —— 迁移后的知识库（`from_jds` 已改指 JD id）
      `kb_touched`  —— 被改指的知识点条数
      `kb_unreadable` —— 报告读不出、引用**没敢改**的旧 id 列表（如实报告，不静默丢）
      `interviews`  —— `[(路径, 新内容 dict), …]`
      `groups`      —— 每个 JD 的对照信息（旧分析 id / 归组依据 / 报告文件名 / JD 记录）
      `merged`      —— 保留原样的 `merged_into` 记录
      `skipped`     —— `(no_text, too_short)` 两类自成一组的原因计数
    """
    with jdid._JD_LOCK:                        # noqa: SLF001 —— 锁顺序恒为 _JD_LOCK → _KB_LOCK
        jds_data = jdid.load_jds()
        existing = [r for r in jds_data.get("jds", []) if isinstance(r, dict)]
        raw_jobs = store.load_jobs()
        active = [j for j in raw_jobs if isinstance(j, dict) and not _merged_into(j)]
        merged = [j for j in raw_jobs if isinstance(j, dict) and _merged_into(j)]

        records, new_ids, failed = _plan_groups(jds_data, existing, active)

        # 已合并记录的引用：指向它 `merged_into` 那条所在的 JD（无悬空）；没有对应 JD → 不动
        for rec in merged:
            target = _merged_into(rec)
            if new_ids.get(target):
                new_ids[str(rec.get("id") or "")] = new_ids[target]
        # **被并入的那条旧 id 不是身份**：只有「有 JD 挂上去」的旧 id 才算这个 JD 的原文
        identity_ids = {rid for r in records for rid in r["ids"]}

        jds_out = _build_jds(jds_data, records)
        jobs_out = _build_jobs(raw_jobs, new_ids)
        kb_out, kb_touched = _build_knowledge(new_ids, jd_by_id={r["jd_id"]: r for r in records})
        interviews = _build_interviews(new_ids, identity_ids)
        skipped = {"no_text": sum(1 for r in records if r["why"] == "缺 JD 原文"),
                   "too_short": sum(1 for r in records if r["why"].startswith("原文过短"))}
        return {
            "jobs": jobs_out, "jds_doc": jds_out, "knowledge": kb_out["data"],
            "kb_touched": kb_touched, "kb_unreadable": failed, "interviews": interviews,
            "groups": records, "merged": merged, "skipped": skipped,
            "active": len(active), "before_jds": len(existing),
            "before_kb": _from_jds_ids(store.load_knowledge()),
            "seq": max([r["seq"] or 0 for r in records]
                       + [jdid._as_int(jds_data.get("seq"), 0)] + [0]),   # noqa: SLF001
        }


def _plan_groups(jds_data, existing, active):
    """把活跃分析归成 JD 组、算出每组的新 id 与编号（**只读**）。

    编号从现有 `seq` 之后顺延：**已经建过的组按 `migrated_from` 认领原 id/原编号**，
    所以重跑既不新建 JD、也不涨 seq。返回 `(records, {旧分析id: JD id}, 未改指的旧 id)`。
    """
    groups, no_text, too_short = _groups_of(active)
    bundle = [(g, "指纹") for g in groups]
    bundle += [([r], "缺 JD 原文") for r in no_text]           # 缺原文 → 各自一组，不猜
    bundle += [([r], "原文过短（无法指纹判定）") for r in too_short]
    seq = max(0, jdid._as_int(jds_data.get("seq"), 0))         # noqa: SLF001
    records, new_ids = [], {}
    for members, why in bundle:
        record = {
            "canonical": members[0],              # 组内时间更早的那份（jobdup 的 keep）
            "members": members,
            "why": why,
            "ids": [str(r.get("id") or "") for r in members],
            "reports": sorted(os.path.basename(str(r.get("report") or ""))
                              for r in members if str(r.get("report") or "")),
            "report": {},
        }
        for rec in members:                       # 报告读不出就试下一份；都读不出则用台账字段
            record["report"] = _report_data_of(rec)
            if record["report"]:
                break
        # 同组其它分析的报告（字段互补：老模板的报告可能缺 job_title / education）
        record["other_reports"] = [d for d in (_report_data_of(r) for r in members) if d]
        record["fields"] = (jdid.derive_fields(record["report"]) if record["report"]
                            else jdid.derive_fields(record["canonical"]))
        record["derived_id"] = jdid.build_id(record["fields"], 0)
        old = _find_group_jd(existing, record)
        if old is not None:
            record["existing"] = old
            record["jd_id"] = str(old.get("id") or "")
            record["seq"] = _seq_of_jd(old)
        else:
            seq += 1
            record["existing"] = None
            record["seq"] = seq
            record["jd_id"] = jdid.build_id(record["fields"], seq)
        records.append(record)
        for rid in record["ids"]:
            new_ids[rid] = record["jd_id"]
    failed = [rid for rid, jd_id in new_ids.items() if not jd_id]
    return records, new_ids, failed


def _seq_of_jd(jd_rec):
    """已有 JD 记录的编号（`seq` 字段优先，没有就从 id 末段解出来）；解不出 → None"""
    seq = jdid._as_int(jd_rec.get("seq"), 0)                   # noqa: SLF001
    if seq:
        return seq
    try:
        return jdid.parse_id(str(jd_rec.get("id") or ""))[1]
    except ValueError:
        return None


def _build_jds(jds_data, records):
    """拼出整份 `jds.json`：**已存在的 JD 记录原样保留**（用户手改的字段绝不覆盖）。

    注意：被本轮某组**认领**的已有 JD（`migrated_from` 命中）不能再原样追加一份 ——
    否则同一个 id 会出现两次（重跑一次多一份，正是「幂等」要拦住的东西）。
    """
    claimed = set()
    for record in records:
        if isinstance(record.get("existing"), dict) and record["existing"].get("id"):
            claimed.add(record["existing"]["id"])
    out, seq_max = [], 0
    for jds_rec in jds_data.get("jds", []):
        if not isinstance(jds_rec, dict) or not jdid._text(jds_rec.get("id")):   # noqa: SLF001
            continue
        seq_max = max(seq_max, jdid._as_int(jds_rec.get("seq"), 0))          # noqa: SLF001
        if jds_rec.get("id") not in claimed:
            out.append(jds_rec)
    for record in records:
        seq_max = max(seq_max, record["seq"] or 0)
        out.append(_order_jd_record(_merge_jd_record(record)))
    return {"version": jdid._as_int(jds_data.get("version"), 1) or 1,   # noqa: SLF001
            "seq": seq_max, "jds": out}


def _jd_text_file_of(record):
    """这一组的 JD 原文文件名：优先取记录/报告里的值，否则挑一个**真的存在**的 `<id>.txt`。"""
    fields = _jd_fields_for(record["members"], record["report"], record.get("other_reports") or ())
    text_file = store.safe_name(fields.get("jd_text_file")) or ""
    if text_file and os.path.isfile(store.p_path("jd", text_file)):
        return text_file
    identity = [r for r in record["members"] if str(r.get("id") or "")]
    for rec in identity:
        cand = store.safe_name(str(rec.get("id"))) + ".txt"
        if os.path.isfile(store.p_path("jd", cand)):
            return cand
    return text_file


def _merge_jd_record(record):
    """一组 → 一条 JD 记录：**ID 六段取归一化值，JD 级字段取原始值**。

    已存在的 JD（上次迁移建过）**只补不覆盖**：用户手改过的 id / 状态 / 笔记绝不能被重跑冲掉。
    """
    fields = _jd_fields_for(record["members"], record["report"], record.get("other_reports") or ())
    text_file = _jd_text_file_of(record)
    if text_file:
        fields["jd_text_file"] = text_file
    image = store.safe_name(fields.get("jd_image")) or ""
    if not image or not os.path.isfile(store.p_path("jd", image)):
        image = _jd_image_for(text_file)
    if image:
        fields["jd_image"] = image
    canonical = record["canonical"]
    existing = record["existing"]
    created = (str(existing.get("created_at") or "").strip() if isinstance(existing, dict) else "")
    created = created or str(canonical.get("created_at") or "").strip() or _now()
    # ⚠ 这里**绝不**把 `record["fields"]`（ID 的归一化六段）写进 JD 级字段：
    #   `company` / `salary` 等 JD 级字段要的是**原始表述**（`6-8K（一金7k+）` / 公司全名），
    #   归一化值是**身份**（ID）才用的。把两者混在一起会让用户看到被截短的薪资与公司名
    #   —— 那正是规格 §3「ID 六段取归一化值、JD 级字段取原始值」这条要守住的分界。
    if isinstance(existing, dict):
        new_rec = dict(existing)
        for key, value in fields.items():
            if not str(new_rec.get(key) or "").strip():
                new_rec[key] = value
        migrated = [x for x in (new_rec.get("migrated_from")
                                if isinstance(new_rec.get("migrated_from"), list) else [])]
        for rid in record["ids"]:
            if rid not in migrated:
                migrated.append(rid)
        new_rec["migrated_from"] = sorted(migrated)
        new_rec.setdefault("id_history", [])
        new_rec.setdefault("jd_facts", {})
        new_rec["id"] = str(existing.get("id") or record["jd_id"])
        new_rec["seq"] = jdid._as_int(existing.get("seq"), record["seq"] or 0)   # noqa: SLF001
        new_rec["created_at"] = created
        new_rec["updated_at"] = str(existing.get("updated_at") or _now())
        return new_rec
    new_rec = {
        "id": record["jd_id"], "seq": record["seq"], "created_at": created,
        "updated_at": _now(), "id_history": [], "migrated_from": sorted(record["ids"]),
        "jd_facts": {},
    }
    new_rec.update(fields)
    return new_rec


_JD_ORDER_HEAD = ("id", "seq", "education", "company", "duty", "domain", "city", "salary",
                  "created_at", "updated_at")


def _order_jd_record(rec):
    """固定键序输出（字典比较不看顺序，但**字节级幂等**看；顺序定死才可复现）。"""
    ordered = {key: rec[key] for key in _JD_ORDER_HEAD if key in rec}
    for key in JD_FIELDS:
        if key in rec and key not in ordered:
            ordered[key] = rec[key]
    for key in ("id_history", "migrated_from", "jd_facts"):
        if key in rec and key not in ordered:
            ordered[key] = rec[key]
    for key in rec:                            # 未知键（用户手加的）保留，排在最后
        if key not in ordered:
            ordered[key] = rec[key]
    return ordered


def _build_jobs(raw_jobs, new_ids):
    """`jobs.json`：**只留分析级字段 + `jd_id`**（JD 级字段整体搬走）。

    已 `merged_into` 的记录**原样保留**（含它的 JD 级字段）：它是历史标记、台账不显示它，
    动它只会把用户的记录改坏（见文件头那段决策）。
    """
    out = []
    for rec in raw_jobs:
        if not isinstance(rec, dict) or _merged_into(rec):
            out.append(rec)
            continue
        jid = str(rec.get("id") or "")
        ordered = {"id": jid,
                   "jd_id": new_ids.get(jid) or str(rec.get("jd_id") or jid)}
        # 白名单必须与 `ANALYSIS_FIELDS` 对齐：漏一个键（`resume_id` 曾经就这样丢了）就是
        # **静默丢用户数据** —— 迁移后没人再知道那份报告用的是哪一版简历。
        for key in ("resume", "resume_id", "score", "report", "created_at"):
            if key in rec:
                ordered[key] = rec[key]
        out.append(ordered)
    return out


def _build_knowledge(new_ids, jd_by_id):
    """知识库 `from_jds[].id` → JD id（只算不写）。返回 `(结果 dict, 命中条数)`。

    - 同一 JD 的重复引用**并成一条**（A/B/C 三份分析挂在同一张卡上时，`multi_jd` 不能再被数成 3）
    - 改写时**原位保留** `job_title` / `company` / `date`（用户看到的还是原来的岗位信息），
      缺了才从 JD 记录补
    - 「有内容但解析失败」→ 抛错中止，绝不用空库顶掉（同 `jobdup._repoint_kb`）
    """
    path = store.knowledge_path()
    if store.read_json(path, None) is None and store.read_text(path, "").strip():
        raise RuntimeError("knowledge.json 读不出来（有内容但解析失败）："
                           "已放弃本次迁移，绝不写成空库")
    data = store.load_knowledge()
    touched = 0
    for node in data.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        frm = node.get("from_jds")
        if not isinstance(frm, list):
            continue
        out, seen, changed = [], {}, False
        for x in frm:
            if not isinstance(x, dict) or not isinstance(x.get("id"), str) or not x["id"]:
                out.append(x)
                continue
            old = x["id"]
            new = new_ids.get(old)
            if not new:
                out.append(x)
                continue
            if old != new:
                changed = True
            if new in seen:                    # 同一 JD 的重复引用 → 合并（并补齐缺的展示字段）
                have = seen[new]
                for key in ("job_title", "company", "date"):
                    if not str(have.get(key) or "").strip() and str(x.get(key) or "").strip():
                        have[key] = x[key]
                continue
            item = dict(x)
            item["id"] = new
            if not str(item.get("job_title") or "").strip():
                item["job_title"] = _canonical_text(jd_by_id.get(new), "job_title")
            if not str(item.get("company") or "").strip():
                item["company"] = _canonical_text(jd_by_id.get(new), "company")
            seen[new] = item
            out.append(item)
        if changed:
            node["from_jds"] = out
            touched += 1
    return {"data": data}, touched


def _canonical_text(record, key):
    """从对照信息的「原始记录 / 已有 JD 记录」里取一个展示字段（缺 → 空串）"""
    if not isinstance(record, dict):
        return ""
    for source in (record.get("canonical"), record.get("existing"),
                   record.get("members") or [{}]):
        if isinstance(source, dict):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        elif isinstance(source, list):
            for item in source:
                if isinstance(item, dict) and isinstance(item.get(key), str) \
                        and item[key].strip():
                    return item[key].strip()
    return ""


def _build_interviews(new_ids, identity_ids):
    """面试记录 `status.json.job_id` → JD id（只算不写）。

    只改**真的会变、且是本轮身份之一**的记录：已合并记录自己的旧 id（策略 A 保留原样）
    不该被写进面试记录当身份。
    """
    out = []
    for path in sorted(glob.glob(store.interview_file("*", "status.json"))):
        st = store.read_json(path, None)
        if not isinstance(st, dict):
            continue
        old = st.get("job_id")
        if not isinstance(old, str) or old not in new_ids:
            continue
        if old not in identity_ids and new_ids[old] == old:
            continue
        new = dict(st)
        new["job_id"] = new_ids[old]
        out.append((path, new))
    return out


# ---------------------------------------------------------------- 对照表
def _segs(record):
    fields = record.get("fields") or {}
    parts = [jdid._clean(fields.get(k)) or (jdid.UNKNOWN % jdid._UNKNOWN_WORD[k])   # noqa: SLF001
             for k in jdid.SEGMENTS[:-1]]
    return jdid.SEP.join(parts)


def _when(rec):
    return str(rec.get("created_at") or "无日期")


def _from_jds_ids(data):
    """知识库里出现过多少个**不同的**来源 id（对照表用同一把尺子量迁移前后）"""
    ids = set()
    for node in (data.get("nodes") if isinstance(data, dict) else None) or []:
        if not isinstance(node, dict):
            continue
        for x in node.get("from_jds") or []:
            if isinstance(x, dict) and isinstance(x.get("id"), str) and x["id"]:
                ids.add(x["id"])
    return len(ids)


def render_table(plan):
    """「迁移前 → 迁移后」对照表（给人逐行看的）。返回整段文本。"""
    lines = []
    skipped = plan["skipped"]
    n_jd = len(plan["groups"])
    before_kb = _from_jds_ids(store.load_knowledge())
    after_kb = _from_jds_ids(plan["knowledge"])
    before_multi = kb.stats(store.load_knowledge())["multi_jd"]
    after_multi = kb.stats(plan["knowledge"])["multi_jd"]
    # JD 数这一行必须是**真话**：`after` 取迁移后 jds.json 的**实际条数**，不靠算术猜。
    # 之前写成 `before + groups`，于是二次执行时表头谎报 `3 → 6`（实际 3、且确实是幂等的）
    # —— 这张表是用户决定「要不要执行」的唯一依据。
    before_jds = plan["before_jds"]
    after_jds = len(plan["jds_doc"]["jds"])
    new_jds = after_jds - before_jds

    lines.append("身份迁移 · 试跑（严格只读，一个字节都不写）")
    lines.append("profile: %s" % store.current_profile())
    lines.append("目录: %s" % store.profile_dir())
    lines.append("")
    lines.append("迁移前 → 迁移后")
    lines.append("  活跃分析 %d 份 → %d 个 JD%s" % (
        plan["active"], n_jd,
        "（其中 %d 份缺 JD 原文、%d 份原文过短，各自自成 1 个 JD）"
        % (skipped["no_text"], skipped["too_short"])
        if (skipped["no_text"] or skipped["too_short"]) else ""))
    lines.append("  JD 数 %d → %d%s" % (before_jds, after_jds,
                                      "（已迁移过，无新增）" if new_jds == 0
                                      else ("（新增 %d 个）" % new_jds if new_jds > 0 else "")))
    lines.append("  知识库引用（distinct 来源 id）%d → %d" % (before_kb, after_kb))
    lines.append("  multi_jd %d → %d" % (before_multi, after_multi))
    lines.append("")
    for i, record in enumerate(plan["groups"], 1):
        lines.append("JD %d  →  %s" % (i, record["jd_id"]))
        lines.append("  六段（归一化，进 ID 用）: %s" % _segs(record))
        shown = _jd_fields_for(record["members"], record["report"],
                              record.get("other_reports") or ())
        lines.append("  JD 级字段（原始值，进 jds.json 用）: 标题=%s | 类别=%s | 城市=%s | 薪资=%s | 单位=%s"
                     % (shown.get("job_title") or "—", shown.get("job_category") or "—",
                        shown.get("location") or "—", shown.get("salary") or "—",
                        shown.get("company") or "—"))
        for rec in record["members"]:
            lines.append("    分析 %s | %s | 报告 %s" % (
                rec.get("id"), _when(rec), rec.get("report") or "—"))
        lines.append("  归组依据: %s | 分析 %d 份 | 报告 %d 个 | 编号 %s%s"
                     % (record["why"], len(record["members"]),
                        len(record["reports"]), record["seq"],
                        " | 这个 JD 已存在（沿用原 id/原编号）" if record["existing"] is not None else ""))
        if not record["report"]:
            lines.append("  ⚠ 这一组的报告都读不出 REPORT_DATA → ID 六段从台账记录派生（如实降级，不猜）")
        lines.append("")
    if skipped["no_text"]:
        lines.append("缺 JD 原文（无法比对 → 各自自成 1 个 JD，不猜）：")
        for record in plan["groups"]:
            for rec in record["members"]:
                if str(rec.get("jd_text_file") or "").strip():
                    continue
                if not os.path.isfile(store.p_path("jd", store.safe_name(rec.get("id")) + ".txt")):
                    lines.append("  跳过指纹判定 %s | %s | 报告 %s"
                                 % (rec.get("id"), _when(rec), rec.get("report") or "—"))
        lines.append("")
    if skipped["too_short"]:
        lines.append("有 JD 原文但过短（过不了指纹保守门槛 → 各自自成 1 个 JD）：")
        for record in plan["groups"]:
            if record["why"].startswith("原文过短"):
                lines.append("  跳过指纹判定 %s | %s" % (record["members"][0].get("id"),
                                                        record["why"]))
        lines.append("")
    lines.append("报告文件名受影响情况：**报告文件不改名、不删除**（%d 个 .html 全部就地保留）。"
                 % _report_count())
    lines.append("  本迁移只改 `jobs.json` / 新写 `jds.json` / 改知识库与面试记录里的引用。")
    lines.append("JD 原文 `jd/<记录 id>.txt` 也不改名（`jd_text_file` 字段跟着指向它）。")
    lines.append("缺 JD 原文被跳过的个数：跳过了 %d 个（它们各自自成 1 个 JD）。"
                 % skipped["no_text"])
    if plan["merged"]:
        lines.append("已合并（merged_into）记录 %d 条：**保留原样**（台账本就不显示它们；"
                     "不另建 JD、不加 jd_id），只把指向它们的引用改指对应 JD。"
                     % len(plan["merged"]))
    if plan["kb_unreadable"]:
        lines.append("⚠ 有 %d 处知识库引用读不出报告、**没敢改**（保持原样，绝不指丢）：%s"
                     % (len(plan["kb_unreadable"]), "、".join(plan["kb_unreadable"][:5])))
    lines.append("")
    lines.append("以上只是试跑，**未改动任何文件**（加 --执行 才迁移，先整目录备份）。")
    return "\n".join(lines)


def _report_count():
    return len(glob.glob(os.path.join(store.p_path("reports"), "*.html")))


# ---------------------------------------------------------------- 备份 / 恢复
def _profile_files(root):
    """目录下所有文件的相对路径（含子目录）"""
    out = set()
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            out.add(os.path.relpath(os.path.join(dirpath, fn), root))
    return out


def _ignore_previous_backups(_dirpath, names):
    """`shutil.copytree` 的 ignore：**备份里绝不套备份**。

    ⚠ 实测澄清（别照抄上一版的猜测）：备份落在 `data/profiles/迁移备份_<ts>/`，
    它是 `data/profiles/<key>/` 的**兄弟目录**，所以真布局下**根本拷不进去**嵌套
    （已用隔离副本实测：新备份顶层没有 `迁移备份_*`）。这道闸门钉的是**不变量**：
    只要备份根将来挪进 profile 目录（或用户手工把旧备份搬进来），也不会滚雪球。
    `save_jds` / `_restore_profile` 都不依赖被跳过的那些目录。
    """
    return [n for n in names if n.startswith(BACKUP_PREFIX)]


def _backup_profile():
    """把 `data/profiles/<key>/` **整目录拷贝**到 `data/profiles/迁移备份_<时间戳>/`。

    **拷贝而不是移动**：整目录移动虽然更快，但会让 `profile_dir()` 在迁移过程中不存在
    （`store` 的任何读路径与测试都会当场坏掉），而且「备份」的语义本来就该是
    「原地不动的那一份」——用户翻 `data/profiles/<key>/` 时报告仍然在那里。
    备份**保留不删**（确认无误后用户自行删除）。

    拷贝时跳过 `迁移备份*`（见 `_ignore_previous_backups`）：备份里不再套备份。
    """
    src = os.path.abspath(store.profile_dir())
    if not os.path.isdir(src):
        raise RuntimeError("profile 目录不存在：%s" % src)
    dest = os.path.join(store.profiles_root(),
                        BACKUP_PREFIX + "_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
    if os.path.exists(dest):
        dest = os.path.join(store.profiles_root(),
                            BACKUP_PREFIX + "_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
    os.makedirs(store.profiles_root(), exist_ok=True)
    shutil.copytree(src, dest, ignore=_ignore_previous_backups)
    return dest


def _restore_profile(backup, had_files):
    """从备份恢复：把备份里的文件写回，并**删掉迁移新建的文件**。

    `had_files` 是迁移前该目录的全部文件（相对路径）。恢复后凡是不在 `had_files` 里的
    文件都被删掉 —— 这正是「失败 → 从备份恢复」的严格含义（不留半改的东西、不留新造的文件）。
    """
    root = os.path.abspath(store.profile_dir())
    for dirpath, _dirnames, filenames in os.walk(backup):
        rel = os.path.relpath(dirpath, backup)
        dest_dir = root if rel == "." else os.path.join(root, rel)
        if not os.path.isdir(dest_dir):
            os.makedirs(dest_dir, exist_ok=True)
        for fn in filenames:
            shutil.copy2(os.path.join(dirpath, fn), os.path.join(dest_dir, fn))
    for rel in sorted(_profile_files(root) - set(had_files), reverse=True):
        try:
            os.remove(os.path.join(root, rel))
        except OSError:
            pass
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        if dirpath != root and not dirnames and not filenames:
            try:
                os.rmdir(dirpath)
            except OSError:
                pass


def _write_json(path, obj):
    """原子写一个 JSON，并触发 `_FAIL_AFTER_WRITE` 注入点（生产恒为空）。"""
    store.write_json(path, obj)
    if _FAIL_AFTER_WRITE:
        probe = _FAIL_AFTER_WRITE[0]
        if probe is not None:
            raise getattr(probe, "boom", OSError("注入的写盘失败"))


# ---------------------------------------------------------------- 执行
def execute_migration():
    """备份 → 迁移 → 落盘；失败从备份恢复。返回进程退出码。"""
    plan = plan_migration()
    print(render_table(plan))
    print("")
    root = store.profile_dir()
    had_files = _profile_files(root)
    try:
        backup = _backup_profile()
    except BaseException as e:
        sys.stderr.write("✗ 备份失败，**没有改动任何文件**：%s\n" % e)
        return 1
    print("已备份整目录：%s（保留不删）" % backup)
    log.LOG_FILE = os.path.join(store.profile_dir(), "logs", "events.jsonl")
    try:
        with jdid._JD_LOCK:                    # noqa: SLF001 —— 锁顺序恒为 _JD_LOCK → _KB_LOCK
            _write_json(store.p_path("jds.json"), plan["jds_doc"])
            _write_json(store.jobs_path(), plan["jobs"])
            with kb._KB_LOCK:                  # noqa: SLF001
                if plan["kb_touched"]:
                    _write_json(store.knowledge_path(), plan["knowledge"])
            for path, st in plan["interviews"]:
                _write_json(path, st)
    except BaseException as e:
        import traceback
        restored = False
        try:
            _restore_profile(backup, had_files)
            restored = True
        except BaseException as e2:            # 回滚本身不能再把原始异常吞掉
            sys.stderr.write("✗ 恢复也失败了：%s\n" % e2)
        sys.stderr.write("✗ 迁移失败：%s\n%s\n" % (e, traceback.format_exc()))
        print("✗ 迁移失败：%s（%s）"
              % (e, "已用备份恢复 " + backup if restored else "恢复失败，请手工从 %s 恢复" % backup))
        return 1
    try:
        log.log_event("migration.execute_done", profile=store.current_profile(),
                      jds=len(plan["groups"]), analyses=plan["active"],
                      kb_nodes=plan["kb_touched"], interviews=len(plan["interviews"]),
                      backup=os.path.basename(backup), merged=len(plan["merged"]))
    except Exception:
        pass                                   # 日志写不进去不该让一次成功的迁移算失败
    print("")
    print("迁移完成：%d 个 JD（编号到 %s）| 活跃分析 %d 份 | 知识库改指 %d 条 | 面试记录改指 %d 个"
          % (len(plan["groups"]), max([r["seq"] or 0 for r in plan["groups"]] + [0]),
             plan["active"], plan["kb_touched"], len(plan["interviews"])))
    print("报告文件没改名、没删除；备份保留在 %s（确认无误后可自行删除）" % backup)
    return 0


# ---------------------------------------------------------------- CLI
def main():
    args = [a for a in sys.argv[1:] if a.strip()]
    if args == ["--试跑"]:
        print(render_table(plan_migration()))
        return 0
    if args == ["--执行"]:
        return execute_migration()
    sys.stderr.write(USAGE)
    return 2


if __name__ == "__main__":
    sys.exit(main())
