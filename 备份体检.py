# -*- coding: utf-8 -*-
"""备份体检：把维护脚本留在 `<profiles 根>` 的整目录备份**列清楚**，并允许**点名删一个**。

为什么要有它（体检报告 §8 结构性第 6 条）：
  本项目有四处会往 `data/profiles/` 下建**整目录备份**（它们是 profile 目录的**兄弟**，
  一个地方就能看全"维护脚本在这台机器上留下了哪些备份"）：

    | 前缀            | 谁建的          | 备份的是什么                                   |
    |-----------------|-----------------|------------------------------------------------|
    | `简历迁移备份_` | `简历迁移.py`   | 整个 profile 目录（**含整份 knowledge.json**） |
    | `迁移备份_`     | `身份迁移.py`   | 同上                                           |
    | `报告备份_`     | `报告体检.py`   | `reports/` 子目录                              |
    | `删除备份_`     | `jddelete.py`   | 真删 JD 前的报告 / 原文 / 几份 JSON + manifest |

  命名规则只有一份实现（`迁移脚手架.backup_dir_name` / `报告体检` / `jddelete` 各自那份
  用的都是同一个格式）：`<前缀>_<YYYYmmdd_HHMMSS>`，**同一秒重名时再补 `_<微秒>`**。
  这些备份**只增不减**（留档的意义就在这），以前只能靠用户自己去目录里翻着删。
  本工具给一个"看得见 + 好删"的入口。

用法：
  python 备份体检.py                    # 只体检：列出每一个备份（名字/体积/文件数/时刻/是谁建的）
  python 备份体检.py --删除 <确切名字>   # **只删点名的这一个**（名字必须与体检列表里的一字不差）

安全保证（这一段比功能本身重要 —— 它会删目录，而它就住在用户数据的隔壁）：
  1. **默认只读**：不带 `--删除` 时一个字节都不写（连一份 `.bak-` 都不会多出来）。
  2. **只删点名的这一个**：`--删除` 必须**逐字**给出体检列表里的那个名字；
     不模糊匹配、不接受通配符、一次只许一个。
  3. **闸门（拒绝的理由都打印出来）**：目标必须同时满足
       ① 名字**逐字**匹配已知备份命名（那 4 个前缀 + **可解析成真实时刻**的时间戳）；
       ② 名字里**没有**路径分隔符、不是绝对路径、不含 `..`（所以"`default`"、
          "`<profiles 根>` 本身"、"`../x`"、"别处的绝对路径"全在这一条上被挡掉）；
       ③ 落点确实是 `<profiles 根>` **正下方**（`commonpath` 再判一次）；
       ④ 存在、是**目录**、且**不是**链接（符号链接 / junction 顺着删会删到别处去）。
     任何一条不满足 ⇒ **拒绝 + 非零退出**，并且一个字节都不动。
  4. **绝不自动清理**：没有"清理全部 / 按时间删旧的 / 只留最新 N 份"这类选项
     （那是拿留档换空间 —— 而留档的用处正是"出事之后还能回滚"）。
  5. `<profile>/` 里的 `<文件>.bak-<时间戳>`（`store` / `jobdup` 的覆盖前留档）与
     `resumelib` 的 `<版本id>.md.stale-<8位>` 旁挂**也在本工具的视野里**（`nested_notes`），
     但它们**不在可删清单里**：前者由 `store.BACKUP_KEEP` 自己轮转，后者是 append-only 的留档。

⚠️ 本模块**只** import 标准库与 `store`（用它算 `<profiles 根>` 与当前档名）。
   绝不 import 那四个维护脚本：它们互相 import 会让"谁是入口"变模糊，也会把
   迁移/删除那一整套依赖拖进一个只读工具里。
"""
import os
import re
import shutil
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    # 输出全是中文（还有 ⇒ / ｜ 这些符号）：重定向到管道时按 UTF-8 写，别让它们变成转义串
    # （与 `打包分享.py` 同一做法；控制台直接跑时本来就走 Windows 的控制台 API，不受影响）。
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import store  # noqa: E402

# ---------------------------------------------------------------- 备份命名（唯一一份判断依据）
# 每一种：前缀 → 它是谁建的、备份的是什么。**扫描、可读时间、安全闸门全都读这张表**，
# 所以"新增一个会建备份的脚本"只需要在这里加一行（别处再抄一遍就会漏一处）。
BACKUP_KINDS = (
    ("简历迁移备份", "简历迁移.py 建的整目录备份（含整份 knowledge.json，可达 10.8MB）"),
    ("迁移备份", "身份迁移.py 建的整目录备份（含整份 knowledge.json）"),
    ("报告备份", "报告体检.py 建的 reports/ 整目录备份"),
    ("删除备份", "jddelete.py 在真删 JD 之前留的备份（报告 / 原文 / 几份 JSON + manifest）"),
)
BACKUP_PREFIXES = tuple(p for p, _ in BACKUP_KINDS)
_KIND_BY_PREFIX = dict(BACKUP_KINDS)

