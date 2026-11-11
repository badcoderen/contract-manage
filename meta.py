# -*- coding: utf-8 -*-
"""实体元数据：一份定义同时驱动列表页、表单页、统计与导出。"""

# 「无需报销」是一个**特殊值**（不只是下拉里多一项）：
# 选中它，这笔费用就不进报销审批流程 —— 审批状态直接记成「无需报销」，
# 既不进「报销审批与打款」的待办，也不算进「已批准待打款」。
# 规则由 app._apply_approval() 落实；各处的统计都按状态取值，所以自动就对了。
# 钱照常计入费用台账 / 项目成本 / 首页经营概览（钱是真花了的）。
NO_REIMBURSE = "无需报销"

# ---------------------------------------------------------------- 字典种子数据
DICT_SEED = {
    "项目状态": ["未开工", "施工中", "已竣工", "已结算", "已停工"],
    "单位类型": ["业主单位", "总包单位", "分包单位", "材料供应商", "设备租赁",
                 "设计单位", "监理单位", "咨询服务", "其他"],
    "合同方向": ["收入合同", "支出合同"],
    "合同类别": ["施工合同", "材料采购", "设备采购", "专业分包", "劳务分包",
                 "设计合同", "监理合同", "咨询服务", "租赁合同", "其他"],
    "付款方式": ["银行打款", "现金", "微信", "支付宝", "其他"],
    "履行状态": ["草拟", "已签订", "履行中", "已完成", "已终止", "已作废"],
    "款项性质": ["预付款", "进度款", "结算款", "质保金", "退款", "其他"],
    "支付状态": ["未支付", "已支付", "部分支付"],
    "发票类型": ["增值税专用发票", "增值税普通发票", "电子专用发票", "电子普通发票", "收据"],
    "发票方向": ["收到进项发票", "开具销项发票"],
    # 发票状态＝"这笔钱付没付"（2026-09-18 用户确认只要这三个）：
    #   费用未付款 ＝ 票已收到、钱还没付（进项发票据此先挂一笔费用）
    #   已收讫     ＝ 钱已经付清（进项发票会把对应费用标记成「已支付」）
    #   已作废     ＝ 无效票，不生成/不保留对应费用
    "发票状态": ["费用未付款", "已收讫", "已作废"],
    # ---- 费用报销 / 出勤 ----
    "报销方式": ["垫付报销", "预支核销", NO_REIMBURSE],
    # 工地点工 / 临时工的工种（在「记工管理 → 工人名单」里选）
    "工种": ["瓦工", "木工", "钢筋工", "架子工", "电工", "水暖工", "焊工",
             "油漆工", "小工", "杂工", "司机", "其他"],
    "审批状态": ["待审批", "已批准", "已驳回", "已打款"],
    "请假类型": ["事假", "病假", "年假", "调休", "婚假", "产假", "其他"],
    "预支状态": ["待打款", "已打款", "部分核销", "已核销完"],
    "出差状态": ["待审批", "已批准", "已驳回", "已完成"],
    # ---- 报价 / 送货 ----
    "报价状态": ["草稿", "已发出", "已确认", "已转合同", "已作废"],
    "送货状态": ["待发货", "已发货", "已签收"],
    "计量单位": ["台", "套", "个", "件", "只", "根", "张", "块", "组", "批", "项",
                 "米", "平方米", "立方米", "公斤", "吨", "桶", "卷", "盘"],
    "运输方式": ["公司送货", "快递", "物流", "客户自提", "专车"],
    # 明细行上的含税口径（取代原来放在表头的统一税率）
    "含税方式": ["含税", "不含税"],
    "税点": ["0%", "1%", "3%", "5%", "6%", "9%", "13%"],
    # ---- 对私计算 ----
    "对私状态": ["草稿", "已确认", "已支付", "已作废"],
}

# 工资与补贴参数默认值：(键, 默认值, 说明)
SETTINGS_SEED = [
    # ---- 计薪口径：按人社部《关于职工全年月平均工作时间和工资折算问题的通知》
    ("monthly_days", "21.75",
     "月计薪天数（法定 21.75 = (365−104)÷12）；日工资 = 月工资 ÷ 该天数"),
    ("hours_per_day", "8", "每日标准工时（小时）；小时工资 = 日工资 ÷ 该工时"),
    ("ot_rate_weekday", "1.5", "工作日延长工时加班倍数（法定不低于 150%）"),
    ("ot_rate_weekend", "2.0", "休息日加班倍数（法定不低于 200%）"),
    ("ot_rate_holiday", "3.0", "法定节假日加班倍数（法定不低于 300%）"),
    ("leave_deduct_rate", "1.0", "事假扣款倍数：扣款 = 请假天数 × 日工资 × 该倍数"),
    ("trip_allowance_per_day", "40", "出差补贴标准（元/天）"),
    ("meal_office_per_day", "15", "不出差工作日的餐费补贴（元/天）"),
    ("meal_trip_per_day", "30", "出差期间的餐费补贴（元/天）"),
    # ---- 社保个人缴费部分（政策调整时改这里即可，全员统一）
    ("pension_rate", "0.08", "养老保险个人比例（8%）"),
    ("medical_rate", "0.02", "医疗保险个人比例（2%）"),
    ("unemploy_rate", "0.005", "失业保险个人比例（0.5%）"),
    ("serious_ill_fixed", "10", "大病补充保险个人月缴定额（元）"),
    # ---- 个税
    ("tax_threshold", "5000", "个税基本减除费用（起征点，元/月）"),
    # ---- 其它
    ("salary_default", "4500", "新增员工时的默认基础工资（元）"),
    ("work_days_per_week", "6", "每周工作天数（仅作排班参考，不参与工资折算）"),
    # ---- 对私计算（给居间人算钱）
    ("private_tax_rate", "13",
     "对私计算里的默认【扣税比例】（按百分数填，13 表示 13%）。"
     "名义上是扣税，实际是我方的收入；与发票税点无关，每张单可以逐行改。"),
    # ---- 发票 PDF 自动导入
    ("company_tax_no", "",
     "本公司的纳税人识别号（18 位）。拖入发票 PDF 时用它判断哪一方是我们，"
     "从而自动定进项/销项；留空则只按公司名称判断。"),
]

# 费用类别种子数据：名称, 成本属性, 排序, 说明
COST_CAT_SEED = [
    ("人工费", "直接成本", 10, "施工人员工资、劳务费"),
    ("材料费", "直接成本", 20, "钢材、水泥、砂石等主材及周转材料"),
    ("机械费", "直接成本", 30, "自有机械折旧、外租设备台班费"),
    ("措施费", "直接成本", 40, "安全文明施工、临时设施等"),
    ("分包费", "直接成本", 50, "专业分包、劳务分包结算"),
    ("管理费", "间接成本", 60, "项目部管理人员薪酬及办公支出"),
    ("规费", "间接成本", 70, "社保、公积金等规费"),
    ("税金", "间接成本", 80, "附加税及其他税费"),
    ("其他", "期间费用", 90, "无法归入以上类别的支出"),
]

COST_ATTRS = ["直接成本", "间接成本", "期间费用"]

# ---------------------------------------------------------------- 下拉数据源
SQL_PROJECT = "SELECT id, code || ' ' || name FROM projects ORDER BY code"
SQL_UNIT = "SELECT id, name FROM units ORDER BY name"
# 记工：只列「在用」的工人，名字后面带上工种，选的时候不容易认错
SQL_WORKER = ("SELECT id, name || IFNULL('（' || trade || '）', '') "
              "FROM workers WHERE IFNULL(status, '在用') <> '停用' ORDER BY sort, name")
SQL_CONTRACT = "SELECT id, code || ' ' || name FROM contracts ORDER BY code"
# 发票的「关联合同」按发票方向收窄：进项（我们买进来）只能挂支出合同，
# 销项（开给客户）只能挂收入合同 —— 否则会出现"进项发票挂在收入合同下"。
SQL_CONTRACT_INCOME = ("SELECT id, code || ' ' || name FROM contracts "
                       "WHERE direction = '收入合同' ORDER BY code")
SQL_CONTRACT_EXPENSE = ("SELECT id, code || ' ' || name FROM contracts "
                        "WHERE direction = '支出合同' ORDER BY code")
SQL_COSTCAT = ("SELECT id, name FROM cost_categories "
               "WHERE enabled = 1 ORDER BY sort, name")
# 员工下拉（含财务与管理员，报销/出勤的执行人都从这里选）
SQL_USER = ("SELECT id, IFNULL(display_name, username) FROM users "
            "WHERE enabled = 1 ORDER BY role, username")

SQL_BRAND = "SELECT id, name FROM brands ORDER BY name"
SQL_MODEL = ("SELECT m.id, IFNULL(b.name,'') || ' ' || m.name "
             "FROM models m LEFT JOIN brands b ON m.brand_id = b.id "
             "ORDER BY b.name, m.name")
# 合同清单项（供付款/费用「对应合同清单项」下拉用，带合同 id 由前端按合同过滤）
SQL_CONTRACT_ITEM = ("SELECT id, contract_id, seq, name, brand, model, spec, "
                     "unit, qty, amount FROM contract_items "
                     "ORDER BY contract_id, seq, id")

MONEY_STEP = "0.01"

# ------------------------------------------------- 列表表头排序用的 ORDER BY 片段
# 列表列如果**不是表里的真实列**（例如「已核销」是查 settlements 现算的、
# 「原件」是数附件），想在表头上点着排序，就在这里给它一份 SQL 表达式。
# services.sort_columns() 只认这份表 + 真实列 + 联表别名，其余列的表头不可点。
_INV_SETTLED = ("(SELECT IFNULL(SUM(s.amount),0) FROM settlements s "
                "WHERE s.invoice_id = t.id)")
_INV_PROGRESS = ("(CASE WHEN IFNULL(t.amount_gross,0) > 0 THEN " + _INV_SETTLED +
                 " / t.amount_gross ELSE 0 END)")
_ADV_USED = ("(SELECT IFNULL(SUM(c.amount),0) FROM costs c "
             "WHERE c.advance_id = t.id "
             "AND IFNULL(c.approval_status,'') IN ('已批准','已打款'))")
SORT_SQL = {
    "invoices": {"settled": _INV_SETTLED, "progress": _INV_PROGRESS,
                 "pay_state": _INV_PROGRESS},
    "payments": {"settle_count": ("(SELECT COUNT(*) FROM settlements s "
                                  "WHERE s.payment_id = t.id)")},
    "contracts": {"files": ("(SELECT COUNT(*) FROM attachments a "
                            "WHERE a.ref_type = 'contracts' AND a.ref_id = t.id)")},
    "advances": {"used": _ADV_USED,
                 "balance": "IFNULL(t.amount,0) - " + _ADV_USED},
    # 工程交付资料的「对应项目」：优先取项目管理里的项目名，没有就用手填的
    "handovers": {"proj_label": "IFNULL(p.name, t.project_name)"},
}


def f(name, label, **kw):
    d = {"name": name, "label": label, "type": "text", "hint": ""}
    d.update(kw)
    return d


def dc(name, label, type="text", w="", **kw):
    """明细表的一列。type 决定前端渲染控件：
    text/money/number/textarea/unit(带计量单位下拉)/
    brand(品牌下拉)/model(型号下拉)/contractitem(合同清单项下拉)/
    select(固定或字典下拉，配 options 或 dict_cat)/
    pct(百分数，手填，入库时规范成 "13%"；配 default_setting 可从参数设置取默认值)。
    """
    d = {"name": name, "label": label, "type": type, "w": w}
    d.update(kw)
    return d


