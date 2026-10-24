# -*- coding: utf-8 -*-
"""
AI 求职助手 · 本地工具（Python 标准库，零第三方依赖）

用法：双击 启动.bat，或命令行 `python app.py`
默认地址：http://127.0.0.1:8000
可选参数：--port 8001   换端口
          --no-browser  不自动打开浏览器
"""
import base64
import contextlib
import inspect
import json
import os
import re
import socket
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote, quote

import jdfacts
import jddelete
import jdid
import jobdup
import kb
import kbreuse
import knowledge
import interview
import llm
import log
import outcome
import pdftext
import resumelib
import search as search_mod
import store

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# ------------------------------------------------------------ 启动前置检查（档名文件）
# 档名文件（`data/current_profile.txt`）**存在却读不出来**时（记事本按 ANSI/GBK 存过中文档名、
# 权限/IO 问题…）：印一行人话 + 非零退出。两个"不做"：
#   - **不裸抛**：用户双击 启动.bat，看到的是一坨 traceback，不知道要修哪个文件；
#   - **不回退 default**：那正是刚修掉的**静默串档** —— 用户以为在「张三」下面工作，
#     之后每一次写入（分析/报告/配置/知识/日志）都落进 default，界面上完全看不出来，且不可逆。
#
# 退出码取 `2`：`1` 在 Windows 上被强杀的进程也是 1（容易和"跑起来之后出错"混淆），
# 而 2 是命令行工具对「启动前置条件不满足、什么都没做」的惯例（argparse 的用法错就是 2）。
PROFILE_UNREADABLE_EXIT = 2


def profile_precondition_error():
    """启动前检查当前档名读不读得出来：没问题返回 None，读不出来返回给用户看的多行说明。

    单独抽成函数是为了能**直接单测**（不必起服务、不必跑子进程），
    并且让 import 期与 `main()` 两个入口说**同一句话**。

    ⚠️ 只接 `store.ProfileUnreadable`：别的异常原样冒出去。
    顺手写成 `except Exception` 把真 bug 也包成这句人话，等于把 traceback 藏起来
    —— 用户看到「档名文件坏了」而真实原因（代码/IO 缺陷）永远查不到。
    """
    try:
        store.current_profile()
    except store.ProfileUnreadable as e:
        return _profile_unreadable_message(e)
    return None


def _profile_unreadable_message(exc):
    """把「档名读不出来」写成三句话说清的人话：哪个文件 / 为什么不能继续 / 怎么修。"""
    # 底层原因（OS 级的那个）：store.current_profile() 是在 except 块里 raise 的，
    # 原始异常被自动挂成 __context__；取不到就退回异常本身的文字（信息只多不少）。
    cause = exc.__context__ or exc
    return ("[!] 启动中止：当前档名文件读不出来 —— 不启动，也不替你猜一个档。\n"
            "    哪个文件：%s\n"
            "    读不出来的原因：%s\n"
            "    为什么不能继续：宁可不启动，也不把你的数据写进另一个档。继续跑的话，"
            "之后的每一次写入（分析、报告、配置、知识、日志）都会落进 default，"
            "界面上看不出来，事后也分不清哪些是串过去的。\n"
            "    怎么修：把它改成一行 UTF-8 的档名（例如 示例-姓名），"
            "或者删掉它（删掉 = 回到 default）。\n"
            % (store.current_profile_file(), cause))


def _abort_startup(message):
    """印人话到 stderr，然后**非零退出**（绝不把 traceback 丢给用户）"""
    sys.stderr.write(message)
    sys.stderr.flush()
    raise SystemExit(PROFILE_UNREADABLE_EXIT)


# import 期就要拦：下面的 log.refresh() 会解析档名路径，裸抛的话用户看到的还是 traceback。
# （log 模块自身的 import 期解析不抛、只把错误留档 —— 见 log._initial_log_file()：
#  它是所有模块的公共依赖，它一抛，这里的校验根本轮不到执行。）
_PROFILE_ERROR = profile_precondition_error()
if _PROFILE_ERROR:
    _abort_startup(_PROFILE_ERROR)


ROOT = store.ROOT
WEB = os.path.join(ROOT, "web")
TEMPLATE_PATH = os.path.join(WEB, "report_template.html")
INTERVIEW_TEMPLATE_PATH = os.path.join(WEB, "interview_template.html")   # 面试复盘报告版式
OUTCOME_TEMPLATE_PATH = os.path.join(WEB, "outcome_template.html")       # 结果复盘报告版式
DEMO_PATH = os.path.join(WEB, "demo_data.json")
DEMO_ID = "示例_语音文本评测_80分"   # 示例报告固定文件名，避免重复点击产生 (2)(3) 副本
SOP_FILES = ["00-求职SOP总纲.md", "02-预筛与打分.md", "03-JD拆解.md", "04-简历大纲.md", "05-面试准备.md"]

# JSON 请求体的上限（P1-13）。理由：本工具**唯一**的 POST body 是分析参数（JD 原文 / base64
# 截图 / 简历名）；`_json_body` 会把整个 body 读进内存，所以没有一个上限的话，一个坏请求
# （或本机服务被别的网页打）就能把内存吃满、让服务卡死。
# 口径与 `interview.MAX_UPLOAD_BYTES`（音频上传 500MB）一致：**先看 Content-Length，超限一个
# 字节都不读**。
#
# ⚠️ **8 MiB 是实测定的，不是拍的**（旧值 2 MiB 会挡下**正常**截图）：
#   - 本仓真实夹具 `data/profiles/default/jd/*.jpg` 最大 **923258 字节**（三张都是它）；
#   - 截图以 base64 data URL 进 JSON body ⇒ 923258 → **≈1231012 字节**（4/3 加引号与字段名）；
#   - 那已经是旧上限 2 MiB 的 **58.7%**，**余量只有 1.70×** —— 一张 ≥1.7MB 的**正常**截图
#     就会被 413 挡下，还会被告知"请检查是不是发错了内容"（把正常用户当坏人）。
# 8 MiB 的依据：4 MB 的截图 base64 后 ≈5.4 MB，8 MiB 留出 ≈1.5× 余量；
# 再大就该压缩了（对恶意请求而言 8 MiB 也仍然是个硬上限：超限**一个字节都不读**）。
MAX_JSON_BODY_BYTES = 8 * 1024 * 1024

#: 简历 PDF 的大小上限。base64 之后要 ×4/3：5 MB 的 PDF ≈ 6.7 MB 请求体，仍在上面那条 8 MiB 之内。
#: 前端也会在本地先挡一次（省一次上传），但**判据不能只在前端**（前端可以被绕过）。
PDF_MAX_BYTES = 5 * 1024 * 1024


def _size_text(n):
    """把字节数写成给人看的字符串（≥1 MB 用 MB 一位小数，否则 KB）。"""
    if n >= 1024 * 1024:
        return "%.1f MB" % (n / 1024.0 / 1024.0)
    return "%.0f KB" % (n / 1024.0)

# `Handler._json_body()` 的返回值之一：**已经自己回过 400/413** 了（body 读不出来 / 太大）。
# 用哨兵而不是"抛异常"或"返回 None"：路由里那条 `body.get(...)` 必须**不可能**被误当成
# "没有参数"（P1-13 的病根就是"读不出来"与"没有参数"共用一个 `{}`）。
_NO_BODY_SENTINEL = object()


class TemplateUnreadable(RuntimeError):
    """模板文件**在**、却读不出来（非 UTF-8 / 权限 / 被独占）。

    ⚠️ 单独一个类型是必须的（P1-9）：旧实现把"读不出来"和"没有模板"混成同一句
    「缺少报告模板」—— 用户会去**新建一个已经存在的**文件，而真正的原因（编码/权限）
    一个字都没说。上游（`/reports/*`）据此决定「退回原文件原样发送」。
    """


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
    """把 5 份 SOP 拼成 system prompt。返回 `(prompt, warnings)`。

    ⚠️ **返回值为什么是二元组（P1-10）**：旧实现对"文件在、但读不出来"（非 UTF-8 / 被独占 /
    权限）静默 `append("")` —— system prompt 少一整段 SOP，报告照出，用户永远不知道。
    现在把每一处读不出来的**如实写进 prompt 本体**（模型也知道这段 SOP 缺失），
    **并且**收集到 `warnings` 里带回调用方（`do_analyze` 会把它们落到日志与响应上）。

    两条分支绝不混："**文件不在**（真的没有这份 SOP）" 与 "**文件在、读不出来**" 是两回事：
    前者只提一句"未找到"，后者必须说出文件名与原因。绝不说成同一句假话。
    """
    parts, warnings = [], []
    for fn in SOP_FILES:
        p = os.path.join(ROOT, fn)
        if not os.path.exists(p):
            parts.append("===== %s（未找到该文件）=====" % fn)
            continue
        text = store.read_text(p, None)
        if text is not None:
            parts.append("===== %s =====\n%s" % (fn, text))
            continue
        # 文件**在**却读不出来：这是"本次分析没有 SOP"级别的降级，必须响亮
        why = _unreadable_reason(p)
        warn = "本次分析没能加载 SOP《%s》：%s" % (fn, why)
        warnings.append(warn)
        parts.append("===== %s（读不出来：%s）=====\n【这段 SOP 本次没有加载成功，"
                     "你的分析是在缺少这一段规范的情况下做出的 —— 请在结论里如实提醒用户。】"
                     % (fn, why))
        try:
            log.log_event("sop.unreadable", level="warn", file=fn, path=p, reason=why,
                          analysis_sop_warnings=warnings)
        except Exception:
            pass
    return ("你是一名资深 AI 领域求职顾问，专精数据标注、AI 数据运营、标注管理、AI 产品经理岗位。\n"
            "下面是你必须遵循的求职 SOP：\n\n" + "\n\n".join(parts) + "\n\n" + SCHEMA_HINT,
            warnings)


def _unreadable_reason(path):
    """文件**在**却读不出来时，说清是哪一种读不出来（给用户看的一句话）。

    `store.read_text` 把一切都兜成默认值，这里必须自己再读一遍才能拿到原因 ——
    拿不到原因就说"读不出来"，**绝不编一个**。
    """
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as e:
        return "打不开这个文件（%s）" % e
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return "文件不是 UTF-8 编码（请把它另存为 UTF-8 后重试）"
    return "文件读不出内容（请检查它的权限或被别的程序占用）"



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


def report_template_for(data):
    """报告数据 → 用哪个模板文件。**这是唯一判定点**。

    写盘（`render_report`）与 serve（`_render_report_page`）必须走同一份规则：两处各写一套
    就是「老报告用不上新前端」那类不一致的复发 —— 更糟的是会出现「磁盘上是 JD 骨架、
    服务里打开是复盘版式」的割裂（双击打开的那份永远是错的）。

    三条分支的顺序不重要，**缺省才重要**：kind 是后加的字段，所有老 JD 报告都没有它，
    所以认不出来的 kind 一律回 JD 模板（否则上线当天老报告会集体变成别的版式）。
    """
    kind = data.get("kind") if isinstance(data, dict) else None
    if kind == "interview":
        return INTERVIEW_TEMPLATE_PATH
    if kind == "outcome":
        return OUTCOME_TEMPLATE_PATH
    return TEMPLATE_PATH


def _attach_transcript(data):
    """把这场面试的原始转写塞进报告数据 —— 复盘报告的「原始转写全文」段要它。

    **写盘时已经带上了**（`interview.render_report` 会把 transcript_text 放进 payload），
    这里是给**旧报告**兜底：之前生成的 iv_*.html 里没有正文，动态重渲染时顺手补一次。
    取不到（记录被删 / 文件损坏 / 没有 meta）一律静默跳过：绝不能让报告因此打不开。
    """
    if isinstance(data.get("transcript_text"), str) and data["transcript_text"].strip():
        return                                   # payload 里已经有了，不重复读盘
    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    iid = str(meta.get("interview_id") or "")
    if not iid:
        rid = str(data.get("report_id") or "")
        iid = rid[3:] if rid.startswith("iv_") else ""      # 报告文件名就是 iv_<面试id>
    if not iid:
        return
    tr = store.load_interview_json(iid, "transcript.json", {})
    text = tr.get("text") if isinstance(tr, dict) else ""
    if isinstance(text, str) and text.strip():
        data["transcript_text"] = text


def read_report_template(path):
    """读一份报告模板（按 UTF-8）。**"没有模板"与"模板读不出来"必须分得开**（P1-9）。

    - 文件不存在 → 返回 None（调用方说"缺少报告模板"）；
    - 文件在、却读不出来（非 UTF-8 / 权限 / 被独占）→ 抛 `TemplateUnreadable`，
      消息里带**哪个文件 + 什么原因**，并记一条日志。**绝不说成"缺少"**；
    - 文件在、也是 UTF-8、但是空内容 → 返回空串（调用方当"没有可用模板"处理，
      这不是读失败，是文件真的空）。

    为什么不直接用 `store.read_text`：它把一切异常兜成默认值 —— 那正是这条 bug 的来源
    （解码失败 → 空串 → 上游报"缺少报告模板"）。
    """
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except UnicodeDecodeError as e:
        why = ("不是合法的 UTF-8 文本（第 %d 字节起解不出来：%s）。"
               "请把它另存为 UTF-8 编码后重试（另存时别选 GBK/ANSI）" % (e.start + 1, e.reason))
    except OSError as e:
        why = "打不开/读不出这个文件（%s）。请检查它的权限，或它是否正被别的程序占用" % e
    try:
        log.log_event("report.template_unreadable", level="warn",
                      file=os.path.basename(path), path=path, reason=why)
    except Exception:
        pass
    raise TemplateUnreadable("报告模板读不出来：%s\n    %s" % (path, why))


def _render_report_page(path):
    """用**当前模板** + 报告里的 REPORT_DATA 渲染。kind 决定用哪个模板（report_template_for）。

    kind="interview" → interview_template.html；"outcome" → outcome_template.html；
    其它/缺失 → report_template.html。

    读不出 REPORT_DATA（旧格式 / 文件被改坏）、模板缺失、或拼装时出任何意外时返回 None，
    由调用方退回 _send_file 原样发送 —— 报告绝不能因此打不开。
    """
    data = kb.read_report_data(path)
    if not isinstance(data, dict):
        return None
    tpl_path = report_template_for(data)
    # 读不出来会抛 TemplateUnreadable（不在这里吞）：调用方 `/reports/*` 的兜底
    # 会退回"原样发送磁盘上那份"，报告照样打得开，而日志里已经留下"模板读不出来"。
    tpl = read_report_template(tpl_path)
    if not tpl:
        return None
    if tpl_path == INTERVIEW_TEMPLATE_PATH:
        _attach_transcript(data)
    title = _report_title(path) or os.path.basename(path)
    # 拼装方式必须与 render_report() 完全一致，否则同一份数据会渲染出两种页面
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return tpl.replace("__TITLE__", title).replace("__REPORT_DATA__", payload)


def _reserve_report_name(base):
    """在 `reports/` 里**独占**占下一个可用文件名，返回 `(文件名, 绝对路径)`。

    为什么不能用 `os.path.exists` 找空位（体检 §5 P3-1）：那是"先问再拿"的 TOCTOU ——
    两个并发分析可以**同时**问出"`(3).html` 没人用"，于是两份报告写同一个文件：后写的把
    先写的**整份覆盖**，一份已经花过模型额度的分析凭空消失（更糟的是并发 `os.replace`
    到同一个目标在 Windows 上还会 `PermissionError`，那一份连文件都留不下）。
    判据必须是"**拿到手才算数**"：`open(path, "x")`（= `O_CREAT|O_EXCL`）由内核保证
    "只有一个人能创建这个名字"，抢不到（`FileExistsError`）就换下一个序号重试。

    序号与老实现**逐字一致**：`base.html` → `base(2).html` → `base(3).html`…
    ⚠️ 这个文件名**同时就是报告 id**（去掉 `.html`）：`jobs.json` 的 `report`、
    `report_state/<id>.json`、前端 `body["report"][:-5]`、`jdfacts._report_of` 全都按它
    定位 ⇒ 命名规则与返回的字符串一个字符都不许变。

    返回值里带上绝对路径，是为了让调用方写完就把这一份交出去。占位文件本身是 **0 字节**
    （真正的字节由 `store.write_text` 的原子写换进去）⇒ 调用方失败时要自己收拾
    （见 `render_report` 的 `except`）：否则 `reports/` 里会多一份 0 字节的"报告"，
    `GET /api/reports` 会把它当一份真报告列给用户。
    """
    room = store.p_path("reports")
    os.makedirs(room, exist_ok=True)
    n = 1
    while True:
        fname = base + ".html" if n == 1 else "%s(%d).html" % (base, n)
        path = os.path.join(room, fname)
        try:
            with open(path, "x"):        # 独占创建：这个名字被我拿到了才算数
                pass
        except FileExistsError:
            n += 1
            continue
        return fname, path


def render_report(d, fixed_id=None):
    # 写盘与 serve 共用同一份 kind 判定：否则磁盘上那份会是与用户看到的不同的骨架
    tpl_path = report_template_for(d)
    try:
        tpl = read_report_template(tpl_path)
    except TemplateUnreadable as e:
        # 读不出来**不能**报成"缺少模板"（P1-9）：这里必须是**同一个异常类型**往下传，
        # 由调用方（路由/CLI）原样印给用户。
        raise RuntimeError(str(e))
    if tpl is None:
        raise RuntimeError("缺少报告模板：%s" % os.path.basename(tpl_path))
    if not tpl:
        raise RuntimeError("报告模板是空文件：%s" % tpl_path)
    job = store.slug(d.get("job_title") or "岗位")
    score_disp = "%g" % d["score"]
    reserved = not fixed_id
    if fixed_id:
        # 固定文件名（示例报告 / 面试复盘 / 结果面板）：重复生成直接覆盖，不产生 (2)(3) 副本
        fname = fixed_id + ".html"
        path = os.path.join(store.p_path("reports"), fname)
    else:
        score_file = "%g" % round(d["score"])
        base = "%s_%s_%s分" % (datetime.now().strftime("%Y%m%d"), job, score_file)
        # **独占创建**占名（P3-1）：抢不到就换下一个序号，绝不能"先问再拿"
        fname, path = _reserve_report_name(base)
    d["report_id"] = fname[:-5]
    if tpl_path == INTERVIEW_TEMPLATE_PATH:
        # 复盘报告不能顶着「岗位匹配分析 · 0分」的标题出现在浏览器标签页 / 打印页眉上
        title = ("%s · 面试复盘 · %s" % (d.get("job_title") or "面试", d.get("date") or "")).rstrip(" ·")
    elif tpl_path == OUTCOME_TEMPLATE_PATH:
        # 同理：结果复盘顶着 JD 那句「岗位匹配分析 · 0分 · 」既不真（复盘不打分）又会被误解成
        # 一份匹配度报告。**绝不复用 `岗位匹配分析` 这几个字** —— 那也是测试里区分版式的标记。
        title = ("%s · 结果复盘 · %s" % (d.get("job_title") or "岗位", d.get("outcome_at") or "")).rstrip(" ·")
    else:
        title = "%s · 岗位匹配分析 · %s分 · %s" % (d.get("job_title") or "岗位", score_disp, d.get("verdict") or "")
    payload = json.dumps(d, ensure_ascii=False).replace("</", "<\\/")
    html = tpl.replace("__TITLE__", title).replace("__REPORT_DATA__", payload)
    try:
        store.write_text(path, html)       # 原子写（同目录临时文件 + os.replace）
    except BaseException:
        if reserved:
            # 渲染失败时把**我们自己刚占的那个空占位**收掉：留着会被 `/api/reports` 当成
            # 一份真报告列出来（0 字节、打不开），而那个时刻连一次分析都没成功。
            try:
                os.remove(path)
            except OSError:
                pass
        raise
    return fname


# ---------------------------------------------------------------- 分析前查重
# 为什么必须插在**调模型/调搜索之前**：「同一份 JD 被分析两次」会把同一个概念在知识库里记成
# 「被 2 个岗位提到」，而 multi_jd 是复习优先级的**乘性**因子 —— 已经在静默算错用户现在就能
# 看到的排序，而且同一份 JD 反复重分析会越叠越多。命中时一个额度都不花才叫防住了。
#
# 判定参数（阈值 0.85 / 保守门槛）只有 jobdup 一份实现：这里**只调它**，绝不另抄一套。
# 本块**刻意不用 jobdup._merged_into**（下划线是「模块私有」）：「已合并」这个语义在两侧都要用，
# 抄成两个各自演化的小函数比反向 import 一个私有符号更不容易出错。
ANALYZE_DUP_SUGGESTIONS = ["overwrite", "new"]


class DuplicateSubmission(RuntimeError):
    """提交的 JD 命中已有岗位（§6）→ 路由回 409。异常里带**完整**的重复信息。"""

    def __init__(self, duplicate):
        RuntimeError.__init__(self, "这份 JD 与已有岗位 %s 重复" % duplicate.get("job_id"))
        self.duplicate = duplicate


class BadOverwrite(RuntimeError):
    """`overwrite` 指向不存在 / 已合并 / 不相似的岗位 → 路由回 400。

    ⚠️ 必须严格区分于 DuplicateSubmission：**只认「这个 id 不能用」**。
    否则前端传错 id 时用户看到的是没关系的一句「重复」，永远不知道结果没写回去。
    """


