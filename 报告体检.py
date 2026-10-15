# -*- coding: utf-8 -*-
"""报告体检：扫描/清理报告里的"非 AI 领域来源"，并用当前模板刷新旧报告。

为什么需要它：
  搜索曾经不带领域校验，导致歧义术语搜到无关内容
  （实测：「省略恢复」→「基础：恢复和重做操作 | Microsoft Support」
          「Transformer 架构」→「What is a Transformer?」（电力变压器））。
  代码修好后，**已经生成的报告仍然带着这些旧来源**；重新分析要花钱花额度，
  而过滤本身是纯本地计算——本工具直接就地重写报告里的 REPORT_DATA。

用法：
  python 报告体检.py                 # 只体检，不改任何文件
  python 报告体检.py --fix           # 清理非本领域来源（有改动才自动备份）
  python 报告体检.py --刷新模板      # 用当前模板重渲染（有改动才自动备份）

安全保证：
  - 只读/只改当前 profile 的 reports 目录，绝不碰简历、台账、配置
  - `--fix` / `--刷新模板` 前**有改动才**整目录备份；备份落在**统一备份区**
    `data/profiles/报告备份_<ts>/`（**不在 `reports/` 里** —— 那是被校验的目录）
  - `--fix` 只删「经 knowledge.filter_relevant 判定为非本领域」的来源，
    绝不新增来源、绝不编造内容、绝不改其它字段；
    来源被清空的节点降级为 ai-generated（诚实标注），其余字段原样保留
  - `--刷新模板` 只替换模板骨架，REPORT_DATA 原样保留（不重新分析、不改任何分析结果）；
    渲染结果与盘上那份**逐字相同**时**不写**（真幂等：重复跑不改文件、也不建备份）
"""
import glob
import json
import os
import re
import shutil
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import knowledge  # noqa: E402
import store      # noqa: E402

MARK = "const REPORT_DATA = "
TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "report_template.html")
# 备份目录名前缀（`data/profiles/报告备份_<时间戳>/`）。**唯一一份定义**：
# 备份命名与「拷备份时跳过旧备份」两处都用它（与 `身份迁移.py` / `简历迁移.py` 同一做法）。
BACKUP_PREFIX = "报告备份"


def read_report(path):
    """返回 (全文, 解析出的 dict, dict 在全文中的起止位置)；解析失败返回 None"""
    text = store.read_text(path)
    i = text.find(MARK)
    if i < 0:
        return None
    start = i + len(MARK)
    try:
        data, end = json.JSONDecoder().raw_decode(text, start)
    except ValueError:
        return None
    return text, data, (start, end)


def nodes_of(data):
    kn = data.get("knowledge")
    if isinstance(kn, dict):
        return kn.get("nodes") or []
    if isinstance(kn, list):
        return kn
    return []


def scan(path):
    """体检单份报告 → {total, kept, dropped, nodes_partial, nodes_wiped, samples}"""
    got = read_report(path)
    if not got:
        return None
    _, data, _ = got
    r = {"total": 0, "kept": 0, "dropped": 0, "nodes_partial": 0, "nodes_wiped": 0, "samples": []}
    for n in nodes_of(data):
        src = n.get("sources") or []
        if not isinstance(src, list) or not src:
            continue
        kept = knowledge.filter_relevant(src)
        r["total"] += len(src)
        r["kept"] += len(kept)
        r["dropped"] += len(src) - len(kept)
        if len(kept) == len(src):
            continue
        if kept:
            r["nodes_partial"] += 1
        else:
            r["nodes_wiped"] += 1
        for s in src:
            if not isinstance(s, dict) or s in kept:
                continue
            r["samples"].append((n.get("term"), s.get("title"), s.get("url")))
    return r


def fix(path):
    """就地清理：返回 (改动来源数, 降级节点数)；无改动返回 (0, 0)"""
    got = read_report(path)
    if not got:
        return None
    text, data, (start, end) = got
    dropped = 0
    wiped = 0
    for n in nodes_of(data):
        src = n.get("sources")
        if not isinstance(src, list) or not src:
            continue
        kept = knowledge.filter_relevant(src)
        if len(kept) == len(src):
            continue
        dropped += len(src) - len(kept)
        n["sources"] = kept
        if not kept:
            # 无来源 → 诚实降级；时间线失去支撑时一并清空（本项目"无来源绝不编造"）
            n["confidence"] = "ai-generated"
            if n.get("timeline"):
                n["timeline"] = []
            wiped += 1
    if not dropped:
        return 0, 0
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    store.write_text(path, text[:start] + payload + text[end:])
    return dropped, wiped


