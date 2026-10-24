# -*- coding: utf-8 -*-
"""`打包分享.py`（分享包清单 + 反向往回扫）的测试。

**为什么要在假仓库上测**：真打一个包要遍历整棵树，慢且没法复现"清单漏文件"这种错误。
`collect()` / `verify()` / `dependency_issues()` 都接受可注入的 `root`，
所以本文件用 `tempfile.mkdtemp()` 造一个**假仓库**（app.py + aaa.py + bbb.py + web/x.html），
让**真实的** collect()/verify() 代码路径跑在上面。

三条反向用例是本文件的牙齿：漏模块 / 漏 web 模板 → 必须**点名**缺了哪个文件；
不许写成"只要不抛异常就算过"。

本文件不 import store/log，所有落盘都在临时目录里。
**唯一的例外**：`ExamplePlaceholderTest` 会**只读**地看一眼真实 `data/`，把"用户自己的真名"
取出来当比较基准（P0-15：README 的示例串不许再写真名）—— 真名只用于 `assertNotIn`，
既不写进任何文件、也不打印（失败信息里只报关键词长度）。
⚠️ 那台机器**没有 `data/`**（干净检出）时真名推不出来 ⇒ 相关用例**显式 skip + 给理由**
（见 `_assert_no_real_pii`），绝不退化成"看起来在断言、其实什么都没断言"的空循环。
"""
import hashlib
import importlib
import os
import re
import shutil
import tempfile
import unittest
import zipfile
from unittest import mock

PACK = importlib.import_module("打包分享")
HAND = importlib.import_module("打包交接")

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
    """真仓库上的防回归钉子：清单必须覆盖**包内每个 .py** 的本地依赖（只读 data/）。

    ⚠️ 自检的覆盖边界（实话，别把它当成「包一定完整」的证明）：`_scan_py_sources()` 的起点是
    **`FILES` 里列出的 .py + app.py**，`resolve_dependencies()` 再顺着这些文件里的
    `import` / 静态引用往下走。于是：
      - 包内模块新增了依赖、清单却没跟（`jddelete` 新 import 的 `jdrefs` 不在 FILES）⇒ **抓得到** ✓；
      - 「本该入包、却压根没写进 FILES」的模块 ⇒ **永远抓不到** ✗（起点就是清单本身）；
      - 只在**未入包**脚本里被 import 的模块（`迁移脚手架.py` ← 简历迁移 / 身份迁移）⇒
        自检按设计不管它们，同进同出由 `test_scaffold_ships_iff_its_importers_ship` 钉住。
    """

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

    def test_jdrefs_module_is_packaged(self):
        """P0-10 同款事故再犯一次：批 7.1 把 `jddelete` / `jdid` / `jobdup` 接到 `jdrefs`，

        而 `FILES` 里当时**没有** `jdrefs.py` ⇒ 打出来的分享包缺这个模块，朋友解压双击
        `启动.bat` 就是 `ModuleNotFoundError`。这条把「被 import 就必须在清单里」钉在
        `jdrefs.py` 这个具体名字上（动态判据见下面的 `test_resolve_dependencies_all_packaged`）。
        """
        rels = {rel for rel, _ in PACK.collect()}
        self.assertIn("jdrefs.py", rels, "jddelete/jdid/jobdup 都 import 了 jdrefs，包里却没有它")

    def test_scaffold_ships_iff_its_importers_ship(self):
        """`迁移脚手架.py` 只有 `简历迁移.py` / `身份迁移.py` 两个 import 方 ⇒ 两边必须同进同出。

        现状（本用例在钉的就是这个）：**三个都不入包** —— 它们是会改用户数据的独立维护脚本，
        分享给朋友的是能用起来的应用，不是这些一次性工具 ⇒ `迁移脚手架.py` 不在 `FILES` 里
        **不是缺项**（把只有"没入包的脚本"才 import 的模块单独打进包，是给朋友塞一段死代码）。
        哪天要把迁移脚本也发给朋友，就必须**同时**把脚手架加进 `FILES`，否则那两份脚本一跑
        就 `ModuleNotFoundError`；只加脚手架不加脚本，本用例同样会红。
        """
        listed = set(PACK.FILES)
        importers = [n for n in ("简历迁移.py", "身份迁移.py") if n in listed]
        self.assertEqual(bool(importers), "迁移脚手架.py" in listed,
                         "`迁移脚手架.py` 与它的两个 import 方必须同进同出；"
                         "现在是 importers=%s、脚手架在 FILES 里=%s"
                         % (importers, "迁移脚手架.py" in listed))

    def test_data_maintenance_clis_are_packaged(self):
        """**反复可用**的数据维护入口随包发出：三个都「无参数严格只读 + 显式参数才写 + 写前备份」。

        为什么这条要有（第四轮裁的）：`备份体检.py` 入包而 `报告体检.py` 不入包，是同一类脚本
        两种待遇；而 README **随包发出去**，里面多处直接让用户 `python 报告体检.py --刷新模板`、
        模块表里也把 `岗位去重.py` 当维护入口介绍 —— 不在包里就是 README 说假话
        （朋友照着跑 = `FileNotFoundError`）。
        与它相对的是**一次性**老数据迁移脚本（`简历迁移.py` / `身份迁移.py` /
        `迁移脚手架.py`），那些只在"迁移前的数据"上有意义，仍不入包
        （见 `test_scaffold_ships_iff_its_importers_ship`）。
        判据不是"它会改数据"（这三个都会），而是「无参严格只读 + 显式参数才写 + 写前必备份」。
        """
        rels = {rel for rel, _ in PACK.collect()}
        for name in ("备份体检.py", "报告体检.py", "岗位去重.py"):
            with self.subTest(script=name):
                self.assertIn(name, rels, "数据维护入口 %s 不在分享包里" % name)

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