# 时间戳：`YYYYmmdd_HHMMSS`，同一秒重名时再补 `_<微秒>`（两段/三段都认）。
_TS_PATTERN = r"\d{8}_\d{6}(?:_\d{6})?"
_ALT = "|".join(re.escape(p) for p in BACKUP_PREFIXES)
# **严格**：前缀 + 数字形状的时间戳（还要 `parse_ts` 认得出是真实时刻才算数）。
_NAME_RE = re.compile(r"^(%s)_(%s)$" % (_ALT, _TS_PATTERN))
# **宽松**（只用来解释"为什么它不算备份"）：前缀对，后面只要以数字开头就当成"形状像"。
_LOOKS_RE = re.compile(r"^(%s)_(\d[\d_]*)$" % _ALT)
# 先长后短：`_<微秒>` 的名字必须先按长格式试，否则会被短格式错判。
_TS_FORMATS = ("%Y%m%d_%H%M%S_%f", "%Y%m%d_%H%M%S")

# 旁挂留档的名字标记（只用于"看得见"，**不可删**）。
BAK_SUFFIX = store.BACKUP_SUFFIX      # `.bak-`：store / jobdup 的覆盖前留档
STALE_MARK = ".stale-"                # resumelib 的旧快照旁挂留档


# ---------------------------------------------------------------- 名字 → 它是什么
def parse_ts(stamp):
    """`<YYYYmmdd_HHMMSS[_微秒]>` → `datetime`；认不出 → `None`（**绝不拿当前时间冒充**）。"""
    for fmt in _TS_FORMATS:
        try:
            return datetime.strptime(str(stamp or ""), fmt)
        except ValueError:
            continue
    return None


def readable_ts(stamp):
    """时间戳 → `YYYY-MM-DD HH:MM:SS`（人话）；认不出 → 空串。"""
    dt = parse_ts(stamp)
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else ""


def backup_kind(name):
    """这个名字是不是**已知的**整目录备份？返回 `(前缀, 它是什么)`；不是 → `None`。

    判据三合一：前缀在那 4 个里 + 时间戳**形状**对 + 时间戳能解析成**真实时刻**。
    最后那半条不是洁癖：`报告备份_20261399_999999` 的"月份/日期"是假的，
    它不是任何脚本建得出来的东西 —— 名字对不上就**不许进可删清单**。
    """
    m = _NAME_RE.match(str(name or ""))
    if not m or not parse_ts(m.group(2)):
        return None
    return m.group(1), _KIND_BY_PREFIX[m.group(1)]


def why_not_backup(name):
    """这个名字为什么**不算**已知备份？返回一句给用户看的原因（**只判名字，不碰盘**）。

    合法备份名也返回空串（"名字这一关过了"）—— 是不是目录、存在不存在由调用方另判。
    """
    name = str(name or "")
    if not name:
        return "名字是空的"
    strict = _NAME_RE.match(name)
    if strict and parse_ts(strict.group(2)):
        return ""
    loose = _LOOKS_RE.match(name)
    if loose:
        wrong = strict.group(2) if strict else loose.group(2)
        return ("时间戳部分「%s」不成形（要求 `YYYYmmdd_HHMMSS`，同一秒重名时再补 `_<微秒>`，"
                "而且必须是一个**真实时刻**）" % wrong)
    return "前缀不是维护脚本建备份用的那 4 个（%s）" % " / ".join(BACKUP_PREFIXES)


def looks_like_backup(name):
    """名字**像**备份（前缀对 + 后面以数字开头）？只看名字，不碰盘。"""
    return bool(_LOOKS_RE.match(str(name or "")))


# ---------------------------------------------------------------- 体积（人话）
def _size(path):
    """单文件字节数；数不出来 → `None`（"数不出来"绝不当成 0）。"""
    try:
        return os.path.getsize(path)
    except OSError:
        return None


def dir_stats(path):
    """目录的 `(文件数, 总字节数)`：备份体积必须**如实报给用户**（不许靠猜）。"""
    files = total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for fn in filenames:
            size = _size(os.path.join(dirpath, fn))
            if size is None:                  # 数不出来就不数它，别让"报体积"本身失败
                continue
            files += 1
            total += size
    return files, total


