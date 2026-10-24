# 简历上传支持 PDF（电子版）—— 设计

日期：2026-10-24 ｜ 状态：待用户复核 ｜ 关联：`web/index.html` 的「编辑简历」、`POST /api/resumes`

## 1. 目标与非目标

【大白话】现在「选择文件」只收 `.md` / `.txt`，选 `.pdf` 会被明确挡回去（这是对的，因为二进制按文本读进来
就是乱码）。要做的是：**选一份电子版 PDF → 工具自己把文字抽出来 → 放进编辑区 → 你看过、能改 → 点保存**，
入库的仍然是正常的 `.md` 简历，版本库、快照、AI 变更说明一切照旧。

【技术口径】新增一个**纯标准库**的 PDF 取文字模块 + 一条只读路由；**不改** `POST /api/resumes` 的契约
（它仍然只接 `{name, content}` 文本），所以保存链路、版本库、快照、sha 校验、`ai_diff` 全部不动。

**非目标（明确不做，别当成漏了）**

| 不做 | 为什么 |
|---|---|
| 扫描件 / 拍照 PDF 的 OCR | 标准库没有 OCR；要做得引依赖或调视觉模型（会花钱、有错字）。这类**明确拒绝**并说清该怎么做 |
| `.docx` / `.doc` | 本轮只要 PDF；`.docx` 是 zip+XML，将来是另一件小事 |
| 版面还原（分栏、表格、字号） | 抽的是**文字流**，不是排版。分栏 PDF 可能串行 —— 界面要如实说"抽的是文字，遇到分栏可能串行" |
| 加密 / 有密码的 PDF | 不解密。明确拒绝，让用户另存为无密码版本 |
| 改 PDF 原文件 | 只读：PDF 一个字节都不写、也不保存原件 |

## 2. 现状与改动点（都有证据）

| 位置 | 现状 | 改动 |
|---|---|---|
| `web/index.html:300` | `<input type="file" id="resumeFile" accept=".md,.txt">` | `accept=".md,.txt,.pdf"` + 提示文案 |
| `web/index.html:2703-2738` | `rsIsTextFile()` 只认 `.md/.txt`；非文本 → `alert` 拒绝且**不读文件、不动编辑区** | 加 `rsIsPdf()`；`.pdf` 走新分支（`readAsArrayBuffer` → base64 → 新路由） |
| `app.py:2822` | `POST /api/resumes` 只接文本 | **不动** |
| `app.py:1284` | `save_resume_version` 用 `os.path.splitext(...)[0]` | **不动**：`我的简历.pdf` → 名字 `我的简历`、落盘 `resumes/我的简历.md`（已符合预期） |
| `tests/test_app.py:4782` | 钉着 `.docx`/`.pdf` 被拒绝 | **改为**：`.docx` 仍拒绝；`.pdf` 走新路径（成功/失败两条） |
| `打包分享.py` `FILES` | 无 `pdftext.py` | 加入（依赖自检会当场抓） |

## 3. 接口：`POST /api/resumes/pdf`

请求：`{"name": "我的简历.pdf", "b64": "<base64>"}`

响应（200）：`{"ok": true, "name": "我的简历", "text": "...", "chars": 1234, "pages": 3, "warnings": ["..."]}`

| 情形 | 码 | 人话（要点） |
|---|---|---|
| 名字不是 `.pdf` | 400 | 「只接 PDF：`x.docx` 请另存为 .md/.txt 或用 PDF 再传」 |
| `b64` 缺失/解不开 | 400 | 「上传内容为空 / 不是合法的 base64」 |
| 不是 PDF（无 `%PDF-` 头） | 400 | 「这份文件不是 PDF（或缺了 PDF 头）—— 是不是改了扩展名？」 |
| 超过 `PDF_MAX_BYTES`（5 MB） | 400 | 「PDF 太大（本次 X MB，上限 5 MB）：请先压缩/另存为精简版」 |
| 抽不出文字 | 400 | 「这份 PDF 里抽不出文字（多半是图片版/扫描件）……请另存为 .md/.txt 或先 OCR」 |
| 加密 | 400 | 「这份 PDF 加密了……先另存为无密码版本」 |
| 解析意外崩 | 500 | 「解析 PDF 时出错：<原因>」+ 日志 `resume.pdf_extract_error`（**绝不假装成功**） |

