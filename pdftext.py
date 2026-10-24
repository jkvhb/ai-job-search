# -*- coding: utf-8 -*-
"""从**电子版 PDF**（文字可选中复制）里抽文字 —— 纯标准库，零第三方依赖。

为什么自己写（而不引 pypdf / pdfminer）：这个项目从第一天起就是「零第三方依赖、双击就能跑」。
一个 pip 依赖会让分享包在朋友的机器上**直接起不来**（P0-10 记的就是同类事故）。
而「Word / WPS / 浏览器打印」导出的电子版 PDF 只需要四件事就能读：
对象表、FlateDecode、文本操作符、ToUnicode CMap —— 标准库都有（`zlib` + 自己写的小词法器）。

**不做什么（写清免得下一个人当漏项）**：
  * 不做版面还原：抽的是**文字流**（换行 + 西文空格），分栏 / 表格可能串行；
  * 不做 OCR：扫描件（页面就是一张图）抽不出文字 ⇒ `PdfError`，让用户另存为 .md/.txt 或先 OCR；
  * 不解密：带 `/Encrypt` ⇒ `PdfError`（让用户另存为无密码版本）；
  * 不支持 LZW / DCT / 预测器等：**跳过那条流并记 warning**，绝不猜、绝不吐乱码；
  * 绝不返回 U+FFFD：字体映射不出来就跳过并记 warning；整份都映射不出来 ⇒ `PdfError`。

**安全**（PDF 是不可信输入）：解压有上限（单流 8 MB / 总 64 MB，用 `decompressobj(max_length=…)`
在超限处**停住**，不是先解完再量），对象数 / 页数 / 页树深度 / 输出字符数都有上限。

公开接口只有 `extract_text(bytes) -> PdfText`（`PdfText` 是 namedtuple：text/pages/chars/warnings），
失败一律抛 `PdfError`（消息是给人看的一句话，路由把它翻成 400）。
"""
import collections
import re
import zlib

# ---------------------------------------------------------------- 上限（防炸弹 / 防卡死）
MAX_STREAM_BYTES = 8 * 1024 * 1024          # 单条流解压后
MAX_TOTAL_INFLATED = 64 * 1024 * 1024       # 整份 PDF 解压后总量
MAX_OBJECTS = 20000                         # 对象数
MAX_DEPTH = 32                              # 页树 / 引用链深度
MAX_OUTPUT_CHARS = 2000000                  # 输出字符数
DEFAULT_MAX_PAGES = 50

#: `extract_text` 的返回值（namedtuple：`_fields` 是公开契约，测试钉着它）
PdfText = collections.namedtuple("PdfText", "text pages chars warnings")


class PdfError(Exception):
    """这份 PDF 读不出来（**人话**消息，调用方直接显示给用户）。"""


_WS = b"\x00\t\n\x0c\r "
_DELIM = b"()<>[]{}/%"
_NUM_RE = re.compile(rb"[+-]?(?:\d+\.\d*|\.\d+|\d+)")
_OBJ_RE = re.compile(rb"(?<![A-Za-z0-9])(\d{1,10})\s+(\d{1,5})\s+obj\b")
_REF_RE = re.compile(rb"/Root\s+(\d{1,10})\s+(\d{1,5})\s+R")


def _size(n):
    return "%.1f MB" % (n / (1024.0 * 1024.0))


class _Bad(Exception):
    """对象语法坏了（只在**局部**兜：坏一个对象不该让整份文件读不出来）。"""


_Ref = collections.namedtuple("_Ref", "num gen")


class _Kw:
    """裸关键字 / 操作符（`obj` / `true` / `Tj` …）。`true/false/null` 在词法层已转成 Python 值。"""

    __slots__ = ("name",)

    def __init__(self, name):
        self.name = name


class _Stream:
    __slots__ = ("dict", "raw")

    def __init__(self, d, raw):
        self.dict = d
        self.raw = raw


