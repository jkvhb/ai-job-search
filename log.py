# -*- coding: utf-8 -*-
"""运行日志：JSONL 追加写 + API Key 打码 + 反馈包"""
import json
import os
import re
import sys
import threading
import time
import traceback
import uuid
import zipfile
from collections import namedtuple
from datetime import datetime

import store

APP_VERSION = "0.3.0"
SESSION_ID = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]

# 打码规则（P1-8）：本项目**真实会进日志**的凭证前缀。
#   模型调用走 OpenAI 兼容（`sk-`，DeepSeek / Kimi）；Tavily 搜索的 Key 是 `tvly-`
#   （旧实现只认 `sk-`，于是 Tavily 的 Key 原样落盘，而说明书却写着"已自动打码"）；
#   博查（bocha）走同一个 `Bearer` 头，Key 形如 `bsk-` / `sk-`。
# ⚠️ 前缀前必须是"**ASCII** 字母数字之外"的位置，用 `(?<![A-Za-z0-9])` 而**不是** `\b`：
#   ① 没有这道闸门时，`task-1234567890` 会被打成 `ta` + `sk-1234***`（旧实现就是这个行为 ——
#      打码顺手改坏了正常文本）；
#   ② 但 `\b` 在 `str` 模式是**Unicode 语义**：`\w` 把汉字也算单词字符，于是
#      `密钥tvly-abc…`（汉字直接贴着 Key，没有空格）里 `钥`|`t` 之间**没有**词边界，
#      该打码的 Key 会被原样放过 —— 那正是 P1-8 要消掉的"说明书写着已打码、实际漏"。
#      只挡 ASCII 字母数字则两头都对：`task-…` 挡得住，`密钥tvly-…` 也打得上。
KEY_PAT = re.compile(r"(?<![A-Za-z0-9])((?:sk|tvly|bsk)-[A-Za-z0-9_\-]{4})[A-Za-z0-9_\-]{4,}", re.I)
# `Bearer` 后面跟的长串（JWT 那种带点的也算）。门槛 16 位是为了**不误伤散文**：
# "Bearer token" / "bearer 认证失败" 这种正常文字不许被打码。
# 前缀前同样是 `(?<![A-Za-z0-9])` 而非 `\b`，理由见 KEY_PAT 上面 ②（`认证Bearer <长串>` 同病）。
BEARER_PAT = re.compile(r"(?i)(?<![A-Za-z0-9])Bearer\s+[A-Za-z0-9_\-\.]{16,}")
SENSITIVE_KEYS = ("api_key", "apikey", "key", "authorization", "token", "password", "secret")

# 追加写不是原子的：并发（如 knowledge 的并行搜索/时间线）会交错写坏 JSONL 行
_LOG_LOCK = threading.Lock()


def log_file_path():
    """当前 profile 的日志文件路径。

    `store.current_profile()` 在档名文件（`data/current_profile.txt`）**存在却读不出来**时
    抛 `store.ProfileUnreadable`，这里**不接、不包**，原样冒给调用方 ——
    理由见本文件 `_initial_log_file()` 的说明（那个异常已经带齐「哪个文件 / 为什么 / 怎么修」，
    再包一层只会让调用方多猜一层类型；精确的子类让 app 能只接这一种，不靠文案匹配）。
    """
    return store.p_path("logs", "events.jsonl")


# import 期解析失败时**原样留档**的那个异常（None = 一切正常）。见 `_initial_log_file()`。
LOG_PATH_ERROR = None


def _initial_log_file():
    """import 期的路径解析：档名读不出来时**不抛**，只把错误留档，返回空串占位。

    ⚠️ 为什么这一处必须和 `log_file_path()` 不一样：
    log 是**所有模块的公共依赖**（jdfacts / jddelete / jdid / jobdup / kb / … 都 import 它，
    而它们又都在 app 之前被 import），所以 import 期一抛，`import app` 连自己的启动校验
    都走不到，用户（双击 启动.bat）看到的仍是一坨 traceback —— 正是这次要消掉的东西。
    库文件也不该替进程决定生死：那会让任何 `import log` 的东西都意外退出。

    **但这不是吞掉**：异常对象原封不动存在 `LOG_PATH_ERROR` 里，`log_file_path()` 照旧抛，
    由 app 的启动校验统一印人话 + 非零退出（`app.profile_precondition_error()`）。
    `LOG_FILE` 给空串占位而非 default 的路径 —— 宁可这段时间不写日志，也绝不把日志
    写进另一个档（那正是「静默串档」）。写日志本来就是「失败不打扰用户」的语义，
    空路径下它安静地不写。
    """
    global LOG_PATH_ERROR
    try:
        return log_file_path()
    except store.ProfileUnreadable as e:
        LOG_PATH_ERROR = e
        return ""