**限流/限额**：请求体仍受 `MAX_JSON_BODY_BYTES`（8 MB，`app.py:128`）管；base64 后 5 MB PDF ≈ 6.7 MB，
在 8 MB 内。前端**先在本地**挡 5 MB（省一次上传），服务端**再挡一次**（不能只靠前端）。

**文件名口径**：`name` 用 `rsStem()` / `os.path.splitext()` 取干；`我的简历.pdf` → 编辑区显示「我的简历.md」
（用户看到的是**将要落盘的名字**，不是 `.pdf`）。

## 4. 前端流程（`rsFileChosen` 的第三个分支）

```
选文件 → rsIsPdf(name)?
   ├─ 否 → 原有 .md/.txt 分支；其它 → 原有"明确拒绝"（不读、不动编辑区）
   └─ 是 → ① f.size > 5MB → alert 拒绝（不读）
           ② FileReader.readAsArrayBuffer(f)
           ③ rsB64FromBytes(buf)   ← 纯 JS 实现的 base64（**不用 btoa**：Node 探针里没有它，
                                      也避开超大数组的 String.fromCharCode 调用栈问题）
           ④ POST /api/resumes/pdf  {name, b64}
           ⑤ ok → 编辑区 = r.text；pill.name = stem + '.md'；提示「已从 PDF 抽出 N 页 / M 字符…
                    保存时才登记为版本」；warnings 非空时逐条显示（如"第 2 页有字体无法映射"）
              fail → alert(server.error)，**编辑区与 pill 一个字都不动**（与 .docx 拒绝同一纪律）
```

日志：`resume.pdf_loaded`（页数/字符数）、`resume.pdf_rejected`（原因）、`resume.pdf_read_failed`。

## 5. 新模块 `pdftext.py`（纯标准库）

```python
class PdfError(Exception): ...          # 人话消息，路由翻成 400
class PdfText:                          # text / pages / chars / warnings
    ...

def extract_text(data: bytes, *, max_pages=50) -> PdfText
```

解析步骤（每步都有"读不出来就如实拒绝"的出口，绝不吐 `\uFFFD` 冒充）：

1. **入口校验**：前 1024 字节里找 `%PDF-`；trailer 里有 `/Encrypt` → `PdfError("加密")`。
2. **对象表**：先按 `startxref` 读经典 xref 表；任何异常 → 全文件扫 `\d+\s+\d+\s+obj` 兜底
   （不追求 PDF 规范完备，追求"WPS/Word 导出的 PDF 能读"）。
3. **页树**：`/Root` → `/Pages` → 递归 `/Kids`（深度 ≤ 32，页数 ≤ `max_pages`）；每页 `/Contents`
   （可能是数组）按顺序拼起来解码。
4. **过滤器**：`FlateDecode`（`zlib`）+ 预测器按需；`ASCIIHexDecode` 顺带支持；其它过滤器
   （含 `LZWDecode` / `ASCII85Decode`）**跳过该流并记 warning**（不猜、不写没测过的代码）。
5. **内容流取词**：`BT/ET`、`Tf`（换字体）、`Td/TD/T*/Tm/TL`（定位 → 判换行）、`Tj/TJ/'/"`（出字）、
   `q/Q/gs/Do/cm`（跳过）。`TJ` 里数值 `< -100` 且**两侧都不是 CJK** 时补一个空格（中文之间不补）。
6. **字体/编码**：`/ToUnicode` CMap（`bfchar`/`bfrange`）→ 字节串 → Unicode；
   简单字体无 ToUnicode 时按 `/Encoding`（WinAnsi/PDFDoc）兜；**Type0/CID 且无 ToUnicode →
   该页标"字体无法映射"并跳过**（warning 里说第几页）。所有页都这样 → `PdfError`。
7. **归一**：去掉除 `\n\t` 外的控制字符、行尾空白、三连以上空行；`strip()` 后为空 → `PdfError`。

**安全上限**（防解压炸弹；超了如实拒绝，不硬扛）：单流解压后 ≤ 8 MB、总解压预算 ≤ 64 MB、
对象数 ≤ 20000、页 ≤ 50、页树深度 ≤ 32、`zlib.decompressobj(max_length=…)` 截断**并判超限**。

## 6. 测试计划（先红后绿）

