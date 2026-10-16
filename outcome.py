# -*- coding: utf-8 -*-
"""offer 结果记录与复盘归因（闭环最后一步）。

设计要点：
  * 用户的 outcome_reason 与 AI 的 outcome_ai **分开存** —— 报告里先显示人写的，再显示机器推的，
    混在一个字段里就再也分不清哪句是谁说的
  * 归因必须带 evidence；没有依据的条目**直接丢弃**，宁可这一节空着并如实说明，也不填泛泛之谈
  * to_study 只列不建卡（新词由用户手动入库 —— 用户明确要求，怕知识库被灌爆）
  * 同步执行：归因是一次模型调用（几秒到几十秒），与 JD 分析同量级，不需要后台任务
  * 模型可注入：`chat(prompt, cfg)`（与 interview.analyze 同一约定）—— 测试绝不真调模型

状态词表**已经存在**（`store.STATUSES`），本模块绝不另造一套；`TERMINAL` 只是它的子集。
"""
import os
from datetime import datetime

import kb
import llm
import log
import store

# 只有到终态才谈得上归因（"还没投递/还在面"时归因纯属瞎猜）。**必须是 store.STATUSES 的子集。**
TERMINAL = ("已拒", "Offer", "放弃")

# 喂进模型的条数上限：归因不该因为一份超长 JD 或十场面试而把上下文撑爆
MAX_ITEMS = 10
MAX_QUESTIONS = 8
MAX_FOCUS = 6
# JD 原文进提示词的字数上限（JD 拆解要点另行结构化喂入，这里只是补原文语气）
MAX_JD_CHARS = 2000

# 提示词：要求一次输出那 6 段 JSON（复盘报告模板直接吃这份结构）
_DIRECTIVES = """你是资深 AI 领域求职复盘教练。求职者已经知道了这个岗位的最终结果，现在要复盘「为什么是这个结果」。

只输出 JSON（不要 markdown 代码块、不要解释），结构如下：
{"verdict_summary":"一段总评：这次最可能是输在哪（150~300 字，说人话）",
 "possible_reasons":[{"reason":"可能的原因","evidence":"引用面试复盘或 JD 里的具体依据","confidence":"高|中|低","controllable":true}],
 "not_controllable":["HC 取消、薪资不匹配、内部竞争者…"],
 "actions":[{"area":"简历|表达|知识|投递策略","action":"具体到可执行的一句话","why":"依据"}],
 "to_study":[{"term":"要补的知识点术语","why":"为什么该补","definition":"一句大白话解释"}],
 "next_time":"同类岗位下次怎么调（投递策略/简历侧重/准备重点）"}

硬性要求：
- possible_reasons 的每一条**必须带 evidence**，且要引用**具体内容**（某个被问到的题、某个答砸的点、
  JD 里的某条要求）。禁止「可能沟通能力不足」这种没有依据的话。
- 没有依据的原因宁可不写：报告会如实说明这一节为空，绝不用泛泛之谈把它填满。
- controllable 必须逐条标注 true/false（可控/不可控分开是这份复盘有没有用的分水岭）。
- 没有面试记录时只依据 JD 与求职者自己写的原因判断，**不要假装看到了面试过程**。
- to_study 只列出知识点，**不要写入知识库**（入不入库由用户自己决定）。
- 字符串内部禁止英文双引号（用中文引号「」），禁止裸换行。
- 全部中文；依据里没有的内容不要编造。"""


class NotTerminal(Exception):
    """还没到终态就要求归因。"""


# ---------------------------------------------------------------- 容错（照 interview 的 _normalise 风格）
def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _as_str(v):
    """容错成字符串：None/布尔 → ''（它们不是文本），数字 → 文本（模型爱把分数写成数字）。"""
    if v is None or isinstance(v, bool):
        return ""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, (int, float)):
        return str(v)
    return ""


def _as_list(v):
    return v if isinstance(v, list) else []


def _as_line(v):
    """一行文本：裸字符串直接用；模型偶尔写成 {"reason": "…"} 也要能读出来（别丢掉一条真原因）。"""
    if isinstance(v, dict):
        for k in ("reason", "item", "text", "name", "why"):
            s = _as_str(v.get(k))
            if s:
                return s
        return ""
    return _as_str(v)


