# -*- coding: utf-8 -*-
"""`pdftext.py` 的测试：夹具全是**测试里手写的合成 PDF**（不引任何库、不读真实 data/、不写盘）。

为什么手写：本项目零第三方依赖，连测试也不能引 `reportlab` / `pypdf`。合成夹具的好处是
**每条用例只制造一个被测条件**（坏 xref / 炸弹 / 无 ToUnicode / 加密…各一条），
坏处是盖不住 WPS 之类的真实怪癖 —— 那一条靠用户提供的真实样例（见设计文档 §8）。
"""
import unittest
import zlib

import pdftext


# ---------------------------------------------------------------- 合成 PDF 的工具
def _stream(payload, *, filters=b"", extra=b""):
    d = b"<< /Length %d" % len(payload)
    if filters:
        d += b" /Filter " + filters
    d += extra + b" >>"
    return d + b"\nstream\n" + payload + b"\nendstream"


def _pdf(objs, *, root=1, trailer_extra=b"", bad_xref=False):
    """按 PDF 语法拼一份**结构正常**的文件（xref 偏移是真的；`bad_xref=True` 则故意写错）。"""
    body = bytearray()
    head = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
    offsets = {}
    for num in sorted(objs):
        offsets[num] = len(head) + len(body)
        body += b"%d 0 obj\n" % num + objs[num] + b"\nendobj\n"
    size = max(objs) + 1
    xref_at = len(head) + len(body)
    body += b"xref\n0 %d\n" % size + b"0000000000 65535 f \n"
    for num in range(1, size):
        if num in offsets:
            body += b"%010d 00000 n \n" % (0 if bad_xref else offsets[num])
        else:
            body += b"0000000000 65535 f \n"
    body += (b"trailer\n<< /Size %d /Root %d 0 R" % (size, root)) + trailer_extra + b" >>\n"
    body += b"startxref\n%d\n%%%%EOF\n" % xref_at
    return head + bytes(body)


def _simple(content, *, filter_name=b"", extra_objs=None, pages=1, **kw):
    """单页（或把 content 当唯一一页）的最简文档。"""
    objs = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"),
        4: _stream(content, filters=filter_name),
        5: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    }
    if extra_objs:
        objs.update(extra_objs)
    return _pdf(objs, **kw)


def _content(body):
    return b"BT /F1 12 Tf 72 720 Td " + body + b" ET"


# ---------------------------------------------------------------- 1) 基本取词
class BasicTextTest(unittest.TestCase):
    def test_a_literal_string_is_extracted(self):
        got = pdftext.extract_text(_simple(_content(b"(Hello Resume) Tj")))
        self.assertEqual(got.text, "Hello Resume")
        self.assertEqual(got.pages, 1)
        self.assertEqual(got.chars, len("Hello Resume"))
        self.assertEqual(got.warnings, [])

    def test_flate_compressed_content_stream_is_inflated(self):
        body = _content(b"(Compressed Text) Tj")
        got = pdftext.extract_text(_simple(zlib.compress(body), filter_name=b"/FlateDecode"))
        self.assertEqual(got.text, "Compressed Text")

    def test_a_multi_line_resume_keeps_line_structure(self):
        """`Td` 换行 + 两行文本 ⇒ 输出里**真的有两行**（不是全糊成一行）。"""
        body = (b"BT /F1 12 Tf 72 720 Td (line one) Tj "
                b"0 -14 Td (line two) Tj 0 -14 Td (line three) Tj ET")
        got = pdftext.extract_text(_simple(body))
        self.assertEqual(got.text.splitlines(), ["line one", "line two", "line three"])

    def test_tj_kerning_adds_a_space_for_latin_but_not_between_cjk(self):
        """`TJ` 里的大负数 = 字距：西文补空格；两侧都是中文时**一个空格都不许补**。

        中文那份用 Type0+ToUnicode 字体（`CIDTextTest` 的夹具），这里只测西文那一半，
        中文那一半在 `CIDTextTest.test_no_space_is_inserted_between_cjk_glyphs`。
        """
        body = b"BT /F1 12 Tf 72 720 Td [(Hel) -300 (lo)] TJ ET"
        got = pdftext.extract_text(_simple(body))
        self.assertEqual(got.text, "Hel lo")

    def test_crlf_and_extra_blank_lines_are_normalised(self):
        body = b"BT /F1 12 Tf 72 720 Td (a) Tj\r\n 0 -14 Td (b) Tj\r\n\r\n 0 -14 Td (c) Tj ET"
        got = pdftext.extract_text(_simple(body))
        self.assertEqual(got.text.splitlines(), ["a", "b", "c"])

    def test_array_contents_are_concatenated_in_order(self):
        objs = {
            1: b"<< /Type /Catalog /Pages 2 0 R >>",
            2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                b"/Resources << /Font << /F1 5 0 R >> >> /Contents [4 0 R 6 0 R] >>"),
            4: _stream(b"BT /F1 12 Tf 72 720 Td (first) Tj ET"),
            5: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            6: _stream(b"BT /F1 12 Tf 72 700 Td (second) Tj ET"),
        }
        got = pdftext.extract_text(_pdf(objs))
        self.assertEqual(got.text.splitlines(), ["first", "second"])

    def test_ascii_hex_filter_is_supported(self):
        body = _content(b"(Hex) Tj").hex().encode("ascii") + b">"
        got = pdftext.extract_text(_simple(body, filter_name=b"/ASCIIHexDecode"))
        self.assertEqual(got.text, "Hex")


