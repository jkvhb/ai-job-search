# -*- coding: utf-8 -*-
"""岗位去重（JD 原文判重）：归一化 + 包含度判定 + 体检 + 合并。

**为什么需要它**：「同一份 JD 被分析两次」会把同一个概念在知识库里记成「被 2 个岗位提到」，
而 `multi_jd`（被多少岗位提到）是复习优先级的**乘性**因子 —— 它在静默算错用户现在就能看到的
排序，而且同一份 JD 反复重分析会越叠越多。用户真实数据里 6 个 multi_jd 概念**全部**来自同一
对岗位（`20261003_语音文本评测_75分` / `20261008_语音文本评测_72分`：125 字 vs 198 字，
同一份 JD 的两次粘贴，其中一份是截断的）。

**为什么是包含度，而不是哈希 / 最长公共前缀 / Jaccard**（实测，别改回去）：同一份 JD 的两次
粘贴 —— 精确相等 = False；最长公共前缀只占短文本的 44%；Jaccard 只有 0.573（用常见的 0.8
阈值会**漏报**）；包含度 `|A∩B| / min(|A|,|B|)` = 0.965。不同 JD 的包含度 0.059，与阈值 0.85
之间还有 16 倍余量。包含度对「一份是另一份的截断 / 摘抄」天然友好。

**铁律**：
  1. `kb.py` 是纯数据模块，**绝不能** import 本模块（方向不能反）。
  2. 写 `knowledge.json` **只经 `kb` 的窄接口**（`kb.merge_jd_refs`）：读-改-写整段由它
     在 `kb._KB_LOCK` 里完成。本模块只在"备份 → 改库必须是一个临界区"这一点上重入那把锁
     —— 否则会和 `absorb` / `record_interview` 并发写坏文件（本项目真实发生过丢更新）。
  3. 合并只标 `merged_into`，**绝不删岗位记录、绝不删报告、绝不删 JD 原文**（报告是用户的资产）。
  4. 合并前必备份（带时间戳；**每个目标只保留最新 `store.BACKUP_KEEP` 份**，更旧的轮转删掉），
     任一步失败用备份回滚；**备份读不出来时拒绝回滚**并如实报「回滚未完成」，绝不把文件写成空的
     （轮转正是会删掉原像的那只手 —— 这条老雷就是这么变可达的）。
  5. `find_duplicate_jobs` **只读**（不写文件、不记日志），这样体检 CLI 无参数时能保证零写入。
"""
import copy
import glob
import os
import re
import threading
import unicodedata
from datetime import datetime

import jdrefs
import kb
import log
import store

# 判定参数（实测得出，不要自己拍）：
#   同一份 JD 0.965 / 不同 JD 0.059 → 0.85 落在中间，两侧余量都很大
THRESHOLD = 0.85
# 保守门槛：极短文本的包含度天然偏高（"质检" vs "质检" 就是 1.0），不做判定的下限
MIN_CHARS = 40
MIN_BIGRAMS = 30

BACKUP_SUFFIX = ".bak-"

# 标点 / 空白（含全角，与 kb._PUNCT 同一套字符集）。刻意各留一份：kb 的标点表服务于
# 「术语身份键」（术语短、要求严），这份服务于「JD 原文判重」（正文长、只求两侧同一把尺子），
# 两者的演进理由不同，共用一份反而会互相绑架。
_PUNCT = re.compile(r"[\s\u3000·・、,，.。;；:：!！?？\"'“”‘’()（）\[\]【】<>《》/\\|_\-—－+*#~`]+")

# 本模块自己的整体互斥（与 kb._KB_LOCK 同理）：merge_jobs 是「备份 → 改 KB → 改面试记录 →
# 改台账」多步写，多步之间必须整体串行。用 RLock 而不是 Lock：将来若有别的写路径互相调用，
# 普通 Lock 会直接死锁。
_MERGE_LOCK = threading.RLock()


# ---------- 判定 ----------
def normalize(text):
    """JD 文本归一化：NFKC 折叠 → 小写 → 去空白 → 去中英标点。

    非字符串一律归成空串（调用方可能拿到 None / 手改过的 JSON 字段），绝不抛异常。
    """
    if not isinstance(text, str):
        return ""
    return _PUNCT.sub("", unicodedata.normalize("NFKC", text).lower())