class BadResumeVersion(RuntimeError):
    """`resume_id` 指向一个**不存在**的版本 → 路由回 400（修 4）。

    为什么必须复核而不是直通：`resume_id` 是「这次用的是**哪一版**」的唯一凭据 —— 它会写进
    `jobs.json` 与报告数据（报告里那句"用的是哪一版"就成了假话），同时又是查重的另一半粒度
    （两个不同文件共用一个 id → **误 409**，而前端那时没有出口）。与 `overwrite` 同一套纪律：
    给了一个 id，就必须**真的**能在 `resumelib` 里找到它；找不到就明确报错，绝不静默接受。
    空串是**另一回事**（老前端 / 老数据不传版本号）→ 照常放行，记录里不写这个键。
    """


def _job_merged_into(job):
    """记录上的 `merged_into`（只认非空字符串；手改出的 5 / [] / "" 一律当没合并）。"""
    v = job.get("merged_into") if isinstance(job, dict) else None
    return v.strip() if isinstance(v, str) else ""


# ---------------------------------------------------------------- JD 形象（jds.json = JD 级真相源）
# 阶段 4A 的分界线（规格 §3）。**这不是抄一份，是两套字段**：
#   * **JD 级**（`JD_FIELDS`）—— 岗位是什么、投递到哪一步、结果如何。**只存 `jds.json`**。
#     为什么 outcome_* 也算 JD 级：你在一个岗位上只投一次、只用一个简历，结果属于岗位。
#   * **分析级**（`ANALYSIS_FIELDS`）—— 这次分析用哪份简历、多少分、报告在哪。只存 `jobs.json`。
# 两边各存一份是本项目反复栽的坑：`status` 在 jobs.json 改了、jds.json 里还是旧的，
# 于是台账一个说法、结果面板另一个说法。**写 jobs.json 之前一律过 `_analysis_fields` 剥一遍。**
JD_FIELDS = ("job_title", "job_category", "location", "salary", "company", "hr_name", "phone",
             "address", "status", "verdict", "notes", "jd_text_file", "jd_image", "source_type",
             "source_link", "outcome_at", "outcome_reason", "outcome_ai")
# `resume_id`（阶段 4C）= 这次分析用的是**哪一版**简历（`resumelib` 的版本 id，如 `rv_0001`），
# 与 `resume`（显示名）并存：显示名兼容旧报告与旧界面，版本 id 才是"取回那一版"的凭据。
# **它同时是查重的另一半粒度**（见 `find_analyze_duplicate`）：同 JD + 同版本才算重复。
# 老分析**没有**这个键（不是空串）→ 界面显示「版本未知（早于版本库）」，绝不猜。
ANALYSIS_FIELDS = ("id", "jd_id", "resume", "resume_id", "score", "report", "created_at", "merged_into")

# 视图窗口（见 `_job_view_window`）：惰性打一次补丁，之后按线程分发。
# `_STORE_PATCH_LOCK` 只保护「打补丁」这一步（幂等，不在窗口期持有）。
_STORE_PATCH_LOCK = threading.RLock()
_STORE_PATCHED = False
_STORE_ORIGINALS = {}


def _original_upsert_job():
    """补丁**之前**的 `store.upsert_job`（分析记录写盘唯一的真出口，绝不用模块属性：

    模块属性在窗口里是视图版 —— 用它就会无限递归（实测踩到 `maximum recursion depth exceeded`）。
    没打过补丁时直接用模块属性（那时它就是原函数）。
    """
    if _STORE_PATCHED:
        return _STORE_ORIGINALS["upsert_job"]
    return store.upsert_job


def _analysis_fields(rec):
    """只留分析级字段 + `jd_id`（`merged_into` 是 3.6 的合并标记，也要留着才过滤得掉）。

    写 `jobs.json` 的**唯一入口**都走这里 —— 否则 JD 级字段会悄悄漏回 jobs.json，
    又要回到「两个文件各存一份」。`jd_id` 空但 `id` 有值（迁移前的记录）时退回 `id`：
    迁移前身份 == 报告文件名，而历史 `from_jds` 指的正是它，两者一致才不会指丢。
    """
    if not isinstance(rec, dict):
        return {}
    out = {k: rec[k] for k in ANALYSIS_FIELDS if k in rec}
    if not str(out.get("jd_id") or "") and str(out.get("id") or ""):
        out["jd_id"] = out["id"]
    return out


def _write_analysis(rec, key=None):
    """真正写 `jobs.json` 的那一层（分析记录只留分析级字段 + `jd_id`）。

    ⚠️ 永远走 `_original_upsert_job()`：窗口里 `store.upsert_job` 是视图版，
    直接调模块属性会无限递归（实测踩到 `maximum recursion depth exceeded`）。
    """
    clean = _analysis_fields(rec)
    if not clean.get("id"):
        raise ValueError("分析记录必须有 id")
    return _original_upsert_job()(clean, key)


def _save_analysis(rec, key=None):
    """把一条**分析**记录写进 `jobs.json`（JD 级字段在这里被剥掉）。"""
    return _write_analysis(rec, key)


def _jd_of_analysis(rec):
    """一条分析记录挂在哪个 JD 下。`jd_id` 优先，迁移前的记录退回 `id`（那时身份==报告文件名）。"""
    if not isinstance(rec, dict):
        return {}
    jid = str(rec.get("jd_id") or rec.get("id") or "")
    return jdid.find_jd(jid) if jid else None


def _analysis_view(rec):
    """`jds.json` 的 JD 记录 **+** 这条分析的 `id`/`score`/`report`/`resume` —— 给 outcome 用。

    为什么要有这一层：`outcome` 模块读的是「一条岗位记录」的扁平形状（`job.get("status")` …），
    而真相已经搬进 `jds.json`。这层把同一个形状照旧端出来；**写回**时由 `_with_job_views`
    把 JD 级字段交给 `jdid.upsert_jd`、分析级字段交给 `_write_analysis`（写错地方就退回两处各存一份）。
    """
    if not isinstance(rec, dict):
        return None
    jd = _jd_of_analysis(rec) or {}
    view = dict(jd)
    view["id"] = str(rec.get("id") or "")            # 只读视图里 id = 这条分析的 id
    view["jd_id"] = str(rec.get("jd_id") or jd.get("id") or rec.get("id") or "")
    for k in ("score", "report", "resume", "created_at"):
        if k in rec:
            view[k] = rec[k]
    if "merged_into" in rec:
        view["merged_into"] = rec["merged_into"]
    return view


def _ledger_analyses():
    """台账里**当前有效**的分析记录：`merged_into` 非空的（已合并的标记记录）全部排除。

    §4：合并只打标记不删记录，所以「列表」与「统计」都必须过滤，否则被并的那条
    会一直躺在台账里，用户看到两个岗位。
    """
    return [j for j in store.load_jobs() if not _job_merged_into(j)]


def _jd_id_of_row(rec):
    """一条分析记录对应的 JD id（`jd_id` 优先，迁移前退回 `id`）。"""
    return str(rec.get("jd_id") or rec.get("id") or "")


def _analysis_scores(rec):
    """一条分析**当时**的逐维分数（从它自己的报告 `REPORT_DATA` 里读）。

    返回 `(scores, error)`：

    - `scores` = `[{"dimension","weight","score"}, …]`（**只留真有维度名的**）；`error` = ""
    - 读不出（没有报告名 / 文件不在盘上 / 报告坏）→ `(None, 原因)`，`error` 非空

    ⚠️ **读不出就是读不出**：绝不编一份分数、也绝不拿 `[]` 冒充"这一版一个维度都没评"
    （前者是假话，后者会让界面把"读不到"说成"没评"）。前端据 `scores`/`scores_error`
    决定那一格是「该版未评这一维」还是「报告读不出评分」。
    """
    name = store.safe_name(str((rec or {}).get("report") or ""))
    if not name:
        return None, "这条分析没有报告文件名"
    path = os.path.join(store.p_path("reports"), name)
    if not os.path.isfile(path):
        return None, "报告文件不在盘上：%s" % name
    data = kb.read_report_data(path)
    if not isinstance(data, dict):
        return None, "报告里读不出 REPORT_DATA：%s" % name
    out = []
    for s in (data.get("scores") or []):
        if not isinstance(s, dict):
            continue
        dim = str(s.get("dimension") or "").strip()
        if not dim:
            continue
        out.append({"dimension": dim, "weight": s.get("weight"), "score": s.get("score")})
    return out, ""


def _jd_row(jd_id, records):
    """一行 = 一个 JD：JD 记录全字段 + 分析聚合（份数 / 最高分 / 每份分析）。

    ⚠️ `id` = **JD id**（不是分析 id）：前端拿它去 `/api/jd_text`、`/api/job/outcome`、
    `/api/job/result`、`X-Job-Id`，这些现在都以 JD 为身份。分析级字段（`report`/`resume`/
    `score`/`jd_text_file`）逐份挂在 `analyses` 里，**不**冒充 JD 字段。

    `analyses[]` 另外带两样**前端要用的真相**（阶段 4C 的并排表与取回分支）：
    `resume_id`（这次用的是**哪一版** —— 缺它就只能显示「版本未知」）与
    `scores`/`scores_error`（那一版当时的逐维分数，源报告见 `_analysis_scores`）。
    """
    jd = jdid.find_jd(jd_id) or {}
    rows = sorted(records, key=lambda r: (str(r.get("created_at") or ""), str(r.get("id") or "")))
    analyses = []
    for r in rows:
        scores, scores_error = _analysis_scores(r)
        analyses.append({
            "id": str(r.get("id") or ""),
            "resume": str(r.get("resume") or ""),
            # 老分析**没有**这个键（不是空串）→ 前端显示「版本未知（早于版本库）」，不猜
            "resume_id": str(r.get("resume_id") or ""),
            "score": r.get("score"),
            "report": str(r.get("report") or ""),
            "created_at": str(r.get("created_at") or ""),
            "scores": scores,
            "scores_error": scores_error,
        })
    scores = []
    for r in rows:
        try:
            if r.get("score") is not None:
                scores.append(float(r.get("score")))
        except (TypeError, ValueError):
            pass
    row = dict(jd) if isinstance(jd, dict) else {}
    row["id"] = jd_id
    row["jd_id"] = jd_id
    row["analysis_count"] = len(rows)
    row["max_score"] = max(scores) if scores else None
    row["analyses"] = analyses
    # 顶层 score 也放最高分：老前端把它当「这个岗位的分」，改成最高分才符合「一行 = 一个 JD」
    row["score"] = row["max_score"]
    if rows:
        row["created_at"] = str(rows[0].get("created_at") or row.get("created_at") or "")
        row["updated_at"] = str(rows[-1].get("created_at") or row.get("updated_at") or "")
        if not str(row.get("resume") or ""):
            row["resume"] = str(rows[-1].get("resume") or "")
        if not str(row.get("report") or ""):
            row["report"] = str(rows[-1].get("report") or "")
    return row


def _config_type_error(body):
    """`POST /api/config` 的 body 里**有类型错的字段**吗？返回一句人话（都对就回空串）。

    ⚠️ **"没有这个字段"与"字段类型错"必须分开**：前者**合法**（前端只发它要改的部分、
    老前端不认新字段），后者是**调用方发错了东西** —— 后者绝不能静默忽略（理由见调用点）。
    """
    if not isinstance(body, dict):
        return ("请求体必须是一个对象，收到的是 %s —— 这次一个字节都没有写盘。"
                % type(body).__name__)
    wanted = (("text_model", dict), ("vision_model", dict), ("search_providers", list))
    for key, want in wanted:
        if key in body and not isinstance(body[key], want):
            return ("设置 `%s` 的类型不对：应当是 %s，收到的是 %s（%s）—— "
                    "这次一个字节都没有写盘，请检查发上来的内容。"
                    % (key, want.__name__, type(body[key]).__name__, str(body[key])[:80]))
    return ""


def _unreadable(err, path):
    """strict 读报了原因时，盘上**真的有东西可丢**吗？

    判据是 `store.file_has_bytes_to_lose` —— 与 store 自己那三个 `*_safe_to_write` 用的
    **同一把尺子**：**不存在 / 真的是 0 字节** = 合法的"第一次"（新建档案；用户在别处
    `open(..., "w")` 也会留下 0 字节空文件），照旧回空；其余（**有字节却读不出来**）
    才是"读不出来"，必须如实报出去。

    ⚠️ 为什么不直接拿 strict 读的原因当判据：0 字节的 `jobs.json` / `knowledge.json`
    在 `json.load` 眼里同样是 `Expecting value: line 1 column 1` —— 拿它当判据就会把
    "新建档案"报成"读不出来"（那是另一种假话，而且让新用户第一次打开就看见一片红）。
    """
    return bool(err) and store.file_has_bytes_to_lose(path)


# `POST /api/config` 在**配置读不出来**时给用户的那句话（问题 4：GET 与 POST 不许两套话术）。
# 三件事必须说清：① 这是**这个文件现在的状态**，不是"服务器的错"（读不出来的是用户自己的
# `config.json` —— 与 `/api/jd_text` 那条论证同源：**500 会是句假话**）；② 这次**没有保存
# 任何设置**、盘上一个字节都没动；③ 为什么不"照样保存"：`load_config()` 读不出来时回的是
# 默认配置（`api_key` 为空、`search_providers` 为空），它与「你从没配过」长得一模一样，
# 写下去就是真的 API Key 与搜索来源永久消失（盘上没有任何副本）。
# ⚠️ 内部异常原文（`Expecting value…` / `codec can't decode…`）**只进日志**，绝不进这句。
_CONFIG_UNREADABLE_SAVE_SAY = (
    "配置 `config.json` 读不出来（这是这个文件当前的状态，不是服务器的错）："
    "**这次没有保存任何设置**，盘上那份文件一个字节都没动。"
    "为什么不能照样保存：读不出来时 `load_config()` 回的是默认配置"
    "（`api_key` 为空、`search_providers` 为空）—— 它与「你从没配过」一模一样，"
    "写下去就是真的 API Key 与搜索来源永久消失，而盘上没有任何副本。"
    "请先把 `config.json` 修好（或从别处恢复、把它移开）再保存。")


def _ledger_store_error():
    """台账那一行**读不出来**的原因（都读得出来回空串）。

    判据只用 `store` 的 strict 读 ——"读不出来 ≠ 没有"这条口径在仓库里**只准有一份实现**，
    这里不另写一套。为什么两条数据文件都要问：`_ledger_rows()` 的每一行是 `jobs.json`
    （分析记录）与 `jds.json`（JD 级真相，经 `jdid.find_jd`）**拼**出来的 ——
    任何一半读不出来，界面拿到的都是一份**残缺却看起来完整**的台账。

    ⚠️ 用的是 `read_*_strict_deep`（= 既有 strict 读 + **元素级**校验）：`[{好记录}, 1]`
    这种**半坏**的台账若只报"读得出来"，`_ledger_rows()` 会把坏的那条静默 `continue` 掉
    （用户少看见一条，却不知道少了）；`[1, 2, 3]` 更是整份回「共 0 个岗位」。
    浅的那两个函数**没有**被收紧（它们是 `*_safe_to_write` 等写路径的判据，语义不许动）。
    """
    _jobs, jobs_err = store.read_jobs_strict_deep()
    if _unreadable(jobs_err, store.jobs_path()):
        return jobs_err
    _jds, jds_err = store.read_jds_strict_deep()
    if _unreadable(jds_err, store.p_path("jds.json")):
        return jds_err
    return ""


def _ledger_rows():
    """台账：**一行 = 一个 JD**（JD 级真相来自 `jds.json`，分析份数/最高分现算）。

    没有 JD 记录的历史分析**兜底成一行**（用记录本身当 JD 级来源）：迁移还没跑时
    台账不能因此空白 —— 「记录看不见」比「字段是老形状」严重得多。
    """
    groups = {}
    order = []
    for rec in store.load_jobs():
        if not isinstance(rec, dict) or _job_merged_into(rec):
            continue
        jd_id = _jd_id_of_row(rec)
        if jd_id not in groups:
            groups[jd_id] = []
            order.append(jd_id)
        groups[jd_id].append(rec)
    rows = [_jd_row(jd_id, groups[jd_id]) for jd_id in order]
    rows.sort(key=lambda r: (str(r.get("created_at") or ""), str(r.get("id") or "")), reverse=True)
    return rows


def _jd_row_of(jd_id):
    """按 **JD id** 找台账里那一行（已合并的分析不算「台账里还有这个岗位」）。找不到回 None。"""
    want = str(jd_id or "")
    if not want:
        return None
    rows = _analysis_records_of(want)
    return _jd_row(want, rows) if rows else None


def _analysis_records_of(jd_id):
    """该 JD 下**有效**（未合并）的分析记录，按 created_at 升序。"""
    want = str(jd_id or "")
    if not want:
        return []
    rows = [r for r in _ledger_analyses() if _jd_id_of_row(r) == want]
    return sorted(rows, key=lambda r: (str(r.get("created_at") or ""), str(r.get("id") or "")))


def _analysis_record_by_id(analysis_id):
    """按**分析 id** 找一条有效分析记录。"""
    want = str(analysis_id or "")
    if not want:
        return None
    for r in _ledger_analyses():
        if str(r.get("id") or "") == want:
            return r
    return None


def _job_in_ledger(job_id):
    """按 id 在**有效台账**里找一条记录（已合并的不算「台账里还有这个岗位」）。

    新模型的**读写入口都收 JD id**（前端从 `/api/jobs` 拿到的 `id` 就是 JD id），
    但历史记录 / 老前端可能还传分析 id 或报告文件名 —— 两种都认，认不出回 None
    （调用方据此回 404 / 400）。返回的是**分析记录**，JD 级字段请用 `_jd_of_analysis`。
    """
    want = str(job_id or "")
    if not want:
        return None
    by_id = _analysis_record_by_id(want)
    if by_id is not None:
        return by_id
    rows = _analysis_records_of(want)
    return rows[-1] if rows else None


def _jd_view(job_id):
    """把一个 id（JD id / 分析 id 都认）解析成 `(JD记录, 有效分析记录)`；认不出回 `(None, None)`。

    **已合并的分析不算「台账里还有这个岗位」**（`_job_in_ledger` 已排除）—— 写接口拿到
    `(JD, None)` 时必须回 404，否则就变成「悄悄改一条用户看不到的记录」。
    """
    want = str(job_id or "")
    if not want:
        return None, None
    jd = jdid.find_jd(want)
    rec = _job_in_ledger(want)
    if jd is None and rec is not None:
        jd = _jd_of_analysis(rec)
    if rec is None and jd is not None:
        rows = _analysis_records_of(want)
        rec = rows[-1] if rows else None
    return jd, rec


def _purge_target_id(raw):
    """把前端传来的一个 id 解析成「要删的那个岗位」的 JD id（认不出回空串）。

    与别的写接口**同一口径**（`_jd_view`）：JD id 优先；老前端 / 历史链接传**分析 id**
    或**报告文件名**也认（`_job_in_ledger` 两种都查）。解析不出就原样返回去空白后的值 ——
    预演会据此回 404（"JD 库里没有这个 ID，也没有挂在它下面的分析"），**绝不猜一个**。
    """
    want = str(raw or "").strip()
    if not want:
        return ""
    jd, rec = _jd_view(want)
    if isinstance(jd, dict) and str(jd.get("id") or ""):
        return str(jd["id"])
    if isinstance(rec, dict):
        return _jd_id_of_row(rec)
    return want


def _jd_text_path(jd):
    """JD 原文的磁盘路径；没有原文回空串。

    新记录：`jd_text_file` 是 `<jd_id>.txt`；迁移前的老记录 `jd_text_file` 可能是
    `<报告文件名>.txt` —— 两种都按字段走，字段空时才退回 `<jd_id>.txt`（Task 2 之前的
    记录根本没有 `jd_text_file` 指向 JD id 这一说）。
    """
    if not isinstance(jd, dict):
        return ""
    name = store.safe_name(jd.get("jd_text_file"))
    if not name and str(jd.get("id") or ""):
        name = store.safe_name(str(jd.get("id")) + ".txt")
    return os.path.join(store.p_path("jd"), name) if name else ""


def _jd_text_of(jd_id):
    """读该 JD 的原文（`jds.json` 的 `jd_text_file` → 磁盘）；读不到回空串。"""
    jd = jdid.find_jd(jd_id)
    path = _jd_text_path(jd)
    if path and os.path.exists(path):
        return store.read_text(path, "")
    # 兜底：老记录 / 还没建 JD 记录时，原文就叫 `<id>.txt`
    return jobdup.jd_text_of(jd_id)


def _similar_saved_jd(jd_text):
    """在**已有 JD** 里找与新提交 JD 判定为重复的那一个；找不到回 None。

    命中时返回 `(jd_id, similarity)`，取相似度最高的那条（§3 的指纹判定，
    复用 `jobdup.containment` / `jobdup.THRESHOLD` —— 阈值只有一份实现）。
    只能比较**读得到 JD 原文**的 JD（缺原文的一律不猜，与迁移脚本同一条纪律）。
    """
    best = None
    for jd in jdid.load_jds()["jds"]:
        if not isinstance(jd, dict) or not str(jd.get("id") or ""):
            continue
        saved = _jd_text_of(jd.get("id"))
        if not saved.strip():
            continue
        score = jobdup.containment(jd_text, saved)
        if score >= jobdup.THRESHOLD and (best is None or score > best[1]):
            best = (str(jd.get("id")), score)
    return best


