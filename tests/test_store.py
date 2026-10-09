# -*- coding: utf-8 -*-
import glob
import os
import unittest

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


if __name__ == "__main__":
    unittest.main()
