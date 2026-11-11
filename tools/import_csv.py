# -*- coding: utf-8 -*-
"""把 CSV 导入到指定实体表（用于从 Access 版迁移历史数据）。

用法：
    python tools/import_csv.py contracts "D:\\导出\\合同.csv" --dry-run
    python tools/import_csv.py contracts "D:\\导出\\合同.csv"

CSV 表头支持两种写法：
  1) 中文标签，例如 合同编号,合同名称,所属项目,合同金额(含税)…
  2) 英文字段名，例如 code,name,project_id,amount_gross…
关联字段（所属项目 / 对方单位 / 关联合同）请填“名称或编号”，脚本会自动换算成 ID。
"""
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import meta                      # noqa: E402
from db import _connect, init_db, now_str   # noqa: E402
from services import calc_tax, round2        # noqa: E402


def read_csv(path):
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
        try:
            with open(path, "r", encoding=enc, newline="") as fh:
                rows = list(csv.DictReader(fh))
            return rows, enc
        except UnicodeDecodeError:
            continue
    raise SystemExit("无法识别文件编码，请另存为 UTF-8 或 GBK 编码的 CSV")


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        raise SystemExit(1)
    key, path = sys.argv[1], sys.argv[2]
    dry = "--dry-run" in sys.argv
    ent = meta.ENTITIES.get(key)
    if not ent:
        raise SystemExit("未知实体：%s，可选：%s" % (key, ", ".join(meta.ENTITIES)))
    if not os.path.exists(path):
        raise SystemExit("文件不存在：" + path)

    init_db()
    conn = _connect()
    conn.row_factory = None

    label2field = {}
    for fld in ent["fields"]:
        label2field[fld["label"]] = fld["name"]
        label2field[fld["name"]] = fld["name"]

    rows, enc = read_csv(path)
    print(f"读取 {len(rows)} 行（编码 {enc}）")
    if not rows:
        return
    header = list(rows[0].keys())
    fields = [label2field.get(h.strip()) for h in header]
    unknown = [h for h, f in zip(header, fields) if not f]
    if unknown:
        print("提示：以下列无法识别，将被忽略 -> " + "、".join(unknown))
    use = [(h, f) for h, f in zip(header, fields) if f]

    lookups = {}   # 字段名 -> {任意可读值: id}
    for fld in ent["fields"]:
        if fld["type"] == "lookup" and fld.get("options_sql"):
            tbl = fld["options_sql"].split("FROM")[1].strip().split()[0]
            cur = conn.execute(f"SELECT * FROM {tbl}")
            cols = [d[0] for d in cur.description]
            m = {}
            for r in cur.fetchall():
                d = dict(zip(cols, r))
                for c in cols:
                    if d.get(c) not in (None, ""):
                        m[str(d[c])] = d["id"]
            lookups[fld["name"]] = m

    types = {fld["name"]: fld["type"] for fld in ent["fields"]}
    calc = ent.get("calc")
    ok = skip = 0
    errors = []
    for i, r in enumerate(rows, start=2):
        data = {}
        for h, f in use:
            v = (r.get(h) or "").strip()
            t = types[f]
            if t == "lookup":
                if v == "":
                    data[f] = None
                else:
                    rid = lookups.get(f, {}).get(v)
                    if rid is None:
                        errors.append(f"第 {i} 行：{f} 找不到「{v}」")
                    data[f] = rid
            elif t in ("money", "rate", "number"):
                try:
                    data[f] = float(v.replace(",", "")) if v else 0
                except ValueError:
                    data[f] = 0
            elif t == "checkbox":
                data[f] = 1 if v in ("1", "是", "Y", "y", "true", "True") else 0
            else:
                data[f] = v or None
        if calc and data.get(calc["gross"]) is not None:
            net, tax = calc_tax(data.get(calc["gross"]), data.get(calc["rate"]))
            data[calc["net"]] = net
            data[calc["tax"]] = tax

        cols = [f for f in data if data[f] is not None]
        if not cols:
            skip += 1
            continue
        sql = (f"INSERT INTO {ent['table']} ({', '.join(cols)}) "
               f"VALUES ({', '.join('?' * len(cols))})")
        if dry:
            ok += 1
            continue
        try:
            conn.execute(sql, [data[c] for c in cols])
            ok += 1
        except Exception as e:      # noqa: BLE001
            errors.append(f"第 {i} 行插入失败：{e}")
    if not dry:
        conn.commit()
    conn.close()

    print(f"{'（试运行）' if dry else ''}成功 {ok} 行，跳过 {skip} 行，错误 {len(errors)} 条")
    for e in errors[:20]:
        print("  ! " + e)
    if errors:
        print(f"（共 {len(errors)} 条错误，仅显示前 20 条）")


if __name__ == "__main__":
    main()