def _job_view_of(job_id, key=None):
    """「扁平视图」：把一条记录拼成 `outcome` 认得的形状（JD 级 + 分析级）。"""
    rec = _job_in_ledger(job_id)
    if rec is not None:
        return _analysis_view(rec)
    jd = jdid.find_jd(job_id)
    if jd is not None:
        rows = _analysis_records_of(jd.get("id"))
        base = dict(rows[-1]) if rows else {"id": jd.get("id"), "jd_id": jd.get("id")}
        # 手改坏的 JD 记录也要让 outcome 走它的容错分支（它是纯数据模块）
        return dict(list(_analysis_view(base).items()) + list(jd.items()))
    return None


def _job_view_upsert(record, key=None):
    """视图版的「保存一条岗位记录」：**分头落盘**（JD 级 → `jds.json`；分析级 → `jobs.json`）。

    这是本层存在的全部意义 —— 不做这一步，`outcome` 的一次保存就会把 JD 级字段
    写回 `jobs.json`，「两个文件各存一份」立刻复活。
    """
    if not isinstance(record, dict):
        raise ValueError("记录必须是字典")
    jd_id = str(record.get("jd_id") or "")
    rec = None
    if jd_id:
        rows = _analysis_records_of(jd_id)
        rec = rows[-1] if rows else None
    if rec is None:
        rec = _job_in_ledger(record.get("id"))
    if rec is None:
        # 没有对应的分析记录（不该发生）：至少别把 JD 级字段写进 jobs.json
        return _save_analysis(record)
    merged = dict(_analysis_fields(rec))
    merged.update(_analysis_fields(record))
    merged["jd_id"] = str(merged.get("jd_id") or jd_id or rec.get("jd_id") or "")
    merged["id"] = str(rec.get("id") or record.get("id") or "")
    if jd_id:
        jd = dict(record)
        jd["id"] = jd_id
        jdid.upsert_jd(jd)
    # ⚠️ 用 `_write_analysis`（内部走 `_original_upsert_job()`，即**补丁之前的**原函数）：
    # 直接调 `store.upsert_job` 会再落到这个被换掉的实现上 → 无限递归（实测踩到过）。
    _write_analysis(merged)
    # 返回**视图**（JD 级 + 分析级拼起来的扁平形状）：调用方 `outcome.save_result`
    # 会拿返回值去做 `_view_of(...)` —— 只回分析记录的话状态永远是「未投递」。
    out = _job_view_of(merged.get("id"))
    return out if isinstance(out, dict) else merged


# 线程本地窗口：只有**主动进入窗口的那个线程**看到视图，别的线程一律走原实现。
# 阶段 4A 之后 JD 级真相住在 `jds.json`（键是 `jd_id`），而 `outcome` 那份实现假设
# 「一条扁平记录 = 一个文件」。翻译层放在这里；**绝不能让它影响别的线程** ——
# `/api/job/attribute` 的窗口里含**一次几十秒的模型调用**，改全局的话其他请求
# 会在这几十秒里读到「另一个世界」的记录。
_VIEW_LOCAL = threading.local()


def _ensure_store_patch():
    """把 `store.find_job` / `store.upsert_job` 换成「按线程分发」的版本（**只换一次**）。

    原实现备份在**替换之前**抓住：窗口里分发给原实现时若再去读 `store.find_job`
    就会无限递归。补丁是**惰性**的（第一次进窗口才打），不在导入期打下 —— 导入期
    打补丁会在「测试先 mock `store.find_job` 再触发第一次窗口」时把 mock 抓成原实现。
    """
    global _STORE_PATCHED
    with _STORE_PATCH_LOCK:
        if _STORE_PATCHED:
            return
        _STORE_ORIGINALS["find_job"] = store.find_job
        _STORE_ORIGINALS["upsert_job"] = store.upsert_job

        def find_job(job_id, key=None):
            if getattr(_VIEW_LOCAL, "active", False):
                return _job_view_of(job_id, key)
            return _STORE_ORIGINALS["find_job"](job_id, key)

        def upsert_job(record, key=None):
            if getattr(_VIEW_LOCAL, "active", False):
                return _job_view_upsert(record, key)
            return _STORE_ORIGINALS["upsert_job"](record, key)

        store.find_job, store.upsert_job = find_job, upsert_job
        _STORE_PATCHED = True


@contextlib.contextmanager
def _job_view_window():
    """当前线程进入「视图窗口」：窗口内 `store.find_job` / `store.upsert_job` 走视图。

    **可以安全地包住一次模型调用**（几十秒也没关系）：分发是按线程查的
    （`_VIEW_LOCAL.active`），别的线程读到的一直是原实现。退出时（含异常）清标记 ——
    线程被复用（`ThreadingHTTPServer` 的线程池）也不会把窗口带进下一个请求。
    """
    _ensure_store_patch()
    previous = getattr(_VIEW_LOCAL, "active", False)
    _VIEW_LOCAL.active = True
    try:
        yield
    finally:
        # 还原成「进入之前」的值而不是 False：将来嵌套调用（窗口里再开一个窗口）
        # 不会因为内层退出就把外层的窗口关掉。
        _VIEW_LOCAL.active = previous


def _with_job_views(fn, *args, **kw):
    """在「视图窗口」里跑 `fn`（见 `_job_view_window`）：读拼起来、写分头落盘。

    为什么必须有这一层：`outcome` 模块的读写接口收的是「一条岗位记录的 id」，内部走
    `store.find_job(id)` / `store.upsert_job(job)`。阶段 4A 之后 JD 级字段住在 `jds.json`
    （键是 `jd_id`），`jobs.json` 里按分析 id 才找得到 —— 不接这一层，`outcome` 会对着一个
    JD id 说「找不到岗位」（404），结果面板与归因全线失效。
    """
    with _job_view_window():
        return fn(*args, **kw)


def _seq_of_id(jd_id):
    """ID 末尾的编号段；不是结构化 ID（历史报告文件名）→ 0。"""
    try:
        return jdid.parse_id(jd_id)[1]
    except ValueError:
        return 0


def _jd_record_of(jd_id, data, source_bits, link, jd_text_file, jd_image_url, base=None):
    """一条 JD 记录的 JD 级字段。**展示值用原始表述，ID 用归一化后的短值**（Task 1 交接注意 1）。

    `derive_fields` 会把 `6-8K（入职五险，提及一金7k+）` 归成 `6-8K`、把公司全名缩成简称 ——
    那是**身份**该有的样子。用户在台账里要看的却是原始薪资 / 原始公司名，所以这一份
    从 `REPORT_DATA`（也就是 `normalize` 之后的 `data`）直接取原值，不经过归一化。
    `base` 是已有 JD 记录（复用同一个 JD 时）：用户手改过、或上一次分析有而这次没有的字段
    **不能被空值覆盖**（那是把用户填过的东西丢掉）。
    """
    old = base if isinstance(base, dict) else {}

    def pick(key, value):
        v = value if isinstance(value, str) else ("" if value is None else str(value))
        v = v.strip()
        if v:
            return v
        return old.get(key) if isinstance(old.get(key), str) else ""

    app = data.get("application") if isinstance(data.get("application"), dict) else {}
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    return {
        "id": str(jd_id),
        "job_title": pick("job_title", data.get("job_title")),
        "job_category": pick("job_category", data.get("job_category") or "其他"),
        "location": pick("location", data.get("location")),
        "salary": pick("salary", data.get("salary")),            # **原始**薪资表述
        "company": pick("company", data.get("company")),         # **原始**公司名
        "hr_name": pick("hr_name", data.get("hr_name")),
        "phone": pick("phone", data.get("phone")),
        "address": pick("address", data.get("address")),
        "status": pick("status", app.get("status") or "未投递"),
        "verdict": pick("verdict", data.get("verdict")),
        "notes": pick("notes", app.get("notes")),
        "jd_text_file": str(jd_text_file or old.get("jd_text_file") or ""),
        "jd_image": str(jd_image_url or old.get("jd_image") or ""),
        "source_type": pick("source_type", " + ".join(source_bits) or "文本输入"),
        "source_link": pick("source_link", link),
        "created_at": str(old.get("created_at") or now),
    }


def _duplicate_of_resume(rows, resume_id):
    """该 JD 下**同一版简历**的那条已有分析（没有 → `None`）。阶段 4C 的查重粒度。

    三条规则（规格 §6.2）：

    - 同 JD + **不同** `resume_id` → **不是重复**（用户的核心用法：几份简历比同一个岗位）
    - 同 JD + **同一** `resume_id` → 重复（409）
    - 任一侧**没有** `resume_id`（老数据 / 老前端）→ **不判重复**（宁可多建，也不误拦）

    为什么老数据不判重复：`resume_id` 是"这次用的是哪一版"的唯一凭据。老记录里没有它，
    就**无法判断**新旧两次是不是同一份简历 —— 拿显示名去猜会把"同一份简历的第 2 版"判成重复，
    正是要修的那个 409。查不到就不拦，用户最多多一条分析；拦错了用户会**卡死**（前端没有出口）。
    """
    want = str(resume_id or "").strip()
    if not want:
        return None
    for r in reversed(rows):                       # 最新的那条优先（弹窗要显示最近一次的分数/报告）
        rid = str(r.get("resume_id") or "").strip()
        if rid and rid == want:
            return r
    return None


def find_analyze_duplicate(jd_text, overwrite_id="", resume_id=""):
    """分析前查重（§6）。**只读**：不写任何文件、不调模型、不调搜索。

    - 返回 `None`  → 照常分析（未命中 / 版本不同 / 版本未知 / 带了 allow_duplicate / overwrite 已校验通过）
    - 抛 `BadOverwrite`      → `overwrite` 不可用（不存在 / 已合并 / 不相似）→ 400
    - 抛 `DuplicateSubmission` → 命中且没带任何选择 → 409

    `overwrite_id` 非空时必须**真的**是相似的那一条（用 jobdup 复核一次）：
    否则前端传错 id 就会把这次分析的结果写到一条**无关岗位**上。

    **命中的粒度是 (JD 指纹 × `resume_id`)**（阶段 4C 改，此前只看 JD）：返回的
    `job_id` / `jd_id` 都是**已有 JD 的 id** —— 前端拿它当 `overwrite` 发回来就走「沿用原身份」
    那一支，不会再算成一个新岗位；`report` / `score` / `resume_id` 是**命中的那条分析**的，
    给前端的 409 弹窗显示"你要覆盖的是哪一份"。

    ⚠️ `analysis_id` 是**命中那条分析**的 id，前端「覆盖」必须拿它当 `overwrite`（原因见返回值
    那一段的注释）：一个 JD 下可以挂多份分析（几份简历），只给 JD id 的话后端只能取"最新那条"，
    覆盖就会写到**另一份简历**的记录上。

    为什么粒度必须带简历（用户今天被它卡死）：他 C 阶段的核心用法是「同一个岗位拿三份简历
    分别分析」，而旧实现只看 JD 文本 → 第二份简历被判重复 → **409**，前端又没有出口。
    """
    overwrite_id = str(overwrite_id or "").strip()
    if overwrite_id:
        jd, job = _jd_view(overwrite_id)
        hit_id = str((jd or {}).get("id") or (job or {}).get("jd_id") or "")
        if not hit_id or _job_in_ledger(overwrite_id) is None:
            raise BadOverwrite("要覆盖的岗位不存在或已被合并：%s" % overwrite_id)
        saved = _jd_text_of(hit_id)
        score = jobdup.containment(jd_text, saved)
        if score < jobdup.THRESHOLD:
            raise BadOverwrite(
                "要覆盖的岗位（%s）与这次提交的 JD 不相似（相似度 %.3f）："
                "拒绝把结果写到无关岗位上" % (overwrite_id, score))
        return None
    hit = _similar_saved_jd(jd_text)
    if hit is None:
        return None
    jd_id, score = hit
    rows = _analysis_records_of(jd_id)
    dup = _duplicate_of_resume(rows, resume_id)
    if dup is None:
        # 同一份 JD 但**这一版简历没分析过**（或没有版本号可比）→ 不是重复，正常新建。
        return None
    jd = jdid.find_jd(jd_id) or {}
    return {
        # `job_id` 留着是为了老前端（阶段 3.6 的弹窗读它）；两者现在都是 **JD id**
        "job_id": jd_id,
        "jd_id": jd_id,
        "job_title": str(jd.get("job_title") or ""),
        "created_at": str(dup.get("created_at") or jd.get("created_at") or ""),
        "score": dup.get("score"),
        # 前端弹窗（阶段 4C 的 409 出口）要显示"覆盖的是哪一份分析"，这几项此前没有
        "report": str(dup.get("report") or ""),
        "resume": str(dup.get("resume") or ""),
        "resume_id": str(dup.get("resume_id") or ""),
        # ⚠️ 「覆盖」必须用**这个**（不是 JD id）：一个 JD 下可以挂多份分析 —— 用户的用法就是
        # 一个岗位对三份简历。`_job_in_ledger(<JD id>)` 只能取该 JD 下**最新**那条记录，于是
        # 「覆盖」会写到**另一份简历**的记录上（实测：把 B 那份分析的版本号与报告一起改写成 A 的
        # —— 用户看到的是"覆盖了这一份、动的是那一份"，而且报告文件名与弹窗上说的不一致）。
        # 用命中那条分析的 id，`fixed_id` 才真的等于它的报告名 → 「沿用原报告文件名」才是真话。
        "analysis_id": str(dup.get("id") or ""),
        "similarity": score,
        "suggestions": list(ANALYZE_DUP_SUGGESTIONS),
    }


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


# ---------------------------------------------------------------- 简历版本库（阶段 4C）
# 版本记录、族谱、母版快照、sha 核对**全在 `resumelib`**（它只读写 `resumes.json` 与
# `resumes/` 下的文件）。这里只做三件事：把 HTTP 的参数翻成它的调用、把它的 `ValueError`
# 翻成 400/404/409、把结果翻成 JSON。**绝不在 app.py 里自己写 `resumes.json` 或简历文件** ——
# 两处各写一份就是本阶段要根除的"保存 = 静默覆盖、没有版本记录"。
_RESUME_EXTS = (".md", ".txt")
# 「延续」（= 在选定母版的基础上改）的 `lineage` 取值。中文与英文都收：前端写哪个词都行，
# 但**真正决定族谱的是 `parent_id`**（前端选了"延续"就发它）。`lineage` 只是让"延续"这个
# 选择在没有 parent_id 时也能落地（母亲 = 已被「选定继续优化」的那一版）。
_LINEAGE_CONTINUE = ("continue", "child", "延续", "子版")


def _resume_files_strict():
    """磁盘上真实的简历文件名 + **目录读不出来**时的原因（读得出 / 目录真的不存在 → 空串）。

    ⚠️ **为什么不能再用 `os.path.isdir()` 当闸**（旧写法正是这一处漏网）：它对"没有这个目录"
    （合法：还没建过简历目录）与"路径存在、但不是目录 / 读不出来"（必须报错）**静默回同一个 False**
    ⇒ `files: []`。而同一响应里 `resumelib.list_versions()` 可能**成功地**回了一串版本记录 ——
    两半自相矛盾：有版本记录，却说盘上没文件（用户会以为简历被删了）。
    判据与 `/api/reports`（§P1-14 的第 5 处）**逐字同一套**：直接 `os.listdir`，`FileNotFoundError`
    才是"真的没有"（回空），其余 `OSError`（含 `NotADirectoryError`）一律如实报出去。
    """
    d = store.p_path("resumes")
    try:
        names = os.listdir(d)
    except FileNotFoundError:
        return [], ""        # 真的还没有简历目录 = 合法的「盘上 0 份」，照旧回空
    except OSError as e:
        return [], "简历目录 `%s` 读不出来：%s —— 这不是「你盘上没有简历」。" % (d, e)
    return sorted(f for f in names if f.lower().endswith(_RESUME_EXTS)), ""


def _resume_files():
    """磁盘上真实的简历文件名（与老实现同一口径，供前端下拉 / 老前端使用）。

    ⚠️ 这里**丢掉了**"读不出来"的原因（调用方只要一份名字列表）。需要区分
    "读不出来"与"真的没有"的地方一律用 `_resume_files_strict()` —— `/api/resumes` 就是。
    """
    return _resume_files_strict()[0]


def save_resume_version(name, content, parent_id="", lineage=""):
    """保存一份简历并登记版本（**唯一**的写简历路径，规格 §4.1/4.2）。

    走 `resumelib.create_version`：留母版快照、记 sha / 时间 / rev。**绝不** `store.write_text`
    直接写简历文件 —— 那条路没有版本记录（"保存 = 截断覆盖"，Task 2 要根除的）。

    `parent_id` 非空 = **延续**（同一族谱，覆盖前留母版快照）；空 = **新开一份独立的**
    （独立根、rev=1）。`lineage` 只在没给 `parent_id` 时起作用：说的是"延续"就解析出
    该简历被「选定继续优化」的那一版当母亲（没有选定的母版 → `ValueError`，明确告诉用户）。

    会抛 `ValueError`（路由翻成 400）：名字为空 / 重名（未移出的）/ 父版本不存在 /
    父版本已移出 / 延续时改了名字 / 要延续却没有选定的母版。

    **延续**时多走一步：`resumelib.ai_diff_of`（AI 写"改了哪些地方"，规格 §4.4）。
    它失败**不影响保存**（版本已落盘，原因进 `ai_diff_error`）；"新开一份独立的"**一次模型都不调**。
    """
    clean = os.path.splitext(store.safe_name(name))[0].strip()
    if not clean:
        raise ValueError("文件名不能为空")
    pid = str(parent_id or "").strip()
    mode = "new"
    if pid:
        mode = "continue"
    elif str(lineage or "").strip().lower() in _LINEAGE_CONTINUE:
        mother = _mother_of_name(clean)
        if mother is None:
            raise ValueError("这份简历还没有「选定继续优化」的母版：先选定一份，或选「新开一份独立的」")
        pid = str(mother.get("id") or "")
        mode = "continue"
    rec = resumelib.create_version(clean, content, parent_id=pid or None)
    log.log_event("resume.save", name=rec.get("name"), id=rec.get("id"), rev=rec.get("rev"),
                  parent_id=rec.get("parent_id"), mode=mode, chars=len(content or ""))
    if mode == "continue":
        # 规格 §4.4：AI 变更比对**只在延续时触发**（"新开一份独立的"没有可比对的对象 → 一次都不调）。
        # 版本在上面已经落盘；比对失败**不是致命**（`ai_diff_of` 会把原因记进 `ai_diff_error`）。
        # 绝不在这里自己拼 prompt / 发请求：一切都在 `resumelib.ai_diff_of` 里（含注入点）。
        rec = resumelib.ai_diff_of(rec.get("id"))
        diff = rec.get("ai_diff") or {}
        log.log_event("resume.ai_diff", name=rec.get("name"), id=rec.get("id"),
                      ok=bool(diff.get("summary") or (diff.get("changes") or [])),
                      changes=len(diff.get("changes") or []),
                      error=str(rec.get("ai_diff_error") or "")[:300])
    return rec


def _mother_of_name(name):
    """名字所属族谱里 `chosen` 为真的那一版（没有 → `None`）。"""
    for rec in resumelib.list_versions():
        if str(rec.get("name") or "") == name:
            return resumelib.mother_of(rec.get("root_id"))
    return None


# ---------------------------------------------------------------- 入参口径
_BOOL_TRUE_WORDS = ("1", "true", "yes")
_BOOL_FALSE_WORDS = ("", "0", "false", "no")


def _as_bool(value):
    """布尔入参的**唯一**真值口径（HTTP body 里的值什么类型都可能）。

    ⚠️ 为什么不能直接 `bool(value)`：手写 API（curl / 自己的脚本 / 别的客户端）把布尔写成
    字符串是常态，而 `bool("false") is True`、`bool("0") is True` —— 于是一句
    `"refresh_jd_facts": "false"` 会**白烧一次真算**（206 秒 + ~53 次搜索额度），
    `"allow_duplicate": "false"` 会悄悄跳过查重（本该 409）。

    **认不出的值（`"maybe"` / `2` / `[1]` …）按原语义 `bool(value)`**：这个转换只负责修
    「字符串假值」这一类 —— `2` 从真改成假同样是行为变化，既有语义不能悄悄翻面。
    字符串先 `strip().lower()`：`"FALSE"` / `" false "` / 纯空白串都算假（那都是明确的「没开」）。

    ⚠️ 与 `outcome._as_bool` **不是**同一个契约：那个是**三态**解析（读不出来＝`None`「未判断」，
    认不出的走 `default`），用在「可控/不可控」那种不该乱下结论的地方。这里要的是把 HTTP body
    里的字符串按真值读成布尔、认不出就保持原语义 —— 两者别合并（合并必然改掉一边的语义）。
    """
    if isinstance(value, str):
        word = value.strip().lower()
        if word in _BOOL_TRUE_WORDS:
            return True
        if word in _BOOL_FALSE_WORDS:
            return False
        return bool(value)              # 认不出的字符串：原语义（非空即真）
    return bool(value)                  # 非字符串：原语义（True/1/2 → 真；0/None/[]/{} → 假）