def _as_bool(v, default=None):
    """可控/不可控的容错解析：**读不出来就是 None（未判断）**。

    绝不默认成 True：那等于在没有依据的情况下宣称「这事你能控制」—— 与「没有 evidence 的原因
    要丢弃」是同一类错误。三态语义：True 可控 / False 不可控 / None 未判断；
    报告模板应把 None 单列成「未判断」，不要混进「可控」。
    """
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    s = _as_str(v).lower()
    if s in ("true", "yes", "y", "1", "可控", "是"):
        return True
    if s in ("false", "no", "n", "0", "不可控", "否"):
        return False
    return default


def _as_score(v):
    """匹配分：读不出来就给 0.0（app.render_report 的 "%g" 要求它是数字）。"""
    try:
        return round(float(v or 0), 1)
    except (TypeError, ValueError):
        return 0.0


def _clean_status(v):
    """只认 store.STATUSES 里的值；非法/垃圾类型一律返回空串（由调用方决定退回什么）。"""
    s = _as_str(v)
    return s if s in store.STATUSES else ""


def _require_job(job_id):
    """岗位必须存在：不存在 → LookupError（路由映射 404）。"""
    jid = _as_str(job_id)
    job = store.find_job(jid) if jid else None
    if not isinstance(job, dict):
        raise LookupError("找不到岗位：%s" % (jid or job_id))
    return job


def _raw_application(job):
    """岗位记录里的 application 子字典（**未归一的副本**，写回时不丢别的键）。"""
    app = job.get("application")
    return dict(app) if isinstance(app, dict) else {}


def _recorded_status(job):
    """用户**记录过**的状态：台账顶层优先，`application` 镜像兜底；从没记过/非法 → ""。

    判门**只看这个值**（严格版）："" 不在 TERMINAL 里，所以「没填」与「填了未投递」一样被拦。
    `application_of` 显示的默认值「未投递」只用于展示，绝不参与判门 —— 否则一条从没记过结果的
    记录会被放行去归因，AI 就会给一个「结果未知」的岗位写「你为什么没拿到 offer」。
    """
    app = _raw_application(job)
    return _clean_status(job.get("status")) or _clean_status(app.get("status"))


def _view_of(job):
    """application 的只读视图：补齐默认值、纠正垃圾类型，绝不抛异常。

    状态以**台账顶层 status 为准**：真实 jobs.json 是扁平的（`/api/jobs`、按状态筛选、
    行内徽章读的都是它），`application.status` 是镜像。反过来（子字典优先）的话，
    用户在台账里改了状态就再也盖不回去了。
    """
    app = _raw_application(job)
    ai = app.get("outcome_ai")
    return {
        "status": _recorded_status(job) or "未投递",
        "next_followup": _as_str(app.get("next_followup")),
        "notes": _as_str(job.get("notes")) or _as_str(app.get("notes")),
        "outcome_at": _as_str(app.get("outcome_at")),
        "outcome_reason": _as_str(app.get("outcome_reason")),
        "outcome_ai": ai if isinstance(ai, dict) else None,     # 垃圾类型 → None，绝不崩前端
    }


# ---------------------------------------------------------------- 结果存取
def application_of(job_id):
    """读该岗位的 application 并补默认值；岗位不存在 → LookupError；垃圾类型一律纠正。"""
    return _view_of(_require_job(job_id))


def save_result(job_id, status, reason, when=None):
    """写 status / outcome_reason / outcome_at。

    status 不在 store.STATUSES → ValueError；岗位不存在 → LookupError（路由分别映射 400 / 404）。
    只写这几个键：**绝不碰 outcome_ai**（那是 AI 的地盘），也不动 next_followup/notes 等已有字段。
    """
    job = _require_job(job_id)
    if status not in store.STATUSES:                 # 词表只有一份：store.STATUSES
        raise ValueError("状态不合法：%s（只能是 %s）" % (status, "、".join(store.STATUSES)))
    app = _raw_application(job)
    app["status"] = status
    app["outcome_reason"] = _as_str(reason)          # 用户的字一个不改地存下来
    app["outcome_at"] = _as_str(when) or _now()
    job["application"] = app
    # 顶层 status 也同步：它是岗位台账的真相（/api/jobs、按状态筛选、行内徽章都读它）。
    # 不镜像的话，复盘面板里刚存的「已拒」在台账里还是「未投递」——同一件事两个说法。
    job["status"] = status
    job = store.upsert_job(job)
    log.log_event("outcome.result_saved", job=job.get("id"), status=status,
                  has_reason=bool(app["outcome_reason"]))
    return _view_of(job)


