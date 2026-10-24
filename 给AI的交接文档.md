# 给 AI 的交接文档（接手 / 个性化改动用）

> **怎么用这份包**：把整个目录（或 zip）交给你的 AI 编码助手（Claude Code / Codex / DeepSeek Harness /
> Cursor 等），说一句：
> **「先读 `给AI的交接文档.md` 和 `manifest.json`，按里面的硬约束和不变动手；改完跑测试。」**
>
> 这份文档假设你没有读过这个项目的任何历史。它只写**代码里能核对的事实**；数字（文件数、测试数、
> 提交号）以 `manifest.json` 为准，不写死在正文里。

---

## 1. 这是什么

一个**跑在用户自己电脑上**的 AI 求职助手：粘贴岗位 JD（或上传截图）→ 调用用户自己的模型 API
跑一套固定的 6 步 SOP → 产出一份**可交互的 HTML 报告**，并把岗位、简历、知识、面试记录都存成本地文件。

它有 7 个功能面：

| 功能 | 一句话 |
|---|---|
| 分析岗位 | JD → 打分 / 拆解 / 简历大纲 / 优化方向 / 面试题 / 知识地图 → HTML 报告 |
| 岗位台账 | 报告与岗位记录可搜索、可编辑、可看「一个 JD 下多份简历」的并排表 |
| 简历版本库 | 族谱（母版→子版）、每次保存一个版本、母版快照、AI 写的变更说明 |
| 知识库 | 卡片的复习队列 + 掌握度；报告里的勾 = 库里的状态（同一份数据） |
| 面试复盘 | 本地影片/录音 → vidkit 转写 → 模型复盘 → 反哺知识库 |
| 结果与复盘 | 记录 offer 结果 + 用户自己写的原因 → 模型归因 → 复盘报告 |
| 真删岗位 | 只读预演 → 两次确认 → 先备份再删 → 失败逐字节回滚 |

---

## 2. 硬约束（改代码前必须知道，违反会直接退化成另一个项目）

1. **Python 3.9+，只用标准库。** 没有 `requirements.txt`、没有 pip 安装、没有构建步骤。
   HTTP 用 `http.server`，测试用 `unittest`，JSON 就是 `json`。**不要引入任何第三方包**
   （朋友拿到的是一个 zip，装不了依赖；项目历史上因"分享包少文件"出过事故，见 `打包分享.py` 的注释）。
2. **前端是单文件**：`web/index.html`，CSS/JS 全部内联，无框架、无 CDN、无打包器。
   报告页是 `web/report_template.html` + 注入的 `const REPORT_DATA = {...}`。
3. **只监听 `127.0.0.1`**，不做多用户、不做登录、不连数据库。并发模型是 `ThreadingHTTPServer`
   + 进程内锁。
4. **数据全是 JSON 文件**，路径在 `data/profiles/<档名>/` 下（见第 5 节）。
5. **Windows 优先**（`启动.bat`），但代码本身跨平台（用 `os.path`，不拼反斜杠）。
6. **中文文案是产品的一部分。** 用户是求职者，不是工程师。所有报错必须说"人能照做的一句话"，
   而且**不许说假话**：做不到就说做不到，读不出来就说读不出来（第 6 节）。

---

## 3. 架构地图（文件 → 职责 → 不许破坏的点）

