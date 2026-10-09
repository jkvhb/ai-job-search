# -*- coding: utf-8 -*-
"""
AI 求职助手 · 本地工具（Python 标准库，零第三方依赖）

用法：双击 启动.bat，或命令行 `python app.py`
默认地址：http://127.0.0.1:8000
可选参数：--port 8001   换端口
          --no-browser  不自动打开浏览器
"""
import base64
import json
import os
import re
import socket
import sys
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote, quote

import kb
import knowledge
import llm
import log
import search as search_mod
import store

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = store.ROOT
WEB = os.path.join(ROOT, "web")
TEMPLATE_PATH = os.path.join(WEB, "report_template.html")
DEMO_PATH = os.path.join(WEB, "demo_data.json")
DEMO_ID = "示例_语音文本评测_80分"   # 示例报告固定文件名，避免重复点击产生 (2)(3) 副本
SOP_FILES = ["00-求职SOP总纲.md", "02-预筛与打分.md", "03-JD拆解.md", "04-简历大纲.md", "05-面试准备.md"]

PORT = 8000
for _i, _a in enumerate(sys.argv):
    if _a in ("--port", "-p") and _i + 1 < len(sys.argv):
        try:
            PORT = int(sys.argv[_i + 1])
        except ValueError:
            pass

# 目录引导：ensure_profile() 建好当前 profile 下的 resumes/reports/jd/logs；
# log.refresh() 让 log.LOG_FILE 指向该 profile 的日志文件
os.makedirs(WEB, exist_ok=True)
store.ensure_profile()
log.refresh()


# ---------------------------------------------------------------- 分析提示词
SCHEMA_HINT = """
只输出一个 JSON 对象（不要 markdown 代码块、不要任何解释文字），结构如下：
{
  "job_title": "岗位名称",
  "company": "招聘公司名（原文有就填，没有留空）",
  "hr_name": "HR/招聘者姓名（原文有就填，没有留空）",
  "phone": "联系电话或微信号（原文有就填，没有留空）",
  "address": "工作地址（尽量具体到区/街道；只知道城市就填城市）",
  "job_category": "岗位基础分类，必须从这些里选一个：数据标注/质检、AI数据运营、标注管理、AI产品经理、AI训练师、其他",
  "location": "城市",
  "salary": "薪资", "education": "学历要求", "experience": "经验要求", "work_type": "用工形式（正式/驻场外包/离场/未标明）",
  "score": 数字0到100,
  "verdict": "建议投递/概率偏低/不建议",
  "verdict_level": "green 或 amber 或 red",
  "hard_gates": [{"name":"门槛名","jd":"JD要求","me":"我的情况","result":"通过/待确认/拦截","level":"ok/warn/bad/info"}],
  "scores": [{"dimension":"维度名","weight":权重数字,"score":得分0到10,"basis":"打分依据"}],
  "score_note": "对打分的补充说明",
  "job_profile": "一句话岗位画像",
  "pain_points": [{"duty":"职责原文","pain":"背后业务痛点","evidence":"候选人可用的证据"}],
  "gaps": [{"skill":"能力要求","current":"候选人现状","verdict":"具备/部分/缺失"}],
  "keywords": ["JD高频关键词"],
  "resume_outline": {"highlights":["重点卖点,含量化证据"],"reorder":["经历重排建议"],"greeting":"BOSS打招呼话术1到2句"},
  "resume_optimization": {
    "add":["建议补写的内容"], "remove":["建议删或缩的内容"], "rewrite":["表述改写方向"], "quantify":["缺数字的地方怎么补"],
    "keyword_map":[{"jd":"JD关键词","resume":"建议在简历里写成"}],
    "section_notes":[{"section":"简历段落","advice":"改写建议"}]
  },
  "interview": {"questions":[{"q":"预测面试题","intent":"考察意图","skeleton":"答案骨架,含关键词"}],"asks":["反问问题"]},
  "risks": ["投递风险提示"],
  "checklist": ["投递前要确认的事项"],
  "application": {"status":"未投递","next_followup":"","notes":""}
}
硬性要求：
- **JSON 语法必须严格合法**：所有字符串内部**禁止出现英文双引号**（需要引用时用中文引号「」或『』），字符串内部禁止裸换行，禁止注释，禁止尾随逗号。
- 联系信息（公司名 / HR 姓名 / 电话 / 微信号 / 地址）**原文没有就留空字符串，严禁编造或猜测**。
- 打分严格对照候选人简历，禁止编造其经历或数据。
- scores 的 6 个维度固定为：团队/供应商管理(权重25)、质量体系能力(22)、标注生产经验(20)、AI 认知(15)、产品/需求能力(10)、行业经验(8)；若岗位类型差异明显可微调权重并在 score_note 说明。
- resume_optimization 具体到"改哪一段、怎么改"，每条控制在 80 字以内，避免输出过长导致 JSON 出错。
- 全部中文输出。
"""


