# -*- coding: utf-8 -*-
"""
把项目打包成可分享给朋友的干净 zip。

用法：python 打包分享.py
产物：dist/AI求职助手-分享版-YYYYMMDD.zip

安全保证：
  1. 自动剔除 API Key（config.json 重新生成，key 留空）
  2. 自动剔除个人数据（简历 / 报告 / 岗位台账 / 日志 / 上传的 JD 图与原文）
  3. 打包完成后会反向扫描 zip，确认没有明文 key、没有个人信息，有问题直接报错
     （P0-15：个人信息关键词**自动并入当前 profile 自己的档名与简历文件名**，
      不再依赖被 gitignore 的 `.leak-keywords.txt` —— 那个文件在干净检出上不存在，
      于是"关键词表为空 ⇒ 不扫 PII ⇒ 带真名的包照样发出去"，本机只是恰好留着它才偶然拦住）
  4. 反向扫描里还有一条**依赖自检**：从真实仓库推导「app.py 等源码 import 了哪些本地模块、
     引用了哪些 web/ 静态文件」，缺一个就报错并删包（P0-10：以前包里少 9 个 .py，
     朋友解压后双击 启动.bat 直接 ImportError，而 verify() 照样打印「✔ 通过」）。
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
# ⚠️ 这里必须涵盖 **app.py 模块级 import 的每一个本地模块**（P0-10：曾漏 9 个，
#    分享包解压后双击 启动.bat 直接 ImportError）。漏了会在 verify() 的依赖自检里被点名。
FILES = [
    "app.py", "store.py", "log.py", "llm.py", "search.py", "knowledge.py", "migrate_to_profiles.py",
    "interview.py", "jdfacts.py", "jddelete.py", "jdid.py", "jdrefs.py", "jobdup.py",
    "kb.py", "kbreuse.py", "outcome.py", "resumelib.py",
    "备份体检.py",
    "启动.bat", "README.md", "EXAMPLES.md",
    "00-求职SOP总纲.md", "01-资产库构建.md", "02-预筛与打分.md",
    "03-JD拆解.md", "04-简历大纲.md", "05-面试准备.md", "06-投递追踪.md",
]
# 注：`创建桌面快捷方式.bat` 与 `分享说明.md` **盘上没有这个文件**，它们由 SHORTCUT_BAT /
# SHARE_NOTE 字符串直接写进包（见 main()）；挂在 FILES 里只会每次打一行"跳过（不存在）"。
DIRS = ["web", "assets", "templates"]

# ---- 绝不打包（个人数据 / 密钥）----
# 注意：从重构后 app.py 只读 data/profiles/<key>/，jobs.json / events.jsonl 等散落
# 旧路径一并拦住；.leak-keywords.txt 里是真实姓名/手机号，绝不允许进包。
BLOCK_FILES = {"config.json", "jobs.json", "events.jsonl", ".leak-keywords.txt"}
BLOCK_DIRS = {"data", "reports", "resumes", "dist", "__pycache__", "docs", ".git"}


def _blocked(rel):
    """这个相对路径按打包策略**绝不允许**进包（个人数据 / 密钥）？"""
    rel = rel.replace("\\", "/")
    return os.path.basename(rel) in BLOCK_FILES or rel.split("/")[0] in BLOCK_DIRS


NEW_CONFIG = {
    "text_model": {"label": "DeepSeek（文本分析 / 跑 SOP）",
                   "base_url": "https://api.deepseek.com/v1",
                   "model": "deepseek-flash", "temperature": 0.3, "api_key": ""},
    "vision_model": {"label": "Kimi（识图 OCR / 读 JD 截图）",
                     "base_url": "https://api.moonshot.cn/v1",
                     "model": "kimi-k2.6", "temperature": 1, "api_key": ""},
    # 搜索源留空：不配也能用，只是知识点来源会全部标「AI 整理待查证」
    "search_providers": [],
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
6. **知识学习地图**（JD → 12~15 个核心知识点：专业定义 + 通俗解释 + 前世今生时间线 +
   关联知识 + 面试会怎么问；配了搜索源还会附**真实来源链接**，可点开原文）

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
   - （可选）「搜索源」：填 `tavily` 或 `bocha` 的 Key，知识点才会带真实来源；
     不填也能用，只是来源统一标「AI 整理待查证」（不编造链接）
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
| API Key | `data/profiles/<当前用户>/config.json`（**千万别把这个文件发给别人**） |
| 简历 | `data/profiles/<当前用户>/resumes/` |
| 分析报告 | `data/profiles/<当前用户>/reports/` |
| 岗位台账 | `data/profiles/<当前用户>/jobs.json` |
| 知识库 | `data/profiles/<当前用户>/knowledge.json` |
| 上传的 JD 截图 / 原文 | `data/profiles/<当前用户>/jd/` |
| 运行日志 | `data/profiles/<当前用户>/logs/events.jsonl` |

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


def collect(root=None, files=None, dirs=None):
    """收集要打包的文件（相对路径 -> 绝对路径）

    root / files / dirs 可注入，只为让测试能在**假仓库**上跑真实的代码路径；
    默认就是本文件所在仓库 + 模块顶上的 FILES / DIRS。
    """
    root = root or ROOT
    files = FILES if files is None else files
    dirs = DIRS if dirs is None else dirs
    items = []
    for f in files:
        # 二次保险：万一以后有人往 FILES 里加了个人数据文件，也进不了包
        if _blocked(f):
            print("  跳过（禁止打包）:", f)
            continue
        p = os.path.join(root, f)
        if os.path.exists(p):
            items.append((f, p))
        else:
            print("  跳过（不存在）:", f)
    for d in dirs:
        base = os.path.join(root, d)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [x for x in dirnames if x not in BLOCK_DIRS and not x.startswith("__")]
            for fn in filenames:
                if fn in BLOCK_FILES or fn.startswith("_"):
                    continue
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, root).replace("\\", "/")
                items.append((rel, full))
    return items


# ---- 自动并入「当前 profile 自己的 PII」（P0-15）----
# 为什么不能只靠 `.leak-keywords.txt`：那个文件**被 .gitignore**（里面就是真名/手机号，
# 本来也不该入库）⇒ 干净检出 / 新机器上它根本不存在 ⇒ `leak_keywords()` 返回空表 ⇒
# `verify()` 只查 API Key、**不查 PII** ⇒ 带真名的分享包照样通过、可以发出去。
# 实测：本机恰好留着那个文件，才偶然拦住 app.py / README.md 里的真名（否则它早发出去了）。
#
# 所以关键词表必须**自成来源**：不看那个可选文件，直接从 `data/profiles/<当前档>/` 推导 ——
# "当前这个用户自己的名字"本身就是判据，不需要任何人再维护一份清单。
#
# 只吃「**名字**」这一类短标识，绝不读进任何正文：
#   - 数据来源只有两处：`data/current_profile.txt` 的那一行档名（= `store.current_profile()`
#     的来源）与 `data/profiles/<档>/resumes/` 的**文件名**（`os.listdir`，正文一个字节都不读）；
#   - 简历的**显示名**就是文件名主干：`resumelib.add_version(name=…)` 里 `name` 直接决定
#     `resumes/<name>.md`（见 `_resume_rel(clean)`），所以扫目录等价于读 `resumes.json` 的
#     `name` / `file` 字段，还省掉一次"解析用户 JSON"的风险（P0-6 那类坏文件正是解析出来的）；
#   - ⚠️ 不扫 `resumes/快照/`：里面的名字是版本 id（`rv_0001`），不含身份信息；
#   - ⚠️ 不扫 `config.json`：本仓库的配置里**没有任何姓名字段**（只有 text_model /
#     vision_model / search_providers / max_searches，见 `store.DEFAULT_CONFIG`）——
#     哪天真加了"我的名字"这种字段，往 `_profile_name_sources()` 里加一行即可；
#   - 数据源严格限定在 `data/profiles/` 之内（绝不"顺便"去扫 ~/.ssh 之类家目录）。
_SEP_RE = re.compile(r"[\s\-_.·|/\\()（）【】\[\]]+")

# 长度判据 —— 过短的关键词等于「把每一个包都判成泄漏」（`verify()` 一报就删包）：
#   - 含非 ASCII（中文姓名）：至少 **2 个字**。单字太泛（"张"、"李"），而且 1 个字在源码/
#     文档里的出现率极高，一个这样的关键词就能让这个工具**永远打不出包**。
#   - 纯 ASCII（拼音档名 / 英文简历名）：至少 **3 个字符**且至少含一个字母。两字母组合
#     （"in" / "cv" / "ok" / "md"）在源码里到处都是；纯数字（"2024"）只是年份/序号，不是身份。
_ASCII_MIN = 3
_CJK_MIN = 2

# 通用词：**不是身份**，却必然会被从文件名里切出来（或本来就是兜底档名）。
# 依据是实测，不是拍脑袋：
#   - `main()` **自己**往包里写 `data/profiles/default/resumes/我的简历.md` ⇒ "default" /
#     "我的" / "简历" 一旦入表，**每一个包都会被它自己写进去的起步简历判成泄漏**；
#   - 本机实测 `data/current_profile.txt` 的内容就是 `default`（用户没设过档名）；
#   - README / app.py 满篇"母版 / 快照 / 版本 / 简历"，简历文件名也常带这些分类词。
# 代价是"某人的真名恰好就是这些词"时漏检 —— 与"每个包都被删"相比，这个方向更合理。
#
# ⚠️ 匹配方式（2026-10-16 改过 —— 修 P0-15 的第 4 条漏检，理由见 `_is_generic`）：
#   - 英文只按**整串**判：`cv` / `new` / `old` 当子串会误伤真英文名
#     （"newman" / "goldman" / "cvmaker" 全被吃掉），只认"这一段就等于这个词"。
#   - 中文：整段**被通用词吃干净**才算通用；只粘着一个"版次词"的（`张三母版`）不算 ——
#     旧实现是"任意通用词是子串就整段丢"，于是 `张三母版.md` 整段被丢、真名一起丢。
#
# 通用词分两桶，**退化行为不同**（这次修复的核心）：
#   * `_GENERIC_CJK_STICKY`（版次/状态）：它们**粘在人名后面**是最常见的命名
#     （`张三母版.md` / `李四终版.md`，真名后面连个 `-` 都不加）⇒ 只把这几个词本身挖掉，
#     **前面粘着的名字必须留下**（旧行为整段丢 = 真名跟着进不了关键词表 = 带真名的包
#     照样过 `verify()`，与 P0-15 要防的事一模一样）；
#   * `_GENERIC_CJK_WHOLE`（文件是什么）：前面粘着的通常是**限定词而不是名字**
#     （`个人简历.md` / `中文简历.md`）⇒ 保持旧行为整段丢。绝不能从 `个人简历` 里切出
#     "个人"当关键词：分享包自己就写着"仅供个人求职使用" ⇒ 那种词会让**每个包**都被判成
#     泄漏（另一个方向的错，同样不许）。
# ⚠️ 两张表加起来 = 原来那 18 个词，**一个字都没删**（不许为了让粘连用例通过就放宽表）。
_GENERIC_CJK_STICKY = ("母版", "版本", "快照", "终版", "定稿", "草稿", "初稿",
                       "备份", "副本", "新版", "旧版")
_GENERIC_CJK_WHOLE = ("简历", "我的", "通用", "模板", "正文", "汇总", "示例")
_GENERIC_CJK = _GENERIC_CJK_STICKY + _GENERIC_CJK_WHOLE
_GENERIC_ASCII = frozenset([
    "default", "resume", "cv", "final", "copy", "master", "version", "backup",
    "new", "old", "template",
])


def _generic_residues(word):
    """把片段里出现的**所有**通用词挖掉，返回剩下的碎片（挖空处拿 `\\x00` 记个坑）。

    `我的简历` → `[]`（整段就是通用词的串联）；`张三母版` → `['张三']`（真名留下）；
    `newman` → `['newman']`（英文表只认整串，`new` 不会把 `newman` 切开）。
    """
    out = str(word or "")
    for g in _GENERIC_CJK:
        if g in out:
            out = out.replace(g, "\x00")
    return [p for p in out.split("\x00") if p]


def _is_generic(word):
    """这一段只是"简历文件的分类词"，不是身份？（匹配方式的取舍见上面那两条注释）

    ⚠️ 判据 2026-10-16 换过（P0-15 第 4 条漏检）：**不再**是"任意通用词是子串即整段丢"，
    而是「整段被通用词吃干净」或「整段里含『文件是什么』类词」：
      - `我的简历` / `cv` / `new` ⇒ 通用（挖掉 我的+简历 后什么都不剩）⇒ **仍然要丢** ✓
        —— 包自己就会写 `data/profiles/default/resumes/我的简历.md`，起步简历正文第一行
        也是「# 我的简历…」，它一旦入表，**每个包都会被自己判成泄漏**；
      - `张三母版` ⇒ **不通用**（挖掉"母版"还剩"张三"，那是真名）⇒ 不许再整段丢 ✓；
      - `个人简历` ⇒ 通用（含"简历"这桶词）⇒ 整段丢，绝不切出"个人"。
    """
    word = str(word or "")
    if word.lower() in _GENERIC_ASCII:
        return True
    if any(g in word for g in _GENERIC_CJK_WHOLE):
        return True
    return not _generic_residues(word)


# 包**自己一定会带上**的两段样板文本（`main()` 直接写进 zip）。只拿来当"**已知非 PII 词源**"，
# ⚠️ **不是**把它们整段当通用词表（那会把整段文本里的每个词都误杀）。
#
# 安全论证（一句话）：这两段是**本工具自己的常量**，按构造**不含用户 PII** ⇒ 凡是出现在它们
# 里的词，**不可能是"用户的真名"** ⇒ 把命中它们的残余丢掉，**不会掩盖任何真实泄漏**。
# （唯一理论例外：用户真名恰好就是样板文本里的某个普通词 —— 那已被上面"真名恰等于通用词
#   会漏检"这条**已知取舍**覆盖，方向与它一致。）
#
# 为什么非丢不可：不丢的话，`个人终版.md` / `中文母版.md` 这种"限定词 + 版次词"的档名会切出
# `个人` / `中文` —— 而分享说明里就写着"仅供个人求职使用" / "中文名照抄" ⇒ `verify()` 点名、
# **每一个包都被删**（fail-closed 删包），正是"另一个方向的错"。
_SAMPLE_TEXT = STARTER_RESUME + "\n" + SHARE_NOTE


def _in_sample_text(word):
    """这个词出现在包自带的样板文本里？（= 已知的非 PII 词源，见上面那条安全论证）"""
    return bool(word) and word in _SAMPLE_TEXT


def _usable_keyword(word):
    """这个词够不够"具体"到可以当泄漏关键词？（长度判据见上面那条注释）"""
    if not word:
        return False
    if all(ord(c) < 128 for c in word):
        return len(word) >= _ASCII_MIN and bool(re.search(r"[A-Za-z]", word))
    return len(word) >= _CJK_MIN


def _name_fragments(text):
    """把一个"名字"切成可用片段：`示例-姓名-母版.md` → `示例` / `姓名` / `母版` 三段，
    其中 `示例`、`母版` 是通用词被丢掉，最后只剩 `姓名`。

    ⚠️ 整段**不是**通用词、但**粘着**版次类通用词时（`张三母版`），只把通用词挖掉、把真名
    留下（`张三`）—— 旧实现"子串命中就整段丢"会把真名一起丢掉（P0-15 第 4 条漏检，
    这种"真名后面直接跟母版/终版、不加连字符"的命名非常常见）。

    ⚠️ 但**挖出来的残余**要再用 `_in_sample_text` 过一遍：落在包自带样板文本里的（`个人终版`
    → `个人`）是**已知的非 PII 词源**，留着只会让每个包都被自己判成泄漏（安全论证见上面
    `_SAMPLE_TEXT` 那段）。这一刀只砍样板文本里的词，真名（`张三母版` → `张三`）照留。

    ⚠️ 只对**文件名主干 / 档名**这类短标识调用，绝不对正文调用。
    """
    out = []
    for part in _SEP_RE.split(str(text or "")):
        if _is_generic(part):
            continue
        for piece in _generic_residues(part):
            if not _usable_keyword(piece) or _is_generic(piece) or _in_sample_text(piece):
                continue
            out.append(piece)
    return out


def _profile_keys(root):
    """该扫哪些档？返回档名列表。

    口径与 `store.current_profile()` 一致（文件不存在 → default；内容为空 → default），
    但**多一条 fail-safe**：档名文件存在却读不出来时，`store` 是抛 `ProfileUnreadable`
    （拒绝启动），这里只是"**不知道当前是谁**" ⇒ 改成把 `data/profiles/` 下**每个档**都扫。
    宁可多拦几个（都是用户自己的档），也不在"读不出来"时静默一条 PII 都不扫 ——
    那不扫的样子**和以前那个假绿灯一模一样**，正是本项要根治的习惯。
    """
    fp = os.path.join(root, "data", "current_profile.txt")
    try:
        with open(fp, "r", encoding="utf-8") as f:
            key = f.read().strip()
        return [key] if key else ["default"]
    except FileNotFoundError:
        return ["default"]
    except Exception:
        pass
    pr = os.path.join(root, "data", "profiles")
    try:
        return sorted(d for d in os.listdir(pr) if os.path.isdir(os.path.join(pr, d)))
    except OSError:
        return []


def profile_leak_keywords(root=None):
    """从**当前 profile 自己的名字**推导要拦截的 PII 关键词（P0-15，不依赖任何配置文件）。

    只读两处短标识：档名 + `resumes/` 下的**文件名**（理由见文件里那一段注释）。
    推不出来（没有 `data/`、名字全是通用词/太短）就返回 `[]` —— 那说明这台机器上
    没有"自己的名字"可保护，不是失败。
    """
    root = root or ROOT
    kws, seen = [], set()

    def add(words):
        for w in words:
            if w not in seen:
                seen.add(w)
                kws.append(w)

    for key in _profile_keys(root):
        add(_name_fragments(key))
        resumes = os.path.join(root, "data", "profiles", key, "resumes")
        try:
            names = sorted(os.listdir(resumes))
        except OSError:
            continue
        for n in names:
            # 只认**文件**：`resumes/快照/` 是目录，直接跳过（版本 id 不是身份）。
            if os.path.isfile(os.path.join(resumes, n)):
                add(_name_fragments(os.path.splitext(n)[0]))
    return kws


def leak_keywords(root=None):
    """要拦截的个人信息关键词：**可选的人工补充表** + **自动并入的当前 profile 自己的 PII**。

    `.leak-keywords.txt` 是可选的（被 .gitignore，干净检出/新机器上没有它）；真正兜底的是
    自动推导（见 `profile_leak_keywords`）。所以 P0-15 之后，即使那个文件不存在，
    "分享包里出现用户自己的名字"也一定会被 `verify()` 拦下 —— 以前这里是"文件不在就
    静默不扫 PII"，那正是假绿灯。每行一个关键词，# 开头为注释。
    """
    root = root or ROOT
    kws = []
    p = os.path.join(root, ".leak-keywords.txt")
    try:
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    kws.append(line)
    except Exception:
        pass
    for kw in profile_leak_keywords(root):
        if kw not in kws:
            kws.append(kw)
    return kws


# ---- 依赖自检（P0-10）----
# 这里推的是「**盘上真实存在**、且会被源码 import / 引用的文件」，不是「包里已经有什么」。
# 拿 zip 自己的 namelist 当基准 = 自证循环，永远通过 —— 那正是本项要根治的假绿灯。
_IMPORT_RE = re.compile(r"(?m)^\s*(?:import|from)\s+([A-Za-z_]\w*)")
_ASSET_RE = re.compile(
    r"[\"'](/?[A-Za-z0-9_][A-Za-z0-9_\-./]*"
    r"\.(?:html|css|js|json|ico|png|jpg|jpeg|svg|woff2?))[\"']")


def _scan_py_sources(root, files=None):
    """依赖自检要读的源码：包内会带上的根目录 .py + app.py（永远扫）。

    app.py 永远在扫描名单里：它是"分享包能不能起来"的入口，
    即使哪天有人手滑把它从 FILES 里删掉，自检也不会因此失明。
    """
    names = set()
    for f in list(files if files is not None else FILES) + ["app.py"]:
        f = f.replace("\\", "/")
        if "/" in f or not f.endswith(".py"):
            continue
        if os.path.isfile(os.path.join(root, f)):
            names.add(f)
    return sorted(names)


def _read_text(path):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except Exception:
        return ""


def resolve_dependencies(root=None, files=None):
    """从**真实仓库**推导「分享包里必须有的文件」清单（相对路径，已排序）。

    两条都是真实引用，不写死清单：
      1. 包内根目录 .py（含 app.py）的本地 import —— 仓库里存在同名 .py 才算依赖；
      2. 这些 .py 里引用的 web/ 静态文件（报告模板 / 示例数据 / 首页）—— 盘上真实存在才算。
    新增一个本地模块或一个模板而忘了打包，这里就会多出一条，verify() 随后点名。
    """
    root = root or ROOT
    deps = set()
    for name in _scan_py_sources(root, files):
        txt = _read_text(os.path.join(root, name))
        for mod in _IMPORT_RE.findall(txt):
            if mod == os.path.splitext(name)[0]:
                continue      # 自己 import 自己不算
            if os.path.isfile(os.path.join(root, mod + ".py")):
                deps.add(mod + ".py")
        for ref in _ASSET_RE.findall(txt):
            ref = ref.lstrip("/")
            # 裸文件名先在 web/ 下找，再当仓库根下的路径找；带 / 的按仓库根算。
            # ⚠️ 被 BLOCK_* 拦下的文件（config.json / data/… ）永远不算依赖：
            #    否则自检会反过来要求把 API Key 打进分享包。
            cands = (ref,) if "/" in ref else ("web/" + ref, ref)
            for cand in cands:
                cand = cand.replace("\\", "/")
                if _blocked(cand):
                    continue
                if os.path.isfile(os.path.join(root, cand)):
                    deps.add(cand)
                    break
    return sorted(deps)


def _strip_pkg_prefix(names):
    """把 zip 里的「顶层目录/相对路径」归一成相对路径集合（包名叫什么都不影响自检）。"""
    out = set()
    for n in names:
        n = n.replace("\\", "/")
        out.add(n.split("/", 1)[1] if "/" in n else n)
    return out


def dependency_issues(root=None, packaged=None):
    """依赖自检：返回「被真实引用、却不在包里」的文件（空列表 = 包能跑）。"""
    have = _strip_pkg_prefix(packaged or [])
    return [rel for rel in resolve_dependencies(root) if rel not in have]


def verify(zpath, root=None):
    """反向扫描 zip，确认没有泄漏、而且**真的跑得起来**"""
    root = root or ROOT
    issues = []
    kws = leak_keywords(root)
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
        # ---- 依赖自检：以前只查"有没有泄密"，不查"能不能跑"，于是印出假绿灯 ----
        # 基准来自**真实仓库**（app.py 等源码在盘上写了什么 import、引用了哪些 web 资源），
        # 绝不来自 zip 自己的 namelist —— 拿包内已列文件当基准是自证循环，永远通过。
        for rel in dependency_issues(root, z.namelist()):
            issues.append("缺少依赖文件：%s（解压后双击 启动.bat 会 ImportError / 报告缺模板）" % rel)
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
        # 重构后 app 只读 data/profiles/<key>/ 下的数据（store.p_path），
        # 所以配置与起步简历**必须写进 profile 目录**，否则：
        #   - 测试者打开「简历」下拉框是空的（根目录的 resumes/ 不会被读取）
        #   - 改根目录的 config.json 不生效
        # data/profiles/default/{reports,jd,logs} 由 store.ensure_profile() 按需创建。
        z.writestr(PKG + "/data/profiles/default/config.json",
                   json.dumps(NEW_CONFIG, ensure_ascii=False, indent=2))
        z.writestr(PKG + "/data/profiles/default/resumes/我的简历.md", STARTER_RESUME)
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

    with zipfile.ZipFile(zpath) as z:
        names = z.namelist()
    deps = resolve_dependencies()
    n_py = len([n for n in names if n.endswith(".py")])
    print("  ✔ 通过：无明文 Key、无个人信息")
    print("     依赖自检通过：包内含 %d 个 .py 模块；%d 个被 import / 引用的文件全部在包内"
          % (n_py, len(deps)))

    size = os.path.getsize(zpath)
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