| 文件 | 职责 | 不许破坏 |
|---|---|---|
| `app.py` | HTTP 路由、分析编排、报告渲染、SOP 拼装 | 路由的 `ok/error` 语义；body 上限；origin 守卫 |
| `store.py` | **所有**文件读写、原子写、备份轮转、锁、路径 | 原子写；`.bak-` 轮转；`read_*_strict` 与 `*_safe_to_write` 的分工 |
| `log.py` | 事件日志（jsonl）、日志页、反馈包 | 轮转**只增不删**；`log_event` 永不抛给调用方 |
| `llm.py` | 调模型（文本 / 视觉）、JSON 解析与修复 | 超时、错误信息、`parse_json_reply` 的容错口径 |
| `search.py` | 搜索源（tavily / bocha） | 多源并发 + 失败如实上报（不假装搜到） |
| `knowledge.py` | 知识点的**领域过滤**（搜索来源是否与岗位领域相关） | 过滤只删"明显无关"，不新增、不编造 |
| `kb.py` | 知识库唯一写入口、卡片合并、复习调度、跨模块引用接口 | `_with_kb_write` 的闸门语义（第 6 节） |
| `kbreuse.py` | 分析时"先查本地库"的复用判定 | 复用/命中/不够格的**口径**；绝不编造来源 |
| `jdfacts.py` | 知识地图**按岗位缓存**、从旧报告播种 | 缓存键、来源标注（cache / seeded / computed） |
| `jdid.py` | JD 身份（`jds.json`）的读写、改名 | 身份格式；改名的引用重指；`seq` 单调 |
| `jdrefs.py` | **"一个 JD 被哪些东西引用"的唯一真相源** | 引用种类清单（`KINDS`）；整体成功或整体失败 |
| `jddelete.py` | 真删岗位（预演 / 备份 / 删除 / 回滚） | 预演只读；删除前备份 sha 核对；失败逐字节回滚 |
| `jobdup.py` | 合并重复岗位（只标 `merged_into`，不删） | 只标记不删；引用重指与归档顺序 |
| `resumelib.py` | 简历版本库（族谱 / 快照 / 取回 / AI 变更说明） | "最多两份内容文件"；一次保存要么成立要么不成立 |
| `interview.py` | 面试复盘（转写、7 段分析、状态机） | 状态机与中断恢复；不假装成功 |
| `outcome.py` | 结果归因（终态门槛、证据过滤、可控性） | 终态才归因；用户写的原因与 AI 推断分开 |
| `pdftext.py` | 电子版 PDF 抽文字（零依赖） | 绝不吐乱码；解压上限；扫描件明确拒绝 |
| `打包分享.py` / `打包交接.py` | 打分享包 / 打 AI 交接包 | 泄漏扫描 + 依赖自检（少文件就失败） |
| `报告体检.py` / `岗位去重.py` / `备份体检.py` | 维护脚本（无参严格只读） | 无参只读；写前备份；只动自己的数据 |
| `简历迁移.py` / `身份迁移.py` / `迁移脚手架.py` | 一次性老数据迁移（**不随分享包发**） | 名字必须 GBK 可编码（有测试钉着） |
| `tests/` | 全量测试（`unittest`，约 3 分钟） | 每个用例都在临时数据根里跑，**绝不碰真实 `data/`** |

---

## 4. 一次分析的数据流

```
前端「开始分析」
  → POST /api/analyze {jd_text | image(base64) | link, resume_name, resume_id?}
  → app.do_analyze()
      1. 选简历版本（resumelib）→ 读正文
      2. build_system_prompt()：把 5 份 SOP（00/02/03/04/05）拼成 system prompt
         ⚠️ 读不出来的 SOP 会在 prompt 里**明说**缺了哪段，并把 warning 带回响应
      3. 有截图 → llm 视觉模型（Kimi）抄成文字
      4. 调文本模型（DeepSeek）→ 6 段 JSON
      5. 知识地图：jdfacts 缓存 → kb 复用 → search 联网 → knowledge 过滤
      6. normalize()：算总分（各维 score×weight/10）、判定档、清洗字段
      7. 落盘：报告 HTML（模板 + REPORT_DATA）、jds.json、jobs.json、jd/ 原文与截图
  → 响应 {ok:true, report:"<文件名>"}；报告在 /reports/<文件名>
```

**想改提示词/评分**：改 `app.py` 的 `build_system_prompt()`、`normalize()`，以及 `0*-*.md` 这几份 SOP
（它们是 prompt 的正文，改完重启进程即可生效）。

---

