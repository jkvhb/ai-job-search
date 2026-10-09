# -*- coding: utf-8 -*-
"""面试记录：本地转写 + AI 复盘（阶段 3.4 的核心流水线）。

    上传 → 转写 → AI 分析（7 段）→ 反哺知识库 → 渲染复盘报告

设计要点：
  * 转写走【外部 venv 子进程】调 vidkit 的模块 —— 本项目零依赖，不能把 faster-whisper 引进来
  * 转写与模型调用都可注入（runner= / chat= / render=）—— 测试绝不真跑 vidkit、绝不真调模型
  * 转写是 0.9× 音频时长（实测：11 分 23 秒音频 → 613 秒），30 分钟的面试要转 27 分钟，
    所以必须是后台线程 + 状态落盘，不能同步等：程序中途关掉后重启要能看出「上次被中断、可重试」
  * 失败**保留音频**，只写 error 状态并抛出 —— 绝不删用户的录音
"""
import json
import os
import subprocess
import threading
import time
from datetime import datetime

import kb
import llm
import log
import store

ROOT = os.path.dirname(os.path.abspath(__file__))

# 上传上限：与 store.save_stream 的调用方共用同一个数字（app.py 用它先查 Content-Length）
MAX_UPLOAD_BYTES = 500 * 1024 * 1024

# 超时 = 音频时长 × TRANSCRIBE_SLACK + TRANSCRIBE_FLOOR，封顶 TRANSCRIBE_TIMEOUT_MAX。
# 实测转写约 0.9× 音频时长，这里留 3 倍余量是给「慢机器 + 大模型首次加载」的。
TRANSCRIBE_SLACK = 3.0
TRANSCRIBE_FLOOR = 600
TRANSCRIBE_TIMEOUT_MAX = 4 * 3600

# 超过这个字符数就先分块摘要再汇总（map-reduce），否则一次请求会把上下文撑爆
MAX_ANALYZE_CHARS = 40000
# 单块送进模型摘要时的硬上限（map 阶段，块本身不该太长）
MAX_SUMMARY_CHARS = 12000

# 录音/视频扩展名：优先顺序即这里的顺序（保证同一目录下选出的是同一份文件，测试可重复）
AUDIO_EXTS = (".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus",
              ".mp4", ".mov", ".mkv", ".webm", ".avi")

# interview_id → 活动线程。status() 靠它区分「真在跑」与「上次被中断」
_INTERVIEW_THREADS = {}
_THREADS_LOCK = threading.Lock()

# 提示词：要求一次输出那 7 段 JSON（前端复盘报告直接吃这份结构）
KB_DIRECTIVES = """你是资深 AI 领域技术面试复盘教练。读下面这场面试的**完整转写**，输出一份复盘。

只输出 JSON（不要 markdown 代码块、不要解释），结构如下：
{"summary":"整场表现总评（150~300 字，说人话，指出最该改的一件事）",
 "questions":[{"q":"面试官问的原问题","category":"考察方向","answer_quality":"好|一般|差","comment":"你的回答点评"}],
 "focus_areas":[{"area":"考察方向","share":40}],
 "new_terms":[{"term":"面试里出现但求职者可能不懂的术语","definition":"一句大白话解释"}],
 "interviewer_quality":{"score":1,"comment":"面试官专业度点评（问了什么、有没有引导、是否讲清岗位）"},
 "answer_review":[{"topic":"被问到的主题","verdict":"答砸了|答得一般|答得好","answer":"你当时的答法",
                   "better_answer":"更好的答法（要具体，能直接背）"}],
 "weak_concepts":[{"term":"答砸/答不上来的知识点术语","why":"为什么没答上"}],
 "next_round_prep":{"likely_followups":["二面可能追问的问题"],"to_study":["优先补的知识点"]}}

硬性要求：
- focus_areas 的 share 是百分比整数，全部加起来凑到 100 左右
- weak_concepts 的 term 要短、是能拿去复习的知识点名词（例如「RLHF」，不是一整句话）
- 字符串内部禁止英文双引号（用中文引号「」），禁止裸换行
- 全部中文；转写里没提到的内容不要编造"""