def human_size(n):
    """人类可读体积（1024 进制；B 不带小数，其余一位小数）。"""
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return ("%d B" % int(size)) if unit == "B" else ("%.1f %s" % (size, unit))
        size /= 1024.0


# ---------------------------------------------------------------- 只读：扫描
def scan(root=None):
    """**只读**地列出 `<profiles 根>` 正下方的每一个整目录备份。

    返回 `[{name, path, prefix, kind, stamp, ts, files, bytes, size}, …]`，按名字排序。

    只看**顶层**、只收**目录**：那四个脚本建的都是 `<profiles 根>/<前缀>_<时间戳>/`，
    是 profile 目录的兄弟。**profile 目录本身、`<profiles 根>` 本身、无关文件一律不进这个
    列表** —— 这个列表就是"可以删的那些"，混进用户数据本体是最贵的错。
    """
    root_abs = os.path.abspath(root or store.profiles_root())
    out = []
    try:
        names = sorted(os.listdir(root_abs))
    except OSError:
        return out
    for name in names:
        hit = backup_kind(name)
        if not hit:
            continue
        full = os.path.join(root_abs, name)
        if not os.path.isdir(full):
            continue
        prefix, kind = hit
        files, total = dir_stats(full)
        stamp = _NAME_RE.match(name).group(2)
        out.append({"name": name, "path": full, "prefix": prefix, "kind": kind,
                    "stamp": stamp, "ts": readable_ts(stamp),
                    "files": files, "bytes": total, "size": human_size(total)})
    return out


def nested_notes(root=None):
    """**只读**地数一遍"不在 `<profiles 根>` 顶层的那些留档"（它们**不在**可删清单里）。

    为什么要数：用户翻目录时看不到这些名字的意义，而它们确实占地方、也确实在增长——
      * `<文件>.bak-<时间戳>`：`store.snapshot_before_overwrite` / `jobdup._snapshot` 在**覆盖
        之前**留的档（`jobs.json` / `knowledge.json` / `config.json` / 面试 `status.json`）。
        它**自带轮转**（每个目标只留最新 `store.BACKUP_KEEP` 份），所以本工具只报告、不代删。
      * `<版本id>.md.stale-<8位>`：`resumelib` 的旧快照**旁挂留档**，append-only
        （它自己说明"可手工清理"）；删它需要读懂版本族谱，不是这个工具的活。
      * 嵌在 `<档>/reports/` 里的旧形态 `报告备份_<ts>/`：`报告体检.py` **老实现**留下的，
        数量有限（真实数据里 2 个），本工具只把它们**数出来**、指出"它们不是顶层备份"。

    返回 `[{"kind", "count", "bytes", "where"}, …]`；没有就返回空列表。
    """
    root_abs = os.path.abspath(root or store.profiles_root())
    bak_files = bak_bytes = stale_files = stale_bytes = 0
    nested_dirs = []
    try:
        names = sorted(os.listdir(root_abs))
    except OSError:
        return []
    for name in names:
        top = os.path.join(root_abs, name)
        if not os.path.isdir(top) or backup_kind(name):
            continue                      # 顶层备份本身是"可删清单"里的事，这里只管它的里面
        for dirpath, dirnames, filenames in os.walk(top):
            for d in dirnames:
                if backup_kind(d):
                    nested_dirs.append(os.path.join(dirpath, d))
            for fn in filenames:
                size = _size(os.path.join(dirpath, fn)) or 0
                if BAK_SUFFIX in fn:
                    bak_files += 1
                    bak_bytes += size
                elif STALE_MARK in fn:
                    stale_files += 1
                    stale_bytes += size
    out = []
    if bak_files:
        out.append({"kind": "`<文件>%s<时间戳>`：覆盖前留档（store / jobdup，自带轮转）" % BAK_SUFFIX,
                    "count": bak_files, "bytes": bak_bytes, "where": "各个 profile 目录里"})
    if stale_files:
        out.append({"kind": "`<版本id>.md%s<8位>`：旧快照旁挂留档（resumelib，append-only）"
                            % STALE_MARK,
                    "count": stale_files, "bytes": stale_bytes, "where": "各个 profile 目录里"})
    if nested_dirs:
        out.append({"kind": "`报告备份_<时间戳>`：**老实现**嵌在 `reports/` 里的历史备份（不是顶层备份）",
                    "count": len(nested_dirs), "bytes": 0,
                    "where": os.path.dirname(nested_dirs[0])})
    return out


