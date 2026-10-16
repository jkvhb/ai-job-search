# -*- coding: utf-8 -*-
"""`报告体检.py` 的**退出码必须说真话**（P1-21）：`--刷新模板` 只要有一份**没能刷新**，
就必须非零退出、并把**是哪一份、为什么**列出来。

⚠️ 本脚本的另一批用例在 `tests/test_migration.py::ReportHealthBackupTest`
（P0-12：备份挪出被校验的 `reports/`、说清路径与体积、备份里不套备份）—— 那批钉的是备份，
本文件钉的是**退出码**与「跳过 vs 失败」的判据，两边互补、不重复。

判据（与 `报告体检.py` 里 `main()` 的注释同一口径，三选一，互斥）：
  * **成功**（`rerender → True`）：这一份真的被重写了；
  * **正常跳过**（`rerender → False`）：盘上那份与当前模板渲染出来的**逐字相同**
    ⇒ 一个字节都不用写（幂等带来的正常结果，**绝不算失败** —— 一组报告全是"已是当前模板"时
    退出码必须是 0，否则天天误报，人就会开始无视这个退出码）；
  * **失败**（→ 退出码 1）：这一份**没能刷新**，分两种、都要点名：
      ① 读不出 `REPORT_DATA`（这份文件不是本工具能刷的报告 / 已损坏）—— 工具对它**什么都没做**，
         不能算"完成"；
      ② 渲染/写盘时抛异常（模板读不出来、只读文件、磁盘满…）—— 连原因一起点名。

所有用例都在 `IsolatedCase` 的隔离根（`tempfile.mkdtemp()`）里跑，**绝不碰真实 `data/`**。
"""
import contextlib
import importlib
import io
import json
import os
import sys
import unittest
from unittest import mock

import store

from tests.base import IsolatedCase

REPORT_CLI = importlib.import_module("报告体检")

# 临时模板：本文件钉的是退出码，**不该**被 `web/report_template.html` 的并发改动牵连
# （与 `tests/test_migration.py::ReportHealthBackupTest` 同一做法）。
TEMPLATE_TEXT = ("<html><head><title>__TITLE__</title></head><body>"
                 "<script>\nconst REPORT_DATA = __REPORT_DATA__;\n</script></body></html>")
# 旧骨架：比当前模板多一行注释 ⇒ 渲染结果与盘上那份**不同**，于是这一份**需要**被重写。
# （用①"内容有差异"来逼出真正的写盘，别让用例在"已是当前模板"的支路上空跑。）
OLD_SKELETON = ("<html><head><title>%s</title></head><body><!-- 旧骨架 --><script>\n"
                "const REPORT_DATA = %s;\n</script></body></html>")


