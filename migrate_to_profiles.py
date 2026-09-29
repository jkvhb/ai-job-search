# -*- coding: utf-8 -*-
"""一次性迁移：把扁平数据**复制**进 data/profiles/default/（非破坏性，可重复运行）

注意：本脚本只复制，不删除任何原数据。等 Task 7 把 app.py 切到新路径、
      回归验证通过之后，再手动清理根目录的 config.json / resumes/ / reports/
      以及 data/ 下的散落文件。
"""
import os
import shutil
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import store

ROOT = store.ROOT


def copy_file(src, dst):
    if not os.path.exists(src):
        return "跳过(不存在)"
    if os.path.exists(dst):
        return "跳过(目标已存在)"
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)
    return "已复制"


def copy_dir(src, dst):
    if not os.path.isdir(src):
        return "跳过(不存在)"
    if os.path.isdir(dst) and os.listdir(dst):
        # 目录非空：核对文件数，避免"上次半途中断 + 本次跳过"掩盖缺文件
        src_n = sum(len(fs) for _, _, fs in os.walk(src))
        dst_n = sum(len(fs) for _, _, fs in os.walk(dst))
        if src_n != dst_n:
            return "⚠ 跳过(目标非空但疑似不完整: %d/%d 文件)" % (dst_n, src_n)
        return "跳过(目标非空)"
    shutil.copytree(src, dst, dirs_exist_ok=True)
    return "已复制"


def main():
    dest = store.ensure_profile("default")
    store.set_current_profile("default")
    print("目标 profile:", dest)

    print("  %-16s -> %s" % ("config.json",
                             copy_file(os.path.join(ROOT, "config.json"),
                                       os.path.join(dest, "config.json"))))

    for d in ("resumes", "reports"):
        print("  %-16s -> %s" % (d + "/",
                                 copy_dir(os.path.join(ROOT, d), os.path.join(dest, d))))

    for name in ("jobs.json", "jd", "logs"):
        src = os.path.join(ROOT, "data", name)
        dst = os.path.join(dest, name)
        fn = copy_dir if os.path.isdir(src) else copy_file
        print("  data/%-11s -> %s" % (name, fn(src, dst)))

    print("\n完成（非破坏性：原数据仍在原位，Task 7 验证通过后再清理）")
    print("当前 profile:", store.current_profile())


if __name__ == "__main__":
    main()