class _Lexer:
    """PDF 对象语法的词法器 + 递归下降解析器（只覆盖 `extract_text` 用得上的那部分）。"""

    def __init__(self, data, pos=0):
        self.data = data
        self.pos = pos
        self.n = len(data)

    # ---- 基础 ----
    def skip(self):
        d, n = self.data, self.n
        while self.pos < n:
            c = d[self.pos]
            if c in _WS:
                self.pos += 1
            elif c == 0x25:                       # `%` 注释到行尾
                while self.pos < n and d[self.pos] not in b"\r\n":
                    self.pos += 1
            else:
                return

    def at_end(self):
        self.skip()
        return self.pos >= self.n

    def parse(self):
        self.skip()
        if self.pos >= self.n:
            raise _Bad()
        c = self.data[self.pos]
        if c == 0x2F:                             # `/`
            return self._name()
        if c == 0x28:                             # `(`
            return self._literal()
        if c == 0x3C:                             # `<`
            if self.data[self.pos:self.pos + 2] == b"<<":
                return self._dict()
            return self._hex()
        if c == 0x5B:                             # `[`
            return self._array()
        if c in b"+-.0123456789":
            return self._number_or_ref()
        kw = self._keyword()
        if kw == "true":
            return True
        if kw == "false":
            return False
        if kw == "null":
            return None
        return _Kw(kw)

    # ---- 各类对象 ----
    def _name(self):
        self.pos += 1
        out = bytearray()
        d = self.data
        while self.pos < self.n:
            c = d[self.pos]
            if c in _WS or c in _DELIM:
                break
            if c == 0x23 and self.pos + 2 < self.n:        # `#xx`
                try:
                    out.append(int(d[self.pos + 1:self.pos + 3], 16))
                    self.pos += 3
                    continue
                except ValueError:
                    pass
            out.append(c)
            self.pos += 1
        return out.decode("latin-1")

    def _literal(self):
        self.pos += 1
        depth = 1
        out = bytearray()
        d = self.data
        escapes = {0x6E: 10, 0x72: 13, 0x74: 9, 0x62: 8, 0x66: 12}
        while self.pos < self.n:
            c = d[self.pos]
            if c == 0x5C:                              # `\`
                self.pos += 1
                if self.pos >= self.n:
                    break
                e = d[self.pos]
                if e in escapes:
                    out.append(escapes[e])
                    self.pos += 1
                elif e in b"()\\":
                    out.append(e)
                    self.pos += 1
                elif e in b"01234567":
                    val = cnt = 0
                    while self.pos < self.n and cnt < 3 and d[self.pos] in b"01234567":
                        val = val * 8 + (d[self.pos] - 48)
                        self.pos += 1
                        cnt += 1
                    out.append(val & 0xFF)
                elif e in b"\r\n":                     # 行连接
                    self.pos += 1
                    if e == 0x0D and self.pos < self.n and d[self.pos] == 0x0A:
                        self.pos += 1
                else:
                    out.append(e)
                    self.pos += 1
            elif c == 0x28:
                depth += 1
                out.append(c)
                self.pos += 1
            elif c == 0x29:
                depth -= 1
                self.pos += 1
                if depth == 0:
                    return bytes(out)
                out.append(0x29)
            else:
                out.append(c)
                self.pos += 1
        raise _Bad()

    def _hex(self):
        self.pos += 1
        digits = bytearray()
        d = self.data
        while self.pos < self.n:
            c = d[self.pos]
            self.pos += 1
            if c == 0x3E:
                break
            if c in _WS:
                continue
            digits.append(c)
        if len(digits) % 2:
            digits.append(0x30)                        # 奇数位补 0（规范允许）
        try:
            return bytes.fromhex(digits.decode("ascii"))
        except (ValueError, UnicodeDecodeError):
            raise _Bad()

    def _array(self):
        self.pos += 1
        out = []
        while True:
            self.skip()
            if self.data[self.pos:self.pos + 1] == b"]":
                self.pos += 1
                return out
            if self.pos >= self.n:
                raise _Bad()
            out.append(self.parse())

    def _dict(self):
        self.pos += 2
        out = {}
        while True:
            self.skip()
            if self.data[self.pos:self.pos + 2] == b">>":
                self.pos += 2
                return out
            if self.pos >= self.n:
                raise _Bad()
            if self.data[self.pos:self.pos + 1] != b"/":
                self.parse()                           # 坏文件：键不是名字，跳过这个 token
                continue
            key = self._name()
            out[key] = self.parse()

    def _keyword(self):
        start = self.pos
        d = self.data
        while self.pos < self.n:
            c = d[self.pos]
            if c in _WS or c in _DELIM:
                break
            self.pos += 1
        if self.pos == start:
            raise _Bad()
        return d[start:self.pos].decode("latin-1")

    def _number_or_ref(self):
        m = _NUM_RE.match(self.data, self.pos)
        if not m:
            raise _Bad()
        self.pos = m.end()
        text = m.group()
        if b"." in text:
            return float(text)
        val = int(text)
        save = self.pos
        self.skip()
        m2 = _NUM_RE.match(self.data, self.pos)
        if m2 and b"." not in m2.group():
            end = m2.end()
            j = end
            while j < self.n and self.data[j] in _WS:
                j += 1
            if self.data[j:j + 1] == b"R":
                self.pos = j + 1
                return _Ref(val, int(m2.group()))
        self.pos = save
        return val


