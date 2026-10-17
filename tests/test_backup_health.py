# -*- coding: utf-8 -*-
"""`备份体检.py`：备份看得见 + **只能点名删一个** + 安全闸门 + 默认只读。

为什么最重的用例是"安全闸门"（第 2 条）：这个脚本**会删目录**，而它就住在用户数据
（`<profiles 根>` 下的各个 profile 目录）的**隔壁**。项目里已经出过一次"清空用户 145 张卡"
的事故，所以这里的判据一律是**两条一起**：
  ① 返回值/退出码说"拒绝"；② **盘上逐项没变**（连"别处那个绝对路径"也没变）。
只断言①的话，一个"先删再报错"的实现照样全绿。

命名规则（4 类整目录备份，都在 `<profiles 根>` 顶层，是 profile 目录的**兄弟**）：
  - `简历迁移备份_<YYYYmmdd_HHMMSS>` ← `简历迁移.py`
  - `迁移备份_<YYYYmmdd_HHMMSS>`     ← `身份迁移.py`
  - `报告备份_<YYYYmmdd_HHMMSS>`     ← `报告体检.py`
  - `删除备份_<YYYYmmdd_HHMMSS>`     ← `jddelete.py`
（同一秒重名时后面再补 `_<微秒>` ⇒ 时间戳两段/三段都认。）

所有用例都在 `IsolatedCase` 的隔离根里跑（`store.DATA_ROOT = tempfile.mkdtemp()`）：
即使被测代码把"默认 root"当成真实 `data/profiles`，它拿到的也是临时目录 —— 真实
`data/` 一个字节都不会被这个文件碰到。
"""
import contextlib
import importlib
import io
import os
import shutil
import tempfile
import unittest

import store

from tests.base import IsolatedCase

CLI = importlib.import_module("备份体检")

# 夹具用的两个备份名（时间戳是**假**的，只为了"可读时间"能断言）
RESUME_BACKUP = "简历迁移备份_20261013_191537"
DELETE_BACKUP = "删除备份_20261001_000000"
OTHER_ROOT_BACKUP = "报告备份_20260101_000000"


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _tree(root):
    """隔离根的**完整形状**：`{相对路径: 大小}` + `{相对目录路径}`。

    目录也要进快照：光比文件的话，"顺手多建/少建一个空目录"这种改动看不出来，
    而备份目录**恰恰可能就是空的**。
    """
    files, dirs = {}, set()
    for dirpath, dirnames, filenames in os.walk(root):
        for d in dirnames:
            dirs.add(os.path.relpath(os.path.join(dirpath, d), root))
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            files[os.path.relpath(p, root)] = os.path.getsize(p)
    return {"files": files, "dirs": sorted(dirs)}


def _names(entries):
    return [e["name"] for e in entries]