def bigrams(s):
    """字符 bigram 集合（"abc" → {"ab","bc"}）。

    入参按传入内容原样分块 —— **调用方应先 normalize()**（containment 就是这么做的）。
    长度不足 2 时返回空集合。
    """
    if not isinstance(s, str) or len(s) < 2:
        return set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _blocks(text):
    """归一化 + 分块 + 保守门槛。过短（< 40 字 或 bigram < 30）返回 None。

    单独拆出来只为让 find_duplicate_jobs 把每份 JD 只归一化一次
    （O(n²) 对比较时同一份 JD 会被比 n-1 次）。
    """
    s = normalize(text)
    if len(s) < MIN_CHARS:
        return None
    b = bigrams(s)
    return b if len(b) >= MIN_BIGRAMS else None


def _score(a_blocks, b_blocks):
    """两个已过门槛的 bigram 集合的包含度 |A∩B| / min(|A|,|B|)（入参保证非空）"""
    return len(a_blocks & b_blocks) / min(len(a_blocks), len(b_blocks))


def containment(a, b):
    """包含度 = |A∩B| / min(|A|,|B|)。任一侧为空或过短（保守门槛）→ 0.0。"""
    ba, bb = _blocks(a), _blocks(b)
    if ba is None or bb is None:
        return 0.0
    return _score(ba, bb)


def is_duplicate(a, b):
    """包含度 ≥ THRESHOLD 即视为同一份 JD。"""
    return containment(a, b) >= THRESHOLD


def jd_text_of(job_id):
    """读 `jd/<job_id>.txt`；缺失 / 非法 id → 空串。"""
    name = store.safe_name(job_id)
    if not name:
        return ""
    return store.read_text(store.p_path("jd", name + ".txt"), "")


# ---------- 体检 ----------
def _merged_into(job):
    """返回记录上的 `merged_into`（只认非空字符串，手改出的 5 / [] 一律当没合并）。"""
    v = job.get("merged_into") if isinstance(job, dict) else None
    return v.strip() if isinstance(v, str) else ""


