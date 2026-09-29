# -*- coding: utf-8 -*-
import unittest

import llm


class ParseJsonTest(unittest.TestCase):
    def test_plain_json(self):
        self.assertEqual(llm.parse_json_reply('{"a":1}'), {"a": 1})

    def test_fenced_json(self):
        self.assertEqual(llm.parse_json_reply('```json\n{"a":1}\n```'), {"a": 1})

    def test_json_with_trailing_noise(self):
        self.assertEqual(llm.parse_json_reply('{"a":1}\n\n以上就是分析结果'), {"a": 1})

    def test_json_with_leading_text(self):
        self.assertEqual(llm.parse_json_reply('好的，这是结果：\n{"a":1}'), {"a": 1})

    def test_invalid_raises(self):
        with self.assertRaises(RuntimeError):
            llm.parse_json_reply("完全不是 JSON")


class TemperatureTest(unittest.TestCase):
    def test_build_payload_omits_temperature_when_none(self):
        p = llm.build_payload("m", [{"role": "user", "content": "x"}], None, False)
        self.assertNotIn("temperature", p)

    def test_build_payload_includes_temperature(self):
        p = llm.build_payload("m", [], 1, False)
        self.assertEqual(p["temperature"], 1)

    def test_build_payload_json_mode(self):
        p = llm.build_payload("m", [], None, True)
        self.assertEqual(p["response_format"], {"type": "json_object"})


if __name__ == "__main__":
    unittest.main()
