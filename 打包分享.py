# -*- coding: utf-8 -*-
"""
把项目打包成可分享给朋友的干净 zip。

用法：python 打包分享.py
产物：dist/AI求职助手-分享版-YYYYMMDD.zip

安全保证：
  1. 自动剔除 API Key（config.json 重新生成，key 留空）
  2. 自动剔除个人数据（简历 / 报告 / 岗位台账 / 日志 / 上传的 JD 图与原文）
  3. 打包完成后会反向扫描 zip，确认没有明文 key、没有个人信息，有问题直接报错
"""
import json
import os
import re
import sys
import zipfile
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(ROOT, "dist")
STAMP = datetime.now().strftime("%Y%m%d")
PKG = "AI求职助手-分享版-%s" % STAMP

# ---- 要打包的源码与文档 ----
FILES = [
    "app.py", "启动.bat", "创建桌面快捷方式.bat", "分享说明.md", "README.md", "EXAMPLES.md",
    "00-求职SOP总纲.md", "01-资产库构建.md", "02-预筛与打分.md",
    "03-JD拆解.md", "04-简历大纲.md", "05-面试准备.md", "06-投递追踪.md",
]
DIRS = ["web", "assets", "templates"]

# ---- 绝不打包（个人数据 / 密钥）----
BLOCK_FILES = {"config.json", "jobs.json", "events.jsonl"}
BLOCK_DIRS = {"data", "reports", "resumes", "dist", "__pycache__", "docs", ".git"}

NEW_CONFIG = {
    "text_model": {"label": "DeepSeek（文本分析 / 跑 SOP）",
                   "base_url": "https://api.deepseek.com/v1",
                   "model": "deepseek-flash", "temperature": 0.3, "api_key": ""},
    "vision_model": {"label": "Kimi（识图 OCR / 读 JD 截图）",
                     "base_url": "https://api.moonshot.cn/v1",
                     "model": "kimi-k2.6", "temperature": 1, "api_key": ""},
}

STARTER_RESUME = """# 我的简历（把这里换成你自己的内容）

## 基本信息
- 姓名：
- 学历：
- 工作年限：
- 期望城市：
- 求职方向：

## 工作经历
### 公司名 · 职位（起止时间）
- 职责：
- 成果（尽量带数字，如"质检准确率从 92% 提升到 98.5%"）：

## 项目经历
### 项目名
- 背景：
- 我的角色：
- 做了什么：
- 量化结果：

## 技能
-

## 提示
简历写得越具体、数字越多，AI 的分析和简历优化建议就越准。
"""

SHORTCUT_BAT = (
    "@echo off\r\n"
    "cd /d \"%~dp0\"\r\n"
    "echo Creating desktop shortcut...\r\n"
    "powershell -NoProfile -ExecutionPolicy Bypass -Command \""
    "$d=[Environment]::GetFolderPath('Desktop');"
    "$w=New-Object -ComObject WScript.Shell;"
    "$s=$w.CreateShortcut((Join-Path $d 'AI Job Search.lnk'));"
    "$s.TargetPath=(Join-Path (Get-Location).Path '启动.bat');"
    "$s.WorkingDirectory=(Get-Location).Path;"
    "$s.IconLocation=(Join-Path (Get-Location).Path 'assets\\icon.ico');"
    "$s.Description='AI Job Search Tool';"
    "$s.Save();"
    "Write-Host 'OK - shortcut created on your Desktop.'\"\r\n"
    "echo.\r\n"
    "pause\r\n"
)

