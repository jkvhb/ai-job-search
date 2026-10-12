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
import outcome
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
        store.upsert_job(dict({"id": job_id, "job_title": "AI训练标注师",
                               "job_category": "数据标注/质检"}, **kw))

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
        # 落盘了才算真保存：重新读一遍 jobs.json（不信任内存里的返回值）
        job = store.find_job("j1")
        self.assertEqual(job.get("status"), "已拒")                       # 台账顶层镜像
        self.assertEqual(job["application"]["outcome_reason"], "HR 说预算冻结了")
        self.assertTrue(job["application"]["outcome_at"])

    def test_invalid_status_is_400_and_nothing_is_written(self):
        self._job()
        code, body = self._post_json("/api/job/result", {"id": "j1", "status": "胡说", "reason": "x"})
        self.assertEqual(code, 400)
        self.assertFalse(body.get("ok"))
        self.assertEqual(store.find_job("j1").get("status"), None)        # 一个键都没写进去

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
        self.assertEqual(store.find_job("j1")["application"]["outcome_reason"], raw)


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
        job = store.find_job("j1")
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
        self.assertEqual(store.find_job("j1")["application"]["status"], "已拒")

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
        """已有的那条岗位 + 它的 JD 原文（形状照真实记录：id / report / jd_text_file 都在）。"""
        store.upsert_job(dict({"id": job_id, "job_title": "语音/文本评测",
                               "created_at": "2026-10-03 00:30",
                               "report": job_id + ".html",
                               "jd_text_file": job_id + ".txt",
                               "score": 75.0}, **kw))
        store.write_text(store.p_path("jd", job_id + ".txt"), text or self.LONG)

    def _resume(self, name="我的简历.md"):
        store.ensure_profile()
        store.write_text(os.path.join(store.p_path("resumes"), name), "候选人简历正文")
        return name

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
        self._old_job()
        calls = self._patch_analyze_deps()
        code, body = self._post_json("/api/analyze",
                                     {"jd_text": self.LONG, "source_type": "粘贴文本"})
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
        # 结果**写回了那条已有记录**：字段真的更新了（只断言「记录还在」会被平局掩盖）
        self.assertEqual(jobs["j-old"]["report"], "j-old.html")           # 报告文件名 == 原 id
        self.assertEqual(jobs["j-old"]["score"], 91.5)                    # 新分数写上去了
        self.assertEqual(jobs["j-old"]["job_title"], "语音数据质检（改后）")
        self.assertEqual(jobs["j-old"]["created_at"], "2026-10-03 00:30")  # 原始时间不丢
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
        self._old_job(text=self.LONG)
        calls = self._patch_analyze_deps()
        code, body = self._post_json("/api/analyze", {"link": "https://example.com/jd"})
        self.assertEqual(code, 409)
        self.assertEqual(body["duplicate"]["job_id"], "j-old")
        self.assertEqual(calls["search"], 1)          # 抓正文那次已经发生（命中是在拿到文本之后）
        self.assertEqual(calls["llm"], 0)             # 但最贵的模型一次都没调
        self.assertEqual(calls["knowledge"], 0)


class LedgerFilterTest(_AnalyzeRouteCase):
    """§4：`merged_into` 非空的记录只是标记，不该再出现在台账列表里。"""

    def test_merged_jobs_are_hidden_from_the_ledger(self):
        store.upsert_job({"id": "k", "job_title": "岗", "score": 80.0})
        store.upsert_job({"id": "d", "job_title": "岗", "merged_into": "k", "score": 72.0})
        code, body = self._get("/api/jobs")
        self.assertEqual(code, 200)
        self.assertEqual([j["id"] for j in body["jobs"]], ["k"])     # 细到「个体」那一层
        # 反证：过滤不是靠「整个列表被清空」蒙对的
        self.assertNotIn("d", [j["id"] for j in body["jobs"]])

    def test_an_empty_merged_into_string_does_not_hide_a_record(self):
        """手改出的 `merged_into: ""` 是「没合并」，不能被当成合并过的（否则记录会凭空消失）。"""
        store.upsert_job({"id": "k", "job_title": "岗"})
        store.upsert_job({"id": "e", "job_title": "岗", "merged_into": ""})
        code, body = self._get("/api/jobs")
        self.assertEqual(code, 200)
        self.assertEqual(sorted(j["id"] for j in body["jobs"]), ["e", "k"])

    def test_job_update_refuses_a_merged_record(self):
        """台账里看不到的记录，写接口也不能悄悄改它。"""
        store.upsert_job({"id": "k", "job_title": "岗"})
        store.upsert_job({"id": "d", "job_title": "岗", "merged_into": "k"})
        code, body = self._post_json("/api/jobs/update", {"id": "d", "job_title": "改一下"})
        self.assertEqual(code, 404)
        self.assertFalse(body.get("ok"))
        self.assertEqual(store.find_job("d")["job_title"], "岗")     # 盘上确实没被改


if __name__ == "__main__":
    unittest.main()
