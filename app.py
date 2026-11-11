# -*- coding: utf-8 -*-
"""工程合同・费用・发票管理系统 —— Web 版（可部署于飞牛 NAS / Docker）

权限模型（三种角色）：
  admin    管理员 ：全部功能 + 用户管理 + 登录日志 + 参数设置
  finance  财务   ：除「用户管理」「登录日志」「参数设置」外基本等同于管理员：
                    可增删改查、维护字典与费用类别、审批与打款费用报销、
                    打款预支、审批加班/请假/出差、查看全部台账与工资测算
  employee 员工   ：只保留「自己经手」的功能 —— 费用报销、出勤申请，
                    以及自己负责项目下的合同/付款/费用/发票；
                    工资管理、查询统计、系统管理不对员工开放
                    （meta.GROUP_ROLES 定义分组可见性，meta.ITEM_ROLES 收窄到单项）。

员工的数据范围限制在「自己负责的项目（projects.manager_user_id）」与
「管理员指定的项目（project_access）」，加上自己提交的单据；越权直达一律 403。
"""
import csv
import io
import json
import os
import re
import secrets
import shutil
import sqlite3
import time
import uuid
from datetime import datetime, timedelta
from functools import wraps

from flask import (Flask, Response, abort, flash, g, jsonify, redirect,
                   render_template, request, send_from_directory, session, url_for)
from markupsafe import escape

import invoice_parse
import meta
import services as S
from db import (UPLOAD_DIR, close_db, dict_options, init_db, lookup_options,
                now_str, query)

APP_TITLE = os.environ.get("APP_TITLE", "工程合同・费用・发票管理系统")
# 版本号：正式版从 1.00 起算（改这里 + docker-compose 的 APP_VERSION 一起改）
APP_VERSION = os.environ.get("APP_VERSION", "1.12")
# 正式版发布日期：显示在登录页与「参数设置 → 系统信息」
RELEASE_DATE = os.environ.get("RELEASE_DATE", "2026-11-04")
# 公司（使用单位）名称：显示在顶栏品牌位、登录页、首页与报表表头
COMPANY_NAME = os.environ.get("COMPANY_NAME", "示例公司")
APP_SUBTITLE = os.environ.get("APP_SUBTITLE",
                              "合同台账 ・ 费用归集 ・ 发票管理 ・ 合同原件归档 ・ 收付核销")
SESSION_TIMEOUT_MIN = int(os.environ.get("SESSION_TIMEOUT_MIN", "120"))
# 走 HTTPS 反向代理后打开：把 http 请求 301 到 https
FORCE_HTTPS = os.environ.get("FORCE_HTTPS", "") == "1"
# 可选 IP 白名单（逗号分隔，支持 CIDR），留空不限制。仅启用 HTTPS 反代后再开更好用
ALLOW_IP_CIDRS = S.parse_allow_list(os.environ.get("ALLOW_IP_CIDRS", ""))

# 只有管理与财务能进入的基础资料模块（字典、费用类别属于口径维护）
MANAGER_ONLY_ENTITIES = {"dicts", "cost_categories"}

# 「用户管理 → 页面权限」可以按账号放开/收起的页面类型
# （e 实体列表 / q 查询台账 / r 报表 / p 功能页；用户管理这类仅管理员的除外）
PER_ACCOUNT_KINDS = ("e", "q", "r", "p")

# 自定义页面 key → URL 路径（用于侧栏高亮）
NAV_PATH = {"reimb_my": "/reimb/my", "reimb_approve": "/reimb/approve",
            "attend_approve": "/attend/approve", "payroll": "/payroll",
            "settings": "/settings", "profit": "/profit",
            "worklog_sheet": "/worklog/sheet",
            "backup": "/backup"}

MIME_XLSX = ("application/vnd.openxmlformats-officedocument"
             ".spreadsheetml.sheet")
MIME_DOCX = ("application/vnd.openxmlformats-officedocument"
             ".wordprocessingml.document")

app = Flask(__name__)
app.config.update(
    MAX_CONTENT_LENGTH=S.MAX_UPLOAD,
    SECRET_KEY=os.environ.get("SECRET_KEY", "contract-app-dev-secret"),
    SESSION_COOKIE_NAME="contract_sid",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(minutes=SESSION_TIMEOUT_MIN),
)
app.teardown_appcontext(close_db)
app.jinja_env.globals.update(money=S.money, percent=S.percent, human_size=S.human_size)


def _list_url(key, q="", page=None, per=None, sort="", sort_dir="asc"):
    """列表页链接：把搜索词、页码、每页条数、排序一起带上。

    表头排序、翻页、改搜索词都走这里，任何一项操作都不会把别的条件弄丢。
    """
    args = {}
    q = (q or "").strip()
    if q:
        args["q"] = q
    if page and int(page) > 1:
        args["page"] = int(page)
    if per and int(per) != 25:
        args["per"] = int(per)
    sort = (sort or "").strip()
    if sort:
        args["sort"] = sort
        args["dir"] = "desc" if str(sort_dir or "").lower() == "desc" else "asc"
    return url_for("entity_list", key=key, **args)


app.jinja_env.globals["list_url"] = _list_url


def _ledger_url(endpoint, key, q="", page=None, per=None):
    """台账 / 报表页的链接：带上筛选词、页码与每页行数。

    与 list_url 分开是有意的：那边的端点固定是 entity_list、默认每页 25 行，
    台账这边默认每页 200 行（大表不分页会在浏览器里卡几十秒）。
    """
    args = {"key": key}
    q = (q or "").strip()
    if q:
        args["q"] = q
    try:
        if page and int(page) > 1:
            args["page"] = int(page)
        if per and int(per) != S.PAGE_PER_DEFAULT:
            args["per"] = int(per)
    except (TypeError, ValueError):
        pass
    return url_for(endpoint, **args)


app.jinja_env.globals["ledger_url"] = _ledger_url
# 明细里的百分数列（如扣税比例）：入库 13% 的文本 → 输入框里显示 13
app.jinja_env.globals["pct_num"] = S.pct_num
app.jinja_env.globals.update(ROLE_LABELS=S.ROLE_LABELS, ROLE_DESC=S.ROLE_DESC)
# 模板里用它决定要不要显示「新增 / 编辑 / 保存」按钮
app.jinja_env.globals["can_write"] = lambda kind, key: _item_editable(kind, key)


def _badge_class(v):
    """按状态值给徽章配色（审批状态、核销状态等共用）。"""
    v = str(v or "")
    if v in ("已批准", "已结清", "已核销完", "已打款", "已完成", "已抵扣",
             "已认证", "已支付", "已签订", "已竣工", "已结算"):
        return "tag-ok"
    if v in ("已驳回", "已作废", "已终止", "已停工", "已收讫"):
        return "tag-danger"
    if v in ("待审批", "部分核销", "部分收付", "部分支付", "未定", "未支付"):
        return "tag-warn"
    return ""


app.jinja_env.filters["badge_class"] = _badge_class


def _numfmt(v):
    """数字输入框的默认值：去掉无意义的小数与尾零。"""
    if v in (None, ""):
        return ""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return v
    if f == int(f):
        return str(int(f))
    return ("%.6f" % f).rstrip("0").rstrip(".")


app.jinja_env.filters["num"] = _numfmt


# ------------------------------------------------------------------ 权限
def cur_role():
    return session.get("role", "")


def cur_uid():
    return session.get("uid")


def is_admin():
    return cur_role() == S.ROLE_ADMIN


def is_finance():
    return cur_role() == S.ROLE_FINANCE


def is_approver():
    """可审批/打款/看工资测算：管理员 + 财务。"""
    return cur_role() in S.ROLE_APPROVERS


def is_employee():
    return cur_role() == S.ROLE_EMPLOYEE


def _role_ok(roles):
    """roles 为 None / 空 表示不限角色。"""
    return (not roles) or (cur_role() in roles)


def page_allowed(spec):
    roles = spec.get("roles") or []
    return (not roles) or (cur_role() in roles)


def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not session.get("uid"):
            return redirect(url_for("login", next=request.path))
        return fn(*a, **kw)
    return wrapper


def _gate(fn, ok, msg):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not session.get("uid"):
            return redirect(url_for("login", next=request.path))
        if not ok():
            flash(msg, "error")
            return redirect(url_for("index"))
        return fn(*a, **kw)
    return wrapper


def admin_required(fn):
    return _gate(fn, is_admin, "该功能仅管理员可用")


def approver_required(fn):
    return _gate(fn, is_approver, "该功能仅管理员或财务可用")


def finance_required(fn):
    """打款类动作归财务：管理员可查看，但不经手资金操作。"""
    return _gate(fn, is_finance, "打款操作仅财务可用")


def page_required(page_key, write=False):
    """按「页面可见性」限制功能页。

    与左侧菜单用同一套判断（_item_visible）：角色默认 + 用户管理里的按人开关，
    这样管理员在「用户管理 → 页面权限」里放开/收起，直接敲地址也会跟着生效。
    write=True 用于会改数据的动作（保存工资、记工、备份…）：只读账号一律拦掉。
    """
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            if not session.get("uid"):
                return redirect(url_for("login", next=request.path))
            if not _item_visible("p", page_key):
                abort(403)
            if write and not _item_editable("p", page_key):
                abort(403)
            return fn(*a, **kw)
        return wrapper
    return deco


def scope_of(entity_key):
    """当前用户在某个实体上的数据范围（None = 不限制）。"""
    return S.scope_where(entity_key, cur_uid(), cur_role())


PUBLIC_ENDPOINTS = {"login", "static", "healthz"}


@app.before_request
def guard():
    """会话校验：IP 白名单 + 强制 HTTPS + 账号有效性 + 空闲超时 + 强制改密。"""
    if request.endpoint == "static" or request.endpoint is None:
        return None
    # 每个会话一个 CSRF token（表单与 AJAX 都要带）
    if not session.get("_csrf"):
        session["_csrf"] = secrets.token_urlsafe(32)

    ip = S.client_ip(request)
    if not S.ip_allowed(ip, ALLOW_IP_CIDRS):
        return render_template("error.html", code=403,
                               msg="你的 IP 不在允许访问的范围内，请联系管理员"), 403

    if FORCE_HTTPS and not request.is_secure:
        # 只在反代「明确告诉我们原始请求是 http」时才跳转。
        # 如果没传这个头就不动（否则没有反代或反代漏配时会跳到一个不存在的
        # https 端口，甚至把自己锁在无限重定向里）。
        proto = (request.headers.get("X-Forwarded-Proto") or "").strip().lower()
        if proto == "http":
            host = request.headers.get("X-Forwarded-Host") or request.host
            return redirect(f"https://{host}{request.full_path}", code=301)

    if request.endpoint in PUBLIC_ENDPOINTS:
        return None
    uid = session.get("uid")
    if not uid:
        return None

    row = S.user_get(uid)
    if not row or not row["enabled"]:
        session.clear()
        flash("账号已停用或不存在，请重新登录", "error")
        return redirect(url_for("login"))

    session["uname"] = row["username"]
    session["dname"] = row["display_name"] or row["username"]
    session["role"] = row["role"]

    now = time.time()
    last = session.get("ts")
    if last and (now - last) > SESSION_TIMEOUT_MIN * 60:
        session.clear()
        flash(f"已超过 {SESSION_TIMEOUT_MIN} 分钟未操作，请重新登录", "error")
        return redirect(url_for("login"))
    session["ts"] = now

    if row["must_change_pwd"] and request.endpoint not in ("password", "logout"):
        return redirect(url_for("password", forced=1))
    return None


