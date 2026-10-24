# -*- coding: utf-8 -*-
import ast
import glob
import importlib
import io
import json
import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

import jddelete
import kb
import store

from tests.base import IsolatedCase


class StorePathTest(IsolatedCase):
    def test_profile_dir_uses_key(self):
        d = store.profile_dir("alice")
        self.assertTrue(d.endswith(os.path.join("profiles", "alice")))

    def test_default_profile_when_key_empty(self):
        d = store.profile_dir("")
        self.assertTrue(d.endswith(os.path.join("profiles", "default")))

    def test_ensure_profile_creates_subdirs(self):
        store.ensure_profile("bob")
        for sub in ("resumes", "reports", "jd", "logs"):
            self.assertTrue(os.path.isdir(os.path.join(store.profile_dir("bob"), sub)), sub)

    def test_p_path_resolves_under_profile(self):
        p = store.p_path("jobs.json", key="carol")
        self.assertTrue(p.endswith(os.path.join("profiles", "carol", "jobs.json")))

    def test_a_misspelled_key_kwarg_blows_up_instead_of_hitting_the_default_profile(self):
        """拼错的 kwarg 必须**当场报错**，而不是被静默忽略、读写到 default profile。

        原先签名是 `p_path(*parts, **kw)` + `kw.get("key")`：`kye="dave"` 会被吞掉，
        于是"给 dave 写"变成"给 default 写"（P5-7 那条【实跑】）。
        """
        store.set_current_profile("dave")
        with self.assertRaises(TypeError):
            store.p_path("jobs.json", kye="dave")
        # 反向对照：正确写法照旧解析到指定 profile
        self.assertTrue(store.p_path("jobs.json", key="dave").endswith(
            os.path.join("profiles", "dave", "jobs.json")))

    def test_set_and_get_current_profile(self):
        store.set_current_profile("dave")
        self.assertEqual(store.current_profile(), "dave")

    def test_json_roundtrip(self):
        p = os.path.join(self.tmp, "x.json")
        store.write_json(p, {"a": 1, "中文": "值"})
        self.assertEqual(store.read_json(p, {}), {"a": 1, "中文": "值"})

    def test_read_json_returns_default_on_missing(self):
        self.assertEqual(store.read_json(os.path.join(self.tmp, "nope.json"), {"d": 1}), {"d": 1})

    def test_p_path_follows_current_profile(self):
        store.set_current_profile("erin")
        self.assertTrue(store.p_path("jobs.json").endswith(
            os.path.join("profiles", "erin", "jobs.json")))

    def test_safe_name_blocks_traversal(self):
        self.assertEqual(store.safe_name("../../etc/passwd"), "passwd")
        self.assertEqual(store.safe_name(".."), "")
        self.assertEqual(store.safe_name("."), "")
        self.assertEqual(store.safe_name(""), "")


class ReportStateTest(IsolatedCase):
    def test_roundtrip(self):
        self.assertTrue(store.save_report_state("20261008_岗A_80分", {"gate:0": True, "cl:2": False}))
        d = store.load_report_state("20261008_岗A_80分")
        self.assertEqual(d["report_id"], "20261008_岗A_80分")
        self.assertEqual(d["checks"], {"gate:0": True, "cl:2": False})

    def test_missing_file_returns_empty(self):
        self.assertEqual(store.load_report_state("没见过").get("checks"), {})

    def test_path_traversal_is_neutralised(self):
        for bad in ("", "..", ".", "../../etc/passwd", "a/../.."):
            path = store.report_state_path(bad)
            if path:
                self.assertTrue(path.startswith(store.report_state_dir()),
                                "越级路径逃出了 report_state 目录：%r -> %r" % (bad, path))
        self.assertEqual(store.report_state_path(""), "")
        self.assertEqual(store.report_state_path(".."), "")
        self.assertFalse(store.save_report_state("..", {"a": True}))

    def test_corrupt_file_is_quarantined_not_overwritten(self):
        store.ensure_profile()
        rid = "20261008_坏报告"
        path = store.report_state_path(rid)
        store.write_text(path, "{ 用户手改坏的 json")
        d = store.load_report_state(rid)                      # 读到坏文件 → 空状态
        self.assertEqual(d["checks"], {})
        self.assertTrue(store.save_report_state(rid, {"gate:0": True}))
        backups = glob.glob(path + ".corrupt-*")
        self.assertEqual(len(backups), 1)                     # 旧文件被保住
        self.assertEqual(store.read_text(backups[0]), "{ 用户手改坏的 json")
        self.assertEqual(store.load_report_state(rid)["checks"], {"gate:0": True})

    def test_checks_are_coerced_to_bool_and_junk_keys_dropped(self):
        self.assertTrue(store.save_report_state("r1", {"a": 1, "b": 0, "": True}))
        self.assertEqual(store.load_report_state("r1")["checks"], {"a": True, "b": False})


class InterviewStoreTest(IsolatedCase):
    def test_path_traversal_is_neutralised(self):
        self.assertEqual(store.interview_dir(""), "")
        self.assertEqual(store.interview_dir(".."), "")
        p = store.interview_dir("../../etc")
        if p:
            self.assertTrue(p.startswith(store.interviews_dir()))

    def test_save_stream_writes_and_returns_size(self):
        import io
        n = store.save_stream("iv1", "audio.wav", io.BytesIO(b"x" * 5000), max_bytes=10 * 1024)
        self.assertEqual(n, 5000)
        self.assertEqual(os.path.getsize(store.interview_file("iv1", "audio.wav")), 5000)

    def test_save_stream_rejects_oversize_and_cleans_partial_file(self):
        import io
        with self.assertRaises(ValueError):
            store.save_stream("iv1", "audio.wav", io.BytesIO(b"x" * 5000), max_bytes=1000)
        self.assertFalse(os.path.exists(store.interview_file("iv1", "audio.wav")))

    def test_read_write_json_roundtrip_in_interview_dir(self):
        self.assertTrue(store.save_interview_json("iv1", "status.json", {"state": "queued"}))
        self.assertEqual(store.load_interview_json("iv1", "status.json")["state"], "queued")

    def test_missing_interview_json_returns_default(self):
        self.assertEqual(store.load_interview_json("nope", "status.json", {"state": "?"}),
                         {"state": "?"})


class ReadKnowledgeStrictTest(IsolatedCase):
    """`read_knowledge_strict()`：**区分"读不出来"与"没有"**。

    这条区分是阶段 D 两条数据丢失路径的根因（把"读不出来"当成"空"，然后写回去），
    所以它只准有一份实现。
    """

    def test_a_missing_file_is_empty_with_no_reason(self):
        data, err = store.read_knowledge_strict()
        self.assertEqual(data, {"nodes": []})
        self.assertEqual(err, "", "文件不存在不是错误")

    def test_a_valid_file_returns_its_nodes(self):
        store.save_knowledge({"nodes": [{"id": "a", "term": "A"}]})
        data, err = store.read_knowledge_strict()
        self.assertEqual([n["id"] for n in data["nodes"]], ["a"])
        self.assertEqual(err, "")

    def test_a_corrupt_file_reports_a_reason_instead_of_a_silent_empty(self):
        with open(store.knowledge_path(), "wb") as f:
            f.write(b'{"nodes": [ <<<CORRUPT>>>')
        data, err = store.read_knowledge_strict()
        self.assertEqual(data, {"nodes": []})
        self.assertTrue(err, "坏文件必须给出原因，绝不能静默当成空库")
        self.assertIn("knowledge.json", err)

    def test_a_wrong_shape_reports_a_reason(self):
        store.write_text(store.knowledge_path(), '{"nodes": "not-a-list"}')
        data, err = store.read_knowledge_strict()
        self.assertEqual(data, {"nodes": []})
        self.assertTrue(err, "结构不对也算读不出来")

    def test_the_reason_names_the_file_that_was_actually_read(self):
        """调用方传别的 path 时，原因里必须出现**那个**文件名，不能写死 knowledge.json。

        文案原先写死 `knowledge.json`：读 `别处的库.json` 失败时，用户看到的却是
        "知识库 `knowledge.json` 读不出来"，照着这句话去修就修错了文件。
        结构不对那一句同理（同一个写死的地方）。
        """
        other = os.path.join(self.tmp, "别处的库.json")
        with open(other, "wb") as f:
            f.write(b'{"nodes": [ <<<CORRUPT>>>')
        _, err = store.read_knowledge_strict(other)
        self.assertIn("别处的库.json", err)
        self.assertNotIn("knowledge.json", err)
        store.write_text(other, '{"nodes": "not-a-list"}')
        _, err2 = store.read_knowledge_strict(other)
        self.assertIn("别处的库.json", err2)
        self.assertNotIn("knowledge.json", err2)

    def test_it_does_not_write_anything(self):
        bad = b'{"nodes": [ <<<CORRUPT>>>'
        with open(store.knowledge_path(), "wb") as f:
            f.write(bad)
        # 用 knowledge_path() 反推目录，而不是 profile_dir()：后者是"无参回落硬编码 default"，
        # 知识库真正落在 current_profile() 下，两者当前恰好相同纯属碰巧。
        folder = os.path.dirname(store.knowledge_path())
        before = sorted(os.listdir(folder))
        store.read_knowledge_strict()
        with open(store.knowledge_path(), "rb") as f:
            self.assertEqual(f.read(), bad, "只读：坏文件原样留原路径")
        self.assertEqual(sorted(os.listdir(folder)), before,
                         "只读：不许多写/改名任何文件（如隔离副本）")


class KnowledgeSafeToWriteTest(IsolatedCase):
    """`(可写, 原因)` 的各种情形 —— 「读不出来 ≠ 空」这条规则**唯一**的写盘判据。

    P0-6 之后判据是「**不存在 或 真的是 0 字节**才算没东西可丢」：纯空白文件（有字节、
    却解析不出）从 safe 翻成了 not safe（见下面那条用例的 docstring）。
    """

    def _write(self, blob):
        with open(store.knowledge_path(), "wb") as f:
            f.write(blob)

    def test_a_missing_file_is_safe(self):
        self.assertEqual(store.knowledge_safe_to_write(), (True, ""))

    def test_a_valid_file_is_safe(self):
        store.save_knowledge({"nodes": [{"id": "a", "term": "A"}]})
        self.assertEqual(store.knowledge_safe_to_write(), (True, ""))

    def test_a_zero_byte_file_is_still_safe(self):
        """0 字节 = 合法的"第一次"：没有可丢的内容，别为它造垃圾备份。"""
        self._write(b"")
        self.assertEqual(store.knowledge_safe_to_write(), (True, ""))

    def test_a_whitespace_only_file_is_no_longer_safe(self):
        """**这两条断言是从 safe 翻成 not safe 的（体检报告 P0-6），翻是对的。**

        老判据借的是 `content_bytes_present()`（"`strip()` 之后还有东西吗"），于是
        `b"\\r\\n   \\r\\n"` 被判成"没有可丢的内容" ⇒ 可写 ⇒ 被内存里的 `{"nodes": []}` 顶掉。
        字节语义上它确实没有可见内容，但它**不是 0 字节**：可能是用户手写坏的、别的工具写坏的、
        或将来还能救回来的东西。写盘判据因此收紧成"**不存在 或 真的是 0 字节**"才算没东西可丢。

        （`content_bytes_present()` 本体的语义**没有变**、钉它的测试也没动 —— 它回答的是
        "有没有可见内容"，那对 `_quarantine_report_state` 那种"几个勾不值得挡住写入"的场景
        仍然是对的。两个判据分工不同，见 `FileHasBytesToLoseTest`。）
        """
        for blob in (b"   \n  ", b"\r\n", b"\r\n   \r\n"):
            with self.subTest(blob=blob):
                self._write(blob)
                ok, err = store.knowledge_safe_to_write()
                self.assertFalse(ok, "有字节却解析不出（含纯空白）→ 绝不可写盘：%r" % (blob,))
                self.assertIn("knowledge.json", err)

    def test_a_truncated_json_file_is_not_safe(self):
        self._write(b'{"nodes": [ <<<CORRUPT>>>')
        ok, err = store.knowledge_safe_to_write()
        self.assertFalse(ok, "有内容却读不出来 → 绝不能写盘")
        self.assertIn("knowledge.json", err)

    def test_a_non_utf8_file_is_not_safe(self):
        """**这条是本任务的全部理由**：GBK 写坏的库里有用户的整库，绝不能被空库顶掉。

        老判据在这里会误判成"空文件"（解码失败被兜成空串）→ 不备份、直接写盘 → 数据丢。
        """
        self._write(json.dumps({"nodes": [{"id": "语音识别", "term": "语音识别"}]},
                               ensure_ascii=False).encode("gbk"))
        ok, err = store.knowledge_safe_to_write()
        self.assertFalse(ok, "非 UTF-8 文件有字节 → 不可写盘")
        self.assertIn("读不出来", err)

    def test_a_wrong_shape_file_is_not_safe(self):
        self._write(b'{"nodes": "not-a-list"}')
        self.assertFalse(store.knowledge_safe_to_write()[0])

    def test_it_does_not_write_anything(self):
        bad = b'{"nodes": [ <<<CORRUPT>>>'
        self._write(bad)
        folder = os.path.dirname(store.knowledge_path())
        before = sorted(os.listdir(folder))
        store.knowledge_safe_to_write()
        with open(store.knowledge_path(), "rb") as f:
            self.assertEqual(f.read(), bad, "只读：坏文件原样留原路径")
        self.assertEqual(sorted(os.listdir(folder)), before, "只读：不许多写/改名任何文件")


