# -*- coding: utf-8 -*-
"""全电发票 / 增值税电子发票 PDF 解析（纯本地，不联网、不外传）。

设计要点：**按坐标取「标签右边那一格」**，不依赖文本流顺序。
同一套全电发票，两张 PDF 的文本顺序可能完全不同（一张先出标签、一张先出值），
按行号顺序猜必然翻车；按坐标找"标签右侧最近的文本"才稳。

对外只有 parse_invoice_pdf() 一个入口，任何情况下都返回 dict（不出异常），
识别不到就留空 + 进 warnings，由页面提示人工补 —— 绝不瞎猜金额。
"""
import os
import re
from collections import Counter

try:
    import pymupdf
except ImportError:                                  # 没装解析库时不影响其它功能
    pymupdf = None

MAX_PAGES = 2            # 只看前两页：发票就一页，多的也不看（防超大文件）

# ------------------------------------------------------------------ 小工具
_CN_DIGIT = {"零": 0, "壹": 1, "贰": 2, "叁": 3, "肆": 4, "伍": 5,
             "陆": 6, "柒": 7, "捌": 8, "玖": 9}
_CN_UNIT = {"拾": 10, "佰": 100, "仟": 1000}
_FULL2HALF = {ord("０") + i: ord("0") + i for i in range(10)}
_FULL2HALF.update({ord("Ａ") + i: ord("A") + i for i in range(26)})
_FULL2HALF.update({ord("ａ") + i: ord("a") + i for i in range(26)})
# 全角括号/冒号/空格 → 半角（票面上两种混用）
_PUNC = {"（": "(", "）": ")", "：": ":", "，": ",", "．": ".", "／": "/",
         "\u3000": " ", "\xa0": " "}


def norm(s):
    """去掉空白、全角转半角，用于比对标签和公司名。"""
    if not s:
        return ""
    s = s.translate(_FULL2HALF).translate(str.maketrans(_PUNC))
    return re.sub(r"\s+", "", s)


def _num(s):
    try:
        return round(float(str(s).replace(",", "").replace("¥", "").strip()), 2)
    except (TypeError, ValueError):
        return None


def cn_amount(s):
    """大写金额 → 数字：肆万陆仟圆整 → 46000.0；认不出返回 None。"""
    t = norm(s)
    if not t:
        return None
    m = re.search(r"[零壹贰叁肆伍陆柒捌玖拾佰仟万亿圆元角分整正]{2,}", t)
    if not m:
        return None
    t = m.group(0)
    total, section, cur = 0.0, 0.0, 0
    for ch in t:
        if ch in _CN_DIGIT:
            cur = _CN_DIGIT[ch]
        elif ch in _CN_UNIT:
            section += (cur or 1) * _CN_UNIT[ch]
            cur = 0
        elif ch == "万":
            section = (section + cur) * 10000
            total += section
            section, cur = 0, 0
        elif ch == "亿":
            total = (total + section + cur) * 100000000
            section, cur = 0, 0
        elif ch == "圆" or ch == "元":
            section += cur
            cur = 0
            total += section
            section = 0
        elif ch == "角":
            total += (cur or 0) * 0.1
            cur = 0
        elif ch == "分":
            total += (cur or 0) * 0.01
            cur = 0
        elif ch in "整正":
            section += cur
            cur = 0
    total += section + cur
    return round(total, 2) if total else None


def cn_date(s):
    """2026年01月23日 / 2026-01-23 / 20260123 → 2026-01-23。"""
    t = norm(s)
    m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", t)
    if m:
        return "%04d-%02d-%02d" % tuple(int(x) for x in m.groups())
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", t)
    if m:
        return "%04d-%02d-%02d" % tuple(int(x) for x in m.groups())
    m = re.search(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)", t)
    if m:
        return "%s-%s-%s" % m.groups()
    return None


def cn_rate(s):
    """13% / 0.13 / 13 → 0.13；免税、*** → None。"""
    t = norm(s)
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", t)
    if m:
        return round(float(m.group(1)) / 100.0, 6)
    m = re.search(r"(?<![\d.])(0\.\d{1,4})(?![\d])", t)
    if m:
        return round(float(m.group(1)), 6)
    return None


# ------------------------------------------------------------- 坐标取文本
def _spans(pdf_path):
    """[{t,x0,y0,x1,y1,p}]，按页、上到下、左到右排好。"""
    out = []
    doc = pymupdf.open(pdf_path)
    try:
        for pno in range(min(doc.page_count, MAX_PAGES)):
            pg = doc[pno]
            d = pg.get_text("dict")
            for blk in d.get("blocks", []):
                for line in blk.get("lines", []):
                    for sp in line.get("spans", []):
                        t = (sp.get("text") or "").strip()
                        if not t:
                            continue
                        x0, y0, x1, y1 = sp["bbox"]
                        out.append({"t": t, "x0": x0, "y0": y0, "x1": x1,
                                    "y1": y1, "p": pno})
            out.extend([])
    finally:
        doc.close()
    out.sort(key=lambda s: (s["p"], round(s["y0"], 1), s["x0"]))
    return out