def can_attribute(job_id):
    """只有到终态才谈得上归因 —— **严格版，没有例外**。

    判据只有一个：`_recorded_status(job) in TERMINAL`。状态**缺失或非法（不在词表里）也一律拦**，
    当作非终态。

    为什么不留「没记过状态就放行」的缝：真实台账里每个岗位都有 status，但畸形 / 手改 / 遗留记录
    没有。放行那种记录，等于让 AI 给一个「从未记录过结果」的岗位写「你为什么没拿到 offer」——
    正是本设计要防的「没有依据却言之凿凿」。用户看到「还没有结果」→ 去记录结果 → 才能归因，
    这恰好就是「先记结果，再复盘」的流程顺序（计划原稿与这条冲突，按裁定采用严格语义）。
    """
    return _recorded_status(_require_job(job_id)) in TERMINAL


def _not_terminal_message(job):
    """「为什么不能归因」必须说**真实**情况：绝不谎报一个用户从没填过的状态。"""
    status = _recorded_status(job)
    if not status:
        return ("这个岗位还没有结果：没有记录过状态（空的或不在词表里）。"
                "请先在「🏁 结果」里记录结果，到终态（%s）后再归因。" % "／".join(TERMINAL))
    return ("这个岗位还没有结果（当前状态：%s）。只有到了 %s 才谈得上归因。"
            % (status, "／".join(TERMINAL)))


# ---------------------------------------------------------------- 归因素材（喂模型 = 报告里的「原始依据」）
def _jd_facts(job):
    """JD 要点：复盘报告里的报告数据（拆解/打分/硬门槛）+ 原始 JD 文本。

    报告读不出来（老报告、被删、格式变了）时**静默降级**，只用台账里那几个字段，绝不崩。
    """
    d = {}
    report = store.safe_name(job.get("report"))
    if report:
        rd = kb.read_report_data(os.path.join(store.p_path("reports"), report))
        if isinstance(rd, dict):
            d = rd
    gates = []
    for g in _as_list(d.get("hard_gates"))[:MAX_ITEMS]:
        if not isinstance(g, dict):
            continue
        line = "：".join([x for x in (_as_str(g.get("name")), _as_str(g.get("jd"))) if x])
        res = _as_str(g.get("result"))
        if line:
            gates.append(line + ("（%s）" % res if res else ""))
    scores = []
    for s in _as_list(d.get("scores"))[:MAX_ITEMS]:
        if not isinstance(s, dict):
            continue
        dim, basis = _as_str(s.get("dimension")), _as_str(s.get("basis"))
        if dim:
            scores.append("%s %s%s" % (dim, _as_str(s.get("score")),
                                       ("：" + basis) if basis else ""))
    gaps = []
    for g in _as_list(d.get("gaps"))[:MAX_ITEMS]:
        if not isinstance(g, dict):
            continue
        skill, verdict = _as_str(g.get("skill")), _as_str(g.get("verdict"))
        if skill:
            gaps.append("%s%s" % (skill, ("（%s）" % verdict) if verdict else ""))
    f = store.safe_name(job.get("jd_text_file"))
    return {
        "job_profile": _as_str(d.get("job_profile")),
        "hard_gates": gates,
        "scores": scores,
        "gaps": gaps,
        "keywords": [_as_line(k) for k in _as_list(d.get("keywords"))[:MAX_ITEMS] if _as_line(k)],
        "jd_text": store.read_text(os.path.join(store.p_path("jd"), f))[:MAX_JD_CHARS] if f else "",
    }