class SingleImplementationGuardTest(IsolatedCase):
    """「这条规则只准有一份实现」的守卫：别人把它内联回去，这些测试就会红。"""

    def test_jddelete_follows_the_shared_implementation(self):
        real, calls = store.read_knowledge_strict, []

        def spy(path=None):
            calls.append(path)
            return {"nodes": []}, "哨兵：故意报一个读不出来的原因"

        store.read_knowledge_strict = spy
        try:
            self.assertEqual(jddelete._knowledge_or_error(),
                             ({"nodes": []}, "哨兵：故意报一个读不出来的原因"))
            self.assertTrue(calls, "jddelete 没有走共享实现（有人把逻辑内联回去了）")
        finally:
            store.read_knowledge_strict = real

    def test_kb_quarantine_follows_the_shared_implementation(self):
        real, seen = store.knowledge_safe_to_write, []

        def spy(path=None):
            seen.append(1)
            return True, ""

        store.knowledge_safe_to_write = spy
        try:
            self.assertTrue(kb._quarantine_corrupt_kb())
            self.assertEqual(len(seen), 1, "kb 没有走共享判据（有人把它内联回去了）")
        finally:
            store.knowledge_safe_to_write = real

    def test_report_state_quarantine_follows_the_shared_primitive(self):
        real, seen = store.content_bytes_present, []

        def spy(path):
            seen.append(path)
            return False

        store.content_bytes_present = spy
        try:
            store._quarantine_report_state(os.path.join(store.profile_dir(), "随便.json"))
            self.assertTrue(seen, "勾选状态的隔离判据没有走共享原语（有人把它内联回去了）")
        finally:
            store.content_bytes_present = real


class ContentBytesPresentTest(IsolatedCase):
    """`content_bytes_present()` —— "看原始字节"这条易错规则**唯一**的实现。

    它单独被测的理由：现在有两个调用方（知识库、简历版本库），
    谁把它改回"看解码后的字符串"，两个地方就会一起坏。
    """

    def _write(self, blob, name="probe.json"):
        path = os.path.join(store.profile_dir(), name)
        with open(path, "wb") as f:
            f.write(blob)
        return path

    def test_a_missing_file_has_nothing_to_lose(self):
        self.assertFalse(store.content_bytes_present(
            os.path.join(store.profile_dir(), "没有这个文件")))

    def test_empty_and_whitespace_only_files_have_nothing_to_lose(self):
        for blob in (b"", b"   \n  ", b"\r\n"):
            with self.subTest(blob=blob):
                self.assertFalse(store.content_bytes_present(self._write(blob)))

    def test_a_non_utf8_file_has_bytes_to_lose(self):
        """**本函数的全部理由**：GBK 文件解码会失败并被兜成空串，但它的**字节**是可丢的。"""
        self.assertTrue(store.content_bytes_present(self._write("中文".encode("gbk"))))

    def test_a_valid_file_has_bytes_to_lose(self):
        self.assertTrue(store.content_bytes_present(self._write(b'{"a": 1}')))


class ReportStateNonUtf8Test(IsolatedCase):
    """GBK 写坏的勾选状态文件：**必须先改名备份**，绝不能被空状态顶掉。

    老判据 `read_text(path, "").strip()` 在 GBK 上会兜成空串 → 误判"没什么可丢" →
    不备份、直接覆盖 → 用户在该报告里勾的"已掌握"**永久丢失、盘上无任何副本**（实测）。
    """

    def test_a_non_utf8_state_file_is_quarantined_before_overwriting(self):
        rid = "20261008_岗A_80分"
        path = store.report_state_path(rid)
        raw = json.dumps({"version": 1, "report_id": rid,
                          "checks": {"know:语音识别": True, "know:词错率（WER）": True}},
                         ensure_ascii=False).encode("gbk")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(raw)

        store.save_report_state(rid, {"know:新概念": True})

        folder = os.path.dirname(path)
        backups = [f for f in os.listdir(folder) if ".corrupt-" in f]
        self.assertEqual(len(backups), 1, "读不出来的状态文件必须先被改名备份")
        with open(os.path.join(folder, backups[0]), "rb") as f:
            self.assertEqual(f.read(), raw, "备份必须逐字节等于原文（含中文勾选）")
        # 本次写入照常成功（状态文件只值几个勾，不值得为它挡掉用户刚做的勾选）
        self.assertTrue(store.load_report_state(rid)["checks"].get("know:新概念"))

    def test_an_empty_state_file_is_not_quarantined(self):
        """0 字节不是损坏：正常写入，别造垃圾备份。"""
        rid = "空状态"
        path = store.report_state_path(rid)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(b"")
        store.save_report_state(rid, {"a": True})
        self.assertEqual([f for f in os.listdir(os.path.dirname(path))
                          if ".corrupt-" in f], [])
        self.assertTrue(store.load_report_state(rid)["checks"].get("a"))


class WriteBytesParentDirsTest(IsolatedCase):
    """`write_bytes()` 也要建父目录（与 `write_text` 一致）—— 这条断言就是它**唯一**的钉子。

    （**更正**：早先这里写着"独立复查的变异 M-D 把 `os.makedirs` 那行删掉、**全量 900 条仍全绿**
    —— 也就是说没有任何测试钉住它"。那句话**不可复现**，已撤回：本机实测删掉那行会
    **16 FAIL / 34 ERROR**，钉住它的测试多的是。留这条注是为了不再重复一句没验证过的立论 ——
    下面这条断言是**直接钉子**，不需要拿"全量仍全绿"当理由。）

    为什么非钉不可：备份/回滚路径全靠它 —— 目录不在时 `open(path, "wb")` 直接
    `FileNotFoundError`，回滚就会在"救数据"的路上再炸一次（那时连原像都写不下去）。
    """

    def test_write_bytes_creates_missing_parent_directories(self):
        path = os.path.join(store.profile_dir(), "新建的一层", "再一层", "x.bin")
        store.write_bytes(path, b"\x00\x01abc")
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"\x00\x01abc")


class ContentBytesPresentUnreadableTest(IsolatedCase):
    """**存在但打不开**（权限/被锁）→ 必须当成"有内容"（fail-safe），不是"没有"。"""

    def test_an_unreadable_file_counts_as_having_content(self):
        path = os.path.join(store.profile_dir(), "locked.json")
        with open(path, "wb") as f:
            f.write(b'{"a": 1}')

        real_open = open

        def fake_open(p, *a, **kw):
            if os.path.abspath(str(p)) == os.path.abspath(path) and (not a or a[0] == "rb"):
                raise PermissionError(13, "拒绝访问")
            return real_open(p, *a, **kw)

        with mock.patch("builtins.open", fake_open):
            self.assertTrue(store.content_bytes_present(path),
                            "读不出来不等于没有内容 —— 不许把'不知道'当成'安全'")


class FileHasBytesToLoseTest(IsolatedCase):
    """`file_has_bytes_to_lose()` —— P0-6 收紧之后的**写盘**判据。

    它和 `content_bytes_present()` 是**两个分工不同的判据**，混用就是丢数据那条路：
      - `content_bytes_present()`：有没有**可见内容**（`strip()` 之后还有东西吗）；
      - `file_has_bytes_to_lose()`：盘上有没有**一个字节**（本函数）。
    写盘判据只能用后者：一个只有 `b"\\r\\n   \\r\\n"` 的文件不是 0 字节。
    """

    def _write(self, blob, name="probe.json"):
        path = os.path.join(store.profile_dir(), name)
        with open(path, "wb") as f:
            f.write(blob)
        return path

    def test_a_missing_file_has_nothing_to_lose(self):
        self.assertFalse(store.file_has_bytes_to_lose(
            os.path.join(store.profile_dir(), "没有这个文件")))

    def test_a_zero_byte_file_has_nothing_to_lose(self):
        self.assertFalse(store.file_has_bytes_to_lose(self._write(b"")))

    def test_a_whitespace_only_file_still_has_bytes_to_lose(self):
        """**这条就是 P0-6 的全部理由**：纯空白不是 0 字节 → 写盘判据必须拦住。"""
        for blob in (b"   \n  ", b"\r\n", b"\r\n   \r\n"):
            with self.subTest(blob=blob):
                self.assertTrue(store.file_has_bytes_to_lose(self._write(blob)))

    def test_a_non_utf8_file_has_bytes_to_lose(self):
        self.assertTrue(store.file_has_bytes_to_lose(self._write("中文".encode("gbk"))))


class JobsSafeToWriteTest(IsolatedCase):
    """`jobs_safe_to_write()` —— 台账的写盘判据（体检报告 P0-2）。

    为什么必须有它：`load_jobs()` 在**任何**读/解析失败时都返回 `[]`，而 `upsert_job` 是
    分析记录写盘的**唯一真出口** —— 于是"一次读失败"就等于"整份台账被那一条新记录顶掉"，
    旧记录一条不剩、盘上也没有副本。判据必须问**严格读**，不能看 `load_jobs()` 的返回值
    （那正是把"读不出来"和"空台账"混成同一个信号的根源）。
    """

    def _write(self, blob):
        path = store.jobs_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(blob)
        return path

    def test_a_missing_ledger_is_safe(self):
        self.assertEqual(store.jobs_safe_to_write(), (True, ""))

    def test_a_valid_ledger_is_safe(self):
        store.save_jobs([{"id": "a", "score": 80}])
        self.assertEqual(store.jobs_safe_to_write(), (True, ""))

    def test_a_zero_byte_ledger_is_safe(self):
        """0 字节 = 合法的"第一次"，没有可丢的内容。"""
        self._write(b"")
        self.assertEqual(store.jobs_safe_to_write(), (True, ""))

    def test_a_non_utf8_ledger_is_not_safe(self):
        self._write(json.dumps([{"id": "语音标注", "score": 80}], ensure_ascii=False).encode("gbk"))
        ok, err = store.jobs_safe_to_write()
        self.assertFalse(ok, "有字节却读不出来 → 绝不能写盘")
        self.assertIn("jobs.json", err)

    def test_a_truncated_ledger_is_not_safe(self):
        self._write(b'[{"id": "a"}, <<<CORRUPT>>>')
        self.assertFalse(store.jobs_safe_to_write()[0])

    def test_a_wrong_shape_ledger_is_not_safe(self):
        self._write(b'{"id": "a"}')          # 合法 JSON，但不是列表 → 台账结构不对
        self.assertFalse(store.jobs_safe_to_write()[0])

    def test_a_whitespace_only_ledger_is_not_safe(self):
        self._write(b"\r\n   \r\n")
        self.assertFalse(store.jobs_safe_to_write()[0])

    def test_it_does_not_write_anything(self):
        bad = b'[{"id": "a"}, <<<CORRUPT>>>'
        path = self._write(bad)
        folder = os.path.dirname(path)
        before = sorted(os.listdir(folder))
        store.jobs_safe_to_write()
        with open(path, "rb") as f:
            self.assertEqual(f.read(), bad, "只读：坏台账原样留原路径")
        self.assertEqual(sorted(os.listdir(folder)), before, "只读：不许多写/改名任何文件")


class UpsertJobRefusesUnreadableLedgerTest(IsolatedCase):
    """P0-2 的核心断言：**读不出来的台账，一个字节都不许动**。

    老行为：`upsert_job` → `load_jobs()` 兜成 `[]` → `save_jobs([那条新记录])` ——
    整份台账被 1 条记录覆盖，旧记录在盘上再无痕迹。
    """

    def _gbk_ledger(self):
        path = store.jobs_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        raw = json.dumps(
            [{"id": "20261008_语音文本评测_72分", "score": 72.0, "resume": "示例姓名-母版"},
             {"id": "20261003_语音文本评测_75分", "score": 75.0, "resume": "示例姓名-母版"}],
            ensure_ascii=False).encode("gbk")
        with open(path, "wb") as f:
            f.write(raw)
        return path, raw

    def test_a_gbk_ledger_refuses_the_write_and_keeps_every_byte(self):
        path, raw = self._gbk_ledger()
        folder = os.path.dirname(path)
        before = sorted(os.listdir(folder))
        with self.assertRaises(RuntimeError) as cm:
            store.upsert_job({"id": "刚跑完的分析", "score": 88.0})
        self.assertIn("jobs.json", str(cm.exception))
        with open(path, "rb") as f:
            self.assertEqual(f.read(), raw, "拒绝写盘时台账必须逐字节未变")
        self.assertEqual(len(json.loads(raw.decode("gbk"))), 2, "夹具里真有 2 条记录，别做成空台账假绿")
        with open(path, "rb") as f:
            self.assertEqual(len(json.loads(f.read().decode("gbk"))), 2,
                             "不许被那一条新记录顶掉（「没有多出半条」）")
        self.assertEqual(sorted(os.listdir(folder)), before,
                         "守卫先于留档：连 .bak- 都不该多出来")

    def test_save_jobs_also_refuses(self):
        """`save_jobs` 是四个阵营共用的出口（app / jddelete / jdid / jobdup），必须自己拦。"""
        path, raw = self._gbk_ledger()
        with self.assertRaises(RuntimeError):
            store.save_jobs([{"id": "整个台账被顶掉"}])
        with open(path, "rb") as f:
            self.assertEqual(f.read(), raw)

    def test_a_missing_ledger_is_created(self):
        self.assertIsNone(store.find_job("新分析"))
        store.upsert_job({"id": "新分析", "score": 88.0})
        self.assertEqual([j["id"] for j in store.load_jobs()], ["新分析"])

    def test_a_zero_byte_ledger_is_created(self):
        path = store.jobs_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(b"")
        store.upsert_job({"id": "新分析", "score": 88.0})
        self.assertEqual([j["id"] for j in store.load_jobs()], ["新分析"])
        self.assertEqual(glob.glob(path + ".bak-*"), [], "0 字节没有可留的旧内容")


class SaveJobsBackupTest(IsolatedCase):
    """覆盖前留档（P0-2 要求 3）：`.bak-<时间戳>`，内容 = **覆盖前那一份**（逐字节）。"""

    def test_overwriting_a_ledger_with_content_keeps_a_timestamped_backup(self):
        store.save_jobs([{"id": "旧记录", "score": 60.0}])
        old = store.read_bytes(store.jobs_path())
        store.save_jobs([{"id": "新记录", "score": 90.0}])

        backups = glob.glob(store.jobs_path() + ".bak-*")
        self.assertEqual(len(backups), 1, "覆盖有内容的台账必须留下一份 .bak-<时间戳>")
        self.assertEqual(store.read_bytes(backups[0]), old, "备份必须逐字节等于覆盖前那一份")
        self.assertEqual([j["id"] for j in store.load_jobs()], ["新记录"])

    def test_the_first_write_leaves_no_backup(self):
        self.assertEqual(glob.glob(store.jobs_path() + ".bak-*"), [])
        store.save_jobs([{"id": "第一条"}])
        self.assertEqual(glob.glob(store.jobs_path() + ".bak-*"), [],
                         "第一次写没有旧内容可留，别造垃圾备份")