## 5. 数据模型（`data/profiles/<档名>/`）

```
config.json                模型配置 + 搜索源（⚠️ 含 API Key）
resumes/<名字>.md          该族谱**最新版**正文
resumes/快照/<版本id>.md    母版快照（覆盖前留一份）
resumes.json               版本记录（族谱）
jobs.json                  分析记录（每次分析一条）
jds.json                   JD 身份（一个岗位一条）
knowledge.json             知识库卡片
report_state/<报告id>.json  报告里的勾选状态
interviews/<面试id>/…       status.json + 转写文本 + 原始音视频
jd/<jd_id>.txt              JD 原文（截图另存在 jd/ 下）
reports/<文件名>.html       报告
logs/events.jsonl          事件日志
```

关键字段（**改数据模型前先读对应模块的 docstring**）：

| 文件 | 主要字段 |
|---|---|
| `config.json` | `text_model{label,base_url,model,temperature,api_key}`、`vision_model{…}`、`search_providers[]`、`max_searches` |
| `jobs.json` | `id`（分析 id）、`jd_id`、`report`（报告文件名）、`created_at`、`score`、`resume`（显示名）、`resume_id`（可能缺）、`merged_into` |
| `jds.json` | `{version, seq, jds:[{id, seq, job_title, job_category, location, salary, company, hr_name, phone, address, status, verdict, notes, jd_text_file, jd_image, source_type, source_link, created_at, merged_into?}]}` |
| `knowledge.json` | `{version, nodes:[{id, term, aliases[], definition, plain_explanation, category, layer, sources[], timeline[], related[], interview_questions[], confidence, from_jds[{id,job_title,date}], state, last_outcome, asked_count, review_count, last_reviewed_at, last_result, first_seen, last_seen}]}` |
| `resumes.json` | `{version, resumes:[{id:"rv_0001", name, root_id, parent_id, rev, at, size, sha256, file, mother_file, chosen, ai_diff{summary,changes[]}, ai_diff_error, note, removed}]}` |
| `report_state/<rid>.json` | `{version, report_id, checks:{"gate:0":true, "know:术语":true, …}}` |
| `interviews/<id>/status.json` | `state`（uploaded / transcribing / analyzing / done / failed / interrupted）+ `interrupted_from` + 错误与结果元数据 |

> **ID 语义**：`jd_id` 是岗位身份（六段归一化短值 + 序号），**报告文件名不是身份**。
> 同一个 JD 换简历再分析 = 同一 `jd_id`、新的分析 `id`。

---

## 6. 不许破坏的不变量（这是这个项目的"宪法"）

1. **原子写**：任何文件都经 `store._atomic_write_bytes` / `write_json` / `save_stream`
   （临时文件 + `os.replace`）。**永远不要**直接 `open(path,"w")` 覆盖用户数据。
2. **写之前先判"能不能写"**：`*_safe_to_write()` 回答的是"写上去会不会丢东西"。
   **读不出来 ≠ 空 ≠ 可以覆盖**（`read_*_strict` 与 `load_*` 是两套信号）。
3. **元素形状也算读不干净**：`{"jobs":[…, 1]}` / `{"nodes":[…, 1]}` ⇒ 写路径**拒绝**，
   绝不允许"读进来过滤掉坏元素、再整份写回"（那会把用户文件里的一行静默抹掉）。
4. **覆盖前留档**：`.bak-`（保留最新 3 份）、母版快照、删除前的整目录备份。
5. **一次保存要么整体成立、要么整体不成立**（`resumelib.create_version` 失败会回滚正文；
   删岗位失败会逐字节回滚）。**不许留"正文在盘上、记录里没有"的中间态**。
6. **锁序**：`_JOBS/_CONFIG` → `_JD/_MERGE` → `_KB` → 写盘。**新代码取锁必须遵守这个顺序**，
   且不要在持内层锁时去拿外层锁。