def build_system_prompt():
    parts = []
    for fn in SOP_FILES:
        p = os.path.join(ROOT, fn)
        if os.path.exists(p):
            parts.append("===== %s =====\n%s" % (fn, store.read_text(p)))
    return ("你是一名资深 AI 领域求职顾问，专精数据标注、AI 数据运营、标注管理、AI 产品经理岗位。\n"
            "下面是你必须遵循的求职 SOP：\n\n" + "\n\n".join(parts) + "\n\n" + SCHEMA_HINT)


def normalize(data, resume_name, source):
    d = data if isinstance(data, dict) else {}
    scores = d.get("scores") or []
    total = 0.0
    for s in scores:
        try:
            total += float(s.get("score", 0)) * float(s.get("weight", 0))
        except Exception:
            pass
    if scores and not d.get("score"):
        d["score"] = round(total / 10, 1)
    try:
        d["score"] = round(float(d.get("score") or 0), 1)
    except Exception:
        d["score"] = 0.0
    if not d.get("verdict_level"):
        d["verdict_level"] = "green" if d["score"] >= 60 else ("amber" if d["score"] >= 45 else "red")
    if not d.get("verdict"):
        d["verdict"] = {"green": "建议投递", "amber": "概率偏低", "red": "不建议"}[d["verdict_level"]]
    d["candidate"] = os.path.splitext(store.safe_name(resume_name))[0]
    d["date"] = datetime.now().strftime("%Y-%m-%d")
    d["source"] = source
    d.setdefault("application", {"status": "未投递", "next_followup": "", "notes": ""})
    # 清理联系信息里的"无/未提供"等占位
    for k in ("company", "hr_name", "phone", "address", "job_category"):
        v = str(d.get(k) or "").strip()
        if v in ("无", "未提供", "未知", "N/A", "n/a", "-", "—", "null", "None"):
            v = ""
        d[k] = v
    if d["job_category"] not in store.CATEGORIES:
        d["job_category"] = "其他"
    return d


def _report_title(path):
    """取报告 HTML 里的 <title>…</title>；取不到就退回文件名（与 报告体检.py 同一口径）。"""
    m = re.search(r"<title>(.*?)</title>", store.read_text(path), re.S)
    return m.group(1) if m else os.path.basename(path)


def _render_report_page(path):
    """用**当前模板**重渲染一份已存在的报告；做不到时返回 None。

    为什么必须这样：报告的前端 JS 是内嵌在每份 HTML 里的，改模板只影响之后新生成的报告 ——
    老报告的逻辑永远停在「生成那一天」的版本（「列表视图位置记忆」和「打钩存服务端」
    已经两次踩到同一个坑）。所以 /reports/* 一律动态拼装：
      - 分析数据：仍然取该报告文件里的 REPORT_DATA 快照，一个字都不改
      - 页面骨架 / 前端逻辑：用当前 web/report_template.html

    读不出 REPORT_DATA（旧格式 / 文件被改坏）或模板缺失时返回 None，
    由调用方退回 _send_file 原样发送 —— 报告绝不能因此打不开。
    """
    data = kb.read_report_data(path)
    if not data:
        return None
    tpl = store.read_text(TEMPLATE_PATH)
    if not tpl:
        return None
    # 拼装方式必须与 render_report() 完全一致，否则同一份数据会渲染出两种页面
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return tpl.replace("__TITLE__", _report_title(path)).replace("__REPORT_DATA__", payload)


