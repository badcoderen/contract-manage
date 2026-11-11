# -*- coding: utf-8 -*-
"""Office 文件读写 —— 纯标准库实现。

为什么不用 openpyxl / python-docx：
  · 目标环境是飞牛 NAS 上的容器，少一个依赖少一分部署风险；
  · .xlsx / .docx 本质就是「zip + 若干 XML」，读写都能自己做。

对外能力：
  read_rows(data, filename)  → [[cell, ...], ...]  读 .xlsx / .csv
  Grid / grid_xlsx(grid)     → 带样式、合并单元格、列宽的 .xlsx 字节流
  docx_bytes(blocks)         → 送货单这类表格文档的 .docx 字节流
"""
import csv
import io
import re
import zipfile

# ============================================================ 公共小工具
_XML_BAD = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&apos;"}


def esc(v):
    return "".join(_XML_BAD.get(ch, ch) for ch in str(v if v is not None else ""))


def col_letter(n):
    """1 → A, 27 → AA"""
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def col_index(ref):
    """'C12' → 3"""
    m = re.match(r"([A-Z]+)", (ref or "").upper())
    if not m:
        return 0
    n = 0
    for ch in m.group(1):
        n = n * 26 + (ord(ch) - 64)
    return n


def _cell_text(v):
    return str(v if v is not None else "").strip()


def _num(v):
    """把 '1,234.50 元' / '12%' / '¥88' 这类文本转成数字；转不了返回 None。"""
    t = _cell_text(v).replace(",", "").replace("￥", "").replace("¥", "")
    t = t.replace("元", "").strip()
    if not t:
        return None
    pct = t.endswith("%")
    if pct:
        t = t[:-1]
    try:
        x = float(t)
    except ValueError:
        return None
    return x / 100.0 if pct else x


def to_float(v, default=0.0):
    x = _num(v)
    return default if x is None else x


# ============================================================ 读 xlsx / csv
_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def read_rows(data, filename=""):
    """读表格文件，返回二维字符串列表（第一行通常是表头）。

    支持 .xlsx（zip+XML，含 inlineStr / sharedStrings 两种写法）与 .csv（UTF-8 / GBK）。
    """
    name = (filename or "").lower()
    if name.endswith(".csv") or (not name.endswith(".xlsx") and data[:2] != b"PK"):
        return _read_csv(data)
    return _read_xlsx(data)


def _read_csv(data):
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = data.decode("utf-8", "replace")
    rows = []
    for row in csv.reader(io.StringIO(text)):
        rows.append([_cell_text(c) for c in row])
    return rows


def _read_xlsx(data):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        shared = []
        if "xl/sharedStrings.xml" in names:
            root = _root(z.read("xl/sharedStrings.xml"))
            for si in root.findall(_NS + "si"):
                shared.append("".join(t.text or "" for t in si.iter(_NS + "t")))
        sheets = sorted(n for n in names
                        if re.match(r"xl/worksheets/sheet\d+\.xml$", n))
        if not sheets:
            raise ValueError("这个 Excel 里没有工作表")
        root = _root(z.read(sheets[0]))
    rows = []
    for row in root.iter(_NS + "row"):
        cells = {}
        for c in row.findall(_NS + "c"):
            ci = col_index(c.get("r") or "")
            if ci <= 0:
                continue
            t = c.get("t")
            if t == "inlineStr":
                node = c.find(_NS + "is")
                val = ("".join(x.text or "" for x in node.iter(_NS + "t"))
                       if node is not None else "")
            else:
                v = c.find(_NS + "v")
                raw = (v.text if v is not None else "") or ""
                if t == "s" and raw.isdigit() and int(raw) < len(shared):
                    val = shared[int(raw)]
                else:
                    val = raw
            cells[ci] = _cell_text(val)
        if cells:
            rows.append([cells.get(i, "") for i in range(1, max(cells) + 1)])
    return rows


def _root(blob):
    import xml.etree.ElementTree as ET
    return ET.fromstring(blob)


# ============================================================ 写 xlsx（带样式）
# 样式索引（与下方 STYLES_XML 里的 cellXfs 顺序一一对应）
S_NORM, S_TITLE, S_SUB, S_HEAD, S_TEXT, S_CENTER, S_MONEY, S_NUM, S_TOT_TEXT, \
    S_TOT_MONEY, S_NOTE, S_LABEL, S_INT, S_HEAD_L, S_TOT_NUM = range(15)


