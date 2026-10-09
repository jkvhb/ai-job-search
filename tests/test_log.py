# -*- coding: utf-8 -*-
import json
import unittest

import log

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


if __name__ == "__main__":
    unittest.main()
