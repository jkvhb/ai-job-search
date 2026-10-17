# -*- coding: utf-8 -*-
import contextlib
import io
import json
import os
import re
import shutil
import unittest
import zipfile
from unittest import mock

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


# ---------------------------------------------------------------- P1-8 打码口径
class KeyMaskingTest(unittest.TestCase):
    """`KEY_PAT` 必须覆盖本项目**真实用到**的 Key 形态（P1-8）。

    本项目实际会写进日志的凭证（见 `search.py` / `llm.py` / `README.md`）：
      - 模型调用（OpenAI 兼容，DeepSeek / Kimi）→ `sk-...`
      - Tavily 搜索 → `tvly-...`（**旧实现漏的就是它**：说明书写着"已打码"，实际原样落盘）
      - 博查搜索 → 同样走 `Authorization: Bearer <key>` 头，配置里可能是 `sk-` / `bsk-`
    所以规则定为：「`sk-` / `tvly-` / `bsk-` 前缀的长 key（保留前 8 位）」+「`Bearer <16 位以上长串>`」。

    ⚠️ 正例之外**必须**有反例：扩大正则最容易顺手把正常文本改坏
    （旧实现没有词边界，`task-1234567890` 会被打成 `ta` + `sk-1234***`）。
    """

    def test_masks_sk_key_inside_string(self):
        out = log.redact("用 sk-abcdef1234567890 调用")
        self.assertNotIn("abcdef1234567890", out)
        self.assertIn("***", out)

    def test_masks_tavily_key_inside_string(self):
        out = log.redact("tavily 返回 401，key tvly-dev-abcdef1234567890 已失效")
        self.assertNotIn("abcdef1234567890", out, "tvly- 开头的 Key 必须被打码")
        self.assertIn("***", out)

    def test_masks_bsk_key_inside_string(self):
        out = log.redact("bocha key bsk-abcdef1234567890 已过期")
        self.assertNotIn("abcdef1234567890", out)

    def test_masks_bearer_token(self):
        out = log.redact("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payloadpart.sigpart")
        self.assertNotIn("eyJhbGciOiJIUzI1NiJ9", out, "Bearer 后面的长串必须被打码")
        self.assertIn("Bearer ***", out)

    def test_masks_key_inside_nested_container(self):
        out = log.redact({"providers": [{"note": "tvly-abcdef1234567890"}]})
        self.assertNotIn("abcdef1234567890", json.dumps(out, ensure_ascii=False))

    def test_masks_key_glued_to_chinese_text_without_a_space(self):
        """汉字**紧邻**在 Key 前面（没隔空格）时也必须打码。

        ⚠️ 这条钉的是 `\\b` 的 Unicode 语义：`str` 模式下的 `\\w` 把汉字也算**单词字符**，
        于是 `密钥tvly-…` 里 `钥`|`t` 之间**没有**词边界 —— 只见 `\\b` 的正则会把这种
        Key 原样放过去。日志里的提示语正是这种写法（"搜索失败：密钥tvly-…" 不隔空格），
        所以它是 P1-8「只打码 sk-」的同一个坑再犯一次：说明书写"已自动打码"、实际漏。
        反例（`task-…` 不许被改坏）见下面两个用例 —— 修法必须是"只挡 ASCII 字母数字"，
        不是干脆把边界判断删掉。
        """
        out = log.redact("搜索失败：密钥tvly-abcdef1234567890 已失效")
        self.assertNotIn("abcdef1234567890", out, "汉字紧邻的 tvly- Key 必须被打码")
        self.assertIn("***", out)

        out = log.redact("上一轮 sk-abcdef1234567890 已过期")
        self.assertNotIn("abcdef1234567890", out, "汉字紧邻的 sk- Key 必须被打码")

        # 同一个坑对 `Bearer` 也一样：`认证Bearer <长串>` 没有词边界
        out = log.redact("调用返回401认证Bearer eyJhbGciOiJIUzI1NiJ9.payloadpart.sigpart")
        self.assertNotIn("eyJhbGciOiJIUzI1NiJ9", out, "汉字紧邻的 Bearer 长串必须被打码")

    # ---- 反例：不该打码的正常文本，一个字都不许动 ----
    def test_leaves_normal_text_with_dashes_alone(self):
        for text in ("task-1234567890 已关闭",
                     "risk-1234567890 是风险编号",
                     "disk-1234567890 空间不足",
                     "职位编号 2024-1234567890",
                     "https://example.com/job?id=1234567890123"):
            self.assertEqual(log.redact(text), text, "正常文本被改坏了：%s" % text)

    def test_leaves_too_short_or_prefix_only_strings_alone(self):
        for text in ("Bearer token", "sk-abc", "tvly-1", "请把 sk- 换成真 key", "bearer 认证失败"):
            self.assertEqual(log.redact(text), text, "短串/前缀本身不该被打码：%s" % text)


