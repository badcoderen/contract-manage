# -*- coding: utf-8 -*-
"""SQLite 连接、建库、种子数据与通用查询封装。"""
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from flask import g

import meta

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE_DIR / "data")))
DB_PATH = Path(os.environ.get("DB_PATH", str(DATA_DIR / "app.db")))
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", str(DATA_DIR / "uploads")))
SCHEMA_PATH = BASE_DIR / "schema.sql"


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _connect():
    conn = sqlite3.connect(str(DB_PATH), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def get_db():
    if "db" not in g:
        g.db = _connect()
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def query(sql, args=()):
    cur = get_db().execute(sql, args)
    rows = [dict(r) for r in cur.fetchall()]
    cur.close()
    return rows


def one(sql, args=()):
    rows = query(sql, args)
    return rows[0] if rows else None


def scalar(sql, args=(), default=0):
    v = one(sql, args)
    if not v:
        return default
    val = list(v.values())[0]
    return default if val is None else val


def execute(sql, args=()):
    db = get_db()
    cur = db.execute(sql, args)
    db.commit()
    last = cur.lastrowid
    cur.close()
    return last


def execute_many(sql, seq):
    db = get_db()
    cur = db.executemany(sql, seq)
    db.commit()
    cur.close()


# ------------------------------------------------------------------ 初始化
def init_db(force_reset=False):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    if force_reset and DB_PATH.exists():
        DB_PATH.unlink()
    fresh = not DB_PATH.exists()

    conn = _connect()
    try:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.commit()
        _migrate(conn)
        if fresh or conn.execute("SELECT COUNT(*) c FROM dicts").fetchone()["c"] == 0:
            _seed_dicts(conn)
        else:
            n = _seed_dicts(conn, only_missing=True)   # 新版本新增的字典类目
            if n:
                print(f"[init] 补充 {n} 条下拉字典（新增类目）")
        if fresh or conn.execute("SELECT COUNT(*) c FROM cost_categories").fetchone()["c"] == 0:
            _seed_categories(conn)
        if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0:
            _seed_admin(conn)
        _seed_settings(conn)
    finally:
        conn.close()
    return fresh


# 老库升级：CREATE TABLE IF NOT EXISTS 不会给已存在的表加列，这里补齐。
# ★ 全部是「加列」，不删不改已有列，因此不会影响已录入的数据。
MIGRATIONS = {
    "users": [
        ("role", "TEXT NOT NULL DEFAULT 'employee'"),
        ("enabled", "INTEGER NOT NULL DEFAULT 1"),
        ("must_change_pwd", "INTEGER NOT NULL DEFAULT 0"),
        ("updated_at", "TEXT"),
        ("last_login_at", "TEXT"),
        ("last_login_ip", "TEXT"),
        # 员工薪资资料（用于工资与补贴测算）
        ("monthly_salary", "REAL DEFAULT 0"),
        ("emp_no", "TEXT"),
        ("hire_date", "TEXT"),
        ("memo", "TEXT"),
    ],
    "projects": [
        # 项目负责人（员工账号）：该员工可查看/录入此项目
        ("manager_user_id", "INTEGER REFERENCES users(id)"),
    ],
    "contracts": [
        # 质保：开始日期 + 保修期限（月）；个别项目另有约定的写在 warranty_memo
        ("warranty_start", "TEXT"),
        ("warranty_months", "REAL DEFAULT 0"),
        ("warranty_memo", "TEXT"),
    ],
    "quotation_items": [
        # 逐行标含税口径与税点（原来只有表头一个统一税率）
        ("tax_included", "TEXT"),
        ("tax_rate", "TEXT"),
    ],
    "contract_items": [
        ("tax_included", "TEXT"),
        ("tax_rate", "TEXT"),
    ],
    "private_settlement_items": [
        # 明细行属于哪张合同：一个对私单可以关联多张合同，行上再标明归属，
        # 留空＝不指定（纯成本行当"共同成本"）
        ("row_contract_id", "INTEGER REFERENCES contracts(id)"),
    ],
    "delivery_items": [
        # 送货明细要记住对应哪条合同清单项，才能算"还没送的数量"
        ("contract_item_id", "INTEGER REFERENCES contract_items(id)"),
    ],
    "invoices": [
        # 发票状态是不是系统按「核销满了」自动标的（1＝系统标的，0＝人工选的）。
        # 核销被撤销时，系统只收回自己标的「已收讫」，人工标的一律不动。
        ("paid_auto", "INTEGER DEFAULT 0"),
        # 进项发票＝成本：成本归到哪个费用类别（cost_categories.id）。
        # 老库按它已经自动生成的那条费用回填，见 _migrate()。
        ("category", "TEXT"),
    ],
    # 记工：第二工资（月表里点成「2」时按它算一整天）+ 每条记的是哪种价
    "workers": [
        ("rate2", "REAL DEFAULT 0"),
    ],
    "worklogs": [
        # '默认' / '第二' / 空（手工改过的价）—— 只用于还原月表格子的显示，不参与算钱
        ("rate_src", "TEXT"),
    ],
    "staff": [
        # 有些员工不缴社保（如退休返聘），个人部分记 0
        ("social_off", "INTEGER DEFAULT 0"),
        # 离职日期：离职当月按天折算工资，之后的月份不再进工资表
        ("leave_date", "TEXT"),
    ],
    "payroll": [
        # 手工调整标记：离职折算只对"没手工改过"的行生效，
        # 否则用户在【调整】里改完保存，读回来又被折算盖掉，看着就是"改不了"
        ("manual", "INTEGER DEFAULT 0"),
        # 2026-11-11：加班时数与出差天数可以直接录入（不必先提申请），
        # 加班费 / 出差费 / 餐补都按这两个数自动算
        ("ot_hours", "REAL DEFAULT 0"),
        ("trip_days", "REAL DEFAULT 0"),
    ],
    "handover_devices": [
        # 工程交付资料的设备清单：补上子网掩码与网关
        ("netmask", "TEXT"),
        ("gateway", "TEXT"),
    ],
    "costs": [
        # 由进项发票自动生成的费用 → 记住是哪个发票（删票据/作废时要跟着走）
        ("invoice_id", "INTEGER"),
        # 实际已经付出去的钱（第 14 轮）：首页「累计付款」按它统计，
        # 因为用户实际付出去的钱大多记在费用支出里，而不是「付款记录」
        ("paid_amount", "REAL DEFAULT 0"),
        # 费用报销扩展
        ("employee_id", "INTEGER REFERENCES users(id)"),      # 报销人（员工）
        ("settle_type", "TEXT"),                              # 垫付报销 / 预支核销
        ("advance_id", "INTEGER REFERENCES advances(id)"),    # 预支核销时关联的预支单
        ("approval_status", "TEXT"),                          # 待审批/已批准/已驳回/已打款
        ("approver_id", "INTEGER"),
        ("approved_at", "TEXT"),
        ("approve_memo", "TEXT"),
        ("paid_at", "TEXT"),
        ("paid_by", "INTEGER"),
    ],
}

# 「只加索引」的增量升级：不动任何一行数据，纯粹给查询加路。
# 必须在 _migrate() 补完列**之后**执行 —— 有的索引建在迁移列上
# （如 costs.employee_id），在 schema.sql 里建会因为列还不存在直接报错。
# 全部是 IF NOT EXISTS，反复执行无副作用。
EXTRA_INDEXES = [
    # 首页概览 / 项目成本 / 往来单位台账都按「项目」汇总付款与发票
    "CREATE INDEX IF NOT EXISTS ix_payments_project ON payments(project_id)",
    "CREATE INDEX IF NOT EXISTS ix_invoices_project ON invoices(project_id)",
    "CREATE INDEX IF NOT EXISTS ix_invoices_unit ON invoices(unit_id)",
    # 员工只能看「自己项目的 + 自己报销的」，按报销人过滤要走索引
    "CREATE INDEX IF NOT EXISTS ix_costs_employee ON costs(employee_id)",
]


def _migrate(conn):
    added = []
    for table, cols in MIGRATIONS.items():
        exist = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not exist:
            continue
        for col, spec in cols:
            if col not in exist:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {spec}")
                added.append(f"{table}.{col}")
    # 角色升级：新增「员工」角色后，老的 editor 一律转为 employee。
    # 本句幂等，且只改角色字段，不动业务数据。
    n = conn.execute("UPDATE users SET role = 'employee' WHERE role = 'editor'").rowcount
    # 角色的历史遗留值兜底
    conn.execute("UPDATE users SET role = 'employee' "
                 "WHERE role IS NULL OR role = ''")
    if n:
        added.append(f"users.role(editor→employee, {n} 个账号)")
    conn.execute("UPDATE users SET enabled = 1 WHERE enabled IS NULL")

    # ---- 字典值调整：「付款方式」纳入微信 / 支付宝，措辞统一为「银行打款」
    # 判断依据是"还残留旧值"，这样无论 _seed_dicts 先跑还是后跑都能收敛，
    # 且可重复执行（第二次已经没有旧值，直接跳过）。
    LEGACY_PAY = {"银行转账", "承兑汇票", "支票"}
    new_pay = list(meta.DICT_SEED["付款方式"])
    cur_pay = {r["value"] for r in conn.execute(
        "SELECT value FROM dicts WHERE category = '付款方式'")}
    if cur_pay & LEGACY_PAY:
        conn.execute("DELETE FROM dicts WHERE category = '付款方式'")
        conn.executemany(
            "INSERT INTO dicts (category, value, sort) VALUES (?,?,?)",
            [("付款方式", v, (i + 1) * 10) for i, v in enumerate(new_pay)])
        # 历史数据里的旧写法一并换掉，否则老记录打开时下拉是空的
        remap = {"银行转账": "银行打款", "承兑汇票": "其他", "支票": "其他"}
        touched = 0
        for tbl in ("payments", "advances"):
            for old_v, new_v in remap.items():
                touched += conn.execute(
                    f"UPDATE {tbl} SET pay_method = ? WHERE pay_method = ?",
                    (new_v, old_v)).rowcount
        added.append("付款方式字典 → %s（同步历史数据 %d 条）"
                     % ("/".join(new_pay), touched))

    # ---- 费用的「已付金额」回填（第 14 轮，只在**首次加列**时跑一次）
    # 语义：已支付 ⇒ 已付 = 金额（额付清了）；其余（未支付 / 部分支付 / 空）⇒ 0。
    # 不在每次启动时跑，避免和用户后来手工填的值打架。
    if "costs.paid_amount" in added:
        n_paid = conn.execute(
            "UPDATE costs SET paid_amount = ROUND(IFNULL(amount,0), 2) "
            "WHERE IFNULL(pay_status,'') = '已支付'").rowcount
        added.append("费用「已付金额」按支付状态回填（已支付的 %d 条按全额，其余 0）"
                     % n_paid)

    # ---- 进项发票的「费用类别」：按它已经自动生成的那条费用回填（只在新加列时跑一次）
    if "invoices.category" in added:
        n_cat = conn.execute(
            "UPDATE invoices SET category = (SELECT k.category FROM costs k "
            "  WHERE k.invoice_id = invoices.id ORDER BY k.id LIMIT 1) "
            " WHERE category IS NULL AND EXISTS "
            "  (SELECT 1 FROM costs k WHERE k.invoice_id = invoices.id)").rowcount
        added.append("发票「费用类别」按已生成的费用回填（%d 张）" % n_cat)

    # ---- 字典收敛：「发票状态」改成三个（费用未付款 / 已收讫 / 已作废）
    # 语义从"税务认证状态"改成"付款状态"，所以老值（待认证/已认证/已抵扣）
    # 一律落到「费用未付款」——不猜"钱付没付"。已收讫/已作废原样保留。
    LEGACY_INV = {"待认证", "已认证", "已抵扣"}
    new_inv = list(meta.DICT_SEED["发票状态"])
    cur_inv = {r["value"] for r in conn.execute(
        "SELECT value FROM dicts WHERE category = '发票状态'")}
    if cur_inv & LEGACY_INV:
        conn.execute("DELETE FROM dicts WHERE category = '发票状态'")
        conn.executemany(
            "INSERT INTO dicts (category, value, sort) VALUES (?,?,?)",
            [("发票状态", v, (i + 1) * 10) for i, v in enumerate(new_inv)])
        n_inv = conn.execute(
            "UPDATE invoices SET status = '费用未付款' WHERE status IN (?,?,?)",
            tuple(LEGACY_INV)).rowcount
        added.append("发票状态字典 → %s（历史发票 %d 张改为「费用未付款」）"
                     % ("/".join(new_inv), n_inv))

    conn.commit()
    # ---- 字典新增值：给**已存在**的类目补值
    # _seed_dicts(only_missing=True) 只补"整个类目都不存在"的，所以往老类目里
    # 加一个新值必须在这里显式补一次（幂等）。
    if "报销方式" in {r["category"] for r in conn.execute(
            "SELECT DISTINCT category FROM dicts")}:
        have = {r["value"] for r in conn.execute(
            "SELECT value FROM dicts WHERE category = '报销方式'")}
        if meta.NO_REIMBURSE not in have:
            nxt = 10 * (len(have) + 1)
            conn.execute("INSERT INTO dicts (category, value, sort) VALUES (?,?,?)",
                         ("报销方式", meta.NO_REIMBURSE, nxt))
            added.append("字典「报销方式」+「%s」" % meta.NO_REIMBURSE)

    # ---- 费用类别：把历史上存成"名称"的旧值换成 cost_categories.id
    # （category 是 lookup 字段，正统存 id；混着存会让列表显示数字、按类别统计全变 0）
    try:
        n = conn.execute(
            "UPDATE costs SET category = (SELECT CAST(c.id AS TEXT) "
            "  FROM cost_categories c WHERE c.name = costs.category) "
            " WHERE category IN (SELECT name FROM cost_categories)").rowcount
        if n:
            added.append("费用类别统一成 id（%d 条）" % n)
    except sqlite3.Error as e:
        print("[migrate] 费用类别归一跳过：%s" % e)

    # ---- 纯索引升级（不动数据）
    for sql in EXTRA_INDEXES:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError as e:
            print("[migrate] 建索引跳过：%s（%s）" % (sql.split()[5], e))
    conn.commit()

    if added:
        print("[migrate] 已完成增量升级：" + ", ".join(added))


def _seed_settings(conn):
    rows = [(k, str(v), memo) for k, v, memo in meta.SETTINGS_SEED]
    conn.executemany(
        "INSERT OR IGNORE INTO settings (key, value, memo, updated_at) "
        "VALUES (?,?,?,?)",
        [(k, v, m, now_str()) for k, v, m in rows])
    conn.commit()


def _seed_dicts(conn, only_missing=False):
    """写入下拉字典种子。

    only_missing=True 时只补"整个类目都不存在"的（老库升级新增了类目），
    已存在的类目不动 —— 用户可能故意删掉了某些值，不能被重新塞回来。
    """
    seed = meta.DICT_SEED
    if only_missing:
        have = {r["category"] for r in conn.execute(
            "SELECT DISTINCT category FROM dicts")}
        seed = {c: v for c, v in seed.items() if c not in have}
        if not seed:
            return 0
    rows = []
    for cat, values in seed.items():
        for i, v in enumerate(values):
            rows.append((cat, v, (i + 1) * 10))
    conn.executemany(
        "INSERT OR IGNORE INTO dicts (category, value, sort) VALUES (?,?,?)", rows)
    conn.commit()
    return len(rows)


def _seed_categories(conn):
    rows = [(n, attr, 1, s, memo) for n, attr, s, memo in meta.COST_CAT_SEED]
    conn.executemany(
        "INSERT OR IGNORE INTO cost_categories (name, cost_attr, enabled, sort, memo) "
        "VALUES (?,?,?,?,?)", rows)
    conn.commit()


def _seed_admin(conn):
    from werkzeug.security import generate_password_hash
    user = os.environ.get("ADMIN_USER", "admin")
    pwd = os.environ.get("ADMIN_PASS", "admin123")
    # 默认强制首登改密，避免默认口令被外网爆破
    force = os.environ.get("FORCE_PASSWORD_CHANGE", "1") == "1"
    conn.execute(
        "INSERT INTO users (username, password_hash, display_name, role, enabled, "
        "must_change_pwd, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (user, generate_password_hash(pwd), "系统管理员", "admin", 1,
         1 if force else 0, now_str(), now_str()))
    conn.commit()


def dict_options(category):
    return [r["value"] for r in query(
        "SELECT value FROM dicts WHERE category = ? ORDER BY sort, value", (category,))]


def lookup_options(sql):
    try:
        rows = query(sql)
    except sqlite3.Error:
        return []
    out = []
    for r in rows:
        vals = list(r.values())
        out.append((vals[0], vals[1] if len(vals) > 1 else vals[0]))
    return out