# ---------------------------------------------------------------- 文档结构
class _Doc:
    def __init__(self, data):
        self.data = data
        self.objects = _scan_objects(data)
        self.warnings = []
        self.inflated = 0
        self._expand_object_streams()

    def _expand_object_streams(self):
        """PDF 1.5+ 会把一批小对象**压进一条 `/ObjStm` 流**里（Catalog / Pages / Page 都可能在里面）。

        不展开它们，一份完全正常的 PDF 会被报成「找不到文档目录」—— 那是**假红**（用户会把
        好好的简历当成坏文件）。只展开一层：对象流里再嵌对象流的文件极少见，遇到就按读不到处理。
        """
        for st in list(self.objects.values()):
            if not isinstance(st, _Stream):
                continue
            if st.dict.get("Type") != "ObjStm":
                continue
            try:
                raw = self.stream_bytes(st)
            except PdfError:
                continue
            if not raw:
                continue
            try:
                count = int(st.dict.get("N") or 0)
                first = int(st.dict.get("First") or 0)
            except (TypeError, ValueError):
                continue
            if count <= 0 or first < 0 or first > len(raw):
                continue
            lex = _Lexer(raw, 0)
            pairs = []
            for _ in range(count):
                try:
                    num = lex.parse()
                    off = lex.parse()
                except _Bad:
                    break
                if isinstance(num, int) and isinstance(off, int):
                    pairs.append((num, off))
            for num, off in pairs:
                if num in self.objects:
                    continue
                pos = first + off
                if pos < 0 or pos >= len(raw):
                    continue
                sub = _Lexer(raw, pos)
                try:
                    self.objects[num] = sub.parse()
                except _Bad:
                    continue

    def resolve(self, value):
        depth = 0
        while isinstance(value, _Ref):
            depth += 1
            if depth > MAX_DEPTH:
                return None
            value = self.objects.get(value.num)
        return value

    def catalog(self):
        m = _REF_RE.search(self.data[-8192:])
        if m:
            root = self.objects.get(int(m.group(1)))
            if isinstance(root, dict):
                return root
        for value in self.objects.values():
            d = value.dict if isinstance(value, _Stream) else value
            if isinstance(d, dict) and d.get("Type") == "Catalog":
                return d
        raise PdfError("这份 PDF 里找不到文档目录（/Catalog）—— 文件可能不完整或已损坏")

    def pages(self, max_pages):
        root = self.catalog()
        node = self.resolve(root.get("Pages"))
        if not isinstance(node, dict):
            raise PdfError("这份 PDF 里找不到页树（/Pages）—— 文件可能不完整或已损坏")
        out = []
        self._walk(node, 0, out, max_pages)
        if not out:
            raise PdfError("这份 PDF 里没有任何一页")
        return out

    def _walk(self, node, depth, out, max_pages):
        if depth > MAX_DEPTH:
            raise PdfError("这份 PDF 的页树嵌套太深（超过 %d 层）" % MAX_DEPTH)
        d = self.resolve(node)
        if not isinstance(d, dict):
            return
        kids = d.get("Kids")
        if isinstance(kids, list):
            for kid in kids:
                self._walk(kid, depth + 1, out, max_pages)
            return
        out.append(d)
        if len(out) > max_pages:
            raise PdfError("这份 PDF 超过 %d 页（已读到第 %d 页就停）—— 简历不该这么长；"
                           "请只上传简历那几页" % (max_pages, len(out)))

    def stream_bytes(self, stream):
        """解出一条流的字节；**不支持的过滤器返回 None 并记 warning**。"""
        name = None
        filters = self.resolve(stream.dict.get("Filter"))
        names = [filters] if isinstance(filters, str) else []
        if isinstance(filters, list):
            for f in filters:
                f = self.resolve(f)
                if isinstance(f, str):
                    names.append(f)
        parms = self.resolve(stream.dict.get("DecodeParms"))
        if isinstance(parms, dict) and parms.get("Predictor") not in (None, 1):
            self.warnings.append("跳过了一条需要预测器（Predictor）的流")
            return None
        data = stream.raw
        for name in names:
            if name in ("FlateDecode", "Fl"):
                data = self._inflate(data)
            elif name in ("ASCIIHexDecode", "AHx"):
                data = _ascii_hex(data)
            else:
                self.warnings.append("跳过了不支持的过滤器 %s" % name)
                return None
        return data

    def _inflate(self, raw):
        obj = zlib.decompressobj()
        try:
            out = obj.decompress(raw, MAX_STREAM_BYTES + 1)
        except zlib.error as e:
            raise PdfError("PDF 里的压缩流解不开（文件可能损坏或被截断）：%s" % e)
        if len(out) > MAX_STREAM_BYTES or obj.unconsumed_tail:
            raise PdfError("PDF 里有一条压缩流解压后超过 %s —— 拒绝按「压缩炸弹」处理"
                           % _size(MAX_STREAM_BYTES))
        self.inflated += len(out)
        if self.inflated > MAX_TOTAL_INFLATED:
            raise PdfError("这份 PDF 解压后总量超过 %s —— 拒绝按「压缩炸弹」处理"
                           % _size(MAX_TOTAL_INFLATED))
        return out