def render_report(d, fixed_id=None):
    tpl = store.read_text(TEMPLATE_PATH)
    if not tpl:
        raise RuntimeError("缺少报告模板 web/report_template.html")
    job = store.slug(d.get("job_title") or "岗位")
    score_disp = "%g" % d["score"]
    if fixed_id:
        # 固定文件名（示例报告）：重复生成直接覆盖，不产生 (2)(3) 副本
        fname = fixed_id + ".html"
    else:
        score_file = "%g" % round(d["score"])
        base = "%s_%s_%s分" % (datetime.now().strftime("%Y%m%d"), job, score_file)
        fname = base + ".html"
        n = 2
        while os.path.exists(os.path.join(store.p_path("reports"), fname)):
            fname = "%s(%d).html" % (base, n)
            n += 1
    d["report_id"] = fname[:-5]
    title = "%s · 岗位匹配分析 · %s分 · %s" % (d.get("job_title") or "岗位", score_disp, d.get("verdict") or "")
    payload = json.dumps(d, ensure_ascii=False).replace("</", "<\\/")
    html = tpl.replace("__TITLE__", title).replace("__REPORT_DATA__", payload)
    store.write_text(os.path.join(store.p_path("reports"), fname), html)
    return fname


# ---------------------------------------------------------------- 业务
def save_jd_source(job_id, jd_text, image_data_url):
    """把 JD 原始文本/截图落盘，返回可访问的相对文件名"""
    jd_text_file = ""
    jd_image_url = ""
    if jd_text:
        jd_text_file = job_id + ".txt"
        store.write_text(os.path.join(store.p_path("jd"), jd_text_file), jd_text)
    if image_data_url and image_data_url.startswith("data:image"):
        try:
            head, b64 = image_data_url.split(",", 1)
            ext = "png"
            m = re.search(r"image/(\w+)", head)
            if m:
                ext = {"jpeg": "jpg", "jpg": "jpg", "png": "png", "webp": "webp", "gif": "gif"}.get(
                    m.group(1).lower(), "png")
            raw = base64.b64decode(b64)
            fn = "%s.%s" % (job_id, ext)
            with open(os.path.join(store.p_path("jd"), fn), "wb") as f:
                f.write(raw)
            jd_image_url = fn
        except Exception:
            jd_image_url = ""
    return jd_text_file, jd_image_url


