# -*- coding: utf-8 -*-
"""简历版本库：族谱（母版 → 子版）、母版快照、取回规则。

**边界**：本模块**只**读写 `resumes.json` 与 `resumes/` 下的文件；
绝不碰 `jobs.json` / `jds.json` / `knowledge.json`（那是 `app.py` 的活）。

磁盘布局（**每个族谱最多 2 份内容文件**）：:

    resumes/<名字>.md          ← 该族谱【最新版】内容（保持现有约定：打包分享等照旧）
    resumes/快照/<版本id>.md    ← 【母版】快照（只在上传子版、覆盖前留一份）

两条最容易写歪的规则（写歪了就是"以为留了档其实没留"）：

1. **快照的唯一触发条件**：当且仅当「母版的内容 == 此刻 ``<名字>.md`` 的内容」
   **且** 快照文件不存在 → 才写。**已存在不重写**（快照一经生成即**不可变**，
   这是它能当"参照原点"的前提）。推论：族谱 ``母(v1) → 子(v2) → 孙(v3)`` 里，
   v3 生成时**不产生新快照、也不重写 v1 的快照**。
   另外，延续**必须沿用族谱的名字**：换了名字，"即将被覆盖的 ``<名字>.md``"
   就不是母亲那一份，条件永远不成立 → 这里直接报错（见 ``create_version``）。

2. ``read_version`` 的四种 ``kind``：``latest``（读 ``<名字>.md``）／
   ``mother``（读快照）／
   ``mismatch``（读到内容了，但**内容与记录里的 ``sha256`` 不符**）／
   ``missing``（既不最新也不是母版的历史版 → 原文没保留）。
   **判定顺序（修 3）**：**先看快照，再看"文件主人"**。快照不可变、正是为
   "``<名字>.md`` 已经不是这一版了"留的；先认主人就会在 ``purge`` 掉最新版之后，
   拿文件里残留的**别人/别版的内容**回报这一版（``mismatch``），而它的原文其实完好地
   躺在快照里。只有**没有快照**时才走"文件主人 + sha 核对"那条路（母版同时是最新版、
   且从未延续过 → 没有快照 → 读最新版文件，仍报 ``latest``）。
   ``missing`` 时 ``text`` 是空串是**事实**，但调用方**必须靠 ``kind`` 分辨**，
   **绝不把空串当成功**（界面据此显示"旧版原文未保留"）。
   ``mismatch`` 与 ``missing`` 是**两回事**：一个"从来没存过"，一个"存过、但磁盘上的
   东西不再是它了"。``mismatch`` 时 ``text`` **照常返回**（让调用方能显示出来对账），
   但 ``kind`` 必须如实说是 ``mismatch``，**不许**谎报 ``latest`` / ``mother``。

诚实边界（规格 §8）：

- **旧版原文不保留**（用户已定的取舍：每个族谱只留母版 + 最新版）→ 历史版只有
  ``kind="missing"``。``ai_diff`` 也只是 AI 写的（Task 4），可能漏，以原文为准。
- ``sha256`` 是**内容指纹**（文本口径，与 ``sha256_of`` / ``_write_content`` 一致）。
  ``read_version`` **自己**用它核对磁盘（快照路径也核 —— 快照不可变，对不上同样是出事了），
  不一致就报 ``mismatch``。**不指望每个调用方都记得比 sha**：一处忘了，就会**静默把别人的
  简历内容当成这一版**返回 —— 这违反"失败绝不静默变成成功"。会不一致的三条真实路径：
  ① 文件被**程序外**改过（手工编辑 / 外部工具 / 别的程序写同名文件）；
  ② ``purge`` 彻底删掉某族谱的最新版后 ``resumes/<名字>.md`` 成**孤儿**，随后
  **另开同名简历**覆盖了它 → 该族谱里仍有记录、仍被 ``_owner_in`` 认作"文件主人"的版本
  会读到**别人的内容**（``kind`` 曾是 ``latest``，sha 却对不上）；
  ③ 手工编辑 ``resumes.json``（记录的 ``sha256`` 与磁盘文件对不上：记录被改了，或文件没跟着改）。
- **调用方约定**：见到 ``mismatch`` 必须显示
  「内容与记录不符（可能被程序外修改过）」，并**把内容与 sha 都亮出来**，
  **绝不静默当正常版本用**（规格 §8.2）。
- 内容文件按 ``newline=""`` 写：磁盘字节 == 内容的 UTF-8 字节（不做 ``\\r\\n`` 转换），
  所以 ``size`` 与 ``sha256`` 严格对应同一份内容，且与仓库里已有的 LF 简历文件一致。
"""
import hashlib
import os
import re
import threading
from datetime import datetime

