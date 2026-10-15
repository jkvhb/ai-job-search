# -*- coding: utf-8 -*-
import glob
import importlib
import json
import os
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
            [{"id": "20261008_语音文本评测_72分", "score": 72.0, "resume": "周汉钦-母版"},
             {"id": "20261003_语音文本评测_75分", "score": 75.0, "resume": "周汉钦-母版"}],
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


if __name__ == "__main__":
    unittest.main()
