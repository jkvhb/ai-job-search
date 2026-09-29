# -*- coding: utf-8 -*-
"""模型调用：OpenAI 兼容 chat / 识图 OCR / JSON 解析与自动修复"""
import json
import re
import urllib.error
import urllib.request

import store


def build_payload(model, messages, temperature=None, json_mode=False):
    payload = {"model": model, "messages": messages, "stream": False}
    if temperature is not None:
        payload["temperature"] = temperature
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    return payload


def call_openai_compatible(base_url, api_key, model, messages, json_mode=False,
                           timeout=240, temperature=None):
    """调用 OpenAI 兼容 /chat/completions；对 temperature / response_format 不支持时自动降级重试"""
    if not api_key:
        raise RuntimeError("未配置 API Key，请到「设置」里填写")
    url = base_url.rstrip("/") + "/chat/completions"
    payload = build_payload(model, messages, temperature, json_mode)
    req = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + api_key},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        low = detail.lower()
        if json_mode and ("response_format" in low or "json_object" in low or "not supported" in low):
            return call_openai_compatible(base_url, api_key, model, messages, json_mode=False,
                                          timeout=timeout, temperature=temperature)
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


def parse_json_reply(text):
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    first_err = "返回内容不是 JSON 对象"
    try:
        obj = json.loads(t)
        if isinstance(obj, dict):
            return obj
    except Exception as e:
        first_err = e
    i, j = t.find("{"), t.rfind("}")
    if i != -1 and j > i:
        try:
            obj = json.loads(t[i:j + 1])
            if isinstance(obj, dict):
                return obj
        except Exception as e:
            first_err = e
    raise RuntimeError("JSON 解析失败：%s" % first_err)


def ocr_image(image_data_url, cfg):
    vm = cfg["vision_model"]
    messages = [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": image_data_url}},
        {"type": "text", "text": "请完整、逐字转录这张图片中的所有文字内容，包括公司名、岗位名称、薪资、地点、"
                                 "岗位职责、任职要求、HR 姓名、联系电话、工作地址等所有字段。"
                                 "保持原文，不要总结，不要遗漏任何一行。只输出转录的文字。"}]}]
    return call_openai_compatible(vm["base_url"], vm["api_key"], vm["model"], messages,
                                  timeout=180, temperature=vm.get("temperature"))


def repair_json(tm, broken, err):
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