import store

RESUMES_FILE = "resumes.json"
SNAPSHOT_DIR = "快照"              # resumes/<SNAPSHOT_DIR>/<版本id>.md
REMOVED = "removed"

_AT_FMT = "%Y-%m-%d %H:%M"
_ID_RE = re.compile(r"^rv_(\d+)$")

# 读 → 改 → 写 必须整体串行：两个并发保存各自「读到同一份旧 doc」再各自写回，
# 后写的那次会把先写的记录整条抹掉，而用户在界面上看不出丢了档。
# 用 RLock 而不是 Lock：create_version 内部还要调 load_resumes / save_resumes（同线程可重入）。
_LOCK = threading.RLock()


# ---------- 路径 / 取值小工具 ----------
def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clean_id(value):
    """版本 id 只能是**单个**文件名段 —— 它会被拼进快照路径，不许带路径分隔符。"""
    v = _text(value)
    if not v or v in (".", "..") or os.path.basename(v) != v:
        return ""
    return v


def _resume_rel(name):
    return "resumes/%s.md" % name


def _resume_path(name):
    return store.p_path("resumes", name + ".md")


def _snapshot_rel(version_id):
    return "resumes/%s/%s.md" % (SNAPSHOT_DIR, version_id)


def _snapshot_path(version_id):
    return store.p_path("resumes", SNAPSHOT_DIR, version_id + ".md")


def _doc_path():
    return store.p_path(RESUMES_FILE)


