# -*- coding: utf-8 -*-
"""路径解析（profile 隔离）+ 文本/JSON 读写 + config/jobs/knowledge 存取"""
import json
import os
import re

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_ROOT = os.path.join(ROOT, "data")
# 重要：路径一律用函数动态计算，不要缓存成模块级常量——
# 否则测试替换 DATA_ROOT 后仍会指向真实数据目录，污染用户数据。

DEFAULT_CONFIG = {
    "text_model": {"label": "DeepSeek（文本分析 / 跑 SOP）",
                   "base_url": "https://api.deepseek.com/v1",
                   "model": "deepseek-flash", "temperature": 0.3, "api_key": ""},
    "vision_model": {"label": "Kimi（识图 OCR / 读 JD 截图）",
                     "base_url": "https://api.moonshot.cn/v1",
                     "model": "kimi-k2.6", "temperature": 1, "api_key": ""},
    "search_providers": [],
    "max_searches": 40,   # 每次知识分析最多搜多少次（核心概念优先），控制搜索额度消耗
}

CATEGORIES = ["数据标注/质检", "AI数据运营", "标注管理", "AI产品经理", "AI训练师", "其他"]
STATUSES = ["未投递", "已投", "待回复", "面试中", "已拒", "Offer", "放弃"]


# ---------- profile ----------
def profiles_root():
    return os.path.join(DATA_ROOT, "profiles")


def current_profile_file():
    return os.path.join(DATA_ROOT, "current_profile.txt")


def profile_dir(key=None):
    k = (key or "").strip() or "default"
    k = re.sub(r"[^\w\-]", "_", k)
    return os.path.join(profiles_root(), k)


def ensure_profile(key=None):
    d = profile_dir(key)
    for sub in ("resumes", "reports", "jd", "logs"):
        os.makedirs(os.path.join(d, sub), exist_ok=True)
    return d


def current_profile():
    try:
        with open(current_profile_file(), "r", encoding="utf-8") as f:
            return f.read().strip() or "default"
    except Exception:
        return "default"


def set_current_profile(key=None):
    k = (key or "").strip() or "default"
    ensure_profile(k)
    fp = current_profile_file()
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    with open(fp, "w", encoding="utf-8") as f:
        f.write(k)
    return k


def p_path(*parts, **kw):
    """解析到指定 key 的 profile；未指定则用「当前 profile」（多用户隔离的关键）"""
    return os.path.join(profile_dir(kw.get("key") or current_profile()), *parts)


# ---------- 读写 ----------
def read_text(path, default=""):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return default


def write_text(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def safe_name(name):
    """只取文件名；拒绝 . / .. 这类可越级的名字（返回空串）"""
    n = os.path.basename(str(name or "").strip())
    return "" if n in (".", "..") else n


def slug(text, limit=28):
    t = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "", str(text or "")).strip()
    t = re.sub(r"\s+", "", t)
    return (t[:limit] or "岗位")


# ---------- config ----------
def config_path(key=None):
    return p_path("config.json", key=key)


def load_config(key=None):
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    data = read_json(config_path(key), {})
    if isinstance(data, dict):
        for k in ("text_model", "vision_model"):
            if isinstance(data.get(k), dict):
                cfg[k].update({kk: vv for kk, vv in data[k].items() if vv is not None})
        if isinstance(data.get("search_providers"), list):
            cfg["search_providers"] = data["search_providers"]
        # 可调项：用户可在 config.json 里改搜索次数上限（bool 是 int 子类，要排除）
        ms = data.get("max_searches")
        if isinstance(ms, int) and not isinstance(ms, bool) and ms >= 0:
            cfg["max_searches"] = ms
    return cfg


def save_config(cfg, key=None):
    write_json(config_path(key), cfg)


# ---------- jobs（岗位台账）----------
def jobs_path(key=None):
    return p_path("jobs.json", key=key)


def load_jobs(key=None):
    d = read_json(jobs_path(key), [])
    return d if isinstance(d, list) else []


def save_jobs(jobs, key=None):
    write_json(jobs_path(key), jobs)


def upsert_job(record, key=None):
    jobs = load_jobs(key)
    for i, j in enumerate(jobs):
        if j.get("id") == record.get("id"):
            j.update(record)
            jobs[i] = j
            save_jobs(jobs, key)
            return j
    jobs.insert(0, record)
    save_jobs(jobs, key)
    return record


def find_job(job_id, key=None):
    for j in load_jobs(key):
        if j.get("id") == job_id:
            return j
    return None


# ---------- knowledge（知识库，阶段 1 先留骨架）----------
def knowledge_path(key=None):
    return p_path("knowledge.json", key=key)


def load_knowledge(key=None):
    d = read_json(knowledge_path(key), {"nodes": []})
    if not isinstance(d, dict) or not isinstance(d.get("nodes"), list):
        return {"nodes": []}
    return d


def save_knowledge(kb, key=None):
    write_json(knowledge_path(key), kb)