# ---------------------------------------------------------------- 状态（status.json）
def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _read_status(interview_id):
    st = store.load_interview_json(interview_id, "status.json", {})
    return st if isinstance(st, dict) else {}


def _set_status(interview_id, state, error=None, **extra):
    """写 status.json。**每一步状态变化都要经过这里**（前端轮询只看这一个文件）。

    已有字段一律继承：original_name / audio_bytes / started_at 是上传阶段写的元数据，
    后一步把它整个覆盖掉的话前端就再也显示不出「面试录音.m4a」了。
    """
    old = _read_status(interview_id)
    started = old.get("started_at") or _now()
    st = dict(old)
    st.update(extra)                      # extra 里的键覆盖旧值（kind/job_id 等）
    st["state"] = state
    st["started_at"] = started
    st["updated_at"] = _now()
    st["elapsed"] = _elapsed_since(started)
    if error is not None:
        st["error"] = str(error)
    elif state != "error":
        st["error"] = ""
    store.save_interview_json(interview_id, "status.json", st)
    return st


def _elapsed_since(started_at):
    try:
        return max(0.0, round(time.time() - datetime.strptime(str(started_at), "%Y-%m-%d %H:%M:%S").timestamp(), 1))
    except Exception:
        return 0.0


def is_running(interview_id):
    """这个面试记录此刻是否真有活动线程。"""
    with _THREADS_LOCK:
        t = _INTERVIEW_THREADS.get(interview_id)
    return bool(t and t.is_alive())


def status(interview_id):
    """给前端轮询的状态。文件里停在「转写中/分析中」但**没有活动线程** → 报 interrupted。

    这个判据是「程序中途被关掉」唯一能被看出来的地方：进程死了线程就没了，
    于是重启后前端会显示「上次被中断，可重试」，而不是永远转圈。
    """
    st = _read_status(interview_id)
    if not st:
        return {}
    if st.get("state") in ("transcribing", "analyzing") and not is_running(interview_id):
        st = dict(st, state="interrupted", interrupted_from=st.get("state"))
    return st


def list_interviews(job_id=""):
    """某个岗位下的面试记录 id（按 mtime 倒序，最近的在前）。纯本地目录扫描。"""
    jid = str(job_id or "").strip()
    if not jid:
        return []
    root = store.interviews_dir()
    if not os.path.isdir(root):
        return []
    found = []
    for name in os.listdir(root):
        try:
            st = store.load_interview_json(name, "status.json", {})
        except Exception:
            continue
        if isinstance(st, dict) and str(st.get("job_id") or "") == jid:
            found.append((os.path.getmtime(store.interview_dir(name)), name))
    found.sort(key=lambda x: (-x[0], x[1]))
    return [name for _, name in found]


# ---------------------------------------------------------------- 转写（外部 venv 子进程）
def find_vidkit(cfg):
    """定位 vidkit 目录：配置优先，其次自动探测同级目录。返回可用路径或 None。

    只判「这个目录在不在」：真正的 venv 校验留给 run_transcribe 报更准的错
    （「找不到 vidkit」与「vidkit 的虚拟环境不存在」是两件要用户做不同操作的事）。
    """
    cand = str((cfg or {}).get("vidkit_dir") or "").strip()
    if not cand:
        cand = os.path.join(os.path.dirname(ROOT), "video-link-extractor")   # 本项目同级
    return cand if cand and os.path.isdir(cand) else None


def _vidkit_python(vidkit_dir):
    return os.path.join(vidkit_dir, ".venv", "Scripts", "python.exe")


def _extract_duration(out):
    """从 vidkit 输出的 JSON 里取时长（秒）：优先 duration，其次最后一个 segment 的 end。"""
    try:
        d = float(out.get("duration") or 0)
    except (TypeError, ValueError):
        d = 0.0
    if d > 0:
        return d
    segs = out.get("segments")
    if isinstance(segs, list) and segs:
        ends = []
        for s in segs:
            if isinstance(s, dict):
                try:
                    ends.append(float(s.get("end") or 0))
                except (TypeError, ValueError):
                    continue
        if ends:
            return max(ends)
    return 0.0