class ConfigSafeToWriteTest(IsolatedCase):
    """`config_safe_to_write()` —— 体检报告 P0-4。

    `load_config()` 读不出来时回的是 `DEFAULT_CONFIG`（`api_key=''`、`search_providers=[]`），
    而那**看起来就是"没配过"的样子**：用户点一次"保存设置"就把真的 API Key 与搜索源
    永久抹掉了，盘上没有任何副本。
    """

    def _write(self, blob):
        path = store.config_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(blob)
        return path

    def test_a_missing_config_is_safe(self):
        self.assertEqual(store.config_safe_to_write(), (True, ""))

    def test_a_valid_config_is_safe(self):
        store.save_config({"text_model": {"api_key": "sk-x"}})
        self.assertEqual(store.config_safe_to_write(), (True, ""))

    def test_a_zero_byte_config_is_safe(self):
        self._write(b"")
        self.assertEqual(store.config_safe_to_write(), (True, ""))

    def test_a_non_utf8_config_is_not_safe(self):
        self._write(json.dumps({"text_model": {"api_key": "sk-真钥匙"}},
                               ensure_ascii=False).encode("gbk"))
        ok, err = store.config_safe_to_write()
        self.assertFalse(ok, "有字节却读不出来 → 绝不能写盘")
        self.assertIn("config.json", err)

    def test_a_truncated_config_is_not_safe(self):
        self._write(b'{"text_model": { <<<CORRUPT>>>')
        self.assertFalse(store.config_safe_to_write()[0])

    def test_a_wrong_shape_config_is_not_safe(self):
        self._write('["不是对象"]'.encode("utf-8"))
        self.assertFalse(store.config_safe_to_write()[0])

    def test_a_whitespace_only_config_is_not_safe(self):
        self._write(b"\r\n   \r\n")
        self.assertFalse(store.config_safe_to_write()[0])


class SaveConfigRefusesUnreadableConfigTest(IsolatedCase):
    """P0-4 的核心断言：GBK 的 `config.json` → `save_config(load_config())` 必须拒绝。"""

    def _gbk_config(self):
        path = store.config_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        raw = json.dumps({"text_model": {"api_key": "sk-真钥匙", "model": "deepseek-flash"},
                          "search_providers": [{"name": "某个搜索源", "key": "k-1"}]},
                         ensure_ascii=False).encode("gbk")
        with open(path, "wb") as f:
            f.write(raw)
        return path, raw

    def test_a_gbk_config_refuses_the_save_and_keeps_every_byte(self):
        path, raw = self._gbk_config()
        with self.assertRaises(RuntimeError) as cm:
            store.save_config(store.load_config())       # 正是"点一次保存设置"那条路
        self.assertIn("config.json", str(cm.exception))
        with open(path, "rb") as f:
            self.assertEqual(f.read(), raw, "拒绝写盘时配置必须逐字节未变")
        self.assertIn("sk-真钥匙", raw.decode("gbk"), "夹具里真有钥匙，别做成空配置假绿")
        self.assertEqual(glob.glob(path + ".bak-*"), [], "守卫先于留档：连 .bak- 都不该多出来")

    def test_a_missing_config_is_written(self):
        self.assertFalse(os.path.exists(store.config_path()))
        store.save_config({"text_model": {"api_key": "sk-新的"}})
        self.assertEqual(store.load_config()["text_model"]["api_key"], "sk-新的")

    def test_a_zero_byte_config_is_written(self):
        path = store.config_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(b"")
        store.save_config({"text_model": {"api_key": "sk-新的"}})
        self.assertEqual(store.load_config()["text_model"]["api_key"], "sk-新的")
        self.assertEqual(glob.glob(path + ".bak-*"), [], "0 字节没有可留的旧内容")


class SaveConfigBackupTest(IsolatedCase):
    def test_overwriting_a_config_with_content_keeps_a_timestamped_backup(self):
        store.save_config({"text_model": {"api_key": "sk-旧钥匙"}})
        old = store.read_bytes(store.config_path())
        store.save_config({"text_model": {"api_key": "sk-新钥匙"}})

        backups = glob.glob(store.config_path() + ".bak-*")
        self.assertEqual(len(backups), 1, "覆盖有内容的配置必须留下一份 .bak-<时间戳>")
        self.assertEqual(store.read_bytes(backups[0]), old)
        self.assertIn("sk-旧钥匙", store.read_bytes(backups[0]).decode("utf-8"))


class CurrentProfileUnreadableTest(IsolatedCase):
    """P0-3：`current_profile.txt` **存在却读不出来**时，绝不静默退回 default。

    老实现 `except Exception: return "default"` 把两件相反的事混成一个信号：
      - 文件不存在 → default（合法，保持不变）；
      - 文件存在却有字节读不出来（记事本按 ANSI/GBK 存了中文档名、磁盘错误）→ 也 default。
    后者意味着：用户以为在"张三"下工作，**之后每一次写入都进 default**，
    界面上完全看不出来，而且**不可逆**（新记录已经落进另一个档）。
    """

    def test_a_gbk_profile_file_does_not_silently_fall_back_to_default(self):
        path = store.current_profile_file()
        with open(path, "wb") as f:
            f.write("张三".encode("gbk"))

        with self.assertRaises(RuntimeError) as cm:
            store.current_profile()
        self.assertIn("current_profile.txt", str(cm.exception))
        # 真正的杀伤面在 p_path()：它是所有写盘路径的入口，必须一起响亮失败，
        # 而不是把这次写入静默挪到 default 档去。
        with self.assertRaises(RuntimeError):
            store.p_path("jobs.json")
        with open(path, "rb") as f:
            self.assertEqual(f.read(), "张三".encode("gbk"), "只读：坏档名文件一个字节都不许动")

    def test_a_missing_file_is_still_default(self):
        os.remove(store.current_profile_file())
        self.assertEqual(store.current_profile(), "default")

    def test_a_blank_file_is_still_default(self):
        with open(store.current_profile_file(), "w", encoding="utf-8") as f:
            f.write("   \n")
        self.assertEqual(store.current_profile(), "default")


class SetCurrentProfileAtomicTest(IsolatedCase):
    """P0-3 要求 3：`set_current_profile` 必须**原子写**。

    老实现是"先 `open(fp, "w")` 把文件截断成 0 字节、再写"：写一半崩掉（或另一条线程 /
    另一个进程正好在这时读）看到的就是空文件，而空文件按约定等于 default ——
    用户被**静默**换档，正是这条需求要防的那件事。
    """

    def test_a_failed_replace_keeps_the_old_content(self):
        store.set_current_profile("dave")
        path = store.current_profile_file()

        with mock.patch.object(store.os, "replace", side_effect=OSError("拒绝访问")):
            with self.assertRaises(OSError):
                store.set_current_profile("erin")

        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"dave",
                             "写失败时旧档名必须原样还在（老实现这里会是 b'' → 静默变 default）")
        self.assertEqual(store.current_profile(), "dave")
        self.assertEqual(glob.glob(os.path.join(os.path.dirname(path), ".tmp*")), [],
                         "写失败不留临时文件")

    def test_a_successful_switch_writes_exactly_the_new_key(self):
        store.set_current_profile("张三")
        self.assertEqual(store.current_profile(), "张三")
        with open(store.current_profile_file(), "rb") as f:
            self.assertEqual(f.read(), "张三".encode("utf-8"), "原子写完，文件里没有半截旧值")
        self.assertEqual(glob.glob(os.path.join(os.path.dirname(store.current_profile_file()),
                                                ".tmp*")), [], "写完不留临时文件")


class BackupRotationTest(IsolatedCase):
    """`.bak-` 轮转：每个目标文件**只留最新 `store.BACKUP_KEEP` 份**，更旧的删掉。

    为什么需要它：`snapshot_before_overwrite` 每次覆盖都留一份，而 `upsert_job`
    （= `app._write_analysis`、`outcome.py` 的落盘出口）**每次分析**都走它 —— 连写 6 次就是 6 份；
    `简历迁移` / `身份迁移` 的整目录备份还会把这些 `.bak-` 一起拷走 ⇒ profile 目录无上限增长。
    """

    def test_the_keep_constant_is_three_with_a_stated_reason(self):
        """常量必须是模块级的、且值为 3（够覆盖"最近几次覆盖"，又不会无限长）。"""
        self.assertEqual(store.BACKUP_KEEP, 3)
        self.assertIn("3", (store.snapshot_before_overwrite.__doc__ or ""),
                      "轮转份数要写在留档函数的 docstring 里（不然下一个人不知道 3 从哪来）")

    def test_writing_five_times_keeps_exactly_the_three_newest(self):
        path = store.jobs_path()
        raws = []
        for i in range(1, 6):
            store.save_jobs([{"id": "第%d版" % i, "score": float(i)}])
            raws.append(store.read_bytes(path))

        baks = glob.glob(path + ".bak-*")
        self.assertEqual(len(baks), 3,
                         "连写 5 次只该留 3 份 .bak-，实际：%s"
                         % sorted(os.path.basename(b) for b in baks))
        self.assertEqual(sorted(store.read_bytes(b) for b in baks), sorted(raws[1:4]),
                         "留下的必须是**最近 3 次**覆盖前的样子（按内容断言，不是只看数量）")
        self.assertNotIn(raws[0], [store.read_bytes(b) for b in baks],
                         "最旧的那一份（第 1 次写进去的样子）必须被删掉")
        self.assertEqual(store.read_bytes(path), raws[4], "当前台账仍是最后一次写进去的那份")

    def test_the_analysis_path_upsert_job_rotates_too(self):
        """连写 6 次**分析**（`upsert_job`）也只留 3 份 —— 这正是实测过的"6 次就 6 份"。

        备份是**每次覆盖前**的内容，所以 6 次 upsert 依次留的是 `[0]`、`[1,0]`、`[2,1,0]`、
        `[3,2,1,0]`、`[4,3,2,1,0]`；留最新 3 份 ⇒ 首条 id 分别是第 2/3/4 次分析。
        """
        for i in range(6):
            store.upsert_job({"id": "第%d次分析" % i, "score": 80.0})

        baks = glob.glob(store.jobs_path() + ".bak-*")
        self.assertEqual(len(baks), 3, "连写 6 次分析只该留 3 份 .bak-")
        firsts = [json.loads(store.read_bytes(b).decode("utf-8"))[0]["id"] for b in baks]
        self.assertEqual(sorted(firsts), ["第2次分析", "第3次分析", "第4次分析"],
                         "留的必须是最近 3 份（按内容断言）：%s" % sorted(firsts))

    def test_a_short_history_is_not_deleted_early(self):
        """目标不存在（合法的第一次）→ 一份都不留；不足 3 份时**一份都不许删**。"""
        path = store.jobs_path()
        store.save_jobs([{"id": "第一次"}])
        self.assertEqual(glob.glob(path + ".bak-*"), [],
                         "第一次写没有旧内容可留（目标不存在）；0 字节的情况见 "
                         "UpsertJobRefusesUnreadableLedgerTest.test_a_zero_byte_ledger_is_created")
        store.save_jobs([{"id": "第二次"}])
        store.save_jobs([{"id": "第三次"}])
        self.assertEqual(len(glob.glob(path + ".bak-*")), 2, "只有 2 份时不许提前删")

    def test_rotation_orders_by_timestamp_not_by_filename(self):
        """仓库里 `.bak-` 有**两种真实命名**，判据必须是时间戳，不是文件名本身。

        `snapshot_before_overwrite` 用 `%Y%m%d%H%M%S%f`（纯数字 20 位），
        `jobdup._snapshot` 用 `%Y%m%d_%H%M%S_%f`（带下划线）。下划线 `_`(0x5F) 比数字大，
        直接按文件名排序会把同一天的两种格式**交错** —— 这里造一份"时间上最旧、文件名排序却不是最前"
        的 jobdup 格式备份，钉住"删的是时间上最旧那份"：按文件名排序会误删另一份。
        """
        path = store.jobs_path()
        store.save_jobs([{"id": "当前台账"}])
        day = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")     # 昨天：一定早于下面那次写
        oldest_jobdup = "%s.bak-%s_010203_000001" % (path, day)           # 时间上最旧
        store.write_bytes(oldest_jobdup, b"A-oldest-jobdup-format")
        second = "%s.bak-%s010203000002" % (path, day)                    # 时间上第二旧
        store.write_bytes(second, b"B-second")
        store.write_bytes("%s.bak-%s010203000003" % (path, day), b"C-third")
        # 按**文件名**排序时 B 排在 A 前面（`0` < `_`）⇒ 天真实现会误删 B、留下 A。

        store.save_jobs([{"id": "当前台账", "score": 1.0}])      # 再覆盖一次 → 触发轮转

        names = sorted(os.path.basename(b) for b in glob.glob(path + ".bak-*"))
        self.assertEqual(len(names), 3, "轮转后应当恰好 3 份：%s" % names)
        self.assertNotIn(os.path.basename(oldest_jobdup), names,
                         "被删的必须是**时间上最旧**的那份（按文件名排序会误删 B）：%s" % names)
        self.assertIn(os.path.basename(second), names,
                      "时间上不是最旧的那份不许被删（按文件名排序会删掉它）：%s" % names)


