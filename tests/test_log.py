# -*- coding: utf-8 -*-
import json
import os
import unittest

import log
import store

from tests.base import IsolatedCase


class RedactTest(unittest.TestCase):
    def test_masks_api_key_field(self):
        self.assertEqual(log.redact({"api_key": "sk-abcdef1234567890"})["api_key"], "***已打码***")

    def test_masks_nested_token(self):
        self.assertEqual(log.redact({"a": {"token": "xyz"}})["a"]["token"], "***已打码***")

    def test_masks_key_inside_string(self):
        out = log.redact("用 sk-abcdef1234567890 调用")
        self.assertNotIn("abcdef1234567890", out)
        self.assertIn("***", out)

    def test_keeps_normal_values(self):
        self.assertEqual(log.redact({"n": 1, "s": "hello"}), {"n": 1, "s": "hello"})


class LogEventTest(IsolatedCase):
    def test_log_event_appends_jsonl(self):
        log.log_event("t.one", a=1)
        log.log_event("t.two", level="warn", b=2)
        lines = [l for l in open(log.LOG_FILE, encoding="utf-8").read().splitlines() if l.strip()]
        self.assertEqual(len(lines), 2)
        first = json.loads(lines[0])
        self.assertEqual(first["event"], "t.one")
        self.assertEqual(first["data"]["a"], 1)

    def test_read_logs_returns_recent_and_total(self):
        for i in range(5):
            log.log_event("e%d" % i)
        recent, total = log.read_logs(2)
        self.assertEqual(total, 5)
        self.assertEqual(len(recent), 2)
        self.assertEqual(recent[-1]["event"], "e4")

    def test_log_exc_records_traceback(self):
        try:
            raise ValueError("boom")
        except ValueError as e:
            log.log_exc("t.err", e)
        recent, _ = log.read_logs(1)
        self.assertIn("boom", recent[0]["data"]["error"])
        self.assertIn("Traceback", recent[0]["data"]["traceback"])


class LogFilePathTest(IsolatedCase):
    """档名文件（`current_profile.txt`）**存在却读不出来**时，日志路径解析怎么办。

    **本文件选定的行为：`log_file_path()` 让 `store.ProfileUnreadable` 原样冒出去**，
    不自己包一层更清楚的异常。理由有两条：
      ① store 抛的那个异常已经带齐「哪个文件 / 为什么 / 怎么修」，再包一层只会让调用方
         多猜一层类型，文案还容易两头改一头忘；
      ② 精确的子类让 app 的启动校验能**只**接这一种（`except store.ProfileUnreadable`），
         不靠文案匹配 —— 顺手写成 `except Exception` 就会把真 bug 一起吞掉，
         那是这次改动最容易引入的坏味道。

    **但 import 期不能抛**：log 是所有模块的公共依赖（jdfacts / jddelete / jdid / jobdup / kb …
    都 import 它，而它们又都在 app 之前被 import），lib 里一抛，`import app` 连自己的启动
    校验都走不到，用户看到的仍是一坨 traceback。所以 import 期只把错误**原样留档**在
    `LOG_PATH_ERROR`，交给 app 统一印人话 + 非零退出 —— 留档 ≠ 吞掉。
    """

    def setUp(self):
        super().setUp()
        # 「留档」是模块级状态：不还原的话会漏进别的用例
        self.addCleanup(setattr, log, "LOG_PATH_ERROR", getattr(log, "LOG_PATH_ERROR", None))

    def _gbk_profile_file(self):
        """复现真实形态：记事本按 ANSI/GBK 存了中文档名。"""
        fp = store.current_profile_file()
        with open(fp, "w", encoding="gbk") as f:
            f.write("周汉钦")
        with open(fp, "rb") as f:
            raw = f.read()
        # 反证：这份文件确实**不是**合法的 UTF-8（否则「读不出来」这条用例本身是假的）
        self.assertRaises(UnicodeDecodeError, raw.decode, "utf-8")
        return fp

    def test_log_file_path_raises_profile_unreadable(self):
        self._gbk_profile_file()
        self.assertRaises(store.ProfileUnreadable, log.log_file_path)

    def test_import_time_resolution_records_the_error_instead_of_raising(self):
        """import 期（模块级 `LOG_FILE = _initial_log_file()`）必须活下来，但错误要留档。"""
        fp = self._gbk_profile_file()
        self.assertEqual(log._initial_log_file(), "")      # 占位，绝不是 default 的路径
        self.assertIsInstance(log.LOG_PATH_ERROR, store.ProfileUnreadable)
        # 留档的错误要指得出是哪个文件坏了，app 的人话才有东西可印
        self.assertIn(fp, str(log.LOG_PATH_ERROR))

    def test_refresh_clears_the_recorded_error_once_the_file_is_readable_again(self):
        self._gbk_profile_file()
        log._initial_log_file()
        self.assertIsNotNone(log.LOG_PATH_ERROR)
        os.remove(store.current_profile_file())            # 删掉 = 回到 default
        self.assertEqual(log.refresh(), os.path.join(store.profile_dir(), "logs", "events.jsonl"))
        self.assertIsNone(log.LOG_PATH_ERROR)


if __name__ == "__main__":
    unittest.main()
