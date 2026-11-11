# -*- coding: utf-8 -*-
"""灌入演示数据（可选）。用法：python seed_sample.py [--force]

覆盖四种典型核销情形：一付对一票、一付对多票、一票多付、按比例部分核销。
"""
import sys
from datetime import datetime

from db import DB_PATH, init_db, _connect
from services import calc_tax, round2

PROJECTS = [
    ("XM-0001", "项目一", "公司一", "某市一区",
     "员工七", "2026-03-01", "", "施工中", 9500000),
    ("XM-0002", "项目二", "公司七", "某市二区",
     "员工八", "2026-04-01", "", "施工中", 7200000),
    ("XM-0003", "项目三", "公司八", "某市三区",
     "员工九", "2026-05-01", "", "未开工", 4300000),
]

UNITS = [
    ("公司一", "业主单位", "000000000000000000", "员工一", "0000-0000001", "某市一区一号路 1 号"),
    ("公司二", "总包单位", "000000000000000000", "员工二", "0000-0000002", "某市二区二号路 8 号"),
    ("公司三", "材料供应商", "000000000000000000", "员工三", "0000-0000003", "某市物流园 A 区"),
    ("公司四", "设备租赁", "000000000000000000", "员工四", "0000-0000004", "某市城北机电城"),
    ("公司五", "分包单位", "000000000000000000", "员工五", "0000-0000005", "某市三区三号路 66 号"),
    ("公司六", "咨询服务", "000000000000000000", "员工六", "0000-0000006", "某市商务区 B 座 12 层"),
]

# 合同：编号, 名称, 项目序号, 单位序号, 方向, 类别, 签订日期, 含税金额, 税率, 履行状态, 经办人
CONTRACTS = [
    ("HT-0001", "项目一施工总承包合同", 1, 1, "收入合同", "施工合同",
     "2026-02-25", 11300000, 0.09, "履行中", "员工七"),
    ("HT-0002", "项目一材料采购合同", 1, 3, "支出合同", "材料采购",
     "2026-03-05", 2260000, 0.13, "履行中", "员工十"),
    ("HT-0003", "项目二土建施工合同", 2, 2, "收入合同", "施工合同",
     "2026-03-28", 8600000, 0.09, "履行中", "员工八"),
    ("HT-0004", "项目二设备租赁合同", 2, 4, "支出合同", "租赁合同",
     "2026-04-10", 560000, 0.13, "履行中", "员工四"),
    ("HT-0005", "项目三劳务分包合同", 3, 5, "支出合同", "劳务分包",
     "2026-04-26", 3200000, 0.03, "已签订", "员工九"),
    ("HT-0006", "项目二咨询服务合同", 2, 6, "支出合同", "咨询服务",
     "2026-05-08", 180000, 0.06, "已完成", "员工八"),
]

# 付款：编号, 合同序号, 项目序号, 方向, 款项性质, 日期, 金额, 方式, 经办人
PAYMENTS = [
    ("FK-0001", 1, 1, "收入合同", "预付款", "2026-03-05", 2000000, "银行转账", "员工七"),
    ("FK-0002", 2, 1, "支出合同", "预付款", "2026-03-12", 300000, "银行转账", "员工十"),
    ("FK-0003", 3, 2, "收入合同", "进度款", "2026-04-18", 2580000, "银行转账", "员工八"),
    ("FK-0004", 4, 2, "支出合同", "进度款", "2026-04-22", 280000, "银行转账", "员工四"),
    ("FK-0005", 5, 3, "支出合同", "进度款", "2026-05-09", 800000, "银行转账", "员工九"),
    ("FK-0006", 2, 1, "支出合同", "结算款", "2026-05-26", 1600000, "银行转账", "员工十"),
    ("FK-0007", 6, 2, "支出合同", "结算款", "2026-06-15", 90000, "银行转账", "员工八"),
    ("FK-0008", 1, 1, "收入合同", "进度款", "2026-06-28", 3500000, "银行转账", "员工七"),
    ("FK-0009", 4, 2, "支出合同", "进度款", "2026-07-06", 500000, "银行转账", "员工四"),
    ("FK-0010", 3, 2, "收入合同", "质保金", "2026-07-20", 430000, "银行转账", "员工八"),
    ("FK-0011", 2, 1, "支出合同", "进度款", "2026-07-22", 150000, "银行转账", "员工十"),
    ("FK-0012", 2, 1, "支出合同", "结算款", "2026-08-05", 250000, "银行转账", "员工十"),
]

