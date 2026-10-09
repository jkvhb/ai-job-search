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
import io
import json
import os
import threading
import unittest
from unittest import mock
from urllib.parse import quote

import app
import interview
import kb
import store

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
    (app.Handler.do_GET if method == "GET" else app.Handler.do_POST)(h)
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

    def _no_start(self):
        """把 interview.start 换成空实现，返回记录调用的列表。

        **每个碰上传路由的用例都必须先调它**，哪怕那条路径"按理说走不到 start"：
        实现被改坏时（例如漏了 413 判断）路由会继续往下走并**真起一条流水线线程**；
        那个线程在 addCleanup 还原 store.DATA_ROOT / log.LOG_FILE **之后**才跑完，
        于是它会把 status.json / 日志写进**用户真实的 data 根**（真事故，见汇报）。
        测试里关掉 start 是唯一能挡住这个的结构性做法。
        """
        calls = []
        self._patch(interview, "start", lambda *a, **k: calls.append((a, k)))
        return calls

    def _upload(self, body=b"RIFF....", headers=None):
        """上传一条音频（默认 8 字节），返回 (code, body)。"""
        hd = {"X-Job-Id": "j1", "X-File-Name": "a.m4a"}
        hd.update(headers or {})
        return self._post_binary("/api/interview/upload", body, hd)

    # ---- 上传
    def test_upload_rejects_missing_job(self):
        self._no_start()
        code, _ = self._post_binary("/api/interview/upload", b"x" * 10,
                                    {"X-Job-Id": "", "X-File-Name": "a.wav"})
        self.assertEqual(code, 400)

    def test_upload_rejects_oversize_without_reading_body(self):
        self._no_start()      # 实现被改坏时这条路径会继续往下走 —— 必须挡住真流水线
        # 用一个"声称超大"的 Content-Length（不真传那么多字节）→ 必须 413 且不写文件
        code, _ = self._post_binary("/api/interview/upload", b"x" * 10,
                                    {"X-Job-Id": "j1", "X-File-Name": "a.wav",
                                     "Content-Length": str(interview.MAX_UPLOAD_BYTES + 1)})
        self.assertEqual(code, 413)
        # 反证：不但拒了，还没有留下任何目录/文件（半截上传必须清理干净）
        self.assertFalse(os.path.exists(store.interviews_dir()))

    def test_upload_writes_the_file_and_starts_the_pipeline(self):
        started = []
        self._patch(interview, "start", lambda *a, **k: started.append((a, k)))
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
        self._patch(interview, "start", lambda *a, **k: None)
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
        self._patch(interview, "start", lambda *a, **k: None)
        code, body = self._upload(headers={"X-File-Name": quote("../../evil.wav")})
        self.assertEqual(code, 200)
        iv = body["interview_id"]
        d = store.interview_dir(iv)
        self.assertEqual(sorted(os.listdir(d)), ["audio.wav", "status.json"])
        self.assertFalse(os.path.exists(os.path.join(store.p_path("reports"), "evil.wav")))

    def test_upload_text_kind_skips_transcribing(self):
        """文字稿必须存成 source.txt 并且 kind=text —— run_pipeline 对文字稿读的就是这个名字。"""
        started = []
        self._patch(interview, "start", lambda *a, **k: started.append((a, k)))
        code, body = self._upload(body="面试官：你好".encode("utf-8"),
                                  headers={"X-File-Name": quote("面试记录.md")})
        self.assertEqual(code, 200)
        iv = body["interview_id"]
        self.assertEqual(store.load_interview_json(iv, "status.json")["kind"], "text")
        self.assertEqual(store.read_text(store.interview_file(iv, "source.txt")), "面试官：你好")
        self.assertEqual(started[0][1].get("kind"), "text")     # 传给 start 的 kind 是 text

    def test_upload_honors_x_kind_and_falls_back_for_unknown_extensions(self):
        """X-Kind 优先（规格 §4：audio/text/auto）；扩展名认不出时按音频存成 audio.wav ——
        否则 interview._find_audio 按扩展名找录音会找不到，转写直接报「请重新上传录音」。
        """
        self._no_start()
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

        self._no_start()
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
        interview.start 被替换 → 不起真转写线程、不碰用户真实数据。
        """
        import http.client
        from http.server import ThreadingHTTPServer
        started = []
        self._patch(interview, "start", lambda *a, **k: started.append(a))
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

    def test_report_written_by_the_real_pipeline_uses_the_interview_template(self):
        """真实现写出来的复盘报告（<title> 里带「· 岗位匹配分析 ·」）也必须走访谈模板。

        这条是防假绿的：上面那份是手写的 <title>T</title>，而真报告的标题由
        app.render_report 生成、**确实含「岗位匹配分析」**（复盘复用了 JD 的标题格式）。
        真人拿到的就是这里的文件，所以必须按真文件断言一次。
        """
        interview.render_report({"kind": "interview", "summary": "s", "meta": {"interview_id": "iv1"}},
                                "j1", "iv1")
        path = os.path.join(store.p_path("reports"), "iv_iv1.html")
        self.assertTrue(os.path.exists(path))
        self.assertIn("岗位匹配分析", store.read_text(path))      # 反证：真标题里确实有这段
        code, body = self._get("/reports/iv_iv1.html")
        self.assertEqual(code, 200)
        self.assertIn(INTERVIEW_TEMPLATE_MARKER, body)
        self.assertNotIn(JD_TEMPLATE_MARKER, body)
        self.assertNotIn("__REPORT_DATA__", body)

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


if __name__ == "__main__":
    unittest.main()