def _scan_objects(data):
    """对象表：**顺序扫 `N G obj`**（不依赖 xref，所以 xref 坏掉的 PDF 也能读）。

    扫的时候每解析完一个对象就跳到它之后 —— 这样不会把二进制流里偶然出现的 `obj` 当成对象。
    """
    objects = {}
    pos = 0
    while True:
        m = _OBJ_RE.search(data, pos)
        if not m:
            return objects
        lex = _Lexer(data, m.end())
        try:
            value = lex.parse()
            if isinstance(value, dict):
                lex.skip()
                if data[lex.pos:lex.pos + 6] == b"stream":
                    st = _read_stream(lex, value)
                    if st is not None:
                        value = st
            lex.skip()
            if data[lex.pos:lex.pos + 6] == b"endobj":
                lex.pos += 6
        except _Bad:
            pos = m.end()                             # 坏对象：跳过它继续（坏一个不该拖垮整份）
            continue
        num = int(m.group(1))
        if num not in objects:
            objects[num] = value
        pos = max(lex.pos, m.end())
        if len(objects) > MAX_OBJECTS:
            raise PdfError("这份 PDF 的对象太多（超过 %d 个）—— 拒绝继续解析" % MAX_OBJECTS)


def _read_stream(lex, d):
    data = lex.data
    p = lex.pos + 6                                   # `stream` 之后
    if data[p:p + 2] == b"\r\n":
        p += 2
    elif data[p:p + 1] in (b"\r", b"\n"):
        p += 1
    length = d.get("Length")
    if isinstance(length, int) and 0 <= length <= lex.n - p:
        end = p + length
        if b"endstream" in data[end:end + 24]:        # 长度可信（后面紧跟 endstream）
            lex.pos = end + 9
            return _Stream(d, data[p:end])
    e = data.find(b"endstream", p)                    # 长度不可信（引用 / 写错）：往后找
    if e < 0:
        raise _Bad()
    raw = data[p:e]
    if raw.endswith(b"\r\n"):
        raw = raw[:-2]
    elif raw.endswith(b"\n") or raw.endswith(b"\r"):
        raw = raw[:-1]
    lex.pos = e + 9
    return _Stream(d, raw)


