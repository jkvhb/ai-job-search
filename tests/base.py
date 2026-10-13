# -*- coding: utf-8 -*-
"""测试共享脚手架。

为什么要隔离：store / log 的模块级路径默认指向仓库里**真实的** data/ 目录，
测试若直接跑会把日志追加进用户真实数据。凡是要碰 store/log 的测试都必须继承 IsolatedCase。

**为什么光隔离还不够（第二道防线，本次新增）**：模块级路径是**全局可变**的，
把路径还原成真实值之后，如果还有后台线程活着，它照样写进用户真实文件 —— 本项目已经
真实发生过两次（app 的外部调用、interview 的 daemon 流水线线程；后者往真实
events.jsonl 追加过一行）。所以拆隔离必须是这个顺序：

  1. **先**有界地等已知后台线程排空（短间隔轮询 + 上限，不许长时间固定 sleep）；
  2. 等不到 → **响亮失败**，并且**保持隔离**（不还原路径、不删临时目录）。

宁可留下一个「隔离没拆干净」的红，也绝不让线程写到用户真实文件里。
（代价说明：daemon 线程杀不掉，失败后它仍会继续跑；但它的 DATA_ROOT/LOG_FILE 停在临时目录，
 最坏也只是写到临时目录，不会碰真实数据。）
"""
import os
import shutil
import tempfile
import time
import unittest

import log
import store

# 有界等待的粒度与上限：20ms 查一次、最多 2 秒。
# **不许用长时间的固定 sleep 硬等** —— 那既慢，又把「偶发」变成「更慢的偶发」。
DRAIN_POLL_SECONDS = 0.02
DRAIN_TIMEOUT_SECONDS = 2.0


def live_background_threads():
    """还活着的已知后台线程：[(说明文本, Thread), ...]；没有就是空列表。

    只认**登记表**（`interview._INTERVIEW_THREADS`，真实形态是 `{interview_id: Thread}`）：
    app.py 的上传/重试路由都经由 `interview.start` 起线程，所以它就是「会写 store/log 的
    后台执行体」的权威名单。不盲目扫 `threading.enumerate()` —— 那会把测试自己起的
    服务线程/工作线程也算进来，那是另一件事、不该在这里判红。
    """
    import interview          # 延迟 import：避免本模块被 import 时就拉起整条 app 依赖链

    with interview._THREADS_LOCK:
        items = list(interview._INTERVIEW_THREADS.items())
    found = []
    for interview_id, t in items:
        if t is None or not t.is_alive():
            continue
        state = ""
        try:
            st = store.load_interview_json(interview_id, "status.json", {})
            state = "，落盘状态=%s" % ((st or {}).get("state") if isinstance(st, dict) else None)
        except Exception:
            state = ""
        found.append(("interview._INTERVIEW_THREADS[%s] thread=%s(ident=%s)%s"
                      % (interview_id, t.name, t.ident, state), t))
    return found


def drain_background_threads(timeout=DRAIN_TIMEOUT_SECONDS, poll=DRAIN_POLL_SECONDS):
    """有界等待后台线程排空。返回超时那一刻**还活着**的 [(说明文本, Thread), ...]。"""
    deadline = time.monotonic() + timeout
    while True:
        live = live_background_threads()
        if not live or time.monotonic() >= deadline:
            return live
        time.sleep(poll)


class IsolatedCase(unittest.TestCase):
    def setUp(self):
        # 用 addCleanup 而不是 tearDown：set_current_profile() 是真实磁盘 I/O，
        # 一旦它抛错，unittest 不会调用 tearDown，全局路径就永久停在临时目录上。
        # addCleanup 在 setUp 失败时仍会执行，且后进先出——所以必须在改值**之前**注册还原。
        self.tmp = tempfile.mkdtemp()
        self._real_data_root = store.DATA_ROOT
        self._real_log_file = log.LOG_FILE
        # 「排空线程 → 还原路径 → 删临时目录」必须是**一个** cleanup：
        # 拆成多个的话，前一个（比如排空线程）失败并不会阻止后面那个还原真实路径 ——
        # 那正是真实污染发生的地方。
        self.addCleanup(self._teardown_isolation)
        store.DATA_ROOT = self.tmp
        store.set_current_profile("default")
        log.LOG_FILE = os.path.join(store.profile_dir(), "logs", "events.jsonl")

    def _teardown_isolation(self):
        live = drain_background_threads()
        if live:
            # 响亮失败，但**先别还原**：还原就是给还在跑的线程指路，让它写进用户真实文件。
            # 临时目录也不删：线程还在往里写，删了只会让它去 os.makedirs(真实目录)。
            self.fail("隔离没拆干净：还原数据根/日志路径之前仍有 %d 个后台线程活着"
                      "（等了 %.1fs，每 %.0fms 查一次）\n"
                      "  - %s\n"
                      "临时数据根：%s（**故意保留**，便于事后看线程写到了哪）\n"
                      "**store.DATA_ROOT / log.LOG_FILE 未还原**：宁可留下这条「隔离没拆干净」的红，"
                      "也绝不让线程写到用户真实文件里。\n"
                      "修法：用例要用 join()/Event 等线程真的结束，不要用固定 sleep 赌它跑完。"
                      % (len(live), DRAIN_TIMEOUT_SECONDS, DRAIN_POLL_SECONDS * 1000,
                         "\n  - ".join(label for label, _ in live), self.tmp))
        store.DATA_ROOT = self._real_data_root
        log.LOG_FILE = self._real_log_file
        shutil.rmtree(self.tmp, ignore_errors=True)
