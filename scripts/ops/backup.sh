#!/usr/bin/env bash
# 用途：经 IAP SSH 用 SQLite 在线备份 API 备份生产数据库和台账到本机。
set -euo pipefail

SSH_ALIAS="${SW_OPS_SSH_ALIAS:-${SW_TUNNEL_SSH_ALIAS:-workbench-iap}}"
LOCAL_BACKUP_ROOT="${SW_SERVER_BACKUP_DIR:-${HOME}/sw-server-backups}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOCAL_DIR="${LOCAL_BACKUP_ROOT}/${STAMP}"

die() { printf '\n✗ %s\n' "${1}" >&2; shift; for line in "$@"; do printf '  %s\n' "${line}" >&2; done; exit 1; }
note() { printf '  %s\n' "${1}"; }
ok() { printf '  ✓ %s\n' "${1}"; }

command -v ssh >/dev/null 2>&1 || die "本机没有 ssh 命令"
command -v tar >/dev/null 2>&1 || die "本机没有 tar 命令"
[[ "${STAMP}" =~ ^[0-9]{8}T[0-9]{6}Z$ ]] || die "生成的 UTC 时间戳格式异常：${STAMP}"
[[ ! -e "${LOCAL_DIR}" ]] || die "本机备份目录已存在：${LOCAL_DIR}" "请等待一秒后重试，避免覆盖已有备份。"

umask 077
mkdir -p "${LOCAL_BACKUP_ROOT}"
TEMP_DIR="$(mktemp -d "${LOCAL_BACKUP_ROOT}/.${STAMP}.XXXXXX")"

printf '生产 core 在线备份\n\n'
note "连接 ${SSH_ALIAS}，创建卷内一致性快照并拷回本机"
backup_stream() {
ssh -o ConnectTimeout=25 "${SSH_ALIAS}" "bash -s -- ${STAMP}" <<'REMOTE'
set -euo pipefail

stamp="${1}"
cd "${HOME}/social_workflow"

docker compose exec -T core python3 - "${stamp}" <<'PY'
import re
import sqlite3
import sys
from pathlib import Path

stamp = sys.argv[1]
data_dir = Path("/app/data")
source_path = data_dir / "social_workflow.db"
accounts_path = data_dir / "accounts.yaml"
backups_dir = data_dir / "backups"
target_path = backups_dir / ("sw-" + stamp + ".db")

if not source_path.is_file():
    raise SystemExit("数据库不存在：{}".format(source_path))
if not accounts_path.is_file():
    raise SystemExit("生产台账不存在：{}".format(accounts_path))

backups_dir.mkdir(parents=True, exist_ok=True)
if target_path.exists():
    print("卷内快照已存在，保留且拷出  {}".format(target_path), file=sys.stderr)
else:
    source = sqlite3.connect("file:{}?mode=ro".format(source_path), uri=True)
    target = sqlite3.connect(str(target_path))
    try:
        # sqlite3.Connection.backup 在源库仍有写入时生成事务一致的快照。
        source.backup(target)
    finally:
        target.close()
        source.close()

pattern = re.compile(r"sw-\d{8}T\d{6}Z\.db\Z")
managed = sorted(
    (path for path in backups_dir.iterdir() if path.is_file() and pattern.fullmatch(path.name)),
    key=lambda path: path.name,
    reverse=True,
)
removed = 0
for stale_path in managed[7:]:
    stale_path.unlink()
    removed += 1

print("卷内快照  {}  {} bytes".format(target_path, target_path.stat().st_size), file=sys.stderr)
print(
    "卷内轮转  保留 {} 份，删除 {} 份本脚本创建的旧快照".format(min(len(managed), 7), removed),
    file=sys.stderr,
)
PY
docker compose exec -T core python3 - "${stamp}" <<'PY'
import sys
import tarfile
from pathlib import Path

stamp = sys.argv[1]
files = (
    (Path("/app/data/backups") / ("sw-" + stamp + ".db"), "social_workflow.db"),
    (Path("/app/data/accounts.yaml"), "accounts.yaml"),
)
with tarfile.open(fileobj=sys.stdout.buffer, mode="w|") as archive:
    for source_path, archive_name in files:
        if not source_path.is_file():
            raise SystemExit("拷出源不存在：{}".format(source_path))
        archive.add(str(source_path), arcname=archive_name, recursive=False)
PY
REMOTE
}

copied=0
attempt=1
while [[ "${attempt}" -le 2 ]]; do
  if backup_stream | tar -xf - -C "${TEMP_DIR}"; then
    copied=1
    break
  fi
  if [[ "${attempt}" -lt 2 ]]; then
    note "IAP 连接中断，3 秒后重试一次"
    sleep 3
  fi
  attempt=$((attempt + 1))
done
[[ "${copied}" -eq 1 ]] || die "备份拷回失败，卷内快照未覆盖，本机临时目录保留：${TEMP_DIR}"

[[ -s "${TEMP_DIR}/social_workflow.db" ]] || die "拷出的数据库为空或缺失：${TEMP_DIR}/social_workflow.db"
[[ -f "${TEMP_DIR}/accounts.yaml" ]] || die "拷出的生产台账缺失：${TEMP_DIR}/accounts.yaml"
mv "${TEMP_DIR}" "${LOCAL_DIR}"

DB_BYTES="$(wc -c < "${LOCAL_DIR}/social_workflow.db" | tr -d '[:space:]')"
ACCOUNTS_BYTES="$(wc -c < "${LOCAL_DIR}/accounts.yaml" | tr -d '[:space:]')"
ok "本机备份完成：${LOCAL_DIR}"
note "数据库 ${DB_BYTES} bytes"
note "生产台账 ${ACCOUNTS_BYTES} bytes"