class HandoffPackTest(unittest.TestCase):
    """**交接包**（`打包交接.py`）：自包含、不带个人数据、manifest 能对账。

    这个包和分享包的目标不同（它给"对方的 AI"看源码 + 测试 + 项目纪律），所以必须钉三件事：
      ① 该有的都有（交接文档 / manifest / 源码 / tests / docs）；
      ② 不该有的一律没有（`data/`、`config.json`、泄漏关键词表）；
      ③ manifest 里的指纹与包里**真实字节**一致 —— 否则"对账"这件事本身就是假话。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pack-handoff-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _build(self):
        zpath, man = HAND.build(dest=self.tmp)
        return zpath, man

    @staticmethod
    def _rels(zpath):
        with zipfile.ZipFile(zpath) as z:
            names = z.namelist()
        return names, {n.split("/", 1)[1] for n in names if "/" in n}

    def test_the_share_package_ships_the_newcomer_guide(self):
        self.assertIn("新手引导.md", PACK.FILES)
        self.assertIn("新手引导.md", {rel for rel, _ in PACK.collect()})
        self.assertIn("新手引导.md", PACK.REQUIRED_IN_PACKAGE,
                      "入门文件必须列进「缺少就报错」的清单，否则哪天被删了没人发现")

    def test_verify_flags_a_package_without_the_newcomer_guide(self):
        """反向：**缺**新手引导的包必须被点名（不然上面那条只是"清单里有"）。"""
        root = os.path.join(self.tmp, "fake")
        os.makedirs(os.path.join(root, "web"))
        for rel, text in (("app.py", FAKE_APP), ("aaa.py", "# aaa\n"),
                          ("bbb.py", "# bbb\n"), ("web/x.html", "<html></html>\n"),
                          ("web/demo.json", "{}\n")):
            _write(os.path.join(root, rel), text)
        zpath = os.path.join(self.tmp, "fake.zip")
        with zipfile.ZipFile(zpath, "w") as z:
            for rel, full in PACK.collect(root=root, files=FAKE_FILES, dirs=["web"]):
                z.write(full, "pkg/" + rel)
        issues = PACK.verify(zpath, root=root, required=PACK.REQUIRED_IN_PACKAGE)
        self.assertTrue([i for i in issues if "新手引导.md" in i],
                        "缺新手引导却没被点名：%r" % (issues,))

    def test_launcher_checks_the_python_version(self):
        """`启动.bat` 必须真的**检查版本**：只 `where python` 时，老 Python 会抛用户看不懂的语法错。"""
        with open(os.path.join(REPO, "启动.bat"), encoding="utf-8") as f:
            txt = f.read()
        self.assertIn("version_info", txt)
        self.assertIn("3, 9", txt)

    def test_handoff_package_is_self_contained_and_clean(self):
        zpath, man = self._build()
        names, rels = self._rels(zpath)
        for rel in ("给AI的交接文档.md", "manifest.json", "app.py", "pdftext.py",
                    "tests/test_pdftext.py", "docs/批2-7-验收报告.md",
                    "web/index.html", "新手引导.md", "启动.bat"):
            self.assertIn(rel, rels, "交接包缺少 %s" % rel)
        for bad in ("config.json", ".leak-keywords.txt", "manifest.json.bak"):
            self.assertNotIn(bad, rels, "交接包不许带 %s" % bad)
        self.assertEqual([], [r for r in rels if r.startswith("data/")],
                         "交接包不许带用户数据：%r" % [r for r in rels if r.startswith("data/")])
        self.assertEqual(man["counts"]["files"] + 1, len(names),      # +manifest.json 自己
                         "manifest 的文件数与 zip 里的条目数对不上")

    def test_manifest_fingerprints_match_the_packaged_bytes(self):
        zpath, man = self._build()
        pkg = os.path.basename(zpath)[:-4]
        with zipfile.ZipFile(zpath) as z:
            for entry in man["files"]:
                data = z.read(pkg + "/" + entry["path"])
                self.assertEqual(len(data), entry["bytes"], entry["path"])
                self.assertEqual(hashlib.sha256(data).hexdigest()[:16], entry["sha256"],
                                 entry["path"])

    def test_fake_key_exemption_is_named_and_narrow(self):
        """`tests/` 里的假 Key 被**点名豁免**；`tests/` 以外的 sk- 串照样报（不许整包豁免）。"""
        zpath, _man = self._build()
        notes = []
        issues = PACK.verify(zpath, root=REPO, allow_fake_keys_in=("tests/",), notes=notes)
        self.assertEqual([], [i for i in issues if "API Key" in i], issues)
        self.assertTrue([n for n in notes if "已豁免" in n], "豁免必须被如实说出来：%r" % (notes,))

        root = os.path.join(self.tmp, "fakekey")
        os.makedirs(os.path.join(root, "web"))
        for rel, text in (("app.py", FAKE_APP), ("aaa.py", 'KEY = "sk-' + "a" * 30 + '"\n'),
                          ("bbb.py", "# bbb\n"), ("web/x.html", "<html></html>\n"),
                          ("web/demo.json", "{}\n")):
            _write(os.path.join(root, rel), text)
        z2 = os.path.join(self.tmp, "fakekey.zip")
        with zipfile.ZipFile(z2, "w") as z:
            for rel, full in PACK.collect(root=root, files=FAKE_FILES, dirs=["web"]):
                z.write(full, "pkg/" + rel)
        issues2 = PACK.verify(z2, root=root, allow_fake_keys_in=("tests/",))
        self.assertTrue([i for i in issues2 if "aaa.py" in i],
                        "`tests/` 以外的明文 Key 被漏掉了：%r" % (issues2,))


class ReadmeCommandsTest(unittest.TestCase):
    """**随包发出的 README 不许让用户去跑包里没有的脚本**（第四轮加的钉子）。

    为什么这是真问题（不是洁癖）：`README.md` 在 `FILES` 里、会原样发给朋友，而它在好几处
    直接写 `python 报告体检.py --刷新模板`。脚本不在包里 ⇒ 朋友照着 README 做就是
    `No such file or directory` ——「文档说假话」的同类（本项目把它当缺陷，不当文案）。
    判据是**扫 README 里的 `python X.py` 命令**，不是维护一份"我觉得该入包"的清单：
    以后谁在 README 里加一条让人跑的脚本，忘了打包，这条会红。
    """

    #: README 给了 `python X.py` 命令、但**故意不随包发出**的脚本。
    #: 每一条都要有理由；而且必须「README 真的这么用」「FILES 里真的没有」——
    #: 哪天它入包了，`test_allowlist_is_not_rotten` 会要求把豁免删掉（豁免不许变成死字）。
    NOT_SHIPPED = {
        "简历迁移.py": "一次性老数据登记：只对『版本库出现之前』的数据有意义，朋友拿到的是新数据",
        "身份迁移.py": "一次性老数据迁移：岗位身份从报告文件名换成结构化 id，同上",
    }

    def _readme_commands(self):
        """README 里所有 `python X.py` 的 `X.py`（`python -m unittest …` 不算）。"""
        with open(os.path.join(REPO, "README.md"), encoding="utf-8") as f:
            src = f.read()
        return sorted(set(re.findall(r"python\s+([^\s`]+\.py)", src)))

    def test_readme_python_commands_are_packaged(self):
        cmds = self._readme_commands()
        self.assertIn("app.py", cmds, "README 的 python 命令一条都没扫出来？解析方式不对")
        self.assertIn("报告体检.py", cmds, "README 已经不教用户跑报告体检了？解析方式不对")
        missing = [c for c in cmds if c not in PACK.FILES and c not in self.NOT_SHIPPED]
        self.assertEqual([], missing,
                         "随包发出的 README 让用户跑这些脚本，但包里没有：%s" % missing)

    def test_allowlist_is_not_rotten(self):
        """豁免必须两条都成立：README 真的这么用、FILES 里真的没有。"""
        cmds = set(self._readme_commands())
        for name, why in sorted(self.NOT_SHIPPED.items()):
            with self.subTest(script=name):
                self.assertIn(name, cmds, "README 已经不这么用了，把 %s 从豁免里删掉" % name)
                self.assertNotIn(name, PACK.FILES,
                                 "%s 已经入包了，豁免该删（理由原来是：%s）" % (name, why))


# ---- P0-15：泄漏扫描必须自动吃「当前 profile 自己的 PII」----
# 背景：`.leak-keywords.txt` **被 gitignore** ⇒ 干净检出 / 新机器上它不存在 ⇒ 关键词表为空
# ⇒ 带真名的分享包照样通过并可发出去（实测：本机恰好留着那个文件才偶然拦住）。
# ⚠️ 下面所有"名字"都是**自造假名**；用户真名只在 `ExamplePlaceholderTest` 里从 `data/`
#    **只读**取出来做比较，绝不写进文件、绝不打印。
FAKE_PROFILE_KEY = "甲方测试档"
FAKE_RESUME_NAME = "乙方测试名-母版"


def _line_of(path, n):
    with open(path, encoding="utf-8") as f:
        return f.read().splitlines()[n - 1]


class ProfilePiiTest(unittest.TestCase):
    """没有 .leak-keywords.txt 时，当前 profile 自己的名字也必须被 `verify()` 拦住。"""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="pack-pii-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        for rel, text in (("app.py", FAKE_APP), ("aaa.py", "# aaa\nVALUE = 'aaa'\n"),
                          ("bbb.py", "# bbb\nVALUE = 'bbb'\n"),
                          ("web/x.html", "<html>x</html>\n"), ("web/demo.json", "{}\n")):
            _write(os.path.join(self.root, rel), text)
        self.zip_path = os.path.join(self.root, "out.zip")
        self.assertFalse(os.path.exists(os.path.join(self.root, ".leak-keywords.txt")),
                         "本用例的前提是本地**没有** .leak-keywords.txt（干净检出）")

    # ---- 工具 ----

    def make_profile(self, key=FAKE_PROFILE_KEY, resumes=(FAKE_RESUME_NAME,)):
        """造一个"当前档"：档名 + 简历文件名都用自造假名。"""
        pdir = os.path.join(self.root, "data", "profiles", key)
        os.makedirs(os.path.join(pdir, "resumes"), exist_ok=True)
        _write(os.path.join(self.root, "data", "current_profile.txt"), key)
        for n in resumes:
            _write(os.path.join(pdir, "resumes", n), "# 假简历正文（正文一个字都不许进关键词表）\n")
        return pdir

    def build(self, pii=""):
        """按包里真实的样子造 zip；pii 非空 = 故意把那段字符串夹带进包。"""
        if pii:
            _write(os.path.join(self.root, "aaa.py"), "# 故意夹带：%s\n" % pii)
        with zipfile.ZipFile(self.zip_path, "w") as z:
            for rel, full in PACK.collect(root=self.root, files=FAKE_FILES, dirs=["web"]):
                z.write(full, PKG_NAME + "/" + rel)
        return self.zip_path

    # ---- 需求一：自动并入 profile 自己的 PII ----

    def test_auto_keywords_cover_profile_and_resume_names(self):
        self.make_profile()
        kws = PACK.profile_leak_keywords(self.root)
        self.assertIn(FAKE_PROFILE_KEY, kws, "当前档名必须进关键词表")
        self.assertIn("乙方测试名", kws, "简历文件名必须去扩展名、按分隔符切片后进表")

    def test_verify_blocks_profile_key_without_keyword_file(self):
        self.make_profile()
        issues = PACK.verify(self.build(pii=FAKE_PROFILE_KEY), root=self.root)
        self.assertTrue(any(FAKE_PROFILE_KEY in i for i in issues),
                        "档名进了分享包，verify() 却没拦：%r" % (issues,))

    def test_verify_blocks_resume_name_without_keyword_file(self):
        self.make_profile()
        issues = PACK.verify(self.build(pii=FAKE_RESUME_NAME), root=self.root)
        self.assertTrue(any("乙方测试名" in i for i in issues),
                        "简历名进了分享包，verify() 却没拦：%r" % (issues,))

    def test_resume_content_never_enters_keywords(self):
        """正文不许进关键词表：把正文写成一句独特的话，它不该变成关键词。"""
        pdir = self.make_profile()
        secret = "正文里的独特句子绝不入表"
        _write(os.path.join(pdir, "resumes", FAKE_RESUME_NAME), secret + "\n")
        self.assertNotIn(secret, PACK.profile_leak_keywords(self.root))
        self.assertEqual([], PACK.verify(self.build(pii=secret), root=self.root))

    # ---- 需求二：不许误伤 ----

    def test_verify_passes_when_pack_has_no_pii(self):
        self.make_profile()
        self.assertEqual([], PACK.verify(self.build(), root=self.root))

    def test_one_char_and_short_ascii_keywords_are_not_used(self):
        """1 个汉字 / 2 个字母的组合会把**每个包**都判成泄漏 ⇒ 一律不入表。"""
        self.make_profile(key="ab", resumes=("单-字.md", "c.md"))
        self.assertEqual([], PACK.profile_leak_keywords(self.root))
        self.assertEqual([], PACK.verify(self.build(pii="单 ab c"), root=self.root))

    def test_generic_names_are_not_used(self):
        """包**自己**就会写 `data/profiles/default/resumes/我的简历.md`，
        所以 default / 我的 / 简历 / 母版 这些通用词一旦入表，每个包都会被自己判成泄漏。"""
        self.make_profile(key="default", resumes=("我的简历.md", "简历-母版.md"))
        self.assertEqual([], PACK.profile_leak_keywords(self.root))
        self.assertEqual([], PACK.verify(self.build(pii="data/profiles/default 我的简历"), root=self.root))

    # ---- 需求三（P0-15 第 4 条漏检）：真名与通用词**粘连**（无分隔符）时不许整段丢 ----
    # 复查用隔离探针实测：`张三母版.md` 在旧实现下给出 `[]` —— 中文按"通用词是子串"判，
    # "母版"一命中 ⇒ **整个 token 被丢掉** ⇒ 真名 `张三` 一起丢 ⇒ 带真名的包照样过 `verify()`。
    # 这个形状（真名后面直接跟"母版/终版"、不加连字符）非常常见；本机不触发只是因为
    # 真实档名恰好写成 `真名-母版.md`（中间有连字符）⇒ 属**潜伏**缺陷。

    def _reset_data(self):
        """清掉上一个子用例造的 `data/`：每个子用例一份干净夹具，互不串味。"""
        shutil.rmtree(os.path.join(self.root, "data"), ignore_errors=True)

    def test_sticky_generic_suffix_keeps_the_name(self):
        """`张三母版.md` / `李四终版.md` ⇒ 真名必须留在关键词表里（旧实现：`[]`）。"""
        for resume, want in (("张三母版.md", "张三"), ("李四终版.md", "李四")):
            with self.subTest(resume=resume):
                self._reset_data()
                self.make_profile(key="default", resumes=(resume,))
                self.assertEqual([want], PACK.profile_leak_keywords(self.root),
                                 "%s 里的真名被通用词一起吃掉了" % resume)

    def test_verify_blocks_sticky_name_end_to_end(self):
        """端到端：粘连命名的档，真名进了包也必须被 `verify()` **点名**（不只是表里有）。"""
        self._reset_data()
        self.make_profile(key="default", resumes=("张三母版.md",))
        issues = PACK.verify(self.build(pii="张三"), root=self.root)
        self.assertTrue(any("张三" in i for i in issues),
                        "粘连命名的真名进了分享包，verify() 却没拦：%r" % (issues,))

    def test_separated_name_still_works(self):
        """对照组（本机真实档名的形状）：`张三-母版.md` 照旧给出 `张三`，修复不许碰坏它。"""
        self._reset_data()
        self.make_profile(key="default", resumes=("张三-母版.md",))
        self.assertEqual(["张三"], PACK.profile_leak_keywords(self.root))

    def test_whole_generic_words_are_still_dropped(self):
        """对照组：**整段就是通用词**的仍然一个都不许入表（子串判据当初存在的理由）。

        `我的简历.md` 一旦入表，**每个包都会被它自己判成泄漏**：`main()` 自己就往包里写
        `data/profiles/default/resumes/我的简历.md`，起步简历正文第一行也是「# 我的简历…」。
        ASCII 的 `cv` / `new` 同理（只认整串，且太短 ⇒ 入表就是灾难）。
        """
        self._reset_data()
        self.make_profile(key="default", resumes=("我的简历.md", "cv.md", "new.md"))
        self.assertEqual([], PACK.profile_leak_keywords(self.root))
        self.assertEqual([], PACK.verify(self.build(pii="data/profiles/default 我的简历 cv new"),
                                         root=self.root))

    # ---- 收紧（复查批准的那一刀）：残余若出现在**包自带的样板文本**里 ⇒ 丢弃 ----
    # 安全论证：`STARTER_RESUME` / `SHARE_NOTE` 是本工具**自己的常量**，按构造**不含用户 PII**
    # ⇒ 出现在它们里的词不可能是"用户的真名" ⇒ 丢掉它**不会掩盖任何真实泄漏**。
    # 不丢的代价：`个人终版.md` 这种"限定词 + 版次词"的档名会切出 `个人`（包自带的
    # 分享说明里就写着"仅供个人求职使用"）⇒ verify() 点名、**每个包都被删**。

    def test_residues_that_are_our_own_sample_words_are_dropped(self):
        """正例：`个人终版` / `中文母版` 切出来的 `个人` / `中文` **必须被丢弃**。

        它们都在包自带的样板文本里（`分享说明.md`：仅供个人求职使用 / 中文名照抄）⇒ 留着就是
        "每个包都被自己判成泄漏"（fail-closed 删包），是另一个方向的错。
        """
        for resume in ("个人终版.md", "中文母版.md"):
            with self.subTest(resume=resume):
                self._reset_data()
                self.make_profile(key="default", resumes=(resume,))
                self.assertEqual([], PACK.profile_leak_keywords(self.root),
                                 "%s 切出了包自带样板文本里的词" % resume)
                self.assertEqual([], PACK.verify(self.build(pii="个人 中文"), root=self.root))

    def test_sticky_residue_survives_when_it_is_not_a_sample_word(self):
        """反例（不许削弱）：不在样板文本里的假名粘连（`张三母版`）⇒ `张三` 必须仍然留下。

        与上一条成对：那把刀只砍"我们自己样板文本里的词"，绝不许顺手把真名一起砍掉。
        """
        self.assertNotIn("张三", PACK.STARTER_RESUME + PACK.SHARE_NOTE,
                         "夹具前提被破坏：样板文本里出现了这个假名")
        self._reset_data()
        self.make_profile(key="default", resumes=("张三母版.md",))
        self.assertEqual(["张三"], PACK.profile_leak_keywords(self.root))


class ExamplePlaceholderTest(unittest.TestCase):
    """需求二（P0-15）：README 的简历文件名示例必须是**明显占位**，不许再写真名。"""

    def _real_pii(self):
        """用户自己的真名判据：**只从 data/ 只读**推导，一个字都不落盘、不打印。

        ⚠️ 干净检出（没有 `data/`、或名字全是通用词）时这里**正常**返回 `[]` —— 那**不等于**
        "没有泄漏"。所以调用方必须先看空不空（见 `_assert_no_real_pii`），不许直接 `for` 循环。
        """
        return PACK.profile_leak_keywords(REPO)

    def _rv_example_lines(self):
        """README 里「带 `rv_` 版本号的示例块」的每一行 → `[(行号, 行内容), …]`。

        ⚠️ **按内容定位，不硬编码行号**：2026-10-16 在脚本表里补两行 ⇒ 示例块从 271/272
        位移到 273/274 ⇒ 硬编码行号的那条断言直接转红（那次改动本身是对的，脆的是断言）。
        一块都找不到 ⇒ 直接红（示例块被删了也逃不掉）。
        """
        path = os.path.join(REPO, "README.md")
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
        blocks, inside, cur = [], False, []      # cur = 当前围栏块的行号列表
        for i, ln in enumerate(lines, 1):
            if ln.strip().startswith("```"):
                if inside:
                    blocks.append(cur)
                inside, cur = not inside, []
                continue
            if inside:
                cur.append(i)
        if inside:                                # 围栏没闭合（README 坏了）也当一块看
            blocks.append(cur)
        marked = [b for b in blocks if any("rv_" in _line_of(path, n) for n in b)]
        self.assertTrue(marked, "README 里找不到任何带 `rv_` 版本号的示例块（示例块被删了？）")
        return [(n, _line_of(path, n)) for block in marked for n in block]

    def _assert_no_real_pii(self, text, where):
        """`text` 里不许出现任何真实 PII 关键词。

        ⚠️ 关键词表为空时**显式跳过**（unittest 报 skipped + 这条理由），绝不静默通过：
        旧写法是 `for kw in self._real_pii(): assertNotIn(…)` —— 空表时循环一次都不进，
        「README 不许写真名」这半条**看起来在断言、其实什么都没断言**，没有 `data/` 的
        干净检出照样全绿（独立复查 2026-10-16 点名）。口径同 `tests/test_app.py:6152`：
        读不到真名就把回落**写明白**（跳过并给理由），不留空转的断言。
        """
        kws = self._real_pii()
        if not kws:
            self.skipTest("干净检出（没有 data/，或名字全是通用词）⇒ 推不出『用户自己的真名』，"
                          "『%s 不许写真名』这半条无从断言：显式跳过，不是通过" % where)
        for kw in kws:
            self.assertNotIn(kw, text, "%s 里还留着真实 PII（关键词长度 %d）" % (where, len(kw)))

    def test_readme_resume_example_is_placeholder(self):
        """示例块里凡是带 `rv_` 的那几行必须写明「示例」（占位一眼可辨）。

        这一条**不依赖 `data/`**（占位串本身就能查）⇒ 干净检出上也照跑不误。
        """
        for n, line in self._rv_example_lines():
            if "rv_" in line:
                self.assertIn("示例", line, "README.md:%d 必须写明这是占位示例：%r" % (n, line))

    def test_readme_resume_example_has_no_real_pii(self):
        """示例块里**每一行**都不许出现 profile 真名（P0-15；比原来只扫两行更严）。

        ⚠️ 在干净检出（没有 `data/`）上这条会**显式 skip**（理由见 `_assert_no_real_pii`）——
        旧写法在这种机器上是静默变空断言、全量照样绿，那正是复查点名的地方。
        """
        for n, line in self._rv_example_lines():
            self._assert_no_real_pii(line, "README.md:%d" % n)

    def test_the_no_real_pii_check_is_not_vacuous(self):
        """反证：把关键词表换成 README 示例行里**一定有**的串（「示例」）⇒ 同一个判据必须报错。

        钉的是「回落之后判据本体还活着」：万一它又被写成空转（空循环），这条会红。
        用的是**假串**，不碰真名。
        """
        hit = next(((n, line) for n, line in self._rv_example_lines() if "示例" in line), None)
        self.assertIsNotNone(hit, "README 的示例块里没有『示例』二字，本反证无从构造")
        n, line = hit
        with mock.patch.object(ExamplePlaceholderTest, "_real_pii", return_value=["示例"]):
            with self.assertRaises(AssertionError):
                self._assert_no_real_pii(line, "README.md:%d" % n)


if __name__ == "__main__":
    unittest.main()