def _overlap(a0, a1, b0, b1):
    return min(a1, b1) - max(a0, b0)


def _labels(spans, text):
    """所有"标签"span（去空白后以 text 开头）。按 y 排序返回。"""
    key = norm(text)
    hit = [s for s in spans if norm(s["t"]).startswith(key)]
    hit.sort(key=lambda s: (s["p"], s["y0"], s["x0"]))
    return hit


def _right_of(spans, label, ytol=3.0, max_dx=600.0):
    """标签右侧同一行、水平最近的那段文本（票据里就是"右边那一格"）。

    判据用**中心点**而不是 bbox 边：全电发票的标签框带尾随空隙，
    常常压住值的左边缘几个像素，用 `s.x0 >= label.x1` 会把真值排掉。
    """
    lcy = (label["y0"] + label["y1"]) / 2
    lcx = (label["x0"] + label["x1"]) / 2
    lh = max(label["y1"] - label["y0"], 1)
    best, best_x = None, 1e9
    for s in spans:
        if s["p"] != label["p"] or s is label:
            continue
        if (s["x0"] + s["x1"]) / 2 <= lcx + 2:        # 必须在标签中心右侧
            continue
        if s["x0"] - label["x1"] > max_dx:
            continue
        sh = max(s["y1"] - s["y0"], 1)
        if _overlap(label["y0"], label["y1"], s["y0"], s["y1"]) < min(lh, sh) * 0.4:
            if abs((s["y0"] + s["y1"]) / 2 - lcy) > ytol:   # 不在同一行
                continue
        if s["x0"] < best_x:
            best, best_x = s, s["x0"]
    return best


def _right_text(spans, label, gap=6.0, max_dx=600.0):
    """标签右边那一格的**完整**文本：同一行相邻的小段拼起来。

    票面里「¥」和「46000.00」常常是两个 span，只取最近那个就只剩一个「¥」。
    """
    first = _right_of(spans, label, max_dx=max_dx)
    if not first:
        return None
    row = [first]
    x_end = first["x1"]
    lcy = (label["y0"] + label["y1"]) / 2
    lh = max(label["y1"] - label["y0"], 1)
    rest = sorted((s for s in spans
                   if s["p"] == label["p"] and s is not first
                   and s["x0"] >= first["x1"] - 1
                   and (s["x0"] - first["x1"]) <= gap
                   and (_overlap(label["y0"], label["y1"], s["y0"], s["y1"])
                        >= min(lh, max(s["y1"] - s["y0"], 1)) * 0.4
                        or abs((s["y0"] + s["y1"]) / 2 - lcy) <= 3)),
                  key=lambda s: s["x0"])
    for s in rest:
        if s["x0"] - x_end > gap:
            break
        row.append(s)
        x_end = max(x_end, s["x1"])
    return "".join(x["t"].strip() for x in row)


def _value_of(spans, label_text, ytol=3.0, gap=6.0):
    """标签右边那一格的值（多个同名标签时取第一个）。"""
    for lb in _labels(spans, label_text):
        t = _right_text(spans, lb, gap=gap)
        if t and norm(t):
            return t.strip()
    return None


def _column_x(spans, header_text):
    """表头某一列的水平范围 (x0, x1) —— 用同一行左右相邻表头的边界切出来。

    第一列的左边界必须是**页面左边**（明细第一列从 x≈13 开始，
    而表头「项目名称」自己从 x≈45 起，用表头自身 x0 当左边界会把明细全漏掉）。
    """
    hs = [s for s in spans if norm(s["t"]) == norm(header_text)]
    if not hs:
        return None
    h = hs[0]
    cy = (h["y0"] + h["y1"]) / 2
    same = [s for s in spans
            if s["p"] == h["p"] and s is not h
            and abs((s["y0"] + s["y1"]) / 2 - cy) <= 4]
    left = [s["x1"] for s in same if s["x1"] <= h["x0"] + 1]
    right = [s["x0"] for s in same if s["x0"] >= h["x1"] - 1]
    x0 = max(left) if left else 0.0
    x1 = min(right) if right else h["x1"] + 80
    return (x0, x1)


# --------------------------------------------------------------- 发票类型
_TYPE_MAP = [
    (("电子发票(增值税专用发票)", "增值税电子专用发票", "全电发票(增值税专用发票)"),
     "增值税专用发票"),
    (("电子发票(普通发票)", "增值税电子普通发票", "电子发票(电子普通发票)"),
     "增值税普通发票"),
    (("增值税专用发票",), "增值税专用发票"),
    (("增值税普通发票",), "增值税普通发票"),
]