# ---------------------------------------------------------------- P1-7 口径一致
class ReadLogsTotalsTest(IsolatedCase):
    """`total` 与 `records` 必须是**同一口径**（P1-7）。

    选定的口径：**`total` = 能解析出来的记录总条数**（不受 `limit` 影响），
    `records` = 其中最后 `limit` 条 ⇒ 恒等式 `len(records) == min(limit, total)`。
    行数口径另给（`lines` / `blank_lines` / `bad_lines`），
    所以"文件里有 5 行、只有 3 条能解析"这件事**本身可见**，而不是被悄悄抹平。
    """

    def _write(self, lines):
        os.makedirs(os.path.dirname(log.LOG_FILE), exist_ok=True)
        with open(log.LOG_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    def _mixed(self):
        """5 行：3 条好记录 + 1 行空行 + 1 行坏 JSON。"""
        self._write([json.dumps({"event": "e1"}), "", "{这不是 JSON",
                     json.dumps({"event": "e2"}), json.dumps({"event": "e3"})])

    def test_total_counts_parsable_records_not_lines(self):
        self._mixed()
        page = log.read_logs(10)
        self.assertEqual(page.lines, 5)
        self.assertEqual(page.blank_lines, 1)
        self.assertEqual(page.bad_lines, 1)
        self.assertEqual(page.total, 3, "total 必须是能解析的条数，不是文件行数")
        self.assertEqual(len(page.records), 3)

    def test_line_accounting_is_an_exact_identity(self):
        """两个数不许"看起来差不多"：必须有确定的恒等式。"""
        self._mixed()
        page = log.read_logs(10)
        self.assertEqual(page.total + page.blank_lines + page.bad_lines, page.lines)

    def test_total_is_independent_of_limit_and_matches_records(self):
        self._mixed()
        for limit in (1, 2, 3, 5, 10):
            page = log.read_logs(limit)
            self.assertEqual(page.total, 3, "total 不该被 limit 截断（limit=%d）" % limit)
            self.assertEqual(len(page.records), min(limit, page.total),
                             "len(records) 必须等于 min(limit, total)（limit=%d）" % limit)
        self.assertEqual(log.read_logs(2).records[-1]["event"], "e3")
        # limit<=0 视为不截断（与旧实现 limit=0 的行为一致）
        self.assertEqual(len(log.read_logs(0).records), 3)

    def test_two_value_unpacking_still_works(self):
        self._mixed()
        recent, total = log.read_logs(2)
        self.assertEqual(total, 3)
        self.assertEqual(len(recent), 2)


# ---------------------------------------------------------------- P1-6 读不出来 ≠ 空
class ReadLogsUnreadableTest(IsolatedCase):
    """日志文件**存在却读不出来**时，绝不许被读成"共 0 条日志"（P1-6）。"""

    def _write_bytes(self, raw):
        os.makedirs(os.path.dirname(log.LOG_FILE), exist_ok=True)
        with open(log.LOG_FILE, "wb") as f:
            f.write(raw)

    def _gbk_bytes(self):
        """真实形态：记事本按 ANSI/GBK 存过的日志（非 UTF-8）。"""
        raw = "中文日志内容".encode("gbk")
        self.assertRaises(UnicodeDecodeError, raw.decode, "utf-8")   # 反证：这份字节真的读不出来
        return raw

    def test_missing_file_is_a_true_zero(self):
        page = log.read_logs(10)
        self.assertEqual(page.records, [])
        self.assertEqual(page.total, 0)
        self.assertIsNone(page.error, "文件不存在 = 真的没有日志，不是读不出来")
        self.assertTrue(page.readable)

    def test_empty_file_is_a_true_zero(self):
        self._write_bytes(b"")
        page = log.read_logs(10)
        self.assertEqual(page.total, 0)
        self.assertIsNone(page.error, "0 字节 = 真的没有日志，不是读不出来")

    def test_existing_but_unreadable_is_distinguishable_from_a_true_zero(self):
        self._write_bytes(self._gbk_bytes())
        self.assertGreater(os.path.getsize(log.LOG_FILE), 0)         # 反证：盘上确实有内容

        page = log.read_logs(10)
        self.assertFalse(page.readable, "存在却读不出来，必须能与'真的是 0 条'区分")
        self.assertIsNotNone(page.error)
        self.assertIn(log.LOG_FILE, page.error, "原因里要指得出是哪个文件")
        # 外形不变：仍然可解包成 (records, total)，但 error 把真相带出去了
        records, total = page
        self.assertEqual(records, [])
        self.assertEqual(total, 0)

    def test_unreadable_is_not_the_same_object_state_as_empty(self):
        self._write_bytes(b"")
        empty = log.read_logs(10)
        self._write_bytes(self._gbk_bytes())
        broken = log.read_logs(10)
        self.assertNotEqual(bool(empty.error), bool(broken.error),
                            "两者必须能被调用方区分开：一个 error 为 None，一个不为 None")

    def test_unresolved_log_path_is_not_reported_as_zero(self):
        """`LOG_FILE` 为空串占位（import 期档名文件读不出来）时，也不许说成"共 0 条"。"""
        self.addCleanup(setattr, log, "LOG_FILE", log.LOG_FILE)
        log.LOG_FILE = ""
        page = log.read_logs(10)
        self.assertFalse(page.readable)
        self.assertIn("档名", page.error)


# ---------------------------------------------------------------- P1-4 数不出来 ≠ 0
class CountFilesTest(IsolatedCase):
    """`_count_files` 的返回形状：`(计数, 原因)`；原因非空时计数是 `None`（P1-4）。"""

    def test_missing_directory_is_a_true_zero(self):
        """目录**真的不存在**（新建档案的第一天）→ 这才是唯一允许回 0 的形态。

        ⚠️ 必须先把目录删掉：`IsolatedCase.setUp` 里的 `store.set_current_profile()`
        会顺手 `ensure_profile()` 把 `resumes/` 建出来 —— 不删的话这条用例名义上在测
        "目录不存在"，实际测的只是"空目录"，`FileNotFoundError` 那一支**一行都没走到**。
        （变异取证：把 `except FileNotFoundError` 整支删掉，改名前这条用例照样绿；
        它挡不住任何东西，等于没测。所以下面那条 `assertFalse(os.path.exists(p))` 是反证。
        `test_unlistable_directory_is_not_zero` 的 mock 也**盖不住**这一支：
        它 mock 掉的是 `os.listdir`，走的是通用 `except`。）
        """
        p = store.p_path("resumes")
        if os.path.isdir(p):
            shutil.rmtree(p)
        self.assertFalse(os.path.exists(p), "反证：目录确实不存在，否则本用例测的还是空目录")
        self.assertEqual(log._count_files(p, (".md", ".txt")), (0, ""))

    def test_counts_only_matching_extensions(self):
        d = store.p_path("resumes")
        os.makedirs(d, exist_ok=True)
        for n in ("a.md", "b.txt", "c.pdf", "d.MD"):
            open(os.path.join(d, n), "w").close()
        self.assertEqual(log._count_files(d, (".md", ".txt")), (3, ""))

    def test_a_regular_file_in_place_of_the_directory_is_not_zero(self):
        """报告里的实测复现：传普通文件也返回 0 —— 那个 0 会变成"用户 0 份简历"。"""
        store.ensure_profile()
        p = store.p_path("resumes")
        if os.path.isdir(p):
            shutil.rmtree(p)
        with open(p, "w", encoding="utf-8") as f:
            f.write("我不是目录")
        value, why = log._count_files(p, (".md", ".txt"))
        self.assertIsNone(value, "存在却不是目录 = 数不出来，不许回 0")
        self.assertIn(p, why)

    def test_unlistable_directory_is_not_zero(self):
        d = store.p_path("resumes")
        os.makedirs(d, exist_ok=True)
        with mock.patch("os.listdir", side_effect=PermissionError(13, "拒绝访问")):
            value, why = log._count_files(d, (".md",))
        self.assertIsNone(value, "存在却列不出来 = 数不出来，不许回 0")
        self.assertTrue(why)


class EnvCountsTest(IsolatedCase):
    """`env.json` 里不许出现一个会被误读成"用户真的 0 份简历"的 0（P1-4）。"""

    def _env(self):
        name = log.build_log_bundle(port=1234)
        with zipfile.ZipFile(os.path.join(os.path.dirname(log.LOG_FILE), name)) as z:
            return json.loads(z.read("env.json").decode("utf-8"))

    def test_healthy_profile_reports_real_numbers_and_no_unreadable_entries(self):
        store.ensure_profile()
        d = store.p_path("resumes")
        os.makedirs(d, exist_ok=True)
        open(os.path.join(d, "简历.md"), "w").close()
        env = self._env()
        self.assertEqual(env["counts"]["resumes"], 1)
        self.assertEqual(env["counts_unreadable"], {})

    def test_unreadable_resumes_directory_leaves_no_zero_behind(self):
        store.ensure_profile()
        p = store.p_path("resumes")
        if os.path.isdir(p):
            shutil.rmtree(p)
        with open(p, "w", encoding="utf-8") as f:
            f.write("我不是目录")
        env = self._env()
        self.assertNotIn("resumes", env["counts"], "数不出来时不许写一个 0 进 counts")
        self.assertIn("resumes", env["counts_unreadable"])
        self.assertTrue(env["counts_unreadable"]["resumes"])

    def test_unreadable_jobs_ledger_leaves_no_zero_behind(self):
        store.ensure_profile()
        with open(store.jobs_path(), "w", encoding="utf-8") as f:
            f.write("{坏掉的台账")
        env = self._env()
        self.assertNotIn("jobs", env["counts"], "台账读不出来时不许写 0 个岗位")
        self.assertIn("jobs", env["counts_unreadable"])


# ---------------------------------------------------------------- P1-5 写失败必须可见
class LogEventWriteFailureTest(IsolatedCase):
    """写失败（含 `json.dumps` 失败、磁盘满、被占用）不许静默（P1-5）。

    选定行为：**失败时返回 `None`**（成功仍返回那条 rec，形状不变）+ 进程内留痕
    （`LOG_WRITE_FAILURES` 计数 + `LAST_LOG_WRITE_FAILURE` 最近一次原因 + stderr 一行），
    但**绝不把异常抛回业务逻辑**。理由：`None` 是"没写进盘"的最直白说法，
    而原来的写法回一条**看起来成功了**的 rec 才是那句假话。
    """

    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, log, "LOG_WRITE_FAILURES", getattr(log, "LOG_WRITE_FAILURES", 0))
        self.addCleanup(setattr, log, "LAST_LOG_WRITE_FAILURE",
                        getattr(log, "LAST_LOG_WRITE_FAILURE", None))
        log.LOG_WRITE_FAILURES = 0
        log.LAST_LOG_WRITE_FAILURE = None

    def _lines(self):
        if not os.path.exists(log.LOG_FILE):
            return []
        with open(log.LOG_FILE, encoding="utf-8") as f:
            return [l for l in f.read().splitlines() if l.strip()]

    def test_unserializable_data_is_visible_to_the_caller(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            back = log.log_event("t.bad", bad=object())
        self.assertIsNone(back, "写失败必须让调用方看得出来：不能回一条'看起来成功'的记录")
        self.assertEqual(self._lines(), [], "反证：这一条确实没写进盘")
        self.assertGreaterEqual(log.LOG_WRITE_FAILURES, 1)
        self.assertEqual(log.LAST_LOG_WRITE_FAILURE.get("event"), "t.bad")
        self.assertTrue(log.LAST_LOG_WRITE_FAILURE.get("error"))
        self.assertIn("t.bad", err.getvalue(), "进程内至少要留一行痕迹")

    def test_disk_write_failure_is_visible_and_does_not_raise(self):
        err = io.StringIO()
        with mock.patch("builtins.open", side_effect=OSError("磁盘满")):
            with contextlib.redirect_stderr(err):
                back = log.log_event("t.diskfull", a=1)      # 不许抛回业务逻辑
        self.assertIsNone(back)
        self.assertGreaterEqual(log.LOG_WRITE_FAILURES, 1)
        self.assertIn("磁盘满", log.LAST_LOG_WRITE_FAILURE.get("error") or "")
        self.assertIn("t.diskfull", err.getvalue())

    def test_a_successful_write_is_not_counted_as_a_failure(self):
        back = log.log_event("t.ok", a=1)
        self.assertIsInstance(back, dict)
        self.assertEqual(back["event"], "t.ok")
        self.assertEqual(log.LOG_WRITE_FAILURES, 0, "没失败就不许记失败，否则计数本身是新的假话")
        self.assertIsNone(log.LAST_LOG_WRITE_FAILURE)

    def test_failure_stats_are_reported_in_env_json(self):
        with contextlib.redirect_stderr(io.StringIO()):
            log.log_event("t.bad", bad=object())
        name = log.build_log_bundle()
        with zipfile.ZipFile(os.path.join(os.path.dirname(log.LOG_FILE), name)) as z:
            env = json.loads(z.read("env.json").decode("utf-8"))
        self.assertGreaterEqual(env["log_write_failures"]["count"], 1)
        self.assertTrue(env["log_write_failures"]["last_error"])

    def test_empty_log_path_is_quietly_skipped_but_still_counted(self):
        """`LOG_FILE` 是空串占位（import 期档名文件读不出来）时写日志的行为。

        这里把 P1-5 与 P1-6 的接缝钉死：`_initial_log_file()` 的说明写着"空路径下它安静地不写"，
        而"安静"**不等于**"成功"——
          - 不许抛（那会把 `import log` 之后的所有调用打成 traceback，见上一条用例的说明）；
          - 也不许回一条"看起来成功了"的 rec（P1-5 那句假话），要回 `None`；
          - 更要记进 `LOG_WRITE_FAILURES`：**这段时间的日志确实一条都没落盘**，
            反馈包里必须看得见，否则 AI 排错时会把"缺日志"读成"用户没操作过"。
        """
        self.addCleanup(setattr, log, "LOG_FILE", log.LOG_FILE)
        log.LOG_FILE = ""
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            back = log.log_event("t.nopath", a=1)        # 不许抛回业务逻辑
        self.assertIsNone(back, "没写进盘就必须回 None，不能回一条'看起来成功'的记录")
        self.assertGreaterEqual(log.LOG_WRITE_FAILURES, 1, "空路径下没写盘也必须留痕")
        self.assertEqual(log.LAST_LOG_WRITE_FAILURE.get("event"), "t.nopath")
        self.assertIn("t.nopath", err.getvalue())


# ---------------------------------------------------------------- 批 6.5 日志轮转
class LogRotationTest(IsolatedCase):
    """按**大小**轮转，且**一条记录都不许丢**（批 6.5）。

    选定契约（先读了 `read_logs` 的既有调用方再定，见 `log.read_logs` / `log.archive_paths`
    的 docstring）：
      - 写新记录**之前**，当前文件若已达 `log.LOG_ROTATE_BYTES`（生产 1 MiB），
        就把当前文件**改名**成 `events.jsonl.<YYYYmmdd_HHMMSS_微秒>`（**改名，不是截断**）；
      - `read_logs` 读的是**所有段**（存档按时间升序 + 当前文件），所以界面上
        「最近 N 条」「共 N 条」跨轮转仍是一条不少 —— 这正是"轮转不许丢证据"的落点；
      - 存档**不删**（阈值只决定分几段，不决定留多久）；
      - 轮转失败**不许吃掉这条记录**：记录照旧写进当前文件，只在 stderr 留一行痕迹。

    阈值在用例里被压到 300 字节（生产 1 MiB）：不压小就不可能用单元测试触发轮转。
    """

    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, log, "LOG_ROTATE_BYTES", log.LOG_ROTATE_BYTES)
        log.LOG_ROTATE_BYTES = 300          # 一条记录 ~150 字节 ⇒ 两三条就轮转一次

    def _archives(self):
        """磁盘上的存档名（只认 `<日志文件名>.<时间戳>` 这个形状，老的在前）。"""
        d = os.path.dirname(log.LOG_FILE)
        base = os.path.basename(log.LOG_FILE)
        if not os.path.isdir(d):
            return []
        return sorted(n for n in os.listdir(d)
                      if n.startswith(base + ".")
                      and re.match(r"^\d{8}_\d{6}_\d{6}$", n[len(base) + 1:]))

    def test_rotation_creates_an_archive_and_loses_no_record(self):
        """主判据：超过阈值 ⇒ 出现存档 + 最近的记录都在 + 老记录也读得到。

        改前红在这两处：`assertTrue(self._archives(), ...)`（压根没有轮转），
        以及 `read_logs` 只读当前文件（轮转后就读不到旧段）。
        """
        for i in range(12):
            log.log_event("e%d" % i)
        self.assertTrue(self._archives(), "当前文件超过阈值后必须出现存档")
        self.assertEqual([r["event"] for r in log.read_logs(5).records],
                         ["e7", "e8", "e9", "e10", "e11"], "轮转后最近 5 条必须一条不少")
        page = log.read_logs(10 ** 9)
        self.assertEqual([r["event"] for r in page.records],
                         ["e%d" % i for i in range(12)], "轮转不许丢记录（存档里的老记录也要读得到）")
        self.assertEqual(page.total, 12)

    def test_no_archive_below_the_threshold(self):
        """对照组：没到阈值 ⇒ **不轮转**、不产生存档、当前文件就是全部。"""
        log.LOG_ROTATE_BYTES = 10 ** 9
        for i in range(5):
            log.log_event("e%d" % i)
        self.assertEqual(self._archives(), [], "没到阈值不许轮转、不许产生存档")
        page = log.read_logs(10 ** 9)
        self.assertEqual([r["event"] for r in page.records], ["e%d" % i for i in range(5)])
        self.assertEqual(page.total, 5)

    def test_every_archive_is_readable_and_names_sort_chronologically(self):
        """多段：存档名字必须**升序 = 时间升序**（`read_logs` 就靠这个拼回全量顺序）。"""
        for i in range(20):
            log.log_event("e%d" % i)
        archives = self._archives()
        self.assertGreaterEqual(len(archives), 2, "20 条记录（阈值 300 字节）至少该轮转两次")
        on_disk = []
        for name in archives + [os.path.basename(log.LOG_FILE)]:
            with open(os.path.join(os.path.dirname(log.LOG_FILE), name), encoding="utf-8") as f:
                on_disk += [json.loads(l)["event"] for l in f if l.strip()]
        self.assertEqual(on_disk, ["e%d" % i for i in range(20)],
                         "存档（升序）+ 当前文件拼起来必须正好是写入顺序、一条不多一条不少")

    def test_rotation_failure_still_keeps_the_record(self):
        """轮转失败（改名被占用等）**不许**把这条记录一起弄丢 —— 那才是真丢证据。"""
        log.LOG_ROTATE_BYTES = 1            # 让每次写都尝试轮转
        log.log_event("e0")
        log.log_event("e1")
        archives_before = self._archives()
        err = io.StringIO()
        with mock.patch("os.replace", side_effect=OSError(17, "另一个进程占着")):
            with contextlib.redirect_stderr(err):
                back = log.log_event("e2")
        self.assertIsNotNone(back, "轮转失败也必须把这条记录写进盘")
        self.assertIn("轮转", err.getvalue(), "轮转失败必须在 stderr 留一行痕迹")
        self.assertEqual(self._archives(), archives_before, "轮转没成功就不该凭空多出存档")
        self.assertEqual([r["event"] for r in log.read_logs(10 ** 9).records], ["e0", "e1", "e2"])

    def test_user_copied_backup_file_is_not_treated_as_a_segment(self):
        """只认 `<日志名>.<时间戳>`：用户手工拷的 `.bak-*` 不许被当成日志段读进来。"""
        for i in range(6):
            log.log_event("e%d" % i)
        decoy = log.LOG_FILE + ".bak-手动拷的"
        with open(decoy, "w", encoding="utf-8") as f:
            f.write(json.dumps({"event": "手工拷的文件，不是日志段"}) + "\n")
        self.assertNotIn(decoy, log.archive_paths())
        self.assertEqual(log.read_logs(10 ** 9).total, 6, "手工拷的 .bak 不许被当成日志段")
        self.assertEqual(self._archives(), [os.path.basename(p) for p in log.archive_paths()],
                         "archive_paths() 与磁盘上的存档名必须一致（老的在前）")

    def test_feedback_bundle_carries_the_archives_too(self):
        """反馈包不许因为轮转变空：刚轮转过时当前文件可能只剩一两条，存档必须一起进包。"""
        for i in range(12):
            log.log_event("e%d" % i)
        archives = self._archives()
        self.assertTrue(archives)
        name = log.build_log_bundle(port=1234)
        with zipfile.ZipFile(os.path.join(os.path.dirname(log.LOG_FILE), name)) as z:
            names = z.namelist()
        for a in archives:
            self.assertIn(a, names, "存档必须一起进反馈包")


if __name__ == "__main__":
    unittest.main()