def _accepts_reuse(fn):
    """这个 builder 收不收 `reuse` 关键字参数？（判据只有签名，见 `_builder_with_reuse`）

    ⚠️ **这道签名判断是必需的，不是画蛇添足 —— 别"顺手简化"掉它。**
    原因（后人一定要看这里，否则会踩）：
      * `jdfacts._compute_once`（`jdfacts.py:303`）用 `builder(jd_text, cfg)` —— **两个位置参数** ——
        调那条 builder 缝；而本仓**既有**的替身（`tests/test_analyze_knowledge_cache.py` 里那批
        `fake_knowledge`、`tests/test_app.py:1350` 的 `_patch_analyze_deps` 等）签的就是
        `def fake_knowledge(jd_text, cfg)`；
      * 所以在闭包里**硬塞** `fn(jd_text, cfg, reuse=reuse)` 会让这些替身**在进入函数体之前**
        就 `TypeError`（`calls` 全空、断言全红）—— 而它们**不在**本次改动的文件范围内，
        修不了也不该修（那是"替身没跟上新签名"，不是被测代码该出的错）；
      * 反过来，不注入就等于阶段 E **静默失效**（见 `_builder_with_reuse`）。
    所以判据只能是签名：**对方真的收，才传**。两个方向都由测试钉住
    （`test_the_real_builder_receives_reuse_as_a_keyword` 与
    `test_a_builder_that_only_accepts_the_old_two_arguments_still_works`）。
    """
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return False        # 查不出签名（内建 / C 扩展）：按那条缝的老契约两个参数调
    for p in params:
        if p.kind is inspect.Parameter.VAR_KEYWORD:
            return True     # `**kw` → 收
        if p.name == "reuse" and p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                                           inspect.Parameter.KEYWORD_ONLY):
            return True
    return False


def _builder_with_reuse(reuse):
    """把阶段 E 的 `reuse` 绑进 `jdfacts.resolve` 那条 builder 缝，返回 `builder(jd_text, cfg)`。

    ⚠️ **为什么必须闭包**：`jdfacts._compute_once` 是用 `builder(jd_text, cfg)`（两个位置参数）
    调的。直接把 `knowledge.build_for_jd` 传进去的话，`reuse` 永远留在默认值 `None` ——
    知识地图照旧出得来、报告也照旧写，只是**每条都去联网**：阶段 E 会**静默失效**，
    而且看不出来（这正是本项目最忌讳的失败形态）。

    ⚠️ **为什么还要看签名**（`_accepts_reuse`）：那条缝原本的契约就是 `builder(jd_text, cfg)`，
    注入的替身/别的实现只收这两个参数（`tests/test_analyze_knowledge_cache.py` 里那批
    `fake_knowledge`、`tests/test_app.py:1350` 的 `_patch_analyze_deps` 等，都在**本次改动的
    文件之外**）。硬塞 `reuse=` 会让它们 `TypeError` —— 那是"替身没跟上新签名"，
    不是被测代码该出的错（实测：不加这道判断，那一批两参数的假 builder 会在**进入函数体之前**
    就炸，`calls` 全空）。所以只把对方**真的收**的参数传过去 —— 判据与理由详见 `_accepts_reuse`
    的 docstring（那里写着"为什么不能简化掉它"）。

    ⚠️ 这里**没有** try/except：`Reuse` 自己不许抛（读不出来只落在 `stats` 里），
    真抛出来的话那是别处的 bug，应当响亮地失败。
    """
    def call(jd_text, cfg):
        fn = knowledge.build_for_jd        # 每次真算时**重新取**：测试会替换这个属性
        if _accepts_reuse(fn):
            return fn(jd_text, cfg, reuse=reuse)
        return fn(jd_text, cfg)

    return call


def _discard_orphan_jd(jd_id):
    """删掉**本次这次调用刚建、却什么也没落上**的那条 JD 记录（善后；不删任何文件）。

    什么时候会有这种记录：`do_analyze` 为了给知识地图缓存一个挂靠点，会在「定下 JD 身份」
    之后**先落一条只有 id 的记录**，而真正那份带展示字段的记录在报告之后才写。若异常落在这
    中间，盘上就留下一条**没有 `jd_facts`、也没有任何分析挂在它下面**的孤儿：台账里看不见它
    （台账的行来自 `jobs.json`），但 `_similar_saved_jd` 会通过 `jd/<id>.txt` 命中它
    （similarity 1.0）→ 用户重提同一份 JD 只会得到 **409**，而前端**没有**
    `allow_duplicate` 入口 → 「失败：未知错误」且无法继续。

    **为什么只敢删这一种（代价不对称）**：
      * 善后只动 `jds.json` 里**这一条**记录，**绝不删文件** —— 用户贴过的原文照旧留在
        `jd/` 下（那是「这个 JD 我提交过」的痕迹，删文件才是真的数据损失）；
      * 只删**同时满足**两个条件的：没有 `jd_facts`，且 `jdid.analyses_of` 为空。已经有分析
        挂在它下面的记录**绝不碰**（那已经是用户的台账）；不是本次刚建的记录也**绝不碰**
        （调用方只在「这条 id 是这次调用建的」时才调本函数，空 id 直接返回）；
      * 留着孤儿的代价只是「下一次同 JD 分析会换一个新身份」；删错的代价是用户数据丢失。
    真的删掉了一条时记一条 `analyze.jd_orphan_cleaned`（事后查得到），返回 True。
    """
    want = str(jd_id or "")
    if not want:
        return False
    try:
        # 与 jdid / jdfacts 用同一把锁，读-改-写是一个临界区（锁顺序仍恒为 _JD_LOCK → _KB_LOCK）
        with jdid._JD_LOCK:
            data = jdid.load_jds()
            rec, rest = None, []
            for r in data["jds"]:
                if r.get("id") == want:
                    rec = r
                else:
                    rest.append(r)
            if rec is None:
                return False
            if rec.get("jd_facts"):
                return False            # 有缓存 = 知识地图已经落上，这不是「什么也没落下」
            if jdid.analyses_of(want):
                return False            # 已经有分析挂在它下面 = 用户的台账
            data["jds"] = rest
            jdid.save_jds(data)
    except Exception as e:
        # 善后失败绝不能盖掉真正的那个异常（调用方正在 except 里）
        log.log_exc("analyze.jd_orphan_cleanup_error", e, jd=want)
        return False
    log.log_event("analyze.jd_orphan_cleaned", jd=want)
    return True