class WholeProfileBackupKeepsLedgerBackupsOutTest(IsolatedCase):
    """整目录备份（`简历迁移._backup_profile`）里**不许套 `.bak-` 文件**。

    那条「备份里不套备份」的闸门原本只挡**目录**前缀（`简历迁移备份_*`），挡不住
    `store.snapshot_before_overwrite` 留在 profile 里的 `.bak-<时间戳>`：每迁移一次就把上一次的
    留档再拷一份 ⇒ profile 目录无上限增长（`.bak-` 会被备份再备份）。

    ⚠️ 位置说明：这条用例本该落在 `tests/test_resume_migration.py`，但本轮施工的文件纪律只允许改
    `store.py` / 本文件 / `test_jddelete.py` / `test_jobdup.py`（另一个实现者正在改 `kb` 那条线），
    所以它落在 store 的测试文件里 —— 判据本身正是"整目录备份与 `store.BACKUP_SUFFIX` 的关系"。
    """

    def test_ledger_backups_are_not_copied_into_the_backup(self):
        cli = importlib.import_module("简历迁移")
        store.ensure_profile()
        store.save_jobs([{"id": "第一条"}])
        store.save_jobs([{"id": "第二条"}])                  # 这一步会留下 jobs.json.bak-<ts>
        store.write_text(store.config_path(), "{}")
        baks = glob.glob(store.jobs_path() + ".bak-*")
        self.assertEqual(len(baks), 1, "夹具前提：profile 里真有一份 .bak-（否则这条用例没在测东西）")

        dest = cli._backup_profile()                          # noqa: SLF001 - 被拷的就是它

        copied = sorted(os.path.relpath(os.path.join(dp, fn), dest)
                        for dp, _dn, fns in os.walk(dest) for fn in fns)
        self.assertEqual([n for n in copied if ".bak-" in n], [],
                         "整目录备份把 .bak- 一起拷走了（备份里再套备份 ⇒ 无上限增长）：%s" % copied)
        # 反假绿：数据文件**必须**照旧拷进去（别把闸门做成"什么都不拷"）
        self.assertIn("jobs.json", copied)
        self.assertIn("config.json", copied)
        self.assertTrue(os.path.exists(baks[0]), "备份是拷贝，原地的 .bak- 不许被动")


class ReadJdsStrictTest(IsolatedCase):
    """`read_jds_strict()`：**区分"读不出来"与"没有"**（`jds.json` 这一侧原先唯一的缺口）。

    为什么必须有它：`jdid.load_jds()` 在任何读/解析失败时都回一份**空库**
    （`{"version":1,"seq":0,"jds":[]}`，它的契约是"绝不抛异常"），而 `jdid.save_jds` 是
    **整份覆盖** —— 于是"一次读失败"就等于整个 JD 库被内存里那份空库顶掉。
    config / jobs / knowledge 三份早就有这对判据，jds 是漏掉的那一份。

    **实测（隔离探针，299 字节 / 2 条记录的 GBK `jds.json`）**：
      - `upsert_jd(一条新记录)` → 盘上 272 字节、**只剩新那一条**（旧 2 条被清、`.bak-` 0 份）
      - `next_seq()`           → 盘上 47 字节、`{"version":1,"seq":1,"jds":[]}`（**全清**、0 份）
    """

    def _path(self):
        return store.p_path("jds.json")

    def _write(self, blob, path=None):
        p = path or self._path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(blob)
        return p

    def test_a_missing_file_is_empty_with_no_reason(self):
        data, err = store.read_jds_strict()
        self.assertEqual(data, {"version": 1, "seq": 0, "jds": []})
        self.assertEqual(err, "", "文件不存在不是错误")

    def test_a_valid_file_returns_its_records(self):
        store.write_json(self._path(),
                         {"version": 1, "seq": 3, "jds": [{"id": "a"}, {"id": "b"}]})
        data, err = store.read_jds_strict()
        self.assertEqual([r["id"] for r in data["jds"]], ["a", "b"])
        self.assertEqual(data["seq"], 3)
        self.assertEqual(err, "")

    def test_a_non_utf8_file_reports_a_reason_instead_of_a_silent_empty(self):
        raw = json.dumps({"version": 1, "seq": 7, "jds": [
            {"id": "本科_字节跳动_数据标注_文本_北京_6-8K_0001", "company": "字节跳动"}]},
            ensure_ascii=False).encode("gbk")
        try:
            raw.decode("utf-8")
            raise AssertionError("夹具没坏：这串字节居然能按 UTF-8 解出来")
        except UnicodeDecodeError:
            pass
        self.assertIn("字节跳动", raw.decode("gbk"))          # 自证：内容真的在里面
        self._write(raw)
        data, err = store.read_jds_strict()
        self.assertEqual(data, {"version": 1, "seq": 0, "jds": []}, "读不出来时给的是空库占位")
        self.assertTrue(err, "坏文件必须给出原因，绝不能静默当成空库")
        self.assertIn("jds.json", err)
        self.assertIn("读不出来", err)

    def test_a_truncated_file_reports_a_reason(self):
        self._write(b'{"jds": [ <<<CORRUPT>>>')
        _, err = store.read_jds_strict()
        self.assertTrue(err)
        self.assertIn("jds.json", err)

    def test_a_wrong_shape_reports_a_reason(self):
        self._write(b'{"jds": "not-a-list"}')
        _, err = store.read_jds_strict()
        self.assertTrue(err, "结构不对也算读不出来")
        self._write(b"[]")                                     # 合法 JSON、但不是含 jds 的对象
        _, err2 = store.read_jds_strict()
        self.assertTrue(err2)

    def test_the_reason_names_the_file_that_was_actually_read(self):
        """判据是通用函数（调用方可以传别的 path）：原因里必须出现**那个**文件名。
        写死 `jds.json` 会让用户照着这句话去修错文件（与 `read_knowledge_strict` 同一条）。"""
        other = os.path.join(self.tmp, "别处的库.json")
        self._write(b'{"jds": [ <<<CORRUPT>>>', path=other)
        _, err = store.read_jds_strict(other)
        self.assertIn("别处的库.json", err)
        self.assertNotIn("jds.json", err)
        self._write(b'{"jds": "not-a-list"}', path=other)
        _, err2 = store.read_jds_strict(other)
        self.assertIn("别处的库.json", err2)
        self.assertNotIn("jds.json", err2)

    def test_it_does_not_write_anything(self):
        bad = b'{"jds": [ <<<CORRUPT>>>'
        path = self._write(bad)
        folder = os.path.dirname(path)
        before = sorted(os.listdir(folder))
        store.read_jds_strict()
        with open(path, "rb") as f:
            self.assertEqual(f.read(), bad, "只读：坏文件原样留原路径")
        self.assertEqual(sorted(os.listdir(folder)), before, "只读：不许多写/改名任何文件")


class JdsSafeToWriteTest(IsolatedCase):
    """`jds_safe_to_write()` —— `jds.json` 的写盘判据（与另外三份同形）。

    可写 = **读得出来 或 真的是 0 字节**；有字节却读不出来 → 不可写（调用方必须拒绝写盘）。
    """

    def _path(self):
        return store.p_path("jds.json")

    def _write(self, blob, path=None):
        p = path or self._path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(blob)
        return p

    def test_a_missing_library_is_safe(self):
        self.assertEqual(store.jds_safe_to_write(), (True, ""))

    def test_a_valid_library_is_safe(self):
        store.write_json(self._path(),
                         {"version": 1, "seq": 0, "jds": [{"id": "本科_字节_标注_文本_北京_6-8K_0001"}]})
        self.assertEqual(store.jds_safe_to_write(), (True, ""))

    def test_a_zero_byte_library_is_safe(self):
        """0 字节 = 合法的"第一次"，没有可丢的内容。"""
        self._write(b"")
        self.assertEqual(store.jds_safe_to_write(), (True, ""))

    def test_a_non_utf8_library_is_not_safe(self):
        raw = json.dumps({"version": 1, "seq": 7, "jds": [
            {"id": "本科_腾讯_语音评测_语音_深圳_13-26K_0002", "company": "腾讯"}]},
            ensure_ascii=False).encode("gbk")
        try:
            raw.decode("utf-8")
            raise AssertionError("夹具没坏：这串字节居然能按 UTF-8 解出来")
        except UnicodeDecodeError:
            pass
        self._write(raw)
        ok, err = store.jds_safe_to_write()
        self.assertFalse(ok, "有字节却读不出来 → 绝不能写盘")
        self.assertIn("jds.json", err)

    def test_a_truncated_library_is_not_safe(self):
        self._write(b'{"jds": [ <<<CORRUPT>>>')
        self.assertFalse(store.jds_safe_to_write()[0])

    def test_a_wrong_shape_library_is_not_safe(self):
        self._write('{"jds": "不是列表"}'.encode("utf-8"))
        self.assertFalse(store.jds_safe_to_write()[0])

    def test_a_whitespace_only_library_is_not_safe(self):
        self._write(b"\r\n   \r\n")
        ok, err = store.jds_safe_to_write()
        self.assertFalse(ok, "有字节（不是 0 字节）→ 不可写")
        self.assertIn("jds.json", err)

    def test_the_judgement_asks_the_path_it_was_given(self):
        """判据必须问**传进来的那个路径**（按 key 指定的档别拿默认 path 去判）。"""
        other = os.path.join(self.tmp, "别的档", "jds.json")
        self._write(b'{"jds": [ <<<CORRUPT>>>', path=other)
        ok, err = store.jds_safe_to_write(other)
        self.assertFalse(ok)
        self.assertIn("jds.json", err)
        self.assertEqual(store.jds_safe_to_write(), (True, ""), "默认那份不存在，不该被别的档连累")

    def test_it_does_not_write_anything(self):
        bad = b'{"jds": [ <<<CORRUPT>>>'
        path = self._write(bad)
        folder = os.path.dirname(path)
        before = sorted(os.listdir(folder))
        store.jds_safe_to_write()
        with open(path, "rb") as f:
            self.assertEqual(f.read(), bad, "只读：坏库原样留原路径")
        self.assertEqual(sorted(os.listdir(folder)), before, "只读：不许多写/改名任何文件")


class PruneBackupsPublicEntryTest(IsolatedCase):
    """轮转的**公开入口**：`store.prune_backups`（`jobdup` 要给知识库 / 面试记录轮转）。

    为什么要有它：`jobdup` 原先直接调带下划线的 `store._prune_backups` —— 跨模块调私有名，
    哪天 store 把它改名/挪走，就是**静默**失效（备份无上限增长，没人报错）。
    """

    def test_it_is_public_and_is_the_same_single_implementation(self):
        self.assertTrue(callable(store.prune_backups), "缺少公开的 prune_backups")
        self.assertIs(store.prune_backups, store._prune_backups,
                      "公开入口必须是同一份实现（两份轮转判据会各自漂移）")

    def test_it_rotates_to_the_keep_limit_and_reports_what_it_removed(self):
        path = os.path.join(self.tmp, "目标.json")
        names = ["%s.bak-2026010100000%d" % (path, i) for i in range(1, 6)]
        for i, n in enumerate(names):
            store.write_bytes(n, b"v%d" % i)
        removed = store.prune_backups(path)                       # 默认 keep=store.BACKUP_KEEP
        left = sorted(os.path.basename(n) for n in glob.glob(path + ".bak-*"))
        self.assertEqual(len(left), store.BACKUP_KEEP, "轮转后只该留最新 keep 份：%s" % left)
        self.assertEqual(left, sorted(os.path.basename(n) for n in names[-store.BACKUP_KEEP:]))
        self.assertEqual(sorted(os.path.basename(n) for n in removed),
                         sorted(os.path.basename(n) for n in names[:-store.BACKUP_KEEP]),
                         "被删的是最旧那几份，且返回值要如实给出它们")

    def test_the_script_no_longer_claims_backups_are_never_deleted(self):
        """`岗位去重.py` 的文案必须与实现一致：`.bak-` 只留最新 `store.BACKUP_KEEP`（= 3）份。

        旧文案写的是"保留不删"/"（确认无误后可自行删除）"—— 说过头了：轮转会把更旧的删掉。
        只钉文案（逻辑一个字不动），否则下一个人照着这句话会以为备份永远都在。
        """
        src = store.read_text(os.path.join(store.ROOT, "岗位去重.py"))
        self.assertNotIn("保留不删", src, "文案还写着「保留不删」，与轮转实现不符")
        self.assertIn("BACKUP_KEEP", src, "文案该说清「只留最新 BACKUP_KEEP 份」")


class UpsertJobConcurrencyTest(IsolatedCase):
    """P0-5（体检报告 §2 / §10 的 1.9）：`jobs.json` 的**读-改-写必须整体互斥**。

    老实现（`store.py` 里一把锁都没有，而 `jds.json` / `knowledge.json` / `resumes.json` 都有）
    下这条必红：两个线程各自 `load_jobs()` 读到同一份旧台账、各自插入自己那条、再**整份覆盖**
    写回 —— 后写的那份把先写的顶掉（主线程实测 20/20 全丢）。报告 §12.3 里那条 20% 的
    `[WinError 5]` 是同一个根因。

    ⚠️ 这里**故意把竞争窗口拉宽**（在 `store.write_json` 里注入一次 sleep）：断言本身一个字都没
    放宽（写进去的每一条都必须还在），拉宽的只是"让无锁实现必然露馅"的时序 —— 不拉宽的话
    GIL + 很小的台账会让无锁实现偶尔侥幸通过，变异取证就杀不掉它。
    加了锁之后这些 sleep 只发生在**持锁的临界区内部**，另一个线程根本进不来。
    """

    THREADS = 2
    PER_THREAD = 5
    ROUNDS = 20
    WRITE_DELAY_SECONDS = 0.003

    def _slow_write_json(self, path, obj):
        time.sleep(self.WRITE_DELAY_SECONDS)
        return self._real_write_json(path, obj)

    def _writer(self, ids, barrier, errors):
        try:
            for i in ids:
                # 两个线程的**每一次**写入都从同一个时刻出发（与 test_app.py 的线程用例同一做法：
                # 用 Event/Barrier 定序，不靠 sleep 赌时序）。
                barrier.wait(30)
                store.upsert_job({"id": i, "score": 60.0, "resume": "并发夹具"})
        except BaseException as e:            # noqa: BLE001 - 线程里的异常必须能被断言看到
            errors[threading.current_thread().name] = e

    def test_concurrent_upserts_lose_nothing(self):
        self._real_write_json = store.write_json
        expected, errors = [], {}
        with mock.patch.object(store, "write_json", self._slow_write_json):
            for r in range(self.ROUNDS):
                barrier = threading.Barrier(self.THREADS)
                ids = [["第%d轮-%s-%d" % (r, who, i) for i in range(self.PER_THREAD)]
                       for who in ("A", "B")]
                ts = [threading.Thread(target=self._writer, args=(ids[w], barrier, errors),
                                       name="writer-%d" % w) for w in range(self.THREADS)]
                for t in ts:
                    t.start()
                for t in ts:
                    t.join(60)
                self.assertFalse(any(t.is_alive() for t in ts), "有写线程没跑完（死等）")
                self.assertEqual(errors, {}, "写线程里抛了异常：%r" % errors)
                expected.extend(ids[0] + ids[1])
                got = [j.get("id") for j in store.load_jobs()]
                self.assertEqual(len(got), len(expected),
                                 "第 %d 轮之后台账**丢了记录**（丢更新）：期望 %d 条、实际 %d 条"
                                 % (r + 1, len(expected), len(got)))
        self.assertEqual(sorted(got), sorted(expected),
                         "台账里的 id 与写进去的对不上：写了 %d 条、盘上 %d 条"
                         % (len(expected), len(got)))

    def test_the_locks_are_module_level_reentrant_locks(self):
        """照 `kb._KB_LOCK` / `jdid._JD_LOCK` 的做法：模块级 **RLock**。

        `upsert_job` 里 load→save 必须在**同一把锁里重入**，普通 `Lock` 会当场死锁；
        两把锁都是**叶子锁**（store 不 import 任何别的模块），谁都不会去反过来抢
        `_JD_LOCK` / `_KB_LOCK`，所以锁序恒为"上层锁 → store 的锁"。
        """
        for name in ("_JOBS_LOCK", "_CONFIG_LOCK"):
            self.assertTrue(hasattr(store, name), "缺少模块级锁 %s" % name)
            lock = getattr(store, name)
            with lock:
                with lock:                        # 重入必须不死锁
                    pass


