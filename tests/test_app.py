# -*- coding: utf-8 -*-
"""GET /reports/* 的回归测试。

这条路径此前**零覆盖**，但它是所有报告都要走的：报告一律用「当前模板 + 报告文件里的
REPORT_DATA 快照」动态重渲染（模板改进立刻作用到老报告），读不出快照时**原样兜底**返回
文件本身。拼装 + 多层兜底，必须逐条钉住。

沙箱化 handler 的做法照抄 tests/test_kb.py 的 KnowledgeRouteTest：
直接实例化 app.Handler、不监听端口、不接套接字（do_GET 抛异常会原样冒出来 = 真实世界里的
连接裸断）。DATA_ROOT / LOG_FILE 由 IsolatedCase 隔离，报告文件只写进隔离根的 reports/，
绝不碰用户真实 data/，也不起真实服务（否则会往用户真实日志追加）。
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import threading
import unittest
from unittest import mock
from urllib.parse import quote

import app
import interview
import jddelete
import jdfacts
import jdid
import kb
import kbreuse
import llm
import log
import outcome
import resumelib
import store

from tests import web_probe
from tests.base import IsolatedCase


# 「路径穿越没泄漏」这条断言的金丝雀：它躺在 reports/ 的**上一级**（profile 根）的 config.json 里，
# 一旦守卫失效就会原样出现在响应体里，测试必须能看见它不见了。
CANARY_KEY = "sk-canary-should-never-leak-0123456789"

# 只可能出现在**旧文件**里的前端标记：动态重渲染后必然消失，兜底原样发送时必然还在。
OLD_FRONTEND_MARKER = "old-only-frontend-marker"


class _SandboxedHandler(app.Handler):
    """与 tests/test_kb.py::_SandboxedHandler 同一套写法：不接套接字的 Handler 沙箱。

    比那里多收一项响应头（本文件要断 Content-Type）。
    """

    def __init__(self, path, method="GET", body=None):
        self.path = path
        self.command = method
        payload = b"" if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.rfile = io.BytesIO(payload)
        # Content-Length 必须与 payload 一致：_json_body() 按它读，不一致就会读空/读残
        self.headers = {"Content-Length": str(len(payload))}
        self.wfile = io.BytesIO()
        self.code = None
        self.headers_out = {}

    def send_response(self, code, message=None):
        self.code = code

    def send_header(self, key, value):
        self.headers_out[key] = value

    def end_headers(self):
        pass

    def raw(self):
        return self.wfile.getvalue()

    def text(self):
        return self.raw().decode("utf-8")

    def content_type(self):
        return self.headers_out.get("Content-Type", "")

    def json_body(self):
        return json.loads(self.text())


def _get(path):
    h = _SandboxedHandler(path)
    h.do_GET()
    return h


def _disk_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def _text_of(h):
    """响应体文本。

    落盘走的是 store.write_text（文本模式），Windows 上 \\n 会变成 \\r\\n；这里归一化回来，
    便于与内存里的字面量逐字比较。「与磁盘文件一模一样」另外用字节级断言钉住。
    """
    return h.raw().decode("utf-8").replace("\r\n", "\n")


def _extract_report_data(text):
    """按 app._render_report_page / kb.read_report_data 的**同一口径**抽 REPORT_DATA。

    刻意不另写正则：JSON 之后的 JS 代码里也有 `}`，正则会被截错。
    抽不到返回 None（调用方负责断言）。
    """
    i = text.find(kb.REPORT_MARK)
    if i < 0:
        return None
    try:
        data, _ = json.JSONDecoder().raw_decode(text, i + len(kb.REPORT_MARK))
    except ValueError:
        return None
    return data


def _report_data():
    """报告快照。键顺序刻意**不是**字母序：sort_keys 比较之外再钉一层「原样搬运」。

    note 里塞了 `</script>`：拼装时会被转义成 `<\\/script>`，解析回来必须一模一样。
    """
    return {
        "score": 88,
        "job_title": "语音文本评测",
        "company": "甲公司",
        "z_last_key": "键序不该被重排",
        "note": "含 </script> 与 </html> 的收尾片段",
        "items": [1, 2, {"嵌套": "中文"}],
    }


def _write_report(name, content):
    """把报告写进**隔离根**的 reports/ 目录（绝不碰用户真实 data/）"""
    store.ensure_profile()
    path = os.path.join(store.p_path("reports"), name)
    store.write_text(path, content)
    return path


def _old_report_html(payload):
    """旧格式报告：**含 REPORT_DATA 快照**，但前端代码是旧模板那一版的。

    落盘格式与 store.render_report / tests/test_kb.py::ImportReportsTest 同口径。
    """
    return ("<!DOCTYPE html>\n"
            "<html lang=\"zh-CN\"><head><meta charset=\"UTF-8\">"
            "<title>老报告标题 · 岗位匹配分析 · 88分</title></head>\n"
            "<body><h1>旧版报告骨架</h1>\n<script>\n"
            "%s%s;\n"
            "// %s：只有老文件里才有这段前端逻辑\n"
            "console.log(\"%s\");\n"
            "</script></body></html>\n"
            % (kb.REPORT_MARK, json.dumps(payload, ensure_ascii=False).replace("</", "<\\/"),
               OLD_FRONTEND_MARKER, OLD_FRONTEND_MARKER))


class ReportRouteTest(IsolatedCase):
    """GET /reports/* 的五条必经分支：动态重渲染 / 两种兜底 / 404 / 路径穿越。"""

    NAME = "20261008_语音文本评测_88分.html"

    def test_existing_report_is_rendered_with_current_template_and_file_snapshot(self):
        data = _report_data()
        content = _old_report_html(data)
        _write_report(self.NAME, content)
        # 前置反证：老文件里既没有当前模板的前端代码……（下面靠它证明「模板真的换上了」）
        self.assertNotIn("/api/report_state", content)
        self.assertNotIn("__REPORT_DATA__", content)

        h = _get("/reports/" + self.NAME)

        self.assertEqual(h.code, 200)
        self.assertTrue(h.content_type().startswith("text/html"),
                        "Content-Type 应为 text/html，实际 %r" % h.content_type())
        body = h.text()
        # ① 套用了**当前模板**的前端代码（老文件里没有这段）
        self.assertIn("/api/report_state", body)
        # 走的是动态拼装而不是兜底原样发送（老文件独有的标记必须消失）
        self.assertNotIn(OLD_FRONTEND_MARKER, body)
        # 模板占位符被填掉了，标题取的是文件里的 <title>
        self.assertNotIn("__REPORT_DATA__", body)
        self.assertNotIn("__TITLE__", body)
        self.assertIn("<title>老报告标题 · 岗位匹配分析 · 88分</title>", body)
        # ② 响应体里抽出的 REPORT_DATA 与文件里那份逐字一致
        in_file = _extract_report_data(content)
        self.assertIsNotNone(in_file)
        self.assertEqual(json.dumps(in_file, sort_keys=True), json.dumps(data, sort_keys=True))
        in_body = _extract_report_data(body)
        self.assertIsNotNone(in_body, "响应体里抽不出 REPORT_DATA —— 说明没走动态拼装")
        self.assertEqual(json.dumps(in_body, sort_keys=True), json.dumps(in_file, sort_keys=True))
        # sort_keys 看不见键序被重排，这里补一刀
        self.assertEqual(list(in_body.keys()), list(data.keys()))

    def test_report_without_snapshot_falls_back_to_the_raw_file(self):
        content = ("<!DOCTYPE html>\n<html><head><meta charset=\"UTF-8\">"
                   "<title>旧格式报告</title></head>\n"
                   "<body><p>没有 REPORT_DATA 快照的老报告</p></body></html>\n")
        path = _write_report(self.NAME, content)
        self.assertNotIn(kb.REPORT_MARK, content)    # 反证：这份文件里真的没有快照

        h = _get("/reports/" + self.NAME)

        self.assertEqual(h.code, 200)
        self.assertTrue(h.content_type().startswith("text/html"))
        # 走 _send_file：磁盘上的报告逐字节原样送出
        self.assertEqual(h.raw(), _disk_bytes(path))
        self.assertEqual(_text_of(h), content)
        self.assertNotIn("/api/report_state", _text_of(h))   # 绝不套当前模板

    def test_report_with_corrupt_snapshot_falls_back_instead_of_500(self):
        content = ("<!DOCTYPE html>\n<html><head><meta charset=\"UTF-8\">"
                   "<title>坏快照报告</title></head>\n"
                   "<body><script>\n"
                   + kb.REPORT_MARK + "{ \"job_title\": \"坏掉的报告\", ] 这不是合法 JSON }\n"
                   "</script></body></html>\n")
        path = _write_report(self.NAME, content)
        # 反证：这份快照真的抽不出来（不是靠「文件里没有标记」蒙过去的）
        self.assertIsNone(_extract_report_data(content))

        h = _get("/reports/" + self.NAME)

        self.assertEqual(h.code, 200)                # 绝不许 500
        self.assertTrue(h.content_type().startswith("text/html"))
        self.assertEqual(h.raw(), _disk_bytes(path)) # 原样兜底
        self.assertEqual(_text_of(h), content)
        self.assertIn("这不是合法 JSON", _text_of(h))

    def test_missing_report_returns_404(self):
        store.ensure_profile()
        self.assertEqual(os.listdir(store.p_path("reports")), [])   # 反证：目录里真的没有它

        h = _get("/reports/不存在的报告.html")

        self.assertEqual(h.code, 404)
        self.assertEqual(h.json_body().get("error"), "not found")

    def test_report_path_traversal_is_blocked_and_does_not_leak_config(self):
        # 金丝雀就位：api_key 真的躺在 reports/ 的上一级（profile 根）的 config.json 里。
        # 去掉 safe_name / unquote 那道守卫，`/reports/../config.json` 就会把它原样吐出来。
        store.ensure_profile()
        cfg_path = store.config_path()
        store.write_json(cfg_path, {"text_model": {"model": "canary", "api_key": CANARY_KEY}})
        self.assertIn(CANARY_KEY, store.read_text(cfg_path))         # 反证：金丝雀确实在盘上

        for path in ("/reports/../config.json",
                     "/reports/..%2fconfig.json",
                     "/reports/%2e%2e%2fconfig.json"):
            h = _get(path)
            self.assertEqual(h.code, 404, "%s 应为 404" % path)
            body = h.text()
            self.assertNotIn(CANARY_KEY, body, "%s 泄漏了 api_key" % path)
            self.assertNotIn("api_key", body, "%s 泄漏了配置字段" % path)


# ------------------------------------------------------------ 面试：5 条路由
# 模板金丝雀：**各自只可能在对应模板里出现**的稳定文案（不是 <div>/<h1> 这种通用元素名，
# 也不是两个模板共有的注入点 kb.REPORT_MARK）。真报告的文件 <title> 由 app.render_report
# 生成，是「岗位 · 岗位匹配分析 · 0分 · 」（**没有**「报告」二字），所以
# 「岗位匹配分析报告」只可能来自 report_template.html 的 kicker 那一行。
JD_TEMPLATE_MARKER = "岗位匹配分析报告"
INTERVIEW_TEMPLATE_MARKER = "面试复盘报告"


def _sandbox_handler(method, path, body: bytes, headers=None):
    """沙箱化 app.Handler：不监听端口、不接套接字，直接调 do_GET / do_POST。

    这样测才碰得到真问题：`do_GET` **没有外层 try**，路由里未捕获的异常在这里会原样冒出来
    （真实世界里 = 连接裸断、客户端只看到 RemoteDisconnected）；`interview.start` 可以
    patch 成假实现 —— 测试绝不真起转写线程。
    """
    h = app.Handler.__new__(app.Handler)
    h.path = path
    h.command = method
    h.request_version = "HTTP/1.1"
    h.close_connection = True
    h.rfile = io.BytesIO(body)
    h.wfile = io.BytesIO()
    h.headers = dict(headers or {})              # 调用方负责塞 Content-Length
    h._status, h._body, h._ctype, h._sent_file = None, None, "", None
    h._send = lambda code, obj=None, ctype="application/json; charset=utf-8", raw=False: (
        setattr(h, "_status", code), setattr(h, "_body", obj), setattr(h, "_ctype", ctype), obj)[-1]
    h._send_file = lambda p, ctype: (setattr(h, "_status", 200), setattr(h, "_sent_file", p), None)[-1]
    # DELETE 也要走沙箱：`/api/resumes` 的「移出列表」与 `os.remove` 的区别正是要测的东西。
    {"GET": app.Handler.do_GET, "POST": app.Handler.do_POST,
     "DELETE": app.Handler.do_DELETE}.get(method, app.Handler.do_POST)(h)
    return h


class InterviewRouteTest(IsolatedCase):
    """面试 5 条路由的沙箱化测试（写法照 ReportRouteTest / KnowledgeRouteTest）。
    上传用 BytesIO 当 rfile，头部带 Content-Length。"""

    def _handler(self, method, path, body: bytes, headers=None):
        h = _sandbox_handler(method, path, body, headers)
        return h._status, h._body

    def _post_binary(self, path, body: bytes, headers=None):
        hd = {"Content-Length": str(len(body))}
        hd.update(headers or {})
        return self._handler("POST", path, body, hd)

    def _post_json(self, path, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        return self._handler("POST", path, raw,
                             {"Content-Length": str(len(raw)),
                              "Content-Type": "application/json; charset=utf-8"})

    def _get(self, path):
        return self._handler("GET", path, b"", {})

    def _patch(self, obj, name, value):
        """patch 一个模块级属性；用 addCleanup 还原，测试失败也不会漏还原。"""
        p = mock.patch.object(obj, name, value)
        p.start()
        self.addCleanup(p.stop)

    def _no_external_calls(self):
        """把上传路由的两个「真世界副作用」都换掉，返回（start 调用记录, ffprobe 调用记录）。

        - `interview.start`：**每个碰上传路由的用例都必须先调它**，哪怕那条路径"按理说走不到 start"。
          实现被改坏时（例如漏了 413 判断）路由会继续往下走并**真起一条流水线线程**；那个线程在
          addCleanup 还原 store.DATA_ROOT / log.LOG_FILE **之后**才跑完，于是会把 status.json /
          日志写进**用户真实的 data 根**（真事故，见汇报）。
        - `interview.ffprobe_duration`：**绝不真跑 ffprobe**（这台机器装了 ffmpeg，PATH 上可能真有
          ffprobe）。不然用例的耗时/结果就取决于机器环境，还可能被一个 20 秒超时拖慢。
        """
        starts, probes = [], []
        self._patch(interview, "start", lambda *a, **k: starts.append((a, k)))
        self._patch(interview, "ffprobe_duration", lambda path: probes.append(path) or 0.0)
        return starts, probes

    def _upload(self, body=b"RIFF....", headers=None):
        """上传一条音频（默认 8 字节），返回 (code, body)。"""
        hd = {"X-Job-Id": "j1", "X-File-Name": "a.m4a"}
        hd.update(headers or {})
        return self._post_binary("/api/interview/upload", body, hd)

    # ---- 上传
    def test_upload_rejects_missing_job(self):
        self._no_external_calls()
        code, _ = self._post_binary("/api/interview/upload", b"x" * 10,
                                    {"X-Job-Id": "", "X-File-Name": "a.wav"})
        self.assertEqual(code, 400)

    def test_upload_rejects_oversize_without_reading_body(self):
        self._no_external_calls()      # 实现被改坏时这条路径会继续往下走 —— 必须挡住真流水线
        # 用一个"声称超大"的 Content-Length（不真传那么多字节）→ 必须 413 且不写文件
        code, _ = self._post_binary("/api/interview/upload", b"x" * 10,
                                    {"X-Job-Id": "j1", "X-File-Name": "a.wav",
                                     "Content-Length": str(interview.MAX_UPLOAD_BYTES + 1)})
        self.assertEqual(code, 413)
        # 反证：不但拒了，还没有留下任何目录/文件（半截上传必须清理干净）
        self.assertFalse(os.path.exists(store.interviews_dir()))

    def test_upload_writes_the_file_and_starts_the_pipeline(self):
        started, _ = self._no_external_calls()
        code, body = self._post_binary("/api/interview/upload", b"RIFF....",
                                       {"X-Job-Id": "j1", "X-File-Name": "面试录音.m4a"})
        self.assertEqual(code, 200)
        iv = body["interview_id"]
        self.assertEqual(os.path.getsize(store.interview_file(iv, "audio.m4a")), 8)
        self.assertEqual(len(started), 1)
        # 中文文件名经 URL 编码传进来 → 必须还原成可读的元数据（但仍只作元数据，不拼路径）
        self.assertEqual(store.load_interview_json(iv, "status.json")["original_name"], "面试录音.m4a")

    def test_upload_decodes_percent_encoded_filename(self):
        """头部只能放 ASCII，中文名必须 encodeURIComponent 后才发得出来 → 服务端必须 unquote。

        刻意**不**用那个"裸中文头"的用例当唯一证据：`unquote("面试录音.m4a")` 恰好等于原串，
        去掉 unquote 也照样绿（假绿）。这里传的是真编码过的名字，去掉 unquote 必红。
        """
        self._no_external_calls()
        name = quote("面试录音 第2轮.m4a")
        self.assertNotIn("面试", name)                    # 反证：发出去的确实不是明文
        self.assertNotEqual(name, "面试录音 第2轮.m4a")
        code, body = self._upload(headers={"X-File-Name": name})
        self.assertEqual(code, 200)
        st = store.load_interview_json(body["interview_id"], "status.json")
        self.assertEqual(st["original_name"], "面试录音 第2轮.m4a")
        # 服务端生成文件名：客户端名字（哪怕带空格/中文）绝不参与拼路径
        self.assertEqual(st["stored_name"], "audio.m4a")

    def test_upload_never_uses_the_client_filename_as_a_path(self):
        """X-File-Name 带目录穿越也不能越出这场面试的目录。"""
        self._no_external_calls()
        code, body = self._upload(headers={"X-File-Name": quote("../../evil.wav")})
        self.assertEqual(code, 200)
        iv = body["interview_id"]
        d = store.interview_dir(iv)
        self.assertEqual(sorted(os.listdir(d)), ["audio.wav", "status.json"])
        self.assertFalse(os.path.exists(os.path.join(store.p_path("reports"), "evil.wav")))

    def test_upload_text_kind_skips_transcribing(self):
        """文字稿必须存成 source.txt 并且 kind=text —— run_pipeline 对文字稿读的就是这个名字。"""
        started, probes = self._no_external_calls()
        code, body = self._upload(body="面试官：你好".encode("utf-8"),
                                  headers={"X-File-Name": quote("面试记录.md")})
        self.assertEqual(code, 200)
        iv = body["interview_id"]
        self.assertEqual(store.load_interview_json(iv, "status.json")["kind"], "text")
        self.assertEqual(store.read_text(store.interview_file(iv, "source.txt")), "面试官：你好")
        self.assertEqual(started[0][1].get("kind"), "text")     # 传给 start 的 kind 是 text
        self.assertEqual(probes, [])                           # 文字稿不该去探时长

    def test_upload_honors_x_kind_and_falls_back_for_unknown_extensions(self):
        """X-Kind 优先（规格 §4：audio/text/auto）；扩展名认不出时按音频存成 audio.wav ——
        否则 interview._find_audio 按扩展名找录音会找不到，转写直接报「请重新上传录音」。
        """
        self._no_external_calls()
        code, body = self._upload(body="口头转写的文本".encode("utf-8"),
                                  headers={"X-Kind": "text", "X-File-Name": "recording.mp4"})
        self.assertEqual(code, 200)
        self.assertEqual(store.load_interview_json(body["interview_id"], "status.json")["kind"], "text")
        code, body = self._upload(body=b"\x00\x01\x02\x03",
                                  headers={"X-File-Name": "没有扩展名的录音"})
        self.assertEqual(code, 200)
        iv = body["interview_id"]
        self.assertEqual(store.load_interview_json(iv, "status.json")["kind"], "audio")
        self.assertTrue(os.path.exists(store.interview_file(iv, "audio.wav")))

    def test_upload_empty_file_is_rejected(self):
        self._patch(interview, "start", lambda *a, **k: None)
        code, _ = self._upload(body=b"")
        self.assertEqual(code, 400)
        self.assertEqual(os.listdir(store.interviews_dir()), [])   # 空文件不留垃圾目录

    def test_upload_never_asks_the_socket_for_more_than_content_length(self):
        """**死锁回归**：rfile 是套接字上的 BufferedReader，`read(256KB)` 会一直阻塞到凑满
        256KB 或对端关连接 —— 而客户端正在等响应，于是上传永远卡住（本地 BytesIO 测试看不见：
        它到末尾就返回短数据）。这里用「每次只回 1 字节、越界索取就记账」的假 rfile 钉住根因。
        """
        class SocketLike:
            """像真套接字那样：每次最多给 1 字节；一旦被要求读超过剩余 Content-Length 的字节数，
            就说明调用方想「读到 EOF」= 真实世界里会死等。"""
            def __init__(self, data):
                self.data, self.pos, self.over = data, 0, False
            def read(self, n=-1):
                left = len(self.data) - self.pos
                if n is None or n < 0 or n > left:
                    self.over = True
                n = min(n if (n is not None and n >= 0) else left, left)
                out = self.data[self.pos:self.pos + max(0, n)]
                self.pos += len(out)
                return out

        self._patch(interview, "start", lambda *a, **k: None)
        payload = b"RIFF" * 3
        src = SocketLike(payload)
        h = self._raw_upload_handler(src, len(payload))
        app.Handler.do_POST(h)
        self.assertFalse(src.over, "路由向套接字索取了超过 Content-Length 的字节 → 真实上传会死等")
        self.assertEqual(h._status, 200)
        self.assertEqual(os.path.getsize(store.interview_file(h._body["interview_id"], "audio.wav")),
                         len(payload))

    def _raw_upload_handler(self, rfile, content_length, name="a.wav"):
        """手搓一个只差 rfile 的 handler（用给定的 reader 当请求体，声明 content_length 字节）。"""
        h = app.Handler.__new__(app.Handler)
        h.path, h.command, h.request_version, h.close_connection = "/api/interview/upload", "POST", "HTTP/1.1", True
        h.rfile, h.wfile = rfile, io.BytesIO()
        h.headers = {"Content-Length": str(content_length), "X-Job-Id": "j1", "X-File-Name": name}
        h._status, h._body, h._sent_file = None, None, None
        h._send = lambda code, obj=None, ctype="", raw=False: (setattr(h, "_status", code),
                                                              setattr(h, "_body", obj), obj)[-1]
        return h

    def test_upload_truncated_body_is_rejected_and_cleaned_up(self):
        """客户端中途断开（声明的字节数没到齐）：必须报错 + 清理，**绝不**把半截录音当成上传成功。

        ContentLengthReader 拿不到声明的字节数就返回 EOF，save_stream 只会当正常结束 ——
        所以路由必须自己比对写入字节数（否则用户最后只看到「转写失败」，还不知道是上传断了）。
        """
        class Dropping:
            """只有 20 字节却声称 100：每次最多给 3 字节，给完就 EOF（模拟对端断开）。"""
            def __init__(self, data):
                self.data, self.pos = data, 0
            def read(self, n=-1):
                want = len(self.data) - self.pos if (n is None or n < 0) else min(n, len(self.data) - self.pos)
                out = self.data[self.pos:self.pos + max(0, min(3, want))]
                self.pos += len(out)
                return out

        self._no_external_calls()
        h = self._raw_upload_handler(Dropping(b"RIFF" * 5), 100)      # 声明 100，实际只给 20
        app.Handler.do_POST(h)
        self.assertEqual(h._status, 400, h._body)
        self.assertIn("上传中断", h._body["error"])
        self.assertIn("20/100", h._body["error"])
        # 半截录音与它那层目录都必须清掉（只剩 interviews/ 这个空壳是正常的，别的记录还要用它）
        self.assertEqual(os.listdir(store.interviews_dir()), [])

    def test_upload_over_a_real_socket_finishes(self):
        """真 socket + 真 http.client 的回归：这条才是「上传不会卡死」的终局证据
        （沙箱 BytesIO 版本对 read() 的阻塞语义一无所知）。

        服务只绑 127.0.0.1 的临时端口、数据根与日志都被 IsolatedCase 隔离，
        interview.start / ffprobe_duration 都被替换 → 不起真转写线程、不跑真 ffprobe、不碰用户真实数据。
        """
        import http.client
        from http.server import ThreadingHTTPServer
        started, _ = self._no_external_calls()
        srv = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        self.addCleanup(srv.server_close)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        self.addCleanup(srv.shutdown)
        payload = b"RIFF" + b"\x00" * 500
        conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=15)
        self.addCleanup(conn.close)
        conn.request("POST", "/api/interview/upload", body=payload,
                     headers={"Content-Type": "application/octet-stream", "X-Job-Id": "j1",
                              "X-File-Name": quote("面试录音.m4a")})
        resp = conn.getresponse()          # 卡住的话这里会超时抛异常（= 变红）
        body = json.loads(resp.read().decode("utf-8"))
        self.assertEqual(resp.status, 200, body)
        self.assertEqual(os.path.getsize(store.interview_file(body["interview_id"], "audio.m4a")),
                         len(payload))
        self.assertEqual(len(started), 1)

    # ---- status / list / retry
    def test_status_and_list_and_retry(self):
        store.save_interview_json("iv1", "status.json", {"state": "done"})
        self.assertEqual(self._get("/api/interview/status?id=iv1")[1]["state"], "done")
        self.assertEqual(self._get("/api/interview/status?id=nope")[0], 404)
        self.assertIn("interviews", self._get("/api/interview/list?job=j1")[1])
        retried = []
        self._patch(interview, "start", lambda *a, **k: retried.append(a))
        self.assertEqual(self._post_json("/api/interview/retry", {"id": "iv1"})[0], 200)
        self.assertEqual(len(retried), 1)

    def test_upload_records_probed_duration_for_the_estimate(self):
        """上传时用 ffprobe 探到的时长效进 status.json（规格 §5 的「预计」全靠它）。

        **绝不真跑 ffprobe**：把 interview.ffprobe_duration 换成假实现。
        """
        self._no_external_calls()
        self._patch(interview, "ffprobe_duration", lambda path: 600.0)
        code, body = self._upload()
        self.assertEqual(code, 200)
        st = self._get("/api/interview/status?id=" + quote(body["interview_id"]))[1]
        self.assertEqual(st["audio_duration"], 600.0)
        self.assertEqual(st["estimate_hint"]["estimate_sec"], 540.0)
        self.assertIn("音频时长 × 0.9", st["estimate_hint"]["text"])

    def test_upload_survives_a_failing_duration_probe_and_has_no_estimate(self):
        """探测失败/没装 ffprobe：上传照常成功，但**不返回** estimate_hint（不编预计）。"""
        def boom(_path):
            raise OSError("ffprobe 不存在")
        self._no_external_calls()
        self._patch(interview, "ffprobe_duration", boom)
        code, body = self._upload()
        self.assertEqual(code, 200, body)
        st = self._get("/api/interview/status?id=" + quote(body["interview_id"]))[1]
        self.assertEqual(st["audio_duration"], 0.0)
        self.assertNotIn("estimate_hint", st)

    def test_upload_does_not_probe_text_transcripts(self):
        """文字稿没有音频可探：不该去调 ffprobe（调了也是白等）。"""
        self._no_external_calls()
        called = []
        self._patch(interview, "ffprobe_duration", lambda path: called.append(path) or 600.0)
        code, _ = self._upload(body="面试官：你好".encode("utf-8"),
                               headers={"X-File-Name": quote("面试记录.md")})
        self.assertEqual(code, 200)
        self.assertEqual(called, [])

    def test_status_failure_returns_a_structured_500(self):
        """do_GET 没有外层 try：路由必须自己兜底，否则客户端拿到的是连接裸断（RemoteDisconnected）。"""
        def boom(_iid):
            raise RuntimeError("坏掉的 status.json")
        self._patch(interview, "status", boom)
        code, body = self._get("/api/interview/status?id=iv1")
        self.assertEqual(code, 500)
        self.assertFalse(body["ok"])
        self.assertIn("坏掉的 status.json", body["error"])

    def test_status_and_list_require_and_respect_ids(self):
        store.save_interview_json("iv1", "status.json", {"state": "done", "job_id": "j1"})
        store.save_interview_json("iv2", "status.json", {"state": "done", "job_id": "j2"})
        self.assertEqual(self._get("/api/interview/status")[0], 400)
        listed = self._get("/api/interview/list?job=j1")[1]["interviews"]
        self.assertEqual([row["id"] for row in listed], ["iv1"])
        self.assertEqual(listed[0]["running"], False)     # 没有活动线程 → 不是「在跑」
        self.assertEqual(self._get("/api/interview/list")[1]["interviews"], [])

    def test_retry_rejects_unknown_id(self):
        started = []
        self._patch(interview, "start", lambda *a, **k: started.append(a))
        self.assertEqual(self._post_json("/api/interview/retry", {"id": "nope"})[0], 404)
        self.assertEqual(self._post_json("/api/interview/retry", {})[0], 400)
        self.assertEqual(started, [])                     # 找不到记录绝不假装起了任务

    def test_retry_passes_the_saved_job_and_kind(self):
        store.save_interview_json("iv1", "status.json",
                                  {"state": "error", "job_id": "j1", "kind": "text"})
        seen = []
        self._patch(interview, "start", lambda *a, **k: seen.append((a, k)))
        self.assertEqual(self._post_json("/api/interview/retry", {"id": "iv1"})[0], 200)
        self.assertEqual(seen[0][0][0], "j1")             # 岗位从 status.json 带过去
        self.assertEqual(seen[0][1]["kind"], "text")      # 文字稿不会退化成音频流水线

    # ---- 手动入库
    def test_knowledge_add_creates_card_as_not_started(self):
        code, body = self._post_json("/api/knowledge/add",
                                     {"term": "向量检索", "definition": "把文本变成向量后按相似度找"})
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        card = [n for n in store.load_knowledge()["nodes"] if n["id"] == "向量检索"][0]
        self.assertEqual(card["state"], "待学习")              # 手动入库 → 进复习队列
        self.assertEqual(card["definition"], "把文本变成向量后按相似度找")

    def test_knowledge_add_rejects_empty_term(self):
        self.assertEqual(self._post_json("/api/knowledge/add", {"term": "（）"})[0], 400)

    def test_knowledge_add_keeps_existing_card_content(self):
        """已经存在的卡：只进复习队列，**不用报告里的解释覆盖用户/更全的内容**。"""
        self._post_json("/api/knowledge/add", {"term": "RLHF",
                                               "definition": "一整套很长的既有解释，不能被短句顶掉"})
        self._post_json("/api/knowledge/add", {"term": "RLHF", "definition": "短解释"})
        nodes = [n for n in store.load_knowledge()["nodes"] if n["id"] == "rlhf"]
        self.assertEqual(len(nodes), 1)                       # 同一个概念只有一张卡
        self.assertEqual(nodes[0]["state"], "待学习")
        self.assertIn("不能被短句顶掉", nodes[0]["definition"])


class TemplateKindTest(IsolatedCase):
    """/reports/* 按 REPORT_DATA 里的 kind 选模板：interview → 复盘模板，缺省 → JD 模板。

    缺省必须是 JD 模板（否则上线当天所有老 JD 报告都会变成复盘版式）。
    """

    def _handler(self, method, path, body: bytes = b"", headers=None):
        h = _sandbox_handler(method, path, body, headers)
        return h._status, h._body

    def _get(self, path):
        return self._handler("GET", path)

    def _write_report(self, name, payload):
        store.ensure_profile()
        path = os.path.join(store.p_path("reports"), name)
        store.write_text(path, "<html><head><title>T</title></head><body><script>\n%s%s;\n</script></body></html>"
                         % (kb.REPORT_MARK, json.dumps(payload, ensure_ascii=False)))
        return path

    def test_markers_are_template_exclusive(self):
        """金丝雀自检：两个标记必须各自只在自己那个模板里 —— 否则下面的断言全靠巧合。"""
        jd = store.read_text(app.TEMPLATE_PATH)
        iv = store.read_text(os.path.join(app.WEB, "interview_template.html"))
        self.assertIn(JD_TEMPLATE_MARKER, jd)
        self.assertNotIn(JD_TEMPLATE_MARKER, iv)
        self.assertIn(INTERVIEW_TEMPLATE_MARKER, iv)
        self.assertNotIn(INTERVIEW_TEMPLATE_MARKER, jd)

    def test_interview_report_uses_interview_template(self):
        self._write_report("x_面试复盘.html", {"kind": "interview", "summary": "s"})
        body = self._get("/reports/" + quote("x_面试复盘.html"))[1]
        self.assertIn(INTERVIEW_TEMPLATE_MARKER, body)          # 访谈模板独有的字符串
        self.assertNotIn(JD_TEMPLATE_MARKER, body)

    def test_each_template_contains_the_report_marker_exactly_once(self):
        """`kb.read_report_data` 从**第一次**出现的位置开始解析：模板里多一处（哪怕写在注释里）
        就会让所有报告的快照都读不出来 —— 整批报告静默退化成「原样发送旧文件」。

        这不是假想：访谈模板的头部注释里抄了一遍那行赋值，本文件另一条用例当场抓到。
        """
        for path in (app.TEMPLATE_PATH, app.INTERVIEW_TEMPLATE_PATH):
            text = store.read_text(path)
            self.assertEqual(text.count(kb.REPORT_MARK), 1,
                             "%s 里 %r 出现了 %d 次"
                             % (os.path.basename(path), kb.REPORT_MARK, text.count(kb.REPORT_MARK)))

    def test_written_interview_report_data_is_readable(self):
        """真写出来的复盘报告，其 REPORT_DATA 必须能被 `kb.read_report_data` 读出来 ——
        读不出来 = 报告列表回填 / 知识库体检 / 我的动态渲染全都看不到这场面试的数据。"""
        interview.render_report({"kind": "interview", "summary": "独有总评"}, "j1", "iv1")
        path = os.path.join(store.p_path("reports"), "iv_iv1.html")
        data = kb.read_report_data(path)
        self.assertIsInstance(data, dict)
        self.assertEqual(data.get("kind"), "interview")
        self.assertEqual(data.get("summary"), "独有总评")

    def test_report_written_by_the_real_pipeline_uses_the_interview_template(self):
        """真实现写出来的复盘报告也必须走访谈模板（不管是磁盘上那份还是 serve 出来的）。

        这条是防假绿的：上面那份是手写的 <title>T</title>，而真报告的 <title> 由
        app.render_report 生成 —— 必须按真文件断言一次，否则「写盘用 JD、serve 用访谈」
        这种割裂会静默通过。
        """
        interview.render_report({"kind": "interview", "summary": "s", "meta": {"interview_id": "iv1"}},
                                "j1", "iv1")
        path = os.path.join(store.p_path("reports"), "iv_iv1.html")
        self.assertTrue(os.path.exists(path))
        code, body = self._get("/reports/iv_iv1.html")
        self.assertEqual(code, 200)
        self.assertIn(INTERVIEW_TEMPLATE_MARKER, body)
        self.assertNotIn(JD_TEMPLATE_MARKER, body)
        self.assertNotIn("__REPORT_DATA__", body)

    def test_report_on_disk_uses_the_interview_template_without_any_route(self):
        """**磁盘上那份文件本身**就必须是复盘版式 —— 直接读文件（= file:// 语义），不经过 HTTP。

        用户会右键保存 / 双击打开 / 发给别人；如果只有 serve 时才换模板，那份文件永远是
        「JD 骨架 + 访谈数据」（而且没有转写正文）。
        """
        store.save_interview_json("iv1", "transcript.json",
                                  {"text": "面试官：讲讲 RLHF 的独有一句。", "language": "zh"})
        interview.render_report({"kind": "interview", "job_title": "AI 产品经理", "date": "2026-10-09",
                                 "summary": "s", "meta": {"interview_id": "iv1"}}, "j1", "iv1")
        path = os.path.join(store.p_path("reports"), "iv_iv1.html")
        on_disk = store.read_text(path)
        self.assertIn(INTERVIEW_TEMPLATE_MARKER, on_disk)
        self.assertNotIn(JD_TEMPLATE_MARKER, on_disk)
        self.assertNotIn("__REPORT_DATA__", on_disk)
        self.assertNotIn("__TITLE__", on_disk)
        # 转写正文写进 payload → 不经过任何路由也能看见（这是 file:// 打开时的唯一来源）
        self.assertIn("讲讲 RLHF 的独有一句", on_disk)
        # 标题也不能还是「岗位匹配分析」（那是 JD 报告的说法，出现在标签页/打印页眉上）
        self.assertIn("面试复盘", on_disk)
        self.assertNotIn("岗位匹配分析", on_disk)

    def test_write_and_serve_share_one_template_rule(self):
        """写盘与 serve 必须共用同一份 kind 判定 —— 两处各写一套迟早分叉。"""
        self.assertEqual(app.report_template_for({"kind": "interview"}), app.INTERVIEW_TEMPLATE_PATH)
        self.assertEqual(app.report_template_for({"kind": "jd"}), app.TEMPLATE_PATH)
        self.assertEqual(app.report_template_for({}), app.TEMPLATE_PATH)
        self.assertEqual(app.report_template_for(None), app.TEMPLATE_PATH)
        # 同一份数据：写盘产出的页面与 serve 出来的页面必须都是访谈版式
        interview.render_report({"kind": "interview", "summary": "s", "meta": {"interview_id": "iv1"}},
                                "j1", "iv1")
        path = os.path.join(store.p_path("reports"), "iv_iv1.html")
        served = app._render_report_page(path)
        self.assertIn(INTERVIEW_TEMPLATE_MARKER, store.read_text(path))
        self.assertIn(INTERVIEW_TEMPLATE_MARKER, served)
        self.assertNotIn(JD_TEMPLATE_MARKER, served)

    def test_old_interview_report_without_transcript_gets_it_at_serve_time(self):
        """旧报告的兜底：payload 里没有 transcript_text（历史文件），serve 时从 transcript.json 补。"""
        store.save_interview_json("iv9", "transcript.json", {"text": "旧报告的转写正文"})
        self._write_report("iv_9.html", {"kind": "interview", "summary": "s",
                                         "meta": {"interview_id": "iv9"}})
        self.assertIn("旧报告的转写正文", self._get("/reports/iv_9.html")[1])

    def test_jd_report_still_uses_jd_template(self):
        self._write_report("x_岗位.html", {"job_title": "岗"})
        body = self._get("/reports/" + quote("x_岗位.html"))[1]
        self.assertIn(JD_TEMPLATE_MARKER, body)
        self.assertNotIn(INTERVIEW_TEMPLATE_MARKER, body)

    def test_unknown_kind_falls_back_to_jd_template(self):
        self._write_report("y.html", {"kind": "something-else"})
        body = self._get("/reports/y.html")[1]
        self.assertIn(JD_TEMPLATE_MARKER, body)
        self.assertNotIn(INTERVIEW_TEMPLATE_MARKER, body)

    def test_missing_interview_template_falls_back_to_the_raw_file(self):
        """复盘模板缺失 → 退回文件本身（与 JD 同款兜底），绝不 5xx、也绝不换成 JD 版式。"""
        path = self._write_report("z.html", {"kind": "interview", "summary": "s"})
        real = app.INTERVIEW_TEMPLATE_PATH
        self.addCleanup(setattr, app, "INTERVIEW_TEMPLATE_PATH", real)
        app.INTERVIEW_TEMPLATE_PATH = os.path.join(store.p_path("reports"), "不存在.html")
        # 绝不能偷偷改用 JD 模板顶上（那样这里会返回一段 HTML 而不是 None）
        self.assertIsNone(app._render_report_page(path))
        h = _sandbox_handler("GET", "/reports/z.html", b"")
        self.assertEqual(h._status, 200)
        self.assertEqual(h._sent_file, path)          # 原样发送的就是这份文件
        self.assertIsNone(h._body)                    # 没走动态拼装（_send_file 才算兜底）

    def test_interview_report_page_carries_the_transcript(self):
        """复盘报告的「原始转写全文」段要有正文可折叠 —— 它只能来自这次面试的 transcript.json。"""
        store.save_interview_json("iv1", "transcript.json",
                                  {"text": "面试官：讲讲 RLHF。我：……", "language": "zh"})
        self._write_report("iv_1.html", {"kind": "interview", "summary": "s",
                                         "meta": {"interview_id": "iv1"}})
        body = self._get("/reports/iv_1.html")[1]
        self.assertIn("讲讲 RLHF", body)

    def test_interview_report_without_transcript_still_renders(self):
        self._write_report("iv_2.html", {"kind": "interview", "summary": "s"})
        code, body = self._get("/reports/iv_2.html")
        self.assertEqual(code, 200)
        self.assertIn(INTERVIEW_TEMPLATE_MARKER, body)


# ------------------------------------------------------------ 结果 / 复盘：3 条路由 + 模板
# 模板金丝雀：**只可能**来自 outcome_template.html 的稳定文案。刻意避开两个坑：
#   ① 不能选两个模板共有的注入点 kb.REPORT_MARK；
#   ② 不能选「岗位匹配分析报告」——那是 JD 模板的 kicker，与 kind 判定无关；
#      也**不能靠 <title> 里的措辞**：真报告的标题由 app.render_report 拼，
#      JD / 复盘 / 结果复盘三种标题都可能是同一批字眼（上一轮踩过）。
OUTCOME_TEMPLATE_MARKER = "复盘结论：为什么是这个结果"
# 结果复盘模板的**静态**哨兵（头部 kicker 那一行，原样躺在模板文件里）。
# 与上面那个动态标记一起断言，才能排除「两份模板凑出同一句话」的巧合。
OUTCOME_TEMPLATE_STATIC = "AI 求职 SOP · 结果复盘报告"


class _OutcomeRouteCase(IsolatedCase):
    """3 条结果/复盘路由的沙箱化测试（写法照 InterviewRouteTest / TemplateKindTest）。

    **绝不真调模型**：`outcome.llm.repair_json` 一律 patch 成抛错 —— 真实现会走网络；
    归因成功路径全部注入假 `chat`。报告只写进隔离根的 reports/。
    """

    # **类定义时**就把真函数抓在手里：setUp 会把 outcome.attribute / render_report 换成替身，
    # 想走真链路的用例（`_real_attribute`）这时再去 `outcome.attribute` 拿到的已经是替身了
    # —— 那会让「注入假 chat」变成「注入一个替身」，看起来像实现坏了。
    _TRUE_ATTRIBUTE = staticmethod(outcome.attribute)
    _TRUE_REAL_RENDER = staticmethod(outcome.render_report)

    @staticmethod
    def _TRUE_RENDER(payload, job_id):
        """替身版的 render_report：**写盘时用结果复盘模板**，但**不经过 `report_template_for`**。

        「选哪个模板」是路由那条用例的测点；只把模板选择留在这里，是为了让
        `OutcomeTemplateTest` 能**单独**证明 kind=outcome 的判定（改坏那一条只会让那边变红，
        不会把两边一起弄红，取证才能定位到根因）。
        """
        import app as _app
        data = dict(payload) if isinstance(payload, dict) else {}
        data["kind"] = "outcome"
        data["report_id"] = "oc_" + str(job_id)
        fname = "oc_%s.html" % job_id
        store.ensure_profile()
        store.write_text(os.path.join(store.p_path("reports"), fname),
                         store.read_text(_app.OUTCOME_TEMPLATE_PATH)
                         .replace("__TITLE__", "结果复盘")
                         .replace("__REPORT_DATA__", json.dumps(data, ensure_ascii=False)))
        return fname

    def setUp(self):
        super().setUp()
        # 归因里 llm.repair_json 那条路径**不可注入**（outcome._parse_reply 直接调模块属性），
        # 只能在这里挡住：干净 JSON 走不到它，坏 JSON 的用例会覆盖成自己要的假实现。
        self._patch(outcome.llm, "repair_json",
                    mock.Mock(side_effect=AssertionError("测试里不该真去修 JSON")))
        self.attribute_calls = []
        self._patch(outcome, "attribute", self._recording_attribute)

    def _patch(self, obj, name, value):
        p = mock.patch.object(obj, name, value)
        p.start()
        self.addCleanup(p.stop)

    def _recording_attribute(self, job_id, cfg, chat=None):
        """默认替身：记录调用参数后抛错（用例想走成功路径必须自己注入假 chat）。

        抛的是 AssertionError —— 归因路由的兜底 `except Exception` 会把它变成 500，
        于是「忘了注入假实现」在用例里表现为醒目的 500 而不是意外 200，不会静默假绿。
        """
        self.attribute_calls.append({"job_id": job_id, "cfg": cfg, "chat": chat})
        raise AssertionError("本用例没有注入假的 attribute 实现")

    def _real_attribute(self, chat, cfg=None, real_render=False):
        """还原**真实现**并注入假模型 —— 只有走真链路的用例才调它。

        `real_render=True` 时才把 `outcome.render_report` 还原成真函数（真选模板）；
        默认装的是 `_TRUE_RENDER` 替身，它**只写盘不选模板**。
        """
        real = self._TRUE_ATTRIBUTE
        calls = {"n": 0, "prompt": ""}

        def spy(prompt, _cfg):
            calls["n"] += 1
            calls["prompt"] = prompt
            return chat(prompt, _cfg)

        def patched(job_id, _cfg, chat=None):
            return real(job_id, cfg or {}, chat=spy)

        self.attribute_calls = []
        self._patch(outcome, "attribute", patched)
        self._patch(outcome, "render_report",
                    self._TRUE_REAL_RENDER if real_render else self._TRUE_RENDER)
        return calls

    def _handler(self, method, path, body: bytes = b"", headers=None):
        h = _sandbox_handler(method, path, body, headers)
        return h._status, h._body

    def _post_json(self, path, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        return self._handler("POST", path, raw,
                             {"Content-Length": str(len(raw)),
                              "Content-Type": "application/json; charset=utf-8"})

    def _get(self, path):
        return self._handler("GET", path, b"", {})

    def _job(self, job_id="j1", **kw):
        """一条岗位：**JD 记录（`jds.json`）+ 分析记录（`jobs.json`）**，阶段 4A 的形状。

        调用方仍然只传一个 id —— 两个文件都用它（真实数据迁移后 `jd_id` 是结构化 ID、
        分析 id 是报告文件名，但那是迁移脚本的事；这里要的是「同一个 id 两边都指得到」）。
        JD 级字段（job_title / job_category / status / notes / …）进 `jds.json`，
        分析级字段（report / resume / score）进 `jobs.json`。
        """
        jd = {"id": job_id}
        rec = {"id": job_id, "jd_id": job_id, "report": job_id + ".html", "score": 80.0}
        for k, v in kw.items():
            if k in app.ANALYSIS_FIELDS:
                rec[k] = v
            else:
                jd[k] = v
        jd.setdefault("job_title", "AI训练标注师")
        jd.setdefault("job_category", "数据标注/质检")
        jdid.upsert_jd(jd)
        app._save_analysis(rec)
        return rec

    def _report_path(self, job_id="j1"):
        return os.path.join(store.p_path("reports"), "oc_%s.html" % job_id)


def _fake_chat(text):
    """与 interview.analyze / outcome.attribute 同一约定：`chat(prompt, cfg)`。"""
    return lambda prompt, cfg: text


class JobResultRouteTest(_OutcomeRouteCase):
    """POST /api/job/result —— 保存结果（status 非法 → 400；岗位不存在 → 404）。"""

    def test_saves_a_valid_result_and_returns_the_view(self):
        self._job()
        code, body = self._post_json("/api/job/result",
                                     {"id": "j1", "status": "已拒", "reason": "HR 说预算冻结了"})
        self.assertEqual(code, 200)
        self.assertTrue(body.get("ok"))
        self.assertEqual(body["application"]["status"], "已拒")
        self.assertEqual(body["application"]["outcome_reason"], "HR 说预算冻结了")
        # 落盘了才算真保存：重新读一遍 **jds.json**（阶段 4A：结果属 JD 级）——不信任内存里的返回值
        jd = jdid.find_jd("j1")
        self.assertEqual(jd.get("status"), "已拒")                         # JD 记录上的状态
        self.assertEqual(jd["application"]["outcome_reason"], "HR 说预算冻结了")
        self.assertTrue(jd["application"]["outcome_at"])
        # 反向：**jobs.json 里绝不能有这些 JD 级字段**（否则两个文件各存一份）
        rec = store.find_job("j1")
        for stolen in ("status", "notes", "job_title", "company", "application",
                       "outcome_reason", "outcome_at"):
            self.assertNotIn(stolen, rec, "JD 级字段 %s 漏回了 jobs.json" % stolen)

    def test_invalid_status_is_400_and_nothing_is_written(self):
        self._job()
        code, body = self._post_json("/api/job/result", {"id": "j1", "status": "胡说", "reason": "x"})
        self.assertEqual(code, 400)
        self.assertFalse(body.get("ok"))
        self.assertNotIn("status", jdid.find_jd("j1"))                    # 一个键都没写进去
        self.assertNotIn("application", jdid.find_jd("j1"))

    def test_unknown_job_is_404(self):
        code, body = self._post_json("/api/job/result",
                                     {"id": "没有这个岗位", "status": "已拒", "reason": "x"})
        self.assertEqual(code, 404)
        self.assertFalse(body.get("ok"))
        # 光看状态码不行：通用的「未匹配路由」也是 404，会把这条用例白送成绿
        self.assertIn("找不到岗位", body.get("error", ""))

    def test_missing_id_is_404(self):
        code, body = self._post_json("/api/job/result", {"status": "已拒", "reason": "x"})
        self.assertEqual(code, 404)
        self.assertFalse(body.get("ok"))
        self.assertIn("找不到岗位", body.get("error", ""))

    def test_empty_reason_is_allowed(self):
        self._job()
        code, body = self._post_json("/api/job/result", {"id": "j1", "status": "放弃", "reason": ""})
        self.assertEqual(code, 200, body)
        self.assertEqual(body["application"]["outcome_reason"], "")

    def test_reason_with_markup_survives_verbatim(self):
        """用户写的原因要**原样**存下来（转义是报告模板的事，不是存储的事）。"""
        self._job()
        raw = "他说「你不合适」</script><b>x</b>"
        code, _ = self._post_json("/api/job/result", {"id": "j1", "status": "已拒", "reason": raw})
        self.assertEqual(code, 200)
        self.assertEqual(jdid.find_jd("j1")["application"]["outcome_reason"], raw)


class OutcomeQueryRouteTest(_OutcomeRouteCase):
    """GET /api/job/outcome?id= —— 读回 application（含「是否已复盘」）供前端渲染。"""

    def test_returns_the_view_without_any_attribution_yet(self):
        self._job()
        code, body = self._get("/api/job/outcome?id=j1")
        self.assertEqual(code, 200)
        self.assertTrue(body.get("ok"))
        self.assertEqual(body["application"]["status"], "未投递")
        self.assertIsNone(body["application"]["outcome_ai"])
        self.assertFalse(body["has_ai"])                          # 前端据此画「未复盘」
        self.assertIsInstance(body["statuses"], list)             # 状态下拉直接用 store.STATUSES
        self.assertEqual(body["statuses"], store.STATUSES)
        self.assertEqual(body["terminal"], list(outcome.TERMINAL))

    def test_reflects_a_saved_result_and_an_existing_attribution(self):
        self._job()
        self._post_json("/api/job/result", {"id": "j1", "status": "已拒", "reason": "预算冻结"})
        self._real_attribute(_fake_chat('{"verdict_summary":"总评"}'))
        code, _ = self._post_json("/api/job/attribute", {"id": "j1"})
        self.assertEqual(code, 200)
        code, body = self._get("/api/job/outcome?id=j1")
        self.assertEqual(code, 200)
        self.assertEqual(body["application"]["outcome_reason"], "预算冻结")
        self.assertTrue(body["has_ai"])
        self.assertEqual(body["application"]["outcome_ai"]["verdict_summary"], "总评")

    def test_unknown_job_is_404(self):
        code, body = self._get("/api/job/outcome?id=nope")
        self.assertEqual(code, 404)
        self.assertFalse(body.get("ok"))
        self.assertIn("找不到岗位", body.get("error", ""))

    def test_missing_id_is_400(self):
        code, body = self._get("/api/job/outcome")
        self.assertEqual(code, 400)
        self.assertFalse(body.get("ok"))
        self.assertIn("缺少岗位 id", body.get("error", ""))

    def test_corrupt_job_record_returns_a_structured_500_not_a_bare_disconnect(self):
        """`do_GET` **没有外层 try**：路由自己不兜底 → 真实世界里客户端只看到 RemoteDisconnected。

        手改坏 jobs.json（这里用 patch 模拟）时必须是结构化 500 —— 前端才有东西可显示。
        """
        self._job()
        self._patch(outcome, "application_of", mock.Mock(side_effect=TypeError("坏记录")))
        code, body = self._get("/api/job/outcome?id=j1")
        self.assertEqual(code, 500)
        self.assertFalse(body.get("ok"))
        self.assertIn("读取失败", body.get("error", ""))


class JobAttributeRouteTest(_OutcomeRouteCase):
    """POST /api/job/attribute —— 归因 + 生成报告（未到终态 → 400 且说明「还没有结果」）。"""

    def test_not_terminal_job_is_400_with_an_honest_message(self):
        """**走真实现**（不是替身）：严格门槛下裸岗位必须被 `outcome.NotTerminal` 拦在模型调用之前。"""
        self._job()
        calls = self._real_attribute(_fake_chat("{}"), cfg={})
        code, body = self._post_json("/api/job/attribute", {"id": "j1"})
        self.assertEqual(code, 400, body)
        self.assertFalse(body.get("ok"))
        self.assertIn("还没有结果", body.get("error", ""))
        self.assertEqual(calls["n"], 0, "没到终态时一次模型调用都不该发出去")
        self.assertFalse(os.path.exists(self._report_path()))     # 没生成报告

    def test_recording_a_non_terminal_status_also_gets_400(self):
        self._job()
        self._post_json("/api/job/result", {"id": "j1", "status": "已投", "reason": ""})
        calls = self._real_attribute(_fake_chat("{}"), cfg={})
        code, body = self._post_json("/api/job/attribute", {"id": "j1"})
        self.assertEqual(code, 400, body)
        self.assertIn("还没有结果", body.get("error", ""))
        self.assertIn("已投", body.get("error", ""))              # 说真实状态，不谎报
        self.assertEqual(calls["n"], 0)

    def test_unknown_job_is_404(self):
        calls = self._real_attribute(_fake_chat("{}"), cfg={})
        code, body = self._post_json("/api/job/attribute", {"id": "nope"})
        self.assertEqual(code, 404)
        self.assertFalse(body.get("ok"))
        self.assertIn("找不到岗位", body.get("error", ""))
        self.assertEqual(calls["n"], 0)                           # 岗位都没有，更不该调模型

    def test_terminal_job_attribution_writes_the_report_and_returns_its_url(self):
        """成功路径：**必须先记录终态结果**（严格门槛下裸岗位直接 NotTerminal）。"""
        self._job()
        self._post_json("/api/job/result", {"id": "j1", "status": "已拒", "reason": "预算冻结"})
        self._real_attribute(_fake_chat(json.dumps({
            "verdict_summary": "总评独有句",
            "possible_reasons": [{"reason": "答不上来", "evidence": "被问到 RLHF 时卡住",
                                  "confidence": "高", "controllable": True}],
            "actions": [{"area": "知识", "action": "补 RLHF", "why": "被问到"}],
            "to_study": [{"term": "RLHF", "why": "被问到", "definition": "人类反馈强化学习"}],
            "next_time": "下次先复习 RLHF",
        }, ensure_ascii=False)))

        code, body = self._post_json("/api/job/attribute", {"id": "j1"})
        self.assertEqual(code, 200, body)
        self.assertTrue(body.get("ok"))
        self.assertEqual(body["report"], "oc_j1.html")
        self.assertEqual(body["url"], "/reports/oc_j1.html")
        self.assertEqual(body["outcome_ai"]["verdict_summary"], "总评独有句")
        # ① 报告文件**真被写出**
        path = self._report_path()
        self.assertTrue(os.path.exists(path))
        # ② **通过路由 serve 出来的那份**也是结果复盘版式（route 写盘 + serve 共用一条 kind 规则）
        code2, served = self._get("/reports/oc_j1.html")
        self.assertEqual(code2, 200)
        self.assertIn(OUTCOME_TEMPLATE_MARKER, served)
        self.assertNotIn(JD_TEMPLATE_MARKER, served)
        self.assertNotIn(INTERVIEW_TEMPLATE_MARKER, served)
        # ③ 快照读得回来（写成注释/多一处 REPORT_MARK 会让整批报告静默降级）
        data = kb.read_report_data(path)
        self.assertIsInstance(data, dict)
        self.assertEqual(data.get("kind"), "outcome")

    def test_the_report_renders_the_users_own_words_not_a_paraphrase(self):
        """报告里的「你写的原因」必须是用户**原话**（由 outcome.render_report 从台账补齐）。

        这条刻意走**真 render_report**：替身不负责补台账字段，补进去的断言就没有意义了。
        """
        self._job()
        self._post_json("/api/job/result", {"id": "j1", "status": "Offer", "reason": "HR 说恭喜"})
        self._real_attribute(_fake_chat('{"verdict_summary":"总评"}'), real_render=True)
        self._post_json("/api/job/attribute", {"id": "j1"})
        data = kb.read_report_data(self._report_path())
        self.assertEqual(data.get("outcome_reason"), "HR 说恭喜")
        self.assertEqual(data.get("status"), "Offer")

    def test_attribution_never_overwrites_the_users_reason(self):
        """分清「人说的」与「机器猜的」是本设计的底线：重复归因也不许覆盖。"""
        self._job()
        self._post_json("/api/job/result", {"id": "j1", "status": "已拒", "reason": "我自己写的"})
        self._real_attribute(_fake_chat('{"verdict_summary":"第一次","outcome_reason":"AI 编的"}'))
        self._post_json("/api/job/attribute", {"id": "j1"})
        self._real_attribute(_fake_chat('{"verdict_summary":"第二次","outcome_reason":"又编一遍"}'))
        self._post_json("/api/job/attribute", {"id": "j1"})
        job = jdid.find_jd("j1")                        # 阶段 4A：application 在 JD 记录上
        self.assertEqual(job["application"]["outcome_reason"], "我自己写的")
        self.assertNotIn("outcome_reason", job["application"]["outcome_ai"],
                         "outcome_reason 绝不能被复制进 outcome_ai")
        self.assertEqual(job["application"]["outcome_ai"]["verdict_summary"], "第二次")

    def test_unparseable_model_reply_is_a_structured_500(self):
        self._job()
        self._post_json("/api/job/result", {"id": "j1", "status": "已拒", "reason": ""})
        self._patch(outcome.llm, "repair_json", mock.Mock(side_effect=RuntimeError("修不动")))
        self._real_attribute(_fake_chat("这不是 JSON"))
        code, body = self._post_json("/api/job/attribute", {"id": "j1"})
        self.assertEqual(code, 500, body)
        self.assertFalse(body.get("ok"))
        self.assertIn("归因失败", body.get("error", ""))
        self.assertFalse(os.path.exists(self._report_path()))     # 归因失败 → 不生成报告
        # 用户写的状态/原因已在保存时落盘：归因失败不会把它弄丢
        self.assertEqual(jdid.find_jd("j1")["application"]["status"], "已拒")

    def test_the_model_receives_the_users_reason_and_the_jd_basis(self):
        """归因必须把「用户写的原因」和 JD 拆解喂进去 —— 否则报告是凭空生成的。"""
        self._job(report="r.html", jd_text_file="j1.txt")
        store.ensure_profile()
        store.write_text(os.path.join(store.p_path("jd"), "j1.txt"), "我们要求熟悉 RLHF 与评测集构建")
        store.write_text(os.path.join(store.p_path("reports"), "r.html"),
                         "<html><body><script>%s%s;\n</script></body></html>"
                         % (kb.REPORT_MARK, json.dumps({"job_profile": "标注岗画像"},
                                                       ensure_ascii=False)))
        self._post_json("/api/job/result", {"id": "j1", "status": "已拒", "reason": "他说我表达不清楚"})
        calls = self._real_attribute(_fake_chat('{"verdict_summary":"总评"}'))
        code, _ = self._post_json("/api/job/attribute", {"id": "j1"})
        self.assertEqual(code, 200)
        self.assertEqual(calls["n"], 1, "同步归因只调用模型一次")
        self.assertIn("他说我表达不清楚", calls["prompt"])
        self.assertIn("标注岗画像", calls["prompt"])
        self.assertIn("RLHF", calls["prompt"])

    def test_attribution_route_uses_the_configured_cfg(self):
        """route 必须把设置里的 cfg 传给 attribute（不传就等于没配模型）。"""
        self._job()
        self._post_json("/api/job/result", {"id": "j1", "status": "已拒", "reason": ""})
        seen = {}
        self._patch(outcome, "attribute",
                    lambda job_id, cfg, chat=None: seen.update(job_id=job_id, cfg=cfg) or {})
        store.save_config({"text_model": {"model": "假模型"}})
        code, _ = self._post_json("/api/job/attribute", {"id": "j1"})
        self.assertEqual(code, 200)
        self.assertEqual(seen["job_id"], "j1")
        self.assertEqual((seen["cfg"] or {}).get("text_model", {}).get("model"), "假模型")


class OutcomeTemplateTest(_OutcomeRouteCase):
    """结果复盘模板本身：标记唯一、快照可读、kind 三分支各走对模板。"""

    def _write_report(self, name, payload):
        store.ensure_profile()
        path = os.path.join(store.p_path("reports"), name)
        store.write_text(path, "<html><head><title>T</title></head><body><script>\n%s%s;\n</script></body></html>"
                         % (kb.REPORT_MARK, json.dumps(payload, ensure_ascii=False)))
        return path

    def test_markers_are_template_exclusive(self):
        """金丝雀自检：三个标记各自只在自己那个模板里 —— 否则下面的断言全靠巧合。"""
        jd = store.read_text(app.TEMPLATE_PATH)
        iv = store.read_text(app.INTERVIEW_TEMPLATE_PATH)
        oc = store.read_text(app.OUTCOME_TEMPLATE_PATH)
        self.assertIn(OUTCOME_TEMPLATE_MARKER, oc)
        self.assertNotIn(OUTCOME_TEMPLATE_MARKER, jd)
        self.assertNotIn(OUTCOME_TEMPLATE_MARKER, iv)
        self.assertIn(JD_TEMPLATE_MARKER, jd)
        self.assertNotIn(JD_TEMPLATE_MARKER, oc)
        self.assertIn(INTERVIEW_TEMPLATE_MARKER, iv)
        self.assertNotIn(INTERVIEW_TEMPLATE_MARKER, oc)

    def test_outcome_template_contains_the_report_marker_exactly_once(self):
        """模板里多抄一处那行赋值（哪怕写在注释里）→ 所有报告的快照都读不出来。"""
        for path in (app.TEMPLATE_PATH, app.INTERVIEW_TEMPLATE_PATH, app.OUTCOME_TEMPLATE_PATH):
            text = store.read_text(path)
            self.assertEqual(text.count(kb.REPORT_MARK), 1,
                             "%s 里 %r 出现了 %d 次"
                             % (os.path.basename(path), kb.REPORT_MARK, text.count(kb.REPORT_MARK)))

    def test_kind_outcome_selects_the_outcome_template(self):
        self.assertEqual(app.report_template_for({"kind": "outcome"}), app.OUTCOME_TEMPLATE_PATH)

    def test_the_other_two_kinds_are_untouched(self):
        """老 JD 报告与面试复盘报告都不受影响（缺省仍 JD）。"""
        self.assertEqual(app.report_template_for({"kind": "interview"}), app.INTERVIEW_TEMPLATE_PATH)
        self.assertEqual(app.report_template_for({"kind": "jd"}), app.TEMPLATE_PATH)
        self.assertEqual(app.report_template_for({}), app.TEMPLATE_PATH)
        self.assertEqual(app.report_template_for(None), app.TEMPLATE_PATH)
        self.assertEqual(app.report_template_for({"kind": "something-else"}), app.TEMPLATE_PATH)

    def test_outcome_report_is_served_with_the_outcome_template(self):
        self._write_report("oc_x.html", {"kind": "outcome", "verdict_summary": "s"})
        code, body = self._get("/reports/oc_x.html")
        self.assertEqual(code, 200)
        self.assertIn(OUTCOME_TEMPLATE_MARKER, body)
        self.assertNotIn(JD_TEMPLATE_MARKER, body)
        self.assertNotIn(INTERVIEW_TEMPLATE_MARKER, body)
        self.assertNotIn("__REPORT_DATA__", body)
        self.assertNotIn("__TITLE__", body)

    def test_old_jd_and_interview_reports_still_use_their_own_templates(self):
        self._write_report("x_岗位.html", {"job_title": "岗"})
        code, body = self._get("/reports/" + quote("x_岗位.html"))
        self.assertEqual(code, 200)
        self.assertIn(JD_TEMPLATE_MARKER, body)
        self.assertNotIn(OUTCOME_TEMPLATE_MARKER, body)
        self._write_report("iv_9.html", {"kind": "interview", "summary": "s"})
        code, body = self._get("/reports/iv_9.html")
        self.assertEqual(code, 200)
        self.assertIn(INTERVIEW_TEMPLATE_MARKER, body)
        self.assertNotIn(OUTCOME_TEMPLATE_MARKER, body)

    def test_report_written_by_the_real_pipeline_is_the_outcome_template(self):
        """真实现写出来那份（不是手写的 <title>T</title>）也必须走结果复盘版式。

        `OUTCOME_TEMPLATE_MARKER` 是个**动态渲染**产物（卡片标题由 JS 拼），所以这里再钉一个
        **静态哨兵**：结果复盘模板头部那句 kicker 必须原样出现在文件里 —— 两份东西一起断言，
        才排除掉「标记恰好被别的模板凑出来」这种巧合。
        """
        self._job()
        self._post_json("/api/job/result", {"id": "j1", "status": "已拒", "reason": "预算冻结"})
        self._real_attribute(_fake_chat('{"verdict_summary":"总评独有句"}'), real_render=True)
        self._post_json("/api/job/attribute", {"id": "j1"})
        on_disk = store.read_text(self._report_path())
        self.assertIn(OUTCOME_TEMPLATE_MARKER, on_disk)                  # 动态产物
        self.assertIn(OUTCOME_TEMPLATE_STATIC, on_disk)                  # 静态骨架
        self.assertNotIn(JD_TEMPLATE_MARKER, on_disk)
        self.assertNotIn(INTERVIEW_TEMPLATE_MARKER, on_disk)
        self.assertIn("预算冻结", on_disk)              # 用户原话在磁盘文件里（file:// 也看得见）
        served = self._get("/reports/oc_j1.html")[1]
        self.assertIn(OUTCOME_TEMPLATE_MARKER, served)
        self.assertIn(OUTCOME_TEMPLATE_STATIC, served)
        self.assertNotIn(JD_TEMPLATE_MARKER, served)

    def test_missing_outcome_template_falls_back_to_the_raw_file(self):
        """结果复盘模板缺失 → 退回文件本身（与 JD/访谈同款兜底），绝不偷偷改用 JD 版式。"""
        path = self._write_report("oc_z.html", {"kind": "outcome", "verdict_summary": "s"})
        real = app.OUTCOME_TEMPLATE_PATH
        self.addCleanup(setattr, app, "OUTCOME_TEMPLATE_PATH", real)
        app.OUTCOME_TEMPLATE_PATH = os.path.join(store.p_path("reports"), "不存在.html")
        self.assertIsNone(app._render_report_page(path))
        h = _sandbox_handler("GET", "/reports/oc_z.html", b"")
        self.assertEqual(h._status, 200)
        self.assertEqual(h._sent_file, path)


# ------------------------------------------------- 分析前查重 + 台账过滤
# 「同一份 JD 被分析两次」会把同一个概念记成「被 2 个岗位提到」，而 multi_jd 是复习优先级的
# **乘性**因子。所以查重必须发生在**调模型/调搜索之前**：命中时一个额度都不花。
#
# ⚠️ 本文件最危险的假绿：_handle_post("/api/analyze") 自己 except Exception → **200**，
# do_POST 还有一层外层 try → **500**。只断言「body 里某个词」的话，实现被换成「抛异常」
# 照样成立（错误文本里那些词可能是异常类名）。所以每条用例都同时钉三样东西：
#   ① HTTP 状态码 ② duplicate 字段的**具体内容** ③ 假模型/假搜索的**调用计数**
class _AnalyzeRouteCase(IsolatedCase):
    """POST /api/analyze 的沙箱化测试：绝不真调模型、绝不真调搜索。"""

    LONG = ("数据质检验收把控：严格按评测标准对大模型语音通话数据质检验收；"
            "熟悉ASR与TTS评测流程；能定位Bad Case并做归因分析；有标注规范落地经验。")
    UNRELATED = "负责抖音电商直播运营，策划带货活动与达人矩阵，盯直播间转化率。"

    # 假模型回一份**最小合法**的分析结果。job_title 刻意与旧记录不同：
    # 「overwrite 到底写没写回那条记录」只能靠字段真的变了才证明得了（平局会掩盖）。
    REPLY = json.dumps({
        "job_title": "语音数据质检（改后）", "score": 91.5, "company": "",
        "verdict": "建议投递",
        "scores": [{"name": "匹配度", "score": 91.5, "weight": 10}],
    }, ensure_ascii=False)

    def _handler(self, method, path, body: bytes = b"", headers=None):
        h = _sandbox_handler(method, path, body, headers)
        return h._status, h._body

    def _post_json(self, path, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        return self._handler("POST", path, raw,
                             {"Content-Length": str(len(raw)),
                              "Content-Type": "application/json; charset=utf-8"})

    def _get(self, path):
        return self._handler("GET", path, b"", {})

    def _old_job(self, job_id="j-old", text=None, **kw):
        """已有的那条岗位 + 它的 JD 原文（阶段 4A 形状：JD 记录 + 分析记录 + jd/<id>.txt）。"""
        jd = {"id": job_id, "job_title": "语音/文本评测", "jd_text_file": job_id + ".txt",
              "salary": "13-26K", "company": "示例公司", "location": "北京",
              "job_category": "数据标注/质检", "status": "未投递"}
        rec = {"id": job_id, "jd_id": job_id, "report": job_id + ".html",
               "created_at": "2026-10-03 00:30", "score": 75.0, "resume": "旧简历"}
        for k, v in kw.items():
            if k in app.ANALYSIS_FIELDS:
                rec[k] = v
            else:
                jd[k] = v
        jdid.upsert_jd(jd)
        app._save_analysis(rec)
        store.ensure_profile()
        store.write_text(store.p_path("jd", job_id + ".txt"), text or self.LONG)
        return rec

    def _resume(self, name="我的简历.md"):
        store.ensure_profile()
        store.write_text(os.path.join(store.p_path("resumes"), name), "候选人简历正文")
        return name

    def _version(self, name="我的简历", content="候选人简历正文", **kw):
        """登记一份**真实**的简历版本，返回它的记录（`resume_id` 从修 4 起会被校验）。

        内容与 `_resume()` 写的逐字相同：版本记录的 sha 与盘上的文件保持一致。
        `parent_id` 走 `**kw`：族谱（母版 → 子版 → 孙版）那几条用例要真的续版。
        """
        store.ensure_profile()
        return resumelib.create_version(name, content, **kw)

    def _patch_analyze_deps(self):
        """把**真正花钱的三个入口**换成计数替身（先读 do_analyze 看清它调谁）：
        `llm.call_openai_compatible`（文本模型 + OCR 都走它）、`search.fetch_link`、
        `knowledge.build_for_jd`（阶段 1 的知识点抽取，也调模型）。
        """
        calls = {"llm": 0, "search": 0, "knowledge": 0}

        def fake_llm(base_url, api_key, model, messages, json_mode=False, **kw):
            calls["llm"] += 1
            return self.REPLY

        def fake_search(link):
            calls["search"] += 1
            # 抓回来的正文就是**同一份 JD**（否则命中的是 URL 而不是 JD，就测不到查重）
            return self.LONG

        def fake_knowledge(jd_text, cfg):
            calls["knowledge"] += 1
            return []

        self._patch(app.llm, "call_openai_compatible", fake_llm)
        self._patch(app.search_mod, "fetch_link", fake_search)
        self._patch(app.knowledge, "build_for_jd", fake_knowledge)
        # 真 repair_json 会走网络。干净 JSON 走不到它，但坏 JSON 的路径必须被挡住 ——
        # 挡住了才说明「模型替身真的被用上了」，而不是悄悄换了一条路。
        self._patch(app.llm, "repair_json",
                    mock.Mock(side_effect=AssertionError("测试里不该真去修 JSON")))
        return calls

    def _patch(self, obj, name, value):
        p = mock.patch.object(obj, name, value)
        p.start()
        self.addCleanup(p.stop)

    def _report(self, name):
        return os.path.join(store.p_path("reports"), name)


class AnalyzeDedupTest(_AnalyzeRouteCase):
    """§6 分析前查重：409 命中 / allow_duplicate 放行 / overwrite 写回原记录 / 非法 overwrite 400。"""

    def test_duplicate_returns_409_without_calling_the_model_or_search(self):
        # 阶段 4C：查重粒度 = (JD 指纹 × **简历版本**)。所以"已有那条分析"与"这次提交"都必须
        # 带上**同一个** `resume_id` 才叫重复（老数据没有 `resume_id` → 不判重复，规格 §6.2）。
        # id 必须是**真版本**：修 4 起 `resume_id` 会先查存在（否则 400）。
        v = self._version("旧简历")
        self._old_job(resume_id=v["id"])
        calls = self._patch_analyze_deps()
        code, body = self._post_json("/api/analyze",
                                     {"jd_text": self.LONG, "source_type": "粘贴文本",
                                      "resume_id": v["id"]})
        # ① 状态码：409（不是 200 的 {"ok":false,"error":...}，更不是 500）
        self.assertEqual(code, 409)
        # ② 响应体的**具体内容**（不是「error 里有某个词」）
        self.assertFalse(body.get("ok"))
        dup = body.get("duplicate")
        self.assertIsInstance(dup, dict)
        self.assertEqual(dup["job_id"], "j-old")
        self.assertEqual(dup["job_title"], "语音/文本评测")
        self.assertEqual(dup["created_at"], "2026-10-03 00:30")
        self.assertEqual(dup["score"], 75.0)
        self.assertEqual(dup["suggestions"], ["overwrite", "new"])
        self.assertGreaterEqual(dup["similarity"], 0.85)
        self.assertLessEqual(dup["similarity"], 1.0)
        # ③ 一个额度都没花：**在分析之前就返回**
        self.assertEqual(calls["llm"], 0)
        self.assertEqual(calls["search"], 0)
        self.assertEqual(calls["knowledge"], 0)
        # 台账没被动过（没多出第二个岗位）
        self.assertEqual([j["id"] for j in store.load_jobs()], ["j-old"])

    def test_a_genuinely_different_jd_is_not_a_duplicate(self):
        """反向：无关 JD 必须照常分析（否则「命中」就成了恒真）。"""
        self._old_job()
        calls = self._patch_analyze_deps()
        code, body = self._post_json("/api/analyze",
                                     {"jd_text": self.UNRELATED, "resume_name": self._resume(),
                                      "source_type": "粘贴文本"})
        self.assertEqual(code, 200)
        self.assertTrue(body.get("ok"), body)
        self.assertGreaterEqual(calls["llm"], 1)

    def test_allow_duplicate_proceeds_to_analyze(self):
        self._old_job()
        calls = self._patch_analyze_deps()
        code, body = self._post_json("/api/analyze",
                                     {"jd_text": self.LONG, "resume_name": self._resume(),
                                      "source_type": "粘贴文本", "allow_duplicate": True})
        self.assertNotEqual(code, 409)
        self.assertEqual(code, 200)
        self.assertTrue(body.get("ok"), body)
        self.assertGreaterEqual(calls["llm"], 1)          # 放行 = 真的走了分析
        self.assertEqual(len(store.load_jobs()), 2)       # 「仍然新建」= 真的多了一条

    def test_overwrite_reuses_the_existing_job_id(self):
        self._old_job()
        calls = self._patch_analyze_deps()
        code, body = self._post_json("/api/analyze",
                                     {"jd_text": self.LONG, "resume_name": self._resume(),
                                      "source_type": "粘贴文本", "overwrite": "j-old"})
        self.assertEqual(code, 200)
        self.assertTrue(body.get("ok"), body)
        self.assertGreaterEqual(calls["llm"], 1)          # 分析照跑
        jobs = {j["id"]: j for j in store.load_jobs()}
        # 没新建第二个：id 前缀匹配的只有一条，且**没有**任何新 id
        self.assertEqual(len([i for i in jobs if i.startswith("j-old")]), 1)
        self.assertEqual(list(jobs), ["j-old"])
        self.assertEqual(body["report"], "j-old.html")
        # 结果**写回了那条已有记录**：分析级字段在 jobs.json，JD 级字段在 jds.json
        # （只断言「记录还在」会被平局掩盖）
        self.assertEqual(jobs["j-old"]["report"], "j-old.html")           # 报告文件名 == 原 id
        self.assertEqual(jobs["j-old"]["score"], 91.5)                    # 新分数写上去了
        self.assertEqual(jobs["j-old"]["created_at"], "2026-10-03 00:30")  # 原始时间不丢
        self.assertEqual(jobs["j-old"]["jd_id"], "j-old")                 # 还挂在同一个 JD 下
        # JD 级字段（阶段 4A 之后只住在 jds.json）
        jd = jdid.find_jd("j-old")
        self.assertEqual(jd["job_title"], "语音数据质检（改后）")
        self.assertNotIn("job_title", jobs["j-old"], "JD 级字段漏回了 jobs.json")
        # JD 也是唯一那一个：覆盖不该建出第二个 JD
        self.assertEqual([j["id"] for j in jdid.load_jds()["jds"]], ["j-old"])
        # 报告文件真的落在那个文件名上，且内容是新分析（不是旧文件没被覆盖）
        on_disk = store.read_text(self._report("j-old.html"))
        self.assertIn("语音数据质检（改后）", on_disk)
        # 没有第二个报告文件
        html = sorted(f for f in os.listdir(store.p_path("reports")) if f.endswith(".html"))
        self.assertEqual(html, ["j-old.html"])

    def test_overwrite_with_unknown_or_unrelated_id_is_400(self):
        self._old_job()
        calls = self._patch_analyze_deps()
        for bad, jd in (("不存在", self.LONG),                 # 台账里没有这个 id
                        ("j-old", self.UNRELATED)):            # id 在，但这份 JD 与它不相似
            with self.subTest(bad=bad, jd=jd[:8]):
                code, body = self._post_json("/api/analyze",
                                             {"jd_text": jd, "resume_name": self._resume(),
                                              "overwrite": bad})
                self.assertEqual(code, 400)
                self.assertFalse(body.get("ok"))
                self.assertNotIn("duplicate", body)
        # 400 也发生在分析**之前**：一个额度都没花
        self.assertEqual(calls["llm"], 0)
        self.assertEqual(calls["search"], 0)
        self.assertEqual(len(store.load_jobs()), 1)          # 没写坏台账

    def test_overwrite_may_not_target_an_already_merged_job(self):
        """已合并的记录只是标记：结果写上去等于写进一条「不在台账里」的记录。"""
        self._old_job("k")
        self._old_job("d", **{"merged_into": "k"})
        calls = self._patch_analyze_deps()
        code, body = self._post_json("/api/analyze",
                                     {"jd_text": self.LONG, "resume_name": self._resume(),
                                      "overwrite": "d"})
        self.assertEqual(code, 400)
        self.assertEqual(calls["llm"], 0)

    def test_duplicate_detection_also_runs_for_linked_jds(self):
        """带 link 也在分析之前查重：抓正文（1 次，这一步跑不掉）之后立刻命中，**模型 0 次**。"""
        v = self._version("旧简历")
        self._old_job(text=self.LONG, resume_id=v["id"])
        calls = self._patch_analyze_deps()
        code, body = self._post_json("/api/analyze",
                                     {"link": "https://example.com/jd", "resume_id": v["id"]})
        self.assertEqual(code, 409)
        self.assertEqual(body["duplicate"]["job_id"], "j-old")
        self.assertEqual(calls["search"], 1)          # 抓正文那次已经发生（命中是在拿到文本之后）
        self.assertEqual(calls["llm"], 0)             # 但最贵的模型一次都没调
        self.assertEqual(calls["knowledge"], 0)


class AnalyzeFlagTruthinessTest(_AnalyzeRouteCase):
    """`allow_duplicate` / `refresh_jd_facts` 的真值口径**只有一处**（`app._as_bool`）。

    手写 API（curl / 自己的脚本 / 别的客户端）最容易把布尔写成字符串：
    `{"refresh_jd_facts": "false"}`。改之前两个参数都直接吃 Python 的 `bool(value)` ——
    `bool("false") is True` → **白烧一次真算**（206 秒 + ~53 次搜索），
    `{"allow_duplicate": "false"}` 同理 → 悄悄跳过查重。

    **现状钉住**（改之前逐个输入实测）：两个参数都是 `bool(value)` ——
      为真：`True / 1 / "1" / "true" / "yes" / "false" / "0" / "no" / "   " / "maybe" / 2 / [1]`
      为假：`False / 0 / 0.0 / None / "" / [] / {}`
    改之后**只有「字符串假值」这一类翻面**：`"false" / "0" / "no"`（含大小写、首尾空白、
    以及纯空白串）；认不出的（`"maybe" / 2 / [1]`）保持原语义 —— 把 `2` 从真改成假同样是
    行为变化，会打破「只改一类」这条纪律。见下面 `FALSY` / `TRUTHY` 两张表的差集。
    """

    NODE = {"id": "n1", "term": "语音识别", "definition": "d", "layer": 0, "sources": [],
            "timeline": [], "related": [], "interview_questions": [], "confidence": "guess"}

    # ⚠️ **现状钉住用的两张表**（改之前的口径 = `bool(value)`）。它们留在这里是为了让
    # 「只改了字符串假值这一类」这句话**可被测试证明**（见 test_only_string_false_values_flipped）。
    OLD_FALSY = (None, False, 0, 0.0, "", [], {})
    OLD_TRUTHY = (True, 1, "1", "true", "yes", "false", "0", "no", "   ", "FALSE", " false ",
                  " no ", "maybe", 2, [1])

    # 改**之后**的口径（`_as_bool`）：差集只有 `"0" / "false" / "no"`（含大小写与空白变体）。
    FALSY = OLD_FALSY + ("0", "false", "no", "FALSE", " no ", "   ")
    TRUTHY = (True, 1, "1", "true", "TRUE", " yes ", "maybe", 2, [1], 0.5)

    def _deps(self):
        """三个花钱入口的计数替身。知识地图回**非空**节点：空列表不算可用缓存
        （`jdfacts._as_nodes(...) or None`），那种替身会让「有没有重算」永远分不出来（假绿）。
        """
        calls = {"llm": 0, "search": 0, "knowledge": 0}

        def fake_llm(base_url, api_key, model, messages, json_mode=False, **kw):
            calls["llm"] += 1
            return self.REPLY

        def fake_search(link):
            calls["search"] += 1
            return self.LONG

        def fake_knowledge(jd_text, cfg):
            calls["knowledge"] += 1
            return [dict(self.NODE)]

        self._patch(app.llm, "call_openai_compatible", fake_llm)
        self._patch(app.search_mod, "fetch_link", fake_search)
        self._patch(app.knowledge, "build_for_jd", fake_knowledge)
        self._patch(app.llm, "repair_json",
                    mock.Mock(side_effect=AssertionError("测试里不该真去修 JSON")))
        return calls

    def _prime(self):
        """先真算一次把缓存落上（没有缓存时「真假」都只是「没缓存要算」，分不出真值口径）。"""
        calls = self._deps()
        code, body = self._post_json("/api/analyze",
                                     {"jd_text": self.LONG, "resume_name": self._resume(),
                                      "source_type": "粘贴文本"})
        self.assertEqual((code, body.get("ok")), (200, True), body)
        self.assertEqual(calls["knowledge"], 1)
        return calls

    def _refresh_body(self, value, resume="第二版.md"):
        return {"jd_text": self.LONG, "resume_name": self._resume(resume),
                "source_type": "粘贴文本", "allow_duplicate": True,
                "refresh_jd_facts": value}

    def test_as_bool_is_the_single_truthiness_judgement(self):
        """`_as_bool` 的真值表（逐个输入，含垃圾值）。"""
        table = [(True, True), (1, True), ("1", True), ("true", True), ("TRUE", True),
                 (" yes ", True), ("yes", True),
                 (False, False), (0, False), (0.0, False), ("0", False), ("false", False),
                 ("FALSE", False), (" false ", False), ("no", False), (" no ", False),
                 (None, False), ("", False), ("   ", False), ([], False), ({}, False),
                 # 认不出的：按**原语义** `bool(value)`（只有字符串假值这一类翻面）
                 ("maybe", True), (2, True), ([1], True), (0.5, True)]
        for value, want in table:
            with self.subTest(value=value):
                self.assertIs(app._as_bool(value), want)

    def test_only_string_false_values_flipped(self):
        """**现状 → 现在**的差集必须**只有字符串假值**这一类（既有语义不能悄悄翻面）。

        两个都漏过：把 `2` 从真改成假（「垃圾值也一律当假」）同样是一种行为变化 —— 那会把
        手写客户端里 `refresh_jd_facts: 2` 的意思从「刷新」改成「不刷新」。
        """
        flipped = [(v, bool(v)) for v in self.OLD_FALSY + self.OLD_TRUTHY
                   if app._as_bool(v) is not bool(v)]
        self.assertEqual([v for v, _ in flipped],
                         ["false", "0", "no", "   ", "FALSE", " false ", " no "],
                         "翻面的不止「字符串假值」这一类：%r" % (flipped,))
        for value, before in flipped:
            self.assertIsInstance(value, str, "非字符串的值也翻面了：%r" % (value,))
            self.assertIn(value.strip().lower(), ("", "0", "false", "no"))

    def test_refresh_jd_facts_falsy_values_reuse_the_cache(self):
        """手写 API 的 `"refresh_jd_facts": "false"` 曾经 = **真** → 白烧一次真算。"""
        calls = self._prime()
        for value in self.FALSY:
            with self.subTest(value=value):
                before = calls["knowledge"]
                code, body = self._post_json("/api/analyze", self._refresh_body(value))
                self.assertEqual((code, body.get("ok")), (200, True), body)
                self.assertEqual(calls["knowledge"], before,
                                 "`refresh_jd_facts`=%r 被当成了真 → 白烧 206 秒" % (value,))

    def test_refresh_jd_facts_truthy_values_recompute(self):
        calls = self._prime()
        for value in self.TRUTHY:
            with self.subTest(value=value):
                before = calls["knowledge"]
                code, body = self._post_json("/api/analyze", self._refresh_body(value))
                self.assertEqual((code, body.get("ok")), (200, True), body)
                self.assertEqual(calls["knowledge"], before + 1,
                                 "`refresh_jd_facts`=%r 没被当成真（刷新没透传）" % (value,))

    def test_allow_duplicate_falsy_values_still_hit_the_duplicate_check(self):
        """`"allow_duplicate": "false"` 曾经 = **真** → 悄悄跳过查重（该 409 的没 409）。"""
        # 阶段 4C：判重复还要看简历版本 —— 这条用例喂的是**同一版**（同一个真版本 id），
        # 所以"该 409 的"仍然是 409，测的还是 `allow_duplicate` 的真值口径。
        # 版本名与下面提交的 `resume_name` 必须**同一份**（修 8 起 resume_id 要属于被分析的那份
        # 简历，否则 400）：这里用的是 `self._resume()` 默认的「我的简历.md」。
        v = self._version("我的简历")
        self._old_job(resume_id=v["id"])
        calls = self._deps()
        for value in self.FALSY:
            with self.subTest(value=value):
                code, body = self._post_json("/api/analyze",
                                             {"jd_text": self.LONG, "resume_name": self._resume(),
                                              "source_type": "粘贴文本", "allow_duplicate": value,
                                              "resume_id": v["id"]})
                self.assertEqual(code, 409, "`allow_duplicate`=%r 被当成了真" % (value,))
                self.assertEqual(body["duplicate"]["job_id"], "j-old")
                # 查重命中时**一个额度都不花**（这批假值全都在调模型/搜索之前返回）
                self.assertEqual((calls["llm"], calls["search"], calls["knowledge"]), (0, 0, 0))

    def test_allow_duplicate_truthy_values_proceed(self):
        self._old_job()
        calls = self._deps()
        for value in self.TRUTHY:
            with self.subTest(value=value):
                before = calls["llm"]
                code, body = self._post_json("/api/analyze",
                                             {"jd_text": self.LONG, "resume_name": self._resume(),
                                              "source_type": "粘贴文本", "allow_duplicate": value})
                self.assertEqual((code, body.get("ok")), (200, True), body)
                self.assertGreater(calls["llm"], before, "`%r` 没有被放行" % (value,))


class LedgerFilterTest(_AnalyzeRouteCase):
    """§4：`merged_into` 非空的记录只是标记，不该再出现在台账列表里。"""

    def test_merged_jobs_are_hidden_from_the_ledger(self):
        self._old_job("k")
        self._old_job("d", merged_into="k")
        code, body = self._get("/api/jobs")
        self.assertEqual(code, 200)
        self.assertEqual([j["id"] for j in body["jobs"]], ["k"])     # 细到「个体」那一层
        # 反证：过滤不是靠「整个列表被清空」蒙对的
        self.assertNotIn("d", [j["id"] for j in body["jobs"]])
        # 记录本身**没被删**（只是打了标记），报告也还在
        self.assertTrue(os.path.exists(store.jobs_path()))
        self.assertIn("d", [j.get("id") for j in store.load_jobs()])

    def test_an_empty_merged_into_string_does_not_hide_a_record(self):
        """手改出的 `merged_into: ""` 是「没合并」，不能被当成合并过的（否则记录会凭空消失）。"""
        self._old_job("k")
        self._old_job("e", merged_into="")
        code, body = self._get("/api/jobs")
        self.assertEqual(code, 200)
        self.assertEqual(sorted(j["id"] for j in body["jobs"]), ["e", "k"])

    def test_job_update_refuses_a_merged_record(self):
        """台账里看不到的记录，写接口也不能悄悄改它。"""
        self._old_job("k")
        self._old_job("d", merged_into="k")
        code, body = self._post_json("/api/jobs/update", {"id": "d", "job_title": "改一下"})
        self.assertEqual(code, 404)
        self.assertFalse(body.get("ok"))
        self.assertEqual(jdid.find_jd("d")["job_title"], "语音/文本评测")   # 盘上确实没被改


# ⚠️ 本块的核心不变量：**同一份 JD 分析两次 → 只有 1 个 JD、2 份分析**。
# 「报告文件名 = 岗位身份」那个根因（文件名里嵌日期+分数）就是靠它根除的。
# 断言必须细到「个体」那一层：逐条断言 jd_id 相同、jds.json 里只有 1 条 ——
# 只断言「分析数是 2」会被「两个 JD 各挂一份」蒙混过去（那正是要防的旧行为）。
class JdIdentityTest(_AnalyzeRouteCase):
    """阶段 4A：新分析生成/复用 JD 并把分析挂在它下面（`app.do_analyze` 的接线）。"""

    # 假模型回一份**字段齐全**的 JD 分析结果（ID 的六段要从这里派生）。
    # 薪资用带括号的长表述：ID 里该是 `6-8K`，JD 记录里该是**原文**（Task 1 交接注意 1）。
    FULL = json.dumps({
        "job_title": "视频标注质检", "score": 82.0, "verdict": "建议投递",
        "company": "四川金沙人力资源开发管理有限公司（金沙人力）",
        "education": "统招公办本科及以上",
        "job_category": "数据标注/质检", "location": "成都",
        "address": "成都武侯区瑞鑫时代大厦-A座",
        "salary": "6-8K（入职五险，提及一金7k+）",
        "application": {"status": "未投递", "notes": "先看评测集"},
        "scores": [{"name": "匹配度", "score": 82.0, "weight": 10}],
    }, ensure_ascii=False)

    def _patch_full_model(self, reply=None):
        calls = self._patch_analyze_deps()
        payload = reply or self.FULL

        def fake_llm(base_url, api_key, model, messages, json_mode=False, **kw):
            calls["llm"] += 1
            return payload

        self._patch(app.llm, "call_openai_compatible", fake_llm)
        return calls

    def _analyze(self, jd_text=None, resume="我的简历.md", **extra):
        body = {"jd_text": jd_text or self.LONG, "resume_name": self._resume(resume)}
        body.update(extra)
        return self._post_json("/api/analyze", body)

    def test_a_new_analysis_creates_a_jd_and_hangs_the_analysis_under_it(self):
        self._patch_full_model()
        code, body = self._analyze()
        self.assertEqual(code, 200, body)
        self.assertTrue(body.get("ok"), body)
        store.ensure_profile()

        jds = jdid.load_jds()["jds"]
        self.assertEqual(len(jds), 1)
        jd = jds[0]
        # ① ID 是**归一化后**的六段 + 4 位编号
        fields, seq = jdid.parse_id(jd["id"])
        self.assertEqual(fields["education"], "本科")
        self.assertEqual(fields["company"], "金沙人力")
        self.assertEqual(fields["salary"], "6-8K")
        self.assertEqual(fields["city"], "成都")
        self.assertEqual(fields["domain"], "视频")
        self.assertEqual(seq, 1)
        # ② JD 记录里的**展示字段是原始值**（不是归一化后的短值）
        self.assertEqual(jd["salary"], "6-8K（入职五险，提及一金7k+）")
        self.assertEqual(jd["company"], "四川金沙人力资源开发管理有限公司（金沙人力）")
        self.assertEqual(jd["job_title"], "视频标注质检")
        self.assertEqual(jd["status"], "未投递")
        self.assertEqual(jd["notes"], "先看评测集")
        self.assertEqual(jd["verdict"], "建议投递")
        self.assertTrue(jd["jd_text_file"])
        self.assertEqual(jd["seq"], 1)
        # ③ 分析记录只留分析级字段 + jd_id
        rec = store.find_job(jd["id"] + "")       # 分析 id 未知，先按 jd_id 找
        if rec is None:
            rec = [j for j in store.load_jobs() if j.get("jd_id") == jd["id"]][0]
        self.assertEqual(rec["jd_id"], jd["id"])
        self.assertEqual(rec["score"], 82.0)
        self.assertEqual(rec["resume"], "我的简历")
        self.assertEqual(rec["report"], body["report"])
        for stolen in ("job_title", "company", "salary", "status", "notes", "verdict",
                       "job_category", "location", "source_type", "jd_text_file"):
            self.assertNotIn(stolen, rec, "JD 级字段 %s 漏回了 jobs.json" % stolen)

    def test_two_analyses_of_the_same_jd_share_one_jd(self):
        """**核心不变量**：同一份 JD 两次分析 → 1 个 JD、2 份分析、两个 jd_id 相同、seq 不涨。

        第二次带 `allow_duplicate`（用户在查重弹窗里选了「仍然新建」）—— 那是**跳过一次查重提示**，
        **不是**「再建一个岗位身份」：`allow_duplicate` 只影响「要不要弹窗」，JD 身份照样由
        指纹决定（这正是本阶段的根因修复）。
        """
        self._patch_full_model()
        code1, body1 = self._analyze(resume="第一版.md")
        self.assertEqual(code1, 200, body1)
        # 第二份简历：文字略有出入（多一句），指纹仍然命中 → **必须复用**而不是新建
        code2, body2 = self._analyze(jd_text=self.LONG + "加分项：有 TTS 评测集搭建经验。",
                                     resume="第二版.md", allow_duplicate=True)
        self.assertEqual(code2, 200, body2)

        jds = jdid.load_jds()
        self.assertEqual(len(jds["jds"]), 1, "同一份 JD 绝不能建出第二个 JD")
        self.assertEqual(jds["seq"], 1, "复用已有 JD 时不该再消耗一个编号")
        jd_id = jds["jds"][0]["id"]

        jobs = store.load_jobs()
        self.assertEqual(len(jobs), 2, "两次分析 = 两份分析记录")
        # ⚠️ 逐条断言 jd_id 相同（不能只断言「分析数是 2」）
        self.assertEqual([j["jd_id"] for j in jobs], [jd_id, jd_id])
        self.assertEqual([j["resume"] for j in jobs], ["第二版", "第一版"])   # jobs.json 新的在前
        self.assertEqual(sorted(j["score"] for j in jobs), [82.0, 82.0])
        # 报告是两份（每份分析一个），但**JD 只有一个**
        self.assertNotEqual(body1["report"], body2["report"])
        self.assertEqual(sorted(f for f in os.listdir(store.p_path("reports"))
                                if f.endswith(".html")), sorted([body1["report"], body2["report"]]))

    def test_a_genuinely_different_jd_gets_its_own_jd(self):
        """反向：无关 JD 必须建出**第二个** JD（否则「复用」就成了恒真）。"""
        self._patch_full_model()
        self.assertEqual(self._analyze()[0], 200)
        self._patch_analyze_deps()
        self._patch(app.llm, "call_openai_compatible",
                    lambda *a, **kw: json.dumps({
                        "job_title": "抖音直播运营", "score": 40.0,
                        "job_category": "其他", "location": "上海", "salary": "10-15K",
                    }, ensure_ascii=False))
        code, body = self._analyze(jd_text=self.UNRELATED)
        self.assertEqual(code, 200, body)
        jds = jdid.load_jds()
        self.assertEqual(len(jds["jds"]), 2)
        self.assertEqual(jds["seq"], 2)

    def test_the_knowledge_base_points_at_the_jd_id_not_the_report_name(self):
        """知识库 `from_jds[].id` 必须是 **JD id** —— 指向报告文件名等于把身份绑回日期+分数。"""
        self._patch_analyze_deps()
        self._patch(app.knowledge, "build_for_jd",
                    lambda jd_text, cfg: [{"term": "RLHF", "definition": "人类反馈强化学习"}])
        self._patch(app.llm, "call_openai_compatible", lambda *a, **kw: self.FULL)
        code, body = self._analyze()
        self.assertEqual(code, 200, body)
        kb_data = store.load_knowledge()
        cards = [n for n in kb_data["nodes"] if kb.normalize_id(n.get("term")) == kb.normalize_id("RLHF")]
        self.assertEqual(len(cards), 1)
        ids = [x.get("id") for x in cards[0]["from_jds"]]
        jd_id = jdid.load_jds()["jds"][0]["id"]
        self.assertEqual(ids, [jd_id])
        report_id = body["report"][:-5]
        self.assertNotIn(report_id, ids, "from_jds 指到了报告文件名（身份又绑回日期+分数）")


# ------------------------------------------------- 阶段 E：知识库优先复用（接上总闸）
# 前面几条 Task 只把 `kbreuse` 与 `knowledge.build_for_jd(reuse=…)` 建好，**默认关闭**。
# 这一组钉的是「总闸真的接上了」：`do_analyze` 必须把 `Reuse` 注入那条 builder 缝，
# 并把统计带进报告元数据。三条纪律（每条都对着一种假绿）：
#   ① 「复用了」必须同时断言**那一次联网搜索没发生** —— 只断言"报告里有卡片内容"是恒真的；
#   ② 库坏了必须**如实说**（`kb_reuse.knowledge_error` 非空），不许假装一切正常；
#   ③ 缓存命中时**根本没跑**库优先 —— 不许写「复用了 0 条」（那是在说假话）。
class AnalyzeKbReuseTest(_AnalyzeRouteCase):
    """`do_analyze` 的 `kbreuse.Reuse` 接线：闭包注入 + 统计进 `knowledge_meta`。"""

    # 抽出来的两个术语：「语音识别」库里有**够格**的整条卡；另一个库里没有 → 必须照旧联网。
    REUSABLE_TERM = "语音识别"
    MISS_TERM = "完全没见过的概念"

    def setUp(self):
        super().setUp()
        store.ensure_profile()
        self.searches = []      # 每次联网搜索的 query
        self.judged = []        # 每次知识库判定（默认配置没有 api_key → 一次都不该有）
        # 真 `knowledge.build_for_jd`（本组要测的正是它的注入点）→ 三个花钱入口全是替身。
        self._patch(app.search_mod, "search", self._fake_search)
        self._patch(app.llm, "call_openai_compatible", self._fake_chat)
        self._patch(app.llm, "repair_json",
                    mock.Mock(side_effect=AssertionError("测试里不该真去修 JSON")))

    # ---------------- 夹具
    def _card(self):
        """库里那张**够格**的卡：定义 + 通俗解释 + ≥1 条带 url 的来源（+时间线，省掉 ④）。"""
        return {"id": kb.normalize_id(self.REUSABLE_TERM), "term": self.REUSABLE_TERM,
                "definition": "库里的定义", "plain_explanation": "库里的通俗解释",
                "category": "核心概念", "layer": 0,
                "sources": [{"title": "维基", "url": "https://w"}],
                "timeline": [{"year": "2010", "text": "库里的时间线"}],
                "from_jds": [{"id": "old", "job_title": "老岗位", "date": "2025-01-02"}]}

    def _fake_search(self, query, providers=None, **kw):
        self.searches.append(query)
        return [{"title": "搜索结果", "url": "https://x", "snippet": "大模型 语音 标注"}]

    def _fake_chat(self, base_url, api_key, model, messages, json_mode=False, **kw):
        """按系统提示词**分派**：do_analyze 自己的 SOP 分析 / ①拆解 / ②头脑风暴 / ④时间线 / 判定。

        为什么必须分派：这几步走同一个模型入口，回错形状会让被测那一步**静默降级** ——
        那样断言就变成"什么也没测"。
        """
        sys_prompt = messages[0]["content"]
        if "求职顾问" in sys_prompt:                       # do_analyze 的 SOP 分析
            return self.REPLY
        if "从岗位 JD 中抽出" in sys_prompt:                # 知识地图 ① 拆解
            return json.dumps({"nodes": [
                {"id": "a", "term": self.REUSABLE_TERM, "definition": "本次模型写的定义",
                 "plain_explanation": "本次模型写的通俗", "category": "核心概念"},
                {"id": "b", "term": self.MISS_TERM, "definition": "x",
                 "plain_explanation": "y", "category": "核心概念"}]}, ensure_ascii=False)
        if "知识网络构建专家" in sys_prompt:                # ② 头脑风暴
            return json.dumps({"related": []}, ensure_ascii=False)
        if "技术史专家" in sys_prompt:                      # ④ 时间线
            return json.dumps({"timeline": [{"year": "2020", "text": "补算的时间线"}]},
                              ensure_ascii=False)
        if "知识库管理员" in sys_prompt:                    # 知识库判定
            self.judged.append(messages)
            return json.dumps({"pairs": []}, ensure_ascii=False)
        raise AssertionError("假模型收到了认不出的提示词：%s" % sys_prompt[:60])

    def _data_of(self, report):
        data = kb.read_report_data(os.path.join(store.p_path("reports"), report))
        self.assertIsInstance(data, dict, "报告 %s 里抽不出 REPORT_DATA" % report)
        return data

    def _kb_reuse_of(self, report):
        meta = self._data_of(report).get("knowledge_meta")
        self.assertIsInstance(meta, dict, "报告 %s 里没有 knowledge_meta" % report)
        reuse = meta.get("kb_reuse")
        self.assertIsInstance(reuse, dict, "报告元数据里没有 kb_reuse 统计：%r" % (meta,))
        return reuse

    def _run(self, resume=None, **extra):
        body = {"jd_text": self.LONG, "source_type": "粘贴文本",
                "resume_name": self._resume(resume or "我的简历.md")}
        body.update(extra)
        code, rbody = self._post_json("/api/analyze", body)
        self.assertEqual((code, rbody.get("ok")), (200, True), rbody)
        self.assertNotIn("knowledge_error", self._data_of(rbody["report"]),
                         "知识地图整步失败了：%r" % (rbody["report"],))
        return rbody["report"]

    # ---------------- ① 够格的卡：整条复用 + 那次搜索真的被省下
    def test_a_reusable_card_is_used_and_that_term_is_never_searched(self):
        store.save_knowledge({"nodes": [self._card()]})
        report = self._run()
        reuse = self._kb_reuse_of(report)
        self.assertGreaterEqual(reuse["reusable"], 1, "库里那张完整的卡没被复用：%r" % (reuse,))
        self.assertEqual(reuse["knowledge_error"], "", "库是好的，不该有 error")
        # 被复用的那个术语**一次都没搜**（本功能的全部意义，spec §10.3）
        self.assertEqual([q for q in self.searches if self.REUSABLE_TERM in q], [],
                         "够格复用就不该再联网搜：%r" % (self.searches,))
        # 反向：没命中的那个**照旧联网** —— 否则"没搜"可能只是因为压根没搜过任何东西
        self.assertTrue([q for q in self.searches if self.MISS_TERM in q],
                        "没命中的概念必须照旧搜（否则上面那条是假绿）：%r" % (self.searches,))
        # 默认 profile 的 text_model 没有 api_key → 那次"模型判定"一次都不该发生（省额度）
        self.assertEqual(self.judged, [], "没有可用文本模型时不该发起判定调用")
        # 报告节点里读到的必须是**库里那份**内容（不是本次模型的输出）
        nodes = {n["term"]: n for n in self._data_of(report)["knowledge"]}
        self.assertEqual(nodes[self.REUSABLE_TERM]["definition"], "库里的定义")
        self.assertEqual(nodes[self.REUSABLE_TERM]["timeline"],
                         [{"year": "2010", "text": "库里的时间线"}], "库里有时间线 → ④ 也不许跑")

    def test_a_builder_that_only_accepts_the_old_two_arguments_still_works(self):
        """`jdfacts` 那条缝的**老契约**就是 `builder(jd_text, cfg)`（两个位置参数）。

        注入的替身/别的实现只会收这两个参数 —— 硬塞一个 `reuse=` 会让它们 `TypeError`
        （那是替身没跟上新签名，不是被测代码该出的错）。所以接线必须**看签名**传参。
        同时钉住：替身收不到 `reuse` 时，报告里**不许编**一份统计出来。
        """
        calls = []

        def old_builder(jd_text, cfg):
            calls.append(jd_text)
            return [{"id": "x", "term": "RLHF", "definition": "d", "layer": 0,
                     "sources": [], "timeline": [], "related": [], "interview_questions": [],
                     "confidence": "ai-generated"}]

        self._patch(app.knowledge, "build_for_jd", old_builder)
        report = self._run()
        self.assertEqual(calls, [self.LONG], "两参数的老 builder 没被正常调到（签名适配没生效）")
        self.assertEqual(self._kb_reuse_of(report),
                         {"not_run": True, "source": jdfacts.SOURCE_COMPUTED},
                         "库优先根本没跑 → 说「没跑」+ 真实来源，不许编 reusable: 0 这种假统计")

    def test_the_real_builder_receives_reuse_as_a_keyword(self):
        """反向（追加 A）：**收 `reuse` 的** builder 必须真的收到它 —— 参数得是关键字。

        `jdfacts._compute_once` 用 `builder(jd_text, cfg)` 两个位置参数调；`reuse` 只能靠
        `_builder_with_reuse` 的闭包**按关键字**传进去。这条直接盯住那个关键字本身
        （只看报告里"复用了 N 条"是间接证据：`reuse.used` 还可能因为别的路径被置上）。
        """
        seen = []

        def builder_that_accepts_reuse(jd_text, cfg, reuse=None):
            seen.append({"reuse": reuse, "jd_text": jd_text})
            return [{"id": "x", "term": "RLHF", "definition": "d", "layer": 0,
                     "sources": [], "timeline": [], "related": [], "interview_questions": [],
                     "confidence": "ai-generated"}]

        self._patch(app.knowledge, "build_for_jd", builder_that_accepts_reuse)
        report = self._run()
        self.assertEqual(len(seen), 1, "builder 没被调到")
        self.assertIsInstance(seen[0]["reuse"], kbreuse.Reuse,
                              "`reuse` 没按关键字传进 builder（功能会静默失效）")
        self.assertEqual(seen[0]["jd_text"], self.LONG, "JD 原文要原样喂进去")
        # builder 自己没跑库优先 → 这次就是「没跑」，来源是 computed（真的算了一份）
        self.assertEqual(self._kb_reuse_of(report),
                         {"not_run": True, "source": jdfacts.SOURCE_COMPUTED})

    # ---------------- ② 库坏了：分析照做，但坦白说清楚
    def test_a_broken_knowledge_base_is_reported_not_papered_over(self):
        with open(store.knowledge_path(), "wb") as f:
            f.write(b'{"nodes": [ <<<CORRUPT>>>')
        report = self._run()                     # 分析照常出（HTTP 200 / ok:true）
        reuse = self._kb_reuse_of(report)
        self.assertTrue(reuse["knowledge_error"], "坏库必须如实说 —— 不许假装一切正常（spec §9）")
        self.assertIn("knowledge.json", reuse["knowledge_error"])
        self.assertEqual(reuse["reusable"], 0)
        self.assertTrue(self.searches, "库读不出来 → 全部退回联网（一个都不能省）")

    # ---------------- ③ 缓存命中：这次**根本没跑**库优先
    def test_a_cached_map_does_not_claim_zero_reuse(self):
        store.save_knowledge({"nodes": [self._card()]})
        first = self._run()
        self.assertGreaterEqual(self._kb_reuse_of(first)["reusable"], 1, "夹具前提：第一次真的复用了")
        searches_before = len(self.searches)
        second = self._run(resume="第二版.md", allow_duplicate=True)
        reuse = self._kb_reuse_of(second)
        self.assertEqual(self._data_of(second)["knowledge_meta"]["source"], "cache",
                         "夹具前提：第二次真的走了缓存")
        self.assertEqual(reuse, {"not_run": True, "source": "cache"},
                         "缓存命中时这次**根本没跑**库优先 —— 不许写 reusable: 0 这种假话")
        self.assertEqual(len(self.searches), searches_before, "复用缓存时一次搜索都不该有")

    # ---------------- ③b 播种：同样是"没跑"，但来源**不是**缓存
    def test_a_seeded_map_says_seeded_not_cached(self):
        """追加 C：`used` 为假也覆盖**播种**路径 —— 键名叫 `cached` 而来源是 `seeded:…`
        就是**说错话**（读报告的人会以为"库里那份缓存被复用了"，而实际是从旧报告免费播种的）。

        夹具：先真算一次（留下 JD + 报告 + 缓存）→ 把该 JD 的缓存 `jd_facts` 摘掉 →
        再分析一次（同一份 JD 文本 / 同一份 JD 身份）→ 只能走播种。
        """
        store.save_knowledge({"nodes": [self._card()]})
        first = self._run()
        self.assertGreaterEqual(self._kb_reuse_of(first)["reusable"], 1, "夹具前提：第一次真的复用了")

        data = jdid.load_jds()
        for rec in data["jds"]:
            rec.pop("jd_facts", None)          # 摘掉缓存 → 三级回退只剩"播种"
        jdid.save_jds(data)

        second = self._run(resume="第二版.md", allow_duplicate=True)
        meta = self._data_of(second)["knowledge_meta"]
        self.assertTrue(meta["source"].startswith(jdfacts.SEEDED_PREFIX),
                        "夹具前提：第二次必须走播种，实际来源是 %r" % (meta["source"],))
        self.assertEqual(meta["kb_reuse"], {"not_run": True, "source": meta["source"]},
                         "播种路径不许被说成 cached —— 键名与来源必须一致")
        self.assertNotIn("cached", meta["kb_reuse"],
                         "「没跑」不等于「缓存命中」：说准来源（追加 C）")


# ⚠️ 本块的另一组高风险假绿：
#   ① `do_POST` 有外层 try → 异常会变成 500 的「服务器内部错误：…」；
#   ② `/api/analyze` 那条路由自己 `except Exception` → **200 + {"ok": false}**。
# 只断言「error 里有某个词」的话，实现被换成「抛异常」断言照样成立。
# 所以每条用例都同时钉：**HTTP 状态码 + 具体字段 + 落盘事实**。
class JdRouteTest(_AnalyzeRouteCase):
    """`GET /api/jds` 与 `POST /api/jd/id`（阶段 4A 的新路由）。"""

    def _two_analyses_one_jd(self, jd_id="JD-原"):
        jdid.upsert_jd({"id": jd_id, "job_title": "语音/文本评测", "status": "未投递",
                        "job_category": "数据标注/质检", "salary": "13-26K", "company": "示例公司",
                        "jd_text_file": jd_id + ".txt"})
        store.ensure_profile()
        store.write_text(store.p_path("jd", jd_id + ".txt"), self.LONG)
        app._save_analysis({"id": "a1", "jd_id": jd_id, "report": "a1.html", "score": 66.0,
                            "resume": "第一版", "created_at": "2026-10-01 09:00"})
        app._save_analysis({"id": "a2", "jd_id": jd_id, "report": "a2.html", "score": 75.0,
                            "resume": "第二版", "created_at": "2026-10-05 09:00"})
        # 报告文件真的存在（「改 ID 绝不重命名报告」那条断言才有意义：文件在才谈得上没被改名）
        for name in ("a1.html", "a2.html"):
            store.write_text(os.path.join(store.p_path("reports"), name), "<html>%s</html>" % name)
        return jd_id

    # ---------------- GET /api/jds
    def test_jd_list_aggregates_analyses_of_one_jd_into_one_row(self):
        jd_id = self._two_analyses_one_jd()
        code, body = self._get("/api/jds")
        self.assertEqual(code, 200)
        self.assertTrue(body.get("ok"), body)
        rows = body["jds"]
        # ① 一行 = 一个 JD（**两份分析不能显示成两行**）
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["id"], jd_id)
        self.assertEqual(row["jd_id"], jd_id)
        # ② 聚合出**具体份数与最高分**
        self.assertEqual(row["analysis_count"], 2)
        self.assertEqual(row["max_score"], 75.0)
        # ③ 每份分析都在（细到简历名与分数那一层）
        self.assertEqual([a["resume"] for a in row["analyses"]], ["第一版", "第二版"])
        self.assertEqual([a["score"] for a in row["analyses"]], [66.0, 75.0])
        self.assertEqual([a["id"] for a in row["analyses"]], ["a1", "a2"])
        self.assertEqual([a["report"] for a in row["analyses"]], ["a1.html", "a2.html"])
        # ④ JD 级字段来自 jds.json
        self.assertEqual(row["job_title"], "语音/文本评测")
        self.assertEqual(row["status"], "未投递")
        self.assertEqual(row["salary"], "13-26K")

    def test_jd_list_excludes_merged_analyses_from_the_counts(self):
        jd_id = self._two_analyses_one_jd()
        app._save_analysis({"id": "a1", "jd_id": jd_id, "report": "a1.html", "score": 66.0,
                            "resume": "第一版", "created_at": "2026-10-01 09:00",
                            "merged_into": "a2"})
        code, body = self._get("/api/jds")
        self.assertEqual(code, 200)
        self.assertEqual(len(body["jds"]), 1)
        self.assertEqual(body["jds"][0]["analysis_count"], 1)
        self.assertEqual(body["jds"][0]["max_score"], 75.0)
        self.assertEqual([a["id"] for a in body["jds"][0]["analyses"]], ["a2"])

    def test_api_jobs_carries_the_same_jd_rows(self):
        """`/api/jobs` 的字段来源也改到 jds.json（既有路由，字段来源换了但形状不变）。"""
        jd_id = self._two_analyses_one_jd()
        code, body = self._get("/api/jobs")
        self.assertEqual(code, 200)
        self.assertEqual(len(body["jobs"]), 1)
        row = body["jobs"][0]
        self.assertEqual(row["id"], jd_id)
        self.assertEqual(row["job_title"], "语音/文本评测")
        self.assertEqual(row["analysis_count"], 2)
        self.assertEqual(row["max_score"], 75.0)

    # ---------------- POST /api/jd/id
    def test_id_change_renames_the_jd_and_db_references(self):
        jd_id = self._two_analyses_one_jd()
        store.save_knowledge({"nodes": [{"id": "rlhf", "term": "RLHF", "from_jds": [
            {"id": jd_id, "job_title": "语音/文本评测", "date": "2026-10-01"}]}]})
        new_id = "硕士_示例公司_语音评测_语音_北京_13-26K_0009"
        code, body = self._post_json("/api/jd/id", {"old_id": jd_id, "new_id": new_id})
        self.assertEqual(code, 200, body)
        self.assertTrue(body.get("ok"), body)
        self.assertEqual(body["id"], new_id)
        # ① jds.json：id 换了、旧值进 id_history
        self.assertIsNone(jdid.find_jd(jd_id))
        jd = jdid.find_jd(new_id)
        self.assertIsNotNone(jd)
        self.assertEqual(jd["id_history"], [jd_id])
        # ② jobs.json：每条分析的 jd_id 都跟着改
        self.assertEqual(sorted(j["jd_id"] for j in store.load_jobs()), [new_id, new_id])
        # ③ 知识库 from_jds 跟着改
        card = [n for n in store.load_knowledge()["nodes"] if n.get("id") == "rlhf"][0]
        self.assertEqual([x["id"] for x in card["from_jds"]], [new_id])
        # ④ 报告文件**永不重命名**
        self.assertTrue(os.path.exists(os.path.join(store.p_path("reports"), "a1.html")))
        self.assertTrue(os.path.exists(os.path.join(store.p_path("reports"), "a2.html")))
        # ⑤ 台账读得回来（新 ID 的那一行，份数还是 2）
        code2, body2 = self._get("/api/jds")
        self.assertEqual(code2, 200)
        self.assertEqual([r["id"] for r in body2["jds"]], [new_id])
        self.assertEqual(body2["jds"][0]["analysis_count"], 2)

    def test_a_taken_id_is_409_and_nothing_is_saved(self):
        jd_id = self._two_analyses_one_jd()
        other = "本科_金沙人力_数据标注质检_视频_成都_6-8K_0002"
        jdid.upsert_jd({"id": other, "job_title": "另一个岗位"})
        code, body = self._post_json("/api/jd/id", {"old_id": jd_id, "new_id": other})
        # ① 状态码必须是 409（不是 200、不是被外层 try 兜成的 500）
        self.assertEqual(code, 409, body)
        self.assertFalse(body.get("ok"))
        # ② 具体字段：错误说明里必须点名「已被占用」
        self.assertIn("占用", body.get("error", ""))
        # ③ 落盘事实：两边都**没被改**
        self.assertIsNotNone(jdid.find_jd(jd_id))
        self.assertEqual(jdid.find_jd(other)["job_title"], "另一个岗位")
        self.assertEqual(sorted(j["jd_id"] for j in store.load_jobs()), [jd_id, jd_id])

    def test_a_structurally_invalid_id_is_400_and_nothing_is_saved(self):
        jd_id = self._two_analyses_one_jd()
        for bad in ("少一段_0003", "本科_有 空格_标注_文本_北京_6-8K_0004",
                    "本科_含/斜杠_标注_文本_北京_6-8K_0005"):
            with self.subTest(bad=bad):
                code, body = self._post_json("/api/jd/id", {"old_id": jd_id, "new_id": bad})
                self.assertEqual(code, 400, body)
                self.assertFalse(body.get("ok"))
                self.assertIsNotNone(jdid.find_jd(jd_id))
                self.assertEqual(sorted(j["jd_id"] for j in store.load_jobs()), [jd_id, jd_id])

    def test_unknown_old_id_is_404(self):
        self._two_analyses_one_jd()
        code, body = self._post_json("/api/jd/id", {"old_id": "没有这个JD",
                                                    "new_id": "本科_示例_标注_文本_北京_6-8K_0006"})
        self.assertEqual(code, 404)
        self.assertFalse(body.get("ok"))
        self.assertIn("没有这个", body.get("error", ""))

    def test_missing_parameters_are_400(self):
        for payload in ({"old_id": "x"}, {"new_id": "y"}, {}):
            with self.subTest(payload=payload):
                code, body = self._post_json("/api/jd/id", payload)
                self.assertEqual(code, 400)
                self.assertFalse(body.get("ok"))


class JdLevelFieldsTest(_AnalyzeRouteCase):
    """既有路由的字段来源改到 `jds.json`（`/api/jobs/update`、`/api/jd_text`、结果读回）。"""

    def test_jobs_update_writes_the_jd_record_not_jobs_json(self):
        jd_id = "本科_金沙人力_数据标注质检_视频_成都_6-8K_0001"
        jdid.upsert_jd({"id": jd_id, "job_title": "旧标题", "job_category": "其他",
                        "status": "未投递"})
        app._save_analysis({"id": "r1", "jd_id": jd_id, "report": "r1.html", "score": 70.0,
                            "resume": "我的简历"})
        code, body = self._post_json("/api/jobs/update",
                                     {"id": jd_id, "job_title": "新标题", "status": "已投",
                                      "notes": "备注", "job_category": "数据标注/质检"})
        self.assertEqual(code, 200, body)
        self.assertTrue(body.get("ok"), body)
        jd = jdid.find_jd(jd_id)
        self.assertEqual(jd["job_title"], "新标题")
        self.assertEqual(jd["status"], "已投")
        self.assertEqual(jd["notes"], "备注")
        # **jobs.json 里绝不能多出这些 JD 级字段**
        rec = store.find_job("r1")
        for stolen in ("job_title", "status", "notes", "job_category"):
            self.assertNotIn(stolen, rec, "JD 级字段 %s 漏回了 jobs.json" % stolen)

    def test_jd_text_is_served_from_the_jd_record(self):
        jd_id = "本科_金沙人力_数据标注质检_视频_成都_6-8K_0001"
        jdid.upsert_jd({"id": jd_id, "jd_text_file": jd_id + ".txt"})
        store.ensure_profile()
        store.write_text(store.p_path("jd", jd_id + ".txt"), "这是 JD 原文")
        code, body = self._get("/api/jd_text?id=" + quote(jd_id))
        self.assertEqual(code, 200)
        self.assertEqual(body["text"], "这是 JD 原文")

    def test_jd_text_falls_back_to_the_analysis_id_for_legacy_records(self):
        """历史记录：分析 id 与 JD id 是同一个（迁移前身份 == 报告文件名）→ 也要读得到。"""
        jdid.upsert_jd({"id": "旧岗位_66分", "jd_text_file": "旧岗位_66分.txt"})
        store.ensure_profile()
        store.write_text(store.p_path("jd", "旧岗位_66分.txt"), "老 JD 原文")
        code, body = self._get("/api/jd_text?id=" + quote("旧岗位_66分"))
        self.assertEqual(code, 200)
        self.assertEqual(body["text"], "老 JD 原文")

    def test_jd_text_404_when_there_is_no_source(self):
        jdid.upsert_jd({"id": "本科_示例_标注_文本_北京_6-8K_0007"})
        code, body = self._get("/api/jd_text?id=" + quote("本科_示例_标注_文本_北京_6-8K_0007"))
        self.assertEqual(code, 404)
        self.assertIn("没有保存", body.get("error", ""))

    def test_outcome_read_comes_from_the_jd_record(self):
        jd_id = "本科_金沙人力_数据标注质检_视频_成都_6-8K_0001"
        jdid.upsert_jd({"id": jd_id, "status": "已拒", "job_title": "语音评测",
                        "application": {"status": "已拒", "outcome_reason": "预算冻结"}})
        app._save_analysis({"id": "r1", "jd_id": jd_id, "report": "r1.html", "score": 70.0})
        code, body = self._get("/api/job/outcome?id=" + quote(jd_id))
        self.assertEqual(code, 200, body)
        self.assertEqual(body["application"]["status"], "已拒")
        self.assertEqual(body["application"]["outcome_reason"], "预算冻结")

    def test_job_result_is_written_to_the_jd_record(self):
        jd_id = "本科_金沙人力_数据标注质检_视频_成都_6-8K_0001"
        jdid.upsert_jd({"id": jd_id, "status": "未投递"})
        app._save_analysis({"id": "r1", "jd_id": jd_id, "report": "r1.html", "score": 70.0})
        code, body = self._post_json("/api/job/result",
                                     {"id": jd_id, "status": "Offer", "reason": "HR 说恭喜"})
        self.assertEqual(code, 200, body)
        jd = jdid.find_jd(jd_id)
        self.assertEqual(jd["status"], "Offer")
        self.assertEqual(jd["application"]["outcome_reason"], "HR 说恭喜")
        rec = store.find_job("r1")
        self.assertNotIn("status", rec)
        self.assertNotIn("application", rec)


class JobViewWindowTest(_AnalyzeRouteCase):
    """`_job_view_window` 必须是**按线程**的窗口。

    原来的实现是「进窗口时替换模块级全局、退出时换回来」，窗口里含**一次几十秒的模型调用**
    （`/api/job/attribute`）—— 那几十秒里**别的线程**读到的也是视图版（另一个世界的记录）。
    现在改成「打一次补丁 + `threading.local()` 分发」：只有主动进窗口的那个线程看到视图。

    线程同步一律用 `threading.Event`（确定性），**绝不用 sleep 猜时序**。
    """

    JID = "本科_金沙人力_数据标注质检_视频_成都_6-8K_0001"
    AID = "20261012_视频标注质检_82分"

    def _records(self):
        """分析记录**只有分析级字段**（没有 job_title）；JD 级字段在 jds.json。"""
        app._ensure_store_patch()
        jdid.upsert_jd({"id": self.JID, "job_title": "视频标注质检", "status": "未投递"})
        app._save_analysis({"id": self.AID, "jd_id": self.JID, "report": self.AID + ".html",
                            "score": 82.0, "resume": "我的简历"})

    def test_the_view_window_is_per_thread(self):
        """① A 在窗口里 → A 看到视图；**B 同时调 `store.find_job` 拿到原实现的结果**。"""
        self._records()
        raw = store.find_job                                    # 窗口之外 = 原实现
        self.assertEqual(raw(self.AID).get("jd_id"), self.JID)  # 原实现按分析 id 找得到

        in_a, leave_a, b_done = (threading.Event(), threading.Event(), threading.Event())
        seen = {}

        def thread_a():
            with app._job_view_window():
                seen["a"] = store.find_job(self.AID)            # 视图：JD 级字段拼进来
                in_a.set()
                leave_a.wait(10)                               # 保持窗口开着，等 B 读完
            seen["a_after"] = store.find_job(self.AID)          # 窗口退出后：原实现
            b_done.set()

        def thread_b():
            in_a.wait(10)                                       # A 已在窗口内
            seen["b"] = store.find_job(self.AID)
            seen["b_active_flag"] = bool(getattr(app._VIEW_LOCAL, "active", False))
            leave_a.set()
            b_done.wait(10)

        ta, tb = threading.Thread(target=thread_a), threading.Thread(target=thread_b)
        ta.start(); tb.start(); ta.join(10); tb.join(10)
        self.assertFalse(ta.is_alive(), "线程 A 没跑完（死等超时）")
        self.assertFalse(tb.is_alive(), "线程 B 没跑完（死等超时）")

        # A：视图 —— 拿到了 jds.json 里的岗位背景
        self.assertEqual(seen["a"]["job_title"], "视频标注质检")
        self.assertEqual(seen["a"]["id"], self.AID)
        # B：**原实现** —— 同一个 id 在 jobs.json 里就是分析记录（没有 job_title 这些 JD 级字段）
        self.assertNotIn("job_title", seen["b"])
        self.assertEqual(seen["b"].get("jd_id"), self.JID)
        # ⚠️ `store.find_job` **函数对象**在补丁之后是恒定的（补丁只打一次），所以「是不是同一个
        # 函数对象」证明不了任何事 —— 真正的证据是**同一时刻同一个 id 两个线程拿到不同结果**：
        # 上面对 A/B 返回值的那两条断言就是它。再补一条「窗口标记只对 A 为真」。
        self.assertFalse(seen["b_active_flag"], "线程 B 也被当成在窗口里了（分发没按线程）")
        # 窗口退出后 A 也恢复原实现
        self.assertNotIn("job_title", seen["a_after"])

    def test_the_window_does_not_leak_out_of_the_context(self):
        """② 窗口退出后当前线程立刻恢复原实现 —— 复用的线程不会把窗口带进下一个请求。"""
        self._records()
        with app._job_view_window():
            self.assertTrue(getattr(app._VIEW_LOCAL, "active", False))
            self.assertEqual(store.find_job(self.AID)["job_title"], "视频标注质检")
        self.assertFalse(getattr(app._VIEW_LOCAL, "active", False))
        self.assertNotIn("job_title", store.find_job(self.AID))

    def test_an_exception_inside_the_window_still_clears_it(self):
        """③ 窗口里抛异常（真实现里＝模型挂了）也不能残留标记 —— `try/finally` 必须兜住。"""
        self._records()
        raw = store.find_job
        self.assertIsNotNone(raw)                 # 补丁版也在，但它按线程分发 —— 断言的是**行为**

        def boom():
            raise RuntimeError("模型挂了")

        with self.assertRaises(RuntimeError):
            app._with_job_views(boom)
        self.assertNotIn("job_title", store.find_job(self.AID))
        self.assertFalse(getattr(app._VIEW_LOCAL, "active", False),
                         "窗口标记残留：下一个请求会被当成还在窗口里")
        # 同一个线程再进一次窗口依然正常（没有残留状态）
        with app._job_view_window():
            self.assertEqual(store.find_job(self.AID)["job_title"], "视频标注质检")

    def test_nested_windows_restore_the_outer_one(self):
        """嵌套（窗口里再开一个窗口）时内层退出不能把外层的窗口关掉。"""
        self._records()
        with app._job_view_window():
            with app._job_view_window():
                self.assertEqual(store.find_job(self.AID)["job_title"], "视频标注质检")
            self.assertEqual(store.find_job(self.AID)["job_title"], "视频标注质检")   # 外层还在
        self.assertFalse(getattr(app._VIEW_LOCAL, "active", False))


# --------------------------------------------- 简历版本库接线（Task 2）+ 查重粒度 (JD × 简历)
# 规格 §6.1（路由）/ §6.2（查重粒度）/ §4.6（删除语义）/ §8（诚实边界）。
# 防假绿：
#   ① 查重那几条同时钉 **状态码 + duplicate 的具体字段 + (llm,search,knowledge) 调用计数**；
#   ② 取回那几条钉 **kind 与 text 的个体值**（绝不写「整页 HTML 里有没有某字符串」）；
#   ③ 删除那条**逐字**断言盘上文件 —— 只断言「文件还在」会被「被清空 / 被换成别人」蒙过去。
class ResumeDedupGranularityTest(_AnalyzeRouteCase):
    """§6.2：查重粒度 = **(JD 指纹 × resume_id)**。这条正是今天**卡死用户**的那个 bug。"""

    def _analyze(self, jd_text=None, resume="我的简历.md", **extra):
        body = {"jd_text": jd_text or self.LONG, "resume_name": self._resume(resume)}
        body.update(extra)
        return self._post_json("/api/analyze", body)

    def test_the_same_jd_with_a_different_resume_version_is_not_a_duplicate(self):
        """**本次核心**：一个岗位 × 多份简历。第二份简历绝不能被判「重复」。

        旧实现只看 JD 文本 → 第二份简历拿到的是一句「重复」（409），而前端全仓没有
        `allow_duplicate` / `overwrite` 入口 → 用户看到「失败」且无法继续。
        """
        calls = self._patch_analyze_deps()
        # 两份简历各自**真的登记**一个版本（修 4 起 `resume_id` 会先查存在）
        va, vb = self._version("简历A"), self._version("简历B")
        code1, body1 = self._analyze(resume="简历A.md", resume_id=va["id"])
        self.assertEqual(code1, 200, body1)
        # 第二份简历：**不传 allow_duplicate**（用户没做过任何选择，就该照常分析）
        code2, body2 = self._analyze(resume="简历B.md", resume_id=vb["id"])
        self.assertEqual(code2, 200, body2)                    # ← 旧代码这里 409
        self.assertNotEqual(body1["report"], body2["report"])   # 两次分析各有各的报告
        self.assertGreaterEqual(calls["llm"], 2)

        jds = jdid.load_jds()
        self.assertEqual(len(jds["jds"]), 1, "同一份 JD 换简历绝不能建出第二个 JD")
        jobs = store.load_jobs()
        self.assertEqual(len(jobs), 2, "两次分析 = 两条分析记录")
        jd_id = jds["jds"][0]["id"]
        self.assertEqual([j["jd_id"] for j in jobs], [jd_id, jd_id])      # 逐条，不是只断数量
        self.assertEqual(sorted(j["resume_id"] for j in jobs), sorted([va["id"], vb["id"]]))
        self.assertEqual(sorted(j["resume"] for j in jobs), ["简历A", "简历B"])

    def test_the_same_jd_with_the_same_resume_version_is_still_a_duplicate(self):
        """同一 JD + **同一版**简历 → 409，且**一个额度都不花**（查重在花钱之前）。"""
        v = self._version("我的简历")
        self._old_job(resume_id=v["id"])
        calls = self._patch_analyze_deps()
        code, body = self._analyze(resume="我的简历.md", resume_id=v["id"])
        self.assertEqual(code, 409, body)
        dup = body["duplicate"]
        self.assertEqual(dup["job_id"], "j-old")
        self.assertEqual(dup["jd_id"], "j-old")
        self.assertEqual(dup["job_title"], "语音/文本评测")
        self.assertEqual(dup["created_at"], "2026-10-03 00:30")     # 命中的是**这一条**
        self.assertEqual(dup["score"], 75.0)
        self.assertEqual(dup["report"], "j-old.html")               # 前端弹窗要用
        self.assertEqual(dup["resume_id"], v["id"])
        self.assertEqual(dup["suggestions"], ["overwrite", "new"])
        self.assertEqual((calls["llm"], calls["search"], calls["knowledge"]), (0, 0, 0))
        self.assertEqual(len(store.load_jobs()), 1)                 # 没多建

    def test_an_old_analysis_without_a_resume_id_never_blocks(self):
        """老数据（分析记录**没有** `resume_id`）→ 不判重复：宁可多建，也不误拦（规格 §6.2）。"""
        self._old_job()                                    # resume="旧简历"，**没有** resume_id
        self.assertNotIn("resume_id", store.load_jobs()[0])
        v = self._version("旧简历")                          # 真有这么一版（修 4 起要查存在）
        self._patch_analyze_deps()
        # 两种请求都不许拦：① 带了版本号；② 连版本号都没带（老前端）。
        # 简历**名字**故意与老记录逐字相同 —— 证明让它过去的是「那一版没有版本记录」，不是名字不同。
        for extra in ({"resume_id": v["id"]}, {}):
            with self.subTest(extra=extra):
                code, body = self._analyze(resume="旧简历.md", **extra)
                self.assertEqual(code, 200, body)
        self.assertEqual(len(store.load_jobs()), 3)

    def test_a_new_analysis_records_the_resume_id_it_used(self):
        """新分析把**这一版**钉进 `jobs.json`；报告数据里同时带 `resume` 与 `resume_id`。"""
        v = self._version("我的简历")
        self._patch_analyze_deps()
        code, body = self._analyze(resume="我的简历.md", resume_id=v["id"])
        self.assertEqual(code, 200, body)
        rec = store.load_jobs()[0]
        self.assertEqual(rec["resume_id"], v["id"])          # ← 真正落盘的那一行
        self.assertEqual(rec["resume"], "我的简历")           # 显示名（兼容旧报告）
        data = kb.read_report_data(os.path.join(store.p_path("reports"), body["report"]))
        self.assertEqual(data.get("resume_id"), v["id"])
        self.assertEqual(data.get("resume"), "我的简历")

    def test_an_old_analysis_without_a_resume_id_reads_as_version_unknown(self):
        """反向：老记录**不许**被补上一个猜出来的 `resume_id`（界面显示「版本未知（早于版本库）」）。"""
        self._old_job()
        self.assertNotIn("resume_id", store.load_jobs()[0])
        v = self._version("另一份")
        self._patch_analyze_deps()
        code, body = self._analyze(resume="另一份.md", resume_id=v["id"])
        self.assertEqual(code, 200, body)
        old = [j for j in store.load_jobs() if j["id"] == "j-old"][0]
        self.assertNotIn("resume_id", old)                   # 没被"顺手"改成新版本号

    def test_overwriting_without_a_version_does_not_keep_the_old_pin(self):
        """覆盖同一条记录时没给版本 → **不许留着上一次的版本号**（那会是一句假话）。

        `store.upsert_job` 是合并写：不显式清掉，上一版的 `resume_id` 会留在这次分析头上，
        而这次用的可能是**另一份**简历（`resume` 显示名都被改成新的了）。
        """
        self._old_job(resume_id="rv_0001")
        self._patch_analyze_deps()
        code, body = self._post_json("/api/analyze",
                                     {"jd_text": self.LONG, "source_type": "粘贴文本",
                                      "resume_name": self._resume("另一份.md"), "overwrite": "j-old"})
        self.assertEqual(code, 200, body)
        rec = store.load_jobs()[0]
        self.assertEqual(rec["id"], "j-old")
        self.assertEqual(rec["resume"], "另一份")            # 写回了同一条记录
        self.assertFalse(rec.get("resume_id"),
                         "覆盖后还背着上一版的版本号：%r" % (rec.get("resume_id"),))


class ResumeIdValidationTest(_AnalyzeRouteCase):
    """修 4：`resume_id` 非空时**必须真的存在于 `resumelib`**（不是"填了就算"）。

    不校验的代价（审查原文）：传错 id → 报告数据（`app.py` 的 `d["resume_id"]`）与 `jobs.json`
    都写下"这次用的是哪一版"这句**假话**；而 `resume_id` 同时是查重的另一半粒度 → 两个不同
    文件共用一个 id 就会**误 409**。`overwrite` 早就是复核的（`app.py:779-790`），这条新路不该例外。

    空串 = 老前端 / 老数据那条路（记录里**不写**这个键）→ 照常放行。
    """

    def test_an_unknown_version_is_400_and_spends_nothing(self):
        calls = self._patch_analyze_deps()
        code, body = self._post_json("/api/analyze",
                                     {"jd_text": self.LONG, "resume_name": self._resume(),
                                      "source_type": "粘贴文本", "resume_id": "rv_9999"})
        self.assertEqual(code, 400, body)                    # ← 修之前是 200 + ok:true
        self.assertFalse(body.get("ok"))
        self.assertIn("rv_9999", str(body.get("error")))      # 明确说明是哪个 id 不存在
        # 一个额度都没花：校验在**所有花钱的事之前**
        self.assertEqual((calls["llm"], calls["search"], calls["knowledge"]), (0, 0, 0))
        self.assertEqual(store.load_jobs(), [])               # 台账一个字都没写
        self.assertEqual(jdid.load_jds()["jds"], [])          # JD 库也没建

    def test_a_version_that_really_exists_is_accepted_and_pinned_verbatim(self):
        v = self._version("我的简历")
        self._patch_analyze_deps()
        code, body = self._post_json("/api/analyze",
                                     {"jd_text": self.LONG, "resume_name": self._resume(),
                                      "source_type": "粘贴文本", "resume_id": v["id"]})
        self.assertEqual((code, body.get("ok")), (200, True), body)
        self.assertEqual(store.load_jobs()[0]["resume_id"], v["id"])     # ← 真正落盘的那一行

    def test_an_empty_version_is_still_allowed(self):
        """老前端 / 老数据那条路：不带（或只有空白）→ 照常分析，记录里**不写**这个键。"""
        self._patch_analyze_deps()
        for extra in ({}, {"resume_id": ""}, {"resume_id": "   "}):
            with self.subTest(extra=extra):
                body = {"jd_text": self.LONG, "resume_name": self._resume(),
                        "source_type": "粘贴文本"}
                body.update(extra)
                code, resp = self._post_json("/api/analyze", body)
                self.assertEqual((code, resp.get("ok")), (200, True), resp)
        for rec in store.load_jobs():
            self.assertNotIn("resume_id", rec)                # 不猜、也不写一个空串


class ResumeVersionOwnershipTest(_AnalyzeRouteCase):
    """修 8：`resume_id` **存在**还不够 —— 它必须**属于被分析的那份简历**。

    只校验"存在"时的代价：拿 A 的版本号 + B 的文件名提交也能过 → 报告数据与 `jobs.json`
    都把"这次用的是哪一版"记成**假话**（B 的内容 + A 的版本号）；而版本号又是查重的另一半粒度
    → 同一份 B 用 A 的版本号提交还会**误 409**（正是本阶段要根除的那条死路）。
    名字口径与 `create_version` 存的一致：文件名**去扩展名**（`我的简历.md` ↔ 版本 `我的简历`）。
    """

    def test_a_version_of_another_resume_is_400_and_spends_nothing(self):
        calls = self._patch_analyze_deps()
        va = self._version("我的简历", "我的简历正文")
        other = self._resume("别人的简历.md")
        code, body = self._post_json("/api/analyze",
                                     {"jd_text": self.LONG, "resume_name": other,
                                      "source_type": "粘贴文本", "resume_id": va["id"]})
        self.assertEqual(code, 400, body)                    # ← 修之前是 200 + ok:true
        self.assertFalse(body.get("ok"))
        err = str(body.get("error"))
        self.assertIn(va["id"], err)                         # 说清是哪个版本号
        self.assertIn("我的简历", err)                        # 说清这一版属于谁
        self.assertIn("别人的简历", err)                      # 说清要分析的是谁
        # 一个额度都没花：校验在**所有花钱的事之前**（与"版本不存在"那条同一位置）
        self.assertEqual((calls["llm"], calls["search"], calls["knowledge"]), (0, 0, 0))
        self.assertEqual(store.load_jobs(), [])               # 台账一个字都没写
        self.assertEqual(jdid.load_jds()["jds"], [])

    def test_the_matching_version_passes_the_same_gate(self):
        """反向：**同名**那一版照常通过 —— 否则上面那条就成了「恒真的拦截」。

        `我的简历.md` ↔ 版本 `我的简历`（去扩展名同一口径）；落盘的 `resume_id` 逐字正确。
        """
        v = self._version("我的简历")
        self._patch_analyze_deps()
        code, body = self._post_json("/api/analyze",
                                     {"jd_text": self.LONG, "resume_name": self._resume("我的简历.md"),
                                      "source_type": "粘贴文本", "resume_id": v["id"]})
        self.assertEqual((code, body.get("ok")), (200, True), body)
        self.assertEqual(store.load_jobs()[0]["resume_id"], v["id"])


class ResumeVersionMustBeCurrentTest(_AnalyzeRouteCase):
    """修 1（后端）：`resume_id` 必须指向**它所属族谱的当前版**（不是"存在 + 名字对得上"就算）。

    为什么"存在 + 名字对得上"还不够：`do_analyze` 读的一律是 `resumes/<名字>.md` 那个文件
    = 该族谱的**最新版正文**（`app.py:1097-1104`）。把**历史版**的 id 钉上去，报告数据
    （`d["resume_id"]`）与 `jobs.json` 里那句「这次用的是哪一版」就是**假话**
    （记录说 rv_0001、模型读的却是 rv_0003 的正文）。而 `resume_id` 又是查重的另一半粒度
    （`app.py:748-770`）→ 同一个 JD 用同一族谱的第二版再分析会**误 409**，前端又没有 409 出口
    （Task 3b 的事）→ 用户卡死。所以这里与「版本不存在」「版本是别人的」同一位置、同一口径拦截。

    "当前版"的口径**只有一处**：`resumelib.current_of()`（族谱内 rev 最大的**未移出**版本）——
    前端下拉给的那个 value 就是它，两边必须是同一个答案。
    """

    def _three(self, name="我的简历"):
        """一个真族谱：v1 → v2 → v3（都是同一个 root，名字不改）。"""
        v1 = self._version(name, "第 1 版正文")
        v2 = self._version(name, "第 2 版正文", parent_id=v1["id"])
        v3 = self._version(name, "第 3 版正文", parent_id=v2["id"])
        return v1, v2, v3

    def _analyze_with(self, version_id, name="我的简历"):
        return self._post_json("/api/analyze",
                               {"jd_text": self.LONG, "resume_name": name + ".md",
                                "source_type": "粘贴文本", "resume_id": version_id})

    def test_a_historical_version_is_400_and_spends_nothing(self):
        v1, v2, v3 = self._three()
        calls = self._patch_analyze_deps()
        code, body = self._analyze_with(v1["id"])
        self.assertEqual(code, 400, body)                    # ← 修之前是 200 + ok:true
        self.assertFalse(body.get("ok"))
        err = str(body.get("error"))
        self.assertIn(v1["id"], err)                         # 说清是哪个版本号
        self.assertIn(v3["id"], err)                         # 说清当前版是哪一个
        # 一个额度都没花：校验在**所有花钱的事之前**
        self.assertEqual((calls["llm"], calls["search"], calls["knowledge"]), (0, 0, 0))
        self.assertEqual(store.load_jobs(), [])              # 台账一个字都没写
        self.assertEqual(jdid.load_jds()["jds"], [])

    def test_a_removed_version_is_not_the_current_one(self):
        """已移出的版本同样不是当前版（移出 = 不能继续用）—— 不能因为"记录还在"就放行。"""
        v1, v2, v3 = self._three()
        resumelib.remove(v2["id"])
        calls = self._patch_analyze_deps()
        code, body = self._analyze_with(v2["id"])
        self.assertEqual(code, 400, body)
        self.assertIn(v2["id"], str(body.get("error")))
        self.assertEqual((calls["llm"], calls["search"], calls["knowledge"]), (0, 0, 0))
        self.assertEqual(store.load_jobs(), [])

    def test_the_current_version_still_passes_the_same_gate(self):
        """反向：**当前版**照常通过 —— 否则上面两条就成了「恒真的拦截」。"""
        v1, v2, v3 = self._three()
        self._patch_analyze_deps()
        code, body = self._analyze_with(v3["id"])
        self.assertEqual((code, body.get("ok")), (200, True), body)
        self.assertEqual(store.load_jobs()[0]["resume_id"], v3["id"])   # ← 真正落盘的那一行


class AnalyzeUnreadableResumeTest(_AnalyzeRouteCase):
    """修 5②（Task 1.10）：简历**读不出来**时分析必须明确失败，绝不拿一份空简历喂给模型。

    旧实现 `resume = store.read_text(rp)` 会把非 UTF-8（解码失败）兜成空串，于是模型收到的是
    「【候选人简历】(空)」—— 报告照样生成，用户以为分析的是自己那份简历。
    """

    def test_an_unreadable_resume_fails_loudly_without_calling_the_model(self):
        store.ensure_profile()
        path = os.path.join(store.p_path("resumes"), "读不出来的简历.md")
        with open(path, "wb") as f:
            f.write("候选人简历正文".encode("gbk"))
        calls = self._patch_analyze_deps()

        code, body = self._post_json("/api/analyze",
                                     {"jd_text": self.LONG, "source_type": "粘贴文本",
                                      "resume_name": "读不出来的简历.md"})
        self.assertEqual(code, 200, body)                    # 路由既有口径：业务失败也是 200 + ok:false
        self.assertFalse(body.get("ok"), body)
        self.assertIn("读不出来", str(body.get("error")))
        self.assertEqual(calls["llm"], 0, "读不出来就该当场停：一个额度都不花")
        self.assertEqual((calls["search"], calls["knowledge"]), (0, 0))
        self.assertEqual(store.load_jobs(), [])              # 没有留下半条分析记录


class ResumeVersionRouteTest(IsolatedCase):
    """§6.1 简历版本路由：族谱元数据 / 取回（四种 kind）/ 选定 / 移出 / 彻底删。"""

    def _handler(self, method, path, obj=None):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8") if obj is not None else b""
        headers = {"Content-Length": str(len(raw)),
                   "Content-Type": "application/json; charset=utf-8"}
        h = _sandbox_handler(method, path, raw, headers)
        return h._status, h._body

    def _get(self, path):
        return self._handler("GET", path)

    def _post(self, path, obj):
        return self._handler("POST", path, obj)

    def _delete(self, path):
        return self._handler("DELETE", path)

    def _save(self, name, content, **extra):
        body = {"name": name, "content": content}
        body.update(extra)
        code, resp = self._post("/api/resumes", body)
        return code, resp

    def _new(self, name, content, **extra):
        """保存并断言成功，返回新版本记录。"""
        code, body = self._save(name, content, **extra)
        self.assertEqual(code, 200, body)
        return body["resume"]

    def _snap_path(self, rel):
        return store.p_path(rel)

    def test_get_returns_lineage_metadata_and_hides_removed_by_default(self):
        v1 = self._new("母版.md", "第一版内容")
        v2 = self._new("母版.md", "第二版内容", parent_id=v1["id"])
        self.assertEqual((v2["rev"], v2["parent_id"], v2["root_id"]),
                         (2, v1["id"], v1["id"]))

        code, body = self._get("/api/resumes")
        self.assertEqual(code, 200)
        self.assertEqual([r["id"] for r in body["resumes"]], [v1["id"], v2["id"]])
        row = [r for r in body["resumes"] if r["id"] == v1["id"]][0]
        for k in ("id", "name", "rev", "at", "chosen", "removed", "mother_file", "ai_diff"):
            self.assertIn(k, row)                                  # 前端要的族谱元数据逐项都在
        self.assertEqual((row["name"], row["rev"], row["removed"], row["chosen"]),
                         ("母版", 1, False, False))
        self.assertEqual(body["files"], ["母版.md"])               # 磁盘上的文件名照旧给

        # 移出后**默认列表里不再有它**（← 变异③「移出后列表仍显示它」会把这两条打红）
        code, body = self._delete("/api/resumes?version=" + v1["id"])
        self.assertEqual(code, 200, body)
        code, body = self._get("/api/resumes")
        self.assertEqual([r["id"] for r in body["resumes"]], [v2["id"]])
        self.assertEqual(body["files"], ["母版.md"])               # 文件还在盘上（下一步逐字断言）
        code, body = self._get("/api/resumes?include_removed=1")
        self.assertEqual([r["id"] for r in body["resumes"]], [v1["id"], v2["id"]])

    def test_unregistered_files_on_disk_are_reported_not_hidden(self):
        """迁移之前（Task 5 之前）盘上就有简历文件、`resumes.json` 还没有 —— 界面必须看得见它们。"""
        store.ensure_profile()
        store.write_text(os.path.join(store.p_path("resumes"), "周汉钦-母版.md"), "老简历正文")
        code, body = self._get("/api/resumes")
        self.assertEqual(code, 200)
        self.assertEqual(body["files"], ["周汉钦-母版.md"])
        self.assertEqual(body["unregistered"], ["周汉钦-母版.md"])   # 没有版本记录 → 如实标出
        self.assertEqual(body["resumes"], [])

    def test_a_removed_version_still_counts_as_registered(self):
        """修 5：`unregistered` 只报**真的没有任何版本记录**的文件。

        「移出列表」不是「没有版本记录」：移出只是不显示、不能继续用，记录与文件都在盘上。
        用**过滤后**的版本名集合算 `unregistered`，会把刚移出的简历报成"没有版本记录" ——
        T3a 据此给「登记」入口就是错的（用户以为自己的版本记录丢了）。
        """
        v1 = self._new("母版.md", "第一版内容")
        code, body = self._get("/api/resumes")
        self.assertEqual(body["unregistered"], [])
        code, body = self._delete("/api/resumes?version=" + v1["id"])
        self.assertEqual(code, 200, body)
        self.assertTrue(resumelib.by_id(v1["id"])["removed"])
        code, body = self._get("/api/resumes")
        self.assertEqual(body["resumes"], [])                  # 默认列表里不再有它
        self.assertEqual(body["files"], ["母版.md"])            # 文件还在盘上
        self.assertEqual(body["unregistered"], [])              # ← 修之前是 ["母版.md"]

        # 反证：真的没有版本记录的文件仍要如实标出（别把 unregistered 一刀切成了空）
        store.write_text(os.path.join(store.p_path("resumes"), "另一份.md"), "没登记过")
        code, body = self._get("/api/resumes")
        self.assertEqual(body["unregistered"], ["另一份.md"])

    def test_a_write_failure_is_a_structured_500_not_a_bare_disconnect(self):
        """修 6：`do_DELETE` 此前**没有外层 try** —— `resumelib.remove` 写盘失败
        （`store.write_json` 会抛）就是**连接裸断**（真实世界里客户端只看到 RemoteDisconnected），
        不是 JSON 错误（`do_GET` 同类问题有兜底，这条新路没有）。

        而且必须"失败得干净"：`resumes.json` **一字未变**（原子写在这里是前提），
        磁盘上的记录也**没有**被写成 removed。
        """
        v1 = self._new("母版.md", "内容")
        path = store.p_path("resumes.json")
        with open(path, "rb") as f:
            before = f.read()
        with mock.patch.object(store, "write_json", side_effect=OSError("disk full (注入)")):
            code, body = self._delete("/api/resumes?version=" + v1["id"])
        self.assertEqual(code, 500, body)              # ← 修之前：异常冒出去（= 裸断）
        self.assertFalse(body.get("ok"))
        self.assertIn("disk full", str(body.get("error")))
        with open(path, "rb") as f:
            self.assertEqual(f.read(), before)         # 盘上的记录一字未变
        self.assertFalse(resumelib.by_id(v1["id"])["removed"])   # 也没"假装移出成功"
        self.assertEqual(store.read_text(self._snap_path("resumes/母版.md")), "内容")

    def test_delete_moves_the_version_out_and_keeps_every_file_verbatim(self):
        """§4.6「删除」= 移出列表：文件一律保留（母版快照与变更说明都在）。**逐字**断言。"""
        v1 = self._new("母版.md", "第一版内容")
        code, body = self._post("/api/resumes/choose", {"version": v1["id"]})
        self.assertEqual(code, 200, body)
        v2 = self._new("母版.md", "第二版内容", parent_id=v1["id"])
        snap_rel = resumelib.by_id(v1["id"])["mother_file"]
        self.assertTrue(snap_rel)

        code, body = self._delete("/api/resumes?version=" + v1["id"])
        self.assertEqual(code, 200, body)
        self.assertTrue(resumelib.by_id(v1["id"])["removed"])

        # 逐字：不是「文件还在」——「还在」会被「被清空 / 被换成别人」蒙过去（← 变异② os.remove）
        with open(self._snap_path("resumes/母版.md"), "rb") as f:
            self.assertEqual(f.read(), "第二版内容".encode("utf-8"))
        with open(self._snap_path(snap_rel), "rb") as f:
            self.assertEqual(f.read(), "第一版内容".encode("utf-8"))
        self.assertEqual(len(resumelib.list_versions(include_removed=True)), 2)   # 记录也还在

    def test_delete_by_name_still_moves_out_the_current_version(self):
        """老前端（按文件名删）也**只移出列表**：文件一字不动。"""
        self._new("母版.md", "内容")
        code, body = self._delete("/api/resumes?name=" + quote("母版.md"))
        self.assertEqual(code, 200, body)
        self.assertEqual(store.read_text(self._snap_path("resumes/母版.md")), "内容")
        self.assertEqual(resumelib.list_versions(), [])

    def test_delete_by_name_resolves_and_removes_in_one_critical_section(self):
        """修 7：`_version_of_name`（解析"当前版本"）与 `remove` 是**两步**，而且都在锁外/锁内分开。

        这条钉住"解析 + 移出必须在 `resumelib` 的**一个入口**里完成"：
        解析当前版本一旦跑到临界区外（老写法的 `resumelib.current_of`），这里就当场炸。
        """
        v1 = self._new("母版.md", "内容")
        calls = []
        real = resumelib.remove_current_of

        def spy(name):
            calls.append(name)
            return real(name)

        with mock.patch.object(resumelib, "remove_current_of", spy), \
                mock.patch.object(resumelib, "current_of",
                                  mock.Mock(side_effect=AssertionError(
                                      "解析「当前版本」跑到临界区外了（修 7 被改回去了）"))):
            code, body = self._delete("/api/resumes?name=" + quote("母版.md"))
        self.assertEqual(code, 200, body)
        self.assertEqual(calls, ["母版.md"])                    # 一次调用，不是"先解析再移出"两步
        self.assertEqual(body["removed"]["id"], v1["id"])
        self.assertTrue(resumelib.by_id(v1["id"])["removed"])

    def test_a_concurrent_save_cannot_slip_between_resolving_and_removing(self):
        """修 7：两个线程（`Event` 同步、**不 sleep**）—— 一个保存、一个按名字移出。

        老写法是两步（`app._version_of_name` 解析出 vid → `resumelib.remove(vid)`），
        中间插进一次并发保存就会移出**过时的那一版**。这里用 `Event` 精确制造那次插入：
        保存线程被唤醒后去抢 `resumelib._LOCK`。

        - 修好之后：移出这一步持着锁，保存线程只能**排在它后面** →
          `slip_before_removal` 为假、被移出的就是"当时"那一版。
        - 改回两步（反向取证）：保存线程在锁外那段空隙里完成 → `slip_before_removal` 为真 → 红。
        """
        v1 = self._new("母版.md", "第一版")
        self._post("/api/resumes/choose", {"version": v1["id"]})
        v2 = self._new("母版.md", "第二版", parent_id=v1["id"])

        go, saved = threading.Event(), threading.Event()
        writes, out, errors = [], {}, []
        real_current_of, real_save = resumelib.current_of, resumelib.save_resumes
        REMOVED = resumelib.REMOVED

        def hook_current_of(root_id):
            """只有**老写法**才会走到这里：唤醒保存线程并**等它完成**（制造锁外空隙）。"""
            rec = real_current_of(root_id)
            go.set()
            saved.wait(10)
            return rec

        def hook_save(doc):
            if not go.is_set():
                go.set()                 # 保存线程醒来 → 立刻去抢同一把锁
            # 每次落盘的"谁是已移出"都记下来：**移出那一次写盘**长这样（有 removed=True）
            writes.append([(str(r.get("id") or ""), bool(r.get(REMOVED)))
                           for r in doc.get("resumes") or []])
            return real_save(doc)

        def saver():
            go.wait(10)
            try:
                out["v3"] = resumelib.create_version("母版", "第三版", parent_id=v1["id"])
            except Exception as e:       # noqa: BLE001 —— 收起来一起断言
                errors.append(repr(e))
            finally:
                saved.set()

        t = threading.Thread(target=saver)
        t.start()
        with mock.patch.object(resumelib, "current_of", hook_current_of), \
                mock.patch.object(resumelib, "save_resumes", hook_save):
            code, body = self._delete("/api/resumes?name=" + quote("母版.md"))
            # 等保存线程**在补丁还生效时**跑完：它的那一次写盘也要被记进 writes（否则顺序不可比）
            self.assertTrue(saved.wait(10), "保存线程没能在 10 秒内跑完")
            t.join(10)
        self.assertFalse(t.is_alive())
        self.assertEqual(errors, [])

        self.assertEqual(code, 200, body)
        self.assertEqual(body["removed"]["id"], v2["id"])       # 移出的是**当时**那一版

        def write_state(rows):
            return dict(rows)

        # 「移出」那一次写盘 = v2 第一次被打上 removed；「保存」那一次 = v3 第一次出现在文件里。
        # 两者都按**落盘顺序**记：并发保存若插在"解析"与"移出"之间，v3 就会**先**出现。
        removal_at = [i for i, rows in enumerate(writes)
                      if write_state(rows).get(v2["id"]) is True]
        create_at = [i for i, rows in enumerate(writes) if out["v3"]["id"] in write_state(rows)]
        self.assertTrue(removal_at and create_at, writes)
        self.assertLess(removal_at[0], create_at[0],
                        "并发保存插到了「解析」与「移出」之间：移出的不是当时那一版")

        # 不丢数据：三条记录都在；新版内容成了最新版容器；母版快照逐字未变
        recs = resumelib.load_resumes()["resumes"]
        self.assertEqual(sorted(r["id"] for r in recs),
                         sorted([v1["id"], v2["id"], out["v3"]["id"]]))
        self.assertTrue(resumelib.by_id(v2["id"])["removed"])
        self.assertFalse(resumelib.by_id(v1["id"])["removed"])
        self.assertFalse(resumelib.by_id(out["v3"]["id"])["removed"])
        self.assertEqual(store.read_text(self._snap_path("resumes/母版.md")), "第三版")
        self.assertEqual(store.read_text(self._snap_path(resumelib.by_id(v1["id"])["mother_file"])),
                         "第一版")

    def test_the_file_route_serves_the_latest_and_the_mother_verbatim(self):
        v1 = self._new("母版.md", "第一版内容")
        self._post("/api/resumes/choose", {"version": v1["id"]})
        v2 = self._new("母版.md", "第二版内容", parent_id=v1["id"])
        code, body = self._get("/api/resumes/file?version=" + v2["id"])
        self.assertEqual((code, body["kind"], body["text"], body["ok"]), (200, "latest", "第二版内容", True))
        code, body = self._get("/api/resumes/file?version=" + v1["id"])
        self.assertEqual((code, body["kind"], body["text"], body["ok"]), (200, "mother", "第一版内容", True))

    def test_the_file_route_says_the_text_was_never_kept_instead_of_serving_an_empty_file(self):
        """§8.1 旧版原文不保留（用户已定的取舍）：**明确说明**，绝不用空串冒充原文。"""
        v1 = self._new("母版.md", "第一版内容")
        self._post("/api/resumes/choose", {"version": v1["id"]})
        v2 = self._new("母版.md", "第二版内容", parent_id=v1["id"])
        self._new("母版.md", "第三版内容", parent_id=v2["id"])

        code, body = self._get("/api/resumes/file?version=" + v2["id"])
        self.assertEqual(code, 200)
        self.assertEqual(body["kind"], "missing")
        self.assertFalse(body["ok"])
        self.assertIn("原文未保留", body["error"])        # 明确说明，不是沉默
        self.assertNotIn("text", body)                    # ← 绝不给一个空串冒充原文

    def test_the_file_route_flags_content_that_no_longer_matches_the_record(self):
        """§8.2 程序外改过 → 如实说「不符」+ 亮出内容与两个 sha，绝不静默当正常版本。"""
        v1 = self._new("母版.md", "原始内容")
        with open(self._snap_path("resumes/母版.md"), "w", encoding="utf-8", newline="") as f:
            f.write("别人手改的内容")                      # 模拟程序外改文件
        code, body = self._get("/api/resumes/file?version=" + v1["id"])
        self.assertEqual(code, 200)
        self.assertEqual(body["kind"], "mismatch")
        self.assertFalse(body["ok"])
        self.assertIn("不符", body["error"])
        self.assertEqual(body["text"], "别人手改的内容")   # 内容照常亮出来对账
        self.assertEqual(body["sha256"], v1["sha256"])     # 记录里的
        self.assertEqual(body["actual_sha256"], resumelib.sha256_of("别人手改的内容"))
        self.assertNotEqual(body["actual_sha256"], body["sha256"])

    def test_a_new_root_never_calls_ai_diff_of_at_all(self):
        """新开（`mode != "continue"`）时 `resumelib.ai_diff_of` **根本没被调用**。

        为什么必须 spy **那个函数**、而不是只数模型调用：`resumelib.ai_diff_of` 自己还有一道闸
        （这一版没有母版 → 直接返回、一次都不调模型），所以只断言"模型没被调"时，把 `app.py`
        那道 `if mode == "continue":` 拿掉，用例**照样绿** —— 两道闸任一在就都不调模型，
        这条断言是唯一能钉住 app 侧那道闸的东西。
        """
        calls = []
        real = resumelib.ai_diff_of

        def spy(version_id, *args, **kwargs):
            calls.append(version_id)
            return real(version_id, *args, **kwargs)

        fake_model = mock.Mock(return_value='{"summary": "补了 ASR 评测经历", '
                                            '"changes": [{"where": "技能", "what": "加了 ASR"}]}')
        with mock.patch.object(resumelib, "ai_diff_of", side_effect=spy), \
                mock.patch.object(llm, "call_openai_compatible", fake_model):
            v1 = self._new("母版.md", "第一版内容")
            self.assertEqual(calls, [], "「新开一份独立的」不该调 ai_diff_of：%s" % calls)
            self.assertEqual(fake_model.call_count, 0)
            # 对照：**同一个 spy** 在"延续"时必须被调用一次 —— 证明 spy 真装上了、
            # 上面那条不是"patch 没生效"造成的假绿。
            self._post("/api/resumes/choose", {"version": v1["id"]})
            v2 = self._new("母版.md", "第二版内容", lineage="延续")
        self.assertEqual(calls, [v2["id"]])
        self.assertEqual(fake_model.call_count, 1)

    def test_saving_goes_through_create_version_so_a_record_always_appears(self):
        """§4.1 保存必须走 `resumelib.create_version`（留快照、记 sha/时间/rev）。"""
        code, body = self._save("母版.md", "第一版内容")
        self.assertEqual(code, 200, body)
        doc = resumelib.load_resumes()
        self.assertEqual(len(doc["resumes"]), 1)     # ← 旧实现直接 store.write_text：这里 0 条
        rec = doc["resumes"][0]
        self.assertEqual((rec["name"], rec["rev"], rec["file"]), ("母版", 1, "resumes/母版.md"))
        self.assertEqual(rec["sha256"], resumelib.sha256_of("第一版内容"))
        self.assertEqual(rec["size"], len("第一版内容".encode("utf-8")))
        self.assertEqual(body["resume"]["id"], rec["id"])            # 路由返回新记录
        self.assertEqual(store.read_text(self._snap_path("resumes/母版.md")), "第一版内容")

    def test_a_second_new_root_with_the_same_name_is_rejected_not_silently_overwritten(self):
        """同名「新开」→ 明确报错（绝不静默覆盖别人的文件）。"""
        self._new("母版.md", "第一版内容")
        code, body = self._save("母版.md", "别的内容")               # 不带 parent_id = 新开
        self.assertEqual(code, 400, body)
        self.assertIn("母版", str(body.get("error")))
        self.assertEqual(store.read_text(self._snap_path("resumes/母版.md")), "第一版内容")

    def test_a_continuation_without_parent_id_resolves_the_chosen_mother(self):
        """`lineage="延续"`：母亲 = 这份简历里被「选定继续优化」的那一版。"""
        v1 = self._new("母版.md", "第一版内容")
        self._post("/api/resumes/choose", {"version": v1["id"]})
        v2 = self._new("母版.md", "第二版内容", lineage="延续")
        self.assertEqual(v2["parent_id"], v1["id"])
        self.assertEqual(v2["rev"], 2)
        with open(self._snap_path(resumelib.by_id(v1["id"])["mother_file"]), "rb") as f:
            self.assertEqual(f.read(), "第一版内容".encode("utf-8"))   # 覆盖前那份逐字留下

    def test_choose_purge_and_restore_routes(self):
        v1 = self._new("甲.md", "A1")
        v2 = self._new("甲.md", "A2", parent_id=v1["id"])
        other = self._new("乙.md", "B1")

        code, body = self._post("/api/resumes/choose", {"version": v1["id"]})
        self.assertEqual(code, 200, body)
        self.assertTrue(resumelib.by_id(v1["id"])["chosen"])
        self.assertFalse(resumelib.by_id(v2["id"])["chosen"])        # 同族谱只能一个

        # 彻底删：记录没了，**别人的文件逐字未变**（反假绿）
        before = store.read_text(self._snap_path("resumes/乙.md"))
        code, body = self._post("/api/resumes/purge", {"version": v1["id"]})
        self.assertEqual(code, 200, body)
        self.assertIsNone(resumelib.by_id(v1["id"]))
        self.assertEqual(store.read_text(self._snap_path("resumes/乙.md")), before)
        self.assertIsNotNone(resumelib.by_id(other["id"]))

        # 恢复：移出 → 放回
        code, body = self._delete("/api/resumes?version=" + v2["id"])
        self.assertEqual(code, 200, body)
        self.assertTrue(resumelib.by_id(v2["id"])["removed"])
        code, body = self._post("/api/resumes/restore", {"version": v2["id"]})
        self.assertEqual(code, 200, body)
        self.assertFalse(resumelib.by_id(v2["id"])["removed"])

        # 找不到的版本 → 404（不是 200 的静默成功）
        for path in ("/api/resumes/choose", "/api/resumes/restore", "/api/resumes/purge"):
            with self.subTest(path=path):
                code, body = self._post(path, {"version": "rv_9999"})
                self.assertEqual(code, 404, body)
        code, body = self._get("/api/resumes/file?version=rv_9999")
        self.assertEqual(code, 404, body)
        code, body = self._get("/api/resumes/file")
        self.assertEqual(code, 400, body)

    def test_restore_refuses_a_name_taken_by_a_live_resume(self):
        """恢复时名字被占用 → 409，且**不许**把记录硬塞回列表（界面会看到两份同名简历）。"""
        v1 = self._new("甲.md", "A1")
        code, body = self._delete("/api/resumes?version=" + v1["id"])
        self.assertEqual(code, 200, body)
        self._new("甲.md", "B1")                                     # 名字空出来了 → 同名新开
        code, body = self._post("/api/resumes/restore", {"version": v1["id"]})
        self.assertEqual(code, 409, body)
        self.assertIn("占用", body["error"])
        self.assertTrue(resumelib.by_id(v1["id"])["removed"])        # 还在移出状态

    def test_the_name_route_never_serves_an_unreadable_file_as_empty_text(self):
        """修 5①（Task 1.10）：`GET /api/resumes?name=` 对**非 UTF-8** 文件曾回
        `200 {"content": ""}` —— 前端 `editResumeFile` 会把空串填进编辑框，用户一保存就把
        真文件覆盖成空的（**用户可见的破坏路径**）。

        现在必须回一句明确的错误（前端 `if(!d || d.error) alert(...)` 能显示），**绝不**回空串。

        **M-4（Task 1.10b）**：状态码从 500 改成 **409** —— 这不是"服务器内部错误"，
        而是"盘上那份东西现在的状态与这次读取冲突"（有字节但不是 UTF-8），
        与同轮的姊妹场景（`POST /api/resumes` 接不住母版原文 → 409）同一口径。
        """
        store.ensure_profile()
        path = os.path.join(store.p_path("resumes"), "读不出来.md")
        with open(path, "wb") as f:
            f.write("我的简历：十个项目经验".encode("gbk"))
        before = _disk_bytes(path)

        code, body = self._get("/api/resumes?name=" + quote("读不出来.md"))
        self.assertEqual(code, 409, body)                    # ← M-4 修之前：500（"服务器内部错误"是句假话）
        self.assertFalse(body.get("ok"))
        self.assertIn("读不出来", str(body.get("error")))
        self.assertNotIn("content", body, "绝不能回一个空串让前端填进编辑框（保存即覆盖）")
        self.assertEqual(_disk_bytes(path), before)          # 只读：一个字节都没动

    def test_an_unreadable_mother_file_is_409_with_the_human_sentence_not_a_bare_500(self):
        """修 6（Task 1.10）：`POST /api/resumes` 此前只接 `ValueError`，于是"母版原文留不了档"
        那条 `RuntimeError` 掉进 `do_POST` 的兜底 → **HTTP 500 + 「服务器内部错误：…」前缀**（复查实测）。

        现在路由接住它并回 **409**：这是「目标文件当前的状态与这次保存冲突」（被别的程序占用 /
        读不出来），而且**只**把那句人话显示给用户 —— 用户看到的不是"服务器内部错误"。
        """
        v1 = self._new("母版.md", "原文内容")
        code, body = self._post("/api/resumes/choose", {"version": v1["id"]})
        self.assertEqual(code, 200, body)
        path = store.p_path("resumes", "母版.md")
        real = store.read_bytes

        def fake(p):
            if os.path.abspath(str(p)) == os.path.abspath(path):
                raise PermissionError(13, "拒绝访问")
            return real(p)

        with mock.patch.object(store, "read_bytes", side_effect=fake):
            code, body = self._save("母版.md", "第二版内容", parent_id=v1["id"])

        self.assertEqual(code, 409, body)                    # ← 修之前：500
        self.assertFalse(body.get("ok"))
        self.assertIn("留档", str(body.get("error")))          # 那句人话原样给用户
        self.assertNotIn("服务器内部错误", str(body.get("error")))
        self.assertEqual(store.read_text(path), "原文内容", "读不出来就不覆盖")
        self.assertEqual(len(resumelib.load_resumes()["resumes"]), 1, "这次保存整体中止")

    def _stale_repair_precondition(self):
        """造出"按记录判断快照已过时、而盘上那份恰好就是这一版"的形状（= 修正时会先留档）。

        返回 `(v1, snap_path)`：下一次延续就会走进"先留档、再修正旧快照"那条路。
        """
        v1 = self._new("母版.md", "原文内容")
        self._post("/api/resumes/choose", {"version": v1["id"]})
        v2 = self._new("母版.md", "第二版内容", parent_id=v1["id"])
        code, body = self._post("/api/resumes/purge", {"version": v2["id"]})
        self.assertEqual(code, 200, body)                     # 记录没了 → 主人退回 v1
        snap = store.p_path("resumes", resumelib.SNAPSHOT_DIR, v1["id"] + ".md")
        disk = store.p_path("resumes", "母版.md")
        doc = resumelib.load_resumes()                        # 人工把 v1 的 sha 改成盘上那份
        for r in doc["resumes"]:
            if r.get("id") == v1["id"]:
                r["sha256"] = resumelib.sha256_of(store.read_text(disk))
        resumelib.save_resumes(doc)
        self.assertEqual(resumelib.read_version(v1["id"])[2], "latest", "前置条件：在记录看来盘上就是 v1")
        return v1, snap

    def test_a_failed_snapshot_archive_is_409_not_a_bare_500(self):
        """Task 1.10b 修 M-9：只有「没能留档」这一种冲突才回 **409**（而且旧快照逐字节未变）。

        留档失败时 `create_version` 抛 `resumelib.SnapshotArchiveError` → 路由接住 → 409 +
        那句人话。变异取证：把 `except resumelib.SnapshotArchiveError` 换回裸
        `except RuntimeError`，下面的 `test_a_plain_runtime_error_is_not_reported_as_an_aborted_save`
        会变红（它的 `RuntimeError` 会被误当"保存被中止"）。
        """
        v1, snap = self._stale_repair_precondition()
        before = store.read_bytes(snap)
        self.assertTrue(before, "前置条件：旧快照里有字节")

        with mock.patch.object(resumelib, "_archive_old_snapshot",
                               side_effect=resumelib.SnapshotArchiveError(
                                   "没能给旧快照留档（磁盘满了），已中止本次修正：宁可这次不修，"
                                   "也绝不把旧快照静默盖掉（那是不可逆的）。")):
            code, body = self._save("母版.md", "第三版内容", parent_id=v1["id"])

        self.assertEqual(code, 409, body)
        self.assertFalse(body.get("ok"))
        self.assertIn("留档", str(body.get("error")))
        self.assertNotIn("服务器内部错误", str(body.get("error")))
        self.assertEqual(store.read_bytes(snap), before, "中止 = 旧快照一个字节都不许变")
        self.assertEqual([r["id"] for r in resumelib.list_versions()], [v1["id"]],
                         "这次保存整体中止：不许留下一条没有正文的新版本")

    def test_a_plain_runtime_error_is_not_reported_as_an_aborted_save(self):
        """Task 1.10b 修 M-9 的**反向**钉子：别的 `RuntimeError` 绝不许被说成"保存已中止"。

        `ai_diff_of` 在**新版本已经落盘之后**才被调用。那里冒出任何 `RuntimeError` 时，
        旧写法（裸 `except RuntimeError`）会回 **409「保存被中止」** —— 而这次保存**已经成功**了
        （文件与记录都在盘上）。对用户说"没保存"而实际保存了 = 又一句假话，
        用户会去重做一遍，结果多出一个版本。
        """
        v1 = self._new("母版.md", "原文内容")
        code, body = self._post("/api/resumes/choose", {"version": v1["id"]})
        self.assertEqual(code, 200, body)

        def boom(*a, **kw):
            raise RuntimeError("模型那一层炸了")

        with mock.patch.object(resumelib, "ai_diff_of", boom):
            code, body = self._save("母版.md", "第二版内容", parent_id=v1["id"])

        versions = resumelib.list_versions()
        # E-4（Task 1.10c）：落盘证据要**逐项**都在 —— 新增的版本记录 + 新正文都真的写下去了。
        self.assertEqual(len(versions), 2, "前提：这次保存**其实已经成功**（新增了第二版）：%r" % (versions,))
        self.assertEqual(store.read_text(store.p_path("resumes", "母版.md")), "第二版内容",
                         "E-4：新正文必须真的落盘（下面据此断定「409 已中止」是假话）")
        # 既然保存其实成功了，就**绝不能**回 409「冲突 / 已中止」那句假话。
        # ⚠️ E-4（Task 1.10c）：这里**不许**把"走兜底 500"钉成期望值 ——
        # "保存成功了却回 500"同样是一句假话（用户会以为没保存、重做一遍，结果多一个版本）。
        # 要钉的只有一条：**它不是 409**（不是「保存被中止」），而保存确实已落盘（上面两条前提）。
        self.assertNotEqual(code, 409, body)
        self.assertNotIn("已中止", str(body.get("error")),
                         "保存已经落盘，却对用户说「已中止」：%r" % (body,))


# ---------------- 前端接线（修 1 / 修 2 / 修 3 / 修 4 / 修 5 / 修 6）+ 阶段 4C：简历下拉 ----------------
#
# 为什么这里的断言要**真跑页面的 JS**（`tests/web_probe.py`）：源码字符串断言太容易假绿 ——
# `'<option value="'` 在**占位选项**（`'<option value="">（无简历，请先新建）'`）里也出现，
# 把真选项的 value 删掉照样能通过（上一位实现者实测到过这条假绿）。所以凡是要断言"渲染出来
# 长什么样"的，一律把真函数丢进 Node 里跑，再逐个断言**那一个元素 / 那一个值**。
WEB_INDEX = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "web", "index.html")


def _js_value(v):
    """JS 值字面量（走 JSON：中文 / 引号 / 反斜杠都不用手工转义）。"""
    return json.dumps(v, ensure_ascii=False)


def _js_fn_body(name, arg=""):
    """某个顶层函数的函数体（切不到就**立刻失败** —— 静默空跑出来的绿是假绿）。"""
    with open(WEB_INDEX, "r", encoding="utf-8") as f:
        js = f.read()
    head = "function %s(%s)" % (name, arg)
    start = js.index(head)
    return js[start:js.index("\n}", start)]


class ReportStateWriteOkTest(IsolatedCase):
    """`/api/report_state/write`：**有失败就绝不回 `ok: True`**（假 ok 比报错更糟）。

    为什么这条重要：前端只认 `ok !== true` —— `web/report_template.html:292` 是
    `if(!r || r.ok !== true) throw new Error((r && r.error) || '写入失败')`，
    再由 `applyCheck`（:293-297）的 catch **回滚勾选框**并告示「没存上，勾选已还原」。
    老写法在这里**无条件** `return {"ok": True, ...}`：勾没落盘也显示「已保存 ✓」，
    用户以为存上了、关掉程序就没了（正是不许假装存上的那一类）。
    所以这两条用例挡的是「把 ok 写死成 True」这个具体退化。
    """

    def _post(self, path, body):
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Length": str(len(raw)), "Content-Type": "application/json; charset=utf-8"}
        h = _sandbox_handler("POST", path, raw, headers)
        return h._status, h._body

    def test_a_failed_check_write_is_not_reported_as_saved(self):
        old = store.save_report_state
        store.save_report_state = mock.Mock(return_value=False)     # 勾选落盘失败
        self.addCleanup(setattr, store, "save_report_state", old)
        code, body = self._post("/api/report_state/write",
                                {"report_id": "r1", "checks": {"gate:0": True}})
        self.assertEqual(code, 200, body)          # 请求本身处理完了，只是这一项没写进去
        self.assertIs(body["ok"], False, "勾没落盘却回了 ok:true（界面会显示「已保存 ✓」）：%r" % (body,))
        self.assertEqual(body["saved"], 0)
        self.assertGreaterEqual(body["failed"], 1)
        self.assertIn("没存上", body["error"])
        self.assertIn("gate:0", body["error"])
        self.assertFalse(os.path.exists(store.report_state_path("r1")),
                         "夹具前提：这一项确实一个字节都没落盘")

    def test_corrupt_library_plus_failed_quarantine_is_reported_not_swallowed(self):
        """本批给 `upsert_check` 新增的那条 `ok=False`（坏库 + 留档失败）必须**如实回给前端**。

        这条路径以前是**不可达**的（`upsert_check` 只在 state/术语不合法时回 False，而那两种
        情况本来就不会被前端发上来），所以老写法「ok 写死 True」一直没暴露；现在它可达了：
        知识库读不出来、把坏文件留档改名也失败 → `(False, False, None)` → 用户的勾**真的丢了**。
        这里从路由层走一遍，钉住「丢了的勾会被说出来」，而不是当成成功。
        """
        store.write_json(store.knowledge_path(), {"nodes": []})       # 先有一个正常库
        with mock.patch.object(kb, "_quarantine_corrupt_kb", return_value=None):
            store.write_bytes(store.knowledge_path(), b'{"nodes": [ <<<CORRUPT>>>')  # 手改坏
            code, body = self._post("/api/report_state/write",
                                    {"report_id": "r1", "know": {"新概念": {"state": "已掌握"}}})
        self.assertEqual(code, 200, body)
        self.assertIs(body["ok"], False, "勾真的丢了却回了 ok:true：%r" % (body,))
        self.assertEqual(body["saved"], 0)
        self.assertEqual(body["failed"], 1)
        self.assertIn("新概念", body["error"])
        self.assertIn("留档", body["error"])
        # 坏文件必须原地保留（留档失败就不许动它）—— 测试也不许把它删掉
        with open(store.knowledge_path(), "rb") as f:
            self.assertIn(b"CORRUPT", f.read())

    def test_a_valid_know_check_still_reports_ok(self):
        """别把闸门做过头：正常勾选仍然必须是 `ok: true`（新加的 ok=False 只覆盖真失败）。"""
        code, body = self._post("/api/report_state/write",
                                {"report_id": "r1",
                                 "know": {"新概念": {"state": "已掌握", "definition": "定义"}}})
        self.assertEqual(code, 200, body)
        self.assertIs(body["ok"], True, body)
        self.assertEqual(body["saved"], 1)
        self.assertEqual(body["created"], 1)
        self.assertNotIn("error", body)
        self.assertEqual(store.load_knowledge()["nodes"][0]["state"], "已掌握")


def _need_node():
    """缺 Node **响亮失败**，绝不静默跳过（1.10③）。

    为什么改成红：原先这里 `raise unittest.SkipTest(...)`，而缺 Node 的机器上全量测试
    只会在末尾多一行 `skipped=…`、照样打印 OK —— 这批「真跑页面 JS」的渲染断言
    （本文件 30+ 处，含报告模板标注那批）就**静默消失**了。那种绿是假绿：
    改坏模板/页面的 JS 不会让任何测试变红。缺环境就该是红的，并且一眼看出缺什么、
    丢了哪些覆盖（Node 是开发机依赖：装一次即可，不是"这条测试不稳定"）。
    """
    if web_probe.NODE is None:
        raise AssertionError(
            "本环境缺 Node：`node` 不在 PATH 上，这批「真跑 web/ 页面 JS」的前端断言"
            "不能静默跳过。\n"
            "  · 丢掉的覆盖：tests/test_app.py 里全部 Node 渲染探针（简历下拉、JD 卡片、"
            "面试面板、报告模板标注等 30+ 处），以及 tests/test_analyze_knowledge_cache.py "
            "里模板文案那两条用例。\n"
            "  · 修法：装 Node 并确保 `node` 在 PATH 上（`node --version` 能跑）后重跑本批。\n"
            "  · **不要**把它改回 SkipTest —— 那等于把这批不变量删掉。"
            "确实要在无 Node 环境跑，就显式说明这次没验前端，并让 CI 在有 Node 的机器上跑。")


def _resumes_view(include_removed=True):
    """`/api/resumes` 的真实响应（用户看到的就是这份数据：`resumes` / `files` / `unregistered`）。"""
    path = "/api/resumes?include_removed=1" if include_removed else "/api/resumes"
    h = _sandbox_handler("GET", path, b"", {})
    return h._status, h._body


def _resume_text_view(vid, resp):
    """**真跑** `openResumeText()`（取回原文的渲染层）→ 回它渲染出来的标题 + 正文。

    响应**从参数给**：要断言的正是"同一份服务端响应在界面上被说成什么话"（D 的措辞就在这一层）。
    标题与正文都为空一律报错 —— 否则 `assertNotIn` / `assertIn` 会变成**恒真的假绿**。
    """
    _need_node()
    out = web_probe.run_index(
        "var q = [__fixture.resp];\n"
        "fetch = function(){ return Promise.resolve({ok:true, json:function(){"
        "return Promise.resolve(q.shift() || {});}}); };\n"
        "await openResumeText(__fixture.vid);\n"
        "out.title = String($('#rsViewTitle').textContent);\n"
        "out.body = String($('#rsViewBody').innerHTML);\n",
        {"vid": vid, "resp": resp})
    if not out.get("title") and not out.get("body"):
        raise AssertionError("取回原文的弹窗什么都没渲染（页面为空）：%r" % (out,))
    return out


def _picker_probe(view):
    """**真跑** `renderResumePicker()`，回下拉里的每一个 option + 浏览器会提交的那一份。

    提交那一份照 `#goBtn` 的**真实逻辑**（`web/index.html:2523-2533`）：按 `sel.value` 在
    `RESUME_PICKS` 里取一条 → `pick.name` 当 `resume_name`、`pick.version` 当 `resume_id`。
    不在 Python 里重抄一遍前端（旧写法按字段名摊平，在"一个族谱一项"之后已经不成立了）。

    空结果 = 渲染根本没发生 → 直接报错（否则断言会变成恒真的假绿）。
    """
    _need_node()
    out = web_probe.run_index(
        "renderResumePicker(__fixture);\n"
        "var sel = document.querySelector('#resumeSel');\n"
        "var pick = RESUME_PICKS.filter(function(p){ return p.key === String(sel.value); })[0] || null;\n"
        "out.options = __opts(sel);\n"
        "out.selected = String(sel.value);\n"
        "out.sent = {resume_name: pick ? pick.name : '', resume_id: (pick && pick.version) || ''};\n",
        view)
    if not out.get("options"):
        raise AssertionError("下拉一个选项都没有（渲染没有发生）—— 断言会变成恒真的假绿：%r" % (out,))
    return out


def _continue_modal(view, stem, content="改过的正文"):
    """**真跑** `rsAskContinue()`（续版弹窗），回它渲染出来的东西。

    空弹窗直接报错：不然「文案里没有某句话」这类断言会**恒真**（空字符串里什么都没有）。
    """
    _need_node()
    out = web_probe.run_index(
        "RESUME_VIEW = __fixture;\n"
        "var m = null;\n"
        "__fixture.resumes.forEach(function(v){\n"
        "  if(!v.removed && v.chosen && v.name === " + _js_value(stem) + ") m = v;\n"
        "});\n"
        "out.mother = m ? m.id : null;\n"
        "rsAskContinue(" + _js_value(stem + ".md") + ", " + _js_value(content) + ", m);\n"
        "out.body = String(document.querySelector('#rsPickBody').innerHTML);\n"
        "out.title = String(document.querySelector('#rsPickTitle').textContent);\n"
        "out.suggested = String(rsSuggestName(" + _js_value(stem) + "));\n",
        view)
    if not out.get("mother"):
        raise AssertionError("探针没在 fixture 里找到母版（用例的前提不对）：%r" % (out,))
    if not out.get("body"):
        raise AssertionError("续版弹窗什么都没渲染（页面为空）：%r" % (out,))
    return out


class ResumeSelectWiringTest(IsolatedCase):
    """简历下拉的数据来源必须与 `/api/resumes` 的**真实形状**对齐。

    用户真实数据的形状（Task 5 迁移之前）：`resumes/` 里有文件、`resumes.json` **不存在**
    → 接口回 `{"resumes": [], "files": ["周汉钦-母版.md"], "unregistered": ["周汉钦-母版.md"], …}`。
    `resumes` 是**版本记录对象数组**（`app.py` 的 `versions`），`files` 是**文件名数组**，
    `unregistered` 才是"盘上有文件但**还没有**版本记录"那一份（用户现在的全部简历）。

    阶段 4C / 修 1 起下拉是「**当前版 id 当 items 的 version**（分析请求据此带上 `resume_id` ——
    新查重粒度 (JD × 简历版本) 的另一半，漏了它生产里就等于"永不查重"）+ **文件名当 `name`**
    （`#goBtn` 把它当 `resume_name` 发出去）」。两条来源缺一不可：只读 `resumes` → 盘上那份没
    登记版本的简历看不见；只读 `files` → 没有版本 id。

    **怎么"读"前端**：真跑 `renderResumePicker()`（`_picker_probe`），再照 `#goBtn` 的逻辑取
    出它会提交的那一份。这样字段名读错 / 那条 `<option>` 丢了 value 都会立刻红 —— 比在 Python
    里按字段名摊平（旧写法）可靠：后者既不覆盖"一个族谱一项"的新语义，也看不见真实渲染。
    源码形状那一条（`<option value="'+esc(p.key)+'">`）另外由
    `test_the_option_template_carries_the_version_id_as_value` 钉住，且**精确到那一行**。
    """

    def _handler(self, method, path):
        h = _sandbox_handler(method, path, b"", {})
        return h._status, h._body

    def _get(self, path):
        return self._handler("GET", path)

    def _sent(self, response):
        """浏览器**真的会发出去**的那一份（真跑页面的 JS，见 `_picker_probe`）。"""
        return _picker_probe(response)["sent"]

    def test_the_option_template_carries_the_version_id_as_value(self):
        """丢掉 `<option value=…>` 时浏览器提交的只有 option 文本 → 分析请求里再不会有
        `resume_id` = **静默失效**（不报错，只是查重粒度没了）。这条只钉源码形状；
        真渲染（`option.value` 实际是什么）由 `_picker_probe` 断言。

        ⚠️ 断言必须**精确到那一行**：`'<option value="'` 这个前缀在占位选项
        （`'<option value="">（无简历，请先新建）</option>'`）里也有 —— 只判前缀会**假绿**
        （已用变异 `optval` 实测：把条目那一行的 value 删掉，宽松断言照样通过）。
        所以这里连上 `esc(p.key)`：value 必须真的来自**这一条**。
        """
        body = _js_fn_body("renderResumePicker", "r")
        self.assertIn("""'<option value="'+esc(p.key)+'">'+esc(p.name)+'</option>'""", body,
                      "下拉条目那一行的 <option> 丢了 value（或 value 不再来自版本 id）：resume_id 发不出去")
        self.assertIn("resumePickOf(", body, "下拉条目不再经过 resumePickOf()：value/文本的来源没了保证")

    def test_disk_only_shape_shows_the_file_and_submits_the_file_name(self):
        store.ensure_profile()
        store.write_text(os.path.join(store.p_path("resumes"), "周汉钦-母版.md"), "老简历正文")
        self.assertFalse(os.path.exists(store.p_path("resumes.json")))     # 用户真实数据的形状
        code, body = self._get("/api/resumes")
        self.assertEqual(code, 200)
        self.assertEqual(body["resumes"], [])                             # 版本库还是空的
        self.assertEqual(body["files"], ["周汉钦-母版.md"])                # 盘上的真话在这儿
        self.assertEqual(body["unregistered"], ["周汉钦-母版.md"])          # 还没登记版本的就是它
        sent = self._sent(body)
        self.assertEqual(sent, {"resume_name": "周汉钦-母版.md", "resume_id": ""},
                         "盘上那份简历没有出现在下拉里：字段读错了（body=%r）" % (body,))
        self.assertNotEqual(sent["resume_name"], "[object Object]")        # ← 修 1 之前的实测症状
        self.assertTrue(os.path.exists(os.path.join(store.p_path("resumes"), sent["resume_name"])),
                        "发出去的 resume_name 在盘上找不到 → do_analyze 会回「找不到简历」")
        self.assertEqual(sent["resume_id"], "")                            # 没有版本可钉 → 如实不传

    def test_a_registered_version_is_picked_by_version_id(self):
        v1 = resumelib.create_version("周汉钦-母版", "登记过的正文")
        code, body = self._get("/api/resumes")
        self.assertEqual(code, 200)
        self.assertEqual([r["id"] for r in body["resumes"]], [v1["id"]])   # 记录是**对象**数组
        self.assertEqual(body["files"], ["周汉钦-母版.md"])
        self.assertEqual(body["unregistered"], [])                         # 有版本记录 → 不重复出现
        sent = self._sent(body)
        self.assertEqual(sent, {"resume_name": "周汉钦-母版.md", "resume_id": v1["id"]},
                         "下拉没有把**版本 id** 当 value → 分析请求不会带 resume_id")


class ResumePickerOneOptionPerLineageTest(IsolatedCase):
    """修 1（前端）：**一份简历（一个族谱）只给一个选项**，且它指向该族谱的**当前版**。

    为什么不能每版一个选项：`do_analyze` **永远读 `resumes/<名字>.md`**（= 该族谱的最新版正文），
    而 `resume_id` 是要写进报告与 `jobs.json` 的「这次用了哪一版」。3 版族谱在旧实现下会渲染出
    三行同名选项（value = rv_0001 / rv_0002 / rv_0003），用户（或默认选中的第一项 = 最老那一版）
    于是拿到「记录说用了 rv_0001、模型读的是 rv_0003 正文」的**假话**；`resume_id` 又是查重的
    另一半粒度 → 同 JD 同族谱**误 409**，而前端没有 409 出口 → 死路。

    已移出的族谱**一个选项都不给**（移出 = 不能继续用）；`unregistered`（盘上有文件、还没登记
    版本）那条路保持不变：value 退成 `file:<文件名>`，分析请求如实**不带** `resume_id`。
    """

    def test_three_versions_give_exactly_one_option_holding_the_current_id(self):
        v1 = resumelib.create_version("周汉钦-母版", "第 1 版正文")
        v2 = resumelib.create_version("周汉钦-母版", "第 2 版正文", parent_id=v1["id"])
        v3 = resumelib.create_version("周汉钦-母版", "第 3 版正文", parent_id=v2["id"])
        code, view = _resumes_view()
        self.assertEqual(code, 200, view)
        opts = _picker_probe(view)["options"]
        mine = [o for o in opts if o["text"] == "周汉钦-母版.md"]
        self.assertEqual(len(mine), 1, "同一族谱给了不止一个选项：%r" % (opts,))
        self.assertEqual(mine[0]["value"], v3["id"])            # ← 逐字等于当前版 id
        values = [o["value"] for o in opts]
        for historical in (v1["id"], v2["id"]):
            self.assertNotIn(historical, values,
                             "历史版 %s 还能被选中 → 记录里那句「用了哪一版」会是假话" % historical)

    def test_the_current_version_is_what_the_same_lineage_reports_as_current(self):
        """两边**同一个口径**：下拉给的那条 == `resumelib.current_of(root)`。"""
        v1 = resumelib.create_version("周汉钦-母版", "第 1 版正文")
        v2 = resumelib.create_version("周汉钦-母版", "第 2 版正文", parent_id=v1["id"])
        code, view = _resumes_view()
        self.assertEqual(code, 200, view)
        picked = _picker_probe(view)["selected"]
        self.assertEqual(picked, resumelib.current_of(v2["root_id"])["id"])

    def test_a_removed_lineage_gets_no_option_at_all(self):
        va = resumelib.create_version("甲", "甲正文")
        vb = resumelib.create_version("乙", "乙正文")
        resumelib.remove(va["id"])
        code, view = _resumes_view()
        self.assertEqual(code, 200, view)
        self.assertEqual(view["unregistered"], [], "移出的名字不该同时出现在「还没登记版本」里")
        opts = _picker_probe(view)["options"]
        self.assertEqual([o["text"] for o in opts], ["乙.md"], "已移出的族谱还在下拉里：%r" % (opts,))
        self.assertEqual([o["value"] for o in opts], [vb["id"]])

    def test_the_unregistered_path_still_gives_exactly_one_option_without_a_version(self):
        store.ensure_profile()
        store.write_text(os.path.join(store.p_path("resumes"), "盘上简历.md"), "正文")
        code, view = _resumes_view()
        self.assertEqual(code, 200, view)
        probe = _picker_probe(view)
        self.assertEqual(probe["options"], [{"value": "file:盘上简历.md", "text": "盘上简历.md"}])
        self.assertEqual(probe["sent"], {"resume_name": "盘上简历.md", "resume_id": ""})


class ResumePickerKeepsTheSelectionTest(IsolatedCase):
    """修 2：重渲染**不许**清掉用户已选的简历。

    `loadResumes()` 每次都重写 `sel.innerHTML`，浏览器于是把 select 打回**第一项**（= 最早那一版）。
    本次新增了 5 个触发点（★ 选定 / 移出 / 彻底删 / 恢复 / 保存），于是"去简历 tab 点一下 ★ 或
    保存一下"之后，回分析页看到的下拉已经**静默换成另一项** —— 用户可能分析**错的简历/版本**。
    要求：原值还在新列表里就保留；**原值没了**才回落到第一项（不能卡在一个不存在的值上）。
    """

    def _two(self):
        va = resumelib.create_version("甲", "甲正文")
        vb = resumelib.create_version("乙", "乙正文")
        code, view = _resumes_view()
        self.assertEqual(code, 200, view)
        return va, vb, view

    def test_the_second_option_survives_a_rerender(self):
        va, vb, view = self._two()
        _need_node()
        out = web_probe.run_index(
            "renderResumePicker(__fixture);\n"
            "var sel = document.querySelector('#resumeSel');\n"
            "var before = __opts(sel);\n"
            "out.before = before;\n"
            "sel.value = before[1].value;\n"                       # 用户选了第 2 项
            "out.picked = String(sel.value);\n"
            "renderResumePicker(__fixture);\n"                     # 任意一个触发点重渲染
            "out.after = String(sel.value);\n"
            "out.afterOptions = __opts(sel);\n",
            view)
        before = out["before"]
        self.assertEqual(len(before), 2, "fixture 不对：应有 2 个选项，实际 %r" % (before,))
        self.assertEqual(before[1]["value"], vb["id"])              # 第 2 项 = 乙
        self.assertEqual(out["picked"], vb["id"], "前置条件：真的选中了第 2 项")
        self.assertEqual(out["after"], vb["id"], "重渲染把用户已选的简历清掉了")
        self.assertNotEqual(out["after"], before[0]["value"], "重渲染后跳回了第一项")
        self.assertEqual([o["value"] for o in out["afterOptions"]], [va["id"], vb["id"]])

    def test_a_vanished_selection_falls_back_to_the_first_option(self):
        """原值**不在**新列表里（那一族谱被移出）→ 如实回落到第一项，不保留一个已不存在的值。"""
        va, vb, view = self._two()
        _need_node()
        out = web_probe.run_index(
            "renderResumePicker(__fixture);\n"
            "var sel = document.querySelector('#resumeSel');\n"
            "sel.value = __opts(sel)[1].value;\n"
            "out.picked = String(sel.value);\n"
            "__fixture = {ok: true, resumes: __fixture.resumes.filter(function(v){\n"
            "  return String(v.name) !== " + _js_value("乙") + ";\n"
            "}), unregistered: []};\n"
            "renderResumePicker(__fixture);\n"
            "out.after = String(sel.value);\n"
            "out.afterOptions = __opts(sel);\n",
            view)
        self.assertEqual(out["picked"], vb["id"])
        self.assertEqual([o["value"] for o in out["afterOptions"]], [va["id"]])
        self.assertEqual(out["after"], va["id"], "选中的那一项没了却还留着旧值（下拉会提交空简历）")


class ResumeContinueModalTest(IsolatedCase):
    """续版弹窗（修 3 / 修 5 / 修 6②）：不承诺做不到的事；预览的次数与真正落盘**同一口径**。"""

    def _family(self):
        """真族谱 v1 → v2 → v3，母版选定 v2。"""
        v1 = resumelib.create_version("族谱", "第 1 版正文")
        v2 = resumelib.create_version("族谱", "第 2 版正文", parent_id=v1["id"])
        v3 = resumelib.create_version("族谱", "第 3 版正文", parent_id=v2["id"])
        resumelib.choose(v2["id"])
        return v1, v2, v3

    def test_the_snapshot_promise_is_conditional(self):
        """修 3 / Task 1.9：快照**不是**无条件写的，也**不是**"与记录不符就不写"。

        盘上那份与记录不符时字节**照样留**（存档 ≠ 冒充）；**两种情形**才不留：
        ① `missing`（这一版原文**从来没保留过**）；② 盘上那份**已经不是这一版**
        （快照已经是好的 / 两边都不是这一版）—— 那它就不是这一版的原文（与
        `resumelib.py:43-46` 的两条口径、以及弹窗自己写的两种情形一致）。
        弹窗只能承诺做得到的事：说清「什么时候才**记为**母版原文的存档」、
        「不符时字节**照样保留**」、「**从来没保留过**时才不留」。
        """
        self._family()
        code, view = _resumes_view()
        self.assertEqual(code, 200, view)
        body = _continue_modal(view, "族谱")["body"]
        # 化石清理（Task 1.10 第 8 条）：原来钉的那句「覆盖前先把母版原文留成快照」**只在**
        # `43c2c8f:web/index.html` 出现过、且早在 `cbf87cb` 就被删掉了 —— 从本任务基线起，
        # 这个字面量只存在于这条断言自己里，等于永远为真的空断言。
        # 换成 `cbf87cb` 真正写进过界面的旧口径：「只有母版原文可用」时才留快照
        # （它承诺了下半句「与记录不符时不写快照」，那条 Task 1.9 已实测推翻 —— 现在照样留，只是不冒充）。
        self.assertNotIn("只有母版原文可用", body,
                         "旧口径又回来了：它承诺「只有母版原文可用时才留快照」，"
                         "而 Task 1.9 已推翻「与记录不符就不写」—— 现在不符时字节照样留（只是不冒充）")
        self.assertNotIn("不写快照", body,
                         "Task 1.9：盘上那份与记录不符时**字节照样留** ——"
                         "老文案「与记录不符时不写快照」是句假话，不许再回到弹窗里")
        self.assertIn("就是母版原文", body, "没有说清「什么时候才记为母版原文的存档」")
        self.assertIn("照样保留", body, "没有说清「与记录不符时字节照样保留」")
        self.assertIn("从来没保留过", body, "没有说清「什么时候才不留」")
        # C-1（Task 1.10c）：留档的实现是**直接写一份副本**（`_write_side`），不是改名。
        # 弹窗（用户可见）曾写"先改名留档" —— 那是**上一版实现**的说法，而且正是复查实测的
        # 丢数据路径（改名失败时旧快照还在原地等着被盖掉）。文案必须与实现一致。
        self.assertNotIn("改名", body,
                         "弹窗又写成「改名留档」了：实现是**直接写一份副本**，"
                         "改名一旦失败旧快照就还在原地等着被覆盖（复查实测的不可逆丢数据路径）")

    def test_the_preview_revision_matches_what_is_really_written(self):
        """修 5：预览与 `create_version` **同一口径（含已移出）**。

        造一个「最高的那一版**已移出**」的族谱：旧口径（只看未移出）预览「第 3 次修改」，
        而 `create_version` 用**含已移出**的最大 rev 真的写出「第 4 次修改」——预览比落盘小。
        """
        v1, v2, v3 = self._family()
        resumelib.remove(v3["id"])                              # 最高的那一版已移出
        code, view = _resumes_view()
        self.assertEqual(code, 200, view)
        body = _continue_modal(view, "族谱")["body"]
        real = resumelib.create_version("族谱", "第 4 版正文", parent_id=v2["id"])
        self.assertEqual(real["rev"], 4, "落盘口径变了，这条用例的前提要重看：%r" % (real,))
        self.assertIn("<b>第 %d 次修改</b>" % real["rev"], body,
                      "预览的次数与真正落盘的不一致（预览 %r，实际第 %d 次）"
                      % (body, real["rev"]))

    def test_the_suggested_name_uses_the_first_free_suffix(self):
        """修 6②：建议名从**第一个空闲后缀**起（空闲的 `-1` 也要用，不能从 `-2` 起跳过它）。"""
        resumelib.create_version("甲", "甲正文")
        resumelib.create_version("乙-1", "乙正文")
        store.ensure_profile()
        store.write_text(os.path.join(store.p_path("resumes"), "丙-1.md"), "丙正文")
        code, view = _resumes_view()
        self.assertEqual(code, 200, view)
        _need_node()
        out = web_probe.run_index(
            "RESUME_VIEW = __fixture;\n"
            "out.free = String(rsSuggestName(" + _js_value("甲") + "));\n"
            "out.taken = String(rsSuggestName(" + _js_value("乙") + "));\n"
            "out.unreg = String(rsSuggestName(" + _js_value("丙") + "));\n",
            view)
        self.assertEqual(out["free"], "甲-1.md", "`-1` 空着却跳到了别的后缀")
        self.assertEqual(out["taken"], "乙-2.md")               # 乙-1 已被一个未移出的版本占着
        self.assertEqual(out["unreg"], "丙-2.md")               # 丙-1 被盘上那份没登记版本的文件占着


class ResumePurgeWordingTest(IsolatedCase):
    """修 4：彻底删的文案要说清**到底删了什么**、**什么还留在盘上**。

    `resumelib.purge` 只删「这条记录 + **这一版自己的**快照」；这一版自己的内容文件
    `resumes/<名字>.md`（整个族谱的最新版容器）**留在盘上**，当这个名字再没有任何登记版本时，
    它会以「盘上有文件 · 还没登记版本」的形态**重新出现在列表里**（`app.py:1542`）。
    旧文案只说「别的版本 / 别的简历的文件一律不动」，用户会以为"我的文件被删了"，看到一个
    自己的文件又冒出来时只会更慌。
    """

    def test_the_second_confirmation_says_what_stays_on_disk(self):
        _need_node()
        out = web_probe.run_index(
            # 第一句「是」→ 去看第二句；第二句「否」→ 就此打住（一个请求都不发）
            "__confirmQueue.push(true, false);\n"
            "purgeResumeVersion('rv_0003', " + _js_value("族谱.md 第 3 次修改") + ");\n"
            "out.confirms = __confirms.slice();\n")
        msgs = out["confirms"]
        self.assertEqual(len(msgs), 2, "两次确认的文案没走到第二句：%r" % (msgs,))
        second = msgs[1]
        self.assertIn("记录", second)                 # 删了什么：这条记录
        self.assertIn("快照", second)                 # 删了什么：它自己的快照
        self.assertIn("留在盘上", second)              # 什么还在
        self.assertIn("resumes/", second)             # 说清是哪个文件还在
        self.assertIn("还没登记版本", second)          # 会以什么形态重新出现
        self.assertIn("重新出现", second)


class ResumeUnregisteredRowTest(IsolatedCase):
    """修 6①：`rsUnregRowHtml` 写了 `data-unreg` 却**从不绑定**（死属性）→ 删掉，行照旧渲染。

    断言切的是**这一个函数体**（不是"整页里有没有某字符串"），并且配一条正向断言：行还在、
    「编辑」入口还在（`renderResumeTree` 里那行 `[data-edit]` 绑定就是它的入口）。
    """

    def test_the_row_no_longer_carries_a_dead_attribute(self):
        body = _js_fn_body("rsUnregRowHtml", "n")
        self.assertNotIn("data-unreg", body, "死属性又回来了：写了但没有任何地方绑定它")
        self.assertIn("data-edit=", body, "行的「编辑」入口没了（那一行才是它的入口）")
        self.assertIn("盘上有文件 · 还没登记版本", body)
        self.assertIn("list.querySelectorAll('[data-edit]')", _js_fn_body("renderResumeTree", "r"),
                      "「编辑」按钮不再被绑定 → 这一行就没救了")


class ResumeLoadShapeGuardTest(IsolatedCase):
    """修 6③：`loadResumes()` 的 **render 也要兜住**（200 但缺字段 → 结构化错误提示，不许抛）。

    为什么必须兜：调用点多数**没有 await**（`web/index.html:2605` 的 `loadResumes();`），
    抛出去就是一条没人接的 rejected promise —— 界面上什么都不显示，用户以为"我没有简历"。
    这条用 Node 探针**真跑**，并把 `unhandledRejection` 一并收上来断言：那条假绿正是
    "抛了但你看不见"。
    """

    def _load(self, response):
        _need_node()
        return web_probe.run_index(
            "__fetchJson = " + _js_value(response) + ";\n"
            "loadResumes();\n"                       # ← 故意**不 await**：页面里就是这么调的
            "await new Promise(function(r){ setTimeout(r, 20); });\n"
            "out.list = String(document.querySelector('#resumeList').innerHTML);\n"
            "out.sel = String(document.querySelector('#resumeSel').innerHTML);\n")

    def test_a_200_without_the_resumes_field_shows_a_structured_error(self):
        out = self._load({"ok": True})               # 200，但**没有** resumes / unregistered
        self.assertEqual(out["unexpected"], [], "未 await 的调用抛了一条没人接的 rejected promise")
        self.assertIn("简历列表读取失败", out["list"], "缺字段时界面没有明说读取失败：%r" % (out,))
        self.assertIn("resumes", out["list"], "没有说清缺的是哪个字段：%r" % (out["list"],))
        self.assertIn("简历读取失败", out["sel"], "下拉没有显示读取失败：%r" % (out["sel"],))

    def test_a_render_that_throws_is_caught_instead_of_rejecting(self):
        out = self._load({"ok": True, "resumes": [None], "unregistered": []})   # 渲染时必抛
        self.assertEqual(out["unexpected"], [], "渲染失败抛成了一条没人接的 rejected promise")
        self.assertIn("简历列表渲染失败", out["list"], "渲染失败没有被转成结构化提示：%r" % (out,))


# ---------------- 阶段 4C / Task 3b：6 维度并排表 + 409 出口 + 两处收尾 ----------------
#
# 这一段的断言原则（历次假绿的教训，逐条照做）：
#   ① 表要**逐格**断言（尤其那个故意缺维的格子：必须空 + 标注，**不是 0**）；
#   ② **绝不写"整页 HTML 里有没有某字符串"** —— 那句话可能就在源码里；
#   ③ 409 的两面都要证明：换简历**不弹**（后端 200）、同一版**才弹**（后端 409）；
#   ④ 前端拿到的响应体一律是**真后端产出的那一份**（口径一致：测试喂的形状生产真的到得了）。

_DIM_PARSE_JS = r"""
function attrOf(s, n){
  var r = new RegExp('\\b' + n + '="([^"]*)"').exec(String(s));
  return r ? r[1] : '';
}
function cellsOf(trHtml){
  var cells = [], re = /<(td|th)\b([^>]*)>([\s\S]*?)<\/\1>/g, m;
  while((m = re.exec(trHtml))){
    var a = m[2];
    cells.push({tag: m[1], dim: attrOf(a, 'data-dim'), label: attrOf(a, 'data-dimlabel'),
                score: attrOf(a, 'data-score'),
                missing: attrOf(a, 'data-missing'), noscore: attrOf(a, 'data-noscore'),
                noread: attrOf(a, 'data-noread'),
                text: m[3].replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim()});
  }
  return cells;
}
function parseDimTable(html, jid){
  var i = String(html).indexOf('data-dimtab="' + jid + '"');
  if(i < 0) return null;
  var s = String(html).lastIndexOf('<table', i), e = String(html).indexOf('</table>', i);
  if(s < 0 || e < 0) return null;
  var t = String(html).slice(s, e), rows = [], re = /<tr\b([^>]*)>([\s\S]*?)<\/tr>/g, m;
  while((m = re.exec(t))){
    rows.push({rid: attrOf(m[1], 'data-rid'), report: attrOf(m[1], 'data-report'),
               versionUnknown: attrOf(m[1], 'data-version-unknown'),
               scoresError: attrOf(m[1], 'data-scores-error'), cells: cellsOf(m[2])});
  }
  return rows;
}
function scoreLinesOf(html){
  var out = [], re = /<div class="rsline" data-scoreline="1"([^>]*)>([\s\S]*?)<\/div>/g, m;
  while((m = re.exec(String(html)))){
    out.push({job: attrOf(m[1], 'data-job'), score: attrOf(m[1], 'data-score'),
              at: attrOf(m[1], 'data-at'), report: attrOf(m[1], 'data-report'),
              text: m[2].replace(/<[^>]*>/g, '').replace(/\s+/g, ' ').trim()});
  }
  return out;
}
"""

DIM_TEAM = "团队/供应商管理"
DIM_QUALITY = "质量体系能力"
DIM_ANNO = "标注生产经验"
DIM_AI = "AI 认知"
DIM_PROD = "产品/需求能力"
DIM_IND = "行业经验"


def _dim(dim, score, weight=10):
    return {"dimension": dim, "weight": weight, "score": score}


class LedgerWithReportsCase(_AnalyzeRouteCase):
    """一个 JD 挂多份分析，每份带**真报告文件**（`REPORT_DATA.scores` 是真读出来的）。"""

    JD_ID = "本科_字节_标注_文本_北京_6-8K_0001"
    JD_TEXT = _AnalyzeRouteCase.LONG

    def _report_file(self, name, scores, resume=""):
        store.ensure_profile()
        data = {"job_title": "语音文本评测", "score": 70, "resume": resume, "scores": scores}
        store.write_text(os.path.join(store.p_path("reports"), name),
                         "<html><body><script>\nconst REPORT_DATA = %s;\n</script></body></html>"
                         % json.dumps(data, ensure_ascii=False))

    def _analysis(self, rec_id, resume, resume_id, report, score, created_at, scores=None,
                  write_report=True):
        jdid.upsert_jd({"id": self.JD_ID, "job_title": "语音文本评测", "salary": "13-26K",
                        "company": "示例公司", "location": "北京", "status": "未投递",
                        "jd_text_file": self.JD_ID + ".txt"})
        store.ensure_profile()
        store.write_text(os.path.join(store.p_path("jd"), self.JD_ID + ".txt"), self.JD_TEXT)
        if write_report:
            self._report_file(report, scores if scores is not None else [])
        rec = {"id": rec_id, "jd_id": self.JD_ID, "report": report, "created_at": created_at,
               "score": score, "resume": resume}
        if resume_id:
            rec["resume_id"] = resume_id
        app._save_analysis(rec)

    def _row(self):
        code, body = self._get("/api/jds")
        self.assertEqual(code, 200, body)
        rows = body["jds"]
        self.assertEqual(len(rows), 1, "夹具不对：应当只有 1 个 JD，实际 %r" % (rows,))
        return rows[0]


class JdAnalysisScoresShapeTest(LedgerWithReportsCase):
    """后端给前端的形状：`analyses[]` 必须带 `resume_id` 与**真从报告读出来**的 `scores`。

    并排表与「取回历史版要看当时的分数」都吃这两项。**读不出就如实标 `scores_error`** ——
    绝不返回 `[]` 冒充"这一版一个维度都没评"（那是两句不同的话）。
    """

    def test_readable_scores_come_from_the_report_and_carry_the_pinned_version(self):
        va = self._version("简历A")
        self._analysis("a1", "简历A", va["id"], "a1.html", 75.0, "2026-10-01 10:00",
                       [_dim(DIM_TEAM, 5, 25), _dim(DIM_AI, 8, 15)])
        row = self._row()
        a = row["analyses"][0]
        self.assertEqual(a["resume_id"], va["id"])                     # 钉住哪一版
        self.assertEqual(a["scores"],
                         [{"dimension": DIM_TEAM, "weight": 25, "score": 5},
                          {"dimension": DIM_AI, "weight": 15, "score": 8}])
        self.assertEqual(a["scores_error"], "")

    def test_an_unreadable_report_says_so_instead_of_pretending_no_dimensions(self):
        va = self._version("简历A")
        self._analysis("a1", "简历A", va["id"], "a1.html", 75.0, "2026-10-01 10:00",
                       write_report=False)                             # 报告文件根本不在
        a = self._row()["analyses"][0]
        self.assertIsNone(a["scores"], "读不出却回了一个列表：界面会把「读不到」说成「没评」")
        self.assertIn("不在盘上", a["scores_error"])
        self.assertIn("a1.html", a["scores_error"])

    def test_an_old_analysis_without_a_resume_id_reports_an_empty_version(self):
        self._analysis("a1", "旧简历", "", "a1.html", 66.0, "2026-08-01 09:00", [])
        a = self._row()["analyses"][0]
        self.assertEqual(a["resume_id"], "")          # 空串 = 版本未知，不是猜一个出来
        self.assertEqual(a["scores"], [])


class _DimTableCase(LedgerWithReportsCase):
    """并排表两条用例共用：真跑 `renderJobs()`，再把**渲染出来的** `<table>` 逐格解析回来。"""

    def _render(self, row, extra=""):
        _need_node()
        out = web_probe.run_index(
            _DIM_PARSE_JS +
            "JOBS = [__fixture.row];\n"
            "renderJobs();\n"
            "var html = String(document.querySelector('#jobList').innerHTML);\n"
            "out.cardHasTable = html.indexOf('data-dimtab=') >= 0;\n"
            "out.rows = parseDimTable(html, String(__fixture.row.id));\n"
            "out.matrix = jdDimMatrix(__fixture.row);\n"
            "out.dims = jdDimColumns(jdAnalyses(__fixture.row));\n"
            + extra,
            {"row": row})
        if not out.get("rows"):
            raise AssertionError("大卡片里没渲染出并排表（渲染没发生 → 断言会变成恒真的假绿）：%r" % (out,))
        return out


class JdDimensionTableTest(_DimTableCase):
    """用户决策 #3：展开 JD 卡片后的**6 维度并排表**（一行一份简历、一列一个维度）。

    维度取法（**确定规则**）：该 JD 下所有分析的维度名**并集**，按**出现次数降序**排，
    次数相同按首次出现顺序。某个分析缺某一维 → 那一格**留空 + 标注「该版未评这一维」**，
    **绝不补 0**（0 分是"评了很差"，跟"没评"是两件事 —— 夹具里两种都有）。
    """

    # 三份简历的维度**故意各不相同**：
    #   A 全评 6 维；B 少「标注生产经验」；C 少「行业经验」，且「产品/需求能力」是**真 0 分**
    A = [_dim(DIM_TEAM, 5, 25), _dim(DIM_QUALITY, 8, 22), _dim(DIM_ANNO, 8, 20),
         _dim(DIM_AI, 8, 15), _dim(DIM_PROD, 6, 10), _dim(DIM_IND, 6, 8)]
    B = [_dim(DIM_AI, 9), _dim(DIM_QUALITY, 7), _dim(DIM_TEAM, 4), _dim(DIM_IND, 5), _dim(DIM_PROD, 3)]
    C = [_dim(DIM_ANNO, 10), _dim(DIM_QUALITY, 9), _dim(DIM_TEAM, 3), _dim(DIM_AI, 2), _dim(DIM_PROD, 0)]

    # 出现次数：团队 3 / 质量 3 / AI 3 / 产品 3 / 标注 2 / 行业 2
    # → 次数降序 + 首次出现顺序（A 的顺序）：
    EXPECT_DIMS = [DIM_TEAM, DIM_QUALITY, DIM_AI, DIM_PROD, DIM_ANNO, DIM_IND]
    # 列键是**归一后**的（`jdDimKey`：NFKC + 去空白），所以 DIM_AI 的列键没有那个空格；
    # 显示用的原始写法另算（这里每维只有一种写法，两者只差 AI 这个空格）。
    EXPECT_KEYS = [DIM_TEAM, DIM_QUALITY, "AI认知", DIM_PROD, DIM_ANNO, DIM_IND]

    def _three(self):
        va = self._version("简历A"); vb = self._version("简历B"); vc = self._version("简历C")
        self._analysis("a1", "简历A", va["id"], "a1.html", 75.0, "2026-10-01 10:00", self.A)
        self._analysis("a2", "简历B", vb["id"], "a2.html", 66.0, "2026-10-02 11:00", self.B)
        self._analysis("a3", "简历C", vc["id"], "a3.html", 58.0, "2026-10-03 12:00", self.C)
        return va, vb, vc

    def test_every_cell_is_asserted_one_by_one(self):
        va, vb, vc = self._three()
        out = self._render(self._row())
        self.assertEqual(out["unexpected"], [], "渲染时抛了没人接的 promise")

        # ① 列：并集 + 出现次数降序（标注生产经验在 A 里最先出现，但只有 2 份评了 → 排后面）
        self.assertEqual(out["dims"], self.EXPECT_KEYS)
        cells = out["rows"][0]["cells"]
        self.assertEqual([c["text"] for c in cells], ["简历 \\ 维度"] + self.EXPECT_DIMS)
        # 角格不是维度 → 它的 dim / label 都是空串（表头每一格的显示文案见上面那条 text 断言）
        self.assertEqual([c["label"] for c in cells], [""] + self.EXPECT_DIMS)
        self.assertEqual([c["dim"] for c in cells], [""] + self.EXPECT_KEYS)

        # ② 逐行：表头（简历名 / 版本号 / 分数）
        body = out["rows"][1:]
        self.assertEqual(len(body), 3, "表只渲染了 %d 份简历（应当 3 份）" % len(body))
        self.assertEqual([r["rid"] for r in body], [va["id"], vb["id"], vc["id"]])
        self.assertEqual([r["versionUnknown"] for r in body], ["", "", ""])
        heads = [r["cells"][0]["text"] for r in body]
        self.assertEqual(heads, ["简历A · 75 分 %s" % va["id"],
                                 "简历B · 66 分 %s" % vb["id"],
                                 "简历C · 58 分 %s" % vc["id"]])

        # ③ 逐格：3 行 × 6 列，一个都不放过
        want = [
            # 团队5 质量8 AI8 产品6 标注8 行业6
            [("score", "5"), ("score", "8"), ("score", "8"), ("score", "6"), ("score", "8"), ("score", "6")],
            # 团队4 质量7 AI9 产品3 **标注缺** 行业5
            [("score", "4"), ("score", "7"), ("score", "9"), ("score", "3"), ("missing", ""), ("score", "5")],
            # 团队3 质量9 AI2 **产品真 0 分** 标注10 **行业缺**
            [("score", "3"), ("score", "9"), ("score", "2"), ("score", "0"), ("score", "10"), ("missing", "")],
        ]
        for ri, row in enumerate(body):
            got = []
            for ci, cell in enumerate(row["cells"][1:]):
                self.assertEqual(cell["dim"], self.EXPECT_KEYS[ci],
                                 "第 %d 行第 %d 列的列键不是这一维：%r" % (ri, ci, cell))
                # 经过归一之后，那一格显示的**原始写法**必须仍是这一维（不许把中文替掉/换了）
                self.assertEqual(cell["label"], self.EXPECT_DIMS[ci],
                                 "第 %d 行第 %d 列显示的原始写法不对：%r" % (ri, ci, cell))
                if cell["missing"] == "1":
                    got.append(("missing", ""))
                else:
                    got.append(("score", cell["score"]))
            self.assertEqual(got, want[ri], "第 %d 行逐格对不上" % ri)

        # ④ 那个**故意缺维**的格子：空 + 标注，**不是 0**
        miss_b = body[1]["cells"][5]
        self.assertEqual(miss_b["missing"], "1")
        self.assertEqual(miss_b["score"], "", "缺维那格带了分数：%r" % (miss_b,))
        self.assertEqual(miss_b["text"], "该版未评这一维")
        self.assertNotIn("0", miss_b["text"], "缺维被补成了 0：%r" % (miss_b,))
        miss_c = body[2]["cells"][6]
        self.assertEqual((miss_c["missing"], miss_c["score"], miss_c["text"]),
                         ("1", "", "该版未评这一维"))
        self.assertNotIn("0", miss_c["text"])

        # ⑤ 反向：**真 0 分**必须照旧显示 0（不许被"缺维"那条路吞掉）
        zero = body[2]["cells"][4]
        self.assertEqual(zero["missing"], "")
        self.assertEqual((zero["score"], zero["text"]), ("0", "0"))

        # ⑥ 表结构：表头 7 格（1 个角 + 6 维），行数 == 分析数（渲染产物与数据两边同一口径）
        self.assertEqual(len(out["rows"]), 4)                       # 1 表头 + 3 行
        self.assertEqual(len(out["matrix"]["rows"]), len(body))

    def test_the_column_rule_is_count_then_first_seen(self):
        """维度取法**单独钉一遍**（顺序错了上面那条也会红，这条让"为什么是这个顺序"看得见）。"""
        va, vb, vc = self._three()
        row = self._row()
        _need_node()
        out = web_probe.run_index(
            _DIM_PARSE_JS +
            "out.dims = jdDimColumns(jdAnalyses(__fixture.row));\n"
            "out.one = jdDimColumns([{scores: [{dimension: '只有这一维', score: 1}]}]);\n"
            "out.none = jdDimColumns([{scores: []}, {scores: null}]);\n",
            {"row": row})
        self.assertEqual(out["dims"], self.EXPECT_KEYS)
        self.assertEqual(out["one"], ["只有这一维"])
        self.assertEqual(out["none"], [])

    def test_a_report_that_cannot_be_read_is_not_reported_as_unrated(self):
        """报告读不出 ≠ 这一版没评。**不许**把前者写成「该版未评这一维」。"""
        va = self._version("简历A")
        vb = self._version("简历B")
        self._analysis("a1", "简历A", va["id"], "a1.html", 75.0, "2026-10-01 10:00", self.A)
        self._analysis("a2", "简历B", vb["id"], "a2.html", 66.0, "2026-10-02 11:00", write_report=False)
        out = self._render(self._row())
        rows = out["rows"][1:]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["scoresError"], "")
        self.assertIn("报告文件不在盘上", rows[1]["scoresError"])
        for cell in rows[1]["cells"][1:]:
            self.assertEqual(cell["noread"], "1", "读不出被当成了「没评」：%r" % (cell,))
            self.assertEqual(cell["missing"], "")
            self.assertEqual(cell["text"], "报告读不出评分")

    def test_the_table_is_wired_into_the_card_and_says_it_does_not_zero_fill(self):
        """表格接进大卡片的那一行 + 「不补 0」这句要写在表里（逐行钉 `renderJobs` 的函数体）。"""
        body = _js_fn_body("renderJobs")
        self.assertIn("h +=   jdDimTable(j);", body,
                      "并排表没有接进大卡片：表再对也不显示")
        self.assertIn("不补 0", _js_fn_body("jdDimTable", "j"))
        self.assertIn("该版未评这一维", _js_fn_body("jdDimTable", "j"))


class JdDimensionNameFilterTest(_DimTableCase):
    """维度名归一（上一轮审查的**真问题**）：真实报告里同时存在 `AI认知` 与 `AI 认知`（差一个空格）。

    按规格字面"维度名并集"它们是**两列**，两只各填一只 → 用户看到的是"全都没评"。
    裁定（与知识库卡片 id 的 `normalize_id` 同一条原则）：比较维度名时先做 **NFKC 归一 + 去掉
    所有空白**；显示时用**出现次数最多的那个原始写法**（同次数按首次出现顺序）。

    这里两份分析**故意**一份写 `AI认知`、一份写 `AI 认知`（带空格的是多数写法）。
    """

    # ⚠️ **不要**在这个类里用模块级的 `DIM_AI`：父类 `JdDimensionTableTest` 里那个类属性
    # 恰好叫 `DIM_AI`（= "AI认知"，无空格），会把模块常量遮住 —— 上一位实现者就是这样
    # 让 `assertEqual(..., DIM_AI)` 变成"拿无空格去比有空格"（真红却看着像文案不对）。
    DIM_AI_NOSPACE = "AI认知"
    DIM_AI_SPACED = "AI 认知"
    A = [_dim(DIM_AI_NOSPACE, 8)]
    B = [_dim(DIM_AI_SPACED, 9)]

    def test_one_space_variant_is_not_a_second_column_and_both_cells_are_filled(self):
        va = self._version("简历A")
        vb = self._version("简历B")
        # ⚠️ 局部变量**不能**叫 A / B：那会遮住类属性 `self.A` / `self.B`（夹具就喂成了两份无空格写法，
        # 「带空格是多数写法」那个前提静默失效 —— 这条用例第一版就踩了这个坑）。
        self._analysis("a1", "简历A", va["id"], "a1.html", 75.0, "2026-10-01 10:00", self.A)
        self._analysis("a2", "简历B", vb["id"], "a2.html", 66.0, "2026-10-02 11:00", self.B)

        out = self._render(self._row())

        self.assertEqual(out["unexpected"], [], "渲染时抛了没人接的 promise")
        # ① 只有一列（← 变异「维度不归一」会让这里变成两列）；列键是归一的
        self.assertEqual(out["dims"], ["AI认知"], "带空格/不带空格被当成了两维：%r" % (out["dims"],))
        cells = out["rows"][0]["cells"]
        # ② 表头显示**出现次数最多的那种写法**：这次两种写法各出现 1 次（平局）→ 按首次出现顺序，
        #    也就是"先被写出来的那一种"。平局是**真会发生的**（这里就是），所以不能写死某一种。
        first_seen = self.A[0]["dimension"]
        self.assertEqual([c["label"] for c in cells], ["", first_seen])
        self.assertEqual([c["dim"] for c in cells], ["", self.DIM_AI_NOSPACE])
        self.assertEqual(cells[1]["text"], first_seen)

        body = out["rows"][1:]
        self.assertEqual(len(body), 2)
        # ③ 两格**都填上了**（而不是"全都没评"）
        self.assertEqual([r["cells"][1]["text"] for r in body], ["8", "9"])
        self.assertEqual([r["cells"][1]["missing"] for r in body], ["", ""],
                         "归一之后仍有一格被当成「该版未评这一维」：%r" % ([r["cells"][1] for r in body],))
        # ④ 格子里的 `data-dim` 也必须是归一后的那个键（否则前端别处按 dim 找会又找不着）
        self.assertEqual([r["cells"][1]["dim"] for r in body],
                         [self.DIM_AI_NOSPACE, self.DIM_AI_NOSPACE])

    def test_the_zero_score_of_a_normalized_dimension_is_still_not_swallowed(self):
        """归一不许把**真 0 分**吞成"没评"（两份都是 0 分，两格都得是 0）。"""
        va = self._version("简历A")
        vb = self._version("简历B")
        self._analysis("a1", "简历A", va["id"], "a1.html", 75.0, "2026-10-01 10:00",
                       [_dim(self.DIM_AI_NOSPACE, 0)])
        self._analysis("a2", "简历B", vb["id"], "a2.html", 66.0, "2026-10-02 11:00",
                       [_dim(self.DIM_AI_SPACED, 0)])
        out = self._render(self._row())
        body = out["rows"][1:]
        self.assertEqual([r["cells"][1]["missing"] for r in body], ["", ""])
        self.assertEqual([(r["cells"][1]["text"], r["cells"][1]["score"]) for r in body],
                         [("0", "0"), ("0", "0")])

    def test_a_more_common_spelling_wins_over_first_seen(self):
        """反向钉住"次数优先"：两份分析都写**带空格**那种 → 表头就是带空格那种（不是"一律用第一次那个"）。

        顺带钉住一条老边界：**同一份分析里**写了两种写法时只算一维（那一份不会既填 8 又填 9 地重复）。
        """
        va = self._version("简历A")
        vb = self._version("简历B")
        self._analysis("a1", "简历A", va["id"], "a1.html", 75.0, "2026-10-01 10:00",
                       [_dim(self.DIM_AI_SPACED, 8)])
        self._analysis("a2", "简历B", vb["id"], "a2.html", 66.0, "2026-10-02 11:00",
                       [_dim(self.DIM_AI_SPACED, 9), _dim(self.DIM_AI_NOSPACE, 3)])
        out = self._render(self._row())
        self.assertEqual(out["dims"], [self.DIM_AI_NOSPACE], "两份分析各写一种写法 → 应当只有一列")
        self.assertEqual([c["label"] for c in out["rows"][0]["cells"]], ["", self.DIM_AI_SPACED])
        # 同一份里两种写法只算一格：B 那一格是它先写的那种的值（8/9 不是各占一格）
        self.assertEqual(len(out["rows"][1]["cells"]), 2)
        self.assertEqual([r["cells"][1]["text"] for r in out["rows"][1:]], ["8", "9"])

    def test_a_dimension_that_only_one_resume_wrote_is_still_empty_in_the_other_row(self):
        """反向：归一**不许**把真的缺维也填上去（否则上面"都填上了"就成了恒真）。"""
        va = self._version("简历A")
        vb = self._version("简历B")
        self._analysis("a1", "简历A", va["id"], "a1.html", 75.0, "2026-10-01 10:00",
                       [_dim(self.DIM_AI_NOSPACE, 8), _dim(DIM_QUALITY, 7)])
        self._analysis("a2", "简历B", vb["id"], "a2.html", 66.0, "2026-10-02 11:00",
                       [_dim(self.DIM_AI_SPACED, 9)])
        out = self._render(self._row())
        body = out["rows"][1:]
        # 质量体系能力：A 评了、B **没评** → A 那格 7 分，B 那格是 missing（不是 0、不是空白得分）
        self.assertEqual(body[0]["cells"][2]["text"], "7")
        self.assertEqual(body[1]["cells"][2]["missing"], "1")
        self.assertEqual(body[1]["cells"][2]["text"], "该版未评这一维")
        self.assertEqual(body[1]["cells"][2]["score"], "")

    def test_nfkc_folding_also_catches_a_full_width_variant(self):
        """NFKC 也认：全角 `ＡＩ认知` 与 `AI认知` 是同一维（只去空白挡不住这一种）。"""
        va = self._version("简历A")
        vb = self._version("简历B")
        self._analysis("a1", "简历A", va["id"], "a1.html", 75.0, "2026-10-01 10:00",
                       [_dim("ＡＩ认知", 4)])
        self._analysis("a2", "简历B", vb["id"], "a2.html", 66.0, "2026-10-02 11:00",
                       [_dim(self.DIM_AI_NOSPACE, 6)])
        out = self._render(self._row())
        self.assertEqual(len(out["dims"]), 1, "全角/半角被当成了两维：%r" % (out["dims"],))
        body = out["rows"][1:]
        self.assertEqual([r["cells"][1]["text"] for r in body], ["4", "6"])


class ResumeTextVerificationWordingTest(IsolatedCase):
    """D（Task 1.10c）：**无指纹时不许冒充「最新版原文」**（读侧最后一处假话）。

    为什么单独一个类、而不是写进 `ResumeVersionRouteTest`：`ResumeAiDiffRouteTest` 继承它，
    写进去会**被跑两遍**（同一份断言跑两次没有价值，还会把全量计数弄乱）。

    复查枚举出 7 行组合：记录里**没有** `sha256` 时 `_kind_on_disk` 无从核对，只能维持
    `latest`/`mother`，而正文其实**不是这一版**。路由因此多回一个 `verified`
    （= 记录里**有** sha256，也就是"核对过"），界面据它决定措辞。
    """

    def _handler(self, method, path, obj=None):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8") if obj is not None else b""
        headers = {"Content-Length": str(len(raw)),
                   "Content-Type": "application/json; charset=utf-8"}
        h = _sandbox_handler(method, path, raw, headers)
        return h._status, h._body

    def _get(self, path):
        return self._handler("GET", path)

    def _new(self, name, content, **extra):
        body = {"name": name, "content": content}
        body.update(extra)
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        h = _sandbox_handler("POST", "/api/resumes", raw,
                             {"Content-Length": str(len(raw)),
                              "Content-Type": "application/json; charset=utf-8"})
        self.assertEqual(h._status, 200, h._body)
        return h._body["resume"]

    def _drop_sha(self, version_id):
        """抹掉记录里的 `sha256`（模拟"老记录 / 人工改坏"）：`_kind_on_disk` 只能维持 ok_kind。"""
        doc = resumelib.load_resumes()
        for r in doc.get("resumes", []):
            if r.get("id") == version_id:
                r.pop("sha256", None)
        resumelib.save_resumes(doc)

    def test_the_file_route_marks_a_version_without_a_fingerprint_as_unverified(self):
        v1 = self._new("独苗.md", "原始内容")
        with open(store.p_path("resumes", "独苗.md"), "w", encoding="utf-8", newline="") as f:
            f.write("别人那一版的内容")            # 盘上那份已经不是这一版了
        self._drop_sha(v1["id"])                   # 记录里也没有指纹 → 无从核对

        code, body = self._get("/api/resumes/file?version=" + v1["id"])
        self.assertEqual(code, 200, body)
        self.assertEqual(body["kind"], "latest", body)      # 前提：读侧只能"维持"
        self.assertEqual(body["text"], "别人那一版的内容")    # 前提：正文确实不是这一版
        self.assertIn("verified", body, "路由没回 verified → 界面只能照旧断言「最新版原文」")
        self.assertFalse(body["verified"], "没有指纹却报「核对过」：%r" % (body,))

        out = _resume_text_view(v1["id"], body)
        self.assertEqual(out["unexpected"], [])
        shown = out["title"] + out["body"]
        self.assertNotIn("最新版原文", shown,
                         "没有指纹、正文又不是这一版，界面却照旧断言「最新版原文」——用户可见的假话：%r"
                         % (shown,))
        self.assertIn("未核对", shown, "换成不断言的措辞时要真的说出来（「没有指纹、内容未核对」）：%r"
                      % (shown,))

    def test_a_fingerprinted_version_is_marked_verified_and_still_says_latest(self):
        """D 的**对照**：有指纹（核对过）时照旧说「最新版原文」—— 诚实措辞不许把正常路径也改糊。"""
        v1 = self._new("唯一.md", "只有这一版")
        code, body = self._get("/api/resumes/file?version=" + v1["id"])
        self.assertEqual((code, body["kind"], body["text"]), (200, "latest", "只有这一版"), body)
        self.assertIn("verified", body)
        self.assertTrue(body["verified"], "有 sha256（核对过）却没有报 verified：%r" % (body,))

        out = _resume_text_view(v1["id"], body)
        self.assertEqual(out["unexpected"], [])
        self.assertIn("最新版原文", out["title"],
                      "核对过的版本照旧要说「最新版原文」（不许因为 D 就一律改成含糊措辞）：%r"
                      % (out["title"],))

    def test_a_mother_snapshot_without_a_fingerprint_is_not_called_the_mother_text(self):
        """D 的另一半：**快照**那一支同样没有指纹依据（`mother` 也只是"维持"）。

        与上一条同一形状，只是"别人那一版"装在**母版快照**里：记录里没有 `sha256` 时
        `_kind_on_disk` 只能维持 `mother`，界面若照旧在**标题**上写「母版原文」、正文说
        「从母版快照读回，是这一族谱的参照原点」，用户就会拿着别人的内容当母版原文用
        （`mother` 比 `latest` 更危险：它被当作"参照原点"）。
        """
        v1 = self._new("母版.md", "第一版内容")
        code, _ = self._handler("POST", "/api/resumes/choose", {"version": v1["id"]})
        self.assertEqual(code, 200)
        self._new("母版.md", "第二版内容", parent_id=v1["id"])     # 生成 v1 的母版快照
        snap = store.p_path("resumes", resumelib.SNAPSHOT_DIR, v1["id"] + ".md")
        self.assertTrue(os.path.exists(snap), "前提：v1 的快照已经留下")
        with open(snap, "w", encoding="utf-8", newline="") as f:
            f.write("别人那一版的内容")            # 快照里那份已经不是这一版了（但读得出来）
        self._drop_sha(v1["id"])                   # 记录里也没有指纹 → 无从核对

        code, body = self._get("/api/resumes/file?version=" + v1["id"])
        self.assertEqual(code, 200, body)
        self.assertEqual(body["kind"], "mother", body)      # 前提：快照优先，且读侧只能"维持"
        self.assertEqual(body["text"], "别人那一版的内容")
        self.assertFalse(body["verified"], "没有指纹却报「核对过」：%r" % (body,))

        out = _resume_text_view(v1["id"], body)
        self.assertEqual(out["unexpected"], [])
        self.assertNotIn("母版原文", out["title"],
                         "没有指纹、快照里又不是这一版，标题却断言「母版原文」——用户可见的假话：%r"
                         % (out["title"],))
        self.assertIn("未核对", out["title"],
                      "标题要如实说「内容未核对」：%r" % (out["title"],))
        self.assertIn("未核对", out["body"], "正文也要说清没有指纹：%r" % (out["body"],))


# 变更说明的三种情形（规格 §8.3/§8.4）：后端**真数据**喂进 `rsDiffText` 的**渲染结果**。
# 断言逐条到"个体"：摘要那一行、每一条 where/what、失败那一句+原因、第一版那一句。
_DIFF_CASE_JS = r"""
function stripTags(s){ return String(s).replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim(); }
out.diff = rsDiffText(__fixture.diff);
out.fail = rsDiffText(__fixture.fail);
out.first = rsDiffText(__fixture.first);
out.empty = rsDiffText(__fixture.empty);
out.diffText = stripTags(out.diff);
out.failText = stripTags(out.fail);
out.firstText = stripTags(out.first);
out.emptyText = stripTags(out.empty);
"""


class ResumeAiDiffRouteTest(ResumeVersionRouteTest):
    """Task 4 的接线面（规格 §4.4）：`POST /api/resumes` 在**延续**时调一次模型写变更说明。

    注入约定与 `interview.analyze` / `outcome.attribute` 一致：替身**实现**
    `llm.call_openai_compatible`（`resumelib` 调用时现取，不是 import 时绑定）——
    **绝不真调模型**，计数由替身自己做。
    """

    MOTHER = "候选人简历正文\n项目经历：语音质检平台\n技能：Python"
    CHILD = "候选人简历正文\n项目经历：语音质检平台；新增语音Agent项目\n技能：Python"

    def _patch(self, obj, name, value):
        p = mock.patch.object(obj, name, value)
        p.start()
        self.addCleanup(p.stop)

    def _fake_chat(self, reply):
        calls = []

        def fake(base_url, api_key, model, messages, json_mode=False, **kw):
            calls.append(messages[0]["content"])
            if isinstance(reply, Exception):
                raise reply
            return reply

        self._patch(app.llm, "call_openai_compatible", fake)
        return calls

    def test_continuing_calls_the_model_once_with_the_mother_and_both_versions_verbatim(self):
        calls = self._fake_chat(json.dumps(
            {"summary": "补了语音Agent项目",
             "changes": [{"where": "项目经历", "what": "新增语音Agent项目"}]},
            ensure_ascii=False))
        v1 = self._new("母版.md", self.MOTHER)
        self._post("/api/resumes/choose", {"version": v1["id"]})
        v2 = self._new("母版.md", self.CHILD, parent_id=v1["id"])

        self.assertEqual(len(calls), 1, "延续应当**恰好**调一次模型：%r" % (calls,))
        self.assertIn(self.MOTHER, calls[0], "提示词里没有母版内容（逐字）")
        self.assertIn(self.CHILD, calls[0], "提示词里没有子版内容（逐字）")
        self.assertEqual(v2["ai_diff"], {"summary": "补了语音Agent项目",
                                         "changes": [{"where": "项目经历",
                                                      "what": "新增语音Agent项目"}]})
        self.assertEqual(v2["ai_diff_error"], "")
        # 落盘了才算数（不是只活在响应里）
        self.assertEqual(resumelib.by_id(v2["id"])["ai_diff"], v2["ai_diff"])
        # 母版快照照旧留下（比对不改变"只留母版 + 最新"这条取舍）
        self.assertTrue(resumelib.by_id(v1["id"])["mother_file"])

    def test_a_failing_model_still_saves_the_version_with_an_honest_error(self):
        self._fake_chat(RuntimeError("模型接口返回 429：余额不足（注入）"))
        v1 = self._new("母版.md", self.MOTHER)
        self._post("/api/resumes/choose", {"version": v1["id"]})
        code, body = self._save("母版.md", self.CHILD, parent_id=v1["id"])

        self.assertEqual(code, 200, body)                       # 比对失败**不是**致命的
        rec = body["resume"]
        self.assertEqual(store.read_text(self._snap_path("resumes/母版.md")), self.CHILD)
        self.assertIn("429", rec["ai_diff_error"])
        self.assertEqual(rec["ai_diff"], {})                    # ← 绝不写一个空的假 ai_diff
        self.assertEqual(resumelib.by_id(rec["id"])["ai_diff"], {})

    def test_a_broken_json_reply_is_not_retried_and_never_fakes_a_diff(self):
        calls = self._fake_chat("这根本不是 JSON")
        v1 = self._new("母版.md", self.MOTHER)
        self._post("/api/resumes/choose", {"version": v1["id"]})
        code, body = self._save("母版.md", self.CHILD, parent_id=v1["id"])

        self.assertEqual(code, 200, body)
        self.assertEqual(len(calls), 1, "坏 JSON **不重试**")
        self.assertIn("JSON", body["resume"]["ai_diff_error"])
        self.assertEqual(body["resume"]["ai_diff"], {})
        self.assertEqual(store.read_text(self._snap_path("resumes/母版.md")), self.CHILD)

    def test_a_new_independent_resume_never_calls_the_model(self):
        calls = self._fake_chat('{"summary":"不该被调用","changes":[]}')
        v1 = self._new("母版.md", self.MOTHER)                  # 新开一份独立的
        self.assertEqual(calls, [], "新开一份独立的没有可比对的对象：一次 chat 都不该调")
        self.assertEqual((v1["ai_diff"], v1["ai_diff_error"]), ({}, ""))
        # 「要延续却没有选定母版」= 400，**也不许**为了"试一试"先调一次模型
        code, body = self._save("母版.md", self.CHILD, lineage="延续")
        self.assertEqual(code, 400, body)
        self.assertEqual(calls, [])

    def test_the_lineage_continue_word_also_triggers_exactly_one_comparison(self):
        """前端选「延续」时可能只发 `lineage`（母版由"已选定"解析）—— 那条路同样只调一次。"""
        calls = self._fake_chat(json.dumps({"summary": "改了技能", "changes": []}, ensure_ascii=False))
        v1 = self._new("母版.md", self.MOTHER)
        self._post("/api/resumes/choose", {"version": v1["id"]})
        rec = self._new("母版.md", self.CHILD, lineage="延续")
        self.assertEqual(len(calls), 1)
        self.assertIn(self.MOTHER, calls[0])
        self.assertEqual(rec["parent_id"], v1["id"])
        self.assertEqual(rec["ai_diff"]["summary"], "改了技能")


class ResumeChangeSummaryRenderTest(IsolatedCase):
    """简历 tab 的「变更说明」入口要显示**三种情形**，数据来自后端 `resumes.json` 的真实记录。

    Task 4 只负责"后端把真数据接上"：这一页的渲染 T3a 已经写好了，所以这里钉的是
    「后端产出的三个形状 → 渲染出的三句话」对得上（含 §8.3 的"AI 生成，可能漏"）。
    """

    def _render(self, fixture):
        _need_node()
        out = web_probe.run_index(_DIFF_CASE_JS, fixture)
        self.assertEqual(out["unexpected"], [], "渲染时抛了没人接的 promise")
        for k in ("diff", "fail", "first", "empty"):
            self.assertTrue(out.get(k), "`rsDiffText` 对 %s 什么都没渲染（探针空跑 = 假绿）" % k)
        return out

    def test_a_generated_summary_renders_every_change_verbatim(self):
        out = self._render({
            "diff": {"rev": 2, "ai_diff": {"summary": "补充了语音Agent项目",
                                           "changes": [{"where": "项目经历", "what": "新增语音Agent项目"},
                                                       {"where": "技能", "what": "补了向量检索"}]},
                     "ai_diff_error": ""},
            "fail": {"rev": 2, "ai_diff": {}, "ai_diff_error": ""},
            "first": {"rev": 1, "ai_diff": {}, "ai_diff_error": ""},
            "empty": {"rev": 2, "ai_diff": {}, "ai_diff_error": ""},
        })
        self.assertIn("AI 写的", out["diffText"])              # §8.3：标注是模型写的
        self.assertIn("以原文为准", out["diffText"])
        self.assertIn("补充了语音Agent项目", out["diffText"])   # summary 逐字
        for where, what in (("项目经历", "新增语音Agent项目"), ("技能", "补了向量检索")):
            self.assertIn(where, out["diffText"], "少渲染了一条变更的 where：%r" % (out["diffText"],))
            self.assertIn(what, out["diffText"], "少渲染了一条变更的 what：%r" % (out["diffText"],))
        self.assertNotIn("没能生成变更说明", out["diffText"])

    def test_a_failed_comparison_says_so_with_the_reason_and_the_version_still_saved(self):
        out = self._render({
            "diff": {"rev": 2, "ai_diff": {"summary": "s", "changes": []}, "ai_diff_error": ""},
            "fail": {"rev": 2, "ai_diff": {}, "ai_diff_error": "模型接口返回 429：余额不足"},
            "first": {"rev": 1, "ai_diff": {}, "ai_diff_error": ""},
            "empty": {"rev": 2, "ai_diff": {}, "ai_diff_error": ""},
        })
        self.assertIn("这次没能生成变更说明", out["failText"])
        self.assertIn("模型接口返回 429：余额不足", out["failText"])   # 原因逐字，不许吞掉
        self.assertIn("版本照样保存了", out["failText"])              # §8.4

    def test_the_first_version_says_there_is_no_mother_to_compare_with(self):
        out = self._render({
            "diff": {"rev": 2, "ai_diff": {"summary": "s", "changes": []}, "ai_diff_error": ""},
            "fail": {"rev": 2, "ai_diff": {}, "ai_diff_error": "x"},
            "first": {"rev": 1, "ai_diff": {}, "ai_diff_error": ""},
            "empty": {"rev": 2, "ai_diff": {}, "ai_diff_error": ""},
        })
        self.assertIn("第一版", out["firstText"])
        self.assertIn("没有可比对的母版", out["firstText"])

    def test_a_child_with_neither_diff_nor_error_does_not_pretend_a_summary_exists(self):
        """反假绿：没变更说明也没原因时，**不许**编一句"AI 写了变更说明"。"""
        out = self._render({
            "diff": {"rev": 2, "ai_diff": {"summary": "s", "changes": []}, "ai_diff_error": ""},
            "fail": {"rev": 2, "ai_diff": {}, "ai_diff_error": "x"},
            "first": {"rev": 1, "ai_diff": {}, "ai_diff_error": ""},
            "empty": {"rev": 2, "ai_diff": {}, "ai_diff_error": ""},
        })
        self.assertIn("没有留下变更说明", out["emptyText"])
        self.assertNotIn("AI 写的", out["emptyText"])
        self.assertNotIn("补充", out["emptyText"])


class DuplicateExitModalTest(LedgerWithReportsCase):
    """409 出口的两面（规格 §6.2 / §5.3）。

    前端拿到的响应体一律是**真后端**刚产出的那一份：先用探针跑 `analyzePayload()` 拿到
    "浏览器真会发出去的那一份"，把那**逐字**提交给沙箱后端，再把响应喂回页面。这样
    「前端发的形状」与「后端判的形状」不可能各说各话（有过"测试喂的形状生产到不了"的教训）。
    """

    def _payload(self, key, name):
        """真跑页面：下拉里选中某一版 → 回传 `analyzePayload()`。"""
        _need_node()
        code, view = _resumes_view()
        self.assertEqual(code, 200, view)
        out = web_probe.run_index(
            "renderResumePicker(__fixture.view);\n"
            "$('#resumeSel').value = __fixture.key;\n"
            "$('#jd').value = __fixture.jd;\n"
            "$('#link').value = '';\n"
            "out.payload = analyzePayload();\n"
            "out.picked = String($('#resumeSel').value);\n",
            {"view": view, "key": key, "jd": self.JD_TEXT})
        self.assertEqual(out["picked"], key, "下拉没选中这一版（fixture 不对）")
        self.assertEqual(out["payload"].get("resume_id"), key)
        return out["payload"]

    def _replay(self, payload, responses, extra=""):
        """把**真响应**喂回页面，真跑 `analyzeSubmit()` / `dupResend()`，回传它发出的请求体。"""
        _need_node()
        out = web_probe.run_index(
            _DIM_PARSE_JS +
            "var sent = [];\n"
            "var q = __fixture.resp.slice();\n"
            "fetch = function(url, opt){\n"
            "  var u = String(url);\n"
            "  if(u.indexOf('/api/analyze') === 0){\n"
            "    sent.push(JSON.parse(String(opt && opt.body)));\n"
            "    var b = q.length ? q.shift() : {ok:false, error:'（探针：没有更多预设响应）'};\n"
            "    return Promise.resolve({ok:true, json:function(){return Promise.resolve(b);}});\n"
            "  }\n"
            "  return Promise.resolve({ok:true, json:function(){return Promise.resolve("
            "{ok:true, jds:[], categories:[], statuses:[]});}});\n"
            "};\n"
            "RESUME_PICKS = [{key: __fixture.payload.resume_id, name: __fixture.payload.resume_name,"
            " version: __fixture.payload.resume_id}];\n"
            "$('#resumeSel').value = String(__fixture.payload.resume_id || '');\n"
            "$('#jd').value = String(__fixture.payload.jd_text || '');\n"
            "$('#link').value = '';\n"
            # 垫片不看 HTML 的 `hidden` 属性（元素恒为 false）→ 这里手工摆成页面真实的初始状态
            "$('#dupModal').hidden = true;\n"
            "out.modalBefore = $('#dupModal').hidden;\n"
            "await analyzeSubmit();\n"
            "out.modalAfterFirst = $('#dupModal').hidden;\n"
            "out.dupBody = String($('#dupBody').innerHTML);\n"
            "out.stAfterFirst = String($('#st').textContent);\n"
            "out.attr = {};\n"
            "['job','report','analysis','score','at','resume','resume-id'].forEach(function(k){\n"
            "  out.attr[k] = attrOf(out.dupBody, 'data-dup-' + k);\n"
            "});\n"
            "out.hasOverwriteBtn = out.dupBody.indexOf('id=\"dupOverwrite\"') >= 0;\n"
            "out.hasNewBtn = out.dupBody.indexOf('id=\"dupNew\"') >= 0;\n"
            "out.sent = sent;\n"
            + extra,
            {"payload": payload, "resp": responses})
        return out

    def _first_body(self, out):
        return out["sent"][0]

    def test_a_different_resume_version_does_not_open_the_duplicate_modal(self):
        """**用户的核心用法**：同一个岗位换第二份简历 → 后端 200 → 前端**不该**弹「重复」。

        这里证明的是"前端只在响应体真带 `duplicate` 时才弹"：喂进去的是后端**真的**会
        给的那一份（200 + report），所以弹窗必须保持关闭。
        """
        calls = self._patch_analyze_deps()
        va = self._version("简历A")
        self._analysis("a1", "简历A", va["id"], "a1.html", 75.0, "2026-10-01 10:00", [])
        vb = self._version("简历B")
        payload = self._payload(vb["id"], "简历B.md")
        self.assertNotIn("overwrite", payload)
        self.assertNotIn("allow_duplicate", payload)
        code, body = self._post_json("/api/analyze", payload)
        self.assertEqual(code, 200, body)                      # ← 换简历不是重复
        self.assertNotIn("duplicate", body)

        out = self._replay(payload, [body])
        self.assertEqual(out["unexpected"], [])
        self.assertTrue(out["modalBefore"])
        self.assertTrue(out["modalAfterFirst"], "换一份简历也弹了「重复」弹窗：%r" % (out["dupBody"][:300],))
        self.assertEqual(out["dupBody"], "")
        self.assertIn("分析完成", out["stAfterFirst"])
        sent = self._first_body(out)
        self.assertEqual(sent, payload, "前端发出去的与刚才真提交的那一份不一致")
        self.assertFalse(out["hasOverwriteBtn"] or out["hasNewBtn"])
        self.assertEqual(calls["llm"], 1)

    def test_the_same_resume_version_opens_the_modal_with_what_will_be_overwritten(self):
        calls = self._patch_analyze_deps()
        v = self._version("简历A")
        self._analysis("a1", "简历A", v["id"], "a1.html", 75.0, "2026-10-01 10:00", [])
        payload = self._payload(v["id"], "简历A.md")
        code, body = self._post_json("/api/analyze", payload)
        self.assertEqual(code, 409, body)                       # ← 同一版才是重复
        self.assertEqual(calls["llm"], 0, "查重必须在花钱之前")

        out = self._replay(payload, [body, {"ok": True, "report": "a1.html"}])
        self.assertEqual(out["unexpected"], [])
        self.assertFalse(out["modalAfterFirst"], "同一版没弹出口 → 用户卡死")
        self.assertTrue(out["hasOverwriteBtn"], "弹窗里没有「覆盖」按钮：%r" % (out["dupBody"],))
        self.assertTrue(out["hasNewBtn"], "弹窗里没有「仍然新建」按钮：%r" % (out["dupBody"],))
        # 弹窗显示的必须是**要覆盖的那一条**的具体信息（逐项，不是"文案里有某个词"）
        self.assertEqual(out["attr"]["job"], self.JD_ID)
        self.assertEqual(out["attr"]["report"], "a1.html")
        self.assertEqual(out["attr"]["analysis"], "a1")     # 「覆盖」要用的就是它（不是 JD id）
        self.assertEqual(out["attr"]["score"], "75")
        self.assertEqual(out["attr"]["at"], "2026-10-01 10:00")
        self.assertEqual(out["attr"]["resume"], "简历A")
        self.assertEqual(out["attr"]["resume-id"], v["id"])

    def test_overwrite_resends_with_the_original_analysis_id_and_nothing_else(self):
        self._patch_analyze_deps()
        v = self._version("简历A")
        self._analysis("a1", "简历A", v["id"], "a1.html", 75.0, "2026-10-01 10:00", [])
        payload = self._payload(v["id"], "简历A.md")
        code, dup_body = self._post_json("/api/analyze", payload)
        self.assertEqual(code, 409, dup_body)

        out = self._replay(payload, [dup_body, {"ok": True, "report": "a1.html"}],
                           extra="await dupResend('overwrite');\n"
                                 "out.sentAfter = sent.slice();\n"
                                 "out.modalAfterOverwrite = $('#dupModal').hidden;\n"
                                 "out.stAfter = String($('#st').textContent);\n")
        self.assertEqual(out["unexpected"], [])
        self.assertEqual(len(out["sentAfter"]), 2)          # 第一次 + 覆盖那一次
        first, second = out["sentAfter"]
        self.assertEqual(first, payload)
        # 覆盖带的是**那条分析的 id**（不是 JD id —— 见下面那条 E2E 发现的真问题）
        self.assertEqual(second["overwrite"], "a1")
        self.assertNotIn("allow_duplicate", second, "覆盖时还带了 allow_duplicate：两条路串了")
        self.assertEqual(second["resume_id"], v["id"])      # 还是这一版
        self.assertEqual(second["resume_name"], "简历A.md")
        self.assertEqual(second["jd_text"], payload["jd_text"])
        self.assertTrue(out["modalAfterOverwrite"], "覆盖成功后弹窗没关")
        self.assertIn("覆盖", out["stAfter"])

    def test_the_overwrite_lands_on_the_duplicate_record_not_on_the_newest_one(self):
        """**E2E 发现并修掉的真问题**：一个 JD 下有几份分析时，「覆盖」必须落到**要覆盖的那条**。

        只带 JD id 时后端走 `_job_in_ledger(<JD id>)` → 取该 JD 下**最新**那条记录，于是
        "覆盖 A 这份"会把 **B 那份**的记录改掉（连同它的 `resume_id`）——用户看到弹窗上写着
        A 的报告名，盘上动的却是 B。所以 409 的 body 必须带命中那条分析的 `analysis_id`，
        前端拿它当 `overwrite`。这条把**前端真的发出去的那一份**再交给真后端，端到端钉住。
        """
        self._patch_analyze_deps()
        va = self._version("简历A")
        vb = self._version("简历B")
        self._analysis("a1", "简历A", va["id"], "a1.html", 75.0, "2026-10-01 10:00", [])
        # 更新的那一条：`_job_in_ledger(<JD id>)` 会取它 —— 这正是坑
        self._analysis("a2", "简历B", vb["id"], "a2.html", 66.0, "2026-10-02 11:00", [])
        other_before = store.read_text(self._report("a2.html"))

        payload = self._payload(va["id"], "简历A.md")
        code, dup_body = self._post_json("/api/analyze", payload)
        self.assertEqual(code, 409, dup_body)
        self.assertEqual(dup_body["duplicate"]["analysis_id"], "a1")     # 命中的是这一条
        self.assertEqual(dup_body["duplicate"]["report"], "a1.html")

        out = self._replay(payload, [dup_body, {"ok": True, "report": "a1.html"}],
                           extra="await dupResend('overwrite');\nout.sentAfter = sent.slice();\n")
        resend = out["sentAfter"][1]
        self.assertEqual(resend["overwrite"], "a1", "覆盖没有指向命中那条分析")

        code2, body2 = self._post_json("/api/analyze", resend)           # ← 用前端真发的那一份
        self.assertEqual(code2, 200, body2)
        recs = {r["id"]: r for r in store.load_jobs()}
        self.assertEqual(sorted(recs), ["a1", "a2"], "多建/少建了记录")
        self.assertEqual(recs["a1"]["score"], 91.5, "要覆盖的那条没被改")
        self.assertEqual(recs["a1"]["resume_id"], va["id"])
        self.assertEqual(recs["a1"]["report"], "a1.html", "覆盖没有沿用原报告文件名")
        self.assertEqual((recs["a2"]["score"], recs["a2"]["resume_id"], recs["a2"]["resume"]),
                         (66.0, vb["id"], "简历B"), "另一份简历的记录被改掉了")
        self.assertEqual(store.read_text(self._report("a2.html")), other_before,
                         "另一份简历的报告文件被改写了")

    def test_still_create_resends_with_allow_duplicate_and_nothing_else(self):
        self._patch_analyze_deps()
        v = self._version("简历A")
        self._analysis("a1", "简历A", v["id"], "a1.html", 75.0, "2026-10-01 10:00", [])
        payload = self._payload(v["id"], "简历A.md")
        code, dup_body = self._post_json("/api/analyze", payload)
        self.assertEqual(code, 409, dup_body)

        out = self._replay(payload, [dup_body, {"ok": True, "report": "a1(2).html"}],
                           extra="await dupResend('new');\n"
                                 "out.sentAfter = sent.slice();\n"
                                 "out.modalAfterNew = $('#dupModal').hidden;\n"
                                 "out.stAfter = String($('#st').textContent);\n")
        self.assertEqual(out["unexpected"], [])
        second = out["sentAfter"][1]
        self.assertIs(second["allow_duplicate"], True)
        self.assertNotIn("overwrite", second, "「仍然新建」还带了 overwrite：那不是新建")
        self.assertEqual(second["resume_id"], v["id"])
        self.assertTrue(out["modalAfterNew"])
        self.assertIn("另建", out["stAfter"])

    def test_the_two_buttons_are_bound_to_the_two_resend_modes(self):
        """探针的垫片里 `addEventListener` 是空实现 → 按钮到函数的**绑定**只能在源码那一行上钉。

        真点击（无头 Chrome 里点这两个按钮）由 E2E 负责；这里只保证"点到的就是这两个模式"。
        """
        body = _js_fn_body("dupAsk", "payload, dup")
        self.assertIn("$('#dupOverwrite').addEventListener('click', ()=>dupResend('overwrite'));", body)
        self.assertIn("$('#dupNew').addEventListener('click', ()=>dupResend('new'));", body)
        self.assertIn("$('#dupCancel').addEventListener('click', ()=>dupClose('cancel'));",
                      _js_fn_body("rsInit"))
        self.assertIn("$('#dupModal').hidden = false;", body)


class ResumeMissingBranchScoreTest(LedgerWithReportsCase):
    """规格 §4.5：历史版原文没保留时，除 `ai_diff` 外还要显示**这一版当时的分数**。

    分数的唯一来源是台账（`analyses[]` 按 `resume_id` 精确匹配）。**找不到就如实说
    「没有它的评分记录」**，绝不按显示名猜、绝不编一个数（规格 §8 的诚实边界）。
    """

    def _family(self):
        v1 = resumelib.create_version("族谱", "第 1 版正文")
        resumelib.choose(v1["id"])                                     # 母版
        v2 = resumelib.create_version("族谱", "第 2 版正文", parent_id=v1["id"])
        v3 = resumelib.create_version("族谱", "第 3 版正文", parent_id=v2["id"])
        return v1, v2, v3

    def _missing_response(self, vid):
        """真接口拿到的 `missing` 那一份（空渲染/空响应一律报错，绝不当"成功"。）"""
        code, body = self._get("/api/resumes/file?version=" + vid)
        self.assertEqual(code, 200, body)
        self.assertEqual(body.get("kind"), "missing", body)
        self.assertEqual(body.get("version"), vid)
        self.assertNotIn("text", body, "历史版返回了 text：那是在假装能取回原文")
        self.assertIn("旧版原文未保留", body["error"])
        return body

    def _view(self, vid, response, rows, ledger_fails=False):
        _need_node()
        return web_probe.run_index(
            _DIM_PARSE_JS +
            "JOBS = __fixture.rows;\n"
            "var q = [__fixture.resp];\n"
            "fetch = function(url){\n"
            "  if(String(url).indexOf('/api/jds') === 0){\n"
            "    if(__fixture.ledgerFails) return Promise.reject(new Error('台账读不到'));\n"
            "    return Promise.resolve({ok:true, json:function(){return Promise.resolve({jds:__fixture.rows});}});\n"
            "  }\n"
            "  return Promise.resolve({ok:true, json:function(){return Promise.resolve(q.shift() || {});}});\n"
            "};\n"
            "JOBS = __fixture.ledgerFails ? [] : __fixture.rows;\n"
            "await openResumeText(__fixture.vid);\n"
            "var body = String($('#rsViewBody').innerHTML);\n"
            "out.body = body;\n"
            "out.scoresKind = attrOf(body, 'data-scores');\n"
            "out.scoresError = attrOf(body, 'data-scores-error');\n"
            "out.lines = scoreLinesOf(body);\n"
            "out.hasMissingNote = body.indexOf('旧版原文未保留') >= 0;\n"
            "out.saysNoRecord = body.indexOf('没有它的评分记录') >= 0;\n",
            {"vid": vid, "resp": response, "rows": rows, "ledgerFails": ledger_fails})

    def test_the_score_of_that_version_is_shown_from_the_ledger(self):
        v1, v2, v3 = self._family()
        self._analysis("a1", "族谱", v2["id"], "a1.html", 71.5, "2026-10-05 12:00", [])
        # 干扰项：**显示名一模一样**、但钉的是别的版本 / 没钉版本 → 绝不能被算进这一版
        self._analysis("a2", "族谱", v3["id"], "a2.html", 90.0, "2026-10-06 12:00", [])
        self._analysis("a3", "族谱", "", "a3.html", 55.0, "2026-10-07 12:00", [])
        resp = self._missing_response(v2["id"])
        out = self._view(v2["id"], resp, [self._row()])
        self.assertEqual(out["unexpected"], [])
        self.assertTrue(out["hasMissingNote"])
        self.assertEqual(out["scoresKind"], "1")
        self.assertFalse(out["saysNoRecord"])
        self.assertEqual(out["lines"], [{"job": self.JD_ID, "score": "71.5",
                                         "at": "2026-10-05 12:00", "report": "a1.html",
                                         "text": "岗位「语音文本评测」 · 71.5 分 · 2026-10-05 12:00"}])
        self.assertIn("71.5", out["body"])       # 变更说明那一段也在（这里只补分数这一项）

    def test_a_version_without_any_analysis_says_there_is_no_score_record(self):
        v1, v2, v3 = self._family()
        self._analysis("a1", "族谱", v3["id"], "a1.html", 71.5, "2026-10-05 12:00", [])
        resp = self._missing_response(v2["id"])
        out = self._view(v2["id"], resp, [self._row()])
        self.assertEqual(out["scoresKind"], "none")
        self.assertEqual(out["lines"], [])
        self.assertTrue(out["saysNoRecord"], "找不到评分记录时没有如实说：%r" % (out["body"],))

    def test_a_ledger_read_failure_is_not_reported_as_no_record(self):
        """"台账读不到"与"台账里没有它的评分记录"是两句不同的话 —— 不许混成一句。"""
        v1, v2, v3 = self._family()
        self._analysis("a1", "族谱", v2["id"], "a1.html", 71.5, "2026-10-05 12:00", [])
        resp = self._missing_response(v2["id"])
        out = self._view(v2["id"], resp, [self._row()], ledger_fails=True)
        self.assertEqual(out["scoresKind"], "error")
        self.assertEqual(out["scoresError"], "台账读不到")
        self.assertFalse(out["saysNoRecord"], "把「没读到」说成了「没有记录」：%r" % (out["body"],))
        self.assertEqual(out["lines"], [])

    def test_the_lookup_is_by_resume_id_not_by_display_name(self):
        """`rsScoresOfVersion` 单独钉一遍：同名不同版 / 无版本号都不算（这就是这次要修的坑）。"""
        v1, v2, v3 = self._family()
        self._analysis("a1", "族谱", v2["id"], "a1.html", 71.5, "2026-10-05 12:00", [])
        self._analysis("a2", "族谱", v3["id"], "a2.html", 90.0, "2026-10-06 12:00", [])
        self._analysis("a3", "族谱", "", "a3.html", 55.0, "2026-10-07 12:00", [])
        row = self._row()
        _need_node()
        out = web_probe.run_index(
            _DIM_PARSE_JS +
            "out.same = rsScoresOfVersion([__fixture.row], __fixture.v2);\n"
            "out.other = rsScoresOfVersion([__fixture.row], __fixture.v3);\n"
            "out.unknown = rsScoresOfVersion([__fixture.row], '');\n",
            {"row": row, "v2": v2["id"], "v3": v3["id"]})
        self.assertEqual([x["score"] for x in out["same"]], [71.5])
        self.assertEqual([x["score"] for x in out["other"]], [90.0])
        self.assertEqual(out["unknown"], [])           # 没有版本 id 就不查（不猜）


class ResumeFilePickRejectTest(IsolatedCase):
    """收尾：**非文本文件要明确拒绝**（`.docx` 被当文本读进来 = 乱码存进简历库）。

    `accept=".md,.txt"` 只是给文件对话框的**建议**，用户强选 `.docx` 照样能选进来 —— 所以
    必须按扩展名显式判断：非 `.md` / `.txt` → 明确提示，**不读文件、不动编辑区**。
    """

    def _pick(self, files):
        """真跑 `rsFileChosen()`（垫一个 FileReader，只在这里注入"读到的文本"）。"""
        _need_node()
        return web_probe.run_index(
            _DIM_PARSE_JS +
            "$('#resumeContent').value = '原有内容';\n"
            "RS_EDIT = {name: '原有.md', version: '', content: '原有内容'};\n"
            "FileReader = function(){\n"
            "  var self = this;\n"
            "  this.readAsText = function(f){ self.result = String(f.__text || '');"
            " if(self.onload) self.onload(); };\n"
            "};\n"
            "out.results = __fixture.files.map(function(f){\n"
            "  __alerts.length = 0;\n"
            "  var ok = rsFileChosen(f);\n"
            "  return {name: String(f.name), ok: ok, alerts: __alerts.slice(),\n"
            "          content: String($('#resumeContent').value),\n"
            "          picked: String($('#editingName').dataset.name || ''),\n"
            "          rsEdit: String(RS_EDIT.name || '')};\n"
            "});\n"
            "out.isText = ['a.md', 'A.MD', 'b.txt', 'B.TXT', 'a.docx', 'a.pdf', 'md', 'a.md.exe']"
            ".map(function(n){ return rsIsTextFile(n); });\n",
            {"files": files})

    def test_a_docx_is_rejected_out_loud_and_never_read(self):
        out = self._pick([{"name": "我的简历.docx", "__text": "PK\x03\x04binary"}])
        self.assertEqual(out["unexpected"], [])
        got = out["results"][0]
        self.assertFalse(got["ok"], "非文本文件被当成功处理了")
        alerts = got["alerts"]
        self.assertEqual(len(alerts), 1, "没有明确提示（或提示了不止一次）：%r" % (alerts,))
        self.assertIn("只支持 .md / .txt", alerts[0])
        self.assertIn("我的简历.docx", alerts[0])
        self.assertEqual(got["content"], "原有内容", "被拒绝的文件仍然污染了编辑区（乱码）")
        self.assertEqual(got["picked"], "", "被拒绝的文件仍然占住了「正在编辑」那一栏")
        self.assertEqual(got["rsEdit"], "原有.md")

    def test_md_and_txt_are_read_in_cluding_uppercase_names(self):
        out = self._pick([{"name": "简历A.MD", "__text": "# 甲\n正文"},
                          {"name": "简历B.txt", "__text": "乙正文"}])
        md, txt = out["results"]
        self.assertTrue(md["ok"])
        self.assertEqual((md["content"], md["picked"], md["rsEdit"]), ("# 甲\n正文", "简历A.MD", "简历A.MD"))
        self.assertEqual(md["alerts"], [])
        self.assertEqual((txt["content"], txt["picked"]), ("乙正文", "简历B.txt"))

    def test_the_extension_rule_itself(self):
        out = self._pick([])
        self.assertEqual(out["isText"],
                         [True, True, True, True, False, False, False, False],
                         "扩展名判断不对（大小写要认、`md` 这种没扩展名的与 `a.md.exe` 都不收）")
        body = _js_fn_body("rsFileChosen", "f")
        self.assertIn("rsIsTextFile(f.name)", body,
                      "读文件之前没有按扩展名显式判断")


class JdPurgeRouteTest(LedgerWithReportsCase):
    """阶段 D 的两条新路由：`GET /api/jd/purge_preview`（只读预演）与 `POST /api/jd/purge`（真删）。

    这里只钉**路由层**的契约（状态码 / 形状 / 参数名 / 指纹判门）；删除语义本身在
    `tests/test_jddelete.py` 里逐条钉（含逐字节回滚）。
    """

    def _two_analyses(self):
        self._analysis("a1", "简历甲", "", "a1.html", 66.0, "2026-10-01 09:00")
        self._analysis("a2", "简历乙", "", "a2.html", 72.0, "2026-10-05 09:00")

    def test_preview_returns_the_itemized_list_and_is_read_only(self):
        self._two_analyses()
        before = _disk_bytes(os.path.join(store.p_path("jobs.json")))
        code, body = self._get("/api/jd/purge_preview?jd=" + quote(self.JD_ID))
        self.assertEqual(code, 200, body)
        self.assertTrue(body["ok"], body)
        self.assertTrue(body["found"])
        self.assertEqual(body["id"], self.JD_ID)
        # 逐条：两条分析（id + 简历名 + 分数）
        self.assertEqual([a["id"] for a in body["analyses"]], ["a1", "a2"])
        self.assertEqual([a["resume"] for a in body["analyses"]], ["简历甲", "简历乙"])
        self.assertEqual([a["score"] for a in body["analyses"]], [66.0, 72.0])
        # 逐条：报告文件 + JD 原文（文件名逐个点名）
        names = [f["name"] for f in body["files"]]
        self.assertEqual(sorted(f["name"] for f in body["files"]
                                if f["kind"] == "analysis_report"), ["a1.html", "a2.html"])
        self.assertIn(self.JD_ID + ".txt", names)
        # 指纹非空（执行侧要用它判"清单有没有变"）
        self.assertTrue(body["fingerprint"])
        # **严格只读**：读接口不能改一个字节
        self.assertEqual(_disk_bytes(os.path.join(store.p_path("jobs.json"))), before)

    def test_preview_accepts_the_documented_jd_parameter_and_the_id_alias(self):
        self._two_analyses()
        for qs in ("?jd=", "?id="):
            with self.subTest(qs=qs):
                code, body = self._get("/api/jd/purge_preview" + qs + quote(self.JD_ID))
                self.assertEqual(code, 200, body)
                self.assertEqual(body["id"], self.JD_ID)

    def test_the_route_does_not_leak_internal_keys_to_the_frontend(self):
        self._two_analyses()
        code, body = self._get("/api/jd/purge_preview?jd=" + quote(self.JD_ID))
        self.assertEqual(code, 200, body)
        for k in ("_analyses", "_files", "_kb_cards", "_interviews"):
            self.assertNotIn(k, body)

    def test_a_missing_jd_is_404_with_a_sentence_not_a_crash(self):
        code, body = self._get("/api/jd/purge_preview?jd=" + quote("根本没有_0001"))
        self.assertEqual(code, 404, body)
        self.assertFalse(body["ok"])
        self.assertIn("没有", body["error"])

    def test_a_missing_id_is_400(self):
        code, body = self._get("/api/jd/purge_preview")
        self.assertEqual(code, 400, body)
        self.assertFalse(body["ok"])
        self.assertIn("id", body["error"])

    def test_purge_deletes_the_records_and_files_and_reports_the_backup_path(self):
        self._two_analyses()
        code, p = self._get("/api/jd/purge_preview?jd=" + quote(self.JD_ID))
        self.assertEqual(code, 200, p)
        code, body = self._post_json("/api/jd/purge",
                                     {"id": self.JD_ID, "confirm_fingerprint": p["fingerprint"]})
        self.assertEqual(code, 200, body)
        self.assertTrue(body["ok"], body)
        self.assertTrue(os.path.isdir(body["backup"]), body["backup"])
        self.assertEqual(sorted(f["name"] for f in body["removed_files"]),
                         ["a1.html", "a2.html", self.JD_ID + ".txt"])
        self.assertIsNone(jdid.find_jd(self.JD_ID))
        for name in ("a1.html", "a2.html", self.JD_ID + ".txt"):
            self.assertFalse(os.path.exists(os.path.join(store.p_path("reports"), name)))
        # 返回的台账里已经没有这一行了
        self.assertEqual(body["jobs"], [])

    def test_purge_without_a_fingerprint_still_works(self):
        """老前端 / curl 不传指纹时照常执行（指纹是**额外**的一道闸门，不是必填）。"""
        self._two_analyses()
        code, body = self._post_json("/api/jd/purge", {"id": self.JD_ID})
        self.assertEqual(code, 200, body)
        self.assertIsNone(jdid.find_jd(self.JD_ID))

    def test_a_stale_fingerprint_is_409_and_nothing_is_deleted(self):
        self._two_analyses()
        code, p = self._get("/api/jd/purge_preview?jd=" + quote(self.JD_ID))
        self.assertEqual(code, 200, p)
        self._analysis("a3", "简历丙", "", "a3.html", 90.0, "2026-10-09 09:00")
        code, body = self._post_json("/api/jd/purge",
                                     {"id": self.JD_ID, "confirm_fingerprint": p["fingerprint"]})
        self.assertEqual(code, 409, body)
        self.assertFalse(body["ok"])
        self.assertIn("清单", body["error"])
        self.assertIsNotNone(jdid.find_jd(self.JD_ID))
        self.assertTrue(os.path.exists(os.path.join(store.p_path("reports"), "a1.html")))

    def test_purging_a_missing_jd_is_404(self):
        code, body = self._post_json("/api/jd/purge", {"id": "根本没有_0001"})
        self.assertEqual(code, 404, body)
        self.assertFalse(body["ok"])

    def test_a_missing_id_on_purge_is_400(self):
        code, body = self._post_json("/api/jd/purge", {})
        self.assertEqual(code, 400, body)
        self.assertFalse(body["ok"])

    def test_a_failed_rollback_is_500_with_its_code_and_backup_path(self):
        """回滚**也**失败 → 500，响应体里带 `code` 与备份目录。

        前端靠这两个字段分流：`backup_failed`（一个字节都没动）/ `failed`（已回滚）/
        `rollback_failed`（**数据可能不是原样，要人工从 backup 恢复**）。
        没有 `code` 的话前端只能靠文案猜，一旦有人改文案就悄悄错一类提示。
        """
        self._two_analyses()
        jddelete._FAIL_AFTER = 1
        try:
            with mock.patch.object(jddelete, "_rollback", return_value=False):
                code, body = self._post_json("/api/jd/purge", {"id": self.JD_ID})
        finally:
            jddelete._FAIL_AFTER = 0
        self.assertEqual(code, 500, body)
        self.assertFalse(body["ok"])
        self.assertEqual(body["code"], "rollback_failed", body)
        self.assertTrue(body["backup"], "回滚失败却没把备份目录回给前端：%r" % (body,))
        self.assertTrue(os.path.isdir(body["backup"]), body["backup"])

    def test_a_backup_failure_says_nothing_was_touched_and_carries_its_code(self):
        """备份就没成功 → 500 + `backup_failed`（**一个字节都没动**，与"已回滚"是两回事）。"""
        self._two_analyses()
        with mock.patch.object(jddelete, "_copy_into_backup",
                               side_effect=OSError("磁盘满了")):
            code, body = self._post_json("/api/jd/purge", {"id": self.JD_ID})
        self.assertEqual(code, 500, body)
        self.assertEqual(body["code"], "backup_failed", body)
        self.assertIn("没有删除任何东西", body["error"])
        # 真的一个字节都没动
        self.assertIsNotNone(jdid.find_jd(self.JD_ID))
        self.assertTrue(os.path.exists(os.path.join(store.p_path("reports"), "a1.html")))


_PURGE_PROBE_JS = """
function attrOf(html, name){
  const re = new RegExp(name + '="([^"]*)"');
  const m = re.exec(String(html || ''));
  return m ? m[1] : null;
}
function disabledOf(html, id){
  const re = new RegExp('<button[^>]*id="' + id + '"[^>]*>');
  const m = re.exec(String(html || ''));
  return !!(m && /\\bdisabled\\b/.test(m[0]));
}
function captureToasts(){
  __TOASTS = [];
  toast = function(msg){ __TOASTS.push(String(msg)); };
}
/* 只留下**业务请求**：`logUI` 往 `/api/log` 发的是遥测，数进"发了几个请求"里会把断言
   变成"日志发了几条"（顺序与条数都会被它搅乱）。 */
function bizCalls(calls){
  return calls.filter(function(c){ return String(c.url).indexOf('/api/log') < 0; });
}
"""


class JdDeleteFrontendTest(LedgerWithReportsCase):
    """前端入口（规格 §3）：按钮不再是禁用的占位；预演清单**逐条**渲染；两步确认；
    删除后刷新台账；每个 fetch 有 catch；所有数据过 `esc()`。"""

    def _row_fixture(self):
        self._analysis("a1", "简历甲", "", "a1.html", 66.0, "2026-10-01 09:00")
        self._analysis("a2", "简历乙", "", "a2.html", 72.0, "2026-10-05 09:00")
        return self._row()

    def _preview(self):
        """**真后端**产出的那份预演（前端要用真清单渲染，不是手搓一个像的）。"""
        self._row_fixture()
        code, body = self._get("/api/jd/purge_preview?jd=" + quote(self.JD_ID))
        self.assertEqual(code, 200, body)
        return body

    def test_the_card_carries_a_real_delete_button_not_a_disabled_placeholder(self):
        _need_node()
        row = self._row_fixture()
        out = web_probe.run_index(
            _PURGE_PROBE_JS +
            "JOBS = [__fixture.row];\n"
            "renderJobs();\n"
            "var html = String(document.querySelector('#jobList').innerHTML);\n"
            "out.btn = attrOf(html, 'data-jddel');\n"
            "out.disabled = disabledOf(html, 'jddel-' + __fixture.row.id);\n"
            "out.placeholder = html.indexOf('暂不可用') >= 0;\n",
            {"row": row})
        if not out.get("btn"):
            raise AssertionError("卡片里没有删除按钮（渲染没发生 → 断言会变成恒真的假绿）：%r" % (out,))
        self.assertEqual(out["btn"], self.JD_ID)
        self.assertFalse(out["disabled"], "删除按钮还是禁用的占位：%r" % (out,))
        self.assertFalse(out["placeholder"])
        # 绑定的那一行也要在（写了 data-jddel 却不绑 = 死按钮）
        self.assertIn("querySelectorAll('[data-jddel]')", _js_fn_body("renderJobs"))

    def test_the_preview_list_is_rendered_item_by_item(self):
        _need_node()
        p = self._preview()
        out = web_probe.run_index(
            "out.lines = jdPurgeLines(__fixture.p);\n"
            "out.text = out.lines.join('\\n');\n",
            {"p": p})
        lines = out["lines"]
        self.assertTrue(lines, "清单渲染成了空的（断言会变成恒真的假绿）")
        # 每一条分析、每一个报告文件、JD 原文、引用命中数**各自一行**
        for a in p["analyses"]:
            self.assertTrue(any(a["id"] in ln for ln in lines), "分析 %s 没被逐条列出" % a["id"])
        for f in p["files"]:
            self.assertTrue(any(f["name"] in ln for ln in lines),
                            "文件 %s 没被逐条列出" % f["name"])
        self.assertTrue(any(("知识库引用：" + str(p["refs"]["knowledge"]["hits"])) in ln
                            for ln in lines), lines)
        self.assertTrue(any(("面试记录：" + str(p["refs"]["interviews"]["hits"])) in ln
                            for ln in lines), lines)

    def test_the_second_confirmation_lists_every_report_file_of_the_real_preview(self):
        _need_node()
        p = self._preview()
        out = web_probe.run_index(
            # 第一句「是」→ 去预演；第二句「否」→ 打住（一个字节都不删）
            "__fetchJson = __fixture.p;\n"
            "__confirmQueue.push(true, false);\n"
            "await jdDelete(__fixture.id);\n"
            "out.confirms = __confirms.slice();\n",
            {"id": self.JD_ID, "p": p})
        msgs = out["confirms"]
        self.assertEqual(len(msgs), 2, "没有走两步确认：%r" % (msgs,))
        second = msgs[1]
        # 第二次确认里**逐条**列出将被删除的每一个报告文件（一个都不许漏）
        for f in p["files"]:
            if f["kind"] == "analysis_report":
                self.assertIn(f["name"], second, second)
        self.assertIn(self.JD_ID + ".txt", second)          # JD 原文也点名
        self.assertIn("不可恢复", second)
        self.assertIn("备份", second)
        # 每一条分析也逐条在清单里
        for a in p["analyses"]:
            self.assertIn(a["id"], second, second)

    def test_the_second_confirmation_lists_files_one_per_line_with_their_kind(self):
        """② 里每份文件**各自一行**、并带**它自己的**类型标签（不是一句通吃的"报告文件"）。

        审查者实证的变异：把逐条列出改成用 `、` join 成一行 —— **17 个测试照样全绿**，
        因为原断言是 `assertIn(name, 整段文字)`，对"逐条"这件事**毫无约束**。
        所以这里要求"存在一行**以该文件名结尾**"，并要求同行带对类型标签。
        """
        _need_node()
        p = self._preview()
        out = web_probe.run_index(
            "out.text = jdPurgeConfirmText(__fixture.p, jdPurgeLines(__fixture.p));\n",
            {"p": p})
        text = out["text"]
        self.assertTrue(text, "确认文案是空的（下面的断言会变成恒真的假绿）")
        # **只看 ② 区**：① 区（`jdPurgeLines`）本来就逐条列文件名，把整段文案拿来找
        # "有一行以这个名字结尾"会被 ① 满足 —— 那样 M1 那个变异（② 用 `、` join 成一行）
        # 照样绿，等于没测。（我第一版就是这么写的，变异当场打脸。）
        self.assertIn("②", text)
        sec2 = text.split("②", 1)[1].split("③", 1)[0]
        lines = [ln.strip() for ln in sec2.split("\n")]
        want_kind = {"jd_text": "JD 原文", "jd_image": "原截图", "analysis_report": "报告文件"}
        checked = 0
        for f in p["files"]:
            if f["kept"] or not f["exists"]:
                continue
            checked += 1
            self.assertTrue(any(ln.endswith(f["name"]) for ln in lines),
                            "文件 %s 在②区里没有单独占据一行：%r" % (f["name"], sec2))
            self.assertTrue(
                any(want_kind[f["kind"]] in ln and ln.endswith(f["name"]) for ln in lines),
                "文件 %s 的类型标签不是它自己的（要 %s）：%r" % (f["name"], want_kind[f["kind"]], sec2))
        self.assertGreaterEqual(checked, 3, "夹具里要删的文件太少，这条测试没在测东西")

    def test_the_second_confirmation_does_not_call_kept_or_absent_files_deleted(self):
        """`kept`（还有别的岗位在用）与**不在盘上**的文件，不许列在"即将删除"下面。

        原先 ② 的标题是"将被删除的报告文件"，却把这两类也列了进去 —— 而 ④ 又说 `kept`
        那些**留着**：同一段话自相矛盾，用户没法知道到底删哪几个。
        正确做法是**照样点名**，但放在④/⑤里说明白。
        """
        _need_node()
        self._row_fixture()
        absent = "a2.html"
        os.remove(os.path.join(store.p_path("reports"), absent))          # → 不在盘上
        app._save_analysis({"id": "别的岗的一条", "jd_id": "别的岗位_0009",
                            "report": "a1.html", "created_at": "2026-10-07 09:00",
                            "score": 70.0, "resume": "简历丁"})           # → a1.html 被保留
        code, p = self._get("/api/jd/purge_preview?jd=" + quote(self.JD_ID))
        self.assertEqual(code, 200, p)
        kinds = {f["name"]: f for f in p["files"]}
        self.assertTrue(kinds["a1.html"]["kept"], "夹具不对：a1.html 应当被判为保留")
        self.assertFalse(kinds[absent]["exists"], "夹具不对：a2.html 应当不在盘上")

        out = web_probe.run_index(
            "out.text = jdPurgeConfirmText(__fixture.p, jdPurgeLines(__fixture.p));\n",
            {"p": p})
        text = out["text"]
        # ② 区只许出现**真会删**的文件
        self.assertIn("②", text)
        sec2 = text.split("②", 1)[1].split("③", 1)[0]
        self.assertNotIn("a1.html", sec2, "要保留的文件被列进了「即将删除」：%r" % (sec2,))
        self.assertNotIn(absent, sec2, "不在盘上的文件被列进了「即将删除」：%r" % (sec2,))
        # ② 里至少要有一份**真会删**的文件（否则上面两条断言可能"因为 ② 是空的"而恒真）
        self.assertIn(self.JD_ID + ".txt", sec2, sec2)
        # ④ 区（**只看 ④ 到 ⑤ 之间**：扫"从 ④ 往后"的话，把名字印在 ⑤ 也能过）
        self.assertIn("④", text)
        sec4 = text.split("④", 1)[1].split("⑤", 1)[0] if "⑤" in text else text.split("④", 1)[1]
        self.assertIn("a1.html", sec4, "要保留的文件没在 ④ 里点名：%r" % (sec4,))
        # ⑤ 区只该有"不在盘上"的
        if "⑤" in text:
            sec5 = text.split("⑤", 1)[1]
            self.assertIn(absent, sec5, "不在盘上的文件没在 ⑤ 里点名：%r" % (sec5,))
            self.assertNotIn("a1.html", sec5, "要保留的文件混进了 ⑤：%r" % (sec5,))
        # ④ 里必须写**这一份自己的**原因，不能拿别的文件的原因糊过去
        self.assertIn("别的 JD", sec4, sec4)

    def test_the_second_confirmation_writes_each_kept_files_own_reason(self):
        """④ 区每份保留文件要带**它自己的** `why`（后端给的是两种不同的原因）。

        原先 ④ 把 `kept[0].why` 写成整段的共同原因 —— 一份"报告被别的 JD 用"
        和一份"JD 原文被别的 JD 用"混在一起时，解释就串了。
        """
        _need_node()
        p = {"id": "JD_X", "files": [
            {"name": "共用报告.html", "where": "reports", "kind": "analysis_report",
             "exists": True, "kept": True, "note": "",
             "why": "还有**别的 JD** 的分析在引用这份报告：留着它，绝不删别人的东西"},
            {"name": "共用原文.txt", "where": "jd", "kind": "jd_text", "exists": True,
             "kept": True, "note": "JD 原文",
             "why": "还有**别的 JD** 的记录点名了这份文件：留着它，绝不删别人的东西"},
            {"name": "真删.html", "where": "reports", "kind": "analysis_report",
             "exists": True, "kept": False, "note": "", "why": ""}]}
        out = web_probe.run_index(
            "out.text = jdPurgeConfirmText(__fixture.p, jdPurgeLines(__fixture.p));\n",
            {"p": p})
        text = out["text"]
        sec4 = text.split("④", 1)[1]
        # 每份保留文件**各自一行**，并带各自的原因（"分析在引用" vs "记录点名了"）
        lines = [ln.strip() for ln in sec4.split("\n")]
        self.assertTrue(any("共用报告.html" in ln and "的分析在引用" in ln for ln in lines),
                        "报告的原因不对：%r" % (sec4,))
        self.assertTrue(any("共用原文.txt" in ln and "的记录点名了" in ln for ln in lines),
                        "原文的原因不对：%r" % (sec4,))

    def test_every_failure_code_says_its_own_thing(self):
        """四种失败各自的说法都要对得上，尤其**不许把没删过的说成"已回滚"**。

        `code="failed"` 才是"已回滚"，`not_found` / `stale` / `unreadable` 都是**什么都没动**。
        原先只有一句无条件的"删除失败（已回滚）"，对后三种就是假话。
        """
        _need_node()
        p = self._preview()
        cases = [
            ("failed", "删除失败（已回滚）"),
            ("backup_failed", "一个字节都没动"),
            ("not_found", "什么都没动"),
            ("stale", "什么都没动"),
            ("unreadable", "什么都没动"),
            ("", "什么都没动"),                       # 未知 code 也不能声称回滚
        ]
        for code, want in cases:
            res = {"ok": False, "id": self.JD_ID, "error": "后端原话", "backup": ""}
            if code:
                res["code"] = code
            out = self._failed_purge_probe(res)
            text = "\n".join(out["toasts"]) + "\n".join(out["alerts"])
            self.assertIn(want, text, "code=%r 少了自己的说法：%r" % (code, text))
            if code != "failed":
                self.assertNotIn("已回滚", text,
                                 "code=%r 不该说「已回滚」：%r" % (code, text))
            # 后端的原话必须原样转述（不许被前端的概括吞掉）
            self.assertIn("后端原话", text, "code=%r 没转述后端的话：%r" % (code, text))

    def test_the_success_alert_writes_each_skipped_files_own_reason(self):
        """成功弹窗里"保留未删"的原因要**逐个照抄后端给的 `why`**，不能概括成一句。

        `jddelete` 会给出两种不同的原因（"还有别的 JD 在用" / "文件不在盘上"），
        前端原先对**所有** `skipped_files` 都硬写"（还有别的岗位在用）" ——
        对第二种就是假话，而这句话正是用户判断"我到底删掉了什么"的依据。
        """
        _need_node()
        p = self._preview()
        out = web_probe.run_index(
            _PURGE_PROBE_JS +
            "captureToasts();\n"
            "var calls = [];\n"
            "fetch = function(url, opt){\n"
            "  var u = String(url);\n"
            "  calls.push(String(url));\n"
            "  if(u.indexOf('/api/jd/purge_preview') === 0)\n"
            "    return Promise.resolve({ok:true, json:function(){ return Promise.resolve(__fixture.p); }});\n"
            "  if(u.indexOf('/api/jd/purge') === 0)\n"
            "    return Promise.resolve({ok:true, json:function(){ return Promise.resolve(__fixture.res); }});\n"
            "  return Promise.resolve({ok:true, json:function(){ return Promise.resolve({ok:true, jds:[]}); }});\n"
            "};\n"
            "__confirmQueue.push(true, true);\n"
            "await jdDelete(__fixture.id);\n"
            "out.alerts = __alerts.slice();\n",
            {"id": self.JD_ID, "p": p,
             "res": {"ok": True, "id": self.JD_ID,
                     "backup": "E:/x/data/profiles/删除备份_20261013_120000",
                     "removed_files": [{"name": "a1.html"}],
                     "skipped_files": [
                         {"name": "共用报告.html", "why": "还有**别的 JD** 的分析在引用这份报告"},
                         {"name": "早就没了.html", "why": "文件不在盘上（可能已被手工删掉）"}]}})
        self.assertEqual(len(out["alerts"]), 1, out)
        text = out["alerts"][0]
        self.assertIn("共用报告.html", text)
        self.assertIn("早就没了.html", text)
        # 两种原因都要**照实**写出来，而不是一句"（还有别的岗位在用）"糊过去
        self.assertIn("还有**别的 JD** 的分析在引用这份报告", text)
        self.assertIn("文件不在盘上（可能已被手工删掉）", text)

    def test_clicking_the_delete_button_really_invokes_the_purge_flow(self):
        """点那个按钮**真的**会走删除流程（不是"渲染了个按钮 + 源码里有行字符串"）。

        审查者实证的变异：把绑定改成空函数 `()=>{ }`，**17 个前端/路由测试照样全绿** ——
        因为垫片的 `querySelectorAll` 永远返回 `[]`，"死按钮"根本走不到。
        垫片补上真实按钮解析之后，这条测试用 `click()` 真正点一次。
        """
        _need_node()
        row = self._row_fixture()
        p = self._preview()
        out = web_probe.run_index(
            _PURGE_PROBE_JS +
            "captureToasts();\n"
            "var calls = [];\n"
            "fetch = function(url, opt){\n"
            "  calls.push(String(url));\n"
            "  return Promise.resolve({ok:true, json:function(){ return Promise.resolve(__fixture.p); }});\n"
            "};\n"
            "JOBS = [__fixture.row];\n"
            "renderJobs();\n"
            "var btns = document.querySelector('#jobList').querySelectorAll('[data-jddel]');\n"
            "out.btnCount = btns.length;\n"
            "out.btnValue = btns.length ? btns[0].dataset.jddel : null;\n"
            "__confirmQueue.push(true, false);\n"      # 第一句"是"→去预演；第二句"否"→打住
            "if(btns.length) btns[0].click();\n"
            "await new Promise(function(r){ setTimeout(r, 0); });\n"
            "out.calls = bizCalls(calls.map(function(u){ return {url: u}; }));\n",
            {"row": row, "p": p})
        self.assertEqual(out["btnCount"], 1, "卡片里没找到删除按钮：%r" % (out,))
        self.assertEqual(out["btnValue"], self.JD_ID)
        self.assertTrue(out["calls"], "点了删除按钮却一个请求都没发（绑定是死的）")
        # 台账渲染本身也会发 `/api/interview/list`（每个岗位一次），所以不能盯 `calls[0]`
        purge = [c["url"] for c in out["calls"] if "/api/jd/purge" in c["url"]]
        self.assertTrue(purge, "点了删除按钮却没发预演请求（绑定是死的）：%r" % (out["calls"],))
        self.assertIn("/api/jd/purge_preview", purge[0])

    def test_delete_flow_uses_the_real_preview_and_refreshes_the_ledger(self):
        _need_node()
        p = self._preview()
        out = web_probe.run_index(
            _PURGE_PROBE_JS +
            "captureToasts();\n"
            "var calls = [];\n"
            "fetch = function(url, opt){\n"
            "  var u = String(url);\n"
            "  if(u.indexOf('/api/jd/purge_preview') === 0){\n"
            "    calls.push({url: u, method: String((opt && opt.method) || 'GET')});\n"
            "    return Promise.resolve({ok:true, json:function(){ return Promise.resolve(__fixture.p); }});\n"
            "  }\n"
            "  if(u.indexOf('/api/jd/purge') === 0){\n"
            "    calls.push({url: u, method: String((opt && opt.method) || 'GET'),\n"
            "                body: String((opt && opt.body) || '')});\n"
            "    return Promise.resolve({ok:true, json:function(){ return Promise.resolve(__fixture.res); }});\n"
            "  }\n"
            "  calls.push({url: u, method: String((opt && opt.method) || 'GET')});\n"
            "  return Promise.resolve({ok:true, json:function(){ return Promise.resolve({ok:true, jds:[]}); }});\n"
            "};\n"
            "__confirmQueue.push(true, true);\n"
            "await jdDelete(__fixture.id);\n"
            "out.confirms = __confirms.slice();\n"
            "out.alerts = __alerts.slice();\n"
            "out.calls = bizCalls(calls);\n",
            {"id": self.JD_ID, "p": p,
             "res": {"ok": True, "id": self.JD_ID, "backup": "E:/x/data/profiles/删除备份_20261013_120000",
                     "removed_files": [{"name": "a1.html"}, {"name": "a2.html"},
                                       {"name": self.JD_ID + ".txt"}],
                     "skipped_files": []}})
        self.assertEqual(len(out["confirms"]), 2, "没有走两步确认：%r" % (out["confirms"],))
        self.assertEqual(len(out["calls"]), 3, out["calls"])
        # ① 先**只读**预演（参数名就是规格里的 `jd`）
        self.assertIn("/api/jd/purge_preview?jd=", out["calls"][0]["url"])
        self.assertEqual(out["calls"][0]["method"], "GET")
        # ② 第二次确认的文案里**逐条**列出了将被删除的报告文件
        second = out["confirms"][1]
        for name in ("a1.html", "a2.html"):
            self.assertIn(name, second, second)
        self.assertIn(self.JD_ID + ".txt", second)
        self.assertIn("不可恢复", second)
        self.assertIn("备份", second)
        # ③ 删除请求带着预演给的那份清单的指纹
        self.assertEqual(out["calls"][1]["method"], "POST")
        self.assertIn("/api/jd/purge", out["calls"][1]["url"])
        self.assertEqual(json.loads(out["calls"][1]["body"]),
                         {"id": self.JD_ID, "confirm_fingerprint": p["fingerprint"]})
        # ④ 删完**如实说出备份路径**（不许让用户以为磁盘上真没了）
        self.assertEqual(len(out["alerts"]), 1, out["alerts"])
        self.assertIn("删除备份_20261013_120000", out["alerts"][0])
        self.assertIn("不可恢复", out["alerts"][0])
        # ⑤ 刷新台账（第三次请求就是它）
        self.assertIn("/api/jds", out["calls"][2]["url"])

    def test_a_failed_purge_is_reported_and_never_says_deleted(self):
        """请求**发出去但结果拿不到**（网络中断）时的说法。

        这一支最容易被写成谎话：请求可能已经在服务端跑完了（甚至已经删掉了），只是回包没
        拿到。所以"已回滚"和"一个字节都没动"**都不能说** —— 唯一诚实的是"不知道，去看台账"，
        而且程序要**替用户把台账刷新出来**，别让他自己猜。
        """
        _need_node()
        p = self._preview()
        out = web_probe.run_index(
            _PURGE_PROBE_JS +
            "captureToasts();\n"
            "var calls = [];\n"
            "fetch = function(url, opt){\n"
            "  var u = String(url);\n"
            "  calls.push(u);\n"
            "  if(u.indexOf('/api/jd/purge_preview') === 0)\n"
            "    return Promise.resolve({ok:true, json:function(){ return Promise.resolve(__fixture.p); }});\n"
            "  if(u.indexOf('/api/jd/purge') === 0)\n"
            "    return Promise.reject(new Error('连接被重置'));\n"
            "  return Promise.resolve({ok:true, json:function(){ return Promise.resolve({ok:true, jds:[]}); }});\n"
            "};\n"
            "__confirmQueue.push(true, true);\n"
            "await jdDelete(__fixture.id);\n"
            "out.alerts = __alerts.slice();\n"
            "out.toasts = __TOASTS.slice();\n"
            "out.calls = bizCalls(calls.map(function(u){ return {url: u}; }));\n",
            {"id": self.JD_ID, "p": p, "res": None})
        self.assertEqual(out["unexpected"], [], "catch 没兜住：%r" % (out["unexpected"],))
        self.assertEqual(out["alerts"], [], "失败了却弹了「已删除」的提示")
        self.assertTrue(out["toasts"], "失败时一句提示都没有")
        text = "\n".join(out["toasts"])
        self.assertNotIn("已真删除", text)
        self.assertNotIn("已回滚", text, "结果未知却说「已回滚」：%r" % (text,))
        self.assertNotIn("一个字节都没动", text, "结果未知却说「什么都没动」：%r" % (text,))
        self.assertIn("不知道", text)
        # 程序要**替用户把状态查清楚**：预演 + 删除 + 刷新台账
        self.assertEqual(len(out["calls"]), 3, out["calls"])
        self.assertIn("/api/jds", out["calls"][2]["url"])

    def test_an_unreadable_ledger_stops_the_flow_before_any_delete_request(self):
        """预演说 `can_purge=false`（台账读不出来）→ **一个删除请求都不许发**。

        这条闸门非有不可：台账读不出来时清单看起来"只有 0 条分析"，照它删下去会把
        整份台账覆盖成空（所有岗位的分析记录全没）。用 alert 而不是 toast：
        这是要用户去修文件的，不能 3 秒后自己消失。
        """
        _need_node()
        blocked = {"ok": True, "id": self.JD_ID, "found": True, "can_purge": False,
                   "unreadable": "台账文件 `jobs.json` 读不出来：Expecting value",
                   "reason": "", "not_found": "", "fingerprint": "deadbeef",
                   "jd_record": None, "analyses": [], "files": [],
                   "refs": {"knowledge": {"hits": 0, "cards": [], "cards_kept": 0, "note": ""},
                            "interviews": {"hits": 0, "records": []}},
                   "summary": {"analyses": 0, "files": 0, "reports": 0, "kb_hits": 0,
                               "interviews": 0},
                   "advisories": [{"kind": "unreadable_jobs",
                                   "message": "台账文件 `jobs.json` 读不出来：Expecting value"}]}
        out = web_probe.run_index(
            _PURGE_PROBE_JS +
            "captureToasts();\n"
            "var calls = [];\n"
            "fetch = function(url, opt){\n"
            "  var u = String(url);\n"
            "  calls.push(u);\n"
            "  if(u.indexOf('/api/jd/purge_preview') === 0)\n"
            "    return Promise.resolve({ok:true, json:function(){ return Promise.resolve(__fixture.p); }});\n"
            "  return Promise.resolve({ok:true, json:function(){ return Promise.resolve({ok:true, jds:[]}); }});\n"
            "};\n"
            "__confirmQueue.push(true, true);\n"
            "await jdDelete(__fixture.id);\n"
            "out.alerts = __alerts.slice();\n"
            "out.toasts = __TOASTS.slice();\n"
            "out.confirms = __confirms.slice();\n"
            "out.calls = bizCalls(calls.map(function(u){ return {url: u}; }));\n",
            {"id": self.JD_ID, "p": blocked})
        self.assertEqual(out["unexpected"], [], "catch 没兜住：%r" % (out["unexpected"],))
        # 只发了那一个预演请求；**没有任何** /api/jd/purge
        self.assertEqual(len(out["calls"]), 1, out["calls"])
        self.assertNotIn("/api/jd/purge?", out["calls"][0]["url"])
        # 没有第二次确认（连清单都不该给用户看）
        self.assertEqual(len(out["confirms"]), 1, out["confirms"])
        self.assertEqual(len(out["alerts"]), 1, "没用 alert 说清楚：%r" % (out,))
        self.assertIn("不能删", out["alerts"][0])
        self.assertIn("读不出来", out["alerts"][0])

    def _failed_purge_probe(self, res):
        """后端返回 `{ok:false, code, backup, error}` 时，前端到底说了什么。"""
        _need_node()
        p = self._preview()
        return web_probe.run_index(
            _PURGE_PROBE_JS +
            "captureToasts();\n"
            "var calls = [];\n"
            "fetch = function(url, opt){\n"
            "  var u = String(url);\n"
            "  calls.push(u);\n"
            "  if(u.indexOf('/api/jd/purge_preview') === 0)\n"
            "    return Promise.resolve({ok:true, json:function(){ return Promise.resolve(__fixture.p); }});\n"
            "  if(u.indexOf('/api/jd/purge') === 0)\n"
            "    return Promise.resolve({ok:true, json:function(){ return Promise.resolve(__fixture.res); }});\n"
            "  return Promise.resolve({ok:true, json:function(){ return Promise.resolve({ok:true, jds:[]}); }});\n"
            "};\n"
            "__confirmQueue.push(true, true);\n"
            "await jdDelete(__fixture.id);\n"
            "out.alerts = __alerts.slice();\n"
            "out.toasts = __TOASTS.slice();\n"
            "out.calls = bizCalls(calls.map(function(u){ return {url: u}; }));\n",
            {"id": self.JD_ID, "p": p, "res": res})

    def test_a_failed_rollback_uses_an_alert_and_never_says_rolled_back(self):
        """`rollback_failed`：**数据可能不是原样**，只说一个 3 秒就消失的 toast 是不够的。

        断言读的是**弹窗/提示的真实文本**（探针捕获的运行时值），不是"整页里有没有某个
        字符串" —— 后者会被 `index.html` 源码里那句 `'删除失败（已回滚）'` 直接满足（假绿）。
        """
        bk = "E:/x/data/profiles/删除备份_20261013_120000"
        out = self._failed_purge_probe(
            {"ok": False, "id": self.JD_ID, "code": "rollback_failed", "backup": bk,
             "error": "删除失败：boom（**回滚也失败了**，请手工从 %s 恢复）" % bk})
        self.assertEqual(out["unexpected"], [], "catch 没兜住：%r" % (out["unexpected"],))
        self.assertEqual(len(out["alerts"]), 1,
                         "回滚失败没有用 alert —— 用户会错过「要手工恢复」这句话：%r" % (out,))
        text = out["alerts"][0]
        self.assertIn(bk, text, "备份路径没给到用户，就没法自救了：%r" % (text,))
        self.assertNotIn("已回滚", text,
                         "回滚失败还说「已回滚」—— 用户会以为数据没事：%r" % (text,))
        self.assertEqual(out["toasts"], [],
                         "只弹了一个会自己消失的 toast：%r" % (out["toasts"],))

    def test_a_failed_backup_says_nothing_was_touched_and_not_rolled_back(self):
        """`backup_failed`：备份就没成功 = **一个字节都没动**，说"已回滚"是多说的。"""
        out = self._failed_purge_probe(
            {"ok": False, "id": self.JD_ID, "code": "backup_failed", "backup": "",
             "error": "备份失败，**没有删除任何东西**：磁盘满了"})
        self.assertEqual(out["alerts"], [], "备份失败不该弹「要手工恢复」的告警")
        text = "\n".join(out["toasts"])
        self.assertTrue(text, "备份失败时一句提示都没有")
        self.assertIn("一个字节都没动", text)
        self.assertNotIn("已回滚", text,
                         "备份都没成功却说「已回滚」：%r" % (text,))

    def test_a_failed_preview_says_so_and_sends_no_delete_request(self):
        _need_node()
        out = web_probe.run_index(
            _PURGE_PROBE_JS +
            "captureToasts();\n"
            "var calls = [];\n"
            "fetch = function(url, opt){\n"
            "  calls.push(String(url));\n"
            "  return Promise.reject(new Error('服务没在跑'));\n"
            "};\n"
            "__confirmQueue.push(true);\n"
            "await jdDelete(__fixture.id);\n"
            "out.alerts = __alerts.slice();\n"
            "out.toasts = __TOASTS.slice();\n"
            "out.calls = bizCalls(calls.map(function(u){ return {url: u}; }));\n",
            {"id": self.JD_ID})
        self.assertEqual(out["unexpected"], [], "catch 没兜住：%r" % (out["unexpected"],))
        self.assertEqual(len(out["calls"]), 1, out["calls"])
        self.assertEqual(out["alerts"], [])
        self.assertIn("预演失败", "\n".join(out["toasts"]))

    def test_the_two_confirmations_are_taken_before_any_delete_request(self):
        _need_node()
        p = self._preview()
        out = web_probe.run_index(
            _PURGE_PROBE_JS +
            "captureToasts();\n"
            "var calls = [];\n"
            "fetch = function(url, opt){\n"
            "  calls.push(String(url));\n"
            "  return Promise.resolve({ok:true, json:function(){ return Promise.resolve(__fixture.p); }});\n"
            "};\n"
            "__confirmQueue.push(true, false);\n"   # 第二步点了「取消」
            "await jdDelete(__fixture.id);\n"
            "out.confirms = __confirms.slice();\n"
            "out.calls = bizCalls(calls.map(function(u){ return {url: u}; }));\n"
            "out.toasts = __TOASTS.slice();\n",
            {"id": self.JD_ID, "p": p})
        self.assertEqual(len(out["confirms"]), 2, out["confirms"])
        self.assertEqual(len(out["calls"]), 1, out["calls"])          # 只预演，没发删除
        self.assertIn("/api/jd/purge_preview", out["calls"][0]["url"])
        self.assertIn("已取消", "\n".join(out["toasts"]))


# --------------------------------------- 启动前置检查：档名文件读不出来时的人话 + 非零退出
# 复现的真实形态：data/current_profile.txt 被记事本按 ANSI/GBK 存过中文档名 ⇒ 存在却读不出来。
# 从 87cabe9 起 `store.current_profile()` 不再静默回退 default（那个决定是对的），
# 但 import 期的解析（log / app）会**裸抛**：用户双击启动只看到一坨 traceback。
#
# 探针脚本的顺序**绝不能反**：先把 store.DATA_ROOT 指到隔离根，再 import app。
# app 在 import 期就解析档名（log.refresh()），那一刻 DATA_ROOT 若还是真实的 data/，
# 探针就会去读用户的真实档名文件（今天刚出过一次「探针没隔离根目录」的事故）。
_STARTUP_PROBE = '''# -*- coding: utf-8 -*-
import sys
sys.path.insert(0, %r)
import store
store.DATA_ROOT = %r
import log
import app
# 万一守卫失效：让 main() 立刻返回，而不是真起一个 ThreadingHTTPServer 把用例挂住。
# 守卫在 main() 的第一行，所以正常情况下这行假实现只会被"越过"、不会被用到。
app.port_in_use = lambda port: True
print("APP_IMPORTED_OK")
print("LOG_FILE=" + log.LOG_FILE)
'''

_MAIN_PROBE = '''
print("BEFORE_MAIN")                   # 能打到这行 = import 期（含档名解析）确实走通了
app.main()
print("MAIN_RETURNED")
'''

# import **之后**才把档名文件弄坏（记事本按 GBK 存中文档名）：只有这样才测得到 main() 自己的
# 那道检查 —— 坏在 import 之前的话，第一道检查就把进程拦掉了，main() 根本进不去。
_BREAK_AFTER_IMPORT = '''
import io as _io
with _io.open(store.current_profile_file(), "wb") as _f:
    _f.write("周汉钦".encode("gbk"))
print("PROFILE_BROKEN")
'''


class StartupProfilePreconditionTest(IsolatedCase):
    """三个入口（log 的 import 期 / app 的 import 期 / app.main()）都要一行人话 + 非零退出。

    子进程探针是**唯一**能证明「import 期也没裸抛」的证据：`unittest` 进程里 app 早就
    import 过了，import 期那条路径不可能再走一遍。
    """

    def setUp(self):
        super().setUp()
        self.repo = os.path.dirname(os.path.abspath(app.__file__))

    # ---- 脚手架
    def _write_probe(self, extra=""):
        path = os.path.join(self.tmp, "startup_probe.py")
        with open(path, "w", encoding="utf-8") as f:
            f.write((_STARTUP_PROBE % (self.repo, self.tmp)) + extra)
        return path

    def _run_probe(self, extra=""):
        """跑探针。带超时 —— 实现被改坏时它可能去起服务，绝不能把用例挂死在这里。"""
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        return subprocess.run([sys.executable, self._write_probe(extra)],
                              capture_output=True, timeout=90, env=env)

    @staticmethod
    def _text(raw):
        # app 在 import 期把 stderr 重配成了 utf-8（见 app.py 顶部），所以按 utf-8 解
        return raw.decode("utf-8", "replace")

    def _gbk_profile_file(self):
        fp = store.current_profile_file()
        with open(fp, "w", encoding="gbk") as f:
            f.write("周汉钦")
        with open(fp, "rb") as f:
            self.assertRaises(UnicodeDecodeError, f.read().decode, "utf-8")
        return fp

    def _assert_human_abort(self, returncode, stderr):
        """非零退出 + 三句人话（哪个文件 / 为什么不能继续 / 怎么修）+ 没有 traceback。"""
        self.assertEqual(returncode, 2,
                         "退出码应为 2（启动前置条件不满足，什么都没做），实际 %r\nstderr=%s"
                         % (returncode, stderr))
        self.assertNotIn("Traceback", stderr, "给用户的是 traceback，不是人话：\n%s" % stderr)
        self.assertIn("current_profile.txt", stderr)     # 哪个文件坏了
        self.assertIn("default", stderr)                 # 为什么不能继续（串档）+ 怎么修
        self.assertIn("UTF-8", stderr)                   # 怎么修

    # ---- 入口 1+2：log 的 import 期 + app 的 import 期
    def test_import_time_check_exits_nonzero_with_a_human_message(self):
        self._gbk_profile_file()

        p = self._run_probe()

        self.assertNotIn("APP_IMPORTED_OK", self._text(p.stdout), "档名读不出来却把 app 起来了")
        self._assert_human_abort(p.returncode, self._text(p.stderr))
        # **反串档**：中止的同时，default 档里必须一个字节都没被写（日志也不例外）
        self.assertFalse(os.path.exists(os.path.join(store.profiles_root(), "default",
                                                     "logs", "events.jsonl")),
                         "档名坏了却往 default 档里写了东西")

    # ---- 入口 3：app.main()（档名文件在 import 之后才坏掉）
    def test_main_also_aborts_nonzero_when_the_profile_breaks_after_import(self):
        # 反证：import 那一刻档名文件还是好的，坏掉发生在 import 之后
        self.assertEqual(store.current_profile(), "default")

        p = self._run_probe(_BREAK_AFTER_IMPORT + _MAIN_PROBE)

        out = self._text(p.stdout)
        self.assertIn("APP_IMPORTED_OK", out)     # 反证：import 期确实是好的（走的是第二道检查）
        self._assert_human_abort(p.returncode, self._text(p.stderr))
        self.assertIn("PROFILE_BROKEN", out)      # 反证：坏掉确实发生在 import 之后
        self.assertIn("BEFORE_MAIN", out)         # 反证：这次真的走到了 main()
        self.assertNotIn("MAIN_RETURNED", out, "档名坏了却把服务起起来了")
        # 中止得够早：这一趟一个字节都没写进 default 档的日志
        self.assertFalse(os.path.exists(log.LOG_FILE))

    # ---- 回归钉子：删掉那个文件 = 回到 default，一切照旧
    def test_removing_the_file_is_a_normal_start(self):
        fp = store.current_profile_file()
        self.assertTrue(os.path.exists(fp))          # 反证：文件原本真的在（是 IsolatedCase 建的）
        os.remove(fp)

        p = self._run_probe(_MAIN_PROBE)

        out = self._text(p.stdout)
        self.assertEqual(p.returncode, 0, self._text(p.stderr))
        self.assertIn("APP_IMPORTED_OK", out)
        self.assertIn("MAIN_RETURNED", out)
        self.assertNotIn("Traceback", self._text(p.stderr))
        self.assertIn(os.path.join(store.profiles_root(), "default", "logs"), out)

    def test_a_valid_profile_file_still_starts_and_is_honored(self):
        """反向的钉子：修法绝不能变成「档名文件存在就拒绝启动」。"""
        store.set_current_profile("周汉钦")

        p = self._run_probe()

        out = self._text(p.stdout)
        self.assertEqual(p.returncode, 0, self._text(p.stderr))
        self.assertIn("APP_IMPORTED_OK", out)
        self.assertIn(os.path.join(store.profiles_root(), "周汉钦", "logs"), out)

    # ---- 单元层：可单测的校验函数本身
    def test_precondition_returns_none_when_the_file_is_fine(self):
        self.assertIsNone(app.profile_precondition_error())

    def test_precondition_returns_the_message_when_the_file_is_broken(self):
        fp = self._gbk_profile_file()
        msg = app.profile_precondition_error()
        self.assertIsInstance(msg, str)
        for word in (fp, "default", "UTF-8"):
            self.assertIn(word, msg)

    def test_precondition_does_not_swallow_other_exceptions(self):
        """**这次改动最容易引入的坏味道**：`except Exception` 顺手把真 bug 也包成同一句人话。

        真 bug（这里用 ValueError 代表）必须原样冒出去 —— 被同一出口吞掉的话，
        用户看到的是「档名文件坏了」，而真实原因（代码/IO 缺陷）永远查不到。
        """
        with mock.patch.object(store, "current_profile",
                               mock.Mock(side_effect=ValueError("真 bug，不是档名问题"))):
            self.assertRaises(ValueError, app.profile_precondition_error)


# ------------------------------------------------------- P2-1：来源校验（Host / Origin）
# 威胁（体检报告 §4 P2-1）：**任意一个你正在浏览的网页**都能对 http://127.0.0.1:8000/api/config
# 发一个 `text/plain` 的**简单请求**（不触发 CORS 预检），body 里只换 base_url；
# `cfg[k].update(body[k])` 会**保留你原来的 api_key**，下一次分析就把 key 打给攻击者的地址
# （不需要读回包）。同一手法还能打 /api/logs/clear（销掉取证线索）、/api/resumes 等；
# 没有 Host 校验时，域名重绑定下连 `GET /api/config`（**明文回 api_key**）都开着。
#
# 修法只能落在服务端（浏览器端做任何判断都不是防线）。本类钉住三件事：
#   ① 外站 Origin 的请求被拒，且**盘上的 config.json 逐字节没变**（不是"只挡了状态码"）；
#   ② 非本机 Host 被拒（挡 DNS rebinding），响应体里绝不出现 api_key；
#   ③ 对照组必须**照常工作**：不带 Origin（命令行 curl / 双击 启动.bat 打开的页面）
#      与恰好同源的两个写法（127.0.0.1 / localhost）。
#
# ⚠️ 缺 Host 的请求**必须放行**（不是"漏了"）：HTTP/1.0 与非浏览器客户端不带它，
# 本文件的沙箱 handler 与 tests/test_kb.py:1209 也一样不带 —— 判它非法 = 把既有测试与
# 真实用法一起弄红。而域名重绑定下的浏览器**必定**带 Host（且是攻击者的域名），
# 所以"缺 Host 放行"不削弱这条防线。
class ForeignRequestGuardTest(IsolatedCase):
    ATTACKER_BASE_URL = "https://attacker.invalid/v1"
    USER_KEY = "sk-user-own-key-must-never-leave-0123456789"

    # ---- 脚手架
    def _seed_user_config(self):
        """盘上放一份"用户自己配好的"配置，返回它的原始字节（用来逐字节比对）。"""
        store.ensure_profile()
        store.write_json(store.config_path(),
                         {"text_model": {"base_url": "https://api.deepseek.com/v1",
                                         "api_key": self.USER_KEY, "model": "deepseek-chat"}})
        before = _disk_bytes(store.config_path())
        self.assertIn(self.USER_KEY.encode("utf-8"), before)      # 反证：key 真的在盘上
        return before

    def _sandbox_get(self, path, headers=None):
        h = _SandboxedHandler(path)
        h.headers.update(headers or {})
        h.do_GET()
        return h

    def _real_server(self):
        """真 socket + 真 http.client：Host 由客户端按**真实端口**自动带上（最接近攻击现场）。"""
        import http.client
        from http.server import ThreadingHTTPServer
        srv = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        self.addCleanup(srv.server_close)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=15)
        self.addCleanup(conn.close)
        return conn, srv.server_address[1]

    def _post_config(self, conn, headers, base_url=None):
        raw = json.dumps({"text_model": {"base_url": base_url or self.ATTACKER_BASE_URL}})
        conn.request("POST", "/api/config", body=raw, headers=headers)
        r = conn.getresponse()
        return r.status, r.read().decode("utf-8")

    # ---- ① 真正要防的那件事：跨站请求把模型地址换成攻击者的
    def test_cross_origin_post_cannot_rewrite_the_model_base_url(self):
        before = self._seed_user_config()
        conn, _port = self._real_server()

        # 攻击者页面发的正是这种"简单请求"：text/plain 不触发预检
        status, body = self._post_config(conn, {"Content-Type": "text/plain",
                                                "Origin": "http://evil.example"})

        self.assertEqual(status, 403, "跨站请求没被拒（body=%s）" % body)
        self.assertEqual(_disk_bytes(store.config_path()), before,
                         "config.json 被跨站请求改掉了：base_url 一换，用户自己的 api_key "
                         "就会被下一次分析打给攻击者的地址")
        self.assertNotIn(self.ATTACKER_BASE_URL, body)

    # ---- ② DNS rebinding：非本机 Host，读路径也全关
    def test_foreign_host_cannot_read_the_config(self):
        self._seed_user_config()
        h = self._sandbox_get("/api/config", {"Host": "attacker.example"})

        self.assertEqual(h.code, 403, "Host 不是本机却没被拒（响应=%r）" % h.text()[:200])
        self.assertNotIn(self.USER_KEY, h.text(), "域名重绑定下 api_key 被明文读走了")

    # ---- 拒绝的响应本身：一句人话 + 一条取证日志
    def test_foreign_origin_is_rejected_with_a_human_message_and_a_log_line(self):
        h = self._sandbox_get("/api/config", {"Origin": "http://evil.example"})

        self.assertEqual(h.code, 403)
        body = h.text()
        self.assertIn("不是从本机页面", body)     # 用户能据此判断"我是不是被别的网页打了"
        self.assertIn("http://127.0.0.1:%d" % app.PORT, body)   # 告诉他正常入口是哪个
        raw = _disk_bytes(log.LOG_FILE).decode("utf-8")
        self.assertIn("evil.example", raw)        # 记一条日志，便于事后取证

    # ---- ③ 对照组：不带 Origin（命令行 curl / 双击启动）必须照常
    def test_request_without_origin_is_allowed(self):
        self._seed_user_config()
        h = self._sandbox_get("/api/config", {"Host": "127.0.0.1:%d" % app.PORT})

        self.assertEqual(h.code, 200, "不带 Origin 的正常请求被误伤了（命令行/双击场景）")
        self.assertEqual((h.json_body().get("text_model") or {}).get("api_key"), self.USER_KEY)

    # ---- ④ 对照组：恰好同源（两种写法）必须照常
    def test_same_origin_is_allowed(self):
        self._seed_user_config()
        for host, origin in (("127.0.0.1:%d" % app.PORT, "http://127.0.0.1:%d" % app.PORT),
                             ("localhost:%d" % app.PORT, "http://localhost:%d" % app.PORT)):
            h = self._sandbox_get("/api/config", {"Host": host, "Origin": origin})
            self.assertEqual(h.code, 200, "%s 被误伤了" % origin)

    def test_same_origin_post_over_a_real_socket_still_writes_the_config(self):
        """同源 POST 必须真的生效 —— 顺带钉住"端口要对"是拿**服务真正监听的端口**比的
        （这里监听的是临时端口，写死 8000 的话这条就会红）。"""
        self._seed_user_config()
        conn, port = self._real_server()
        new_url = "https://api.example.org/v1"

        status, body = self._post_config(conn,
                                         {"Content-Type": "application/json",
                                          "Origin": "http://127.0.0.1:%d" % port},
                                         base_url=new_url)

        self.assertEqual(status, 200, body)
        cfg = json.loads(_disk_bytes(store.config_path()).decode("utf-8"))
        self.assertEqual(cfg["text_model"]["base_url"], new_url)        # 真的写下去了
        self.assertEqual(cfg["text_model"]["api_key"], self.USER_KEY)   # key 没被动

    # ---- 别的入口/别的破坏面走的是同一个校验
    def test_foreign_origin_cannot_clear_the_logs(self):
        log.log_event("canary.before_attack")
        before = _disk_bytes(log.LOG_FILE)
        self.assertIn(b"canary.before_attack", before)      # 反证：取证线索原本真的在

        h = _sandbox_handler("POST", "/api/logs/clear", b"", {"Origin": "http://evil.example"})

        self.assertEqual(h._status, 403)
        # 日志**没被清空**：既没走 logs.cleared，原来的那行也还在
        # （拒绝本身会追一行取证记录，所以这里比"内容"而不是比"字节"）
        after = _disk_bytes(log.LOG_FILE)
        self.assertIn(b"canary.before_attack", after, "取证线索被跨站请求销掉了")
        self.assertNotIn(b"logs.cleared", after)

    def test_foreign_origin_delete_is_rejected(self):
        h = _sandbox_handler("DELETE", "/api/resumes?name=x.md", b"",
                             {"Origin": "http://evil.example"})

        self.assertEqual(h._status, 403)

    # ---- 顺手：nosniff
    def test_responses_carry_nosniff(self):
        store.ensure_profile()
        h = self._sandbox_get("/api/config", {"Host": "127.0.0.1:%d" % app.PORT})
        self.assertEqual(h.headers_out.get("X-Content-Type-Options"), "nosniff")

        store.write_text(os.path.join(store.p_path("reports"), "nosniff.html"),
                         "<html><body>没有 REPORT_DATA 快照</body></html>")
        hf = self._sandbox_get("/reports/nosniff.html", {"Host": "127.0.0.1:%d" % app.PORT})
        self.assertEqual(hf.code, 200)
        self.assertEqual(hf.headers_out.get("X-Content-Type-Options"), "nosniff")


# ============================================================ 批 4「说假话」（报告 §3）
# P1-13 / P1-9 / P1-10 / P1-11 四条：服务端把"读不出来"说成"没有"或"成功"。
# 这一批的共同判据只有一句：**读失败必须与"没有"分得开，且必须有一句真话 + 一条日志**。
class LyingReportBatchTest(IsolatedCase):
    """批 4 服务端四条。沙箱 handler 一律**不带 Host**（缺 Host 放行是既定口径，见 P2-1）。"""

    def _sandbox(self, method, path, body: bytes, headers=None):
        h = _sandbox_handler(method, path, body, headers)
        return h

    # ---------------- P1-13：请求体不是合法 JSON ≠ 没有参数
    def test_bad_json_body_is_400_not_silently_empty(self):
        """坏 JSON 必须 400 + 说清"请求体不是合法 JSON" + 留一条日志；**绝不**回 ok:true。

        旧实现 `_json_body` 把 json.loads 的异常吞成 `{}` ⇒ POST /api/config 会“当成没改
        任何东西”照常保存并回 ok:true —— 用户以为设置存上了，其实一个字段都没动。
        """
        raw = b'{"text_model": {"base_url": "https://x/v1",}}'      # 尾随逗号 = 非法 JSON
        log.log_event("canary.before_bad_body")
        h = self._sandbox("POST", "/api/config", raw,
                          {"Content-Length": str(len(raw)), "Content-Type": "application/json"})

        self.assertEqual(h._status, 400, "坏 JSON 没回 400（body=%r）" % (h._body,))
        self.assertFalse(h._body.get("ok"), "坏 JSON 还回了 ok:true：%r" % (h._body,))
        msg = h._body.get("error") or ""
        self.assertIn("请求体", msg)                 # 说清是**请求体**的问题，别让用户去查配置
        self.assertIn("JSON", msg)
        raw_log = _disk_bytes(log.LOG_FILE).decode("utf-8")
        self.assertIn("canary.before_bad_body", raw_log)   # 反证：日志真的写进去了
        self.assertIn("http.bad_json_body", raw_log)       # 留一条取证线索

    def test_non_utf8_body_is_400_too_not_silently_empty(self):
        """非 UTF-8 的 body 也必须 400（`decode` 与 `json.loads` 是两个不同的失败点，各钉一条）。"""
        raw = b"\xff\xfe\x00\x81"                    # 在 UTF-8 里非法
        h = self._sandbox("POST", "/api/config", raw, {"Content-Length": str(len(raw))})

        self.assertEqual(h._status, 400, "非 UTF-8 body 没回 400（body=%r）" % (h._body,))
        self.assertIn("UTF-8", h._body.get("error") or "")

    def test_oversized_json_body_is_rejected_without_reading_it(self):
        """超大 body 要有上限（与 interview.MAX_UPLOAD_BYTES 同一口径：先看 Content-Length，
        超限**一个字节都不读**，否则一个坏请求就能把内存吃满）。"""
        declared = app.MAX_JSON_BODY_BYTES + 1
        h = self._sandbox("POST", "/api/config", b"", {"Content-Length": str(declared)})

        self.assertEqual(h._status, 413, "超限 body 没被拒（body=%r）" % (h._body,))
        self.assertFalse(h._body.get("ok"))
        self.assertEqual(h.rfile.tell(), 0, "超限请求还是把 body 读了 —— 内存会被吃满")

    def test_empty_body_still_means_no_parameters(self):
        """对照组：没有 body 是**合法**的（很多 POST 不带参数）—— 必须照旧当 `{}` 处理。"""
        log.log_event("canary.before_empty_body")
        h = self._sandbox("POST", "/api/logs/clear", b"", {"Content-Length": "0"})

        self.assertEqual(h._status, 200, "空 body 被误伤了（body=%r）" % (h._body,))
        self.assertTrue(h._body.get("ok"), "空 body 的对照组不该失败：%r" % (h._body,))

    # ---------------- P1-9：模板"存在但读不出来"≠"缺少模板"
    def test_non_utf8_template_says_read_failure_not_missing(self):
        """模板存在但不是 UTF-8 → 文案必须说出**哪个文件 + 读不出来的原因**，绝不说"缺少"。"""
        path = os.path.join(store.p_path("reports"), "坏模板.html")
        with open(path, "wb") as f:
            # ⚠️ 这一串字节**必须真的解不成 UTF-8**（0xFF/0xFE 在 UTF-8 里非法）。
            # 别用 GBK 的中文糊弄：那种字节往往仍是合法 UTF-8，测试会**假绿**。
            f.write(b"<!doctype html><title>\xff\xfe\x00\x81</title>")
        err = self._render_error(path)

        self.assertNotIn("缺少报告模板", err,
                         '把"读不出来"继续报成"缺少"：用户会去建一个已经存在的文件')
        self.assertIn("坏模板.html", err)                 # 说清是哪个文件
        self.assertNotIn("Traceback", err)                # 人话，不是 traceback
        raw_log = _disk_bytes(log.LOG_FILE).decode("utf-8")
        self.assertIn("report.template_unreadable", raw_log)   # 必须有日志

    def test_missing_template_still_says_missing(self):
        """对照组：模板**真不存在**时，那句话必须照旧（两条分支不许混成一句）。"""
        err = self._render_error(os.path.join(store.p_path("reports"), "根本不存在.html"))
        self.assertIn("缺少报告模板", err)
        self.assertIn("根本不存在.html", err)

    def _render_error(self, tpl_path):
        """把模板路径指到 `tpl_path` 后跑一次真 `render_report`，回它抛出的那句话。"""
        real = app.TEMPLATE_PATH
        self.addCleanup(setattr, app, "TEMPLATE_PATH", real)
        app.TEMPLATE_PATH = tpl_path
        with self.assertRaises(RuntimeError) as cm:
            app.render_report({"job_title": "测试岗位", "score": 80.0})
        return str(cm.exception)

    # ---------------- P1-10：SOP 读不出来 ≠ 没有 SOP
    def test_unreadable_sop_is_not_silently_dropped(self):
        """SOP 存在但读不出来 → 必须有一条 warn 日志，且**结果里带着**"本次没加载 SOP"。"""
        fn = _first_readable_sop()
        real_read_text = store.read_text

        def fake(path, default="", *a, **kw):
            if os.path.basename(str(path)) == fn:
                return default
            return real_read_text(path, default, *a, **kw)

        with mock.patch.object(store, "read_text", side_effect=fake):
            prompt, warnings = app.build_system_prompt()

        self.assertTrue(warnings, "SOP 读不出来却一条警告都没有（报告照样出）")
        self.assertTrue(any(fn in w for w in warnings),
                        "警告里没说是哪个 SOP：%r" % (warnings,))
        self.assertIn(fn, prompt)                       # 文件**在**（不是"没有"）
        self.assertIn("读不出来", prompt)
        raw_log = _disk_bytes(log.LOG_FILE).decode("utf-8")
        self.assertIn("sop.unreadable", raw_log)

    def test_sop_warning_reaches_the_analysis_result(self):
        """修 1：警告不能只躺在日志里 —— 这次分析的结果必须带着它（否则用户永远不知道）。"""
        fn = _first_readable_sop()
        real_read_text = store.read_text

        def fake(path, default="", *a, **kw):
            if os.path.basename(str(path)) == fn:
                return default
            return real_read_text(path, default, *a, **kw)

        with mock.patch.object(store, "read_text", side_effect=fake), \
             mock.patch.object(llm, "call_openai_compatible",
                               return_value=json.dumps({"job_title": "x", "score": 80.0})), \
             mock.patch.object(log, "log_event") as fake_log:
            store.write_text(os.path.join(store.p_path("resumes"), "母版.md"), "候选人的简历正文")
            app.do_analyze("岗位职责：标注质检。", None, "母版.md")
            events = [c[0][0] for c in fake_log.call_args_list]
            self.assertIn("sop.unreadable", events)
            kw = [c[1] for c in fake_log.call_args_list if c[0][0] == "sop.unreadable"][0]
            self.assertIn("analysis_sop_warnings", kw)
            self.assertTrue(kw["analysis_sop_warnings"])   # 调用方要能拿到，才能回给前端

    def test_analyze_route_reports_the_sop_warning(self):
        """/api/analyze 的响应体里带着 sop_warnings（用户看得见的那个出口）。"""
        res = self._analyze_with_broken_sop()
        self.assertEqual(res._status, 200, res._body)
        self.assertTrue(res._body.get("ok"), res._body)
        self.assertTrue(res._body.get("sop_warnings"),
                        "响应里没有 sop_warnings：SOP 少了一段，用户永远不知道")

    def _analyze_with_broken_sop(self):
        """让一份 SOP 读不出来，跑一次 /api/analyze（模型/搜索/知识地图全部打桩，不花钱）。"""
        store.write_text(os.path.join(store.p_path("resumes"), "母版.md"), "候选人的简历正文")
        fn = _first_readable_sop()
        real_read_text = store.read_text

        def fake(path, default="", *a, **kw):
            if os.path.basename(str(path)) == fn:
                return default
            return real_read_text(path, default, *a, **kw)

        reply = json.dumps({"job_title": "语音质检", "score": 80.0}, ensure_ascii=False)
        body = json.dumps({"jd_text": "岗位职责：语音数据质检。", "resume_name": "母版.md"},
                          ensure_ascii=False).encode("utf-8")
        with mock.patch.object(store, "read_text", side_effect=fake), \
             mock.patch.object(llm, "call_openai_compatible", return_value=reply), \
             mock.patch.object(app.search_mod, "fetch_link", return_value=""), \
             mock.patch.object(kbreuse, "Reuse", _FakeReuse), \
             mock.patch.object(jdfacts, "resolve", return_value=([], "computed")):
            return self._sandbox("POST", "/api/analyze", body,
                                 {"Content-Length": str(len(body)),
                                  "Content-Type": "application/json"})

    # ---------------- P1-11：/api/logs/clear 失败不许说成功
    def test_logs_clear_failure_is_not_ok_true(self):
        """清空日志失败必须如实（ok:false + error + 一条日志），绝不回 ok:true。"""
        log.log_event("canary.before_failed_clear")
        with mock.patch.object(log, "LOG_FILE", os.path.join(self.tmp, "不存在的目录", "events.jsonl")):
            h = self._sandbox("POST", "/api/logs/clear", b"", {"Content-Length": "0"})

        self.assertFalse(h._body.get("ok"),
                         "清空日志失败了还回 ok:true —— 前端会 toast「日志已清空」，线索白销：%r"
                         % (h._body,))
        self.assertTrue(h._body.get("error"), "失败却连一句 error 都没有：%r" % (h._body,))


class _FakeReuse(object):
    """`kbreuse.Reuse` 的替身：这次分析里它只被用来统计"跑没跑库优先"。"""

    def __init__(self, cfg):
        self.used = False
        self.stats = {}


def _first_readable_sop():
    """挑一份**真的在盘上、真读得出来**的 SOP 当"读不出来"的靶子。

    不硬编码文件名：SOP 清单万一改名，这条测试不该跟着烂。找不到就**响亮失败**
    （静默跳过等于把这条不变量删掉）。
    """
    for fn in app.SOP_FILES:
        p = os.path.join(app.ROOT, fn)
        if os.path.exists(p) and store.read_text(p).strip():
            return fn
    raise AssertionError("盘上一份可读的 SOP 都没有（vault 不完整？）：%r" % (app.SOP_FILES,))


# ============================================================ 批 4「说假话」· 前端（P1-14~P1-18）
# 前端这四条的病根是同一个：**读失败被当成"你没有数据"**。用户看到的每一句话都必须是真话：
#   · 台账读失败 → 绝不显示"共 0 个岗位 / 台账还是空的"，要明说读不到；
#   · 设置读失败 → 绝不留一片空白（那与"从没配过"分不开）；
#   · 清空日志失败 → 绝不 toast「日志已清空」（那是把取证线索说没了）；
#   · 日志读失败 → 绝不显示 undefined / NaN KB；
#   · 报告名为空 → 绝不渲染指向 /reports/undefined 的"真链接"。
#
# 全部**真跑页面 JS**（tests/web_probe 的 Node 垫片）：断言的是**渲染出来的**文字/链接，
# 不是"源码里有那句话" —— 后者在本项目出过假绿（见 web_probe 的说明）。
_FETCH_OK_JS = (
    "fetch = function(){ return Promise.resolve({ok:true, json:function(){"
    "return Promise.resolve(__fixture.resp || {});}}); };\n")


class FrontendReadFailureTest(IsolatedCase):
    """四条前端"读失败"用例。每条都先反证"旧行为的那句假话"真的会出现，再断言它不出现。"""

    # ---------------- P1-14：台账读失败 ≠ 你没有岗位
    def test_ledger_read_failure_is_loud_and_never_says_zero(self):
        _need_node()
        out = web_probe.run_index(
            "fetch = function(){ return Promise.reject(new Error('接口 500')); };\n"
            "await loadJobs();\n"
            "out.count = String($('#jobCount').textContent);\n"
            "out.tip = String($('#jobCntTip').textContent);\n"
            "out.list = String($('#jobList').innerHTML);\n")

        self.assertNotIn("共 0 个岗位", out["tip"],
                         "台账读失败却显示「共 0 个岗位」—— 用户会以为岗位没了")
        self.assertNotIn("台账还是空的", out["list"],
                         "台账读失败却显示「台账还是空的」—— 这是那句假话本身")
        self.assertNotEqual(out["count"].strip(), "0",
                            '读失败还把计数写成 0（与"真的是空的"分不开）')
        blob = out["tip"] + out["list"]
        self.assertIn("接口 500", blob)                 # 如实说出读不出来的原因
        self.assertTrue(("读取失败" in blob) or ("读不到" in blob),
                        "读失败没有一句明确的错误说明：%r" % (blob,))

    def test_an_empty_ledger_is_still_reported_as_empty_not_as_error(self):
        """对照组：**真的是空的**（接口正常回了空列表）必须照旧说"台账还是空的"。

        没有这条，修法很容易变成"一律报错" —— 那又是另一种假话。
        """
        _need_node()
        out = web_probe.run_index(
            _FETCH_OK_JS +
            "await loadJobs();\n"
            "out.count = String($('#jobCount').textContent);\n"
            "out.tip = String($('#jobCntTip').textContent);\n"
            "out.list = String($('#jobList').innerHTML);\n",
            {"resp": {"jds": [], "categories": [], "statuses": []}})

        self.assertIn("台账还是空的", out["list"], "空台账被误报成读失败：%r" % (out,))
        self.assertIn("共 0 个岗位", out["tip"])
        self.assertNotIn("读取失败", out["list"])

    # ---------------- P1-15：设置读失败 ≠ 从没配过
    def test_config_read_failure_says_so_instead_of_blank_page(self):
        _need_node()
        out = web_probe.run_index(
            "fetch = function(){ return Promise.resolve({ok:true, json:function(){"
            "return Promise.resolve({ok:false, error:'配置读不出来（权限）'}); }}); };\n"
            "await loadConfig();\n"
            "out.st = String($('#cfgSt').textContent);\n"
            "out.base = String($('#tBase').value);\n")

        self.assertIn("配置读不出来（权限）", out["st"],
                      '设置页读失败却什么都没说（一片空白 = 与"从没配过"分不开）：%r' % (out,))
        self.assertTrue(out["st"].strip(), "设置页读失败后没有任何提示")

    # ---------------- P1-16：清空日志失败 ≠ 日志已清空
    def test_clear_logs_failure_does_not_claim_success(self):
        _need_node()
        out = web_probe.run_index(
            "fetch = function(){ return Promise.resolve({ok:true, json:function(){"
            "return Promise.resolve({ok:false, error:'日志文件被占用'}); }}); };\n"
            "confirm = function(){ return true; };\n"
            "await clearLogs();\n"
            "out.toast = String($('#toast').textContent);\n"
            "out.list = String($('#logList').innerHTML);\n")

        self.assertNotIn("日志已清空", out["toast"],
                         "清空失败了还 toast「日志已清空」—— 取证线索被一句假话销掉")
        self.assertIn("日志文件被占用", out["toast"] + out["list"])   # 如实说出失败原因

    # ---------------- P1-17：日志读失败 ≠ undefined / NaN KB
    def test_logs_read_failure_shows_no_undefined_or_nan(self):
        _need_node()
        out = web_probe.run_index(
            "fetch = function(){ return Promise.resolve({ok:true, json:function(){"
            "return Promise.resolve({ok:false, error:'日志读不出来'}); }}); };\n"
            "await loadLogs();\n"
            "out.total = String($('#logTotal').textContent);\n"
            "out.meta = String($('#logMeta').innerHTML);\n"
            "out.list = String($('#logList').innerHTML);\n")

        blob = out["total"] + out["meta"] + out["list"]
        self.assertNotIn("undefined", blob, "日志读失败却渲染出 undefined：%r" % (out,))
        self.assertNotIn("NaN", blob, "日志读失败却渲染出 NaN KB：%r" % (out,))
        self.assertIn("日志读不出来", blob)            # 如实说出原因

    # ---------------- P1-18：没有报告名 ≠ 可以点开的报告
    def test_missing_report_name_renders_no_undefined_link(self):
        _need_node()
        out = web_probe.run_index(
            "showResult(__fixture.report);\n"
            "out.html = String($('#result').innerHTML);\n",
            {"report": ""})

        self.assertNotIn("/reports/undefined", out["html"],
                         "报告名为空却渲染了指向 /reports/undefined 的真链接")
        self.assertNotIn("href=\"/reports/\"", out["html"], "渲染了一个空的报告链接")
        self.assertNotIn("✅", out["html"], "没有报告却仍然说「报告已生成」")
        self.assertTrue(out["html"].strip(), "没有报告却什么都没说（用户不知道该干什么）")


# ============================================================ P0-15：提示文案里不许有真实姓名
# `_profile_unreadable_message` 是**给用户看的一段人话**，里面举的"档名"例子曾经写着一个**真名**
# （这台机器上的 profile 名）。分享包在别人机器上会直接把这个名字带出去：`打包分享` 的 PII 扫描
# 依赖一个被 gitignore 的 `.leak-keywords.txt`，干净检出时它不存在 ⇒ 扫描根本拦不住。
# 修法是：例子必须**一眼看出是占位**（`示例-姓名`）。本测试钉住"占位串在、真名不在"。
# ⚠️ 真名只能从 `data/` **只读**出来做比较，**绝不落进这个测试文件**。
class ProfileMessagePlaceholderTest(IsolatedCase):
    PLACEHOLDER = "示例-姓名"

    def test_the_example_profile_name_is_a_placeholder_not_a_real_name(self):
        store.DATA_ROOT = self._real_data_root          # 需要看真实档名文件（只读）
        try:
            real_name = store.current_profile()
        except Exception:
            real_name = ""                              # 读不出来（干净检出）→ 只钉占位串

        msg = app._profile_unreadable_message(RuntimeError("坏掉的档名文件"))

        self.assertIn(self.PLACEHOLDER, msg,
                      "给用户看的档名例子不是一个明显的占位串")
        if real_name and real_name != "default" and real_name != self.PLACEHOLDER:
            self.assertNotIn(real_name, msg,
                             "这段提示里出现了**真实的**档名（= 用户真名）：分享到别人机器上"
                             "会把真名带出去（打包分享的 PII 扫描依赖被 gitignore 的关键词文件，"
                             "干净检出时拦不住）")


if __name__ == "__main__":
    unittest.main()