def _created_key(job):
    """排序键：**更早**的排前面。日期解析不出来的排到最后（没有证据就不自称更早）。

    返回 `(0, 真时间, "", id)` 或 `(1, datetime.min, 原字符串, id)` —— 元组形状一致，
    可比较且全序（同秒也不会有平局：最后一位是 id）。
    """
    jid = str(job.get("id") or "")
    raw = str(job.get("created_at") or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return (0, datetime.strptime(raw, fmt), "", jid)
        except ValueError:
            pass
    return (1, datetime.min, raw, jid)


def find_duplicate_jobs():
    """体检：找出疑似「同一份 JD」的岗位对。**只读**（不写任何文件、不记日志）。

    返回 `(pairs, skipped)`：
      pairs   —— 升序稳定的相似对列表，每项
                 `{keep, drop, score, both_titles, both_dates, both_scores}`
                 （`both_*` 都是 `[keep 的值, drop 的值]`）
      skipped —— 因**缺 JD 原文**而跳过的在册岗位数（如实报出，不静默）

    - `keep` = 时间**更早**的那条（合并后的身份）；日期解析不出来的排在有时间的那条之后。
    - `merged_into` 非空（已合并）的记录**既不参与判定，也不计入 skipped**。
    - 排序：按 `(keep 时间, keep id, drop 时间, drop id)` 升序 —— 顺序是契约的一部分
      （CLI 直接照印），不依赖 jobs.json 里的排列。
    """
    skipped = 0
    usable = []
    for job in store.load_jobs():
        if not isinstance(job, dict) or _merged_into(job):
            continue
        text = jd_text_of(job.get("id"))
        if not text.strip():
            skipped += 1
            continue
        blocks = _blocks(text)
        if blocks is None:
            # 有原文但过短 → 不判重（也不计入「缺原文」的 skipped：那是另一回事）
            continue
        usable.append((_created_key(job), job, blocks))

    found = []
    for i in range(len(usable)):
        ka, ja, ba = usable[i]
        for k in range(i + 1, len(usable)):
            kb_key, jb, bb = usable[k]
            score = _score(ba, bb)
            if score < THRESHOLD:
                continue
            keep, drop = (ja, jb) if ka <= kb_key else (jb, ja)
            found.append((_created_key(keep), _created_key(drop), {
                "keep": keep.get("id"),
                "drop": drop.get("id"),
                "score": score,
                "both_titles": [str(keep.get("job_title") or ""), str(drop.get("job_title") or "")],
                "both_dates": [str(keep.get("created_at") or ""), str(drop.get("created_at") or "")],
                "both_scores": [keep.get("score"), drop.get("score")],
            }))
    found.sort(key=lambda t: (t[0], t[1]))
    return [p for _k, _d, p in found], skipped


# ---------- 合并 ----------
def _result(ok, reason="", **kw):
    r = {"ok": ok, "reason": reason, "noop": False, "rolled_back": False, "backups": [],
         "kb_repointed": 0, "kb_deduplicated": 0, "interview_files": 0,
         "multi_jd_before": None, "multi_jd_after": None}
    r.update(kw)
    return r


def _prune_backups(path):
    """轮转本模块留在 `path` 旁边的 `.bak-<时间戳>`：**复用 `store` 的唯一一份实现**。

    为什么必须轮转：`_snapshot` 给 `knowledge.json` 与每个 `interviews/<面试id>/status.json`
    都留一份，**每次合并各留一份、谁都不删** ⇒ 这两处无上限增长（`简历迁移` / `身份迁移` 的
    整目录备份还会把它们一起拷走）。而 `store.snapshot_before_overwrite` 的轮转**只管它自己的
    目标**（`config.json` / `jobs.json`），这两处不在里面。

    为什么走 `store` 的**公开入口** `store.prune_backups`（而不是带下划线的私有名）：它本来就是
    "按路径轮转 `<path>.bak-*`"的通用实现（`snapshot_before_overwrite` 只是它的调用方之一），
    而"新旧怎么判"是个**已经踩过坑**的判据（仓库里两种真实命名、按文件名里的时间戳去掉分隔符比
    数字；见它的注释）。另写一套 = 两份轮转实现各自漂移，正是本项目在修的那类病 —— 而且
    `docstring` 里论证"为什么可以调私有名"是一次**自相矛盾**：跨模块调带下划线的名字，`store`
    哪天把它改名 / 挪走，调用方就是**静默**失效（备份无上限增长，没人报错）。`store` 已经把这份
    实现作为公开入口提供（`prune_backups`，同一份实现、不是副本），这里就直接用它。
    上限也直接用 `store.BACKUP_KEEP`（同一个数）。
    """
    return store.prune_backups(path, keep=store.BACKUP_KEEP)


def _snapshot(path, ts, backups):
    """把 path 的**当前内容**存成 `<path>.bak-<ts>`，返回备份路径。

    文件不存在时写一个空快照 —— 备份的语义是「回到合并前的状态」，缺失也是一种状态，
    回滚时写回空内容即可（load_knowledge / load_jobs 对空文件都容错成空结构）。
    留档是**写成功之后**才记账（`backups.append` 在 `write_bytes` 后面）：所以 `backups` 里
    的每一对，在快照那一刻盘上**确实**有一份备份文件 —— 这正是 `_restore`「记账过的备份不见了
    就抛」的依据。

    **轮转**：留档之后只保留最新 `store.BACKUP_KEEP`（= 3）份（见 `_prune_backups`）；
    否则 `knowledge.json` / `interviews/*/status.json` 的 `.bak-` 会无上限增长。
    """
    backup = path + BACKUP_SUFFIX + ts
    store.write_bytes(backup, store.read_bytes(path) or b"")
    _prune_backups(path)
    backups.append((path, backup))
    return backup


def _restore(path, backup):
    """把单个文件写回备份内容。**知识库那一个必须持 `kb._KB_LOCK`** —— 回滚同样是一次
    读-改-写，不加锁会把并发的 absorb 覆盖掉（回滚本来就是为了不丢数据，不能再制造一次丢失）。

    快照按字节（`store.read_bytes` / `write_bytes`），避免解码兜底与换行转换：
    用 `read_text` 时非 UTF-8 文件会被兜成空串、写回去就把用户的数据清空；`write_text`
    在 Windows 上还会把 LF 换成 CRLF —— 那两种情况下回滚都不是"逐字节还原"。

    **拿不到可信的原像时一律抛，绝不写空**（本批修的正是这条老雷：轮转让它可达）：
      - `store.read_bytes` **只在文件不存在时**返回 `None`（权限/被占用会原样抛 `OSError`
        —— 所以"备份存在但读不出来"那一路本来就抛得出去）。而 `_snapshot` 永远是**先写成功、
        再记账**，所以记账过的备份"不见了"只可能是被删了（`_prune_backups` 轮转 / 用户手删）
        —— 那是**回滚的前提没了**，绝不是"原文本来就是空的"。判据里"合法的没留档"是
        **这一对根本不在 `backups` 里**（例如失败点在任何 `_snapshot` 之前），那种情况
        `_rollback` 根本不会走到这里、一个文件都不动；不把它做成硬错误。
      - 备份**存在但是 0 字节**：0 字节本身是合法原像（快照那一刻原文不存在 / 真的是 0 字节），
        但它**不许**盖到一份**现在有字节**的目标上 —— 那些字节是快照之后才出现的
        （`kb.merge_jd_refs` / `write_json` / `save_jobs` 写的，或并发写者写的），**没有任何备份
        覆盖它们**，写回去就是"拿一次回滚换掉用户的数据"。当前真实路径上撞不到这一条
        （台账里得有 keep/drop 才有这次合并；面试 `status.json` 是被 `read_json` 认出 `job_id`
        的；知识库要**本来就有卡**才会被 `kb.merge_jd_refs` 改写）—— 它是给"备份被外部截成 0 字节"
        和"我们快照之后有并发写者新建了目标"留的 fail-safe。

    用 `RuntimeError`：与 `kb.merge_jd_refs` 的"坏库就拒绝"同一个既有类型（全仓"前置条件不满足
    就中止"的惯例），`_rollback` 只 catch `Exception`，两种成因用同一个类型表达"拒绝回滚"，
    调用方与测试不必猜是哪种 OS 错误。
    """
    raw = store.read_bytes(backup)
    if raw is None:
        raise RuntimeError(
            "回滚的前提没了：备份 `%s` 不存在（备份总是写成功之后才记账，不见了 = 被轮转/被删）。"
            "绝不把「备份读不出来」当成「原文是空的」—— 那会把 `%s` 写成 0 字节，"
            "用户的台账/知识库就永久消失了。备份路径见本次合并的 `backups`。" % (backup, path))
    if not raw and store.file_has_bytes_to_lose(path):
        raise RuntimeError(
            "备份 `%s` 是 0 字节，而 `%s` **现在有内容**：这些字节是快照之后才写的、没有任何备份"
            "覆盖它们，写回去就是拿一次回滚换掉用户的数据。拒绝回滚。" % (backup, path))
    blob = raw
    if path == store.knowledge_path():
        with kb._KB_LOCK:
            store.write_bytes(path, blob)
    else:
        store.write_bytes(path, blob)


def _rollback(backups):
    """用备份把已改的文件恢复回去（倒序）。返回 `(是否全部恢复成功, 没能还原的文件名列表)`。

    绝不删任何文件。一个目标还原失败**不影响其它目标继续还原**（能救一个是一个），但返回值必须
    如实反映"有东西没还原回去" —— 调用方**不许**在这种情况下说"已回滚"。
    """
    ok = True
    failed = []
    for path, backup in reversed(backups):
        try:
            _restore(path, backup)
        except Exception as e:            # 回滚本身不能再抛出去把原始异常吞掉
            ok = False
            failed.append(os.path.basename(path))
            log.log_event("jobdup.rollback_failed", level="error",
                          file=os.path.basename(path), backup=backup, error=str(e))
    return ok, failed


def _interview_status_files(drop_id):
    """扫 `interviews/<面试id>/status.json`，返回 `(job_id == drop_id 的文件路径, 读不出来的原因)`。

    **原因非空 = 有一份 status.json 有字节却读不出来**，调用方（`_merge_jobs_locked`）必须
    据此**拒绝这次合并**（闸门装在任何留档 / 写盘之前）。

    口径与 `jdid._interview_status_paths` 逐字同一份（同一个闸门的两处骨架）：老实现
    `store.read_json(path, None)` 把"读不出来"当成"这份没引用我" → 返回 `[]`，合并照样成功
    → 那条面试记录的 `job_id` 指向**已被合并掉**的岗位（悬空引用，而且全程静默）。
    **"读不出来"就是"不知道它指向谁"**，而"不知道"不许当成"安全"。
    0 字节 / 文件不存在**不算**读不出来：里面一个字节都没有，也就没有 `job_id` 可指。
    """
    pattern = os.path.join(store.interviews_dir(), "*", "status.json")
    out = []
    err = ""
    for path in sorted(glob.glob(pattern)):
        st = store.read_json(path, None)
        if isinstance(st, dict):
            if st.get("job_id") == drop_id:
                out.append(path)
            continue
        if store.file_has_bytes_to_lose(path):
            err = err or ("面试记录 `%s` 读不出来（拿不到它的 `job_id`）" % path)
    return out, err


def merge_jobs(keep_id, drop_id):
    """把 drop 并进 keep：备份 → 改知识库引用 → 改面试记录 → 标记 drop 记录。

    返回一个结果 dict（便于 CLI 打印，测试不依赖它）：
      `{ok, reason, noop, rolled_back, backups, kb_repointed, kb_deduplicated,
        interview_files, multi_jd_before, multi_jd_after}`

    - **幂等**：drop 已有 `merged_into` → 立刻返回（`noop=True`，**一个文件都不动、不多备份**）
    - **只标记不删除**：drop 记录留在台账（`merged_into: keep_id`），报告 / JD 原文一个都不删
    - 任一步失败 → 用本次备份把已改的文件全部回滚，并记 `jobdup.merge_failed`
    """
    keep_id = str(keep_id or "").strip()
    drop_id = str(drop_id or "").strip()
    if not keep_id or not drop_id:
        return _result(False, "两个岗位 id 都要给")
    if keep_id == drop_id:
        return _result(False, "不能和自己合并")
    with _MERGE_LOCK:
        return _merge_jobs_locked(keep_id, drop_id)


def _mark_drop_merged(jobs, drop_id, keep_id):
    """`store.update_jobs` 用的 `fn`：给 drop 那条打上 `merged_into`，**并把显式写着
    `jd_id == drop_id` 的记录改指 keep**（批 7.1，判据见 `jdrefs.repoint_jobs_in`）。

    两件事**必须在同一次台账事务里**：分成两次写，中间那一瞬是「记录已并入 keep、却仍挂在
    drop 下」的半成品状态（而且多一次跨调用的读-改-写）。判据仍只有一份
    （`jdrefs.repoint_jobs_in` → `jd_of_record`）。
    ⚠️ **不退回 `id`**（`include_legacy=False`）：`merged_into` 那条记录（`id == drop`）按
    `身份迁移.py` 的策略 A **保留原样**（不加 `jd_id`）—— 它是历史标记、台账里不显示；
    给它补一个 `jd_id` 会与"已合并记录不挂到新 JD 上"这条既有决定冲突
    （`tests/test_migration.py::MergedRecordTest` 钉着）。真正的外部引用（知识库 / 面试
    `status.json` / `feedback.json`）由 `jdrefs` 的其余 kind 覆盖，一条都不会漏。

    返回 `(要写回的列表, 标记成功了吗)`；临界区里找不到那条记录 → `(None, False)`
    （**一个字节都不写**，由调用方响亮失败）。
    ⚠️ 本函数**只改内存**：绝不许在这里取锁 / 读别的文件 —— 它是在 `_JOBS_LOCK` 里跑的，
    而调用方还持着 `_MERGE_LOCK`（`tests/test_store.py::UpdateJobsTransactionTest` 钉着这条约定）。
    """
    jdrefs.repoint_jobs_in(jobs, drop_id, keep_id, include_legacy=False)
    for j in jobs:
        if isinstance(j, dict) and j.get("id") == drop_id:
            j["merged_into"] = keep_id
            return jobs, True
    return None, False


# 合并时要重指哪几种引用（批 7.1）：**除了 `jds` 本体**——合并绝不改 JD 记录的 id
# （那不是"合并"该动的东西，而且很可能撞上已有的那个 id）；`merged_into`（悬空引用）
# 归批 7.2，本步不处理（`jdrefs` 会如实报 ok=False）。
# ⚠️ 台账那一种（`jobs`）**不在这里**：它要和 `merged_into` 标记进**同一次** `update_jobs`
# 事务（见 `_mark_drop_merged`），由本模块在一次事务里做完 —— 判据仍调 `jdrefs.repoint_jobs_in`。
_MERGE_KINDS = ("kb", "iv_status", "report_state", "iv_feedback")


def _kb_repoint_counts(data, drop_id, keep_id):
    """**只读**算出这次合并在知识库上会"原位改指几条 / 去掉几条重复项"。

    为什么不自己再遍历一遍：那正是"同一套变换两份实现"的老病（体检 §8.1）。这里把
    `kb.ensure_kb` 的结果**复制一份**，交给**唯一一份**实现 `kb.repoint_refs_in`
    （判据 `strip=True`，与 `jdrefs` 的重写同一把尺子），只取它的计数三元组。
    副本上的改动不会落盘；真正落盘仍由 `jdrefs.rewrite_jd_refs` 在锁里做。
    """
    try:
        scratch = copy.deepcopy(kb.ensure_kb(data if isinstance(data, dict) else {}))
        _touched, repointed, deduped = kb.repoint_refs_in(scratch, drop_id, keep_id, strip=True)
        return repointed, deduped
    except Exception:                     # noqa: BLE001 —— 计数只是给日志/CLI 看的，绝不让它挡住合并
        return 0, 0


def _merge_jobs_locked(keep_id, drop_id):
    """merge_jobs 的主体，**调用方必须已持 _MERGE_LOCK**（拆出来只为让锁的范围一眼可见）"""
    raw = store.load_jobs()
    index = {}
    for j in raw:
        if isinstance(j, dict) and isinstance(j.get("id"), str) and j["id"]:
            index.setdefault(j["id"], j)
    keep, drop = index.get(keep_id), index.get(drop_id)
    if keep is None:
        return _result(False, "台账里找不到 keep 岗位：%s" % keep_id)
    if drop is None:
        return _result(False, "台账里找不到 drop 岗位：%s" % drop_id)
    done = _merged_into(drop)
    if done:
        return _result(True, "drop 已是合并过的记录（merged_into=%s），本次不做任何改动" % done,
                       noop=True)

    # ---- 需求一：任何一份面试 status.json 读不出来 → **拒绝这次合并** ----
    # 判据与 `jdid._interview_status_paths` 同一份（见 `_interview_status_files`）：面试记录是
    # 每个面试目录一个文件，漏一份没有任何集中信号；老实现把它当成"这份没引用我"跳过，
    # 于是 drop 被标 `merged_into`、那条面试记录的 `job_id` 仍指向**已被合并掉**的岗位
    # （悬空引用 + 同岗位两种身份，全程静默）。
    # 闸门必须在**任何 `_snapshot` / 写盘之前**：拒绝时一个数据文件都没动（连一份 `.bak-` 都不多）。
    iv_files, iv_err = _interview_status_files(drop_id)
    if iv_err:
        msg = ("%s。**已拒绝这次合并**：读不出来就无法确认它是不是挂在 %s 下面，"
               "照合会留下一条 `job_id` 指向已被合并掉的岗位的面试记录（悬空引用）。"
               "这次合并**没有改动任何数据文件**（只往日志里记了这一条拒绝，连一份 `.bak-` 都没多）"
               "—— 请先把它修好（或从别处恢复、把它移开）再合并。"
               % (iv_err, drop_id))
        log.log_event("jobdup.merge_rejected", level="warn", reason=msg,
                      keep=keep_id, drop=drop_id)
        return _result(False, msg)

    # ---- 批 7.1：引用清单只有一份（`jdrefs.KINDS`）----
    # 原先这里只重指知识库那一半 + 面试 `status.json`，漏了 `interviews/*/feedback.json`
    # （键就是 drop）与 `report_state/<报告id>.json`。
    # ⚠️ 这一层**不自己判"读不出来"**：`rewrite_jd_refs` 自己在任何落盘之前就会因为
    # `err` 非空 / 预检不过而 `JdRefsRefused`（一个字节都不写），下面那个 `except` 会回滚并
    # 如实回报。那正是"读不出来 ≠ 没有引用"的**唯一一份**判据，本模块不另造一套。
    refs = jdrefs.refs_of_jd(drop_id)
    rewrite_refs = [r for r in refs if r.kind in _MERGE_KINDS]

    before = kb.stats(store.load_knowledge())["multi_jd"]
    repointed, deduped = _kb_repoint_counts(store.load_knowledge(), drop_id, keep_id)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backups = []
    try:
        _snapshot(store.jobs_path(), ts, backups)
        # 面试记录先留档（**在任何重指之前**）：`jdrefs` 接下来会改写它们的 `job_id`，
        # 回滚的原像必须是动手之前那份。清单复用上面那道闸门扫出来的同一份
        # —— 否则"闸门扫的"与"写盘扫的"是两个时刻的输入（同源读取只改一半）。
        for path in iv_files:
            _snapshot(path, ts, backups)
        # 知识库那一段的备份与读-改-写必须在同一把锁里：备份若在锁外取，中间挤进来一次
        # absorb 就会让「备份」不再是本次改动的原像，回滚会把别人的更新抹掉。
        # 引用重指本身收归 `jdrefs.rewrite_jd_refs`（单一真源）：这里持着 `_KB_LOCK` 只是
        # **重入**（备份 → 改库 必须是一个临界区），真正保护它的锁在 kb 自己的模块里。
        with kb._KB_LOCK:
            _snapshot(store.knowledge_path(), ts, backups)
            # ---- 引用重指：`jdrefs` 的清单，漏一种就是盘上孤儿 ----
            # 它写之前重新过一遍每一处的 `*_safe_to_write`：任何一处读不出来 ⇒ **整次拒绝**
            # （一个字节都不写）；落盘中途失败 ⇒ 用原像逐字节回滚后再抛。
            try:
                jdrefs.rewrite_jd_refs(drop_id, keep_id, refs=rewrite_refs)
            except (jdrefs.JdRefsRefused, jdrefs.JdRefsWriteFailed) as e:
                # 坏库 / 坏文件 / 写盘失败：一律走下面的 `except`（回滚 + 如实回报），
                # 但保留 `jdrefs` 那句说清"哪个文件、为什么"的原文。
                raise RuntimeError(str(e))

        # 台账那一段（计划 2.4）：读-改-写收进 `store.update_jobs` —— 整段在 `_JOBS_LOCK` 里，
        # 而且用的必须是**刚读出来**的那份、不是第 352 行那份快照。老写法 `save_jobs(raw)`
        # 把旧快照整份写回：合并窗口里挤进来的一次分析落盘（`upsert_job`）会被整份顶掉
        # —— 报告文件还在盘上，台账里却再也看不到那次刚花过模型额度的分析。
        # ⚠️ 这一笔**同时**做两件事（`_mark_drop_merged`）：把挂在 drop 下的记录改指 keep
        # （否则 `refs_of_jd(drop)` 永远非空）与打 `merged_into` 标记。
        # ⚠️ `fn` 只改内存（此刻本线程持着 `_MERGE_LOCK`，去取 store 的 `_JOBS_LOCK` 是
        # "外层 → 内层"，与原来在同一行调 `save_jobs` 取的是同一把锁，锁序逐边不变）。
        marked = store.update_jobs(lambda jobs: _mark_drop_merged(jobs, drop_id, keep_id))
        if not marked:
            # 临界区里那条记录不见了（合并窗口里被别的写者删掉，例如一次并发的真删除）：
            # 不改写、也不谎报成功。`RuntimeError` 与其余失败同一条路（下面 `except` 会回滚）。
            raise RuntimeError(
                "台账里 drop 岗位（%s）在合并过程中被别的写者删掉了：本次合并中止并回滚，"
                "绝不把一份已经不是最新状态的快照写回盘上。" % drop_id)
    except BaseException as e:
        restored, failed = _rollback(backups)
        log.log_exc("jobdup.merge_failed", e, keep=keep_id, drop=drop_id,
                    rolled_back=restored, backups=len(backups), not_restored=failed)
        if not backups:
            # 备份一份都没留成（失败点在任何 `_snapshot` 之前）⇒ 目标文件**一个字节都没动**。
            # 这时候说"已回滚"是多说的（同 jddelete 那条规矩：备份都没成功 ≠ 已回滚）。
            how = "尚未改动任何文件：备份没留成，目标一个字节都没动"
        elif restored:
            how = "已用备份回滚"
        else:
            # 回滚**没成**：必须如实说，还要点名哪个文件没还原（用户据此手工自救）
            how = "回滚未完成！没能还原：%s（细节见日志 jobdup.rollback_failed，备份路径见下）" \
                  % "、".join(failed)
        return _result(False, "合并失败：%s（%s）" % (e, how),
                       rolled_back=restored, backups=[b for _p, b in backups])

    after = kb.stats(store.load_knowledge())["multi_jd"]
    log.log_event("jobdup.merge_done", keep=keep_id, drop=drop_id,
                  kb_repointed=repointed, kb_deduplicated=deduped,
                  interview_files=len(iv_files), multi_jd_before=before, multi_jd_after=after)
    return _result(True, "已把 %s 并入 %s" % (drop_id, keep_id),
                   backups=[b for _p, b in backups], kb_repointed=repointed,
                   kb_deduplicated=deduped, interview_files=len(iv_files),
                   multi_jd_before=before, multi_jd_after=after)