# 兼容旧引用：模块级 LOG_FILE 由 refresh() 更新
LOG_FILE = _initial_log_file()


def refresh():
    """切换 profile 后调用，刷新日志文件路径。

    与 import 期不同，这里是**被显式调用**的：档名读不出来就原样抛给调用方
    （app 的启动校验会在这之前就拦下，所以正常情况下到不了这里）。
    """
    global LOG_FILE, LOG_PATH_ERROR
    LOG_FILE = log_file_path()
    LOG_PATH_ERROR = None
    return LOG_FILE


def _mask_keys(s):
    """字符串里的凭证打码：前缀 Key（保留前 8 位）+ `Bearer <长串>`。规则见 KEY_PAT 的注释。"""
    return BEARER_PAT.sub("Bearer ***", KEY_PAT.sub(r"\1***", s))


def redact(obj, depth=0):
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
        return _mask_keys(obj)[:4000]
    return obj


# ---------------------------------------------------------------- 写失败留痕（P1-5）
# ⚠️ 这两个名字里都写明了"本进程 / 本次启动"：它们**绝不会**被读成"日志一切正常"，
#    只在**确实写失败**的那条分支上 +1（不做任何猜测、不做兜底统计）。
#    它们随 env.json 一起进反馈包，AI 排错时能看见"有 N 条日志没写进盘、最近一次是什么原因"。
_FAIL_LOCK = threading.Lock()
LOG_WRITE_FAILURES = 0            # 本进程内写日志失败的条数
LAST_LOG_WRITE_FAILURE = None     # {"event": ..., "error": ...}，最近一次失败


def _note_write_failure(event, exc):
    """记一笔写失败：计数 + 最近一次原因 + stderr 一行。

    **本函数绝不会抛**：日志写不进去只该少一份证据，不该把"记不上日志"升级成"功能坏掉"。
    """
    global LOG_WRITE_FAILURES, LAST_LOG_WRITE_FAILURE
    detail = {"event": str(event), "error": "%s: %s" % (type(exc).__name__, exc)}
    with _FAIL_LOCK:
        LOG_WRITE_FAILURES += 1
        LAST_LOG_WRITE_FAILURE = detail
    try:
        sys.stderr.write("[log] 写日志失败（这一条没落盘，业务不受影响）：%s —— %s\n"
                         % (detail["event"], detail["error"]))
        sys.stderr.flush()
    except Exception:
        pass


def write_failure_stats():
    """给反馈包用的快照（形状固定，省得每个调用方各拼各的）。"""
    with _FAIL_LOCK:
        last = LAST_LOG_WRITE_FAILURE or {}
        return {"count": LOG_WRITE_FAILURES,
                "last_event": last.get("event"),
                "last_error": last.get("error")}


def log_event(event, level="info", data=None, **kw):
    """写一条日志。**成功返回那条 `rec`；没写进盘返回 `None`**（P1-5）。

    为什么失败回 `None` 而不是照旧回 `rec`：原来的写法回一条"看起来成功了"的记录，
    调用方（含未来的人）会以为记录成功了 —— 那正是一句假话。`None` 是"没写进盘"最直白的说法，
    判断成本是 `if rec is None:`。成功路径的返回值形状**没变**（还是那个 dict）。
    ⚠️ 任何情况下都不 re-raise。
    """
    d = {}
    if isinstance(data, dict):
        d.update(data)
    d.update(kw)
    rec = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
           "level": level, "session": SESSION_ID, "event": event, "data": redact(d)}
    written = True
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        with _LOG_LOCK:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception as e:
        # 含 json.dumps 失败（不可序列化的 data）、磁盘满、被占用、LOG_FILE 为空串占位等
        written = False
        _note_write_failure(event, e)
    if level in ("error", "warn"):
        try:
            print("  [%s] %s %s" % (level.upper(), event, json.dumps(rec["data"], ensure_ascii=False)[:400]))
        except Exception:
            pass
    return rec if written else None


def log_exc(event, exc=None, **kw):
    log_event(event, level="error", error=str(exc) if exc else "",
              traceback=traceback.format_exc()[-3000:], **kw)