7. **绝不假装成功**：路由失败回 `ok:false` + 原因；前端只在 `ok===true` 时说"已保存"。
   写日志失败也不许吞（用户得知道这次没记下来）。
8. **知识库坏文件的两条语义**（`kb._with_kb_write`）：读不出来时——写"用户已有的卡"的入口
   **拒绝**；写"用户这次操作"的入口**先逐字节留档再写**；**元素坏**这一档**一律拒绝且不留档**
   （留档会把好卡从活库挪走，用户看到"知识库没了"）。
9. **零依赖**：连测试都不许 import 第三方库。
10. **名字与身份**：`jd_id` 是岗位身份；改名/合并/删除必须走 `jdrefs`（引用点单一真相源），
    不许各写一遍"我改这里、你改那里"。
11. **迁移脚本必须保持 GBK 可编码**（`tests/test_migration.py::GbkSafetyTest`）：
    用户在中文 Windows 下双击运行时，控制台会按 GBK 解码。
12. **探针 / 测试绝不动真实 `data/`**：测试用 `tests/base.py::IsolatedCase`（临时根 + 还原路径）；
    手工验证必须先把 `store.DATA_ROOT` 指到 `tempfile.mkdtemp()`，并在 `finally` 里还原。
    （历史事故：一次探针直接写真实库，145 张卡被清空；所以这条是硬红线。）

---

## 7. 个性化改动的 7 个 recipe

| 想改什么 | 改哪里 | 怎么验 |
|---|---|---|
| **评分权重 / 门槛 / 判定文案** | `app.py`：`build_system_prompt()` 里的 6 维权重说明（25/22/20/15/10/8）与 `02-预筛与打分.md`；`normalize()` 里的 60/45 分档 | `tests/test_app.py` 里搜 `normalize` / `score`；再真跑一次分析看报告顶部 |
| **分析内容（SOP）** | `0*-*.md` 五份文件（它们是 prompt 正文）；`SOP_FILES` 决定拼哪几份 | 改完重启 `python app.py`；报告第 2~7 模块应随 SOP 变化 |
| **报告版式 / 新增一个模块** | `web/report_template.html`（骨架 + 前端渲染）；`app.py` 的 `normalize()` / 渲染函数 | 打开一份报告；再跑 `python 报告体检.py --刷新模板` 看老报告是否重渲染正常 |
| **换模型 / 加一个模型供应商** | 「⚙️ 设置」提交的 `POST /api/config` → `store.DEFAULT_CONFIG`；`llm.py` 的请求构造 | `tests/test_app.py` 的配置用例；真填一个新 base_url 跑一次 |
| **加搜索源** | `search.py`；`config.search_providers[]` 的 `{name,base_url,api_key}` | 不填 Key 时**必须**降级为"没有来源"，绝不编造链接 |
| **加一条新路由** | `app.py` 的 `do_GET` / `do_POST`（`if p == "/api/…"`）；`_json_body()` 读 body；`_send()` 回包 | 参照 `tests/test_app.py` 的 `_sandbox_handler(...)` 写法补用例（含失败分支） |
| **改前端交互** | `web/index.html`（单文件）；`tests/web_probe.py` 用 Node `vm` 真跑页面 JS | 跑 `python -m unittest tests.test_app`（前端断言全在这里） |

---

## 8. 测试与验证

```bash
# 全量（约 3 分钟；当前条数见 manifest.json 的 counts.test_methods）
python -m unittest discover -s tests -t .
# 只跑某个模块
python -m unittest tests.test_kb tests.test_jdid
# 前端探针需要 Node（没装 Node 会**响亮失败**，不会静默跳过）
node --version
```

**验证纪律**（不是建议，是项目规则）：
- 先写**会红的测试**再改实现；改完跑全量。
- 想证明"这条测试真的在管这件事"：在 **`%TEMP%` 的仓库副本**里把判据改坏，看它是否变红。
  **绝不在工作树里做变异**（历史上有人这么做，留下过一段死锁形状的代码）。
