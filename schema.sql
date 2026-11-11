-- 工程合同・费用・发票管理系统  (Web 版 / SQLite)
-- 与 Access 版数据模型一一对应

-- 登录账号：两种角色
--   admin  管理员：全部权限，含删除、用户管理、字典/费用类别维护
--   editor 录入查询：可新增/修改/查询/导出，不能删除、不能进管理类模块
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT NOT NULL UNIQUE,
    password_hash   TEXT NOT NULL,
    display_name    TEXT,
    role            TEXT NOT NULL DEFAULT 'editor',
    enabled         INTEGER NOT NULL DEFAULT 1,
    must_change_pwd INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT,
    updated_at      TEXT,
    last_login_at   TEXT,
    last_login_ip   TEXT
);

-- 登录审计与防爆破（同时按账号和 IP 限速）
CREATE TABLE IF NOT EXISTS login_logs (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT,
    ip       TEXT,
    ok       INTEGER NOT NULL DEFAULT 0,
    reason   TEXT,
    at       TEXT
);
CREATE INDEX IF NOT EXISTS ix_login_logs_at ON login_logs(at);
CREATE INDEX IF NOT EXISTS ix_login_logs_user ON login_logs(username);

CREATE TABLE IF NOT EXISTS projects (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    code       TEXT,
    name       TEXT NOT NULL,
    owner_name TEXT,
    location   TEXT,
    manager    TEXT,
    start_date TEXT,
    end_date   TEXT,
    status     TEXT,
    budget     REAL DEFAULT 0,
    memo       TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_projects_code ON projects(code);

CREATE TABLE IF NOT EXISTS units (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name    TEXT NOT NULL,
    type    TEXT,
    tax_no  TEXT,
    contact TEXT,
    phone   TEXT,
    address TEXT,
    memo    TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_units_name ON units(name);

CREATE TABLE IF NOT EXISTS contracts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    code            TEXT,
    name            TEXT NOT NULL,
    project_id      INTEGER REFERENCES projects(id),
    unit_id         INTEGER REFERENCES units(id),
    direction       TEXT,
    category        TEXT,
    sign_date       TEXT,
    amount_gross    REAL DEFAULT 0,
    tax_rate        REAL DEFAULT 0,
    amount_net      REAL DEFAULT 0,
    tax_amount      REAL DEFAULT 0,
    prepay_ratio    REAL DEFAULT 0,
    retention_ratio REAL DEFAULT 0,
    pay_method      TEXT,
    status          TEXT,
    -- 质保：开始日期 + 期限（月），到期日由程序算；warranty_memo 记个别项目的单独约定
    warranty_start  TEXT,
    warranty_months REAL DEFAULT 0,
    warranty_memo   TEXT,
    handler         TEXT,
    memo            TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_contracts_code ON contracts(code);
CREATE INDEX IF NOT EXISTS ix_contracts_project ON contracts(project_id);
CREATE INDEX IF NOT EXISTS ix_contracts_unit ON contracts(unit_id);

CREATE TABLE IF NOT EXISTS payments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT,
    contract_id INTEGER REFERENCES contracts(id),
    project_id  INTEGER REFERENCES projects(id),
    direction   TEXT,
    nature      TEXT,
    pay_date    TEXT,
    amount      REAL DEFAULT 0,
    pay_method  TEXT,
    handler     TEXT,
    memo        TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_payments_code ON payments(code);
CREATE INDEX IF NOT EXISTS ix_payments_contract ON payments(contract_id);
-- 2026-10-10 补：首页概览 / 项目成本 / 往来单位台账都按「项目」汇总付款，
-- 之前只有 contract_id 索引 —— 每个项目都要全表扫一遍付款表。
CREATE INDEX IF NOT EXISTS ix_payments_project ON payments(project_id);
-- 付款记录的默认排序是 pay_date DESC，翻页时走这个索引不用整表排序
CREATE INDEX IF NOT EXISTS ix_payments_date ON payments(pay_date);

CREATE TABLE IF NOT EXISTS invoices (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    no           TEXT,
    code         TEXT,
    type         TEXT,
    direction    TEXT,
    contract_id  INTEGER REFERENCES contracts(id),
    project_id   INTEGER REFERENCES projects(id),
    unit_id      INTEGER REFERENCES units(id),
    -- 进项发票＝成本：这笔成本归到哪个费用类别（cost_categories.id）。
    -- 销项发票留空；进项发票填了，自动生成的费用就用这个类别。
    category     TEXT,
    issue_date   TEXT,
    amount_gross REAL DEFAULT 0,
    tax_rate     REAL DEFAULT 0,
    amount_net   REAL DEFAULT 0,
    tax_amount   REAL DEFAULT 0,
    status       TEXT,
    -- 这个「已收讫」是系统按「核销满了」自动标的（1），还是人工在下拉里选的（0）：
    -- 核销被撤销时，系统只收回自己标的那个，人工标的一律不动
    paid_auto    INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_invoices_no ON invoices(no);
CREATE INDEX IF NOT EXISTS ix_invoices_contract ON invoices(contract_id);
-- 同上：概览与发票台账按项目/单位汇总发票
CREATE INDEX IF NOT EXISTS ix_invoices_project ON invoices(project_id);
CREATE INDEX IF NOT EXISTS ix_invoices_unit ON invoices(unit_id);
CREATE INDEX IF NOT EXISTS ix_invoices_date ON invoices(issue_date);

-- 付款 <> 发票 核销明细（多对多）
CREATE TABLE IF NOT EXISTS settlements (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    payment_id INTEGER NOT NULL REFERENCES payments(id) ON DELETE CASCADE,
    invoice_id INTEGER REFERENCES invoices(id),
    amount     REAL DEFAULT 0,
    ratio      REAL DEFAULT 0,
    memo       TEXT
);
CREATE INDEX IF NOT EXISTS ix_settle_payment ON settlements(payment_id);
CREATE INDEX IF NOT EXISTS ix_settle_invoice ON settlements(invoice_id);

CREATE TABLE IF NOT EXISTS costs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT,
    project_id  INTEGER REFERENCES projects(id),
    contract_id INTEGER REFERENCES contracts(id),
    unit_id     INTEGER REFERENCES units(id),
    category    TEXT,
    summary     TEXT,
    cost_date   TEXT,
    amount      REAL DEFAULT 0,
    pay_status  TEXT,
    handler     TEXT,
    memo        TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_costs_code ON costs(code);
CREATE INDEX IF NOT EXISTS ix_costs_project ON costs(project_id);
-- cost_date 是默认排序，翻页同理
CREATE INDEX IF NOT EXISTS ix_costs_date ON costs(cost_date);
-- 注意：costs.employee_id 是后来的迁移列（老库靠 ALTER TABLE 补），
-- 所以它的索引放在 db.py 的 EXTRA_INDEXES 里、在补列之后再建。

CREATE TABLE IF NOT EXISTS dicts (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,
    value    TEXT NOT NULL,
    sort     INTEGER DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_dicts ON dicts(category, value);

CREATE TABLE IF NOT EXISTS cost_categories (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    name      TEXT NOT NULL,
    cost_attr TEXT,
    enabled   INTEGER DEFAULT 1,
    sort      INTEGER DEFAULT 0,
    memo      TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_costcat_name ON cost_categories(name);

-- 合同原件等附件（文件落盘，库内只存元数据）
CREATE TABLE IF NOT EXISTS attachments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ref_type    TEXT NOT NULL,
    ref_id      INTEGER NOT NULL,
    orig_name   TEXT,
    stored_name TEXT,
    size        INTEGER DEFAULT 0,
    mime        TEXT,
    uploaded_at TEXT,
    uploader    TEXT
);
CREATE INDEX IF NOT EXISTS ix_attach_ref ON attachments(ref_type, ref_id);

-- =====================================================================
-- 以下为「费用报销 / 出勤管理」扩展（2026-09 新增，全部为增量，不改老表结构）
-- =====================================================================

-- 全局参数（工资、补贴标准等），单表 key-value，可在「参数设置」页维护
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    memo       TEXT,
    updated_at TEXT
);

-- 员工可访问的项目（管理员/财务指定）；另有 projects.manager_user_id 表示"负责的项目"
CREATE TABLE IF NOT EXISTS project_access (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    memo       TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_project_access ON project_access(user_id, project_id);
CREATE INDEX IF NOT EXISTS ix_project_access_user ON project_access(user_id);

-- 页面/模块权限（按账号覆盖角色默认值）
-- 只存"改过默认"的项：allow=1 强制可见，allow=0 强制隐藏；没有行＝跟随角色。
-- kind 与左侧菜单一致：e 实体列表 / q 查询台账 / r 报表 / p 功能页
CREATE TABLE IF NOT EXISTS user_pages (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind     TEXT NOT NULL,
    item_key TEXT NOT NULL,
    allow    INTEGER NOT NULL DEFAULT 1
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_user_pages ON user_pages(user_id, kind, item_key);
CREATE INDEX IF NOT EXISTS ix_user_pages_user ON user_pages(user_id);

-- 预支款：财务先打款给员工，员工后续用费用支出核销
CREATE TABLE IF NOT EXISTS advances (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT,
    employee_id INTEGER REFERENCES users(id),
    amount      REAL DEFAULT 0,          -- 预支金额
    pay_date    TEXT,                    -- 打款日期
    pay_method  TEXT,
    status      TEXT,                    -- 已打款 / 部分核销 / 已核销完
    handler     TEXT,                    -- 打款经办（财务）
    memo        TEXT,
    created_by  INTEGER,
    created_at  TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_advances_code ON advances(code);
CREATE INDEX IF NOT EXISTS ix_advances_emp ON advances(employee_id);

-- 加班申请
CREATE TABLE IF NOT EXISTS overtime (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    code         TEXT,
    employee_id  INTEGER REFERENCES users(id),
    project_id   INTEGER REFERENCES projects(id),
    ot_date      TEXT,                   -- 加班日期
    start_time   TEXT,                   -- 开始时间 HH:MM
    end_time     TEXT,                   -- 结束时间 HH:MM
    hours        REAL DEFAULT 0,         -- 加班时长（小时）
    reason       TEXT,
    status       TEXT,                   -- 待审批 / 已批准 / 已驳回
    approver_id  INTEGER,
    approved_at  TEXT,
    approve_memo TEXT,
    created_at   TEXT,
    updated_at   TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_overtime_code ON overtime(code);
CREATE INDEX IF NOT EXISTS ix_overtime_emp ON overtime(employee_id, ot_date);

-- 请假申请
CREATE TABLE IF NOT EXISTS leaves (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    code         TEXT,
    employee_id  INTEGER REFERENCES users(id),
    leave_type   TEXT,                   -- 事假/病假/年假/调休/其他
    start_date   TEXT,
    end_date     TEXT,
    days         REAL DEFAULT 0,         -- 请假天数
    hours        REAL DEFAULT 0,         -- 请假时长（小时，半天可用）
    reason       TEXT,
    status       TEXT,
    approver_id  INTEGER,
    approved_at  TEXT,
    approve_memo TEXT,
    created_at   TEXT,
    updated_at   TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_leaves_code ON leaves(code);
CREATE INDEX IF NOT EXISTS ix_leaves_emp ON leaves(employee_id, start_date);

-- 出差单（回程日期可先留空，后期按项目进度补填）
CREATE TABLE IF NOT EXISTS trips (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    code          TEXT,
    employee_id   INTEGER REFERENCES users(id),
    project_id    INTEGER REFERENCES projects(id),
    destination   TEXT,                  -- 出差地点
    start_date    TEXT,                  -- 出发日期
    end_date      TEXT,                  -- 回程日期，留空=未定
    days          REAL DEFAULT 0,        -- 出差天数（按已批准区间自动算，可手工覆盖）
    allowance_std REAL DEFAULT 0,        -- 每日补贴标准（留空取全局参数）
    allowance     REAL DEFAULT 0,        -- 出差补贴 = 天数 × 标准
    reason        TEXT,
    status        TEXT,                  -- 待审批 / 已批准 / 已驳回
    approver_id   INTEGER,
    approved_at   TEXT,
    approve_memo  TEXT,
    created_at    TEXT,
    updated_at    TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_trips_code ON trips(code);
CREATE INDEX IF NOT EXISTS ix_trips_emp ON trips(employee_id, start_date);

-- ============================================================ 工资管理
-- 员工花名册：工资名册与登录账号解耦 —— 有些员工只发工资、不用系统，
-- 也照样能录进来参与月度工资测算与工资条导出。
CREATE TABLE IF NOT EXISTS staff (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    emp_no        TEXT,                          -- 工号
    name          TEXT NOT NULL,                 -- 姓名
    id_card       TEXT,                          -- 身份证号
    bank_account  TEXT,                          -- 银行帐号
    dept          TEXT,                          -- 部门
    user_id       INTEGER REFERENCES users(id),  -- 关联登录账号（可空）
    base_salary   REAL DEFAULT 0,                -- 基础工资（月标准）
    post_salary   REAL DEFAULT 0,                -- 岗位津贴
    fund_fixed    REAL DEFAULT 0,                -- 公积金个人月缴额
    social_base   REAL DEFAULT 0,                -- 社保缴费基数（0 = 按基础工资）
    social_off    INTEGER DEFAULT 0,             -- 1 = 不缴社保（个人部分记 0）
    tax_deduct    REAL DEFAULT 0,                -- 每月专项附加扣除合计
    join_date     TEXT,                          -- 入职日期
    status        TEXT DEFAULT '在职',           -- 在职 / 停用
    leave_date    TEXT,                          -- 离职日期：离职当月按天折算，之后自动不再进工资表
    sort          INTEGER DEFAULT 0,
    memo          TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_staff_no ON staff(emp_no);

-- 每人每月的工资明细。自动测算后可以逐项调整；本月一旦保存（locked=1），
-- 之后再点「重新计算」不会覆盖手工改过的数字。
CREATE TABLE IF NOT EXISTS payroll (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ym            TEXT NOT NULL,                 -- 2026-08
    staff_id      INTEGER NOT NULL REFERENCES staff(id),
    work_days     REAL DEFAULT 0,                -- 计薪出勤天数（周一至周六 − 法定假日 + 调休，再扣请假）
    ot_hours      REAL DEFAULT 0,                -- 计入加班费的加班小时（可直接录入，不必先提加班申请）
    trip_days     REAL DEFAULT 0,                -- 计入出差费的出差天数（可直接录入）
    base_salary   REAL DEFAULT 0,
    post_salary   REAL DEFAULT 0,
    bonus         REAL DEFAULT 0,                -- 奖金
    sea_allow     REAL DEFAULT 0,                -- 出海补贴
    trip_allow    REAL DEFAULT 0,                -- 出差补贴
    other_allow   REAL DEFAULT 0,                -- 其他补贴
    overtime      REAL DEFAULT 0,                -- 加班费
    meal_allow    REAL DEFAULT 0,                -- 餐补
    welfare       REAL DEFAULT 0,                -- 福利金
    heat_allow    REAL DEFAULT 0,                -- 高温补贴
    extra_pay     REAL DEFAULT 0,                -- 其他补发
    pension       REAL DEFAULT 0,                -- 养老保险
    medical       REAL DEFAULT 0,                -- 医疗保险
    serious_ill   REAL DEFAULT 0,                -- 大病补充
    unemployment  REAL DEFAULT 0,                -- 失业保险
    fund          REAL DEFAULT 0,                -- 公积金
    annuity       REAL DEFAULT 0,                -- 企业年金
    other_deduct  REAL DEFAULT 0,                -- 其他扣款
    tax           REAL DEFAULT 0,                -- 个人所得税
    locked        INTEGER DEFAULT 0,
    manual        INTEGER DEFAULT 0,   -- 1 = 在【调整】里手工改过：离职折算不再覆盖这一行
    memo          TEXT,
    updated_at    TEXT,
    UNIQUE(ym, staff_id)
);

-- ============================================================ 报价 / 送货
-- 品牌 + 型号（型号里带参数）：报价与合同清单里填过的品牌型号会自动入库，
-- 下次直接下拉选，不用重复输入。
CREATE TABLE IF NOT EXISTS brands (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    memo TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_brand_name ON brands(name);

CREATE TABLE IF NOT EXISTS models (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    brand_id INTEGER REFERENCES brands(id) ON DELETE CASCADE,
    name     TEXT NOT NULL,              -- 型号
    spec     TEXT,                       -- 参数（规格描述）
    unit     TEXT,                       -- 常用计量单位
    price    REAL DEFAULT 0,             -- 参考单价
    memo     TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_model_name ON models(brand_id, name);

-- 报价单（主表）；明细在 quotation_items
CREATE TABLE IF NOT EXISTS quotations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    code          TEXT,
    title         TEXT,                  -- 报价标题
    direction     TEXT,                  -- 合同方向（转合同时沿用）
    unit_id       INTEGER REFERENCES units(id),      -- 客户 / 往来单位
    contact       TEXT,
    phone         TEXT,
    project_id    INTEGER REFERENCES projects(id),
    quote_date    TEXT,
    valid_days    INTEGER DEFAULT 30,    -- 报价有效期（天）
    tax_rate      REAL DEFAULT 0,        -- 0.13 = 13%
    amount_total  REAL DEFAULT 0,        -- 明细合计
    discount      REAL DEFAULT 0,        -- 优惠让利
    amount_final  REAL DEFAULT 0,        -- 报价总额 = 明细合计 − 优惠
    status        TEXT DEFAULT '草稿',    -- 草稿/已发出/已确认/已转合同/已作废
    handler       TEXT,                  -- 报价人（姓名）
    handler_user_id INTEGER REFERENCES users(id),   -- 经手员工账号
    contract_id   INTEGER REFERENCES contracts(id), -- 转成的合同
    converted_at  TEXT,
    memo          TEXT,
    created_at    TEXT,
    updated_at    TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_quotation_code ON quotations(code);

CREATE TABLE IF NOT EXISTS quotation_items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    quotation_id INTEGER NOT NULL REFERENCES quotations(id),
    seq          INTEGER DEFAULT 0,
    name         TEXT,                   -- 品名
    brand        TEXT,
    model        TEXT,
    spec         TEXT,                   -- 参数 / 规格
    unit         TEXT,
    qty          REAL DEFAULT 0,
    price        REAL DEFAULT 0,
    amount       REAL DEFAULT 0,
    tax_included TEXT,                   -- 含税 / 不含税（逐行标）
    tax_rate     TEXT,                   -- 税点，如 13%
    remark       TEXT,
    brand_id     INTEGER REFERENCES brands(id),
    model_id     INTEGER REFERENCES models(id)
);

-- 合同清单：由报价转来，也可直接录；付款/费用可对应到具体清单项
CREATE TABLE IF NOT EXISTS contract_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id INTEGER NOT NULL REFERENCES contracts(id),
    seq         INTEGER DEFAULT 0,
    name        TEXT,
    brand       TEXT,
    model       TEXT,
    spec        TEXT,
    unit        TEXT,
    qty         REAL DEFAULT 0,
    price       REAL DEFAULT 0,
    amount      REAL DEFAULT 0,
    tax_included TEXT,
    tax_rate    TEXT,
    remark      TEXT
);

CREATE TABLE IF NOT EXISTS payment_items (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    payment_id       INTEGER NOT NULL REFERENCES payments(id),
    contract_item_id INTEGER REFERENCES contract_items(id),
    name             TEXT,                -- 快照：清单项名称
    amount           REAL DEFAULT 0,
    memo             TEXT
);

CREATE TABLE IF NOT EXISTS cost_items (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    cost_id          INTEGER NOT NULL REFERENCES costs(id),
    contract_item_id INTEGER REFERENCES contract_items(id),
    name             TEXT,
    amount           REAL DEFAULT 0,
    memo             TEXT
);

-- 送货单
CREATE TABLE IF NOT EXISTS deliveries (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    code         TEXT,
    contract_id  INTEGER REFERENCES contracts(id),
    project_id   INTEGER REFERENCES projects(id),
    unit_id      INTEGER REFERENCES units(id),       -- 收货单位
    receiver     TEXT,                   -- 收货人
    phone        TEXT,
    address      TEXT,                   -- 收货地址
    deliver_date TEXT,
    carrier      TEXT,                   -- 运输方式 / 车牌
    status       TEXT DEFAULT '待发货',
    handler      TEXT,
    memo         TEXT,
    created_at   TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_delivery_code ON deliveries(code);

CREATE TABLE IF NOT EXISTS delivery_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id INTEGER NOT NULL REFERENCES deliveries(id),
    seq         INTEGER DEFAULT 0,
    name        TEXT,
    brand       TEXT,
    model       TEXT,
    spec        TEXT,
    unit        TEXT,
    qty         REAL DEFAULT 0,
    remark      TEXT
);

-- ============================================================ 对私计算
-- 场景：部分收入要按「合同里的每个项目」算一笔账 —— 收入扣掉税点、扣掉成本，
-- 剩下的付给居间人。税点逐行设（默认取合同清单项的），成本手工填
-- （旁边显示费用支出里已登记的金额供参考），扣完全额付。
CREATE TABLE IF NOT EXISTS private_settlements (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    code           TEXT,
    title          TEXT,
    contract_id    INTEGER REFERENCES contracts(id),
    project_id     INTEGER REFERENCES projects(id),
    unit_id        INTEGER REFERENCES units(id),
    broker         TEXT,                   -- 居间人（手填姓名）
    broker_contact TEXT,                   -- 居间人联系方式
    calc_date      TEXT,
    amount_income  REAL DEFAULT 0,         -- 收入合计（明细汇总）
    amount_tax     REAL DEFAULT 0,         -- 扣税合计
    amount_cost    REAL DEFAULT 0,         -- 成本合计
    amount_payable REAL DEFAULT 0,         -- 应付居间人合计
    status         TEXT DEFAULT '草稿',
    handler        TEXT,
    memo           TEXT,
    created_at     TEXT,
    updated_at     TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_private_code ON private_settlements(code);

CREATE TABLE IF NOT EXISTS private_settlement_items (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    settlement_id    INTEGER NOT NULL REFERENCES private_settlements(id),
    seq              INTEGER DEFAULT 0,
    contract_item_id INTEGER REFERENCES contract_items(id),
    row_contract_id  INTEGER REFERENCES contracts(id),   -- 这一行属于哪张合同（空＝共同成本）
    name             TEXT,                 -- 项目 / 品名（从清单项带出，可改）
    income           REAL DEFAULT 0,       -- 这个项目的收入
    tax_included     TEXT,                 -- 【已弃用】发票口径的含税，保留兼容
    tax_rate         TEXT,                 -- 扣税比例（我方口径），如 13%
    tax_amount       REAL DEFAULT 0,       -- 扣税 = 收入 × 税点
    cost_amount      REAL DEFAULT 0,       -- 成本（手工填）
    payable          REAL DEFAULT 0,       -- 应付居间人 = 收入 − 扣税 − 成本
    remark           TEXT
);

-- 对私计算可以同时关联多张合同（明细里每行再标明属于哪张）
CREATE TABLE IF NOT EXISTS private_settlement_contracts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    settlement_id INTEGER NOT NULL REFERENCES private_settlements(id),
    contract_id   INTEGER NOT NULL REFERENCES contracts(id),
    sort          INTEGER DEFAULT 10
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_psc
    ON private_settlement_contracts(settlement_id, contract_id);

-- ============================================================ 工程交付资料
-- 场景：一个工程做完，要交给客户一份"交付资料"——IP 地址段、各台设备
-- （录像机 / 门禁 / 交换机…）的登录用户名与口令、点位图 PDF。
-- 资料可以挂在【项目管理】里已有的项目上，也可以只写一个项目名
-- （手填的项目名不进项目管理，只在交付资料里用）。
CREATE TABLE IF NOT EXISTS handovers (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    code         TEXT,
    title        TEXT,                   -- 【已弃用，保留兼容】原来当"资料名称"用，
                                         -- 现在名称直接用「对应项目 / 手填项目名」
    project_id   INTEGER REFERENCES projects(id),  -- 对应已有项目（可空）
    project_name TEXT,                   -- 手填的项目名（不进项目管理）
    ip_range     TEXT,                   -- IP 地址段（可写多段 / 多行）
    deliver_date TEXT,                   -- 交付时间（自己选）
    handler      TEXT,                   -- 交付人
    memo         TEXT,
    created_at   TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_handover_code ON handovers(code);

-- 一台设备一行，可多行（录像机、门禁主机、交换机…）
CREATE TABLE IF NOT EXISTS handover_devices (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    handover_id INTEGER NOT NULL REFERENCES handovers(id),
    seq         INTEGER DEFAULT 0,
    name        TEXT,                    -- 设备名称
    model       TEXT,                    -- 型号
    ip          TEXT,                    -- 设备 IP（可写范围：192.168.10.11-20）
    netmask     TEXT,                    -- 子网掩码
    gateway     TEXT,                    -- 网关
    port        TEXT,                    -- 端口
    username    TEXT,                    -- 用户名
    password    TEXT,                    -- 密码 / 口令
    remark      TEXT
);

-- ============================================================ 工资表封存 / 解锁
-- 规则：*下个月*的工资表一旦生成，本月就自动封存（只读）。
-- 要改封存月份的工资，必须先解锁并**填写原因** —— 这里留审计记录，
-- 页面顶部会一直显示"谁、什么时候、因为什么"解锁的。
-- 当前是否处于"已解锁"状态 = 该月最后一条记录是不是 unlock（改完可以点【重新锁定】）。
CREATE TABLE IF NOT EXISTS payroll_lock_log (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ym     TEXT NOT NULL,              -- 2026-09
    action TEXT NOT NULL,              -- unlock / relock
    reason TEXT,                       -- 解锁原因（必填）
    actor  TEXT,                       -- 操作人
    at     TEXT
);
CREATE INDEX IF NOT EXISTS ix_paylock_ym ON payroll_lock_log(ym, id);

-- 月份级「定稿」标记：有这一行才算本月工资定稿。
-- 逐人【调整】只写 payroll 行（locked=1, manual=1），**不等于**整月定稿 ——
-- 定稿必须点【本月工资定稿】；解除定稿要输入登录密码（记 payroll_lock_log）。
CREATE TABLE IF NOT EXISTS payroll_final (
    ym           TEXT PRIMARY KEY,   -- 2026-10
    finalized_at TEXT,
    actor        TEXT,
    memo         TEXT
);

-- ============================================================ 【已弃用】工资表「本月不发」
-- 2026-10-09 上线过一版「某个月单独不发某人」，同一天用户改主意：
-- 改用花名册里的【离职日期】来管（离职当月按天折算、之后自动不出现在工资表里），
-- 这个功能整体撤掉了。**表保留不动**（老库升级不删表），代码里已不再读写。
CREATE TABLE IF NOT EXISTS payroll_exclude (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ym         TEXT NOT NULL,
    staff_id   INTEGER NOT NULL REFERENCES staff(id),
    actor      TEXT,
    at         TEXT,
    UNIQUE(ym, staff_id)
);

-- ================================================== 记工（点工 / 临时工）
-- ⚠️ 这套跟「工资管理」里的月薪员工（staff + payroll）是**两套**，互不影响：
--    staff 是正式员工（月薪 + 社保 + 个税），workers 是工地上的点工 / 临时工
--    （干一天算一天，按天或半天结，没有社保个税）。
CREATE TABLE IF NOT EXISTS workers (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    code         TEXT,
    name         TEXT NOT NULL,
    id_card      TEXT,                     -- 身份证号
    phone        TEXT,
    trade        TEXT,                     -- 工种（瓦工 / 木工 / 电工 / 小工 …）
    default_rate REAL DEFAULT 0,           -- 默认日工资，记工时自动带出来
    rate2        REAL DEFAULT 0,           -- 第二工资：另一种活 / 双工的日薪，
                                           -- 月表里点成「2」时按它算一整天
    bank_account TEXT,                     -- 银行卡号（发工资用）
    status       TEXT DEFAULT '在用',       -- 在用 / 停用
    memo         TEXT,
    sort         INTEGER DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_workers_code ON workers(code);
CREATE INDEX IF NOT EXISTS ix_workers_name ON workers(name);
CREATE INDEX IF NOT EXISTS ix_workers_status ON workers(status);

-- 一条 = 某人某天记了多少工。amount = rate × days（保存时服务端算）
-- 附件（身份证照片挂在 workers 上；现场照片挂在每条 worklog 上）走通用 attachments 表。
CREATE TABLE IF NOT EXISTS worklogs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    work_date  TEXT NOT NULL,                        -- YYYY-MM-DD
    worker_id  INTEGER NOT NULL REFERENCES workers(id),
    project_id INTEGER REFERENCES projects(id),
    rate       REAL DEFAULT 0,                       -- 当天日工资（可单独改）
    days       REAL DEFAULT 1,                       -- 1 = 整天，0.5 = 半天
    -- 这条的价是哪一种：'默认' = 默认日工资、'第二' = 第二工资（月表格子显示 2）、
    -- 空 = 手工改过的价。只用来还原月表上的显示，不参与算钱。
    rate_src   TEXT,
    amount     REAL DEFAULT 0,
    memo       TEXT,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_worklogs_date ON worklogs(work_date);
CREATE INDEX IF NOT EXISTS ix_worklogs_worker ON worklogs(worker_id);
CREATE INDEX IF NOT EXISTS ix_worklogs_project ON worklogs(project_id);
-- 月表按月查、批量记工要判断"这天记过没有"，这条复合索引最常用
CREATE INDEX IF NOT EXISTS ix_worklogs_worker_date ON worklogs(worker_id, work_date);
