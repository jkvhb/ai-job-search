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

import jdid
import jobdup
import kb
import knowledge
import interview
import llm
import log
import outcome
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
INTERVIEW_TEMPLATE_PATH = os.path.join(WEB, "interview_template.html")   # 面试复盘报告版式
OUTCOME_TEMPLATE_PATH = os.path.join(WEB, "outcome_template.html")       # 结果复盘报告版式
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
    tpl = store.read_text(tpl_path)
    if not tpl:
        return None
    if tpl_path == INTERVIEW_TEMPLATE_PATH:
        _attach_transcript(data)
    title = _report_title(path) or os.path.basename(path)
    # 拼装方式必须与 render_report() 完全一致，否则同一份数据会渲染出两种页面
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return tpl.replace("__TITLE__", title).replace("__REPORT_DATA__", payload)


def render_report(d, fixed_id=None):
    # 写盘与 serve 共用同一份 kind 判定：否则磁盘上那份会是与用户看到的不同的骨架
    tpl_path = report_template_for(d)
    tpl = store.read_text(tpl_path)
    if not tpl:
        raise RuntimeError("缺少报告模板：%s" % os.path.basename(tpl_path))
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
    store.write_text(os.path.join(store.p_path("reports"), fname), html)
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
ANALYSIS_FIELDS = ("id", "jd_id", "resume", "score", "report", "created_at", "merged_into")

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


def _jd_row(jd_id, records):
    """一行 = 一个 JD：JD 记录全字段 + 分析聚合（份数 / 最高分 / 每份分析）。

    ⚠️ `id` = **JD id**（不是分析 id）：前端拿它去 `/api/jd_text`、`/api/job/outcome`、
    `/api/job/result`、`X-Job-Id`，这些现在都以 JD 为身份。分析级字段（`report`/`resume`/
    `score`/`jd_text_file`）逐份挂在 `analyses` 里，**不**冒充 JD 字段。
    """
    jd = jdid.find_jd(jd_id) or {}
    rows = sorted(records, key=lambda r: (str(r.get("created_at") or ""), str(r.get("id") or "")))
    analyses = [{
        "id": str(r.get("id") or ""),
        "resume": str(r.get("resume") or ""),
        "score": r.get("score"),
        "report": str(r.get("report") or ""),
        "created_at": str(r.get("created_at") or ""),
    } for r in rows]
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