class ReportHealthExitCodeTest(IsolatedCase):
    """"有失败就必须非零退出 + 点名"（P1-21）；"全部成功/正常跳过"仍必须 0。"""

    def setUp(self):
        IsolatedCase.setUp(self)
        os.makedirs(store.p_path("reports"), exist_ok=True)
        self.tpl = os.path.join(self.tmp, "report_template.html")
        store.write_text(self.tpl, TEMPLATE_TEXT)
        patcher = mock.patch.object(REPORT_CLI, "TEMPLATE", self.tpl)
        patcher.start()
        self.addCleanup(patcher.stop)

    # ---------------- 夹具 ----------------
    def data(self):
        return {"job_title": "语音/文本评测", "score": 70.0,
                "knowledge": {"nodes": [{"term": "Transformer", "sources": []}]}}

    def write_report(self, name, skeleton=OLD_SKELETON):
        """按给定骨架写一份**能被解析出 REPORT_DATA** 的报告，返回绝对路径。"""
        path = os.path.join(store.p_path("reports"), name)
        store.write_text(path, skeleton % (name, json.dumps(self.data(), ensure_ascii=False)))
        return path

    def write_current_report(self, name):
        """写一份**已经就是当前模板渲染出来的**报告（幂等支路：渲染结果与盘上逐字相同）。"""
        path = self.write_report(name)
        store.write_text(path, REPORT_CLI.render(path))
        return path

    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", ["报告体检.py"] + argv):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = REPORT_CLI.main()
        return code, out.getvalue(), err.getvalue()

    # ---------------- 失败：必须非零 + 点名 ----------------
    def test_a_html_without_report_data_is_a_failure_not_a_silent_skip(self):
        """读不出 REPORT_DATA 的 `.html` ⇒ **非零退出**且点名（老实现：只 print + `return 0`）。

        为什么这算失败而不是"正常跳过"：它躺在 `reports/` 里（本该是报告），工具对它
        **一个字节都没动** —— 此时说"模板刷新完成"并退出 0，就是一句假话。
        """
        self.write_report("好报告.html")
        store.write_text(os.path.join(store.p_path("reports"), "坏报告.html"),
                         "<html><body>这不是一份报告：没有 REPORT_DATA</body></html>")

        code, text, _err = self.run_cli(["--刷新模板"])

        self.assertEqual(code, 1, "有一份读不出 REPORT_DATA 却退出了 0：\n%s" % text)
        self.assertIn("坏报告.html", text, "失败了却没点名：\n%s" % text)

    def test_a_write_failure_is_a_failure_and_names_the_reason(self):
        """写盘抛异常（只读文件 / 磁盘满…）⇒ 非零退出 + **连原因一起**点名。"""
        path = self.write_report("写不进去.html")
        real_write = store.write_text

        def boom(target, content):
            if os.path.abspath(target) == os.path.abspath(path):
                raise OSError("注入：磁盘空间不足")
            return real_write(target, content)

        with mock.patch.object(REPORT_CLI.store, "write_text", side_effect=boom):
            code, text, _err = self.run_cli(["--刷新模板"])

        self.assertEqual(code, 1, "写盘失败却退出了 0：\n%s" % text)
        self.assertIn("写不进去.html", text, "失败了却没点名：\n%s" % text)
        self.assertIn("磁盘空间不足", text, "没把失败原因说出来：\n%s" % text)

    def test_a_failure_is_reported_even_when_fix_is_also_requested(self):
        """`--刷新模板 --fix` 同时带上时，模板刷新失败**照样**必须非零退出。"""
        self.write_report("好报告.html")
        store.write_text(os.path.join(store.p_path("reports"), "坏报告.html"),
                         "<html><body>没有 REPORT_DATA</body></html>")

        code, text, _err = self.run_cli(["--刷新模板", "--fix"])

        self.assertEqual(code, 1, "--fix 把模板刷新的失败吞掉了：\n%s" % text)
        self.assertIn("坏报告.html", text)

    # ---------------- 对照组：不许误判 ----------------
    def test_all_reports_refreshed_returns_zero(self):
        """全部成功 ⇒ 0（而且报告**真的**被换了骨架 —— 否则这三条断言全是空跑）。"""
        a = self.write_report("甲.html")
        b = self.write_report("乙.html")

        code, text, _err = self.run_cli(["--刷新模板"])

        self.assertEqual(code, 0, "全部成功却退出了非零：\n%s" % text)
        for path in (a, b):
            self.assertNotIn("旧骨架", store.read_text(path), "这一份根本没被刷新：%s" % path)

    def test_a_report_already_on_current_template_is_a_normal_skip_not_a_failure(self):
        """正常跳过：盘上那份**已经是**当前模板渲染出来的 ⇒ 退出码 0、一个字节都不改。

        这是"跳过 vs 失败"的**对照组**：谁要是把这一种也判成失败（或反过来把失败判成跳过），
        这条就会红 —— 两种情形必须真的分得开。
        """
        path = self.write_current_report("已是最新.html")
        before = store.read_text(path)

        code, text, _err = self.run_cli(["--刷新模板"])

        self.assertEqual(code, 0, "「已是当前模板」被误判成失败了（会天天误报）：\n%s" % text)
        self.assertEqual(store.read_text(path), before, "无事可做却改了文件（幂等破了）")


if __name__ == "__main__":
    unittest.main()
