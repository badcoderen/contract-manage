# -*- coding: utf-8 -*-
"""业务逻辑：单号生成、金额计算、核销、附件、查询与统计。"""
import ast
import calendar
import ipaddress
import io
import os
import re
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import uuid
from datetime import datetime, date, timedelta

import meta
import officedocs
from db import (DB_PATH, UPLOAD_DIR, execute, execute_many, now_str, one, query,
                scalar, dict_options)

TOL = 0.01  # 金额容差（元）

ALLOWED_EXT = {"pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx",
               "jpg", "jpeg", "png", "gif", "bmp", "tif", "tiff",
               "txt", "csv", "zip", "rar", "7z", "dwg"}
IMAGE_EXT = {"jpg", "jpeg", "png", "gif", "bmp", "tif", "tiff"}
MAX_UPLOAD = int(os.environ.get("MAX_UPLOAD_MB", "64")) * 1024 * 1024


def round2(x):
    try:
        return round(float(x or 0) + 0.0, 2)
    except (TypeError, ValueError):
        return 0.0


def r4(x):
    try:
        return round(float(x or 0) + 0.0, 4)
    except (TypeError, ValueError):
        return 0.0


def calc_tax(gross, rate):
    """价税合计 + 税率 -> (不含税金额, 税额)"""
    g = round2(gross)
    r = r4(rate)
    if r <= -1:
        return g, 0.0
    net = round2(g / (1 + r)) if r else g
    return net, round2(g - net)


def money(x):
    return f"{round2(x):,.2f}"


def percent(x):
    return f"{r4(x) * 100:.1f}%"


# --------------------------------------------------------------- 单号自动生成
def gen_code(entity_key):
    ent = meta.ENTITIES.get(entity_key)
    if not ent or not ent.get("code"):
        return None
    field, prefix = ent["code"]
    rows = query(f"SELECT {field} AS c FROM {ent['table']} WHERE {field} LIKE ?",
                 (prefix + "%",))
    mx = 0
    for r in rows:
        tail = (r["c"] or "")[len(prefix):]
        if tail.isdigit():
            mx = max(mx, int(tail))
    return f"{prefix}{mx + 1:04d}"


# --------------------------------------------------------------- 列表查询
LIST_SQL = {
    "projects": ("projects t", "t", ""),
    "units": ("units t", "t", ""),
    "cost_categories": ("cost_categories t", "t", ""),
    "dicts": ("dicts t", "t", ""),
    "contracts": ("contracts t LEFT JOIN projects p ON t.project_id = p.id "
                  "LEFT JOIN units u ON t.unit_id = u.id", "t",
                  "p.name AS project_name, u.name AS unit_name"),
    "payments": ("payments t LEFT JOIN contracts h ON t.contract_id = h.id "
                 "LEFT JOIN projects p ON t.project_id = p.id", "t",
                 "h.code AS contract_code, p.name AS project_name"),
    # 费用类别存的是 cost_categories.id（lookup 字段），列表要显示名称。
    # ⚠️ extra 是按逗号切的，**不能**写 IFNULL(cc.name, t.category) 这种带逗号的表达式；
    #    历史上存过名称的旧值，由 db._migrate() 统一换成 id，这里只做纯联表。
    "costs": ("costs t LEFT JOIN projects p ON t.project_id = p.id "
              "LEFT JOIN contracts h ON t.contract_id = h.id "
              "LEFT JOIN units u ON t.unit_id = u.id "
              "LEFT JOIN cost_categories cc ON t.category = cc.id", "t",
              "p.name AS project_name, h.code AS contract_code, u.name AS unit_name, "
              "cc.name AS category_name"),
    "invoices": ("invoices t LEFT JOIN projects p ON t.project_id = p.id "
                 "LEFT JOIN units u ON t.unit_id = u.id "
                 "LEFT JOIN contracts h ON t.contract_id = h.id "
                 "LEFT JOIN cost_categories cc ON t.category = cc.id", "t",
                 "p.name AS project_name, u.name AS unit_name, h.code AS contract_code, "
                 "cc.name AS category_name"),
    "brands": ("brands t", "t",
               "(SELECT COUNT(*) FROM models m WHERE m.brand_id = t.id) AS model_count"),
    "models": ("models t LEFT JOIN brands b ON t.brand_id = b.id", "t",
               "b.name AS brand_name"),
    "quotations": ("quotations t LEFT JOIN units u ON t.unit_id = u.id "
                   "LEFT JOIN projects p ON t.project_id = p.id "
                   "LEFT JOIN contracts h ON t.contract_id = h.id", "t",
                   "u.name AS unit_name, p.name AS project_name, "
                   "h.code AS contract_code"),
    "private_settlements": (
        "private_settlements t LEFT JOIN contracts h ON t.contract_id = h.id "
        "LEFT JOIN projects p ON t.project_id = p.id "
        "LEFT JOIN units u ON t.unit_id = u.id", "t",
        # 关联合同可能有多张：拼成「HT-001、HT-002」
        "(SELECT GROUP_CONCAT(c.code, '、') FROM private_settlement_contracts pc "
        "   JOIN contracts c ON pc.contract_id = c.id "
        "  WHERE pc.settlement_id = t.id) AS contract_code, "
        "h.name AS contract_name, "
        "p.name AS project_name, u.name AS unit_name"),
    "deliveries": ("deliveries t LEFT JOIN contracts h ON t.contract_id = h.id "
                   "LEFT JOIN projects p ON t.project_id = p.id "
                   "LEFT JOIN units u ON t.unit_id = u.id", "t",
                   "h.code AS contract_code, p.name AS project_name, "
                   "u.name AS unit_name"),
    # 工程交付资料：项目名优先取项目管理里的，没有就用手填的（在 enrich 里合）
    "handovers": ("handovers t LEFT JOIN projects p ON t.project_id = p.id", "t",
                  "p.name AS proj_name, "
                  "(SELECT COUNT(*) FROM handover_devices d "
                  "  WHERE d.handover_id = t.id) AS device_count, "
                  "(SELECT COUNT(*) FROM attachments a "
                  "  WHERE a.ref_type = 'handovers' AND a.ref_id = t.id) AS files"),
    # ---- 记工（点工 / 临时工，跟上面的 staff 是两套）----
    "workers": ("workers t", "t", ""),
    "worklogs": ("worklogs t LEFT JOIN workers w ON t.worker_id = w.id "
                 "LEFT JOIN projects p ON t.project_id = p.id", "t",
                 "w.name AS worker_name, w.trade AS worker_trade, "
                 "p.name AS project_name"),
    # ---- 工资管理 ----
    "staff": ("staff t LEFT JOIN users u ON t.user_id = u.id", "t",
              "IFNULL(u.display_name, u.username) AS user_name"),
    # ---- 费用报销 / 出勤 ----
    "advances": ("advances t LEFT JOIN users e ON t.employee_id = e.id", "t",
                 "IFNULL(e.display_name, e.username) AS employee_name"),
    "overtime": ("overtime t LEFT JOIN users e ON t.employee_id = e.id "
                 "LEFT JOIN projects p ON t.project_id = p.id", "t",
                 "IFNULL(e.display_name, e.username) AS employee_name, "
                 "p.name AS project_name"),
    "leaves": ("leaves t LEFT JOIN users e ON t.employee_id = e.id", "t",
               "IFNULL(e.display_name, e.username) AS employee_name"),
    "trips": ("trips t LEFT JOIN users e ON t.employee_id = e.id "
              "LEFT JOIN projects p ON t.project_id = p.id", "t",
              "IFNULL(e.display_name, e.username) AS employee_name, "
              "p.name AS project_name"),
}


def _extra_aliases(extra):
    """从联表附加列里取出别名："b.name AS brand_name" → {brand_name}"""
    out = set()
    for part in (extra or "").split(","):
        bits = part.split()
        if len(bits) >= 2 and bits[-2].upper() == "AS":
            out.add(bits[-1])
    return out


def _extra_exprs(extra):
    """"b.name AS brand_name" → {brand_name: "b.name"}。

    联表列的**表达式**：WHERE 里不能用 SELECT 起的别名，只能用表达式本身。
    """
    out = {}
    for part in (extra or "").split(","):
        bits = part.split()
        if len(bits) >= 3 and bits[-2].upper() == "AS":
            out[bits[-1]] = " ".join(bits[:-2])
    return out


def _order_sql(order, alias="t", extra_cols=()):
    """排序片段。

    联表算出来的列（如 brand_name）在 ORDER BY 里**不能带表别名**
    （t.brand_name 不存在，会报 no such column），要直接用别名。
    """
    parts = []
    for tok in (order or "id").split(","):
        bits = tok.strip().split()
        if not bits:
            continue
        col = bits[0]
        head = col if col in extra_cols else f"{alias}.{col}"
        parts.append(head + " " + " ".join(bits[1:]).strip())
    return ", ".join(parts).strip()


def unit_id_by_name(name):
    """按票面上的单位名称找往来单位。

    比对前先去掉空白与全角/半角差异（票面名称常带全角括号）。
    三级匹配：完全相同 → 互为包含（取最短的）→ 都没有则返回 None。
    返回 (id, 匹配置信度)：exact / fuzzy / None，供页面提示用。
    """
    want = meta_norm(name)
    if not want:
        return None, None
    rows = query("SELECT id, name FROM units")
    for r in rows:                                  # 1) 去空白后完全相同
        if meta_norm(r["name"]) == want:
            return r["id"], "exact"
    cand = [r for r in rows
            if meta_norm(r["name"]) and (meta_norm(r["name"]) in want
                                         or want in meta_norm(r["name"]))]
    if len(cand) == 1:                              # 2) 互为包含且唯一
        return cand[0]["id"], "fuzzy"
    if cand:                                        # 多个包含 → 取名字最长的
        cand.sort(key=lambda r: -len(meta_norm(r["name"])))
        return cand[0]["id"], "fuzzy"
    return None, None


def meta_norm(s):
    """去掉空白、全角括号/冒号，用于名称比对。"""
    if not s:
        return ""
    s = str(s)
    for a, b in (("（", "("), ("）", ")"), ("：", ":"), ("，", ","),
                 ("\u3000", ""), (" ", ""), ("\t", ""), ("\n", "")):
        s = s.replace(a, b)
    return s.strip().upper()


def sort_columns(entity_key):
    """列表页可排序的列 → ORDER BY 表达式（表头能点的那些）。

    白名单式：只认元数据 SORT_SQL 里声明的、表里真有的列、联表算出来的别名。
    前端只传**列名**，SQL 表达式全在服务端拼，手改 URL 也进不了 SQL。
    """
    ent = meta.ENTITIES[entity_key]
    frm, alias, extra = LIST_SQL[entity_key]
    extra_cols = _extra_aliases(extra)
    real = _table_cols(ent["table"])
    decl = (getattr(meta, "SORT_SQL", None) or {}).get(entity_key, {})
    out = {}
    for c in ent.get("list") or []:
        nm = c.get("name")
        if not nm or c.get("nosort"):
            continue
        if nm in decl:
            out[nm] = decl[nm]
        elif nm in real:                      # 表里的真实列
            out[nm] = f"{alias}.{nm}"
        elif nm in extra_cols:                # 联表列不能带表别名
            out[nm] = nm
    return out


def list_rows(entity_key, q="", page=1, per_page=25, scope=None,
              sort=None, sort_dir="asc"):
    """scope = (where_fragment, args) 用于员工的数据范围限制；None 表示不限。"""
    ent = meta.ENTITIES[entity_key]
    frm, alias, extra = LIST_SQL[entity_key]
    where, args = "", []
    q = (q or "").strip()
    extra_cols = _extra_aliases(extra)
    if q:
        # 只在实际存在的列 / 联表别名上做 LIKE：搜索项里写了别的名字
        # （如 t.brand_name 这种不存在的列）会直接 500
        real = _table_cols(ent["table"])
        exprs = _extra_exprs(extra)
        cols = []
        for c in (ent.get("search") or []):
            if c in real:
                cols.append(f"{alias}.{c}")
            elif c in exprs:                  # 联表列要换成表达式，别名在 WHERE 里不认
                cols.append(exprs[c])
        if cols:
            where = " WHERE (" + " OR ".join(
                f"IFNULL(CAST({c} AS TEXT),'') LIKE ?" for c in cols) + ")"
            args = [f"%{q}%"] * len(cols)
    if scope:
        frag, sargs = scope
        if frag:
            where += (" AND " if where else " WHERE ") + frag
            args = list(args) + list(sargs)
    total = scalar(f"SELECT COUNT(*) FROM {frm}{where}", args)
    per_page = max(5, min(200, int(per_page or 25)))
    pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(pages, int(page or 1)))
    off = (page - 1) * per_page
    cols = f"{alias}.*" + (("," + extra) if extra else "")
    order = _order_sql(ent.get("order"), alias, extra_cols)
    scols = sort_columns(entity_key)
    key = (sort or "").strip()
    if key in scols:
        # 方向只认 asc/desc（手改 URL 传别的值一律当升序）
        desc = str(sort_dir or "").lower() in ("desc", "d", "down", "-1")
        # 后面接上实体默认排序：同值时次序稳定，翻页不会跳来跳去
        order = "%s %s, %s" % (scols[key], "DESC" if desc else "ASC", order)
    rows = query(f"SELECT {cols} FROM {frm}{where} "
                 f"ORDER BY {order} LIMIT ? OFFSET ?",
                 args + [per_page, off])
    enrich(entity_key, rows)
    return rows, total, pages, page


def enrich(entity_key, rows):
    """补齐列表页需要的派生列。"""
    if not rows:
        return rows
    ids = [r["id"] for r in rows]
    ph = ",".join("?" * len(ids))
    if entity_key == "contracts":
        fmap = {r["ref_id"]: r["c"] for r in query(
            f"SELECT ref_id, COUNT(*) c FROM attachments "
            f"WHERE ref_type='contracts' AND ref_id IN ({ph}) GROUP BY ref_id", ids)}
        for r in rows:
            r["files"] = fmap.get(r["id"], 0)
    elif entity_key == "handovers":
        # 「对应项目」列：项目管理里选了就用项目名，否则用手填的项目名。
        # 两条来源合成一个可搜索、可排序的列（排序走 SORT_SQL 里的 proj_label）
        for r in rows:
            r["proj_label"] = r.get("proj_name") or r.get("project_name") or ""
    elif entity_key == "payments":
        smap = {r["payment_id"]: r["c"] for r in query(
            f"SELECT payment_id, COUNT(*) c FROM settlements "
            f"WHERE payment_id IN ({ph}) GROUP BY payment_id", ids)}
        for r in rows:
            r["settle_count"] = smap.get(r["id"], 0)
    elif entity_key == "invoices":
        smap = {r["invoice_id"]: round2(r["s"]) for r in query(
            f"SELECT invoice_id, SUM(amount) s FROM settlements "
            f"WHERE invoice_id IN ({ph}) GROUP BY invoice_id", ids)}
        for r in rows:
            st_, un_, pr_, ps_ = invoice_settle_view(r, smap.get(r["id"], 0.0))
            r["settled"], r["unsettled"] = st_, un_
            r["progress"], r["pay_state"] = pr_, ps_
    elif entity_key == "advances":
        used = {r["advance_id"]: round2(r["s"]) for r in query(
            f"SELECT advance_id, SUM(amount) s FROM costs "
            f"WHERE advance_id IN ({ph}) "
            f"AND IFNULL(approval_status,'') IN ('已批准','已打款') "
            f"GROUP BY advance_id", ids)}
        for r in rows:
            amt = round2(r.get("amount"))
            u = used.get(r["id"], 0.0)
            r["used"] = u
            r["balance"] = round2(amt - u)
            # 「待打款」表示钱还没给员工，不参与核销状态的推导
            if (r.get("status") or "") == "待打款":
                continue
            # 已打款之后按核销情况细分，避免手工填错
            if u <= TOL:
                r["status"] = "已打款"
            elif u + TOL < amt:
                r["status"] = "部分核销"
            else:
                r["status"] = "已核销完"
    return rows


def invoice_settle_view(row, settled):
    """发票的核销口径：状态「已收讫」＝钱已经付清了 → 一律按「已结清」看。

    2026-09-18 用户要求：发票管理里选了「已收讫」，列表就该显示已结清。
    所以「已核销 / 未结清 / 核销进度 / 收付状态」四个派生值**一起**按已结清算，
    否则会出现"进度 0% 但状态已结清"这种自相矛盾的画面。

    只影响展示与台账口径（列表 / 发票面板 / 未结清台账），
    **不改 settlements 明细本身**，也不影响"付款能不能核销这张票"的校验。
    """
    gross = round2(row.get("amount_gross"))
    if (row.get("status") or "") == "已收讫":
        return gross, 0.0, 1.0, "已结清"
    return (settled, round2(gross - settled),
            (settled / gross) if gross > 0 else 0.0, pay_state(settled, gross))


def pay_state(settled, gross):
    if gross <= 0:
        return "未收付"
    if settled >= gross - TOL:
        return "已结清"
    if settled > TOL:
        return "部分收付"
    return "未收付"


def get_row(entity_key, row_id):
    frm, alias, extra = LIST_SQL[entity_key]
    cols = f"{alias}.*" + (("," + extra) if extra else "")
    row = one(f"SELECT {cols} FROM {frm} WHERE {alias}.id = ?", (row_id,))
    if row:
        enrich(entity_key, [row])
    return row


# --------------------------------------------------------------- 表单 → 数据
def field_value(field, form):
    t = field["type"]
    raw = form.get(field["name"])
    if t == "checkbox":
        return 1 if raw in ("1", "on", "true", "True") else 0
    raw = (raw or "").strip()
    if t in ("money", "rate", "number"):
        if raw == "":
            return 0.0 if t != "number" else None
        try:
            return float(raw.replace(",", ""))
        except ValueError:
            return 0.0
    if t == "lookup":
        if raw == "":
            return None
        try:
            return int(raw)
        except ValueError:
            return None
    if raw == "":
        return None
    return raw


def row_get(row, key, default=None):
    """从 dict / sqlite3.Row / MultiDict 里安全取值。"""
    if row is None:
        return default
    try:
        val = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if val is None else val


def form_to_data(entity_key, form, base_row=None):
    """把表单内容转成字段字典。

    base_row：编辑时的原始记录。自动编号等「表单里不展示」的字段取不到值，
    必须沿用原值，否则保存会把已生成的单号清空。
    """
    ent = meta.ENTITIES[entity_key]
    auto_code = (ent.get("code") or (None,))[0]
    data = {}
    for field in ent["fields"]:
        name = field["name"]
        if name == auto_code or field.get("mode") == "hidden":
            data[name] = row_get(base_row, name)
            continue
        data[field["name"]] = field_value(field, form)

    calc = ent.get("calc")
    if calc:
        gross = round2(data.get(calc["gross"]))
        rate = r4(data.get(calc["rate"]))
        net, tax = calc_tax(gross, rate)
        data[calc["gross"]] = gross
        data[calc["rate"]] = rate
        data[calc["net"]] = net
        data[calc["tax"]] = tax

    if ent.get("code"):
        field, _prefix = ent["code"]
        if not data.get(field):
            data[field] = gen_code(entity_key)
    return data


def validate(entity_key, data, row_id=None):
    ent = meta.ENTITIES[entity_key]
    errors = []
    for field in ent["fields"]:
        if field.get("required") and not data.get(field["name"]):
            errors.append(f"【{field['label']}】不能为空")
    code_spec = ent.get("code")
    if code_spec:
        field = code_spec[0]
        val = data.get(field)
        if val:
            sql = f"SELECT id FROM {ent['table']} WHERE {field} = ?"
            args = [val]
            if row_id:
                sql += " AND id <> ?"
                args.append(row_id)
            if one(sql, args):
                errors.append(f"【{field}】{val} 已存在，请换一个编号")
    return errors


def cost_paid_expr(alias="k"):
    """费用的「已付金额」SQL 表达式（统一口径，别各处自己拼）。

    老数据若 paid_amount 还没回填，就按支付状态兜底（已支付＝全额）。
    首页「累计付款」、按项目明细表都用它，保证两处口径永远一致。
    """
    return ("IFNULL(%s.paid_amount, "
            "CASE WHEN IFNULL(%s.pay_status,'') = '已支付' "
            "THEN IFNULL(%s.amount,0) ELSE 0 END)" % (alias, alias, alias))


def cost_normalize_paid(data, ent=None):
    """把费用的「支付状态」与「已付金额」对齐（保存时调用，服务端兜底）。

    2026-09-18 用户确认的口径：
      · 已支付   ⇒ 已付金额 = 金额（钱全部付清了）
      · 未支付   ⇒ 已付金额 = 0
      · 部分支付 ⇒ 用填的金额，越界就据此修正状态（填 0 当未支付、填满当已支付）
      · 状态留空 ⇒ 由已付金额反推状态（0→未支付 / 满额→已支付 / 中间→部分支付），
                   这样"只填了已付金额没选状态"也不会把金额丢掉
    没有 paid_pair 声明的实体原样返回。
    """
    pair = (ent or {}).get("paid_pair")
    if not pair:
        return data
    st_k, paid_k, amt_k = pair["status"], pair["paid"], pair["amount"]
    amount = round2(_num(data.get(amt_k)))
    paid = round2(_num(data.get(paid_k)))
    st = (data.get(st_k) or "").strip()
    if st == "已支付":
        paid = amount
    elif st == "未支付":
        paid = 0.0
    elif st == "部分支付":
        if paid <= 0:
            paid, st = 0.0, "未支付"
        elif amount and paid >= amount:
            paid, st = amount, "已支付"
    else:
        if paid <= 0:
            paid, st = 0.0, "未支付"
        elif amount and paid >= amount:
            paid, st = amount, "已支付"
        else:
            st = "部分支付"
    data[st_k] = st
    data[paid_k] = paid
    return data


def save_entity(entity_key, data, row_id=None):
    ent = meta.ENTITIES[entity_key]
    data = cost_normalize_paid(data, ent)      # 费用：支付状态 ⇄ 已付金额 对齐
    cols = [fld["name"] for fld in ent["fields"]]
    vals = [data.get(c) for c in cols]
    if row_id:
        sets = ", ".join(f"{c} = ?" for c in cols)
        execute(f"UPDATE {ent['table']} SET {sets} WHERE id = ?", vals + [row_id])
        return int(row_id)
    marks = ", ".join("?" * len(cols))
    return execute(f"INSERT INTO {ent['table']} ({', '.join(cols)}) "
                   f"VALUES ({marks})", vals)


# 删除顺序：先子后父，避免留下悬挂引用
def delete_entity(entity_key, row_id):
    ent = meta.ENTITIES[entity_key]
    tbl = ent["table"]

    refs = _references(tbl, row_id)
    if refs:
        raise ValueError("该记录已被以下数据引用，无法删除：" + "、".join(refs))

    # 先清理从属数据，再删主记录
    if tbl == "projects":
        # 项目授权是附属配置：删项目时一起清掉（老库没有 ON DELETE CASCADE）
        execute("DELETE FROM project_access WHERE project_id = ?", (row_id,))
    if tbl == "payments":
        # 付款记录没了，它核销过的票要重新判一次（核销不满 → 退回未付款）
        touched = [r["invoice_id"] for r in query(
            "SELECT DISTINCT invoice_id FROM settlements WHERE payment_id = ?",
            (row_id,))]
        execute("DELETE FROM settlements WHERE payment_id = ?", (row_id,))
        invoice_auto_status_many(touched)
    if tbl == "staff":
        # 没做过工资表的员工才删得掉（上面 REF_CHECKS 挡住了有工资记录的）
        execute("DELETE FROM payroll_exclude WHERE staff_id = ?", (row_id,))
    if tbl == "private_settlements":
        execute("DELETE FROM private_settlement_contracts WHERE settlement_id = ?",
                (row_id,))
    # 明细（报价明细 / 合同清单 / 送货明细 …）随主记录一起删
    for spec in (ent.get("details") or []):
        execute(f"DELETE FROM {spec['table']} WHERE {spec['fk']} = ?", (row_id,))
    if tbl == "invoices":
        # 进项发票自动生成的费用跟着发票走：票删了/作废了，这笔成本就不存在了
        for c in query("SELECT id FROM costs WHERE invoice_id = ?", (row_id,)):
            _delete_cost(c["id"])
    if ent.get("attachments") or tbl == "contracts":
        # 附件跟着单据走：删单据就别把 PDF 留在磁盘上变孤儿
        for a in query("SELECT stored_name FROM attachments "
                       "WHERE ref_type=? AND ref_id=?", (entity_key, row_id)):
            _remove_file(a["stored_name"])
        execute("DELETE FROM attachments WHERE ref_type=? AND ref_id=?",
                (entity_key, row_id))
    try:
        execute(f"DELETE FROM {tbl} WHERE id = ?", (row_id,))
    except sqlite3.IntegrityError:
        raise ValueError("该记录仍被其他数据引用，无法删除") from None


# 每张表各自需要检查的外键引用：表 -> [(引用方, 字段, 显示名)]
REF_CHECKS = {
    "projects": [("contracts", "project_id", "合同"),
                 ("payments", "project_id", "付款记录"),
                 ("costs", "project_id", "费用"),
                 ("invoices", "project_id", "发票"),
                 ("quotations", "project_id", "报价单"),
                 ("deliveries", "project_id", "送货单"),
                 ("private_settlements", "project_id", "对私计算"),
                 ("handovers", "project_id", "工程交付资料"),
                 ("overtime", "project_id", "加班单"),
                 ("trips", "project_id", "出差单"),
                 ("worklogs", "project_id", "记工")],
    "units": [("contracts", "unit_id", "合同"),
              ("costs", "unit_id", "费用"),
              ("invoices", "unit_id", "发票"),
              ("quotations", "unit_id", "报价单"),
              ("deliveries", "unit_id", "送货单"),
              ("private_settlements", "unit_id", "对私计算")],
    "contracts": [("payments", "contract_id", "付款记录"),
                  ("costs", "contract_id", "费用"),
                  ("invoices", "contract_id", "发票"),
                  ("deliveries", "contract_id", "送货单"),
                  ("quotations", "contract_id", "报价单"),
                  ("private_settlements", "contract_id", "对私计算"),
                  ("private_settlement_contracts", "contract_id", "对私计算关联"),
                  ("private_settlement_items", "row_contract_id", "对私明细")],
    "invoices": [("settlements", "invoice_id", "付款核销记录")],
    "advances": [("costs", "advance_id", "费用核销")],
    "brands": [("models", "brand_id", "型号"),
               ("quotation_items", "brand_id", "报价明细")],
    "models": [("quotation_items", "model_id", "报价明细")],
    "workers": [("worklogs", "worker_id", "记工")],
    # 员工一旦做过工资表就不能删（历史月份会缺人）—— 离职请改成「停用」
    "staff": [("payroll", "staff_id", "月度工资表")],
}


def _references(tbl, row_id):
    """检查 row_id 是否被其他表引用（只查该表真正相关的外键列）。"""
    out = []
    for t, col, label in REF_CHECKS.get(tbl, []):
        n = scalar(f"SELECT COUNT(*) FROM {t} WHERE {col} = ?", (row_id,))
        if n:
            out.append(f"{label} {n} 条")
    # 费用类别被费用表按 id 引用（category 存的是 cost_categories.id）
    if tbl == "cost_categories":
        n = scalar("SELECT COUNT(*) FROM costs WHERE category = ?", (str(row_id),))
        if n:
            out.append(f"费用 {n} 条")
    # 工人一旦记过工就不能删（删了历史记工就缺人）—— 走的人请改成「停用」
    if tbl == "workers":
        n = scalar("SELECT COUNT(*) FROM worklogs WHERE worker_id = ?", (row_id,))
        if n:
            out.append(f"记工 {n} 条")
    return out


# --------------------------------------------------------------- 核销
def invoice_settled(invoice_id, exclude_payment=None):
    sql = "SELECT IFNULL(SUM(amount),0) s FROM settlements WHERE invoice_id = ?"
    args = [invoice_id]
    if exclude_payment:
        sql += " AND payment_id <> ?"
        args.append(exclude_payment)
    return round2(scalar(sql, args))


# ------------------------------------------------- 发票状态 ↔ 核销（自动）
# 2026-10-11 用户确认的规则：在「付款记录」里把一张票核销满了，
# 这张票的【发票状态】就该自动变成「已收讫」（＝这笔钱付清了）。
# 以前得手工去发票页改下拉，实际没人改 —— 于是首页「已开票付款金额」永远是 0。
#
# paid_auto 用来记住「这个已收讫是系统标的，还是人工选的」：
#   · 系统标的（=1）：核销被撤销后会退回「费用未付款」
#   · 人工选的（=0）：一律不动，人工说了算
def invoice_auto_status(inv_id):
    """按核销情况维护发票状态。返回处理后的状态。"""
    inv = one("SELECT id, status, IFNULL(amount_gross,0) AS gross, "
              "IFNULL(paid_auto,0) AS auto FROM invoices WHERE id = ?", (inv_id,))
    if not inv:
        return None
    st = inv["status"] or ""
    # 作废票不碰；价税合计为 0 的票也标不了（核销 0 会算成"已满"）
    if st == "已作废" or (inv["gross"] or 0) <= 0:
        return st
    full = invoice_settled(inv_id) >= round2(inv["gross"]) - TOL
    if full:
        if st != "已收讫":
            execute("UPDATE invoices SET status = '已收讫', paid_auto = 1 "
                    "WHERE id = ?", (inv_id,))
        # 进项票：派生费用的支付状态跟着走（销项票这里什么都不会发生）。
        # 即使状态本来就是「已收讫」（比如人工标过、后来又补录了付款核销），
        # 也要重算一次 —— 否则费用仍是「已支付」，首页累计付款会把这笔钱算两遍。
        invoice_cost_sync(inv_id)
        return "已收讫"
    if (not full) and st == "已收讫" and inv["auto"]:
        execute("UPDATE invoices SET status = '费用未付款', paid_auto = 0 "
                "WHERE id = ?", (inv_id,))
        invoice_cost_sync(inv_id)
        return "费用未付款"
    return st


def invoice_auto_status_many(inv_ids):
    """一次付款可能核销到多张票，逐个过一遍上面的规则。"""
    for i in sorted({int(x) for x in (inv_ids or []) if x}):
        invoice_auto_status(i)


def invoice_status_manual(inv_id, old_status, new_status):
    """人工在发票页改了状态 → 收回「系统自动标」的归属（paid_auto=0）。

    改过就归人工：以后核销再变动也不会自动把它退回未付款。
    """
    if (old_status or "") != (new_status or ""):
        execute("UPDATE invoices SET paid_auto = 0 WHERE id = ?", (inv_id,))


def payment_settled(payment_id):
    return round2(scalar(
        "SELECT IFNULL(SUM(amount),0) s FROM settlements WHERE payment_id = ?",
        (payment_id,)))


