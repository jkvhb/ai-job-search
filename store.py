# -*- coding: utf-8 -*-
"""路径解析（profile 隔离）+ 文本/JSON 读写 + config/jobs/knowledge 存取"""
import json
import os
import re
import tempfile
import threading
from datetime import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_ROOT = os.path.join(ROOT, "data")
# 重要：路径一律用函数动态计算，不要缓存成模块级常量——
# 否则测试替换 DATA_ROOT 后仍会指向真实数据目录，污染用户数据。

DEFAULT_CONFIG = {
    "text_model": {"label": "DeepSeek（文本分析 / 跑 SOP）",
                   "base_url": "https://api.deepseek.com/v1",
                   "model": "deepseek-flash", "temperature": 0.3, "api_key": ""},
    "vision_model": {"label": "Kimi（识图 OCR / 读 JD 截图）",
                     "base_url": "https://api.moonshot.cn/v1",
                     "model": "kimi-k2.6", "temperature": 1, "api_key": ""},
    "search_providers": [],
    "max_searches": 40,   # 每次知识分析最多搜多少次（核心概念优先），控制搜索额度消耗
}

CATEGORIES = ["数据标注/质检", "AI数据运营", "标注管理", "AI产品经理", "AI训练师", "其他"]
STATUSES = ["未投递", "已投", "待回复", "面试中", "已拒", "Offer", "放弃"]

# 覆盖前留档的命名后缀（与 `jobdup.BACKUP_SUFFIX` 同一风格；`_quarantine_report_state` 用的是
# `.corrupt-<时间戳>`，那个语义是"读不出来的坏文件先挪走"，与"留一份覆盖前的样子"不同）。
BACKUP_SUFFIX = ".bak-"

# 每个目标文件**只保留最新几份** `.bak-`，更旧的删掉（轮转）。
# 为什么是 3：留档的用处是"这次覆盖错了，马上退回上一两次的样子"，最近 3 次足够覆盖真实误操作
# （连点几次"保存设置"、连跑几次分析）；再多也只是让 profile 目录无上限变大 —— `upsert_job`
# **每次分析**都留一份，而 `简历迁移` / `身份迁移` 的整目录备份还会把这些 `.bak-` 一起拷走。
BACKUP_KEEP = 3

# ---------- 并发：读-改-写的整体互斥 ----------
# 为什么 store 需要锁：`jobs.json` 的读-改-写（`upsert_job` = load → 改 → 整份写回）在并发下会
# **静默丢台账** —— 两个分析各自读到同一份旧台账、各自写回，后写的把先写的那条顶掉
# （实测 20 轮 ×2 条 → 20/20 全丢）。而 `jds.json`(`jdid._JD_LOCK`)、`knowledge.json`
# (`kb._KB_LOCK`)、`resumes.json`(`resumelib._LOCK`)、`log`、`jobdup` 早就有锁，只有台账没有。
# 锁加在 store 而不是调用方：app / jddelete / jdid / jobdup 四个写者因此共用同一把（包在 app 一层
# 等于没修，另外三个写者照样互踩）。
#
# 用 RLock 而不是 Lock：`upsert_job` 内部要调 `load_jobs` / `save_jobs`，同一线程必须能重入
# （普通 Lock 会当场死锁）。名字与注释风格照 `kb._KB_LOCK` / `jdid._JD_LOCK`。
#
# 锁序：store 是**叶子模块**（不 import 任何别的项目模块），这两把锁永远是**最内层**的锁，
# 锁序恒为"上层锁 → store 的锁"（`_JD_LOCK` / `_MERGE_LOCK` / `app._STORE_PATCH_LOCK` → 这里），
# 不存在反向获取，因此不会与既有锁形成环。
# 为什么分两把（而不是一把罩两个文件）：一把锁只护一个文件，与 `_JD_LOCK`(jds.json) /
# `_KB_LOCK`(knowledge.json) 同一风格，"这把锁在护谁"一眼可见；且 store 里**没有任何一处**
# 同时碰台账与配置（两把锁之间零嵌套），所以它们之间不存在锁序问题，分开也不会互相排队 ——
# "保存设置"没必要等"分析落台账"写盘。
_JOBS_LOCK = threading.RLock()
_CONFIG_LOCK = threading.RLock()


class ProfileUnreadable(RuntimeError):
    """`current_profile.txt` **存在却读不出来** —— 绝不能静默退回 default。

    为什么是这个类型：全仓库「前置条件不满足就中止」用的是 `RuntimeError`
    （`jddelete._readable_jobs_or_raise`、`简历迁移._doc_state`、app 的各个路由），
    继承它能让既有兜底接住并**如实回话**（例如 `简历迁移.main()` 会印成一行错误而不是 traceback）；
    独立的子类让调用方与测试能精确区分「档名读不出来」这一种，不必靠文案匹配。
    """