def find_analyze_duplicate(jd_text, overwrite_id=""):
    """分析前查重（§6）。**只读**：不写任何文件、不调模型、不调搜索。

    - 返回 `None`  → 照常分析（未命中 / 带了 allow_duplicate / overwrite 已校验通过）
    - 抛 `BadOverwrite`      → `overwrite` 不可用（不存在 / 已合并 / 不相似）→ 400
    - 抛 `DuplicateSubmission` → 命中且没带任何选择 → 409

    `overwrite_id` 非空时必须**真的**是相似的那一条（用 jobdup 复核一次）：
    否则前端传错 id 就会把这次分析的结果写到一条**无关岗位**上。

    **命中的粒度是 JD**（阶段 4A）：返回的 `job_id` / `jd_id` 都是**已有 JD 的 id** ——
    前端拿它当 `overwrite` 发回来就走「沿用原身份」那一支，不会再算成一个新岗位。
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
    jd = jdid.find_jd(jd_id) or {}
    rows = _analysis_records_of(jd_id)
    latest = rows[-1] if rows else {}
    return {
        # `job_id` 留着是为了老前端（阶段 3.6 的弹窗读它）；两者现在都是 **JD id**
        "job_id": jd_id,
        "jd_id": jd_id,
        "job_title": str(jd.get("job_title") or ""),
        "created_at": str(latest.get("created_at") or jd.get("created_at") or ""),
        "score": latest.get("score") if latest else jd.get("score"),
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


def do_analyze(jd_text, image_data_url, resume_name, link="", overwrite_id="", allow_duplicate=False):
    """跑一次 JD 分析并出报告。`overwrite_id` 非空 = 「覆盖」：结果写回那条已有记录。

    `allow_duplicate` 为真 = 用户已在查重提示里选过「仍然新建」→ **跳过查重**，照常新建。

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
            dup = find_analyze_duplicate(jd_text, overwrite_id)
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
            "resume": os.path.splitext(store.safe_name(resume_name))[0],
        }
        _save_analysis(rec)
        log.log_event("analyze.success", ms=int((time.time() - t_start) * 1000), repair_rounds=repairs,
                  report=fname, jd=jd_id, job_title=jd_rec["job_title"], score=rec["score"],
                  category=jd_rec["job_category"], verdict=jd_rec["verdict"],
                  contact_found=bool(jd_rec["phone"] or jd_rec["hr_name"] or jd_rec["company"]),
                  source=jd_rec["source_type"], jd_chars=len(jd_text))
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
            # §4：`merged_into` 非空的只是「已合并」标记，不该继续出现在列表里
            # （否则用户台账里会同时看到被并的那条，等于合并没生效）。
            # 阶段 4A：**一行 = 一个 JD** —— JD 级字段来自 `jds.json`，份数/最高分现算。
            rows = _ledger_rows()
            return self._send(200, {"jobs": rows, "jds": rows,
                                    "categories": store.CATEGORIES, "statuses": store.STATUSES})

        if p == "/api/jds":
            # JD 列表 + 聚合（份数 / 最高分 / 每份分析）。与 /api/jobs 同一份数据 ——
            # 分开的路径只是因为「JD 库」是新概念，前端要有个明确的入口。
            rows = _ledger_rows()
            return self._send(200, {"ok": True, "jds": rows, "jobs": rows,
                                    "total": len(rows),
                                    "categories": store.CATEGORIES, "statuses": store.STATUSES})

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
            return self._send(200, {"text": store.read_text(path, "")})

        if p == "/api/job/outcome":
            # 结果面板的读接口：读回 application（含「是否已复盘」）供前端渲染。
            # 阶段 4A：**JD 级真相在 `jds.json`** —— 这里把该 JD 的记录与分析记录拼成
            # `outcome` 认得的扁平形状（它写回的字段由 POST 那侧分头落盘）。
            jid = store.safe_name(q.get("id", [""])[0])
            if not jid:
                return self._send(400, {"ok": False, "error": "缺少岗位 id"})
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
                return self._send(404, {"ok": False, "error": str(e)})
            except Exception as e:
                # do_GET **没有外层 try**（见 /api/knowledge 的注释）：手改坏的岗位记录会让
                # application_of 抛出来，服务器直接关连接 —— 前端只看到 RemoteDisconnected，
                # 连 500 都拿不到，只能永远转圈。必须在这里兜成结构化错误。
                log.log_exc("outcome.api_error", e, job=jid)
                return self._send(500, {"ok": False, "error": "结果读取失败：%s" % e})

        if p == "/api/interview/status":
            iid = store.safe_name(q.get("id", [""])[0])
            if not iid:
                return self._send(400, {"ok": False, "error": "缺少面试记录 id"})
            try:
                st = interview.status(iid)
                if not st:
                    return self._send(404, {"ok": False, "error": "找不到该面试记录"})
                return self._send(200, dict(st, ok=True, id=iid,
                                            running=interview.is_running(iid)))
            except Exception as e:
                # do_GET **没有外层 try**（见 /api/knowledge 的注释）：不兜底的话服务器直接关连接，
                # 前端只看到 RemoteDisconnected —— 连 500 都拿不到，只能永远转圈。
                log.log_exc("interview.status_error", e, interview=iid)
                return self._send(500, {"ok": False, "error": "面试状态读取失败：%s" % e})

        if p == "/api/interview/list":
            job = store.safe_name(q.get("job", [""])[0])
            try:
                rows = []
                for iid in interview.list_interviews(job):
                    st = interview.status(iid)
                    if st:
                        rows.append(dict(st, id=iid, running=interview.is_running(iid)))
                return self._send(200, {"ok": True, "job": job, "interviews": rows})
            except Exception as e:
                # 同上：do_GET 必须自己兜底，否则连接裸断
                log.log_exc("interview.list_error", e, job=job)
                return self._send(500, {"ok": False, "error": "面试记录列表读取失败：%s" % e})

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
                return self._send(400, {"ok": False, "error": "术语不能为空（只有标点也算空）"})
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
                                   allow_duplicate=body.get("allow_duplicate"))
                return self._send(200, {"ok": True, "report": fname})
            except DuplicateSubmission as e:
                log.log_event("analyze.duplicate", level="warn", job=e.duplicate.get("job_id"),
                              similarity=round(e.duplicate.get("similarity") or 0, 4))
                return self._send(409, {"ok": False, "duplicate": e.duplicate})
            except BadOverwrite as e:
                # 前端传错 id：结果**没有**被写进任何一条记录，必须说清（不能只回一句「重复」）
                log.log_event("analyze.bad_overwrite", level="warn", error=str(e))
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
