# -*- coding: utf-8 -*-
"""`打包分享.py`（分享包清单 + 反向往回扫）的测试。

**为什么要在假仓库上测**：真打一个包要遍历整棵树，慢且没法复现"清单漏文件"这种错误。
`collect()` / `verify()` / `dependency_issues()` 都接受可注入的 `root`，
所以本文件用 `tempfile.mkdtemp()` 造一个**假仓库**（app.py + aaa.py + bbb.py + web/x.html），
让**真实的** collect()/verify() 代码路径跑在上面。

三条反向用例是本文件的牙齿：漏模块 / 漏 web 模板 → 必须**点名**缺了哪个文件；
不许写成"只要不抛异常就算过"。

本文件不 import store/log、不碰真实 `data/`，所有落盘都在临时目录里。
"""
import importlib
import os
import re
import shutil
import tempfile
import unittest
import zipfile

PACK = importlib.import_module("打包分享")

# 真实仓库根目录（测试只读它，不写）
REPO = os.path.dirname(os.path.abspath(PACK.__file__))

# zip 内的顶层目录名。故意与真实的 PKG 不同：verify() 不该依赖包名前缀。
PKG_NAME = "假包-20260101"

# 假 app.py：三种情况都要覆盖
#   aaa / bbb  → 盘上有同名 .py，**属于本地依赖，必须在包里**
#   ccc        → 盘上没有 ccc.py，不算依赖（不能凭空要求）
#   os / json  → 标准库
#   web/x.html、web/demo.json → 被真实引用的 web 静态文件
FAKE_APP = """# -*- coding: utf-8 -*-
import json
import os
from datetime import datetime

import aaa
import bbb
import ccc as ccc_mod

WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
TEMPLATE = os.path.join(WEB, "x.html")
DEMO = os.path.join(WEB, "demo.json")
"""

FAKE_FILES = ["app.py", "aaa.py", "bbb.py"]

# P0-10 点名漏掉的 9 个模块（app.py 模块级 import 得到，却不在 FILES 里）
P0_10_MODULES = ["interview.py", "jddelete.py", "jdfacts.py", "jdid.py", "jobdup.py",
                 "kb.py", "kbreuse.py", "outcome.py", "resumelib.py"]


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


class FakeRepoTest(unittest.TestCase):
    """假仓库：真实的 collect()/verify() 代码路径。"""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="pack-test-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        _write(os.path.join(self.root, "app.py"), FAKE_APP)
        _write(os.path.join(self.root, "aaa.py"), "# aaa\nVALUE = 'aaa'\n")
        _write(os.path.join(self.root, "bbb.py"), "# bbb\nVALUE = 'bbb'\n")
        _write(os.path.join(self.root, "web", "x.html"), "<html>x</html>\n")
        _write(os.path.join(self.root, "web", "demo.json"), "{}\n")
        self.zip_path = os.path.join(self.root, "out.zip")

    # ---- 工具 ----

    def collect(self, files=None, dirs=("web",)):
        return PACK.collect(root=self.root,
                            files=FAKE_FILES if files is None else files,
                            dirs=list(dirs))

    def build(self, items):
        """按包里真实的样子写 zip（顶层目录 + 相对路径）。"""
        with zipfile.ZipFile(self.zip_path, "w") as z:
            for rel, full in items:
                z.write(full, PKG_NAME + "/" + rel)
        return self.zip_path

    def names(self, zpath):
        with zipfile.ZipFile(zpath) as z:
            return z.namelist()

    def collected(self):
        return {rel for rel, _ in self.collect()}

    # ---- 正向：清单齐全时什么都不许报 ----

    def test_collect_covers_local_imports(self):
        rels = self.collected()
        self.assertIn("aaa.py", rels)
        self.assertIn("bbb.py", rels)
        self.assertNotIn("ccc.py", rels, "盘上没有 ccc.py，不该被当成依赖")

    def test_dependency_issues_empty_when_complete(self):
        zpath = self.build(self.collect())
        self.assertEqual([], PACK.dependency_issues(self.root, self.names(zpath)))

    def test_verify_passes_when_complete(self):
        zpath = self.build(self.collect())
        self.assertEqual([], PACK.verify(zpath, root=self.root))

    # ---- 反向 1：漏一个本地模块（P0-10 的核心牙）----

    def test_missing_local_module_is_reported(self):
        items = self.collect(files=["app.py", "aaa.py"])          # 故意漏掉 bbb.py
        zpath = self.build(items)
        self.assertEqual(["bbb.py"], PACK.dependency_issues(self.root, self.names(zpath)))

    def test_verify_names_missing_local_module(self):
        items = self.collect(files=["app.py", "aaa.py"])
        issues = PACK.verify(self.build(items), root=self.root)
        hit = [i for i in issues if "bbb.py" in i]
        self.assertTrue(hit, "依赖自检没有点名缺 bbb.py，实际报的是：%r" % (issues,))
        self.assertTrue(any("依赖" in i or "缺少" in i for i in hit),
                        "点名了 bbb.py，但没说清是『缺少依赖』：%r" % (hit,))

    # ---- 反向 2：web/ 模板被引用却没打包 ----

    def test_missing_web_template_is_reported(self):
        items = self.collect(dirs=())                              # 模拟 DIRS 忘了带 web
        zpath = self.build(items)
        self.assertEqual(["web/demo.json", "web/x.html"],
                         PACK.dependency_issues(self.root, self.names(zpath)))

    def test_verify_names_missing_web_template(self):
        items = self.collect(dirs=())
        issues = PACK.verify(self.build(items), root=self.root)
        self.assertTrue(any("web/x.html" in i for i in issues),
                        "被引用的 web 模板没进包，verify() 却没点名：%r" % (issues,))

    # ---- 反向 3：模块和模板一起漏 → 两条都要点名（不许只报第一条）----

    def test_verify_names_every_missing_file(self):
        items = self.collect(files=["app.py", "aaa.py"], dirs=())
        text = "\n".join(PACK.verify(self.build(items), root=self.root))
        self.assertIn("bbb.py", text)
        self.assertIn("web/x.html", text)
        self.assertIn("web/demo.json", text)


