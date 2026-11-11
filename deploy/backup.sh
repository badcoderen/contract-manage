#!/bin/sh
# ============================================================================
# 合同系统 · 一键备份
#
#   · 数据库用 SQLite 的在线备份（VACUUM INTO）—— **不用停服务**，
#     而且拿到的是一致快照。直接 tar 整个 data/ 在 WAL 模式下可能
#     把未合并的事务落下、甚至拿到"半张表"，别那么干。
#   · 附件（uploads/）单独打包，和库放在同一个备份目录里。
#   · 备份完做 integrity_check + 逐表行数清单，坏备份当场就能发现。
#   · 最后按保留策略轮转（默认留最近 14 份）。
#
# 用法：
#   sh backup.sh                  # 备份到默认目录
#   BK_DIR=/vol2/backup sh backup.sh
#   TAG=manual sh backup.sh       # 文件名带 manual 标记
#
# 定时（飞牛/NAS 上 root 的 crontab，每天 02:30）：
#   30 2 * * * /bin/sh /vol1/1000/docker/contract-app/deploy/backup.sh \
#              >> /vol1/1000/docker/_backup/backup.log 2>&1
# ============================================================================
set -eu

APP_DIR=${APP_DIR:-/vol1/1000/docker/contract-app}
DATA_DIR=${DATA_DIR:-$APP_DIR/data}
BK_DIR=${BK_DIR:-/vol1/1000/docker/_backup/contract-app}
KEEP=${KEEP:-14}
TAG=${TAG:-auto}
STAMP=$(date +%Y%m%d_%H%M%S)
NAME="contract-app_${STAMP}_${TAG}"

log() { echo "[$(date '+%F %T')] $*"; }

mkdir -p "$BK_DIR"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

log "备份开始 → $BK_DIR/$NAME"

# ---- 1. 数据库：在线一致性快照（不需要停容器）----
python3 - "$DATA_DIR/app.db" "$WORK/app.db" <<'PY'
import sqlite3, sys
src_path, out_path = sys.argv[1], sys.argv[2]
src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
dst = sqlite3.connect(out_path)
src.backup(dst)          # 一致性快照，WAL 里的内容一并合并进去
dst.close(); src.close()
PY
log "数据库快照完成：$(du -h "$WORK/app.db" | cut -f1)"

# ---- 2. 完整性校验 + 行数清单（坏备份要当场发现）----
python3 - "$WORK/app.db" "$WORK/manifest.txt" <<'PY'
import sqlite3, sys, datetime
db = sqlite3.connect(sys.argv[1])
ok = db.execute("PRAGMA integrity_check").fetchone()[0]
lines = ["integrity_check: %s" % ok,
         "生成时间: %s" % datetime.datetime.now().strftime("%F %T"), ""]
total = 0
for (t,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' "
                       "AND name NOT LIKE 'sqlite_%' ORDER BY name"):
    try:
        n = db.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
    except sqlite3.Error:
        continue
    total += n
    lines.append("%-28s %8d" % (t, n))
lines.append("%-28s %8d" % ("【合计】", total))
open(sys.argv[2], "w", encoding="utf-8").write("\n".join(lines) + "\n")
print("\n".join(lines))
if ok != "ok":
    sys.exit(2)
PY

# ---- 3. 附件 + 清单，一起打包 ----
if [ -d "$DATA_DIR/uploads" ]; then
    tar czf "$WORK/uploads.tgz" -C "$DATA_DIR" uploads
    log "附件打包：$(du -h "$WORK/uploads.tgz" | cut -f1)"
fi
tar czf "$BK_DIR/$NAME.tgz" -C "$WORK" app.db manifest.txt uploads.tgz 2>/dev/null \
    || tar czf "$BK_DIR/$NAME.tgz" -C "$WORK" app.db manifest.txt

log "备份完成：$BK_DIR/$NAME.tgz（$(du -h "$BK_DIR/$NAME.tgz" | cut -f1)）"

# ---- 4. 轮转：同前缀只留最近 KEEP 份 ----
cd "$BK_DIR"
ls -1t contract-app_*.tgz 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r f; do
    rm -f -- "$f"
    log "轮转：删掉旧备份 $f"
done

log "当前保留 $(ls -1 contract-app_*.tgz 2>/dev/null | wc -l) 份，最新：$(ls -1t contract-app_*.tgz | head -1)"
