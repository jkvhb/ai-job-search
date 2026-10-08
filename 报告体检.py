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
  python 报告体检.py --fix           # 清理非本领域来源（先自动备份）
  python 报告体检.py --刷新模板      # 用当前模板重渲染（先自动备份）

安全保证：
  - 只读/只改当前 profile 的 reports 目录，绝不碰简历、台账、配置
  - --fix / --刷新模板 前必定整目录备份
  - --fix 只删「经 knowledge.filter_relevant 判定为非本领域」的来源，
    绝不新增来源、绝不编造内容、绝不改其它字段；
    来源被清空的节点降级为 ai-generated（诚实标注），其余字段原样保留
  - --刷新模板 只替换模板骨架，REPORT_DATA 原样保留（不重新分析、不改任何分析结果）
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


def rerender(path):
    """用当前模板重新渲染，REPORT_DATA 原样保留（只换骨架，不动分析结果）"""
    got = read_report(path)
    if not got:
        return None
    _, data, _ = got
    old = store.read_text(path)
    m = re.search(r"<title>(.*?)</title>", old, re.S)
    title = m.group(1) if m else os.path.basename(path)
    tpl = store.read_text(TEMPLATE)
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    store.write_text(path, tpl.replace("__TITLE__", title).replace("__REPORT_DATA__", payload))
    return True


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

    backup = None
    if do_fix or do_tpl:
        backup = os.path.join(reports_dir, "报告备份_%s" % datetime.now().strftime("%Y%m%d_%H%M%S"))
        os.makedirs(backup, exist_ok=True)
        for p in files:
            shutil.copy2(p, os.path.join(backup, os.path.basename(p)))
        print("已备份到 %s\n" % backup)

    if do_tpl:
        ok = bad = 0
        for p in files:
            try:
                if rerender(p):
                    ok += 1
                else:
                    bad += 1
                    print("  ⚠ 跳过（读不出 REPORT_DATA）：%s" % os.path.basename(p))
            except Exception as e:
                bad += 1
                print("  ⚠ 失败 %s: %s" % (os.path.basename(p), e))
        print("模板刷新完成：成功 %d 份，跳过/失败 %d 份" % (ok, bad))
        print("备份保留在 %s（确认无误后可自行删除）\n" % backup)
        if not do_fix:
            return 0

    tot = drop = 0
    changed = []
    for p in files:
        r = scan(p)
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
    print("备份保留在 %s（确认无误后可自行删除）" % backup)
    return 0


if __name__ == "__main__":
    sys.exit(main())