def render(path):
    """算出「用当前模板重渲染之后」的全文（**只算不写**）；读不出 REPORT_DATA → `None`。"""
    got = read_report(path)
    if not got:
        return None
    _, data, _ = got
    old = store.read_text(path)
    m = re.search(r"<title>(.*?)</title>", old, re.S)
    title = m.group(1) if m else os.path.basename(path)
    tpl = store.read_text(TEMPLATE)
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return tpl.replace("__TITLE__", title).replace("__REPORT_DATA__", payload)


def rerender(path):
    """用当前模板重新渲染，REPORT_DATA 原样保留（只换骨架，不动分析结果）。

    返回：`True` = 写过了；`False` = **盘上那份已经就是当前模板渲染出来的样子，一个字节都不用写**
    （真幂等：重复跑 `--刷新模板` 不改文件，于是 P0-12 那条"有改动才备份"也就跟着成立）；
    `None` = 读不出 REPORT_DATA（跳过，绝不覆盖）。
    """
    new = render(path)
    if new is None:
        return None
    if store.read_text(path) == new:
        return False
    store.write_text(path, new)
    return True


# ---------------------------------------------------------------- 备份（统一备份区，不在 reports/ 里）
def _dir_stats(path):
    """目录的 `(文件数, 总字节数)`：备份体积必须**如实报给用户**（与两个迁移脚本同一口径）。"""
    files = total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for fn in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, fn))
            except OSError:                    # 数不出来就不数它，别让"报体积"本身失败
                continue
            files += 1
    return files, total


def _human_size(n):
    """人类可读体积（1024 进制；B 不带小数，其余一位小数）。"""
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return ("%d B" % int(size)) if unit == "B" else ("%.1f %s" % (size, unit))
        size /= 1024.0


def _backup_note(path):
    """备份的「在哪、多大、可删」一句话（三个维护脚本同一口径，骨架抄三遍就得更小心）。"""
    files, total = _dir_stats(path)
    return ("%s（%d 个文件，%d 字节 ≈ %s；确认无误后可自行删除）"
            % (path, files, total, _human_size(total)))


def _ignore_previous_backups(_dirpath, names):
    """`shutil.copytree` 的 ignore：**备份里绝不套备份**（与两个迁移脚本同名闸门同一口径）。

    ① 名字以 `报告备份` 开头 → 跳过。这不是假设：真实数据
       `data/profiles/default/reports/` 里就有两个 `报告备份_2026*`，不挡的话每备份一次就把
       上一次的备份再拷一份（滚雪球）。
    ② 还要挡住 `store.snapshot_before_overwrite` / `jobdup._snapshot` 留下的
       `<文件>.bak-<时间戳>` **文件**（`store.BACKUP_SUFFIX`）：它们不是用户的数据，只是覆盖前的
       留档，备份里不需要第二份（留档自身的上限由 `store._prune_backups` 轮转）。
    """
    out = []
    for n in names:
        if n.startswith(BACKUP_PREFIX):
            out.append(n)
        elif store.BACKUP_SUFFIX in n and not os.path.isdir(os.path.join(_dirpath, n)):
            out.append(n)
    return out