def goods_cols():
    """清单类明细的通用列（报价明细 / 合同清单）：品名 品牌 型号 参数 单位 数量 单价 金额 备注。

    「数量 × 单价 = 金额」在前端即时计算，服务端保存时再算一遍（兜底）。
    """
    return [dc("name", "品名 / 项目", "text", "15%", ph="如 桥架、配电箱"),
            dc("brand", "品牌", "brand", "10%"),
            dc("model", "型号", "model", "10%"),
            dc("spec", "参数 / 规格", "text", "15%"),
            dc("unit", "单位", "unit", "5%"),
            dc("qty", "数量", "number", "6%"),
            dc("price", "单价", "money", "9%"),
            dc("amount", "金额", "money", "10%", calc="qty*price", ro=True),
            # 每一行各自标含税口径与税点（原来统一放在表头的【税率】已去掉）
            # sets：选了「不含税」就把税点钉成 0%（不含税即不开票，税点没意义）；
            # 反向不自动（改回含税时保留当前税点，要 13% 还是 9% 自己选）。
            dc("tax_included", "含税", "select", "6%", dict_cat="含税方式",
               default="含税", sets={"不含税": {"tax_rate": "0%"}}),
            dc("tax_rate", "税点", "select", "6%", dict_cat="税点", default="13%"),
            dc("remark", "备注", "text", "8%")]


def private_cols():
    """对私计算的明细列：一行 = 合同里的一个清单项，或者一行「纯成本」。

    ⚠️ 这里的【扣税比例】不是发票的税点。它由我们自己设定 —— 名义上是「扣税」，
    实际是我方的收入（留成）。因此：
      · 逐行手工填，随时可改；新增行的默认值取「参数设置 → private_tax_rate」；
      · 与合同清单项的含税口径 / 发票税点**没有任何联动**，那是两回事；
      · 留空按 0 算（也可以填 12.5 这种小数，列类型 pct 按百分点存成 "12.5%"）。

    两种行（成本都在【成本】里手填）：
      · **收入项**：先选【所属合同】→ 再选【对应合同清单项】→ 带出名称与收入；
        所属合同会跟着清单项自动钉上，多张合同时不容易选错行。
      · **纯成本项**：所属合同留空（＝共同成本）或选一张，**名称手填、收入留 0、
        只填【成本】**；这时【应付居间人】是负的 —— 单头
        「收入 − 扣税 − 成本 = 应付」的合计照样对得上。

    派生列用 `expr` 声明公式（前后端各按同一份声明算一遍）：
      · 扣税金额   = 收入 × 扣税比例        ——按点数直接扣
      · 应付居间人 = 收入 − 扣税金额 − 成本   扣完剩下的全给居间人
    公式里的 pct(列名) 把 "13%" 这样的文本换成 0.13。
    口径要改只动这一行，别去动 services / app.js 里的计算代码。
    """
    return [dc("row_contract_id", "所属合同", "contract", "10%",
               hint="可留空＝共同成本；选了就按这张合同筛清单项"),
            dc("contract_item_id", "对应合同清单项", "contractitem", "18%",
               # 选中清单项后，把它的字段带到本行的这些列（可再手工改）。
               # 只带名称/金额/合同归属：扣税比例是我方口径，不从清单项带。
               fills={"name": "name", "income": "amount",
                      "row_contract_id": "contract_id"}),
            dc("name", "项目 / 品名", "text", "15%",
               ph="选清单项自动带出；成本项直接手填"),
            dc("income", "收入金额", "money", "10%",
               hint="默认取合同清单项的金额；纯成本项留 0"),
            dc("tax_rate", "扣税比例", "pct", "8%",
               hint="我们自己设定的比例（按百分数填，如 13），不是发票税点；"
                    "名义扣税，实为我方收入",
               default_setting="private_tax_rate", default="13%"),
            dc("tax_amount", "我方利润", "money", "9%", ro=True,
               expr="income * pct(tax_rate)",
               hint="扣税比例 × 收入金额；「扣税」是名义叫法，实际就是我方利润"),
            dc("cost_amount", "成本", "money", "9%",
               hint="手工填；上面的参考面板列出费用支出里已登记的金额"),
            dc("payable", "应付居间人", "money", "10%", ro=True,
               expr="income - tax_amount - cost_amount"),
            dc("remark", "备注", "text", "8%")]


def ref_cols(fill_amount=False):
    """付款 / 费用「对应合同清单项」的列。

    fill_amount=True 时（费用支出用）：选中清单项就把它的金额带进「本次金额」；
    留空保存也会在服务端兜底取清单项金额（明细声明 amount_from_item），
    免得存成 0 —— 付款那边**不带**，因为一笔付款往往只核销清单项的一部分。
    """
    item_col = dc("contract_item_id", "对应合同清单项", "contractitem", "50%")
    if fill_amount:
        item_col["fills"] = {"amount": "amount"}
    return [item_col,
            dc("amount", "本次金额", "money", "16%",
               hint="选清单项会自动带出它的金额，可改成实际金额"),
            dc("memo", "备注", "text", "34%")]


def delivery_cols():
    """送货明细列（只关心数量，不带价格）。

    第一列是隐藏的 contract_item_id —— 记住这行对应合同清单里的哪一项，
    下次从合同生成送货单时才能算出"还没送的数量"。
    """
    return [dc("contract_item_id", "清单项", "hidden", ""),
            dc("name", "品名 / 项目", "text", "20%"),
            dc("brand", "品牌", "brand", "13%"),
            dc("model", "型号", "model", "13%"),
            dc("spec", "参数 / 规格", "text", "20%"),
            dc("unit", "单位", "unit", "7%"),
            dc("qty", "送货数量", "number", "10%"),
            dc("remark", "备注", "text", "17%")]


GOODS_DETAIL = {
    "key": "items",
    "title": "清单明细",
    "table": "contract_items",        # 占位，各实体覆盖
    "fk": "contract_id",
    "cols": goods_cols(),
    "sum": ["amount"],
    "library": True,                  # 品牌/型号自动入库
    "import": True,                   # 支持 Excel 导入
}