# ---------- profile ----------
def profiles_root():
    return os.path.join(DATA_ROOT, "profiles")


def current_profile_file():
    return os.path.join(DATA_ROOT, "current_profile.txt")


def profile_dir(key=None):
    k = (key or "").strip() or "default"
    k = re.sub(r"[^\w\-]", "_", k)
    return os.path.join(profiles_root(), k)


def ensure_profile(key=None):
    d = profile_dir(key)
    for sub in ("resumes", "reports", "jd", "logs"):
        os.makedirs(os.path.join(d, sub), exist_ok=True)
    return d


def current_profile():
    """当前 profile 的档名。**文件不存在 → `"default"`**；**存在却读不出来 → 抛错**。

    ⚠️ 为什么不能再是 `except Exception: return "default"`：那会把两件**相反**的事混成
    同一个信号 —— "文件不存在"（合法的第一次）与"文件存在却有字节读不出来"
    （记事本按 ANSI/GBK 存过中文档名、磁盘/IO 错误、权限问题）。后者意味着：
    用户以为自己在"张三"下面工作，而**之后每一次写入都落进 default**
    （`p_path()` 是靠本函数解析目录的），界面上完全看不出来，而且**不可逆**——
    新写的分析/配置/知识都已经进了另一个档，事后无法分辨哪些是串过去的。

    所以这里只认两种情形：读得出来（用它的值）、文件真的不存在（→ default）。
    其余一律响亮失败（`ProfileUnreadable`），让用户先去修那个文件；宁可这次不启动，
    也不静默把用户的东西写进另一个人的档里。
    """
    fp = current_profile_file()
    try:
        with open(fp, "r", encoding="utf-8") as f:
            return f.read().strip() or "default"
    except FileNotFoundError:
        return "default"
    except Exception as e:
        raise ProfileUnreadable(
            "档名文件 `%s` **存在但读不出来**：%s\n"
            "**已拒绝**静默回退到 default —— 那会让之后的每一次写入串到另一个档，且不可逆。"
            "请先把它修好（内容应当是一行 UTF-8 的档名），或者删掉它（删掉 = 回到 default）。"
            % (fp, e))


def set_current_profile(key=None):
    """切换当前 profile 的档名，**原子写**。

    ⚠️ 为什么必须是原子写：老写法 `open(fp, "w")` 会**立刻**把原文件截断成 0 字节，之后才写
    新档名。这中间任何一次读（另一条线程、另一个进程、或进程恰好在这里崩掉/断电）拿到的都是
    空文件 —— 而空文件按约定等于 `default`，用户就被**静默**换档了，正是本文件要防的那件事。
    先写同目录临时文件再 `os.replace`（同目录 = 同文件系统 = 原子），失败时删掉临时文件、抛错。
    """
    k = (key or "").strip() or "default"
    ensure_profile(k)
    fp = current_profile_file()
    d = os.path.dirname(fp)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-profile-", suffix=".txt", dir=d)
    os.close(fd)
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(k)
        os.replace(tmp, fp)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return k


def p_path(*parts, **kw):
    """解析到指定 key 的 profile；未指定则用「当前 profile」（多用户隔离的关键）"""
    return os.path.join(profile_dir(kw.get("key") or current_profile()), *parts)


# ---------- 读写 ----------
def read_text(path, default=""):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return default