def _interview_facts(job_id):
    """该岗位名下所有面试复盘的要点（按记录 id 排序 —— 顺序必须确定且可重复）。

    只认 status.json 里 job_id 对得上的记录：喂进别的岗位的面试 = 把别人的面试当成你的。
    记录被删/analysis.json 损坏 → 少喂一块（规格 §6 的静默降级），绝不崩。
    """
    root = store.interviews_dir()
    if not job_id or not os.path.isdir(root):
        return []
    out = []
    for name in sorted(os.listdir(root)):
        if not os.path.isdir(store.interview_dir(name)):
            continue
        st = store.load_interview_json(name, "status.json", {})
        if not isinstance(st, dict) or _as_str(st.get("job_id")) != job_id:
            continue
        ana = store.load_interview_json(name, "analysis.json", {})
        ana = ana if isinstance(ana, dict) else {}
        weak = [_as_str(w.get("term")) for w in _as_list(ana.get("weak_concepts"))
                if isinstance(w, dict) and _as_str(w.get("term"))][:MAX_ITEMS]
        questions = []
        for q in _as_list(ana.get("questions"))[:MAX_QUESTIONS]:
            if not isinstance(q, dict) or not _as_str(q.get("q")):
                continue
            questions.append({"q": _as_str(q.get("q")),
                              "answer_quality": _as_str(q.get("answer_quality")),
                              "comment": _as_str(q.get("comment"))})
        focus = []
        for x in _as_list(ana.get("focus_areas"))[:MAX_FOCUS]:
            if not isinstance(x, dict) or not _as_str(x.get("area")):
                continue
            focus.append({"area": _as_str(x.get("area")), "share": _as_str(x.get("share"))})
        out.append({"id": name, "weak_concepts": weak, "questions": questions, "focus_areas": focus})
    return out


def _facts_of(job, view):
    return {"status": view.get("status") or "", "reason": view.get("outcome_reason") or "",
            "jd": _jd_facts(job), "interviews": _interview_facts(_as_str(job.get("id")))}


def _build_prompt(job, facts):
    """用户提示：岗位背景 + JD 拆解 + 面试复盘 + **求职者自己写的原因** + 那 6 段的结构要求。"""
    jd = facts.get("jd") or {}
    head = _as_str(job.get("job_title")) or "（未命名岗位）"
    cat, company = _as_str(job.get("job_category")), _as_str(job.get("company"))
    if cat:
        head += "（%s）" % cat
    if company:
        head += " @ %s" % company
    bits = ["【岗位】%s" % head,
            "【当前状态】%s" % (facts.get("status") or ""),
            "【匹配分】%g" % _as_score(job.get("score"))]
    if jd.get("job_profile"):
        bits.append("【岗位画像】%s" % jd["job_profile"])
    for label, key in (("【硬门槛】", "hard_gates"), ("【匹配打分】", "scores"),
                       ("【能力差距】", "gaps"), ("【JD 高频关键词】", "keywords")):
        if jd.get(key):
            bits.append(label + "\n- " + "\n- ".join(jd[key]))
    if jd.get("jd_text"):
        bits.append("【JD 原文（节选）】\n%s" % jd["jd_text"])
    ivs = facts.get("interviews") or []
    if ivs:
        for i, iv in enumerate(ivs, 1):
            lines = ["【面试复盘 %d（%s）】" % (i, iv.get("id") or "")]
            if iv.get("weak_concepts"):
                lines.append("- 答砸/答不上来的知识点：" + "、".join(iv["weak_concepts"]))
            if iv.get("focus_areas"):
                lines.append("- 考察重点：" + "、".join(
                    "%s%s" % (x["area"], ("（%s%%）" % x["share"]) if x.get("share") else "")
                    for x in iv["focus_areas"]))
            for q in iv.get("questions") or []:
                detail = "：".join([x for x in (q.get("answer_quality"), q.get("comment")) if x])
                lines.append("- 被问到：%s%s" % (q["q"], ("（%s）" % detail) if detail else ""))
            bits.append("\n".join(lines))
    else:
        bits.append("【面试复盘】（没有面试记录：只依据 JD 与求职者自己写的原因判断，"
                    "不要假装看到了面试过程）")
    reason = facts.get("reason") or ""
    bits.append("【求职者自己写的原因】%s" % (reason if reason else "（你没有填写原因）"))
    bits.append("请按上面的 JSON 结构输出这次结果的归因。")
    return "\n\n".join(bits)


