# -*- coding: utf-8 -*-
"""多源搜索：按配置顺序尝试，失败自动回退；全失败返回空列表（由调用方降级）"""
import json
import re
import urllib.error
import urllib.request

DEFAULT_MAX = 5


def _http_json_sender(path):
    """造一个搜索源实现：向 `base_url + path` 发 JSON POST，返回解析后的 JSON。

    `tavily` / `bocha` 的调用协议**逐字相同**（同一份 payload、同一个 `Bearer` 头、
    同样 `json.loads` 应答），只有路径后缀不同 —— 所以只有这一份实现（原来 18 行同构
    抄了两遍，是"改一处漏一处"的温床）。后缀由 `REGISTRY` 各自钉住，
    `tests/test_search.py` 里那条 `REGISTRY` 测试盯着它（既有测试全注入假 `http`，
    真实现一行都没被走过，后缀写错也发现不了）。
    """
    def send(base_url, payload, api_key, timeout):
        url = base_url.rstrip("/") + path
        req = urllib.request.Request(
            url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + api_key},
            method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    return send


REGISTRY = {"tavily": _http_json_sender("/search"),
            "bocha": _http_json_sender("/v1/web-search")}


def _normalize(raw):
    """把不同厂商的返回统一成 [{title, url, snippet, date}]"""
    out = []
    if not isinstance(raw, dict):
        return out
    items = raw.get("results") or raw.get("data", {}).get("webPages", {}).get("value") or []
    for it in items:
        if not isinstance(it, dict):
            continue
        out.append({
            "title": it.get("title") or it.get("name") or "",
            "url": it.get("url") or it.get("link") or "",
            "snippet": it.get("content") or it.get("snippet") or it.get("summary") or "",
            "date": it.get("published_date") or it.get("datePublished") or it.get("date") or "",
        })
    return [x for x in out if x["url"]]


def search(query, providers=None, max_results=DEFAULT_MAX, timeout=20, http=None):
    """按顺序尝试各 provider，成功即返回；全失败返回 []

    http(url, payload, api_key, timeout) -> dict  可注入，便于测试
    """
    providers = [p for p in (providers or []) if (p or {}).get("api_key")]
    last_err = None
    for p in providers:
        name = (p.get("name") or "").strip().lower()
        fn = http or REGISTRY.get(name)
        if fn is None:
            last_err = "未知搜索源：%s" % name
            continue
        payload = {"query": query, "max_results": max_results, "count": max_results}
        try:
            raw = fn(p.get("base_url") or "", payload, p.get("api_key") or "", timeout)
            items = _normalize(raw)
            if items:
                return items[:max_results]
            last_err = "%s 返回空结果" % name
        except Exception as e:
            last_err = "%s 失败：%s" % (name, e)
            continue
    if last_err:
        try:
            import log
            log.log_event("search.all_failed", level="warn", query=query, reason=str(last_err)[:300])
        except Exception:
            pass
    return []


def fetch_link(url, timeout=20):
    """抓取岗位分享链接正文（多数招聘站反爬/需登录，失败抛出友好提示）"""
    if not re.match(r"^https?://", url or ""):
        raise RuntimeError("链接格式不对，需要以 http:// 或 https:// 开头")
    req = urllib.request.Request(url, headers={
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
        "Accept-Language": "zh-CN,zh;q=0.9"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            html = resp.read().decode("utf-8", "replace")
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
                           "建议：直接上传 JD 截图，或把 JD 文字粘贴进来。")
    return text[:6000]