# ---------------------------------------------------------------- 安全闸门（最重要的一段）
def check_deletable(name, root=None):
    """闸门：`(可删的绝对路径 | None, 拒绝原因)`。**原因非空 = 拒绝，一个字节都不许动**。

    逐条挡的是这些形态（每一条都对应一种"删到用户数据本体"的真实路径）：
      1. 空名字 / 没给名字（本工具**只删点名的那一个**，没有"清理全部"）；
      2. 名字里有 `/` 或 `\\`、是绝对路径、带盘符、是 `.` / `..`、含 `..`
         ⇒ 挡掉 `<profiles 根>` 本身、`../x`、以及"指向别处的绝对路径"；
      3. 名字**逐字**匹配已知备份命名（前缀 + 可解析的真实时间戳）；
         它同时挡掉 `default` 与**任何 profile 目录**（它们本来就不长这个样子）；
      4. 落点确实在 `<profiles 根>` 正下方（`commonpath` + `dirname` 各判一次）；
      5. 不是符号链接 / junction（顺着链接删，删掉的是链接**指向**的那个目录）；
      6. 存在、且是**目录**（那四个脚本建的都是目录；名字像备份的文件不是备份）。
    """
    root_abs = os.path.abspath(root or store.profiles_root())
    if not isinstance(name, str) or not name:
        return None, ("没有给出要删的名字 —— 本工具**只删点名的那一个**"
                      "（没有「清理全部」「删旧的」这类选项：留档的意义就在这）")

    sep = "/" if "/" in name else ("\\" if "\\" in name else "")
    if sep or os.path.isabs(name) or os.path.splitdrive(name)[0] \
            or name in (os.curdir, os.pardir) or ".." in name:
        return None, ("「%s」里有路径分隔符 / 是绝对路径 / 含 `..` —— **拒绝**："
                      "本工具只认 `<profiles 根>` **正下方**、名字**逐字**给出的那一个备份，"
                      "绝不接受任何路径形式（那正是「删到别处」的口子）。" % name)

    if not backup_kind(name):
        why = why_not_backup(name)
        if os.path.isdir(os.path.join(root_abs, name)):
            return None, ("「%s」是 `<profiles 根>` 下的一个**目录**，但它不是已知备份命名"
                          "（%s）—— 它极可能是 **profile 目录（用户数据本体）**。**拒绝**。"
                          % (name, why))
        return None, ("「%s」不是已知的备份命名（%s）。**拒绝**："
                      "只有能证明是维护脚本建的那一个才可删。" % (name, why))

    full = os.path.join(root_abs, name)
    try:
        inside = os.path.commonpath([root_abs, os.path.abspath(full)]) == root_abs
    except ValueError:                    # 不同盘符
        inside = False
    if not inside or os.path.dirname(os.path.abspath(full)) != root_abs:
        return None, ("「%s」的落点不在 `<profiles 根>` 正下方（%s）—— **拒绝**。"
                      % (name, root_abs))

    is_link = os.path.islink(full)
    if not is_link and hasattr(os.path, "isjunction"):
        try:
            is_link = bool(os.path.isjunction(full))
        except OSError:
            is_link = True                # 问不出来就当它是链接：宁可拒绝
    if is_link:
        return None, ("「%s」是指向别处的链接（符号链接 / junction）—— **拒绝**："
                      "顺着它删会把链接**指向**的那个目录里的东西删掉，而那个目录不在这个根里。"
                      % name)
    if not os.path.exists(full):
        return None, "备份不存在：%s（**一个字节都没删**）" % full
    if not os.path.isdir(full):
        return None, ("「%s」确实存在，但**不是目录** —— 本工具只删整目录备份"
                      "（那四个脚本建的都是目录）。" % name)
    return full, ""


# ---------------------------------------------------------------- 体检报告
def _profile_note(root):
    """`(当前档名, 它在 root 下的目录)`；档名读不出来 → `(空, 空)`（**绝不瞎猜成 default**）。

    为什么重要：`<profiles 根>` 下的每个 profile 目录都是**用户数据本体**，
    它们与备份**住在一起**、名字也很像 —— 报告里必须点名"当前这个是谁、它不在可删清单里"。
    """
    try:
        key = store.current_profile()
    except Exception:
        return "", ""
    return key, os.path.join(root, key)