# ---------------------------------------------------------------- 实体定义
ENTITIES = {
    # ============================================================ 基础资料
    "projects": {
        "title": "项目管理",
        "table": "projects",
        "group": "基础资料",
        "code": ("code", "XM-"),
        "order": "code",
        "search": ["code", "name", "owner_name", "manager", "location", "status"],
        "fields": [
            f("code", "项目编号", hint="留空自动生成"),
            f("name", "项目名称", required=True),
            f("owner_name", "业主单位"),
            f("location", "项目地点"),
            f("manager", "项目经理"),
            f("manager_user_id", "负责员工", type="lookup", options_sql=SQL_USER,
              hint="该员工可查看与录入此项目的费用"),
            f("start_date", "开工日期", type="date"),
            f("end_date", "竣工日期", type="date"),
            f("status", "项目状态", type="select_dict", dict_cat="项目状态"),
            f("budget", "项目预算", type="money", step=MONEY_STEP),
            f("memo", "备注", type="textarea"),
        ],
        "list": [
            {"name": "code", "label": "项目编号", "w": 11},
            {"name": "name", "label": "项目名称", "w": 26},
            {"name": "owner_name", "label": "业主单位", "w": 18},
            {"name": "manager", "label": "项目经理", "w": 10},
            {"name": "status", "label": "状态", "w": 10},
            {"name": "budget", "label": "项目预算", "w": 14, "type": "money"},
        ],
        # 列表每行多一个按钮：进「项目全景」（合同 / 发票 / 费用类别 / 利润一次看完）
        "row_links": [{"label": "项目全景", "endpoint": "project_profile"}],
    },
    "units": {
        "title": "往来单位管理",
        "table": "units",
        "group": "基础资料",
        "code": None,
        "order": "name",
        "search": ["name", "type", "contact", "phone", "tax_no"],
        "fields": [
            f("name", "单位名称", required=True),
            f("type", "单位类型", type="select_dict", dict_cat="单位类型"),
            f("tax_no", "统一社会信用代码"),
            f("contact", "联系人"),
            f("phone", "联系电话"),
            f("address", "地址"),
            f("memo", "备注", type="textarea"),
        ],
        "list": [
            {"name": "name", "label": "单位名称", "w": 28},
            {"name": "type", "label": "单位类型", "w": 14},
            {"name": "contact", "label": "联系人", "w": 12},
            {"name": "phone", "label": "联系电话", "w": 16},
            {"name": "address", "label": "地址", "w": 22},
        ],
    },
    "cost_categories": {
        "title": "费用类别管理",
        "table": "cost_categories",
        "group": "基础资料",
        "code": None,
        "order": "sort, name",
        "search": ["name", "cost_attr", "memo"],
        "fields": [
            f("name", "类别名称", required=True),
            f("cost_attr", "成本属性", type="select_fixed", options=COST_ATTRS),
            f("sort", "排序", type="number", step="1"),
            f("enabled", "是否启用", type="checkbox", default=1,
              hint="取消勾选后不再出现在费用录入的下拉中"),
            f("memo", "说明", type="textarea"),
        ],
        "list": [
            {"name": "sort", "label": "排序", "w": 8},
            {"name": "name", "label": "类别名称", "w": 20},
            {"name": "cost_attr", "label": "成本属性", "w": 14},
            {"name": "enabled", "label": "启用", "w": 8, "type": "check"},
            {"name": "memo", "label": "说明", "w": 38},
        ],
    },
    "dicts": {
        "title": "下拉选项维护",
        "table": "dicts",
        "group": "基础资料",
        "code": None,
        "order": "category, sort, value",
        "search": ["category", "value"],
        "fields": [
            f("category", "字典类别", required=True),
            f("value", "选项值", required=True),
            f("sort", "排序", type="number", step="1"),
        ],
        "list": [
            {"name": "category", "label": "字典类别", "w": 20},
            {"name": "value", "label": "选项值", "w": 30},
            {"name": "sort", "label": "排序", "w": 10},
        ],
    },
    # ============================================================ 业务单据
    "contracts": {
        "title": "合同管理",
        "table": "contracts",
        "group": "业务单据",
        "code": ("code", "HT-"),
        "order": "code",
        "search": ["code", "name", "direction", "category", "status", "handler",
                   "project_name"],
        "calc": {"gross": "amount_gross", "rate": "tax_rate",
                 "net": "amount_net", "tax": "tax_amount"},
        "attachments": True,
        "note": "填写【合同金额】和【税率】，不含税金额与税额自动计算；合同原件在合同编号保存后可上传（PDF / Word / 图片）。",
        "details": [{
            "key": "items",
            "title": "合同清单",
            "table": "contract_items",
            "fk": "contract_id",
            "cols": goods_cols(),
            "sum": ["amount"],
            "library": True,
            "import": True,
            "totals": False,          # 合同金额自己填，不被清单覆盖
        }],
        "fields": [
            f("code", "合同编号", hint="留空自动生成"),
            f("name", "合同名称", required=True),
            f("project_id", "所属项目", type="lookup", options_sql=SQL_PROJECT),
            f("unit_id", "对方单位", type="lookup", options_sql=SQL_UNIT),
            f("direction", "合同方向", type="select_dict", dict_cat="合同方向"),
            f("category", "合同类别", type="select_dict", dict_cat="合同类别"),
            f("sign_date", "签订日期", type="date"),
            f("amount_gross", "合同金额(含税)", type="money", step=MONEY_STEP,
              hint="含税总价，例如 1130000"),
            f("tax_rate", "税率", type="rate", step="0.0001", hint="0.09 表示 9%"),
            f("amount_net", "不含税金额", type="money", readonly=True, calc="net",
              hint="由 合同金额 ÷ (1+税率) 自动计算"),
            f("tax_amount", "税额", type="money", readonly=True, calc="tax",
              hint="由 合同金额 − 不含税金额 自动计算"),
            f("prepay_ratio", "预付款比例(%)", type="number", step="0.01",
              hint="20 表示 20%"),
            f("retention_ratio", "质保金比例(%)", type="number", step="0.01",
              hint="5 表示 5%"),
            f("pay_method", "付款方式", type="select_dict", dict_cat="付款方式"),
            f("status", "履行状态", type="select_dict", dict_cat="履行状态"),
            # 质保：开始时间 + 期限（月）→ 到期日自动算；个别项目另有约定写备注
            f("warranty_start", "质保开始", type="date",
              hint="质保从哪天起算，一般等于验收 / 竣工日期；留空表示还没起算"),
            f("warranty_months", "保修期限(月)", type="number", step="1",
              hint="按整月填：12 = 1 年、24 = 2 年；「质保到期」自动算出来"),
            f("warranty_memo", "保修备注",
              hint="个别设备 / 项目另有保修约定的写这里，例如「主机 3 年、辅材 1 年」"),
            f("handler", "经办人"),
            f("memo", "备注", type="textarea"),
        ],
        "list": [
            {"name": "code", "label": "合同编号", "w": 11},
            {"name": "name", "label": "合同名称", "w": 26},
            {"name": "project_name", "label": "所属项目", "w": 16},
            {"name": "unit_name", "label": "对方单位", "w": 16},
            {"name": "amount_gross", "label": "合同金额", "w": 13, "type": "money"},
            {"name": "status", "label": "履行状态", "w": 10},
            {"name": "files", "label": "原件", "w": 8, "type": "files"},
        ],
        # 列表页每行多出来的按钮（送货单不再单列菜单，从合同里生成）
        "row_links": [{"label": "生成送货单", "endpoint": "contract_new_delivery"},
                      {"label": "对私计算", "endpoint": "contract_new_private"}],
    },
    "payments": {
        "title": "付款记录管理",
        "table": "payments",
        "group": "业务单据",
        "code": ("code", "FK-"),
        "order": "code",
        "search": ["code", "direction", "nature", "handler", "pay_date"],
        "settlements": True,
        "note": "一笔付款可对应一张或多张发票；填【核销金额】自动算比例，填【核销比例】自动算金额。",
        "details": [{
            "key": "items",
            "title": "对应合同清单项",
            "table": "payment_items",
            "fk": "payment_id",
            "cols": ref_cols(),
            "sum": ["amount"],
            "source_field": "contract_id",   # 按主表合同过滤清单项
        }],
        "fields": [
            f("code", "付款单号", hint="留空自动生成"),
            f("contract_id", "关联合同", type="lookup", options_sql=SQL_CONTRACT),
            f("project_id", "关联项目", type="lookup", options_sql=SQL_PROJECT),
            f("direction", "付款方向", type="select_dict", dict_cat="合同方向"),
            f("nature", "款项性质", type="select_dict", dict_cat="款项性质"),
            f("pay_date", "付款日期", type="date"),
            f("amount", "付款金额", type="money", step=MONEY_STEP),
            f("pay_method", "付款方式", type="select_dict", dict_cat="付款方式"),
            f("handler", "经办人"),
            f("memo", "备注", type="textarea"),
        ],
        "list": [
            {"name": "code", "label": "付款单号", "w": 12},
            {"name": "pay_date", "label": "付款日期", "w": 11},
            {"name": "contract_code", "label": "关联合同", "w": 13},
            {"name": "project_name", "label": "关联项目", "w": 16},
            {"name": "direction", "label": "方向", "w": 9},
            {"name": "nature", "label": "款项性质", "w": 10},
            {"name": "amount", "label": "付款金额", "w": 13, "type": "money"},
            {"name": "settle_count", "label": "核销发票数", "w": 10, "type": "int"},
        ],
    },
    "costs": {
        "title": "费用支出管理",
        "table": "costs",
        "group": "业务单据",
        "code": ("code", "FY-"),
        "order": "code",
        # 「支付状态 + 已付金额」是一对：保存时由 services.cost_normalize_paid() 对齐
        "paid_pair": {"status": "pay_status", "paid": "paid_amount", "amount": "amount"},
        # category_name 是联表列（cost_categories.name）—— 存的是类别 id，
        # 列表与搜索都要按名称走
        "search": ["code", "category_name", "summary", "handler", "pay_status",
                   "settle_type", "approval_status"],
        "note": "费用类别在 基础资料 → 费用类别管理 中维护。报销人、报销方式与审批状态见「费用报销」栏；员工提交后由财务审批打款。",
        "details": [{
            "key": "items",
            "title": "对应合同清单项",
            "table": "cost_items",
            "fk": "cost_id",
            "cols": ref_cols(fill_amount=True),
            "sum": ["amount"],
            "source_field": "contract_id",
            # 本次金额留空 → 服务端取合同清单项的金额（不存 0）
            "amount_from_item": True,
        }],
        "fields": [
            f("code", "费用单号", hint="留空自动生成"),
            f("employee_id", "报销人", type="lookup", options_sql=SQL_USER,
              hint="员工提交报销时选自己"),
            f("settle_type", "报销方式", type="select_dict", dict_cat="报销方式",
              hint="垫付报销＝先自己出钱，填单后财务审批打款；"
                   "预支核销＝从已预支的备用金里抵扣；"
                   "无需报销＝这笔不用报销（公司直接付了 / 不涉及给谁打款），"
                   "审批状态直接记「无需报销」，不进报销审批待办"),
            f("advance_id", "关联预支单", type="lookup",
              options_sql="SELECT id, code || '  余额 ' || "
                          "(ROUND(amount - IFNULL((SELECT SUM(c.amount) FROM costs c "
                          "WHERE c.advance_id = advances.id AND IFNULL(c.approval_status,'') "
                          "IN ('已批准','已打款')),0),2)) FROM advances "
                          "ORDER BY pay_date DESC, id DESC",
              hint="仅「预支核销」时填，用于从该笔预支款里核销"),
            f("project_id", "所属项目", type="lookup", options_sql=SQL_PROJECT),
            f("contract_id", "关联合同", type="lookup", options_sql=SQL_CONTRACT),
            f("unit_id", "往来单位", type="lookup", options_sql=SQL_UNIT),
            f("category", "费用类别", type="lookup", options_sql=SQL_COSTCAT),
            f("summary", "费用摘要"),
            f("cost_date", "发生日期", type="date"),
            f("amount", "金额", type="money", step=MONEY_STEP),
            f("approval_status", "审批状态", type="select_fixed",
              options=["待审批", "已批准", "已驳回", "已打款", NO_REIMBURSE],
              approval=True,
              hint="员工提交后自动为「待审批」，由财务/管理员在「报销审批与打款」处理；"
                   "报销方式选「无需报销」的单子会直接记成「无需报销」，不进待办"),
            f("approve_memo", "审批意见", approval=True),
            # sets 声明在**触发者**（这个下拉）上：选了哪个值就把目标字段设成什么。
            # "@amount" = 取同表单「金额」字段当前的值（见 static/app.js 的字段联动）。
            # ⚠️ 别声明到目标字段（paid_amount）上 —— JS 只读下拉的 data-sets，
            #    而且 money 输入框根本没有 data-sets 的渲染分支。
            f("pay_status", "支付状态", type="select_fixed",
              options=["未支付", "已支付", "部分支付"],
              sets={"未支付": {"paid_amount": "0"},
                    "已支付": {"paid_amount": "@amount"}},
              hint="这笔钱付了没有。选好它，「已付金额」会自动跟着变"),
            # 已付金额：首页「累计付款」就是按它统计的（保存时服务端再对齐一次）
            f("paid_amount", "已付金额", type="money", step=MONEY_STEP,
              hint="实际已经付出去的钱。选「已支付」自动等于金额、选「未支付」自动归 0；"
                   "选「部分支付」自己填已付了多少"),
            f("handler", "经办人"),
            f("memo", "备注", type="textarea"),
        ],
        "list": [
            {"name": "code", "label": "费用单号", "w": 10},
            {"name": "cost_date", "label": "发生日期", "w": 9},
            {"name": "employee_name", "label": "报销人", "w": 8},
            {"name": "project_name", "label": "所属项目", "w": 13},
            {"name": "category_name", "label": "费用类别", "w": 8},
            {"name": "summary", "label": "费用摘要", "w": 14},
            {"name": "amount", "label": "金额", "w": 10, "type": "money"},
            # 实际付出去多少（首页「累计付款」按它统计）
            {"name": "paid_amount", "label": "已付金额", "w": 10, "type": "money"},
            {"name": "settle_type", "label": "报销方式", "w": 10},
            {"name": "approval_status", "label": "审批", "w": 9, "type": "state"},
        ],
    },

    # ============================================================ 费用报销
    "advances": {
        "title": "预支打款",
        "table": "advances",
        "group": "费用报销",
        "code": ("code", "YZ-"),
        "order": "pay_date DESC, id DESC",
        "search": ["code", "handler", "status", "memo"],
        "note": "财务先打款给员工（备用金/预支），员工之后在「费用支出管理」里按「预支核销」抵扣。余额＝预支金额 − 已批准的核销金额。",
        "fields": [
            f("code", "预支单号", hint="留空自动生成"),
            f("employee_id", "收款员工", type="lookup", options_sql=SQL_USER,
              required=True),
            f("amount", "预支金额", type="money", step=MONEY_STEP, required=True),
            f("pay_date", "打款日期", type="date", required=True),
            f("pay_method", "打款方式", type="select_dict", dict_cat="付款方式"),
            f("status", "预支状态", type="select_fixed", finance_edit=True,
              options=["待打款", "已打款", "部分核销", "已核销完"],
              hint="钱还没打时是「待打款」；财务实际打款后请改成「已打款」。"
                   "「部分核销 / 已核销完」由核销情况自动判断"),
            f("handler", "打款经办"),
            f("memo", "备注", type="textarea"),
        ],
        "list": [
            {"name": "code", "label": "预支单号", "w": 12},
            {"name": "employee_name", "label": "收款员工", "w": 11},
            {"name": "pay_date", "label": "打款日期", "w": 11},
            {"name": "amount", "label": "预支金额", "w": 12, "type": "money"},
            {"name": "used", "label": "已核销", "w": 12, "type": "money"},
            {"name": "balance", "label": "未核销余额", "w": 12, "type": "money"},
            {"name": "status", "label": "状态", "w": 11, "type": "state"},
            {"name": "handler", "label": "经办", "w": 10},
        ],
    },

    # ============================================================ 工资管理
    "staff": {
        "title": "员工花名册",
        "table": "staff",
        "group": "工资管理",
        "code": None,
        "order": "sort, emp_no",
        "search": ["emp_no", "name", "dept", "id_card"],
        # note / hint 都是原样输出、不吃 Markdown，一律写纯文本
        "note": "这份花名册就是工资表的「发薪名单 + 工资标准」来源："
                "工资表里的基础工资、岗位津贴、社保基数、公积金、专项附加扣除都取自这里，"
                "关联了登录账号的员工，其已批准的加班 / 请假 / 出差还会自动汇总进当月工资。"
                "不登录系统的员工也能录进来，同样参与测算与工资条导出。"
                "新入职：在这里加一行，下个月生成工资表时自动就有他。"
                "离职：把【状态】改成「停用」，再填上【离职日期】—— "
                "离职当月工资按在职天数自动折算，之后的月份自动不再出现他。"
                "做过工资表的员工系统不让删（一删历史月份就缺人），离职一律用停用。",
        "fields": [
            f("emp_no", "工号", hint="可留空"),
            f("name", "姓名", required=True),
            f("id_card", "身份证号", hint="工资表与个税会用到"),
            f("bank_account", "银行帐号"),
            f("dept", "部门"),
            f("user_id", "关联登录账号", type="lookup", options_sql=SQL_USER,
              hint="不用系统就留空"),
            f("base_salary", "基础工资(月)", type="money", step=MONEY_STEP),
            f("post_salary", "岗位津贴", type="money", step=MONEY_STEP),
            f("social_base", "社保缴费基数", type="money", step=MONEY_STEP,
              hint="填 0 或留空表示按基础工资计"),
            f("social_off", "不缴社保", type="checkbox", check_label="不缴社保（个人部分记 0）",
              hint="勾选后养老 / 医疗 / 失业 / 大病补充均记 0"),
            f("fund_fixed", "公积金月缴额", type="money", step=MONEY_STEP,
              hint="个人承担部分，按月定额"),
            f("tax_deduct", "专项附加扣除(月)", type="money", step=MONEY_STEP,
              hint="子女教育 / 房贷 / 赡养老人等合计，影响个税"),
            f("join_date", "入职日期", type="date"),
            f("status", "状态", type="select_fixed", options=["在职", "停用"],
              hint="离职就改成「停用」，并在下面填上【离职日期】；不要删这一行"),
            f("leave_date", "离职日期", type="date",
              hint="填了它，工资表会自动按这个日期算：离职当月按在职天数折算"
                   "（基础工资与岗位津贴按天算，出勤天数与餐补也只算到这一天）；"
                   "之后的月份自动不再出现他，不用再手工去工资表里删人"),
            f("sort", "排序", type="number", hint="数字小的排在前面"),
            f("memo", "备注", type="textarea"),
        ],
        "list": [
            {"name": "emp_no", "label": "工号", "w": 8},
            {"name": "name", "label": "姓名", "w": 9},
            {"name": "dept", "label": "部门", "w": 9},
            {"name": "user_name", "label": "登录账号", "w": 11},
            {"name": "base_salary", "label": "基础工资", "w": 11, "type": "money"},
            {"name": "post_salary", "label": "岗位津贴", "w": 11, "type": "money"},
            {"name": "social_base", "label": "社保基数", "w": 11, "type": "money"},
            {"name": "tax_deduct", "label": "专项扣除", "w": 10, "type": "money"},
            {"name": "status", "label": "状态", "w": 8, "type": "state"},
            {"name": "leave_date", "label": "离职日期", "w": 10},
        ],
    },

    # ============================================================ 出勤管理
    "overtime": {
        "title": "加班申请",
        "table": "overtime",
        "group": "出勤管理",
        "code": ("code", "JB-"),
        "order": "ot_date DESC, id DESC",
        "search": ["code", "reason", "status"],
        "note": "只填日期和起止时间，加班时长自动算出（支持跨夜）；提交后进入「出勤审批」由财务/管理员审批，审批状态与意见由审批环节产生。",
        "auto_calc": {"kind": "time", "start": "start_time", "end": "end_time",
                      "targets": [{"field": "hours"}]},
        "fields": [
            f("code", "加班单号"),
            f("employee_id", "加班人", type="lookup", options_sql=SQL_USER,
              required=True),
            f("project_id", "所属项目", type="lookup", options_sql=SQL_PROJECT),
            f("ot_date", "加班日期", type="date", required=True),
            f("start_time", "开始时间", type="time", hint="点右侧时钟图标选时间"),
            f("end_time", "结束时间", type="time", hint="跨夜也支持，例如 22:00 → 02:00"),
            f("hours", "加班时长(小时)", type="number", step="0.5",
              hint="按起止时间自动计算；需要时可手工改"),
            f("reason", "加班事由"),
            f("status", "审批状态", type="select_fixed", approval=True,
              options=["待审批", "已批准", "已驳回"]),
            f("approve_memo", "审批意见", approval=True),
        ],
        "list": [
            {"name": "code", "label": "加班单号", "w": 12},
            {"name": "ot_date", "label": "加班日期", "w": 11},
            {"name": "employee_name", "label": "加班人", "w": 10},
            {"name": "project_name", "label": "所属项目", "w": 16},
            {"name": "start_time", "label": "开始", "w": 8},
            {"name": "end_time", "label": "结束", "w": 8},
            {"name": "hours", "label": "时长(时)", "w": 9, "type": "number"},
            {"name": "status", "label": "审批", "w": 9, "type": "state"},
        ],
    },
    "leaves": {
        "title": "请假申请",
        "table": "leaves",
        "group": "出勤管理",
        "code": ("code", "QJ-"),
        "order": "start_date DESC, id DESC",
        "search": ["code", "leave_type", "reason", "status"],
        "note": "选好起止日期后请假天数自动算出（含首尾）；只请半天时把天数手工改成 0.5。提交后进入「出勤审批」，审批状态与意见由审批环节产生。",
        "auto_calc": {"kind": "date", "start": "start_date", "end": "end_date",
                      "targets": [{"field": "days"},
                                  {"field": "hours", "mul_param": "hours_per_day"}]},
        "fields": [
            f("code", "请假单号"),
            f("employee_id", "请假人", type="lookup", options_sql=SQL_USER,
              required=True),
            f("leave_type", "请假类型", type="select_dict", dict_cat="请假类型"),
            f("start_date", "开始日期", type="date", required=True),
            f("end_date", "结束日期", type="date"),
            f("days", "请假天数", type="number", step="0.5",
              hint="按起止日期自动算（含首尾）；半天请改为 0.5"),
            f("hours", "折合小时", type="number", step="0.5",
              hint="按 天数 × 每日工时 自动算"),
            f("reason", "请假事由"),
            f("status", "审批状态", type="select_fixed", approval=True,
              options=["待审批", "已批准", "已驳回"]),
            f("approve_memo", "审批意见", approval=True),
        ],
        "list": [
            {"name": "code", "label": "请假单号", "w": 12},
            {"name": "employee_name", "label": "请假人", "w": 10},
            {"name": "leave_type", "label": "类型", "w": 9},
            {"name": "start_date", "label": "开始", "w": 11},
            {"name": "end_date", "label": "结束", "w": 11},
            {"name": "days", "label": "天数", "w": 8, "type": "number"},
            {"name": "status", "label": "审批", "w": 9, "type": "state"},
        ],
    },
    "trips": {
        "title": "出差单",
        "table": "trips",
        "group": "出勤管理",
        "code": ("code", "CC-"),
        "order": "start_date DESC, id DESC",
        "search": ["code", "destination", "reason", "status"],
        "note": "选好出发/回程日期后天数与补贴自动算出。回程不确定时可先留空（按未定处理），回程后补填即可自动重算。提交后进入「出勤审批」。",
        "auto_calc": {"kind": "date", "start": "start_date", "end": "end_date",
                      "targets": [{"field": "days"}]},
        "fields": [
            f("code", "出差单号"),
            f("employee_id", "出差人", type="lookup", options_sql=SQL_USER,
              required=True),
            f("project_id", "所属项目", type="lookup", options_sql=SQL_PROJECT),
            f("destination", "出差地点", required=True),
            f("start_date", "出发日期", type="date", required=True),
            f("end_date", "回程日期", type="date",
              hint="未确定可留空，回程后补填即自动重算"),
            f("days", "出差天数", type="number", step="0.5",
              hint="按起止日期自动算（含首尾）；未定回程时为 0"),
            # 补贴对填单人不可见：按 天数 × 参数里的每日标准 自动计算并存库，
            # 供工资测算使用。想调标准请去「参数设置」。
            f("allowance_std", "每日补贴标准", type="money", step=MONEY_STEP,
              mode="hidden"),
            f("allowance", "出差补贴", type="money", mode="hidden"),
            f("status", "审批状态", type="select_fixed", approval=True,
              options=["待审批", "已批准", "已驳回"]),
            f("reason", "出差事由"),
            f("approve_memo", "审批意见", approval=True),
        ],
        "list": [
            {"name": "code", "label": "出差单号", "w": 11},
            {"name": "employee_name", "label": "出差人", "w": 9},
            {"name": "destination", "label": "出差地点", "w": 14},
            {"name": "project_name", "label": "所属项目", "w": 15},
            {"name": "start_date", "label": "出发", "w": 10},
            {"name": "end_date", "label": "回程", "w": 10},
            {"name": "days", "label": "天数", "w": 7, "type": "number"},
            {"name": "allowance", "label": "补贴", "w": 10, "type": "money"},
            {"name": "status", "label": "审批", "w": 9, "type": "state"},
        ],
    },

    "invoices": {
        "title": "发票管理",
        "table": "invoices",
        "group": "业务单据",
        "code": None,
        "attachments": True,              # 拖入的 PDF 原件挂在这张发票上
        # 发票列多（10 列），手机上横着看不全 → 780px 以下整行变卡片
        # （列表模板 list.html 按这个标记加 .card-on-mobile / .card-wrap）
        "card_on_mobile": True,
        "order": "issue_date DESC, id DESC",
        # unit_name / project_name 是联表列（units.name / projects.name），
        # 搜索白名单放行联表别名 —— 按对方公司、按项目名都能搜到发票
        "search": ["no", "code", "type", "direction", "status", "unit_name",
                   "project_name", "category_name"],
        "calc": {"gross": "amount_gross", "rate": "tax_rate",
                 "net": "amount_net", "tax": "tax_amount"},
        "note": "只需录入【价税合计】和【税率】，不含税金额与税额自动计算；"
                "核销情况在保存后由付款记录维护。"
                "发票不用手录：在列表上方点【选择文件…】把电子发票 PDF 导进来"
                "（电脑上直接拖进去也行），票面字段自动读出来，核对一眼再保存。",
        "fields": [
            f("no", "发票号码", required=True),
            f("code", "发票代码"),
            f("type", "发票类型", type="select_dict", dict_cat="发票类型"),
            f("direction", "发票方向", type="select_dict", dict_cat="发票方向"),
            # 关联合同跟着「发票方向」变：进项只列支出合同、销项只列收入合同。
            # 只针对项目采购、没有对应支出合同的情况，就留空 —— 成本按「所属项目」归集。
            f("contract_id", "关联合同", type="lookup", options_sql=SQL_CONTRACT,
              options_by={"field": "direction",
                          "map": {"开具销项发票": SQL_CONTRACT_INCOME,
                                  "收到进项发票": SQL_CONTRACT_EXPENSE}},
              hint="下选项跟着上面的【发票方向】走："
                   "开具销项发票 → 只列收入合同；收到进项发票 → 只列支出合同。"
                   "如果这张票只是项目上的采购、没有对应支出合同，就留空，"
                   "成本照样按【所属项目】归集，不影响成本与毛利。"),
            f("project_id", "所属项目", type="lookup", options_sql=SQL_PROJECT),
            f("unit_id", "往来单位", type="lookup", options_sql=SQL_UNIT),
            # 进项发票＝成本：这笔成本归到哪一类。项目全景、费用类别统计都按它汇总。
            f("category", "费用类别", type="lookup", options_sql=SQL_COSTCAT,
              hint="收到进项发票时填：这笔成本属于哪一类（人工费 / 材料费 / 设备费…）。"
                   "选完保存，自动生成的那条费用就用这个类别；"
                   "项目全景里按类别汇总的费用也能分清。销项发票留空即可。"),
            f("issue_date", "开票日期", type="date"),
            f("amount_gross", "价税合计", type="money", step=MONEY_STEP,
              hint="含税金额，手工录入"),
            f("tax_rate", "税率", type="rate", step="0.0001", hint="0.09 表示 9%"),
            f("amount_net", "不含税金额", type="money", readonly=True, calc="net",
              hint="由 价税合计 ÷ (1+税率) 自动计算"),
            f("tax_amount", "税额", type="money", readonly=True, calc="tax",
              hint="由 价税合计 − 不含税金额 自动计算"),
            f("status", "发票状态", type="select_dict", dict_cat="发票状态",
              hint="费用未付款＝钱还没付；已收讫＝已付款（进项发票会自动把对应费用"
                   "标记为已支付）；已作废＝无效票（对应的费用会一起删掉）"),
        ],
        "list": [
            {"name": "no", "label": "发票号码", "w": 13},
            {"name": "type", "label": "发票类型", "w": 10},
            {"name": "direction", "label": "方向", "w": 7},
            # 对方公司 = 发票上的「往来单位」，名称联 units 表取
            {"name": "unit_name", "label": "对方公司", "w": 14},
            # 所属项目 = 发票表单里的【所属项目】，名称联 projects 表取
            # （没挂项目的留空；成本/毛利就是按这一列归集的）
            {"name": "project_name", "label": "所属项目", "w": 13},
            {"name": "category_name", "label": "费用类别", "w": 9},
            {"name": "issue_date", "label": "开票日期", "w": 10},
            {"name": "amount_gross", "label": "价税合计", "w": 11, "type": "money"},
            {"name": "settled", "label": "已核销", "w": 10, "type": "money"},
            {"name": "progress", "label": "核销进度", "w": 9, "type": "percent"},
            {"name": "pay_state", "label": "收付状态", "w": 9, "type": "state"},
        ],
    },

    # ==================================================== 记工（点工 / 临时工）
    # ⚠️ 这一组跟「工资管理」里的月薪员工（staff / payroll）是**两套**，互不影响：
    #    那边是正式员工（月薪 + 社保 + 公积金 + 个税）；
    #    这边是工地上的点工、临时工 —— 干一天算一天，整天 / 半天结，没有社保个税。
    "workers": {
        "title": "工人名单",
        "table": "workers",
        "group": "记工管理",
        "code": ("code", "GR-"),
        "order": "sort, name",
        "attachments": True,          # 身份证正反面照片
        "search": ["code", "name", "id_card", "phone", "trade", "memo"],
        "note": "工地上的点工 / 临时工在这里登记（跟「工资管理」里的月薪员工是两套，互不影响）。"
                "身份证正反面照片在表单底部上传；日工资填了，记工时自动带出来。",
        "fields": [
            f("code", "工号", hint="留空自动生成"),
            f("name", "姓名", required=True),
            f("trade", "工种", type="select_dict", dict_cat="工种"),
            f("id_card", "身份证号", hint="身份证正反面照片在表单底部上传"),
            f("phone", "电话"),
            f("default_rate", "默认日工资", type="money", step=MONEY_STEP,
              hint="记工时自动带出来；某天工价不一样（比如 450），在记工那条上直接改"),
            f("rate2", "第二工资", type="money", step=MONEY_STEP,
              hint="另一种活 / 双工的日薪。月度记工表里把格子点成「2」就按它算一整天"),
            f("bank_account", "银行卡号", hint="发工资用，可以不填"),
            f("status", "状态", type="select_fixed", options=["在用", "停用"],
              hint="走的人改成「停用」，不要删 —— 删了历史记工就缺人"),
            f("sort", "排序", type="number", hint="数字小的排前面"),
            f("memo", "备注", type="textarea"),
        ],
        "list": [
            {"name": "name", "label": "姓名", "w": 12},
            {"name": "trade", "label": "工种", "w": 10},
            {"name": "id_card", "label": "身份证号", "w": 20},
            {"name": "phone", "label": "电话", "w": 14},
            {"name": "default_rate", "label": "日工资", "w": 10, "type": "money"},
            {"name": "rate2", "label": "第二工资", "w": 10, "type": "money"},
            {"name": "status", "label": "状态", "w": 8},
        ],
    },

    "worklogs": {
        "title": "记工",
        "table": "worklogs",
        "group": "记工管理",
        "code": None,
        "attachments": True,          # 当天的现场照片
        "order": "work_date DESC, id DESC",
        "search": ["work_date", "worker_name", "project_name", "memo"],
        "note": "一条 = 某人某天。整天选 1、半天选 0.5；日工资默认带出工人名单里的默认值，"
                "某天工价不一样就改那个数。要一次记一批人 / 一批天，用上面的【批量记工】；"
                "看整月的格子表用【月度记工表】。",
        "fields": [
            f("work_date", "日期", type="date", required=True),
            f("worker_id", "工人", type="lookup", options_sql=SQL_WORKER,
              required=True),
            f("project_id", "所属项目", type="lookup", options_sql=SQL_PROJECT),
            f("rate", "当日工资", type="money", step=MONEY_STEP,
              hint="默认带出工人名单里的默认日工资；这天给 450 就改成 450"),
            f("days", "工天", type="select_fixed", options=["1", "0.5"],
              hint="整天选 1，半天选 0.5。金额 = 当日工资 × 工天，保存时自动算"),
            f("memo", "备注", type="textarea"),
        ],
        "list": [
            {"name": "work_date", "label": "日期", "w": 11},
            {"name": "worker_name", "label": "工人", "w": 12},
            {"name": "project_name", "label": "所属项目", "w": 18},
            {"name": "rate", "label": "日工资", "w": 10, "type": "money"},
            {"name": "days", "label": "工天", "w": 7, "type": "number"},
            {"name": "amount", "label": "金额", "w": 10, "type": "money"},
            {"name": "memo", "label": "备注", "w": 14},
        ],
    },

    # ============================================================ 报价 / 送货
    "brands": {
        "title": "品牌管理",
        "table": "brands",
        "group": "基础资料",
        "code": None,
        "order": "name",
        "search": ["name", "memo"],
        "note": "报价单 / 合同清单里填过的品牌会自动进到这里。品牌维护好之后，"
                "下次在清单里直接下拉选，不用重复打字。",
        "fields": [
            f("name", "品牌名称", required=True),
            f("memo", "备注", type="textarea"),
        ],
        "list": [
            {"name": "name", "label": "品牌", "w": 22},
            {"name": "model_count", "label": "型号数", "w": 10, "type": "int"},
            {"name": "memo", "label": "备注", "w": 40},
        ],
    },
    "models": {
        "title": "型号参数库",
        "table": "models",
        "group": "基础资料",
        "code": None,
        "order": "brand_name, name",
        "search": ["name", "spec", "unit", "brand_name"],
        "note": "型号里带参数：参数/规格、常用单位、参考单价。"
                "填了参考单价，报价时选型号会自动带出单价；参数会跟着填进清单。",
        "fields": [
            f("brand_id", "品牌", type="lookup", options_sql=SQL_BRAND, required=True),
            f("name", "型号", required=True, hint="如 S7-1200 CPU1214C"),
            f("spec", "参数 / 规格", type="textarea",
              hint="如 380V 15kW IP55 或 规格 300×100×1.5mm"),
            f("unit", "常用单位", type="select_dict", dict_cat="计量单位"),
            f("price", "参考单价", type="money", step=MONEY_STEP),
            f("memo", "备注", type="textarea"),
        ],
        "list": [
            {"name": "brand_name", "label": "品牌", "w": 16},
            {"name": "name", "label": "型号", "w": 24},
            {"name": "spec", "label": "参数 / 规格", "w": 28},
            {"name": "unit", "label": "单位", "w": 8},
            {"name": "price", "label": "参考单价", "w": 12, "type": "money"},
        ],
    },
    "quotations": {
        "title": "报价单",
        "table": "quotations",
        "group": "业务单据",
        "code": ("code", "BJ-"),
        "order": "quote_date DESC, id DESC",
        "search": ["code", "title", "contact", "phone", "status", "handler"],
        "note": "在下面的【报价明细】里逐项添加，或下载导入模板填好上传。"
                "每一行各自标注【含税 / 不含税】与【税点】。"
                "明细里的品牌 / 型号 / 参数会自动进「品牌型号库」。"
                "报价完成后，管理员、财务或本单经手员工可以点【转为合同】。",
        "fields": [
            f("code", "报价单号", hint="留空自动生成"),
            f("title", "报价标题", hint="如 ××项目 配电箱报价"),
            f("direction", "合同方向", type="select_dict", dict_cat="合同方向",
              hint="转成合同时沿用；给客户报价通常是「收入合同」"),
            f("unit_id", "客户 / 往来单位", type="lookup", options_sql=SQL_UNIT),
            f("contact", "联系人"),
            f("phone", "联系电话"),
            f("project_id", "关联项目", type="lookup", options_sql=SQL_PROJECT),
            f("quote_date", "报价日期", type="date"),
            f("valid_days", "有效期(天)", type="number"),
            f("amount_total", "明细合计", type="money", ro=True,
              hint="由下面清单自动汇总"),
            f("discount", "优惠让利", type="money", step=MONEY_STEP),
            f("amount_final", "报价总额", type="money", ro=True,
              hint="明细合计 − 优惠让利"),
            f("status", "报价状态", type="select_dict", dict_cat="报价状态"),
            f("handler", "报价人"),
            f("handler_user_id", "经手员工", type="lookup", options_sql=SQL_USER,
              hint="关联账号后，该员工可以查看本报价并转为合同"),
            f("memo", "报价说明", type="textarea",
              hint="会打印在报价单底部（付款方式、交期、质保等）"),
        ],
        "list": [
            {"name": "code", "label": "报价单号", "w": 11},
            {"name": "title", "label": "报价标题", "w": 22},
            {"name": "unit_name", "label": "客户", "w": 18},
            {"name": "quote_date", "label": "报价日期", "w": 11},
            {"name": "amount_final", "label": "报价总额", "w": 13, "type": "money"},
            {"name": "status", "label": "状态", "w": 11,
             "type": "inline", "dict_cat": "报价状态"},
            {"name": "handler", "label": "报价人", "w": 9},
        ],
        # 允许在列表页直接改的字段（改完立即生效）
        "inline": ["status"],
        "details": [{
            "key": "items",
            "title": "报价明细",
            "table": "quotation_items",
            "fk": "quotation_id",
            "cols": goods_cols(),
            "sum": ["amount"],
            "library": True,
            "import": True,
            "totals": True,          # 合计回填主表的 明细合计 / 报价总额
        }],
    },
    "private_settlements": {
        "title": "对私计算",
        "table": "private_settlements",
        "group": "业务单据",
        "code": ("code", "DS-"),
        "order": "calc_date DESC, id DESC",
        "search": ["code", "title", "broker", "status", "handler"],
        "note": "一个单子可以同时关联多张合同（上面的勾选面板，可多选）。"
                "明细里一行对应合同里的一个清单项：收入扣掉【扣税】、再扣掉成本，"
                "剩下的付给居间人；行上的【所属合同】标明这一行是哪张合同的，"
                "选了它清单项下拉就只看这张合同。"
                "不挂清单项的成本（运费、临时人工这类）直接加一行："
                "名称手填、收入留 0、只填成本，这一行的应付居间人是负的，"
                "单头「收入 − 扣税 − 成本」的合计照样对得上。"
                "扣税比例是我们自己设定的（名义扣税、实为我方收入），逐行手填、"
                "跟发票税点无关，比例一样时用【批量设置】一次刷完。"
                "成本手工填 —— 参考面板会列出「费用支出」里已经登记的金额。",
        "fields": [
            f("code", "计算单号", hint="留空自动生成"),
            f("title", "计算单标题", hint="如 ××项目 对私结算"),
            f("project_id", "关联项目", type="lookup", options_sql=SQL_PROJECT),
            f("unit_id", "对方单位", type="lookup", options_sql=SQL_UNIT),
            f("broker", "居间人"),
            f("broker_contact", "居间人联系方式"),
            f("calc_date", "计算日期", type="date"),
            f("amount_income", "收入合计", type="money", ro=True,
              hint="由下面的明细自动汇总"),
            f("amount_tax", "我方利润合计", type="money", ro=True,
              hint="明细各行【我方利润】之和（原来叫「扣税」，是名义叫法）；"
                   "收入 − 成本 − 我方利润 = 返还居间人"),
            f("amount_cost", "成本合计", type="money", ro=True),
            f("amount_payable", "应付居间人", type="money", ro=True,
              hint="收入合计 − 扣税合计 − 成本合计"),
            f("status", "状态", type="select_dict", dict_cat="对私状态"),
            f("handler", "经办人"),
            f("memo", "说明", type="textarea"),
        ],
        "list": [
            {"name": "code", "label": "计算单号", "w": 11},
            {"name": "title", "label": "标题", "w": 20},
            {"name": "contract_code", "label": "关联合同", "w": 15},
            {"name": "broker", "label": "居间人", "w": 10},
            {"name": "calc_date", "label": "计算日期", "w": 11},
            {"name": "amount_income", "label": "收入", "w": 11, "type": "money"},
            {"name": "amount_tax", "label": "我方利润", "w": 11, "type": "money"},
            {"name": "amount_cost", "label": "成本", "w": 10, "type": "money"},
            {"name": "amount_payable", "label": "应付居间人", "w": 12, "type": "money"},
            {"name": "status", "label": "状态", "w": 11,
             "type": "inline", "dict_cat": "对私状态"},
        ],
        "inline": ["status"],
        "details": [{
            "key": "items",
            "title": "对私计算明细",
            "table": "private_settlement_items",
            "fk": "settlement_id",
            "cols": private_cols(),
            "sum": ["income", "tax_amount", "cost_amount", "payable"],
            "totals": True,          # 合计回填主表的四个金额
            # 批量设置（明细上方那一条）：只应用在这里主动填/选过的项。
            # 扣税比例是手填的百分数，所以这一条是数字框而不是下拉。
            "bulk": [{"name": "tax_rate", "label": "扣税比例", "type": "pct"}],
            # 清单项可以留空：那是「纯成本行」（只填名称和成本），不是填漏了
            "item_optional": True,
        }],
    },
    "deliveries": {
        "title": "送货单",
        "table": "deliveries",
        "group": "业务单据",
        "code": ("code", "SH-"),
        "order": "deliver_date DESC, id DESC",
        "search": ["code", "receiver", "address", "carrier", "status"],
        "note": "送货单从「合同管理 → 生成送货单」里勾选产生，不单独占菜单。"
                "保存后可【导出 Word】打印，交给收货人签字回执。",
        "fields": [
            f("code", "送货单号", hint="留空自动生成"),
            f("contract_id", "关联合同", type="lookup", options_sql=SQL_CONTRACT,
              hint="选好合同后点【从合同带出清单】"),
            f("project_id", "关联项目", type="lookup", options_sql=SQL_PROJECT),
            f("unit_id", "收货单位", type="lookup", options_sql=SQL_UNIT),
            f("receiver", "收货人"),
            f("phone", "联系电话"),
            f("address", "收货地址"),
            f("deliver_date", "送货日期", type="date"),
            f("carrier", "运输方式 / 车牌", type="select_dict", dict_cat="运输方式"),
            f("status", "送货状态", type="select_dict", dict_cat="送货状态"),
            f("handler", "送货人 / 经办人"),
            f("memo", "备注", type="textarea"),
        ],
        "list": [
            {"name": "code", "label": "送货单号", "w": 11},
            {"name": "deliver_date", "label": "送货日期", "w": 11},
            {"name": "contract_code", "label": "关联合同", "w": 12},
            {"name": "project_name", "label": "关联项目", "w": 16},
            {"name": "unit_name", "label": "收货单位", "w": 18},
            {"name": "receiver", "label": "收货人", "w": 9},
            {"name": "status", "label": "状态", "w": 9},
        ],
        "details": [{
            "key": "items",
            "title": "送货明细",
            "table": "delivery_items",
            "fk": "delivery_id",
            "cols": delivery_cols(),
            "sum": ["qty"],
            "library": True,
        }],
    },

    # ============================================================ 工程交付资料
    # 放在「基础资料」分组下，但**不进基础资料那个选项卡组** ——
    # 它是一份份要交给客户的资料（IP 段 + 设备账号口令 + 点位图 PDF），
    # 跟"项目/单位/品牌"这类下拉底表不是一回事，所以单独占一个菜单项。
    "handovers": {
        "title": "工程交付资料",
        "table": "handovers",
        "group": "基础资料",
        "code": ("code", "JF-"),
        "order": "deliver_date DESC, id DESC",
        "attachments": True,          # 点位图 PDF 挂在这份资料上
        "search": ["code", "project_name", "proj_name", "ip_range", "handler"],
        # 注意：note / hint 都是**原样输出**的，不吃 Markdown（`**xx**` 会连星号一起显示出来），
        # 所以这里一律写纯文本。
        # 没有「资料名称」这一项：名称直接用【对应项目】或【项目名称】。
        "note": "一个工程做完，把要交给客户的东西登记在这儿：IP 地址段、每台设备"
                "（录像机 / 门禁 / 交换机…）的 IP、子网掩码、网关、端口、用户名和口令，"
                "以及点位图 PDF。PDF 在这张单子保存后，到页面下方的【合同原件】里上传。"
                "【对应项目】从「项目管理」里选；这个项目还没进项目管理的话，"
                "直接在【项目名称】里手写一个就行 —— 手填的项目名不会出现在项目管理里，"
                "两处填一个即可，这个填的就是这份资料的名称。",
        "fields": [
            f("code", "资料编号", hint="留空自动生成"),
            f("project_id", "对应项目", type="lookup", options_sql=SQL_PROJECT,
              hint="从「项目管理」里选一个（它就是这份资料的名称）；"
                   "下拉里没有你要的项目，就先在「项目管理」里建，"
                   "或者直接用下面的【项目名称】手填"),
            f("project_name", "项目名称（手填）",
              hint="对应不到已有项目时填这里。填了这个就不用再选【对应项目】，"
                   "两处只能填一个；手填的名字不会出现在项目管理里。"),
            f("ip_range", "IP 地址段", type="textarea",
              hint="可以写多段、一行一段。例如："
                   "192.168.10.0/24（办公）、192.168.20.0/24（监控）、"
                   "10.0.0.1 - 10.0.0.60（门禁）。"
                   "同一网段里一批连续地址写在设备清单那一行也可以。"),
            f("deliver_date", "交付时间", type="date", hint="自己选，留空也可以"),
            f("handler", "交付人"),
            f("memo", "备注", type="textarea"),
        ],
        "list": [
            {"name": "code", "label": "资料编号", "w": 10},
            {"name": "proj_label", "label": "对应项目 / 名称", "w": 26},
            {"name": "deliver_date", "label": "交付时间", "w": 11},
            {"name": "ip_range", "label": "IP 地址段", "w": 20},
            {"name": "device_count", "label": "设备数", "w": 7, "type": "int"},
            {"name": "files", "label": "点位图", "w": 8, "type": "files"},
            {"name": "handler", "label": "交付人", "w": 8},
        ],
        # 一台设备一行，可多台；IP / 子网掩码 / 网关 / 用户名 / 口令 都存在明细里。
        # 【IP 地址】是纯文本，允许写范围或一串地址 ——
        # 一个机柜 10 个摄像头不必开 10 行，写「192.168.10.11-20」一行就够，
        # 分属不同网段就用「、」隔开（10.0.0.11-13、10.0.1.11-13）。
        "details": [{
            "key": "devices",
            "title": "设备清单（IP / 掩码 / 网关 / 用户名 / 密码）",
            "table": "handover_devices",
            "fk": "handover_id",
            "cols": [
                dc("name", "设备名称", "text", "13%", ph="如 录像机 / 门禁主机"),
                dc("model", "型号", "text", "9%"),
                dc("ip", "IP 地址", "text", "17%",
                   ph="192.168.10.11-20 或 10.0.0.11-13、10.0.1.11-13"),
                dc("netmask", "子网掩码", "text", "10%", ph="255.255.255.0"),
                dc("gateway", "网关", "text", "12%", ph="192.168.10.1"),
                dc("port", "端口", "text", "6%", ph="8000"),
                dc("username", "用户名", "text", "10%"),
                dc("password", "密码", "text", "10%"),
                dc("remark", "备注", "text", "13%"),
            ],
        }],
    },
}