# ---------------------------------------------------------------- 模型调用（可注入）
def _call_model(chat, cfg, system, user):
    """注入约定：`chat(prompt, cfg)`。模型细节（base_url/key/model/temperature）在这里收口。

    这样测试里传个 `lambda prompt, cfg: '…'` 就能整条链路跑通，而真实现走 llm 的 OpenAI
    兼容调用 —— **绝不把网络细节漏进 attribute 的业务逻辑里**。
    """
    text = (system or "") + "\n\n" + (user or "")
    tm = (cfg or {}).get("text_model") or {}
    if chat is not None:
        return chat(text, cfg)
    return llm.call_openai_compatible(tm.get("base_url") or "", tm.get("api_key") or "",
                                     tm.get("model") or "", [{"role": "user", "content": text}],
                                     json_mode=True, temperature=tm.get("temperature"))


def _parse_reply(reply, cfg):
    """模型回复 → dict。解析不动就先让 llm 的 JSON 修复器修一次，还不行就报清楚。"""
    if isinstance(reply, dict):
        return reply
    try:
        return llm.parse_json_reply(reply)
    except Exception as e:
        first = e          # 不能写成 `except … as first`：except 块结束时会解绑那个名字
    tm = (cfg or {}).get("text_model") or {}
    try:
        return llm.parse_json_reply(llm.repair_json(tm, reply, first))
    except Exception as second:
        raise RuntimeError("模型没有返回可解析的归因结果：%s（自动修复也失败：%s）"
                           % (first, second))


# ---------------------------------------------------------------- 归一化（缺段补默认值，绝不 KeyError）
def _clean_row(item, required):
    """单条容错：裸字符串补成 {required: 文本}；非字典/必填为空 → None（丢弃）。

    容忍裸字符串是因为模型时不时把 `[{"term":"X"}]` 写成 `["X"]` —— 那是一条**有效**条目，
    丢掉它等于让用户看不见模型真给出的东西。数字/布尔/null 一律丢（它们不是内容）。
    """
    if isinstance(item, str):
        item = {required: item}
    if not isinstance(item, dict):
        return None
    row = {k: _as_str(v) for k, v in item.items()}
    return row if row.get(required) else None


def _normalise_outcome(data):
    """把模型回复归一成 6 段齐全的字典：**缺段补空默认值，绝不 KeyError**。

    模型是不可信的输入源（会被截断、会少字段、会把列表写成字符串），而复盘报告模板直接吃
    这份结构 —— 少一段就是前端一片空白或者直接崩。
    """
    data = data if isinstance(data, dict) else {}
    reasons = []
    for item in _as_list(data.get("possible_reasons")):
        row = _clean_row(item, "reason")
        if not row:
            continue
        if not row.get("evidence"):
            continue                                     # 没依据的条目**直接丢弃**，绝不替它编一段
        reasons.append({"reason": row["reason"], "evidence": row["evidence"],
                        "confidence": row.get("confidence") or "",
                        "controllable": _as_bool(item.get("controllable"))})
    actions = []
    for item in _as_list(data.get("actions")):
        row = _clean_row(item, "action")
        if row:
            actions.append({"area": row.get("area") or "", "action": row["action"],
                            "why": row.get("why") or ""})
    to_study = []
    for item in _as_list(data.get("to_study")):
        row = _clean_row(item, "term")
        if row:
            to_study.append({"term": row["term"], "why": row.get("why") or "",
                             "definition": row.get("definition") or ""})
    # 顺序一律**保持模型给的优先级顺序**（绝不排序）：模型把最可能的原因放在最前面
    return {
        "verdict_summary": _as_str(data.get("verdict_summary")),
        "possible_reasons": reasons,
        "not_controllable": [s for s in (_as_line(x) for x in _as_list(data.get("not_controllable")))
                             if s],
        "actions": actions,
        "to_study": to_study,
        "next_time": _as_str(data.get("next_time")),
        # 空列表一律为 True：无论是「模型没给」还是「给的都没依据」，报告都该如实说
        # 「模型没有给出带依据的原因，可以重试」，而不是留一片无从解释的空白。
        "reasons_all_dropped": not reasons,
    }


