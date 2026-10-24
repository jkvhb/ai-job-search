# -*- coding: utf-8 -*-
"""打包「交给对方的 AI」的交接包：源码 + 文档 + 测试 + 交接文档 + `manifest.json`。

和分享包的区别（别把两者合并）：
  * `打包分享.py` → 给**朋友**用的：剔除一切个人数据，目标只有一个「解压能跑起来」；
  * `打包交接.py`（本文件）→ 给**朋友的 AI** 用的：目标「能读懂、能改、能自测」——
    所以带 `tests/` 与 `docs/`（项目纪律与历史都写在那里），并附 `manifest.json`
    （文件指纹 + 模块清单 + 测试条数），方便接手方先对账再动手。

两者共用同一套安全检查：`打包分享.verify()`（泄漏扫描 + 依赖自检 + 入门文件齐不齐）。
本文件**只读**仓库、只写 `dist/`；绝不动 `data/`，也绝不把 `config.json` / 泄漏关键词表打进包。

用法：
  python 打包交接.py                 # 产物在 dist/，打印路径、体积与清单
  python 打包交接.py --允许个人信息    # 万一 docs/ 里出现真实姓名：默认删包；加这个参数才放行（会把命中的位置逐条打印）
"""
import ast
import hashlib
import importlib
import json
import os
import subprocess
import sys
import zipfile
from datetime import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(ROOT, "dist")
PACK = importlib.import_module("打包分享")

DOC_NAME = "给AI的交接文档.md"
MANIFEST_NAME = "manifest.json"

#: 交接包**绝不带**的东西（个人数据 / 密钥 / 缓存 / 构建产物）
SKIP_DIRS = {"data", "dist", "__pycache__", ".git", ".idea", ".vscode", ".pytest_cache"}
SKIP_FILES = {"config.json", ".leak-keywords.txt"}
SKIP_SUFFIX = (".pyc", ".zip", ".log", ".tmp", ".bak")
#: 交接包额外带的目录（分享包只带 web/assets/templates）
DIRS = ("web", "assets", "templates", "docs", "tests")


def package_name(now=None):
    return "AI求职助手-AI交接包-" + (now or datetime.now()).strftime("%Y%m%d")


def _skip(rel):
    parts = rel.replace("\\", "/").split("/")
    for p in parts[:-1]:
        if p in SKIP_DIRS or p.startswith("."):
            return True
    name = parts[-1]
    return (name in SKIP_FILES or name.startswith("_") or name.endswith(SKIP_SUFFIX)
            or name.startswith("."))


def collect(root=None):
    """返回 `[(相对路径, 绝对路径)]`：根目录源码/文档/脚本 + 四个目录（含 docs/ 与 tests/）。"""
    root = root or ROOT
    items, seen = [], set()

    def add(rel, full):
        rel = rel.replace("\\", "/")
        if rel in seen or _skip(rel) or not os.path.isfile(full):
            return
        seen.add(rel)
        items.append((rel, full))

    for name in sorted(os.listdir(root)):
        if name.endswith((".py", ".md", ".bat")):
            add(name, os.path.join(root, name))
    for d in DIRS:
        base = os.path.join(root, d)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(x for x in dirnames
                                 if x not in SKIP_DIRS and not x.startswith("."))
            for fn in sorted(filenames):
                full = os.path.join(dirpath, fn)
                add(os.path.relpath(full, root), full)
    return items