# ---------------------------------------------------------------- 查询与报表

# 台账 / 报表的数据范围：查询 key -> (实体, 该 SQL 里的表别名)。
# 员工登录时按 scope_where 拼条件；没登记的查询（按类别汇总、单位台账、
# 出勤汇总这类没法按项目切的）员工一律看不到，避免放开权限后看到全公司数据。
QUERY_SCOPE = {
    "quotation_ledger": ("quotations", "q"),
    "contract_item_ledger": ("contracts", "h"),
    "contract_ledger": ("contracts", "c"),
    "project_cost": ("projects", "p"),
    "project_profit": ("projects", "p"),
    "invoice_ledger": ("invoices", "v"),
    "payment_ledger": ("payments", "x"),
    "cost_ledger": ("costs", "k"),
    "settle_detail": ("payments", "x"),
    "settle_summary": ("payments", "x"),
    "invoice_open": ("invoices", "v"),
    "advance_ledger": ("advances", "a"),
    "reimb_ledger": ("costs", "c"),
}


QUERIES = [
    {
        "key": "quotation_ledger",
        "title": "报价台账",
        "group": "查询统计",
        "desc": "报价金额、状态、是否已转合同、报价人与客户",
        "sql": """
SELECT q.code AS 报价单号, q.title AS 报价标题, q.quote_date AS 报价日期,
       u.name AS 客户, p.name AS 关联项目, q.direction AS 合同方向,
       q.amount_total AS 明细合计, q.discount AS 优惠让利,
       q.amount_final AS 报价总额, q.status AS 报价状态,
       IFNULL(h.code,'') AS 已转合同, q.handler AS 报价人
FROM quotations q
LEFT JOIN units u ON q.unit_id = u.id
LEFT JOIN projects p ON q.project_id = p.id
LEFT JOIN contracts h ON q.contract_id = h.id
ORDER BY q.code
""",
    },
    {
        "key": "contract_item_ledger",
        "title": "合同清单汇总",
        "group": "查询统计",
        "desc": "逐项列出各合同的清单：品名、品牌型号参数、数量、单价、金额",
        "sql": """
SELECT h.code AS 合同编号, h.name AS 合同名称, p.name AS 项目名称,
       i.seq AS 序号, i.name AS 品名, i.brand AS 品牌, i.model AS 型号,
       i.spec AS 参数规格, i.unit AS 单位, i.qty AS 数量,
       i.price AS 单价, i.amount AS 金额, i.remark AS 备注
FROM contract_items i
LEFT JOIN contracts h ON i.contract_id = h.id
LEFT JOIN projects p ON h.project_id = p.id
ORDER BY h.code, i.seq, i.id
""",
    },
    {
        "key": "contract_ledger",
        "title": "合同台账",
        "group": "查询统计",
        "desc": "合同金额、累计付款、未付金额与付款进度",
        "sql": """
SELECT c.code AS 合同编号, c.name AS 合同名称, c.direction AS 合同方向,
       p.name AS 项目名称, u.name AS 对方单位,
       c.amount_gross AS 合同金额,
       IFNULL((SELECT SUM(x.amount) FROM payments x
               WHERE x.contract_id = c.id AND x.direction = '收入合同'), 0) AS 累计收款,
       IFNULL((SELECT SUM(x.amount) FROM payments x
               WHERE x.contract_id = c.id AND x.direction = '支出合同'), 0) AS 累计付款,
       c.status AS 履行状态
FROM contracts c
LEFT JOIN projects p ON c.project_id = p.id
LEFT JOIN units u ON c.unit_id = u.id
ORDER BY c.code
""",
    },
    {
        "key": "project_cost",
        "title": "项目成本汇总",
        "group": "查询统计",
        "desc": "按项目归集收入合同、支出合同、已付资金与费用",
        "sql": """
SELECT p.code AS 项目编号, p.name AS 项目名称, p.status AS 项目状态, p.budget AS 项目预算,
       IFNULL((SELECT SUM(c.amount_gross) FROM contracts c
               WHERE c.project_id = p.id AND c.direction = '收入合同'), 0) AS 收入合同额,
       IFNULL((SELECT SUM(c.amount_gross) FROM contracts c
               WHERE c.project_id = p.id AND c.direction = '支出合同'), 0) AS 支出合同额,
       IFNULL((SELECT SUM(x.amount) FROM payments x
               WHERE x.project_id = p.id AND x.direction = '支出合同'), 0) AS 已付支出,
       IFNULL((SELECT SUM(k.amount) FROM costs k WHERE k.project_id = p.id), 0) AS 费用合计
FROM projects p
ORDER BY p.code
""",
    },
    {
        "key": "project_profit",
        "title": "项目收益分析",
        "group": "查询统计",
        "desc": "收入 − 支出合同 − 费用 = 预计毛利与毛利率",
        "sql": """
SELECT p.code AS 项目编号, p.name AS 项目名称,
       IFNULL((SELECT SUM(c.amount_gross) FROM contracts c
               WHERE c.project_id = p.id AND c.direction = '收入合同'), 0) AS 合同收入,
       IFNULL((SELECT SUM(c.amount_gross) FROM contracts c
               WHERE c.project_id = p.id AND c.direction = '支出合同'), 0) AS 合同支出,
       IFNULL((SELECT SUM(k.amount) FROM costs k WHERE k.project_id = p.id), 0) AS 费用合计,
       IFNULL((SELECT SUM(c.amount_gross) FROM contracts c
               WHERE c.project_id = p.id AND c.direction = '收入合同'), 0)
       - IFNULL((SELECT SUM(c.amount_gross) FROM contracts c
                 WHERE c.project_id = p.id AND c.direction = '支出合同'), 0)
       - IFNULL((SELECT SUM(k.amount) FROM costs k WHERE k.project_id = p.id), 0) AS 预计毛利
FROM projects p
ORDER BY p.code
""",
    },
    {
        "key": "invoice_ledger",
        "title": "发票台账",
        "group": "查询统计",
        "desc": "发票金额与核销进度、收付状态",
        "sql": """
SELECT v.no AS 发票号码, v.type AS 发票类型, v.direction AS 发票方向,
       v.issue_date AS 开票日期, v.amount_gross AS 价税合计,
       CASE WHEN IFNULL(v.status,'') = '已收讫' THEN v.amount_gross
            ELSE IFNULL((SELECT SUM(s.amount) FROM settlements s
                         WHERE s.invoice_id = v.id), 0) END AS 已核销金额,
       CASE WHEN IFNULL(v.status,'') = '已收讫' THEN 0
            ELSE v.amount_gross - IFNULL((SELECT SUM(s.amount) FROM settlements s
                         WHERE s.invoice_id = v.id), 0) END AS 未核销金额,
       CASE WHEN IFNULL(v.status,'') = '已收讫' THEN 1
            WHEN v.amount_gross <= 0 THEN 0
            ELSE IFNULL((SELECT SUM(s.amount) FROM settlements s
                         WHERE s.invoice_id = v.id), 0) / v.amount_gross END AS 核销进度,
       v.status AS 发票状态, p.name AS 项目名称, u.name AS 往来单位,
       h.code AS 关联合同
FROM invoices v
LEFT JOIN projects p ON v.project_id = p.id
LEFT JOIN units u ON v.unit_id = u.id
LEFT JOIN contracts h ON v.contract_id = h.id
ORDER BY v.issue_date DESC, v.id DESC
""",
        "percent_cols": ["核销进度"],
        "state_from": ("已核销金额", "价税合计", "收付状态"),
    },
    {
        "key": "payment_ledger",
        "title": "付款台账",
        "group": "查询统计",
        "desc": "付款明细与核销的发票张数",
        "sql": """
SELECT x.code AS 付款单号, x.pay_date AS 付款日期, x.direction AS 付款方向,
       x.nature AS 款项性质, x.amount AS 付款金额, x.pay_method AS 付款方式,
       h.code AS 关联合同, p.name AS 关联项目,
       IFNULL((SELECT COUNT(*) FROM settlements s WHERE s.payment_id = x.id), 0) AS 核销发票数,
       IFNULL((SELECT SUM(s.amount) FROM settlements s WHERE s.payment_id = x.id), 0) AS 已核销金额,
       x.amount - IFNULL((SELECT SUM(s.amount) FROM settlements s WHERE s.payment_id = x.id), 0)
              AS 未分配金额,
       x.handler AS 经办人
FROM payments x
LEFT JOIN contracts h ON x.contract_id = h.id
LEFT JOIN projects p ON x.project_id = p.id
ORDER BY x.pay_date DESC, x.id DESC
""",
    },
    {
        "key": "cost_ledger",
        "title": "费用台账",
        "group": "查询统计",
        "desc": "费用明细，按类别与项目归集",
        "sql": """
SELECT k.code AS 费用单号, k.cost_date AS 发生日期, cc.name AS 费用类别,
       k.summary AS 费用摘要, k.amount AS 金额, k.pay_status AS 支付状态,
       p.name AS 项目名称, h.code AS 关联合同, u.name AS 往来单位, k.handler AS 经办人
FROM costs k
LEFT JOIN cost_categories cc ON k.category = cc.id
LEFT JOIN projects p ON k.project_id = p.id
LEFT JOIN contracts h ON k.contract_id = h.id
LEFT JOIN units u ON k.unit_id = u.id
ORDER BY k.cost_date DESC, k.id DESC
""",
    },
    {
        "key": "cost_category_stat",
        "title": "费用类别统计",
        "group": "查询统计",
        "desc": "每个费用类别的使用笔数与累计金额",
        "sql": """
SELECT c.name AS 类别名称, c.cost_attr AS 成本属性,
       CASE WHEN c.enabled = 1 THEN '是' ELSE '否' END AS 是否启用,
       IFNULL((SELECT COUNT(*) FROM costs k WHERE k.category = CAST(c.id AS TEXT)), 0) AS 使用笔数,
       IFNULL((SELECT SUM(k.amount) FROM costs k WHERE k.category = CAST(c.id AS TEXT)), 0) AS 累计金额
FROM cost_categories c
ORDER BY c.sort, c.name
""",
    },
    {
        "key": "unit_ledger",
        "title": "往来单位台账",
        "group": "查询统计",
        "desc": "每家单位的合同、付款与发票汇总",
        "sql": """
SELECT u.name AS 单位名称, u.type AS 单位类型, u.contact AS 联系人, u.phone AS 联系电话,
       IFNULL((SELECT COUNT(*) FROM contracts c WHERE c.unit_id = u.id), 0) AS 合同数,
       IFNULL((SELECT SUM(c.amount_gross) FROM contracts c WHERE c.unit_id = u.id), 0) AS 合同金额,
       IFNULL((SELECT SUM(x.amount) FROM payments x
               WHERE x.contract_id IN (SELECT id FROM contracts c WHERE c.unit_id = u.id)), 0) AS 累计付款,
       IFNULL((SELECT COUNT(*) FROM invoices v WHERE v.unit_id = u.id), 0) AS 发票张数,
       IFNULL((SELECT SUM(v.amount_gross) FROM invoices v WHERE v.unit_id = u.id), 0) AS 发票金额
FROM units u
ORDER BY u.name
""",
    },
    {
        "key": "settle_detail",
        "title": "付款核销明细",
        "group": "核销与收付",
        "desc": "每一笔付款与每一张发票的核销对应关系",
        "sql": """
SELECT x.code AS 付款单号, x.pay_date AS 付款日期, x.direction AS 付款方向,
       x.amount AS 付款金额, v.no AS 发票号码, v.issue_date AS 开票日期,
       v.amount_gross AS 发票价税合计, s.amount AS 核销金额, s.ratio AS 核销比例,
       h.code AS 关联合同, p.name AS 关联项目
FROM settlements s
LEFT JOIN payments x ON s.payment_id = x.id
LEFT JOIN invoices v ON s.invoice_id = v.id
LEFT JOIN contracts h ON v.contract_id = h.id
LEFT JOIN projects p ON v.project_id = p.id
ORDER BY x.pay_date DESC, v.issue_date
""",
        "percent_cols": ["核销比例"],
    },
    {
        "key": "settle_summary",
        "title": "付款核销汇总",
        "group": "核销与收付",
        "desc": "每笔付款的已核销、未分配金额与核销比例",
        "sql": """
SELECT x.code AS 付款单号, x.pay_date AS 付款日期, x.direction AS 付款方向,
       x.amount AS 付款金额,
       IFNULL((SELECT COUNT(*) FROM settlements s WHERE s.payment_id = x.id), 0) AS 核销发票数,
       IFNULL((SELECT SUM(s.amount) FROM settlements s WHERE s.payment_id = x.id), 0) AS 已核销金额,
       x.amount - IFNULL((SELECT SUM(s.amount) FROM settlements s WHERE s.payment_id = x.id), 0)
              AS 未分配金额,
       CASE WHEN x.amount = 0 THEN 0
            ELSE IFNULL((SELECT SUM(s.amount) FROM settlements s WHERE s.payment_id = x.id), 0)
                 / x.amount END AS 核销比例,
       p.name AS 关联项目, h.code AS 关联合同
FROM payments x
LEFT JOIN projects p ON x.project_id = p.id
LEFT JOIN contracts h ON x.contract_id = h.id
ORDER BY x.pay_date DESC
""",
        "percent_cols": ["核销比例"],
    },
    {
        "key": "invoice_open",
        "title": "未结清发票",
        "group": "核销与收付",
        "desc": "尚未全额收付的发票，便于催收与对账",
        "sql": """
SELECT v.no AS 发票号码, v.type AS 发票类型, v.direction AS 发票方向,
       v.issue_date AS 开票日期, v.amount_gross AS 价税合计,
       IFNULL((SELECT SUM(s.amount) FROM settlements s WHERE s.invoice_id = v.id), 0) AS 已核销金额,
       v.amount_gross - IFNULL((SELECT SUM(s.amount) FROM settlements s WHERE s.invoice_id = v.id), 0)
              AS 未核销金额,
       CASE WHEN v.amount_gross <= 0 THEN 0
            ELSE IFNULL((SELECT SUM(s.amount) FROM settlements s WHERE s.invoice_id = v.id), 0)
                 / v.amount_gross END AS 核销进度,
       u.name AS 往来单位, p.name AS 项目名称
FROM invoices v
LEFT JOIN units u ON v.unit_id = u.id
LEFT JOIN projects p ON v.project_id = p.id
WHERE IFNULL(v.status,'') NOT IN ('已收讫','已作废')   -- 已收讫＝钱付清了、已作废＝无效票
  AND IFNULL((SELECT SUM(s.amount) FROM settlements s WHERE s.invoice_id = v.id), 0)
      < v.amount_gross - 0.01
ORDER BY v.issue_date
""",
        "percent_cols": ["核销进度"],
    },

    # ==================================================== 费用报销
    {
        "key": "advance_ledger",
        "title": "预支款台账",
        "group": "费用报销",
        "desc": "财务打款给员工的预支款、已核销金额与可用余额",
        "sql": """
SELECT a.code AS 预支单号, IFNULL(u.display_name, u.username) AS 收款员工,
       a.pay_date AS 打款日期, a.amount AS 预支金额,
       IFNULL((SELECT SUM(c.amount) FROM costs c
               WHERE c.advance_id = a.id
                 AND IFNULL(c.approval_status, '') IN ('已批准', '已打款')), 0) AS 已核销,
       a.amount - IFNULL((SELECT SUM(c.amount) FROM costs c
               WHERE c.advance_id = a.id
                 AND IFNULL(c.approval_status, '') IN ('已批准', '已打款')), 0) AS 未核销余额,
       a.status AS 状态, a.handler AS 经办
FROM advances a
LEFT JOIN users u ON a.employee_id = u.id
ORDER BY a.pay_date DESC, a.id DESC
""",
    },
    {
        "key": "reimb_ledger",
        "title": "报销台账",
        "group": "费用报销",
        "desc": "所有费用报销单据的报销人、金额与审批打款状态",
        "sql": """
SELECT c.code AS 费用单号, IFNULL(u.display_name, u.username) AS 报销人,
       c.settle_type AS 报销方式, c.cost_date AS 发生日期,
       p.name AS 项目名称, cc.name AS 费用类别, c.summary AS 费用摘要,
       c.amount AS 金额, IFNULL(c.approval_status, '') AS 审批状态,
       a.code AS 关联预支单, c.paid_at AS 打款时间
FROM costs c
LEFT JOIN cost_categories cc ON c.category = cc.id
LEFT JOIN users u ON c.employee_id = u.id
LEFT JOIN projects p ON c.project_id = p.id
LEFT JOIN advances a ON c.advance_id = a.id
WHERE c.settle_type IS NOT NULL AND c.settle_type <> ''
ORDER BY c.cost_date DESC, c.id DESC
""",
    },
    {
        "key": "attend_summary",
        "title": "出勤汇总（按月）",
        "group": "出勤管理",
        "desc": "按员工与月份汇总加班时长、请假天数、出差天数与出差补贴",
        "sql": """
SELECT IFNULL(u.display_name, u.username) AS 员工,
       m.ym AS 月份,
       IFNULL(ot.hours, 0) AS 加班小时,
       IFNULL(lv.days, 0) AS 请假天数,
       IFNULL(tr.days, 0) AS 出差天数,
       IFNULL(tr.allowance, 0) AS 出差补贴
FROM (SELECT employee_id AS eid, substr(ot_date, 1, 7) AS ym
        FROM overtime WHERE status = '已批准'
      UNION SELECT employee_id, substr(start_date, 1, 7)
        FROM leaves WHERE status = '已批准'
      UNION SELECT employee_id, substr(start_date, 1, 7)
        FROM trips WHERE status = '已批准') m
LEFT JOIN users u ON m.eid = u.id
LEFT JOIN (SELECT employee_id, substr(ot_date, 1, 7) AS ym, SUM(hours) AS hours
           FROM overtime WHERE status = '已批准'
           GROUP BY employee_id, substr(ot_date, 1, 7)) ot
       ON ot.employee_id = m.eid AND ot.ym = m.ym
LEFT JOIN (SELECT employee_id, substr(start_date, 1, 7) AS ym, SUM(days) AS days
           FROM leaves WHERE status = '已批准'
           GROUP BY employee_id, substr(start_date, 1, 7)) lv
       ON lv.employee_id = m.eid AND lv.ym = m.ym
LEFT JOIN (SELECT employee_id, substr(start_date, 1, 7) AS ym,
                  SUM(days) AS days, SUM(allowance) AS allowance
           FROM trips WHERE status = '已批准'
           GROUP BY employee_id, substr(start_date, 1, 7)) tr
       ON tr.employee_id = m.eid AND tr.ym = m.ym
WHERE u.id IS NOT NULL
ORDER BY m.ym DESC, 员工
""",
    },
]