def _basis_of(job, facts):
    """这次归因**实际喂进去的东西** —— 报告里的「原始依据」就是它，不另编一份。"""
    return {"job_title": _as_str(job.get("job_title")), "status": facts.get("status") or "",
            "outcome_reason": facts.get("reason") or "",
            "interview_ids": [iv.get("id") or "" for iv in facts.get("interviews") or []],
            "interviews": facts.get("interviews") or [], "jd": facts.get("jd") or {}}


# ---------------------------------------------------------------- 归因（同步执行）
def attribute(job_id, cfg, chat=None):
    """一次模型调用产出归因，并写回 application.outcome_ai（**绝不碰 outcome_reason**）。

    顺序是契约的一部分：先过终态门槛（没到终态 / **没记过状态** → NotTerminal），再收集依据，
    再问模型，再做证据过滤 —— 门槛放在最前面，没结果时**一次模型调用都不发**。
    """
    job = _require_job(job_id)                  # 岗位不存在 → LookupError（路由映射 404）
    if not can_attribute(job_id):               # 终态门槛：唯一判据（见 can_attribute）
        raise NotTerminal(_not_terminal_message(job))
    view = _view_of(job)
    facts = _facts_of(job, view)
    reply = _call_model(chat, cfg, _DIRECTIVES, _build_prompt(job, facts))
    out = _normalise_outcome(_parse_reply(reply, cfg))
    # 这三项由**本地事实**说了算，绝不采信模型的数字（它根本不知道我们喂了几场面试）
    out["interview_count"] = len(facts["interviews"])
    out["degraded_no_interview"] = not facts["interviews"]
    out["basis"] = _basis_of(job, facts)
    out["attributed_at"] = _now()
    app = _raw_application(job)
    app["outcome_ai"] = out                                # 只写 AI 的地盘
    job["application"] = app
    job = store.upsert_job(job)
    log.log_event("outcome.attributed", job=job.get("id"), interviews=out["interview_count"],
                  reasons=len(out["possible_reasons"]), dropped=out["reasons_all_dropped"])
    return out


# ---------------------------------------------------------------- 报告
def render_report(payload, job_id):
    """渲染复盘报告 HTML（文件名 `oc_<岗位id>.html`），返回文件名。

    模板细节在 Task 2；这里只负责把数据交给 app.render_report，并把 report_id 钉在岗位 id 上
    （复盘报告按岗位定位，不像岗位报告那样按「日期_岗位_分数」每次生成新文件）。

    **延迟 import app** —— app.py 会 import outcome 的调用方（路由），模块顶部互相 import 会循环导入。

    `outcome_reason` / `status` / `outcome_at` 在这里从台账补齐（setdefault，不覆盖调用方给的）：
    报告里的「你写的原因」必须是用户**原话**，不能靠调用方二次转述。
    """
    import app
    data = dict(payload) if isinstance(payload, dict) else {}
    data["kind"] = "outcome"                                # 唯一判定点：app.report_template_for
    jid = _as_str(job_id)
    job = store.find_job(jid) if jid else None              # 容错：岗位被删也要能出报告
    if isinstance(job, dict):
        view = _view_of(job)
        data.setdefault("job_title", _as_str(job.get("job_title")))
        data.setdefault("company", _as_str(job.get("company")))
        data.setdefault("job_category", _as_str(job.get("job_category")))
        data.setdefault("status", view["status"])
        data.setdefault("outcome_at", view["outcome_at"])
        data.setdefault("outcome_reason", view["outcome_reason"])
        data.setdefault("score", job.get("score"))
        if view["outcome_ai"] is not None:
            data.setdefault("outcome_ai", view["outcome_ai"])
    data.setdefault("job_title", "")
    data.setdefault("company", "")
    data.setdefault("status", "")
    data.setdefault("outcome_at", "")
    data.setdefault("outcome_reason", "")
    data.setdefault("date", datetime.now().strftime("%Y-%m-%d"))
    data["score"] = _as_score(data.get("score"))             # app.render_report 的 "%g" 要数字
    fname = app.render_report(data, fixed_id="oc_" + jid)
    log.log_event("outcome.report_rendered", job=jid, report=fname,
                  degraded_no_interview=bool((data.get("outcome_ai") or {}).get("degraded_no_interview")))
    return fname