class LogPage(namedtuple("LogPage", "records total")):
    """`read_logs()` 的返回值。

    ⚠️ **外形没变**：它仍然是"可解包成 2 项"的值（`recent, total = log.read_logs(n)`），
    `page[0]` / `page[1]` 也照旧 —— `app.py` 的 `/api/logs` 和一堆测试都依赖这一点，
    而它们不在本次改动范围内。多出来的真相挂在属性上：

      - `total`       能解析出来的**记录总条数**（不受 `limit` 截断），与 `records` 同口径
      - `lines`       文件总行数（含空行/坏行）；恒等式 `total + blank_lines + bad_lines == lines`
      - `blank_lines` 空行数、`bad_lines` 非空但 JSON 解析失败的行数
      - `error`       非 `None` = 文件**存在却读不出来**（或日志路径压根没解析出来）。
                      此时 `records == []` 且 `total == 0`，但**它不是"真的没有日志"** ——
                      只有 `error is None` 时 0 才代表"真的没有"。`readable` 是它的布尔形式。
    """

    def __new__(cls, records, total, lines=0, blank_lines=0, bad_lines=0, error=None):
        self = super(LogPage, cls).__new__(cls, records, total)
        self.lines = lines
        self.blank_lines = blank_lines
        self.bad_lines = bad_lines
        self.error = error
        return self

    @property
    def readable(self):
        return self.error is None


def read_logs(limit=200):
    """读最近日志，返回 `LogPage`（可解包成 `(records, total)`，见该类说明）。

    **口径（P1-7）**：`total` = **能解析的记录总条数**，`records` = 其中最后 `limit` 条，
    于是 `len(records) == min(limit, total)` 恒成立 —— "两个数对不上"从根上没了。
    行数口径单独给（`lines` / `blank_lines` / `bad_lines`）：坏行与空行既不会被悄悄算进条数，
    也不会被藏起来（新旧口径都能从这三个数还原出来）。
    `limit <= 0` 视为不截断（旧实现 `lines[-limit:]` 在 limit=0 时就是全量）。

    **读不出来（P1-6）**：文件存在却读不出来时**绝不**回一个会被读成"共 0 条日志"的空结果，
    而是把原因填进 `error` 如实带回调用方（调用方据此显示"日志读不出来"而不是"共 0 条"）。
    """
    if not LOG_FILE:
        # 档名文件读不出来时 LOG_FILE 是空串占位（见 _initial_log_file）：
        # 这不是"没有日志"，是"日志路径压根没解析出来"，同样不许说成 0 条。
        return LogPage([], 0, error="日志路径为空（档名文件读不出来，见 LOG_PATH_ERROR）")
    if not os.path.exists(LOG_FILE):
        return LogPage([], 0)                     # 文件不存在 = 真的没有日志
    try:
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception as e:
        # 存在却读不出来（编码不对 / 权限 / 被独占…）：如实带回，不冒充 0 条
        return LogPage([], 0, error="日志文件 `%s` 存在却读不出来：%s" % (LOG_FILE, e))
    parsed = []
    blank = 0
    bad = 0
    for line in lines:
        line = line.strip()
        if not line:
            blank += 1
            continue
        try:
            parsed.append(json.loads(line))
        except Exception:
            bad += 1
    return LogPage(parsed[-limit:] if limit > 0 else parsed, len(parsed),
                   lines=len(lines), blank_lines=blank, bad_lines=bad)


LOG_GUIDE = """# 反馈日志包说明

这个压缩包用来把「使用过程 + 报错信息」反馈给 AI，让它优化这个工具。

## 包含内容
- `events.jsonl` —— 全部操作与执行日志，每行一条 JSON（用户操作 / 接口调用 / 模型调用 / 报错堆栈）
- `env.json` —— 运行环境信息（版本、模型名、数据量统计、本进程日志写失败次数），**不含任何 API Key**
  - `counts` 里**只有真的数出来**的项；**某项缺失 = 它数不出来**，原因在同级 `counts_unreadable` 里
    （**缺失不等于 0**，不要当成"用户没有简历/岗位"）
  - `log_write_failures.count > 0` = 有若干条日志**没写进盘**（原因见 `last_error`）
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
- 打码规则（与实现一致，只有这几类会打码，**不是**"所有 Key 都打码"）：
  `sk-` / `tvly-` / `bsk-` 前缀的 Key 保留前 8 位、其余变 `***`；`Bearer <长串>` 变 `Bearer ***`；
  字段名是 `api_key` / `token` / `password` / `secret` 等的值整体变 `***已打码***`。
- 日志**不含简历正文、不含 JD 原文**；可能含岗位名称、公司名、简历文件名。
- 不放心的话，先用记事本打开 `events.jsonl` 删掉不想分享的行再发。

## 怎么用
把整个 zip 发给 AI，并简单说明：你做了什么 → 期望什么 → 实际发生了什么。
"""


