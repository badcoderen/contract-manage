# -*- coding: utf-8 -*-
"""清空业务数据（正式启用前用），保留字典、费用类别与登录账号。

用法：
    python tools/clear_data.py            # 交互确认
    python tools/clear_data.py --yes      # 跳过确认

容器内：
    docker exec -it contract-app python tools/clear_data.py --yes

删除按"先子后父"顺序：核销 → 付款 → 发票 → 费用 → 合同 → 项目/往来单位。
附件文件同时从 uploads 目录移除。
"""
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import DB_PATH, UPLOAD_DIR, _connect, init_db  # noqa: E402

# 先子后父，避免外键约束报错
ORDER = ["settlements", "payments", "invoices", "costs", "contracts",
         "projects", "units", "attachments"]

KEEP = ("dicts", "cost_categories", "users")


def main():
    assume_yes = "--yes" in sys.argv
    init_db()
    conn = _connect()
    counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ORDER}

    print("将要清空的表：")
    for t in ORDER:
        print(f"  {t:<16} {counts[t]} 行")
    print("保留：dicts（下拉选项）、cost_categories（费用类别）、users（登录账号）")
    print(f"数据库：{DB_PATH}")
    print(f"附件目录：{UPLOAD_DIR}（文件将一并删除）")

    if not assume_yes:
        ans = input("确认清空？输入 yes 继续：").strip().lower()
        if ans != "yes":
            print("已取消。")
            conn.close()
            return

    for t in ORDER:
        conn.execute(f"DELETE FROM {t}")
    conn.commit()
    conn.close()

    if UPLOAD_DIR.exists():
        for f in UPLOAD_DIR.iterdir():
            try:
                if f.is_file():
                    f.unlink()
                elif f.is_dir():
                    shutil.rmtree(f)
            except OSError as e:
                print("  ! 删除失败", f.name, e)
    print("已清空业务数据（字典、费用类别、登录账号保留）。")
    print("提示：若想让自增编号从 1 重新开始，可删除 data/app.db 后重启，"
          "或执行 VACUUM（编号本身按 前缀+4位序号 计算，不受影响）。")


if __name__ == "__main__":
    main()
