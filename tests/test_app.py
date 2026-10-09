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
import unittest

import app
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


if __name__ == "__main__":
    unittest.main()
