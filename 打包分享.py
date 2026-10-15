# -*- coding: utf-8 -*-
"""
把项目打包成可分享给朋友的干净 zip。

用法：python 打包分享.py
产物：dist/AI求职助手-分享版-YYYYMMDD.zip

安全保证：
  1. 自动剔除 API Key（config.json 重新生成，key 留空）
  2. 自动剔除个人数据（简历 / 报告 / 岗位台账 / 日志 / 上传的 JD 图与原文）
  3. 打包完成后会反向扫描 zip，确认没有明文 key、没有个人信息，有问题直接报错
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
    "interview.py", "jdfacts.py", "jddelete.py", "jdid.py", "jobdup.py",
    "kb.py", "kbreuse.py", "outcome.py", "resumelib.py",
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


def leak_keywords(root=None):
    """从 .leak-keywords.txt 读取要拦截的个人信息关键词。

    该文件**不入库**（见 .gitignore）——避免为了"检测个人信息"而把个人信息写进源码。
    每行一个关键词，# 开头为注释。文件不存在时只检查 API Key。
    """
    p = os.path.join(root or ROOT, ".leak-keywords.txt")
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