def invoice_pick_list(scope=None):
    """给核销下拉用的发票清单；scope=(where, args) 用于员工的数据范围限制。"""
    where, args = "", []
    if scope:
        frag, sargs = scope
        if frag:
            where = " WHERE " + frag
            args = list(sargs)
    rows = query("""
        SELECT v.id, v.no, v.type, v.direction, v.amount_gross, v.issue_date,
               u.name AS unit_name, p.name AS project_name,
               IFNULL((SELECT SUM(s.amount) FROM settlements s
                       WHERE s.invoice_id = v.id), 0) AS settled
        FROM invoices v
        LEFT JOIN units u ON v.unit_id = u.id
        LEFT JOIN projects p ON v.project_id = p.id""" + where + """
        ORDER BY v.issue_date, v.id
    """, tuple(args))
    out = []
    for r in rows:
        gross = round2(r["amount_gross"])
        settled = round2(r["settled"])
        unit = (r["unit_name"] or "").strip()
        date = (r["issue_date"] or "").strip()
        # 下拉里**对方公司放最前面**：手机上下拉很窄、只显示开头几个字，
        # 只写发票号用户根本认不出来（发票号没人记得住）。
        # 顺序＝公司 → 开票日期 → 发票号（价税合计）：越靠前越能认出是哪张票，
        # 万一被截断，剩下的信息还能在行内的提示里看全（见 app.js 的 refreshInfo）。
        # 有公司名：公司｜日期｜发票号（价税合计）；没填公司名：退回「日期｜发票号（金额）」。
        head = "｜".join([x for x in (unit, date) if x])
        if head:
            label = f"{head}｜{r['no']}（{money(gross)}）"
        else:
            label = f"{r['no']}（{money(gross)}）"
        out.append({
            "id": r["id"],
            "label": label,
            "no": r["no"],
            "gross": gross,
            "settled": settled,
            "remaining": round2(gross - settled),
            "unit": unit,
            "date": r["issue_date"] or "",
        })
    return out


def settlement_rows(payment_id):
    return query("""
        SELECT s.id, s.invoice_id, s.amount, s.ratio, s.memo,
               v.no, v.amount_gross, v.issue_date
        FROM settlements s LEFT JOIN invoices v ON s.invoice_id = v.id
        WHERE s.payment_id = ? ORDER BY s.id
    """, (payment_id,))


def parse_settlements(form, payment_id=None):
    """解析表单里的核销行，返回 (rows, errors)。"""
    inv_ids = form.getlist("s_invoice_id")
    amounts = form.getlist("s_amount")
    ratios = form.getlist("s_ratio")
    memos = form.getlist("s_memo")
    rows, errors = [], []
    # 同一张票可能被同一个付款单拆成多行：必须把本单前面行的金额累加，
    # 否则每行都只跟「其它付款」比，合计能超出发票价税合计（实测能超 20%）。
    pending = {}
    for i, raw in enumerate(inv_ids):
        raw = (raw or "").strip()
        memo = (memos[i] if i < len(memos) else "") or ""
        memo = memo.strip()
        if not raw:
            continue
        try:
            inv_id = int(raw)
        except ValueError:
            continue
        amt_s = (amounts[i] if i < len(amounts) else "")
        rat_s = (ratios[i] if i < len(ratios) else "")
        amt = rat = None
        try:
            amt = float(str(amt_s).replace(",", "")) if str(amt_s).strip() else None
        except ValueError:
            amt = None
        try:
            rat = float(str(rat_s).replace(",", "")) if str(rat_s).strip() else None
        except ValueError:
            rat = None

        inv = one("SELECT id, no, amount_gross FROM invoices WHERE id = ?", (inv_id,))
        if not inv:
            errors.append(f"第 {i + 1} 行：所选发票不存在")
            continue
        gross = round2(inv["amount_gross"])
        if amt is None and rat is not None:
            amt = round2(gross * rat)
        if amt is None:
            amt = 0.0
        amt = round2(amt)
        if amt < 0:
            errors.append(f"第 {i + 1} 行：核销金额不能为负数")
            continue
        if amt == 0:
            continue
        ratio = r4(amt / gross) if gross > 0 else 0.0
        used = invoice_settled(inv_id, exclude_payment=payment_id)
        used_here = pending.get(inv_id, 0.0)
        if used + used_here + amt > gross + TOL:
            errors.append(
                f"第 {i + 1} 行：发票 {inv['no']} 价税合计 {money(gross)}，"
                f"已核销 {money(used + used_here)}（含本单前面的行），"
                f"本次 {money(amt)}，合计超出 "
                f"{money(used + used_here + amt - gross)}")
            continue
        pending[inv_id] = used_here + amt
        rows.append({"invoice_id": inv_id, "amount": amt, "ratio": ratio, "memo": memo})
    return rows, errors


def save_settlements(payment_id, rows):
    # 先记下这张付款记录原来核销到哪些票 —— 明细换掉后它们也要重新判一次
    touched = [r["invoice_id"] for r in query(
        "SELECT DISTINCT invoice_id FROM settlements WHERE payment_id = ?",
        (payment_id,))]
    execute("DELETE FROM settlements WHERE payment_id = ?", (payment_id,))
    for r in rows:
        execute("INSERT INTO settlements (payment_id, invoice_id, amount, ratio, memo) "
                "VALUES (?,?,?,?,?)",
                (payment_id, r["invoice_id"], r["amount"], r["ratio"], r["memo"]))
        touched.append(r["invoice_id"])
    # 核销变了 → 发票状态跟着走（核销满自动记「已收讫」）
    invoice_auto_status_many(touched)


# ------------------------------------------------ 进项发票 → 费用支出
# ⚠️ 费用的 category 存的是 cost_categories.id（lookup）。以前这里返回名称，
#    结果库里"名称 + id"两种值混着存 —— 2026-10-18 统一成 id（db._migrate 会把旧值换掉）。
def cost_category_pairs():
    """[(id, name)]，给下拉和自动归类用。"""
    return [(r["id"], r["name"]) for r in query(
        "SELECT id, name FROM cost_categories WHERE enabled = 1 ORDER BY sort, name")]


def cost_category_names():
    """只要名称（兼容旧调用点）。"""
    return [n for _, n in cost_category_pairs()]


def cost_category_id(v):
    """把类别规范成 id：传进来的可能是 id，也可能是名称（老链接 / 手工 POST）。

    认不出来就返回 None（宁可留空，也别把名称写进 code 列）。
    """
    s = str(v if v is not None else "").strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    row = one("SELECT id FROM cost_categories WHERE name = ? LIMIT 1", (s,))
    return row["id"] if row else None


def guess_cost_category(item_names, cats=None):
    """票面是服务 / 劳务 → 人工费；其余（货物、设备）→ 设备费。返回**类别 id**。

    票面明细形如 "*劳务*安装调试费" / "*计算机外部设备*硬盘"，
    所以按"服务/劳务"这两个词出现得多不多来判。类别被改名了就往后退。
    """
    pairs = cats if cats is not None else cost_category_pairs()
    names = [meta_norm(x) for x in (item_names or []) if x]
    svc = sum(1 for n in names if "服务" in n or "劳务" in n)
    if names and svc * 2 >= len(names):
        order = ("人工费", "设备费", "材料费", "其他")
    else:
        # 货物/设备：优先「设备费」，没有就退到「材料费」——别退到人工费去
        order = ("设备费", "材料费", "机械费", "其他", "人工费")
    for want in order:
        for cid, cname in pairs:
            if cname == want:
                return cid
    return pairs[0][0] if pairs else None


def contract_items_of(contract_id):
    if not contract_id:
        return []
    return query("SELECT id, contract_id, seq, name, spec, unit, qty, amount "
                 "FROM contract_items WHERE contract_id = ? ORDER BY seq, id",
                 (contract_id,))


def guess_contract_item(contract_id, item_names):
    """票面品名 → 合同清单项：**唯一**对得上才自动对应，否则留空（让人自己选）。"""
    if not contract_id or not item_names:
        return None
    hits = []
    for it in contract_items_of(contract_id):
        cname = meta_norm(it["name"])
        if not cname:
            continue
        for nm in item_names:
            b = meta_norm(nm)
            short = b.split("*")[-1] if "*" in b else b     # "*类别*名称" → 名称
            if cname in b or (short and (cname in short or short in cname)):
                hits.append(it["id"])
                break
    return hits[0] if len(hits) == 1 else None


def invoice_cost_of(inv_id):
    """发票对应的自动费用（带它已对应的清单项）。"""
    row = one("SELECT * FROM costs WHERE invoice_id = ? ORDER BY id LIMIT 1", (inv_id,))
    if row:
        row["items"] = query(
            "SELECT * FROM cost_items WHERE cost_id = ? ORDER BY id", (row["id"],))
    return row


def _delete_cost(cost_id):
    execute("DELETE FROM cost_items WHERE cost_id = ?", (cost_id,))
    execute("DELETE FROM costs WHERE id = ?", (cost_id,))


def invoice_cost_sync(inv_id, category=None, item_id=None, item_names=None):
    """把「收到进项发票」同步成一条费用支出。

    口径（2026-09-18 用户确认）：
      · 只有进项发票生成费用；金额 = 发票的**价税合计（含税）**；
      · 一生成就计入项目成本 / 收益（用户选的口径）；
      · 发票状态「已收讫」＝钱付了 ⇒ 费用的支付状态＝已支付、已付金额＝全额；
        但**这张票如果在付款记录里被核销过**，钱已经算在付款记录里了，
        费用就保持「未支付」，免得首页「累计付款」把同一笔钱算两遍；
      · 发票状态「已作废」⇒ 自动生成的费用一并删掉（钱不用付了）；
      · 类别默认按票面猜（服务/劳务→人工费，其余→设备费），category 可覆盖；
      · 对应合同清单项：票面品名唯一对得上就自动对应，否则留空；item_id 可指定。
    返回 (费用行, 动作)；动作 = created / updated / deleted / none
    """
    inv = one("SELECT * FROM invoices WHERE id = ?", (inv_id,))
    if not inv:
        return None, "none"
    old = one("SELECT * FROM costs WHERE invoice_id = ? ORDER BY id LIMIT 1", (inv_id,))
    if (inv.get("direction") or "") != "收到进项发票" or \
            (inv.get("status") or "") == "已作废":
        if old:
            _delete_cost(old["id"])
            return None, "deleted"
        return None, "none"

    amount = round2(inv.get("amount_gross"))
    # 钱是怎么付的，决定这条费用的支付状态 —— 两条路**不能同时算**：
    #   · 这张票在「付款记录」里被核销过 → 钱已经记在付款记录里了
    #     （首页「累计付款」按付款记录 + 费用已付两边相加），费用这边保持未支付；
    #   · 没走核销 → 全看发票状态：已收讫＝钱付了 ⇒ 费用＝已支付、已付＝全额。
    paid = ("已支付" if (inv.get("status") or "") == "已收讫"
            and invoice_settled(inv_id) <= TOL else "未支付")
    paid_amt = amount if paid == "已支付" else 0.0     # 已收讫＝钱付了，已付即全额
    unit = one("SELECT name FROM units WHERE id = ?", (inv.get("unit_id"),)) if \
        inv.get("unit_id") else None
    # category 存的是**类别 id**（下拉值也是 id）；兼容老链接传名称的情况。
    # 优先级：表单里临时改的类别 → 发票上选的类别 → 原费用上的类别 → 按票面猜。
    cat = cost_category_id(category)
    if not cat:
        cat = cost_category_id(inv.get("category"))
    if not cat:
        cat = cost_category_id(old.get("category")) if old else None
    if not cat:
        cat = guess_cost_category(item_names)
    # 把最终类别写回发票：下次打开表单看到的就是这一条，发票列表也能按类别区分
    if cat and str(inv.get("category") or "") != str(cat):
        execute("UPDATE invoices SET category = ? WHERE id = ?", (str(cat), inv_id))
    summary = "进项发票 %s" % (inv.get("no") or "")
    if unit:
        summary += "（%s）" % unit["name"]
    memo = "由发票 %s 自动生成；金额＝价税合计（含税）" % (inv.get("no") or inv_id)

    if old:
        execute("UPDATE costs SET project_id=?, contract_id=?, unit_id=?, category=?, "
                "summary=?, cost_date=?, amount=?, pay_status=?, paid_amount=?, memo=? "
                "WHERE id=?",
                (inv.get("project_id"), inv.get("contract_id"), inv.get("unit_id"),
                 cat, summary, inv.get("issue_date"), amount, paid, paid_amt, memo,
                 old["id"]))
        cid, action = old["id"], "updated"
    else:
        cid = execute(
            "INSERT INTO costs (code, project_id, contract_id, unit_id, category, "
            "summary, cost_date, amount, pay_status, paid_amount, handler, memo, "
            "invoice_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (gen_code("costs"), inv.get("project_id"), inv.get("contract_id"),
             inv.get("unit_id"), cat, summary, inv.get("issue_date"), amount,
             paid, paid_amt, inv.get("handler") or "", memo, inv_id))
        action = "created"

    # 对应合同清单项
    cur = query("SELECT * FROM cost_items WHERE cost_id = ? ORDER BY id", (cid,))
    if item_id:
        it = one("SELECT * FROM contract_items WHERE id = ?", (item_id,))
        if it:
            if len(cur) == 1 and cur[0].get("contract_item_id") == item_id:
                pass                                    # 已经是它，不动
            else:
                execute("DELETE FROM cost_items WHERE cost_id = ?", (cid,))
                execute("INSERT INTO cost_items (cost_id, contract_item_id, name, "
                        "amount, memo) VALUES (?,?,?,?,?)",
                        (cid, item_id, it.get("name"), amount, ""))
    elif not cur:
        gid = guess_contract_item(inv.get("contract_id"), item_names)
        if gid:
            it = one("SELECT name FROM contract_items WHERE id = ?", (gid,))
            execute("INSERT INTO cost_items (cost_id, contract_item_id, name, amount, "
                    "memo) VALUES (?,?,?,?,?)",
                    (cid, gid, (it or {}).get("name") or "", amount,
                     "按票面品名自动对应"))
    row = one("SELECT * FROM costs WHERE id = ?", (cid,))
    if row:
        row["items"] = query("SELECT * FROM cost_items WHERE cost_id = ? ORDER BY id",
                             (cid,))
    return row, action


# --------------------------------------------------------------- 附件
def _safe_ext(filename):
    ext = os.path.splitext(filename or "")[1].lower().lstrip(".")
    return ext if ext in ALLOWED_EXT else None


def attachments_for(ref_type, ref_id):
    return query("SELECT * FROM attachments WHERE ref_type = ? AND ref_id = ? "
                 "ORDER BY id", (ref_type, ref_id))


def add_attachment(ref_type, ref_id, file_storage, uploader=""):
    if not file_storage or not file_storage.filename:
        raise ValueError("请选择要上传的文件")
    orig = os.path.basename(file_storage.filename)
    if not _safe_ext(orig):
        raise ValueError("不支持的文件类型，仅允许 PDF / Word / Excel / PPT / 图片等常见格式")
    data = file_storage.read()
    return add_attachment_bytes(ref_type, ref_id, orig, data, uploader,
                                file_storage.mimetype or "")


def add_attachment_bytes(ref_type, ref_id, orig_name, data, uploader="", mime=""):
    """直接写字节的归档入口（发票 PDF 导入用它，省一次落盘的搬运）。"""
    orig = os.path.basename(orig_name or "")
    ext = _safe_ext(orig)
    if not ext:
        raise ValueError("不支持的文件类型，仅允许 PDF / Word / Excel / PPT / 图片等常见格式")
    if len(data) > MAX_UPLOAD:
        raise ValueError(f"文件超过 {MAX_UPLOAD // 1024 // 1024} MB 限制")
    if not data:
        raise ValueError("文件内容为空")
    stored = uuid.uuid4().hex + "." + ext
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    (UPLOAD_DIR / stored).write_bytes(data)
    return execute(
        "INSERT INTO attachments (ref_type, ref_id, orig_name, stored_name, size, "
        "mime, uploaded_at, uploader) VALUES (?,?,?,?,?,?,?,?)",
        (ref_type, ref_id, orig, stored, len(data), mime or "", now_str(), uploader))


def get_attachment(aid):
    return one("SELECT * FROM attachments WHERE id = ?", (aid,))


def _remove_file(stored_name):
    if not stored_name:
        return
    p = UPLOAD_DIR / stored_name
    if p.exists():
        try:
            p.unlink()
        except OSError:
            pass


def delete_attachment(aid):
    a = get_attachment(aid)
    if a:
        _remove_file(a["stored_name"])
        execute("DELETE FROM attachments WHERE id = ?", (aid,))
    return a


def human_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def is_image(ext):
    return (ext or "").lower().lstrip(".") in IMAGE_EXT


# --------------------------------------------------------------- 查询与统计
# 台账页每页多少行。以前一次把几万行全渲染进 HTML：
# 50 年数据下「付款核销明细」有 40MB HTML / 97 万 DOM 节点，浏览器要 27.6 秒。
# 分页之后每页只渲染这么多行，打印 / 导出仍是全量。
PAGE_PER_DEFAULT = 200


def _query_spec(key):
    q = next((x for x in meta.QUERIES if x["key"] == key), None)
    if not q:
        raise KeyError(key)
    return q


def _filter_query_rows(q, rows, search):
    """按筛选词过滤。

    **显示值与原始值都算命中** —— 用户是照着屏幕上的数字搜的，
    而屏幕上金额带千分位（60,000.00）、比例带百分号，跟库里的原值不一样；
    只管原值的话会出现"明明看着有、却搜不到"。
    """
    if not search:
        return rows
    s = search.strip().lower()
    if not s:
        return rows
    out = []
    for r, f in zip(rows, _fmt_query_rows(q, rows)):
        if any(s in str(v).lower() for v in r.values()) or \
                any(s in str(v).lower() for v in f.values()):
            out.append(r)
    return out


def _fmt_query_rows(q, rows):
    """把原始行格式化成页面要显示的样子（金额千分位、百分比、收付状态）。"""
    fmt = []
    pct_cols = set(q.get("percent_cols") or [])
    state_from = q.get("state_from")
    for r in rows:
        out = {}
        for k, v in r.items():
            if k in pct_cols:
                out[k] = percent(v)
            elif v is None:
                out[k] = ""
            elif isinstance(v, float):
                out[k] = money(v)
            else:
                out[k] = v
        if state_from:
            settled_col, gross_col, label = state_from
            out[label] = pay_state(_num(r.get(settled_col)), _num(r.get(gross_col)))
        fmt.append(out)
    return fmt


def query_scope(key, uid, role):
    """这条台账对当前用户的数据范围。

    None  = 不限制（管理员 / 财务）
    False = 这条查询没法按项目切分 → 员工不给看
    (where, args) = 员工的项目范围条件（别名已在 QUERY_SCOPE 里声明）
    """
    if role != ROLE_EMPLOYEE or not uid:
        return None
    spec = (getattr(meta, "QUERY_SCOPE", None) or {}).get(key)
    if not spec:
        return False
    entity, alias = spec
    return scope_where(entity, uid, role, alias=alias)


def _inject_scope(sql, scope):
    """把范围条件插到 SQL 最外层的 WHERE 里（括号 / 引号感知，不碰子查询）。

    台账 SQL 里有大量相关子查询（各自带 WHERE），所以不能用「找第一个 WHERE」
    这种笨办法：必须按括号深度找到最外层，再插在 GROUP BY / ORDER BY / LIMIT 之前。
    """
    if not scope:
        return sql, ()
    frag, args = scope
    frag = (frag or "").strip()
    if not frag:
        return sql, ()
    depth, quote, i, cut = 0, None, 0, len(sql)
    top_where = -1
    while i < len(sql):
        ch = sql[i]
        if quote:
            if ch == quote:
                if quote == "'" and i + 1 < len(sql) and sql[i + 1] == "'":
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0:
            if ch.upper() == "W" and sql[i:i + 5].upper() == "WHERE" \
                    and (i == 0 or not sql[i - 1].isalnum()) \
                    and not sql[i + 5:i + 6].isalnum():
                top_where = i
            if cut == len(sql):
                for kw in ("GROUP BY", "ORDER BY", "LIMIT"):
                    if sql[i:i + len(kw)].upper() == kw \
                            and (i == 0 or not sql[i - 1].isalnum()) \
                            and not sql[i + len(kw):i + len(kw) + 1].isalnum():
                        cut = i
                        break
        i += 1
    joiner = " AND " if (0 <= top_where < cut) else " WHERE "
    return (sql[:cut].rstrip() + joiner + "(" + frag + ")\n" + sql[cut:].lstrip(),
            tuple(args))


def run_query(key, search="", scope=None):
    """全量（打印、导出用）。scope 见 query_scope()。"""
    q = _query_spec(key)
    sql, args = _inject_scope(q["sql"], scope)
    rows = _filter_query_rows(q, query(sql, args), search)
    return q, _fmt_query_rows(q, rows)


def clamp_per(v):
    try:
        n = int(v)
    except (TypeError, ValueError):
        n = PAGE_PER_DEFAULT
    return max(20, min(1000, n))