def _read_text(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(256 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _git():
    def run(*args):
        try:
            p = subprocess.run(["git"] + list(args), cwd=ROOT, capture_output=True,
                               text=True, timeout=20)
            return p.stdout.strip() if p.returncode == 0 else ""
        except Exception:
            return ""
    return {"head": run("rev-parse", "HEAD"),
            "branch": run("rev-parse", "--abbrev-ref", "HEAD"),
            "dirty": bool(run("status", "--porcelain"))}


def _module_info(rel, path):
    """一个模块的首行说明 / 行数 / 公开函数与类（**静态**解析，不 import —— 不触发副作用）。"""
    try:
        src = _read_text(path)
        tree = ast.parse(src)
        doc = (ast.get_docstring(tree) or "").strip().splitlines()
        first = doc[0].strip() if doc else ""
        publics = [n.name for n in tree.body
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                   and not n.name.startswith("_")]
        return {"path": rel, "doc": first[:160], "lines": src.count("\n") + 1,
                "public": publics}
    except Exception as e:                              # 坏文件不该让打包失败
        return {"path": rel, "doc": "", "lines": 0, "public": [], "error": str(e)}


def _test_methods(root=None):
    root = root or ROOT
    total, files = 0, []
    tdir = os.path.join(root, "tests")
    for fn in sorted(os.listdir(tdir)) if os.path.isdir(tdir) else []:
        if not fn.endswith(".py"):
            continue
        try:
            tree = ast.parse(_read_text(os.path.join(tdir, fn)))
        except Exception:
            continue
        n = sum(1 for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name.startswith("test_"))
        if n:
            files.append({"file": "tests/" + fn, "tests": n})
            total += n
    return total, files


def build_manifest(items, root=None):
    """`manifest.json` 的内容（对账用：接手方改完代码可以拿 sha 看动了哪些文件）。"""
    root = root or ROOT
    files = [{"path": rel, "bytes": os.path.getsize(full), "sha256": _sha256(full)[:16]}
             for rel, full in items]
    total, test_files = _test_methods(root)
    modules = [_module_info(rel, full) for rel, full in items
               if rel.endswith(".py") and "/" not in rel]
    return {
        "package": "AI 求职助手 · AI 交接包",
        "read_me_first": DOC_NAME,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "python": sys.version.split()[0],
        "git": _git(),
        "counts": {"files": len(files), "modules": len(modules), "test_methods": total},
        "how_to_run": "python app.py   （Windows 双击 启动.bat）",
        "how_to_test": "python -m unittest discover -s tests -t .   （约 3 分钟）",
        "files": files,
        "modules": modules,
        "test_files": test_files,
        "excluded": sorted(list(SKIP_FILES) + list(SKIP_DIRS)),
    }


def build(dest=None, root=None):
    """真正打 zip；返回 `(zip 路径, manifest 字典)`。不删包（安全检查在 `main()` 里做）。"""
    root = root or ROOT
    dest = dest or DIST
    items = collect(root)
    pkg = package_name()
    os.makedirs(dest, exist_ok=True)
    zpath = os.path.join(dest, pkg + ".zip")
    manifest = build_manifest(items, root)
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for rel, full in items:
            z.write(full, pkg + "/" + rel)
        z.writestr(pkg + "/" + MANIFEST_NAME,
                   json.dumps(manifest, ensure_ascii=False, indent=2))
    return zpath, manifest


def main(argv=None, dest=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    allow_pii = "--允许个人信息" in argv
    zpath, manifest = build(dest)
    notes, pii = [], []
    issues = PACK.verify(zpath, root=ROOT, allow_fake_keys_in=("tests/",), notes=notes,
                         required=PACK.REQUIRED_IN_PACKAGE)
    pii = [i for i in issues if i.startswith("个人信息")]
    others = [i for i in issues if not i.startswith("个人信息")]

    print("=" * 58)
    print("  打包 AI 交接包")
    print("=" * 58)
    print("  文件数：%d（含 tests/ %d 条用例、docs/）"
          % (manifest["counts"]["files"], manifest["counts"]["test_methods"]))
    for note in notes:
        print("  · " + note)

    if others or (pii and not allow_pii):
        bad = others + pii
        print("  ✘ 发现 %d 个问题，已删除该包：" % len(bad))
        for i in bad:
            print("     -", i)
        os.remove(zpath)
        if pii and not allow_pii:
            print("\n  提示：以上「个人信息」都来自 docs/ 或源码正文。")
            print("        若这是**只给你自己的 AI** 的交接包，可以加 --允许个人信息 放行；")
            print("        若要发给别人，请先删掉/脱敏对应文件再打包。")
        return 1

    if pii:
        print("  ⚠️ 已放行 %d 处个人信息（--允许个人信息）：" % len(pii))
        for i in pii:
            print("     -", i)

    print("  ✔ 通过：无明文 API Key、依赖自检干净、入门文件齐全")
    size = os.path.getsize(zpath)
    with zipfile.ZipFile(zpath) as z:
        n = len(z.namelist())
    print("\n  产物：%s" % zpath)
    print("  大小：%.1f MB   文件数：%d" % (size / 1024 / 1024, n))
    print("  包内必读：%s / %s" % (DOC_NAME, MANIFEST_NAME))
    print("\n  交给对方的 AI 时说：先读 %s 和 %s，按里面的硬约束动手。" % (DOC_NAME, MANIFEST_NAME))
    return 0


if __name__ == "__main__":
    sys.exit(main())