**`tests/test_pdftext.py`（新建，约 18 条）** —— 夹具是**测试里手写的合成 PDF**（不引任何库）：
1. 最小字面串 PDF（`(Hello)`）；
2. `FlateDecode` 压缩流；
3. `TJ` 字距（`[(A) -300 (B)]` → `A B`；中文两侧不补空格）；
4. `Td`/`T*` 换行与多页；
5. `Type0` + `ToUnicode` `bfchar`（中文 CID → 汉字）；
6. `bfrange` 批量映射；
7. `Type0` **无** ToUnicode → warning + 不返回乱码；
8. 所有页都无法映射 → `PdfError`；
9. 加密 → `PdfError`；
10. xref 故意坏（偏移全错）→ 扫 `obj` 兜底仍能读出；
11. 解压炸弹（`0x00` × 100 MB 压成几 KB）→ 拒绝且**耗时 < 1s**；
12. 非 PDF 字节 → `PdfError`；13. 空文字 PDF → `PdfError`；
14. 页数超过 `max_pages` → **拒绝并说明**（不截断：截断会让人以为简历只有 50 页）；
15–18. 奇怪但真实的形状：数组 `/Contents`、缺 `/Root`、`ASCIIHexDecode`、行尾 `\r\n`。

**`tests/test_app.py`（改 + 增，约 8 条）**：成功（合成 PDF → `ok:true` + `text`/`pages`）、
名字不是 pdf、base64 坏、超 5 MB、非 PDF 头、抽不出文字（400 人话）、解析崩溃（500 + 日志）、
**任何失败都不写盘**（目录快照前后一致）。原 `ResumeFilePickRejectTest` 里 `.pdf` 的期望改为"新路径"。

**前端探针（`tests/web_probe.py` 场景，约 6 条）**：`.pdf` 走 `readAsArrayBuffer` 且 `fetch` 打到
`/api/resumes/pdf`；成功填编辑区且提示含页数/字符；失败**不动**编辑区；`.docx` 仍拒绝；
`a.PDF` 收、`a.pdf.exe` 拒；`rsB64FromBytes` 与 Python `base64` 对同一字节串结果一致（含 0x00/0xFF）。

**打包**：`pdftext.py` 进 `FILES`；`test_pack` 的依赖自检必须绿。

**变异证据（只在内存/临时副本里做）**：①去掉 `%PDF-` 校验 ⇒ 非 PDF 用例红；
②去掉解压上限 ⇒ 炸弹用例红（或耗时爆）；③去掉 ToUnicode 分支 ⇒ 中文用例红。

## 7. 兼容与回滚

- 新增模块 + 新增路由 + 前端第三分支；**`POST /api/resumes`、版本库、报告、分析全部零改动**。
- 回滚 = 删掉 `pdftext.py`、路由、前端分支（`accept` 改回）⇒ 回到今天的行为，无数据迁移。
- 用户盘上不会多出任何 PDF 副本：PDF 只在请求体内过一次内存。

## 8. 风险与已知边界（写进 README 的一句话）

1. **扫描件不支持**（会明确拒绝）——最常见的"以为支持了"的误会，文案必须说清。
2. **分栏/表格可能串行**；抽的是文字流，不是版面。
3. **字体没 ToUnicode 的 PDF 会有一部分页读不出**（如实 warning 并拒绝，不给乱码）。
4. **5 MB / 50 页**上限（简历足够；超了明确说）。
5. WPS / 浏览器"打印成 PDF"产物的怪癖只能用真实样例验证 —— **请用户给一份真简历 PDF 当夹具**
   （放进仓库根目录即可，我把它只读用；要不要入库由用户定）。

## 9. 验收判据（做到这些才算完）

1. 选一份电子版 PDF → 编辑区出现**可编辑**文字（页数/字符数如实显示）→ 保存 → 版本库里是
   `<名字>.md`，`rev/快照/sha` 与手动上传 `.md` 完全一致。
2. 扫描件 / 坏 PDF / 超大 / 加密 → **明确人话拒绝**，编辑区一个字不动，盘上不多任何文件。
3. 全量测试绿（现有 1635 + 新增）；三条变异全红；真 HTTP 探针（临时数据根）走通
   「上传 PDF → 保存 → 取回原文」全链。
4. README 的简历一节补一句：支持 `.md`/`.txt`/**电子版 PDF**，扫描件请先 OCR。