# 发票：号码, 代码, 类型, 方向, 合同序号, 项目序号, 单位序号, 日期, 价税合计, 税率, 状态
INVOICES = [
    ("00000000000000000001", "011002601000", "增值税专用发票", "收到进项发票", 2, 1, 3, "2026-03-10", 300000, 0.13, "已抵扣"),
    ("00000000000000000002", "011002601000", "增值税专用发票", "收到进项发票", 2, 1, 3, "2026-05-20", 1600000, 0.13, "已抵扣"),
    ("00000000000000000003", "011002601000", "增值税普通发票", "开具销项发票", 1, 1, 1, "2026-03-02", 2000000, 0.09, "已收讫"),
    ("00000000000000000004", "011002601000", "增值税专用发票", "开具销项发票", 3, 2, 2, "2026-04-15", 2580000, 0.09, "已认证"),
    ("00000000000000000005", "011002601000", "增值税专用发票", "收到进项发票", 4, 2, 4, "2026-04-20", 280000, 0.13, "已认证"),
    ("00000000000000000006", "011002601000", "增值税专用发票", "收到进项发票", 5, 3, 5, "2026-05-06", 800000, 0.03, "已认证"),
    ("00000000000000000007", "011002601000", "增值税专用发票", "收到进项发票", 6, 2, 6, "2026-06-10", 90000, 0.06, "已认证"),
    ("00000000000000000008", "011002601000", "增值税普通发票", "开具销项发票", 1, 1, 1, "2026-06-25", 3500000, 0.09, "已认证"),
    ("00000000000000000009", "011002601000", "增值税专用发票", "收到进项发票", 4, 2, 4, "2026-07-02", 200000, 0.13, "待认证"),
    ("00000000000000000010", "011002601000", "增值税专用发票", "收到进项发票", 4, 2, 4, "2026-07-05", 300000, 0.13, "待认证"),
    ("00000000000000000011", "011002601000", "增值税专用发票", "收到进项发票", 2, 1, 3, "2026-07-18", 400000, 0.13, "待认证"),
    ("00000000000000000012", "011002601000", "增值税专用发票", "收到进项发票", 2, 1, 3, "2026-08-10", 150000, 0.13, "待认证"),
]

# 费用：编号, 项目序号, 合同序号, 单位序号, 类别, 摘要, 日期, 金额, 支付状态, 经办人
COSTS = [
    ("FY-0001", 1, 1, 0, "人工费", "路基施工班组 3 月工资", "2026-03-31", 186000, "已支付", "员工七"),
    ("FY-0002", 1, 2, 3, "材料费", "商品混凝土首批货款", "2026-04-08", 320000, "已支付", "员工十"),
    ("FY-0003", 1, 0, 4, "机械费", "压路机、摊铺机台班费", "2026-04-30", 68000, "已支付", "员工四"),
    ("FY-0004", 1, 0, 0, "措施费", "安全文明施工费", "2026-05-12", 45000, "已支付", "员工七"),
    ("FY-0005", 2, 0, 0, "管理费", "项目部办公及差旅费", "2026-05-31", 52000, "部分支付", "员工八"),
    ("FY-0006", 2, 4, 4, "机械费", "污水厂吊装设备租赁", "2026-06-08", 96000, "已支付", "员工四"),
    ("FY-0007", 3, 5, 5, "分包费", "人才公寓劳务分包首期", "2026-06-20", 240000, "未支付", "员工九"),
    ("FY-0008", 2, 6, 6, "其他", "造价咨询尾款", "2026-07-15", 18000, "已支付", "员工八"),
]

# 核销：(付款序号, 发票序号, 核销金额, 备注)
SETTLEMENTS = [
    (1, 3, 2000000, "预付款全额对应销项发票"),
    (2, 1, 300000, "预付款对应材料发票"),
    (3, 4, 2580000, ""),
    (4, 5, 280000, ""),
    (5, 6, 800000, ""),
    (6, 2, 1600000, ""),
    (7, 7, 90000, ""),
    (8, 8, 2450000, "按 70% 部分收款"),
    (9, 9, 200000, "一笔付款对应两张发票"),
    (9, 10, 300000, "一笔付款对应两张发票"),
    (11, 11, 150000, "一张发票由两笔付款合计核销"),
    (12, 11, 250000, "一张发票由两笔付款合计核销"),
]