- 改数据相关代码前，先看 `tests/test_store.py` 的既有口径（原子写 / 备份 / 严格读）。

---

## 9. 改动地图（关键词 → 文件）

| 关键词 | 去哪里 |
|---|---|
| 打分、总分、verdict、档位 | `app.py::normalize`；`02-预筛与打分.md` |
| system prompt、SOP | `app.py::build_system_prompt`、`SOP_FILES`、`0*-*.md` |
| 报告模板、REPORT_DATA | `web/report_template.html`、`app.py` 渲染函数、`报告体检.py` |
| 台账、jobs、upsert | `store.py`（`load_jobs/save_jobs/upsert_job`）、`app.py` 台账路由 |
| JD 身份、改名、合并、删除 | `jdid.py`、`jobdup.py`、`jddelete.py`、`jdrefs.py` |
| 简历版本、快照、族谱 | `resumelib.py`、`app.py` 的 `/api/resumes*` |
| 知识库、卡片、复习 | `kb.py`、`kbreuse.py`、`knowledge.py` |
| 面试复盘 | `interview.py`（+ 外部 vidkit 目录） |
| 结果归因 | `outcome.py` |
| PDF 上传 | `pdftext.py`、`app.py` 的 `/api/resumes/pdf`、`web/index.html` 的 `rsPdf*` |
| 日志、反馈包 | `log.py`、`app.py` 的 `/api/logs*` |
| 打包 / 泄漏扫描 | `打包分享.py`、`打包交接.py` |

---

## 10. 已知边界（别当成 bug 去"修"，先读理由）

1. **PDF 只支持电子版**（文字可选中）：扫描件明确拒绝；加密不支持；无 ToUnicode 的字体那几页读不出。
2. **简历每族谱最多两份内容文件**（母版快照 + 最新版）：这是"省磁盘 + 可对账"的取舍，
   取不回中间版本原文是**故意的**，界面会如实说。
3. **知识库与报告是两份数据**：删报告不会删卡；报告里的勾与库里的状态是同一件事。
4. **链接抓取常常失败**（招聘站反爬）：链接只用来存档，分析请用"文字 / 截图"。
5. **读路径**会过滤掉不是对象的元素（容错）；**写路径**遇到它们会拒绝（不替用户改文件）。
6. `简历迁移.py` / `身份迁移.py` / `迁移脚手架.py` **不随分享包发**（一次性迁移工具）。

---

## 11. 给 Agent 的开工三步

1. **先读**：本文件 → `README.md` 的「数据存在哪 / 简历版本 / 知识库」几节 → 你要改的那个模块的
   文件头 docstring（这个项目把设计理由写在 docstring 里，读它能省掉一半猜测）。
2. **先跑**：全量测试确认基线是绿的（红的就别往下改）。
3. **改之前问自己三句**：① 项目里是不是已经有同口径的函数（`grep` 一下再写）？
   ② 这次改动会不会破坏第 6 节的某条不变量？③ 这条改动我用什么测试证明它生效、又用什么变异证明
   测试不是假绿？

**禁止**：给真实 `data/` 写任何东西；引入第三方依赖；在工作树里做变异；
把 `config.json` / 真实简历 / 日志正文发到任何网络位置。

---

## 12. 版本信息

用 `manifest.json` 核对（不要相信文档里写死的数字）：

- `git.head` / `git.branch` / `git.dirty`：打包时的提交
- `counts`：文件数、模块数、测试方法数
- `files[]`：每个文件的 `bytes` 与 `sha256`（改了代码后可以对账）
- `modules[]`：每个模块的首行说明、行数、公开函数/类数量

**隐私**：这个包里**没有** `data/`、`config.json`、日志正文或泄漏关键词表；仓库历史文档与测试夹具里的
真实姓名已统一替换成占位符「示例姓名」。你改完代码后，可以用 `manifest.json` 的指纹对比动了哪些文件。