def _backup_reports():
    """把整个 `reports/` 拷到**统一备份区** `data/profiles/报告备份_<时间戳>/`，返回备份目录。

    为什么不留在 `reports/报告备份_<ts>/` 里（老实现）：`reports/` 是**被校验的目录**
    （"这次报告有几份 / 有哪些"就是扫它得出的），把备份混进去以后这个数字就永远说不清 ——
    真实数据里已经堆了两个 `报告备份_2026*`，而且每次 `--刷新模板 --fix` 再添一个。
    挪到 `data/profiles/` 是因为那里正是本项目既有的**统一备份区**：`身份迁移.py` 的
    `迁移备份_<ts>/`、`简历迁移.py` 的 `简历迁移备份_<ts>/`、`jddelete.py` 的 `删除备份_<ts>/`
    都在同一层 —— 一个地方就能看全"维护脚本在这台机器上留下了哪些备份"。
    拷贝时按 `_ignore_previous_backups` 跳过旧备份目录与 `.bak-` 留档（备份里不再套备份）。
    """
    src = store.p_path("reports")
    root = store.profiles_root()
    os.makedirs(root, exist_ok=True)
    dest = os.path.join(root, BACKUP_PREFIX + "_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
    if os.path.exists(dest):                     # 同一秒内重跑：补微秒，绝不覆盖上一份
        dest = os.path.join(root, BACKUP_PREFIX + "_"
                            + datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
    shutil.copytree(src, dest, ignore=_ignore_previous_backups)
    return dest


def main():
    do_fix = "--fix" in sys.argv
    do_tpl = "--刷新模板" in sys.argv
    reports_dir = store.p_path("reports")
    files = sorted(glob.glob(os.path.join(reports_dir, "*.html")))
    if not files:
        print("没有找到报告：%s" % reports_dir)
        return 0

    print("profile: %s" % store.current_profile())
    print("reports: %s\n" % reports_dir)

    # ---- 先算「到底要改哪些」，再决定要不要备份（审计 §2 P0-12）----
    # 计划阶段**一个字节都不写**：`scan` / `render` 都只读。两个判据：
    #   * 刷新模板：用当前模板渲染出来的全文与盘上那份**逐字比对**，不同才需要写
    #     （所以"第二次 --刷新模板"既不改文件、也不建备份）；
    #   * 清理来源：`scan` 说有来源会被丢掉才需要写。
    scans = {p: scan(p) for p in files} if do_fix else {}
    tpl_targets = []
    if do_tpl:
        for p in files:
            new = render(p)
            if new is not None and new != store.read_text(p):
                tpl_targets.append(p)
    fix_targets = [p for p in files if (scans[p] or {}).get("dropped")] if do_fix else []
    targets = sorted(set(tpl_targets) | set(fix_targets))

    backup = None
    if (do_fix or do_tpl) and targets:
        backup = _backup_reports()
        print("已备份 reports 整目录：%s" % _backup_note(backup))
        print("  （备份**不在 `reports/` 里** —— 那是被校验的目录，放备份会让「报告有几份」说不清；"
              "统一备份区就在 profile 的兄弟目录下）\n")
    elif do_fix or do_tpl:
        # 与两个迁移脚本同一口径：无事可做的一轮不建备份（备份是回滚的**前提**，没有改动就
        # 无从回滚），一个字节都不写。
        print("本次无需改动：没有任何报告需要刷新模板或清理来源"
              "（本次**未建备份** —— 备份只为回滚改动而建）。\n")

    if do_tpl:
        ok = bad = same = 0
        for p in files:
            try:
                done = rerender(p)
            except Exception as e:
                bad += 1
                print("  ⚠ 失败 %s: %s" % (os.path.basename(p), e))
                continue
            if done is None:
                bad += 1
                print("  ⚠ 跳过（读不出 REPORT_DATA）：%s" % os.path.basename(p))
            elif done:
                ok += 1
            else:
                same += 1
        print("模板刷新完成：成功 %d 份，已是当前模板 %d 份，跳过/失败 %d 份" % (ok, same, bad))
        if backup:
            print("备份保留在 %s\n" % _backup_note(backup))
        if not do_fix:
            return 0

    tot = drop = 0
    changed = []
    for p in files:
        r = scans[p] if do_fix else scan(p)
        name = os.path.basename(p)
        if r is None:
            print("  ⚠ 跳过（读不出 REPORT_DATA）：%s" % name)
            continue
        if r["total"] == 0:
            print("  · %-46s 无来源（纯 AI 整理）" % name[:46])
            continue
        tot += r["total"]
        drop += r["dropped"]
        flag = "⚠ 有跑偏来源" if r["dropped"] else "✓ 干净"
        print("  %s %-44s 来源 %3d | 保留 %3d | 丢弃 %2d | 受影响节点 %d"
              % (flag, name[:44], r["total"], r["kept"], r["dropped"],
                 r["nodes_partial"] + r["nodes_wiped"]))
        if r["dropped"]:
            changed.append(p)
            for term, title, url in r["samples"][:5]:
                print("        ✘ [%s] %s" % (term, (title or "")[:56]))

    print("\n合计：来源 %d 条，其中非 AI 领域 %d 条" % (tot, drop))
    if not do_fix:
        if drop:
            print("\n加 --fix 可零成本就地清理（不重新分析、不消耗搜索额度）。")
        return 0

    print("\n开始清理…")
    total_drop = total_wiped = 0
    for p in changed:
        got = fix(p)
        if got:
            d, w = got
            total_drop += d
            total_wiped += w
            print("  ✓ %-44s 删除 %d 条来源，%d 个节点降级为待查证" % (os.path.basename(p)[:44], d, w))
    print("\n完成：共删除 %d 条非本领域来源，%d 个节点降级为「AI 整理待查证」。" % (total_drop, total_wiped))
    if backup:
        print("备份保留在 %s" % _backup_note(backup))
    return 0


if __name__ == "__main__":
    sys.exit(main())