def do_analyze(jd_text, image_data_url, resume_name, link=""):
    t_start = time.time()
    cfg = store.load_config()
    jd_text = (jd_text or "").strip()
    link = (link or "").strip()

    source_bits = []
    if jd_text:
        source_bits.append("粘贴文本")
    if image_data_url:
        source_bits.append("截图")
    if link:
        source_bits.append("分享链接")

    log.log_event("analyze.start", resume=resume_name, has_text=bool(jd_text), has_image=bool(image_data_url),
              has_link=bool(link), text_model=cfg["text_model"]["model"],
              vision_model=cfg["vision_model"]["model"])
    try:
        if not jd_text:
            if image_data_url:
                _t = time.time()
                jd_text = llm.ocr_image(image_data_url, cfg)
                source_bits.append("OCR(%s)" % cfg["vision_model"]["model"])
                log.log_event("analyze.ocr_done", ms=int((time.time() - _t) * 1000), chars=len(jd_text))
            elif link:
                _t = time.time()
                jd_text = search_mod.fetch_link(link)
                source_bits.append("已抓取正文")
                log.log_event("analyze.link_fetched", ms=int((time.time() - _t) * 1000), chars=len(jd_text))
            else:
                raise RuntimeError("请提供 JD：粘贴文字、上传截图，或填分享链接（三者任一）")

        rp = os.path.join(store.p_path("resumes"), store.safe_name(resume_name))
        if not os.path.exists(rp):
            raise RuntimeError("找不到简历：%s" % resume_name)
        resume = store.read_text(rp)

        tm = cfg["text_model"]
        messages = [
            {"role": "system", "content": build_system_prompt()},
            {"role": "user", "content": "【候选人简历】\n%s\n\n【岗位 JD】\n%s\n\n"
                                        "请按 SOP 分析并输出 JSON。注意：联系信息（公司/HR/电话/地址）原文没有就留空，不要编造。"
                                        % (resume, jd_text)},
        ]
        _t = time.time()
        reply = llm.call_openai_compatible(tm["base_url"], tm["api_key"], tm["model"], messages, json_mode=True,
                                       temperature=tm.get("temperature"))
        log.log_event("analyze.model_done", ms=int((time.time() - _t) * 1000), reply_chars=len(reply),
                  model=tm["model"])

        data, last_err, repairs = None, None, 0
        for attempt in range(3):
            try:
                data = llm.parse_json_reply(reply)
                break
            except Exception as e:
                last_err = e
                if attempt >= 2:
                    break
                repairs += 1
                print("  [warn] JSON 解析失败，让模型自动修复（第 %d 次）：%s" % (attempt + 1, e))
                log.log_event("analyze.json_repair", level="warn", attempt=attempt + 1, error=str(e),
                          reply_head=reply[:300])
                try:
                    reply = llm.repair_json(tm, reply, e)
                except Exception as e2:
                    last_err = e2
                    break
        if data is None:
            raise RuntimeError("模型返回的 JSON 无法解析，自动修复也未成功：%s" % last_err)
        d = normalize(data, resume_name, " + ".join(source_bits) or "文本输入")

        # ---- 知识学习地图（阶段 1）----
        know_nodes = []
        try:
            _t = time.time()
            know_nodes = knowledge.build_for_jd(jd_text, cfg)
            log.log_event("analyze.knowledge_done", ms=int((time.time() - _t) * 1000),
                          nodes=len(know_nodes))
        except Exception as e:
            # 明确区分「管线失败」与「真的没有知识点」，避免静默变成空模块
            log.log_exc("analyze.knowledge_error", e)
            d["knowledge_error"] = str(e)
        d["knowledge"] = know_nodes

        fname = render_report(d)
        job_id = fname[:-5]

        # ---- 知识库自动沉淀（阶段 3.1）：失败绝不影响出报告 ----
        # kb.absorb 故意不吞 store.save_knowledge 的 I/O 失败（吞掉就等于骗用户「已沉淀」），
        # 所以边界必须在这里：知识库出任何问题都只记日志，报告已经写好了。
        try:
            # 没抽出任何知识点时绝不碰库文件：库文件损坏 + 本次零知识点的组合下，absorb 会
            # 「先隔离坏文件、再写回空库」—— 等于吸收了个寂寞，还把用户已有的积累清空。
            if know_nodes:
                kb.absorb(know_nodes, {
                    "id": job_id,
                    "job_title": d.get("job_title") or "",
                    "company": d.get("company") or "",
                    "date": datetime.now().strftime("%Y-%m-%d"),
                })
        except Exception as e:
            log.log_exc("kb.absorb_error", e)

        # 落盘原始 JD，并写入岗位台账
        jd_text_file, jd_image_url = save_jd_source(job_id, jd_text, image_data_url)
        rec = {
            "id": job_id,
            "report": fname,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "job_title": d.get("job_title") or "",
            "company": d.get("company") or "",
            "hr_name": d.get("hr_name") or "",
            "phone": d.get("phone") or "",
            "address": d.get("address") or "",
            "job_category": d.get("job_category") or "其他",
            "location": d.get("location") or "",
            "salary": d.get("salary") or "",
            "score": d["score"],
            "verdict": d.get("verdict") or "",
            "source_type": " + ".join(source_bits) or "文本输入",
            "source_link": link,
            "jd_text_file": jd_text_file,
            "jd_image": jd_image_url,
            "resume": os.path.splitext(store.safe_name(resume_name))[0],
            "status": (d.get("application") or {}).get("status") or "未投递",
            "notes": (d.get("application") or {}).get("notes") or "",
        }
        store.upsert_job(rec)
        log.log_event("analyze.success", ms=int((time.time() - t_start) * 1000), repair_rounds=repairs,
                  report=fname, job_title=rec["job_title"], score=rec["score"],
                  category=rec["job_category"], verdict=rec["verdict"],
                  contact_found=bool(rec["phone"] or rec["hr_name"] or rec["company"]),
                  source=rec["source_type"], jd_chars=len(jd_text))
        return fname
    except Exception as e:
        log.log_exc("analyze.error", e, ms=int((time.time() - t_start) * 1000), resume=resume_name,
                has_text=bool(jd_text), has_image=bool(image_data_url), has_link=bool(link))
        raise