def _invoice_type(text):
    t = norm(text)
    for keys, val in _TYPE_MAP:
        for k in keys:
            if norm(k) in t:
                return val
    return None


# ------------------------------------------------------------------ 主函数
def parse_invoice_pdf(pdf_path, company_name="", company_tax_no=""):
    """解析一张发票 PDF → dict（字段 + 明细名 + 告警）。永不抛异常。"""
    res = {
        "file_name": os.path.basename(pdf_path),
        "ok": False, "needs_ocr": False, "error": None,
        "fields": {"no": "", "code": "", "type": "", "direction": "",
                   "issue_date": "", "amount_gross": "", "tax_rate": "",
                   "status": "待认证", "unit_name": ""},
        "buyer": {"name": "", "tax": ""}, "seller": {"name": "", "tax": ""},
        "items": [], "item_count": 0, "amount_cn": None, "amount_matched": False,
        "warnings": [], "raw_text": "", "pages": 0,
    }
    if pymupdf is None:
        res["error"] = "服务端没有装 PDF 解析库（pymupdf），请联系我重新部署"
        return res
    try:
        doc = pymupdf.open(pdf_path)
        res["pages"] = doc.page_count
        raw = "\n".join(doc[i].get_text() for i in range(min(doc.page_count, MAX_PAGES)))
        doc.close()
    except Exception as e:                            # 坏文件、加密、假 PDF
        res["error"] = "这个 PDF 打不开（%s）" % type(e).__name__
        return res
    res["raw_text"] = raw
    text = norm(raw)

    # 没有文字层 → 扫描件/照片做的 PDF，需要 OCR
    if len(text) < 30:
        res["needs_ocr"] = True
        res["error"] = ("这个 PDF 里没有文字层（多半是扫描件或拍照做的），"
                        "目前只能自动识别电子发票 PDF")
        return res

    try:
        spans = _spans(pdf_path)
    except Exception as e:
        res["error"] = "解析 PDF 内容失败（%s）" % type(e).__name__
        return res

    W = res["warnings"]

    # ---- 发票类型（票面第一行大标题）
    res["fields"]["type"] = _invoice_type(raw) or ""

    # ---- 发票号码 / 代码
    no = _value_of(spans, "发票号码")
    if no:
        m = re.search(r"(\d{8,25})", norm(no))
        no = m.group(1) if m else ""
    if not no:                                        # 文件名兜底：dzfp_号码_…
        m = re.search(r"dzfp[_-]?(\d{8,25})", res["file_name"], re.I)
        no = m.group(1) if m else ""
        if no:
            W.append("发票号码是从文件名里认出来的，请核对")
    res["fields"]["no"] = no or ""
    code = _value_of(spans, "发票代码")
    if code:
        m = re.search(r"(\d{10,15})", norm(code))
        res["fields"]["code"] = m.group(1) if m else ""
    if not no:
        W.append("没认出发票号码，请手工填写")

    # ---- 开票日期
    dt = _value_of(spans, "开票日期")
    dt = cn_date(dt) if dt else None
    if not dt:
        dt = cn_date(res["file_name"])                 # 文件名里的时间戳兜底
        if dt:
            W.append("开票日期是从文件名里认出来的，请核对")
    res["fields"]["issue_date"] = dt or ""
    if not dt:
        W.append("没认出开票日期，请手工填写")

    # ---- 购买方 / 销售方（票面上「名称」出现两次，上面的买方、下面的卖方）
    names = []
    for lb in _labels(spans, "名称"):
        v = _right_of(spans, lb)
        if v:
            names.append(v["t"].strip())
    names = [n for n in names if len(norm(n)) >= 2]
    taxes = []
    for lb in _labels(spans, "统一社会信用代码"):
        v = _right_of(spans, lb)
        if v:
            taxes.append(v["t"].strip())
    if len(names) >= 2:
        res["buyer"]["name"], res["seller"]["name"] = names[0], names[1]
    elif len(names) == 1:
        res["buyer"]["name"] = names[0]
    if len(taxes) >= 2:
        res["buyer"]["tax"], res["seller"]["tax"] = taxes[0], taxes[1]
    elif len(taxes) == 1:
        res["buyer"]["tax"] = taxes[0]

    # ---- 方向 + 对方公司：哪一方是我们，另一方就是往来单位
    me, me_tax = norm(company_name), norm(company_tax_no)
    b_name, s_name = res["buyer"]["name"], res["seller"]["name"]
    b_tax, s_tax = norm(res["buyer"]["tax"]), norm(res["seller"]["tax"])
    buy_is_me = bool((me and me in norm(b_name)) or (me_tax and me_tax == b_tax))
    sell_is_me = bool((me and me in norm(s_name)) or (me_tax and me_tax == s_tax))
    if buy_is_me and not sell_is_me:
        res["fields"]["direction"] = "收到进项发票"
        res["fields"]["unit_name"] = s_name
    elif sell_is_me and not buy_is_me:
        res["fields"]["direction"] = "开具销项发票"
        res["fields"]["unit_name"] = b_name
    else:
        res["fields"]["unit_name"] = b_name or s_name
        if not buy_is_me and not sell_is_me:
            W.append("票面上没找到「%s」，进项/销项请手工选" % (company_name or "本公司"))
        else:
            W.append("买卖双方都像本公司，进项/销项请手工选")
    if not res["fields"]["unit_name"]:
        W.append("没认出对方公司名称，请手工选往-来单位")

    # ---- 价税合计（小写）＋ 大写交叉校验
    amt = None
    v = _value_of(spans, "(小写)") or _value_of(spans, "小写")
    if v:
        m = re.search(r"(?:¥|￥)?\s*([\d,]+\.?\d*)", norm(v).replace("￥", "¥"))
        if m:
            amt = _num(m.group(1))
    if amt is None:                                   # 兜底：票面最后一个 ¥ 金额
        vals = [_num(x) for x in re.findall(r"[¥￥]\s*([\d,]+\.\d{2})", text)]
        vals = [x for x in vals if x]
        if vals:
            amt = max(vals)
            W.append("价税合计是猜的（取票面最大金额 %s），请核对" % amt)
    res["fields"]["amount_gross"] = amt if amt is not None else ""
    if amt is None:
        W.append("没认出价税合计，请手工填写")

    # 大写金额：认出来就对一下（对得上说明这票读得比较准）
    cn_v = _value_of(spans, "价税合计(大写)") or _value_of(spans, "(大写)")
    if not cn_v:
        m = re.search(r"[零壹贰叁肆伍陆柒捌玖拾佰仟万亿圆元角分整正]{2,}", text)
        cn_v = m.group(0) if m else ""
    res["amount_cn"] = cn_amount(cn_v)
    if amt is not None and res["amount_cn"] is not None:
        res["amount_matched"] = abs(amt - res["amount_cn"]) < 0.02
        if not res["amount_matched"]:
            W.append("小写 %.2f 与大写 %s（%s）对不上，请核对"
                     % (amt, cn_v, res["amount_cn"]))

    # ---- 税率（取「税率/征收率」列里出现最多的那个）
    rate = None
    col = _column_x(spans, "税率/征收率")
    if col:
        x0, x1 = col
        head = [s for s in spans if norm(s["t"]) == norm("税率/征收率")]
        y_h = head[0]["y0"] if head else 0
        hits = []
        for s in spans:
            if s["y0"] <= y_h or not (x0 <= s["x0"] <= x1):
                continue
            r = cn_rate(s["t"])
            if r:
                hits.append(r)
        if hits:
            cnt = Counter(hits)
            rate = cnt.most_common(1)[0][0]
            if len(cnt) > 1:
                W.append("票面有多个税率（%s），已按出现最多的 %s 填，请核对"
                         % ("、".join("%g%%" % (r * 100) for r in sorted(cnt)),
                            "%g%%" % (rate * 100)))
    if rate is None:                                  # 兜底：全文最常见的百分数
        hits = [cn_rate(x) for x in re.findall(r"\d{1,2}(?:\.\d+)?%", text)]
        hits = [h for h in hits if h]
        if hits:
            rate = Counter(hits).most_common(1)[0][0]
            W.append("税率是全文扫出来的（%g%%），请核对" % (rate * 100))
    res["fields"]["tax_rate"] = rate if rate is not None else ""
    if rate is None:
        if "免税" in text or "不征税" in text:
            res["fields"]["tax_rate"] = 0
            W.append("票面是免税/不征税，税率已填 0")
        else:
            W.append("没认出税率，请手工填写")

    # ---- 货物/服务名称（只为让人核对用，不写入单据）
    items = []
    if col_name := _column_x(spans, "项目名称"):
        x0, x1 = col_name
        for s in spans:
            t = s["t"].strip()
            if "*" in t and x0 <= s["x0"] <= x1:
                items.append(norm(t).replace(" ", ""))
    res["items"] = items[:60]
    res["item_count"] = len(items)

    # ---- 成色判断
    res["ok"] = bool(res["fields"]["no"] and res["fields"]["amount_gross"] != "")
    if not res["ok"] and not W:
        W.append("这张票的关键字段没认全，请手工补一下")
    return res