REPORTS = [
    {
        "key": "rpt_contracts",
        "title": "合同台账表",
        "group": "报表打印",
        "query": "contract_ledger",
        "landscape": True,
    },
    {
        "key": "rpt_project_cost",
        "title": "项目成本汇总表",
        "group": "报表打印",
        "query": "project_cost",
        "landscape": True,
    },
    {
        "key": "rpt_invoices",
        "title": "发票台账表",
        "group": "报表打印",
        "query": "invoice_ledger",
        "landscape": True,
    },
    {
        "key": "rpt_settle",
        "title": "付款核销明细表",
        "group": "报表打印",
        "query": "settle_detail",
        "landscape": True,
    },
]

MENU = [
    # 基础资料改用选项卡组：项目 / 往来单位 / 品牌 / 型号 / 费用类别 / 下拉选项
    # 工程交付资料**不进选项卡组**（它不是下拉底表），直接在分组下单独列一项
    ("基础资料", [("t", "base"), ("e", "handovers")]),
    # 报价单 → 合同 → 付款 / 费用 / 发票，按业务发生顺序排
    ("业务单据", [("e", "quotations"), ("e", "contracts"), ("e", "payments"),
                  ("e", "costs"), ("e", "invoices"),
                  ("e", "private_settlements")]),
    # 记工：工地点工 / 临时工（跟下面「工资管理」的月薪员工是两套）
    # 记工只在「月度记工表」上点格子录，不再单独留一张明细列表当菜单项。
    # 这几组用 "t"（选项卡组）：左侧只占一行，点进去是一排选项卡，
    # 避免左菜单越堆越长。
    ("费用报销", [("t", "reimburse")]),
    # 记工管理放在「费用报销」下面：记工最后也是汇成一条人工费进费用支出
    ("记工管理", [("p", "worklog_sheet"), ("e", "workers")]),
    ("出勤管理", [("t", "attendance")]),
    ("工资管理", [("t", "payroll")]),
    ("查询统计", [("t", "reports")]),
    ("系统管理", [("p", "settings"), ("u", "users"), ("u", "logs"),
                  ("p", "backup")]),
]