def write_text(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def read_bytes(path):
    """按**原始字节**读；文件不存在 → `None`（读失败**不**兜成空 —— 那正是数据丢失的来源）。

    ⚠️ **备份 / 回滚必须用它**，不能用 `read_text`：非 UTF-8 文件在 `read_text` 里解码失败
    会被兜成默认空串，于是"快照"里存的是空 —— 回滚时就把用户的文件写成空的。
    本项目实测过：`jobdup` 的合并回滚、`jdid` 的改 ID 回滚都会这样。
    阶段 D 在 `jddelete` 里已经踩过同一个坑（那时连 LF→CRLF 都会被换掉），修法就是"快照按字节"。
    """
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return None


def write_bytes(path, raw):
    """按**原始字节**写（不做任何编码/换行转换）。父目录不存在则建（与 `write_text` 一致）。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(raw)


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path, obj):
    """原子写 JSON：先写同目录临时文件，再 os.replace 换过去。

    为什么必须原子：`open(path,"w")` 会立刻把原文件截断成 0 字节，之后 json.dump 是一段一段
    往盘上写的（knowledge.json 有 27 万字节）。这中间任何一次并发读（例如用户在知识库 tab
    里点掌握度时的 load）读到的是半截文件，`read_json` 归成 None、`load_knowledge` 静默返回
    `{"nodes": []}`，下一次 absorb 还会把这份坏文件当垃圾隔离走 —— 用户 97 个概念就此从可见库
    消失。进程被 kill / 断电时同样会留下半截文件。

    临时文件必须与目标**同目录**：os.replace 只在同一文件系统内保证原子（跨盘会退化成复制）。
    序列化或写盘中途失败时删掉临时文件，绝不在目录里留垃圾。
    """
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=d)
    os.close(fd)
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def safe_name(name):
    """只取文件名；拒绝 . / .. 这类可越级的名字（返回空串）"""
    n = os.path.basename(str(name or "").strip())
    return "" if n in (".", "..") else n


def slug(text, limit=28):
    t = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "", str(text or "")).strip()
    t = re.sub(r"\s+", "", t)
    return (t[:limit] or "岗位")


# ---------- config ----------
def config_path(key=None):
    return p_path("config.json", key=key)


def load_config(key=None):
    """读配置（持 `_CONFIG_LOCK`：与 `save_config` 的整份覆盖互斥，见 `_CONFIG_LOCK` 的注释）。"""
    with _CONFIG_LOCK:
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        data = read_json(config_path(key), {})
        if isinstance(data, dict):
            for k in ("text_model", "vision_model"):
                if isinstance(data.get(k), dict):
                    cfg[k].update({kk: vv for kk, vv in data[k].items() if vv is not None})
            if isinstance(data.get("search_providers"), list):
                cfg["search_providers"] = data["search_providers"]
            # 可调项：用户可在 config.json 里改搜索次数上限（bool 是 int 子类，要排除）
            ms = data.get("max_searches")
            if isinstance(ms, int) and not isinstance(ms, bool) and ms >= 0:
                cfg["max_searches"] = ms
        return cfg


def save_config(cfg, key=None):
    """写配置。**读不出来时拒绝**（理由见 `config_safe_to_write`），覆盖前留一份 `.bak-`。

    ⚠️ 整个"判能不能写 → 留档 → 写盘"在 `_CONFIG_LOCK` 里：判据与写盘之间若挤进另一个写者，
    留档就不再是"覆盖前"那一份（`jobs.json` 的丢更新就是同一个毛病）。
    """
    with _CONFIG_LOCK:
        p = config_path(key)
        ok, err = config_safe_to_write(p)
        if not ok:
            raise RuntimeError(
                "%s。**拒绝保存设置**：`load_config()` 读不出来时回的是默认配置"
                "（`api_key` 为空、`search_providers` 为空），它**看起来就是「没配过」的样子** ——"
                "写下去就是真的 API Key 与搜索来源永久消失，盘上没有任何副本。"
                "请先把 `config.json` 修好（或从别处恢复、把它移开）再保存。" % err)
        snapshot_before_overwrite(p)
        write_json(p, cfg)


def read_config_strict(path=None):
    """读配置，**区分"读不出来"与"没有"**：返回 `(配置, 原因)`，原因非空 = 不可信。

    形状照 `read_knowledge_strict` 抄 —— "读不出来 ≠ 没有"这条口径只准有一份实现，
    只是这里的"有"是"含 api_key / search_providers 的对象"。
    """
    p = path or config_path()
    if not os.path.exists(p):
        return {}, ""
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        # 文件名必须插值：本函数是通用判据（调用方可以传别的 path），写死会让用户去修错文件。
        return {}, "配置 `%s` 读不出来：%s" % (os.path.basename(p), e)
    if not isinstance(data, dict):
        return {}, "配置 `%s` 的结构不对（应当是含 text_model / search_providers 的对象）" % (
            os.path.basename(p),)
    return data, ""


def config_safe_to_write(path=None):
    """`config.json` 当前是否可以**安全写盘**？返回 `(可写, 原因)`。

    可写 = **读得出来**，**或者**文件里根本没有一个字节（不存在 / 真的是 0 字节）。
    其余（有字节、却读不出来）→ 不可写：调用方必须**拒绝**这次保存。

    为什么必须有它：`load_config()` 在任何读/解析失败时都回 `DEFAULT_CONFIG`，
    而那份默认值看起来**就是"没配过"的样子**（`api_key=''`、`search_providers=[]`）：
    用户手改坏了 `config.json`（或记事本按 ANSI 存成了 GBK），界面上一切正常，
    点一次"保存设置"就把真的 API Key 与搜索来源**永久**抹掉了 —— 不可恢复、没有任何副本。

    与 `read_config_strict` 的分工同知识库：那个回答"读得出来吗"，这个回答"写上去会不会丢东西"
    （0 字节的文件读不出来，但写上去不丢任何东西，是合法的"第一次"）。
    """
    p = path or config_path()
    _data, err = read_config_strict(p)
    if not err:
        return True, ""
    if not file_has_bytes_to_lose(p):
        return True, ""
    return False, err


# ---------- jobs（岗位台账）----------
def jobs_path(key=None):
    return p_path("jobs.json", key=key)


def load_jobs(key=None):
    """读台账（持 `_JOBS_LOCK`：与 `save_jobs` 的整份覆盖互斥，见 `_JOBS_LOCK` 的注释）。"""
    with _JOBS_LOCK:
        d = read_json(jobs_path(key), [])
        return d if isinstance(d, list) else []


def read_jobs_strict(path=None):
    """读台账，**区分"读不出来"与"没有"**：返回 `(记录列表, 原因)`，原因非空 = 不可信。

    形状照 `read_knowledge_strict` 抄（"读不出来 ≠ 没有"这条口径只准有一份实现）。
    必须有它，是因为 `load_jobs()` 在任何读/解析失败时都回 `[]`，
    而 `[]` 与"真的没有台账"无法区分 —— 调用方一旦把它写回去就是**整份台账被清空**。

    `jddelete` 早就有自己的 `_jobs_or_error()`（只保护它那条删除路径）；
    这条收归到 store 之后，四个写者（app / jddelete / jdid / jobdup）才共用同一把尺子。

    **只读**：不隔离、不改名、不写盘。
    """
    p = path or jobs_path()
    if not os.path.exists(p):
        return [], ""
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return [], "台账 `%s` 读不出来：%s" % (os.path.basename(p), e)
    if not isinstance(data, list):
        return [], "台账 `%s` 的结构不对（应当是一个列表）" % (os.path.basename(p),)
    return data, ""


def jobs_safe_to_write(path=None):
    """`jobs.json` 当前是否可以**安全写盘**？返回 `(可写, 原因)`。

    可写 = **读得出来**，**或者**文件里根本没有一个字节（不存在 / 真的是 0 字节）。
    其余（有字节、却读不出来）→ 不可写。

    为什么必须有它：`upsert_job` 是分析记录写盘的**唯一真出口**（`app.py` 的 `/api/analyze`
    与结果面板、`outcome.py` 都走它），而它的第一步 `load_jobs()` 在读/解析失败时回 `[]`
    —— 于是"一次读失败"就等于"整份台账被那一条新记录顶掉"：所有岗位的分析记录一次性消失，
    报告文件还在盘上，但台账与界面上再也看不到这份刚花过模型额度跑出来的分析。
    触发条件是现实的：用户手改 JSON、非 UTF-8 保存（本项目已栽过一次 GBK）、
    杀毒软件短暂锁文件、磁盘/IO 错误。

    ⚠️ 判据必须问**严格读**，**不能**看 `load_jobs()` 的返回值 ——
    那正是把"读不出来"和"空台账"混成同一个信号的根源。
    """
    p = path or jobs_path()
    _data, err = read_jobs_strict(p)
    if not err:
        return True, ""
    if not file_has_bytes_to_lose(p):
        return True, ""          # 不存在 / 真的是 0 字节：合法的"第一次"
    return False, err


def save_jobs(jobs, key=None):
    """写台账。**读不出来时拒绝**（理由见 `jobs_safe_to_write`），覆盖前留一份 `.bak-`。

    ⚠️ 整个"判能不能写 → 留档 → 写盘"在 `_JOBS_LOCK` 里（理由见 `_JOBS_LOCK` 的注释）：
    判据与写盘之间若挤进另一个写者，留下就不是"覆盖前"那一份了。
    """
    with _JOBS_LOCK:
        p = jobs_path(key)
        ok, err = jobs_safe_to_write(p)
        if not ok:
            raise RuntimeError(
                "%s。**拒绝写入**：`save_jobs` / `upsert_job` 是**整份覆盖**，而现在内存里那份是从"
                "读不出来的文件兜出来的空台账 —— 写下去就是所有岗位的分析记录一次性没掉。"
                "请先把 `jobs.json` 修好（或从别处恢复、把它移开）再保存。" % err)
        snapshot_before_overwrite(p)
        write_json(p, jobs)


def upsert_job(record, key=None):
    """插入/更新一条分析记录。**整段在 `_JOBS_LOCK` 里**：读 → 改 → 写是一个临界区。

    ⚠️ 锁必须包住整个读-改-写，不能只包 `save_jobs`：`upsert_job` 是分析落盘的**唯一真出口**
    （`app._write_analysis`、`outcome.py` 都走它），两个并发分析各持一份"读出来的旧台账"，
    后写的就会把先写的那条**静默**顶掉（实测 20/20 丢）。只锁写盘那一步挡不住这种丢更新
    —— 那正是"两个写者各自读、各自写"的经典丢失。
    """
    with _JOBS_LOCK:
        p = jobs_path(key)
        # ⚠️ 先判"能不能写"再读：读不出来时 `load_jobs()` 给的是 `[]`，
        # 拿它去 upsert 就是把整份台账换成这一条（老行为，实测丢过）。
        ok, err = jobs_safe_to_write(p)
        if not ok:
            raise RuntimeError(
                "%s。**拒绝写入**：这次 upsert 会把整份台账覆盖成「这一条 + 读出来的空列表」，"
                "旧记录会在盘上消失且没有副本。请先把 `jobs.json` 修好"
                "（或从别处恢复、把它移开）再分析。" % err)
        jobs = load_jobs(key)
        for i, j in enumerate(jobs):
            if j.get("id") == record.get("id"):
                j.update(record)
                jobs[i] = j
                save_jobs(jobs, key)
                return j
        jobs.insert(0, record)
        save_jobs(jobs, key)
        return record


def find_job(job_id, key=None):
    for j in load_jobs(key):
        if j.get("id") == job_id:
            return j
    return None


# ---------- jds（JD 库，`jdid` 的写目标）----------
# `jds.json` 的**路径**归 `jdid._jds_path(key)` 管（含 profile key 的解析）；store 这一侧只提供
# "读得出来吗 / 能不能写"这对判据 —— 形状与 config / jobs / knowledge 三份完全相同
# （「读不出来 ≠ 没有」这条口径**只准有一份实现**，而这三份早就有了，jds 是漏掉的那一份）。
# ⚠️ 按 key 指定的那一份，调用方必须把 path **显式传进来**：这里的默认值是"当前 profile"，
# 拿它去判另一个档的文件就是问错了对象（判据与写盘目标必须**同一个路径**）。
def _empty_jd_library():
    """空 JD 库的占位形状（与 `jdid.load_jds()` 的空库逐字段一致，方便调用方写类型安全的分支）。

    ⚠️ 它只是**读不出来时的占位**，绝不能被写回盘上 —— 那正是这对判据要拦的事。
    """
    return {"version": 1, "seq": 0, "jds": []}


def read_jds_strict(path=None):
    """读 JD 库，**区分"读不出来"与"没有"**：返回 `(数据, 原因)`，原因非空 = 不可信。

    形状照 `read_knowledge_strict` / `read_jobs_strict` 抄。为什么必须有它：
    `jdid.load_jds()` 的契约是"**绝不抛异常**"（任何读/解析失败 → 空库），而 `jdid.save_jds`
    是**整份覆盖** —— 于是"一次读失败"就等于整个 JD 库被内存里那份空库顶掉。
    这是四个写目标里**唯一**还没有护栏的一个（config / jobs / knowledge 早就有各自的那一对）。

    **只读**：不隔离、不改名、不写盘（坏文件原样留着，等用户自己处理）。
    """
    p = path or p_path("jds.json")
    if not os.path.exists(p):
        return _empty_jd_library(), ""
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        # 文件名必须插值：调用方可以传别的 path，写死 jds.json 会让用户照着这句话去修错文件。
        return _empty_jd_library(), "JD 库 `%s` 读不出来：%s" % (os.path.basename(p), e)
    if not isinstance(data, dict) or not isinstance(data.get("jds"), list):
        return _empty_jd_library(), "JD 库 `%s` 的结构不对（应当是含 `jds` 列表的对象）" % (
            os.path.basename(p),)
    return data, ""


def jds_safe_to_write(path=None):
    """`jds.json` 当前是否可以**安全写盘**？返回 `(可写, 原因)`。

    可写 = **读得出来**，**或者**文件里根本没有一个字节（不存在 / 真的是 0 字节）。
    其余（有字节、却读不出来）→ 不可写：调用方必须**拒绝**这次写盘。

    为什么必须有它（**实测，隔离探针，299 字节 / 2 条记录的 GBK `jds.json`**）：
      - `jdid.upsert_jd(一条新记录)` → 盘上 272 字节、**只剩新那一条**（旧 2 条被清）
      - `jdid.next_seq()`            → 盘上 47 字节、`{"version":1,"seq":1,"jds":[]}`（**全清**）
      两次都**没有留下任何 `.bak-`**（老 `save_jds` 压根没有留档这一步）—— 不可恢复。
    触发条件是现实的：用户手改 JSON、记事本按 ANSI/GBK 存过中文、杀毒软件短暂锁文件、IO 错误。

    ⚠️ 判据必须问**严格读**，**不能**看 `jdid.load_jds()` 的返回值 ——
    那正是把"读不出来"和"空库"混成同一个信号的根源。
    """
    p = path or p_path("jds.json")
    _data, err = read_jds_strict(p)
    if not err:
        return True, ""
    if not file_has_bytes_to_lose(p):
        return True, ""          # 不存在 / 真的是 0 字节：合法的"第一次"
    return False, err


# ---------- knowledge（知识库，阶段 1 先留骨架）----------
def knowledge_path(key=None):
    return p_path("knowledge.json", key=key)


def load_knowledge(key=None):
    d = read_json(knowledge_path(key), {"nodes": []})
    if not isinstance(d, dict) or not isinstance(d.get("nodes"), list):
        return {"nodes": []}
    return d


def read_knowledge_strict(path=None):
    """读知识库，**区分"读不出来"与"没有"**：返回 `(data, 原因)`，原因非空 = 不可信。

    为什么必须有这么一个函数：`load_knowledge()` 在任何读/解析失败时都返回
    `{"nodes": []}`，而调用方往往会**把它写回去** —— 于是"一次读失败"就等于
    "用户的全部积累被清空"。这条规则**只准有一份实现**（`jddelete` 已经用它；
    后续的 `kbreuse` 也会用同一条规则）。

    **只读**：不隔离、不改名、不写盘（坏文件原样留着，等用户或 `kb.absorb` 自己处理）。
    """
    p = path or knowledge_path()
    if not os.path.exists(p):
        return {"nodes": []}, ""
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        # 文件名必须插值：调用方可以传别的 path，写死 knowledge.json 会让用户照着这句话
        # 去修**另一个**文件（本函数是通用判据，不是 knowledge.json 专用）。
        return {"nodes": []}, "知识库 `%s` 读不出来：%s" % (os.path.basename(p), e)
    if not isinstance(data, dict) or not isinstance(data.get("nodes"), list):
        return {"nodes": []}, "知识库 `%s` 的结构不对（应当是含 `nodes` 列表的对象）" % (os.path.basename(p),)
    return data, ""


def content_bytes_present(path):
    """这个文件里有没有**可丢的字节**？

    ⚠️ **必须看原始字节**，绝不能用解码后的字符串：非 UTF-8 文件（比如被 GBK 写坏的
    `knowledge.json` / `resumes.json`）在 `read_text` 里解码会失败、并被兜成默认空串，
    于是"有内容但读不出来"被误判成"空文件，没有可丢的数据"。
    实测过：GBK 的 `knowledge.json` 会被内存里的空库顶掉，用户的卡全没。

    **它是公开的**：其余调用方是勾选状态的隔离（`_quarantine_report_state`，那里判的是
    "值不值得造一份备份"）、简历版本库（`resumelib`）与 app 的两道简历闸。
    ⚠️ **写盘判据不要用它**（P0-6 之后改用 `file_has_bytes_to_lose`）：纯空白文件在它这里
    算"没有可丢的内容"，于是"有字节却解析不出"会被判成可写、被内存里的空结构顶掉。
    """
    try:
        with open(path, "rb") as f:
            return bool(f.read().strip())
    except FileNotFoundError:
        return False          # 真的不存在：没有可丢的数据
    except OSError:
        # **存在但打不开**（权限/被锁）：不知道里面有什么 → 一律当成"有内容"。
        # 把"不知道"当成"安全"正是这个项目丢过数据的那条思维习惯。
        return True


def file_has_bytes_to_lose(path):
    """这个文件里**有没有一个字节**可以丢？（**写盘判据**专用，P0-6）

    与 `content_bytes_present()` 的分工（两个判据都在，**别混用**）：
      - `content_bytes_present()` 回答"有没有**可见内容**"（`strip()` 之后还有东西吗）。
        它按自己的语义是对的、且有测试钉着 —— **不要改它**；
        `_quarantine_report_state()` 那种"几个勾、不值得为它挡掉写入"的场景继续用它。
      - 本函数回答"盘上有没有**一个字节**"，是写盘判据：一个只有 `b"\\r\\n   \\r\\n"` 的文件
        **不是 0 字节** —— 它可能是用户手写的、别的工具写坏的、或将来还能救回来的东西。
        "解析不出"不等于"没有可丢的内容"；把两者混起来，用户的知识库/版本库就会被空结构顶掉。

    只有"**不存在**"或"**真的是 0 字节**"才算没东西可丢。
    **存在但打不开**（权限/被锁）→ 当成"有"（fail-safe）：把"不知道"当成"安全"就是丢数据的习惯。
    """
    try:
        return os.path.getsize(path) > 0
    except FileNotFoundError:
        return False
    except OSError:
        return True


def _prune_backups(path, keep=BACKUP_KEEP):
    """只留最新 `keep` 份 `<path>.bak-*`，更旧的删掉；返回被删掉的路径列表。

    "新旧"按**文件名里的时间戳**判断（不是 mtime、也不是文件名本身）：
    仓库里 `.bak-` 有**两种真实命名**，都以微秒结尾、都是定宽 ——
      - `snapshot_before_overwrite`：`%Y%m%d%H%M%S%f`（20 位纯数字）
      - `jobdup._snapshot`：`%Y%m%d_%H%M%S_%f`（22 位、带下划线）
    直接比文件名时 `_`(0x5F) 比数字大，**同一天的两种格式会交错**，于是"按名字最小"的那个
    可能并不是最旧的 → 删错一份（`test_rotation_orders_by_timestamp_not_by_filename` 钉住它）。
    去掉分隔符后两者都是同一个 20 位数字串，字符串比较与时间先后一致。
    认不出时间戳的名字（本项目的两个写者都不会产生）**排最后、当成最新**：删不掉只是少腾一点
    空间，删错了就是永久少一份原像（fail-safe）。

    删不掉（被占用/权限）**不抛**：轮转是清理，不该让用户这次写盘因为清理失败而失败
    （与 `_quarantine_report_state` 同一个取舍）。
    """
    prefix = os.path.basename(path) + BACKUP_SUFFIX
    d = os.path.dirname(path) or "."
    try:
        names = [n for n in os.listdir(d) if n.startswith(prefix)]
    except OSError:
        return []

    def sort_key(name):
        digits = re.sub(r"\D", "", name[len(prefix):])
        return (not digits, digits, name)          # 认不出时间戳的排最后（当成最新）

    names.sort(key=sort_key)
    keep = max(0, int(keep))
    removed = []
    for name in names[:max(0, len(names) - keep)]:
        full = os.path.join(d, name)
        try:
            os.remove(full)
        except OSError:
            continue
        removed.append(full)
    return removed


# **公开入口，为 `jobdup` 提供**：它给 `knowledge.json` 与每个面试 `status.json` 留 `.bak-`，
# 也需要"只留最新 N 份"这条轮转（`snapshot_before_overwrite` 只管它自己的目标文件）。
# 跨模块调带下划线的私有名是隐患：store 哪天把它改名/挪走，调用方就是**静默**失效
# （备份无上限增长，没人报错）。这里是同一份实现（不是副本），旧的私有名保留给内部调用。
prune_backups = _prune_backups


def snapshot_before_overwrite(path):
    """覆盖前留档：文件**存在且真的有字节** → 复制成同目录 `<path>.bak-<时间戳>`，返回备份路径。

    为什么要它：`save_jobs` / `save_config` 都是**整份覆盖**。读不出来时上面两道守卫已经拒绝
    写入了，但**读得出来也可能把东西写没**（用户手改过的字段被一次保存抹平、代码 bug、
    把"加载失败兜出来的默认值"当数据写回）—— 覆盖前的那一份是事后唯一能救回来的东西。

    命名沿用仓库既有风格（`jobdup.BACKUP_SUFFIX = ".bak-"`）；时间戳带微秒
    （`%f`）—— 秒级粒度下同一秒内的两次覆盖会互相盖掉备份（`_quarantine_report_state` 同理）。
    按**原始字节**复制（`read_bytes` / `write_bytes`）：`read_text` 会把非 UTF-8 文件兜成空串、
    `write_text` 在 Windows 上还会把 LF 换成 CRLF，那样留下的就不是"覆盖前那一份"了。

    判据是"**真的有字节**"而不是"有可见内容"：纯空白的老台账也是花过钱的分析留下的痕迹。

    **轮转**：留档之后只保留最新 `BACKUP_KEEP`（= 3）份，更旧的删掉（见 `_prune_backups`）。
    为什么必须有：本函数是 `upsert_job` 的必经之路，而 `upsert_job` **每次分析**都走
    —— 连写 6 次就是 6 份；`简历迁移` / `身份迁移` 的整目录备份还会把它们一起拷走，
    profile 目录因此无上限增长。为什么是 3：够退回最近几次覆盖，又不会无限长。

    文件不存在 / 0 字节 → 返回 `None`（合法的第一次，没有旧内容可留）。
    备份**失败就抛**：调用方本来就在写盘路径上，悄悄少一份留档正是
    "以为有兜底、其实没有"那类假安全感（同一目录写不出字节时，紧随其后的写盘多半也会失败，
    早抛比晚抛更清楚）。
    """
    if not file_has_bytes_to_lose(path):
        return None
    backup = "%s%s%s" % (path, BACKUP_SUFFIX, datetime.now().strftime("%Y%m%d%H%M%S%f"))
    write_bytes(backup, read_bytes(path) or b"")
    _prune_backups(path)
    return backup


def knowledge_safe_to_write(path=None):
    """知识库当前是否可以**安全写盘**？返回 `(可写, 原因)`。

    可写 = **读得出来**，**或者**文件里根本没有一个字节（不存在 / 真的是 0 字节）。
    其余（有字节、却读不出来，**含纯空白**）→ 不可写：调用方必须先备份或放弃本次写盘，
    **绝不能**把内存里那份（`load_knowledge()` 给的 `{"nodes": []}`）写回去。

    这是「读不出来 ≠ 空」这条规则**唯一**的实现（`kb` / `jobdup` / `身份迁移` 都用它）。
    注意它与 `read_knowledge_strict` 的分工不同：那个回答"读得出来吗"，
    这个回答"写上去会不会丢东西" —— 0 字节的文件读不出来，但写上去不丢任何东西。

    ⚠️ P0-6：判据从 `content_bytes_present()` 收紧成 `file_has_bytes_to_lose()`。
    老判据对 `b"\\r\\n   \\r\\n"` 这种"有字节但 strip 之后为空"的文件判"没有可丢的内容" ⇒ 可写，
    于是它会被内存里的空库顶掉（实测）。字节意义上它不是空的 —— 那就不能当成"没什么可丢"。
    """
    p = path or knowledge_path()
    _data, err = read_knowledge_strict(p)
    if not err:
        return True, ""
    if not file_has_bytes_to_lose(p):
        return True, ""
    return False, err


def save_knowledge(kb, key=None):
    write_json(knowledge_path(key), kb)


# ---------- report_state（报告专属勾选状态，阶段 3.3）----------
def report_state_dir(key=None):
    return p_path("report_state", key=key)


def report_state_path(report_id, key=None):
    """报告专属勾选状态的文件路径。report_id 非法（空 / . / ..）时返回 ''。

    safe_name 会取 basename，所以 '../../etc/passwd' 被削成 'passwd'，天然挡住越级。
    """
    name = safe_name(report_id)
    if not name:
        return ""
    return os.path.join(report_state_dir(key), name + ".json")


def _clean_checks(checks):
    return {k: bool(v) for k, v in (checks or {}).items() if isinstance(k, str) and k}


def _quarantine_report_state(path):
    """读不出来的状态文件先改名备份，绝不用空状态静默顶掉用户积累。

    判据是「文件有内容 **且** 解析失败」：不存在的文件、以及内容为空都直接放行
    （没有可丢的数据），正常的 {"checks": {}} 解析成功也不会被误备份走。
    "有内容"这件事**必须**走共享原语 `content_bytes_present()`（看原始字节）：内联成
    `read_text(path, "").strip()` 的话，GBK 写坏的状态文件会被解码兜成空串 → 判成
    "没什么可丢" → 不备份、直接覆盖 → 用户勾的"已掌握"永久丢失、盘上无任何副本（实测）。

    时间戳带微秒（%f）：只用秒级粒度时，同一秒内两次隔离会互相覆盖备份。

    这里不记日志：store.py 不 import log（要守住 store 不反依赖 log 的方向），
    日志由调用方（路由）记。改名失败也不阻断本次写入 —— 状态文件只值几个勾，
    为它挡掉用户刚做的勾选不划算（与知识库坏文件的「宁可这次不沉淀」方向不同）。
    """
    if not content_bytes_present(path) or read_json(path, None) is not None:
        return
    backup = "%s.corrupt-%s" % (path, datetime.now().strftime("%Y%m%d%H%M%S%f"))
    try:
        os.replace(path, backup)
    except OSError:
        pass


def load_report_state(report_id, key=None):
    """读报告专属勾选状态。缺失/损坏一律返回空结构，绝不抛异常。"""
    rid = safe_name(report_id)
    path = report_state_path(report_id, key)
    if not path:
        return {"version": 1, "report_id": "", "checks": {}}
    d = read_json(path, None)
    if not isinstance(d, dict):
        return {"version": 1, "report_id": rid, "checks": {}}
    return {"version": d.get("version") or 1, "report_id": rid,
            "checks": _clean_checks(d.get("checks"))}


def save_report_state(report_id, checks, key=None):
    """写报告专属勾选状态。report_id 非法返回 False（路由据此回 400）。"""
    path = report_state_path(report_id, key)
    if not path:
        return False
    _quarantine_report_state(path)
    write_json(path, {"version": 1, "report_id": safe_name(report_id),
                      "checks": _clean_checks(checks)})
    return True


# ---------- interviews（面试记录，阶段 3.4）----------
def interviews_dir(key=None):
    return p_path("interviews", key=key)


def interview_dir(interview_id, key=None):
    name = safe_name(interview_id)
    return os.path.join(interviews_dir(key), name) if name else ""


def interview_file(interview_id, filename, key=None):
    d, n = interview_dir(interview_id, key), safe_name(filename)
    return os.path.join(d, n) if d and n else ""


def save_stream(interview_id, filename, src, max_bytes):
    """把可读对象分块写到面试目录。超限或出错时删掉半截文件并抛异常。返回写入字节数。

    分块（256KB）是为了几十 MB 的录音不吃内存；
    写失败必须清理，否则目录里留下半截文件、下次还会被当成有效录音。
    """
    path = interview_file(interview_id, filename)
    if not path:
        raise ValueError("非法的面试 id 或文件名")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    total = 0
    try:
        with open(path, "wb") as f:
            while True:
                chunk = src.read(256 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError("文件超过上限 %d MB" % (max_bytes // 1024 // 1024))
                f.write(chunk)
    except Exception:
        try:
            os.remove(path)
        except OSError:
            pass
        raise
    return total


def save_interview_json(interview_id, filename, obj, key=None):
    path = interview_file(interview_id, filename, key)
    if not path:
        return False
    write_json(path, obj)
    return True


def load_interview_json(interview_id, filename, default=None, key=None):
    path = interview_file(interview_id, filename, key)
    return read_json(path, default) if path else default