class AtomicWriteTest(IsolatedCase):
    """P3-5：`write_text` / `write_bytes` 也必须是**原子**的（同目录临时文件 + `os.replace`）。

    老实现是"先 `open(path, "w"/"wb")` 把目标**截断成 0 字节**、再一段段写"：中途失败
    （磁盘满 / 被 kill / 断电）留下的是**半截文件**，而**同一个模块**里的 `write_json`
    早就是原子的（mkstemp + `os.replace`）—— 一个模块两套语义，调用点（报告 HTML、JD 原文、
    报告就地改写、`jobdup` 留档快照、`jdid` 回滚）全都暴露在"把用户唯一那份写成半截"之下。

    下面每条**都**在改前的实现上红，红在哪一行写在各自 docstring 里。
    """

    def _leftover_temps(self, room):
        return sorted(n for n in os.listdir(room) if n.startswith(".tmp-"))

    def test_a_failed_write_bytes_keeps_the_old_bytes_and_leaves_no_temp_file(self):
        """写失败（mock `os.replace` 抛 OSError）→ 目标**逐字节仍是旧内容**、且不留 `.tmp-`。

        改前红在第 ① 条断言：老实现先把目标截断成 0 字节、紧接着写进了 `b"NEW"`，
        `os.replace` 压根没被调用 ⇒ 盘上是 `b"NEW"`（旧内容已经没了）。
        """
        path = os.path.join(self.tmp, "旧的.bin")
        old = b"OLD-\xe4\xb8\xad\xe6\x96\x87-\r\n"
        store.write_bytes(path, old)

        with mock.patch.object(store.os, "replace", side_effect=OSError(28, "磁盘空间不足")):
            with self.assertRaises(OSError):
                store.write_bytes(path, b"NEW")

        self.assertEqual(store.read_bytes(path), old,
                         "写失败时旧内容必须一个字节都没变（老实现：目标已被截断后写成了半截）")
        self.assertEqual(self._leftover_temps(self.tmp), [],
                         "失败时不许在目标目录里留下 .tmp- 垃圾")

    def test_a_failed_write_text_keeps_the_old_content_and_leaves_no_temp_file(self):
        """同上，`write_text` 那一支（报告 HTML / JD 原文 / 就地改写走的就是它）。"""
        path = os.path.join(self.tmp, "旧报告.html")
        store.write_text(path, "<html>旧报告\r\n第二行</html>")
        old = store.read_bytes(path)

        with mock.patch.object(store.os, "replace", side_effect=OSError(28, "磁盘空间不足")):
            with self.assertRaises(OSError):
                store.write_text(path, "<html>新的</html>")

        self.assertEqual(store.read_bytes(path), old, "写失败时旧报告必须一个字都没变")
        self.assertEqual(self._leftover_temps(self.tmp), [], "失败时不许留下 .tmp- 垃圾")

    def test_the_temp_file_lives_in_the_target_directory(self):
        """临时文件必须与目标**同目录**：`os.replace` 只在同一文件系统内才是原子的。

        改前红在第 ① 条断言：老实现**没有**任何 `os.replace`，`srcs` 是空列表 ⇒ `[] != [path]`。
        """
        path = os.path.join(self.tmp, "同一目录.txt")
        srcs = []
        real_replace = os.replace

        def spy(src, dst):
            srcs.append((src, dst))
            return real_replace(src, dst)

        with mock.patch.object(store.os, "replace", side_effect=spy):
            store.write_text(path, "内容")

        self.assertEqual([dst for _src, dst in srcs], [path], "写出必须靠 os.replace 换上去")
        self.assertEqual(os.path.dirname(srcs[0][0]), os.path.dirname(path),
                         "临时文件必须在目标**同一个目录**里（跨盘 os.replace 会退化成复制）")
        self.assertEqual(self._leftover_temps(self.tmp), [], "成功之后临时文件不许留在盘上")

    def test_a_successful_write_text_is_byte_identical_to_the_old_text_mode_write(self):
        """写成功 → 与改前那条实现（文本模式直写）的产物**逐字节**相同。

        为什么拿"文本模式直写"当参照物：改前的 `write_text` 是
        `open(path, "w", encoding="utf-8")`，而文本模式在 Windows 上会把 `\\n` 翻成 `\\r\\n`。
        新实现若图省事直接 `content.encode("utf-8")`，盘上每个换行都变了 —— 那是**行为变更**，
        不是修 bug。CRLF / 裸 CR / 非 ASCII / 空文件一起钉住。
        """
        cases = ["", "a\nb", "a\r\nb", "a\rb", "a\r\nb\nc\rd", "中文\n结束\r\n", "x\n" * 3]
        for i, text in enumerate(cases):
            ref = os.path.join(self.tmp, "ref-%d.txt" % i)
            got = os.path.join(self.tmp, "got-%d.txt" % i)
            with open(ref, "w", encoding="utf-8") as f:        # 改前那条实现
                f.write(text)
            store.write_text(got, text)
            with open(ref, "rb") as f:
                self.assertEqual(store.read_bytes(got), f.read(),
                                 "第 %d 个用例的产物与改前不一致：%r" % (i, text))

    def test_a_successful_write_bytes_is_byte_identical_to_the_raw_bytes(self):
        """`write_bytes` 仍按**原始字节**写（含 0 字节、非 UTF-8、空文件）。"""
        for i, raw in enumerate([b"", b"a\r\nb", b"\x00\xff\xfe", "中文".encode("gbk")]):
            path = os.path.join(self.tmp, "raw-%d.bin" % i)
            store.write_bytes(path, raw)
            self.assertEqual(store.read_bytes(path), raw, "第 %d 个用例不是逐字节相同" % i)

    def test_a_write_that_cannot_be_encoded_does_not_touch_the_old_file(self):
        """正文编不出来（孤立代理项）时，旧文件也不能被动到。

        改前红在第 ① 条断言：老实现先截断、再在写的时候抛 `UnicodeEncodeError`
        ⇒ 盘上是被清空的旧文件（用户那份唯一的报告就成这样了）。
        """
        path = os.path.join(self.tmp, "旧报告.html")
        store.write_text(path, "<html>旧报告</html>")
        with self.assertRaises(UnicodeEncodeError):
            store.write_text(path, "孤立的代理项：\ud800")
        self.assertEqual(store.read_bytes(path), "<html>旧报告</html>".encode("utf-8"))
        self.assertEqual(self._leftover_temps(self.tmp), [], "编码失败也要清掉临时文件")


class WriteJsonByteIdentityTest(IsolatedCase):
    """P3-5/P3-6：`write_json` 的**产物**逐字节不变，但内部改成共用同一条原子写 + 加 fsync。"""

    @staticmethod
    def _legacy_write_json(path, obj):
        """改前那份 `write_json` 的逐字复制（mkstemp + 文本模式 `json.dump`）。

        它就是"改前产物长什么样"的**夹具**：下面的断言拿它与新实现的输出逐字节对比，
        格式差一个空格都算行为变更。
        """
        d = os.path.dirname(path)
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=d)
        os.close(fd)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def test_the_output_is_byte_identical_to_the_old_implementation(self):
        fixtures = [
            {},
            [],
            {"nodes": []},
            {"a": 1, "中文": "值\r\n带换行", "巢": {"b": [1, 2.5, None, True, "x\ny"]}},
            [{"id": "x", "score": 80.0, "备注": "多行\n换行\r\n结束"}],
            {"空": "", "列表": [{"深": {"更深的": ["中文", 1]}}]},
        ]
        for i, obj in enumerate(fixtures):
            ref = os.path.join(self.tmp, "ref-%d.json" % i)
            got = os.path.join(self.tmp, "got-%d.json" % i)
            self._legacy_write_json(ref, obj)
            store.write_json(got, obj)
            self.assertEqual(store.read_bytes(got), store.read_bytes(ref),
                             "第 %d 个夹具的 JSON 产物与改前不一致" % i)
        self.assertEqual([n for n in os.listdir(self.tmp) if n.startswith(".tmp-")], [],
                         "写完不许留临时文件")

    def test_flush_and_fsync_happen_before_the_rename(self):
        """先把字节**冲进盘**（flush + fsync）再 `os.replace` —— 这就是 P3-6 那条"断电丢最后一次写入"。

        ⚠️ 说清这条钉子的**性质**：它是"调用顺序"的钉子（spy 记录 `os.fsync` / `os.replace`
        的先后），**不是**对"断电后数据还在"的证明 —— 后者在单元测试里验不了（要真的拔电源）。
        它的用处是：谁把 `fsync` 那两行删掉，这条会红，而不是"删了也没有一条测试发现"。
        """
        path = os.path.join(self.tmp, "x.json")
        calls = []
        real_fsync, real_replace = os.fsync, os.replace

        def spy_fsync(fd):
            calls.append("fsync")
            return real_fsync(fd)

        def spy_replace(src, dst):
            calls.append("replace")
            return real_replace(src, dst)

        with mock.patch.object(os, "fsync", side_effect=spy_fsync), \
                mock.patch.object(os, "replace", side_effect=spy_replace):
            store.write_json(path, {"a": 1})

        self.assertEqual(calls[-1], "replace", "os.replace 必须是最后一步（fsync 之后）")
        self.assertIn("fsync", calls[:-1], "写临时文件之后、replace 之前必须 fsync 一次")
        self.assertEqual(store.read_json(path, None), {"a": 1}, "加了 fsync 之后内容照旧")


class SaveStreamAtomicTest(IsolatedCase):
    """P3-7：`save_stream` 失败**不许删旧文件**。

    老实现直接 `open(path, "wb")`（先截断同名旧文件），失败时 `os.remove(path)` 把**旧文件
    也删了** —— 同名的重传/重试一失败，用户原来那份录音就没了。
    """

    def _leftover_temps(self, room):
        return sorted(n for n in os.listdir(room) if n.startswith(".tmp-"))

    def test_a_failed_save_stream_keeps_the_old_file(self):
        """超限失败 → 旧文件**原封不动**、半截文件与 `.tmp-` 都不留。

        改前红在第 ② 条断言：老实现 `os.remove(path)` 把旧录音一起删了 ⇒ 盘上什么都没有。
        """
        store.save_stream("iv1", "audio.wav", io.BytesIO(b"OLD-AUDIO-BYTES"), max_bytes=10 * 1024)
        path = store.interview_file("iv1", "audio.wav")

        with self.assertRaises(ValueError):
            store.save_stream("iv1", "audio.wav", io.BytesIO(b"x" * 5000), max_bytes=1000)

        self.assertTrue(os.path.exists(path), "失败时不许把旧录音删掉")
        self.assertEqual(store.read_bytes(path), b"OLD-AUDIO-BYTES")
        self.assertEqual(self._leftover_temps(os.path.dirname(path)), [],
                         "失败时不许留下 .tmp- 垃圾")

    def test_a_failed_save_stream_leaves_no_half_written_new_file(self):
        """本来没有同名文件时（第一次上传）：失败不留半截（老行为，别改坏）。"""
        with self.assertRaises(ValueError):
            store.save_stream("iv1", "audio.wav", io.BytesIO(b"x" * 5000), max_bytes=1000)
        self.assertFalse(os.path.exists(store.interview_file("iv1", "audio.wav")))
        self.assertEqual(self._leftover_temps(store.interview_dir("iv1")), [],
                         "失败时不许留下 .tmp- 垃圾")

    def test_a_successful_save_stream_replaces_the_old_file(self):
        """成功 → 返回写入字节数、内容逐字节正确、旧的被换掉，且不留临时文件。"""
        store.save_stream("iv1", "audio.wav", io.BytesIO(b"OLD"), max_bytes=10 * 1024)
        payload = "新录音".encode("utf-8") + b"\x00\x01"
        n = store.save_stream("iv1", "audio.wav", io.BytesIO(payload), max_bytes=10 * 1024)
        path = store.interview_file("iv1", "audio.wav")
        self.assertEqual(n, len(payload))
        self.assertEqual(store.read_bytes(path), payload)
        self.assertEqual(self._leftover_temps(os.path.dirname(path)), [],
                         "成功之后临时文件不许留在盘上")

    def test_the_new_bytes_go_in_through_replace_not_a_direct_write(self):
        """新内容必须是 `os.replace` 换上去的（而不是往目标名上直写）。

        改前红在第 ① 条断言：老实现从头到尾没有 `os.replace` ⇒ 列表是空的。
        """
        path = store.interview_file("iv1", "audio.wav")
        dsts = []
        real_replace = os.replace

        def spy(src, dst):
            dsts.append((src, dst))
            return real_replace(src, dst)

        with mock.patch.object(store.os, "replace", side_effect=spy):
            store.save_stream("iv1", "audio.wav", io.BytesIO(b"NEW"), max_bytes=10 * 1024)

        self.assertEqual([d for _s, d in dsts], [path])
        self.assertEqual(os.path.dirname(dsts[0][0]), os.path.dirname(path))


