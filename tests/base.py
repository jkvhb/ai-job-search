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
        # 用 addCleanup 而不是 tearDown：set_current_profile() 是真实磁盘 I/O，
        # 一旦它抛错，unittest 不会调用 tearDown，全局路径就永久停在临时目录上。
        # addCleanup 在 setUp 失败时仍会执行，且后进先出——所以必须在改值**之前**注册还原。
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(setattr, store, "DATA_ROOT", store.DATA_ROOT)
        self.addCleanup(setattr, log, "LOG_FILE", log.LOG_FILE)
        store.DATA_ROOT = self.tmp
        store.set_current_profile("default")
        log.LOG_FILE = os.path.join(store.profile_dir(), "logs", "events.jsonl")