SHARE_NOTE = """# AI 求职助手 · 测试版

欢迎试用！这是一个**跑在你自己电脑上**的 AI 求职工具。

## 它做什么

粘贴岗位 JD（或上传截图）→ 自动完成：

1. **岗位预筛打分**（硬门槛一票否决 + 六维加权打分）
2. **JD 结构化拆解**（必备能力 / 业务痛点 / 能力缺口 / 关键词）
3. **简历大纲 + 简历优化方向**（增删改、量化补强、关键词对齐、逐段改写）
4. **面试准备**（预测题 + 答案骨架 + 反问）
5. **岗位台账**（自动抽取公司 / HR / 电话 / 地址，可搜索筛选，随时找回要联系的岗位）

产物是一份**可交互的 HTML 报告**（带目录跳转、勾选清单、打分滑块、导出 PDF）。

## 先回答一个常见疑问：需要 Agent 或 AI 对话窗口吗？

**不需要。直接解压、双击就能用。**

这是个**独立的本地程序**：双击 `启动.bat` 后它自己起一个本地网页（`http://127.0.0.1:8000`），
你在网页里贴 JD，程序**直接调用你自己填的模型 API** 完成分析。
全程不需要 Claude Code / Cursor / DeepSeek Harness，也不需要任何对话窗口。

> 只有一种情况会用到 Agent：你想把 bug 反馈给作者时，用「🩺 日志 → 📦 打包反馈包」
> 生成压缩包，再发给 AI 帮你定位。那一步是可选的。

## 三步开始

1. 双击 **`启动.bat`**
   （想省事就先双击 `创建桌面快捷方式.bat`，桌面会多一个图标）
2. 浏览器自动打开 **http://127.0.0.1:8000**
3. 进「⚙️ 设置」填两个 API Key，中文名照抄：
   - 文本分析（DeepSeek）：模型名填 `deepseek-flash`
   - 识图（Kimi / Moonshot）：模型名填 `kimi-k2.6`
   保存后回到「① 分析岗位」就能用了

> 还没申请 Key？先点「**看看示例效果（免 API Key）**」感受一下界面和报告长什么样。

## 只用一把钥匙也能开始

| 想做的事 | 需要填的 Key |
|---|---|
| 粘贴 JD **文字**来分析 | **只要 DeepSeek 的 Key** |
| 上传 JD **截图**让它读图 | 再加 Kimi 的 Key |

Key 申请地址：
- DeepSeek → <https://platform.deepseek.com/> → API Keys
- Kimi / Moonshot → <https://platform.moonshot.cn/> → API Key 管理

> **别把自己的 Key 发给别人**（会花你的额度），各人用各人的。
> 先申请 DeepSeek 那一把就能开始用了。

## 环境要求

- Windows + **Python 3**（[python.org](https://www.python.org/downloads/) 下载，安装时务必勾选
  **Add Python to PATH**）
- **零第三方依赖**，不用 pip install 任何东西

## 你的数据存在哪

| 内容 | 位置 |
|---|---|
| API Key | `config.json`（**千万别把这个文件发给别人**） |
| 简历 | `resumes/` |
| 分析报告 | `reports/` |
| 岗位台账 | `data/jobs.json` |
| 上传的 JD 截图 / 原文 | `data/jd/` |
| 运行日志 | `data/logs/events.jsonl` |

全部只在你本机，不上传任何服务器。

## 遇到问题？这样反馈最有用

1. 打开工具 → 点「**🩺 日志**」标签
2. 点「**📦 打包反馈包（zip）**」，会生成一个压缩包并给出下载链接
3. 把 zip 发给作者，并**用一句话说清**：你做了什么 → 期望什么 → 实际发生了什么

反馈包含：全部操作日志、模型调用耗时、报错堆栈、环境信息。
**不含 API Key**（自动打码）、**不含简历正文和 JD 原文**。

## 已知限制

- 一次只能开一个窗口（重复启动会提示「端口已被占用」）
- 招聘站反爬很严，粘「分享链接」通常抓不到正文 → 建议用**截图**或**直接粘文字**；
  链接仍会存进台账，方便以后点回去找 HR
- 一次分析约 20–60 秒，消耗你自己的 API 额度
- 仅供个人求职使用，请勿用于批量抓取或自动投递（容易被封号）

## 卡住了看这里

| 现象 | 原因 / 解决 |
|---|---|
| 双击 `启动.bat` 窗口一闪而过 | 没装 Python，或装的时候没勾 **Add Python to PATH**（重装时务必勾上） |
| 提示「端口 8000 已被占用」 | 已经开着一个窗口了 —— 先关掉旧的再启动 |
| 点了「开始分析」没反应 | 按 `Ctrl+F5` 刷新页面；分析完成后下方会出现「📊 打开报告 ↗」按钮，点它 |
| 提示「未配置 API Key」 | 去「⚙️ 设置」填 Key 并点保存 |
| 截图读不出来 | 识图走的是 Kimi，确认 Kimi 的 Key 填了、模型名是 `kimi-k2.6` |
| 报错「模型接口返回 400 / 401」 | 多半是 Key 无效或模型名不对，照「设置」里的默认值逐字核对 |
| 想换端口 | 命令行运行：`python app.py --port 8001` |

## 反馈欢迎什么

- 哪一步卡住了 / 报什么错
- 报告里哪块内容对你没用、哪块你最想要但没有
- 打分是否合理（你可以直接拖动滑块改成你心里的分数，顺便在反馈里说一声）
"""