class BackupHealthTest(IsolatedCase):
    """体检列表 / 安全闸门 / 点名删除 / 只读默认 / 退出码。"""

    def setUp(self):
        IsolatedCase.setUp(self)
        # 直接拿**隔离根**当 `<profiles 根>`：这样"默认 root"与"显式 root"是同一条路，
        # 用例不必两边各写一套夹具（同时也钉住"默认走 store.profiles_root()"）。
        self.root = store.profiles_root()
        os.makedirs(self.root, exist_ok=True)
        # 一个含子目录与若干文件的备份（15 字节 / 2 个文件）
        _write(os.path.join(self.root, RESUME_BACKUP, "reports", "a.html"), "abcde")
        _write(os.path.join(self.root, RESUME_BACKUP, "jd", "b.txt"), "0123456789")
        # 一个**空**备份
        os.makedirs(os.path.join(self.root, DELETE_BACKUP), exist_ok=True)
        # profile 目录（用户数据本体：绝不许进可删清单、绝不许被删）
        _write(os.path.join(self.root, "default", "knowledge.json"), "知识库正文")
        os.makedirs(os.path.join(self.root, "default", "resumes"), exist_ok=True)
        # 一个与备份毫无关系的文件
        _write(os.path.join(self.root, "随便.txt"), "无关")

    # ---------------- 工具 ----------------
    def run_cli(self, argv, root=...):
        """跑 `main()`，返回 `(退出码, 输出文本)`；`root=...` 表示"不传 root"（走默认）。"""
        out = io.StringIO()
        kwargs = {} if root is ... else {"root": root}
        with contextlib.redirect_stdout(out):
            code = CLI.main(list(argv), **kwargs)
        return code, out.getvalue()

    def assert_tree_unchanged(self, before, where):
        self.assertEqual(before, _tree(self.root), "%s：盘上的东西被改动了" % where)

    # ---------------- 1. 体检列表：恰好那些备份 ----------------
    def test_scan_lists_exactly_the_backups_with_stats(self):
        """列表**恰好**是那 2 个备份：不多列 profile 目录、不多列无关文件；体积/文件数/时间对。"""
        entries = CLI.scan(self.root)

        self.assertEqual([DELETE_BACKUP, RESUME_BACKUP], _names(entries),
                         "体检列表不是「恰好那 2 个备份」（多了 profile 目录 / 无关文件，或漏了备份）")
        by_name = {e["name"]: e for e in entries}
        res = by_name[RESUME_BACKUP]
        self.assertEqual(2, res["files"], "文件数（含子目录里的）")
        self.assertEqual(15, res["bytes"], "体积（含子目录里的）")
        self.assertEqual("15 B", res["size"], "体积要给人话")
        self.assertEqual("2026-10-13 19:15:37", res["ts"], "时间戳要能读成人话")
        self.assertTrue(res["kind"], "每条都要说清它是哪个脚本建的")
        self.assertIn("简历迁移", res["kind"])
        empty = by_name[DELETE_BACKUP]
        self.assertEqual(0, empty["files"])
        self.assertEqual(0, empty["bytes"])
        self.assertEqual("0 B", empty["size"])
        self.assertEqual("2026-10-01 00:00:00", empty["ts"])
        for e in entries:
            self.assertNotIn("default", e["name"])
            self.assertNotIn("随便.txt", e["name"])

    def test_scan_sees_a_microsecond_timestamp(self):
        """同一秒重名时补微秒（`_<微秒>`）的名字也是合法备份，时间要读对。"""
        name = "报告备份_20261014_101112_000123"
        os.makedirs(os.path.join(self.root, name), exist_ok=True)
        entries = {e["name"]: e for e in CLI.scan(self.root)}
        self.assertIn(name, entries, "带微秒的备份名被漏掉了")
        self.assertEqual("2026-10-14 10:11:12", entries[name]["ts"])

    def test_report_says_which_profile_is_not_deletable(self):
        """体检报告末尾要点名：当前 profile 目录是哪一个、它**不在**可删清单里。"""
        code, text = self.run_cli([], root=self.root)
        self.assertEqual(0, code, "只体检必须退出 0：\n%s" % text)
        self.assertIn("default", text)
        self.assertIn(RESUME_BACKUP, text)
        self.assertIn(DELETE_BACKUP, text)
        self.assertIn("合计", text)

    # ---------------- 2. 安全闸门（最重要）----------------
    def _refused_forms(self):
        other = tempfile.mkdtemp(prefix="backup-health-other-")
        self.addCleanup(shutil.rmtree, other, ignore_errors=True)
        os.makedirs(os.path.join(other, OTHER_ROOT_BACKUP), exist_ok=True)
        _write(os.path.join(other, OTHER_ROOT_BACKUP, "别人的数据.txt"), "别处的东西")
        # "名字像但不是备份"：前缀不认识 / 时间戳不成形 / 时间戳不是真实时刻 / 不是目录
        os.makedirs(os.path.join(self.root, "我的备份_20260101_000000"), exist_ok=True)
        os.makedirs(os.path.join(self.root, "报告备份_2026"), exist_ok=True)
        os.makedirs(os.path.join(self.root, "报告备份_20261399_999999"), exist_ok=True)
        _write(os.path.join(self.root, "删除备份_20260101_000000"), "这不是目录")
        return other, [
            ("default", "profile 目录"),
            (os.path.join(self.root, "default"), "profile 目录的绝对路径"),
            (self.root, "profiles 根本身"),
            ("..", "越级"),
            ("../x", "越级"),
            (os.path.join("..", "报告备份_20260101_000000"), "越级"),
            (os.path.join(other, OTHER_ROOT_BACKUP), "指向别处的绝对路径"),
            ("", "空名字"),
            ("我的备份_20260101_000000", "前缀不认识"),
            ("报告备份_2026", "时间戳不成形"),
            ("报告备份_20261399_999999", "时间戳不是真实时刻"),
            ("删除备份_20260101_000000", "名字像备份但不是目录"),
        ]

    def test_every_dangerous_form_is_refused_and_nothing_changes(self):
        """逐条断言**被拒绝**且**盘上逐项没变**（含别处那个绝对路径所指的目录）。"""
        other, forms = self._refused_forms()
        before_self, before_other = _tree(self.root), _tree(other)
        for name, label in forms:
            with self.subTest(form=label):
                path, why = CLI.check_deletable(name, self.root)
                self.assertIsNone(path, "「%s」（%s）本该被闸门拒绝，却给出了可删路径" % (name, label))
                self.assertTrue(why, "拒绝了却没说原因：%s" % label)
                code, text = self.run_cli(["--删除", name], root=self.root)
                self.assertNotEqual(0, code, "「%s」（%s）本该非零退出：\n%s" % (name, label, text))
                self.assertIn("拒绝", text)
                self.assert_tree_unchanged(before_self, label)
                self.assertEqual(before_other, _tree(other),
                                 "「%s」（%s）把**别处**的目录动了" % (name, label))

    def test_the_gate_accepts_only_the_exact_backup_name(self):
        """对照组：**确切名字**必须被放行（闸门不是"一律拒绝"）。"""
        path, why = CLI.check_deletable(RESUME_BACKUP, self.root)
        self.assertEqual("", why)
        self.assertEqual(os.path.join(self.root, RESUME_BACKUP), path)

    def test_a_symlink_is_refused(self):
        """名字合法、但它是**链接** ⇒ 拒绝（顺着它删会把链接指向的那个目录删掉）。"""
        link = os.path.join(self.root, "报告备份_20260202_020202")
        target = os.path.join(self.root, "default")
        try:
            os.symlink(target, link, target_is_directory=True)
        except (OSError, NotImplementedError, AttributeError) as e:      # 权限不足 / 平台不支持
            self.skipTest("本机建不出符号链接（%s）⇒ 这条无从断言，显式跳过" % e)
        before = _tree(self.root)
        path, why = CLI.check_deletable("报告备份_20260202_020202", self.root)
        self.assertIsNone(path)
        self.assertTrue(why)
        code, _text = self.run_cli(["--删除", "报告备份_20260202_020202"], root=self.root)
        self.assertNotEqual(0, code)
        self.assert_tree_unchanged(before, "链接")
        self.assertTrue(os.path.isdir(target), "链接指向的 profile 目录被删了")

    # ---------------- 3. 点名删除的正常路径 ----------------
    def test_delete_removes_only_the_named_backup(self):
        code, text = self.run_cli(["--删除", RESUME_BACKUP], root=self.root)

        self.assertEqual(0, code, "点名删一个真备份必须成功：\n%s" % text)
        self.assertFalse(os.path.exists(os.path.join(self.root, RESUME_BACKUP)), "点名的那个没删掉")
        self.assertTrue(os.path.isdir(os.path.join(self.root, DELETE_BACKUP)), "别的备份被连坐了")
        self.assertTrue(os.path.isfile(os.path.join(self.root, "default", "knowledge.json")),
                        "profile 目录被连坐了")
        self.assertTrue(os.path.isfile(os.path.join(self.root, "随便.txt")), "无关文件被连坐了")
        self.assertIn("将删除", text, "动手之前必须先把「将删除什么」打出来")

    def test_delete_a_backup_without_passing_root_uses_store_profiles_root(self):
        """不传 root 时走 `store.profiles_root()`（**不许写死字符串**）。"""
        code, _text = self.run_cli(["--删除", DELETE_BACKUP])
        self.assertEqual(0, code)
        self.assertFalse(os.path.exists(os.path.join(self.root, DELETE_BACKUP)))

    # ---------------- 4. 对照组：不带 --删除 一个字节都不改 ----------------
    def test_plain_run_changes_nothing_on_disk(self):
        before_files = sum(len(fs) for _r, _d, fs in os.walk(self.root))
        before = _tree(self.root)
        code, _text = self.run_cli([], root=self.root)
        self.assertEqual(0, code)
        self.assert_tree_unchanged(before, "只体检")
        self.assertEqual(before_files, sum(len(fs) for _r, _d, fs in os.walk(self.root)))

    # ---------------- 5. 退出码 ----------------
    def test_exit_codes(self):
        code, _text = self.run_cli([], root=self.root)
        self.assertEqual(0, code, "体检成功 = 0")

        code, text = self.run_cli(["--删除", "报告备份_20260101_000000"], root=self.root)
        self.assertNotEqual(0, code, "目标不存在必须非零：\n%s" % text)

        code, text = self.run_cli(["--删除"], root=self.root)
        self.assertNotEqual(0, code, "没给名字必须非零：\n%s" % text)

        code, text = self.run_cli(["--删除", RESUME_BACKUP, "--删除", DELETE_BACKUP], root=self.root)
        self.assertNotEqual(0, code, "一次只许点名一个：\n%s" % text)
        self.assertTrue(os.path.isdir(os.path.join(self.root, RESUME_BACKUP)),
                        "被拒绝的那一次不许先删掉第一个")

    def test_no_auto_clean_mode_exists(self):
        """**绝不自动清理**：任何"清理全部 / 按时间删旧的"选项都不存在，而且不许删东西。"""
        before = _tree(self.root)
        for argv in (["--清理全部"], ["--清理"], ["--删除旧备份"], ["--删除", "--all"],
                     ["--删除", "*"], ["--删除", "报告备份_*"]):
            with self.subTest(argv=argv):
                code, text = self.run_cli(argv, root=self.root)
                self.assertNotEqual(0, code, "本工具不该有这种模式：%s\n%s" % (argv, text))
                self.assertIn("拒绝", text)
                self.assert_tree_unchanged(before, "参数 %s" % argv)

    # ---------------- 旁挂留档：看得见、但不在可删清单里 ----------------
    def test_nested_archives_are_counted_but_never_deletable(self):
        """`<文件>.bak-<时间戳>` 与 resumelib 的 `.stale-*` 要**看得见**，但**不可删**。"""
        _write(os.path.join(self.root, "default", "knowledge.json.bak-20261012_131205_209457"), "旧")
        _write(os.path.join(self.root, "default", "resumes", "快照", "rv_0001.md.stale-0a1b2c3d"), "旧")
        notes = {n["kind"]: n for n in CLI.nested_notes(self.root)}
        text = " ".join(notes)
        self.assertTrue(any(store.BACKUP_SUFFIX in k for k in notes), "`.bak-` 留档没数出来：%s" % text)
        self.assertTrue(any(".stale-" in k for k in notes), "`.stale-` 旁挂没数出来：%s" % text)
        self.assertTrue(all(n["count"] >= 1 for n in notes.values()), "计数不对：%s" % notes)

        before = _tree(self.root)
        for name, _label in (("knowledge.json.bak-20261012_131205_209457", "留档文件"),
                             ("rv_0001.md.stale-0a1b2c3d", "旁挂文件"),
                             (os.path.join("default", "resumes", "快照"), "旁挂所在的目录")):
            self.assertNotEqual("", CLI.check_deletable(name, self.root)[1],
                                "「%s」（%s）不该可删" % (name, _label))
        self.assert_tree_unchanged(before, "旁挂留档")


if __name__ == "__main__":
    unittest.main()