def do_demo():
    if not os.path.exists(DEMO_PATH):
        raise RuntimeError("缺少示例数据 web/demo_data.json")
    d = json.loads(store.read_text(DEMO_PATH))
    d.setdefault("candidate", "示例候选人")
    d.setdefault("date", datetime.now().strftime("%Y-%m-%d"))
    d.setdefault("source", "示例数据（本机内置）")
    d.setdefault("application", {"status": "未投递", "next_followup": "", "notes": ""})
    fname = render_report(d, fixed_id=DEMO_ID)
    job_id = fname[:-5]
    store.upsert_job({
        "id": job_id, "report": fname,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "job_title": d.get("job_title") or "", "company": d.get("company") or "",
        "hr_name": d.get("hr_name") or "", "phone": d.get("phone") or "",
        "address": d.get("address") or "", "job_category": d.get("job_category") or "数据标注/质检",
        "location": d.get("location") or "", "salary": d.get("salary") or "",
        "score": d.get("score") or 0, "verdict": d.get("verdict") or "",
        "source_type": "示例数据", "source_link": "",
        "jd_text_file": "", "jd_image": "", "resume": "示例简历",
        "status": "未投递", "notes": "示例数据，可删除",
    })
    return fname


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "AIJobSearch/2.0"

    def log_message(self, fmt, *args):
        pass

    def _after(self, code):
        """请求结束记一条日志（只记 API 与首页，静态资源不记以免噪音）"""
        try:
            p = urlparse(self.path).path
            if p.startswith("/api/log"):
                return
            if p.startswith("/api/") or p in ("/", "/index.html"):
                log.log_event("http", method=self.command, path=p, status=code,
                          ms=int((time.time() - getattr(self, "_t0", time.time())) * 1000))
        except Exception:
            pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", raw=False):
        data = body if raw else json.dumps(body, ensure_ascii=False).encode("utf-8")
        if isinstance(data, str):
            data = data.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except Exception:
            pass
        self._after(code)

    def _json_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except Exception:
            return {}

    def _send_file(self, path, ctype):
        if not os.path.exists(path) or not os.path.isfile(path):
            return self._send(404, {"error": "not found"})
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except Exception:
            pass
        self._after(200)

    # ---- GET
    def do_GET(self):
        self._t0 = time.time()
        u = urlparse(self.path)
        p, q = u.path, parse_qs(u.query)

        if p in ("/", "/index.html"):
            return self._send_file(os.path.join(WEB, "index.html"), "text/html; charset=utf-8")

        if p.startswith("/reports/"):
            fp = os.path.join(store.p_path("reports"), store.safe_name(unquote(p[len("/reports/"):])))
            # 老报告也要吃到最新模板：REPORT_DATA 取文件里的快照，骨架/前端逻辑用当前模板。
            # 读不出快照、模板缺失、或拼装时出任何意外 → 一律退回原文件原样发送，绝不 5xx。
            try:
                html = _render_report_page(fp)
            except Exception:
                html = None
            if html is not None:
                return self._send(200, html, "text/html; charset=utf-8", raw=True)
            return self._send_file(fp, "text/html; charset=utf-8")

        if p.startswith("/jd/"):
            name = store.safe_name(unquote(p[len("/jd/"):]))
            fp = os.path.join(store.p_path("jd"), name)
            ext = os.path.splitext(name)[1].lower()
            ctype = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                     ".webp": "image/webp", ".gif": "image/gif",
                     ".txt": "text/plain; charset=utf-8"}.get(ext, "application/octet-stream")
            return self._send_file(fp, ctype)

        if p == "/api/config":
            return self._send(200, store.load_config())

        if p == "/api/resumes":
            if "name" in q:
                name = store.safe_name(q["name"][0])
                fp = os.path.join(store.p_path("resumes"), name)
                if not os.path.exists(fp):
                    return self._send(404, {"error": "找不到该简历"})
                return self._send(200, {"name": name, "content": store.read_text(fp)})
            files = sorted(f for f in (os.listdir(store.p_path("resumes")) if os.path.isdir(store.p_path("resumes")) else []) if f.lower().endswith((".md", ".txt")))
            return self._send(200, {"resumes": files})

        if p == "/api/reports":
            out = []
            for f in (os.listdir(store.p_path("reports")) if os.path.isdir(store.p_path("reports")) else []):
                if not f.lower().endswith(".html"):
                    continue
                fp = os.path.join(store.p_path("reports"), f)
                out.append({"name": f,
                            "mtime": datetime.fromtimestamp(os.path.getmtime(fp)).strftime("%Y-%m-%d %H:%M")})
            out.sort(key=lambda x: x["mtime"], reverse=True)
            return self._send(200, {"reports": out})

        if p == "/api/jobs":
            return self._send(200, {"jobs": store.load_jobs(), "categories": store.CATEGORIES, "statuses": store.STATUSES})

        if p == "/api/knowledge":
            def one(k, default=""):
                v = q.get(k)
                return v[0] if v else default
            try:
                data = store.load_knowledge()
                return self._send(200, {
                    "ok": True,
                    "nodes": kb.list_nodes(data, q=one("q"), state=one("state"),
                                           category=one("category"), jd_id=one("jd"),
                                           sort=one("sort") or "mentions"),
                    "stats": kb.stats(data),
                    "duplicates": kb.find_duplicates(data),
                    "jds": kb.all_jds(data),
                    "categories": kb.all_categories(data),
                    "states": list(kb.STATE_VALUES),
                })
            except Exception as e:
                # do_POST 有外层 try，do_GET 没有：手改坏的 knowledge.json 曾让这里抛 TypeError，
                # 服务器直接关连接 —— 客户端拿到的是 RemoteDisconnected，连 500 都没有。
                # 兜底成结构化错误，前端才能显示「知识库读取失败」而不是静默白屏。
                log.log_exc("kb.api_error", e)
                return self._send(500, {"ok": False, "error": "知识库读取失败：%s" % e})

        if p == "/api/knowledge/review":
            try:
                try:
                    limit = int(q.get("limit", ["20"])[0])
                except Exception:
                    limit = 20
                limit = max(1, min(limit, 200))
                data = store.load_knowledge()
                queue = kb.review_queue(data, limit=limit)
                today = datetime.now().strftime("%Y-%m-%d")
                nodes = kb.ensure_kb(data)["nodes"]
                return self._send(200, {
                    "ok": True, "queue": queue,
                    "stats": {"due": len(queue),
                              "mastered": sum(1 for n in nodes if n.get("state") == "已掌握"),
                              "reviewed_today": sum(1 for n in nodes
                                                    if n.get("last_reviewed_at") == today)},
                })
            except Exception as e:
                # do_GET 没有外层 try（见上面 /api/knowledge 的注释）：不兜底就会裸断连接
                log.log_exc("kb.review_api_error", e)
                return self._send(500, {"ok": False, "error": "复习队列读取失败：%s" % e})

        if p == "/api/jd_text":
            job = store.find_job(store.safe_name(q.get("id", [""])[0]))
            if not job or not job.get("jd_text_file"):
                return self._send(404, {"error": "没有保存 JD 原文"})
            return self._send(200, {"text": store.read_text(os.path.join(store.p_path("jd"), job["jd_text_file"]))})

        if p == "/api/logs":
            try:
                n = int(q.get("limit", ["150"])[0])
            except Exception:
                n = 150
            recent, total = log.read_logs(n)
            size = os.path.getsize(log.LOG_FILE) if os.path.exists(log.LOG_FILE) else 0
            return self._send(200, {"version": log.APP_VERSION, "session": log.SESSION_ID,
                                    "file": log.LOG_FILE, "total": total, "size": size,
                                    "recent": recent})

        if p == "/api/logs/download":
            if not os.path.exists(log.LOG_FILE):
                return self._send(404, {"error": "还没有日志"})
            with open(log.LOG_FILE, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Disposition",
                             'attachment; filename="ai-job-search-logs-%s.jsonl"' % log.SESSION_ID)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except Exception:
                pass
            self._after(200)
            return

        if p == "/api/logs/file":
            name = store.safe_name(q.get("name", [""])[0])
            fp = os.path.join(os.path.dirname(log.LOG_FILE), name)
            if not name or not os.path.exists(fp) or not os.path.isfile(fp):
                return self._send(404, {"error": "找不到该文件"})
            with open(fp, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition",
                             "attachment; filename=\"logs.zip\"; filename*=UTF-8''%s" % quote(name))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except Exception:
                pass
            self._after(200)
            return

        return self._send(404, {"error": "not found"})
    # ---- POST
    def do_POST(self):
        self._t0 = time.time()
        p = urlparse(self.path).path
        try:
            return self._handle_post(p)
        except Exception as e:
            log.log_exc("http.post_unhandled_error", e, path=p)
            return self._send(500, {"ok": False, "error": "服务器内部错误：%s" % e})

    def _handle_post(self, p):
        body = self._json_body()

        if p == "/api/log":
            ev = str(body.get("event") or "unknown")
            payload = {k: v for k, v in body.items() if k not in ("event", "level")}
            log.log_event("ui." + ev, level=str(body.get("level") or "info"), data=payload)
            return self._send(200, {"ok": True})

        if p == "/api/logs/bundle":
            try:
                name = log.build_log_bundle(PORT)
                return self._send(200, {"ok": True, "file": name})
            except Exception as e:
                log.log_exc("logs.bundle_error", e)
                return self._send(200, {"ok": False, "error": str(e)})

        if p == "/api/logs/clear":
            try:
                open(log.LOG_FILE, "w", encoding="utf-8").close()
            except Exception:
                pass
            log.log_event("logs.cleared")
            return self._send(200, {"ok": True})

        if p == "/api/config":
            cfg = store.load_config()
            changed = []
            for k in ("text_model", "vision_model"):
                if isinstance(body.get(k), dict):
                    for kk, vv in body[k].items():
                        if cfg[k].get(kk) != vv:
                            changed.append("%s.%s" % (k, kk))
                    cfg[k].update(body[k])
            if isinstance(body.get("search_providers"), list):
                cfg["search_providers"] = body["search_providers"]
                changed.append("search_providers")
            store.save_config(cfg)
            log.log_event("config.save", changed=changed, text_model=cfg["text_model"]["model"],
                          vision_model=cfg["vision_model"]["model"],
                          text_key_set=bool(cfg["text_model"]["api_key"]),
                          vision_key_set=bool(cfg["vision_model"]["api_key"]),
                          search_count=len(cfg.get("search_providers") or []))
            return self._send(200, {"ok": True})

        if p == "/api/resumes":
            name = store.safe_name(body.get("name"))
            if not name:
                return self._send(400, {"ok": False, "error": "文件名不能为空"})
            if not name.lower().endswith((".md", ".txt")):
                name += ".md"
            content = body.get("content") or ""
            store.write_text(os.path.join(store.p_path("resumes"), name), content)
            log.log_event("resume.save", name=name, chars=len(content))
            return self._send(200, {"ok": True, "name": name})

        if p == "/api/jobs/update":
            job = store.find_job(store.safe_name(body.get("id")))
            if not job:
                return self._send(404, {"ok": False, "error": "找不到该岗位记录"})
            fields = [k for k in ("company", "hr_name", "phone", "address", "job_category",
                                  "status", "notes", "source_link", "job_title") if k in body]
            for k in fields:
                job[k] = str(body.get(k) or "")
            if job.get("job_category") not in store.CATEGORIES:
                job["job_category"] = "其他"
            store.upsert_job(job)
            log.log_event("job.update", id=job.get("id"), title=job.get("job_title"),
                      fields=fields, status=job.get("status"))
            return self._send(200, {"ok": True, "job": job})

        if p == "/api/knowledge/state":
            ok, msg = kb.set_state(str(body.get("id") or ""), str(body.get("state") or ""))
            return self._send(200 if ok else 400, {"ok": ok, "error": msg})

        if p == "/api/knowledge/review":
            ok, node = kb.record_review(str(body.get("id") or ""), str(body.get("result") or ""))
            if not ok:
                return self._send(400, {"ok": False, "error": "记录失败：知识点或结果不合法"})
            return self._send(200, {"ok": True, "node": node})

        if p == "/api/report_state/read":
            rid = store.safe_name(body.get("report_id"))
            if not rid:
                return self._send(400, {"ok": False, "error": "报告 id 不合法"})
            terms = body.get("terms") if isinstance(body.get("terms"), list) else []
            index = {n.get("id"): n for n in kb.ensure_kb(store.load_knowledge())["nodes"] if n.get("id")}
            know = {}
            for t in terms:
                # 归一化只有 kb.normalize_id 一份实现：路由里再写一套迟早会和知识库分叉
                nid = kb.normalize_id(t)
                if not nid:
                    continue
                card = index.get(nid)
                know[str(t)] = str(card.get("state")) if card else kb.DEFAULT_STATE
            return self._send(200, {"ok": True,
                                    "checks": store.load_report_state(rid)["checks"],
                                    "know": know})

        if p == "/api/report_state/write":
            rid = store.safe_name(body.get("report_id"))
            if not rid:
                return self._send(400, {"ok": False, "error": "报告 id 不合法"})
            checks = body.get("checks") if isinstance(body.get("checks"), dict) else {}
            know = body.get("know") if isinstance(body.get("know"), dict) else {}
            saved = created = 0
            for k, v in checks.items():
                st = store.load_report_state(rid)
                st["checks"][str(k)] = bool(v)
                if store.save_report_state(rid, st["checks"]):
                    saved += 1
            for term, val in know.items():
                if isinstance(val, dict):
                    state = str(val.get("state") or "")
                    meta = val
                else:
                    state = str(val or "")
                    meta = None
                ok, was_created, _ = kb.upsert_check(term, state, meta)
                if ok:
                    saved += 1
                    created += 1 if was_created else 0
            log.log_event("report_state.write", report=rid, saved=saved, created=created)
            return self._send(200, {"ok": True, "saved": saved, "created": created})

        if p == "/api/knowledge/merge":
            ok, msg = kb.merge_nodes(str(body.get("keep_id") or ""), str(body.get("drop_id") or ""))
            return self._send(200 if ok else 400, {"ok": ok, "error": msg, "message": msg})

        if p == "/api/knowledge/import-reports":
            try:
                files, added, merged = kb.absorb_reports()
                return self._send(200, {"ok": True, "files": files,
                                        "added": added, "merged": merged})
            except Exception as e:
                log.log_exc("kb.import_error", e)
                return self._send(200, {"ok": False, "error": str(e)})

        if p == "/api/analyze":
            try:
                fname = do_analyze(body.get("jd_text"), body.get("image"),
                                   body.get("resume_name"), body.get("link"))
                return self._send(200, {"ok": True, "report": fname})
            except Exception as e:
                return self._send(200, {"ok": False, "error": str(e)})

        if p == "/api/demo":
            try:
                fname = do_demo()
                log.log_event("demo.generate", report=fname)
                return self._send(200, {"ok": True, "report": fname})
            except Exception as e:
                log.log_exc("demo.error", e)
                return self._send(200, {"ok": False, "error": str(e)})

        return self._send(404, {"error": "not found"})

    # ---- DELETE
    def do_DELETE(self):
        self._t0 = time.time()
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/api/resumes":
            name = store.safe_name(q.get("name", [""])[0])
            fp = os.path.join(store.p_path("resumes"), name)
            if name and os.path.exists(fp):
                os.remove(fp)
                log.log_event("resume.delete", name=name)
                return self._send(200, {"ok": True})
            return self._send(404, {"ok": False, "error": "找不到该简历"})
        if u.path == "/api/jobs":
            job_id = store.safe_name(q.get("id", [""])[0])
            jobs = store.load_jobs()
            hit = None
            rest = []
            for j in jobs:
                if j.get("id") == job_id:
                    hit = j
                else:
                    rest.append(j)
            if not hit:
                return self._send(404, {"ok": False, "error": "找不到该岗位记录"})
            keep_files = q.get("keep_files", ["0"])[0] == "1"
            if not keep_files:
                for key in ("jd_image", "jd_text_file"):
                    fn = hit.get(key)
                    if fn:
                        try:
                            os.remove(os.path.join(store.p_path("jd"), fn))
                        except Exception:
                            pass
                try:
                    os.remove(os.path.join(store.p_path("reports"), hit.get("report") or ""))
                except Exception:
                    pass
            store.save_jobs(rest)
            log.log_event("job.delete", id=job_id, title=hit.get("job_title"), keep_files=keep_files)
            return self._send(200, {"ok": True})
        return self._send(404, {"error": "not found"})