def _count_files(directory, exts):
    """数一个目录下指定后缀的文件，返回 `(计数, 原因)`：**原因非空时计数是 `None`**。

    ⚠️ 为什么不能"任何异常都回 0"（P1-4）：这个计数会进反馈包的 `env.json`，
    而反馈包是**给 AI 排错用的** —— 一个 0 会被读成"用户 0 份简历 / 0 份报告"，
    把归因直接带偏（实测：传普通文件也回 0）。所以**只有真的数出来是 0 才回 0**：
      - 目录不存在（`FileNotFoundError`）= 新建档案真的没有 → `(0, "")`；
      - 路径存在却不是目录 / 列不出来（权限等）→ `(None, 原因)`，
        由 `build_log_bundle` 写进 `counts_unreadable`，绝不冒充 0。
    """
    try:
        names = os.listdir(directory)
    except FileNotFoundError:
        # 目录不存在（新建档案的第一天）= **真的数出来是 0**：唯一允许回 0 的一种失败形态。
        # ⚠️ 别把它并进下面那支：`store.set_current_profile()` 会 `ensure_profile()`
        #    把 `resumes`/`reports` 建出来，所以"目录不存在"在测试里**必须先把目录删掉**
        #    才走得到这一行（变异取证：删掉本支，全量 1342 条里 test_log 39 条**全绿**）。
        return 0, ""
    except Exception as e:
        # 路径存在却不是目录（NotADirectoryError）/ 列不出来（权限、IO…）：**数不出来**，
        # 回 `None` + 原因，由调用方写进 `counts_unreadable`，绝不冒充 0。
        return None, "目录 `%s` 数不出来：%s" % (directory, e)
    return len([f for f in names if f.lower().endswith(exts)]), ""


def build_log_bundle(port=8000):
    """日志 + 环境信息（不含 key）打包成 zip，方便反馈给 AI 做优化

    port 由调用方传入（app.py 传它实际监听的端口），不要写死。
    """
    log_dir = os.path.dirname(LOG_FILE)
    os.makedirs(log_dir, exist_ok=True)
    name = "反馈日志包_%s.zip" % datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(log_dir, name)
    cfg = store.load_config()
    # counts 的规矩（P1-4）：**能数出来才写数字**。数不出来的项**不写**（不写 0！），
    # 原因记在同级 counts_unreadable 里 —— 少一个键会被看见，一个假的 0 不会被看见。
    counts = {}
    counts_unreadable = {}
    for label, directory, exts in (("resumes", store.p_path("resumes"), (".md", ".txt")),
                                   ("reports", store.p_path("reports"), (".html",))):
        value, why = _count_files(directory, exts)
        if why:
            counts_unreadable[label] = why
        else:
            counts[label] = value
    # `store.load_jobs()` 读不出来也回 []（与"真的没有台账"无法区分），所以这里用
    # `read_jobs_strict()`：它在读不出来/结构不对时给出原因，那个 0 才不许冒充"0 个岗位"。
    jobs, jobs_why = store.read_jobs_strict()
    if jobs_why:
        counts_unreadable["jobs"] = jobs_why
    else:
        counts["jobs"] = len(jobs)
    env = {
        "app_version": APP_VERSION,
        "session": SESSION_ID,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "python": sys.version,
        "platform": sys.platform,
        "port": port,
        "models": {
            "text": {"base_url": cfg["text_model"]["base_url"], "model": cfg["text_model"]["model"],
                     "temperature": cfg["text_model"].get("temperature"),
                     "api_key_set": bool(cfg["text_model"]["api_key"])},
            "vision": {"base_url": cfg["vision_model"]["base_url"], "model": cfg["vision_model"]["model"],
                       "temperature": cfg["vision_model"].get("temperature"),
                       "api_key_set": bool(cfg["vision_model"]["api_key"])},
        },
        "counts": counts,
        "counts_unreadable": counts_unreadable,
        "log_write_failures": write_failure_stats(),
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        if os.path.exists(LOG_FILE):
            z.write(LOG_FILE, "events.jsonl")
        z.writestr("env.json", json.dumps(env, ensure_ascii=False, indent=2))
        z.writestr("反馈说明.md", LOG_GUIDE)
    log_event("logs.bundle", file=name, size=os.path.getsize(path))
    return name
