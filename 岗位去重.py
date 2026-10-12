# -*- coding: utf-8 -*-
"""岗位去重（维护脚本）：体检疑似「同一份 JD」的岗位 + 确认后合并。

为什么需要它：
  「同一份 JD 被分析两次」会把同一个概念在知识库里记成「被 2 个岗位提到」，而
  `multi_jd`（被多少岗位提到）是复习优先级的**乘性**因子 —— 它会让用户现在就能看到的
  排序静默算错，而且同一份 JD 反复重分析会越叠越多。
  分析前查重（app.py）只拦将来的；**已经写进库里的**用这个脚本体检 + 合并修复。

用法：
  python 岗位去重.py                             # 只列疑似重复的岗位对，**不改任何文件**
  python 岗位去重.py --合并 <keep_id> <drop_id>   # 真的合并（先备份；keep 通常是更早那条）

安全保证：
  - **无参数时严格只读**：不写台账 / 知识库 / 日志 / 备份，一个字节都不写
  - 判定只认 JD 原文（`data/profiles/<profile>/jd/<岗位id>.txt`），缺原文的岗位跳过并计数
  - `--合并` 前必定给 jobs.json / knowledge.json / 受影响的面试 status.json 做带时间戳的备份
    （**保留不删**）；任一步失败按备份回滚
  - 合并**只标记** `merged_into`：不删岗位记录、不删报告、不删 JD 原文（报告是用户的资产）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import jobdup  # noqa: E402
import kb      # noqa: E402
import store   # noqa: E402

USAGE = """用法：
  python 岗位去重.py                             # 只列疑似重复的岗位对（不改任何文件）
  python 岗位去重.py --合并 <keep_id> <drop_id>   # 合并（先备份；keep 通常是更早那条）
"""


def _multi_jd():
    return kb.stats(store.load_knowledge())["multi_jd"]


def _score_of(keep_id, drop_id):
    """两份 JD 的包含度；任一侧没有原文时返回 None（如实说「无法比对」）"""
    ta, tb = jobdup.jd_text_of(keep_id), jobdup.jd_text_of(drop_id)
    if not ta.strip() or not tb.strip():
        return None
    return jobdup.containment(ta, tb)


def _fmt(v):
    """分数格式化：None / 空 → 「—」（老岗位可能没打分），非数字原样印出来"""
    if v is None or v == "":
        return "—"
    try:
        return "%.1f" % float(v)
    except (TypeError, ValueError):
        return str(v)


def list_pairs():
    """只读体检：列出台账里的疑似重复对 + 知识库现状。返回进程退出码。"""
    jobs = store.load_jobs()
    merged = sum(1 for j in jobs if isinstance(j, dict)
                 and str(j.get("merged_into") or "").strip())
    pairs, skipped = jobdup.find_duplicate_jobs()

    print("profile: %s" % store.current_profile())
    print("台账 %d 条（其中已合并 %d 条，不参与判重）" % (len(jobs), merged))
    print("跳过（缺 JD 原文）%d 个" % skipped)
    print("知识库：%d 个概念，其中 %d 个「被多个岗位提到」（multi_jd 就是复习优先级的乘性因子）\n"
          % (len(kb.ensure_kb(store.load_knowledge())["nodes"]), _multi_jd()))

    if not pairs:
        print("未发现疑似重复的岗位对（包含度阈值 %.2f）。" % jobdup.THRESHOLD)
        return 0

    print("发现 %d 对疑似重复（包含度 ≥ %.2f；keep = **时间更早**的那条，作为合并后的身份）：\n"
          % (len(pairs), jobdup.THRESHOLD))
    for i, p in enumerate(pairs, 1):
        title_k, title_d = p["both_titles"]
        date_k, date_d = p["both_dates"]
        score_k, score_d = p["both_scores"]
        print("  %d) 相似度 %.3f  标题：%s" % (i, p["score"], title_k))
        print("     keep  %s | %s | %s 分 | %s" % (p["keep"], date_k or "无日期", _fmt(score_k), title_k))
        print("     drop  %s | %s | %s 分 | %s" % (p["drop"], date_d or "无日期", _fmt(score_d), title_d))
        print("     合并：python 岗位去重.py --合并 %s %s" % (p["keep"], p["drop"]))
    print("\n以上只是列出，**没有改动任何文件**。确认后再跑 --合并。")
    return 0


def do_merge(keep_id, drop_id):
    """合并一对（先备份）+ 打印 multi_jd 变化。返回进程退出码。"""
    before = _multi_jd()
    score = _score_of(keep_id, drop_id)
    print("profile: %s" % store.current_profile())
    print("合并前：知识库 %d 个概念，其中 multi_jd %d 个"
          % (len(kb.ensure_kb(store.load_knowledge())["nodes"]), before))
    if score is None:
        print("⚠ 这两条里至少有一条读不到 JD 原文，无法比对相似度（仍按你的确认合并）。")
    elif score < jobdup.THRESHOLD:
        print("⚠ keep↔drop 的包含度只有 %.3f（低于阈值 %.2f）——请再确认这两条真是同一份 JD。"
              % (score, jobdup.THRESHOLD))
    else:
        print("keep↔drop 包含度 %.3f（≥ %.2f，判为同一份 JD）" % (score, jobdup.THRESHOLD))

    res = jobdup.merge_jobs(keep_id, drop_id)
    for b in res["backups"]:
        print("  备份：%s" % b)
    if not res["ok"]:
        print("✗ %s" % res["reason"])
        return 1
    if res["noop"]:
        print("· %s" % res["reason"])
        return 0
    print("✓ %s" % res["reason"])
    print("  知识库改指：%d 条；去掉重复项：%d 条；面试记录改指：%d 个文件"
          % (res["kb_repointed"], res["kb_deduplicated"], res["interview_files"]))
    print("  drop 记录已标记 merged_into（**没删记录、没删报告、没删 JD 原文**）")
    print("合并后：multi_jd %s → %s" % (res["multi_jd_before"], _multi_jd()))
    print("备份保留（确认无误后可自行删除）。")
    return 0


def main():
    args = sys.argv[1:]
    if not args:
        return list_pairs()
    if args[0] == "--合并" and len(args) == 3:
        return do_merge(args[1], args[2])
    sys.stderr.write(USAGE)
    return 2


if __name__ == "__main__":
    sys.exit(main())