def port_in_use(port):
    """主动探测端口：Windows 的 SO_REUSEADDR 允许同端口重复绑定，不探测会静默双开"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.6)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def main():
    if port_in_use(PORT):
        print("")
        print("[!] 端口 %d 已经被占用 —— 多半是你已经开着一个工具窗口了。" % PORT)
        print("    先关掉之前那个窗口（或按 Ctrl+C），再重新双击启动。")
        print("    想同时开第二个：python app.py --port 8001")
        print("")
        return
    url = "http://127.0.0.1:%d" % PORT
    print("=" * 52)
    print("  AI 求职助手已启动")
    print("  地址：%s" % url)
    print("  简历目录：%s" % store.p_path("resumes"))
    print("  报告目录：%s" % store.p_path("reports"))
    print("  台账文件：%s" % store.jobs_path())
    print("  日志文件：%s" % log.LOG_FILE)
    print("  按 Ctrl+C 停止")
    print("=" * 52)
    log.log_event("server.start", version=log.APP_VERSION, port=PORT, python=sys.version.split()[0],
              log_file=log.LOG_FILE)
    if "--no-browser" not in sys.argv:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as e:
        print("")
        print("[!] 启动失败：端口 %d 已被占用。" % PORT)
        print("    多半是你已经开着一个工具窗口了 —— 先关掉它，再重新启动。")
        print("    或者换个端口：python app.py --port 8001")
        print("    原始错误：%s" % e)
        return
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
        log.log_event("server.stop", reason="keyboard_interrupt")
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