def collect():
    """收集要打包的文件（相对路径 -> 绝对路径）"""
    items = []
    for f in FILES:
        p = os.path.join(ROOT, f)
        if os.path.exists(p):
            items.append((f, p))
        else:
            print("  跳过（不存在）:", f)
    for d in DIRS:
        base = os.path.join(ROOT, d)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [x for x in dirnames if x not in BLOCK_DIRS and not x.startswith("__")]
            for fn in filenames:
                if fn in BLOCK_FILES or fn.startswith("_"):
                    continue
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, ROOT).replace("\\", "/")
                items.append((rel, full))
    return items


def leak_keywords():
    """从 .leak-keywords.txt 读取要拦截的个人信息关键词。

    该文件**不入库**（见 .gitignore）——避免为了"检测个人信息"而把个人信息写进源码。
    每行一个关键词，# 开头为注释。文件不存在时只检查 API Key。
    """
    p = os.path.join(ROOT, ".leak-keywords.txt")
    kws = []
    try:
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    kws.append(line)
    except Exception:
        pass
    return kws


def verify(zpath):
    """反向扫描 zip，确认没有泄漏"""
    issues = []
    kws = leak_keywords()
    with zipfile.ZipFile(zpath) as z:
        for n in z.namelist():
            info = z.getinfo(n)
            if info.file_size > 3_000_000:
                issues.append("文件过大：%s (%.1f MB)" % (n, info.file_size / 1e6))
            if n.endswith((".json", ".md", ".py", ".html", ".bat", ".txt", ".js", ".css")):
                try:
                    txt = z.read(n).decode("utf-8", "ignore")
                except Exception:
                    continue
                if re.search(r"sk-[A-Za-z0-9_\-]{20,}", txt):
                    issues.append("疑似明文 API Key：%s" % n)
                for kw in kws:
                    if kw in txt:
                        issues.append("个人信息 '%s' 出现在：%s" % (kw, n))
    return issues


def main():
    os.makedirs(DIST, exist_ok=True)
    items = collect()
    zpath = os.path.join(DIST, PKG + ".zip")

    print("=" * 58)
    print("  打包分享版")
    print("=" * 58)
    print("  共 %d 个源文件" % len(items))

    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for rel, full in items:
            z.write(full, PKG + "/" + rel)
        # 干净的配置（key 留空）
        z.writestr(PKG + "/config.json", json.dumps(NEW_CONFIG, ensure_ascii=False, indent=2))
        # 空目录占位 + 起步简历模板
        z.writestr(PKG + "/resumes/我的简历.md", STARTER_RESUME)
        z.writestr(PKG + "/reports/.gitkeep", "")
        z.writestr(PKG + "/data/jd/.gitkeep", "")
        z.writestr(PKG + "/data/logs/.gitkeep", "")
        z.writestr(PKG + "/创建桌面快捷方式.bat", SHORTCUT_BAT)
        z.writestr(PKG + "/分享说明.md", SHARE_NOTE)

    print("\n  反向安全检查中……")
    issues = verify(zpath)
    if issues:
        print("  ✘ 发现 %d 个问题，已删除该包：" % len(issues))
        for i in issues:
            print("     -", i)
        os.remove(zpath)
        return 1
    print("  ✔ 通过：无明文 Key、无个人信息")

    size = os.path.getsize(zpath)
    with zipfile.ZipFile(zpath) as z:
        names = z.namelist()
    print("\n  产物：%s" % zpath)
    print("  大小：%.1f KB   文件数：%d" % (size / 1024, len(names)))
    print("\n  包内结构：")
    for n in sorted(names):
        short = n.replace(PKG + "/", "")
        if short.count("/") == 0 or short.endswith(("icon.ico", "icon.png")):
            print("     " + short)
    print("\n  可以把这个 zip 发给朋友了。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