def _write_content(path, text):
    """写简历 / 快照内容文件。

    ``newline=""`` 很关键：默认的换行转换会把 ``\\n`` 写成 ``\\r\\n``（Windows），
    于是"磁盘字节数"与"内容的 UTF-8 字节数"对不上，``size`` / ``sha256`` 也不再是
    同一份内容的指纹。也正因为这样，快照与母版原文是**逐字节**相等的。
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def sha256_of(text):
    """内容的 sha256（小写十六进制）。指纹口径 = 文本的 UTF-8 编码（与 ``_write_content`` 一致）。"""
    t = text if isinstance(text, str) else str(text or "")
    return hashlib.sha256(t.encode("utf-8")).hexdigest()


def _kind_on_disk(text, rec, ok_kind):
    """内容与记录指纹**一致** → 维持 ``ok_kind``（``latest`` / ``mother``）；不一致 → ``mismatch``。

    为什么要在这里核、而不是留给调用方：``sha256`` 只有一个用途 —— 回答"磁盘上这份东西
    还是不是这一版"。把它留给每个调用方各自去比，一处忘了就会**静默把别人的简历内容当成
    这一版**返回（见 ``purge`` 的孤儿文件路径）。所以核对收进库里，``kind`` 如实说话。

    口径与边界：
    - 只比 ``text`` 的 sha（``sha256_of`` 口径 = 文本的 UTF-8 字节）。换行转换不参与判断：
      程序外用 CRLF 重写、而按文本读回来一致的文件**不会**被判 mismatch（``size`` 才会变）。
    - 记录里没有 ``sha256``（人工改坏 / 老记录）→ 没有可比对的依据，只能维持 ``ok_kind``；
      这里的"不知道"绝不冒充"核对通过"，但也绝不凭空喊 mismatch。
    - 文件被删 / 读不出来时 ``store.read_text`` 给空串 → 与记录不符 → ``mismatch``：
      "磁盘上的东西不是它了"包括"东西都没了"，这比谎报 ``latest``（text 为空）诚实。
    """
    expected = _text(rec.get("sha256"))
    if expected and sha256_of(text) != expected:
        return "mismatch"
    return ok_kind


# ---------- 记录字段的容错读取（不在读/写侧"净化"，否则坏形状会被悄悄修好） ----------
def _name_of(rec):
    return store.safe_name(rec.get("name"))


def _root_of(rec):
    return _clean_id(rec.get("root_id")) or _clean_id(rec.get("id"))


def _rev_of(rec):
    return _as_int(rec.get("rev"), 1)


def _is_removed(rec):
    return bool(rec.get(REMOVED))


def _find(recs, version_id):
    vid = _clean_id(version_id)
    if not vid:
        return None
    for r in recs:
        if _clean_id(r.get("id")) == vid:
            return r
    return None


def _max_rev(recs, root_id):
    top = 0
    for r in recs:
        if _root_of(r) == root_id:
            top = max(top, _rev_of(r))
    return top


def _next_id(recs):
    top = 0
    for r in recs:
        m = _ID_RE.match(_clean_id(r.get("id")))
        if m:
            top = max(top, int(m.group(1)))
    return "rv_%04d" % (top + 1)          # 超过 9999 原样变长：绝不把两个版本撞成同一个 id


def _owner_in(recs, root_id):
    """**当前 ``<名字>.md`` 里装的是谁的内容** —— 该族谱 rev 最大的版本（含已移出的）。

    为什么含已移出的：文件是"最新版容器"，移出列表**不删文件**，所以最近创建的那一版
    的内容仍在文件里。``current_of()``（未移出的 rev 最大者）回答的是另一个问题；
    这里用 owner 才不会把"已经不在文件里的那一版"说成 latest。
    """
    owner = None
    for r in recs:
        if _root_of(r) != root_id:
            continue
        if owner is None or _rev_of(r) >= _rev_of(owner):
            owner = r
    return owner


def _current_in(recs, root_id):
    cur = None
    for r in recs:
        if _is_removed(r) or _root_of(r) != root_id:
            continue
        if cur is None or _rev_of(r) > _rev_of(cur):
            cur = r
    return cur


def _mother_in(recs, root_id):
    for r in recs:
        if not _is_removed(r) and _root_of(r) == root_id and bool(r.get("chosen")):
            return r
    return None


def _read_in(recs, version_id):
    """``read_version`` 的本体（复用已加载的 doc，供锁内调用）。

    **顺序是判定的一部分**（修 3）：**先看母版快照，再看"文件主人"**。
    快照是不可变的、且正是为"``<名字>.md`` 已经不是这一版了"留的；先认主人的话，
    ``purge`` 掉最新版之后（文件里留着那一版的内容、主人退到它身上）就会把**别人的内容**
    当成这一版、并以 ``mismatch`` 回报 —— 而这一版的原文其实完好地躺在快照里。
    """
    rec = _find(recs, version_id)
    if rec is None:
        raise ValueError("找不到版本：%s" % (version_id,))
    vid = _clean_id(rec.get("id"))
    # ① 有快照 → 从快照取（快照同样是磁盘文件：它不可变，sha 对不上说明出事了 → 照样 mismatch）
    snap = _snapshot_path(vid)
    if os.path.exists(snap):
        text = store.read_text(snap, "")
        return text, snap, _kind_on_disk(text, rec, "mother")
    # ② 没有快照才认"文件主人"（``<名字>.md`` 里装的是它）
    owner = _owner_in(recs, _root_of(rec))
    if owner is not None and _clean_id(owner.get("id")) == vid:
        path = _resume_path(_name_of(rec))
        text = store.read_text(path, "")
        # 读到了也要**核指纹**：`<名字>.md` 是共享容器，磁盘上的东西可能已经不是这一版了
        # （程序外改过 / purge 留的孤儿被别人覆盖）。不符 → mismatch，绝不谎报 latest。
        return text, path, _kind_on_disk(text, rec, "latest")
    # ③ 原文确实没保留：text 为空是**事实**，path 也**故意**给空串——
    # 免得调用方拿着一个"名义路径"去读，把最新版当成这一版发出去。
    return "", "", "missing"


# ---------- 读写 ----------
def load_resumes():
    """读版本库。垃圾 / 缺字段 → 结构完整但列表空（绝不抛异常，也绝不因读失败去改盘）。"""
    with _LOCK:
        raw = store.read_json(_doc_path(), None)
        version = 1
        items = []
        if isinstance(raw, dict):
            version = _as_int(raw.get("version"), 1)
            if isinstance(raw.get("resumes"), list):
                items = raw["resumes"]
        recs = []
        for r in items:
            if not isinstance(r, dict):
                continue
            # 没有 id / 名字（含带路径分隔符的 id）的记录无法寻址也无法定位文件 → 不进入视图。
            # 只在内存里丢弃，不动盘上的字节（人工还能救）。
            if not _clean_id(r.get("id")) or not _name_of(r):
                continue
            recs.append(dict(r))
        return {"version": version, "resumes": recs}


def save_resumes(doc):
    """原子写（``store.write_json``）。**不**按字段净化记录：写侧的坏形状必须留在盘上可见。"""
    d = doc if isinstance(doc, dict) else {}
    items = d.get("resumes")
    items = items if isinstance(items, list) else []
    out = {"version": _as_int(d.get("version"), 1) or 1,
           "resumes": [r for r in items if isinstance(r, dict)]}
    with _LOCK:
        store.write_json(_doc_path(), out)


def list_versions(include_removed=False):
    with _LOCK:
        recs = load_resumes()["resumes"]
        return [dict(r) for r in recs if include_removed or not _is_removed(r)]


def by_id(version_id):
    with _LOCK:
        return _find(load_resumes()["resumes"], version_id)


def current_of(root_id):
    """该族谱 rev 最大的**未移出**版本（都不在 → None）。"""
    with _LOCK:
        return _current_in(load_resumes()["resumes"], _clean_id(root_id))


def mother_of(root_id):
    """该族谱 chosen 为真的版本（没有 → None）。已移出的不算（移出=不能继续用）。"""
    with _LOCK:
        return _mother_in(load_resumes()["resumes"], _clean_id(root_id))


# ---------- 写入 ----------
def create_version(name, content, parent_id=None, note=""):
    """写一份简历并登记版本记录。

    新开（``parent_id=None``）：独立根，``rev=1``，本族谱的第一个版本。
    延续（``parent_id`` 非空）：同一族谱，覆盖 ``<名字>.md`` **之前**
    按"快照的唯一触发条件"留下母版原文。

    会抛 ``ValueError`` 的情形：名字为空／重名（未移出的）／父版本不存在／
    父版本已移出（移出的族谱不能延续）／延续时改了名字。
    """
    text = content if isinstance(content, str) else str(content or "")
    clean = store.safe_name(name)
    if not clean:
        raise ValueError("简历名字不能为空（也不能是 . / ..）")
    pid = _clean_id(parent_id) or None
    with _LOCK:
        doc = load_resumes()
        recs = doc["resumes"]
        parent = None
        if pid is not None:
            parent = _find(recs, pid)
            if parent is None:
                raise ValueError("找不到父版本：%s" % pid)
            if _is_removed(parent):
                raise ValueError("父版本已移出列表，不能延续（先恢复再延续）")
            if _name_of(parent) != clean:
                raise ValueError("延续时简历名不能改（这一族谱叫「%s」）："
                                 "换了名字，被覆盖的就不是母亲那一份，快照条件永远不成立"
                                 % _name_of(parent))
        new_id = _next_id(recs)
        if parent is not None:
            root_id = _root_of(parent)
            # rev 是**族谱内序号**（规格 §3 从 1 起）：取族谱内最大值 +1，而不是 parent.rev+1。
            # 两者在「母版就是最新版」的常见情形下一致；但族谱里已有 v2 时再从 v1 延续，
            # parent.rev+1 会造出第二个 rev=2，让"谁是文件里那一版"变得不确定。
            rev = _max_rev(recs, root_id) + 1
        else:
            root_id = new_id
            rev = 1
        # name 在**未移出**的简历里必须唯一（绝不静默覆盖别人的文件）；
        # 同族谱的其它版本共用这个名字，不算冲突。
        for r in recs:
            if _is_removed(r) or _name_of(r) != clean:
                continue
            if parent is not None and _root_of(r) == root_id:
                continue
            raise ValueError("已有一份未移出的简历叫「%s」，不能覆盖它（先移出或换个名字）" % clean)
        file_path = _resume_path(clean)
        mother = _mother_in(recs, root_id) if parent is not None else None
        if mother is not None:
            mid = _clean_id(mother.get("id"))
            snap_path = _snapshot_path(mid)
            if not os.path.exists(snap_path) and os.path.exists(file_path):
                m_text, _m_path, m_kind = _read_in(recs, mid)
                # 必须**确定**母版原文才敢写快照：m_kind=="missing" 时读出来是空串，
                # 拿它当快照就是"假装留了档"；m_kind=="mismatch" 时磁盘上的东西已经不是母版
                # 那一版了（程序外改过），照写就是把被改过的内容冒充成"母版原文"存进不可变快照。
                # **"母版原文是不是确定"的责任只在 `_read_in` / `_kind_on_disk`**（shas 不符就报
                # mismatch）—— 这里只认它的结论。此前还在这儿把 `m_text` 与同一个文件比了一遍
                # （`m_text == store.read_text(file_path, "")`：同一个文件读两遍、恒真），
                # 那是"一处忘了比 sha 就静默拿错内容"那条教训的残留，已按规格删掉。
                # 已在盘上的快照一律不重写（不可变）。
                if m_kind in ("latest", "mother"):
                    _write_content(snap_path, m_text)
                    mother["mother_file"] = _snapshot_rel(mid)
        _write_content(file_path, text)
        rec = {
            "id": new_id,
            "name": clean,
            "root_id": root_id,
            "parent_id": pid,
            "rev": rev,
            "at": datetime.now().strftime(_AT_FMT),
            "size": os.path.getsize(file_path),
            "sha256": sha256_of(store.read_text(file_path, "")),
            "file": _resume_rel(clean),
            "mother_file": None,
            "chosen": False,
            "ai_diff": {},                 # Task 4 填：{summary, changes:[{where, what}]}
            "ai_diff_error": "",
            "note": note if isinstance(note, str) else str(note or ""),
            REMOVED: False,
        }
        recs.append(rec)
        save_resumes(doc)
        return dict(rec)


def read_version(version_id):
    """取某一版原文 → ``(text, path, kind)``，``kind ∈ {latest, mother, mismatch, missing}``。

    - ``latest``：这一版就是 ``<名字>.md`` 里那一份（**且内容与记录 sha 一致**）→ 读该文件
      （只在**没有快照**时才会走到这一步）
    - ``mother``：这一版留有快照（**且快照内容与记录 sha 一致**）→ 读快照。
      **快照优先于"文件主人"**（修 3）：``purge`` 掉更新的版本之后，``<名字>.md`` 里留着的
      是那一版的内容，而这一版的原文仍完好地在快照里 —— 先认主人就会拿别人的内容回报它。
    - ``mismatch``：**读到内容了，但它不是这一版** —— ``sha256_of(text) != record["sha256"]``。
      ``text`` / ``path`` **照常返回**（调用方要能把内容亮出来对账），只有 ``kind`` 如实变红。
    - ``missing``：原文没保留 → ``text == ""`` 且 ``path == ""``；
      **调用方必须靠 kind 分辨**，绝不把空串当成功

    为什么 ``mismatch`` 必须独立于 ``missing``：``missing`` 是"**从来没存过**"（历史版按设计
    不留原文），``mismatch`` 是"**存过，但磁盘上的东西不再是它了**"。把后者混进前者，用户会
    以为"这版当初就没留档"；混进 ``latest`` / ``mother``，用户会**拿着别人的简历内容当自己
    这一版**用 —— 静默的错比响的错危险得多。

    会走到 ``mismatch`` 的三条真实路径：
    ① 文件被**程序外**改过（手工编辑、外部工具、别的程序写同名文件）；
    ② ``purge`` 彻底删掉某族谱的最新版后 ``resumes/<名字>.md`` 成**孤儿**，随后**另开同名
       简历**覆盖了它 —— 该族谱里仍有记录、仍被认作"文件主人"的版本就会读到**别人的内容**；
    ③ 手工编辑 ``resumes.json``（记录的 ``sha256`` 与磁盘文件对不上：记录被改了，或文件没跟着改）。

    一个说不清的边界（如实写在 ``_kind_on_disk`` 里）：记录**没有** ``sha256`` 字段时没有可
    比对的依据，只能维持 ``latest`` / ``mother`` —— "不知道"不冒充"核对通过"。

    **调用方约定（T2/T3 照此做）**：见到 ``mismatch`` 必须显示
    「内容与记录不符（可能被程序外修改过）」，并**把内容与 sha 都亮出来**（记录的 vs 实测的），
    **绝不静默当正常版本用**。

    未知 id → ``ValueError``。
    """
    with _LOCK:
        return _read_in(load_resumes()["resumes"], version_id)


def choose(version_id):
    """「选定继续优化」= 标记母简历：同族谱内其它版本一律置 False。"""
    with _LOCK:
        doc = load_resumes()
        recs = doc["resumes"]
        rec = _find(recs, version_id)
        if rec is None:
            raise ValueError("找不到版本：%s" % (version_id,))
        if _is_removed(rec):
            raise ValueError("已移出列表的版本不能选为母简历（先恢复）")
        root_id = _root_of(rec)
        for r in recs:
            if _root_of(r) == root_id:
                r["chosen"] = r is rec
        save_resumes(doc)
        return dict(rec)


def remove(version_id):
    """移出列表（不是删文件）：文件、母版快照、变更说明全部留在盘上。"""
    with _LOCK:
        doc = load_resumes()
        rec = _find(doc["resumes"], version_id)
        if rec is None:
            raise ValueError("找不到版本：%s" % (version_id,))
        rec[REMOVED] = True
        save_resumes(doc)
        return dict(rec)


def remove_current_of(name):
    """按简历**名字**移出它**当前**那一版（解析与移出在**同一个临界区**里，修 7）。

    语义与"先 ``current_of`` 再 ``remove``"完全一样，但**两步不许分开**：分开时中间插进一次
    并发保存，移出的就是**过时的那一版**（用户看到消失的是另一版）。数据不见得丢，但"移出的是
    哪一版"成了错的 —— 这种事在界面上根本看不出来，所以必须由库自己在锁内一次做完。

    名字口径与 ``create_version`` 记录里的 ``name`` 一致（**不含扩展名**：``母版.md`` → ``母版``），
    与"老前端按文件名删"那条路对得上。这个名字没有任何**未移出**的版本 → ``ValueError``。
    """
    with _LOCK:
        doc = load_resumes()
        recs = doc["resumes"]
        want = os.path.splitext(store.safe_name(name))[0]
        target = None
        if want:
            for r in recs:
                if _is_removed(r) or _name_of(r) != want:
                    continue
                # 同一族谱里取 rev 最大的**未移出**版本（= 这个名字当前对应的那一版）
                target = _current_in(recs, _root_of(r)) or r
                break
        if target is None:
            raise ValueError("找不到该简历：%s" % (name,))
        target[REMOVED] = True
        save_resumes(doc)
        return dict(target)


def restore(version_id):
    """放回列表。名字被别的未移出简历占用 → ``ValueError``（要求先改名）。"""
    with _LOCK:
        doc = load_resumes()
        recs = doc["resumes"]
        rec = _find(recs, version_id)
        if rec is None:
            raise ValueError("找不到版本：%s" % (version_id,))
        if _is_removed(rec):
            name, root_id = _name_of(rec), _root_of(rec)
            for r in recs:
                if r is rec or _is_removed(r) or _name_of(r) != name:
                    continue
                if _root_of(r) != root_id:
                    raise ValueError("名字「%s」已被另一份未移出的简历占用，先改名再恢复" % name)
            rec[REMOVED] = False
            save_resumes(doc)
        return dict(rec)


def purge(version_id):
    """彻底删：删记录 + **它自己**的快照文件。**绝不碰**别人的文件（反假绿要求）。

    先落盘记录再删文件：万一写盘失败，快照还在（宁可留多，不可留假）。
    注意：``resumes/<名字>.md`` 是**族谱最新版容器**，本函数不删它 ——
    彻底删掉最新版后，那个文件会留在盘上（孤儿文件，名字在索引里空出来）。
    """
    vid = _clean_id(version_id)
    with _LOCK:
        doc = load_resumes()
        recs = doc["resumes"]
        rec = _find(recs, vid)
        if rec is None:
            raise ValueError("找不到版本：%s" % (version_id,))
        recs.remove(rec)
        save_resumes(doc)
        snap = _snapshot_path(vid)
        try:
            if os.path.exists(snap):
                os.remove(snap)        # 只删 `<版本id>.md` 这一个：它由 id 唯一确定
        except OSError:
            pass
        return dict(rec)