# 自定义页面（非实体的功能页）。roles 为空表示所有登录用户可见。
PAGES = {
    "reimb_my": {"title": "我的费用报销", "endpoint": "reimb_my", "roles": []},
    "reimb_approve": {"title": "报销审批与打款", "endpoint": "reimb_approve",
                      "roles": ["admin", "finance"]},
    "attend_approve": {"title": "出勤审批", "endpoint": "attend_approve",
                       "roles": ["admin", "finance"]},
    "payroll": {"title": "工资与补贴测算", "endpoint": "payroll",
                "roles": ["admin", "finance"]},
    # 月度记工表：行=工人、列=1~31 号，能打印给工人签字
    "worklog_sheet": {"title": "月度记工表", "endpoint": "worklog_sheet",
                      "roles": ["admin", "finance"]},
    "settings": {"title": "参数设置", "endpoint": "settings_page",
                 "roles": ["admin"]},
    # 数据备份：把库与附件一键备份到飞牛存储空间1（也可自选位置）。
    # 只在管理员菜单里出现，直接敲地址也会被 page_required 拦掉。
    "backup": {"title": "数据备份", "endpoint": "backup_page",
               "roles": ["admin"]},
    # 对私利润汇总：跨单据看「我方利润」和「返还居间人的总额」
    "profit": {"title": "对私利润汇总", "endpoint": "profit_summary",
               "roles": ["admin", "finance"]},
}