def run_query_page(key, search="", page=1, per=PAGE_PER_DEFAULT, scope=None):
    """台账页：只把当前这一页格式化后端出去（返回 q, rows, total, pages, page, per）。"""
    q = _query_spec(key)
    sql, args = _inject_scope(q["sql"], scope)
    rows = _filter_query_rows(q, query(sql, args), search)
    total = len(rows)
    per = clamp_per(per)
    pages = max(1, (total + per - 1) // per)
    try:
        page = int(page)
    except (TypeError, ValueError):
        page = 1
    page = max(1, min(pages, page))
    off = (page - 1) * per
    return q, _fmt_query_rows(q, rows[off:off + per]), total, pages, page, per


def _num(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def dashboard(project_ids=None, uid=None):
    """首页经营概览。

    project_ids=None 表示不限制（管理员 / 财务，看全公司）；
    传进来一组项目 id 时，**所有金额与计数都收敛到这些项目**，
    这样"卡片上的合计"与"按项目明细表的合计"严格相等，不会出现
    "卡片 100 万、表里明细加起来 30 万"这种对不上的情况。
    员工登录时传的就是他负责的 + 管理员授权的项目；
    `uid` 用于把「自己提交的费用」也算进来（与 scope_where 的口径一致）。
    """
    ids = None if project_ids is None else sorted({int(i) for i in project_ids})
    scoped = ids is not None
    ph = ",".join("?" * len(ids)) if ids else ""

    def sc(alias, restrict=True):
        """项目范围条件（空字符串表示不限制）。"""
        if not scoped or not restrict:
            return "", []
        if not ids:
            return "1 = 0", []
        return "%s.project_id IN (%s)" % (alias, ph), list(ids)

    def pj(alias):
        """直接按项目主键过滤（projects 表本身用）。"""
        if not scoped:
            return "", []
        if not ids:
            return "1 = 0", []
        return "%s.id IN (%s)" % (alias, ph), list(ids)

    def one(sql, cond, params, alias="t"):
        """SUM/COUNT 小工具：cond 为空就不加条件；
        语句里已经有 WHERE 时用 AND 接上去（发票未结清那句自带 WHERE）。"""
        if cond:
            sql += (" AND " + cond) if " WHERE " in sql.upper() else (" WHERE " + cond)
        return scalar(sql, tuple(params))

    d = {}
    d["projects"] = one("SELECT COUNT(*) FROM projects p", *pj("p"))
    d["contracts"] = one("SELECT COUNT(*) FROM contracts t", *sc("t"))
    d["invoices"] = one("SELECT COUNT(*) FROM invoices t", *sc("t"))
    if not scoped:
        d["units"] = scalar("SELECT COUNT(*) FROM units")
    else:
        # 员工只统计"自己项目上打过交道"的往来单位，不暴露全公司名册
        w = "WHERE u.id IN (SELECT c.unit_id FROM contracts c WHERE %s"             % sc("c")[0]
        if ids:
            w += " UNION SELECT v.unit_id FROM invoices v WHERE %s" % sc("v")[0]
            w += " UNION SELECT k.unit_id FROM costs k WHERE %s)" % sc("k")[0]
            d["units"] = scalar("SELECT COUNT(DISTINCT u.id) FROM units u " + w,
                                tuple(list(ids) * 3))
        else:
            d["units"] = 0

    d["contract_income"] = one(
        "SELECT IFNULL(SUM(t.amount_gross),0) FROM contracts t",
        "t.direction = '收入合同'" + (" AND " + sc("t")[0] if scoped else ""),
        list(ids or []))
    d["contract_cost"] = one(
        "SELECT IFNULL(SUM(t.amount_gross),0) FROM contracts t",
        "t.direction = '支出合同'" + (" AND " + sc("t")[0] if scoped else ""),
        list(ids or []))
    d["received"] = one(
        "SELECT IFNULL(SUM(t.amount),0) FROM payments t",
        "t.direction = '收入合同'" + (" AND " + sc("t")[0] if scoped else ""),
        list(ids or []))
    # 「支出合同」方向的付款记录（很多人不录这个，实际付款都记在费用支出里）
    d["pay_rec_out"] = round2(one(
        "SELECT IFNULL(SUM(t.amount),0) FROM payments t",
        "t.direction = '支出合同'" + (" AND " + sc("t")[0] if scoped else ""),
        list(ids or [])))

    # 费用：员工额外算上"自己提交的"（哪怕挂在别的项目上），与单据列表口径一致
    cost_cond, cost_params = sc("k")
    if scoped and uid:
        cost_cond = ("(%s OR k.employee_id = ?)" % cost_cond) if cost_cond else "k.employee_id = ?"
        cost_params = list(cost_params) + [uid]
    d["cost_total"] = round2(one("SELECT IFNULL(SUM(k.amount),0) FROM costs k",
                                 cost_cond, cost_params))
    # 费用里**实际已经付出去**的钱（按「已付金额」，部分支付只算已付的那部分）
    d["cost_paid"] = round2(one("SELECT IFNULL(SUM(%s),0) FROM costs k"
                                % cost_paid_expr("k"), cost_cond, cost_params))

    # 累计付款 = 付款记录（支出方向）+ 费用里已付出去的钱
    # （用户实际付的钱大多记在「费用支出」，只算付款记录会显示成 0）
    d["paid_out"] = round2(d["pay_rec_out"] + d["cost_paid"])

    # 发票指标**只算销项**（开给客户、形成收入的那一半票）。
    # 进项票是成本侧，另有费用台账管，混进来会让这两列既不是收入也不是支出。
    # 已作废的票在下面三处一律不参与（票都作废了，既不算开票额也不算未结清）。
    sc_sale = "t.direction = '开具销项发票'"
    d["invoice_sale"] = round2(one(
        "SELECT IFNULL(SUM(t.amount_gross),0) FROM invoices t",
        sc_sale + " AND IFNULL(t.status,'') <> '已作废'"
        + (" AND " + sc("t")[0] if scoped else ""),
        list(ids or [])))
    # 销项已开票付款金额 = 销项票里状态「已收讫」的价税合计（这些票的钱已收到）
    d["invoice_sale_paid"] = round2(one(
        "SELECT IFNULL(SUM(t.amount_gross),0) FROM invoices t",
        sc_sale + " AND IFNULL(t.status,'') = '已收讫'"
        + (" AND " + sc("t")[0] if scoped else ""),
        list(ids or [])))
    # 已核销：只算销项、不含作废票；人工标了「已收讫」但没有核销记录的票按全额算
    d["invoice_settled"] = round2(one(
        "SELECT IFNULL(SUM(CASE WHEN IFNULL(t.status,'') = '已收讫' "
        "THEN IFNULL(t.amount_gross,0) ELSE IFNULL(s.amount,0) END), 0) "
        "FROM invoices t LEFT JOIN settlements s ON s.invoice_id = t.id "
        "WHERE t.direction = '开具销项发票' "
        "AND IFNULL(t.status,'') <> '已作废'", *sc("t")))
    d["invoice_open"] = one(
        "SELECT COUNT(*) FROM invoices t "
        # 只算销项票：进项票是成本侧，不该混进「未结清发票」张数
        "WHERE t.direction = '开具销项发票' "
        # 已收讫＝钱付清了；已作废＝无效票，两者都不算未结清
        "AND IFNULL(t.status,'') NOT IN ('已收讫', '已作废') "
        "AND IFNULL("
        "(SELECT SUM(s.amount) FROM settlements s WHERE s.invoice_id = t.id), 0) "
        "< t.amount_gross - 0.01", *sc("t"))

    d["profit"] = round2(d["contract_income"] - d["contract_cost"] - d["cost_total"])
    d["profit_rate"] = (d["profit"] / d["contract_income"]) if d["contract_income"] else 0

    d["scoped"] = scoped
    d["recent_contracts"] = query(
        "SELECT c.code, c.name, c.amount_gross, c.status, p.name AS project_name "
        "FROM contracts c LEFT JOIN projects p ON c.project_id = p.id "
        + (("WHERE c.project_id IN (%s) " % ph) if (scoped and ids) else
           ("WHERE 1 = 0 " if scoped else "")) +
        "ORDER BY c.id DESC LIMIT 6", tuple(ids or []))

    # ------------------------------------------------------ 按项目汇总明细
    # 每个项目一行；项目上的合同/付款/发票/费用都归到该项目名下。
    # 没有指定项目的单据单独成一行，否则明细表的合计对不上卡片。
    #
    # 口径与原来那 9 个相关子查询**完全一致**，但改成「先按项目聚合、再 LEFT JOIN」：
    # 相关子查询的写法下，每个项目都要把 payments / costs / invoices 各扫一遍，
    # 项目一多就是 O(项目数 × 表行数)。实测 50 年数据 358ms → 168ms。
    PAID = cost_paid_expr("k")
    d["by_project"] = query("""
        SELECT p.id, p.code, p.name, p.status, p.budget,
          IFNULL(cc.n, 0)           AS contracts,
          IFNULL(cc.income, 0)      AS income,
          IFNULL(cc.cost, 0)        AS contract_cost,
          IFNULL(py.received, 0)    AS received,
          IFNULL(py.pay_rec_out, 0) AS pay_rec_out,
          IFNULL(ks.paid, 0)        AS cost_paid,
          IFNULL(ks.total, 0)       AS cost_total,
          IFNULL(iv.sale, 0)        AS invoice_sale,
          IFNULL(iv.sale_paid, 0)   AS invoice_sale_paid,
          IFNULL(iv.opened, 0)      AS invoice_open
        FROM projects p
        LEFT JOIN (SELECT project_id, COUNT(*) AS n,
                     SUM(CASE WHEN direction = '收入合同'
                              THEN amount_gross ELSE 0 END) AS income,
                     SUM(CASE WHEN direction = '支出合同'
                              THEN amount_gross ELSE 0 END) AS cost
                   FROM contracts WHERE project_id IS NOT NULL
                   GROUP BY project_id) cc ON cc.project_id = p.id
        LEFT JOIN (SELECT project_id,
                     SUM(CASE WHEN direction = '收入合同'
                              THEN amount ELSE 0 END) AS received,
                     SUM(CASE WHEN direction = '支出合同'
                              THEN amount ELSE 0 END) AS pay_rec_out
                   FROM payments WHERE project_id IS NOT NULL
                   GROUP BY project_id) py ON py.project_id = p.id
        LEFT JOIN (SELECT project_id, SUM({PAID}) AS paid, SUM(amount) AS total
                   FROM costs k WHERE k.project_id IS NOT NULL
                   GROUP BY k.project_id) ks ON ks.project_id = p.id
        LEFT JOIN (SELECT project_id,
                     SUM(CASE WHEN v.direction = '开具销项发票'
                               AND IFNULL(v.status,'') <> '已作废'
                              THEN v.amount_gross ELSE 0 END) AS sale,
                     SUM(CASE WHEN v.direction = '开具销项发票'
                               AND IFNULL(v.status,'') = '已收讫'
                              THEN v.amount_gross ELSE 0 END) AS sale_paid,
                     SUM(CASE WHEN IFNULL(v.status,'') <> '已收讫'
                              AND IFNULL(v.status,'') <> '已作废'
                              AND IFNULL((SELECT SUM(s.amount) FROM settlements s
                                          WHERE s.invoice_id = v.id), 0)
                                  < v.amount_gross - 0.01
                              THEN 1 ELSE 0 END) AS opened
                   FROM invoices v WHERE project_id IS NOT NULL
                   GROUP BY project_id) iv ON iv.project_id = p.id
        """.replace("{PAID}", PAID) + (("WHERE p.id IN (%s) " % ph) if (scoped and ids) else
               ("WHERE 1 = 0 " if scoped else "")) +
        "ORDER BY p.code", tuple(ids or []))

    for r in d["by_project"]:
        # 累计付款 = 付款记录（支出方向）+ 费用已付，与卡片同一个口径
        r["paid_out"] = round2(_num(r.get("pay_rec_out")) + _num(r.get("cost_paid")))
        # 收入口径：优先「收入合同额」；**没合同的项目**（日常经营这种）退到
        # 「已开的销项发票额」，再没有就退到「累计收款」——不然收进来的钱
        # 一分都不进毛利，会显示成 0（用户 2026-10-18 反馈）。
        r["income_eff"] = round2(_income_effective(r))
        r["income_fallback"] = bool(not _num(r.get("income")) and r["income_eff"] > 0)
        r["profit"] = round2(r["income_eff"] - r["contract_cost"] - r["cost_total"])
        r["profit_rate"] = (r["profit"] / r["income_eff"]) if r["income_eff"] else 0
        # 未开票合计 = 收入合同额 − 销项已开票合计（就用表里这两列相减，
        # 合计行因此永远等于「收入合计 − 销项合计」，能直接对账）。
        # 负数表示开票超过了合同额：多半是合同没录全、或发票挂错项目，不藏起来。
        r["uninvoiced"] = round2(_num(r.get("income")) - _num(r.get("invoice_sale")))
        r["unassigned"] = False

    # 未指定项目的单据（公司层面发生的合同/费用等）
    un = {"id": None, "code": "", "name": "（未指定项目）", "status": "",
          "budget": 0, "contracts": 0, "income": 0, "contract_cost": 0,
          "received": 0, "pay_rec_out": 0, "cost_paid": 0, "paid_out": 0,
          "cost_total": 0, "invoice_sale": 0, "invoice_sale_paid": 0,
          "invoice_open": 0, "income_eff": 0,
          "income_fallback": False, "unassigned": True}
    if not scoped:
        un.update({
            "contracts": scalar("SELECT COUNT(*) FROM contracts WHERE project_id IS NULL"),
            "income": round2(scalar("SELECT IFNULL(SUM(amount_gross),0) FROM contracts "
                                    "WHERE project_id IS NULL AND direction='收入合同'")),
            "contract_cost": round2(scalar("SELECT IFNULL(SUM(amount_gross),0) FROM contracts "
                                           "WHERE project_id IS NULL AND direction='支出合同'")),
            "received": round2(scalar("SELECT IFNULL(SUM(amount),0) FROM payments "
                                      "WHERE project_id IS NULL AND direction='收入合同'")),
            "pay_rec_out": round2(scalar("SELECT IFNULL(SUM(amount),0) FROM payments "
                                         "WHERE project_id IS NULL "
                                         "AND direction='支出合同'")),
            "cost_paid": round2(scalar("SELECT IFNULL(SUM(%s),0) FROM costs k "
                                       "WHERE k.project_id IS NULL" % PAID)),
            "cost_total": round2(scalar("SELECT IFNULL(SUM(amount),0) FROM costs "
                                        "WHERE project_id IS NULL")),
            "invoice_sale": round2(scalar(
                "SELECT IFNULL(SUM(amount_gross),0) FROM invoices "
                "WHERE project_id IS NULL AND direction = '开具销项发票' "
                "AND IFNULL(status,'') <> '已作废'")),
            "invoice_sale_paid": round2(scalar(
                "SELECT IFNULL(SUM(amount_gross),0) FROM invoices "
                "WHERE project_id IS NULL AND direction = '开具销项发票' "
                "AND IFNULL(status,'') = '已收讫'")),
            "invoice_open": scalar("SELECT COUNT(*) FROM invoices v "
                                   "WHERE v.project_id IS NULL "
                                   "AND IFNULL(v.status,'') NOT IN ('已收讫', '已作废') "
                                   "AND IFNULL((SELECT SUM(s.amount) FROM settlements s "
                                   "WHERE s.invoice_id = v.id), 0) < v.amount_gross - 0.01"),
        })
    elif uid:
        un["cost_total"] = round2(scalar(
            "SELECT IFNULL(SUM(amount),0) FROM costs "
            "WHERE project_id IS NULL AND employee_id = ?", (uid,)))
        un["cost_paid"] = round2(scalar(
            "SELECT IFNULL(SUM(%s),0) FROM costs k "
            "WHERE k.project_id IS NULL AND k.employee_id = ?" % PAID, (uid,)))
    un["paid_out"] = round2(un["pay_rec_out"] + un["cost_paid"])
    un["income_eff"] = round2(_income_effective(un))
    un["income_fallback"] = bool(not _num(un.get("income")) and un["income_eff"] > 0)
    un["profit"] = round2(un["income_eff"] - un["contract_cost"] - un["cost_total"])
    un["profit_rate"] = (un["profit"] / un["income_eff"]) if un["income_eff"] else 0
    un["uninvoiced"] = round2(_num(un.get("income")) - _num(un.get("invoice_sale")))
    if any(un[k] for k in ("contracts", "income", "contract_cost", "received",
                           "paid_out", "cost_total", "invoice_sale",
                           "invoice_sale_paid", "invoice_open")):
        d["by_project"].append(un)

    # 明细表合计（金额列逐列求和，毛利率用合计毛利 ÷ 合计收入）
    keys = ("budget", "income", "contract_cost", "received", "paid_out",
            "pay_rec_out", "cost_paid", "cost_total", "invoice_sale",
            "invoice_sale_paid", "uninvoiced")
    d["totals"] = {k: round2(sum(r[k] or 0 for r in d["by_project"])) for k in keys}
    d["totals"]["contracts"] = sum(r["contracts"] or 0 for r in d["by_project"])
    d["totals"]["invoice_open"] = sum(r["invoice_open"] or 0 for r in d["by_project"])
    d["totals"]["income_eff"] = round2(sum(r["income_eff"] or 0
                                           for r in d["by_project"]))
    # 毛利用各行合计（含没合同按票/收款补进来的收入），这样卡片与明细表严格相等
    d["totals"]["profit"] = round2(sum(r["profit"] or 0 for r in d["by_project"]))
    d["totals"]["profit_rate"] = ((d["totals"]["profit"] / d["totals"]["income_eff"])
                                  if d["totals"]["income_eff"] else 0)
    # 卡片用同一套数，避免两处算法漂移
    d["profit"] = d["totals"]["profit"]
    d["profit_rate"] = d["totals"]["profit_rate"]
    d["income_eff"] = d["totals"]["income_eff"]
    # 卡片与按项目明细表必须严格相等：员工「自己提交、但挂在别人项目上」的费用
    # 只在卡片口径里算过，这里统一收敛到明细表合计（否则两处对不上）。
    d["cost_total"] = d["totals"]["cost_total"]
    d["cost_paid"] = d["totals"]["cost_paid"]
    d["paid_out"] = d["totals"]["paid_out"]
    return d


def _income_effective(r):
    """一行（或未指定项目）的「算毛利用的收入」。

    有收入合同就用合同额；**没合同**的项目退到已开的销项发票额，
    再没有才退到累计收款 —— 只在这两种情况下兜底，
    有合同的项目一分钱都不会因为发票/收款而变（不会重复计）。
    """
    income = _num(r.get("income"))
    if income:
        return income
    sale = _num(r.get("invoice_sale"))
    if sale:
        return sale
    return _num(r.get("received"))


# =============================================================== 账号与权限

ROLE_ADMIN = "admin"
ROLE_FINANCE = "finance"
ROLE_EMPLOYEE = "employee"
ROLE_LABELS = {ROLE_ADMIN: "管理员", ROLE_FINANCE: "财务", ROLE_EMPLOYEE: "员工"}
ROLE_DESC = {
    ROLE_ADMIN: "全部功能：增删改查、用户管理、字典与费用类别维护、参数设置。"
                "自己录入的费用无需审批，但可查看全员费用与报销情况",
    ROLE_FINANCE: "除「用户管理」和「参数设置」外基本等同于管理员：可增删改查、"
                  "维护字典与费用类别、审批与打款费用报销、打款预支、"
                  "审批加班/请假/出差、查看全部台账与工资测算",
    ROLE_EMPLOYEE: "只保留「自己经手」的功能：提交与查看自己的费用报销、预支、"
                   "加班/请假/出差，以及自己负责项目下的合同/付款/费用/发票；"
                   "工资管理、查询统计、系统管理不对员工开放；"
                   "所有列表都只显示自己的数据，入口在页面上隐藏，"
                   "直接敲地址访问也会被拒绝",
}
ROLES = [ROLE_ADMIN, ROLE_FINANCE, ROLE_EMPLOYEE]
# 兼容旧引用：老的「录入查询」现在就是「员工」
ROLE_EDITOR = ROLE_EMPLOYEE
# 可执行审批/打款/工资测算、以及进入管理类模块的角色
ROLE_APPROVERS = (ROLE_ADMIN, ROLE_FINANCE)

# 登录防爆破：同一账号或同一 IP 在窗口期内失败达到上限即锁定
MAX_FAILS = int(os.environ.get("LOGIN_MAX_FAILS", "5"))
FAIL_WINDOW_MIN = int(os.environ.get("LOGIN_FAIL_WINDOW_MIN", "15"))
LOCK_MIN = int(os.environ.get("LOGIN_LOCK_MIN", "15"))
PWD_MIN_LEN = int(os.environ.get("PWD_MIN_LEN", "8"))
LOG_KEEP_DAYS = int(os.environ.get("LOGIN_LOG_KEEP_DAYS", "30"))


def password_problem(pwd):
    """返回密码不合规的原因，合规返回 None。"""
    pwd = pwd or ""
    if len(pwd) < PWD_MIN_LEN:
        return f"密码至少 {PWD_MIN_LEN} 位"
    kinds = sum(bool(re.search(p, pwd))
                for p in (r"[a-z]", r"[A-Z]", r"\d", r"[^A-Za-z0-9]"))
    if kinds < 2:
        return "密码需包含小写字母、大写字母、数字、符号中的至少两类"
    if pwd.lower() in {"admin123", "12345678", "password", "88888888"}:
        return "这个密码太常见了，请换一个"
    return None


def user_list():
    return query("""
        SELECT id, username, display_name, role, enabled, must_change_pwd,
               created_at, updated_at, last_login_at, last_login_ip
        FROM users ORDER BY role, username""")


def user_get(uid):
    return one("SELECT * FROM users WHERE id = ?", (uid,))


def user_by_name(username):
    return one("SELECT * FROM users WHERE username = ?",
               ((username or "").strip(),))


def user_create(username, password, display_name, role, must_change=1):
    username = (username or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.@-]{3,32}", username):
        raise ValueError("用户名需 3–32 位，只能用字母、数字、下划线、点、@、连字符")
    if user_by_name(username):
        raise ValueError(f"用户名 {username} 已存在")
    err = password_problem(password)
    if err:
        raise ValueError(err)
    if role not in ROLES:
        raise ValueError("角色不合法")
    from werkzeug.security import generate_password_hash
    now = now_str()
    return execute(
        "INSERT INTO users (username, password_hash, display_name, role, enabled, "
        "must_change_pwd, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (username, generate_password_hash(password), display_name or username,
         role, 1, 1 if must_change else 0, now, now))


def user_update(uid, display_name, role, enabled):
    row = user_get(uid)
    if not row:
        raise ValueError("账号不存在")
    if role not in ROLES:
        raise ValueError("角色不合法")
    if row["role"] == ROLE_ADMIN and (role != ROLE_ADMIN or not enabled):
        if admin_count() <= 1:
            raise ValueError("至少要保留一个启用状态的管理员账号")
    execute("UPDATE users SET display_name=?, role=?, enabled=?, updated_at=? "
            "WHERE id=?", (display_name or row["username"], role,
                           1 if enabled else 0, now_str(), uid))


def user_profile_save(uid, salary=0, emp_no="", hire_date=""):
    """保存员工的工资与工号等资料（工资用于补贴测算）。"""
    execute("UPDATE users SET monthly_salary=?, emp_no=?, hire_date=?, updated_at=? "
            "WHERE id=?", (round2(salary or 0), emp_no or None,
                           hire_date or None, now_str(), uid))


def user_check_password(uid, password):
    """校验当前账号密码（解除工资定稿等敏感操作用）。"""
    from werkzeug.security import check_password_hash
    row = one("SELECT password_hash FROM users WHERE id = ?", (uid,))
    return bool(row and password and
                check_password_hash(row["password_hash"] or "", password))


def user_set_password(uid, password, must_change=0):
    row = user_get(uid)
    if not row:
        raise ValueError("账号不存在")
    err = password_problem(password)
    if err:
        raise ValueError(err)
    from werkzeug.security import generate_password_hash
    execute("UPDATE users SET password_hash=?, must_change_pwd=?, updated_at=? "
            "WHERE id=?", (generate_password_hash(password),
                           1 if must_change else 0, now_str(), uid))


def user_refs(uid):
    """还有哪些业务表挂在这个账号名下。

    用 PRAGMA 反查外键，新增表时不用再手工维护清单。
    ON DELETE CASCADE 的表（项目授权）不算"占用"，会随账号一起清掉。
    """
    out = []
    tnames = [r["name"] for r in query(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%'")]
    for t in tnames:
        for fk in query(f"PRAGMA foreign_key_list({t})"):
            if fk["table"] != "users" or (fk["on_delete"] or "").upper() == "CASCADE":
                continue
            col = fk["from"]
            n = scalar(f"SELECT COUNT(*) FROM {t} WHERE {col} = ?", (uid,))
            if n:
                out.append((t, col, n))
    return out


TABLE_LABELS = {"staff": "员工花名册", "costs": "费用报销", "advances": "预支单",
                "overtime": "加班单", "leaves": "请假单", "trips": "出差单",
                "projects": "项目", "contracts": "合同"}


def user_delete(uid, operator_id=None):
    row = user_get(uid)
    if not row:
        raise ValueError("账号不存在")
    if operator_id and int(operator_id) == int(uid):
        raise ValueError("不能删除当前登录的账号")
    if row["role"] == ROLE_ADMIN and admin_count() <= 1:
        raise ValueError("至少要保留一个管理员账号")
    # 名下有业务数据的账号不能删 —— 历史单据会变成查不到人的孤儿数据。
    # 这种情况正确做法是「停用」：账号登录不了，历史记录照样留着。
    # 项目授权 / 页面权限是附属配置（有的老库没建 CASCADE），不算业务数据
    refs = [r for r in user_refs(uid)
            if r[0] not in ("project_access", "user_pages")]
    if refs:
        detail = "、".join("%s %d 条" % (TABLE_LABELS.get(t, t), n) for t, _c, n in refs)
        raise ValueError("该账号名下还有 %s，不能删除；"
                         "请改用「停用」，这样历史记录仍然保留" % detail)
    execute("DELETE FROM project_access WHERE user_id = ?", (uid,))
    execute("DELETE FROM users WHERE id = ?", (uid,))


def admin_count():
    return scalar("SELECT COUNT(*) FROM users WHERE role = ? AND enabled = 1",
                  (ROLE_ADMIN,))


def enabled_admin_exists():
    return admin_count() > 0


# --------------------------------------------------------------- 登录与防爆破
def record_login(username, ip, ok, reason=""):
    execute("INSERT INTO login_logs (username, ip, ok, reason, at) VALUES (?,?,?,?,?)",
            ((username or "")[:64], (ip or "")[:64], 1 if ok else 0, reason, now_str()))


def lock_state(username, ip):
    """返回 (是否锁定, 剩余秒数, 锁定原因)"""
    since = (datetime.now() - timedelta(minutes=FAIL_WINDOW_MIN)
             ).strftime("%Y-%m-%d %H:%M:%S")
    n_user = scalar("SELECT COUNT(*) FROM login_logs WHERE ok=0 AND username=? AND at>=?",
                    ((username or ""), since))
    n_ip = scalar("SELECT COUNT(*) FROM login_logs WHERE ok=0 AND ip=? AND at>=?",
                  ((ip or ""), since))
    if n_user < MAX_FAILS and n_ip < MAX_FAILS:
        return False, 0, ""
    last = one("SELECT at FROM login_logs WHERE ok=0 AND (username=? OR ip=?) "
               "ORDER BY id DESC LIMIT 1", ((username or ""), (ip or "")))
    if not last:
        return False, 0, ""
    try:
        last_dt = datetime.strptime(last["at"], "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return False, 0, ""
    left = int((last_dt + timedelta(minutes=LOCK_MIN) - datetime.now()).total_seconds())
    if left <= 0:
        return False, 0, ""
    return True, left, ("账号" if n_user >= MAX_FAILS else "IP")


def login_attempt(username, password, ip):
    """返回 (用户行 或 None, 错误信息 或 None)。"""
    from werkzeug.security import check_password_hash
    username = (username or "").strip()
    password = password or ""
    if not username or not password:
        return None, "请输入用户名和密码"

    locked, left, why = lock_state(username, ip)
    if locked:
        record_login(username, ip, 0, "锁定中")
        mins = max(1, (left + 59) // 60)
        return None, f"登录失败次数过多（{why}触发保护），请在 {mins} 分钟后重试"

    row = user_by_name(username)
    if not row or not check_password_hash(row["password_hash"], password):
        record_login(username, ip, 0, "用户名或密码错误")
        left_try = MAX_FAILS - 1 - scalar(
            "SELECT COUNT(*) FROM login_logs WHERE ok=0 AND ip=? AND at>=?",
            (ip, (datetime.now() - timedelta(minutes=FAIL_WINDOW_MIN)
                  ).strftime("%Y-%m-%d %H:%M:%S")))
        tip = f"，还可尝试 {left_try} 次" if 0 < left_try <= 3 else ""
        return None, "用户名或密码不正确" + tip

    if not row["enabled"]:
        record_login(username, ip, 0, "账号已停用")
        return None, "该账号已被停用，请联系管理员"

    record_login(username, ip, 1, "")
    execute("UPDATE users SET last_login_at=?, last_login_ip=? WHERE id=?",
            (now_str(), ip, row["id"]))
    prune_login_logs()
    return row, None


# 是否信任反向代理传来的 X-Forwarded-For
TRUST_PROXY = os.environ.get("TRUST_PROXY", "1") == "1"
# 需要从别的头取真实 IP 时才设置，例如 Cloudflare 的 CF-Connecting-IP
IP_HEADER = (os.environ.get("IP_HEADER", "") or "").strip()
# 可选：只允许这些网段访问（逗号分隔，支持单个 IP），留空表示不限制
ALLOW_IP_CIDRS = [c.strip() for c in
                  os.environ.get("ALLOW_IP_CIDRS", "").replace(";", ",").split(",")
                  if c.strip()]


def _is_private_ip(ip):
    try:
        return ipaddress.ip_address(ip).is_private
    except ValueError:
        return False


def client_ip(request):
    """取真实来源 IP（用于登录审计与按 IP 的防爆破）。

    两条安全要点，都是实打实踩过的坑：

    1. **只在直连方是内网地址时才采信代理头**。否则攻击者随手伪造一个
       X-Forwarded-For 就能给每个请求换一个假 IP，把按 IP 的锁定绕干净。
       直连方是公网地址（例如端口直接映射到公网）时，一律用 remote_addr。
    2. **取 X-Forwarded-For 的最后一段，而不是第一段**。反代（nginx 的
       `$proxy_add_x_forwarded_for`、Caddy 默认行为）是把自己看到的对端地址**追加**到末尾；
       而第一段是客户端自己可以随便写的。取第一段 = 取攻击者伪造的值。
       （取最后一段同时兼容"反代用 $remote_addr 覆盖"的写法，此时只有一个值。）

    3. 不再采信 X-Real-IP：它不是标准头，没有反代时任何客户端都能伪造。
       确有大 CDN 场景（如 Cloudflare）用 IP_HEADER=CF-Connecting-IP 显式指定。
    """
    peer = request.remote_addr or ""
    if not (TRUST_PROXY and (not peer or _is_private_ip(peer))):
        return peer

    if IP_HEADER:
        val = (request.headers.get(IP_HEADER) or "").strip()
        if val:
            return val

    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        parts = [p.strip() for p in fwd.split(",") if p.strip()]
        if parts:
            return parts[-1]
    return peer


def parse_allow_list(text):
    return [c.strip() for c in (text or "").replace(";", ",").split(",") if c.strip()]


def ip_allowed(ip, cidrs=None):
    """IP 白名单校验；本机（127.0.0.1/::1）永远放行，保证容器健康检查可用。"""
    cidrs = ALLOW_IP_CIDRS if cidrs is None else cidrs
    if not cidrs:
        return True
    try:
        addr = ipaddress.ip_address(ip or "")
    except ValueError:
        return False
    if addr.is_loopback:
        return True
    for c in cidrs:
        try:
            if addr in ipaddress.ip_network(c, strict=False):
                return True
        except ValueError:
            if ip == c:
                return True
    return False


def login_logs(limit=200, only_fail=False):
    sql = ("SELECT id, username, ip, ok, reason, at FROM login_logs "
           + ("WHERE ok = 0 " if only_fail else "")
           + "ORDER BY id DESC LIMIT ?")
    return query(sql, (int(limit),))


def prune_login_logs():
    keep_days = max(1, LOG_KEEP_DAYS)
    cut = (datetime.now() - timedelta(days=keep_days)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        execute("DELETE FROM login_logs WHERE at < ?", (cut,))
    except sqlite3.Error:
        pass


# =====================================================================
#                  费用报销 / 出勤管理 / 工资测算
# =====================================================================

# ------------------------------------------------------------ 参数设置
# 新增参数（老库里没有这些 key 时按这里显示默认值，保存在「参数设置」里覆盖）
DEFAULT_SETTINGS = {
    # 2026 年法定节假日（国务院办公厅 国办发明电〔2025〕7 号）：
    # 元旦 1/1-1/3；春节 2/15-2/23；清明 4/4-4/6；劳动节 5/1-5/5；
    # 端午 6/19-6/21；中秋 9/25-9/27；国庆 10/1-10/7。
    "statutory_holidays": (
        "2026-01-01,2026-01-02,2026-01-03,"
        "2026-02-15,2026-02-16,2026-02-17,2026-02-18,2026-02-19,"
        "2026-02-20,2026-02-21,2026-02-22,2026-02-23,"
        "2026-04-04,2026-04-05,2026-04-06,"
        "2026-05-01,2026-05-02,2026-05-03,2026-05-04,2026-05-05,"
        "2026-06-19,2026-06-20,2026-06-21,"
        "2026-09-25,2026-09-26,2026-09-27,"
        "2026-10-01,2026-10-02,2026-10-03,2026-10-04,"
        "2026-10-05,2026-10-06,2026-10-07",
        "法定节假日（逗号分隔 YYYY-MM-DD）；这些天从「周一至周六」里扣掉，不算计薪天数"),
    "makeup_workdays": (
        "2026-01-04,2026-02-14,2026-02-28,2026-05-09,2026-09-20,2026-10-10",
        "调休上班日（逗号分隔）；落在周日的调休也算一个计薪日"),
    "heat_months": ("6,7,8", "发高温补贴的月份（逗号分隔，1-12）"),
    "heat_allow_per_month": ("300", "高温补贴：每人每月金额"),
    "welfare_months": ("2026-06,2026-09",
                       "发福利金的月份（YYYY-MM，逗号分隔）——只填端午 / 中秋所在月"),
    "welfare_amount": ("100", "福利金：端午 / 中秋月每人自动带出的金额"),
    "trip_pay_mode": ("fixed",
                      "出差费口径：fixed = 按「出差补贴/天」固定金额（默认 40）；"
                      "daily = 日工资 × 出差天数"),
}


# 参数的中文名（页面上显示用；没列到的就显示 key 本身）
SETTING_LABELS = {
    "monthly_days": "月计薪天数（兜底，正常按当月工作日算）",
    "hours_per_day": "每日工时（小时）",
    "ot_rate_weekday": "加班倍数（周一至周六）",
    "ot_rate_weekend": "加班倍数（周日）",
    "ot_rate_holiday": "加班倍数（法定节假日）",
    "overtime_rate": "加班倍数（旧参数，兼容用）",
    "leave_deduct_rate": "请假扣款倍数",
    "trip_allowance_per_day": "出差补贴/天（trip_pay_mode=fixed 时用）",
    "meal_office_per_day": "餐补：平日上班/天",
    "meal_trip_per_day": "餐补：出差/出海/天",
    "pension_rate": "养老个人比例",
    "medical_rate": "医疗个人比例",
    "unemploy_rate": "失业个人比例",
    "serious_ill_fixed": "大病补充（定额/月）",
    "tax_threshold": "个税起征点（月）",
    "salary_default": "默认基础工资",
    "work_days_per_week": "每周上班天数（仅参考）",
    "heat_months": "高温补贴月份",
    "heat_allow_per_month": "高温补贴/人/月",
    "welfare_months": "福利金月份（端午/中秋）",
    "welfare_amount": "福利金金额（每人）",
    "trip_pay_mode": "出差费口径（daily / fixed）",
    "statutory_holidays": "法定节假日日期",
    "makeup_workdays": "调休上班日",
    "company_tax_no": "公司税号",
    "private_tax_rate": "对私税率（%）",
}


def settings_all():
    rows = query("SELECT key, value, memo, updated_at FROM settings ORDER BY key")
    have = {r["key"] for r in rows}
    for k, (v, memo) in DEFAULT_SETTINGS.items():
        if k not in have:
            rows.append({"key": k, "value": v, "memo": memo, "updated_at": ""})
    for r in rows:
        r["label"] = SETTING_LABELS.get(r["key"], r["key"])
    rows.sort(key=lambda r: r["key"])
    return rows


def setting(key, default=""):
    row = one("SELECT value FROM settings WHERE key = ?", (key,))
    if row is None or row["value"] is None or row["value"] == "":
        return default
    return row["value"]


def setting_num(key, default=0.0):
    try:
        return float(setting(key, default))
    except (TypeError, ValueError):
        return float(default)


def settings_save(pairs):
    now = now_str()
    for k, v in pairs.items():
        if one("SELECT key FROM settings WHERE key = ?", (k,)):
            execute("UPDATE settings SET value = ?, updated_at = ? WHERE key = ?",
                    (str(v), now, k))
        else:
            execute("INSERT INTO settings (key, value, memo, updated_at) "
                    "VALUES (?,?,?,?)", (k, str(v), "", now))


def payroll_months():
    """工资表下拉里能选的月份 = **真正做过工资表的月份**（倒序）。

    以前用的是「最近 14 个月」，所以 1~8 月这些空月份也挂在上面，
    点进去只有一张自动测算的空表，容易以为系统里有数据。
    一个月都没做过时退回当前月，至少能进页面。
    """
    ms = [r["ym"] for r in query(
        "SELECT DISTINCT ym FROM payroll ORDER BY ym DESC")]
    return ms or [date.today().strftime("%Y-%m")]


def payroll_default_month():
    """打开工资页默认落在哪个月：在做的最后一个月（没有就用当前月）。"""
    return payroll_months()[0]


def payroll_params():
    """工资测算用到的全部参数（都能在「参数设置」里改）。

    计薪口径按人社部《关于职工全年月平均工作时间和工资折算问题的通知》：
    月计薪天数 21.75 天，日工资 = 月工资 ÷ 21.75。
    """
    return {
        "monthly_days": setting_num("monthly_days", 21.75),
        "hours_per_day": setting_num("hours_per_day", 8),
        "ot_rate_weekday": setting_num("ot_rate_weekday", 1.5),
        "ot_rate_weekend": setting_num("ot_rate_weekend", 2.0),
        "ot_rate_holiday": setting_num("ot_rate_holiday", 3.0),
        "leave_deduct_rate": setting_num("leave_deduct_rate", 1.0),
        "trip_allowance_per_day": setting_num("trip_allowance_per_day", 40),
        "meal_office_per_day": setting_num("meal_office_per_day", 15),
        "meal_trip_per_day": setting_num("meal_trip_per_day", 30),
        "pension_rate": setting_num("pension_rate", 0.08),
        "medical_rate": setting_num("medical_rate", 0.02),
        "unemploy_rate": setting_num("unemploy_rate", 0.005),
        "serious_ill_fixed": setting_num("serious_ill_fixed", 10),
        "tax_threshold": setting_num("tax_threshold", 5000),
        "salary_default": setting_num("salary_default", 4500),
        "work_days_per_week": setting_num("work_days_per_week", 6),
        # 2026-11-11 新增：计薪天数按当月实际工作日算，不再用固定 21.75
        "heat_months": {int(x) for x in re.split(r"[,，\s]+",
                                                 setting("heat_months", "6,7,8"))
                        if x.strip().isdigit()},
        "heat_allow_per_month": setting_num("heat_allow_per_month", 300),
        "welfare_months": {x.strip() for x in
                           re.split(r"[,，\s]+",
                                    setting("welfare_months",
                                            DEFAULT_SETTINGS["welfare_months"][0]))
                           if x.strip()},
        "welfare_amount": setting_num("welfare_amount", 100),
        "trip_pay_mode": (setting("trip_pay_mode", "fixed") or "fixed").strip(),
    }


def _ymd_set(txt):
    """把设置里的日期串解析成集合（支持逗号 / 中文逗号 / 顿号 / 换行）。"""
    out = set()
    for x in re.split(r"[,，、;；\s]+", txt or ""):
        x = x.strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", x):
            out.add(x)
    return out


def payroll_payable_days(ym):
    """当月计薪天数 = 周一至周六的天数 − 法定假日 + 调休上班日。

    · 不包含加班（加班按小时另算）；
    · 周日默认不算；但「调休上班日」里点名的周日算一个计薪日；
    · 节假日清单在「参数设置」里维护，跨年时把新一年的日期贴进去即可。
    """
    y, m = int(ym[:4]), int(ym[5:7])
    days = month_day_count(ym)
    holi = _ymd_set(setting("statutory_holidays",
                            DEFAULT_SETTINGS["statutory_holidays"][0]))
    make = _ymd_set(setting("makeup_workdays",
                            DEFAULT_SETTINGS["makeup_workdays"][0]))
    n = 0
    for d in range(1, days + 1):
        dt = date(y, m, d)
        s = dt.isoformat()
        is_sun = dt.weekday() == 6
        if is_sun and s not in make:
            continue                 # 周日不算法定工作日（调休上班的除外）
        if s in holi and s not in make:
            continue                 # 法定假日扣掉
        n += 1
    return n


# ------------------------------------------------------------ 项目数据范围
def accessible_project_ids(uid):
    """员工可访问的项目：他负责的 + 管理员指定的。"""
    rows = query("SELECT id FROM projects WHERE manager_user_id = ? "
                 "UNION SELECT project_id FROM project_access WHERE user_id = ?",
                 (uid, uid))
    return {r["id"] for r in rows}


def scope_where(entity_key, uid, role, alias="t"):
    """返回 (where 片段, 参数)。None 表示不限制（管理员/财务）。"""
    if role != ROLE_EMPLOYEE or not uid:
        return None
    sub = ("(SELECT id FROM projects WHERE manager_user_id = ? "
           "UNION SELECT project_id FROM project_access WHERE user_id = ?)")
    if entity_key == "projects":
        return (f"{alias}.id IN {sub}", [uid, uid])
    if entity_key in ("contracts", "payments", "invoices", "costs"):
        cond = f"IFNULL({alias}.project_id, -1) IN {sub}"
        if entity_key == "costs":
            # 费用：自己项目的 + 自己报销的都要能看到
            return f"({cond} OR {alias}.employee_id = ?)", [uid, uid, uid]
        return cond, [uid, uid]
    if entity_key in ("advances", "overtime", "leaves", "trips"):
        return (f"{alias}.employee_id = ?", [uid])
    if entity_key == "quotations":
        # 报价单：员工只看自己经手的
        return (f"{alias}.handler_user_id = ?", [uid])
    if entity_key == "deliveries":
        return (f"IFNULL({alias}.project_id, -1) IN {sub}", [uid, uid])
    return None


def can_touch_row(entity_key, row, uid, role):
    """员工能否查看/编辑这条记录。"""
    if role != ROLE_EMPLOYEE:
        return True
    if not row:
        return False
    if entity_key == "projects":
        return row.get("id") in accessible_project_ids(uid)
    if entity_key in ("contracts", "payments", "invoices", "costs"):
        if entity_key == "costs" and row.get("employee_id") == uid:
            return True                      # 自己提交的报销，自己要看得到
        pid = row.get("project_id")
        return bool(pid) and pid in accessible_project_ids(uid)
    if entity_key in ("advances", "overtime", "leaves", "trips"):
        return row.get("employee_id") == uid
    if entity_key == "quotations":
        return row.get("handler_user_id") == uid
    if entity_key == "deliveries":
        pid = row.get("project_id")
        return bool(pid) and pid in accessible_project_ids(uid)
    return True


def project_access_list(uid):
    return query(
        "SELECT pa.project_id, p.code, p.name FROM project_access pa "
        "LEFT JOIN projects p ON pa.project_id = p.id "
        "WHERE pa.user_id = ? ORDER BY p.code", (uid,))


def project_access_set(uid, ids):
    execute("DELETE FROM project_access WHERE user_id = ?", (uid,))
    for pid in ids:
        try:
            execute("INSERT OR IGNORE INTO project_access (user_id, project_id, memo) "
                    "VALUES (?,?,?)", (uid, int(pid), "管理员指定"))
        except (TypeError, ValueError):
            continue


def project_options():
    return query("SELECT id, code, name FROM projects ORDER BY code")


# ------------------------------------------------------- 页面权限（按账号覆盖）
# 覆盖级别：0＝强制隐藏；1＝只读开放（能看不能改）；2＝可编辑开放。没有行＝跟随角色。
PAGE_LVL_HIDE, PAGE_LVL_READ, PAGE_LVL_WRITE = 0, 1, 2


def user_page_overrides(uid):
    """某账号的页面权限覆盖：{(kind, key): 0/1/2}。

    没有行＝完全跟随角色默认；0＝强制隐藏，1＝只读开放，2＝可编辑开放。
    """
    if not uid:
        return {}
    rows = query("SELECT kind, item_key, allow FROM user_pages WHERE user_id = ?",
                 (int(uid),))
    return {(r["kind"], r["item_key"]): int(r["allow"] or 0) for r in rows}


def user_page_overrides_set(uid, choices):
    """整份保存某账号的页面覆盖。

    choices: [(kind, key, None|0|1|2)]，None 表示恢复「跟随角色」。
    只存改过的项，没改的会被删掉 —— 角色以后变了，默认值也跟着变。
    """
    uid = int(uid)
    execute("DELETE FROM user_pages WHERE user_id = ?", (uid,))
    rows = [(uid, kind, key, int(lvl))
            for kind, key, lvl in choices if lvl is not None]
    if rows:
        execute_many("INSERT OR REPLACE INTO user_pages "
                     "(user_id, kind, item_key, allow) VALUES (?,?,?,?)", rows)


def advance_rows_of(uid, limit=50):
    """某人收到的预支款及其核销余额。"""
    rows = query(
        "SELECT a.*, IFNULL((SELECT SUM(c.amount) FROM costs c "
        "  WHERE c.advance_id = a.id "
        "  AND IFNULL(c.approval_status,'') IN ('已批准','已打款')), 0) AS used "
        "FROM advances a WHERE a.employee_id = ? "
        "ORDER BY a.pay_date DESC, a.id DESC LIMIT ?", (uid, limit))
    for r in rows:
        amt = round2(r.get("amount"))
        r["balance"] = round2(amt - round2(r.get("used")))
    return rows


# ------------------------------------------------------------ 出勤自动计算
def _hm_minutes(s):
    m = re.match(r"^\s*(\d{1,2})\s*[:：]\s*(\d{1,2})\s*$", str(s or ""))
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def auto_overtime_hours(start, end):
    """按起止时间算加班时长（小时），支持跨夜。"""
    a, b = _hm_minutes(start), _hm_minutes(end)
    if a is None or b is None:
        return None
    if b < a:
        b += 24 * 60
    return round((b - a) / 60.0, 2)


def day_count(start, end):
    """含首尾的自然日天数；end 为空返回 None（表示未定）。"""
    if not start or not end:
        return None
    try:
        d1 = date.fromisoformat(str(start)[:10])
        d2 = date.fromisoformat(str(end)[:10])
    except (TypeError, ValueError):
        return None
    return float(max(0, (d2 - d1).days + 1))


def apply_attendance_calc(entity_key, data):
    """保存前按规则补齐派生字段（不改用户手填的值）。"""
    if entity_key == "overtime":
        if not data.get("hours"):
            h = auto_overtime_hours(data.get("start_time"), data.get("end_time"))
            if h:
                data["hours"] = h
    elif entity_key == "leaves":
        n = day_count(data.get("start_date"), data.get("end_date"))
        if not data.get("days") and n is not None:
            data["days"] = n
        if not data.get("hours") and data.get("days"):
            data["hours"] = round2(float(data["days"]) * payroll_params()["hours_per_day"])
    elif entity_key == "trips":
        # 补贴对填单人不可见，每次保存都按「天数 × 每日标准」重算，
        # 免得改了日期或改了参数后留下过期数字。
        # 标准取单据上已有的值（隐藏字段会从原记录回填），新建时取参数默认值。
        std = data.get("allowance_std") or setting_num("trip_allowance_per_day", 40)
        data["allowance_std"] = std
        n = day_count(data.get("start_date"), data.get("end_date"))
        if n is None:
            n = 0.0          # 回程未定：先记 0 天，回程后补填日期即自动重算
        if not data.get("days"):
            data["days"] = n
        data["allowance"] = round2(float(data.get("days") or 0) * std)
        # 回程未定 → 状态提示为「未定」，但仍按用户选的审批状态走
    return data


# ------------------------------------------------------------ 报销汇总
def advance_used(advance_id):
    return round2(scalar(
        "SELECT IFNULL(SUM(amount),0) FROM costs WHERE advance_id = ? "
        "AND IFNULL(approval_status,'') IN ('已批准','已打款')", (advance_id,)))


def advance_balance(advance_id):
    amt = round2(scalar("SELECT IFNULL(amount,0) FROM advances WHERE id = ?",
                        (advance_id,)))
    return round2(amt - advance_used(advance_id))


# 「我的费用报销」只认人报上来的费用。发票（进项）保存时会自动派生一条成本费用
# （costs.invoice_id 非空），那不是谁垫的钱、也不该算"待打款给某个人"，
# 所以这里一律排除；它们的金额仍在「费用支出」与项目成本 / 概览里体现。
_NOT_INVOICE_COST = "AND invoice_id IS NULL "


def reimb_summary(uid):
    """某人的费用报销概况：预支、已核销、待审批、已批准未打款、已打款。

    只统计本人提交的报销单，不含发票自动派生的费用（见 _NOT_INVOICE_COST）。
    """
    adv_total = round2(scalar(
        "SELECT IFNULL(SUM(amount),0) FROM advances WHERE employee_id = ?", (uid,)))
    used = round2(scalar(
        "SELECT IFNULL(SUM(amount),0) FROM costs WHERE employee_id = ? "
        + _NOT_INVOICE_COST +
        "AND advance_id IS NOT NULL "
        "AND IFNULL(approval_status,'') IN ('已批准','已打款')", (uid,)))
    pend = round2(scalar(
        "SELECT IFNULL(SUM(amount),0) FROM costs WHERE employee_id = ? "
        + _NOT_INVOICE_COST +
        "AND IFNULL(approval_status,'') = '待审批'", (uid,)))
    aprv = round2(scalar(
        "SELECT IFNULL(SUM(amount),0) FROM costs WHERE employee_id = ? "
        + _NOT_INVOICE_COST +
        "AND IFNULL(approval_status,'') = '已批准'", (uid,)))
    paid = round2(scalar(
        "SELECT IFNULL(SUM(amount),0) FROM costs WHERE employee_id = ? "
        + _NOT_INVOICE_COST +
        "AND IFNULL(approval_status,'') = '已打款'", (uid,)))
    reject = round2(scalar(
        "SELECT IFNULL(SUM(amount),0) FROM costs WHERE employee_id = ? "
        + _NOT_INVOICE_COST +
        "AND IFNULL(approval_status,'') = '已驳回'", (uid,)))
    return {
        "advance_total": adv_total,
        "advance_used": used,
        "advance_balance": round2(adv_total - used),
        "pending": pend, "approved": aprv, "paid": paid, "rejected": reject,
        "pending_count": scalar(
            "SELECT COUNT(*) FROM costs WHERE employee_id = ? "
            + _NOT_INVOICE_COST +
            "AND IFNULL(approval_status,'') = '待审批'", (uid,)),
    }


def reimb_detail(uid, limit=20, exclude_status=()):
    """某人的报销明细（不含发票自动派生的费用）。

    exclude_status：要排除的审批状态（如首页弹窗只看"还没走完"的单子，
    不要把「已批准」「无需报销」这种已办结的又列一遍）。
    """
    # category 列存的是 cost_categories.id，要联表换成名称再给页面显示
    # （否则模板里 r.category 打出来就是一串数字）
    sql = ("SELECT c.id, c.code, c.cost_date, COALESCE(cc.name, c.category) AS category, "
           "       c.summary, c.amount, "
           "       c.settle_type, IFNULL(c.approval_status,'') AS approval_status, "
           "       c.approve_memo, p.name AS project_name "
           "FROM costs c LEFT JOIN projects p ON c.project_id = p.id "
           "LEFT JOIN cost_categories cc ON c.category = cc.id "
           "WHERE c.employee_id = ? AND c.invoice_id IS NULL ")
    args = [uid]
    exclude_status = tuple(s for s in (exclude_status or ()) if s)
    if exclude_status:
        sql += ("AND IFNULL(c.approval_status,'') NOT IN (%s) "
                % ",".join("?" * len(exclude_status)))
        args.extend(exclude_status)
    sql += "ORDER BY c.cost_date DESC, c.id DESC LIMIT ?"
    args.append(limit)
    return query(sql, tuple(args))


def pending_approvals(limit=200):
    """待财务/管理员处理的报销单。"""
    return query(
        "SELECT c.id, c.code, c.cost_date, c.amount, c.settle_type, c.summary, "
        "       IFNULL(u.display_name, u.username) AS employee_name, "
        "       p.name AS project_name, "
        "       COALESCE(cc.name, c.category) AS category, c.memo "
        "FROM costs c LEFT JOIN users u ON c.employee_id = u.id "
        "LEFT JOIN projects p ON c.project_id = p.id "
        "LEFT JOIN cost_categories cc ON c.category = cc.id "
        "WHERE IFNULL(c.approval_status,'') = '待审批' "
        "ORDER BY c.cost_date, c.id LIMIT ?", (limit,))


def attendance_pending(limit=300):
    """待审批的加班 / 请假 / 出差。"""
    out = {}
    out["overtime"] = query(
        "SELECT t.id, t.code, t.ot_date AS d1, '' AS d2, t.hours, t.reason, "
        "       IFNULL(u.display_name,u.username) AS employee_name, "
        "       p.name AS project_name "
        "FROM overtime t LEFT JOIN users u ON t.employee_id=u.id "
        "LEFT JOIN projects p ON t.project_id=p.id "
        "WHERE IFNULL(t.status,'待审批')='待审批' ORDER BY t.ot_date LIMIT ?", (limit,))
    out["leaves"] = query(
        "SELECT t.id, t.code, t.start_date AS d1, t.end_date AS d2, t.days, "
        "       t.leave_type, t.reason, "
        "       IFNULL(u.display_name,u.username) AS employee_name, "
        "       '' AS project_name "
        "FROM leaves t LEFT JOIN users u ON t.employee_id=u.id "
        "WHERE IFNULL(t.status,'待审批')='待审批' ORDER BY t.start_date LIMIT ?", (limit,))
    out["trips"] = query(
        "SELECT t.id, t.code, t.start_date AS d1, IFNULL(t.end_date,'未定') AS d2, "
        "       t.days, t.destination, t.reason, "
        "       IFNULL(u.display_name,u.username) AS employee_name, "
        "       p.name AS project_name "
        "FROM trips t LEFT JOIN users u ON t.employee_id=u.id "
        "LEFT JOIN projects p ON t.project_id=p.id "
        "WHERE IFNULL(t.status,'待审批')='待审批' ORDER BY t.start_date LIMIT ?", (limit,))
    return out


def approve_entity(entity_key, rid, status, memo="", approver_id=None):
    tbl = {"costs": "costs", "overtime": "overtime",
           "leaves": "leaves", "trips": "trips"}.get(entity_key)
    if not tbl:
        raise ValueError("不支持的类型")
    if status not in ("已批准", "已驳回", "已打款", "待审批"):
        raise ValueError("审批状态不合法")
    # 状态机：只允许合法的流转，避免「已打款 → 已批准」这种回退（同一笔钱会再打一次）
    if tbl == "costs":
        cur = (one("SELECT approval_status FROM costs WHERE id = ?", (rid,)) or {}) \
            .get("approval_status") or ""
    else:
        cur = (one(f"SELECT status FROM {tbl} WHERE id = ?", (rid,)) or {}) \
            .get("status") or ""
    allowed = {
        "": {"待审批", "已批准", "已驳回", "已打款"},
        "待审批": {"已批准", "已驳回", "已打款"},
        "已批准": {"已打款", "已驳回", "待审批"},
        "已驳回": {"待审批"},
        # 已打款是终态：只能先退回「待审批」再重新走审批，不能直接改回已批准
        # （否则同一笔钱会被再打一次 —— paid_at 还在，页面上看不出区别）
        "已打款": {"待审批"},
        "无需报销": {"待审批"},
    }
    if cur and status not in allowed.get(cur, set()):
        raise ValueError("不能把「%s」直接改成「%s」" % (cur, status))
    if tbl == "costs":
        if cur == "已打款" and status != "已打款":
            # 退回/改判：把打款痕迹清掉，别让首页继续把它算成已付
            execute("UPDATE costs SET approval_status=?, approver_id=?, approved_at=?, "
                    "approve_memo=?, paid_at=NULL, paid_by=NULL, paid_amount=0 "
                    "WHERE id=?",
                    (status, approver_id, now_str(), memo, rid))
        elif status == "已打款":
            execute("UPDATE costs SET approval_status=?, approver_id=?, approved_at=?, "
                    "approve_memo=?, paid_at=?, paid_by=?, "
                    "paid_amount=ROUND(IFNULL(amount,0),2), pay_status='已支付' "
                    "WHERE id=?",
                    (status, approver_id, now_str(), memo, now_str(), approver_id, rid))
        else:
            execute("UPDATE costs SET approval_status=?, approver_id=?, approved_at=?, "
                    "approve_memo=? WHERE id=?",
                    (status, approver_id, now_str(), memo, rid))
    else:
        execute(f"UPDATE {tbl} SET status=?, approver_id=?, approved_at=?, "
                f"approve_memo=? WHERE id=?",
                (status, approver_id, now_str(), memo, rid))


def mark_paid(rid, operator_id=None):
    row = one("SELECT * FROM costs WHERE id = ?", (rid,))
    if not row:
        raise ValueError("记录不存在")
    if (row.get("approval_status") or "") not in ("已批准", "已打款"):
        raise ValueError("只有「已批准」的报销单才能打款")
    # 打款完成＝这笔费用真的付出去了：同时回写「已付金额」，
    # 否则首页「累计付款 / 费用已付」会把所有已打款的报销漏掉。
    execute("UPDATE costs SET approval_status='已打款', paid_at=?, paid_by=?, "
            "paid_amount=ROUND(IFNULL(amount,0),2), pay_status='已支付' WHERE id=?",
            (now_str(), operator_id, rid))


# ------------------------------------------------------------ 工资测算
# 应发组与扣款组（顺序即工资表列顺序）
PAY_GROSS = ("base_salary", "post_salary", "bonus", "sea_allow", "trip_allow",
             "other_allow", "overtime", "meal_allow", "welfare", "heat_allow")
PAY_DEDUCT = ("pension", "medical", "serious_ill", "unemployment", "fund",
              "annuity", "other_deduct")
PAY_FIELDS = PAY_GROSS + PAY_DEDUCT + ("tax", "extra_pay", "work_days")
# 可直接录入的两个口径（调整页里填，参与加班费 / 出差费 / 餐补计算）
PAY_INPUT = ("ot_hours", "trip_days")
PAY_STORE = PAY_FIELDS + PAY_INPUT

# 个税税率表：月度换算表 / 年度累计表（累计预扣法用）
TAX_MONTH = [(3000, 0.03, 0), (12000, 0.10, 210), (25000, 0.20, 1410),
             (35000, 0.25, 2660), (55000, 0.30, 4410), (80000, 0.35, 7160),
             (float("inf"), 0.45, 15160)]
TAX_YEAR = [(36000, 0.03, 0), (144000, 0.10, 2520), (300000, 0.20, 16920),
            (420000, 0.25, 31920), (660000, 0.30, 52920), (960000, 0.35, 85920),
            (float("inf"), 0.45, 181920)]


def _tax_by_table(amount, table):
    for cap, rate, quick in table:
        if amount <= cap:
            return max(0.0, round2(amount * rate - quick))
    return 0.0


def month_day_count(ym):
    """某月的自然天数（餐补按天算要用）。"""
    import calendar
    try:
        y, m = int(ym[:4]), int(ym[5:7])
        return calendar.monthrange(y, m)[1]
    except (ValueError, IndexError):
        return 30


def staff_rows(only_active=True):
    """员工花名册（带关联的登录账号名）。"""
    sql = ("SELECT s.*, IFNULL(u.display_name, u.username) AS user_name "
           "FROM staff s LEFT JOIN users u ON s.user_id = u.id ")
    if only_active:
        sql += "WHERE IFNULL(s.status, '在职') <> '停用' "
    sql += "ORDER BY IFNULL(s.sort, 0), IFNULL(s.emp_no, ''), s.id"
    return query(sql)


def tax_deduct_of(staff_id):
    r = one("SELECT IFNULL(tax_deduct, 0) AS d FROM staff WHERE id = ?", (staff_id,))
    return round2((r or {}).get("d") or 0)


# 生成下月工资表时「照抄上月」的字段：
# 工资标准、各项补贴、社保公积金、其他扣款、其他补发、备注 —— 这些跟月份无关，
# 上月手改过什么就带过来。加班费 / 餐补 / 出差补贴 / 计薪出勤天数 跟**当月考勤**绑死，
# 个税要按新的应发重算，这五项一律不照抄（见 payroll_month 的 carry_from）。
PAY_CARRY = ("base_salary", "post_salary", "bonus", "sea_allow", "other_allow",
             "welfare", "heat_allow", "extra_pay",
             "pension", "medical", "serious_ill", "unemployment",
             "fund", "annuity", "other_deduct")


def next_month(ym):
    """"2026-09" → "2026-10"。"""
    y, m = int(ym[:4]), int(ym[5:7])
    m += 1
    if m == 13:
        y, m = y + 1, 1
    return "%04d-%02d" % (y, m)


def payroll_month(ym, carry_from=None):
    """按月测算每名员工的工资（按人社部计薪口径）。

      月计薪天数 21.75（法定）        日工资 = 月工资 ÷ 21.75
      小时工资   = 日工资 ÷ 8
      加班费     = Σ 加班小时 × 小时工资 × 倍数
                   （周一至周六视为工作日 × 1.5；周日视为休息日 × 2.0）
      请假扣款   = 请假天数 × 日工资 × 扣款倍数
      出差补贴   = Σ 已批准出差单上的补贴
      餐补       = 非出差出勤天数 × 不出差餐补 + 出差天数 × 出差餐补
      社保个人   = 缴费基数 ×（养老 8% + 医疗 2% + 失业 0.5%）+ 大病定额
      公积金     = 花名册里登记的月缴额
      个税       = 有本年历史月份时用累计预扣法，否则用月度税率表
      应发小计   = 基础+岗位+奖金+出海+出差+其他补贴+加班费+餐补+福利+高温
      扣款小计   = 养老+医疗+大病+失业+公积金+年金+其他扣款+个税
      实发工资   = 应发小计 + 其他补发 − 扣款小计

    数据来源：员工花名册（staff）决定发薪名单与工资标准；
    加班/请假/出差只对「关联了登录账号」的员工自动汇总。
    已经保存过的月份（locked）以库里的数字为准，不会被重新计算覆盖。

    carry_from = "2026-09" 时用于「一键生成下月」：把上月的 PAY_CARRY 各字段
    （工资标准 / 补贴 / 社保公积金 / 其他扣款 / 备注）搬过来当初值，
    加班费、餐补、出差补贴、出勤天数仍按本月考勤算，个税按搬完之后的应发重算。
    """
    p = payroll_params()
    # 计薪天数按当月实际工作日算（周一至周六 − 法定假日 + 调休上班），
    # 参数 monthly_days 仅作兜底（比如节假日没维护时仍能出数）。
    payable = payroll_payable_days(ym)
    cal_days = month_day_count(ym)
    mdays = payable or (p["monthly_days"] or 21.75)
    # 封存（后面已经有月份了）的月份以库里存的定稿值为准，不让规则再穿透
    sealed = bool(scalar("SELECT COUNT(*) FROM payroll WHERE ym > ?", (ym,)))

    # 考勤：加班按周一至周六=工作日、周日=休息日分别累计小时
    ot = {}
    for r in query("SELECT employee_id, ot_date, hours FROM overtime "
                   "WHERE status='已批准' "
                   "AND substr(IFNULL(ot_date,''),1,7) = ?", (ym,)):
        uid = r["employee_id"]
        if not uid:
            continue
        try:
            wd = date.fromisoformat(str(r["ot_date"] or "")[:10]).weekday()
        except (TypeError, ValueError):
            wd = 0
        acc = ot.setdefault(uid, {"weekday": 0.0, "weekend": 0.0})
        acc["weekend" if wd == 6 else "weekday"] += float(r["hours"] or 0)

    lv = {r["employee_id"]: round2(r["d"] or 0) for r in query(
        "SELECT employee_id, SUM(days) d FROM leaves WHERE status='已批准' "
        "AND substr(IFNULL(start_date,''),1,7) = ? GROUP BY employee_id", (ym,))}
    tr = {r["employee_id"]: (round2(r["d"] or 0), round2(r["a"] or 0))
          for r in query(
              "SELECT employee_id, SUM(days) d, SUM(IFNULL(allowance,0)) a "
              "FROM trips WHERE status='已批准' "
              "AND substr(IFNULL(start_date,''),1,7) = ? "
              "GROUP BY employee_id", (ym,))}

    saved = {r["staff_id"]: r for r in query(
        "SELECT * FROM payroll WHERE ym = ?", (ym,))}
    carry = {}
    if carry_from:
        carry = {r["staff_id"]: r for r in query(
            "SELECT * FROM payroll WHERE ym = ?", (carry_from,))}
    hist = {}
    for r in query("SELECT * FROM payroll WHERE substr(ym,1,4) = ? AND ym < ? "
                   "ORDER BY ym", (ym[:4], ym)):
        hist.setdefault(r["staff_id"], []).append(r)

    # ---- 名单：谁该出现在 ym 月的工资表里 ----
    #  a) 花名册里「在职」的：所有月份都算；
    #  b) 「停用」但填了**离职日期**的：离职当月及以前照常算（离职当月按天折算），
    #     之后的月份自动不再出现 —— 这就是"按离职日期到时自动测算"；
    #  c) 「停用」又没填离职日期的：只有**这个月已经存过工资**才保留
    #     （否则一停用，他以前各月的工资行会凭空消失，合计也跟着少人）。
    people = []
    for st in staff_rows():
        people.append(st)
    on_board = {s["id"] for s in people}
    for st in staff_rows(only_active=False):
        if st["id"] in on_board:
            continue
        ld = (st.get("leave_date") or "").strip()[:10]
        if ld and ld[:7] >= ym:            # 离职日期还没到（或就在本月）
            st["_leaver"] = True
            people.append(st)
        elif not ld and st["id"] in saved:
            st["_stopped"] = True          # 没填离职日期的老停用员工：保住历史
            people.append(st)

    rows = []
    for st in people:
        uid = st.get("user_id")
        _base_raw = st.get("base_salary")
        base = round2(p["salary_default"] if _base_raw is None else _base_raw)
        post = round2(st.get("post_salary") or 0)
        daily = round2(base / mdays) if mdays else 0.0
        hourly = round2(daily / p["hours_per_day"]) if p["hours_per_day"] else 0.0

        o = ot.get(uid, {"weekday": 0.0, "weekend": 0.0}) if uid else             {"weekday": 0.0, "weekend": 0.0}
        ot_hours = round2(o["weekday"] + o["weekend"])
        ot_pay = round2(o["weekday"] * hourly * p["ot_rate_weekday"]
                        + o["weekend"] * hourly * p["ot_rate_weekend"])
        leave_days = lv.get(uid, 0.0) if uid else 0.0
        leave_cut = round2(leave_days * daily * p["leave_deduct_rate"])
        trip_days, trip_allow = tr.get(uid, (0.0, 0.0)) if uid else (0.0, 0.0)
        # 计薪/出勤天数：当月计薪天数 − 请假天数（出差照算，不扣；加班不折天）
        work_days = max(0.0, round2(mdays - leave_days))
        # 加班满一个工作日 → 当天也有餐补（用户 2026-11-11 要求）
        ot_days = int(ot_hours // p["hours_per_day"]) if p["hours_per_day"] else 0
        meal = round2((work_days + ot_days) * p["meal_office_per_day"]
                      + trip_days * p["meal_trip_per_day"])
        # 出差费：默认按「日工资 × 出差天数」（与加班费同口径），可切回固定每天标准
        trip_pay = (round2(daily * trip_days) if p["trip_pay_mode"] == "daily"
                    else round2(trip_days * p["trip_allowance_per_day"]))
        # 高温补贴：只有 6/7/8 月；福利金：只在端午 / 中秋所在月
        try:
            _mnum = int(ym[5:7])
        except (TypeError, ValueError):
            _mnum = 0
        heat = round2(p["heat_allow_per_month"]) if _mnum in p["heat_months"] else 0.0
        welfare = round2(p["welfare_amount"]) if ym in p["welfare_months"] else 0.0

        # ---- 离职折算（只对「离职日期落在本月」的人生效）----
        # 在职天数 = 离职日（含当天）；基础工资 / 岗位津贴按月标准 × 在职天数/当月自然天数，
        # 出勤天数与餐补也只算到离职日。加班费仍按**完整月标准**折算的小时工资算
        # （日工资/小时工资不参与折算，那是他签的工价）。
        # 折算结果另存一份，等下覆盖完定稿值还要再盖回去（见下面 leave_fix）。
        leave_note, leave_fix = "", None
        ld = (st.get("leave_date") or "").strip()[:10]
        if st.get("_leaver") and ld[:7] == ym:
            try:
                last = int(ld[8:10])
            except ValueError:
                last = 0
            if 0 < last < cal_days:
                ratio = last / cal_days
                base = round2(base * ratio)
                post = round2(post * ratio)
                work_days = max(0.0, round2(round2(mdays * ratio) - leave_days))
                meal = round2((work_days + ot_days) * p["meal_office_per_day"]
                              + trip_days * p["meal_trip_per_day"])
                leave_note = "%s 离职，当月按 %d/%d 天折算" % (ld, last, cal_days)
                leave_fix = {"base_salary": base, "post_salary": post,
                             "work_days": work_days, "meal_allow": meal}
            elif last >= cal_days:
                leave_note = "%s 离职（当月按满月算）" % ld

        sb = round2(st.get("social_base") or base)      # 社保缴费基数
        no_social = bool(st.get("social_off"))
        item = {
            "staff_id": st["id"], "emp_no": st.get("emp_no") or "",
            "name": st.get("name") or "", "dept": st.get("dept") or "",
            "id_card": st.get("id_card") or "",
            "bank_account": st.get("bank_account") or "",
            "user_name": st.get("user_name") or "",
            "tax_deduct": round2(st.get("tax_deduct") or 0),
            "social_base": sb,
            # 应发
            "base_salary": base, "post_salary": post, "bonus": 0.0,
            "sea_allow": 0.0, "trip_allow": trip_pay, "other_allow": 0.0,
            "overtime": ot_pay, "meal_allow": meal, "welfare": welfare,
            "heat_allow": heat,
            # 扣款（不缴社保的员工个人部分记 0）
            "pension": 0.0 if no_social else round2(sb * p["pension_rate"]),
            "medical": 0.0 if no_social else round2(sb * p["medical_rate"]),
            "serious_ill": 0.0 if no_social else round2(p["serious_ill_fixed"]),
            "unemployment": 0.0 if no_social else round2(sb * p["unemploy_rate"]),
            "fund": round2(st.get("fund_fixed") or 0),
            "annuity": 0.0, "other_deduct": 0.0, "tax": 0.0,
            # 其它
            "extra_pay": 0.0, "work_days": work_days,
            "ot_hours": ot_hours, "trip_days": trip_days,
            "payable_days": mdays,
            "locked": 0, "memo": "",
            "stopped": bool(st.get("_stopped")),      # 花名册里已停用（但本月有工资）
            "leaver": bool(leave_note),               # 离职当月（已按天折算）
            "leave_note": leave_note,
            # 辅助明细（不进工资表，页面上展示用）
            "ot_hours": ot_hours, "ot_weekday": round2(o["weekday"]),
            "ot_weekend": round2(o["weekend"]),
            "leave_days": leave_days, "leave_cut": leave_cut,
            "trip_days": trip_days, "daily": daily, "hourly": hourly,
        }
        # 一键生成下月：标准 / 补贴 / 社保公积金 / 其他扣款 / 备注 照抄上月
        cf = carry.get(st["id"])
        if cf:
            for k in PAY_CARRY:
                if cf.get(k) is not None:
                    item[k] = round2(cf[k])
            item["memo"] = cf.get("memo") or ""
            item["carried"] = True

        sv = saved.get(st["id"])
        locked = bool(sv and sv.get("locked"))
        if locked:
            # 只有「已定稿」的月份才用库里的数字覆盖自动测算结果；
            # 未定稿的记录仍会被读到（用于累计个税），但不覆盖自动值。
            for k in PAY_STORE:
                if sv.get(k) is not None:
                    item[k] = round2(sv[k])
            item["locked"] = 1
            item["memo"] = sv.get("memo") or ""

        # 离职折算要**盖在定稿值之上**：离职日期一填，那一行的基本工资 / 岗位津贴 /
        # 出勤天数 / 餐补 就按天重算 —— 否则"本月已经定稿过"的人填了离职日期却没反应
        # （定稿值把折算结果整行覆盖了，用户看到的就是"没按天算"）。
        # 但两种情况不穿：
        #   · 封存月份 —— 已经归档，要改先填原因解锁；
        #   · 你在【调整】里手工改过的行（manual=1）—— 手工填的数永远说了算，
        #     不然改完一保存又被折算盖回去，看起来就是"调整不了"。
        if locked and leave_fix and not sealed and not sv.get("manual"):
            item.update(leave_fix)
            locked = False            # 收入变了 → 个税要跟着重算

        item["gross"] = round2(sum(item[k] for k in PAY_GROSS))
        if not locked:
            item["tax"] = _tax_of(ym, item, p, hist.get(st["id"], []))
        item["deduct_total"] = round2(sum(item[k] for k in PAY_DEDUCT)
                                      + item["tax"])
        item["net"] = round2(item["gross"] + item["extra_pay"]
                             - item["deduct_total"])
        rows.append(item)

    tot = {"gross": 0.0, "deduct_total": 0.0, "net": 0.0, "tax": 0.0}
    for k in PAY_GROSS + PAY_DEDUCT + ("extra_pay",):
        tot[k] = round2(sum(r[k] for r in rows))
    for k in ("gross", "deduct_total", "net", "tax"):
        tot[k] = round2(sum(r[k] for r in rows))
    return {"ym": ym, "params": p, "month_days": mdays, "cal_days": cal_days,
            "rows": rows, "totals": tot}


def _tax_of(ym, item, p, hist):
    """个人所得税。

    有本年已保存的历史月份时用「累计预扣法」（税务口径）；
    没有历史数据（年初、或只做单月）时退化为月度税率表 ——
    这样不会因为缺前几个月的数据而少扣税。
    """
    sf = round2(sum(item[k] for k in
                    ("pension", "medical", "serious_ill", "unemployment",
                     "fund", "annuity")))
    std_month = p["tax_threshold"] + (item.get("tax_deduct") or 0)
    if not hist:
        return _tax_by_table(max(0.0, round2(item["gross"] - sf - std_month)),
                             TAX_MONTH)
    n = 1
    cum_income, cum_sf, paid = item["gross"], sf, 0.0
    for h in hist:
        cum_income += round2(sum((h.get(k) or 0) for k in PAY_GROSS))
        cum_sf += round2(sum((h.get(k) or 0) for k in
                             ("pension", "medical", "serious_ill",
                              "unemployment", "fund", "annuity")))
        paid += round2(h.get("tax") or 0)
        n += 1
    taxable = max(0.0, round2(cum_income - cum_sf - std_month * n))
    return max(0.0, round2(_tax_by_table(taxable, TAX_YEAR) - paid))


def _payroll_store(ym, rows, locked=1):
    """把一批测算结果写进 payroll；locked=1 定稿、locked=0 存草稿（可再改）。"""
    cols = ["ym", "staff_id"] + list(PAY_STORE) + ["locked", "memo"]
    sql = (f"INSERT INTO payroll ({','.join(cols)}, updated_at) "
           f"VALUES ({','.join('?' * len(cols))}, ?) "
           "ON CONFLICT(ym, staff_id) DO UPDATE SET "
           + ", ".join(f"{c} = excluded.{c}" for c in cols
                       if c not in ("ym", "staff_id"))
           + ", updated_at = excluded.updated_at")
    n = 0
    for r in rows:
        vals = ([ym, r["staff_id"]] + [r.get(c) or 0 for c in PAY_STORE]
                + [int(locked), r.get("memo") or "", now_str()])
        execute(sql, vals)
        n += 1
    return n


def payroll_save(ym):
    """把本月测算结果落库并定稿（locked=1）。之后再点重算也不会覆盖。"""
    return _payroll_store(ym, payroll_month(ym)["rows"])


# ---------------------------------------------------------- 定稿 / 解除定稿
# 状态模型（2026-11-11 用户要求）：
#   · 逐人【调整】只把那一行存进 payroll（locked=1, manual=1），**不等于整月定稿**；
#   · 整月定稿 = 点【本月工资定稿】：写全部行 + 在 payroll_final 打月份标记；
#   · 解除定稿 = 输入当前账号密码后删标记（数值保留，可以继续改，改完再定稿）；
#   · 后面月份一旦定稿，前面的月份就不能再改（要改同样先解除后面月份的定稿）。
def payroll_month_final(ym):
    return one("SELECT * FROM payroll_final WHERE ym = ?", (ym,))


def payroll_is_final(ym):
    return bool(payroll_month_final(ym))


def payroll_seal_state(ym):
    """某月工资表的状态：finalized = 本月已定稿；sealed = 后面的月份已定稿。"""
    fin = payroll_month_final(ym) or {}
    later = scalar("SELECT COUNT(*) FROM payroll_final WHERE ym > ?", (ym,))
    return {
        "ym": ym,
        "finalized": bool(fin),
        "finalized_at": fin.get("finalized_at") or "",
        "actor": fin.get("actor") or "",
        "sealed": bool(later),
        "editable": (not fin) and (not later),
    }


def payroll_block_msg(ym):
    """不能改时的提示；可改返回空字符串。"""
    st = payroll_seal_state(ym)
    if st["finalized"]:
        return ("%s 的工资表已定稿（%s · %s）。要修改请先【解除定稿】——需要输入登录密码。"
                % (ym, st["actor"] or "—", st["finalized_at"] or ""))
    if st["sealed"]:
        return ("%s 后面的月份已经定稿，本月不能再改；要改请先解除后面月份的定稿。"
                % ym)
    return ""


def payroll_finalize(ym, actor=""):
    """本月工资定稿：写全部行 + 打月份标记。返回人数。"""
    n = payroll_save(ym)
    execute("INSERT INTO payroll_final (ym, finalized_at, actor, memo) "
            "VALUES (?,?,?,'') ON CONFLICT(ym) DO UPDATE SET "
            "finalized_at = excluded.finalized_at, actor = excluded.actor",
            (ym, now_str(), actor))
    execute("INSERT INTO payroll_lock_log (ym, action, reason, actor, at) "
            "VALUES (?, 'finalize', '', ?, ?)", (ym, actor, now_str()))
    return n


def payroll_unfinalize(ym, actor="", note=""):
    """解除定稿（调用方已校验密码）：只删标记，数值保留。"""
    if not payroll_is_final(ym):
        return False, "%s 还没有定稿" % ym
    execute("DELETE FROM payroll_final WHERE ym = ?", (ym,))
    execute("INSERT INTO payroll_lock_log (ym, action, reason, actor, at) "
            "VALUES (?, 'unfinalize', ?, ?, ?)", (ym, note or "", actor, now_str()))
    return True, ("%s 已解除定稿，可以继续调整（数值保留；改完再点【本月工资定稿】）"
                  % ym)


def payroll_unlock_log(ym, limit=20):
    return query("SELECT * FROM payroll_lock_log WHERE ym = ? "
                 "ORDER BY id DESC LIMIT ?", (ym, limit))


def payroll_generate_next(ym, actor=""):
    """一键生成下个月工资表（本月已定稿的成果为基准）。

    标准 / 补贴 / 社保公积金 / 其他扣款 / 备注照抄本月；
    加班费 / 餐补 / 出差补贴 / 计薪出勤天数 按下月已批准的考勤重算，个税重算。
    下月已经有工资表时**不覆盖**，返回 (0, 下月) 让页面提示。
    """
    nxt = next_month(ym)
    if scalar("SELECT COUNT(*) FROM payroll WHERE ym = ?", (nxt,)):
        return 0, nxt
    if not payroll_is_final(ym):
        return -1, nxt          # 本月还没定稿：先核对、定稿，再生成下月
    # 生成的是一份**草稿**（locked=0）：先看、再逐人调整，最后点【定稿】才算数
    n = _payroll_store(nxt, payroll_month(nxt, carry_from=ym)["rows"], locked=0)
    return n, nxt


def payroll_one(ym, staff_id):
    """取某人某月的明细（用于手工调整）。"""
    data = payroll_month(ym)
    for r in data["rows"]:
        if r["staff_id"] == int(staff_id):
            return r, data["params"]
    return None, data["params"]


def payroll_reset_leave_month(staff_id, ym):
    """离职日期有变动时调用：把那个人在 <ym> 月那一行的"手工改过"标记清掉，
    让离职折算重新接管 —— 不然先手工调过、后填离职日期的人不会按天重算。
    （只清标记，不动数字；读出来就是折算后的值。）
    """
    if not ym:
        return 0
    return execute("UPDATE payroll SET manual = 0 WHERE ym = ? AND staff_id = ?",
                   (ym, int(staff_id)))


def payroll_update(ym, staff_id, form):
    """手工调整某人的工资明细 —— 改过的月份标记为已定稿。

    存下来的值一律以你填的为准（manual=1）。离职折算只对**没手工改过**的行生效，
    所以这里不再强行折算 —— 否则你填 825、存进去变 2800，就成了"调整不了"。
    """
    cols = list(PAY_STORE) + ["memo"]
    data = {}
    for c in cols[:-1]:
        try:
            data[c] = round2(float(form.get(c) or 0))
        except (TypeError, ValueError):
            data[c] = 0.0
    data["memo"] = (form.get("memo") or "").strip()
    vals = [data[c] for c in cols]
    sql = (f"INSERT INTO payroll (ym, staff_id, {','.join(cols)}, locked, manual, "
           "updated_at) VALUES (?, ?, " + ",".join("?" * len(cols))
           + ", 1, 1, ?) ON CONFLICT(ym, staff_id) DO UPDATE SET "
           + ", ".join(f"{c} = excluded.{c}" for c in cols)
           + ", locked = 1, manual = 1, updated_at = excluded.updated_at")
    execute(sql, [ym, int(staff_id)] + vals + [now_str()])


def payroll_unlock_draft(ym):
    """解除本月"定稿"，让自动测算重新接管（手工改过的数字会被覆盖）。

    与「解锁修改」不是一回事：那个是封存月份填原因后临时放行，
    这个是把这个月退回"测算中"。只对**未封存**的月份有意义。
    """
    # manual 也要清掉：整月重算＝回到规则，离职折算重新接管这一行
    n = execute("UPDATE payroll SET locked = 0, manual = 0 WHERE ym = ?", (ym,))
    execute("DELETE FROM payroll_final WHERE ym = ?", (ym,))
    return n


# 工资表列定义：(字段, 表头, 列宽)
PAY_COLS = [
    ("emp_no", "工号", 8), ("name", "姓名", 10), ("id_card", "身份证号", 21),
    ("bank_account", "帐号", 21), ("dept", "部门", 8),
    ("work_days", "计薪天数", 9),
    ("base_salary", "基础工资", 10), ("post_salary", "岗位津贴", 10),
    ("bonus", "奖金", 8), ("sea_allow", "出海补贴", 10),
    ("trip_allow", "出差补贴", 10), ("other_allow", "其他补贴", 10),
    ("overtime", "加班费", 10), ("meal_allow", "餐补", 9),
    ("welfare", "福利金", 9), ("heat_allow", "高温补贴", 10),
    ("gross", "应发小计", 11),
    ("pension", "养老保险", 10), ("medical", "医疗保险", 10),
    ("serious_ill", "大病补充", 10), ("unemployment", "失业保险", 10),
    ("fund", "公积金", 9), ("annuity", "企业年金", 10),
    ("other_deduct", "其他扣款", 10), ("tax", "个税", 9),
    ("deduct_total", "扣款小计", 11),
    ("extra_pay", "其他补发", 10), ("net", "实发工资", 11),
]


def payroll_export_rows(ym):
    return payroll_month(ym)["rows"]


def payroll_workbook(ym, company="", title=None):
    """生成工资表工作簿：一、工资表（一行一人）；二、工资条（每人一块）。"""
    d = payroll_month(ym)
    rows = d["rows"]
    head = [c[1] for c in PAY_COLS]
    widths = [c[2] for c in PAY_COLS]
    y, m = ym[:4], int(ym[5:7])
    name_title = title or f"{y}年{m}月份工资表"
    stamp = "制表时间：" + now_str()[:10]

    sheet = [[company], [name_title, "", "", stamp], [""] + head]
    for r in rows:
        sheet.append([r.get(f) if f not in ("emp_no", "name", "id_card",
                                            "bank_account", "dept")
                      else (r.get(f) or "") for f, _l, _w in PAY_COLS])
    tot = d["totals"]
    total_row = [""] * len(PAY_COLS)
    total_row[1] = "合计"
    for i, (f, _l, _w) in enumerate(PAY_COLS):
        if f in tot:
            total_row[i] = tot[f]
    sheet.append(total_row)

    # 工资条：每人一小块（空行 + 标题 + 表头 + 数据），打印后裁剪即可发放
    slip = []
    for r in rows:
        slip.append([])
        slip.append([company, name_title])
        slip.append([r.get("emp_no") or "", r.get("name") or "",
                     f"部门：{r.get('dept') or ''}"])
        slip.append([c[1] for c in PAY_COLS])
        slip.append([r.get(f) if f not in ("emp_no", "name", "id_card",
                                           "bank_account", "dept")
                     else (r.get(f) or "") for f, _l, _w in PAY_COLS])
    return make_xlsx([("工资表", sheet, widths), ("工资条", slip, widths)])



def recent_months(n=14):
    """最近 n 个月（YYYY-MM，倒序）。"""
    out, d = [], date.today()
    y, m = d.year, d.month
    for _ in range(n):
        out.append("%04d-%02d" % (y, m))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out


# ------------------------------------------------------------ 极简 xlsx 生成
_XLSX_CT = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
%s
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>"""

_XLSX_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

_XLSX_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="2"><font><sz val="11"/><name val="\u5b8b\u4f53"/></font><font><b/><sz val="11"/><name val="\u5b8b\u4f53"/></font></fonts>
<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FFDCE6F1"/><bgColor indexed="64"/></patternFill></fill></fills>
<borders count="1"><border/></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/></cellXfs>
</styleSheet>"""


def _col_letter(n):
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _xml_escape(v):
    return (str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _sheet_xml(rows, widths=None):
    parts = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
             '<worksheet xmlns="http://schemas.openxmlformats.org/'
             'spreadsheetml/2006/main">']
    if widths:
        parts.append("<cols>")
        for i, w in enumerate(widths, 1):
            parts.append(f'<col min="{i}" max="{i}" width="{w}" customWidth="1"/>')
        parts.append("</cols>")
    parts.append("<sheetData>")
    for ri, row in enumerate(rows, 1):
        cells = []
        for ci, val in enumerate(row, 1):
            ref = f"{_col_letter(ci)}{ri}"
            style = ' s="1"' if ri == 1 else ""
            if val is None or val == "":
                continue
            if isinstance(val, bool) or isinstance(val, (int, float)):
                cells.append(f'<c r="{ref}"{style}><v>{val}</v></c>')
            else:
                txt = _xml_escape(val)
                cells.append(f'<c r="{ref}"{style} t="inlineStr"><is><t '
                             f'xml:space="preserve">{txt}</t></is></c>')
        parts.append(f'<row r="{ri}">' + "".join(cells) + "</row>")
    parts.append("</sheetData></worksheet>")
    return "".join(parts)


def make_xlsx(sheets):
    """sheets: [(sheet_name, rows, widths)] → xlsx 字节流（无需第三方库）。"""
    import zipfile
    buf = io.BytesIO()
    overrides = "".join(
        f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/'
        f'vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for i in range(1, len(sheets) + 1))
    sheet_tags = "".join(
        f'<sheet name="{_xml_escape(nm)}" sheetId="{i}" r:id="rId{i}"/>'
        for i, (nm, _r, _w) in enumerate(sheets, 1))
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
                      for i in range(1, len(sheets) + 1))
            + f'<Relationship Id="rId{len(sheets) + 1}" Type="http://schemas.'
              'openxmlformats.org/officeDocument/2006/relationships/styles" '
              'Target="styles.xml"/></Relationships>')
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _XLSX_CT % overrides)
        z.writestr("_rels/.rels", _XLSX_RELS)
        z.writestr("xl/workbook.xml", wb)
        z.writestr("xl/_rels/workbook.xml.rels", rels)
        z.writestr("xl/styles.xml", _XLSX_STYLES)
        for i, (_nm, rows, widths) in enumerate(sheets, 1):
            z.writestr(f"xl/worksheets/sheet{i}.xml", _sheet_xml(rows, widths))
    return buf.getvalue()


# ------------------------------------------------------------ 出勤导出数据
def attendance_sheets(ym=None, scope=None, uid=None):
    """组装出勤导出用的一系列工作表。scope 用于员工的数据范围限制。"""
    def w(tbl_alias, extra_where="", args=()):
        frag, sargs = ("", [])
        if scope and scope.get(tbl_alias):
            frag, sargs = scope[tbl_alias]
        # 两个条件是「并且」关系：以前用空格拼，员工导出时 SQL 直接语法错
        cond = " AND ".join(x for x in [extra_where, frag] if x)
        return (" WHERE " + cond if cond else ""), list(args) + list(sargs)

    sheets = []

    # 1) 汇总
    wsql = [
        "SELECT IFNULL(u.display_name,u.username) AS 员工, m.ym AS 月份,",
        " IFNULL(ot.hours,0) AS 加班小时, IFNULL(lv.days,0) AS 请假天数,",
        " IFNULL(tr.days,0) AS 出差天数, IFNULL(tr.allowance,0) AS 出差补贴",
        "FROM (SELECT employee_id AS eid, substr(IFNULL(ot_date,''),1,7) AS ym",
        "        FROM overtime WHERE status='已批准'",
        "      UNION SELECT employee_id, substr(IFNULL(start_date,''),1,7)",
        "        FROM leaves WHERE status='已批准'",
        "      UNION SELECT employee_id, substr(IFNULL(start_date,''),1,7)",
        "        FROM trips WHERE status='已批准') m",
        "LEFT JOIN users u ON m.eid=u.id",
        "LEFT JOIN (SELECT employee_id, substr(IFNULL(ot_date,''),1,7) ym,"
        " SUM(hours) hours FROM overtime WHERE status='已批准'",
        " GROUP BY employee_id, substr(IFNULL(ot_date,''),1,7)) ot",
        " ON ot.employee_id=m.eid AND ot.ym=m.ym",
        "LEFT JOIN (SELECT employee_id, substr(IFNULL(start_date,''),1,7) ym,"
        " SUM(days) days FROM leaves WHERE status='已批准'",
        " GROUP BY employee_id, substr(IFNULL(start_date,''),1,7)) lv",
        " ON lv.employee_id=m.eid AND lv.ym=m.ym",
        "LEFT JOIN (SELECT employee_id, substr(IFNULL(start_date,''),1,7) ym,"
        " SUM(days) days, SUM(IFNULL(allowance,0)) allowance FROM trips",
        " WHERE status='已批准' GROUP BY employee_id, substr(IFNULL(start_date,''),1,7)) tr",
        " ON tr.employee_id=m.eid AND tr.ym=m.ym",
        "WHERE u.id IS NOT NULL",
    ]
    sargs = []
    if ym:
        wsql.append(" AND m.ym = ?")
        sargs.append(ym)
    if uid:
        wsql.append(" AND m.eid = ?")
        sargs.append(uid)
    wsql.append(" ORDER BY m.ym DESC, 员工")
    rows = query(" ".join(wsql), sargs)
    head = ["员工", "月份", "加班小时", "请假天数", "出差天数", "出差补贴(元)"]
    data = [head] + [[r.get(c) for c in head] for r in rows]
    sheets.append(("出勤汇总", data, [14, 10, 11, 11, 11, 14]))

    # 2) 加班明细
    cond, args = w("overtime", "(? IS NULL OR substr(IFNULL(t.ot_date,''),1,7) = ?)", [ym, ym])
    rows = query(
        "SELECT t.code AS 加班单号, IFNULL(u.display_name,u.username) AS 员工,"
        " t.ot_date AS 加班日期, t.start_time AS 开始, t.end_time AS 结束,"
        " t.hours AS 加班小时, p.name AS 所属项目, t.reason AS 加班事由,"
        " IFNULL(t.status,'待审批') AS 审批状态"
        " FROM overtime t LEFT JOIN users u ON t.employee_id=u.id"
        " LEFT JOIN projects p ON t.project_id=p.id" + cond
        + " ORDER BY t.ot_date DESC, t.id DESC", args)
    head = ["加班单号", "员工", "加班日期", "开始", "结束", "加班小时",
            "所属项目", "加班事由", "审批状态"]
    sheets.append(("加班明细", [head] + [[r.get(c) for c in head] for r in rows],
                   [12, 10, 12, 8, 8, 10, 20, 24, 10]))

    # 3) 请假明细
    cond, args = w("leaves", "(? IS NULL OR substr(IFNULL(t.start_date,''),1,7) = ?)", [ym, ym])
    rows = query(
        "SELECT t.code AS 请假单号, IFNULL(u.display_name,u.username) AS 员工,"
        " t.leave_type AS 请假类型, t.start_date AS 开始日期, t.end_date AS 结束日期,"
        " t.days AS 请假天数, IFNULL(t.hours,0) AS 折合小时, t.reason AS 请假事由,"
        " IFNULL(t.status,'待审批') AS 审批状态"
        " FROM leaves t LEFT JOIN users u ON t.employee_id=u.id" + cond
        + " ORDER BY t.start_date DESC, t.id DESC", args)
    head = ["请假单号", "员工", "请假类型", "开始日期", "结束日期", "请假天数",
            "折合小时", "请假事由", "审批状态"]
    sheets.append(("请假明细", [head] + [[r.get(c) for c in head] for r in rows],
                   [12, 10, 10, 12, 12, 10, 10, 24, 10]))

    # 4) 出差明细
    cond, args = w("trips", "(? IS NULL OR substr(IFNULL(t.start_date,''),1,7) = ?)", [ym, ym])
    rows = query(
        "SELECT t.code AS 出差单号, IFNULL(u.display_name,u.username) AS 员工,"
        " t.destination AS 出差地点, t.start_date AS 出发日期,"
        " IFNULL(t.end_date,'未定') AS 回程日期, IFNULL(t.days,0) AS 出差天数,"
        " IFNULL(t.allowance_std,0) AS 每日补贴, IFNULL(t.allowance,0) AS 出差补贴,"
        " p.name AS 所属项目, IFNULL(t.status,'待审批') AS 审批状态"
        " FROM trips t LEFT JOIN users u ON t.employee_id=u.id"
        " LEFT JOIN projects p ON t.project_id=p.id" + cond
        + " ORDER BY t.start_date DESC, t.id DESC", args)
    head = ["出差单号", "员工", "出差地点", "出发日期", "回程日期", "出差天数",
            "每日补贴", "出差补贴", "所属项目", "审批状态"]
    sheets.append(("出差明细", [head] + [[r.get(c) for c in head] for r in rows],
                   [12, 10, 16, 12, 12, 10, 10, 10, 20, 10]))

    return sheets



# 表 -> 列名集合（PRAGMA 查一次就缓存；表结构运行期不会变）
_COLS_CACHE = {}


def _table_cols(tbl):
    if tbl not in _COLS_CACHE:
        _COLS_CACHE[tbl] = {r["name"] for r in query(f"PRAGMA table_info({tbl})")}
    return _COLS_CACHE[tbl]


def detail_specs(entity_key):
    """明细规格（列定义）。

    带 `dict_cat` 的列，选项从「下拉选项」字典表里取 —— 这样管理员在
    「基础资料 → 下拉选项」里加一条税点，明细下拉立刻就有。
    （原地写回，页面模板拿到的 ent.details 是同一份对象。）
    """
    specs = meta.ENTITIES.get(entity_key, {}).get("details") or []
    for spec in specs:
        for col in spec["cols"]:
            cat = col.get("dict_cat")
            if cat:
                vals = dict_options(cat)
                if vals:
                    col["options"] = vals
            # 默认值来自「参数设置」的列（如对私计算的默认扣税比例）：
            # 在这里解析一次，前端新增行才会带上这个默认值
            skey = col.get("default_setting")
            if skey:
                raw = setting(skey, "")
                if raw != "":
                    col["default"] = (pct_text(raw) if col["type"] == "pct"
                                      else str(raw))
        # 没显式声明 bulk 的，默认把「下拉列」都放上批量设置条 ——
        # 明细上方那条批量工具就是从这里渲染的
        if "bulk" not in spec:
            spec["bulk"] = [{"name": c["name"], "label": c["label"],
                             "type": "select", "options": c.get("options") or []}
                            for c in spec["cols"] if c["type"] == "select"]
    return specs


def detail_spec(entity_key, dkey="items"):
    for spec in detail_specs(entity_key):
        if spec["key"] == dkey:
            return spec
    return None


def detail_rows(entity_key, rid, dkey="items"):
    spec = detail_spec(entity_key, dkey)
    if not spec or not rid:
        return []
    # 不是每张明细表都有序号列（付款/费用的清单项就没有），没有就按 id 排
    order = "seq, id" if "seq" in _table_cols(spec["table"]) else "id"
    return query(f"SELECT * FROM {spec['table']} WHERE {spec['fk']} = ? "
                 f"ORDER BY {order}", (rid,))


def details_of(entity_key, rid):
    """{明细键: [行, ...]}"""
    return {spec["key"]: detail_rows(entity_key, rid, spec["key"])
            for spec in detail_specs(entity_key)}


def _to_num(v, default=0.0):
    return officedocs.to_float(v, default)


def parse_details(entity_key, form):
    """解析表单里的明细行（字段名 d_<key>_<列>，同名多值即多行）。"""
    out = {}
    for spec in detail_specs(entity_key):
        k = spec["key"]
        lists = {c["name"]: form.getlist(f"d_{k}_{c['name']}") for c in spec["cols"]}
        n = max([len(v) for v in lists.values()] or [0])
        rows = []
        for i in range(n):
            row, empty = {}, True
            for c in spec["cols"]:
                val = lists[c["name"]][i] if i < len(lists[c["name"]]) else ""
                row[c["name"]] = (val or "").strip()
                if row[c["name"]]:
                    empty = False
            if not empty:
                rows.append(row)
        out[k] = rows
    return out


def _detail_data(spec, row, seq):
    """一行明细 → 入库字典（类型转换、金额兜底、列联动、派生列、品牌型号入库）。"""
    data = {"seq": seq}
    calc = {}
    for c in spec["cols"]:
        name, t, raw = c["name"], c["type"], row.get(c["name"], "")
        if t in ("money", "number"):
            data[name] = round2(_to_num(raw))
        elif t == "contract":
            try:
                data[name] = int(raw) if str(raw).strip() else None
            except (TypeError, ValueError):
                data[name] = None
        elif t == "pct":
            # 手填的百分数（13 / "13" / "13%"）统一存成 "13%"；空着就是不扣
            data[name] = pct_text(raw)
        elif t == "contractitem":
            try:
                data[name] = int(raw) if str(raw).strip() else None
            except (TypeError, ValueError):
                data[name] = None
        else:
            data[name] = (raw or "").strip() or None
        if c.get("calc"):
            calc[name] = c["calc"]
    # 列联动（如 含税方式=不含税 → 税点钉成 0%）：服务端也执行一遍。
    # 前端已经做过，但旧页面 / 直接 POST 能绕过去，留下自相矛盾的明细。
    for c in spec["cols"]:
        target = (c.get("sets") or {}).get(data.get(c["name"]))
        if target:
            data.update(target)
    # 「数量 × 单价 = 金额」服务端再算一遍（前端算过的也以这里为准）
    for name, expr in calc.items():
        try:
            a, b = [x.strip() for x in expr.split("*")]
            data[name] = round2((data.get(a) or 0) * (data.get(b) or 0))
        except ValueError:
            continue
    # 派生列（扣税 / 应付居间人）：按列声明重算，绝不信表单里传来的值
    for c in spec["cols"]:
        if c.get("expr"):
            data[c["name"]] = eval_expr(c["expr"], data)
    # 合同清单项：存一份名称快照，清单改了历史单据也看得懂；
    # 顺便把「所属合同」归位 —— 清单项属于哪张合同就写哪张，
    # 免得行上的合同和清单项对不上（表头/费用参考面板会跟着算错）
    cid = data.get("contract_item_id")
    if "contract_item_id" in data and cid:
        it = one("SELECT name, brand, model, contract_id, amount FROM contract_items "
                 "WHERE id = ?", (cid,))
        if it:
            data["name"] = it["name"] or data.get("name")
            if "row_contract_id" in data and it["contract_id"]:
                data["row_contract_id"] = it["contract_id"]
    # 声明了 amount_from_item 的明细（费用支出）：本次金额没填（空/0）就取清单项金额，
    # 别存成 0（2026-09-18 用户要求；付款那边不声明，一笔付款常常只核销一部分）
    if spec.get("amount_from_item") and cid and not data.get("amount"):
        it2 = one("SELECT amount FROM contract_items WHERE id = ?", (cid,))
        if it2 and it2["amount"]:
            data["amount"] = round2(it2["amount"])
    if spec.get("library"):
        bid, mid = _lib_upsert(row, data)
        cols = _table_cols(spec["table"])
        if "brand_id" in cols:
            data["brand_id"] = bid
        if "model_id" in cols:
            data["model_id"] = mid
    return data


def _lib_upsert(row, data):
    """把明细里的品牌 / 型号（带参数、单位、参考单价）自动收进品牌型号库。

    只补空值，不覆盖人工维护过的参数；下次录单就能直接下拉选了。
    """
    brand = (row.get("brand") or "").strip()
    model = (row.get("model") or "").strip()
    bid = mid = None
    if brand:
        b = one("SELECT id FROM brands WHERE name = ?", (brand,))
        bid = b["id"] if b else execute("INSERT INTO brands (name) VALUES (?)", (brand,))
    elif data.get("brand_id"):
        bid = data["brand_id"]
    if model and bid:
        spec_txt = (row.get("spec") or "").strip()
        unit = (row.get("unit") or "").strip()
        price = _to_num(row.get("price")) if "price" in row else 0
        m = one("SELECT id, spec, unit, price FROM models WHERE brand_id = ? "
                "AND name = ?", (bid, model))
        if not m:
            mid = execute("INSERT INTO models (brand_id, name, spec, unit, price) "
                          "VALUES (?,?,?,?,?)",
                          (bid, model, spec_txt or None, unit or None, price))
        else:
            mid = m["id"]
            sets, args = [], []
            if spec_txt and not (m["spec"] or "").strip():
                sets.append("spec = ?")
                args.append(spec_txt)
            if unit and not (m["unit"] or "").strip():
                sets.append("unit = ?")
                args.append(unit)
            if price and not (m["price"] or 0):
                sets.append("price = ?")
                args.append(price)
            if sets:
                execute("UPDATE models SET " + ", ".join(sets) + " WHERE id = ?",
                        args + [mid])
    return bid, mid


def save_details(entity_key, rid, rows_by_key):
    """整块提交明细。

    合同清单比较特殊：它被费用明细 / 付款明细 / 送货明细 / 对私明细
    用 contract_item_id 引用，整表 DELETE 会撞外键（以前一改合同清单就 500）。
    所以合同清单改成**按身份增量保存**：品名 + 型号 + 参数没变的行原地 UPDATE
    （id 不变，引用不丢），新增的行 INSERT，删掉的行先把引用置空再删。
    其他明细表仍按整表替换。
    """
    for spec in detail_specs(entity_key):
        rows = rows_by_key.get(spec["key"]) or []
        if spec["table"] == "contract_items":
            _save_contract_items_incremental(rid, spec, rows)
            continue
        execute(f"DELETE FROM {spec['table']} WHERE {spec['fk']} = ?", (rid,))
        _insert_details(spec, rid, rows)


def _insert_details(spec, rid, rows):
    cols = _table_cols(spec["table"])
    for i, row in enumerate(rows, 1):
        data = _detail_data(spec, row, i)
        data[spec["fk"]] = rid
        data = {k: v for k, v in data.items() if k in cols}
        keys = list(data.keys())
        execute(f"INSERT INTO {spec['table']} ({', '.join(keys)}) "
                f"VALUES ({', '.join('?' * len(keys))})",
                [data[k] for k in keys])


# 引用了合同清单项的表：删清单项前先把引用置空，别撞外键
_CITEM_REFS = ("cost_items", "payment_items", "delivery_items",
               "private_settlement_items")


def _save_contract_items_incremental(rid, spec, rows):
    """合同清单按「品名 + 型号 + 参数」身份增量保存 —— id 稳定，引用不丢。"""
    cols = _table_cols("contract_items")
    old = query("SELECT id, name, model, spec FROM contract_items "
                "WHERE contract_id = ? ORDER BY seq, id", (rid,))
    pool = {}
    for r in old:
        sig = ((r["name"] or "").strip(), (r["model"] or "").strip(),
               (r["spec"] or "").strip())
        pool.setdefault(sig, []).append(r["id"])
    keep = set()
    for i, row in enumerate(rows, 1):
        data = _detail_data(spec, row, i)
        data = {k: v for k, v in data.items() if k in cols}
        sig = ((data.get("name") or "").strip(),
               (data.get("model") or "").strip(),
               (data.get("spec") or "").strip())
        ids = pool.get(sig) or []
        if ids:
            cid = ids.pop(0)
            sets = ", ".join(f"{k} = ?" for k in data if k != "id")
            execute(f"UPDATE contract_items SET {sets} WHERE id = ?",
                    list(data.values()) + [cid])
            keep.add(cid)
        else:
            data["contract_id"] = rid
            keys = list(data.keys())
            keep.add(execute(f"INSERT INTO contract_items ({', '.join(keys)}) "
                             f"VALUES ({', '.join('?' * len(keys))})",
                             [data[k] for k in keys]))
    drop = [r["id"] for r in old if r["id"] not in keep]
    if drop:
        ph = ",".join("?" * len(drop))
        for t in _CITEM_REFS:
            try:
                execute(f"UPDATE {t} SET contract_item_id = NULL "
                        f"WHERE contract_item_id IN ({ph})", drop)
            except sqlite3.OperationalError:
                pass                     # 老库可能没有这张表
        execute(f"DELETE FROM contract_items WHERE id IN ({ph})", drop)


# 派生列公式只放行这几类 AST 节点（数字 / 列名 / pct(列名) / 四则运算）
_EXPR_NODES = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Add, ast.Sub, ast.Mult,
               ast.Div, ast.USub, ast.UAdd, ast.Constant, ast.Call, ast.Name,
               ast.Load)


def pct_value(v):
    """把比例换算成小数：'13%' 与 '13' 都算 13 个百分点 → 0.13（空值给 0）。

    ⚠️ **统一按百分点**：填 0.5 就是 0.5%（不是 50%）。
    扣税比例是手工填的百分数，两种解释混在一起，一旦读错就是 100 倍的金额差，
    所以只认一种：带 % 的去掉 %，不带 % 的也当百分点，一律 ÷100。
    """
    if v is None:
        return 0.0
    txt = str(v).strip().replace("％", "%")
    if not txt:
        return 0.0
    if txt.endswith("%"):
        txt = txt[:-1].strip()
    return _to_num(txt) / 100.0


def pct_text(v, default=None):
    """把用户填的比例规范成 "13%" 这种文本（入库用）。

    输入可以是 13 / "13" / "13%" / "12.5 %"。空值返回 default（默认 None＝不扣）。
    统一存成带 % 的文本，读出来就不会再有「0.5 是 0.5% 还是 50%」的歧义。
    """
    if v is None or str(v).strip() == "":
        return default
    txt = str(v).strip().replace("％", "%")
    if txt.endswith("%"):
        txt = txt[:-1].strip()
    return "%g%%" % round2(_to_num(txt))


def pct_num(v):
    """入库的 "13%" → 输入框里显示的 13（number 输入框里不能带 %）。"""
    if v is None or str(v).strip() == "":
        return ""
    txt = str(v).strip().replace("％", "%").rstrip("%").strip()
    return "%g" % round2(_to_num(txt))


def eval_expr(expr, values):
    """算明细里的派生列（扣税 / 应付居间人）。

    表达式写在 meta 的 `col["expr"]` 里，是**配置**不是用户输入；即便如此也只放行
    白名单节点，`pct()` 以外什么都调不到。算不出来时返回 0 并往 stderr 打一行，
    不让一张单子把整页打 500（配置错误要在测试里抓，不能在线上炸）。
    """
    env = dict(values or {})
    env["pct"] = pct_value
    try:
        tree = ast.parse(expr, mode="eval")
        for node in ast.walk(tree):
            if not isinstance(node, _EXPR_NODES):
                raise ValueError("表达式里有不允许的内容：%s" % expr)
            if isinstance(node, ast.Call) and not (
                    isinstance(node.func, ast.Name) and node.func.id == "pct"
                    and len(node.args) == 1):
                raise ValueError("只允许 pct(列名)：%s" % expr)
            if isinstance(node, ast.Name) and node.id not in env:
                raise ValueError("表达式引用了不存在的列 %s" % node.id)
        # AST 已经在上面逐节点白名单校验过（只允许常量/列名/pct()/四则），
        # 且 __builtins__ 清空，所以这里的 eval 不会执行任意代码。
        return round2(eval(compile(tree, "<expr>", "eval"),  # nosec B307
                           {"__builtins__": {}}, env))
    except Exception as e:          # noqa: BLE001 —— 兜底不让脏表达式打挂页面
        print("[expr] 算不出来 %r：%s" % (expr, e), file=sys.stderr)
        return 0.0


def row_amount(spec, row):
    """按列定义算出这一行的金额。

    金额列是「数量 × 单价」自动算出来的（前端也是 readonly），
    所以校验合计时**不能拿表单里传来的原值**——一律按公式重算。
    """
    col = next((c for c in spec["cols"] if c.get("calc")), None)
    if not col:
        return _to_num(row.get("amount"))
    try:
        a, b = [x.strip() for x in col["calc"].split("*")]
    except ValueError:
        return _to_num(row.get("amount"))
    return round2(_to_num(row.get(a)) * _to_num(row.get(b)))


def detail_sum(entity_key, rows, dkey="items", colname="amount"):
    """明细合计（按公式重算，不看表单原值）。"""
    spec = detail_spec(entity_key, dkey)
    if not spec:
        return 0.0
    if colname == "amount":
        return round2(sum(row_amount(spec, r) for r in rows))
    return round2(sum(_to_num(r.get(colname)) for r in rows))


def detail_error(entity_key, rows_by_key, master=None):
    """明细的业务校验（返回错误文案列表）。"""
    errs = []
    master = master or {}
    for spec in detail_specs(entity_key):
        rows = rows_by_key.get(spec["key"]) or []
        if not rows:
            continue
        for i, row in enumerate(rows, 1):
            if spec.get("library") and not (row.get("name") or "").strip():
                errs.append(f"第 {i} 行【品名】不能为空")
            if "contract_item_id" in row and not row["contract_item_id"]:
                if spec.get("item_optional"):
                    # 对私计算：允许「纯成本行」——不挂清单项，自己填名称和成本
                    if not (row.get("name") or "").strip():
                        errs.append(f"第 {i} 行请选择合同清单项，"
                                    "或者填上名称（纯成本行）")
                else:
                    errs.append(f"第 {i} 行请选择对应的合同清单项")
        # 明细金额合计不得超过主表金额（付款 / 费用）
        if spec.get("sum") and spec.get("source_field") and master.get("amount"):
            tot = 0.0
            for row in rows:
                for col in spec["sum"]:
                    # 金额同样按公式/数值口径取，不用表单原值
                    tot += (row_amount(spec, row) if col == "amount"
                            else _to_num(row.get(col)))
            if tot > round2(_to_num(master.get("amount"))) + TOL:
                errs.append("清单项金额合计 %s 超过单据金额 %s"
                            % (money(tot), money(master.get("amount"))))
    return errs


def detail_totals(entity_key, rows_by_key, dkey="items"):
    spec = detail_spec(entity_key, dkey)
    tot = {}
    if not spec:
        return tot
    for col in spec.get("sum") or []:
        tot[col] = round2(sum(_to_num(r.get(col)) for r in rows_by_key.get(dkey) or []))
    return tot


# ------------------------------------------------------------ 下拉数据源
def brand_list():
    return query("SELECT id, name FROM brands ORDER BY name")


def model_list():
    """型号 + 品牌 + 参数，供清单里的型号下拉用（选中后自动带出参数/单位/单价）。"""
    return query("""
        SELECT m.id, m.name, m.spec, m.unit, m.price,
               IFNULL(b.name,'') AS brand
        FROM models m LEFT JOIN brands b ON m.brand_id = b.id
        ORDER BY b.name, m.name""")


def unit_names():
    return dict_options("计量单位")


def contract_item_groups():
    """清单项按合同分组：对私计算的【对应合同清单项】下拉用。

    一个对私单可以关联多张合同，所以下拉要分组显示，
    否则几十项混在一起根本认不出哪项是哪张合同的。
    """
    groups, idx = [], {}
    for it in contract_item_list():
        k = it["contract_id"]
        if k not in idx:
            code = (it.get("contract_code") or "").strip()
            name = (it.get("contract_name") or "").strip()
            label = ("%s %s" % (code, name)).strip() or ("合同 %s" % k)
            idx[k] = {"contract_id": k, "label": label, "items": []}
            groups.append(idx[k])
        idx[k]["items"].append({"id": it["id"], "label": it["label"],
                                "contract_id": it["contract_id"]})
    return groups


def contract_item_list():
    """全部合同清单项（前端按主表选中的合同过滤）。"""
    rows = query("""
        SELECT i.id, i.contract_id, i.seq, i.name, i.brand, i.model, i.spec,
               i.unit, i.qty, i.amount, i.tax_included, i.tax_rate,
               IFNULL(h.code,'') AS contract_code, IFNULL(h.name,'') AS contract_name
        FROM contract_items i
        LEFT JOIN contracts h ON i.contract_id = h.id
        ORDER BY i.contract_id, i.seq, i.id""")
    out = []
    for r in rows:
        bits = [r["name"] or "", r["brand"] or "", r["model"] or ""]
        tail = []
        if r["spec"]:
            tail.append(r["spec"])
        if r["qty"]:
            tail.append("%g%s" % (round2(r["qty"]), r["unit"] or ""))
        if r["amount"]:
            tail.append(money(r["amount"]))
        label = " ".join(x for x in bits if x)
        if tail:
            label += "（" + " · ".join(tail) + "）"
        out.append({"id": r["id"], "contract_id": r["contract_id"],
                    "label": "第%s项 %s" % (r["seq"] or 0, label),
                    "name": r["name"], "qty": r["qty"], "unit": r["unit"],
                    "amount": r["amount"], "brand": r["brand"], "model": r["model"],
                    "spec": r["spec"],
                    # 合同编号/名称：前端把清单项按合同分组要用（一个单子可能挂多张合同）
                    "contract_code": r["contract_code"],
                    "contract_name": r["contract_name"],
                    # 供「对私计算」选中清单项时自动带出
                    "tax_included": r["tax_included"], "tax_rate": r["tax_rate"]})
    return out


# ------------------------------------------------------------ 报价单
def quotation_recalc(qid):
    """按明细重算报价单的「明细合计 / 报价总额」。"""
    total = round2(scalar("SELECT IFNULL(SUM(amount),0) FROM quotation_items "
                          "WHERE quotation_id = ?", (qid,)))
    q = one("SELECT discount FROM quotations WHERE id = ?", (qid,)) or {}
    disc = round2(q.get("discount") or 0)
    final = round2(total - disc)
    execute("UPDATE quotations SET amount_total = ?, amount_final = ?, updated_at = ? "
            "WHERE id = ?", (total, final, now_str(), qid))
    return {"amount_total": total, "discount": disc, "amount_final": final}


def quotation_can_convert(q, uid, role):
    """谁能把报价转成合同：管理员、财务，或本单的经手员工。"""
    if role in ROLE_APPROVERS:
        return True
    return bool(uid) and q.get("handler_user_id") == uid


def quotation_to_contract(qid, uid=None):
    """报价单 → 合同：金额与清单一起带过去，回写报价状态。"""
    q = one("SELECT * FROM quotations WHERE id = ?", (qid,))
    if not q:
        raise ValueError("报价单不存在")
    if q["contract_id"]:
        h = one("SELECT code FROM contracts WHERE id = ?", (q["contract_id"],))
        raise ValueError("这份报价已经转成合同 %s，不用重复转"
                         % ((h or {}).get("code") or ""))
    items = query("SELECT * FROM quotation_items WHERE quotation_id = ? "
                  "ORDER BY seq, id", (qid,))
    if not items:
        raise ValueError("报价单里还没有明细，先添加清单再转合同")
    cid = execute(
        "INSERT INTO contracts (code, name, project_id, unit_id, direction, "
        "sign_date, amount_gross, status, handler, memo) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (gen_code("contracts"), q["title"] or ("由报价单 %s 转入" % q["code"]),
         q["project_id"], q["unit_id"], q["direction"] or "收入合同",
         now_str()[:10], q["amount_final"] or 0, "草拟", q["handler"],
         "由报价单 %s 转入" % q["code"]))
    # 按两张表「共有的列」复制，不写死列名 —— 以前写死，结果后加的
    # tax_included / tax_rate 就没带过去，合同清单的含税口径全是空的。
    src_cols = _table_cols("quotation_items")
    dst_cols = _table_cols("contract_items")
    copy_cols = [c for c in src_cols
                 if c in dst_cols and c not in ("id", "contract_id", "quotation_id")]
    for it in items:
        execute("INSERT INTO contract_items (contract_id, %s) VALUES (%s)" % (
            ", ".join(copy_cols), ", ".join("?" * (len(copy_cols) + 1))),
            [cid] + [it[c] for c in copy_cols])
    execute("UPDATE quotations SET status = '已转合同', contract_id = ?, "
            "converted_at = ? WHERE id = ?", (cid, now_str(), qid))
    return cid


# ------------------------------------------------------------ 送货单
def delivery_prefill(contract_id):
    """从合同带出送货明细：数量默认 = 合同数量 − 已送数量（送完的项不出现）。"""
    rows = query("""
        SELECT i.id, i.seq, i.name, i.brand, i.model, i.spec, i.unit, i.qty,
               IFNULL((SELECT SUM(d.qty) FROM delivery_items d
                       WHERE d.contract_item_id = i.id), 0) AS sent
        FROM contract_items i WHERE i.contract_id = ?
        ORDER BY i.seq, i.id""", (contract_id,))
    out = []
    for r in rows:
        left = round2((r["qty"] or 0) - (r["sent"] or 0))
        if left <= TOL:
            continue
        out.append({"contract_item_id": r["id"], "name": r["name"],
                    "brand": r["brand"], "model": r["model"], "spec": r["spec"],
                    "unit": r["unit"], "qty": left, "remark": ""})
    if not out and rows:
        out = [{"contract_item_id": r["id"], "name": r["name"], "brand": r["brand"],
                "model": r["model"], "spec": r["spec"], "unit": r["unit"],
                "qty": 0, "remark": "已送完"} for r in rows]
    return out


def set_entity_field(key, rid, field, value):
    """列表页内联改一个字段（目前给「报价状态」用），改完立即生效。"""
    ent = meta.ENTITIES.get(key) or {}
    allowed = ent.get("inline") or []
    if field not in allowed:
        raise ValueError("这个字段不支持在列表里直接修改")
    if field not in _table_cols(ent["table"]):
        raise ValueError("字段不存在：%s" % field)
    # 下拉类字段：只接受字典里有的值，挡住伪造的 POST
    spec = next((f for f in (ent.get("fields") or []) if f["name"] == field), None)
    cat = (spec or {}).get("dict_cat")
    if cat:
        opts = dict_options(cat)
        if opts and value and value not in opts:
            raise ValueError("「%s」不是可选项" % value)
    execute("UPDATE %s SET %s = ? WHERE id = ?" % (ent["table"], field),
            (value or None, rid))
    return value


def delivery_pick_rows(contract_id):
    """合同清单的送货候选行：带合同数量 / 已送数量 / 还能送多少。"""
    rows = query("""
        SELECT i.id, i.seq, i.name, i.brand, i.model, i.spec, i.unit, i.qty,
               IFNULL((SELECT SUM(d.qty) FROM delivery_items d
                       WHERE d.contract_item_id = i.id), 0) AS sent
        FROM contract_items i WHERE i.contract_id = ?
        ORDER BY i.seq, i.id""", (contract_id,))
    out = []
    for r in rows:
        sent = round2(r["sent"] or 0)
        left = round2((r["qty"] or 0) - sent)
        out.append({"id": r["id"], "seq": r["seq"], "name": r["name"] or "",
                    "brand": r["brand"] or "", "model": r["model"] or "",
                    "spec": r["spec"] or "", "unit": r["unit"] or "",
                    "qty": round2(r["qty"] or 0), "sent": sent,
                    "left": left if left > TOL else 0.0})
    return out


def create_delivery_from_contract(contract_id, picked, header=None):
    """按勾选的合同清单项生成一张送货单，返回新送货单 id。

    picked: [(清单项 dict, 本次数量), ...]（已按剩余量封顶）
    header: 收货人 / 电话 / 地址 / 送货日期 / 运输方式 / 备注 / 经办人
    """
    c = contract_summary(contract_id)
    if not c:
        raise ValueError("合同不存在")
    h = dict(header or {})
    did = execute(
        "INSERT INTO deliveries (code, contract_id, project_id, unit_id, receiver,"
        " phone, address, deliver_date, carrier, status, handler, memo, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (h.get("code") or gen_code("deliveries"), contract_id,
         c.get("project_id"), c.get("unit_id"), h.get("receiver") or "",
         h.get("phone") or "", h.get("address") or "",
         h.get("deliver_date") or now_str()[:10], h.get("carrier") or "",
         h.get("status") or "待发货", h.get("handler") or "", h.get("memo") or "",
         now_str()))
    for i, (it, qty) in enumerate(picked, 1):
        execute("INSERT INTO delivery_items (delivery_id, seq, contract_item_id,"
                " name, brand, model, spec, unit, qty, remark) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (did, i, it["id"], it["name"], it["brand"], it["model"],
                 it["spec"], it["unit"], round2(qty), ""))
    return did


# ------------------------------------------------------------ 对私计算
def settlement_contract_ids(rid):
    """这个对私单关联的合同 id 列表（按勾选顺序）。"""
    if not rid:
        return []
    return [int(r["contract_id"]) for r in query(
        "SELECT contract_id FROM private_settlement_contracts "
        "WHERE settlement_id = ? ORDER BY sort, id", (rid,))]


def save_settlement_contracts(rid, ids):
    """保存「关联合同」（可多选），并把明细里用到的合同一并纳入。

    返回 (最终合同 id 列表, 被自动补上的合同编号列表)。

    为什么要把明细里的合同并进来：明细行写了某张合同、表头却没勾它，
    那是表头在说谎（费用支出参考面板会漏算那张合同的费用），
    所以自动补上、并在页面上说一句，而不是默默不一致。
    """
    if not rid:
        return [], []
    want = []
    for x in (ids or []):
        s = str(x).strip()
        if s.isdigit() and int(s) not in want:
            want.append(int(s))
    used = []
    for r in query("""SELECT DISTINCT row_contract_id AS cid
                      FROM private_settlement_items
                      WHERE settlement_id = ? AND row_contract_id IS NOT NULL""",
                   (rid,)):
        cid = int(r["cid"] or 0)
        if cid and cid not in want:
            used.append(cid)
    final = want + used
    execute("DELETE FROM private_settlement_contracts WHERE settlement_id = ?", (rid,))
    for i, cid in enumerate(final):
        execute("INSERT OR IGNORE INTO private_settlement_contracts "
                "(settlement_id, contract_id, sort) VALUES (?,?,?)",
                (rid, cid, (i + 1) * 10))
    # 兼容：contract_id 保留成第一张（列表列、老报表还认这一列）
    execute("UPDATE private_settlements SET contract_id = ? WHERE id = ?",
            (final[0] if final else None, rid))
    added = []
    for cid in used:
        r = one("SELECT code FROM contracts WHERE id = ?", (cid,))
        if r and r["code"]:
            added.append(r["code"])
    return final, added


def profit_summary(date_from=None, date_to=None, status=None):
    """对私结算的「利润」汇总：收入 / 我方利润 / 成本 / 返还居间人。

    ⚠️ 对私计算里的「扣税」是名义叫法，实际就是我方利润（amount_tax）——
    这一页直接按利润口径展示，别再让用户自己换算。
    """
    where, params = [], []
    if date_from:
        where.append("d.calc_date >= ?")
        params.append(date_from)
    if date_to:
        where.append("d.calc_date <= ?")
        params.append(date_to)
    if status:
        where.append("d.status = ?")
        params.append(status)
    else:
        where.append("d.status <> '已作废'")      # 默认不看废单
    w = " WHERE " + " AND ".join(where)
    args = tuple(params)
    money_cols = ("income", "profit", "cost", "payable")

    def norm(rows):
        for r in rows:
            if "gname" in r:                 # 分组别名 → 统一叫 name
                r["name"] = r.pop("gname")
            for k in money_cols:
                r[k] = round2(r.get(k))
            r["n"] = int(r.get("n") or 0)
        return rows

    tot = one("""SELECT COUNT(*) AS n,
                        IFNULL(SUM(d.amount_income),0)  AS income,
                        IFNULL(SUM(d.amount_tax),0)     AS profit,
                        IFNULL(SUM(d.amount_cost),0)    AS cost,
                        IFNULL(SUM(d.amount_payable),0) AS payable,
                        IFNULL(SUM(CASE WHEN d.status = '已支付' THEN d.amount_payable
                                        ELSE 0 END), 0) AS paid,
                        IFNULL(SUM(CASE WHEN d.status <> '已支付' THEN d.amount_payable
                                        ELSE 0 END), 0) AS unpaid
                 FROM private_settlements d""" + w, args) or {}
    for k in money_cols + ("paid", "unpaid"):
        tot[k] = round2(tot.get(k))
    tot["n"] = int(tot.get("n") or 0)

    rows = norm(query("""SELECT d.id, d.code, d.title, d.calc_date, d.status,
                                d.broker, d.amount_income, d.amount_tax,
                                d.amount_cost, d.amount_payable,
                                IFNULL(p.name,'') AS project_name,
                                (SELECT GROUP_CONCAT(c.code, '、')
                                   FROM private_settlement_contracts pc
                                   JOIN contracts c ON pc.contract_id = c.id
                                  WHERE pc.settlement_id = d.id) AS contracts
                         FROM private_settlements d
                         LEFT JOIN projects p ON d.project_id = p.id""" + w +
                      " ORDER BY d.calc_date DESC, d.id DESC", args))

    by_broker = norm(query("""SELECT IFNULL(NULLIF(TRIM(d.broker),''), '（未填居间人）') AS gname,
                                    COUNT(*) AS n,
                                    IFNULL(SUM(d.amount_income),0)  AS income,
                                    IFNULL(SUM(d.amount_tax),0)     AS profit,
                                    IFNULL(SUM(d.amount_cost),0)    AS cost,
                                    IFNULL(SUM(d.amount_payable),0) AS payable
                             FROM private_settlements d""" + w +
                          " GROUP BY gname ORDER BY payable DESC, gname", args))

    by_project = norm(query("""SELECT IFNULL(p.name, '（未指定项目）') AS gname,
                                     COUNT(*) AS n,
                                     IFNULL(SUM(d.amount_income),0)  AS income,
                                     IFNULL(SUM(d.amount_tax),0)     AS profit,
                                     IFNULL(SUM(d.amount_cost),0)    AS cost,
                                     IFNULL(SUM(d.amount_payable),0) AS payable
                              FROM private_settlements d
                              LEFT JOIN projects p ON d.project_id = p.id""" + w +
                           " GROUP BY gname ORDER BY payable DESC, gname", args))

    # 按合同要下到明细行：一张单可能同时关联多张合同，单据级金额没法拆；
    # 而每个明细行都写了【所属合同】，按行汇总才既准确又不重复。
    by_contract = norm(query("""SELECT IFNULL(c.code, '（共同成本/未指定合同）') AS gname,
                                      COUNT(DISTINCT d.id) AS n,
                                      IFNULL(SUM(i.income),0)      AS income,
                                      IFNULL(SUM(i.tax_amount),0)  AS profit,
                                      IFNULL(SUM(i.cost_amount),0) AS cost,
                                      IFNULL(SUM(i.payable),0)     AS payable
                               FROM private_settlement_items i
                               JOIN private_settlements d ON i.settlement_id = d.id
                               LEFT JOIN contracts c ON i.row_contract_id = c.id""" + w +
                            " GROUP BY gname ORDER BY payable DESC, gname", args))

    return {"totals": tot, "rows": rows, "by_broker": by_broker,
            "by_project": by_project, "by_contract": by_contract,
            "date_from": date_from or "", "date_to": date_to or "",
            "status": status or ""}


def add_months(d, months):
    """在日期上加减整月；目标月没有这一天时取月末（1-31 + 1 月 → 2-28/29）。"""
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    day = min(d.day, calendar.monthrange(y, m)[1])
    return datetime(y, m, day)


def warranty_end(start, months):
    """质保到期日 = 质保开始 + 保修期限（月）。算不出返回 None。

    留空 / 月数非正数都当"没设质保"处理，别在页面上显示一个假日期。
    """
    s = str(start or "").strip()
    try:
        d = datetime.strptime(s[:10], "%Y-%m-%d")
    except ValueError:
        return None
    try:
        m = int(round(float(months or 0)))
    except (TypeError, ValueError):
        return None
    if m <= 0:
        return None
    return add_months(d, m).strftime("%Y-%m-%d")


def warranty_expire_days(end):
    """距离质保到期还有多少天（已过期为负数）；算不出返回 None。"""
    try:
        return (datetime.strptime(str(end)[:10], "%Y-%m-%d") - datetime.now()).days
    except (TypeError, ValueError):
        return None


def contract_profile(rid):
    """合同全景：基本信息 + 开票 + 收付款 + 清单（含已送/已登记费用）+ 对私利润。

    一个入口把"这张合同现在到底怎么样了"讲完：开了多少票、收了多少钱、
    送了多少货、登记了多少费用、我方利润和还要给居间人多少。
    """
    c = one("""SELECT h.*, p.name AS project_name, u.name AS unit_name
               FROM contracts h
               LEFT JOIN projects p ON h.project_id = p.id
               LEFT JOIN units u ON h.unit_id = u.id
               WHERE h.id = ?""", (rid,))
    if not c:
        raise ValueError("合同不存在")
    # 质保到期日与剩余天数（页面直接显示，不在模板里算）
    c["warranty_end"] = warranty_end(c.get("warranty_start"), c.get("warranty_months"))
    c["warranty_expire_days"] = warranty_expire_days(c["warranty_end"])
    amount = round2(c.get("amount_gross"))
    is_income = (c.get("direction") or "") == "收入合同"

    invoices = query("""SELECT x.id, x.no, x.code, x.type, x.issue_date,
                               x.amount_gross, x.tax_rate, x.status,
                               IFNULL((SELECT SUM(s.amount) FROM settlements s
                                       WHERE s.invoice_id = x.id), 0) AS settled
                        FROM invoices x WHERE x.contract_id = ?
                        ORDER BY x.issue_date, x.id""", (rid,))
    for r in invoices:
        r["amount_gross"] = round2(r["amount_gross"])
        r["settled"] = round2(r["settled"])
        r["open"] = round2(r["amount_gross"] - r["settled"])
    inv_gross = round2(sum(r["amount_gross"] for r in invoices))
    inv_settled = round2(sum(r["settled"] for r in invoices))
    inv_open_n = sum(1 for r in invoices if r["open"] > TOL)

    pays = query("""SELECT id, code, pay_date, direction, nature, amount,
                           pay_method, handler, memo
                    FROM payments WHERE contract_id = ?
                    ORDER BY pay_date, id""", (rid,))
    for r in pays:
        r["amount"] = round2(r["amount"])
    pay_total = round2(sum(r["amount"] for r in pays))
    pay_in = round2(sum(r["amount"] for r in pays if (r["direction"] or "") == "收入合同"))
    pay_out = round2(sum(r["amount"] for r in pays if (r["direction"] or "") == "支出合同"))

    items = query("""SELECT i.id, i.seq, i.name, i.brand, i.model, i.spec, i.unit,
                            i.qty, i.price, i.amount,
                            IFNULL((SELECT SUM(d.qty) FROM delivery_items d
                                    WHERE d.contract_item_id = i.id), 0) AS sent,
                            IFNULL((SELECT SUM(ci.amount) FROM cost_items ci
                                    WHERE ci.contract_item_id = i.id), 0) AS cost
                     FROM contract_items i WHERE i.contract_id = ?
                     ORDER BY i.seq, i.id""", (rid,))
    for r in items:
        r["qty"] = round2(r["qty"])
        r["price"] = round2(r["price"])
        r["amount"] = round2(r["amount"])
        r["sent"] = round2(r["sent"])
        r["left"] = round2((r["qty"] or 0) - (r["sent"] or 0))
        r["cost"] = round2(r["cost"])
    item_amount = round2(sum(r["amount"] for r in items))
    item_cost = round2(sum(r["cost"] for r in items))

    priv = query("""SELECT d.id, d.code, d.title, d.calc_date, d.broker, d.status,
                           d.amount_income, d.amount_tax, d.amount_cost,
                           d.amount_payable
                    FROM private_settlements d
                    WHERE d.id IN (SELECT pc.settlement_id
                                     FROM private_settlement_contracts pc
                                    WHERE pc.contract_id = ?)
                    ORDER BY d.calc_date, d.id""", (rid,))
    for r in priv:
        for k in ("amount_income", "amount_tax", "amount_cost", "amount_payable"):
            r[k] = round2(r[k])
    p_income = round2(sum(r["amount_income"] for r in priv))
    p_profit = round2(sum(r["amount_tax"] for r in priv))
    p_cost = round2(sum(r["amount_cost"] for r in priv))
    p_payable = round2(sum(r["amount_payable"] for r in priv))

    return {
        "c": c, "is_income": is_income, "amount": amount,
        "invoices": invoices, "inv_gross": inv_gross, "inv_settled": inv_settled,
        "inv_open": round2(inv_gross - inv_settled), "inv_open_n": inv_open_n,
        "pays": pays, "pay_total": pay_total, "pay_in": pay_in, "pay_out": pay_out,
        # 键名别叫 items：Jinja 里 p.items 拿到的是字典的方法，不是这个列表
        "lines": items, "item_amount": item_amount, "item_cost": item_cost,
        "priv": priv, "p_income": p_income, "p_profit": p_profit,
        "p_cost": p_cost, "p_payable": p_payable,
        "deliveries": deliveries_of_contract(rid),
        "files": attachments_for("contracts", rid),
        # 粗略毛利：合同金额 − 已登记费用（明细口径的费用才算，供参考）
        "gross": round2(amount - item_cost),
    }


def project_profile(pid):
    """一张项目的全景：合同、销项 / 进项发票、收付款、费用（按类别）与利润。

    首页「按项目经营情况」点项目名、以及项目管理列表点项目编号都进这里。
    合计口径与首页那张表一致（收入合同额 / 累计收款 / 费用合计 / 销项已开票 /
    预计毛利），这里再把明细拆开展示。
    """
    p = one("""SELECT p.*, u.display_name AS manager_name,
                      u.username AS manager_user
               FROM projects p LEFT JOIN users u ON p.manager_user_id = u.id
               WHERE p.id = ?""", (pid,))
    if not p:
        raise ValueError("项目不存在")

    # ---------------------------------------------------------------- 合同
    contracts = query(
        "SELECT c.id, c.code, c.name, c.direction, c.category, c.amount_gross, "
        "       c.status, c.sign_date, u.name AS unit_name "
        "FROM contracts c LEFT JOIN units u ON c.unit_id = u.id "
        "WHERE c.project_id = ? ORDER BY c.code", (pid,))
    for c in contracts:
        c["amount_gross"] = round2(c.get("amount_gross"))
    income = round2(sum(c["amount_gross"] for c in contracts
                        if (c.get("direction") or "") == "收入合同"))
    contract_cost = round2(sum(c["amount_gross"] for c in contracts
                               if (c.get("direction") or "") == "支出合同"))

    # ---------------------------------------------------------------- 发票
    def _invoices(cond):
        rows = query(
            "SELECT v.*, cc.name AS category_name, un.name AS unit_name, "
            "  IFNULL((SELECT SUM(s.amount) FROM settlements s "
            "          WHERE s.invoice_id = v.id), 0) AS settled "
            "FROM invoices v LEFT JOIN cost_categories cc ON v.category = cc.id "
            "LEFT JOIN units un ON v.unit_id = un.id "
            "WHERE v.project_id = ? AND " + cond +
            " ORDER BY v.issue_date DESC, v.id DESC", (pid,))
        for r in rows:
            r["amount_gross"] = round2(r.get("amount_gross"))
            r["settled"] = round2(r.get("settled"))
            r["open"] = round2(r["amount_gross"] - r["settled"])
        return rows

    invoices_sale = _invoices("IFNULL(v.direction,'') = '开具销项发票'")
    invoices_buy = _invoices("IFNULL(v.direction,'') = '收到进项发票'")

    def _inv_totals(rows):
        """作废票不参与合计（与首页口径一致）。"""
        ok = [r for r in rows if (r.get("status") or "") != "已作废"]
        gross = round2(sum(r["amount_gross"] for r in ok))
        settled = round2(sum(r["settled"] for r in ok))
        paid = round2(sum(r["amount_gross"] for r in ok
                          if (r.get("status") or "") == "已收讫"))
        open_n = sum(1 for r in ok
                     if (r.get("status") or "") != "已收讫"
                     and r["open"] > TOL)
        return {"count": len(ok), "gross": gross, "settled": settled,
                "paid": paid, "open": round2(gross - settled), "open_n": open_n}

    sale_t = _inv_totals(invoices_sale)
    buy_t = _inv_totals(invoices_buy)

    def _group(rows, contracts, totals_fn):
        """按 contract_id 分组，顺序跟合同列表一致，未挂合同的排最后。"""
        cmap = {c["id"]: c for c in (contracts or [])}
        buckets, order = {}, []
        for r in rows:
            k = r.get("contract_id")
            if k not in buckets:
                buckets[k] = []
                order.append(k)
            buckets[k].append(r)
        linked = [k for k in order if k in cmap]
        linked.sort(key=lambda k: cmap[k].get("code") or "")
        rest = [k for k in order if k not in cmap]
        out = []
        for k in linked + rest:
            c = cmap.get(k)
            out.append({
                "contract_id": k, "unlinked": c is None,
                "code": (c or {}).get("code") or "",
                "name": (c or {}).get("name") or "",
                "direction": (c or {}).get("direction") or "",
                "contract_amount": round2((c or {}).get("amount_gross")),
                "rows": buckets[k], "totals": totals_fn(buckets[k]),
            })
        return out

    def _pay_totals(rows):
        return {"n": len(rows),
                "in": round2(sum(r["amount"] for r in rows
                                 if (r.get("direction") or "") == "收入合同")),
                "out": round2(sum(r["amount"] for r in rows
                                  if (r.get("direction") or "") == "支出合同"))}

    sale_groups = _group(invoices_sale, contracts, _inv_totals)
    buy_groups = _group(invoices_buy, contracts, _inv_totals)

    # -------------------------------------------------------------- 收付款
    pays = query("""SELECT id, code, contract_id, pay_date, direction, nature, amount,
                           pay_method, handler, memo
                    FROM payments WHERE project_id = ?
                    ORDER BY pay_date, id""", (pid,))
    for r in pays:
        r["amount"] = round2(r.get("amount"))
    received = round2(sum(r["amount"] for r in pays
                          if (r.get("direction") or "") == "收入合同"))
    pay_rec_out = round2(sum(r["amount"] for r in pays
                             if (r.get("direction") or "") == "支出合同"))

    # -------------------------------------------------- 费用（按类别 + 明细）
    PAID = cost_paid_expr("k")
    cats = query("""
        SELECT IFNULL(cc.name, '（未分类）') AS name,
               COUNT(*) AS n,
               IFNULL(SUM(k.amount), 0) AS amount,
               IFNULL(SUM(%s), 0) AS paid
        FROM costs k LEFT JOIN cost_categories cc ON k.category = cc.id
        WHERE k.project_id = ?
        GROUP BY cc.id, cc.name
        ORDER BY amount DESC""" % PAID, (pid,))
    for r in cats:
        r["amount"] = round2(r.get("amount"))
        r["paid"] = round2(r.get("paid"))
    cost_total = round2(sum(r["amount"] for r in cats))
    cost_paid = round2(sum(r["paid"] for r in cats))

    costs = query("""
        SELECT k.id, k.code, k.cost_date, k.summary, k.amount, k.pay_status,
               IFNULL(k.paid_amount, 0) AS paid_amount,
               IFNULL(cc.name, '') AS category_name, k.invoice_id,
               IFNULL(u.display_name, u.username) AS employee_name
        FROM costs k LEFT JOIN cost_categories cc ON k.category = cc.id
        LEFT JOIN users u ON k.employee_id = u.id
        WHERE k.project_id = ?
        ORDER BY k.cost_date DESC, k.id DESC LIMIT 50""", (pid,))
    for r in costs:
        r["amount"] = round2(r.get("amount"))

    # -------------------------------------------------------------- 对私结算
    priv = query("""
        SELECT id, code, title, calc_date, broker, status,
               amount_income, amount_tax, amount_cost, amount_payable
        FROM private_settlements WHERE project_id = ?
        ORDER BY calc_date DESC, id DESC""", (pid,))
    for r in priv:
        for k in ("amount_income", "amount_tax", "amount_cost", "amount_payable"):
            r[k] = round2(r.get(k))

    pay_groups = _group(pays, contracts, _pay_totals)

    # ---------------------------------------------------------------- 汇总
    paid_out = round2(pay_rec_out + cost_paid)
    income_eff = round2(_income_effective({"income": income,
                                           "invoice_sale": sale_t["gross"],
                                           "received": received}))
    profit = round2(income_eff - contract_cost - cost_total)
    return {
        "p": p, "contracts": contracts,
        "income": income, "contract_cost": contract_cost,
        "invoices_sale": invoices_sale, "invoices_buy": invoices_buy,
        "sale": sale_t, "buy": buy_t,
        "sale_groups": sale_groups, "buy_groups": buy_groups,
        "pays": pays, "pay_groups": pay_groups,
        "received": received, "pay_rec_out": pay_rec_out,
        "paid_out": paid_out,
        "cats": cats, "costs": costs,
        "cost_total": cost_total, "cost_paid": cost_paid,
        "income_eff": income_eff,
        "income_fallback": bool(not income and income_eff > 0),
        "profit": profit,
        "profit_rate": (profit / income_eff) if income_eff else 0,
        "priv": priv,
        "p_profit": round2(sum(r["amount_tax"] for r in priv)),
        "p_payable": round2(sum(r["amount_payable"] for r in priv)),
    }


def private_recalc(rid):
    """按明细重算对私计算单的四个汇总金额。"""
    t = one("""SELECT IFNULL(SUM(income),0) AS i, IFNULL(SUM(tax_amount),0) AS t,
                      IFNULL(SUM(cost_amount),0) AS c, IFNULL(SUM(payable),0) AS p
               FROM private_settlement_items WHERE settlement_id = ?""", (rid,)) or {}
    vals = (round2(t.get("i")), round2(t.get("t")), round2(t.get("c")),
            round2(t.get("p")))
    execute("UPDATE private_settlements SET amount_income = ?, amount_tax = ?, "
            "amount_cost = ?, amount_payable = ?, updated_at = ? WHERE id = ?",
            vals + (now_str(), rid))
    return {"income": vals[0], "tax": vals[1], "cost": vals[2], "payable": vals[3]}


def private_prefill(contract_ids):
    """从一张或多张合同带出对私计算明细：一行一个清单项。

    ⚠️ 扣税比例取「参数设置 → private_tax_rate」的默认值，
    **不取合同清单项的发票税点** —— 那是两回事（这里是给我方算留成）。
    """
    if isinstance(contract_ids, (int, str)):
        contract_ids = [contract_ids]
    ids = [int(x) for x in (contract_ids or []) if str(x).strip().isdigit()]
    if not ids:
        return []
    rate = pct_text(setting("private_tax_rate", "13"), "0%")
    ph = ",".join("?" * len(ids))
    rows = query("""SELECT id, contract_id, name, amount FROM contract_items
                    WHERE contract_id IN (%s)
                    ORDER BY contract_id, seq, id""" % ph, tuple(ids))
    out = []
    for r in rows:
        income = round2(r["amount"] or 0)
        tax = round2(income * pct_value(rate))
        out.append({"contract_item_id": r["id"], "name": r["name"] or "",
                    "row_contract_id": r["contract_id"],
                    "income": income, "tax_rate": rate,
                    "tax_amount": tax, "cost_amount": 0,
                    "payable": round2(income - tax), "remark": ""})
    return out


def _id_list(v):
    """把 1 / "1" / [1, "2"] / None 统一成 int 列表（下拉多选、查询串都用它）。"""
    if v is None:
        return []
    if isinstance(v, (int, str)):
        return [int(v)] if str(v).strip().isdigit() else []
    return [int(x) for x in v if str(x).strip().isdigit()]


def private_cost_reference(contract_ids=None, project_id=None):
    """「费用支出」参考值：只显示，不自动填成本（口径由人判断）。

    一个单子可以关联多张合同，所以按**合同 id 列表**汇总。
    - lines：这些合同里每个清单项已登记的费用（费用支出里选过「对应合同清单项」的）
    - linked：已对应到这些合同清单项的费用合计
    - unlinked：本项目里没对应到这些合同清单项的费用合计
    - project_total：本项目全部费用支出合计
    """
    ids = _id_list(contract_ids)
    rows = []
    if ids:
        ph = ",".join("?" * len(ids))
        for r in query("""
                SELECT i.id, i.seq, i.name, i.amount,
                       IFNULL(h.code,'') AS contract_code,
                       IFNULL((SELECT SUM(ci.amount) FROM cost_items ci
                               WHERE ci.contract_item_id = i.id), 0) AS cost
                FROM contract_items i
                LEFT JOIN contracts h ON i.contract_id = h.id
                WHERE i.contract_id IN (%s)
                ORDER BY h.code, i.seq, i.id""" % ph, tuple(ids)):
            rows.append({"id": r["id"], "seq": r["seq"], "name": r["name"] or "",
                         "contract_code": r["contract_code"],
                         "amount": round2(r["amount"] or 0),
                         "cost": round2(r["cost"] or 0)})
    linked = round2(sum(r["cost"] for r in rows))
    project_total = round2(scalar(
        "SELECT IFNULL(SUM(amount),0) FROM costs WHERE project_id = ?",
        (project_id,))) if project_id else 0.0
    unlinked = project_total
    if ids and project_id:
        ph = ",".join("?" * len(ids))
        unlinked = round2(scalar("""
            SELECT IFNULL(SUM(c.amount),0) FROM costs c
            WHERE c.project_id = ?
              AND NOT EXISTS (SELECT 1 FROM cost_items ci
                              JOIN contract_items i2 ON ci.contract_item_id = i2.id
                              WHERE ci.cost_id = c.id AND i2.contract_id IN (%s))"""
            % ph, tuple([project_id] + ids)))
    # 注意：这个键别叫 items —— Jinja 里 cost_ref.items 拿到的是字典的 items 方法，
    # 不是这个键（会顺着去迭代一个内置方法，直接 TypeError）
    return {"lines": rows, "linked": linked, "project_total": project_total,
            "unlinked": unlinked}


def private_settlement_xlsx(rid, company=""):
    """对私计算单 Excel（带样式）：抬头 + 明细 + 合计 + 大写。"""
    import officedocs
    q = one("""SELECT d.*, h.code AS contract_code, h.name AS contract_name,
                      p.name AS project_name, u.name AS unit_name,
                      (SELECT GROUP_CONCAT(c.code, '、')
                         FROM private_settlement_contracts pc
                         JOIN contracts c ON pc.contract_id = c.id
                        WHERE pc.settlement_id = d.id) AS contract_codes
               FROM private_settlements d
               LEFT JOIN contracts h ON d.contract_id = h.id
               LEFT JOIN projects p ON d.project_id = p.id
               LEFT JOIN units u ON d.unit_id = u.id
               WHERE d.id = ?""", (rid,))
    if not q:
        raise ValueError("对私计算单不存在")
    items = detail_rows("private_settlements", rid)
    # 关联合同时按编号显示（可能多张），每行再标出所属合同
    codes = {r["id"]: (r["code"] or "") for r in query("SELECT id, code FROM contracts")}
    g = officedocs.Grid("对私计算", widths=[5, 14, 22, 10, 12, 12, 11, 13, 16])
    g.add(["%s 对私计算单" % company], officedocs.S_TITLE, height=34)
    g.merge(1, 1, 1, 9)
    g.add(["居间人：" + (q["broker"] or ""), "", "",
           "计算单号：" + (q["code"] or ""), "",
           "计算日期：" + (q["calc_date"] or ""), "", "", ""],
          officedocs.S_TEXT, height=20)
    g.merge(2, 1, 2, 3)
    g.merge(2, 4, 2, 6)
    g.merge(2, 7, 2, 9)
    g.add(["关联合同：" + (q["contract_codes"] or q["contract_code"] or "—"),
           "", "", "所属项目：" + (q["project_name"] or "—"), "",
           "状态：" + (q["status"] or ""), "", "", ""],
          officedocs.S_TEXT, height=20)
    g.merge(3, 1, 3, 3)
    g.merge(3, 4, 3, 6)
    g.merge(3, 7, 3, 9)
    g.add(["计算口径：我方利润 = 收入金额 × 扣税比例（「扣税」是名义叫法）；"
           "应付居间人 = 收入金额 − 我方利润 − 成本。"
           "（扣税比例由我方设定，名义上是扣税、实际为我方收入，与发票税点无关）"
           "　纯成本行：收入留 0、只填成本，这一行应付为负，合计依然等于"
           "「收入 − 扣税 − 成本」。",
           "", "", "", "", "", "", "", ""], officedocs.S_NOTE, height=40)
    g.merge(4, 1, 4, 9)

    g.add(["序号", "所属合同", "项目 / 品名", "扣税比例", "收入金额", "我方利润",
           "成本", "应付居间人", "备注"], officedocs.S_HEAD, height=24)
    for i, it in enumerate(items, 1):
        g.add([{"v": i, "s": officedocs.S_INT},
               {"v": codes.get(it["row_contract_id"], "") or "", "s": officedocs.S_CENTER},
               {"v": it["name"] or "", "s": officedocs.S_TEXT},
               {"v": it["tax_rate"] or "", "s": officedocs.S_CENTER},
               {"v": round2(it["income"]), "s": officedocs.S_MONEY},
               {"v": round2(it["tax_amount"]), "s": officedocs.S_MONEY},
               {"v": round2(it["cost_amount"]), "s": officedocs.S_MONEY},
               {"v": round2(it["payable"]), "s": officedocs.S_MONEY},
               {"v": it["remark"] or "", "s": officedocs.S_TEXT}])
    if not items:
        g.add(["", "（还没有明细）"], officedocs.S_TEXT)
    r = len(g.rows) + 1
    g.add(["", "合计", "", "",
           {"v": round2(q["amount_income"]), "s": officedocs.S_TOT_MONEY},
           {"v": round2(q["amount_tax"]), "s": officedocs.S_TOT_MONEY},
           {"v": round2(q["amount_cost"]), "s": officedocs.S_TOT_MONEY},
           {"v": round2(q["amount_payable"]), "s": officedocs.S_TOT_MONEY},
           ""], officedocs.S_TOT_TEXT, height=24)
    r += 1
    g.add([{"v": "应付居间人合计（大写）：" + money_cn(q["amount_payable"] or 0),
            "s": officedocs.S_LABEL}], officedocs.S_LABEL, height=22)
    g.merge(r, 1, r, 9)
    if q["memo"]:
        r += 1
        g.add([{"v": "说明：" + q["memo"], "s": officedocs.S_NOTE}],
              officedocs.S_NOTE, height=46)
        g.merge(r, 1, r, 9)
    r += 1
    g.add([{"v": "居间人签字：____________    经办人：____________    "
                 "日期：____________", "s": officedocs.S_NORM}], officedocs.S_NORM,
          height=30)
    g.merge(r, 1, r, 9)
    return officedocs.grid_xlsx(g)


def deliveries_of_contract(contract_id):
    """某张合同已经生成的送货单（合同页上列出来，可继续编辑 / 导出）。"""
    return query("""
        SELECT d.id, d.code, d.deliver_date, d.receiver, d.status,
               IFNULL((SELECT SUM(x.qty) FROM delivery_items x
                       WHERE x.delivery_id = d.id), 0) AS total_qty,
               (SELECT COUNT(*) FROM delivery_items x
                WHERE x.delivery_id = d.id) AS item_count
        FROM deliveries d WHERE d.contract_id = ?
        ORDER BY d.id DESC""", (contract_id,))


def contract_summary(contract_id):
    c = one("SELECT c.*, p.name AS project_name, u.name AS unit_name "
            "FROM contracts c LEFT JOIN projects p ON c.project_id = p.id "
            "LEFT JOIN units u ON c.unit_id = u.id WHERE c.id = ?", (contract_id,))
    return c or {}


# ------------------------------------------------------------ 金额大写
def money_cn(n):
    """数字 → 人民币大写（报价单、送货单上用）。"""
    digits = "零壹贰叁肆伍陆柒捌玖"
    unit1 = ("", "拾", "佰", "仟")
    unit2 = ("", "万", "亿", "万亿")
    x = round2(n)
    sign = "负" if x < 0 else ""
    cents = int(round(abs(x) * 100))
    int_part, jiao, fen = cents // 100, (cents // 10) % 10, cents % 10
    if int_part == 0:
        s_int = "零"
    else:
        groups, v = [], int_part
        while v > 0:
            groups.append(v % 10000)
            v //= 10000
        parts = []
        for gi in range(len(groups) - 1, -1, -1):
            seg = groups[gi]
            if seg == 0:
                if parts and any(g != 0 for g in groups[:gi]):
                    parts.append("零")
                continue
            seg_s, pending = "", False
            for i in range(3, -1, -1):
                d = (seg // (10 ** i)) % 10
                if d:
                    if pending and seg_s:
                        seg_s += "零"
                    seg_s += digits[d] + unit1[i]
                    pending = False
                elif seg_s:
                    pending = True
            parts.append(seg_s + unit2[gi])
        s_int = "".join(parts).rstrip("零") or "零"
    out = s_int + "元"
    if jiao == 0 and fen == 0:
        out += "整"
    else:
        out += (digits[jiao] + "角") if jiao else "零"
        if fen:
            out += digits[fen] + "分"
    return sign + out


# ------------------------------------------------------------ Excel 导入
IMPORT_ALIASES = {
    "name": ("品名", "名称", "货物名称", "产品名称", "材料名称", "设备名称",
             "货物", "产品", "材料", "项目", "项目名称", "内容"),
    "brand": ("品牌", "厂牌", "厂商", "生产厂家", "厂家"),
    "model": ("型号", "规格型号", "设备型号", "产品型号"),
    "spec": ("参数", "参数规格", "技术参数", "规格", "规格描述", "参数说明"),
    "unit": ("单位", "计量单位"),
    "qty": ("数量", "数目", "工程量"),
    "price": ("单价", "含税单价", "价格", "综合单价"),
    "amount": ("金额", "合计金额", "小计", "总价", "合价"),
    "tax_included": ("含税", "是否含税", "含税否", "含税方式", "含税/不含税"),
    "tax_rate": ("税点", "税率"),
    "remark": ("备注", "说明", "其他说明", "注"),
}


def _norm_header(v):
    t = str(v or "").strip().replace(" ", "").replace("　", "")
    t = t.replace("（", "(").replace("）", ")").replace("％", "%")
    for cut in ("(含税)", "(元)", "(台)", "(天)", "(%)", "%", "*"):
        t = t.replace(cut, "")
    return t


def _match_alias(cell):
    t = _norm_header(cell)
    if not t:
        return None
    for key, names in IMPORT_ALIASES.items():
        for nm in names:
            if t == nm:
                return key
    for key, names in IMPORT_ALIASES.items():
        for nm in names:
            if len(nm) >= 2 and t.startswith(nm):
                return key
    return None


def import_items_from_rows(rows, limit=800):
    """把表格（Excel/CSV 读出来的二维数组）解析成报价明细。

    表头行通过列名识别（品名 品牌 型号 参数 单位 数量 单价 金额 备注），
    因此模板可以随便调整顺序；「合计」行与空行自动跳过。
    """
    header_idx, mapping = None, {}
    for i, row in enumerate(rows[:25]):
        found = {}
        for j, cell in enumerate(row):
            key = _match_alias(cell)
            if key and key not in found:
                found[key] = j
        if "name" in found and len(found) >= 2:
            header_idx, mapping = i, found
            break
    if header_idx is None:
        raise ValueError("没找到表头行：至少要有一列叫「品名」或「名称」"
                         "（建议下载导入模板照填）")

    def cell(row, key):
        j = mapping.get(key)
        return str(row[j]).strip() if j is not None and j < len(row) else ""

    items, skipped = [], 0
    for row in rows[header_idx + 1:]:
        name = cell(row, "name")
        if not name or any(t in name for t in ("合计", "总计", "小计")):
            skipped += 1
            continue
        qty = _to_num(cell(row, "qty"))
        price = _to_num(cell(row, "price"))
        amount = _to_num(cell(row, "amount"))
        if not amount and (qty or price):
            amount = round2(qty * price)
        items.append({"name": name, "brand": cell(row, "brand"),
                      "model": cell(row, "model"), "spec": cell(row, "spec"),
                      "unit": cell(row, "unit"), "qty": qty, "price": price,
                      "amount": amount,
                      "tax_included": cell(row, "tax_included"),
                      "tax_rate": cell(row, "tax_rate"),
                      "remark": cell(row, "remark")})
        if len(items) >= limit:
            break
    if not items:
        raise ValueError("表格里没读到有效明细（品名列是空的？）")
    return items, skipped


def read_import_file(storage):
    """读上传的 .xlsx / .csv，返回解析好的报价明细。"""
    import officedocs
    if not storage or not storage.filename:
        raise ValueError("请选择要导入的文件")
    name = storage.filename.lower()
    if not name.endswith((".xlsx", ".csv")):
        raise ValueError("只支持 .xlsx 或 .csv；如果是 .xls，请先在 Excel 里另存为 .xlsx")
    data = storage.read()
    if len(data) > 8 * 1024 * 1024:
        raise ValueError("文件太大（超过 8MB），请只保留明细部分")
    rows = officedocs.read_rows(data, storage.filename)
    items, _skipped = import_items_from_rows(rows)
    return items


def merge_import_items(entity_key, rid, items, dkey="items"):
    """把导入的明细追加到已有明细后面（不覆盖手工录的）。返回总行数。"""
    spec = detail_spec(entity_key, dkey)
    exist = [dict(r) for r in detail_rows(entity_key, rid, dkey)]
    rows = []
    for r in exist:
        rows.append({c["name"]: ("" if r.get(c["name"]) is None else str(r.get(c["name"])))
                     for c in spec["cols"]})
    for it in items:
        rows.append({c["name"]: ("" if it.get(c["name"]) is None else str(it.get(c["name"])))
                     for c in spec["cols"]})
    save_details(entity_key, rid, {dkey: rows})
    return len(rows)


# ------------------------------------------------------------ 报价单 / 送货单 导出
def quotation_workbook(qid, company=""):
    """报价单 Excel（带样式）：抬头 + 明细 + 合计 + 大写 + 说明 + 落款。"""
    import officedocs
    q = one("""SELECT q.*, u.name AS unit_name, p.name AS project_name,
                      h.code AS contract_code
               FROM quotations q
               LEFT JOIN units u ON q.unit_id = u.id
               LEFT JOIN projects p ON q.project_id = p.id
               LEFT JOIN contracts h ON q.contract_id = h.id
               WHERE q.id = ?""", (qid,))
    if not q:
        raise ValueError("报价单不存在")
    items = query("SELECT * FROM quotation_items WHERE quotation_id = ? "
                  "ORDER BY seq, id", (qid,))
    g = officedocs.Grid("报价单", widths=[5.5, 22, 11, 12, 22, 6, 7.5, 11, 12.5,
                                          7, 7, 12])
    g.add(["%s 报价单" % company], officedocs.S_TITLE, height=34)
    g.merge(1, 1, 1, 12)
    g.add(["客户单位：" + (q["unit_name"] or ""), "", "",
           "报价单号：" + (q["code"] or ""), "", "",
           "报价日期：" + (q["quote_date"] or "")], officedocs.S_TEXT, height=20)
    g.merge(2, 1, 2, 3)
    g.merge(2, 4, 2, 6)
    g.merge(2, 7, 2, 12)
    valid_to = ""
    if q["quote_date"] and q["valid_days"]:
        try:
            from datetime import datetime, timedelta
            dt = datetime.strptime(q["quote_date"][:10], "%Y-%m-%d")
            valid_to = (dt + timedelta(days=int(q["valid_days"]))).strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            valid_to = ""
    g.add(["联系人：" + (q["contact"] or ""), "", "",
           "联系电话：" + (q["phone"] or ""), "", "",
           "有效期至：" + (valid_to or "—")], officedocs.S_TEXT, height=20)
    g.merge(3, 1, 3, 3)
    g.merge(3, 4, 3, 6)
    g.merge(3, 7, 3, 12)
    g.add(["关联项目：" + (q["project_name"] or "—"), "", "",
           "报价人：" + (q["handler"] or ""), "", "",
           "报价状态：" + (q["status"] or "")],
          officedocs.S_TEXT, height=20)
    g.merge(4, 1, 4, 3)
    g.merge(4, 4, 4, 6)
    g.merge(4, 7, 4, 12)

    head = ["序号", "品名 / 项目", "品牌", "型号", "参数 / 规格", "单位", "数量",
            "单价", "金额", "含税", "税点", "备注"]
    g.add(head, officedocs.S_HEAD, height=26)
    for i, it in enumerate(items, 1):
        g.add([{"v": i, "s": officedocs.S_INT},
               {"v": it["name"] or "", "s": officedocs.S_TEXT},
               {"v": it["brand"] or "", "s": officedocs.S_TEXT},
               {"v": it["model"] or "", "s": officedocs.S_TEXT},
               {"v": it["spec"] or "", "s": officedocs.S_TEXT},
               {"v": it["unit"] or "", "s": officedocs.S_CENTER},
               {"v": round2(it["qty"]), "s": officedocs.S_NUM},
               {"v": round2(it["price"]), "s": officedocs.S_MONEY},
               {"v": round2(it["amount"]), "s": officedocs.S_MONEY},
               {"v": it["tax_included"] or "", "s": officedocs.S_CENTER},
               {"v": it["tax_rate"] or "", "s": officedocs.S_CENTER},
               {"v": it["remark"] or "", "s": officedocs.S_TEXT}])
    if not items:
        g.add(["", "（还没有明细）"], officedocs.S_TEXT)
    r = len(g.rows) + 1
    g.add(["", "合计", "", "", "", "", "", "",
           {"v": round2(q["amount_total"]), "s": officedocs.S_TOT_MONEY}, "", "",
           ""], officedocs.S_TOT_TEXT, height=22)
    g.merge(r, 1, r, 8)
    if round2(q["discount"]):
        r += 1
        g.add(["", "优惠让利", "", "", "", "", "", "",
               {"v": -round2(q["discount"]), "s": officedocs.S_TOT_MONEY}, "", "",
               ""], officedocs.S_TOT_TEXT, height=22)
        g.merge(r, 1, r, 8)
    r += 1
    g.add(["", "报价总额", "", "", "", "", "", "",
           {"v": round2(q["amount_final"]), "s": officedocs.S_TOT_MONEY}, "", "",
           ""], officedocs.S_TOT_TEXT, height=24)
    g.merge(r, 1, r, 8)
    r += 1
    g.add([{"v": "合计人民币（大写）：" + money_cn(q["amount_final"] or 0),
            "s": officedocs.S_LABEL}], officedocs.S_LABEL, height=22)
    g.merge(r, 1, r, 12)
    if q["memo"]:
        r += 1
        g.add([{"v": "报价说明：" + q["memo"], "s": officedocs.S_NOTE}],
              officedocs.S_NOTE, height=46)
        g.merge(r, 1, r, 12)
    r += 1
    g.add([{"v": "报价人签字：____________    客户确认：____________    "
                 "日期：____________", "s": officedocs.S_NORM}], officedocs.S_NORM,
          height=30)
    g.merge(r, 1, r, 12)
    if q["contract_code"]:
        r += 1
        g.add([{"v": "本报价已转为合同：%s" % q["contract_code"], "s": officedocs.S_NOTE}],
              officedocs.S_NOTE, height=18)
        g.merge(r, 1, r, 12)
    return officedocs.grid_xlsx(g)


def quotation_template_xlsx():
    """报价明细导入模板（照填后上传即可）。"""
    import officedocs
    g = officedocs.Grid("报价明细", widths=[5.5, 22, 11, 12, 22, 6, 7.5, 11, 12.5,
                                            8, 8, 12])
    g.add(["报价明细 · 导入模板"], officedocs.S_TITLE, height=30)
    g.merge(1, 1, 1, 12)
    g.add(["填写说明：① 【品名】必填，其余可留空；② 填了数量与单价会自动算金额，"
           "金额列可留空；③ 品牌 / 型号 / 参数会自动收进「品牌型号库」，下次可直接下拉选；"
           "④【含税】填「含税 / 不含税」，【税点】填「0% / 1% / 3% / 6% / 9% / 13%」，"
           "留空按默认（含税 13%）处理；"
           "⑤ 不要改本行以下第 1 行表头文字，明细从第 2 行往下填；⑥ 整行空着或写了"
           "「合计」的行会被自动跳过。"], officedocs.S_NOTE, height=68)
    g.merge(2, 1, 2, 12)
    g.add(["序号", "品名 / 项目", "品牌", "型号", "参数 / 规格", "单位", "数量",
           "单价", "金额", "含税", "税点", "备注"], officedocs.S_HEAD, height=24)
    samples = [
        ["1", "动力配电箱", "正泰", "XL-21", "380V 400A IP54 户外型", "台", 2, 4580,
         9160, "含税", "13%", "含安装调试"],
        ["2", "电缆桥架", "华鹏", "XQJ-200", "200×100×1.5mm 热镀锌", "米", 120, 45,
         5400, "含税", "13%", ""],
        ["3", "电力电缆", "远东", "YJV-4×35", "0.6/1kV 铜芯", "米", 300, 128, 38400,
         "不含税", "3%", "国标足米"],
    ]
    for row in samples:
        g.add([{"v": row[0], "s": officedocs.S_INT},
               {"v": row[1], "s": officedocs.S_TEXT},
               {"v": row[2], "s": officedocs.S_TEXT},
               {"v": row[3], "s": officedocs.S_TEXT},
               {"v": row[4], "s": officedocs.S_TEXT},
               {"v": row[5], "s": officedocs.S_CENTER},
               {"v": row[6], "s": officedocs.S_NUM},
               {"v": row[7], "s": officedocs.S_MONEY},
               {"v": row[8], "s": officedocs.S_MONEY},
               {"v": row[9], "s": officedocs.S_CENTER},
               {"v": row[10], "s": officedocs.S_CENTER},
               {"v": row[11], "s": officedocs.S_TEXT}])
    return officedocs.grid_xlsx(g)


def delivery_docx(did, company=""):
    """送货单 Word（打印出来签字回执）。"""
    import officedocs
    d = one("""SELECT d.*, h.code AS contract_code, p.name AS project_name,
                      u.name AS unit_name
               FROM deliveries d
               LEFT JOIN contracts h ON d.contract_id = h.id
               LEFT JOIN projects p ON d.project_id = p.id
               LEFT JOIN units u ON d.unit_id = u.id
               WHERE d.id = ?""", (did,))
    if not d:
        raise ValueError("送货单不存在")
    items = query("SELECT * FROM delivery_items WHERE delivery_id = ? "
                  "ORDER BY seq, id", (did,))
    kv = [["送货单号", d["code"] or "", "送货日期", d["deliver_date"] or ""],
          ["收货单位", d["unit_name"] or "", "收货人", d["receiver"] or ""],
          ["联系电话", d["phone"] or "", "运输方式", d["carrier"] or ""],
          ["收货地址", d["address"] or "", "关联合同", d["contract_code"] or "—"]]
    if d["project_name"]:
        kv.append(["关联项目", d["project_name"], "送货人", d["handler"] or ""])
    rows = []
    total = 0.0
    for i, it in enumerate(items, 1):
        total += _to_num(it["qty"])
        rows.append([str(i), it["name"] or "", it["brand"] or "", it["model"] or "",
                     it["spec"] or "", it["unit"] or "",
                     ("%g" % round2(it["qty"])), it["remark"] or ""])
    rows.append(["合计（%d 项）" % len(items), "", "", "", "", "",
                 ("%g" % round(total)), ""])
    widths = [640, 2000, 1000, 1200, 2100, 640, 900, 880]
    blocks = [("title", "送 货 单"), ("sub", company),
              ("kv", kv), ("space",),
              ("table", ["序号", "品名 / 项目", "品牌", "型号", "参数 / 规格",
                         "单位", "数量", "备注"], rows, widths),
              ("space",),
              ("p", "说明：请收货人当场核对品名、型号、数量，确认无误后签字；"
                    "如有短缺或损坏请在备注栏注明。",
               {"align": "left", "size": 18}),
              ("space",),
              ("sign", ["收货人签字", "送货人签字", "日期"])]
    return officedocs.docx_bytes(blocks)


def delivery_summary(did):
    return one("""SELECT d.*, h.code AS contract_code, u.name AS unit_name
                  FROM deliveries d
                  LEFT JOIN contracts h ON d.contract_id = h.id
                  LEFT JOIN units u ON d.unit_id = u.id
                  WHERE d.id = ?""", (did,)) or {}


# =====================================================================
#                     数据备份（网页端一键备份）
# =====================================================================
# 备份文件必须写在**挂载进来的宿主目录**里（飞牛上是存储空间1 的备份目录）——
# 容器自己的文件系统是临时的，备份写在那儿等于没备。
# 允许写入的范围由环境变量 BACKUP_ROOTS 控制（默认 /backup），
# 页面上的「自定义位置」只能填这些根目录**底下**的路径，防手滑写到系统目录。
#
# 备份内容与 deploy/backup.sh 完全一致（同名同结构，两者可以混用）：
#   app.db        用 SQLite 在线备份 API 拿的一致性快照（不需要停服务）
#   manifest.txt  integrity_check + 逐表行数
#   uploads.tgz   附件
BACKUP_ROOTS = [p.strip() for p in
                (os.environ.get("BACKUP_ROOTS") or "/backup").split(",")
                if p.strip()]
BACKUP_DIR = (os.environ.get("BACKUP_DIR") or "/backup/contract-app").strip()
BACKUP_KEEP = int(os.environ.get("BACKUP_KEEP") or 14)
BACKUP_PREFIX = "contract-app_"
BACKUP_TAG = "web"


def _real(p):
    """规范化成绝对物理路径（把 .. 与软链接都解开，用于安全校验）。"""
    return os.path.realpath(os.path.abspath(os.path.expanduser(str(p or ""))))


def backup_roots():
    return [_real(p) for p in BACKUP_ROOTS]


def backup_dir_default():
    return _real(BACKUP_DIR)


def backup_check_dir(path):
    """校验备份目录：返回 (物理路径, 错误信息)。

    只允许写在 BACKUP_ROOTS 底下 —— 部署时把宿主目录挂进容器，
    这里就是"能写哪儿"的白名单。
    """
    p = (path or "").strip() or BACKUP_DIR
    if "\x00" in p:
        return None, "目录名不合法"
    rp = _real(p)
    roots = backup_roots()
    for r in roots:
        if rp == r or rp.startswith(r + os.sep):
            return rp, ""
    return None, ("只能写在允许的备份目录里：%s（要加别的位置，"
                  "把那个目录挂进容器并加进 BACKUP_ROOTS）" % "、".join(roots))


def backup_dir_state(path):
    """这个目录现在能不能用（给页面显示状态用）。"""
    rp, err = backup_check_dir(path)
    if err:
        return {"path": path, "ok": False, "err": err,
                "exists": False, "writable": False}
    exists = os.path.isdir(rp)
    ok = writable = False
    err = ""
    if not exists:
        parent = rp
        while parent and not os.path.isdir(parent):
            parent = os.path.dirname(parent)
        writable = os.access(parent or "/", os.W_OK) if parent else False
        ok = writable
        if not writable:
            err = "目录还不存在，且它的上级目录不可写"
    else:
        writable = os.access(rp, os.W_OK)
        ok = writable
        if not writable:
            err = "这个目录不可写（检查挂载与权限）"
    return {"path": rp, "ok": ok, "err": err, "exists": exists,
            "writable": writable}


def backup_targets():
    """可选的备份位置：默认目录 + 各备份根下已存在的一级子目录。"""
    out = [backup_dir_default()]
    for root in backup_roots():
        if not os.path.isdir(root):
            continue
        out.append(root)
        try:
            names = sorted(os.listdir(root))
        except OSError:
            continue
        for n in names:
            full = os.path.join(root, n)
            if os.path.isdir(full) and not os.path.islink(full):
                out.append(full)
    # 去重、按路径排序，但默认目录永远排第一
    seen, uniq = set(), []
    for p in out:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return [uniq[0]] + sorted(uniq[1:])


def backup_list(path=None):
    """目录里已有的备份（新的在前）。"""
    rp, err = backup_check_dir(path)
    if err or not os.path.isdir(rp):
        return []
    out = []
    for n in os.listdir(rp):
        if not (n.startswith(BACKUP_PREFIX) and n.endswith(".tgz")):
            continue
        full = os.path.join(rp, n)
        if not os.path.isfile(full):
            continue
        st = os.stat(full)
        out.append({"name": n, "size": st.st_size,
                    "size_h": human_size(st.st_size),
                    "mtime": datetime.fromtimestamp(st.st_mtime)
                                     .strftime("%Y-%m-%d %H:%M:%S")})
    out.sort(key=lambda r: r["mtime"], reverse=True)
    return out


def backup_safe_name(name):
    """文件名只允许字面量，挡掉 ../ 与绝对路径。"""
    n = (name or "").strip()
    if n != os.path.basename(n) or not n.startswith(BACKUP_PREFIX) \
            or not n.endswith(".tgz"):
        return None
    return n


def _rm_tree_quiet(path):
    """安静地删掉临时目录。

    删不掉**不能**影响备份结果：备份包已经写好了，收尾失败只该留下个
    临时目录（下一次备份开头会再清一遍），不该把成功变成 500。
    有的环境（比如带批量删除守卫的沙箱）会在 rmtree 里直接抛 SystemExit，
    所以这里连 BaseException 一起兜住。
    """
    try:
        shutil.rmtree(path, ignore_errors=True)
    except BaseException:                                  # noqa: BLE001
        pass


def _backup_tidy(path):
    """清掉上一次没删干净的临时目录（.bak_*）。"""
    try:
        names = os.listdir(path)
    except OSError:
        return
    for n in names:
        if n.startswith(".bak_"):
            _rm_tree_quiet(os.path.join(path, n))


def backup_create(path=None, actor=""):
    """做一次备份。返回 (结果字典, 错误信息)。"""
    target, err = backup_check_dir(path)
    if err:
        return None, err
    t0 = datetime.now()
    try:
        os.makedirs(target, exist_ok=True)
    except OSError as e:
        return None, "备份目录建不出来：%s" % e
    if os.path.isfile(target):
        return None, "这个路径是个文件，不是目录"
    _backup_tidy(target)

    stamp = t0.strftime("%Y%m%d_%H%M%S")
    name = "%s%s_%s.tgz" % (BACKUP_PREFIX, stamp, BACKUP_TAG)
    full = os.path.join(target, name)
    work = tempfile.mkdtemp(prefix=".bak_", dir=target)   # 同盘临时目录，最后一步只是改名
    try:
        # ---- 1. 数据库：在线一致性快照（不停服务、包含 WAL 里已提交的内容）
        snap = os.path.join(work, "app.db")
        _backup_snapshot(str(DB_PATH), snap)
        # ---- 2. integrity_check + 逐表行数（坏备份当场发现）
        ok, counts = _backup_manifest(snap, actor, target)
        with open(os.path.join(work, "manifest.txt"), "w", encoding="utf-8") as f:
            f.write(_manifest_text(ok, counts, actor, target))
        if ok != "ok":
            return None, "备份校验没通过（%s），已放弃" % ok
        # ---- 3. 附件单独打包
        files = ["app.db", "manifest.txt"]
        if os.path.isdir(UPLOAD_DIR) and os.listdir(UPLOAD_DIR):
            up = os.path.join(work, "uploads.tgz")
            with tarfile.open(up, "w:gz") as tar:
                tar.add(str(UPLOAD_DIR), arcname="uploads")
            files.append("uploads.tgz")
        # ---- 4. 打成一层包（与 deploy/backup.sh 同结构）
        with tarfile.open(full, "w:gz") as tar:
            for f in files:
                tar.add(os.path.join(work, f), arcname=f)
    except Exception as e:                                  # noqa: BLE001
        try:
            if os.path.exists(full):
                os.remove(full)
        except OSError:
            pass
        return None, "备份失败：%s" % e
    finally:
        _rm_tree_quiet(work)

    size = os.path.getsize(full)
    kept, dropped = _backup_rotate(target)
    return {"name": name, "path": target, "full": full, "size": size,
            "size_h": human_size(size), "tables": counts,
            "integrity": ok, "seconds": (datetime.now() - t0).total_seconds(),
            "rotated": dropped, "kept": kept}, ""


def _backup_snapshot(src_path, out_path):
    """SQLite 在线备份 API —— 别直接拷 app.db（WAL 下可能不一致）。"""
    uri = "file:%s?mode=ro" % src_path.replace("?", "%3f").replace("#", "%23")
    src = sqlite3.connect(uri, uri=True, timeout=30)
    try:
        dst = sqlite3.connect(out_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def _backup_manifest(db_file, actor="", target=""):
    db = sqlite3.connect(db_file)
    try:
        ok = db.execute("PRAGMA integrity_check").fetchone()[0]
        counts = {}
        for (t,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' "
                               "AND name NOT LIKE 'sqlite_%' ORDER BY name"):
            try:
                counts[t] = db.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
            except sqlite3.Error:
                continue
    finally:
        db.close()
    return ok, counts


def _manifest_text(ok, counts, actor, target):
    lines = ["integrity_check: %s" % ok,
             "生成时间: %s" % datetime.now().strftime("%F %T"),
             "操作人: %s" % (actor or "（未记录）"),
             "备份目录: %s" % target,
             "来源库: %s" % DB_PATH, ""]
    for t, n in counts.items():
        lines.append("%-28s %8d" % (t, n))
    lines.append("%-28s %8d" % ("【合计】", sum(counts.values())))
    return "\n".join(lines) + "\n"


def _backup_rotate(path, keep=None):
    """同目录里只留最近 N 份（与 deploy/backup.sh 同一套命名）。

    删旧备份也**不能**影响本次备份的结果：删不掉就留着（下次再删），
    只把删掉的数量如实报给页面。
    """
    keep = int(keep or BACKUP_KEEP)
    items = backup_list(path)
    dropped = []
    for r in items[keep:]:
        try:
            os.remove(os.path.join(path, r["name"]))
            dropped.append(r["name"])
        except BaseException:                              # noqa: BLE001
            pass
    return len(backup_list(path)), dropped


def backup_delete(path, name):
    n = backup_safe_name(name)
    if not n:
        return False, "文件名不合法"
    rp, err = backup_check_dir(path)
    if err:
        return False, err
    full = os.path.join(rp, n)
    if not os.path.isfile(full):
        return False, "这个备份已经不存在了"
    try:
        os.remove(full)
    except BaseException as e:                             # noqa: BLE001
        return False, "删不掉：%s" % e
    return True, ""


def backup_file(path, name):
    """给下载用的 (目录, 文件名)。"""
    n = backup_safe_name(name)
    if not n:
        return None, None
    rp, err = backup_check_dir(path)
    if err or not os.path.isfile(os.path.join(rp, n)):
        return None, None
    return rp, n


def backup_info(path=None):
    """页面要的全部信息。"""
    rp, err = backup_check_dir(path)
    state = backup_dir_state(rp or path)
    return {"dir": state["path"], "state": state, "err": err,
            "roots": backup_roots(), "default": backup_dir_default(),
            "targets": backup_targets(), "keep": BACKUP_KEEP,
            "files": backup_list(state["path"]), "info": backup_stats(state["path"])}


def backup_stats(path=None):
    """库里最要紧的几张表的行数 + 库文件大小（页面上给个"备了什么"的直观印象）。"""
    try:
        n = os.path.getsize(str(DB_PATH))
    except OSError:
        n = 0
    out = {"db_size": n, "db_size_h": human_size(n)}
    for t in ("projects", "contracts", "payments", "costs", "invoices",
              "settlements", "staff", "payroll"):
        try:
            out[t] = scalar("SELECT COUNT(*) FROM %s" % t)
        except sqlite3.Error:
            out[t] = 0
    return out


# =====================================================================
#                          记工（点工 / 临时工）
# =====================================================================
# ⚠️ 跟「工资管理」的月薪员工是两套：那边算月薪 + 社保 + 个税；
#    这边是工地点工 —— 按天 / 半天结，没有社保个税，月底汇总成一笔人工费。
def worklog_default_month():
    return datetime.now().strftime("%Y-%m")


def worklog_months():
    """做过记工的月份（新的在前），给月份下拉用。"""
    rows = query("SELECT DISTINCT substr(work_date,1,7) AS ym FROM worklogs "
                 "ORDER BY ym DESC")
    out = [r["ym"] for r in rows if r["ym"]]
    cur = worklog_default_month()
    if cur not in out:
        out.insert(0, cur)
    return out


def worklog_month(ym, project_id=None, include_active=True):
    """某月的记工月表：行 = 工人，列 = 1~当月天数，格子 = 当天工天。

    一天记了两条（比如上午在这个项目、下午在另一个）会累加。
    返回 {ym, ndays, people, totals, projects}
    """
    ndays = month_day_count(ym)
    sql = ("SELECT t.work_date, t.worker_id, t.rate, t.days, t.amount, t.memo, "
           "IFNULL(t.rate_src,'') AS rate_src, "
           "w.name AS worker_name, w.trade, w.default_rate, w.rate2, "
           "p.name AS project_name "
           "FROM worklogs t LEFT JOIN workers w ON t.worker_id = w.id "
           "LEFT JOIN projects p ON t.project_id = p.id "
           "WHERE substr(t.work_date,1,7) = ?")
    args = [ym]
    if project_id is not None:
        if int(project_id) == 0:
            sql += " AND t.project_id IS NULL"
        else:
            sql += " AND t.project_id = ?"
            args.append(int(project_id))
    sql += " ORDER BY IFNULL(w.sort,0), w.name, t.work_date, t.id"
    rows = query(sql, args)

    people, order = {}, []
    for r in rows:
        wid = r["worker_id"]
        p = people.get(wid)
        if p is None:
            p = {"worker_id": wid, "name": r["worker_name"] or "（已删除的工人）",
                 "trade": r["trade"] or "", "cells": {}, "days": 0.0,
                 "amount": 0.0, "last_rate": 0.0, "rates": set(), "projects": set(),
                 "default_rate": float(r["default_rate"] or 0),
                 "rate2": float(r["rate2"] or 0)}
            people[wid] = p
            order.append(wid)
        day = int(r["work_date"][8:10])
        cell = p["cells"].setdefault(day, {"days": 0.0, "amount": 0.0,
                                           "rate": 0.0, "diff": False,
                                           "src": "", "alt": False})
        cell["days"] += float(r["days"] or 0)
        cell["amount"] += float(r["amount"] or 0)
        cell["rate"] = float(r["rate"] or 0)
        # 这格是按「第二工资」记的（月表上显示成 2）—— 一天记了两条就都标上
        if r["rate_src"] == "第二":
            cell["src"] = "第二"
        p["days"] += float(r["days"] or 0)
        p["amount"] += float(r["amount"] or 0)
        p["last_rate"] = float(r["rate"] or 0)
        if r["rate"]:
            p["rates"].add(round(float(r["rate"]), 2))
        if r["project_name"]:
            p["projects"].add(r["project_name"])

    # 把「在用但本月还没记过」的工人也列出来 —— 否则新月份是一张空表，没地方点
    if include_active and project_id is None:
        for w in query("SELECT id, name, trade, IFNULL(default_rate,0) dr, "
                       "IFNULL(rate2,0) r2, IFNULL(sort,0) s FROM workers "
                       "WHERE IFNULL(status,'在用') <> '停用' ORDER BY s, name"):
            if w["id"] in people:
                continue
            people[w["id"]] = {
                "worker_id": w["id"], "name": w["name"], "trade": w["trade"] or "",
                "cells": {}, "days": 0.0, "amount": 0.0, "last_rate": 0.0,
                "rates": set(), "projects": set(),
                "default_rate": float(w["dr"] or 0),
                "rate2": float(w["r2"] or 0), "blank": True}
            order.append(w["id"])

    out = []
    for wid in order:
        p = people[wid]
        # 格子显示什么：按第二工资记的显示「2」，其余显示工天（1 / 0.5）
        # 工天合计仍按真实工天算（「2」是一整天，算 1 个工）
        for _day, c in p["cells"].items():
            c["alt"] = (c.get("src") == "第二")
            c["diff"] = (not c["alt"] and bool(p["default_rate"]) and bool(c["rate"])
                         and abs(c["rate"] - p["default_rate"]) > 0.01)
            c["label"] = "2" if c["alt"] else money_plain(c["days"])
        p["rate_label"] = ("%s" % money(sorted(p["rates"])[0]) if len(p["rates"]) == 1
                           else (" / ".join(money(x) for x in
                                            sorted(p["rates"])[:3]) if p["rates"]
                                 else (money(p["default_rate"]) if p["default_rate"] else "")))
        p["project_label"] = "、".join(sorted(p["projects"]))
        out.append(p)
    totals = {"days": round2(sum(p["days"] for p in out)),
              "amount": round2(sum(p["amount"] for p in out))}
    return {"ym": ym, "ndays": ndays, "people": out, "totals": totals,
            "n_people": len(out)}


def _insert_worklog(work_date, worker_id, project_id, rate, days, memo=None,
                    rate_src=None):
    amt = round2(float(rate or 0) * float(days or 0))
    return execute("INSERT INTO worklogs (work_date, worker_id, project_id, rate, "
                   "days, amount, memo, created_at, rate_src) "
                   "VALUES (?,?,?,?,?,?,?,?,?)",
                   (work_date, worker_id, project_id or None, float(rate or 0),
                    float(days or 0), amt, (memo or "").strip() or None,
                    now_str(), rate_src))


def _wl_scope(project_id):
    """记工的项目维度：None / 0 表示「未指定项目」（project_id IS NULL）。

    一天可以分两个项目记（上午在 A、下午在 B），所以点格子时**只能动
    当前项目的那条**，不能把别的项目的记录一起删掉重写。
    """
    if project_id in (None, 0, "", "0"):
        return " AND project_id IS NULL", []
    return " AND project_id = ?", [int(project_id)]


def _wl_stat_cond(project_id):
    """统计口径：None / "all" 不限项目；0 → 未指定项目；其它 → 该项目。"""
    if project_id in (None, "", "all", "ALL"):
        return "", []
    if project_id in (0, "0"):
        return " AND project_id IS NULL", []
    return " AND project_id = ?", [int(project_id)]


def _worklog_replace(work_date, worker_id, project_id, rate, days, src):
    """把「这个项目上、这个人这一天」整体改成一条（同一项目里分两条的会合并）。"""
    cond, args = _wl_scope(project_id)
    execute("DELETE FROM worklogs WHERE work_date = ? AND worker_id = ?" + cond,
            [work_date, worker_id] + args)
    _insert_worklog(work_date, worker_id, project_id or None, rate, days,
                    rate_src=src)
    return round2(float(rate or 0) * float(days or 0))


def worklog_toggle(work_date, worker_id, project_id=None):
    """月表上**点一下格子**，四种状态轮着来：

        空 → 1 个工（默认日工资） → 半天 0.5（默认日工资的一半）
          → 2（按【第二工资】记一整天） → 空

    「2」格子里写的是 2，但它是**一整天**、按第二工资算 —— 工天合计仍记 1 个工。
    **没填第二工资的人没有「2」这一档**：半天再点就直接清空，
    免得格子里出现 2 却按默认日工资算钱，把人搞混。
    这一格指的是「这个人这一天」的全部记录 —— 一天分两个项目记了两条的，
    点了就整体调整，不会留下半条。
    返回 {"days", "rate", "amount", "state", "src"}。
    """
    w = one("SELECT id, IFNULL(default_rate,0) AS rate, IFNULL(rate2,0) AS rate2 "
            "FROM workers WHERE id = ?", (worker_id,))
    if not w:
        raise ValueError("这个工人不存在了")
    base = float(w["rate"] or 0)
    alt = float(w["rate2"] or 0)

    cond, cargs = _wl_scope(project_id)
    rows = query("SELECT id, days, rate, IFNULL(rate_src,'') AS src FROM worklogs "
                 "WHERE work_date = ? AND worker_id = ?" + cond + " ORDER BY id",
                 [work_date, worker_id] + cargs)

    if not rows:
        amt = _worklog_replace(work_date, worker_id, project_id, base, 1.0, "默认")
        out = {"days": 1.0, "rate": base, "amount": amt,
               "state": "1", "src": "默认"}
        if base <= 0:
            out["warn"] = ("这个工人还没设【默认日工资】，这条记成了 0 元 —— "
                           "去工人名单里补上日工资就会按新的算")
        return out

    total = round2(sum(float(r["days"] or 0) for r in rows))
    first = rows[0]
    rate = float(first["rate"] or 0)
    src = first["src"] or ""

    # 已经是「第二工资」那格 → 清空
    if src == "第二":
        for r in rows:
            execute("DELETE FROM worklogs WHERE id = ?", (r["id"],))
        return {"days": 0.0, "rate": rate, "amount": 0.0, "state": "", "src": ""}

    # 整天 → 半天（工价不变）
    if total >= 1 - TOL:
        amt = _worklog_replace(work_date, worker_id, project_id, rate, 0.5,
                               src or "默认")
        return {"days": 0.5, "rate": rate, "amount": amt,
                "state": "0.5", "src": src or "默认"}

    # 半天（或别的零碎）→ 第二工资，一整天
    # 没填第二工资的人**没有这一档**：直接清空，别让他看到「2」却按默认价算，
    # 那样只会让人搞混（用户 2026-10-18 的要求）。
    if alt <= 0:
        for r in rows:
            execute("DELETE FROM worklogs WHERE id = ?", (r["id"],))
        return {"days": 0.0, "rate": rate, "amount": 0.0, "state": "", "src": ""}
    amt = _worklog_replace(work_date, worker_id, project_id, alt, 1.0, "第二")
    return {"days": 1.0, "rate": alt, "amount": amt, "state": "2", "src": "第二"}


def worklog_set_rate(work_date, worker_id, rate, project_id=None):
    """**只改「这天这个人」的工价** —— 别的天、别的人一律不动。

    这天还没记过的，顺手记 1 个工（按新工价）。
    """
    try:
        rate = float(rate)
    except (TypeError, ValueError):
        raise ValueError("工价要填数字") from None
    if rate < 0:
        raise ValueError("工价不能是负数")
    cond, cargs = _wl_scope(project_id)
    rows = query("SELECT id FROM worklogs WHERE work_date = ? AND worker_id = ?" + cond,
                 [work_date, worker_id] + cargs)
    if not rows:
        _insert_worklog(work_date, worker_id, project_id or None, rate, 1.0)
        return {"days": 1.0, "rate": rate, "amount": round2(rate), "state": "1"}
    for r in rows:
        # 手工填了价就按手工的算，rate_src 清空（月表按工天显示，价与默认不同会标橙）
        execute("UPDATE worklogs SET rate = ?, "
                "amount = ROUND(? * IFNULL(days,1), 2), rate_src = NULL "
                "WHERE id = ?", (rate, rate, r["id"]))
    tot = one("SELECT IFNULL(SUM(days),0) d, IFNULL(SUM(amount),0) a "
              "FROM worklogs WHERE work_date = ? AND worker_id = ?" + cond,
              [work_date, worker_id] + cargs)
    return {"days": round2(tot["d"]), "rate": rate, "amount": round2(tot["a"]),
            "state": ("1" if round2(tot["d"]) >= 1 - TOL else "0.5")}


def worklog_row_stats(ym, worker_id, project_id="all"):
    cond, args = _wl_stat_cond(project_id)
    r = one("SELECT IFNULL(SUM(days),0) d, IFNULL(SUM(amount),0) a FROM worklogs "
            "WHERE substr(work_date,1,7) = ? AND worker_id = ?" + cond,
            [ym, worker_id] + args)
    return {"days": round2(r["d"]), "amount": round2(r["a"])}


def worklog_stats(ym, project_id="all"):
    cond, args = _wl_stat_cond(project_id)
    r = one("SELECT IFNULL(SUM(days),0) d, IFNULL(SUM(amount),0) a FROM worklogs "
            "WHERE substr(work_date,1,7) = ?" + cond, [ym] + args)
    return {"days": round2(r["d"]), "amount": round2(r["a"])}


def worklog_batch(worker_ids, date_from, date_to, project_id=None,
                  rate_override=None, days=1.0, memo="", skip_existing=True):
    """批量记工：一批人 × 一个日期区间，逐日生成。

    · 日工资默认取各自「工人名单」里的默认日工资；rate_override 有值时统一用它；
    · 已经记过的那天默认跳过（避免手滑点两次翻倍），跳过几条会返回；
    返回 {"created": n, "skipped": n, "missing_rate": [名字…]}
    """
    from datetime import date as _date, timedelta as _td
    try:
        d0 = _date.fromisoformat(date_from)
        d1 = _date.fromisoformat(date_to)
    except (TypeError, ValueError):
        raise ValueError("日期格式不对") from None
    if d1 < d0:
        d0, d1 = d1, d0
    if (d1 - d0).days > 366:
        raise ValueError("一次最多记一年，请把日期区间缩小")

    ids = []
    for x in worker_ids or []:
        try:
            ids.append(int(x))
        except (TypeError, ValueError):
            continue
    if not ids:
        raise ValueError("请至少选一个工人")

    ph = ",".join("?" * len(ids))
    workers = {r["id"]: r for r in query(
        f"SELECT id, name, IFNULL(default_rate,0) AS rate FROM workers "
        f"WHERE id IN ({ph})", ids)}

    d = d0
    created = skipped = 0
    missing = []
    while d <= d1:
        ds = d.isoformat()
        done = {r["worker_id"] for r in query(
            "SELECT worker_id FROM worklogs WHERE work_date = ? "
            "AND worker_id IN (%s)" % ph, [ds] + ids)}
        for wid in ids:
            w = workers.get(wid)
            if not w:
                continue
            if skip_existing and wid in done:
                skipped += 1
                continue
            rate = float(rate_override) if rate_override not in (None, "") else float(w["rate"] or 0)
            if rate <= 0 and w["name"] not in missing:
                missing.append(w["name"])
            amt = round2(rate * float(days))
            execute("INSERT INTO worklogs (work_date, worker_id, project_id, rate, "
                    "days, amount, memo, created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (ds, wid, project_id or None, rate, float(days), amt,
                     (memo or "").strip() or None, now_str()))
            created += 1
        d += _td(days=1)
    return {"created": created, "skipped": skipped, "missing_rate": missing}


def worklog_settle(ym, actor=""):
    """把某月的记工按【项目】汇总成「人工费」费用（幂等：同月同项目只留一条）。

    · 金额 = 该项目当月所有人 amount 之和；
    · 报销方式记「无需报销」、审批状态也是「无需报销」—— 不进报销待办；
    · 已存在的那条只更新金额与摘要（支付状态 / 已付金额不动，别把"已付"冲掉）。
    返回 {"created": n, "updated": n, "items": [...]}
    """
    cat_id = None
    row = one("SELECT id FROM cost_categories WHERE name = '人工费' LIMIT 1")
    if row:
        cat_id = row["id"]
    last_day = "%s-%02d-%02d" % (ym, int(ym[5:7]), month_day_count(ym))

    groups = query(
        "SELECT t.project_id, COUNT(DISTINCT t.worker_id) AS n, "
        "       SUM(t.days) AS days, SUM(t.amount) AS amount "
        "FROM worklogs t WHERE substr(t.work_date,1,7) = ? GROUP BY t.project_id",
        (ym,))
    created = updated = 0
    items = []
    for g in groups:
        pid = g["project_id"]
        pname = scalar("SELECT name FROM projects WHERE id = ?", (pid,)) if pid else ""
        summary = "[记工] %s 人工费%s" % (ym, (" · " + pname) if pname else "")
        amt = round2(g["amount"])
        old = one("SELECT id FROM costs WHERE summary = ? "
                  "AND IFNULL(project_id,0) = IFNULL(?,0)", (summary, pid))
        memo = ("由「记工」自动汇总：%s，%d 人 / %s 个工%s。"
                "改了记工再点一次会更新这条的金额。" % (
                    ym, g["n"], money_plain(g["days"]),
                    ("（%s）" % actor if actor else "")))
        if old:
            execute("UPDATE costs SET amount = ?, category = ? WHERE id = ?",
                    (amt, cat_id, old["id"]))
            updated += 1
            cid, action = old["id"], "updated"
        else:
            cid = execute(
                "INSERT INTO costs (code, project_id, category, summary, cost_date, "
                "amount, pay_status, paid_amount, settle_type, approval_status, memo) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (gen_code("costs"), pid, cat_id, summary, last_day, amt,
                 "未支付", 0.0, meta.NO_REIMBURSE, meta.NO_REIMBURSE, memo))
            created += 1
            action = "created"
        items.append({"id": cid, "project": pname or "（未指定项目）",
                      "people": g["n"], "days": round2(g["days"]), "amount": amt,
                      "action": action})
    return {"created": created, "updated": updated, "items": items}


def money_plain(v):
    """工天这种小数：1.0 → 1、1.5 → 1.5（不要千分位）。"""
    v = float(v or 0)
    return ("%d" % v) if v == int(v) else ("%g" % round(v, 2))

