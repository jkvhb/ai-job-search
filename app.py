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
import traceback
import urllib.error
import urllib.request
import uuid
import webbrowser
import zipfile
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote, quote

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(ROOT, "web")
REPORTS = os.path.join(ROOT, "reports")
RESUMES = os.path.join(ROOT, "resumes")
DATA = os.path.join(ROOT, "data")
JD_DIR = os.path.join(DATA, "jd")
CONFIG_PATH = os.path.join(ROOT, "config.json")
JOBS_PATH = os.path.join(DATA, "jobs.json")
TEMPLATE_PATH = os.path.join(WEB, "report_template.html")
DEMO_PATH = os.path.join(WEB, "demo_data.json")
DEMO_ID = "示例_语音文本评测_80分"   # 示例报告固定文件名，避免重复点击产生 (2)(3) 副本
SOP_FILES = ["00-求职SOP总纲.md", "02-预筛与打分.md", "03-JD拆解.md", "04-简历大纲.md", "05-面试准备.md"]

APP_VERSION = "0.2.0"
SESSION_ID = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
LOG_DIR = os.path.join(DATA, "logs")
LOG_FILE = os.path.join(LOG_DIR, "events.jsonl")

PORT = 8000
for _i, _a in enumerate(sys.argv):
    if _a in ("--port", "-p") and _i + 1 < len(sys.argv):
        try:
            PORT = int(sys.argv[_i + 1])
        except ValueError:
            pass

DEFAULT_CONFIG = {
    "text_model": {"label": "DeepSeek（文本分析 / 跑 SOP）", "base_url": "https://api.deepseek.com/v1",
                   "model": "deepseek-flash", "temperature": 0.3, "api_key": ""},
    "vision_model": {"label": "Kimi（识图 OCR / 读 JD 截图）", "base_url": "https://api.moonshot.cn/v1",
                     "model": "kimi-k2.6", "temperature": 1, "api_key": ""},
}

# 岗位基础分类（用于历史列表快速定位）
CATEGORIES = ["数据标注/质检", "AI数据运营", "标注管理", "AI产品经理", "AI训练师", "其他"]
STATUSES = ["未投递", "已投", "待回复", "面试中", "已拒", "Offer", "放弃"]

for _d in (WEB, REPORTS, RESUMES, DATA, JD_DIR, LOG_DIR):
    os.makedirs(_d, exist_ok=True)


# ---------------------------------------------------------------- 基础工具
def read_text(path, default=""):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return default


def write_text(path, content):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        for k in ("text_model", "vision_model"):
            if isinstance(data.get(k), dict):
                cfg[k].update({kk: vv for kk, vv in data[k].items() if vv is not None})
    except Exception:
        pass
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def safe_name(name):
    """只允许纯文件名，防目录穿越"""
    return os.path.basename(str(name or "").strip())


def slug(text, limit=28):
    t = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "", str(text or "")).strip()
    t = re.sub(r"\s+", "", t)
    return (t[:limit] or "岗位")


# ---------------------------------------------------------------- 日志
# 目的：记录用户操作 / 执行过程 / 报错堆栈，最终可打包喂给 AI 做项目优化
KEY_PAT = re.compile(r"(sk-[A-Za-z0-9_\-]{4})[A-Za-z0-9_\-]{4,}")
SENSITIVE_KEYS = ("api_key", "apikey", "key", "authorization", "token", "password", "secret")


def redact(obj, depth=0):
    """递归打码：API key / 密码等敏感字段绝不进日志"""
    if depth > 6:
        return "…"
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if str(k).lower() in SENSITIVE_KEYS:
                out[k] = "***已打码***" if v else ""
            else:
                out[k] = redact(v, depth + 1)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact(x, depth + 1) for x in list(obj)[:50]]
    if isinstance(obj, str):
        return KEY_PAT.sub(r"\1***", obj)[:4000]
    return obj