LEDGER_OF = {"contracts": "contract_ledger", "payments": "payment_ledger",
             "costs": "cost_ledger", "invoices": "invoice_ledger",
             "advances": "advance_ledger"}

# 出勤类实体（员工只能看自己的；审批走独立页面）
ATTEND_TABLES = ("overtime", "leaves", "trips")
# 按「报销人」归属过滤的实体
OWNER_TABLES = ("costs", "advances", "overtime", "leaves", "trips")

# ---------------------------------------------------------------- 选项卡组
# 左侧菜单里的一项，展开后是一排选项卡。这样"加班申请/请假申请/出差单"
# 这类同类功能只占一个菜单位置，界面不至于被菜单挤满。
# items 里的三元组：(类型, 键, 显示名)
#   e → 实体列表（/e/<key>）
#   q → 查询台账（/q/<key>）
#   p → 自定义功能页（PAGES 里的 key）
_REIMB_QUERIES = {"advance_ledger", "reimb_ledger"}

TAB_GROUPS = {
    "base": {
        "title": "基础资料",
        "items": [("e", "projects", "项目管理"),
                  ("e", "units", "往来单位"),
                  ("e", "brands", "品牌管理"),
                  ("e", "models", "型号参数库"),
                  ("e", "cost_categories", "费用类别"),
                  ("e", "dicts", "下拉选项")],
    },
    "attendance": {
        "title": "出勤管理",
        "items": [("e", "overtime", "加班申请"),
                  ("e", "leaves", "请假申请"),
                  ("e", "trips", "出差单"),
                  ("q", "attend_summary", "出勤汇总"),
                  ("p", "attend_approve", "出勤审批")],
    },
    "payroll": {
        "title": "工资管理",
        "items": [("e", "staff", "员工花名册"),
                  ("p", "payroll", "工资测算")],
    },
    "reimburse": {
        "title": "费用报销",
        "items": [("e", "advances", "预支打款"),
                  ("q", "advance_ledger", "预支款台账"),
                  ("q", "reimb_ledger", "报销台账"),
                  ("p", "reimb_my", "我的费用报销"),
                  ("p", "reimb_approve", "报销审批与打款")],
    },
    "reports": {
        "title": "查询统计",
        "items": ([("q", q["key"], q["title"]) for q in QUERIES
                   if q["key"] not in _REIMB_QUERIES
                   and q.get("group") != "出勤管理"]
                  + [("p", "profit", "对私利润汇总")]),
    },
}

