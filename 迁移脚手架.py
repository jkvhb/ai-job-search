# -*- coding: utf-8 -*-
"""迁移脚手架：维护脚本共用的「整目录备份 / 回滚恢复」骨架（**唯一一份实现**）。

为什么有它（体检报告批 5 §7.2）：`简历迁移.py` 与 `身份迁移.py` 的这套骨架
（`_dir_stats` / `_human_size` / `_backup_note` / `_profile_files` / `_ignore_previous_backups` /
`_backup_profile` / `_restore_profile`）原先**一处一处抄了两遍**，两边唯一的差别是
`_restore_profile` 收尾那圈多一句 `if dirpath == root: continue` —— 而那句正是体检 §2 P0-14 的
真缺陷（失败回滚时把 profile 根目录 `rmdir` 掉，用户下一步就是重试，等于"失败路径上再丢一次"）。
抄两遍的代价不是"代码丑"，是**下一次改一处、漏一处**：这次靠人眼对出来的差异，下次不会。
所以：**一份实现 + 两边跑同一套差分测试**（`tests/test_resume_migration.py` 的
`SharedScaffoldDifferentialTest`）。**改这里的任何一行，都同时改两个脚本的行为。**

契约（两个脚本的行为逐字一致；要改就以本文件为准）：
  1. 备份目录名 = `<前缀>_<YYYYmmdd_HHMMSS>`；同一秒内重名时追加 `_<微秒>`（见 `backup_dir_name`）。
     **前缀由调用方给**：`简历迁移备份` / `迁移备份` 是**故意不同**的（用户要能分开看哪次备份
     是什么、属于哪件事），但**命名规则本身只有这一处** —— 时间戳格式、分隔符、重名兜底
     全在 `backup_dir_name` 里，一个字符都不许两边各写一遍。
  2. 备份是**拷贝**（不是移动）：profile 目录在迁移过程中必须一直可读（`store` 的任何读路径
     都会当场坏掉），而且"备份"本来就该是"原地不动的那一份"。备份**保留不删**。
  3. 「备份里绝不套备份」：① 跳过调用方前缀的旧备份**目录**；② 跳过 `store.BACKUP_SUFFIX`
     （`.bak-`）的**文件**留档。少跳一样，profile 目录就会无上限增长（`.bak-` 被备份再备份，
     真实 profile 里现在就有 `jobs.json.bak-*` / `knowledge.json.bak-*`，这不是假设）。
  4. 恢复 = 把备份里的文件写回 + **删掉迁移新建的文件**（备份里没有的一律不留下）+
     只删**自己新建的**空目录；**profile 根目录永远不删**（P0-14）。
     「备份里就有的空目录」是用户原来的形状（普通文件复制表达不出来），必须重建、绝不许删。
  5. `prefix` 一律是**第一个位置参数**：脚本用 `functools.partial` 把它绑死，于是
     `shutil.copytree(ignore=...)` 仍按 `(dirpath, names)` 两个参数调用，脚本里那几个私有名字
     （`_backup_profile()` / `_restore_profile(backup, had_files)`）的调用形状一个字没变。

只依赖标准库与本项目的 `store`（`profile_dir` / `profiles_root` / `BACKUP_SUFFIX`）；
**绝不 import 那两个脚本**（脚本互相 import 会让"谁是入口"变模糊），也绝不 import `app.py`。

⚠ 施工范围说明：`报告体检.py` 里还有一份**同类**闸门（`报告备份`），它备份的是 `reports/`
子目录、不带 `_restore_profile`，本轮（批 5 §7.2）未合并 —— 将来合并时以本文件为准。
"""
import functools
import os
import shutil
from datetime import datetime

import store

# 备份目录名的时间戳格式：`<前缀>_<YYYYmmdd_HHMMSS>`；同一秒重名时再加 `_<微秒>`。
BACKUP_NAME_FMT = "%Y%m%d_%H%M%S"
BACKUP_NAME_FMT_US = "%Y%m%d_%H%M%S_%f"


def dir_stats(path):
    """目录的 `(文件数, 总字节数)`：备份体积必须**如实报给用户**（不许靠猜，也不许只说路径）。"""
    files = total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for fn in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, fn))
            except OSError:                    # 数不出来就不数它，别让"报体积"本身失败
                continue
            files += 1
    return files, total


def human_size(n):
    """人类可读体积（1024 进制；B 不带小数，其余一位小数）。"""
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return ("%d B" % int(size)) if unit == "B" else ("%.1f %s" % (size, unit))
        size /= 1024.0


def backup_note(path):
    """备份的「在哪、多大、可删」一句话（三个维护脚本同一口径，骨架抄三遍就得更小心）。"""
    files, total = dir_stats(path)
    return ("%s（%d 个文件，%d 字节 ≈ %s；确认无误后可自行删除）"
            % (path, files, total, human_size(total)))


def profile_files(root):
    """目录下所有文件的相对路径（含子目录）"""
    out = set()
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            out.add(os.path.relpath(os.path.join(dirpath, fn), root))
    return out


