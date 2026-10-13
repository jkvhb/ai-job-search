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
import jdid
import kb
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


def _need_node():
    if web_probe.NODE is None:
        raise unittest.SkipTest("本机没有 Node，跳过「真跑页面 JS」的渲染探针")


def _resumes_view(include_removed=True):
    """`/api/resumes` 的真实响应（用户看到的就是这份数据：`resumes` / `files` / `unregistered`）。"""
    path = "/api/resumes?include_removed=1" if include_removed else "/api/resumes"
    h = _sandbox_handler("GET", path, b"", {})
    return h._status, h._body


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
        """修 3：母版原文为 `missing` / `mismatch` 时 `create_version` **故意不写快照**
        （`resumelib.py:369-384`）→「覆盖前先把母版原文留成快照」这句话并不总成立。"""
        self._family()
        code, view = _resumes_view()
        self.assertEqual(code, 200, view)
        body = _continue_modal(view, "族谱")["body"]
        self.assertNotIn("覆盖前先把母版原文留成快照", body,
                         "弹窗又在无条件承诺留快照（母版原文不可用时并不写快照）")
        self.assertIn("母版原文可用", body, "没有说清「什么时候才留快照」")
        self.assertIn("不写快照", body, "没有说清原文不可用时会怎样（只留变更说明）")

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


if __name__ == "__main__":
    unittest.main()