def do_analyze(jd_text, image_data_url, resume_name, link="", overwrite_id="", allow_duplicate=False,
               refresh_jd_facts=False, resume_id=""):
    """跑一次 JD 分析并出报告。`overwrite_id` 非空 = 「覆盖」：结果写回那条已有记录。

    `allow_duplicate` 为真 = 用户已在查重提示里选过「仍然新建」→ **跳过查重**，照常新建。

    `refresh_jd_facts` 为真 = 用户要求**重算 JD 级事实**（目前只有知识地图）→ 忽略缓存
    （`jdfacts.resolve(..., refresh=True)`）。不传 = 有缓存就复用（这是本阶段的全部收益）。

    `resume_id`（阶段 4C）= 这次用的是**哪一版**简历（`resumelib` 的版本 id）。它是查重的
    另一半粒度（同 JD + 同版本才判重复），并落进分析记录 —— 报告因此能追溯到"用的哪一版"。
    **非空时必须真的存在于 `resumelib`**，否则抛 `BadResumeVersion`（路由回 400，修 4）；
    而且它必须是**它所属族谱的当前版**（`resumelib.current_of()`，阶段 4C 修 1）—— 因为本函数
    读的永远是 `resumes/<名字>.md` 那个文件（= 该族谱最新版正文），钉一个历史版上去，
    记录里那句「用的是哪一版」就是**假话**。
    **老调用方不传**（空串）→ 记录里**不写**这个键 = "版本未知（早于版本库）"，界面据此显示，
    **绝不猜**一个填上去。

    ⚠️ 两个布尔参数都过 **`_as_bool`**（唯一的真值口径）：HTTP body 里它们是字符串是常态
    （`"false"` / `"0"`），而 `bool("false") is True` → 白烧一次真算（206 秒 + ~53 次搜索）
    或悄悄跳过查重。直接在 Python 里调本函数也走同一口径，绝不出现两套语义。

    ⚠️ 查重（`find_analyze_duplicate`）**必须在调模型/调搜索之前**跑：命中时抛
    `DuplicateSubmission` / `BadOverwrite`，一个额度都不花 —— 这是本功能的全部意义。
    位置只能在这一处：`jd_text` 若来自 OCR / 抓链接，那时才拿到文本；而**在拿到文本那一行
    的下一条**就查重，保证了「拿到文本之后的第一件花钱的事」之前已经拦过。
    """
    t_start = time.time()
    cfg = store.load_config()
    jd_text = (jd_text or "").strip()
    link = (link or "").strip()
    overwrite_id = str(overwrite_id or "").strip()
    resume_id = str(resume_id or "").strip()
    allow_duplicate = _as_bool(allow_duplicate)
    refresh_jd_facts = _as_bool(refresh_jd_facts)

    # `resume_id` 非空 = 调用方**声称**"这次用的是这一版"→ 必须真的存在（修 4）。
    # 位置在**所有花钱的事之前**（OCR / 抓链接 / 查重 / 模型）：id 错了当场抛 BadResumeVersion
    # → 路由回 400，一个额度都不花。空串是老前端/老数据那条路（记录里不写这个键），放行。
    if resume_id:
        _version = resumelib.by_id(resume_id)
        if _version is None:
            raise BadResumeVersion("找不到这一版简历（resume_id=%s）："
                                   "拒绝把这次分析记到一个不存在的版本上" % resume_id)
        # 存在还不够：这一版必须**属于被分析的这份简历**（阶段 4C 修 8）。
        # 光看"存在"时，从 A 的版本号 + B 的文件名提交也能过 —— 报告数据与 `jobs.json` 都会把
        # "这次用的是哪一版"记成**假话**（B 的内容 + A 的版本号），而版本号又是查重的另一半粒度
        # → 同一份 B 用 A 的版本号提交还会**误 409**。名字口径与 `create_version` 存的一致
        # （文件名去扩展名），与下面 `resume_display` 同一口径。
        _want = os.path.splitext(store.safe_name(resume_name))[0]
        _got = str(_version.get("name") or "")
        if _want and _got != _want:
            raise BadResumeVersion(
                "resume_id=%s 是「%s」的版本，不是要分析的简历「%s」："
                "拒绝把这次分析记到别人那一版上（报告里那句「用的是哪一版」会变成假话）"
                % (resume_id, _got, _want))
        # 存在 + 属于这份简历还不够：**必须还是这一族谱的当前版**（阶段 4C 修 1）。
        # 理由（这一条正是"会写假数据"的地方）：`do_analyze` 读的**永远是** `resumes/<名字>.md`
        # 那个文件 = 该族谱的**最新版正文**（见下面 `rp` / `store.read_text(rp)` 那几行），
        # 而 `resume_id` 是要落进报告数据与 `jobs.json` 的「这次用了哪一版」。钉一个**历史版**
        # 上去，那句话当场就是**假话**（记录说 rv_0001、模型读的是 rv_0003 的正文）；
        # 而 `resume_id` 又是查重的另一半粒度（`_duplicate_of_resume`）→ 同一个 JD 用同一族谱的
        # 第二版再分析会**误 409**，前端目前**没有 409 出口**（Task 3b 才有）→ 用户卡死。
        # "当前版"的口径只有一处：`resumelib.current_of()`（族谱内 rev 最大的**未移出**版本）——
        # 前端下拉给的就是它（一份简历一个选项），两边必须是同一个答案。
        _root = str(_version.get("root_id") or _version.get("id") or "")
        _cur = resumelib.current_of(_root)
        if _cur is None or str(_cur.get("id") or "") != resume_id:
            raise BadResumeVersion(
                "resume_id=%s 不是「%s」这一族谱的当前版（当前版是 %s）："
                "分析读的是当前版文件（resumes/%s.md），钉到历史版/已移出的版本会让记录里"
                "「这次用的是哪一版」变成假话 —— 拒绝"
                % (resume_id, _got, str((_cur or {}).get("id") or "（没有可用版本）"), _got))

    source_bits = []
    if jd_text:
        source_bits.append("粘贴文本")
    if image_data_url:
        source_bits.append("截图")
    if link:
        source_bits.append("分享链接")

    log.log_event("analyze.start", resume=resume_name, has_text=bool(jd_text), has_image=bool(image_data_url),
              has_link=bool(link), overwrite=overwrite_id, text_model=cfg["text_model"]["model"],
              vision_model=cfg["vision_model"]["model"])
    # 本次调用**刚建**的那条 JD 记录（没建就一直是空串）。异常时按孤儿善后：见
    # `_discard_orphan_jd` —— 它绝不能碰到不是本次建的记录。
    created_jd_id = ""
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

        # ---- 分析前查重（§6）：这里是「拿到 JD 文本」之后的第一行，下面才开始花钱 ----
        # 命中且带 overwrite → 校验通过才继续（分析照跑，结果写回那条记录）；
        # 命中且什么都没带 → DuplicateSubmission（路由回 409，模型与搜索一次都不调）。
        # `allow_duplicate` 是用户已经看过提示后做出的选择：跳过判定（§6 表格第一行之外的那支）。
        if not allow_duplicate:
            dup = find_analyze_duplicate(jd_text, overwrite_id, resume_id)
            if dup is not None:
                raise DuplicateSubmission(dup)

        # 覆盖：沿用那条已有记录的身份（id 与报告文件名都不变）。
        # 「报告文件名 == 原 id」是 §6 的契约（前端拿 id 去 /reports/<id>.html 就行），原始
        # created_at 也留在记录上：覆盖是「同一条记录的新分析」，不是一条新记录 ——
        # 把原始时间换成现在等于篡改台账历史（用户就是靠它认「这份 JD 我 10 月 3 日看过」）。
        overwrite_job = _job_in_ledger(overwrite_id) if overwrite_id else None
        fixed_id = ""
        created_at_kept = ""
        if overwrite_job is not None:
            fixed_id = str(overwrite_job.get("id") or "")
            created_at_kept = str(overwrite_job.get("created_at") or "")
            log.log_event("analyze.overwrite", job=fixed_id, jd_chars=len(jd_text))

        rp = os.path.join(store.p_path("resumes"), store.safe_name(resume_name))
        if not os.path.exists(rp):
            raise RuntimeError("找不到简历：%s" % resume_name)
        resume = store.read_text(rp, "")
        # ⚠️ **"读不出来"绝不是"简历是空的"**（Task 1.10 修 5②）：非 UTF-8 / 被锁的简历文件在
        # `read_text` 里解码失败会被兜成空串，旧实现把这份**空简历**喂给模型 → 报告是照着空简历
        # 写的，用户还以为分析的是自己那份。这里按**原始字节**判：有字节却读不出文本 → 明确失败
        # （路由回 `ok:false` + 这句人话），**一个额度都不花**。
        if not resume and store.content_bytes_present(rp):
            raise RuntimeError("简历「%s」读不出来（不是空的）：文件里有字节，但不是 UTF-8 文本、"
                               "或读不出内容 —— 绝不拿一份空简历去分析。"
                               "请把它另存为 UTF-8 的 .md / .txt 后重试。" % resume_name)
        # 简历的**显示名**（文件名去掉扩展名）：报告数据、分析记录与报告文件名都用它。
        # 与 `resumelib` 里的版本 `name` 同一口径（`create_version` 存的就是这个 stem），
        # 所以界面上"报告用的哪一版"与简历库里的那一版对得上。
        resume_display = os.path.splitext(store.safe_name(resume_name))[0]

        tm = cfg["text_model"]
        # ⚠️ SOP 读不出来**不许静默**（P1-10）：`build_system_prompt` 会把每一处读不出来
        # 收进第二个返回值。这里立刻记一条日志（路由层不用再记），并把列表挂到
        # `do_analyze` 的属性上 —— 路由据此把它放进 `/api/analyze` 的响应体，
        # 用户才看得见"这次分析少了 SOP"。属性而不是返回值：返回值是报告文件名，动它会
        # 影响一堆老调用方（CLI / 测试）。
        system_prompt, sop_warnings = build_system_prompt()
        do_analyze.last_sop_warnings = list(sop_warnings)
        if sop_warnings:
            # 这里只补一条"汇总"日志（每条读不出来的明细由 build_system_prompt 自己记）。
            log.log_event("analyze.sop_missing", level="warn",
                          analysis_sop_warnings=sop_warnings,
                          count=len(sop_warnings),
                          note="本次分析的 system prompt 少了这些 SOP 段，报告质量会受影响")
        messages = [
            {"role": "system", "content": system_prompt},
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
        # 报告数据里同时带**显示名**与**版本 id**（规格 §3）：报告因此能回答"用的是哪一版"。
        # 显示名兼容旧报告（此前 REPORT_DATA 里根本没有 `resume`）；版本 id **只在有时才写** ——
        # 老调用方不传时写一个空串，界面就分不清"老分析"与"没钉上版本"，只能显示"版本未知"。
        d["resume"] = resume_display
        if resume_id:
            d["resume_id"] = resume_id

        # ---- JD 稳定身份（阶段 4A）：分析挂在 JD 下，不再挂在报告文件名下 ----
        # 顺序是契约的一部分：**先定 JD 身份（可能复用已有 JD），再出报告、再写记录** ——
        # `jobs.json` 里的 `jd_id` 必须指向一条已存在的 JD（绝不写悬空引用）。
        #
        # ① 复用还是新建，**由 JD 原文的指纹说了算**（`jobdup` 的判定，§3.6 的同一套算法）：
        #    命中 → 挂到那个已有 JD 下（**不新建**）；未命中 → `next_seq()` + `build_id()` 建新的。
        #    为什么不能只看「字段拼出来的 ID 一样不一样」：同一份 JD 换个说法（多一句职责）
        #    就会拼出另一个 ID —— 那正是本阶段要根除的「同一份 JD 两个身份」。
        jd_base = None
        jd_hit = None
        if not overwrite_id:
            jd_hit = _similar_saved_jd(jd_text)
        if overwrite_id and overwrite_job is not None:
            # 覆盖：沿用那条分析**挂着的 JD**（用户已经明确说了「这次是同一个岗位」）
            jd_id = str(overwrite_job.get("jd_id") or overwrite_job.get("id") or "")
            jd_base = jdid.find_jd(jd_id)
        elif jd_hit is not None:
            jd_id = jd_hit[0]
            jd_base = jdid.find_jd(jd_id)
        else:
            # 归一化后的字段拿来拼 ID（**展示字段另取原值**，见 _jd_record_of）
            jd_id = jdid.build_id(jdid.derive_fields(d), jdid.next_seq())
        if not jd_id:
            raise RuntimeError("无法为这次分析确定 JD 身份")
        log.log_event("analyze.jd_identity", jd=jd_id, reused=jd_base is not None,
                      similarity=round(jd_hit[1], 4) if jd_hit else 0)

        # ---- 知识学习地图（阶段 1 + 阶段 4B：按 JD 缓存，同一岗位只真算一次）----
        # ⚠️ **位置是契约**：必须在「定 JD 身份」**之后**。`jdfacts.resolve` 对不存在的 JD
        # 抛 `LookupError`（在真算之前 —— 否则强制刷新会在一个不存在的岗位上空烧 53 次搜索），
        # 而缓存只能挂在一条真的 JD 记录上。新建 JD 时盘上那条记录还不存在（`next_seq()` 只推
        # 编号、`upsert_jd` 在报告之后），所以这里先落一次身份 —— 后来的那次 `upsert_jd` 是
        # **合并**（同一 id），`jd_facts`（缓存）与 JD 级展示字段各写各的键，谁也冲不掉谁。
        if jd_base is None:
            jdid.upsert_jd({"id": jd_id, "seq": _seq_of_id(jd_id)})
            created_jd_id = str(jd_id)      # 这条是**这次调用建的** → 异常时按孤儿善后

        # 三级回退（规格 §4）：① 缓存 → ② 该 JD 自己旧报告的播种 → ③ 真算（builder）。
        # 真算那一步（= 206 秒 + 53 次搜索）**只有这一个入口**，所以这是本阶段收益的全部来源。
        #
        # 阶段 E：真算那一步里**先查本地知识库**（够格的整条复用、跳过那次联网搜索）。
        # `reuse` 必须在 resolve **之前**建好，并由 `_builder_with_reuse` 用闭包绑进那条
        # 「两个位置参数」的 builder 缝 —— 直接写 `builder=knowledge.build_for_jd` 的话
        # `reuse` 永远是 `None`，这个功能会**静默失效**（见 `_builder_with_reuse`）。
        reuse = kbreuse.Reuse(cfg)
        know_nodes = []
        try:
            _t = time.time()
            know_nodes, know_source = jdfacts.resolve(
                jd_id, jd_text, cfg, refresh=refresh_jd_facts,
                builder=_builder_with_reuse(reuse))
            # 「复用/播种/真算」各自的事件由 jdfacts 记（analyze.knowledge_reused / _seeded /
            # _computed）；这一条留着是为了**耗时**（复用时应接近 0，真算时 206 秒）。
            log.log_event("analyze.knowledge_done", ms=int((time.time() - _t) * 1000),
                          nodes=len(know_nodes), source=know_source)
            # 报告里的来源标记（模板 report_template.html 的 knowMetaLine 读它）。
            # `at` 是**这份缓存生成于**（不是「首次生成于」：用户强制刷新后它就是刷新时间）。
            # ⚠️ **只透传真字符串**（口径与模板的 `typeof m.at === 'string'` 守卫一致）：`str()`
            # 一上那道守卫就形同虚设 —— 手改坏的 `knowledge_at: 5`（用户真会改坏这个文件）会渲染
            # 成「复用该岗位 **5** 生成的那份」，那是句**不真实**的话。非字符串/空 → **不传**
            # 这个键，模板退回「复用该岗位已缓存的那份」（真话）。
            meta = {"reused": know_source != jdfacts.SOURCE_COMPUTED, "source": know_source}
            at = jdfacts.get(jd_id).get("knowledge_at")
            if isinstance(at, str) and at.strip():
                meta["at"] = at
            # 阶段 E 的统计（复用了几条 / 命中几条 / 库读不出来的原因）。
            # ⚠️ 两支**不是同一件事**，报告里不许混：
            #   * `used` 为真 = 这次**真的跑过**"库优先" → 如实带上统计（`stats` 为空 dict
            #     也是"跑了"，那表示"库是空的、复用了 0 条"）；
            #   * `used` 为假 = 这次**根本没跑**库优先 → 只标 `not_run` + **真实来源**。
            #     这时写「复用了 0 条」是**假话**（读报告的人会以为查过库、只是没查到）。
            # ⚠️ **键名不许叫 `cached`**（追加 C）：`used` 为假覆盖三种来源，键名叫 cached
            #     就是说错话 —— `source` 是 `cache`（缓存命中）、`seeded:<报告id>`（从该 JD 的
            #     旧报告**免费播种**）、`computed`（注入的 builder 压根不收 `reuse`）。
            #     前两者的共同真话只有一句：**这次没跑库优先**；到底怎么来的，读 `source`。
            meta["kb_reuse"] = (dict(reuse.stats) if reuse.used
                                else {"not_run": True, "source": know_source})
            d["knowledge_meta"] = meta
        except Exception as e:
            # 明确区分「管线失败」与「真的没有知识点」，避免静默变成空模块。
            # 这里也**不写 knowledge_meta**：模板只在有它时才渲染来源那一行，
            # 「没有地图」时绝不显示一句「复用/新算」的谎。
            log.log_exc("analyze.knowledge_error", e)
            d["knowledge_error"] = str(e)
        d["knowledge"] = know_nodes

        # 覆盖时必须用**固定文件名**（fixed_id = 原 id）：报告文件名与岗位 id 都不变。
        # 走的是与示例报告同一条「固定文件名」路径 —— 不另写一份写盘逻辑（否则两处迟早分叉）。
        fname = render_report(d, fixed_id=fixed_id or None)
        job_id = fname[:-5]

        # ---- 知识库自动沉淀（阶段 3.1）：失败绝不影响出报告 ----
        # kb.absorb 故意不吞 store.save_knowledge 的 I/O 失败（吞掉就等于骗用户「已沉淀」），
        # 所以边界必须在这里：知识库出任何问题都只记日志，报告已经写好了。
        try:
            # 没抽出任何知识点时绝不碰库文件：库文件损坏 + 本次零知识点的组合下，absorb 会
            # 「先隔离坏文件、再写回空库」—— 等于吸收了个寂寞，还把用户已有的积累清空。
            if know_nodes:
                # ⚠️ 这里必须喂 **JD id**（不是报告文件名）：知识库 `from_jds[].id` 指向的是
                # **岗位身份**。喂报告文件名就等于把身份又绑回「日期+分数」上 —— 同一份 JD
                # 分析两次会记成两个岗位，`multi_jd` 又算错（本阶段要修的根因）。
                kb.absorb(know_nodes, {
                    "id": jd_id,
                    "job_title": d.get("job_title") or "",
                    "company": d.get("company") or "",
                    "date": datetime.now().strftime("%Y-%m-%d"),
                })
        except Exception as e:
            log.log_exc("kb.absorb_error", e)

        # 落盘原始 JD（文件名用 **JD id**：同一 JD + 同一简历重跑即覆盖，不会攒出一堆副本）
        jd_text_file, jd_image_url = save_jd_source(jd_id, jd_text, image_data_url)

        # ---- JD 级字段 → `jds.json`（唯一真相源）----
        # 注意：这里写的是 `_jd_record_of(...)` 的**原始展示值**（薪资原文 / 公司全名），
        # 而 ID 里那六段是**归一化后的短值**。两者刻意不同（Task 1 交接注意 1）。
        jd_rec = _jd_record_of(jd_id, d, source_bits, link, jd_text_file, jd_image_url, base=jd_base)
        jd_rec["seq"] = jd_rec.get("seq") or _seq_of_id(jd_id)
        jdid.upsert_jd(jd_rec)

        # ---- 分析级字段 → `jobs.json`（JD 级字段一个都不写进去）----
        rec = {
            "id": job_id,
            "jd_id": jd_id,
            "report": fname,
            # 覆盖：沿用原记录的 created_at（同一条记录的新分析，不是新记录）
            "created_at": created_at_kept or datetime.now().strftime("%Y-%m-%d %H:%M"),
            "score": d["score"],
            "resume": resume_display,
        }
        # `resume_id` **只在真有的时候写**：写空串等于记录里出现一个"看起来钉过版本"的空值，
        # 界面就分不清「老分析（早于版本库）」与「新分析但没给版本号」（规格 §3：老数据显示
        # "版本未知"，绝不猜）。这一行是它**真正落盘**的地方（`_analysis_fields` 只在
        # `ANALYSIS_FIELDS` 里挑键，键不在记录上就写不出任何东西）。
        if resume_id:
            rec["resume_id"] = resume_id
        elif overwrite_job is not None and "resume_id" in overwrite_job:
            # 覆盖**同一条记录**时 `store.upsert_job` 是**合并**：不写这个键，上一次的版本号就会
            # 留在记录上 —— 那等于说"这次分析用的是 rv_0001"，而这次用的可能根本不是它。
            # 宁可如实退回"版本未知"（空值），也绝不背着一句假话（`_duplicate_of_resume` 与
            # 界面都按"空 = 没有版本"处理）。
            rec["resume_id"] = ""
        _save_analysis(rec)
        log.log_event("analyze.success", ms=int((time.time() - t_start) * 1000), repair_rounds=repairs,
                  report=fname, jd=jd_id, job_title=jd_rec["job_title"], score=rec["score"],
                  category=jd_rec["job_category"], verdict=jd_rec["verdict"],
                  contact_found=bool(jd_rec["phone"] or jd_rec["hr_name"] or jd_rec["company"]),
                  source=jd_rec["source_type"], jd_chars=len(jd_text))
        return fname
    except Exception as e:
        # 善后：这次调用为挂靠知识地图缓存**先落的那条 JD 记录**，如果后来什么也没落上
        # （没有 `jd_facts`、也没有分析挂在它下面），就清掉它 —— 否则用户重提同一份 JD 会
        # 永远 409 且前端没有出口（详见 `_discard_orphan_jd` 的代价说明）。
        # `created_jd_id` 为空（不是本次建的）时它什么都不做；**绝不**碰已有记录。
        _discard_orphan_jd(created_jd_id)
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
    jd_id = job_id or DEMO_ID                   # 示例报告的 id 就是它的 JD 身份（固定文件名，不会攒副本）
    jdid.upsert_jd({
        "id": jd_id,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "job_title": d.get("job_title") or "", "company": d.get("company") or "",
        "hr_name": d.get("hr_name") or "", "phone": d.get("phone") or "",
        "address": d.get("address") or "", "job_category": d.get("job_category") or "数据标注/质检",
        "location": d.get("location") or "", "salary": d.get("salary") or "",
        "verdict": d.get("verdict") or "",
        "source_type": "示例数据", "source_link": "",
        "jd_text_file": "", "jd_image": "",
        "status": "未投递", "notes": "示例数据，可删除",
        "seq": _seq_of_id(jd_id),
    })
    _save_analysis({
        "id": job_id, "jd_id": jd_id, "report": fname,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "score": d.get("score") or 0, "resume": "示例简历",
    })
    return fname


# ---------------------------------------------------------------- 面试上传
# 服务端自己生成落盘文件名：客户端文件名只进 status.json 当元数据，**绝不参与拼路径**
UPLOAD_AUDIO_EXTS = (".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus",
                     ".mp4", ".mov", ".mkv", ".webm", ".avi")
UPLOAD_TEXT_EXTS = (".txt", ".md")
UPLOAD_FALLBACK_EXT = ".wav"     # 认不出的扩展名：扩展名只是给 _find_audio 认的，格式由 ffmpeg 按内容探


class ContentLengthReader:
    """只让读 n 个字节的读取器 —— 喂给 store.save_stream 的那个「分块读」。

    为什么必须有它：`BaseHTTPRequestHandler.rfile` 是**套接字上的 BufferedReader**，它的
    `read(k)` 会一直阻塞到凑满 k 字节或对端关闭连接。save_stream 每次要 256KB，而
    Content-Length 只有几十 MB（或者一个小文字稿）时，客户端正等着响应、不会关连接 ——
    于是服务端死等，上传永远卡住（本地 BytesIO 的测试**根本发现不了**：它到末尾就返回短数据）。
    按 Content-Length 精确截断，读到 n 字节即 EOF，才谈得上「分块写盘」。

    chunked 请求拿不到 Content-Length，本工具不支持（前端用 File 当 body，浏览器一定会带）。
    """

    def __init__(self, src, length):
        self.src = src
        self.left = max(0, int(length or 0))

    def read(self, size=-1):
        if self.left <= 0:
            return b""
        want = self.left if (size is None or size < 0) else min(int(size), self.left)
        chunk = self.src.read(want) or b""
        if len(chunk) > want:            # 防呆：不守规矩的 reader 也休想让我们写超过声明的字节数
            chunk = chunk[:want]
        self.left -= len(chunk)
        return chunk


def new_interview_id(job_id):
    """`<YYYYMMDD_HHMM>_<岗位slug>`（规格 §3）；同一分钟里连传两次时加序号。

    不用 uuid / 自增计数：id 要能被人一眼看懂是哪场面试（也是报告的文件名 iv_<id>.html）。
    """
    base = "%s_%s" % (datetime.now().strftime("%Y%m%d_%H%M"), store.slug(job_id))
    iid, n = base, 2
    while os.path.isdir(store.interview_dir(iid)):
        iid = "%s-%d" % (base, n)
        n += 1
    return iid


def upload_kind_of(header_kind, ext):
    """这次上传是音频还是文字稿：X-Kind（audio/text）优先，auto/缺省时按扩展名。"""
    k = str(header_kind or "").strip().lower()
    if k in ("audio", "text"):
        return k
    return "text" if ext in UPLOAD_TEXT_EXTS else "audio"


def upload_filename(kind, original):
    """服务端生成的目标文件名。

    文字稿必须叫 source.txt：`interview.run_pipeline` 对 kind="text" 读的就是这个固定名字
    （改成 source.md 会让文字稿永远分析出空文本，而音频那条路又找不到录音）。
    """
    if kind == "text":
        return "source.txt"
    ext = os.path.splitext(str(original or ""))[1].lower()
    return "audio" + (ext if ext in UPLOAD_AUDIO_EXTS else UPLOAD_FALLBACK_EXT)


def interview_kind_of(st, interview_id):
    """这份记录该跑音频还是文字稿流水线。

    status.json 里的 kind 是上传时写下的真值；**老记录没有这个字段**，只能按目录内容推：
    有 source.txt 就按文字稿 —— 否则默认 audio 会去找音频、报「找不到录音文件，请重新上传」。
    """
    k = str((st or {}).get("kind") or "").strip().lower()
    if k in ("audio", "text"):
        return k
    src = store.interview_file(interview_id, "source.txt")
    return "text" if src and os.path.isfile(src) else "audio"


def cleanup_failed_upload(interview_id, filename=""):
    """上传失败后的清理：删掉这次自己刚写的（半截/空）文件，再删掉空目录。

    只动**这次上传刚创建**的那个文件和目录，绝不碰别的面试记录 —— 用户真实录音在成功路径上
    一律保留（规格 §10：失败保留音频，只清理检测到是半截的上传文件）。
    """
    try:
        f = store.interview_file(interview_id, filename)
        if f and os.path.isfile(f):
            os.remove(f)
        d = store.interview_dir(interview_id)
        if d and os.path.isdir(d) and not os.listdir(d):
            os.rmdir(d)
    except OSError:
        pass


# ---------------------------------------------------------------- HTTP
# 来源校验（P2-1）的文案。**正常使用永远不该看到它**：页面本来就是本机的，
# 所以看到这句话基本就等于"有别的网站在偷偷调用本机服务"（已拦下，什么都没执行）。
FOREIGN_REQUEST_ERROR = (
    "已拒绝：这个请求不是从本机页面（http://127.0.0.1:%d）发出的，所以没有执行任何操作。"
    "如果你正在用本程序，请刷新 http://127.0.0.1:%d 的页面重试；"
    "如果你刚才浏览过别的网页，那很可能是那个网页在偷偷调用本机的求职助手"
    "（想改你的模型地址、或读走你的配置）—— 已经被拦下来了。"
)

# `do_GET` 路由内部**没人接住**的意外：该说哪句人话、记哪个日志事件（批 5 / 体检报告 §7.3 第 1 条）。
#
# 为什么是张表而不是每处自己 `_send(500, …)`：那 7 处各拼一份 500 响应（状态码 + 响应体形状 +
# 日志），写法一样、还漏着 6 个分支。现在**唯一**构造 GET 500 的地方是 `do_GET` 的兜底，
# 路由只负责"说清是哪件事失败"。表里这 7 句与旧实现逐字一致 —— 它们被既有测试钉着
# （`tests/test_kb.py` 断"知识库读取失败"/"复习队列读取失败"，`tests/test_app.py` 断"读取失败"），
# 换句话就是换一句假话，所以不许改字。
# ⚠️ **没有登记的路由走通用那句**：兜底只接"没人接住的意外"，不是把 404 / 409 / 400 也吞进来 ——
# 那些状态码仍由路由自己 `_send`（`test_app.GetOuterTryTest` 逐条钉着）。
_GET_ERROR_SAYS = {
    "/api/resumes": ("简历版本库读取失败：%s", "resume.list_error"),
    "/api/knowledge": ("知识库读取失败：%s", "kb.api_error"),
    "/api/knowledge/review": ("复习队列读取失败：%s", "kb.review_api_error"),
    "/api/jd/purge_preview": ("预演失败：%s", "jd.purge_preview_error"),
    "/api/job/outcome": ("结果读取失败：%s", "outcome.api_error"),
    "/api/interview/status": ("面试状态读取失败：%s", "interview.status_error"),
    "/api/interview/list": ("面试记录列表读取失败：%s", "interview.list_error"),
}
_GET_UNHANDLED_SAY = ("服务器内部错误：%s", "http.get_unhandled_error")


class Handler(BaseHTTPRequestHandler):
    server_version = "AIJobSearch/2.0"

    def log_message(self, fmt, *args):
        pass

    def _local_port(self):
        """本服务**真正在监听**的端口。

        优先取 `self.server.server_address[1]`：真实服务就是 PORT，测试里的临时端口也认得
        （写死 PORT 的话，监听临时端口的用例会因为"端口不对"被误伤）。沙箱 handler 没有
        `server` 属性，取不到就回落到模块级 `PORT`。
        """
        addr = getattr(getattr(self, "server", None), "server_address", None)
        try:
            return int(addr[1])
        except (TypeError, IndexError, ValueError, KeyError):
            return PORT

    def _reject_foreign_request(self):
        """来源校验（P2-1）：`Host` 必须是本机；带了 `Origin` 就必须同源。拒绝时已回 403。

        **为什么必须做在服务端**：任意网页都能对本机服务发 `text/plain` 的**简单请求**
        （不触发 CORS 预检），而 `_json_body` 不看 Content-Type —— 于是
        `POST /api/config {"text_model":{"base_url":"https://attacker/v1"}}` 会**保留用户自己的
        api_key**、只换掉地址，下一次分析就把 key 打到攻击者那里（连回包都不用读）。
        同一条路还能销日志（毁掉取证线索）、写简历；没有 Host 校验时，域名重绑定下连
        `GET /api/config`（明文回 api_key）都开着。

        - `Host`：只认 `127.0.0.1:<port>` / `localhost:<port>`，端口取真正在监听的端口。
          **缺 Host 一律放行**：HTTP/1.0 与命令行客户端不带它（tests/test_kb.py 的沙箱 handler
          也不带），判它非法就会把既有测试和真实用法一起弄红；而 DNS rebinding 的浏览器
          **必定**带 Host（那个 Host 正是攻击者的域名），所以放行缺 Host 不缩小防线。
        - `Origin`：**不带就放行**（命令行 curl、双击 启动.bat 打开的页面都可能没有 Origin），
          带了就必须是本机同源（`http://127.0.0.1:<port>` / `http://localhost:<port>`）。
        - 刻意**不**要求 `Content-Type: application/json`：那不是来源判据，还会误伤
          （tests/test_kb.py 的沙箱 handler 与不少真实调用都不设它）。
        """
        port = self._local_port()
        host = str(self.headers.get("Host") or "").strip().lower()
        origin = str(self.headers.get("Origin") or "").strip().lower()
        allowed_hosts = ("127.0.0.1:%d" % port, "localhost:%d" % port)
        allowed_origins = tuple("http://" + h for h in allowed_hosts)

        if host and host not in allowed_hosts:
            return self._deny_foreign("Host 不是本机", host, origin)
        if origin and origin.rstrip("/") not in allowed_origins:
            return self._deny_foreign("Origin 不是本机页面", host, origin)
        return False

    def _deny_foreign(self, why, host, origin):
        """拒绝一个来源不明的请求：先记一条取证日志，再回 403 + 一句人话。"""
        port = self._local_port()
        try:
            log.log_event("http.foreign_request_rejected", level="warn", reason=why,
                          method=getattr(self, "command", ""), host=host, origin=origin,
                          path=urlparse(getattr(self, "path", "")).path)
        except Exception:
            pass
        self._send(403, {"ok": False, "error": FOREIGN_REQUEST_ERROR % (port, port)})
        return True

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
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(data)
        except Exception:
            pass
        self._after(code)

    def _json_body(self):
        """读 POST 的 JSON body。**"没有 body"与"body 不是合法 JSON"必须分开**（P1-13）。

        旧实现 `except Exception: return {}` 把两者混成同一个 `{}`：`POST /api/config` 于是
        会"当成没改任何东西"照常保存并回 `ok:true` —— 一次假成功，用户以为设置存上了。

        返回 `_NO_BODY_SENTINEL` = 已经自己回过 400/413（调用方必须直接 return，别再往下跑）。

        - 没有 Content-Length / Content-Length 为 0 → `{}`（合法：很多 POST 不带参数）；
        - Content-Length 超过 `MAX_JSON_BODY_BYTES` → 413，**一个字节都不读**（防吃满内存）；
        - 读得出来但不是 UTF-8 / 不是合法 JSON → 400 + 一句人话 + 一条日志。
        """
        raw_length = self.headers.get("Content-Length")
        has_length = raw_length is not None and str(raw_length).strip() != ""
        try:
            n = int(raw_length) if has_length else 0
        except (TypeError, ValueError):
            n = 0
        if n <= 0:
            return {}                       # 没有 body（合法），不是"读不出来"
        if n > MAX_JSON_BODY_BYTES:
            log.log_event("http.json_body_too_large", level="warn",
                          declared_bytes=n, limit=MAX_JSON_BODY_BYTES,
                          path=urlparse(self.path).path)
            self._send(413, {"ok": False, "error":
                             "请求体太大（本次 %d 字节（%s），上限 %d 字节（%s））：没有执行任何操作。"
                             "若是上传的 JD 截图，请先压缩或裁剪小一点再传"
                             "（图片以 base64 进请求体会比原图大约 1/3）。"
                             # ⚠️ **真实字节数必须是整数**：只写 `_size_text()` 时，超限 1 字节
                             # （8388609 vs 8388608）两个数都被抹成 "8.0 MB" —— 同一句话里
                             # 「本次」与「上限」看起来一样大，用户根本不知道超了多少。
                             % (n, _size_text(n), MAX_JSON_BODY_BYTES,
                                _size_text(MAX_JSON_BODY_BYTES))})
            return _NO_BODY_SENTINEL
        raw = self.rfile.read(n)
        if not raw:
            return {}                       # 声明了长度却一个字节都没到：当"没有参数"（对照组）
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as e:
            return self._bad_json_body("请求体不是合法的 UTF-8 文本（第 %d 字节起解不出来：%s）"
                                       % (e.start + 1, e.reason), n)
        try:
            return json.loads(text)
        except ValueError as e:
            return self._bad_json_body("请求体不是合法 JSON：%s" % e, n)

    def _bad_json_body(self, why, n):
        """body 读不出来时的统一出口：记一条日志 + 回 400 + 一句人话，返回哨兵。"""
        log.log_event("http.bad_json_body", level="warn", reason=why, bytes=n,
                      path=urlparse(self.path).path)
        self._send(400, {"ok": False, "error":
                         "%s。这次没有执行任何操作 —— 请检查请求内容后重试"
                         "（常见原因：JSON 有尾随逗号或用了单引号、内容被截断、编码不是 UTF-8）。" % why})
        return _NO_BODY_SENTINEL


    def _send_file(self, path, ctype):
        if not os.path.exists(path) or not os.path.isfile(path):
            return self._send(404, {"error": "not found"})
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(data)
        except Exception:
            pass
        self._after(200)

    # ---- GET
    def do_GET(self):
        """GET 的入口：**唯一的外层兜底**（批 5 / 体检报告 §7.3 第 1 条）。

        三个动词里原先只有 GET 没有外层 try：任何一条路由里漏掉的意外（读盘 OSError、
        被手改坏的数据 TypeError…）都会让服务器**不回任何响应就断开连接** —— 客户端只看到
        RemoteDisconnected，连 500 都拿不到，界面只能永远转圈。这里统一兜成结构化
        500 + 一句人话 + 一条日志（与 `do_POST` / `do_DELETE` 同一口径）。

        ⚠️ **兜底只接"没人接住的意外"**：该 404 / 409 / 400 的仍由路由自己 `_send`，
        一个状态码都没有变（见 `_GET_ERROR_SAYS` 与 `test_app.GetOuterTryTest`）。
        """
        self._t0 = time.time()
        # 来源校验放在**所有路由之前**（含静态文件与 /reports/*、/jd/*）：域名重绑定下
        # 攻击者的页面连读都该读不到（P2-1）。do_POST / do_DELETE 走同一个函数。
        if self._reject_foreign_request():
            return
        u = urlparse(self.path)
        p, q = u.path, parse_qs(u.query)
        try:
            return self._handle_get(p, q)
        except Exception as e:
            # 未预期异常的**唯一**出口：路由自己没接住 = 没人能说清是哪件事，按路由登记的话术说
            say, event = _GET_ERROR_SAYS.get(p, _GET_UNHANDLED_SAY)
            log.log_exc(event, e, path=p, query=u.query[:300])
            return self._send(500, {"ok": False, "error": say % (e,)})

    # ---- GET 的各条只读路由（统一由 `do_GET` 兜底，见它的说明；这里只管路由本身）
    def _handle_get(self, p, q):
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
            # ⚠️ **"读不出来"绝不是"从没配过"**（P1-15 的服务端根源）：`load_config()` 在
            # 任何读/解析失败时回 `DEFAULT_CONFIG`（`api_key=''`、`search_providers=[]`）——
            # 它与"真的没配过"**一模一样**，前端再聪明也无从分辨（用户会以为自己填的 Key 丢了）。
            # 判据用 store 的 strict 读；**500**（服务端读不出自己的配置文件，不是 200）。
            _cfg, cfg_err = store.read_config_strict()
            if _unreadable(cfg_err, store.config_path()):
                log.log_event("config.read_failed", level="warn", error=cfg_err)
                return self._send(500, {"ok": False, "error": cfg_err})
            return self._send(200, store.load_config())

        if p == "/api/resumes":
            # `?name=` 是**老前端**按文件名读正文的路（阶段 4C 起改用
            # `/api/resumes/file?version=<id>`；这条留着不删，读盘是无副作用的）。
            if "name" in q:
                name = store.safe_name(q["name"][0])
                fp = os.path.join(store.p_path("resumes"), name)
                if not os.path.exists(fp):
                    return self._send(404, {"error": "找不到该简历"})
                # ⚠️ **"读不出来"绝不是"文件是空的"**（Task 1.10 修 5①）：旧实现把
                # `store.read_text(fp)` 的空串直接回给前端，而 `editResumeFile` 会把它填进
                # 编辑框 —— 用户一保存就把**真文件覆盖成空的**（非 UTF-8 / 被锁的文件实测过）。
                # 所以先按**原始字节**判"里面有没有东西"：有字节却读不出文本 → 明确报错，
                # 一个字节的内容都不回（前端 `if(!d || d.error) alert(...)` 会把这句显示出来）。
                content = store.read_text(fp, "")
                if not content and store.content_bytes_present(fp):
                    log.log_event("resume.read_failed", level="warn", name=name)
                    # **409**（M-4，Task 1.10b）：这是"盘上那份东西现在的状态与这次读取冲突"
                    # （文件里有字节、但不是 UTF-8 文本），与同轮的姊妹场景（`POST /api/resumes`
                    # 接不住的母版原文 → 409）同一口径。**500 是句假话**：「服务器内部错误」
                    # 会让用户以为是程序坏了，其实是他的文件不是 UTF-8。
                    return self._send(409, {"ok": False, "name": name,
                                            "error": "简历「%s」读不出来（不是空的）：文件里有字节，"
                                                     "但不是 UTF-8 文本、或读不出内容。"
                                                     "为避免你保存时把真文件覆盖成空的，这次不回内容 —— "
                                                     "请把它另存为 UTF-8 的 .md / .txt 再试。" % name})
                return self._send(200, {"name": name, "content": content})
            # 阶段 4C：返回**族谱元数据**（版本列表 + id/name/rev/at/chosen/removed/mother_file/ai_diff）。
            # 默认**不含已移出**（移出 = 不再出现在列表里，规格 §4.6）；要连已移出一起看就带
            # `include_removed=1`。`files` 是磁盘上真实的简历文件名（老前端 / 分析下拉要用），
            # `unregistered` 是**盘上有文件但还没有版本记录**的那些 —— 迁移（Task 5）之前它们
            # 就是用户全部的简历，界面必须看得见，绝不能因为"没有版本记录"就当成不存在。
            include_removed = _as_bool(q.get("include_removed", ["0"])[0])
            # 版本名集合一律取**全量（含已移出）**：`unregistered` 说的是"盘上这个文件
            # **有没有版本记录**"，而"移出列表"只是不再显示/不能继续用 —— 记录还在。
            # 拿过滤后的集合算，会把刚移出的简历报成"没有版本记录"（T3a 据此给"登记"
            # 入口就是错的：用户以为自己的版本记录丢了）。列表本身仍按 include_removed 过滤。
            # （这一支原先自带一份 500 包装；批 5 起由 do_GET 的外层兜底统一构造，见 `_GET_ERROR_SAYS`）
            all_versions = resumelib.list_versions(include_removed=True)
            versions = all_versions if include_removed else [r for r in all_versions
                                                            if not r.get(resumelib.REMOVED)]
            # ⚠️ **"读不出来"绝不是"盘上 0 份简历"**（同类漏网的第 ⑧ 处）：旧写法 `_resume_files()`
            # 拿 `os.path.isdir()` 当闸，目录**存在却读不出来 / 根本不是目录**时静默回 `files: []`
            # —— 而上面 `list_versions()` 那半边的结果**是成功的**，两半自相矛盾（有版本记录、
            # 却说盘上没文件）。判据换成 `/api/reports` 那一套（`FileNotFoundError` 才是"真的没有"）。
            # 状态码选 **500 + ok:false**（不选 `files:null` + error）：`files` 的契约一直是**数组**
            # （前端与老前端直接 `.filter` / `.map`，`unregistered` 也从它算出来），`null` 会让它们
            # 渲染出 `undefined` 或静默少一半 —— 那是把"读不出来"换成另一种假话。
            files, files_err = _resume_files_strict()
            if files_err:
                log.log_event("resume.files_read_failed", level="warn",
                              dir=store.p_path("resumes"), error=files_err)
                return self._send(500, {"ok": False, "error": files_err})
            registered = {str(r.get("name") or "") for r in all_versions}
            return self._send(200, {
                "ok": True,
                "resumes": versions,
                "files": files,
                "unregistered": [f for f in files if os.path.splitext(f)[0] not in registered],
                "include_removed": include_removed,
            })

        if p == "/api/resumes/file":
            # 取回某一版**原文**（规格 §4.5）。四种 kind 各有各的说法（阶段 4C 的诚实边界）：
            #   latest / mother → 原文；missing → **明确说明"原文未保留"**（绝不发一个空文件冒充）；
            #   mismatch → **如实说"内容与记录不符"** + 内容与两个 sha 都亮出来（§8.2）。
            vid = str(q.get("version", [""])[0]).strip()
            if not vid:
                return self._send(400, {"ok": False, "error": "缺少 version（要取哪一版？）"})
            try:
                text, path, kind = resumelib.read_version(vid)
            except ValueError as e:
                return self._send(404, {"ok": False, "error": str(e)})
            rec = resumelib.by_id(vid) or {}
            # ⚠️ Task 1.10c 修 D：`kind` 的 `latest` / `mother` 只说明"按现有依据维持"，
            # **不等于**"这一版的内容核对过" —— 记录里**没有** `sha256` 时盘上那份完全可能
            # 是**别人那一版**（复查枚举出 7 行这种组合）。所以这里多回一个 `verified`
            # （= 记录里**有** `sha256`，也就是"核对过"），让界面能说两种不同的话：
            #   verified 真 → 「最新版原文」；verified 假 → 「这一版没有指纹，内容未核对」。
            # `read_version` 的三元组 API 故意不动（它只回答 `kind`）。
            verified = bool(str(rec.get("sha256") or "").strip())
            out = {
                "ok": kind in ("latest", "mother"),
                "kind": kind,
                "version": vid,
                "verified": verified,
                "name": str(rec.get("name") or ""),
                "rev": rec.get("rev"),
                "at": str(rec.get("at") or ""),
                "path": path,
                # 旧版原文不保留（用户已定的取舍）→ 那一版只有 AI 写的变更说明与当时的分数，
                # 所以这两项**任何 kind 都给**（missing 时它们就是全部信息）。
                "ai_diff": rec.get("ai_diff") or {},
                "ai_diff_error": str(rec.get("ai_diff_error") or ""),
            }
            if kind == "missing":
                # ⚠️ **不返回 text**：原文从来没保留（历史版）是**事实**，但空串会被调用方
                # 当"读到了、只是内容为空"用 —— 这里必须靠一句明确的话分辨（规格 §8.1）。
                out["error"] = ("旧版原文未保留：每个族谱只留母版与最新版原文，"
                                "这一版只有变更说明与当时的分数（绝不假装能取回）")
                return self._send(200, out)
            if kind == "mismatch":
                # 内容照常给（用户要能对账），但**绝不当它是这一版的原文**：记录的 sha 与实测的
                # 都亮出来，让"被程序外改过"这件事无处可藏（规格 §8.2）。
                out["error"] = ("内容与记录不符：磁盘上这份内容不是这一版（可能被程序外修改过，"
                                "或该文件已被别的同名简历覆盖）——不要当作这一版原文使用")
                out["sha256"] = str(rec.get("sha256") or "")
                out["actual_sha256"] = resumelib.sha256_of(text)
                out["text"] = text
                log.log_event("resume.file_mismatch", level="warn", version=vid,
                              name=out["name"], path=path)
                return self._send(200, out)
            out["sha256"] = str(rec.get("sha256") or "")
            out["text"] = text
            return self._send(200, out)

        if p == "/api/reports":
            # ⚠️ **"读不出来"绝不是"0 份报告"**（P1-14 的第 5 处）：旧写法
            # `os.listdir(d) if os.path.isdir(d) else []` 把两种**相反**的情形混成同一个信号 ——
            # "还没有报告目录"（合法，回空）与"有东西但读不了 / 根本不是目录"（必须报错）。
            # 直接 listdir 再分别接异常：`isdir()` 对"没权限 / 被占用"也静默回 False，
            # 只有在这里接 `OSError` 才接得住。
            d = store.p_path("reports")
            try:
                names = os.listdir(d)
            except FileNotFoundError:
                names = []          # 真的还没有报告目录 = 合法的「0 份」，照旧回空
            except OSError as e:
                log.log_event("reports.read_failed", level="warn", path=d, error=str(e))
                return self._send(500, {"ok": False,
                                        "error": "报告目录 `%s` 读不出来：%s —— "
                                                 "这不是「你没有报告」。" % (d, e)})
            out = []
            for f in names:
                if not f.lower().endswith(".html"):
                    continue
                fp = os.path.join(store.p_path("reports"), f)
                out.append({"name": f,
                            "mtime": datetime.fromtimestamp(os.path.getmtime(fp)).strftime("%Y-%m-%d %H:%M")})
            out.sort(key=lambda x: x["mtime"], reverse=True)
            return self._send(200, {"reports": out})

        if p == "/api/jobs":
            # §4：`merged_into` 非空的只是「已合并」标记，不该继续出现在列表里
            # （否则用户台账里会同时看到被并的那条，等于合并没生效）。
            # 阶段 4A：**一行 = 一个 JD** —— JD 级字段来自 `jds.json`，份数/最高分现算。
            # ⚠️ **"读不出来"绝不是"共 0 个岗位"**（P1-14 的服务端根源）：判据见
            # `_ledger_store_error`（store 的 strict 读 + 「盘上真有一个字节可丢吗」）。
            # **500**：服务端读不出自己的台账 —— 与这两条路由"没人接住的意外"既有的 500 同口径。
            # ⚠️ **这条路由只有读**：老代码里那个 `DELETE /api/jobs`（按 id 删记录 + 删文件）**已删**
            # —— 体检 §7.1 判它死（前端一处都没调用），而且它按**分析 id** 去 `load_jobs()` → 改 →
            # `save_jobs()`（跨多次调用的复合读-改-写、无备份、绕开合并/引用同步），现在卡片上的 id
            # 是 **JD id**，传过去会删错或 404。**真删除走 `/api/jd/purge`**（预演 → 二次确认 → 备份）。
            ledger_err = _ledger_store_error()
            if ledger_err:
                log.log_event("jobs.read_failed", level="warn", route=p, error=ledger_err)
                return self._send(500, {"ok": False, "error": ledger_err})
            rows = _ledger_rows()
            return self._send(200, {"jobs": rows, "jds": rows,
                                    "categories": store.CATEGORIES, "statuses": store.STATUSES})

        if p == "/api/jds":
            # JD 列表 + 聚合（份数 / 最高分 / 每份分析）。与 /api/jobs 同一份数据 ——
            # 分开的路径只是因为「JD 库」是新概念，前端要有个明确的入口。
            # 读不出来的判据与状态码同 `/api/jobs`（前端 `loadJobs` 读的就是这一条）。
            ledger_err = _ledger_store_error()
            if ledger_err:
                log.log_event("jobs.read_failed", level="warn", route=p, error=ledger_err)
                return self._send(500, {"ok": False, "error": ledger_err})
            rows = _ledger_rows()
            return self._send(200, {"ok": True, "jds": rows, "jobs": rows,
                                    "total": len(rows),
                                    "categories": store.CATEGORIES, "statuses": store.STATUSES})

        if p == "/api/knowledge":
            def one(k, default=""):
                v = q.get(k)
                return v[0] if v else default
            # 手改坏的 knowledge.json 曾让这里抛 TypeError，服务器直接关连接 —— 客户端拿到的是
            # RemoteDisconnected，连 500 都没有。现在由 do_GET 的外层兜底成结构化 500
            # （话术见 `_GET_ERROR_SAYS`），前端才能显示「知识库读取失败」而不是静默白屏。
            # ⚠️ 但"**读出来是空库**"是另一件事（P1-14 的第 2 处）：`load_knowledge()` 在
            # 读/解析失败时也回 `{"nodes": []}` ⇒ 界面说「共 0 个知识点」。判据用 store 的
            # strict 读（**500**：服务端读不出自己的知识库）；⚠️ 用 `*_strict_deep` ——
            # `{"nodes": [1, 2]}`（元素不是对象）同样必须报，不许被 `kb.list_nodes()` 过滤成空。
            _kb, kb_err = store.read_knowledge_strict_deep()

            if _unreadable(kb_err, store.knowledge_path()):
                log.log_event("kb.read_failed", level="warn", error=kb_err)
                return self._send(500, {"ok": False, "error": kb_err})
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

        if p == "/api/knowledge/review":
            try:
                limit = int(q.get("limit", ["20"])[0])
            except Exception:
                limit = 20
            limit = max(1, min(limit, 200))
            # ⚠️ **"读不出来"绝不是"今天没有要复习的"**：`load_knowledge()` 读/解析失败时照旧回
            # `{"nodes": []}` ⇒ 队列空 + 全 0 ⇒ 界面说「今天没有要复习的」。而这条路由是复习队列的
            # **唯一入口** —— 对着一份读不出来的档案说这句话，就是把用户的卡说成不存在。
            # 判据与 `/api/knowledge` **逐字同源**（store 的 strict 读 + `_unreadable` 那把尺子）：
            # **500**（服务端读不出自己的知识库）；**对照组**照旧 —— 真的空库 / 新档案（文件不存在、
            # 或真的是 0 字节）走 `_unreadable` 的假分支 ⇒ 200 + 空队列，别把它做成一律报错。
            # ⚠️ 与 `/api/knowledge` 一样用 `*_strict_deep`：`{"nodes": [1, 2]}` 必须报，
            # 否则 `kb.review_queue()` 过滤掉坏元素 ⇒ 队列空 + 全 0 ⇒「今天没有要复习的」。
            _kb, kb_err = store.read_knowledge_strict_deep()

            if _unreadable(kb_err, store.knowledge_path()):
                log.log_event("kb.review_read_failed", level="warn", error=kb_err)
                return self._send(500, {"ok": False, "error": kb_err})
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

        if p == "/api/jd_text":
            # JD 原文挂在 **JD** 上（阶段 4A）：id 是 JD id（前端从 /api/jobs 拿到的就是它），
            # 老前端 / 历史链接传分析 id 也认（`_jd_view` 两种都解析）。
            jd, rec = _jd_view(q.get("id", [""])[0])
            if jd is None and rec is None:
                return self._send(404, {"error": "没有保存 JD 原文"})
            path = _jd_text_path(jd) if jd else ""
            if not path or not os.path.exists(path):
                # 老记录 / 还没建 JD 记录：原文就叫 `<id>.txt`
                path = _jd_text_path({"id": (jd or {}).get("id") or (rec or {}).get("id")})
            if not path or not os.path.exists(path):
                return self._send(404, {"error": "没有保存 JD 原文"})
            # ⚠️ **"读不出来"绝不是"没有原文"**（P1-14 的第 4 处）：非 UTF-8 / 被锁的原文
            # 在 `store.read_text` 里会被兜成空串 ⇒ 界面说「没有保存 JD 原文」，
            # 而盘上其实有字节。判据与 `/api/resumes?name=` **逐字同源**（那是既有的先例，
            # Task 1.10b，也是本仓库唯一一次为这件事定过状态码的地方）：
            # **409** —— 盘上那份东西**现在的状态**与这次读取冲突（有字节、但不是 UTF-8 文本）。
            # **500 会是句假话**：「服务器内部错误」让用户以为是程序坏了，其实是他的文件不是 UTF-8。
            text = store.read_text(path, "")
            if not text and store.content_bytes_present(path):
                log.log_event("jd.text_read_failed", level="warn", path=path)
                return self._send(409, {"ok": False,
                                        "error": "JD 原文「%s」读不出来（不是空的）：文件里有字节，"
                                                 "但不是 UTF-8 文本、或读不出内容。"
                                                 "请把它另存为 UTF-8 的 .txt 再试。"
                                                 % os.path.basename(path)})
            return self._send(200, {"text": text})

        if p == "/api/jd/purge_preview":
            # 阶段 D：**真删除**的只读预演（先列清单 + 两次确认里的第一次）。
            # 前端把清单逐条念给用户听 —— 所以这里**只读**、且绝不"算不出就猜"：
            # 读不出的东西都进 `advisories` 如实说。id 与别的写接口同一口径（JD id 优先，
            # 老前端传分析 id / 报告名也认）；参数名收 `jd`（用户可见的契约）与 `id`（别名）。
            jd_id = _purge_target_id(q.get("jd", q.get("id", [""]))[0])
            if not jd_id:
                return self._send(400, {"ok": False, "error": "缺少 id（要删哪个岗位？）"})
            # 预演炸了就由 do_GET 的外层兜底成 500「预演失败：…」（批 5，话术见 `_GET_ERROR_SAYS`）
            preview = jddelete.preview_purge(jd_id)
            if not preview.get("found"):
                # 200 之外唯一正确的一支：404（表里没有这个岗位，**不是**崩）
                return self._send(404, {"ok": False, "id": jd_id,
                                        "error": preview.get("not_found") or "找不到该岗位"})
            # 内部键（`_analyses` / `_files` / …）是 `jddelete` 的内部形状：**不出网**，
            # 前端只看摊平后的那份（多一份内部数据就多一份"前端依赖了不该依赖的东西"的机会）。
            out = {k: v for k, v in preview.items() if not k.startswith("_")}
            log.log_event("jd.purge_preview", jd=jd_id,
                          analyses=preview["summary"]["analyses"],
                          files=preview["summary"]["files"], kb_hits=preview["summary"]["kb_hits"])
            return self._send(200, out)

        if p == "/api/job/outcome":
            # 结果面板的读接口：读回 application（含「是否已复盘」）供前端渲染。
            # 阶段 4A：**JD 级真相在 `jds.json`** —— 这里把该 JD 的记录与分析记录拼成
            # `outcome` 认得的扁平形状（它写回的字段由 POST 那侧分头落盘）。
            jid = store.safe_name(q.get("id", [""])[0])
            if not jid:
                return self._send(400, {"ok": False, "error": "缺少岗位 id"})
            # ⚠️ **"台账读不出来"绝不是"这个岗位不存在"**：`_job_in_ledger` 走 `store.load_jobs()`，
            # 读不出来时回 None ⇒ 旧实现照旧回 **404「找不到岗位：x」** —— 把"读不出来"说成
            # "这个岗位（已经被删了）"，**比回空更糟**。所以先问同一把尺子（`_ledger_store_error`：
            # store 的 strict 读 + `file_has_bytes_to_lose`），读不出来就 **500 + 人话**；
            # **404 只留给"台账读得出来、里面确实没有这个岗位"**（那才是既有的 404 语义）。
            ledger_err = _ledger_store_error()
            if ledger_err:
                log.log_event("outcome.read_failed", level="warn", route=p, error=ledger_err)
                return self._send(500, {"ok": False, "error": ledger_err})
            rec = _job_in_ledger(jid)
            if rec is None:
                # 200 之外唯一正确的一支：404（表里没有这个岗位）
                return self._send(404, {"ok": False, "error": "找不到岗位：%s" % jid})
            try:
                view = _with_job_views(outcome.application_of, jid)
                # has_ai 是给前端的一句话判断：outcome_ai 本身可能被手改坏，别让前端自己 typeof
                has_ai = view.get("outcome_ai") is not None
                return self._send(200, {"ok": True, "id": str(rec.get("jd_id") or jid),
                                        "jd_id": str(rec.get("jd_id") or ""),
                                        "application": view,
                                        "has_ai": has_ai,
                                        "statuses": store.STATUSES,
                                        "terminal": list(outcome.TERMINAL)})
            except LookupError as e:
                # ⚠️ **这一处必须自己接**（批 5 里唯一保留的 GET 包装）：它是 **404**，不是 500 ——
                # 兜底只负责"没人接住的意外"，把它交出去就会变成一句「服务器内部错误」的假话。
                # 其余意外（手改坏的岗位记录 → TypeError…）由 do_GET 的外层兜底统一成 500。
                return self._send(404, {"ok": False, "error": str(e)})

        if p == "/api/interview/status":
            iid = store.safe_name(q.get("id", [""])[0])
            if not iid:
                return self._send(400, {"ok": False, "error": "缺少面试记录 id"})
            st = interview.status(iid)
            if not st:
                return self._send(404, {"ok": False, "error": "找不到该面试记录"})
            return self._send(200, dict(st, ok=True, id=iid,
                                        running=interview.is_running(iid)))

        if p == "/api/interview/list":
            job = store.safe_name(q.get("job", [""])[0])
            rows = []
            for iid in interview.list_interviews(job):
                st = interview.status(iid)
                if st:
                    rows.append(dict(st, id=iid, running=interview.is_running(iid)))
            return self._send(200, {"ok": True, "job": job, "interviews": rows})

        if p == "/api/logs":
            try:
                n = int(q.get("limit", ["150"])[0])
            except Exception:
                n = 150
            page = log.read_logs(n)
            # ⚠️ **"文件存在却读不出来"绝不是"共 0 条日志"**（P1-6）。`LogPage.error` 早就会说这件事，
            # 但这里从没把它带出去 ⇒ 前端只看 `total` ⇒ 界面照旧显示"共 0 条"（那句假话）。
            # 所以**加两个字段**如实带出去（`readable` / `error`），
            # 同时**既有字段的语义一个字不改**（`total` = 能解析的条数、`recent` = 最后 n 条）。
            recent, total = page
            size = os.path.getsize(log.LOG_FILE) if os.path.exists(log.LOG_FILE) else 0
            body = {"version": log.APP_VERSION, "session": log.SESSION_ID,
                    "file": log.LOG_FILE, "total": total, "size": size,
                    "recent": recent, "readable": page.readable, "error": page.error}
            if not page.readable:
                # 与其余路由同一口径：读不出来就说 `ok:false` + 一句人话。
                # **不是 500**（服务器没坏，是那份文件读不出来），也不是"你没有日志"。
                body["ok"] = False
            return self._send(200, body)

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
            self.send_header("X-Content-Type-Options", "nosniff")
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
            self.send_header("X-Content-Type-Options", "nosniff")
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
        if self._reject_foreign_request():
            return
        p = urlparse(self.path).path
        try:
            # 上传必须**先于 _json_body()** 分流：那个函数按 Content-Length 把整个 body 读进
            # 内存（上限 500MB），先读就等于既没有「超限不读 body」也没有「分块写盘」。
            if p == "/api/interview/upload":
                return self._interview_upload()
            return self._handle_post(p)
        except Exception as e:
            log.log_exc("http.post_unhandled_error", e, path=p)
            return self._send(500, {"ok": False, "error": "服务器内部错误：%s" % e})

    def _interview_upload(self):
        """POST /api/interview/upload：`application/octet-stream` + X-Job-Id / X-File-Name。

        顺序是有讲究的：
        ① **先看 Content-Length**：超限立刻 413，**一个字节都不读**（否则 500MB 先灌进内存）；
        ② 岗位 id 必填 —— 不知道挂给谁就没法在台账里找到它、也没法反哺知识库；
        ③ 再分块写盘（store.save_stream，256KB/块），文件名由服务端生成；
        ④ 写 status.json 元数据 → `interview.start()` 起后台线程 → **立刻返回**。
           转写是 0.9× 音频时长的事，同步等只会让浏览器超时。

        客户端文件名既是**不可信输入**（绝不拿它拼路径）又必须 `unquote`（HTTP 头放不下中文，
        前端按 encodeURIComponent 发的），还原后只当元数据存进 status.json。
        """
        raw_length = self.headers.get("Content-Length")
        has_length = raw_length is not None and str(raw_length).strip() != ""
        try:
            length = int(raw_length) if has_length else 0
        except (TypeError, ValueError):
            length = 0
        if length > interview.MAX_UPLOAD_BYTES:
            log.log_event("interview.upload_rejected", level="warn", bytes=length, reason="超限")
            return self._send(413, {"ok": False, "error": "文件超过上限 %d MB"
                                    % (interview.MAX_UPLOAD_BYTES // 1024 // 1024)})
        if not has_length or length < 0:
            # 拿不到长度 = 分块传输（或头部被改坏）。分块不支持，直接说清，别当成空文件
            return self._send(400, {"ok": False,
                                    "error": "缺少（或非法）Content-Length，无法确定要收多少字节"})
        job_id = store.safe_name(unquote(str(self.headers.get("X-Job-Id") or "")))
        if not job_id:
            return self._send(400, {"ok": False,
                                    "error": "缺少 X-Job-Id：不知道这场面试属于哪个岗位"})
        original = unquote(str(self.headers.get("X-File-Name") or "")).strip()
        ext = os.path.splitext(original)[1].lower()
        kind = upload_kind_of(self.headers.get("X-Kind"), ext)
        fname = upload_filename(kind, original)
        iid = new_interview_id(job_id)
        try:
            # 必须按 Content-Length 截断后再交给 save_stream：直接喂 self.rfile 的话
            # 套接字上的 read(256KB) 会死等（详见 ContentLengthReader 的注释）
            written = store.save_stream(iid, fname, ContentLengthReader(self.rfile, length),
                                       interview.MAX_UPLOAD_BYTES)
        except ValueError as e:
            # store.save_stream 抛错时已经删掉半截文件，这里只补目录清理与明确的错误码
            cleanup_failed_upload(iid, fname)
            over = "超过上限" in str(e)          # Content-Length 撒谎时（分块传输）的兜底
            log.log_event("interview.upload_rejected", level="warn", job=job_id, reason=str(e))
            return self._send(413 if over else 400, {"ok": False, "error": str(e)})
        except Exception as e:
            # 客户端中途断开也走这里：半截文件必须清掉，否则下次会被当成有效录音
            cleanup_failed_upload(iid, fname)
            log.log_exc("interview.upload_error", e, job=job_id)
            return self._send(500, {"ok": False, "error": "上传失败：%s" % e})
        if written != length:
            # 客户端中途断了：ContentLengthReader 一旦拿不到声明的字节数就返回 EOF，
            # save_stream 只当正常结束。不比对的话会留下**一份被截断的录音**并报告上传成功，
            # 用户等到最后只看到「转写失败 / 转出来是乱码」。
            cleanup_failed_upload(iid, fname)
            log.log_event("interview.upload_truncated", level="warn", job=job_id,
                          got=written, want=length)
            return self._send(400, {"ok": False, "error": "上传中断：只收到 %d/%d 字节，请重新上传"
                                    % (written, length)})
        if written <= 0:
            # 空文件转写出来必然是空文本：当场说清，别让用户等半小时拿到一份空报告
            cleanup_failed_upload(iid, fname)
            return self._send(400, {"ok": False, "error": "上传的文件是空的（0 字节）"})
        duration = 0.0
        if kind == "audio":
            try:
                # 只为「预计 ≈ N 分钟」这一个提示探测时长（规格 §5）：拿不到/没装 ffprobe/
                # 不是媒体文件一律静默跳过（ffprobe_duration 本身就不抛，这里再兜一层
                # 是为了防注入的探针抛错）—— **绝不因为它阻断上传**。
                duration = interview.ffprobe_duration(store.interview_file(iid, fname))
            except Exception:
                duration = 0.0
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        meta = {"state": "queued", "job_id": job_id, "kind": kind, "original_name": original,
                "stored_name": fname, "audio_bytes": written, "audio_duration": duration,
                "started_at": now, "updated_at": now, "elapsed": 0.0, "error": ""}
        store.save_interview_json(iid, "status.json", meta)
        log.log_event("interview.upload", interview=iid, job=job_id, kind=kind,
                      bytes=written, name=original)
        try:
            # 必须走模块属性（interview.start）而不是 from … import start：测试要能替换它
            interview.start(job_id, iid, store.load_config(), kind=kind)
        except Exception as e:
            # 线程都起不来（极罕见）：状态改成 error，但**录进去的字节一律保留**，可以 retry
            log.log_exc("interview.start_error", e, interview=iid)
            store.save_interview_json(iid, "status.json",
                                      dict(meta, state="error",
                                           error="无法启动后台任务：%s" % e))
            return self._send(500, {"ok": False, "interview_id": iid,
                                    "error": "无法启动后台任务：%s" % e})
        return self._send(200, {"ok": True, "interview_id": iid, "kind": kind,
                                "bytes": written, "state": "queued"})

    def _handle_post(self, p):
        body = self._json_body()
        if body is _NO_BODY_SENTINEL:
            return None            # `_json_body` 已经回过 400/413，绝不继续往下跑

        if p == "/api/log":
            ev = str(body.get("event") or "unknown")
            payload = {k: v for k, v in body.items() if k not in ("event", "level")}
            rec = log.log_event("ui." + ev, level=str(body.get("level") or "info"), data=payload)
            # ⚠️ P1-5：`log_event` **写不进盘时回 `None`**（磁盘满 / 被占用 / 没有写权限 /
            # data 不可序列化）。旧写法把返回值丢掉、照旧回 `ok:true` —— 于是"这条 UI 日志
            # 根本没落盘"变成一句**看不见的假话**（调用方拿它当"已记录"，取证线索悄悄没了）。
            # 选 **`ok:false` + 一句人话**，不加新字段：前端 `logUI` 只发不看回包，
            # 而全仓其余失败分支的出口都是 `ok` + `error` —— 多造一个 `logged` 等于再立
            # 一个"没人读的信号"，那正是本仓反复栽过的那种假绿。
            if rec is None:
                return self._send(200, {"ok": False,
                                        "error": "这条日志没有写进盘（日志文件被占用 / 没有写权限 / "
                                                 "磁盘满）：你的这次操作本身没有受影响，"
                                                 "但这条记录丢了。"})
            return self._send(200, {"ok": True})

        if p == "/api/logs/bundle":
            try:
                name = log.build_log_bundle(PORT)
                return self._send(200, {"ok": True, "file": name})
            except Exception as e:
                log.log_exc("logs.bundle_error", e)
                return self._send(200, {"ok": False, "error": str(e)})

        if p == "/api/logs/clear":
            # P1-11：清空失败**不许**回 ok:true（前端只看 ok，会 toast「日志已清空」，
            # 而取证线索其实还在/或压根没清掉 —— 两种情况都必须如实说）。
            # 6.5：清空 = 当前文件 **+ 所有存档段**。只截断当前文件的话，`read_logs`
            # 仍会把存档里的旧记录读回来，「已清空」就是一句假话。
            # ⚠️ 成功时**不再**写 `logs.cleared`：那会立刻在刚清空的日志里留下一行，
            # 界面（`GET /api/logs`）随即显示「共 1 条」—— 删了什么由响应里的计数如实带回。
            res = log.clear_all_logs()
            if not res["ok"]:
                log.log_event("logs.clear_failed", level="error", error=res["error"],
                              archives_removed=len(res["archives_removed"]), file=log.LOG_FILE)
                return self._send(200, {
                    "ok": False,
                    "archives_removed": len(res["archives_removed"]),
                    "cleared_current": res["cleared_current"],
                    "error": "清空日志失败：%s —— 日志没有被完整清空，请检查这些文件是否被占用"
                             "或没有写权限（日志文件：%s）" % (res["error"], log.LOG_FILE)})
            removed = len(res["archives_removed"])
            return self._send(200, {
                "ok": True,
                "archives_removed": removed,
                "cleared_current": res["cleared_current"],
                "message": "已清空：当前日志 + %d 个存档段" % removed})

        if p == "/api/config":
            # ⚠️ **字段类型错不许静默忽略**（P1-13 的兄弟）：旧写法 `if isinstance(body.get(k), dict)`
            # 对类型错的字段**直接跳过** ⇒ 照常 `save_config` + `ok:true` —— 用户看到
            # 「设置已保存 ✓」，而他发上来的那部分**一个字段都没写进去**（又一句"看起来成功了"）。
            # 选 **(A) 400 拒绝、一个字节都不写盘**（不是"存能认的 + 报告被忽略的"）：
            #   ① 与 P1-13 同一口径（body 读不出来 → 400，绝不"当成没改任何东西"照常保存）；
            #   ② `save_config` 是**整份覆盖**，"存一半"会让用户以为整份都生效了 ——
            #      半个成功比一个失败更难查（本仓最贵的一课）；
            #   ③ 这份 body 的形状**只可能来自手写 / 出错的调用方**，正常前端一直发的是
            #      `{text_model:{…}, vision_model:{…}, search_providers:[…]}`。
            type_err = _config_type_error(body)
            if type_err:
                log.log_event("config.save_rejected", level="warn", error=type_err)
                return self._send(400, {"ok": False, "error": type_err})
            # ⚠️ **同一份坏 `config.json`，GET 与 POST 不许两套话术**（问题 4）：
            # 旧写法直接 `load_config()` → `save_config()`，让它的 `RuntimeError` 掉进
            # `do_POST` 的兜底 ⇒ 用户看到「服务器内部错误：配置 `config.json` 读不出来：
            # <内部异常原文>。**拒绝保存设置**：…」——① 把**他自己文件**的状态说成
            # "服务器内部错误"（与 `/api/jd_text` 那条论证同源：500 会是句假话）；
            # ② 把内部异常原文当用户话术（细节该进日志，不该进界面）。
            # 现在提前问**同一把尺子**（`_unreadable` = store 的 strict 读 + 盘上真有字节吗），
            # 出口与 GET /api/config 同源：**500 + ok:false + 一句人话 + 一条日志**；
            # 磁盘**一个字节都不动**（这一步只是读一次，不写盘）。
            _cfg_now, cfg_err = store.read_config_strict()
            if _unreadable(cfg_err, store.config_path()):
                log.log_event("config.save_rejected", level="warn", error=cfg_err,
                              reason="配置读不出来，拒绝保存（盘上没动）")
                return self._send(500, {"ok": False, "error": _CONFIG_UNREADABLE_SAVE_SAY})
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
            try:
                store.save_config(cfg)
            except RuntimeError as e:
                # 兜底：上面那次判据与这次写盘之间文件又坏了（别的进程 / 杀毒软件锁住 / 手改）
                # —— `save_config` 的前置检查（同一把尺子）会拒绝写盘。用户看到的仍是**那句
                # 定制话术**（不带内部原文），内部原文只进日志。
                log.log_event("config.save_rejected", level="warn", error=str(e),
                              reason="save_config 的前置检查没通过（判据与写盘之间的竞态）")
                return self._send(500, {"ok": False, "error": _CONFIG_UNREADABLE_SAVE_SAY})
            log.log_event("config.save", changed=changed, text_model=cfg["text_model"]["model"],
                          vision_model=cfg["vision_model"]["model"],
                          text_key_set=bool(cfg["text_model"]["api_key"]),
                          vision_key_set=bool(cfg["vision_model"]["api_key"]),
                          search_count=len(cfg.get("search_providers") or []))
            return self._send(200, {"ok": True})

        if p == "/api/resumes":
            # 保存/上传一份简历（规格 §4.1/4.2）。**必须走 `resumelib.create_version`**：
            # 老实现是 `store.write_text` 直接截断覆盖 —— 没有版本记录、没有母版快照、没有 sha，
            # 报告里"用的是哪一版"从此无从查起（本阶段要根除的那条路）。
            try:
                rec = save_resume_version(body.get("name"), body.get("content") or "",
                                          parent_id=body.get("parent_id") or "",
                                          lineage=body.get("lineage") or "")
            except ValueError as e:
                log.log_event("resume.save_rejected", level="warn", name=body.get("name"),
                              error=str(e))
                return self._send(400, {"ok": False, "error": str(e)})
            except resumelib.SnapshotArchiveError as e:
                # 「没能给（旧）快照留档 → 中止本次保存」是 `resumelib.create_version` 抛出的
                # **专用**异常（Task 1.10b 修 M-9）。它的性质是**冲突**而不是"服务器内部错误"：
                # 目标文件**当前的状态**（被别的程序占用 / 读不出来）与这次保存冲突 → **409**，
                # 而且**只**把那句人话给用户。
                # ⚠️ **必须只 catch 这一种**：这里用裸 `RuntimeError` 的话，将来 `ai_diff_of`
                # 之类的地方冒出任何 `RuntimeError`，都会把**已经成功落盘**的一次保存
                # 报成 409「已中止」= 又一句用户可见的假话（Task 1.10b 修 M-9）。
                log.log_event("resume.save_conflict", level="warn", name=body.get("name"),
                              error=str(e), cause=str(getattr(e, "cause", "") or ""))
                return self._send(409, {"ok": False, "error": str(e)})
            return self._send(200, {"ok": True, "name": rec.get("name"), "resume": rec,
                                    "id": rec.get("id"), "rev": rec.get("rev"),
                                    "parent_id": rec.get("parent_id")})

        if p == "/api/resumes/pdf":
            # 电子版 PDF → 文字（**只读**：不写盘、不留副本、不改任何记录）。
            # 保存仍然走 `POST /api/resumes`（用户先看见、可改，点「保存」才登记版本）——
            # 这条路由只回答"这份 PDF 里写着什么"，绝不替用户决定"那就存了吧"。
            name = str(body.get("name") or "").strip()
            if not name.lower().endswith(".pdf"):
                return self._send(400, {"ok": False, "error":
                                        "只接 PDF：%r 不是 .pdf 文件（Word 文档请先另存为 PDF，"
                                        "或直接另存为 .md / .txt）" % (name or "（没有名字）")})
            b64 = body.get("b64")
            if not isinstance(b64, str) or not b64.strip():
                return self._send(400, {"ok": False, "error":
                                        "没有收到 PDF 内容（b64 为空）—— 请重新选择文件"})
            try:
                raw = base64.b64decode("".join(b64.split()), validate=False)
            except (ValueError, TypeError):
                return self._send(400, {"ok": False, "error":
                                        "上传内容不是合法的 base64（文件可能在上传时被截断）"})
            if not raw:
                return self._send(400, {"ok": False, "error":
                                        "没有收到 PDF 内容（解出来是 0 字节）—— 请重新选择文件"})
            if len(raw) > PDF_MAX_BYTES:
                return self._send(400, {"ok": False, "error":
                                        "PDF 太大（本次 %s，上限 %s）：简历不需要这么大，"
                                        "请先压缩或只导出简历那几页再传"
                                        % (_size_text(len(raw)), _size_text(PDF_MAX_BYTES))})
            try:
                got = pdftext.extract_text(raw)
            except pdftext.PdfError as e:
                # 读不出来 = **用户能修的问题**（扫描件/加密/坏文件）⇒ 400 + 原话
                log.log_event("resume.pdf_rejected", level="warn", name=name,
                              error=str(e)[:300])
                return self._send(400, {"ok": False, "error": str(e)})
            except Exception as e:                      # 解析器自己的 bug：500，绝不假装是文件问题
                log.log_exc("resume.pdf_extract_error", e)
                return self._send(500, {"ok": False, "error": "解析 PDF 时出错：%s" % e})
            stem = os.path.splitext(store.safe_name(name))[0].strip()
            log.log_event("resume.pdf_loaded", name=stem, pages=got.pages,
                          chars=got.chars, warnings=len(got.warnings))
            return self._send(200, {"ok": True, "name": stem, "text": got.text,
                                    "pages": got.pages, "chars": got.chars,
                                    "warnings": list(got.warnings)})

        if p in ("/api/resumes/choose", "/api/resumes/restore", "/api/resumes/purge"):
            # 三个「改一条版本记录」的动作。它们**只动 `resumes.json`**（purge 只删自己那份快照）：
            # 简历文件与别人的文件一律不碰（规格 §4.6 的反假绿要求）。
            vid = str(body.get("version") or "").strip()
            if not vid:
                return self._send(400, {"ok": False, "error": "缺少 version（要操作哪一版？）"})
            fn = {"choose": resumelib.choose, "restore": resumelib.restore,
                  "purge": resumelib.purge}[p.rsplit("/", 1)[1]]
            try:
                rec = fn(vid)
            except ValueError as e:
                msg = str(e)
                # 「找不到」= 404（那条记录不存在）；「名字被占用」= 409（冲突，用户改名后重试）；
                # 其余（已移出的不能选为母版…）= 400。
                status = 404 if "找不到" in msg else (409 if "占用" in msg else 400)
                log.log_event("resume.%s_rejected" % p.rsplit("/", 1)[1], level="warn",
                              version=vid, status=status, error=msg)
                return self._send(status, {"ok": False, "error": msg})
            log.log_event("resume.%s" % p.rsplit("/", 1)[1], version=vid, name=rec.get("name"))
            return self._send(200, {"ok": True, "version": vid, "resume": rec,
                                    "name": rec.get("name")})

        if p == "/api/jobs/update":
            # 走有效台账：已合并的记录在列表里看不到，写接口也不能悄悄改它
            # （改一条「不存在」的记录 = 用户以为改了，台账里却什么都没有）。
            # 阶段 4A：**改的是 JD 记录**（`jds.json`）—— 这里列的字段全是 JD 级，
            # 写回 jobs.json 就会和 jds.json 各存一份（本阶段要根除的坑）。
            jid = store.safe_name(body.get("id"))
            jd, rec = _jd_view(jid)
            # 已合并的分析不算「台账里还有这个岗位」（`rec` 为 None）：不能悄悄改一条
            # 用户看不到的记录 —— 那等于「用户以为改了，台账里却什么都没有」。
            if jd is None or rec is None:
                return self._send(404, {"ok": False, "error": "找不到该岗位记录"})
            jd_id = str((jd or {}).get("id") or (rec or {}).get("jd_id") or jid)
            jd = dict(jd) if isinstance(jd, dict) else {"id": jd_id}
            jd["id"] = jd_id
            fields = [k for k in ("company", "hr_name", "phone", "address", "job_category",
                                  "status", "notes", "source_link", "job_title") if k in body]
            for k in fields:
                jd[k] = str(body.get(k) or "")
            if jd.get("job_category") not in store.CATEGORIES:
                jd["job_category"] = "其他"
            saved = jdid.upsert_jd(jd)
            log.log_event("job.update", id=jd_id, title=saved.get("job_title"),
                      fields=fields, status=saved.get("status"))
            return self._send(200, {"ok": True, "job": _jd_row_of(jd_id) or saved})

        if p == "/api/jd/id":
            # 改 ID：重名 → **409** 并给出错误说明（规格 §4）；改成功后 `jdid.rename_jd`
            # 会把 4 处引用（jds.json / 知识库 from_jds / 面试记录 job_id / jobs.json.jd_id）
            # 一起改过去，失败自动回滚，**报告文件永不重命名**。
            old_id = str(body.get("old_id") or "").strip()
            new_id = str(body.get("new_id") or "").strip()
            if not old_id or not new_id:
                return self._send(400, {"ok": False, "error": "old_id 与 new_id 都要给"})
            if jdid.find_jd(old_id) is None:
                return self._send(404, {"ok": False, "error": "JD 库里没有这个 ID：%s" % old_id})
            err = jdid.validate_id(new_id, jdid.load_jds()["jds"], exclude_id=old_id)
            if err:
                # 「占用」= 重名 → 409；其余（段数 / 空格 / 非法字符）→ 400。判据是任务 1
                # 约定好的错误说明串，不另造一套校验（两套迟早分叉）。
                taken = "占用" in err
                log.log_event("jd.id_rejected", level="warn", old=old_id, new=new_id,
                              status=409 if taken else 400, reason=err)
                return self._send(409 if taken else 400, {"ok": False, "error": err})
            try:
                saved_id = jdid.rename_jd(old_id, new_id)
            except Exception as e:
                msg = str(e)
                log.log_exc("jd.rename_error", e, old=old_id, new=new_id)
                return self._send(409 if "占用" in msg else 400, {"ok": False, "error": msg})
            log.log_event("jd.id_changed", old=old_id, new=saved_id)
            return self._send(200, {"ok": True, "id": saved_id, "old_id": old_id,
                                    "job": _jd_row_of(saved_id)})

        if p == "/api/jd/purge":
            # 阶段 D：**真删除**（不可恢复是它的语义）。前端必须已经跑过
            # `GET /api/jd/purge_preview` 并**两次确认**；`confirm_fingerprint` 就是
            # 用户确认的那份清单的指纹 —— 对不上（预演之后数据被改过）→ 409 拒绝执行，
            # 绝不"按旧清单删一份新数据"。
            jd_id = _purge_target_id(body.get("id") or body.get("jd"))
            if not jd_id:
                return self._send(400, {"ok": False, "error": "缺少 id（要删哪个岗位？）"})
            try:
                res = jddelete.purge_jd(jd_id,
                                        expect_fingerprint=str(body.get("confirm_fingerprint") or ""))
            except jddelete.JdPurgeError as e:
                msg = str(e)
                code = str(getattr(e, "code", "") or "")
                # 状态码取异常上的 **code**（不解析文案）：not_found → 404；
                # stale / unreadable → 409（当前状态不允许按这份清单删，重试也没用，
                # 得先把数据修好）；备份/删除/回滚失败 → 500。
                status = {"not_found": 404, "stale": 409, "unreadable": 409}.get(code, 500)
                log.log_event("jd.purge_rejected", level="warn", jd=jd_id,
                              status=status, code=code, error=msg)
                # `code` 与 `backup` 都回给前端：前端的三种失败要求完全不同 ——
                # `backup_failed`（一个字节都没动）、`failed`（已回滚）、
                # `rollback_failed`（**数据可能不是原样，要人工从 backup 恢复**）。
                # 混成一句话就会出现"已回滚：回滚也失败了"这种自相矛盾的提示。
                return self._send(status, {"ok": False, "id": jd_id, "code": code,
                                           "backup": str(getattr(e, "backup", "") or ""),
                                           "error": msg})
            log.log_event("jd.purge_done", jd=jd_id, backup=os.path.basename(res["backup"]),
                          files=len(res["removed_files"]), analyses=len(res["analyses"]),
                          skipped=len(res["skipped_files"]))
            # ⚠️ 这里**刻意不算**「刷新后的台账」（体检 §6 的"白算"）：`_ledger_rows()` 的每一行
            # 都要把该 JD 的**每条分析**的报告打开、解析出逐维分数（`_analysis_scores` →
            # `kb.read_report_data`）—— 一次删除成功之后，对着一批**刚被删掉**的报告名去读盘。
            # 而它在响应里唯一的去处是 `jobs`，前端**一个字节都不读**（`web/index.html:1180`
            # 在成功分支上直接 `loadJobs()` 重新拉 `/api/jobs`）。所以这份台账算完就被丢掉：
            # 白读 N 份 HTML、白解析 N 次。删除语义本身一个字节都没动，只是**少做无用功**。
            return self._send(200, {"ok": True, "id": jd_id, "backup": res["backup"],
                                    "backup_note": res["backup_note"],
                                    "removed_files": res["removed_files"],
                                    "skipped_files": res["skipped_files"],
                                    "analyses": res["analyses"],
                                    "kb_nodes": res["kb_nodes"],
                                    "interviews": res["interviews"],
                                    # 批 7.2：这次顺带清掉了几条悬空合并引用（预演里已列过）。
                                    # 它们清完之后重新成为台账里的一行 —— 刷一次 `loadJobs()` 就能看见。
                                    "merged_refs": res["merged_refs"]})

        if p == "/api/job/result":
            # 记录结果：用户的字一个不改地存下来（转义是模板的事）。status 非法 → 400。
            # 阶段 4A：结果属 **JD 级**（你在一个岗位上只投一次）→ 写 `jds.json`。
            jid = store.safe_name(body.get("id"))
            try:
                # status 原样传进去（不做 safe_name/去空白）：词表比对必须是**精确**的，
                # 顺手 trim 会把「已拒 」这种本该报错的输入悄悄改成合法的
                view = _with_job_views(outcome.save_result, jid, body.get("status"),
                                       body.get("reason"))
                return self._send(200, {"ok": True, "id": jid, "application": view})
            except LookupError as e:
                return self._send(404, {"ok": False, "error": str(e)})
            except ValueError as e:
                return self._send(400, {"ok": False, "error": str(e)})

        if p == "/api/job/attribute":
            # 归因是**同步**的（一次模型调用，与 JD 分析同量级）：直接调，不做后台任务。
            # do_POST 已有外层 try（见 do_POST 的 except），但那样只能给一句「服务器内部错误」；
            # 这里的用例需要 404 / 400 / 500 各自的语义，所以逐类接住。
            jid = store.safe_name(body.get("id"))
            if not jid:
                return self._send(404, {"ok": False, "error": "找不到岗位：（空 id）"})
            try:
                rec = _job_in_ledger(jid)
                if rec is None:
                    return self._send(404, {"ok": False, "error": "找不到岗位：%s" % jid})
                ai = _with_job_views(outcome.attribute, jid, store.load_config())
                # 复盘报告以 **JD id** 命名：`rename_jd` 改完 ID 也找得到（报告文件本身永不改名，
                # 但这份「按岗位定位」的报告名跟的是身份 —— 身份变了就该定位到新的那份）。
                # `render_report` 内部也读台账补「你写的原因」→ 同样要接视图（否则报告里是空的）。
                report = _with_job_views(outcome.render_report, ai, str(rec.get("jd_id") or jid))
                return self._send(200, {"ok": True, "id": jid, "outcome_ai": ai,
                                        "report": report, "url": "/reports/" + quote(report)})
            except LookupError as e:
                return self._send(404, {"ok": False, "error": str(e)})
            except outcome.NotTerminal as e:
                # 文案直接用异常的 str(e)：里面那句「还没有结果」是判门拒绝的唯一说法
                return self._send(400, {"ok": False, "error": str(e)})
            except ValueError as e:
                return self._send(400, {"ok": False, "error": str(e)})
            except RuntimeError as e:
                # 模型 JSON 修不动：报告不生成，用户写的 outcome_reason 早在保存时就落盘了
                log.log_exc("outcome.attribute_failed", e, job=jid)
                return self._send(500, {"ok": False, "error": "归因失败：%s" % e})
            except Exception as e:
                # 兜底（do_POST 的外层 try 只能给一句「服务器内部错误」）：
                # 上面四类之外的东西（岗位记录被改坏 → KeyError/TypeError）也要给前端一句人话 + 500。
                log.log_exc("outcome.attribute_error", e, job=jid)
                return self._send(500, {"ok": False,
                                        "error": "归因失败（%s）：%s" % (type(e).__name__, e)})

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
            failed = []                 # 每一项都是「用户点了但没存上」的如实说明
            for k, v in checks.items():
                st = store.load_report_state(rid)
                st["checks"][str(k)] = bool(v)
                if store.save_report_state(rid, st["checks"]):
                    saved += 1
                else:
                    # 以前这里**默默不计**：勾选落盘失败时界面照样显示「已保存 ✓」。
                    failed.append("勾选 `%s` 没落盘（%s 写不进去：目录不可写 / 磁盘满？）"
                                  % (k, store.report_state_dir()))
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
                else:
                    if state not in kb.STATE_VALUES:
                        why = "知识点状态 %r 不是合法值（合法值：%s）" % (state, "、".join(kb.STATE_VALUES))
                    elif not kb.normalize_id(term):
                        why = "术语 %r 归一化后是空的（它建不出卡，勾会丢）" % (term,)
                    else:
                        # 第三轮加了"库读不出来"两档（拒绝 / 留档失败）；第四轮又加了第三档
                        # 「读得出来、但有元素不是对象」（拒绝、**不隔离**）。所以这里不能
                        # 再断言"留档也失败" —— 那句话对元素坏是假话（压根没去留档），
                        # 而用户会照着它去找一个并不存在的 `.corrupt-*`。
                        why = ("知识库这次没有写入：`knowledge.json` 读不干净"
                               "（整个读不出来，或者里面有一条不是对象）。文件一个字节都没动"
                               "—— 修好库之后再点一次这个勾（原因也记在日志 kb.write_refused）")
                    failed.append("知识点 `%s`（%s）没写进知识库：%s" % (term, state, why))
            log.log_event("report_state.write", report=rid, saved=saved, created=created,
                          failed=len(failed))
            if failed:
                # ⚠️ **有失败就绝不回 `ok: True`**：前端（web/report_template.html:292 的
                # `if(!r || r.ok !== true) throw new Error(r.error || '写入失败')`，再由
                # `applyCheck` 的 catch 回滚勾选框并告示）只认 `ok !== true`。
                # 老写法在这里**无条件**回 ok:True，于是勾没存上也显示「已保存 ✓」——
                # 假 ok 比报错更糟：用户以为存上了，关掉程序就没了。
                detail = "；".join(failed[:3])
                if len(failed) > 3:
                    detail += "；…（另有 %d 项，见日志 report_state.write）" % (len(failed) - 3)
                return self._send(200, {
                    "ok": False, "saved": saved, "created": created,
                    "failed": len(failed),
                    "error": "有 %d 项没存上（本次存上 %d 项）：%s" % (len(failed), saved, detail)})
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

        if p == "/api/interview/retry":
            iid = store.safe_name(body.get("id"))
            if not iid:
                return self._send(400, {"ok": False, "error": "缺少面试记录 id"})
            st = interview.status(iid)
            if not st:
                return self._send(404, {"ok": False, "error": "找不到该面试记录"})
            # 「从失败的那一步开始」不用在这里判断：run_pipeline 见到 transcript.json 就跳过转写。
            kind = interview_kind_of(st, iid)
            job_id = str(st.get("job_id") or "")
            running = interview.is_running(iid)
            try:
                # 已在跑时 interview.start 会复用那条线程（单飞），不会重复烧机器
                interview.start(job_id, iid, store.load_config(), kind=kind)
            except Exception as e:
                log.log_exc("interview.retry_error", e, interview=iid)
                return self._send(500, {"ok": False, "error": "重试失败：%s" % e})
            log.log_event("interview.retry", interview=iid, job=job_id, kind=kind,
                          already_running=running)
            return self._send(200, {"ok": True, "id": iid, "kind": kind, "reused": running,
                                    "state": "running" if running else "queued"})

        if p == "/api/knowledge/add":
            term = str(body.get("term") or "").strip()
            if not kb.normalize_id(term):
                return self._send(400, {"ok": False, "error": "术语不能为空（只有标点也算空）"})
            meta = {"definition": str(body.get("definition") or "").strip(),
                    "category": str(body.get("category") or "").strip()}
            try:
                # 手动入库 = 用户已经做出的判断：直接进复习队列（「待学习」），
                # 且**绝不**用报告里的短解释覆盖卡上更全的内容（upsert_check 内已保证）
                ok, created, card = kb.upsert_check(term, kb.DEFAULT_STATE, meta)
            except Exception as e:
                log.log_exc("kb.add_error", e)
                return self._send(500, {"ok": False, "error": "入库失败：%s" % e})
            if not ok or not isinstance(card, dict):
                # 术语在上一道闸门已经验过（空 / 只有标点都 400 了），所以走到这里的
                # `ok=False` 只剩"库读不干净"这一档 —— 不许再说成"术语不能为空"（假话）。
                return self._send(400, {
                    "ok": False,
                    "error": "知识库这次没有写入：`knowledge.json` 读不干净"
                             "（整个读不出来，或者里面有一条不是对象）。文件一个字节都没动"
                             "—— 修好库之后再点一次"})
            log.log_event("kb.add_manual", id=card.get("id"), term=term, created=created)
            return self._send(200, {"ok": True, "created": created,
                                    "id": card.get("id"), "node": card})

        if p == "/api/analyze":
            # 查重命中 → **409**（语义明确：这不是「分析失败」，是「要不要覆盖」的选择题）。
            # `allow_duplicate: true` = 用户在弹窗里看过代价后选了「仍然新建」：跳过查重，
            # 照常跑分析并**新建**一条记录（前端选「新建」时会重发一次请求带这个标记）。
            # ⚠️ 这个 except 必须排在下面那个宽 except 之前：DuplicateSubmission / BadOverwrite
            # 都是 RuntimeError，落进宽 except 就会变成 200 + 「分析失败：这份 JD 与已有岗位
            # 20261003_… 重复」—— 前端拿不到 duplicate 字段，弹窗永远出不来。
            try:
                fname = do_analyze(body.get("jd_text"), body.get("image"),
                                   body.get("resume_name"), body.get("link"),
                                   overwrite_id=body.get("overwrite") or "",
                                   # 两个布尔一律过 `_as_bool`：手写 JSON 里的 `"false"` / `"0"`
                                   # 绝不能被 `bool()` 当**真**（那会白烧 206 秒 / 悄悄跳过查重）
                                   allow_duplicate=_as_bool(body.get("allow_duplicate")),
                                   # 用户想更新知识地图时带 true → 忽略缓存重算（透传给 jdfacts.resolve）
                                   refresh_jd_facts=_as_bool(body.get("refresh_jd_facts")),
                                   # 这次用的是哪一版简历（`resumelib` 的版本 id，如 rv_0001）：
                                   # 查重的另一半粒度 + 落进分析记录（阶段 4C）。老前端不传 → 空串。
                                   resume_id=body.get("resume_id") or "")
                return self._send(200, {"ok": True, "report": fname,
                                        # SOP 少了一段就把这件事**带回前端**（P1-10）：
                                        # 只躺在日志里等于用户永远不知道。
                                        "sop_warnings": list(getattr(do_analyze, "last_sop_warnings", []) or [])})
            except DuplicateSubmission as e:
                # 命中的凭据是 (JD × 简历版本)，日志里也带上版本号：事后才查得出"为什么判了重复"
                log.log_event("analyze.duplicate", level="warn", job=e.duplicate.get("job_id"),
                              resume_id=e.duplicate.get("resume_id"),
                              similarity=round(e.duplicate.get("similarity") or 0, 4))
                return self._send(409, {"ok": False, "duplicate": e.duplicate})
            except BadOverwrite as e:
                # 前端传错 id：结果**没有**被写进任何一条记录，必须说清（不能只回一句「重复」）
                log.log_event("analyze.bad_overwrite", level="warn", error=str(e))
                return self._send(400, {"ok": False, "error": str(e)})
            except BadResumeVersion as e:
                # 传错的版本 id：同上 —— 「哪一版」这句话绝不能靠猜（修 4）
                log.log_event("analyze.bad_resume_version", level="warn", error=str(e),
                              resume_id=str(body.get("resume_id") or ""))
                return self._send(400, {"ok": False, "error": str(e)})
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
        if self._reject_foreign_request():
            return
        try:
            return self._handle_delete()
        except Exception as e:
            # 兜底与 `do_POST` 同一写法：没有它，一次写盘失败（`resumelib.remove` →
            # `store.write_json` 会抛）就是**连接裸断** —— 前端只看到网络错误，
            # 既不知道"移出"成没成，也拿不到一句人能看懂的话（修 6）。
            log.log_exc("http.delete_unhandled_error", e, path=urlparse(self.path).path)
            return self._send(500, {"ok": False, "error": "服务器内部错误：%s" % e})

    def _handle_delete(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/api/resumes":
            # 阶段 4C：**「删除」= 移出列表，不是删文件**（规格 §4.6）。
            # 老实现 `os.remove` 是**永久删、无副本**；现在文件、母版快照、变更说明全部保留，
            # 只是不再出现在列表里（也不能再用于分析）。彻底删是另一个显式动作
            # （`POST /api/resumes/purge` + 前端二次确认）。
            vid = str(q.get("version", [""])[0]).strip()
            if not vid:
                # 老前端按**文件名**删：解析 + 移出**在同一临界区**里完成（`resumelib` 内持锁，
                # 修 7）—— 两步之间插进一次并发保存，就会移出**错的那一版**。
                try:
                    rec = resumelib.remove_current_of(q.get("name", [""])[0])
                except ValueError as e:
                    return self._send(404, {"ok": False, "error": str(e)})
                vid = str(rec.get("id") or "")
            else:
                try:
                    rec = resumelib.remove(vid)
                except ValueError as e:
                    return self._send(404, {"ok": False, "error": str(e)})
            log.log_event("resume.remove", version=vid, name=rec.get("name"))
            return self._send(200, {"ok": True, "removed": rec, "name": rec.get("name"),
                                    "note": "已移出列表；简历文件与母版快照仍留在盘上"})
        # ⚠️ `DELETE /api/jobs` 已**删除**（体检 §7.1 判死；grep 证据：`web/index.html`、
        # `web/report_template.html` 与全仓非 tests 代码里**没有任何**调用，也没有别处用它的
        # `keep_files` 参数）。不动它的理由见 `GET /api/jobs` 那段注释：**真删除走 `/api/jd/purge`**
        # （预演 → 二次确认 → 备份），而它按分析 id 做跨多次调用的复合读-改-写、无备份。
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
    # 前置检查必须在**任何**动作之前（包括端口探测）：档名坏了的时候，用户要看到的
    # 永远是「去修那个文件」，而不是「端口被占用」，更不是一坨 traceback。
    # import 期已经查过一遍，这里是第二道 —— 档名文件可能在 import 之后才坏掉。
    _err = profile_precondition_error()
    if _err:
        _abort_startup(_err)
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
