# -*- coding: utf-8 -*-
"""运行日志：JSONL 追加写 + API Key 打码 + 反馈包"""
import json
import os
import re
import sys
import time
import traceback
import uuid
import zipfile
from datetime import datetime

import store

APP_VERSION = "0.3.0"
SESSION_ID = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]

KEY_PAT = re.compile(r"(sk-[A-Za-z0-9_\-]{4})[A-Za-z0-9_\-]{4,}")
SENSITIVE_KEYS = ("api_key", "apikey", "key", "authorization", "token", "password", "secret")


def log_file_path():
    return store.p_path("logs", "events.jsonl")


# 兼容旧引用：模块级 LOG_FILE 由 refresh() 更新
LOG_FILE = log_file_path()


def refresh():
    """切换 profile 后调用，刷新日志文件路径"""
    global LOG_FILE
    LOG_FILE = log_file_path()
    return LOG_FILE


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
        return KEY_PAT.sub(r"\1***", obj)[:4000]
    return obj


def log_event(event, level="info", data=None, **kw):
    d = {}
    if isinstance(data, dict):
        d.update(data)
    d.update(kw)
    rec = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
           "level": level, "session": SESSION_ID, "event": event, "data": redact(d)}
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
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
    log_event(event, level="error", error=str(exc) if exc else "",
              traceback=traceback.format_exc()[-3000:], **kw)


def read_logs(limit=200):
    if not os.path.exists(LOG_FILE):
        return [], 0
    try:
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return [], 0
    out = []
    for line in lines[-limit:]:
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out, len(lines)


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


def _count_files(directory, exts):
    """安全计数：目录不存在时返回 0（避免新 profile 下打包直接抛错）"""
    try:
        names = os.listdir(directory)
    except Exception:
        return 0
    return len([f for f in names if f.lower().endswith(exts)])


def build_log_bundle():
    """日志 + 环境信息（不含 key）打包成 zip，方便反馈给 AI 做优化"""
    log_dir = os.path.dirname(LOG_FILE)
    os.makedirs(log_dir, exist_ok=True)
    name = "反馈日志包_%s.zip" % datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(log_dir, name)
    cfg = store.load_config()
    env = {
        "app_version": APP_VERSION,
        "session": SESSION_ID,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "python": sys.version,
        "platform": sys.platform,
        "port": 8000,
        "models": {
            "text": {"base_url": cfg["text_model"]["base_url"], "model": cfg["text_model"]["model"],
                     "temperature": cfg["text_model"].get("temperature"),
                     "api_key_set": bool(cfg["text_model"]["api_key"])},
            "vision": {"base_url": cfg["vision_model"]["base_url"], "model": cfg["vision_model"]["model"],
                       "temperature": cfg["vision_model"].get("temperature"),
                       "api_key_set": bool(cfg["vision_model"]["api_key"])},
        },
        "counts": {
            "resumes": _count_files(store.p_path("resumes"), (".md", ".txt")),
            "reports": _count_files(store.p_path("reports"), (".html",)),
            "jobs": len(store.load_jobs()),
        },
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        if os.path.exists(LOG_FILE):
            z.write(LOG_FILE, "events.jsonl")
        z.writestr("env.json", json.dumps(env, ensure_ascii=False, indent=2))
        z.writestr("反馈说明.md", LOG_GUIDE)
    log_event("logs.bundle", file=name, size=os.path.getsize(path))
    return name