def _ascii_hex(raw):
    digits = bytearray()
    for c in raw:
        if c in _WS:
            continue
        if c == 0x3E:                                 # `>`
            break
        digits.append(c)
    if len(digits) % 2:
        digits.append(0x30)
    try:
        return bytes.fromhex(digits.decode("ascii"))
    except (ValueError, UnicodeDecodeError):
        raise PdfError("PDF 里有一条 ASCIIHex 编码的流解不开（文件可能损坏）")


# ---------------------------------------------------------------- 字体 / CMap
def _utf16_be(raw):
    if len(raw) % 2:
        raw = raw[:-1]
    try:
        return raw.decode("utf-16-be")
    except UnicodeDecodeError:
        return ""


def _is_cjk(ch):
    if not ch:
        return False
    o = ord(ch)
    return (0x2E80 <= o <= 0x9FFF or 0x3000 <= o <= 0x303F
            or 0xF900 <= o <= 0xFAFF or 0xFF00 <= o <= 0xFFEF)


def _simple_char(b, full):
    """简单字体的字节 → 字符。`full=False`（没有 `/Encoding` 也没有 ToUnicode）时**只认 ASCII** ——
    宁可少几个字，也不把"自定义编码的高位字节"猜成 latin-1 乱码。"""
    if 0x20 <= b <= 0x7E:
        return chr(b)
    if b < 0x20 or b == 0x7F:
        return None
    if 0xA0 <= b <= 0xFF and full:
        return chr(b)
    return None


def _parse_cmap(raw, default_len):
    """解析 `/ToUnicode` 的 CMap：返回 `(映射表: bytes→str, 源编码字节数)`。

    只认 `bfchar` / `bfrange` / `codespacerange`（Word/WPS 导出的都在这三样里）。
    解析失败**不抛**：CMap 坏了只是"这页映射不出来"，不是"文件读不出来"（由调用方记 warning）。
    """
    lex = _Lexer(raw, 0)
    toks = []
    while not lex.at_end():
        try:
            toks.append(lex.parse())
        except _Bad:
            break
    mapping = {}
    code_len = default_len
    mode = None
    bucket = []

    def flush():
        if mode == "bfchar":
            for i in range(0, len(bucket) - 1, 2):
                src, dst = bucket[i], bucket[i + 1]
                if isinstance(src, bytes) and isinstance(dst, bytes):
                    text = _utf16_be(dst)
                    if text:
                        mapping[src] = text
        elif mode == "bfrange":
            i = 0
            while i + 2 < len(bucket):
                lo, hi, dst = bucket[i], bucket[i + 1], bucket[i + 2]
                i += 3
                if not (isinstance(lo, bytes) and isinstance(hi, bytes)):
                    continue
                if isinstance(dst, list):
                    for k, d in enumerate(dst):
                        if isinstance(d, bytes):
                            text = _utf16_be(d)
                            if text:
                                mapping[_incr(lo, k)] = text
                    continue
                if not isinstance(dst, bytes):
                    continue
                base = _utf16_be(dst)
                if not base:
                    continue
                count = _range_count(lo, hi)
                if count <= 0 or count > 70000:
                    continue
                for k in range(count):
                    mapping[_incr(lo, k)] = _incr_text(base, k)
        elif mode == "codespace":
            for i in range(0, len(bucket) - 1, 2):
                lo = bucket[i]
                if isinstance(lo, bytes) and 1 <= len(lo) <= 4:
                    code_len = len(lo)

    for tok in toks:
        if isinstance(tok, _Kw):
            name = tok.name
            if name == "beginbfchar" or name == "beginbfrange" or name == "begincodespacerange":
                flush()
                bucket = []
                mode = {"beginbfchar": "bfchar", "beginbfrange": "bfrange",
                        "begincodespacerange": "codespace"}[name]
            elif name in ("endbfchar", "endbfrange", "endcodespacerange"):
                flush()
                bucket = []
                mode = None
            # 其余关键字（begincmap / CMapName / def …）一律忽略
            continue
        if mode is not None:
            bucket.append(tok)
    flush()
    return mapping, code_len