class Grid:
    """一张工作表：行 / 列宽 / 合并单元格 / 行高。"""

    def __init__(self, sheet="Sheet1", widths=None):
        self.sheet = sheet
        self.widths = widths or []
        self.rows = []
        self.merges = []
        self.heights = {}

    def add(self, cells, style=S_NORM, height=None):
        row = []
        for c in cells:
            row.append(dict(c) if isinstance(c, dict) else {"v": c, "s": style})
        self.rows.append(row)
        if height:
            self.heights[len(self.rows)] = height
        return self

    def blank(self, height=None):
        return self.add([""], height=height)

    def merge(self, r1, c1, r2, c2):
        self.merges.append((r1, c1, r2, c2))
        return self


STYLES_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<numFmts count="3">
<numFmt numFmtId="176" formatCode="#,##0.00"/>
<numFmt numFmtId="177" formatCode="#,##0"/>
<numFmt numFmtId="178" formatCode="0.00"/>
</numFmts>
<fonts count="5">
<font><sz val="11"/><name val="宋体"/><charset val="134"/></font>
<font><b/><sz val="11"/><name val="宋体"/><charset val="134"/></font>
<font><b/><sz val="18"/><name val="宋体"/><charset val="134"/></font>
<font><b/><sz val="14"/><name val="宋体"/><charset val="134"/></font>
<font><sz val="9"/><color rgb="FF808080"/><name val="宋体"/><charset val="134"/></font>
</fonts>
<fills count="4">
<fill><patternFill patternType="none"/></fill>
<fill><patternFill patternType="gray125"/></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFE8EEF6"/><bgColor indexed="64"/></patternFill></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFF2F2F2"/><bgColor indexed="64"/></patternFill></fill>
</fills>
<borders count="2">
<border><left/><right/><top/><bottom/><diagonal/></border>
<border><left style="thin"><color rgb="FF9E9E9E"/></left><right style="thin"><color rgb="FF9E9E9E"/></right><top style="thin"><color rgb="FF9E9E9E"/></top><bottom style="thin"><color rgb="FF9E9E9E"/></bottom><diagonal/></border>
</borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="15">
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="0" fontId="2" fillId="0" borderId="0" xfId="0" applyFont="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
<xf numFmtId="0" fontId="3" fillId="0" borderId="0" xfId="0" applyFont="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
<xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>
<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1"><alignment horizontal="left" vertical="center" wrapText="1"/></xf>
<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>
<xf numFmtId="176" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyBorder="1" applyAlignment="1"><alignment horizontal="right" vertical="center"/></xf>
<xf numFmtId="178" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
<xf numFmtId="0" fontId="1" fillId="3" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
<xf numFmtId="176" fontId="1" fillId="3" borderId="1" xfId="0" applyNumberFormat="1" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="right" vertical="center"/></xf>
<xf numFmtId="0" fontId="4" fillId="0" borderId="0" xfId="0" applyFont="1" applyAlignment="1"><alignment horizontal="left" vertical="top" wrapText="1"/></xf>
<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1" applyAlignment="1"><alignment horizontal="left" vertical="center"/></xf>
<xf numFmtId="177" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
<xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="left" vertical="center" wrapText="1"/></xf>
<xf numFmtId="177" fontId="1" fillId="3" borderId="1" xfId="0" applyNumberFormat="1" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
</cellXfs>
<cellStyles count="1"><cellStyle name="常规" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>"""

XLSX_CT = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
%(sheets)s
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>"""

XLSX_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""


