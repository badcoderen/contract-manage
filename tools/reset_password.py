# -*- coding: utf-8 -*-
"""管理登录账号：查看 / 重置密码 / 新增 / 改角色 / 停用。

用法：
    python tools/reset_password.py                       # 列出所有账号
    python tools/reset_password.py admin 新密码            # 重置密码（本机直接生效）
    python tools/reset_password.py admin 新密码 --must-change   # 并要求下次登录改密
    python tools/reset_password.py zhangsan 密码 --role admin --name 张三
    python tools/reset_password.py zhangsan --disable      # 停用账号

容器内：
    docker exec -it contract-app python tools/reset_password.py admin 新密码

提示：本机跑时如果服务正在运行，账号数据同时被服务使用没问题（SQLite 支持多进程），
改完密码后到登录页重新登录即可。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import services as S                            # noqa: E402
from db import DB_PATH, _connect, init_db       # noqa: E402


# 需要带值的开关（其余 --xxx 都是布尔开关）
VALUE_FLAGS = {"--role", "--name"}


def flag(name):
    return name in sys.argv


def value_of(name, cast=str):
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            return cast(sys.argv[i + 1])
    return None


def positional():
    """取出位置参数（用户名、密码），跳过开关及其取值。"""
    out, i = [], 1
    while i < len(sys.argv):
        a = sys.argv[i]
        if a in VALUE_FLAGS:
            i += 2
            continue
        if a.startswith("--"):
            i += 1
            continue
        out.append(a)
        i += 1
    return out


def main():
    init_db()
    conn = _connect()
    conn.row_factory = None
    users = conn.execute(
        "SELECT id, username, display_name, role, enabled, must_change_pwd, last_login_at "
        "FROM users ORDER BY role, username").fetchall()

    args = positional()
    if not args:
        print(f"数据库：{DB_PATH}\n当前账号（{len(users)} 个）：")
        for u in users:
            print("  #%d  %-16s %-10s %-8s %s%s%s" % (
                u[0], u[1], u[2] or "", S.ROLE_LABELS.get(u[3], u[3]),
                "启用" if u[4] else "已停用",
                "  待本人改密" if u[5] else "",
                f"  最后登录 {u[6]}" if u[6] else ""))
        print("\n重置密码：python tools/reset_password.py <用户名> <新密码> [--must-change]")
        conn.close()
        return

    username = args[0]
    row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    cols = ([d[0] for d in conn.execute(
        "SELECT * FROM users LIMIT 1").description] if row else [])
    d = dict(zip(cols, row)) if row else None
    has_pwd = len(args) >= 2

    if not row and not has_pwd:
        raise SystemExit(
            f"账号 {username} 不存在。\n"
            f"创建它：python tools/reset_password.py {username} <新密码> "
            f"[--role admin|editor] [--name 姓名]")

    role = value_of("--role") or (d["role"] if d else S.ROLE_EDITOR)
    if role not in S.ROLES:
        raise SystemExit("角色只能是 admin 或 editor")
    name = value_of("--name") or (d["display_name"] if d else username)
    enabled = 0 if flag("--disable") else 1
    if d and d["role"] == S.ROLE_ADMIN and (role != S.ROLE_ADMIN or not enabled):
        n = conn.execute("SELECT COUNT(*) FROM users WHERE role='admin' "
                         "AND enabled=1").fetchone()[0]
        if n <= 1:
            raise SystemExit("至少要保留一个启用状态的管理员账号")

    from werkzeug.security import generate_password_hash

    if has_pwd:
        pwd = args[1]
        err = S.password_problem(pwd)
        if err:
            raise SystemExit("密码不合规：" + err)
        must = 1 if flag("--must-change") else 0
        if row:
            conn.execute("UPDATE users SET password_hash=?, role=?, display_name=?, "
                         "enabled=?, must_change_pwd=?, updated_at=? WHERE id=?",
                         (generate_password_hash(pwd), role, name, enabled, must,
                          S.now_str(), d["id"]))
            print(f"已重置 {username} 的密码（角色 {S.ROLE_LABELS[role]}）"
                  + ("，下次登录需自己改密" if must else ""))
        else:
            conn.execute(
                "INSERT INTO users (username, password_hash, display_name, role, enabled, "
                "must_change_pwd, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (username, generate_password_hash(pwd), name, role, 1, must,
                 S.now_str(), S.now_str()))
            print(f"已创建账号 {username}（{S.ROLE_LABELS[role]}）"
                  + ("，下次登录需自己改密" if must else ""))
    else:
        conn.execute("UPDATE users SET display_name=?, role=?, enabled=?, updated_at=? "
                     "WHERE id=?", (name, role, enabled, S.now_str(), d["id"]))
        print(f"已更新 {username}：角色={S.ROLE_LABELS[role]}，"
              f"{'启用' if enabled else '停用'}，姓名={name}")

    conn.commit()
    conn.close()


if __name__ == "__main__":
    main()
