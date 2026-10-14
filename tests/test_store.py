# -*- coding: utf-8 -*-
import glob
import json
import os
import unittest
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
    """`(可写, 原因)` 的六种情形 —— 「读不出来 ≠ 空」这条规则**唯一**的写盘判据。"""

    def _write(self, blob):
        with open(store.knowledge_path(), "wb") as f:
            f.write(blob)

    def test_a_missing_file_is_safe(self):
        self.assertEqual(store.knowledge_safe_to_write(), (True, ""))

    def test_a_valid_file_is_safe(self):
        store.save_knowledge({"nodes": [{"id": "a", "term": "A"}]})
        self.assertEqual(store.knowledge_safe_to_write(), (True, ""))

    def test_an_empty_or_whitespace_only_file_is_safe(self):
        """没有**字节**可丢 → 可以写（0 字节 / 纯空白**不是**损坏，别为它造垃圾备份）。"""
        for blob in (b"", b"   \n  ", b"\r\n"):
            with self.subTest(blob=blob):
                self._write(blob)
                ok, err = store.knowledge_safe_to_write()
                self.assertTrue(ok, "空文件不该被当成损坏：%r" % (blob,))
                self.assertEqual(err, "")

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


if __name__ == "__main__":
    unittest.main()