def _report(root):
    """只体检：列出每一个备份 + 合计 + 当前 profile 的提示。**一个字节都不写**，返回 0。"""
    print("=" * 66)
    print("  备份体检：`<profiles 根>` 下的整目录备份")
    print("=" * 66)
    print("  profiles 根：%s" % root)
    key, pdir = _profile_note(root)
    if key:
        print("  当前 profile：%s" % key)
        print("               %s" % pdir)
    else:
        print("  当前 profile：**读不出来**（档名文件 `data/current_profile.txt` 有问题）")
    print("  ⚠ 当前 profile 目录是**用户数据本体**，它**不在**下面这份可删清单里。\n")

    items = scan(root)
    if not items:
        print("  没有发现任何整目录备份（说明维护脚本还没在这台机器上留过备份）。")
    else:
        print("  %-32s ｜ %9s ｜ %5s ｜ %s ｜ %s"
              % ("名字", "体积", "文件数", "备份时刻", "它是什么"))
        print("  " + "-" * 108)
        for e in items:
            print("  %-32s ｜ %9s ｜ %5d ｜ %s ｜ %s"
                  % (e["name"][:32], e["size"], e["files"], e["ts"], e["kind"]))
        total = sum(e["bytes"] for e in items)
        print("\n  合计：%d 个备份，%s（%d 个文件）"
              % (len(items), human_size(total), sum(e["files"] for e in items)))

    # 名字像备份、却不是备份的：**名字一个都不许被误当成可删目标**，所以单独说明。
    listed = set(e["name"] for e in items)
    try:
        names = sorted(os.listdir(root))
    except OSError:
        names = []
    skipped = [n for n in names if n not in listed
               and (_NAME_RE.match(n) or looks_like_backup(n))]
    if skipped:
        print("\n  ⚠ 下面这些**名字像备份、但不是**（本工具一个字都不动）：")
        for n in skipped:
            why = why_not_backup(n) or "名字合法，但它不是一个目录"
            print("     - %s：%s" % (n, why))

    notes = nested_notes(root)
    if notes:
        print("\n  另有几类留档**不在可删清单里**（只报个数，本工具不代删）：")
        for n in notes:
            extra = ("，共 %s" % human_size(n["bytes"])) if n["bytes"] else ""
            print("     - %s ｜ %d 个%s ｜ %s" % (n["kind"], n["count"], extra, n["where"]))

    print("\n  要删哪一个：`python 备份体检.py --删除 <上面列出的确切名字>` —— **只删点名的那一个**。")
    print("  本工具**没有**「清理全部」「按时间删旧的」这类选项：备份只增不减，删不删由你决定。")
    return 0


# ---------------------------------------------------------------- 点名删一个
def _delete_one(name, root):
    """删**点名的那一个**：先过闸门、先打印"将删除"，再动手。返回退出码。"""
    full, why = check_deletable(name, root)
    if why:
        print("✘ 拒绝删除：%s" % why)
        print("  **一个字节都没删。**")
        return 1
    files, total = dir_stats(full)
    print("将删除：%s｜%s｜%d 个文件" % (full, human_size(total), files))
    try:
        shutil.rmtree(full)
    except OSError as e:
        print("✘ 删除失败：%s" % e)
        print("  盘上可能还留着半截，请手工确认后再试；别的备份与 profile 目录没有被连坐。")
        return 1
    print("✔ 已删除：%s（%d 个文件，%s）" % (name, files, human_size(total)))
    print("  别的备份、以及**当前 profile 目录**，一个字节都没动。")
    return 0


# ---------------------------------------------------------------- 入口
def main(argv=None, root=None):
    """`argv` 默认取 `sys.argv[1:]`；`root` 默认走 `store.profiles_root()`（**不写死字符串**）。

    返回退出码：体检成功 / 点名删除成功 → 0；目标不存在、被闸门拒绝、参数不认识 → 1。
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    root_abs = os.path.abspath(root or store.profiles_root())
    target = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--删除":
            if i + 1 >= len(argv):
                print("✘ 拒绝：`--删除` 后面必须**逐字**跟一个备份名字（只删点名的那一个）。")
                return 1
            if target is not None:
                print("✘ 拒绝：`--删除` 只允许出现一次、只允许点名**一个**备份。")
                return 1
            target = argv[i + 1]
            i += 2
            continue
        print("✘ 拒绝：不认识的参数「%s」。" % a)
        print("  用法：python 备份体检.py                    # 只体检（一个字节都不改）")
        print("        python 备份体检.py --删除 <确切名字>   # 只删那一个")
        print("  本工具**没有**「清理全部」「按时间删旧的」这类选项：留档的意义就在这。")
        return 1
    if target is None:
        return _report(root_abs)
    return _delete_one(target, root_abs)


if __name__ == "__main__":
    sys.exit(main())