# ---------------------------------------------------------------- 2) 中文 / CID 字体
def _cid_objs(cmap_text, *, with_tounicode=True, content=b"<00010002> Tj"):
    """Type0/Identity-H + ToUnicode 的中文夹具（字号取 12）。"""
    objs = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"),
        4: _stream(b"BT /F1 12 Tf 72 720 Td " + content + b" ET"),
        5: b"<< /Type /Font /Subtype /Type0 /BaseFont /SimSun /Encoding /Identity-H "
           b"/DescendantFonts [7 0 R]" + (b" /ToUnicode 6 0 R" if with_tounicode else b"") + b" >>",
        7: b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /SimSun "
           b"/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> >>",
    }
    if with_tounicode:
        objs[6] = _stream(cmap_text)
    return objs


def _cmap(inner, *, codespace=b"<0000> <FFFF>"):
    return (b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
            b"1 begincodespacerange " + codespace + b" endcodespacerange\n"
            + inner +
            b"\nendcmap end end\n")


class CIDTextTest(unittest.TestCase):
    def test_bfchar_maps_cids_to_chinese(self):
        cmap = _cmap(b"2 beginbfchar <0001> <4E2D> <0002> <6587> endbfchar")
        got = pdftext.extract_text(_pdf(_cid_objs(cmap)))
        self.assertEqual(got.text, "中文")
        self.assertEqual(got.warnings, [])

    def test_bfrange_maps_a_run_of_cids(self):
        cmap = _cmap(b"1 beginbfrange <0001> <0003> <4E2D> endbfrange")
        got = pdftext.extract_text(_pdf(_cid_objs(cmap, content=b"<000100020003> Tj")))
        self.assertEqual(got.text, "中丮丯", "bfrange 的目标码点要逐个 +1（不是三个都当中）")

    def test_bfrange_with_an_array_of_targets(self):
        cmap = _cmap(b"1 beginbfrange <0001> <0002> [<4E2D> <6587>] endbfrange")
        got = pdftext.extract_text(_pdf(_cid_objs(cmap)))
        self.assertEqual(got.text, "中文")

    def test_no_space_is_inserted_between_cjk_glyphs(self):
        """中文之间**绝不许补空格**（`TJ` 的字距在中文排版里是常态，补空格就成"中 文"）。"""
        cmap = _cmap(b"2 beginbfchar <0001> <4E2D> <0002> <6587> endbfchar")
        objs = _cid_objs(cmap, content=b"<0001> Tj <0002> Tj")
        got = pdftext.extract_text(_pdf(objs))
        self.assertEqual(got.text, "中文")
        objs2 = _cid_objs(cmap, content=b"[<0001> -300 <0002>] TJ")
        self.assertEqual(pdftext.extract_text(_pdf(objs2)).text, "中文")

    def test_type0_without_tounicode_is_a_warning_not_garbage(self):
        """没有 ToUnicode 的 CID 字体：**不许**吐 `\\uFFFD` 或一堆看不懂的字符 —— 记 warning 并跳过。"""
        got_text = _pdf(_cid_objs(b"", with_tounicode=False))
        with self.assertRaises(pdftext.PdfError) as ctx:
            pdftext.extract_text(got_text)
        self.assertIn("字体", str(ctx.exception))          # 消息里要说清原因
        self.assertNotIn("\ufffd", str(ctx.exception))

    def test_a_page_that_cannot_be_mapped_does_not_poison_other_pages(self):
        """第 1 页能映射、第 2 页没有 ToUnicode ⇒ 第 1 页照出、第 2 页进 warnings。"""
        cmap = _cmap(b"1 beginbfchar <0001> <4E2D> endbfchar")
        objs = {
            1: b"<< /Type /Catalog /Pages 2 0 R >>",
            2: b"<< /Type /Pages /Kids [3 0 R 8 0 R] /Count 2 >>",
            3: (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"),
            4: _stream(b"BT /F1 12 Tf 72 720 Td <0001> Tj ET"),
            5: b"<< /Type /Font /Subtype /Type0 /BaseFont /SimSun /Encoding /Identity-H "
                b"/DescendantFonts [7 0 R] /ToUnicode 6 0 R >>",
            6: _stream(cmap),
            7: b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /SimSun >>",
            8: (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                b"/Resources << /Font << /F2 9 0 R >> >> /Contents 10 0 R >>"),
            9: b"<< /Type /Font /Subtype /Type0 /BaseFont /SimHei /Encoding /Identity-H "
                b"/DescendantFonts [11 0 R] >>",
            10: _stream(b"BT /F2 12 Tf 72 720 Td <0001> Tj ET"),
            11: b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /SimHei >>",
        }
        got = pdftext.extract_text(_pdf(objs))
        self.assertEqual(got.text, "中")
        self.assertEqual(got.pages, 2)
        self.assertTrue(any("第 2 页" in w for w in got.warnings), got.warnings)


# ---------------------------------------------------------------- 3) 坏输入 / 上限
class BadInputTest(unittest.TestCase):
    def test_non_pdf_bytes_are_rejected(self):
        """必须**直接**说"这不是 PDF" —— 而不是绕到「找不到文档目录」去。

        ⚠️ 这里钉的是**入口校验自己**：去掉 `%PDF-` 校验后，解析会一路走到"找不到 /Catalog"，
        那句里也含 "PDF" 二字 ⇒ 只断言 `in "PDF"` 会**假绿**（变异取证当场抓到的）。
        """
        with self.assertRaises(pdftext.PdfError) as ctx:
            pdftext.extract_text(b"PK\x03\x04 not a pdf at all")
        msg = str(ctx.exception)
        self.assertIn("不是 PDF 文件", msg)
        self.assertIn("扩展名", msg, "要提醒用户：多半是改了后缀")

    def test_encrypted_pdf_is_rejected_with_an_explanation(self):
        blob = _simple(_content(b"(x) Tj"), trailer_extra=b" /Encrypt 9 0 R")
        with self.assertRaises(pdftext.PdfError) as ctx:
            pdftext.extract_text(blob)
        msg = str(ctx.exception)
        self.assertIn("加密", msg)
        self.assertIn("另存", msg)

    def test_a_pdf_with_no_text_is_rejected_instead_of_returning_empty(self):
        with self.assertRaises(pdftext.PdfError) as ctx:
            pdftext.extract_text(_simple(_content(b"() Tj")))
        self.assertIn("抽不出文字", str(ctx.exception))

    def test_broken_xref_offsets_are_survived_by_scanning_objects(self):
        """xref 偏移全是 0（WPS / 手工修过的 PDF 常见）⇒ 扫 `obj` 兜底，仍要读得出来。"""
        blob = _simple(_content(b"(survived) Tj"), bad_xref=True)
        got = pdftext.extract_text(blob)
        self.assertEqual(got.text, "survived")

    def test_a_decompression_bomb_is_refused_quickly(self):
        """20 MB 的零压成几 KB：必须**在上限处停住**，不许真解出 20 MB（更不许 OOM）。"""
        payload = zlib.compress(b"\x00" * (20 * 1024 * 1024), 9)
        self.assertLess(len(payload), 200 * 1024, "夹具没压小，测的就不是炸弹了")
        blob = _simple(payload, filter_name=b"/FlateDecode")
        with self.assertRaises(pdftext.PdfError) as ctx:
            pdftext.extract_text(blob)
        self.assertIn("压缩", str(ctx.exception))
        self.assertLess(len(payload), 200 * 1024)

    def test_too_many_pages_is_refused_not_truncated(self):
        kids = b" ".join(b"%d 0 R" % (10 + i) for i in range(3))
        objs = {
            1: b"<< /Type /Catalog /Pages 2 0 R >>",
            2: b"<< /Type /Pages /Kids [" + kids + b"] /Count 3 >>",
        }
        for i in range(3):
            objs[10 + i] = (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                            b"/Resources << >> /Contents %d 0 R >>" % (20 + i))
            objs[20 + i] = _stream(b"BT /F1 12 Tf 72 720 Td (p%d) Tj ET" % i)
        with self.assertRaises(pdftext.PdfError) as ctx:
            pdftext.extract_text(_pdf(objs), max_pages=2)
        self.assertIn("页", str(ctx.exception))

    def test_an_unknown_filter_skips_that_stream_with_a_warning(self):
        """不认识的过滤器（这里用 LZW）：**跳过 + warning**，绝不猜、也绝不吐乱码。"""
        blob = _simple(b"whatever", filter_name=b"/LZWDecode")
        with self.assertRaises(pdftext.PdfError) as ctx:
            pdftext.extract_text(blob)
        self.assertIn("LZWDecode", str(ctx.exception))

    def test_a_catalog_without_pages_is_refused(self):
        objs = {1: b"<< /Type /Catalog >>"}
        with self.assertRaises(pdftext.PdfError) as ctx:
            pdftext.extract_text(_pdf(objs))
        self.assertIn("页", str(ctx.exception))


# ---------------------------------------------------------------- 4) 真实 PDF 里常见的两种"压缩/继承"形状
def _objstm(pairs):
    """把若干对象压进一条 `/ObjStm` 流（PDF 1.5+ 的写法；Catalog/Pages/Page 都可能在里面）。"""
    parts, offs, pos = [], [], 0
    for num, body in pairs:
        offs.append((num, pos))
        parts.append(body)
        pos += len(body) + 1
    payload = b"\n".join(parts)
    head = b" ".join(b"%d %d" % (n, o) for n, o in offs)
    data = head + b"\n" + payload
    return _stream(data, extra=b" /Type /ObjStm /N %d /First %d" % (len(pairs), len(head) + 1))


class CommonShapesTest(unittest.TestCase):
    def test_objects_inside_an_object_stream_are_found(self):
        """PDF 1.5+ 的对象流：不展开它，一份**完全正常**的简历会被报成"找不到文档目录"（假红）。"""
        objs = {
            9: _objstm([
                (1, b"<< /Type /Catalog /Pages 2 0 R >>"),
                (2, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>"),
                (3, (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                     b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>")),
            ]),
            4: _stream(_content(b"(inside object stream) Tj")),
            5: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        }
        got = pdftext.extract_text(_pdf(objs, root=1))
        self.assertEqual(got.text, "inside object stream")

    def test_resources_inherited_from_the_pages_node_are_used(self):
        """`/Resources` 是可继承的：挂在 `/Pages` 上的字体表也要认（否则读成"没有字体"）。"""
        objs = {
            1: b"<< /Type /Catalog /Pages 2 0 R >>",
            2: (b"<< /Type /Pages /Kids [3 0 R] /Count 1 "
                b"/Resources << /Font << /F1 5 0 R >> >> >>"),
            # ⚠️ 这一页**故意没有** /Resources
            3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R >>",
            4: _stream(_content(b"(inherited) Tj")),
            5: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        }
        got = pdftext.extract_text(_pdf(objs))
        self.assertEqual(got.text, "inherited")


# ---------------------------------------------------------------- 5) 接口形状
class ApiShapeTest(unittest.TestCase):
    def test_result_is_a_plain_object_with_the_documented_fields(self):
        got = pdftext.extract_text(_simple(_content(b"(abc) Tj")))
        self.assertEqual(sorted(got._fields), ["chars", "pages", "text", "warnings"])

    def test_extract_text_is_pure(self):
        """同一份字节跑两次结果完全相同（不许有模块级可变状态）。"""
        blob = _simple(_content(b"(stable) Tj"))
        self.assertEqual(pdftext.extract_text(blob).text, pdftext.extract_text(blob).text)
