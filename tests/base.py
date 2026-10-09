# -*- coding: utf-8 -*-
"""测试共享脚手架。

为什么要隔离：store / log 的模块级路径默认指向仓库里**真实的** data/ 目录，
测试若直接跑会把日志追加进用户真实数据。凡是要碰 store/log 的测试都必须继承 IsolatedCase。
"""
import os
import shutil
import tempfile
import unittest

import log
import store


class IsolatedCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old_data = store.DATA_ROOT
        self._old_logfile = log.LOG_FILE
        store.DATA_ROOT = self.tmp
        store.set_current_profile("default")
        log.LOG_FILE = os.path.join(store.profile_dir(), "logs", "events.jsonl")

    def tearDown(self):
        store.DATA_ROOT = self._old_data
        log.LOG_FILE = self._old_logfile
        shutil.rmtree(self.tmp, ignore_errors=True)