def _range_count(lo, hi):
    a = int.from_bytes(lo, "big")
    b = int.from_bytes(hi, "big")
    return b - a + 1


def _incr(code, k):
    """把编码值 +k（只动最后两个字节 —— CID 编码几乎都是 2 字节）。"""
    if len(code) < 2:
        return code
    val = int.from_bytes(code[-2:], "big") + k
    return code[:-2] + (val & 0xFFFF).to_bytes(2, "big")


def _incr_text(base, k):
    if not base:
        return base
    raw = bytearray(base.encode("utf-16-be"))
    val = int.from_bytes(raw[-2:], "big") + k
    raw[-2:] = (val & 0xFFFF).to_bytes(2, "big")
    return bytes(raw).decode("utf-16-be", "ignore")


class _Font:
    """一份字体：Type0（CID，靠 ToUnicode）/ 简单字体（靠编码表）。"""

    __slots__ = ("type0", "code_len", "cmap", "full")

    def __init__(self, doc, d):
        self.type0 = doc.resolve(d.get("Subtype")) == "Type0"
        enc = doc.resolve(d.get("Encoding"))
        self.full = isinstance(enc, str)                 # WinAnsiEncoding 之类：高位字节有定义
        self.cmap = {}
        self.code_len = 2 if self.type0 else 1
        tu = doc.resolve(d.get("ToUnicode"))
        if isinstance(tu, _Stream):
            try:
                raw = doc.stream_bytes(tu)
            except PdfError:
                raw = None
            if raw:
                self.cmap, self.code_len = _parse_cmap(raw, self.code_len)

    def decode(self, raw):
        """→ `(文本, 没能映射的编码个数)`。**绝不返回 U+FFFD**：映射不出来只计数。"""
        if not raw:
            return "", 0
        if self.type0:
            if not self.cmap:
                return "", max(1, len(raw) // self.code_len)
            out, missed, n = [], 0, self.code_len
            for i in range(0, len(raw) - n + 1, n):
                text = self.cmap.get(raw[i:i + n])
                if text is None:
                    missed += 1
                else:
                    out.append(text)
            return "".join(out), missed
        out, missed = [], 0
        for b in raw:
            if self.cmap:
                text = self.cmap.get(bytes([b]))
            else:
                text = _simple_char(b, self.full)
            if text is None:
                missed += 1
            else:
                out.append(text)
        return "".join(out), missed


# ---------------------------------------------------------------- 内容流：取词
def _approx_width(s, size):
    return sum(size * (1.0 if _is_cjk(ch) else 0.5) for ch in s)


def _skip_inline_image(data, pos):
    """跳过 `BI … ID <二进制> EI`（简历里罕见，但不跳会把二进制当操作符解析）。"""
    idx = data.find(b"ID", pos)
    if idx < 0:
        return len(data)
    j = idx + 2
    while True:
        k = data.find(b"EI", j)
        if k < 0:
            return len(data)
        before = data[k - 1:k] if k > 0 else b" "
        after = data[k + 2:k + 3]
        if before in _WS and (after == b"" or after in _WS):
            return k + 2
        j = k + 2


def _page_text(doc, page, page_no, fonts, warnings):
    contents = doc.resolve(page.get("Contents"))
    parts = []
    if isinstance(contents, _Stream):
        parts.append(doc.stream_bytes(contents))
    elif isinstance(contents, list):
        for item in contents:
            st = doc.resolve(item)
            if isinstance(st, _Stream):
                parts.append(doc.stream_bytes(st))
    raw = b"\n".join(p for p in parts if p)
    if not raw:
        return "", 0

    lex = _Lexer(raw, 0)
    operands = []
    lines, cur = [], []
    font = None
    size = 12.0
    x = y = 0.0
    leading = 0.0
    cur_y = None
    last_x_end = None
    pending_space = False
    missed_total = 0

    def newline():
        if cur:
            lines.append("".join(cur)[:])
            del cur[:]

    def maybe_newline():
        nonlocal cur_y
        if cur_y is not None and abs(y - cur_y) > max(1.0, size * 0.35):
            newline()
        cur_y = y

    def emit(text):
        nonlocal x, last_x_end, pending_space
        if not text:
            return
        if pending_space and cur:
            prev = cur[-1][-1:] if cur[-1] else ""
            if not (_is_cjk(prev) and _is_cjk(text[0])):
                cur.append(" ")
        elif cur and last_x_end is not None and (x - last_x_end) > size * 0.30:
            prev = cur[-1][-1:] if cur[-1] else ""
            if not (_is_cjk(prev) and _is_cjk(text[0])):
                cur.append(" ")
        pending_space = False
        cur.append(text)
        x += _approx_width(text, size)
        last_x_end = x

    def show(value):
        nonlocal missed_total
        if not isinstance(value, bytes):
            return
        if font is None:
            missed_total += len(value)
            return
        text, missed = font.decode(value)
        missed_total += missed
        emit(text)

    while True:
        lex.skip()
        if lex.pos >= lex.n:
            break
        try:
            tok = lex.parse()
        except _Bad:
            break
        if not isinstance(tok, _Kw):
            operands.append(tok)
            continue
        op = tok.name
        if op == "BI":
            lex.pos = _skip_inline_image(raw, lex.pos)
            operands = []
            continue
        if op == "BT":
            x = y = 0.0
            operands = []
            continue
        if op == "ET":
            operands = []
            continue
        if op == "Tf":
            name = operands[-2] if len(operands) >= 2 else None
            if isinstance(name, str) and isinstance(operands[-1], (int, float)):
                size = float(operands[-1])
                font = fonts.get(name)
            operands = []
            continue
        if op in ("Td", "TD"):
            if len(operands) >= 2 and all(isinstance(v, (int, float)) for v in operands[-2:]):
                tx, ty = float(operands[-2]), float(operands[-1])
                x += tx
                y += ty
                if op == "TD":
                    leading = -ty
                last_x_end = x
                maybe_newline()
            operands = []
            continue
        if op == "Tm":
            if len(operands) >= 6 and all(isinstance(v, (int, float)) for v in operands[-6:]):
                x = float(operands[-2])
                y = float(operands[-1])
                last_x_end = x
                maybe_newline()
            operands = []
            continue
        if op == "TL":
            if operands and isinstance(operands[-1], (int, float)):
                leading = float(operands[-1])
            operands = []
            continue
        if op == "T*":
            y -= leading
            newline()
            last_x_end = x
            cur_y = y
            operands = []
            continue
        if op == "Tj":
            if operands:
                show(operands[-1])
            operands = []
            continue
        if op == "TJ":
            if operands and isinstance(operands[-1], list):
                for item in operands[-1]:
                    if isinstance(item, bytes):
                        show(item)
                    elif isinstance(item, (int, float)) and item <= -150:
                        pending_space = True
            operands = []
            continue
        if op == "'":
            y -= leading if leading else size * 1.2
            newline()
            cur_y = y
            last_x_end = x
            if operands:
                show(operands[-1])
            operands = []
            continue
        if op == '"':
            if len(operands) >= 3:
                show(operands[-1])
            operands = []
            continue
        operands = []
    newline()
    text = "\n".join(lines)
    return text, missed_total


def _page_resources(doc, page):
    """取这一页的资源表 —— 自己没写就往 `/Parent` 上找（`/Resources` 是**可继承**的，
    LibreOffice / 某些导出器把字体表挂在 `/Pages` 上；只看本页会读成"没有字体"）。"""
    node = page
    for _ in range(MAX_DEPTH):
        if not isinstance(node, dict):
            return None
        res = doc.resolve(node.get("Resources"))
        if isinstance(res, dict):
            return res
        node = doc.resolve(node.get("Parent"))
    return None


def _normalise(text):
    out = []
    blanks = 0
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = "".join(ch for ch in line if ch == "\t" or ch >= " ")
        line = line.rstrip()
        if not line:
            blanks += 1
            if blanks > 1:
                continue
        else:
            blanks = 0
        out.append(line)
    return "\n".join(out).strip()


# ---------------------------------------------------------------- 公开接口
def extract_text(data, *, max_pages=DEFAULT_MAX_PAGES):
    """从电子版 PDF 里抽文字。**坏输入一律 `PdfError`（人话）**，绝不返回半真半假的结果。"""
    if isinstance(data, bytearray):
        data = bytes(data)
    if not isinstance(data, bytes):
        raise PdfError("这不是 PDF 数据（拿到的是 %s）" % type(data).__name__)
    if len(data) < 16:
        raise PdfError("这不是 PDF 文件（只有 %d 字节，太短了）" % len(data))
    if b"%PDF-" not in data[:1024]:
        raise PdfError("这不是 PDF 文件（前 1 KB 里没有 `%PDF-` 头）—— 是不是改了扩展名？")
    if re.search(rb"/Encrypt\b", data[-8192:]):
        raise PdfError("这份 PDF 加密了，本工具不解密 —— 请先用阅读器打开、另存为一份无密码的 PDF")

    doc = _Doc(data)
    pages = doc.pages(max_pages)
    chunks = []
    warnings = doc.warnings
    for i, page in enumerate(pages, 1):
        fonts = {}
        res = _page_resources(doc, page)
        if isinstance(res, dict):
            table = doc.resolve(res.get("Font"))
            if isinstance(table, dict):
                for name, ref in table.items():
                    d = doc.resolve(ref)
                    if isinstance(d, dict):
                        fonts[name] = _Font(doc, d)
        try:
            text, missed = _page_text(doc, page, i, fonts, warnings)
        except PdfError:
            raise
        if text.strip():
            chunks.append(text)
        if missed:
            warnings.append("第 %d 页有字体无法映射（跳过了 %d 个字符）" % (i, missed))

    text = _normalise("\n".join(chunks))
    if not text:
        detail = "；".join(warnings) if warnings else "这份 PDF 里没有任何可抽取的文字"
        raise PdfError("这份 PDF 里抽不出文字（%s）—— 多半是图片版 / 扫描件：请先用 Word/WPS "
                       "打开另存为 .md 或 .txt，或先做一次 OCR 再上传" % detail)
    if len(text) > MAX_OUTPUT_CHARS:
        raise PdfError("这份 PDF 抽出来的文字超过 %d 字符 —— 不像是简历，拒绝继续" % MAX_OUTPUT_CHARS)
    return PdfText(text=text, pages=len(pages), chars=len(text), warnings=list(warnings))
