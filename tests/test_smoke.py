# -*- coding: utf-8 -*-
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class SmokeTest(unittest.TestCase):
    def test_root_has_app(self):
        self.assertTrue(os.path.exists(os.path.join(ROOT, "app.py")))

    def test_web_assets_exist(self):
        for f in ("web/index.html", "web/report_template.html", "web/demo_data.json"):
            self.assertTrue(os.path.exists(os.path.join(ROOT, f)), f)


if __name__ == "__main__":
    unittest.main()