def ignore_previous_backups(prefix, dirpath, names):
    """`shutil.copytree` 的 ignore：**备份里绝不套备份**（拷一次就多 MB 级、再跑就滚雪球）。

    备份落在 `data/profiles/<前缀>_<ts>/`，是 profile 目录的**兄弟**，真布局下本来就拷不进去；
    这道闸门钉的是**不变量**：备份根将来挪进 profile（或用户手工搬进来）也不会滚雪球。
    （已用隔离副本实测：新备份顶层没有旧备份目录。）

    ⚠️ 还要挡住 `store.snapshot_before_overwrite` / `jobdup._snapshot` 留在 profile 里的
    `<文件>.bak-<时间戳>` **文件**：原来的闸门只看**目录**前缀，这些文件照拷不误 ⇒
    每备份一次就把上一次的留档再拷一份，profile 目录无上限增长（`.bak-` 被备份再备份）。
    它们不是用户的数据（是覆盖前的留档），备份里不需要第二份；留档自身的上限由
    `store._prune_backups` 轮转（每份目标文件只留最新 3 份）。

    `prefix` 放第一个位置参数：调用方用 `functools.partial` 绑死它，这里仍是
    `shutil.copytree` 要的 `(dirpath, names)` 两参形状。
    """
    out = []
    for n in names:
        if n.startswith(prefix):
            out.append(n)
        elif store.BACKUP_SUFFIX in n and not os.path.isdir(os.path.join(dirpath, n)):
            out.append(n)
    return out


def backup_dir_name(prefix, when=None, micro=False):
    """备份目录名：`<prefix>_<YYYYmmdd_HHMMSS>`；`micro=True` 时用 `_<微秒>` 兜重名。

    `when` 只为测试**钉住一个确定的时刻**（生产恒为 `None` = 调用时的当前时刻）；一次调用只取
    一次时刻，所以"重名就换名"那条分支不会跨秒（跨秒会拼出两个不同时刻的时间戳）。
    """
    when = when or datetime.now()
    return prefix + "_" + when.strftime(BACKUP_NAME_FMT_US if micro else BACKUP_NAME_FMT)


def backup_profile(prefix):
    """把 `data/profiles/<key>/` **整目录拷贝**到 `data/profiles/<prefix>_<时间戳>/`，返回备份目录。

    **拷贝而不是移动**：整目录移动会让 `profile_dir()` 在迁移过程中不存在（`store` 的任何读
    路径与测试都会当场坏掉），而且"备份"的语义本来就该是"原地不动的那一份"——用户翻
    `data/profiles/<key>/` 时报告仍然在那里。备份**保留不删**（确认无误后用户自行删除）。

    拷贝时按 `ignore_previous_backups` 跳过旧备份目录与 `.bak-` 留档：备份里不再套备份。
    """
    src = os.path.abspath(store.profile_dir())
    if not os.path.isdir(src):
        raise RuntimeError("profile 目录不存在：%s" % src)
    root = store.profiles_root()
    os.makedirs(root, exist_ok=True)
    dest = os.path.join(root, backup_dir_name(prefix))
    if os.path.exists(dest):
        dest = os.path.join(root, backup_dir_name(prefix, micro=True))
    shutil.copytree(src, dest, ignore=functools.partial(ignore_previous_backups, prefix))
    return dest


def restore_profile(backup, had_files):
    """从备份恢复：把备份里的文件写回，并**删掉迁移新建的东西**（备份里没有的一律不留下）。

    `had_files` 是迁移前该目录的全部文件（相对路径）。恢复后凡是不在 `had_files` 里的文件
    都被删掉 —— 这正是「失败 → 从备份恢复」的严格含义（不留半改的、不留新造的）。

    **空目录也要还原**：`os.walk` 会把空目录也走一遍，所以上面那圈会把 `copytree` 拷过来、
    却一个文件都没有的目录重建出来；收尾那圈只删**备份里没有**的空目录
    （"备份里有的空目录"是用户原来的形状，普通文件复制表达不出来，只能这样补）。

    ⚠️ **profile 根目录永远不删**（`if dirpath == root: continue`）：恢复成空目录树时，收尾那圈
    会把根目录 `rmdir` 掉，而此刻脚本**正在返回错误码**、用户下一步就是重试 ——
    那是「失败路径上再丢一次」（体检 §2 P0-14）。
    ⚠️ 另外**只删自己新建的**空目录：用户 profile 里原本就有的空目录（`jd/` 这种）不是本次迁移
    造出来的东西，删掉就是丢用户原来的形状（`backup_dirs` 那条判据挡的就是它）。
    """
    root = os.path.abspath(store.profile_dir())
    backup_dirs = set()
    for dirpath, _dirnames, filenames in os.walk(backup):
        rel = os.path.relpath(dirpath, backup)
        rel = "" if rel == "." else rel
        backup_dirs.add(rel)
        dest_dir = root if not rel else os.path.join(root, rel)
        if not os.path.isdir(dest_dir):
            os.makedirs(dest_dir, exist_ok=True)
        for fn in filenames:
            shutil.copy2(os.path.join(dirpath, fn), os.path.join(dest_dir, fn))
    for rel in sorted(profile_files(root) - set(had_files), reverse=True):
        try:
            os.remove(os.path.join(root, rel))
        except OSError:
            pass
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        if dirpath == root:
            continue                           # 根目录永远不删（P0-14）
        rel = os.path.relpath(dirpath, root)
        if rel not in backup_dirs and not dirnames and not filenames:
            try:
                os.rmdir(dirpath)
            except OSError:
                pass
