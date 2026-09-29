# -*- coding: utf-8 -*-
import os
import shutil
import tempfile
import unittest

import store


class StorePathTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old = store.DATA_ROOT
        store.DATA_ROOT = self.tmp

    def tearDown(self):
        store.DATA_ROOT = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

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


if __name__ == "__main__":
    unittest.main()