class ElementShapeStrictDeepTest(IsolatedCase):
    """元素级形状：`read_*_strict_deep()` = 既有容器校验 **+ 每一项都必须是对象**。

    **为什么另起一层**（而不是把校验塞进 `read_*_strict`）：那三个函数还被
    `jobs_safe_to_write` / `jds_safe_to_write` / `knowledge_safe_to_write` 等**写路径**当判据用着
    —— 在里面收紧会让"能写"翻成"拒绝写"，而写路径的既有语义**一个字都不许动**
    （本类最后两条用例就是这条守卫的钉子）。读路径上则是另一件必须修的事实：
    元素不是对象时上层会**静默丢掉**它 —— `[{好记录}, 1]` 只显示 1 条、`[1,2,3]` 显示 0 条，
    坏的那部分**没人看得见**（又是一句"共 0 个岗位"级别的假话）。

    人话与"容器坏"同一把尺子：`<文件> x.json 读不出来：第 N 条不是对象`。
    """

    def _write(self, path, blob):
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        data = blob if isinstance(blob, bytes) else blob.encode("utf-8")
        with open(path, "wb") as f:
            f.write(data)
        return path

    # ---------- jobs ----------
    def test_a_non_object_job_record_is_reported_with_its_position(self):
        self._write(store.jobs_path(), json.dumps([{"id": "a"}, {"id": "b"}, 3],
                                                  ensure_ascii=False))
        data, err = store.read_jobs_strict_deep()
        self.assertEqual(data, [], "读不出来时给的是空列表占位")
        self.assertIn("jobs.json", err)
        self.assertIn("读不出来", err)
        self.assertIn("第 3 条不是对象", err)

    def test_a_half_broken_ledger_is_reported_not_partially_readable(self):
        """半坏（`[{好记录}, 1]`）也要报 —— 只说"好的那部分读得出来"就是让坏的那条静默消失。"""
        self._write(store.jobs_path(), '[{"id": "g"}, 1]')
        data, err = store.read_jobs_strict_deep()
        self.assertTrue(err, "半坏被当成读得出来（上层会静默丢掉坏的那条）")
        self.assertEqual(data, [])
        self.assertIn("第 2 条不是对象", err)

    def test_every_non_object_shape_is_caught(self):
        for blob, pos in (("[1, 2, 3]", 1), ("[null]", 1), ('[{"id": "a"}, "x"]', 2),
                          ("[[], {}]", 1), ('[true]', 1)):
            with self.subTest(blob=blob):
                self._write(store.jobs_path(), blob)
                _, err = store.read_jobs_strict_deep()
                self.assertIn("第 %d 条不是对象" % pos, err)

    def test_jobs_control_group_is_still_empty(self):
        """对照组：不存在 / **合法的空列表** / 全是对象 ⇒ 照旧"没有"，不是"读不出来"。

        ⚠️ 真 0 字节这一格**两边都不回空**（`json.load` 对空文件的报错是既有的浅读语义）——
        所以这里钉的是"深读与浅读逐字一致"，"0 字节 = 合法的第一次"那条判据在
        `file_has_bytes_to_lose` / 路由的 `_unreadable` 那一层（`test_app` 的对照组钉着它）。
        """
        self.assertEqual(store.read_jobs_strict_deep(), ([], ""))
        self._write(store.jobs_path(), b"")
        self.assertEqual(store.read_jobs_strict_deep(), store.read_jobs_strict(),
                         "深读改变了 0 字节这一侧的判定（写路径判据用的是浅读，语义不许动）")
        self._write(store.jobs_path(), "[]")
        self.assertEqual(store.read_jobs_strict_deep(), ([], ""))
        self._write(store.jobs_path(), '[{"id": "a"}]')
        self.assertEqual(store.read_jobs_strict_deep(), ([{"id": "a"}], ""))

    # ---------- jds ----------
    def test_a_non_object_jd_record_is_reported(self):
        self._write(store.p_path("jds.json"),
                    json.dumps({"version": 1, "seq": 2, "jds": [{"id": "a"}, 1]},
                               ensure_ascii=False))
        data, err = store.read_jds_strict_deep()
        self.assertEqual(data, {"version": 1, "seq": 0, "jds": []}, "读不出来时给的是空库占位")
        self.assertIn("jds.json", err)
        self.assertIn("第 2 条不是对象", err)

    def test_jds_control_group_is_still_empty(self):
        self.assertEqual(store.read_jds_strict_deep(), ({"version": 1, "seq": 0, "jds": []}, ""))
        self._write(store.p_path("jds.json"), b"")
        self.assertEqual(store.read_jds_strict_deep(), store.read_jds_strict(),
                         "深读改变了 0 字节这一侧的判定")
        self._write(store.p_path("jds.json"), '{"version": 1, "seq": 0, "jds": []}')
        self.assertEqual(store.read_jds_strict_deep()[1], "")
        self._write(store.p_path("jds.json"), '{"version": 1, "seq": 0, "jds": [{"id": "a"}]}')
        self.assertEqual(store.read_jds_strict_deep()[1], "")

    # ---------- knowledge ----------
    def test_a_non_object_knowledge_node_is_reported(self):
        self._write(store.knowledge_path(), '{"nodes": [1, 2]}')
        data, err = store.read_knowledge_strict_deep()
        self.assertEqual(data, {"nodes": []}, "读不出来时给的是空库占位")
        self.assertIn("knowledge.json", err)
        self.assertIn("第 1 条不是对象", err)

    def test_knowledge_control_group_is_still_empty(self):
        self.assertEqual(store.read_knowledge_strict_deep(), ({"nodes": []}, ""))
        self._write(store.knowledge_path(), b"")
        self.assertEqual(store.read_knowledge_strict_deep(), store.read_knowledge_strict(),
                         "深读改变了 0 字节这一侧的判定")
        self._write(store.knowledge_path(), '{"nodes": []}')
        self.assertEqual(store.read_knowledge_strict_deep()[1], "")
        self._write(store.knowledge_path(), '{"nodes": [{"id": "a"}]}')
        self.assertEqual(store.read_knowledge_strict_deep()[1], "")

    def test_the_reason_names_the_file_that_was_actually_read(self):
        """元素坏那一侧同样要插值文件名（与容器坏那一侧同一把尺子）。"""
        other = os.path.join(self.tmp, "别处的库.json")
        self._write(other, '{"nodes": [1]}')
        _, err = store.read_knowledge_strict_deep(other)
        self.assertIn("别处的库.json", err)
        self.assertNotIn("knowledge.json", err)
        self._write(other, '{"nodes": "不是列表"}')          # 容器坏：照旧由既有那层报
        self.assertIn("别处的库.json", store.read_knowledge_strict_deep(other)[1])

    def test_it_does_not_write_anything(self):
        bad = b'{"nodes": [1]}'
        path = self._write(store.knowledge_path(), bad)
        folder = os.path.dirname(path)
        before = sorted(os.listdir(folder))
        store.read_knowledge_strict_deep()
        with open(path, "rb") as f:
            self.assertEqual(f.read(), bad, "只读：元素坏的文件原样留原路径")
        self.assertEqual(sorted(os.listdir(folder)), before, "只读：不许多写/改名任何文件")

    # ---------- 写路径守卫（问题 1 的 3.）：既有语义一个字都不许改 ----------
    def test_the_shallow_strict_reads_were_not_tightened(self):
        """⚠️ 守卫：`read_*_strict` 对**元素坏**照旧说"读得出来"（它们是写路径的判据）。"""
        self._write(store.jobs_path(), '[{"id": "a"}, 1]')
        self._write(store.p_path("jds.json"), '{"version": 1, "seq": 0, "jds": [1]}')
        self._write(store.knowledge_path(), '{"nodes": [1]}')
        self.assertEqual(store.read_jobs_strict(), ([{"id": "a"}, 1], ""))
        self.assertEqual(store.read_jds_strict()[0]["jds"], [1])
        self.assertEqual(store.read_jds_strict()[1], "")
        self.assertEqual(store.read_knowledge_strict(), ({"nodes": [1]}, ""))

    def test_all_three_write_path_rulers_ask_element_shape(self):
        """三个写判据（台账 / JD 库 / 知识库）都问**元素**形状：元素坏 ⇒ 不可写 + 说第几条。

        沿革：第三轮只收紧 `jobs_safe_to_write`（当时 `jds` / `knowledge` 记成"已知未修"，
        本用例当时的名字就是 `test_only_the_jobs_write_path_ruler_was_tightened`）；第四轮把
        那两件做掉了 —— 所以这条现在钉的是**三个都收紧**这件事本身，防有人把它们改回浅读。
        （顺序上先有 `JdsKnowledgeWritePathElementShapeTest` 的行为用例，这条只负责"三个一起"。）

        ⚠️ 浅读那一半（`test_the_shallow_strict_reads_were_not_tightened`）**故意不动**：
        `read_*_strict` 仍然只查容器 —— 收紧落在各 `*_safe_to_write` 自己身上（它改问深读），
        不是把元素校验塞进浅读（那会把"读得出来"这个信号本身改掉）。
        """
        self._write(store.jobs_path(), '[{"id": "a"}, 1]')
        self._write(store.p_path("jds.json"), '{"version": 1, "seq": 0, "jds": [1]}')
        self._write(store.knowledge_path(), '{"nodes": [1]}')
        for name, fn, pos in (("台账", store.jobs_safe_to_write, 2),
                              ("JD 库", store.jds_safe_to_write, 1),
                              ("知识库", store.knowledge_safe_to_write, 1)):
            with self.subTest(file=name):
                ok, err = fn()
                self.assertFalse(ok, "%s的元素坏仍回「可写」" % name)
                self.assertIn("第 %d 条不是对象" % pos, err)


class JobsWritePathElementShapeTest(IsolatedCase):
    """③ 写路径判据也认元素形状：元素坏 ⇒ `(False, 人话)`，`upsert_job` 如实拒绝而不是炸。

    病根（既有行为）：读路径 `read_jobs_strict_deep` 早就会报"第 N 条不是对象"，
    而写路径判据 `jobs_safe_to_write` 只问**浅读**（只看容器）⇒ 对同一份坏库回 `(True, "")`
    ⇒ 谁接着 `upsert_job(...)` 就在坏元素上 `AttributeError: 'int' object has no attribute 'get'`。
    盘上文件确实没受损（写盘之前就炸了），但那是**未捕获异常**，不是"如实的前置条件拒绝"。

    修法只落在 `jobs_safe_to_write`（改问 `read_jobs_strict_deep`）⇒ `upsert_job` 走它
    **原有的**前置条件分支（`RuntimeError` + "拒绝写入" + 原因），类型与文案都是既有口径。
    """

    def _write(self, path, blob):
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        data = blob if isinstance(blob, bytes) else blob.encode("utf-8")
        with open(path, "wb") as f:
            f.write(data)
        return path

    def test_a_non_object_element_makes_the_ledger_not_safe_to_write(self):
        """`[1,2,3]` / `[{好},1]` ⇒ 不可写，原因要说清是**第几条**不是对象。"""
        for blob, pos in (("[1, 2, 3]", 1), ('[{"id": "a"}, 1]', 2)):
            with self.subTest(blob=blob):
                self._write(store.jobs_path(), blob)
                ok, err = store.jobs_safe_to_write()
                self.assertFalse(ok, "元素坏却仍回「可写」：%r" % ((ok, err),))
                self.assertIn("第 %d 条不是对象" % pos, err)
                self.assertIn("jobs.json", err)

    def test_upsert_refuses_with_the_existing_precondition_error(self):
        """同一份坏库上 `upsert_job` ⇒ 既有的前置条件异常，且盘上逐字节不变、不留副本。"""
        blob = b'[{"id": "a"}, 1]'
        path = self._write(store.jobs_path(), blob)
        folder = os.path.dirname(path)
        before = sorted(os.listdir(folder))

        with self.assertRaises(RuntimeError) as ctx:
            store.upsert_job({"id": "刚跑完的分析", "score": 88.0})

        self.assertNotIsInstance(ctx.exception, AttributeError,
                                 "还是那个未捕获的 AttributeError（不是如实的前置条件拒绝）")
        msg = str(ctx.exception)
        self.assertIn("拒绝写入", msg)              # 既有口径：与别的坏库同一个出口
        self.assertIn("第 2 条不是对象", msg)       # 原因如实带出来
        with open(path, "rb") as f:
            self.assertEqual(f.read(), blob, "盘上台账被动了：写盘之前就该拒绝")
        self.assertEqual(sorted(os.listdir(folder)), before,
                         "拒绝时不该多出 .bak- 副本（说明它走到了写盘那一步）")

    def test_the_control_group_is_still_writable(self):
        """对照组：不存在 / 正常台账 / 合法空列表 `[]` / 0 字节 ⇒ 照旧 `(True, "")`。"""
        self.assertEqual(store.jobs_safe_to_write(), (True, ""), "文件不存在 = 合法的「第一次」")
        store.save_jobs([{"id": "a", "score": 80}])
        self.assertEqual(store.jobs_safe_to_write(), (True, ""), "正常台账必须照旧可写")
        self._write(store.jobs_path(), "[]")
        self.assertEqual(store.jobs_safe_to_write(), (True, ""), "合法的空列表必须照旧可写")
        self._write(store.jobs_path(), b"")
        self.assertEqual(store.jobs_safe_to_write(), (True, ""), "0 字节 = 合法的「第一次」")