def log_event(event, level="info", data=None, **kw):
    """追加写一条 JSONL 日志（不覆盖历史，便于长期积累）"""
    d = {}
    if isinstance(data, dict):
        d.update(data)
    d.update(kw)
    rec = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
        "level": level,
        "session": SESSION_ID,
        "event": event,
        "data": redact(d),
    }
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass
    if level in ("error", "warn"):
        try:
            print("  [%s] %s %s" % (level.upper(), event, json.dumps(rec["data"], ensure_ascii=False)[:400]))
        except Exception:
            pass
    return rec


def log_exc(event, exc=None, **kw):
    """记录异常 + 完整堆栈（这是 AI 用来定位 bug 最有价值的部分）"""
    log_event(event, level="error", error=str(exc) if exc else "",
              traceback=traceback.format_exc()[-3000:], **kw)


def read_logs(limit=200):
    """读最近 N 条日志"""
    out = []
    if not os.path.exists(LOG_FILE):
        return out, 0
    try:
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return out, 0
    total = len(lines)
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out, total


# ---------------------------------------------------------------- 岗位台账（历史）
def load_jobs():
    try:
        data = json.loads(read_text(JOBS_PATH, "[]"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_jobs(jobs):
    with open(JOBS_PATH, "w", encoding="utf-8") as f:
        json.dump(jobs, f, ensure_ascii=False, indent=2)


def upsert_job(record):
    """按 id 新增或更新一条岗位记录"""
    jobs = load_jobs()
    for i, j in enumerate(jobs):
        if j.get("id") == record.get("id"):
            j.update(record)
            jobs[i] = j
            save_jobs(jobs)
            return j
    jobs.insert(0, record)
    save_jobs(jobs)
    return record


def find_job(job_id):
    for j in load_jobs():
        if j.get("id") == job_id:
            return j
    return None


# ---------------------------------------------------------------- 模型调用
def call_openai_compatible(base_url, api_key, model, messages, json_mode=False, timeout=240, temperature=None):
    if not api_key:
        raise RuntimeError("未配置 API Key，请到「设置」里填写")
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {"model": model, "messages": messages, "stream": False}
    # 有些新模型（如 kimi-k2.6 / k3）只接受特定 temperature；未配置时不传该字段
    if temperature is not None:
        payload["temperature"] = temperature
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + api_key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        low = detail.lower()
        # 自动降级：模型不支持 response_format 就去掉 json_mode 重试
        if json_mode and ("response_format" in low or "json_object" in low or "not supported" in low):
            return call_openai_compatible(base_url, api_key, model, messages, json_mode=False,
                                          timeout=timeout, temperature=temperature)
        # 自动降级：模型只接受特定 temperature 就不传该字段重试
        if temperature is not None and "temperature" in low:
            return call_openai_compatible(base_url, api_key, model, messages, json_mode=json_mode,
                                          timeout=timeout, temperature=None)
        raise RuntimeError("模型接口返回 %s：%s" % (e.code, detail[:600]))
    except Exception as e:
        raise RuntimeError("调用模型失败：%s" % e)
    try:
        return body["choices"][0]["message"]["content"]
    except Exception:
        raise RuntimeError("模型返回格式异常：%s" % json.dumps(body, ensure_ascii=False)[:500])


def ocr_image(image_data_url, cfg):
    vm = cfg["vision_model"]
    messages = [{
        "role": "user",
        "content": [
            {"type": "image_url", "image_url": {"url": image_data_url}},
            {"type": "text", "text": "请完整、逐字转录这张图片中的所有文字内容，包括公司名、岗位名称、薪资、地点、岗位职责、"
                                     "任职要求、HR 姓名、联系电话、工作地址等所有字段。保持原文，不要总结，不要遗漏任何一行。"
                                     "只输出转录的文字。"},
        ],
    }]
    return call_openai_compatible(vm["base_url"], vm["api_key"], vm["model"], messages, timeout=180,
                                  temperature=vm.get("temperature"))


def fetch_link(url):
    """尝试抓取岗位分享链接的正文。多数招聘站有反爬/需登录，失败时给出明确提示。"""
    if not re.match(r"^https?://", url or ""):
        raise RuntimeError("链接格式不对，需要以 http:// 或 https:// 开头")
    req = urllib.request.Request(url, headers={
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
            html = raw.decode("utf-8", "replace")
    except Exception as e:
        raise RuntimeError("抓取链接失败（招聘站通常有反爬或需登录）：%s\n"
                           "建议：直接上传 JD 截图，或把 JD 文字粘贴进来（链接仍会保存到台账）。" % e)
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", "\n", text)
    text = re.sub(r"&nbsp;?", " ", text)
    text = re.sub(r"\n{2,}", "\n", text).strip()
    low = text.lower()
    if len(text) < 200 or ("登录" in text and "岗位" not in text) or "sign in" in low:
        raise RuntimeError("链接抓回来的内容像登录页/反爬页，没拿到有效 JD。\n"
                           "建议：直接上传 JD 截图，或把 JD 文字粘贴进来（链接仍会保存到台账）。")
    return text[:6000]


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


def repair_json(tm, broken, err):
    """JSON 解析失败时，让模型自己修一遍（比正则修补可靠得多）"""
    msgs = [
        {"role": "system", "content": "你是严格的 JSON 修复器。把用户给的 JSON 修正为语法合法的 JSON，"
                                      "保持原有字段与含义完全不变，只输出修正后的 JSON 本体，"
                                      "不要任何解释、不要 markdown 代码块。"
                                      "关键：字符串内部不能有未转义的英文双引号（改成中文引号「」），不能有裸换行。"},
        {"role": "user", "content": "下面的 JSON 语法有误（%s）。请修正并输出完整、合法的 JSON：\n\n%s"
                                    % (err, broken[:24000])},
    ]
    return call_openai_compatible(tm["base_url"], tm["api_key"], tm["model"], msgs,
                                  json_mode=True, temperature=tm.get("temperature"), timeout=240)


def build_system_prompt():
    parts = []
    for fn in SOP_FILES:
        p = os.path.join(ROOT, fn)
        if os.path.exists(p):
            parts.append("===== %s =====\n%s" % (fn, read_text(p)))
    return ("你是一名资深 AI 领域求职顾问，专精数据标注、AI 数据运营、标注管理、AI 产品经理岗位。\n"
            "下面是你必须遵循的求职 SOP：\n\n" + "\n\n".join(parts) + "\n\n" + SCHEMA_HINT)


def parse_json_reply(text):
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    first_err = "返回内容不是 JSON 对象"
    try:
        obj = json.loads(t)
        if isinstance(obj, dict):
            return obj
        first_err = "返回内容不是 JSON 对象"
    except Exception as e:
        first_err = e
    # 去掉首尾多余内容再试一次（从第一个 { 到最后一个 }）
    i, j = t.find("{"), t.rfind("}")
    if i != -1 and j > i:
        try:
            obj = json.loads(t[i:j + 1])
            if isinstance(obj, dict):
                return obj
        except Exception as e:
            first_err = e
    raise RuntimeError("JSON 解析失败：%s" % first_err)


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
    d["candidate"] = os.path.splitext(safe_name(resume_name))[0]
    d["date"] = datetime.now().strftime("%Y-%m-%d")
    d["source"] = source
    d.setdefault("application", {"status": "未投递", "next_followup": "", "notes": ""})
    # 清理联系信息里的"无/未提供"等占位
    for k in ("company", "hr_name", "phone", "address", "job_category"):
        v = str(d.get(k) or "").strip()
        if v in ("无", "未提供", "未知", "N/A", "n/a", "-", "—", "null", "None"):
            v = ""
        d[k] = v
    if d["job_category"] not in CATEGORIES:
        d["job_category"] = "其他"
    return d


def render_report(d, fixed_id=None):
    tpl = read_text(TEMPLATE_PATH)
    if not tpl:
        raise RuntimeError("缺少报告模板 web/report_template.html")
    job = slug(d.get("job_title") or "岗位")
    score_disp = "%g" % d["score"]
    if fixed_id:
        # 固定文件名（示例报告）：重复生成直接覆盖，不产生 (2)(3) 副本
        fname = fixed_id + ".html"
    else:
        score_file = "%g" % round(d["score"])
        base = "%s_%s_%s分" % (datetime.now().strftime("%Y%m%d"), job, score_file)
        fname = base + ".html"
        n = 2
        while os.path.exists(os.path.join(REPORTS, fname)):
            fname = "%s(%d).html" % (base, n)
            n += 1
    d["report_id"] = fname[:-5]
    title = "%s · 岗位匹配分析 · %s分 · %s" % (d.get("job_title") or "岗位", score_disp, d.get("verdict") or "")
    payload = json.dumps(d, ensure_ascii=False).replace("</", "<\\/")
    html = tpl.replace("__TITLE__", title).replace("__REPORT_DATA__", payload)
    write_text(os.path.join(REPORTS, fname), html)
    return fname


# ---------------------------------------------------------------- 业务
def save_jd_source(job_id, jd_text, image_data_url):
    """把 JD 原始文本/截图落盘，返回可访问的相对文件名"""
    jd_text_file = ""
    jd_image_url = ""
    if jd_text:
        jd_text_file = job_id + ".txt"
        write_text(os.path.join(JD_DIR, jd_text_file), jd_text)
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
            with open(os.path.join(JD_DIR, fn), "wb") as f:
                f.write(raw)
            jd_image_url = fn
        except Exception:
            jd_image_url = ""
    return jd_text_file, jd_image_url


def do_analyze(jd_text, image_data_url, resume_name, link=""):
    t_start = time.time()
    cfg = load_config()
    jd_text = (jd_text or "").strip()
    link = (link or "").strip()

    source_bits = []
    if jd_text:
        source_bits.append("粘贴文本")
    if image_data_url:
        source_bits.append("截图")
    if link:
        source_bits.append("分享链接")

    log_event("analyze.start", resume=resume_name, has_text=bool(jd_text), has_image=bool(image_data_url),
              has_link=bool(link), text_model=cfg["text_model"]["model"],
              vision_model=cfg["vision_model"]["model"])
    try:
        if not jd_text:
            if image_data_url:
                _t = time.time()
                jd_text = ocr_image(image_data_url, cfg)
                source_bits.append("OCR(%s)" % cfg["vision_model"]["model"])
                log_event("analyze.ocr_done", ms=int((time.time() - _t) * 1000), chars=len(jd_text))
            elif link:
                _t = time.time()
                jd_text = fetch_link(link)
                source_bits.append("已抓取正文")
                log_event("analyze.link_fetched", ms=int((time.time() - _t) * 1000), chars=len(jd_text))
            else:
                raise RuntimeError("请提供 JD：粘贴文字、上传截图，或填分享链接（三者任一）")

        rp = os.path.join(RESUMES, safe_name(resume_name))
        if not os.path.exists(rp):
            raise RuntimeError("找不到简历：%s" % resume_name)
        resume = read_text(rp)

        tm = cfg["text_model"]
        messages = [
            {"role": "system", "content": build_system_prompt()},
            {"role": "user", "content": "【候选人简历】\n%s\n\n【岗位 JD】\n%s\n\n"
                                        "请按 SOP 分析并输出 JSON。注意：联系信息（公司/HR/电话/地址）原文没有就留空，不要编造。"
                                        % (resume, jd_text)},
        ]
        _t = time.time()
        reply = call_openai_compatible(tm["base_url"], tm["api_key"], tm["model"], messages, json_mode=True,
                                       temperature=tm.get("temperature"))
        log_event("analyze.model_done", ms=int((time.time() - _t) * 1000), reply_chars=len(reply),
                  model=tm["model"])

        data, last_err, repairs = None, None, 0
        for attempt in range(3):
            try:
                data = parse_json_reply(reply)
                break
            except Exception as e:
                last_err = e
                if attempt >= 2:
                    break
                repairs += 1
                print("  [warn] JSON 解析失败，让模型自动修复（第 %d 次）：%s" % (attempt + 1, e))
                log_event("analyze.json_repair", level="warn", attempt=attempt + 1, error=str(e),
                          reply_head=reply[:300])
                try:
                    reply = repair_json(tm, reply, e)
                except Exception as e2:
                    last_err = e2
                    break
        if data is None:
            raise RuntimeError("模型返回的 JSON 无法解析，自动修复也未成功：%s" % last_err)
        d = normalize(data, resume_name, " + ".join(source_bits) or "文本输入")

        fname = render_report(d)
        job_id = fname[:-5]

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
            "resume": os.path.splitext(safe_name(resume_name))[0],
            "status": (d.get("application") or {}).get("status") or "未投递",
            "notes": (d.get("application") or {}).get("notes") or "",
        }
        upsert_job(rec)
        log_event("analyze.success", ms=int((time.time() - t_start) * 1000), repair_rounds=repairs,
                  report=fname, job_title=rec["job_title"], score=rec["score"],
                  category=rec["job_category"], verdict=rec["verdict"],
                  contact_found=bool(rec["phone"] or rec["hr_name"] or rec["company"]),
                  source=rec["source_type"], jd_chars=len(jd_text))
        return fname
    except Exception as e:
        log_exc("analyze.error", e, ms=int((time.time() - t_start) * 1000), resume=resume_name,
                has_text=bool(jd_text), has_image=bool(image_data_url), has_link=bool(link))
        raise


def do_demo():
    if not os.path.exists(DEMO_PATH):
        raise RuntimeError("缺少示例数据 web/demo_data.json")
    d = json.loads(read_text(DEMO_PATH))
    d.setdefault("candidate", "示例候选人")
    d.setdefault("date", datetime.now().strftime("%Y-%m-%d"))
    d.setdefault("source", "示例数据（本机内置）")
    d.setdefault("application", {"status": "未投递", "next_followup": "", "notes": ""})
    fname = render_report(d, fixed_id=DEMO_ID)
    job_id = fname[:-5]
    upsert_job({
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


LOG_GUIDE = """# 反馈日志包说明

这个压缩包用来把「使用过程 + 报错信息」反馈给 AI，让它优化这个工具。

## 包含内容
- `events.jsonl` —— 全部操作与执行日志，每行一条 JSON（用户操作 / 接口调用 / 模型调用 / 报错堆栈）
- `env.json` —— 运行环境信息（版本、模型名、数据量统计），**不含任何 API Key**
- 本说明文件

## 字段速查
| 字段 | 含义 |
|---|---|
| `ts` | 时间戳（毫秒精度） |
| `level` | info / warn / error |
| `session` | 本次启动的会话 ID，同一窗口内的记录共享同一个 |
| `event` | 事件名，如 `ui.analyze.click`、`analyze.success`、`analyze.error` |
| `data` | 事件细节（已自动打码敏感信息） |

## 重点关注的事件
- `analyze.*` —— 分析全流程（`start` → `ocr_done` → `model_done` → `success` / `error`）
- `ui.*` —— 前端用户操作
- `js_error` / `ui.js_error` —— 前端报错
- `*error` —— 任何带堆栈的失败

## 隐私说明
- API Key 已自动打码为 `sk-xxxx***`。
- 日志**不含简历正文、不含 JD 原文**；可能含岗位名称、公司名、简历文件名。
- 不放心的话，先用记事本打开 `events.jsonl` 删掉不想分享的行再发。

## 怎么用
把整个 zip 发给 AI，并简单说明：你做了什么 → 期望什么 → 实际发生了什么。
"""


def build_log_bundle():
    """日志 + 环境信息（不含 key）打包成 zip，方便反馈给 AI 做优化"""
    os.makedirs(LOG_DIR, exist_ok=True)
    name = "反馈日志包_%s.zip" % datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(LOG_DIR, name)
    cfg = load_config()
    env = {
        "app_version": APP_VERSION,
        "session": SESSION_ID,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "python": sys.version,
        "platform": sys.platform,
        "port": PORT,
        "models": {
            "text": {"base_url": cfg["text_model"]["base_url"], "model": cfg["text_model"]["model"],
                     "temperature": cfg["text_model"].get("temperature"),
                     "api_key_set": bool(cfg["text_model"]["api_key"])},
            "vision": {"base_url": cfg["vision_model"]["base_url"], "model": cfg["vision_model"]["model"],
                       "temperature": cfg["vision_model"].get("temperature"),
                       "api_key_set": bool(cfg["vision_model"]["api_key"])},
        },
        "counts": {
            "resumes": len([f for f in os.listdir(RESUMES) if f.lower().endswith((".md", ".txt"))]),
            "reports": len([f for f in os.listdir(REPORTS) if f.lower().endswith(".html")]),
            "jobs": len(load_jobs()),
        },
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        if os.path.exists(LOG_FILE):
            z.write(LOG_FILE, "events.jsonl")
        z.writestr("env.json", json.dumps(env, ensure_ascii=False, indent=2))
        z.writestr("反馈说明.md", LOG_GUIDE)
    log_event("logs.bundle", file=name, size=os.path.getsize(path))
    return name


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
                log_event("http", method=self.command, path=p, status=code,
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
            return self._send_file(os.path.join(REPORTS, safe_name(unquote(p[len("/reports/"):]))),
                                   "text/html; charset=utf-8")

        if p.startswith("/jd/"):
            name = safe_name(unquote(p[len("/jd/"):]))
            fp = os.path.join(JD_DIR, name)
            ext = os.path.splitext(name)[1].lower()
            ctype = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                     ".webp": "image/webp", ".gif": "image/gif",
                     ".txt": "text/plain; charset=utf-8"}.get(ext, "application/octet-stream")
            return self._send_file(fp, ctype)

        if p == "/api/config":
            return self._send(200, load_config())

        if p == "/api/resumes":
            if "name" in q:
                name = safe_name(q["name"][0])
                fp = os.path.join(RESUMES, name)
                if not os.path.exists(fp):
                    return self._send(404, {"error": "找不到该简历"})
                return self._send(200, {"name": name, "content": read_text(fp)})
            files = sorted(f for f in os.listdir(RESUMES) if f.lower().endswith((".md", ".txt")))
            return self._send(200, {"resumes": files})

        if p == "/api/reports":
            out = []
            for f in os.listdir(REPORTS):
                if not f.lower().endswith(".html"):
                    continue
                fp = os.path.join(REPORTS, f)
                out.append({"name": f,
                            "mtime": datetime.fromtimestamp(os.path.getmtime(fp)).strftime("%Y-%m-%d %H:%M")})
            out.sort(key=lambda x: x["mtime"], reverse=True)
            return self._send(200, {"reports": out})

        if p == "/api/jobs":
            return self._send(200, {"jobs": load_jobs(), "categories": CATEGORIES, "statuses": STATUSES})

        if p == "/api/jd_text":
            job = find_job(safe_name(q.get("id", [""])[0]))
            if not job or not job.get("jd_text_file"):
                return self._send(404, {"error": "没有保存 JD 原文"})
            return self._send(200, {"text": read_text(os.path.join(JD_DIR, job["jd_text_file"]))})

        if p == "/api/logs":
            try:
                n = int(q.get("limit", ["150"])[0])
            except Exception:
                n = 150
            recent, total = read_logs(n)
            size = os.path.getsize(LOG_FILE) if os.path.exists(LOG_FILE) else 0
            return self._send(200, {"version": APP_VERSION, "session": SESSION_ID,
                                    "file": LOG_FILE, "total": total, "size": size,
                                    "recent": recent})

        if p == "/api/logs/download":
            if not os.path.exists(LOG_FILE):
                return self._send(404, {"error": "还没有日志"})
            with open(LOG_FILE, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Disposition",
                             'attachment; filename="ai-job-search-logs-%s.jsonl"' % SESSION_ID)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except Exception:
                pass
            self._after(200)
            return

        if p == "/api/logs/file":
            name = safe_name(q.get("name", [""])[0])
            fp = os.path.join(LOG_DIR, name)
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
            log_exc("http.post_unhandled_error", e, path=p)
            return self._send(500, {"ok": False, "error": "服务器内部错误：%s" % e})

    def _handle_post(self, p):
        body = self._json_body()

        if p == "/api/log":
            ev = str(body.get("event") or "unknown")
            payload = {k: v for k, v in body.items() if k not in ("event", "level")}
            log_event("ui." + ev, level=str(body.get("level") or "info"), data=payload)
            return self._send(200, {"ok": True})

        if p == "/api/logs/bundle":
            try:
                name = build_log_bundle()
                return self._send(200, {"ok": True, "file": name})
            except Exception as e:
                log_exc("logs.bundle_error", e)
                return self._send(200, {"ok": False, "error": str(e)})

        if p == "/api/logs/clear":
            try:
                open(LOG_FILE, "w", encoding="utf-8").close()
            except Exception:
                pass
            log_event("logs.cleared")
            return self._send(200, {"ok": True})

        if p == "/api/config":
            cfg = load_config()
            changed = []
            for k in ("text_model", "vision_model"):
                if isinstance(body.get(k), dict):
                    for kk, vv in body[k].items():
                        if cfg[k].get(kk) != vv:
                            changed.append("%s.%s" % (k, kk))
                    cfg[k].update(body[k])
            save_config(cfg)
            log_event("config.save", changed=changed, text_model=cfg["text_model"]["model"],
                      vision_model=cfg["vision_model"]["model"],
                      text_key_set=bool(cfg["text_model"]["api_key"]),
                      vision_key_set=bool(cfg["vision_model"]["api_key"]))
            return self._send(200, {"ok": True})

        if p == "/api/resumes":
            name = safe_name(body.get("name"))
            if not name:
                return self._send(400, {"ok": False, "error": "文件名不能为空"})
            if not name.lower().endswith((".md", ".txt")):
                name += ".md"
            content = body.get("content") or ""
            write_text(os.path.join(RESUMES, name), content)
            log_event("resume.save", name=name, chars=len(content))
            return self._send(200, {"ok": True, "name": name})

        if p == "/api/jobs/update":
            job = find_job(safe_name(body.get("id")))
            if not job:
                return self._send(404, {"ok": False, "error": "找不到该岗位记录"})
            fields = [k for k in ("company", "hr_name", "phone", "address", "job_category",
                                  "status", "notes", "source_link", "job_title") if k in body]
            for k in fields:
                job[k] = str(body.get(k) or "")
            if job.get("job_category") not in CATEGORIES:
                job["job_category"] = "其他"
            upsert_job(job)
            log_event("job.update", id=job.get("id"), title=job.get("job_title"),
                      fields=fields, status=job.get("status"))
            return self._send(200, {"ok": True, "job": job})

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
                log_event("demo.generate", report=fname)
                return self._send(200, {"ok": True, "report": fname})
            except Exception as e:
                log_exc("demo.error", e)
                return self._send(200, {"ok": False, "error": str(e)})

        return self._send(404, {"error": "not found"})

    # ---- DELETE
    def do_DELETE(self):
        self._t0 = time.time()
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/api/resumes":
            name = safe_name(q.get("name", [""])[0])
            fp = os.path.join(RESUMES, name)
            if name and os.path.exists(fp):
                os.remove(fp)
                log_event("resume.delete", name=name)
                return self._send(200, {"ok": True})
            return self._send(404, {"ok": False, "error": "找不到该简历"})
        if u.path == "/api/jobs":
            job_id = safe_name(q.get("id", [""])[0])
            jobs = load_jobs()
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
                            os.remove(os.path.join(JD_DIR, fn))
                        except Exception:
                            pass
                try:
                    os.remove(os.path.join(REPORTS, hit.get("report") or ""))
                except Exception:
                    pass
            save_jobs(rest)
            log_event("job.delete", id=job_id, title=hit.get("job_title"), keep_files=keep_files)
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
    print("  简历目录：%s" % RESUMES)
    print("  报告目录：%s" % REPORTS)
    print("  台账文件：%s" % JOBS_PATH)
    print("  日志文件：%s" % LOG_FILE)
    print("  按 Ctrl+C 停止")
    print("=" * 52)
    log_event("server.start", version=APP_VERSION, port=PORT, python=sys.version.split()[0],
              log_file=LOG_FILE)
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
        log_event("server.stop", reason="keyboard_interrupt")
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