def grid_xlsx(grids):
    """把一张或多张 Grid 打成 .xlsx 字节流（带样式、合并、列宽、行高）。"""
    if isinstance(grids, Grid):
        grids = [grids]
    buf = io.BytesIO()
    overrides = "".join(
        f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/'
        f'vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for i in range(1, len(grids) + 1))
    sheet_tags = "".join(
        f'<sheet name="{esc(g.sheet)}" sheetId="{i}" r:id="rId{i}"/>'
        for i, g in enumerate(grids, 1))
    wb = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
          'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
          f"<sheets>{sheet_tags}</sheets></workbook>")
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
            'relationships">'
            + "".join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.'
                      f'org/officeDocument/2006/relationships/worksheet" '
                      f'Target="worksheets/sheet{i}.xml"/>'
                      for i in range(1, len(grids) + 1))
            + f'<Relationship Id="rId{len(grids) + 1}" Type="http://schemas.'
              'openxmlformats.org/officeDocument/2006/relationships/styles" '
              'Target="styles.xml"/></Relationships>')
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", XLSX_CT % {"sheets": overrides})
        z.writestr("_rels/.rels", XLSX_RELS)
        z.writestr("xl/workbook.xml", wb)
        z.writestr("xl/_rels/workbook.xml.rels", rels)
        z.writestr("xl/styles.xml", STYLES_XML)
        for i, g in enumerate(grids, 1):
            z.writestr(f"xl/worksheets/sheet{i}.xml", _sheet_xml(g))
    return buf.getvalue()


def _sheet_xml(g):
    parts = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
             '<worksheet xmlns="http://schemas.openxmlformats.org/'
             'spreadsheetml/2006/main">']
    if g.widths:
        parts.append("<cols>")
        for i, w in enumerate(g.widths, 1):
            parts.append(f'<col min="{i}" max="{i}" width="{w}" customWidth="1"/>')
        parts.append("</cols>")
    parts.append("<sheetData>")
    for ri, row in enumerate(g.rows, 1):
        attrs = f' r="{ri}"'
        if g.heights.get(ri):
            attrs += f' ht="{g.heights[ri]}" customHeight="1"'
        cells = []
        for ci, cell in enumerate(row, 1):
            v = cell.get("v")
            s = cell.get("s", 0)
            if v is None or v == "":
                continue
            ref = f"{col_letter(ci)}{ri}"
            st = f' s="{s}"' if s else ""
            if isinstance(v, bool) or isinstance(v, (int, float)):
                cells.append(f'<c r="{ref}"{st}><v>{v}</v></c>')
            else:
                cells.append(f'<c r="{ref}"{st} t="inlineStr"><is><t xml:space='
                             f'"preserve">{esc(v)}</t></is></c>')
        parts.append(f"<row{attrs}>" + "".join(cells) + "</row>")
    parts.append("</sheetData>")
    if g.merges:
        parts.append(f'<mergeCells count="{len(g.merges)}">')
        for r1, c1, r2, c2 in g.merges:
            parts.append(f'<mergeCell ref="{col_letter(c1)}{r1}:'
                         f'{col_letter(c2)}{r2}"/>')
        parts.append("</mergeCells>")
    parts.append("</worksheet>")
    return "".join(parts)


# ============================================================ 写 docx（送货单）
DOCX_CT = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""

DOCX_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""


def _run(text, bold=False, size=None):
    rpr = ["<w:rPr>"]
    if bold:
        rpr.append("<w:b/>")
    if size:
        rpr.append(f'<w:sz w:val="{size}"/><w:szCs w:val="{size}"/>')
    rpr.append('<w:rFonts w:ascii="宋体" w:eastAsia="宋体" w:hAnsi="宋体"/>')
    rpr.append("</w:rPr>")
    return (f"<w:r>{''.join(rpr)}<w:t xml:space=\"preserve\">{esc(text)}</w:t></w:r>")


def _para(runs, align=None, after=0, before=0):
    ppr = ["<w:pPr>"]
    if align:
        ppr.append(f'<w:jc w:val="{align}"/>')
    ppr.append(f'<w:spacing w:before="{before}" w:after="{after}"/>')
    ppr.append("</w:pPr>")
    return f"<w:p>{''.join(ppr)}{''.join(runs)}</w:p>"


def _cell(blocks, width, span=1, valign="center", shade=None):
    tcpr = [f'<w:tcPr><w:tcW w:w="{width}" w:type="dxa"/>']
    if span > 1:
        tcpr.append(f'<w:gridSpan w:val="{span}"/>')
    if valign:
        tcpr.append(f'<w:vAlign w:val="{valign}"/>')
    if shade:
        tcpr.append(f'<w:shd w:val="clear" w:color="auto" w:fill="{shade}"/>')
    tcpr.append("</w:tcPr>")
    return f"<w:tc>{''.join(tcpr)}{''.join(blocks)}</w:tc>"