class RealRepoTest(unittest.TestCase):
    """真仓库上的防回归钉子：清单必须覆盖 app.py 的全部本地依赖（只读 data/）。"""

    def test_files_listed_all_exist_on_disk(self):
        """FILES 里不该挂盘上不存在的名字。

        `创建桌面快捷方式.bat` / `分享说明.md` 是脚本用 SHORTCUT_BAT / SHARE_NOTE
        直接写进包的**生成物**，挂在 FILES 里只会在每次打包时打一行"跳过（不存在）"，
        还会让人误以为包里没有它们。
        """
        missing = [f for f in PACK.FILES
                   if not os.path.exists(os.path.join(REPO, f.replace("/", os.sep)))]
        self.assertEqual([], missing, "FILES 里这些文件盘上不存在：%s" % missing)

    def test_p0_10_missing_modules_now_packaged(self):
        """P0-10：这 9 个模块一个都不能少。"""
        rels = {rel for rel, _ in PACK.collect()}
        missing = [n for n in P0_10_MODULES if n not in rels]
        self.assertEqual([], missing, "P0-10：这些模块 app.py 会 import，但分享包里没有：%s" % missing)

    def test_app_local_imports_are_all_packaged(self):
        """动态版：不写死清单，直接从真 app.py 的 import 推导。"""
        rels = {rel for rel, _ in PACK.collect()}
        with open(os.path.join(REPO, "app.py"), encoding="utf-8") as f:
            src = f.read()
        mods = set(re.findall(r"(?m)^\s*import\s+([A-Za-z_]\w*)", src))
        local = sorted(m for m in mods if os.path.isfile(os.path.join(REPO, m + ".py")))
        self.assertGreaterEqual(len(local), 14, "app.py 的本地 import 变少了？扫到：%s" % local)
        missing = [m + ".py" for m in local if m + ".py" not in rels]
        self.assertEqual([], missing, "app.py import 了这些模块，但包里没有：%s" % missing)

    def test_resolve_dependencies_all_packaged(self):
        """依赖自检的基准必须全部落进包（模块 + web 资源）。"""
        rels = {rel for rel, _ in PACK.collect()}
        deps = PACK.resolve_dependencies(REPO)
        self.assertTrue([d for d in deps if d.startswith("web/")],
                        "web 资源一条都没扫出来，说明引用推导失效了：%s" % deps)
        self.assertEqual([], [d for d in deps if d not in rels],
                         "这些被真实引用的文件没进包：%s" % [d for d in deps if d not in rels])

    def test_web_assets_referenced_by_code_are_dependencies(self):
        deps = set(PACK.resolve_dependencies(REPO))
        for name in ("web/report_template.html", "web/interview_template.html",
                     "web/outcome_template.html", "web/index.html", "web/demo_data.json"):
            self.assertIn(name, deps, "代码引用了 %s，依赖推导却没扫到" % name)

    def test_real_zip_has_no_missing_dependency(self):
        """端到端：拿真仓库的 collect() 结果造包（约 0.7MB），依赖自检必须干净。

        只断言"依赖"这一条 —— 真仓库当前还命中 PII 关键词（app.py / README.md 里
        写着真实姓名），那是另一件事，不在本测试的职责里。
        """
        tmp = tempfile.mkdtemp(prefix="pack-real-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        zpath = os.path.join(tmp, "real.zip")
        items = PACK.collect()
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            for rel, full in items:
                z.write(full, "pkg/" + rel)
        with zipfile.ZipFile(zpath) as z:
            names = z.namelist()
        self.assertEqual([], PACK.dependency_issues(REPO, names))
        # verify() 整条路径也要跑通（PII 那几条不算本测试的事）
        dep_text = [i for i in PACK.verify(zpath, root=REPO) if "缺少依赖文件" in i]
        self.assertEqual([], dep_text)


if __name__ == "__main__":
    unittest.main()