# 反向索引：(类型, 键) → 所属选项卡组。用于判断当前页该高亮哪个选项卡。
GROUP_OF_ITEM = {}
for _gkey, _g in TAB_GROUPS.items():
    for _kind, _key, _label in _g["items"]:
        GROUP_OF_ITEM[(_kind, _key)] = _gkey

# 自定义功能页 → 所属组（按 endpoint 反查）
GROUP_OF_PAGE = {PAGES[k]["endpoint"]: gk
                 for gk, g in TAB_GROUPS.items()
                 for kind, k, _l in g["items"] if kind == "p"}

# ------------------------------------------------------------ 角色可见范围
# 分组名 → 可见角色。没列出来的分组对所有登录用户开放。
#
# 员工（employee）的定位是"录自己经手的单据、看自己相关的数据"：
#   · 工资管理   —— 工资测算与花名册属于管理/财务职能
#   · 查询统计   —— 合同台账、项目成本、发票台账等是跨项目汇总视图
#   · 系统管理   —— 用户管理与登录日志
# 其余分组（基础资料 / 业务单据 / 费用报销 / 出勤管理）员工可以进，
# 但数据范围由 services.scope_where 收窄到"自己负责的项目 + 自己提交的单据"。
GROUP_ROLES = {
    "工资管理": ("admin", "finance"),
    # 记工里是工人的日工资和身份证，跟工资一样只给管理员 / 财务看
    "记工管理": ("admin", "finance"),
    "查询统计": ("admin", "finance"),
    "系统管理": ("admin",),
}

# 组内单项 → 可见角色（优先于上面的分组设置）。
# 这三张台账是"全员汇总"视图：员工要看自己的，入口在费用报销 / 出勤管理里
# 各自都有（我的费用报销、加班申请…），不该再从这里看到别人的金额。
ITEM_ROLES = {
    # 对私计算涉及给居间人分钱，默认只给管理员与财务
    ("e", "private_settlements"): ("admin", "finance"),
    ("p", "profit"): ("admin", "finance"),
    ("q", "attend_summary"):  ("admin", "finance"),
    ("q", "advance_ledger"):  ("admin", "finance"),
    ("q", "reimb_ledger"):    ("admin", "finance"),
    ("u", "users"):           ("admin",),
    ("u", "logs"):            ("admin",),
}

# 不在左侧菜单出现、但有独立页面的项（权限仍按下面的分组判定）。
# 送货单不再单独占一个菜单入口：它从「合同管理 → 生成送货单」进来。
HIDDEN_ITEMS = [
    ("e", "deliveries", "业务单据"),
]

# (类型, 键) → 所属分组名。菜单直接列出的项和选项卡组内的项都收进这一张表，
# 判断"某个页面归哪个分组、谁能看"就只需要查这里。
GROUP_OF_ITEM_ALL = {}
for _gname, _items in MENU:
    for _kind, _k in _items:
        if _kind == "t":
            _g = TAB_GROUPS.get(_k)
            if not _g:
                continue
            for _k2, _key2, _l in _g["items"]:
                GROUP_OF_ITEM_ALL[(_k2, _key2)] = _g["title"]
        else:
            GROUP_OF_ITEM_ALL[(_kind, _k)] = _gname
for _kind, _k, _gname in HIDDEN_ITEMS:
    GROUP_OF_ITEM_ALL[(_kind, _k)] = _gname
# 报表页（/r/<key>）仍按「查询统计」管
for _r in REPORTS:
    GROUP_OF_ITEM_ALL.setdefault(("r", _r["key"]), "查询统计")


def item_group(kind, key):
    """某个页面项属于哪个分组。"""
    return GROUP_OF_ITEM_ALL.get((kind, key))


def item_roles(kind, key):
    """某个页面项允许哪些角色访问；返回 None 表示不限。"""
    r = ITEM_ROLES.get((kind, key))
    if r is not None:
        return r
    return GROUP_ROLES.get(item_group(kind, key))


# ------------------------------------------------------------ 页面权限目录
# 「用户管理 → 页面权限」用：把左侧菜单里**可以按账号开关**的页面项摊平。
# 系统管理（用户管理 / 登录日志 / 参数设置 / 数据备份）只给管理员用，
# 不参与"按人配置"，免得一不小心把危险功能开给普通账号。
PERM_SKIP_PAGES = {"settings", "backup"}


def perm_catalog():
    """[(分组名, [(kind, key, 标题), ...]), ...] —— 可配置页面项清单。"""
    out = []
    for gname, items in MENU:
        entries = []
        for kind, k in items:
            if kind == "t":
                grp = TAB_GROUPS.get(k) or {}
                for k2, key2, label in grp.get("items", ()):
                    if k2 == "p" and key2 in PERM_SKIP_PAGES:
                        continue
                    entries.append((k2, key2, label))
                continue
            if kind == "u":                      # 用户管理 / 登录日志：仅管理员
                continue
            if kind == "p":
                if k in PERM_SKIP_PAGES:
                    continue
                entries.append(("p", k, PAGES[k]["title"]))
            elif kind == "e":
                entries.append(("e", k, ENTITIES[k]["title"]))
        if entries:
            out.append((gname, entries))
    return out


def perm_items():
    """摊平成 [(kind, key, 标题, 分组名), ...]，保存表单时按它遍历。"""
    return [(kind, key, label, gname)
            for gname, items in perm_catalog()
            for kind, key, label in items]