def _tbl(rows, widths, borders=True):
    """rows: [[cell_xml, ...], ...]；widths: 每列宽度（dxa，1/20 磅）"""
    bd = ('<w:tblBorders>'
          + "".join(f'<w:{e} w:val="single" w:sz="6" w:space="0" w:color="808080"/>'
                    for e in ("top", "left", "bottom", "right", "insideH", "insideV"))
          + "</w:tblBorders>") if borders else "<w:tblBorders>" + "".join(
        f'<w:{e} w:val="none" w:sz="0" w:space="0" w:color="auto"/>'
        for e in ("top", "left", "bottom", "right", "insideH", "insideV")) + "</w:tblBorders>"
    grid = "".join(f'<w:gridCol w:w="{w}"/>' for w in widths)
    return ('<w:tbl><w:tblPr><w:tblW w:w="5000" w:type="pct"/>'
            '<w:tblLayout w:type="fixed"/>' + bd + '</w:tblPr>'
            f"<w:tblGrid>{grid}</w:tblGrid>" + "".join(rows) + "</w:tbl>")


def docx_bytes(blocks):
    """按块描述生成 .docx。

    blocks 支持的块：
      ("title", 文本)                     标题（居中加粗 18pt）
      ("sub", 文本)                       副标题（居中 10pt）
      ("p", 文本, {"align":..,"bold":..,"size":..,"after":..})
      ("kv", [[标签, 值, 标签, 值], ...])   两栏/四栏抬头表
      ("table", 表头[], 数据行[[]], 列宽[])  明细表（自动重复表头）
      ("sign", [标签, ...])               签字栏
      ("space")
    """
    body = []
    for b in blocks:
        kind = b[0]
        if kind == "title":
            body.append(_para([_run(b[1], bold=True, size=36)], "center", after=80))
        elif kind == "sub":
            body.append(_para([_run(b[1], size=20)], "center", after=180))
        elif kind == "p":
            opt = b[2] if len(b) > 2 else {}
            body.append(_para([_run(b[1], bold=opt.get("bold"), size=opt.get("size"))],
                              opt.get("align"), after=opt.get("after", 60)))
        elif kind == "kv":
            widths = b[2] if len(b) > 2 else None
            rows = []
            for pair in b[1]:
                widths = widths or [1000, 2200] * (len(pair) // 2)
                tcs = []
                for i, val in enumerate(pair):
                    is_label = i % 2 == 0
                    tcs.append(_cell([_para([_run(val, bold=is_label, size=21)],
                                            "left" if not is_label else "center")],
                                     widths[i] if i < len(widths) else 1500,
                                     shade="F2F2F2" if is_label else None))
                rows.append(f"<w:tr>{''.join(tcs)}</w:tr>")
            body.append(_tbl(rows, widths))
        elif kind == "table":
            head, data = b[1], b[2]
            widths = b[3] if len(b) > 3 else None
            if not widths:
                n = max(1, len(head))
                widths = [9360 // n] * n
            rows = []
            tcs = [_cell([_para([_run(h, bold=True, size=21)], "center")], widths[i],
                         shade="E8EEF6")
                   for i, h in enumerate(head)]
            rows.append(f'<w:tr><w:trPr><w:tblHeader/></w:trPr>{"".join(tcs)}</w:tr>')
            for r in data:
                tcs = []
                for i, val in enumerate(r[:len(widths)]):
                    tcs.append(_cell([_para([_run(val, size=21)], "center")],
                                     widths[i]))
                rows.append(f"<w:tr>{''.join(tcs)}</w:tr>")
            body.append(_tbl(rows, widths))
        elif kind == "sign":
            n = len(b[1])
            widths = [9360 // n] * n
            tcs = [_cell([_para([_run(f"{lb}：" + " " * 12, size=21)], "left")],
                         widths[i]) for i, lb in enumerate(b[1])]
            body.append(_tbl([f"<w:tr>{''.join(tcs)}</w:tr>"], widths, borders=False))
        elif kind == "space":
            body.append(_para([_run("")], after=120))
    sect = ('<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
            '<w:pgMar w:top="1134" w:right="850" w:bottom="1134" w:left="850" '
            'w:header="851" w:footer="992" w:gutter="0"/></w:sectPr>')
    doc = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/'
           '2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/'
           '2006/relationships"><w:body>' + "".join(body) + sect + "</w:body></w:document>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", DOCX_CT)
        z.writestr("_rels/.rels", DOCX_RELS)
        z.writestr("word/document.xml", doc)
    return buf.getvalue()