def _default_runner(cmd, cfg=None, cwd=None):
    """真起子进程。返回 stdout 文本；非零退出码 → RuntimeError（附末尾输出便于定位）。

    timeout 按音频时长算（见 _transcribe_timeout）—— 固定超时会在 1 小时的面试上误杀。
    """
    audio = str(cmd[-1]) if len(cmd) > 1 else ""
    timeout = _transcribe_timeout(audio)
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError("转写超时（超过 %d 分钟仍未结束）。可以调小模型或改用更短的录音。"
                           % int(timeout // 60))
    except OSError as e:
        raise RuntimeError("启动 vidkit 的 python 失败：%s" % e)
    out = (p.stdout or b"").decode("utf-8", "replace")
    if p.returncode != 0:
        err = (p.stderr or b"").decode("utf-8", "replace")
        raise RuntimeError("vidkit 转写失败（退出码 %s）：%s" % (p.returncode, (err or out)[-500:]))
    return out


def _transcribe_timeout(audio_path):
    """超时 = 音频时长 × 3 + 10 分钟，封顶 4 小时（拿不到时长时按体积粗估后同样留 3 倍余量）。"""
    try:
        size_mb = os.path.getsize(audio_path) / 1024.0 / 1024.0
    except OSError:
        size_mb = 0.0
    # 没有音频时长信息时用体积粗估（约 1MB/分钟是常见录音码率），宁可给多不给少
    guess_sec = size_mb * 60.0
    return min(TRANSCRIBE_TIMEOUT_MAX, max(TRANSCRIBE_FLOOR, guess_sec * TRANSCRIBE_SLACK + TRANSCRIBE_FLOOR))


def _json_from_output(out):
    """从子进程输出里取最后一行能解析成 JSON 对象的文本（vidkit 可能先打进度日志）。

    直接 json.loads 整段输出会被进度行毁掉 —— 而这不是「vidkit 坏了」，是它正常打日志。
    """
    text = str(out or "")
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    for line in reversed(lines):
        try:
            data = json.loads(line)
        except Exception:
            continue
        if isinstance(data, dict):
            return data
    return None


def run_transcribe(audio_path, cfg, runner=None):
    """调 vidkit 的 venv + 模块拿完整 JSON（含时间戳）。runner 可注入以便测试。

    **绝不静默返回空转写**：找不到 vidkit / 没装 venv / 返回的不是 JSON 一律抛错。
    静默返回空的话用户会拿到一份「0 字转写」的复盘报告，比报错更难发现。
    """
    vk = find_vidkit(cfg)
    if not vk:
        raise RuntimeError("找不到 vidkit（本地语音转写）。请在设置里填 video-link-extractor 的目录；"
                           "它需要 .venv 与 models/medium 都已就绪。")
    py = _vidkit_python(vk)
    if not os.path.isfile(py):
        raise RuntimeError("vidkit 的虚拟环境不存在：%s（先按它的 README 建 .venv）" % py)
    script = ("import json,sys\n"
              "from vidkit import transcribe\n"
              "r = transcribe.transcribe(sys.argv[1], language='zh', domain='ai')\n"
              "print(json.dumps(r, ensure_ascii=False))\n")
    runner = runner or _default_runner
    out = runner([py, "-c", script, str(audio_path)], cfg=cfg, cwd=vk)
    data = _json_from_output(out)
    if data is None:
        raise RuntimeError("vidkit 没有返回可解析的 JSON（末尾输出：%s）" % str(out)[-300:])
    if "text" not in data:
        raise RuntimeError("vidkit 返回的 JSON 缺少 text 字段")
    return data


# ---------------------------------------------------------------- AI 分析（7 段）
def _split_text(text, limit):
    """按行切成长度 ≤ limit 的块。单行超长（没有换行的转写）时硬切，避免死循环。"""
    text = str(text or "")
    if limit <= 0:
        return [text] if text else []
    chunks, cur = [], ""
    for line in text.splitlines(True):
        while len(line) > limit:                    # 单行比 limit 还长：先塞满当前块，再硬切它
            room = limit - len(cur)
            if room <= 0:
                chunks.append(cur)
                cur = ""
                room = limit
            cur += line[:room]
            line = line[room:]
            chunks.append(cur)
            cur = ""
        if len(cur) + len(line) > limit:
            chunks.append(cur)
            cur = line
        else:
            cur += line
    if cur:
        chunks.append(cur)
    return [c for c in chunks if c.strip()]


def _call_model(chat, cfg, system, user, json_mode=True):
    """注入约定：`chat(prompt, cfg)`。模型细节（base_url/key/model/temperature）在这里收口。

    这样测试里传个 `lambda prompt, cfg: '...'` 就能整条链路跑通，而真实现走 llm 的
    OpenAI 兼容调用 —— **绝不把网络细节漏进 analyze 的业务逻辑里**。
    """
    text = (system or "") + "\n\n" + (user or "")
    tm = (cfg or {}).get("text_model") or {}
    if chat is not None:
        return chat(text, cfg)
    return llm.call_openai_compatible(tm.get("base_url") or "", tm.get("api_key") or "",
                                      tm.get("model") or "", [{"role": "user", "content": text}],
                                      json_mode=json_mode, temperature=tm.get("temperature"))


def _summarize(chunk, chat, cfg):
    """map 阶段：把一块转写压成要点（只为省上下文，不需要 7 段结构）。"""
    body = str(chunk or "")[:MAX_SUMMARY_CHARS]
    return _call_model(chat, cfg, "你在压缩一段面试转写。只输出要点摘要，保留被问到的技术概念、"
                                  "问题原话要点、候选人答得含糊的地方。不要评论、不要 JSON。",
                       body, json_mode=False)


def _build_prompt(text, job):
    """reduce 阶段的用户提示：岗位背景 + 转写正文 + 那 7 段的结构要求。"""
    job = job if isinstance(job, dict) else {}
    bits = []
    if job.get("job_title"):
        bits.append("岗位：%s" % job["job_title"])
    if job.get("company"):
        bits.append("公司：%s" % job["company"])
    head = ("\n".join(bits) + "\n\n") if bits else ""
    return head + "【面试转写】\n%s\n\n请按上面的 JSON 结构输出这场面试的复盘。" % str(text or "")


def _as_str(v):
    """容错成字符串：None → ''，数字/布尔 → 文本（模型偶尔把分数写成数字）。"""
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, bool):
        return ""
    if isinstance(v, (int, float)):
        return str(v)
    return ""


def _as_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _as_dict(v):
    return v if isinstance(v, dict) else {}


def _as_list(v):
    return v if isinstance(v, list) else []


def _clean_dicts(raw, required, limit=0):
    """通用清洗：只留 dict（或裸字符串，补成第一个 required 字段）、required 字段非空，
    其余字段一律转字符串。limit 限制条数（0 不限）。

    容忍裸字符串是因为模型时不时把 `[{"term":"X"}]` 写成 `["X"]` —— 那是一个**有效**的
    复盘条目，丢掉它等于让用户看不见自己被问过什么。但非字符串的裸值（5 / true / null）
    一律丢：它们既不是术语也不是问题，硬转只会造出「5」这种垃圾卡。
    """
    out = []
    for item in _as_list(raw):
        if isinstance(item, str):
            item = {required[0]: item}
        if not isinstance(item, dict):
            continue
        row = {}
        for key in item:
            row[key] = _as_str(item.get(key))
        if any(not row.get(key) for key in required):
            continue
        out.append(row)
        if limit and len(out) >= limit:
            break
    return out


def _normalise_analysis(data):
    """把模型回复归一成 7 段齐全的字典：**缺段补空默认值，绝不 KeyError**。

    模型是不可信的输入源（会被截断、会少给字段、会把列表写成字符串），
    而复盘报告模板直接吃这份结构 —— 少一段就是前端一片空白或者直接崩。
    """
    data = data if isinstance(data, dict) else {}
    focus = []
    for f in _as_list(data.get("focus_areas")):
        if not isinstance(f, dict):
            continue
        area = _as_str(f.get("area"))
        share = _as_int(f.get("share"), -1)
        if not area or share < 0:
            continue
        focus.append({"area": area, "share": share})
    # 按占比降序；**平局按 area 升序**兜底 —— 保证同一份输入渲染顺序稳定
    focus.sort(key=lambda x: (-x["share"], x["area"]))

    iq = _as_dict(data.get("interviewer_quality"))
    prep = _as_dict(data.get("next_round_prep"))

    return {
        "summary": _as_str(data.get("summary")),
        "questions": _clean_dicts(data.get("questions"), ("q",)),
        "focus_areas": focus,
        "new_terms": _clean_dicts(data.get("new_terms"), ("term",)),
        "interviewer_quality": {"score": _as_int(iq.get("score")), "comment": _as_str(iq.get("comment"))},
        "answer_review": _clean_dicts(data.get("answer_review"), ("topic",)),
        "weak_concepts": _clean_dicts(data.get("weak_concepts"), ("term",)),
        "next_round_prep": {
            "likely_followups": [s for s in (_as_str(x) for x in _as_list(prep.get("likely_followups"))) if s],
            "to_study": [s for s in (_as_str(x) for x in _as_list(prep.get("to_study"))) if s],
        },
    }


def analyze(transcript, job, cfg, chat=None):
    """一次模型调用产出 7 段。缺段用空默认值补齐（不崩）。"""
    chat = chat or llm.call_openai_compatible
    text = str((transcript or {}).get("text") or "")
    chunks = _split_text(text, MAX_ANALYZE_CHARS)
    if len(chunks) > 1:                                   # map：分块摘要
        chunks = [_summarize(c, chat, cfg) for c in chunks]
    reply = _call_model(chat, cfg, KB_DIRECTIVES, _build_prompt("\n".join(chunks), job))
    if isinstance(reply, dict):
        data = reply
    else:
        try:
            data = llm.parse_json_reply(reply)
        except Exception as e:
            raise RuntimeError("模型没有返回可解析的分析结果：%s" % e)
    if not isinstance(data, dict):
        raise RuntimeError("模型没有返回可解析的分析结果")
    return _normalise_analysis(data)                      # 缺段补空默认值，绝不 KeyError


# ---------------------------------------------------------------- 流水线
def _job_of(jd_id):
    """岗位台账里的一条记录（找不到就给空 dict —— 分析照样能跑，只是少点背景）。"""
    for j in store.load_jobs():
        if isinstance(j, dict) and str(j.get("id") or "") == str(jd_id or ""):
            return j
    return {}


def _find_audio(interview_id):
    """面试目录里的录音/视频文件（按扩展名优先序选，同一目录下结果确定）。"""
    d = store.interview_dir(interview_id)
    if not d or not os.path.isdir(d):
        raise RuntimeError("找不到面试记录目录：%s" % interview_id)
    names = []
    for name in os.listdir(d):
        p = os.path.join(d, name)
        if os.path.isfile(p) and os.path.splitext(name)[1].lower() in AUDIO_EXTS:
            names.append(name)
    for ext in AUDIO_EXTS:
        for name in sorted(names):
            if name.lower().endswith(ext):
                return os.path.join(d, name)
    raise RuntimeError("找不到录音文件（面试记录 %s 里没有音频/视频）。"
                       "请重新上传录音，或改用文字稿。" % interview_id)


def _meta_of(interview_id, transcript):
    """复盘报告头部要用的元信息：音频时长/语言 + 转写字数。"""
    tr = transcript if isinstance(transcript, dict) else {}
    return {"duration": _extract_duration(tr), "language": _as_str(tr.get("language")) or "zh",
            "chars": len(_as_str(tr.get("text"))), "interview_id": interview_id}


def _apply_feedback(jd_id, transcript, analysis):
    """反哺知识库：转写里**确定性**命中的概念算「被问过」，模型点名答砸的降级。

    命中的判定交给 kb.interview_hits（确定性、可测、零额度），不靠模型判 —— 模型漏读就漏记。
    """
    tr = transcript if isinstance(transcript, dict) else {}
    ana = analysis if isinstance(analysis, dict) else {}
    hits = kb.interview_hits(_as_str(tr.get("text")))
    asked = [n.get("term") for n in hits if isinstance(n, dict) and _as_str(n.get("term"))]
    weak = [_as_str(w.get("term")) for w in _as_list(ana.get("weak_concepts"))
            if isinstance(w, dict) and _as_str(w.get("term"))]
    # 去重按 normalize_id（同一概念的两种写法只算一次），但仍传**原始术语**给 kb 查卡
    seen, asked_u = set(), []
    for t in asked:
        nid = kb.normalize_id(t)
        if nid and nid not in seen:
            seen.add(nid)
            asked_u.append(t)
    seen_weak, weak_u = set(), []
    for t in weak:
        nid = kb.normalize_id(t)
        if nid and nid not in seen_weak:
            seen_weak.add(nid)
            weak_u.append(t)
    # 模型点名「这个我答砸了」的概念，即使转写里没出现原词，也要记成被问过（它答砸了它）
    seen = {kb.normalize_id(x) for x in asked_u}
    for t in weak_u:
        nid = kb.normalize_id(t)
        if nid and nid not in seen:
            seen.add(nid)
            asked_u.append(t)
    return kb.record_interview(jd_id, asked=asked_u, weak=weak_u)


def render_report(out, jd_id, interview_id):
    """渲染复盘报告 HTML，返回文件名。

    模板细节在 Task 6；这里只负责把数据交给 app.render_report，并把 report_id 钉在面试记录
    id 上（复盘报告按面试记录定位，不像岗位报告那样按「日期_岗位_分数」每次生成新文件）。

    **延迟 import app** —— app.py 会 import interview，模块顶部互相 import 会循环导入。
    """
    import app
    out = dict(out or {})
    out.setdefault("job_title", (out.get("meta") or {}).get("job_title") or "")
    out.setdefault("score", 0)
    return app.render_report(out, fixed_id="iv_" + str(interview_id))


def run_pipeline(jd_id, interview_id, cfg, kind="audio", deps=None):
    """【同步】跑完 转写 → 分析 → 反哺 → 渲染。测试直接调它，不必轮询线程。"""
    deps = deps or {}
    t0 = time.time()
    log.log_event("interview.pipeline_start", interview=interview_id, job=jd_id, kind=kind)
    try:
        _set_status(interview_id, "analyzing" if kind == "text" else "transcribing",
                    job_id=str(jd_id or ""), kind=kind)
        tr = store.load_interview_json(interview_id, "transcript.json")
        if not isinstance(tr, dict):                      # 已有转写就跳过（不重复花 27 分钟）
            if kind == "text":
                src = store.read_text(store.interview_file(interview_id, "source.txt"))
                tr = {"text": src, "segments": [], "language": "zh", "duration": 0}
            else:
                tr = run_transcribe(_find_audio(interview_id), cfg, runner=deps.get("runner"))
            store.save_interview_json(interview_id, "transcript.json", tr)
        _set_status(interview_id, "analyzing")
        ana = analyze(tr, _job_of(jd_id), cfg, chat=deps.get("chat"))
        store.save_interview_json(interview_id, "analysis.json", ana)
        _apply_feedback(jd_id, tr, ana)                   # 见 Task 3 的 record_interview
        out = dict(ana, kind="interview", meta=_meta_of(interview_id, tr))
        (deps.get("render") or render_report)(out, jd_id, interview_id)
        _set_status(interview_id, "done")
        log.log_event("interview.pipeline_done", interview=interview_id, job=jd_id,
                      chars=out["meta"]["chars"], ms=int((time.time() - t0) * 1000))
    except Exception as e:
        _set_status(interview_id, "error", error=str(e))   # 失败保留音频，绝不删录音
        log.log_event("interview.pipeline_error", level="error", interview=interview_id,
                      job=jd_id, error=str(e))
        raise


def start(jd_id, interview_id, cfg, kind="audio", deps=None):
    """起后台线程跑流水线，立刻返回（转写是 0.9× 音频时长，不能同步等）。

    注册线程**在 start 之前**：反过来的话，短音频可能在 `_INTERVIEW_THREADS[id] = t`
    之前就跑完了，而那个赋值会把一个**已死线程**放进表里 —— 之后 status() 一直判它
    「没在跑」，会把已 done 的记录误报成 interrupted。
    """
    t = threading.Thread(target=run_pipeline,
                         args=(jd_id, interview_id, cfg, kind, deps or {}), daemon=True)
    with _THREADS_LOCK:
        _INTERVIEW_THREADS[interview_id] = t
    t.start()
    return t