class JdsKnowledgeWritePathElementShapeTest(IsolatedCase):
    """第四轮：把 ③ 那把尺子同样落到 `jds.json` / `knowledge.json` 的**写**判据上。

    病根与 `JobsWritePathElementShapeTest` 逐字相同：写判据只问**浅读**（只看容器）⇒
    `[{"好"}, 1]` 回 `(True, "")`。但这里的**表现与台账不同、而且更坏**：
    `jdid.load_jds()` / `kb.ensure_kb()` 在**读**的时候就把非对象元素过滤掉（这是它们"绝不抛"
    的容错），于是写路径**不会炸** —— 它会**照常成功**，顺手把那条坏元素从盘上**静默抹掉**。
    用户永远不知道自己那个文件被改过，也没有任何提示（台账那边至少是 AttributeError / 拒绝）。
    体检报告 §7.3 曾把它记成"已知未修"（收紧要连带改拒绝话术与 `kb._with_kb_write` 的
    "留档后写"语义）—— 第四轮把话术与那条新分支一起改了，判据本身只改这几行：
    「读不干净的文件一个字节都不碰」。

    ⚠️ 这里只钉**判据**；拒绝的**出口**在 `tests/test_jdid.py::UpsertElementShapeTest`
    与 `tests/test_kb.py::ElementBrokenLibraryIsRefusedTest`（分开钉，否则这里绿了
    也可能只是"多了个没人用的函数"）。
    """

    def _write(self, path, blob):
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        data = blob if isinstance(blob, bytes) else blob.encode("utf-8")
        with open(path, "wb") as f:
            f.write(data)
        return path

    def test_jds_write_criterion_asks_element_shape(self):
        """`{"jds": [1,2,3]}` / `{"jds": [{好}, 1]}` ⇒ 不可写，原因点明**第几条**不是对象。

        ⚠️ 顶部**不能**是裸列表：`{"jds": …}` 这一层由浅读管，裸列表报的是"结构不对"
        （另一档，见对照组）。
        """
        for blob, pos in (('{"version": 1, "jds": [1, 2, 3]}', 1),
                          ('{"version": 1, "jds": [{"id": "a"}, 1]}', 2)):
            with self.subTest(blob=blob):
                self._write(store.p_path("jds.json"), blob)
                ok, err = store.jds_safe_to_write()
                self.assertFalse(ok, "元素坏却仍回「可写」：%r" % ((ok, err),))
                self.assertIn("第 %d 条不是对象" % pos, err)
                self.assertIn("jds.json", err)

    def test_knowledge_write_criterion_asks_element_shape(self):
        for blob, pos in (('{"nodes": [1, 2]}', 1),
                          ('{"version": 1, "nodes": [{"id": "a"}, 1]}', 2)):
            with self.subTest(blob=blob):
                self._write(store.knowledge_path(), blob)
                ok, err = store.knowledge_safe_to_write()
                self.assertFalse(ok, "元素坏却仍回「可写」：%r" % ((ok, err),))
                self.assertIn("第 %d 条不是对象" % pos, err)
                self.assertIn("knowledge.json", err)

    def test_the_control_group_is_still_writable(self):
        """对照组：不存在 / 正常 / 合法空容器 / 0 字节 ⇒ 照旧可写；**容器**坏 ⇒ 仍不可写。"""
        self.assertEqual(store.jds_safe_to_write(), (True, ""), "文件不存在 = 合法的「第一次」")
        self.assertEqual(store.knowledge_safe_to_write(), (True, ""))
        store.write_json(store.p_path("jds.json"), {"version": 1, "seq": 0, "jds": [{"id": "a"}]})
        store.write_json(store.knowledge_path(), {"nodes": [{"id": "a"}]})
        self.assertEqual(store.jds_safe_to_write(), (True, ""), "正常 JD 库必须照旧可写")
        self.assertEqual(store.knowledge_safe_to_write(), (True, ""), "正常知识库必须照旧可写")
        self._write(store.p_path("jds.json"), '{"version": 1, "seq": 0, "jds": []}')
        self._write(store.knowledge_path(), '{"nodes": []}')
        self.assertEqual(store.jds_safe_to_write(), (True, ""), "合法的空容器必须照旧可写")
        self.assertEqual(store.knowledge_safe_to_write(), (True, ""))
        self._write(store.p_path("jds.json"), b"")
        self._write(store.knowledge_path(), b"")
        self.assertEqual(store.jds_safe_to_write(), (True, ""), "0 字节 = 合法的「第一次」")
        self.assertEqual(store.knowledge_safe_to_write(), (True, ""))
        # 反向对照：深读不许把**容器**坏的那一档放过去（别写成"只深不浅"）
        self._write(store.p_path("jds.json"), '["不是对象"]')
        self._write(store.knowledge_path(), '{"nodes": 1}')
        self.assertFalse(store.jds_safe_to_write()[0], "容器坏仍必须不可写")
        self.assertFalse(store.knowledge_safe_to_write()[0], "容器坏仍必须不可写")


class UpdateJobsTransactionTest(IsolatedCase):
    """P3-8 / 计划 2.4：`store.update_jobs` —— 台账的**复合**读-改-写必须是一个临界区。

    要修的形态（`jobdup._merge_jobs_locked` / `jddelete._purge_locked` / `jdid.rename_jd`
    三处都是它）：`load_jobs()` → 改 → `save_jobs()` 跨了**两次** store 调用，而 `_JOBS_LOCK`
    只保证**单次**调用原子 ⇒ 两个写者各自读、各自整份写回，后写的把先写的**静默顶掉**。

    ⚠️ 这个夹具怎么制造"必丢"（实测踩过一版**假绿**的夹具，写法必须照这里）：

    用一个**事件**而不是 Barrier 来定序 —— A（慢改者）先读到旧台账、把 `a_loaded` 置位、
    在"改"这一步里停 SLOW_SECONDS；B 等到 `a_loaded` 才开始读。于是：

      * 老骨架（锁外 load → 改 → save）：A 读完之后 B 才读，B 读到的是**同一份旧台账**；
        B 先写完，A 再把自己的旧快照整份写回 ⇒ **B 的改动被顶掉**（这就是要抓的那一次丢更新）。
      * `update_jobs`：A 的那段"改"是**持着 `_JOBS_LOCK`** 跑的 ⇒ B 卡在锁上，等 A 写完之后
        才**读到 A 的结果**、再改自己的那条 ⇒ 两条都在。

    ⚠️ 两个坑：
    1. **不许用 Barrier 在调用前定序**：`save_jobs` 自己整段持 `_JOBS_LOCK`，被 Barrier 唤醒的那个
       线程去 `load_jobs` 时会**卡在这把锁上**，于是两个 load 被强行串开 ⇒ 老骨架也"不丢"
       ⇒ 上一条对照直接**假绿**（实测：`lost == []`）。
    2. **更不许把会合点放进临界区内部**（例如放进 `fn` 里等另一个线程到达）：`update_jobs` 正常
       工作时另一个线程根本进不来 ⇒ 会合永远等不到 ⇒ 30 秒后超时或死等。所以这里的 `fn`
       只**单向**停一下（自己睡 `SLOW_SECONDS`），不等任何人。
    """

    ROUNDS = 3
    SLOW_SECONDS = 0.05

    def _run_race(self, rmw):
        """跑 ROUNDS 轮"A 先读、B 后读、A 后写"的竞争；`rmw(who, slow, a_loaded)` 是 RMW 骨架。"""
        errors = {}
        a_loaded = threading.Event()

        def worker(who, slow):
            try:
                if who == "B":
                    # A 一定已经**读到**那份旧台账了，B 从这一刻才出发
                    if not a_loaded.wait(20):
                        raise AssertionError("A 没能先读到台账（夹具没构成竞争）")
                rmw(who, slow, a_loaded)
            except BaseException as e:            # noqa: BLE001 - 线程里的异常必须能被断言看到
                errors[threading.current_thread().name] = e

        ts = [threading.Thread(target=worker, args=(who, slow), name="editor-%s" % who)
              for who, slow in (("A", True), ("B", False))]
        for t in ts:
            t.start()
        for t in ts:
            t.join(60)
        self.assertFalse(any(t.is_alive() for t in ts), "有线程没跑完（死等）")
        self.assertEqual(errors, {}, "线程里抛了异常：%r" % errors)

    def _edit_in_memory(self, jobs, who, round_no):
        for j in jobs:
            if isinstance(j, dict) and j.get("id") == who:
                j["touched_%d" % round_no] = who

    def test_two_threads_editing_different_records_keep_both(self):
        """两个线程各改**不同**的一条 ⇒ 两条改动都必须留下来（老骨架必丢一条）。"""
        store.save_jobs([{"id": "A", "score": 60.0}, {"id": "B", "score": 60.0}])

        def via_update_jobs(who, slow, a_loaded):
            for round_no in range(self.ROUNDS):
                def edit(jobs, who=who, round_no=round_no):
                    if slow:                      # 慢改者：拉宽"读完之后、写回之前"那段
                        a_loaded.set()
                        time.sleep(self.SLOW_SECONDS)
                    self._edit_in_memory(jobs, who, round_no)
                    return jobs, True

                store.update_jobs(edit)

        self._run_race(via_update_jobs)

        rows = {j.get("id"): j for j in store.load_jobs()}
        self.assertEqual(sorted(rows), ["A", "B"], "台账里的记录被顶掉了：%r" % sorted(rows))
        for round_no in range(self.ROUNDS):
            for who in ("A", "B"):
                self.assertEqual(rows[who].get("touched_%d" % round_no), who,
                                 "第 %d 轮里 %s 的改动被另一个线程的整份覆盖顶掉了（丢更新）：%r"
                                 % (round_no + 1, who, rows[who]))

    def test_the_fixture_really_catches_a_lost_update(self):
        """自证夹具：**同一个**竞争夹具下，老骨架（锁外 load → 改 → save）必丢一条。

        这条是**对照/证据**（它断言的是"丢了一条"）：没有它，上面那条绿只能说明
        "夹具根本没制造出竞争"，说明不了 `update_jobs` 真的挡掉了丢更新。
        它同时钉住了"把 `update_jobs` 改回锁外 load/save"这个变异确实会被抓到。
        """
        store.save_jobs([{"id": "A", "score": 60.0}, {"id": "B", "score": 60.0}])

        def old_style(who, slow, a_loaded):
            for round_no in range(self.ROUNDS):
                jobs = store.load_jobs()          # ← 锁只护这一次 load
                if slow:
                    a_loaded.set()
                    time.sleep(self.SLOW_SECONDS)
                self._edit_in_memory(jobs, who, round_no)
                store.save_jobs(jobs)             # ← 锁只护这一次 save（中间的改动裸奔）

        self._run_race(old_style)

        rows = {j.get("id"): j for j in store.load_jobs()}
        lost = [who for who in ("A", "B")
                if any(rows.get(who, {}).get("touched_%d" % r) != who
                       for r in range(self.ROUNDS))]
        self.assertTrue(lost,
                        "夹具没能制造出丢更新：两条改动都留下来了 ⇒ 这条对照失去意义，"
                        "上面那条 `update_jobs` 的绿也就没有说服力")

    def test_a_raising_fn_writes_not_a_single_byte(self):
        """`fn` 抛异常 ⇒ **一个字节都不写盘**（连 `.bak-` 都不许多一份）。"""
        store.save_jobs([{"id": "A", "score": 60.0}])
        path = store.jobs_path()
        raw = store.read_bytes(path)
        folder = os.path.dirname(path)
        before = sorted(os.listdir(folder))

        def boom(jobs):
            jobs[0]["score"] = 0.0                    # 就地改：这种中间态绝不许落盘
            jobs.append({"id": "半截分析"})
            raise ValueError("注入的失败")

        with self.assertRaises(ValueError):
            store.update_jobs(boom)

        with open(path, "rb") as f:
            self.assertEqual(f.read(), raw, "fn 抛异常时台账必须逐字节未变")
        self.assertEqual(sorted(os.listdir(folder)), before,
                         "fn 抛异常时连 .bak- 都不该多出来（留档必须发生在「确定要写」之后）")

    def test_it_returns_what_fn_decides_and_none_means_no_write(self):
        """返回值就是 `fn` 给的那个东西；`fn` 用 `None` 说"没改动" ⇒ 一个字节都不写。"""
        store.save_jobs([{"id": "A", "score": 60.0}])
        raw = store.read_bytes(store.jobs_path())

        self.assertEqual(store.update_jobs(lambda jobs: (None, "没改动")), "没改动")
        self.assertEqual(store.read_bytes(store.jobs_path()), raw,
                         "fn 说没改动，就不该写盘（也不该留 .bak-）")

        def bump(jobs):
            jobs[0]["score"] = 99.0
            return jobs, len(jobs)

        self.assertEqual(store.update_jobs(bump), 1, "返回值必须是 fn 决定的那一个")
        self.assertEqual(store.load_jobs()[0]["score"], 99.0)
        self.assertEqual(store.update_jobs(lambda jobs: (jobs, None)), None,
                         "fn 决定返回 None 时也原样返回 None")

    def test_the_fn_sees_the_freshest_ledger_not_a_stale_snapshot(self):
        """`fn` 拿到的必须是**刚读出来**的那份：调用方读快照 → 中途别人写了 → 这里要能看见。

        这一条正是 `jobdup` / `jddelete` / `jdid` 那三处的病根（它们把**旧快照**整份写回）。
        """
        store.save_jobs([{"id": "A"}])
        stale = store.load_jobs()                     # 调用方手里的快照（马上就会过期）
        store.upsert_job({"id": "B"})                 # 另一个写者插进来

        seen = {}

        def look(jobs):
            seen["ids"] = [j.get("id") for j in jobs]
            return None, None

        store.update_jobs(look)
        self.assertEqual(sorted(seen["ids"]), ["A", "B"],
                         "fn 看到的是旧快照（%r），不是盘上最新的那份" % (stale,))

    def test_a_gbk_ledger_refuses_the_transaction_and_keeps_every_byte(self):
        """坏台账（有内容却读不出来）⇒ 拒绝整次事务，逐字节不变、连 `.bak-` 都不多。"""
        path = store.jobs_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        raw = json.dumps([{"id": "字节跳动", "score": 60.0}],
                         ensure_ascii=False).encode("gbk")
        with open(path, "wb") as f:
            f.write(raw)
        folder = os.path.dirname(path)
        before = sorted(os.listdir(folder))

        with self.assertRaises(RuntimeError) as cm:
            store.update_jobs(lambda jobs: ([{"id": "整份顶掉"}], 1))
        self.assertIn("jobs.json", str(cm.exception))

        with open(path, "rb") as f:
            self.assertEqual(f.read(), raw, "拒绝写盘时台账必须逐字节未变")
        self.assertEqual(sorted(os.listdir(folder)), before, "守卫先于留档")

    def test_store_never_takes_an_outer_lock(self):
        """锁序的**反向证据**（AST）：store 绝不引用上层那几把锁。

        为什么这条是本节最关键的证据：`update_jobs` 会在**持 `_JD_LOCK` / `_MERGE_LOCK` /
        `_KB_LOCK` 的调用方内部**被调用（`jdid.rename_jd` / `jobdup` / `jddelete`）——
        那与它们**现在**在同一行调 `store.load_jobs` / `store.save_jobs` 取的是**同一把**
        `_JOBS_LOCK`，所以锁图**逐边不变**、不可能成环。

        这个结论的支柱只有一条：store 是**叶子模块**（只 import 标准库，从不回手去抢上层锁）。
        它要是哪天 import 了 `jdid`/`kb`/`jobdup` 或直接引用那几把锁，"上层 → store"的
        单向性就断了，环就有了 —— 所以必须用 AST 钉住（说明文字里提到那些锁名是应该的，
        注释/文档字符串不算代码，AST 看不见它们）。
        """
        src = store.read_text(os.path.join(store.ROOT, "store.py"))
        self.assertTrue(src)
        tree = ast.parse(src)

        used = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        used |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        for name in ("_JD_LOCK", "_KB_LOCK", "_MERGE_LOCK", "_STORE_PATCH_LOCK",
                     "_VIEW_LOCAL", "_LOCK"):
            self.assertNotIn(name, used,
                             "store.py 里真的去拿/碰了 `%s`：叶子模块的单向性断了" % name)

        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names.add((node.module or "").split(".")[0])
        self.assertTrue(names, "一个 import 都没找到？解析方式不对")
        self.assertEqual(names - {"io", "json", "os", "re", "tempfile", "threading",
                                 "datetime"}, set(),
                         "store 只能 import 标准库（多一个项目模块就多一条回抢上层锁的通道）")