@app.before_request
def csrf_guard():
    """所有写请求必须带本次会话的 CSRF token（表单 _csrf 或 X-CSRF-Token 头）。

    以前全靠 SESSION_COOKIE_SAMESITE=Lax 挡跨站 POST ——SameSite 只是浏览器约定，
    老浏览器 / 非浏览器客户端都能绕。现在服务端硬校验一遍。
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    if request.endpoint in ("static", "healthz"):
        return None
    tok = session.get("_csrf") or ""
    sent = (request.form.get("_csrf")
            or request.headers.get("X-CSRF-Token") or "")
    if not tok or not sent or not secrets.compare_digest(tok, sent):
        return render_template(
            "error.html", code=400,
            msg="页面已过期（安全校验没通过），请刷新页面后重新提交"), 400
    return None


@app.after_request
def harden(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    resp.headers.setdefault("X-XSS-Protection", "0")
    # 页面（HTML）不缓存：手机上最容易出的事就是"看到的还是上次那个旧页面"
    # —— 数据是旧的、样式是旧的，看着像系统坏了。静态资源走 ?v=版本号，
    # 不走这里。
    if resp.mimetype == "text/html":
        resp.headers["Cache-Control"] = "no-store, must-revalidate"
        resp.headers.setdefault("Pragma", "no-cache")
        resp.headers.setdefault("Expires", "0")
        # 所有 method="post" 的表单自动塞一枚 hidden token ——
        # 模板有 40 多个，逐个加容易漏，这里统一注入。
        tok = session.get("_csrf")
        if tok and request.method in ("GET", "HEAD"):
            html = resp.get_data(as_text=True)
            if "_csrf" not in html and re.search(r"<form\b[^>]*method=[\"']post",
                                                 html, re.I):
                html = re.sub(
                    r'(<form\b[^>]*\bmethod=["\']post["\'][^>]*>)',
                    lambda m: m.group(1) + '<input type="hidden" name="_csrf" value="%s">' % tok,
                    html, flags=re.I)
                resp.set_data(html)
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; "
        "base-uri 'self'")
    if os.environ.get("SESSION_COOKIE_SECURE", "") == "1" or FORCE_HTTPS:
        resp.headers.setdefault("Strict-Transport-Security",
                                "max-age=31536000; includeSubDomains")
    return resp


def _asset_ver(name):
    """静态资源的版本号（取文件的修改时间）。

    页面里引用成 /static/app.css?v=1791640777 —— 文件一改，URL 就变，
    手机上的旧缓存自然失效（只靠 Cache-Control: no-cache 有的浏览器不当回事）。
    """
    try:
        p = os.path.join(app.static_folder, name)
        return str(int(os.path.getmtime(p)))
    except OSError:
        return "1"


app.jinja_env.globals["asset_ver"] = _asset_ver
app.jinja_env.globals["csrf_token"] = lambda: session.get("_csrf", "")
app.jinja_env.globals["max_upload_mb"] = S.MAX_UPLOAD // 1024 // 1024


# ------------------------------------------------------------------ 登录
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        ip = S.client_ip(request)
        user, err = S.login_attempt(request.form.get("username"),
                                    request.form.get("password"), ip)
        if err:
            flash(err, "error")
        else:
            session.clear()
            session["uid"] = user["id"]
            session["uname"] = user["username"]
            session["dname"] = user["display_name"] or user["username"]
            session["role"] = user["role"]
            session["ts"] = time.time()
            session.permanent = True
            if user["must_change_pwd"]:
                flash("首次登录（或密码已被管理员重置），请先设置你自己的密码", "ok")
                return redirect(url_for("password", forced=1))
            nxt = (request.args.get("next") or "").strip()
            # 只认站内绝对路径：//evil.com 是协议相对地址，浏览器会当成外站
            if not (nxt.startswith("/") and not nxt.startswith("//")
                    and "\\" not in nxt):
                nxt = url_for("index")
            return redirect(nxt)
    return render_template("login.html", app_title=APP_TITLE,
                           company_name=COMPANY_NAME,
                           max_fails=S.MAX_FAILS, lock_min=S.LOCK_MIN,
                           is_admin_env=S.enabled_admin_exists())


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/password", methods=["GET", "POST"])
def password():
    """修改自己的密码（被管理员要求改密时所有页面都会被挡到这里）。"""
    if not session.get("uid"):
        return redirect(url_for("login"))
    row = S.user_get(session["uid"])
    if not row:
        session.clear()
        return redirect(url_for("login"))
    forced = request.args.get("forced") == "1" or bool(row["must_change_pwd"])

    if request.method == "POST":
        from werkzeug.security import check_password_hash
        old = request.form.get("old_password") or ""
        new1 = request.form.get("new_password") or ""
        new2 = request.form.get("new_password2") or ""
        errs = []
        if not check_password_hash(row["password_hash"], old):
            errs.append("原密码不正确")
        if new1 != new2:
            errs.append("两次输入的新密码不一致")
        if new1 and new1 == old:
            errs.append("新密码不能与原密码相同")
        if not errs:
            try:
                S.user_set_password(row["id"], new1, must_change=0)
            except ValueError as e:
                errs.append(str(e))
        if errs:
            for e in errs:
                flash(e, "error")
        else:
            flash("密码已更新，请牢记新密码", "ok")
            return redirect(url_for("index"))
    return render_template("password.html", forced=forced, app_title=APP_TITLE,
                           company_name=COMPANY_NAME, pwd_min=S.PWD_MIN_LEN)


def _page_is_read_only():
    """当前页面是不是"只读"（按人放开、但角色本来无权的那种）。"""
    item = _current_item()
    if not item:
        return False
    return not _item_editable(item[0], item[1])


@app.context_processor
def inject_nav():
    pend = 0
    if is_approver():
        try:
            pend = S.scalar("SELECT COUNT(*) FROM costs "
                            "WHERE IFNULL(approval_status,'') = '待审批'")
        except Exception:
            pend = 0
    return {"app_title": APP_TITLE, "app_subtitle": APP_SUBTITLE,
            "company_name": COMPANY_NAME,
            "app_version": APP_VERSION, "release_date": RELEASE_DATE,
            # 前端自动计算要用到的参数（每日工时等），改「参数设置」后即时生效
            "p_params": S.payroll_params(),
            "nav_groups": build_nav(), "cur_user": session.get("dname", ""),
            "read_only_page": _page_is_read_only(),
            "cur_role": session.get("role", ""), "is_admin": is_admin(),
            "is_finance": is_finance(), "is_approver": is_approver(),
            "is_employee": is_employee(),
            "pending_count": pend,
            "PAGES": meta.PAGES, "NAV_PATH": NAV_PATH}


def _page_overrides():
    """当前账号在「用户管理 → 页面权限」里配的覆盖（每个请求只查一次）。

    返回 {(kind, key): 0/1/2}；空字典表示完全跟随角色默认。
    """
    ov = getattr(g, "_page_overrides", None)
    if ov is None:
        try:
            ov = S.user_page_overrides(session.get("uid"))
        except Exception:                    # 未登录 / 老库还没有这张表
            ov = {}
        g._page_overrides = ov
    return ov


def _override_applies_role(kind, key, role):
    """某个页面项能不能通过「页面权限」按人开关。

    管理员专用功能（用户管理、登录日志、参数设置、数据备份）以及
    员工本来就无权维护的字典 / 费用类别，不参与配置 ——
    放开了菜单也进不去，不如干脆不给这个选项。
    """
    if kind not in PER_ACCOUNT_KINDS:
        return False
    if kind == "e" and key in MANAGER_ONLY_ENTITIES and role not in S.ROLE_APPROVERS:
        return False
    if kind == "p":
        roles = (meta.PAGES.get(key) or {}).get("roles") or []
        if roles and set(roles) <= {S.ROLE_ADMIN}:
            return False
    return True


def _override_applies(kind, key):
    return _override_applies_role(kind, key, cur_role())


def _default_item_visible(kind, key, role):
    """按角色算某一项的默认可见性（与 _item_visible 的角色部分保持一致）。"""
    roles = meta.item_roles(kind, key)
    if roles and role not in roles:
        return False
    if kind == "e":
        return not (key in MANAGER_ONLY_ENTITIES and role not in S.ROLE_APPROVERS)
    if kind in ("q", "r"):
        return True
    if kind == "p":
        spec = meta.PAGES.get(key)
        return bool(spec) and (not spec.get("roles") or role in spec["roles"])
    if kind == "u":
        return role == S.ROLE_ADMIN
    return False


def _item_visible(kind, key):
    """某个页面项（菜单项 / 选项卡项）对当前用户是否可见。

    优先级：按人配置（管理员自己不受限制，免得把管理员锁在门外）
    → 单项角色（meta.ITEM_ROLES）→ 分组角色（meta.GROUP_ROLES）→ 内置规则。
    隐藏入口只是一半 —— 直接敲地址的也在路由里用 _require_item 拦掉。
    """
    if not is_admin() and _override_applies(kind, key):
        lvl = _page_overrides().get((kind, key))
        if lvl is not None:
            return lvl > S.PAGE_LVL_HIDE
    return _default_item_visible(kind, key, cur_role())


def _item_editable(kind, key):
    """当前用户对某个页面项有没有"写"权限。

    管理员 / 财务照旧（永远可写）。
    员工：页面权限里配成「可编辑」才可写；配成「只读」或「隐藏」一律不可写；
    没配置时按角色默认（角色本来能进的页面，本来就能写）。
    """
    if is_approver():
        return True
    if _override_applies(kind, key):
        lvl = _page_overrides().get((kind, key))
        if lvl is not None:
            return lvl >= S.PAGE_LVL_WRITE
    return True


def _require_item(kind, key, write=False):
    """越权直接访问页面 → 403（而不是只把菜单藏起来）。

    write=True 用于新增/修改/删除/上传这类会改数据的路由：
    只读账号（按人放开页面、但角色本来无权的那种）一律拦掉。
    """
    if not _item_visible(kind, key):
        abort(403)
    if write and not _item_editable(kind, key):
        abort(403)


def _item_url(kind, key):
    """选项卡项对应的 URL。"""
    if kind == "e":
        return url_for("entity_list", key=key)
    if kind == "q":
        return url_for("query_view", key=key)
    if kind == "p":
        spec = meta.PAGES.get(key)
        return url_for(spec["endpoint"]) if spec else None
    return None


def _current_item():
    """当前请求对应哪个页面项 (kind, key)；找不到返回 None。"""
    ep = request.endpoint
    args = request.view_args or {}
    if ep in ("entity_list", "entity_new", "entity_edit"):
        return ("e", args.get("key"))
    if ep == "query_view":
        return ("q", args.get("key"))
    if ep == "report_view":
        return ("r", args.get("key"))
    for k, spec in meta.PAGES.items():
        if spec["endpoint"] == ep:
            return ("p", k)
    return None


def _current_group():
    """当前页面属于哪个选项卡组（不属于任何组则返回 None）。"""
    ep = request.endpoint
    args = request.view_args or {}
    if ep == "entity_list":
        return meta.GROUP_OF_ITEM.get(("e", args.get("key")))
    if ep == "query_view":
        return meta.GROUP_OF_ITEM.get(("q", args.get("key")))
    return meta.GROUP_OF_PAGE.get(ep)


def build_nav():
    """导航分组：[(组名, [(kind, key, 标题), ...]), ...]

    kind: e 实体 / q 查询 / p 功能页 / u 用户管理 / t 选项卡组
    选项卡组在左侧只占一行，组内的项在页面顶部以选项卡呈现。
    查询与报表不再单独列菜单 —— 它们挂在对应选项卡组里，
    打印与导出按钮直接放在台账页上。
    """
    groups = []
    seen_pages = set()
    for gname, items in meta.MENU:
        entries = []
        for kind, k in items:
            if kind == "t":
                # 选项卡组：组内一项都看不到，整组就不出现在菜单里
                spec = meta.TAB_GROUPS.get(k)
                if not spec:
                    continue
                if not any(_item_visible(k2, key2)
                           for k2, key2, _l in spec["items"]):
                    continue
                entries.append(("t", k, spec["title"]))
                continue
            if not _item_visible(kind, k):
                continue
            if kind == "e":
                entries.append(("e", k, meta.ENTITIES[k]["title"]))
            elif kind == "p":
                if k in seen_pages:
                    continue
                seen_pages.add(k)
                entries.append(("p", k, meta.PAGES[k]["title"]))
            elif kind == "u":
                entries.append(("u", k, "用户管理" if k == "users" else "登录日志"))
        if entries:
            groups.append((gname, entries))
    return groups


@app.context_processor
def inject_tabs():
    """页面顶部的选项卡条：同一个分组内的功能平铺成一排。"""
    gkey = _current_group()
    if not gkey:
        return {"tab_bar": None}
    g = meta.TAB_GROUPS[gkey]
    args = request.view_args or {}
    items = []
    for kind, key, label in g["items"]:
        if not _item_visible(kind, key):
            continue
        if kind == "p":
            active = request.endpoint == meta.PAGES[key]["endpoint"]
        else:
            active = (args.get("key") == key
                      and request.endpoint in ("entity_list", "query_view"))
        items.append({"label": label, "url": _item_url(kind, key),
                      "active": active})
    # 注意：键名不要用 items / keys / values / get —— Jinja 里会解析成
    # dict 的内置方法而不是我们的键（这里踩过一次）。
    return {"tab_bar": {"key": gkey, "title": g["title"], "tabs": items}}


@app.route("/tabs/<group>")
@login_required
def tab_group(group):
    """左侧菜单的选项卡组入口：跳到组内第一个当前角色可见的功能。"""
    g = meta.TAB_GROUPS.get(group)
    if not g:
        abort(404)
    if not any(_item_visible(kind, key) for kind, key, _l in g["items"]):
        abort(403)                       # 该分组对当前角色不可见
    for kind, key, _label in g["items"]:
        if not _item_visible(kind, key):
            continue
        url = _item_url(kind, key)
        if url:
            return redirect(url)
    flash("这个分组下没有你能访问的内容", "error")
    return redirect(url_for("index"))


@app.route("/q/<key>/print")
@login_required
def query_print(key):
    """台账页直接打印（浏览器打印 / 另存为 PDF）。

    不再需要单独的「报表打印」菜单：看到哪张台账，就地打印。
    """
    _require_item("q", key)
    try:
        q, rows = S.run_query(key, request.args.get("q", ""),
                              scope=_query_scope(key))
    except KeyError:
        abort(404)
    cols = list(rows[0].keys()) if rows else []
    rep = {"key": key, "title": q["title"], "query": key,
           "landscape": len(cols) > 7}      # 列多就自动横向，避免打印被裁
    return render_template("report.html", rep=rep, rows=rows, cols=cols,
                           printed_at=now_str())


# ------------------------------------------------------------------ 首页
@app.route("/")
@login_required
def index():
    uid = cur_uid()
    # 员工只看自己负责 / 被授权的项目；管理员与财务看全公司。
    # 概览卡片与"按项目"明细表用同一个范围，两者合计严格相等。
    pids = S.accessible_project_ids(uid) if is_employee() else None
    return render_template("dashboard.html",
                           d=S.dashboard(pids, uid if is_employee() else None),
                           reimb=S.reimb_summary(uid),
                           # 弹窗里的「最近报销记录」只列还在流程里的单子：
                           # 已批准（等打款看上面卡片）和无需报销（本来就不用报）
                           # 都已经办结，再列一遍只会分散注意力。
                           reimb_rows=S.reimb_detail(
                               uid, 8, exclude_status=("已批准", "无需报销")),
                           adv=S.advance_rows_of(uid, 8))


# ------------------------------------------------------------------ 实体 CRUD
def _entity(key):
    ent = meta.ENTITIES.get(key)
    if not ent:
        abort(404)
    if key in MANAGER_ONLY_ENTITIES and not is_approver():
        abort(403)                       # 字典、费用类别：员工/无权角色直接拒绝
    return ent


def build_fields(key, row=None, is_new=None):
    """生成表单字段列表。

    几处特殊处理：
      · 自动编号字段（项目编号 / 合同编号 / 各单据号）：表单里不出现，
        由系统自动生成；列表页照样显示。
      · 审批字段（审批状态 / 审批意见）：新建申请时不出现；申请人编辑时
        只读展示审批结果；审批人（管理员/财务）可编辑。
      · mode="readonly"：始终只读展示（如预支状态，由核销情况自动维护）。
    """
    ent = meta.ENTITIES[key]
    if is_new is None:
        is_new = row is None
    auto_code = (ent.get("code") or (None,))[0]
    flds = list(ent["fields"])
    # 用户要求：先选项目、再选跟这个项目相关的合同。
    # 表单顺序上把「所属项目」提到「关联合同」前面（发票 / 付款 / 送货都适用）。
    names = [x["name"] for x in flds]
    if "project_id" in names and "contract_id" in names:
        i_p, i_c = names.index("project_id"), names.index("contract_id")
        if i_p > i_c:
            flds[i_p], flds[i_c] = flds[i_c], flds[i_p]
    _has_project = "project_id" in names
    out = []
    for fld in flds:
        name = fld["name"]
        if name == auto_code or fld.get("mode") == "hidden":
            # 自动编号与 mode="hidden" 的字段都不进表单：
            # 编号由系统生成；隐藏字段（如出差补贴）由服务端自动算好存库。
            continue
        if name == "employee_id" and is_employee():
            # 员工只能给自己报单：归属人直接带出当前账号，不给选
            item = dict(fld)
            item["mode"] = "readonly"
            item["type"] = "text"          # 按文本展示姓名，不套状态徽章
            item["value"] = session.get("dname") or ""
            item["hint"] = "由当前登录账号自动带出，不能替他人申请"
            out.append(item)
            continue
        mode = fld.get("mode") or ""
        if fld.get("approval") or fld.get("approver_edit") or fld.get("finance_edit"):
            # 由特定环节填写的字段：新建时不出现（服务端给默认值），
            # 编辑时只有对应角色能改，其他人只读。
            #   approval     → 审批人（管理员 / 财务）可改
            #   approver_edit→ 同上，语义上不是"审批"而是"由审批环节维护"
            #   finance_edit → 仅财务可改（如预支单状态：打款归财务）
            if is_new:
                continue
            if fld.get("finance_edit"):
                mode = "" if is_finance() else "readonly"
            else:
                mode = "" if is_approver() else "readonly"
        val = None
        if row is not None:
            try:
                val = row[name]
            except (KeyError, IndexError, TypeError):
                val = None
        item = dict(fld)
        item["mode"] = mode
        if fld.get("approval") and mode == "readonly":
            item["hint"] = ("由财务 / 管理员审批后自动更新，申请人不能自行修改；"
                            "审批意见也由审批人填写")
        item["value"] = "" if val is None else val
        if fld["type"] in ("money", "rate", "number") and is_new and val is None:
            item["value"] = fld.get("default", "")
        if fld.get("options_sql"):
            item["options"] = lookup_options(fld["options_sql"])
            # options_by：下选项按「另一个字段的当前值」收窄（如发票关联合同跟着发票方向）。
            # 基础 options 给的是并集（保证旧值还能显示），每个分支给出允许的 id 集合，
            # 前端按集合过滤，服务端再兜一道校验。
            ob = fld.get("options_by")
            if ob:
                item["options_by"] = {
                    "field": ob["field"],
                    "map": {k: [str(v) for v, _ in lookup_options(sql)]
                            for k, sql in (ob.get("map") or {}).items()},
                }
            if _has_project and fld["name"] == "contract_id":
                # 合同 → 项目 的映射：前端拿它把合同下拉收窄到「当前项目」的那些合同
                cmap = {str(r["id"]): r.get("project_id")
                        for r in query("SELECT id, project_id FROM contracts")}
                cfg = item.get("options_by") or {"field": "", "map": {}}
                cfg["project_field"] = "project_id"
                cfg["project_map"] = cmap
                item["options_by"] = cfg
                # 提示：先选项目，合同只列这个项目的
                _h = (item.get("hint") or "").strip()
                _extra = ("先选【所属项目】，这里只列这个项目的合同"
                          "（发票还会按【发票方向】再收窄一次）。")
                item["hint"] = (_h + " " if _h else "") + _extra
        elif fld.get("dict_cat"):
            item["options"] = [(v, v) for v in dict_options(fld["dict_cat"])]
        elif fld.get("options"):
            item["options"] = [(v, v) for v in fld["options"]]
        # 兜底：当前值不在选项里（字典被改过名/收敛过）时把它补进去，
        # 否则下拉会默认选第一项，一保存就把原值悄悄改掉了
        if item.get("options") is not None and val not in (None, ""):
            vals = [str(o[0]) for o in item["options"]]
            if str(val) not in vals:
                item["options"] = list(item["options"]) + [(val, "%s（旧值）" % val)]
        out.append(item)
    return out


def _ids_of(v):
    """把 "3,5" / ["3","5"] / None 统一成 int 列表（勾选面板与查询串都用它）。"""
    if v is None:
        return []
    if isinstance(v, str):
        parts = v.replace(";", ",").split(",")
    else:
        parts = list(v)
    out = []
    for x in parts:
        t = str(x).strip()
        if t.isdigit() and int(t) not in out:
            out.append(int(t))
    return out


def _sync_invoice_cost(rid):
    """发票保存后同步「对应的费用支出」（只对进项发票）。

    类别 / 对应清单项是这张发票页上的两个额外控件（不是发票表字段），
    在这里读出来传给服务层 —— 免得为两个可调的属性再给发票表加列。
    """
    cat = (request.form.get("cost_category") or "").strip() or None
    item_id = request.form.get("cost_item_id", type=int)
    raw = (request.form.get("_item_names") or "").strip()
    names = [x.strip() for x in re.split(r"[、,\n]", raw) if x.strip()] if raw else []
    cost, action = S.invoice_cost_sync(rid, category=cat, item_id=item_id,
                                       item_names=names)
    if action == "created":
        flash("已在「费用支出」自动生成费用 %s（%s，%s）—— 类别与对应清单项"
              "在本页下方可调" % (cost["code"], cost["category"],
                                  S.money(cost["amount"])), "ok")
    elif action == "deleted":
        flash("发票已作废或不是进项，之前自动生成的那条费用已删除", "ok")
    return cost


def _save_contract_links(rid):
    """保存对私计算的「关联合同」多选。

    明细里用到的合同会自动补进列表 —— 否则表头在说谎，费用参考面板会漏算。
    补了就在页面上说一句，不悄悄改用户勾选。
    """
    _, added = S.save_settlement_contracts(rid, request.form.getlist("contract_ids"))
    if added:
        flash("明细里用到的合同 %s 不在「关联合同」里，已自动补上"
              % "、".join(added), "ok")


def _back_url(default=None):
    """「改完回哪一页」—— 列表页把当时的页码 / 搜索词 / 排序一起带过来。

    只认站内绝对路径（/ 开头、不是 //），别的一律退回 default，
    免得被塞一个外部地址做跳转。
    """
    v = (request.form.get("back") or request.args.get("back") or "").strip()
    # 表单里的 & 会被 HTML 转义成 &amp;，这里还原一下再判断
    v = v.replace("&amp;", "&")
    if v.startswith("/") and not v.startswith("//") and "\\" not in v:
        return v
    return default


def _render_form(key, ent, row, is_new, settlement_rows=None, detail_rows=None,
                 sel_contract_ids=None, imp=None):
    """表单页统一上下文：主体字段 + 明细行 + 各种下拉数据。

    （原来 6 处 render_template("form.html", ...) 各写一遍，加明细后太容易漏项。）
    """
    rid = None if is_new else (row or {}).get("id")
    # 先把明细列的字典选项解析出来再渲染：带明细预填（detail_rows 非空）时
    # 不会走 details_of()，以前就漏了这一步 → 下拉是空的
    S.detail_specs(key)
    ctx = {
        "key": key, "ent": ent, "row": row, "is_new": is_new,
        # 返回列表回到「原来那一页」（页码 / 搜索 / 排序都在这个 URL 里）
        "back": _back_url() or url_for("entity_list", key=key),
        "fields": build_fields(key, row, is_new=is_new),
        # 明细（报价明细 / 合同清单 / 付款・费用清单项 / 送货明细）
        "detail_rows": (detail_rows if detail_rows is not None
                        else (S.details_of(key, rid) if rid else {})),
        "brands_json": S.brand_list(),
        "models_json": S.model_list(),
        "units_json": S.unit_names(),
        "contract_items_json": S.contract_item_list(),
        # 付款 ↔ 发票核销
        "settlements_json": S.invoice_pick_list(),
        "settlement_rows": (settlement_rows if settlement_rows is not None
                            else (S.settlement_rows(rid)
                                  if rid and ent.get("settlements") else [])),
        "attachments": (S.attachments_for(key, rid)
                        if rid and ent.get("attachments") else []),
        "payments": (S.payment_settled(rid)
                     if rid and ent.get("settlements") else 0),
        "invoice_stats": (_invoice_stats(rid) if rid and key == "invoices" else None),
        # 发票 PDF 拖拽导入：确认页要的进度 / 告警 / 票面明细（不是导入就没有）
        "imp": imp,
        # 发票 → 自动生成的费用支出（类别 / 对应合同清单项可在这页调）
        "invoice_cost": (S.invoice_cost_of(rid)
                         if rid and key == "invoices" else None),
        # (id, name) 列表：value 用 id（与 costs.category 存的口径一致）
        "cost_cats": (S.cost_category_pairs()
                      if key == "invoices" and not is_new else []),
        "cost_items_pick": (S.contract_items_of((row or {}).get("contract_id"))
                            if key == "invoices" and not is_new else []),
    }
    if key == "private_settlements":
        row = row or {}
        if sel_contract_ids is None:
            if hasattr(row, "getlist"):          # 校验失败回显：保留用户勾的
                sel_contract_ids = _ids_of(row.getlist("contract_ids"))
            elif rid:
                sel_contract_ids = S.settlement_contract_ids(rid)
            if not sel_contract_ids and row.get("contract_id"):
                # 老数据只留了单张合同 / 从合同页点进来带的
                sel_contract_ids = _ids_of([row.get("contract_id")])
        ctx["sel_contract_ids"] = sel_contract_ids or []
        ctx["cost_ref"] = S.private_cost_reference(ctx["sel_contract_ids"],
                                                   row.get("project_id"))
        ctx["contract_pick"] = lookup_options(meta.SQL_CONTRACT)
        # 清单项下拉按合同分组：一个单子可能关联多张合同
        ctx["citem_groups"] = S.contract_item_groups()
    if key == "contracts" and rid:
        ctx["contract_deliveries"] = S.deliveries_of_contract(rid)
    if key == "quotations" and rid:
        ctx["quote_contract"] = (S.one("SELECT id, code, name FROM contracts "
                                       "WHERE id = ?", (row.get("contract_id"),))
                                 if row.get("contract_id") else None)
    # 明细里「对应合同清单项」的可选范围跟着主表的合同走
    ctx["master_contract"] = (row or {}).get("contract_id") if row else None
    return render_template("form.html", **ctx)


@app.route("/e/<key>")
@login_required
def entity_list(key):
    _require_item("e", key)
    ent = _entity(key)
    if ent is None:
        return redirect(url_for("index"))
    q = request.args.get("q", "")
    page = request.args.get("page", 1, type=int)
    # 每页条数跟服务层的钳制保持一致（5~200），否则页面上的 per 和实际不符
    per = max(5, min(200, request.args.get("per", 25, type=int) or 25))
    # 表头排序：sort 传列名，dir 传 asc/desc（服务层按白名单校验）
    sort = (request.args.get("sort") or "").strip()
    sort_dir = "desc" if (request.args.get("dir") or "").lower() == "desc" else "asc"
    rows, total, pages, page = S.list_rows(key, q, page, per, scope=scope_of(key),
                                           sort=sort, sort_dir=sort_dir)
    sortable = S.sort_columns(key)
    if sort not in sortable:          # 手改 URL 传了不能排的列 → 当作没排序
        sort, sort_dir = "", "asc"
    return render_template("list.html", key=key, ent=ent, rows=rows, q=q,
                           total=total, pages=pages, page=page, per=per,
                           scoped=bool(scope_of(key)),
                           sort=sort, sort_dir=sort_dir, sortable=sortable,
                           inline_opts=_inline_opts(ent))


def _inline_opts(ent):
    """列表里可直接改的字段 → 可选值（如报价状态）。"""
    out = {}
    for c in ent.get("list") or []:
        if c.get("type") == "inline":
            out[c["name"]] = (dict_options(c["dict_cat"])
                              if c.get("dict_cat") else [])
    return out


@app.route("/e/<key>/new", methods=["GET", "POST"])
@login_required
def entity_new(key):
    _require_item("e", key, write=True)
    ent = _entity(key)
    if ent is None:
        return redirect(url_for("index"))
    if request.method == "POST":
        data = S.form_to_data(key, request.form)
        errs = _prepare_data(key, data, is_new=True)
        errs += S.validate(key, data)
        if key == "payments":
            st_rows, serrs = S.parse_settlements(request.form)
            errs += serrs
            amt = S.round2(data.get("amount"))
            tot = S.round2(sum(r["amount"] for r in st_rows))
            if amt and tot > amt + S.TOL:
                errs.append(f"核销合计 {S.money(tot)} 超过付款金额 {S.money(amt)}")
            if tot and not amt:
                errs.append("付款金额为 0，却填了核销金额；请先填付款金额")
        else:
            st_rows = None
        d_rows = S.parse_details(key, request.form)
        errs += S.detail_error(key, d_rows, data)
        if key == "quotations":
            tot = S.detail_sum(key, d_rows.get("items") or [])
            if S._to_num(data.get("discount")) > tot + S.TOL:
                errs.append("【优惠让利】%s 超过了明细合计 %s"
                            % (S.money(data.get("discount")), S.money(tot)))
        if errs:
            for e in errs:
                flash(e, "error")
            return _render_form(key, ent, request.form, True,
                                settlement_rows=_form_settlement_rows(),
                                detail_rows=d_rows)
        rid, save_err = _save_or_flash(key, data)
        if save_err:
            flash(save_err, "error")
            # 用提交上来的内容重绘（与校验失败分支一致），别把刚填的整张单丢掉
            return _render_form(key, ent, request.form, True,
                                settlement_rows=_form_settlement_rows(),
                                detail_rows=d_rows)
        if st_rows is not None:
            S.save_settlements(rid, st_rows)
        S.save_details(key, rid, d_rows)
        if key == "quotations":
            S.quotation_recalc(rid)
        elif key == "private_settlements":
            S.private_recalc(rid)
            _save_contract_links(rid)
        elif key == "invoices":
            _sync_invoice_cost(rid)
        if ent.get("attachments"):
            _save_new_files(key, rid)
        flash(f"已新增，{'编号 ' + str(data.get(ent['code'][0])) if ent.get('code') else '记录已保存'}", "ok")
        tok = (request.form.get("_imp") or "").strip()
        if tok and key == "invoices":
            return _imp_done(tok, request.form.get("_imp_i", 0, type=int), rid)
        return redirect(_back_url() or url_for("entity_list", key=key))
    new_row = _new_row(key) or {}      # _new_row 没有预填项时返回 None
    prefill = None
    pick_ids = None                    # 对私计算：勾选面板要预选哪几张合同
    imp = None                         # 发票 PDF 导入的确认页上下文
    if key == "private_settlements":
        # 从合同带出对私明细：
        #   单张（合同列表行按钮）  /e/private_settlements/new?contract_id=N
        #   多张（勾选面板的按钮）  /e/private_settlements/new?contract_ids=3,5
        cids = _ids_of(request.args.get("contract_ids"))
        one = request.args.get("contract_id", type=int)
        if one and one not in cids:
            cids.insert(0, one)
        if cids:
            c = S.contract_summary(cids[0])
            if c:
                new_row.update({"contract_id": cids[0],
                                "project_id": c.get("project_id"),
                                "unit_id": c.get("unit_id"), "status": "草稿"})
                prefill = {"items": S.private_prefill(cids)}
                pick_ids = cids
    if key == "invoices":
        # 拖入 PDF 后的逐张确认：/e/invoices/new?imp=<token>&i=0
        tok = (request.args.get("imp") or "").strip()
        if tok:
            idx = request.args.get("i", 0, type=int)
            items, it = _imp_item(tok, idx)
            if not it:
                flash("这批导入已经结束（或链接过期），请把 PDF 重新拖一次", "error")
                return redirect(url_for("entity_list", key="invoices"))
            pf = ((it.get("parsed") or {}).get("fields")) or {}
            new_row = dict(new_row or {})
            for k_ in ("no", "code", "type", "direction", "issue_date", "status"):
                if pf.get(k_):
                    new_row[k_] = pf[k_]
            for k_ in ("amount_gross", "tax_rate"):
                if pf.get(k_, "") != "" and pf.get(k_) is not None:
                    new_row[k_] = pf[k_]
            uid, conf = S.unit_id_by_name(pf.get("unit_name"))
            nu = request.args.get("new_unit", type=int)
            if nu:
                uid, conf = nu, "new"
            new_row["unit_id"] = uid
            imp = _imp_ctx(tok, idx, items)
            imp["unit_id"], imp["unit_conf"] = uid, conf

    if key == "deliveries":
        # 从合同生成送货单：/e/deliveries/new?contract_id=N
        cid = request.args.get("contract_id", type=int)
        if cid:
            c = S.contract_summary(cid)
            if c:
                new_row.update({"contract_id": cid, "project_id": c.get("project_id"),
                                "unit_id": c.get("unit_id"),
                                "status": "待发货"})
                prefill = {"items": [dict(r, **{"_new": 1})
                                     for r in S.delivery_prefill(cid)]}
    return _render_form(key, ent, new_row, True, detail_rows=prefill,
                        sel_contract_ids=pick_ids, imp=imp)


def _save_or_flash(key, data, row_id=None):
    """保存记录；约束冲突时给出可读提示，而不是抛 500。

    正常操作（从下拉框选择）不会触发，但并发删除关联记录后可能命中，
    这时应该告诉用户「重新选一下」，而不是给一页报错。
    """
    try:
        rid = S.save_entity(key, data, row_id=row_id)
        if key == "worklogs":
            # 金额 = 当日工资 × 工天。服务端算，不采信前端传来的数
            S.execute("UPDATE worklogs SET amount = ROUND(IFNULL(rate,0) * "
                      "IFNULL(days,1), 2) WHERE id = ?", (rid,))
        return rid, None
    except sqlite3.IntegrityError as e:
        msg = str(e)
        if "FOREIGN KEY" in msg:
            return None, "关联的记录（项目 / 合同 / 往来单位 / 人员）不存在或已被删除，请重新选择后再保存"
        if "UNIQUE" in msg:
            return None, "编号或名称与已有记录重复，请更换"
        return None, "保存失败：" + msg


def _new_row(key):
    """新增表单的预填值。

    审批状态与审批意见不在这里预填——它们由保存时的服务端规则决定
    （申请人提交一律为「待审批」，管理员/财务录入视为已批准）。
    """
    row = {}
    if key == "costs":
        row["employee_id"] = cur_uid()
        row["settle_type"] = "垫付报销"
        row["cost_date"] = datetime.now().strftime("%Y-%m-%d")
    elif key == "advances":
        row["pay_date"] = datetime.now().strftime("%Y-%m-%d")
        # 状态不预填：新建即为「待打款」，由财务实际打款后再点【确认打款】
        row["handler"] = session.get("dname", "")
    elif key in ("overtime", "leaves", "trips"):
        row["employee_id"] = cur_uid()
        if key == "trips":
            row["allowance_std"] = S.setting_num("trip_allowance_per_day", 40)
    elif key == "worklogs":
        row["work_date"] = datetime.now().strftime("%Y-%m-%d")
        row["days"] = "1"
    return row or None


# 单据上「由特定环节写入」的状态字段，申请人自己不填。
# 元组含义：(状态字段, 意见字段或 None, 审批人新建时的默认, 申请人/兜底默认)
APPROVAL_FIELDS = {
    "costs": ("approval_status", "approve_memo", "已批准", "待审批"),
    "overtime": ("status", "approve_memo", "已批准", "待审批"),
    "leaves": ("status", "approve_memo", "已批准", "待审批"),
    "trips": ("status", "approve_memo", "已批准", "待审批"),
    # 预支单是财务打款登记：新建时钱还没打，默认「待打款」，
    # 打款完成后由财务改成「已打款」（或点编辑页的【确认打款】）
    "advances": ("status", None, "待打款", "待打款"),
}
PENDING_STATUS = "待审批"
APPROVED_STATUS = "已批准"
PENDING_ADVANCE = "待打款"

# 这几张表的状态归财务维护（打款动作归财务，管理员只查看）
FINANCE_OWNED_STATUS = {"advances"}


def _apply_approval(key, data, is_new, row_id):
    """状态与意见只能由相应环节产生，不由申请人自己写。

    · 申请人（员工）：新建记默认值（待审批 / 待打款），编辑时保持库里已有的结果，
      自己改不了状态、也写不了意见 —— 单据要往上跑一级去处理。
    · 管理员 / 财务：可以改；新建直接记默认（审批类为「已批准」，
      预支单为「待打款」）。
    · 表中 FINANCE_OWNED_STATUS 列出的（预支单）归财务：
      管理员能看能批，但打款状态由财务落定。
    · **费用里「报销方式＝无需报销」是例外**：这种单子本来就没有钱要付给谁，
      状态直接记成「无需报销」——不进「报销审批与打款」的待办，
      也不算进「待审批 / 已批准待打款」的金额（那些口径都按状态取值）。
      改回别的报销方式时，状态会退回「待审批」重新排队（审批人改也一样 ——
      改报销方式不等于批准这笔单子）。
    """
    spec = APPROVAL_FIELDS.get(key)
    if not spec:
        return
    fld, memofld, approver_default, pending = spec
    no_reimb = (key == "costs"
                and (data.get("settle_type") or "").strip() == meta.NO_REIMBURSE)
    tbl = meta.ENTITIES[key]["table"]
    sel = ", ".join(x for x in (f"{fld} AS s",
                                (f"{memofld} AS m" if memofld else None)) if x)
    old = {}
    if not is_new and row_id:
        old = S.one(f"SELECT {sel} FROM {tbl} WHERE id = ?", (row_id,)) or {}
    # 谁能写这个状态：打款类归财务，其余归审批人（管理员 / 财务）
    can_edit = is_finance() if key in FINANCE_OWNED_STATUS else is_approver()
    if can_edit:
        if is_new:
            # 新建时一律用服务端默认值，不采信表单传来的值
            #（这些字段在新建时本来就不出现，能传上来就是伪造的）
            data[fld] = meta.NO_REIMBURSE if no_reimb else approver_default
        else:
            # 编辑时字段在表单里，以提交内容为准；
            # 状态被清空则沿用原值，避免误把已批准/已打款打回默认
            v = data.get(fld) or old.get("s") or approver_default
            if no_reimb:
                v = meta.NO_REIMBURSE
            elif v == meta.NO_REIMBURSE:
                # 从「无需报销」改回别的报销方式 → 这笔钱现在又要付给谁了，
                # 一律退回「待审批」重新排队（审批人自己动手也照此处理：
                # 改方式不等于批准这笔报销，想批再点一下就行）。
                v = pending
            data[fld] = v
        if memofld:
            data[memofld] = data.get(memofld) or None
    else:
        if no_reimb:
            data[fld] = meta.NO_REIMBURSE
        else:
            v = old.get("s") or pending
            if v == meta.NO_REIMBURSE:
                v = pending
            data[fld] = v
        if memofld:
            data[memofld] = old.get("m")


def _save_new_files(key, rid):
    """新建单保存成功后，把录入时一起选的附件归档到这张单上。

    以前附件只能先保存、再进「编辑 → 管理附件」上传 —— 录合同的时候手边正好
    有 PDF，却被迫分两步。这里在新建流程末尾补一次归档，失败也只提示不回滚单据。
    """
    files = [f for f in request.files.getlist("files") if f and f.filename]
    if not files:
        return 0
    ok = 0
    for f in files:
        try:
            S.add_attachment(key, rid, f, session.get("dname", ""))
            ok += 1
        except ValueError as e:
            flash(f"附件 {f.filename} 没有上传：{e}", "error")
    if ok:
        flash(f"已归档 {ok} 个附件", "ok")
    return ok


def _prepare_data(key, data, is_new=False, row_id=None):
    """保存前的服务端补全与业务校验；返回错误列表。"""
    errs = []
    # 出勤自动计算
    if key in ("overtime", "leaves", "trips"):
        S.apply_attendance_calc(key, data)
    if is_employee():
        # 员工新建/编辑时，项目和合同都只能选自己负责或被授权的
        # （列表与编辑页本来有范围限制，但新建流程以前没查，能挂到别人项目上）
        _pids = S.accessible_project_ids(cur_uid())
        _pid = data.get("project_id")
        if _pid not in (None, ""):
            try:
                _pid = int(_pid)
            except (TypeError, ValueError):
                _pid = None
            if _pid and _pid not in _pids:
                errs.append("所选【所属项目】不在你负责 / 被授权的范围内")
        _cid = data.get("contract_id")
        if _cid not in (None, ""):
            try:
                _cid = int(_cid)
            except (TypeError, ValueError):
                _cid = None
            if _cid:
                _crow = S.one("SELECT project_id FROM contracts WHERE id = ?", (_cid,))
                _cp = (_crow or {}).get("project_id")
                if _cp and int(_cp) not in _pids:
                    errs.append("所选【关联合同】不在你负责 / 被授权的项目内")

    # 合同必须挂在所选项目下（先选项目、再选合同；这里服务端再兜一道）
    if key not in ("projects", "contracts"):
        _cid2 = data.get("contract_id")
        if _cid2 not in (None, ""):
            try:
                _cid2 = int(_cid2)
            except (TypeError, ValueError):
                _cid2 = None
            if _cid2:
                _crow2 = S.one("SELECT code, project_id FROM contracts WHERE id = ?",
                               (_cid2,)) or {}
                _cp2 = _crow2.get("project_id")
                _pid2 = data.get("project_id")
                if _pid2 in (None, "") and _cp2:
                    # 只选了合同没选项目 → 按合同把项目带出来（免得成本归不了项目）
                    data["project_id"] = _cp2
                elif _pid2 not in (None, "") and _cp2 is not None \
                        and int(_cp2) != int(_pid2):
                    errs.append(
                        "所选【关联合同】%s 属于另一个项目，跟上面的【所属项目】对不上；"
                        "请先选项目，再从该项目的合同里选。" % (_crow2.get("code") or _cid2))
    if key in ("costs", "overtime", "leaves", "trips"):
        # 归属人：员工只能给自己报，不能替他人申请；管理员/财务可以代录
        old_emp, old_st = None, ""
        if not is_new and row_id:
            _spec = APPROVAL_FIELDS.get(key)
            _st_col = _spec[0] if _spec else "status"
            _tbl = meta.ENTITIES[key]["table"]
            _old = S.one(f"SELECT employee_id, {_st_col} AS st FROM {_tbl} "
                         "WHERE id = ?", (row_id,)) or {}
            old_emp, old_st = _old.get("employee_id"), _old.get("st") or ""
        if is_employee():
            # 归属人必须是自己的：以前这里直接写 cur_uid()，员工保存同项目里
            # 别人的单据（或没归属人的公司费用），归属人会悄悄变成自己。
            _mine = (old_emp is not None and int(old_emp) == int(cur_uid()))
            if not is_new and not _mine:
                errs.append("这不是你提交的单据，不能修改")
            elif key == "costs" and old_st == "已打款":
                errs.append("这笔报销已经打款，不能再改；要调整请让财务先退回")
            else:
                data["employee_id"] = cur_uid()
        elif not data.get("employee_id"):
            # 审批人编辑时没选归属人：沿用原值，别把别人的单据挂到自己名下
            data["employee_id"] = old_emp or cur_uid()
    if key == "handovers":
        # 工程交付资料：项目二选一 —— 要么挂项目管理里的项目，要么手填一个项目名
        # （手填的不进项目管理，只在本单据上显示）
        _pid = data.get("project_id")
        _pname = (data.get("project_name") or "").strip()
        if _pid and _pname:
            errs.append("【对应项目】和【项目名称】只能填一个："
                        "选了已有项目就把项目名称清空；想手填项目名，"
                        "就把【对应项目】选回「— 请选择 —」。")
        elif not _pid and not _pname:
            errs.append("请指定项目：从【对应项目】里选一个，"
                        "或者直接在【项目名称】里手填一个项目名 —— "
                        "这个就是这份资料的名称。")
        data["project_name"] = _pname or None
    if key == "invoices":
        # 关联合同必须跟发票方向一致：进项→支出合同、销项→收入合同。
        # 前端下拉已经按方向过滤，这里再兜一道，防止绕过表单直接 POST。
        _dir = (data.get("direction") or "").strip()
        _cid = data.get("contract_id")
        _want = {"收到进项发票": "支出合同", "开具销项发票": "收入合同"}.get(_dir)
        if _want and _cid:
            _cdir = S.scalar("SELECT direction FROM contracts WHERE id = ?", (_cid,))
            if _cdir and _cdir != _want:
                errs.append(
                    "发票方向是「%s」，【关联合同】只能选%s；现在选的是「%s」的合同。"
                    "如果这张票只是项目上的采购、没有对应支出合同，"
                    "请把【关联合同】留空，改用【所属项目】归集成本。"
                    % (_dir, _want, _cdir or "方向未填"))
    if key == "costs":
        # 预支核销：必须选预支单，且不能超额
        if data.get("settle_type") == "预支核销":
            aid = data.get("advance_id")
            if not aid:
                errs.append("选择「预支核销」时必须指定关联预支单")
            else:
                adv = S.one("SELECT * FROM advances WHERE id = ?", (aid,))
                if not adv:
                    errs.append("关联的预支单不存在")
                elif (adv.get("status") or PENDING_ADVANCE) == PENDING_ADVANCE:
                    errs.append("这笔预支款还没确认打款，不能核销；"
                                "请财务先在预支单上点【确认打款】")
                else:
                    if adv.get("employee_id") != data.get("employee_id"):
                        errs.append("预支单的收款员工与报销人不一致，不能核销")
                    bal = S.advance_balance(aid)
                    if row_id:
                        old = S.one("SELECT amount, advance_id FROM costs WHERE id = ?",
                                    (row_id,))
                        if old and old.get("advance_id") == aid:
                            bal = S.round2(bal + (old.get("amount") or 0))
                    if (data.get("amount") or 0) > bal + S.TOL:
                        errs.append(f"核销金额 {S.money(data.get('amount'))} "
                                    f"超过该预支单可用余额 {S.money(bal)}")
        elif data.get("settle_type"):
            data["advance_id"] = None
    if key == "advances":
        if is_employee():
            errs.append("员工不能自己创建预支单，请联系财务")
        if not data.get("handler"):
            data["handler"] = session.get("dname", "")
    # 审批结果：申请人不能自己定，走上级审批
    _apply_approval(key, data, is_new, row_id)
    return errs


def _scope_guard(key, row):
    """员工越权访问他项目的数据时挡回。"""
    if not S.can_touch_row(key, row, cur_uid(), cur_role()):
        flash("这条记录不在你负责或授权范围内", "error")
        return redirect(url_for("entity_list", key=key))
    return None


def _form_settlement_rows():
    inv_ids = request.form.getlist("s_invoice_id")
    amounts = request.form.getlist("s_amount")
    ratios = request.form.getlist("s_ratio")
    memos = request.form.getlist("s_memo")
    out = []
    for i, v in enumerate(inv_ids):
        out.append({
            "invoice_id": v,
            "amount": amounts[i] if i < len(amounts) else "",
            "ratio": ratios[i] if i < len(ratios) else "",
            "memo": memos[i] if i < len(memos) else "",
        })
    return out


@app.route("/e/<key>/<int:rid>/edit", methods=["GET", "POST"])
@login_required
def entity_edit(key, rid):
    # 模块级权限：以前这里只有 _scope_guard（只管员工的项目范围），
    # 导致员工能直接打开「对私计算」这类受限模块的详情页。
    _require_item("e", key, write=True)
    ent = _entity(key)
    if ent is None:
        return redirect(url_for("index"))
    row = S.get_row(key, rid)
    if not row:
        abort(404)
    g = _scope_guard(key, row)
    if g:
        return g
    if request.method == "POST":
        data = S.form_to_data(key, request.form, base_row=row)
        errs = _prepare_data(key, data, is_new=False, row_id=rid)
        errs += S.validate(key, data, row_id=rid)
        if key == "payments":
            st_rows, serrs = S.parse_settlements(request.form, payment_id=rid)
            errs += serrs
            amt = S.round2(data.get("amount"))
            tot = S.round2(sum(r["amount"] for r in st_rows))
            if amt and tot > amt + S.TOL:
                errs.append(f"核销合计 {S.money(tot)} 超过付款金额 {S.money(amt)}")
        else:
            st_rows = None
        d_rows = S.parse_details(key, request.form)
        errs += S.detail_error(key, d_rows, data)
        if key == "quotations":
            tot = S.detail_sum(key, d_rows.get("items") or [])
            if S._to_num(data.get("discount")) > tot + S.TOL:
                errs.append("【优惠让利】%s 超过了明细合计 %s"
                            % (S.money(data.get("discount")), S.money(tot)))
        if errs:
            for e in errs:
                flash(e, "error")
            # 回显：以原记录打底（保留单号、审批结果等不提交的字段），
            # 再用用户本次填的内容覆盖
            echo = dict(row)
            for k in request.form:
                echo[k] = request.form.get(k)
            echo["id"] = rid
            return _render_form(key, ent, echo, False,
                                settlement_rows=_form_settlement_rows(),
                                detail_rows=d_rows)
        _saved, save_err = _save_or_flash(key, data, row_id=rid)
        if save_err:
            flash(save_err, "error")
            # 保存失败也把 back 带回去，别让「返回列表」掉回第 1 页
            _bk = _back_url()
            return redirect(url_for("entity_edit", key=key, rid=rid,
                                    **({"back": _bk} if _bk else {})))
        if key == "staff":
            # 离职日期变了 → 那一行（离职当月）重新交给离职折算算，
            # 把"手工改过"的标记清掉，否则先手工调过的人补填离职日期不会按天重算
            olds = str(row.get("leave_date") or "").strip()[:7]
            news = str(data.get("leave_date") or "").strip()[:7]
            if olds != news:
                for _m in {olds, news}:
                    S.payroll_reset_leave_month(rid, _m)
        if st_rows is not None:
            S.save_settlements(rid, st_rows)
        S.save_details(key, rid, d_rows)
        if key == "quotations":
            S.quotation_recalc(rid)
        elif key == "private_settlements":
            S.private_recalc(rid)
            _save_contract_links(rid)
        elif key == "invoices":
            # 人工在下拉里改了状态 → 这张票的状态就归人工：
            # 以后核销再变动也不会自动把它退回「费用未付款」
            S.invoice_status_manual(rid, (row or {}).get("status"),
                                    data.get("status"))
            _sync_invoice_cost(rid)
        flash("已保存", "ok")
        _bk = _back_url()
        return redirect(url_for("entity_edit", key=key, rid=rid,
                                **({"back": _bk} if _bk else {})))
    return _render_form(key, ent, row, False)


def _attachment(filename):
    """带中文文件名的下载响应头（RFC 5987 双写，老浏览器也能存对名字）。

    HTTP 响应头只能是 latin-1，所以：
      · filename= 用纯 ASCII 兜底（注意 Python3 正则里的 \\w 会把中文也算作单词字符，
        必须显式写 [A-Za-z0-9._-]，否则兜底名里还是中文 → 响应头编码报错）
      · filename*=UTF-8'' 放百分号编码的真名，现代浏览器优先用它
    """
    from urllib.parse import quote
    ascii_name = re.sub(r"[^A-Za-z0-9._-]", "_", filename) or "download"
    return {"Content-Disposition":
            "attachment; filename=%s; filename*=UTF-8''%s"
            % (ascii_name, quote(filename))}


# ------------------------------------------------------------------ 报价 / 送货
@app.route("/items/template.xlsx")
@login_required
def items_template():
    """明细导入模板（报价单 / 合同清单通用，列一样）。"""
    return Response(S.quotation_template_xlsx(), mimetype=MIME_XLSX,
                    headers=_attachment("明细导入模板.xlsx"))


@app.route("/e/<key>/<int:rid>/xlsx")
@login_required
def entity_xlsx(key, rid):
    """导出报价单 Excel（带样式的正式报价单）。"""
    if key != "quotations":
        abort(404)
    _require_item("e", key)
    row = S.get_row("quotations", rid)
    if not row:
        abort(404)
    g_ = _scope_guard("quotations", row)
    if g_:
        return g_
    return Response(S.quotation_workbook(rid, company=COMPANY_NAME),
                    mimetype=MIME_XLSX,
                    headers=_attachment(f"报价单_{row.get('code') or rid}.xlsx"))


@app.route("/e/private_settlements/<int:rid>/xlsx")
@login_required
def private_xlsx(rid):
    """导出对私计算单 Excel。"""
    _require_item("e", "private_settlements")
    row = S.get_row("private_settlements", rid)
    if not row:
        abort(404)
    g_ = _scope_guard("private_settlements", row)
    if g_:
        return g_
    return Response(S.private_settlement_xlsx(rid, company=COMPANY_NAME),
                    mimetype=MIME_XLSX,
                    headers=_attachment(f"对私计算单_{row.get('code') or rid}.xlsx"))


@app.route("/e/<key>/<int:rid>/import", methods=["POST"])
@login_required
def entity_import(key, rid):
    """按模板填好的 Excel 导入明细（追加，不覆盖已录的）。"""
    _require_item("e", key, write=True)
    ent = _entity(key)
    if ent is None:
        return redirect(url_for("index"))
    spec = (ent.get("details") or [{}])[0]
    if not spec.get("import"):
        abort(404)
    row = S.get_row(key, rid)
    if not row:
        abort(404)
    g = _scope_guard(key, row)
    if g:
        return g
    try:
        items = S.read_import_file(request.files.get("file"))
        total = S.merge_import_items(key, rid, items)
        if key == "quotations":
            S.quotation_recalc(rid)
        flash(f"已导入 {len(items)} 行明细，现在共 {total} 行；"
              "品牌 / 型号 / 参数已自动进「品牌型号库」", "ok")
    except ValueError as e:
        flash(str(e), "error")
    return redirect(url_for("entity_edit", key=key, rid=rid))


@app.route("/quotations/<int:rid>/to_contract", methods=["POST"])
@login_required
def quotation_to_contract(rid):
    """报价单转合同：管理员、财务或本单经手员工。"""
    _require_item("e", "quotations", write=True)
    row = S.get_row("quotations", rid)
    if not row:
        abort(404)
    if not S.quotation_can_convert(row, cur_uid(), cur_role()):
        abort(403)
    try:
        cid = S.quotation_to_contract(rid, cur_uid())
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("entity_edit", key="quotations", rid=rid))
    h = S.one("SELECT * FROM contracts WHERE id = ?", (cid,))
    flash("已按报价生成合同 %s：金额与清单都带过去了，"
          "接着补签订日期与合同原件就行" % (h or {}).get("code", ""), "ok")
    # 员工可能看不到新合同所属项目（不在他的项目范围内），这时给句提示而不是弹回去
    if not S.can_touch_row("contracts", h, cur_uid(), cur_role()):
        flash("这份合同挂在别人的项目下，你看不到它的明细；"
              "需要的话请管理员把项目授权给你", "warn")
        return redirect(url_for("entity_list", key="contracts"))
    return redirect(url_for("entity_edit", key="contracts", rid=cid))


@app.route("/e/deliveries/<int:rid>/docx")
@login_required
def delivery_docx(rid):
    """导出送货单 Word。"""
    _require_item("e", "deliveries")
    row = S.get_row("deliveries", rid)
    if not row:
        abort(404)
    g = _scope_guard("deliveries", row)
    if g:
        return g
    return Response(S.delivery_docx(rid, company=COMPANY_NAME), mimetype=MIME_DOCX,
                    headers=_attachment(f"送货单_{row.get('code') or rid}.docx"))


def _render_delivery_pick(contract_id, row, rows, form=None):
    """送货勾选页：勾项目 + 填本次数量 + 收货信息。"""
    c = S.contract_summary(contract_id)
    f = form or {}
    if not f:
        f = {"receiver": "", "phone": "", "address": "",
             "deliver_date": datetime.now().strftime("%Y-%m-%d"),
             "carrier": "公司送货", "memo": ""}
    return render_template("delivery_pick.html", contract=c, cid=contract_id,
                           rows=rows, f=f,
                           carriers=dict_options("运输方式"),
                           back_url=url_for("entity_edit", key="contracts",
                                            rid=contract_id))


@app.route("/e/contracts/<int:rid>/private")
@login_required
def contract_new_private(rid):
    """按这张合同的清单新建一张对私计算单。"""
    _require_item("e", "private_settlements", write=True)
    if not S.get_row("contracts", rid):
        abort(404)
    return redirect(url_for("entity_new", key="private_settlements",
                            contract_id=rid))


@app.route("/e/contracts/<int:rid>/delivery", methods=["GET", "POST"])
@login_required
def contract_new_delivery(rid):
    """按合同清单生成送货单：勾选要送的项，填本次数量。

    送货单不再单独占菜单入口，一律从合同这里产生。
    """
    _require_item("e", "contracts", write=True)
    if not _entity("contracts"):
        abort(404)
    row = S.get_row("contracts", rid)
    if not row:
        abort(404)
    g = _scope_guard("contracts", row)
    if g:
        return g
    rows = S.delivery_pick_rows(rid)
    if request.method == "POST":
        ids = request.form.getlist("item_id")
        by_id = {str(x["id"]): x for x in rows}
        picked, over = [], []
        for iid in ids:
            it = by_id.get(str(iid))
            if not it:
                continue
            # 数量字段按清单项 id 命名（qty_<id>）：只有勾选的行会提交 item_id，
            # 用两个 list 按位置配对会错位
            qty = S.round2(S._to_num(request.form.get("qty_%s" % iid, "")))
            if qty <= S.TOL:
                continue
            if it["left"] and qty > it["left"] + S.TOL:
                over.append(it["name"])
                qty = it["left"]          # 不让超送：按剩余量封顶
            picked.append((it, qty))
        if not picked:
            flash("还没勾选要送的清单项（或者本次数量都是 0）", "error")
            return _render_delivery_pick(rid, row, rows, request.form)
        header = {
            "receiver": (request.form.get("receiver") or "").strip(),
            "phone": (request.form.get("phone") or "").strip(),
            "address": (request.form.get("address") or "").strip(),
            "deliver_date": (request.form.get("deliver_date") or "").strip(),
            "carrier": (request.form.get("carrier") or "").strip(),
            "memo": (request.form.get("memo") or "").strip(),
            "handler": session.get("dname", ""),
        }
        try:
            did = S.create_delivery_from_contract(rid, picked, header)
        except ValueError as e:
            flash(str(e), "error")
            return _render_delivery_pick(rid, row, rows, request.form)
        msg = "已生成送货单（%d 项）" % len(picked)
        if over:
            msg += "；其中 %s 的本次数量超过未送数量，已按剩余量封顶" % \
                   "、".join(sorted(set(over))[:3])
        flash(msg + "。下面可以修改后再【导出 Word 送货单】打印。", "ok")
        return redirect(url_for("entity_edit", key="deliveries", rid=did))
    return _render_delivery_pick(rid, row, rows)


@app.route("/advances/<int:rid>/confirm_pay", methods=["POST"])
@finance_required
def advance_confirm_pay(rid):
    """财务确认「这笔预支款已经打给员工了」。

    预支单登记时是「待打款」，只有确认打款后员工才能用它核销 ——
    否则会出现"钱还没到手就已经抵扣掉了"。
    """
    row = S.one("SELECT * FROM advances WHERE id = ?", (rid,))
    if not row:
        abort(404)
    if (row.get("status") or PENDING_ADVANCE) != PENDING_ADVANCE:
        flash("这笔预支款已经确认过打款了", "ok")
        return redirect(url_for("entity_edit", key="advances", rid=rid))
    S.execute(
        "UPDATE advances SET status = '已打款', "
        "handler = CASE WHEN IFNULL(handler,'') = '' THEN ? ELSE handler END, "
        "pay_date = CASE WHEN IFNULL(pay_date,'') = '' THEN ? ELSE pay_date END "
        "WHERE id = ?",
        (session.get("dname", ""), datetime.now().strftime("%Y-%m-%d"), rid))
    flash("已确认打款，这笔预支款的余额现在可以用于核销了", "ok")
    return redirect(url_for("entity_edit", key="advances", rid=rid))


def _invoice_stats(rid):
    row = S.get_row("invoices", rid)
    settled, remaining, progress, state = S.invoice_settle_view(
        row, S.invoice_settled(rid))          # 已收讫 ⇒ 已结清（与列表一致）
    return {"gross": S.round2(row.get("amount_gross")), "settled": settled,
            "remaining": remaining, "state": state,
            "details": query("""
                SELECT x.code, x.pay_date, s.amount, s.ratio
                FROM settlements s LEFT JOIN payments x ON s.payment_id = x.id
                WHERE s.invoice_id = ? ORDER BY x.pay_date""", (rid,))}


@app.route("/e/<key>/<int:rid>/inline", methods=["POST"])
@login_required
def entity_inline_set(key, rid):
    """列表页里直接改一个字段（报价状态用）。

    只认实体 `inline` 里声明过的字段，并沿用同一套数据范围校验 —
    和打开表单改是同一条底线，不能因为省一步就放松。
    """
    _require_item("e", key, write=True)
    ent = _entity(key)
    if ent is None:
        abort(404)
    row = S.get_row(key, rid)
    if not row:
        abort(404)
    g = _scope_guard(key, row)
    if g:
        return g
    field = (request.form.get("field") or "").strip()
    value = (request.form.get("value") or "").strip()
    try:
        S.set_entity_field(key, rid, field, value)
        label = next((f["label"] for f in (ent.get("fields") or [])
                      if f["name"] == field), field)
        flash("已把「%s」改为「%s」" % (label, value or "—"), "ok")
    except ValueError as e:
        flash(str(e), "error")
    return redirect(_back_url() or url_for("entity_list", key=key))


@app.route("/e/<key>/<int:rid>/delete", methods=["POST"])
@approver_required
def entity_delete(key, rid):
    _require_item("e", key, write=True)
    if not meta.ENTITIES.get(key):
        abort(404)
    linked = (S.one("SELECT code FROM costs WHERE invoice_id = ?", (rid,))
              if key == "invoices" else None)
    try:
        S.delete_entity(key, rid)
        flash("已删除" + ("（连带删掉了自动生成的费用 %s）" % linked["code"]
                         if linked else ""), "ok")
    except ValueError as e:
        flash(str(e), "error")
    return redirect(_back_url() or url_for("entity_list", key=key))


# ------------------------------------------------------------------ 附件
# ------------------------------------------------- 发票 PDF 拖拽导入
# 一次拖入多张 PDF → 逐张解析 → 逐张确认保存（不是静默入库：
# 版式千差万别，认不准的字段一律留空并在页面上标出来让人核）。
IMP_MAX_FILES = 30          # 一次最多 30 张，防手滑把整个文件夹扔进来
IMP_TTL_HOURS = 24          # 临时目录超过这个时间自动清掉


def _imp_root():
    d = UPLOAD_DIR / "_import"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _imp_dir(token, create=False):
    """token 是 12 位十六进制 —— 白名单式校验，别让它变成路径穿越。"""
    if not re.fullmatch(r"[0-9a-f]{12}", token or ""):
        return None
    d = _imp_root() / token
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d if d.is_dir() else None


def _imp_load(token):
    d = _imp_dir(token)
    if not d:
        return None
    f = d / "items.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _imp_save(token, items):
    d = _imp_dir(token, create=True)
    (d / "items.json").write_text(json.dumps(items, ensure_ascii=False),
                                  encoding="utf-8")


def _imp_cleanup():
    """清掉超过 IMP_TTL_HOURS 的临时导入目录（每次导入顺手做一次）。"""
    import time as _t
    root = _imp_root()
    now = _t.time()
    for d in root.iterdir():
        try:
            if d.is_dir() and now - d.stat().st_mtime > IMP_TTL_HOURS * 3600:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


def _imp_counts(items):
    return {"total": len(items),
            "done": sum(1 for x in items if x.get("state") == "done"),
            "skip": sum(1 for x in items if x.get("state") == "skip")}


def _imp_next(items, start=0):
    """下一个还没处理的序号（先往后找，再回头看前面漏的）；都处理完返回 None。"""
    order = list(range(max(start, 0), len(items))) + list(range(0, max(start, 0)))
    for i in order:
        if items[i].get("state") not in ("done", "skip"):
            return i
    return None


def _imp_item(token, i):
    items = _imp_load(token)
    if not items or not isinstance(i, int) or not (0 <= i < len(items)):
        return None, None
    return items, items[i]


def _imp_ctx(token, i, items=None):
    """确认页的上下文：进度、源文件、告警、票面明细、是否已录过。"""
    if items is None:
        items = _imp_load(token) or []
    it = items[i] if isinstance(i, int) and 0 <= i < len(items) else {}
    pr = it.get("parsed") or {}
    fields = pr.get("fields") or {}
    dup = None
    if fields.get("no"):
        dup = S.one("SELECT id, no, issue_date, amount_gross, unit_id "
                    "FROM invoices WHERE no = ?", (fields["no"],))
    c = _imp_counts(items)
    return {"token": token, "i": i, "pos": i + 1, "counts": c,
            "orig": it.get("orig") or "", "state": it.get("state") or "",
            "fields": fields, "raw_unit": fields.get("unit_name") or "",
            # 键名别叫 items：Jinja 里 imp.items 拿到的是字典的 items 方法（踩过两次了）
            "item_names": pr.get("items") or [],
            "item_count": pr.get("item_count") or 0,
            "warnings": pr.get("warnings") or [], "error": pr.get("error"),
            "amount_cn": pr.get("amount_cn"), "amount_matched": pr.get("amount_matched"),
            "dup": dup, "unit_id": None, "unit_conf": None}


def _imp_done(token, i, rid):
    """这张存好了：把 PDF 归档成它的附件，然后把队列推到下一张。"""
    items = _imp_load(token) or []
    if 0 <= i < len(items):
        it = items[i]
        d = _imp_dir(token)
        p = (d / it["file"]) if (d and it.get("file")) else None
        if p and p.exists():
            try:
                S.add_attachment_bytes("invoices", rid, it.get("orig") or p.name,
                                       p.read_bytes(), session.get("dname", ""),
                                       "application/pdf")
            except ValueError as e:
                flash("发票已保存，但 PDF 原件没归档成功：%s" % e, "error")
        it["state"] = "done"
        it["invoice_id"] = rid
    _imp_save(token, items)
    nxt = _imp_next(items, i + 1)
    c = _imp_counts(items)
    if nxt is None:
        flash("本批 %d 张发票已处理完（保存 %d / 跳过 %d）"
              % (c["total"], c["done"], c["skip"]), "ok")
        d = _imp_dir(token)
        if d:
            shutil.rmtree(d, ignore_errors=True)
        return redirect(url_for("entity_list", key="invoices"))
    return redirect(url_for("entity_new", key="invoices", **{"imp": token, "i": nxt}))


@app.route("/e/invoices/import", methods=["POST"])
@login_required
def invoice_import():
    """接收拖入的 PDF：存到临时目录 → 逐张解析 → 跳到第 1 张的确认页。"""
    _require_item("e", "invoices", write=True)
    files = [f for f in request.files.getlist("files") if f and f.filename]
    if not files:
        flash("没有收到文件（可能选的是文件夹，请选 PDF 文件）", "error")
        return redirect(url_for("entity_list", key="invoices"))
    _imp_cleanup()
    token = uuid.uuid4().hex[:12]
    d = _imp_dir(token, create=True)
    company_tax = S.setting("company_tax_no", "")
    items = []
    for idx, f in enumerate(files[:IMP_MAX_FILES]):
        orig = os.path.basename(f.filename)
        item = {"file": "", "orig": orig, "state": "todo", "parsed": {}}
        if not orig.lower().endswith(".pdf"):
            item["state"] = "skip"
            item["parsed"] = {"error": "只支持 PDF 文件，这张跳过了",
                              "warnings": [], "fields": {}}
            items.append(item)
            continue
        data = f.read()
        if not data.startswith(b"%PDF"):
            item["state"] = "skip"
            item["parsed"] = {"error": "这个文件看着不像 PDF（可能改过扩展名）",
                              "warnings": [], "fields": {}}
            items.append(item)
            continue
        name = "%02d.pdf" % idx
        (d / name).write_bytes(data)
        item["file"] = name
        pr = invoice_parse.parse_invoice_pdf(str(d / name),
                                            company_name=COMPANY_NAME,
                                            company_tax_no=company_tax)
        pr.pop("raw_text", None)          # 票面全文不入库，够用就行
        pr["file_name"] = orig
        item["parsed"] = pr
        if not pr.get("ok"):
            item["state"] = "todo"        # 认不全也让人进确认页看一眼，别静默丢
        items.append(item)
    if len(files) > IMP_MAX_FILES:
        flash("一次最多 %d 张，本次只处理了前 %d 张" % (IMP_MAX_FILES, IMP_MAX_FILES),
              "error")
    _imp_save(token, items)
    for x in items:                    # 跳过/失败的逐条说明，别让人猜
        if x.get("state") == "skip":
            flash("%s：%s" % (x.get("orig"), (x.get("parsed") or {}).get("error")
                              or "已跳过"), "error")
    c = _imp_counts(items)
    flash("收到 %d 张，识别完成，请逐张核对后保存" % c["total"], "ok")
    nxt = _imp_next(items, 0)
    if nxt is None:                   # 一张都没得确认（全是 txt/假 PDF/扫描件）
        shutil.rmtree(_imp_dir(token), ignore_errors=True)
        return redirect(url_for("entity_list", key="invoices"))
    return redirect(url_for("entity_new", key="invoices", **{"imp": token, "i": nxt}))


@app.route("/e/invoices/import/<token>/skip", methods=["POST"])
@login_required
def invoice_import_skip(token):
    """这一张不录（重复的、认不出来的、不是我要的）→ 跳到下一张。"""
    _require_item("e", "invoices", write=True)
    items = _imp_load(token) or []
    i = request.form.get("i", 0, type=int)
    if 0 <= i < len(items):
        items[i]["state"] = "skip"
    _imp_save(token, items)
    nxt = _imp_next(items, i + 1)
    c = _imp_counts(items)
    if nxt is None:
        flash("本批 %d 张已处理完（保存 %d / 跳过 %d）"
              % (c["total"], c["done"], c["skip"]), "ok")
        d = _imp_dir(token)
        if d:
            shutil.rmtree(d, ignore_errors=True)
        return redirect(url_for("entity_list", key="invoices"))
    return redirect(url_for("entity_new", key="invoices", **{"imp": token, "i": nxt}))


@app.route("/e/invoices/import/<token>/cancel", methods=["POST"])
@login_required
def invoice_import_cancel(token):
    """这次不导了：清掉临时文件（已经保存过的发票和附件都不受影响）。"""
    _require_item("e", "invoices", write=True)
    items = _imp_load(token) or []
    c = _imp_counts(items)
    d = _imp_dir(token)
    if d:
        shutil.rmtree(d, ignore_errors=True)
    flash("已结束本次导入（保存了 %d 张，其余未入库）" % c["done"], "ok")
    return redirect(url_for("entity_list", key="invoices"))


@app.route("/e/invoices/import/<token>/unit", methods=["POST"])
@login_required
def invoice_import_unit(token):
    """票面上的单位在往来单位里没有 → 就地新建一个，回到确认页时自动选中。"""
    _require_item("e", "invoices", write=True)
    i = request.form.get("i", 0, type=int)
    name = (request.form.get("name") or "").strip()
    tax_no = (request.form.get("tax_no") or "").strip()
    if not name:
        flash("单位名称不能为空", "error")
        return redirect(url_for("entity_new", key="invoices",
                                **{"imp": token, "i": i}))
    exist, _ = S.unit_id_by_name(name)
    if exist:
        flash("往来单位里已经有「%s」了，已替你选中" % name, "ok")
        return redirect(url_for("entity_new", key="invoices",
                                **{"imp": token, "i": i, "new_unit": exist}))
    data = {"name": name, "tax_no": tax_no, "type": "其他"}
    rid, err = _save_or_flash("units", data)
    if err:
        flash(err, "error")
        return redirect(url_for("entity_new", key="invoices",
                                **{"imp": token, "i": i}))
    flash("已新建往来单位「%s」" % name, "ok")
    return redirect(url_for("entity_new", key="invoices",
                            **{"imp": token, "i": i, "new_unit": rid}))


@app.route("/e/invoices/import/<token>/file/<int:i>")
@login_required
def invoice_import_file(token, i):
    """看一眼这张票的原件（确认页上「查看 PDF」用）。"""
    _require_item("e", "invoices")
    _, it = _imp_item(token, i)
    d = _imp_dir(token)
    if not it or not d or not it.get("file"):
        abort(404)
    return send_from_directory(d, it["file"], as_attachment=False,
                               download_name=it.get("orig") or "invoice.pdf")


@app.route("/e/contracts/<int:rid>/files", methods=["GET", "POST"])
@app.route("/e/<key>/<int:rid>/files", methods=["GET", "POST"])
@login_required
def contract_files(rid, key="contracts"):
    """单据原件归档页（合同 / 发票共用一套，按 key 区分）。"""
    _require_item("e", key)
    if request.method == "POST":
        # 上传是写操作：被设成「只读」的账号不能往单据上追加附件
        _require_item("e", key, write=True)
    ent = _entity(key)
    if ent is None:
        return redirect(url_for("index"))
    row = S.get_row(key, rid)
    if not row:
        abort(404)
    g_ = _scope_guard(key, row)
    if g_:
        return g_
    if request.method == "POST":
        files = request.files.getlist("files")
        ok = 0
        for f in files:
            if not f or not f.filename:
                continue
            try:
                S.add_attachment(key, rid, f, session.get("dname", ""))
                ok += 1
            except ValueError as e:
                flash(str(e), "error")
        if ok:
            flash(f"已上传 {ok} 个文件", "ok")
        return redirect(url_for("contract_files", key=key, rid=rid))
    return render_template("files.html", row=row, key=key, ent=ent,
                           files=S.attachments_for(key, rid),
                           max_mb=S.MAX_UPLOAD // 1024 // 1024)


# 附件是「挂在某张单据上的文件」：下载/预览必须跟着那张单据的权限与数据范围走。
# 以前只校验登录，员工把 /files/<id>/download 的 id 递增就能拿到别项目的合同原件、
# 工人的身份证照片。认不出挂在哪的附件，只给管理员 / 财务。
_FILE_APPROVER_ONLY = {"workers", "staff", "payroll", "advances"}


def _file_guard(a):
    ref = (a or {}).get("ref_type") or ""
    if ref in _FILE_APPROVER_ONLY:
        return is_approver()
    if ref not in meta.ENTITIES or ref not in S.LIST_SQL:
        return is_approver()
    row = S.get_row(ref, a.get("ref_id"))
    if not row:
        return False
    return S.can_touch_row(ref, row, cur_uid(), cur_role())


@app.route("/files/<int:aid>/download")
@login_required
def file_download(aid):
    a = S.get_attachment(aid)
    if not a:
        abort(404)
    if not _file_guard(a):
        abort(403)
    return send_from_directory(UPLOAD_DIR, a["stored_name"], as_attachment=True,
                               download_name=a["orig_name"])


@app.route("/files/<int:aid>/view")
@login_required
def file_view(aid):
    a = S.get_attachment(aid)
    if not a:
        abort(404)
    if not _file_guard(a):
        abort(403)
    ext = os.path.splitext(a["stored_name"])[1].lower()
    inline = ext in (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff")
    return send_from_directory(UPLOAD_DIR, a["stored_name"], as_attachment=not inline,
                               download_name=a["orig_name"])


@app.route("/files/<int:aid>/delete", methods=["POST"])
@approver_required
def file_delete(aid):
    a = S.delete_attachment(aid)
    flash("附件已删除", "ok")
    # 删完回到它自己的原件页（合同 / 发票 / 工程交付资料…都走同一套）
    if a and a["ref_type"] in meta.ENTITIES:
        return redirect(url_for("contract_files", key=a["ref_type"], rid=a["ref_id"]))
    return redirect(url_for("index"))


# ------------------------------------------------------------------ 用户管理
@app.route("/users")
@admin_required
def user_list():
    return render_template("users.html", users=S.user_list(),
                           me=session.get("uid"))


@app.route("/users/new", methods=["GET", "POST"])
@admin_required
def user_new():
    form = {"username": "", "display_name": "", "role": S.ROLE_EMPLOYEE}
    if request.method == "POST":
        form = {k: (request.form.get(k) or "").strip()
                for k in ("username", "display_name", "role", "emp_no", "hire_date")}
        form["monthly_salary"] = _num(request.form.get("monthly_salary"))
        pwd1 = request.form.get("password") or ""
        pwd2 = request.form.get("password2") or ""
        errs = []
        if pwd1 != pwd2:
            errs.append("两次输入的密码不一致")
        if not errs:
            try:
                uid = S.user_create(form["username"], pwd1, form["display_name"],
                                    form["role"], must_change=1)
                S.user_profile_save(uid, form["monthly_salary"], form["emp_no"],
                                    form["hire_date"])
            except ValueError as e:
                errs.append(str(e))
        if errs:
            for e in errs:
                flash(e, "error")
        else:
            flash(f"已创建账号 {form['username']}，首次登录会要求修改密码", "ok")
            return redirect(url_for("user_list"))
    return render_template("user_form.html", form=form, is_new=True,
                           pwd_min=S.PWD_MIN_LEN)


@app.route("/users/<int:uid>/edit", methods=["GET", "POST"])
@admin_required
def user_edit(uid):
    row = S.user_get(uid)
    if not row:
        abort(404)
    if request.method == "POST":
        display_name = (request.form.get("display_name") or "").strip()
        role = request.form.get("role") or S.ROLE_EMPLOYEE
        enabled = 1 if request.form.get("enabled") else 0
        salary = _num(request.form.get("monthly_salary"))
        emp_no = (request.form.get("emp_no") or "").strip()
        hire_date = (request.form.get("hire_date") or "").strip()
        try:
            S.user_update(uid, display_name, role, enabled)
            S.user_profile_save(uid, salary, emp_no, hire_date)
            flash("账号已更新", "ok")
            return redirect(url_for("user_list"))
        except ValueError as e:
            flash(str(e), "error")
            row = dict(row)
            row.update({"display_name": display_name, "role": role,
                        "enabled": enabled, "monthly_salary": salary,
                        "emp_no": emp_no, "hire_date": hire_date})
    return render_template("user_form.html", form=dict(row), is_new=False,
                           pwd_min=S.PWD_MIN_LEN)


def _num(v):
    try:
        return float(str(v).replace(",", "").strip() or 0)
    except (TypeError, ValueError):
        return 0.0


@app.route("/users/<int:uid>/password", methods=["GET", "POST"])
@admin_required
def user_reset_password(uid):
    row = S.user_get(uid)
    if not row:
        abort(404)
    if request.method == "POST":
        pwd1 = request.form.get("password") or ""
        pwd2 = request.form.get("password2") or ""
        if pwd1 != pwd2:
            flash("两次输入的密码不一致", "error")
        else:
            try:
                S.user_set_password(uid, pwd1, must_change=1)
                flash(f"已重置 {row['username']} 的密码，该账号下次登录时需自己改密", "ok")
                return redirect(url_for("user_list"))
            except ValueError as e:
                flash(str(e), "error")
    return render_template("user_password.html", target=row, pwd_min=S.PWD_MIN_LEN)


@app.route("/users/<int:uid>/delete", methods=["POST"])
@admin_required
def user_delete(uid):
    try:
        S.user_delete(uid, operator_id=session.get("uid"))
        flash("账号已删除", "ok")
    except ValueError as e:
        flash(str(e), "error")
    except sqlite3.IntegrityError:
        # 兜底：万一还有没反查到的引用，给一句人话而不是 500 页面
        flash("该账号还被其他数据引用，不能删除；请改用「停用」", "error")
    return redirect(url_for("user_list"))


@app.route("/users/logs")
@admin_required
def user_logs():
    only_fail = request.args.get("only_fail") == "1"
    return render_template("logs.html", logs=S.login_logs(300, only_fail),
                           only_fail=only_fail, max_fails=S.MAX_FAILS,
                           window=S.FAIL_WINDOW_MIN, lock_min=S.LOCK_MIN)


# ------------------------------------------------------------------ 查询 / 报表
def _query_scope(key):
    """台账 / 报表的数据范围：False 表示这条查询没法按项目切 → 员工不给看。"""
    sc = S.query_scope(key, cur_uid(), cur_role())
    if sc is False:
        flash("这张台账没有按项目拆分，员工账号看不到；需要的话请管理员另开权限", "error")
        abort(403)
    return sc


@app.route("/q/<key>")
@login_required
def query_view(key):
    """台账页。**分页显示**（每页默认 200 行，可用 ?per= 调，20~1000）：
    早先一次把全部行渲染进 HTML，几十万行的表在浏览器里要几十秒。
    打印与导出仍然走 run_query（全量），所以「看到的是一页、打印的是全部」。
    """
    _require_item("q", key)
    qtext = request.args.get("q", "")
    try:
        q, rows, total, pages, page, per = S.run_query_page(
            key, qtext, request.args.get("page", 1), request.args.get("per"),
            scope=_query_scope(key))
    except KeyError:
        abort(404)
    cols = list(rows[0].keys()) if rows else []
    return render_template("query.html", q=q, rows=rows, cols=cols, qtext=qtext,
                           total=total, pages=pages, page=page, per=per)


@app.route("/r/<key>")
@login_required
def report_view(key):
    _require_item("r", key)
    rep = next((r for r in meta.REPORTS if r["key"] == key), None)
    if not rep:
        abort(404)
    q, rows, total, pages, page, per = S.run_query_page(
        rep["query"], "", request.args.get("page", 1), request.args.get("per"),
        scope=_query_scope(rep["query"]))
    cols = list(rows[0].keys()) if rows else []
    return render_template("report.html", rep=rep, q=q, rows=rows, cols=cols,
                           printed_at=now_str(), total=total, pages=pages,
                           page=page, per=per)


@app.route("/export/<fmt>/<key>")
@login_required
def export_query(fmt, key):
    _require_item("q", key)
    try:
        q, rows = S.run_query(key, request.args.get("q", ""),
                              scope=_query_scope(key))
    except KeyError:
        abort(404)
    cols = list(rows[0].keys()) if rows else []
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(cols)
        for r in rows:
            w.writerow([r[c] for c in cols])
        data = "\ufeff" + buf.getvalue()
        return Response(data, mimetype="text/csv; charset=utf-8",
                        headers={"Content-Disposition":
                                 f"attachment; filename={key}_{stamp}.csv"})
    html = ['<html><head><meta charset="utf-8"></head><body>',
            f'<table border="1"><tr><th colspan="{len(cols)}">'
            f'{escape(str(q["title"]))}</th></tr><tr>']
    html += "".join(f"<th>{escape(str(c))}</th>" for c in cols)
    html += "</tr>"
    for r in rows:
        html += "<tr>" + "".join(
            f"<td>{escape('' if r[c] is None else str(r[c]))}</td>"
            for c in cols) + "</tr>"
    html += "</table></body></html>"
    return Response("\ufeff" + "".join(html), mimetype="application/vnd.ms-excel",
                    headers={"Content-Disposition":
                             f"attachment; filename={key}_{stamp}.xls"})


# ------------------------------------------------------------------ 费用报销
@app.route("/reimb/my")
@login_required
def reimb_my():
    """我的费用报销：预支余额、待审批、已批待打款、已打款，以及明细。"""
    uid = cur_uid()
    return render_template("reimb_my.html",
                           s=S.reimb_summary(uid),
                           rows=S.reimb_detail(uid, 100),
                           adv=S.advance_rows_of(uid),
                           my_projects=S.project_options()
                           if is_approver() else [])


@app.route("/reimb/approve")
@page_required("reimb_approve")
def reimb_approve():
    return render_template("reimb_approve.html",
                           rows=S.pending_approvals(),
                           params=S.payroll_params())


@app.route("/reimb/<int:rid>/<action>", methods=["POST"])
@approver_required
def reimb_action(rid, action):
    row = S.one("SELECT * FROM costs WHERE id = ?", (rid,))
    if not row:
        abort(404)
    memo = (request.form.get("memo") or "").strip()
    try:
        if action == "approve":
            S.approve_entity("costs", rid, "已批准", memo, cur_uid())
            flash("已批准，可继续打款", "ok")
        elif action == "reject":
            S.approve_entity("costs", rid, "已驳回", memo, cur_uid())
            flash("已驳回", "ok")
        elif action == "pay":
            # 打款归财务：管理员可以查看审批情况，但不经手资金动作
            if not is_finance():
                flash("打款操作仅财务可用", "error")
                return redirect(url_for("reimb_approve"))
            S.mark_paid(rid, cur_uid())
            flash(f"已标记打款：{S.money(row.get('amount'))}", "ok")
        elif action == "reset":
            S.approve_entity("costs", rid, "待审批", memo, cur_uid())
            flash("已退回待审批", "ok")
        else:
            abort(404)
    except ValueError as e:
        flash(str(e), "error")
    return redirect(url_for("reimb_approve"))


# ------------------------------------------------------------------ 出勤
@app.route("/attend/approve")
@page_required("attend_approve")
def attend_approve():
    return render_template("attend_approve.html", pend=S.attendance_pending())


@app.route("/attend/<tbl>/<int:rid>/<action>", methods=["POST"])
@approver_required
def attend_action(tbl, rid, action):
    if tbl not in ("overtime", "leaves", "trips"):
        abort(404)
    memo = (request.form.get("memo") or "").strip()
    status = {"approve": "已批准", "reject": "已驳回",
              "reset": "待审批"}.get(action)
    if not status:
        abort(404)
    try:
        S.approve_entity(tbl, rid, status, memo, cur_uid())
        flash(f"已{('批准' if action == 'approve' else '驳回' if action == 'reject' else '重置')}", "ok")
    except ValueError as e:
        flash(str(e), "error")
    return redirect(url_for("attend_approve"))


# ---------------------------------------------------------- 记工（点工 / 临时工）
# ⚠️ 跟下面的 /payroll（月薪员工）是两套：那边算社保个税，这边按天结。
@app.route("/worklog/sheet")
@page_required("worklog_sheet")
def worklog_sheet():
    """月度记工表：行 = 工人、列 = 1~当月天数、格子 = 当天工天（可打印签字）。"""
    ym = (request.args.get("ym") or S.worklog_default_month()).strip()
    pid = request.args.get("project_id", type=int) or None
    return render_template("worklog_sheet.html", sheet=S.worklog_month(ym, pid),
                           ym=ym, months=S.worklog_months(), project_id=pid,
                           projects=S.query("SELECT id, name FROM projects ORDER BY code"),
                           printed_at=now_str())


def _wl_project():
    """记工的项目上下文：('missing') / None(=未指定项目) / 项目 id。

    记工是「这个人这一天这个项目」三条一起定位的，所以在「全部项目」
    视图里（没选项目）不允许直接点格子 —— 否则会把别的项目的记录一起改掉。
    """
    raw = (request.form.get("project_id") or "").strip()
    if raw == "":
        return "missing"
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return "missing"
    return None if n == 0 else n


@app.route("/worklog/toggle", methods=["POST"])
@page_required("worklog_sheet", write=True)
def worklog_toggle():
    """月表上点一下格子：空 → 1 个工 → 0.5 → 空（不刷新页面）。"""
    date = (request.form.get("date") or "").strip()
    wid = request.form.get("worker_id", type=int)
    if not date or not wid:
        return jsonify({"ok": False, "error": "参数不全"}), 400
    pid = _wl_project()
    if pid == "missing":
        return jsonify({"ok": False,
                        "error": "请先在上方选一个项目（或「未指定项目」）再记工"
                                 "——「全部项目」是只读汇总视图"}), 400
    try:
        r = S.worklog_toggle(date, wid, pid)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    ym = date[:7]
    r.update({"ok": True, "row": S.worklog_row_stats(ym, wid, pid if pid else 0),
              "total": S.worklog_stats(ym, pid if pid else 0)})
    return jsonify(r)


@app.route("/worklog/rate", methods=["POST"])
@page_required("worklog_sheet", write=True)
def worklog_rate():
    """**只改这一天这个人的工价**（别的天、别的人不动）。"""
    date = (request.form.get("date") or "").strip()
    wid = request.form.get("worker_id", type=int)
    if not date or not wid:
        return jsonify({"ok": False, "error": "参数不全"}), 400
    pid = _wl_project()
    if pid == "missing":
        return jsonify({"ok": False,
                        "error": "请先在上方选一个项目（或「未指定项目」）再改工价"}), 400
    try:
        r = S.worklog_set_rate(date, wid, request.form.get("rate"), pid)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    ym = date[:7]
    r.update({"ok": True, "row": S.worklog_row_stats(ym, wid, pid if pid else 0),
              "total": S.worklog_stats(ym, pid if pid else 0)})
    return jsonify(r)


@app.route("/worklog/mark", methods=["POST"])
@page_required("worklog_sheet", write=True)
def worklog_mark():
    """在一行上拖过一段日期 → 这段都记 1 个工（已经记过的跳过）。"""
    wid = request.form.get("worker_id", type=int)
    d1 = (request.form.get("from") or "").strip()
    d2 = (request.form.get("to") or "").strip()
    pid = _wl_project()
    if pid == "missing":
        return jsonify({"ok": False,
                        "error": "请先在上方选一个项目（或「未指定项目」）再批量记工"}), 400
    if not wid or not d1 or not d2:
        return jsonify({"ok": False, "error": "参数不全"}), 400
    a, b = sorted([d1, d2])
    n, skipped = 0, 0
    try:
        d = datetime.strptime(a, "%Y-%m-%d")
        end = datetime.strptime(b, "%Y-%m-%d")
        while d <= end:
            ds = d.strftime("%Y-%m-%d")
            cond, cargs = S._wl_scope(pid)
            exist = S.scalar("SELECT COUNT(*) FROM worklogs WHERE work_date = ? "
                             "AND worker_id = ?" + cond, [ds, wid] + cargs)
            if exist:
                skipped += 1
            else:
                S._insert_worklog(ds, wid, pid, S.scalar(
                    "SELECT IFNULL(default_rate,0) FROM workers WHERE id = ?",
                    (wid,)), 1.0)
                n += 1
            d += timedelta(days=1)
    except ValueError:
        return jsonify({"ok": False, "error": "日期不对"}), 400
    ym = a[:7]
    return jsonify({"ok": True, "created": n, "skipped": skipped,
                    "row": S.worklog_row_stats(ym, wid, pid if pid else 0),
                    "total": S.worklog_stats(ym, pid if pid else 0)})


@app.route("/worklog/batch", methods=["GET", "POST"])
@page_required("worklog_sheet", write=True)
def worklog_batch():
    """批量记工：一批人 × 一个日期区间，逐日生成。"""
    workers = S.query("SELECT id, name, trade, IFNULL(default_rate,0) AS rate "
                      "FROM workers WHERE IFNULL(status,'在用') <> '停用' "
                      "ORDER BY sort, name")
    projects = S.query("SELECT id, name FROM projects ORDER BY code")
    if request.method == "POST":
        try:
            r = S.worklog_batch(request.form.getlist("worker_ids"),
                                (request.form.get("date_from") or "").strip(),
                                (request.form.get("date_to") or "").strip(),
                                request.form.get("project_id", type=int),
                                (request.form.get("rate") or "").strip() or None,
                                float(request.form.get("days") or 1),
                                request.form.get("memo") or "")
        except ValueError as e:
            flash(str(e), "error")
        else:
            msg = "已生成 %d 条记工" % r["created"]
            if r["skipped"]:
                msg += "，跳过 %d 条（那天已经记过）" % r["skipped"]
            if r["missing_rate"]:
                msg += ("。注意：%s 还没设默认日工资，这几条金额是 0 —— "
                        "请在工人名单里补上，或批量时填一个统一日工资"
                        % "、".join(r["missing_rate"]))
            flash(msg, "ok")
            return redirect(url_for("worklog_sheet",
                                    ym=(request.form.get("date_from") or "")[:7]))
    return render_template("worklog_batch.html", workers=workers, projects=projects,
                           today=datetime.now().strftime("%Y-%m-%d"))


@app.route("/worklog/settle", methods=["POST"])
@page_required("worklog_sheet", write=True)
def worklog_settle():
    """把某月记工按【项目】汇总成「人工费」费用（幂等，重复点只更新金额）。"""
    ym = (request.form.get("ym") or S.worklog_default_month()).strip()
    r = S.worklog_settle(ym, session.get("dname", ""))
    if not r["items"]:
        flash("%s 还没有记工记录，没有生成费用" % ym, "error")
    else:
        parts = []
        if r["created"]:
            parts.append("新增 %d 条" % r["created"])
        if r["updated"]:
            parts.append("更新 %d 条" % r["updated"])
        flash("已把 %s 的记工汇总成「人工费」费用（%s，合计 %s）—— 可在"
              "「费用支出管理」里看到，也计入了项目成本" % (
                  ym, "、".join(parts),
                  S.money(sum(i["amount"] for i in r["items"]))), "ok")
    return redirect(url_for("worklog_sheet", ym=ym))


@app.route("/payroll")
@page_required("payroll")
def payroll():
    """按月工资测算（人社部计薪口径）。"""
    ym = request.args.get("ym") or S.payroll_default_month()
    data = S.payroll_month(ym)
    nxt = S.next_month(ym)
    return render_template("payroll.html", pk=data, months=S.payroll_months(),
                           ym=ym, cols=S.PAY_COLS,
                           # locked_n：本月已定稿的人数（0 = 还是草稿，可改可定稿）
                           locked_n=S.scalar("SELECT COUNT(*) FROM payroll "
                                             "WHERE ym = ? AND locked = 1", (ym,)),
                           seal=S.payroll_seal_state(ym), next_ym=nxt,
                           # 本月调整过几个人（≠ 定稿）：草稿月显示「已调整（N 人）」
                           manual_n=S.scalar("SELECT COUNT(*) FROM payroll "
                                             "WHERE ym = ? AND manual = 1", (ym,)),
                           next_n=S.scalar("SELECT COUNT(*) FROM payroll WHERE ym = ?",
                                           (nxt,)),
                           lock_log=S.payroll_unlock_log(ym, 6),
                           staff_total=len(S.staff_rows(only_active=False)),
                           staff_active=len(S.staff_rows()))


@app.route("/payroll/<ym>/save", methods=["POST"])
@page_required("payroll", write=True)
def payroll_save(ym):
    """把本月测算结果定稿落库：之后可以逐人调整，重算不会覆盖。"""
    msg = S.payroll_block_msg(ym)
    if msg:
        flash(msg, "error")
        return redirect(url_for("payroll", ym=ym))
    n = S.payroll_finalize(ym, session.get("dname", ""))
    flash(f"本月工资已定稿：{ym}（{n} 人）。"
          "之后要改，先【解除定稿】——需要输入你的登录密码。", "ok")
    return redirect(url_for("payroll", ym=ym))


@app.route("/payroll/<ym>/generate-next", methods=["POST"])
@page_required("payroll", write=True)
def payroll_generate_next(ym):
    """一键生成下个月工资表：本月定稿的数字为基准，考勤项按下月重算。"""
    n, nxt = S.payroll_generate_next(ym, session.get("dname", ""))
    if n == 0:
        flash(f"{nxt} 的工资表已经存在了，没有覆盖 —— "
              f"请直接切到 {nxt} 查看和修改。", "warn")
    elif n < 0:
        flash(f"{ym} 还没定稿：先核对本月数字、点【定稿 {ym} 工资表】，"
              f"再生成 {nxt}。", "error")
    else:
        flash(f"已按 {ym} 的标准生成 {nxt} 的**草稿**（{n} 人）。"
              f"工资标准 / 补贴 / 社保公积金 照抄上月，"
              f"加班费 / 餐补 / 出差费 按 {nxt} 的考勤重算 —— "
              f"先看、逐人调整，核对完再点【定稿 {nxt} 工资表】。", "ok")
        return redirect(url_for("payroll", ym=nxt))
    return redirect(url_for("payroll", ym=ym))


@app.route("/payroll/<ym>/unfinalize", methods=["POST"])
@page_required("payroll", write=True)
def payroll_unfinalize(ym):
    """解除本月定稿 —— 必须输入当前登录账号的密码。"""
    pwd = request.form.get("password") or ""
    if not S.user_check_password(session.get("uid"), pwd):
        flash("密码不对，没有解除定稿。", "error")
        return redirect(url_for("payroll", ym=ym))
    ok, msg = S.payroll_unfinalize(ym, session.get("dname", ""),
                                   request.form.get("note") or "")
    flash(msg, "ok" if ok else "error")
    return redirect(url_for("payroll", ym=ym))


@app.route("/payroll/<ym>/unlock-draft", methods=["POST"])
@page_required("payroll", write=True)
def payroll_unlock_draft(ym):
    """解除定稿：重新按规则测算（手工调整的值会被覆盖）。"""
    msg = S.payroll_block_msg(ym)
    if msg:
        flash(msg, "error")
        return redirect(url_for("payroll", ym=ym))
    S.payroll_unlock_draft(ym)
    flash(f"已解除 {ym} 的定稿，现在会按规则重新测算（手工调整的值会被覆盖）", "warn")
    return redirect(url_for("payroll", ym=ym))


@app.route("/payroll/<ym>/<int:sid>/edit", methods=["GET", "POST"])
@page_required("payroll", write=True)
def payroll_edit(ym, sid):
    """逐人调整某个人的工资明细。"""
    msg = S.payroll_block_msg(ym)
    if msg:
        flash(msg, "error")
        return redirect(url_for("payroll", ym=ym))
    row, params = S.payroll_one(ym, sid)
    if row is None:
        abort(404)
    if request.method == "POST":
        S.payroll_update(ym, sid, request.form)
        flash(f"已保存 {row['name']} 的 {ym} 工资明细", "ok")
        return redirect(url_for("payroll", ym=ym))
    return render_template("payroll_edit.html", r=row, ym=ym, params=params,
                           cols=S.PAY_COLS, seal=S.payroll_seal_state(ym))


@app.route("/settings", methods=["GET", "POST"])
@admin_required
def settings_page():
    if request.method == "POST":
        pairs = {}
        for row in S.settings_all():
            k = row["key"]
            if k in request.form:
                pairs[k] = (request.form.get(k) or "").strip()
        try:
            S.settings_save(pairs)
            flash("参数已保存，工资测算会立即按新参数重算", "ok")
        except Exception as e:
            flash(f"保存失败：{e}", "error")
        return redirect(url_for("settings_page"))
    return render_template("settings.html", items=S.settings_all(),
                           params=S.payroll_params(),
                           months=S.recent_months())


# -------------------------------------------------------------- 数据备份
@app.route("/backup")
@page_required("backup")
def backup_page():
    """数据备份：一键备到「存储空间1」的默认目录，也可以自己选位置。

    备份文件写在挂载进来的宿主目录里（容器里的 /backup），
    所以关掉容器、换台机器都还能拿到 —— 放在容器里等于没备。
    """
    bk = S.backup_info(request.args.get("dir"))
    return render_template("backup.html", bk=bk, dir=bk["dir"], at=now_str())


@app.route("/backup/run", methods=["POST"])
@page_required("backup", write=True)
def backup_run():
    d = (request.form.get("dir") or "").strip()
    res, err = S.backup_create(d, actor=session.get("dname", ""))
    if err:
        flash("没备份成：" + err, "error")
        return redirect(url_for("backup_page", dir=d or None))
    msg = ("已备份 %s（%s，用了 %.1f 秒，库里 %d 张表）。文件在 %s"
           % (res["name"], res["size_h"], res["seconds"],
              len(res["tables"]), res["path"]))
    if res["rotated"]:
        msg += "；按保留策略删掉了 %d 份更旧的备份。" % len(res["rotated"])
    flash(msg, "ok")
    return redirect(url_for("backup_page", dir=res["path"]))


@app.route("/backup/download")
@page_required("backup")
def backup_download():
    """下载某份备份 —— 浏览器会让你自己选存在哪儿（存到本机/移动硬盘）。"""
    d, n = S.backup_file(request.args.get("dir"), request.args.get("name"))
    if not d:
        abort(404)
    return send_from_directory(d, n, as_attachment=True)


@app.route("/backup/delete", methods=["POST"])
@page_required("backup", write=True)
def backup_delete():
    d = (request.form.get("dir") or "").strip()
    name = request.form.get("name") or ""
    ok, err = S.backup_delete(d, name)
    flash(("已删除备份 %s" % name) if ok else ("删除失败：" + err),
          "ok" if ok else "error")
    return redirect(url_for("backup_page", dir=d or None))


# -------------------------------------------------------------- 合同全景
@app.route("/e/contracts/<int:rid>/profile")
@login_required
def contract_profile(rid):
    """合同全景：这张合同的开票、收付款、清单、送货与对私利润。

    从合同列表点合同编号、或合同表单上的按钮进来。
    沿用同一套权限与数据范围：员工看不到不属于自己的项目。
    """
    _require_item("e", "contracts")
    row = S.get_row("contracts", rid)
    if not row:
        abort(404)
    g = _scope_guard("contracts", row)
    if g:
        return g
    try:
        p = S.contract_profile(rid)
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("entity_list", key="contracts"))
    return render_template("contract_profile.html", p=p, key="contracts")


@app.route("/e/projects/<int:rid>/profile")
@login_required
def project_profile(rid):
    """项目全景：这张项目的合同、销项 / 进项发票、收付款、费用类别汇总与利润。

    首页「按项目经营情况」点项目名称、项目管理列表点项目编号进来。
    员工只能打开自己负责 / 被授权的项目（走 _scope_guard）。
    """
    _require_item("e", "projects")
    row = S.get_row("projects", rid)
    if not row:
        abort(404)
    g = _scope_guard("projects", row)
    if g:
        return g
    return render_template("project_profile.html", p=S.project_profile(rid),
                           key="projects")


# ---------------------------------------------------------- 对私利润汇总
@app.route("/profit")
@login_required
@page_required("profit")
def profit_summary():
    """对私结算的利润汇总：我方利润 + 返还居间人的总额（可按日期/状态筛）。"""
    df = (request.args.get("from") or "").strip() or None
    dt = (request.args.get("to") or "").strip() or None
    st = (request.args.get("status") or "").strip() or None
    return render_template("profit.html",
                           p=S.profit_summary(df, dt, st),
                           statuses=S.dict_options("对私状态"),
                           today=datetime.now().strftime("%Y-%m-%d"))


@app.route("/export/xlsx/profit")
@login_required
@page_required("profit")
def export_profit():
    df = (request.args.get("from") or "").strip() or None
    dt = (request.args.get("to") or "").strip() or None
    st = (request.args.get("status") or "").strip() or None
    p = S.profit_summary(df, dt, st)
    head = ["名称", "单据数", "收入", "我方利润", "成本", "返还居间人"]
    sheets = []
    for nm, rows in (("按居间人", p["by_broker"]), ("按合同", p["by_contract"]),
                     ("按项目", p["by_project"])):
        body = [[r["name"], r["n"], r["income"], r["profit"], r["cost"], r["payable"]]
                for r in rows]
        t = p["totals"]
        body.append(["合计", t["n"], t["income"], t["profit"], t["cost"], t["payable"]])
        sheets.append((nm, [head] + body, [22, 8, 14, 14, 14, 14]))
    detail_head = ["计算单号", "标题", "日期", "关联合同", "项目", "居间人", "状态",
                   "收入", "我方利润", "成本", "返还居间人"]
    detail = [[r["code"], r["title"], r["calc_date"], r.get("contracts") or "",
               r.get("project_name") or "", r.get("broker") or "", r["status"],
               r["income"], r["profit"], r["cost"], r["payable"]]
              for r in p["rows"]]
    t = p["totals"]
    detail.append(["合计", "", "", "", "", "", "", t["income"], t["profit"],
                   t["cost"], t["payable"]])
    sheets.append(("逐单明细", [detail_head] + detail,
                   [12, 22, 11, 16, 14, 12, 9, 13, 13, 12, 13]))
    data = S.make_xlsx(sheets)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    return Response(data, mimetype=MIME_XLSX,
                    headers=_attachment("对私利润汇总_%s.xlsx" % stamp))


# ------------------------------------------------------------------ 导出
@app.route("/export/xlsx/attendance")
@login_required
def export_attendance():
    ym = request.args.get("ym") or None
    sc = scope_of("overtime")
    scope_map = {"overtime": sc, "leaves": scope_of("leaves"),
                 "trips": scope_of("trips")}
    uid = cur_uid() if is_employee() else None
    sheets = S.attendance_sheets(ym=ym, scope=scope_map, uid=uid)
    data = S.make_xlsx(sheets)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    name = f"attendance_{ym or 'all'}_{stamp}.xlsx"
    return Response(data, mimetype="application/vnd.openxmlformats-"
                                   "officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition":
                             f"attachment; filename={name}"})


@app.route("/export/xlsx/payroll")
@page_required("payroll")
def export_payroll():
    ym = request.args.get("ym") or S.payroll_default_month()
    # 一个文件两个表：工资表（一行一人）+ 工资条（每人一块，裁开发放）
    data = S.payroll_workbook(ym, company=COMPANY_NAME)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    return Response(data, mimetype="application/vnd.openxmlformats-"
                                   "officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition":
                             f"attachment; filename=payroll_{ym}_{stamp}.xlsx"})


# ------------------------------------------------------------------ 项目授权
@app.route("/users/<int:uid>/access", methods=["GET", "POST"])
@admin_required
def user_access(uid):
    u = S.user_get(uid)
    if not u:
        abort(404)
    if request.method == "POST":
        ids = request.form.getlist("project_id")
        S.project_access_set(uid, ids)
        flash("项目授权已保存", "ok")
        return redirect(url_for("user_access", uid=uid))
    granted = {r["project_id"] for r in S.project_access_list(uid)}
    managed = {r["id"] for r in query(
        "SELECT id FROM projects WHERE manager_user_id = ?", (uid,))}
    return render_template("user_access.html", u=u,
                           projects=S.project_options(),
                           granted=granted, managed=managed)


# ------------------------------------------------------------------ 页面权限
@app.route("/users/<int:uid>/pages", methods=["GET", "POST"])
@admin_required
def user_pages(uid):
    """按账号放开 / 收起左侧菜单里的页面与模块（不配＝跟随角色默认）。"""
    u = S.user_get(uid)
    if not u:
        abort(404)
    is_admin_target = (u["role"] == S.ROLE_ADMIN)

    if request.method == "POST":
        if is_admin_target:
            flash("管理员账号始终拥有全部功能，不能通过页面权限限制", "error")
            return redirect(url_for("user_pages", uid=uid))
        choices = []
        level_of = {"read": S.PAGE_LVL_READ, "write": S.PAGE_LVL_WRITE,
                    "deny": S.PAGE_LVL_HIDE}
        for kind, key, _title, _gname in meta.perm_items():
            if not _override_applies_role(kind, key, u["role"]):
                continue
            v = (request.form.get("%s:%s" % (kind, key)) or "default").strip()
            choices.append((kind, key, level_of.get(v)))
        S.user_page_overrides_set(uid, choices)
        flash("页面权限已保存，该账号下次点开菜单即生效", "ok")
        return redirect(url_for("user_pages", uid=uid))

    overrides = S.user_page_overrides(uid)
    groups = []
    for gname, items in meta.perm_catalog():
        rows = []
        for kind, key, title in items:
            lvl = overrides.get((kind, key))
            default_visible = _default_item_visible(kind, key, u["role"])
            if lvl is None:
                state = "default"
                effective = "write" if default_visible else "hidden"
            else:
                state = {S.PAGE_LVL_HIDE: "deny", S.PAGE_LVL_READ: "read",
                         S.PAGE_LVL_WRITE: "write"}[lvl]
                effective = state
            rows.append({
                "kind": kind, "key": key, "title": title,
                "field": "%s:%s" % (kind, key),
                "default_visible": default_visible,
                "effective": effective,
                "state": state,
                "grantable": _override_applies_role(kind, key, u["role"]),
            })
        groups.append((gname, rows))
    return render_template("user_pages.html", u=u, groups=groups,
                           is_admin_target=is_admin_target,
                           changed=len(overrides))


# ------------------------------------------------------------------ API / 运维
@app.route("/api/invoices")
@login_required
def api_invoices():
    _require_item("e", "invoices")
    return jsonify(S.invoice_pick_list(scope=scope_of("invoices")))


@app.route("/healthz")
def healthz():
    """容器健康检查用；不返回任何系统信息。"""
    return jsonify({"status": "ok"})


@app.errorhandler(413)
def too_large(_e):
    flash(f"上传文件超过 {S.MAX_UPLOAD // 1024 // 1024} MB 限制", "error")
    return redirect(request.referrer or url_for("index"))


@app.errorhandler(403)
def forbidden(_e):
    return render_template("error.html", code=403,
                           msg="你没有权限访问这个页面"), 403


@app.errorhandler(404)
def not_found(_e):
    return render_template("error.html", code=404, msg="找不到这个页面"), 404


@app.errorhandler(500)
def server_error(_e):
    return render_template("error.html", code=500, msg="服务器内部错误，请查看容器日志"), 500


def bootstrap():
    fresh = init_db()
    if fresh:
        print("[init] 已创建数据库结构与基础字典数据")
    # 注意：db 查询依赖 Flask 应用上下文（g），这里在非请求阶段必须显式压入上下文，
    # 否则 python app.py 启动时会直接抛 Working outside of application context。
    with app.app_context():
        if not S.enabled_admin_exists():
            from db import _seed_admin, _connect
            conn = _connect()
            try:
                _seed_admin(conn)
                print("[init] 未发现可用管理员，已按环境变量创建默认管理员账号")
            finally:
                conn.close()


if __name__ == "__main__":
    bootstrap()
    port = int(os.environ.get("PORT", "8080"))
    host = os.environ.get("HOST", "0.0.0.0")
    debug = os.environ.get("DEBUG", "") == "1"
    if debug:
        # debug 模式会开 Werkzeug 调试器（等于一个远程代码执行入口）：
        # 没显式指定 HOST 时只绑本机，别让它跟着 0.0.0.0 一起暴露。
        dbg_host = os.environ.get("HOST") or "127.0.0.1"
        app.run(host=dbg_host, port=port, debug=True)  # nosec B201 - 开发模式自选
    else:
        from waitress import serve
        print(f"[start] http://{host}:{port}")
        serve(app, host=host, port=port, threads=8)