TABLES = ["settlements", "payments", "invoices", "costs", "contracts",
          "projects", "units"]


def main():
    force = "--force" in sys.argv
    init_db()
    conn = _connect()
    try:
        n = conn.execute("SELECT COUNT(*) c FROM projects").fetchone()["c"]
        if n and not force:
            print(f"数据库已有 {n} 个项目，未灌入演示数据。"
                  f"如确需覆盖，请加 --force（会先清空业务表）。")
            return
        if force:
            for t in TABLES:
                conn.execute(f"DELETE FROM {t}")
            conn.commit()

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        def ins(sql, args):
            cur = conn.execute(sql, args)
            return cur.lastrowid

        pid = {}
        for code, name, owner, loc, mgr, s, e, st, budget in PROJECTS:
            pid[code] = ins(
                "INSERT INTO projects (code,name,owner_name,location,manager,"
                "start_date,end_date,status,budget) VALUES (?,?,?,?,?,?,?,?,?)",
                (code, name, owner, loc, mgr, s, e, st, budget))
        uid = {}
        for name, typ, tax_no, contact, phone, addr in UNITS:
            uid[name] = ins(
                "INSERT INTO units (name,type,tax_no,contact,phone,address) "
                "VALUES (?,?,?,?,?,?)", (name, typ, tax_no, contact, phone, addr))

        cids = []
        for (code, name, pi, ui, direction, cat, d, gross, rate, status, handler) in CONTRACTS:
            net, tax = calc_tax(gross, rate)
            cids.append(ins(
                "INSERT INTO contracts (code,name,project_id,unit_id,direction,category,"
                "sign_date,amount_gross,tax_rate,amount_net,tax_amount,pay_method,status,"
                "handler) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (code, name, list(pid.values())[pi - 1], list(uid.values())[ui - 1],
                 direction, cat, d, gross, rate, net, tax, "银行转账", status, handler)))

        pays = []
        for (code, ci, pi, direction, nature, d, amt, method, handler) in PAYMENTS:
            pays.append(ins(
                "INSERT INTO payments (code,contract_id,project_id,direction,nature,"
                "pay_date,amount,pay_method,handler) VALUES (?,?,?,?,?,?,?,?,?)",
                (code, cids[ci - 1], list(pid.values())[pi - 1], direction, nature,
                 d, amt, method, handler)))

        invs = []
        for (no, code, typ, direction, ci, pi, ui, d, gross, rate, status) in INVOICES:
            net, tax = calc_tax(gross, rate)
            invs.append(ins(
                "INSERT INTO invoices (no,code,type,direction,contract_id,project_id,"
                "unit_id,issue_date,amount_gross,tax_rate,amount_net,tax_amount,status) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (no, code, typ, direction, cids[ci - 1], list(pid.values())[pi - 1],
                 list(uid.values())[ui - 1], d, gross, rate, net, tax, status)))

        for (code, pi, ci, ui, cat, summary, d, amt, pay_status, handler) in COSTS:
            ins("INSERT INTO costs (code,project_id,contract_id,unit_id,category,summary,"
                "cost_date,amount,pay_status,handler) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (code, list(pid.values())[pi - 1],
                 cids[ci - 1] if ci else None,
                 list(uid.values())[ui - 1] if ui else None,
                 cat, summary, d, amt, pay_status, handler))

        for (pi, ii, amt, memo) in SETTLEMENTS:
            inv_id = invs[ii - 1]
            gross = conn.execute("SELECT amount_gross FROM invoices WHERE id=?",
                                 (inv_id,)).fetchone()["amount_gross"]
            ratio = round2(amt) / gross if gross else 0
            ins("INSERT INTO settlements (payment_id,invoice_id,amount,ratio,memo) "
                "VALUES (?,?,?,?,?)", (pays[pi - 1], inv_id, amt, round(ratio, 4), memo))

        conn.commit()
        print(f"演示数据已写入：{len(PROJECTS)} 个项目 / {len(UNITS)} 家单位 / "
              f"{len(CONTRACTS)} 份合同 / {len(PAYMENTS)} 笔收付款 / "
              f"{len(INVOICES)} 张发票 / {len(COSTS)} 笔费用 / "
              f"{len(SETTLEMENTS)} 条核销")
        print("数据库位置：" + str(DB_PATH))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