class AsIntTest(unittest.TestCase):
    """批 5：`store.as_int` 是**唯一**一份容错取整数，**口径**由调用方显式传。

    这张表就是「逐字节等价」的判据：每一格都对着**统一前**每个模块那份副本的行为
    （`kb` / `resumelib` / `jddelete` 是裸 `int(value)` ⇒ 头两列；`jdid` 先拒收 bool ⇒ 第三列）。
    改任何一格都说明某处的口径被动过 —— 那正是这批"行为不变"的整理**唯一**能出的错。
    """

    # (入参, bool_ok=True, bool_ok=False)  —— 后两列 = 统一前两种副本各自的返回值
    CASES = [
        (None, 0, 0),
        ("", 0, 0),
        (" ", 0, 0),
        (True, 1, 0),                    # ← 这里就是两种口径**唯一**分岔的地方
        (False, 0, 0),                   # 两种口径在 False 上恰好一致（都是 0）
        (0, 0, 0),
        (-1, -1, -1),
        ("3", 3, 3),
        (" 3 ", 3, 3),
        ("3.7", 0, 0),                   # int("3.7") 抛 ValueError ⇒ default（不是 3！）
        ("abc", 0, 0),
        (3.7, 3, 3),                     # float 截断，不四舍五入
        (-3.7, -3, -3),
        ([], 0, 0),
        ([1], 0, 0),
        ({}, 0, 0),
        ({"a": 1}, 0, 0),
        ("0x10", 0, 0),
    ]

    def test_the_table_is_identical_under_both_historical_kinds(self):
        for raw, yes, no in self.CASES:
            with self.subTest(raw=raw):
                self.assertEqual(store.as_int(raw, 0, bool_ok=True), yes,
                                 "bool_ok=True 口径（老 kb/resumelib/jddelete）变了：%r" % (raw,))
                self.assertEqual(store.as_int(raw, 0, bool_ok=False), no,
                                 "bool_ok=False 口径（老 jdid）变了：%r" % (raw,))

    def test_the_default_is_returned_as_is(self):
        """`default` 原样返回（老副本是裸 `return default`，不做二次转换）。"""
        self.assertEqual(store.as_int("abc", -1), -1)
        self.assertEqual(store.as_int(None, 7), 7)
        self.assertIsNone(store.as_int([], None))
        self.assertEqual(store.as_int("3", -1), 3, "取得到就不许拿 default 顶")

    def test_only_type_and_value_errors_are_swallowed(self):
        """只兜 `TypeError` / `ValueError` —— `OverflowError` 照旧漏出去（老副本如此）。

        吞掉它等于把"数据离谱到没法解释"改成一堆 0，那比晚一点炸更糟。
        """
        self.assertRaises(OverflowError, store.as_int, float("inf"))
        self.assertRaises(OverflowError, store.as_int, float("-inf"))
        # 对照：这两类被兜住（不抛）
        self.assertEqual(store.as_int([1]), 0)          # int([]) 是 TypeError
        self.assertEqual(store.as_int("abc"), 0)        # int("abc") 是 ValueError


class AsTextTest(unittest.TestCase):
    """批 5：`store.as_text` 是**唯一**一份容错取字符串，两种契约用 `empty=` 表达。

    两种契约都是**真的**，谁也不是另一个的"顺手版本"：
    `empty=None`（老 jdid / jdfacts：调用方 `or` 兜底）与
    `empty=""`（老 jddelete / jdrefs / resumelib：值要直接进 `==` 与字符串模板，
    `None` 在 `str()` 里会变成 `"None"` —— 那是句假话）。
    """

    # (入参, empty=None, empty="")
    CASES = [
        (None, None, ""),
        ("", None, ""),
        (" ", None, ""),
        ("\t\r\n", None, ""),
        ("\u3000", None, ""),            # 全角空格也只算空白
        ("abc", "abc", "abc"),
        (" abc ", "abc", "abc"),
        (" abc\n第二行 ", "abc\n第二行", "abc\n第二行"),   # 只去首尾，不动中间
        ("\u3000 甲 ", "甲", "甲"),
        (5, None, ""),
        (0, None, ""),
        (True, None, ""),
        ([], None, ""),
        (["a"], None, ""),
        ({}, None, ""),
        (b"abc", None, ""),              # bytes 不是 str
    ]

    def test_the_table_is_identical_under_both_contracts(self):
        for raw, none_like, empty_like in self.CASES:
            with self.subTest(raw=raw):
                self.assertEqual(store.as_text(raw, empty=None), none_like,
                                 "empty=None 契约（老 jdid/jdfacts）变了：%r" % (raw,))
                self.assertEqual(store.as_text(raw, empty=""), empty_like,
                                 "empty=\"\" 契约（老 jddelete/jdrefs/resumelib）变了：%r" % (raw,))

    def test_the_two_contracts_only_differ_in_the_missing_cases(self):
        """契约不同**只**在"缺失"那一类上 —— 有内容时两列必须完全相同。"""
        for raw, none_like, empty_like in self.CASES:
            if none_like is None and empty_like == "":
                continue
            self.assertEqual(none_like, empty_like, "有内容的入参两种契约却不同：%r" % (raw,))


class SingleImplementationTest(unittest.TestCase):
    """批 5 的**结构钉子**：取值小工具实现只剩 store 这一份，口径在调用点显式传。

    为什么必须有这条（而不是只钉行为）：行为用例挡不住"在某个模块里又长出一份副本"
    —— 副本刚长出来时行为一模一样，等它漂移了才发现。这里用 AST 钉两件事：
    ① 模块里**没有**自己的 `_as_int` / `_text`；② 每一处 `store.as_int` / `store.as_text`
    调用都**显式**带口径关键字，且带的是**这个模块历史上那一把尺子**。
    """

    # 各模块历史上的口径（跨模块引用也算在本模块头上：kbreuse 用 kb 的、身份迁移用 jdid 的）
    # `interview.py` / `简历迁移.py` 是批 5 收口时**漏在外面**的两份（各自一份裸 `int()` 副本），
    # 第四轮补进来 —— 口径同样由调用点显式传，所以也必须出现在这里，否则"再长一份副本"没人看得见。
    AS_INT_KIND = {"kb.py": True, "kbreuse.py": True, "resumelib.py": True,
                   "jdid.py": False, "身份迁移.py": False,
                   "interview.py": True, "简历迁移.py": True}
    AS_TEXT_EMPTY = {"jdid.py": None, "jdfacts.py": None,
                     "jddelete.py": "", "jdrefs.py": "", "resumelib.py": ""}

    @staticmethod
    def _tree(name):
        return ast.parse(store.read_text(os.path.join(store.ROOT, name)))

    def test_no_module_keeps_its_own_copy(self):
        for name in sorted(set(self.AS_INT_KIND) | set(self.AS_TEXT_EMPTY)):
            with self.subTest(module=name):
                defs = {n.name for n in ast.walk(self._tree(name))
                        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
                self.assertNotIn("_as_int", defs, "%s 里又长出了一份 _as_int 副本" % name)
                self.assertNotIn("_text", defs, "%s 里又长出了一份 _text 副本" % name)

    def _calls(self, name, attr):
        for node in ast.walk(self._tree(name)):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == attr and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "store"):
                yield node

    def test_every_call_site_passes_its_own_kind_explicitly(self):
        for name, want in sorted(self.AS_INT_KIND.items()):
            calls = list(self._calls(name, "as_int"))
            with self.subTest(module=name):
                self.assertTrue(calls, "%s 里一个 store.as_int 都没有？解析方式不对" % name)
            for call in calls:
                kw = {k.arg: k.value for k in call.keywords}
                self.assertIn("bool_ok", kw, "%s:%d 调 store.as_int 没显式传口径"
                              % (name, call.lineno))
                self.assertIsInstance(kw["bool_ok"], ast.Constant)
                self.assertIs(kw["bool_ok"].value, want,
                              "%s:%d 的 bool_ok 口径不是它历史上那把尺子"
                              % (name, call.lineno))
        for name, want in sorted(self.AS_TEXT_EMPTY.items()):
            calls = list(self._calls(name, "as_text"))
            with self.subTest(module=name):
                self.assertTrue(calls, "%s 里一个 store.as_text 都没有？解析方式不对" % name)
            for call in calls:
                kw = {k.arg: k.value for k in call.keywords}
                self.assertIn("empty", kw, "%s:%d 调 store.as_text 没显式传契约"
                              % (name, call.lineno))
                self.assertIsInstance(kw["empty"], ast.Constant)
                self.assertEqual(kw["empty"].value, want,
                                 "%s:%d 的 empty 契约不是它历史上那一份"
                                 % (name, call.lineno))


class AtomicWriteStreamTest(IsolatedCase):
    """④c：`save_stream` 里那套"流式 mkstemp + os.replace"抽成 `_atomic_write_stream`。

    入口只有一份，对外契约（返回字节数 / `ValueError` / `OSError` / 超限那块不写 /
    失败不删旧文件）**一字不变** —— 那几条既有行为由 `SaveStreamAtomicTest` 钉着，
    这里补的是"抽出来的这个入口自己"的契约。
    """

    def _leftover_temps(self, room):
        return sorted(n for n in os.listdir(room) if n.startswith(".tmp-"))

    def _path(self, name="blob.bin"):
        return os.path.join(self.tmp, "stream", name)

    def test_it_returns_the_byte_count_and_leaves_no_temp_behind(self):
        path = self._path()
        payload = "内容".encode("utf-8") + b"\x00\xff"
        n = store._atomic_write_stream(path, io.BytesIO(payload), 10 * 1024)
        self.assertEqual(n, len(payload))
        self.assertEqual(store.read_bytes(path), payload)
        self.assertEqual(self._leftover_temps(os.path.dirname(path)), [])

    def test_the_older_file_survives_an_oversize_failure(self):
        """超限 ⇒ `ValueError`，**旧文件一个字节没动**，超限那一块与 `.tmp-` 都不留。"""
        path = self._path()
        store.write_bytes(path, b"OLD")
        with self.assertRaises(ValueError) as cm:
            store._atomic_write_stream(path, io.BytesIO(b"x" * 5000), 1000)
        self.assertIn("文件超过上限 %d MB" % (1000 // 1024 // 1024), str(cm.exception))
        self.assertEqual(store.read_bytes(path), b"OLD")
        self.assertEqual(self._leftover_temps(os.path.dirname(path)), [])

    def test_a_read_error_keeps_the_older_file_and_leaves_no_temp(self):
        """读源失败（`OSError`）照旧漏出去：目标不动、临时文件删掉（不多一种失败形态）。"""

        class Boom(object):
            def read(self, n):
                raise OSError("读源失败（注入）")

        path = self._path()
        store.write_bytes(path, b"OLD")
        with self.assertRaises(OSError):
            store._atomic_write_stream(path, Boom(), None)
        self.assertEqual(store.read_bytes(path), b"OLD")
        self.assertEqual(self._leftover_temps(os.path.dirname(path)), [])

    def test_no_limit_means_write_everything(self):
        path = self._path()
        n = store._atomic_write_stream(path, io.BytesIO(b"y" * 700000), None)
        self.assertEqual(n, 700000)
        self.assertEqual(os.path.getsize(path), 700000)

    def test_save_stream_really_goes_through_that_one_entry(self):
        """`save_stream` 自己**不再**有第二套 mkstemp —— 它必须调这条入口。"""
        with mock.patch.object(store, "_atomic_write_stream",
                               wraps=store._atomic_write_stream) as spy:
            n = store.save_stream("iv1", "audio.wav", io.BytesIO(b"x" * 10), max_bytes=1000)
        self.assertEqual(n, 10)
        self.assertEqual(spy.call_count, 1, "save_stream 没走 _atomic_write_stream")


if __name__ == "__main__":
    unittest.main()
