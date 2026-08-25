#!/usr/bin/env bash
# 起一份**真实**的 core 给 Playwright 用（不是 mock）。
#
#   bash e2e/serve.sh <port> [ui-token]
#
# - 独立的临时 SQLite 库，不碰开发库
# - 调度器关掉（后台线程会在用例中间改数据）
# - fake 发布器（什么都不会真的发出去）
# - **台账用副本**：P10 起「添加账号」会回写 accounts.yaml，用例绝不能改仓库里那份
# - sidecar 驱动 none：本机与 CI 没有 docker daemon，界面要如实显示"未接入"
# - 生图关掉：e2e 不许真调网关烧钱，界面要如实显示"配图开关为什么是灰的"
# - 把 ui/out 挂到 /workbench：测的就是最终部署形态
set -euo pipefail

valid_port() {
  [[ "$1" =~ ^[0-9]+$ ]]
}

PYTHON_BIN="$(command -v python3 || true)"
readonly PYTHON_BIN
[[ -n "$PYTHON_BIN" ]] || {
  printf '缺少 python3，无法安全建立 e2e 临时目录\n' >&2
  exit 1
}

secure_data_root() {
  local data_root="$1"
  local expected_uid="${2:-}"

  "$PYTHON_BIN" - "$data_root" "$expected_uid" <<'PY'
import os
import stat
import sys


def reject(message: str) -> None:
    print(f"拒绝 e2e 临时根：{message}", file=sys.stderr)
    raise SystemExit(1)


data_root = sys.argv[1]
if not os.path.isabs(data_root) or os.path.normpath(data_root) != data_root:
    reject("路径必须是规范化绝对路径")

try:
    expected_uid = int(sys.argv[2]) if sys.argv[2] else os.geteuid()
except ValueError:
    reject("owner uid 测试参数非法")

parent, leaf = os.path.split(data_root)
if not parent or not leaf:
    reject("路径不能是文件系统根")

required_flags = os.O_RDONLY | os.O_DIRECTORY
no_follow = getattr(os, "O_NOFOLLOW", None)
if no_follow is None:
    reject("当前平台不支持 O_NOFOLLOW")
required_flags |= no_follow | getattr(os, "O_CLOEXEC", 0)

try:
    parent_fd = os.open(parent, required_flags)
except OSError as exc:
    reject(f"无法无跟随打开父目录：{exc.strerror}")

try:
    parent_fd_stat = os.fstat(parent_fd)
    try:
        parent_path_stat = os.stat(parent, follow_symlinks=False)
    except OSError as exc:
        reject(f"无法检查父目录：{exc.strerror}")
    if not stat.S_ISDIR(parent_path_stat.st_mode):
        reject("父路径不是实目录")
    if (parent_fd_stat.st_dev, parent_fd_stat.st_ino) != (
        parent_path_stat.st_dev,
        parent_path_stat.st_ino,
    ):
        reject("父目录在检查期间发生变化")
    if os.path.realpath(parent) != parent:
        reject("父目录物理路径不精确")

    try:
        root_stat = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        try:
            os.mkdir(leaf, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            # 并发创建者获胜时，继续走同一套 lstat/owner 校验。
            pass
        except OSError as exc:
            reject(f"无法创建临时根：{exc.strerror}")
        try:
            root_stat = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as exc:
            reject(f"创建后无法检查临时根：{exc.strerror}")
    except OSError as exc:
        reject(f"无法检查临时根：{exc.strerror}")

    if not stat.S_ISDIR(root_stat.st_mode):
        reject("目标不是实目录（可能是符号链接或普通文件）")
    if root_stat.st_uid != expected_uid:
        reject(
            f"owner uid 不匹配（实际 {root_stat.st_uid}，期望 {expected_uid}）"
        )

    try:
        root_fd = os.open(leaf, required_flags, dir_fd=parent_fd)
    except OSError as exc:
        reject(f"无法无跟随打开临时根：{exc.strerror}")

    try:
        opened_stat = os.fstat(root_fd)
        if not stat.S_ISDIR(opened_stat.st_mode):
            reject("打开的临时根不是目录")
        if opened_stat.st_uid != expected_uid:
            reject("打开后 owner uid 发生变化")
        if (opened_stat.st_dev, opened_stat.st_ino) != (
            root_stat.st_dev,
            root_stat.st_ino,
        ):
            reject("临时根在 lstat/open 之间发生变化")

        try:
            path_stat = os.lstat(data_root)
        except OSError as exc:
            reject(f"无法复核临时根路径：{exc.strerror}")
        if (path_stat.st_dev, path_stat.st_ino) != (
            opened_stat.st_dev,
            opened_stat.st_ino,
        ):
            reject("临时根路径在检查期间发生变化")
        if os.path.realpath(data_root) != data_root:
            reject("临时根物理路径不精确")

        # 只对已经通过 no-follow、owner、inode 和物理路径检查的 fd 收紧权限。
        os.fchmod(root_fd, 0o700)
        secured_stat = os.fstat(root_fd)
        if stat.S_IMODE(secured_stat.st_mode) != 0o700:
            reject("临时根权限无法收紧到 0700")

        final_stat = os.lstat(data_root)
        if (final_stat.st_dev, final_stat.st_ino) != (
            secured_stat.st_dev,
            secured_stat.st_ino,
        ):
            reject("临时根在权限收紧后发生变化")
    finally:
        os.close(root_fd)
finally:
    os.close(parent_fd)
PY
}

prepare_data_dir() {
  local data_root="$1"
  local port="$2"
  local expected_uid="${3:-}"
  local candidate child

  valid_port "$port" || return 2
  secure_data_root "$data_root" "$expected_uid" || return

  # 只有临时根完成 no-follow/owner/0700 校验后才构造删除目标。
  candidate="$data_root/core-$port"
  child="${candidate##*/}"
  [[ "${candidate%/*}" == "$data_root" && "$child" =~ ^core-[0-9]+$ ]] || {
    printf '非法 e2e 数据目录\n' >&2
    return 2
  }
  rm -rf -- "$candidate"
  mkdir -- "$candidate"
  PREPARED_DATA_DIR="$candidate"
}

file_mode() {
  "$PYTHON_BIN" - "$1" <<'PY'
import os
import stat
import sys

print(f"{stat.S_IMODE(os.lstat(sys.argv[1]).st_mode):04o}")
PY
}

run_self_test() (
  local sandbox symlink_root symlink_target regular_root wrong_owner_root
  local secure_root before_mode fake_uid

  for invalid in "" "4x" "/" ".." "4174/../../victim" "../x"; do
    if valid_port "$invalid"; then
      printf '临时目录安全自检失败：错误接受端口 %q\n' "$invalid" >&2
      exit 1
    fi
  done
  valid_port "4174"

  sandbox="$(mktemp -d "${TMPDIR:-/tmp}/social-workflow-e2e-self-test.XXXXXX")"
  sandbox="$(cd -P "$sandbox" && pwd)"
  trap 'rm -rf -- "$sandbox"' EXIT

  # DATA_ROOT 为 symlink：目标目录权限与内容都不能被 chmod/rm 触碰。
  symlink_target="$sandbox/symlink-target"
  symlink_root="$sandbox/symlink-root"
  mkdir -p -- "$symlink_target/core-4174"
  printf '不可删除\n' > "$symlink_target/core-4174/sentinel"
  chmod 0751 "$symlink_target"
  before_mode="$(file_mode "$symlink_target")"
  ln -s -- "$symlink_target" "$symlink_root"
  if (prepare_data_dir "$symlink_root" "4174") > /dev/null 2>&1; then
    printf '临时目录安全自检失败：接受了 DATA_ROOT 符号链接\n' >&2
    exit 1
  fi
  [[ -f "$symlink_target/core-4174/sentinel" ]]
  [[ "$(file_mode "$symlink_target")" == "$before_mode" ]]

  # DATA_ROOT 为普通文件：内容与权限必须保持不变。
  regular_root="$sandbox/regular-root"
  printf '不可改写\n' > "$regular_root"
  chmod 0640 "$regular_root"
  before_mode="$(file_mode "$regular_root")"
  if (prepare_data_dir "$regular_root" "4174") > /dev/null 2>&1; then
    printf '临时目录安全自检失败：接受了普通文件 DATA_ROOT\n' >&2
    exit 1
  fi
  [[ "$(file_mode "$regular_root")" == "$before_mode" ]]
  [[ "$(<"$regular_root")" == "不可改写" ]]

  # 无需特权 chown：把期望 uid 注入为另一个值，审计 owner 拒绝分支及零副作用。
  wrong_owner_root="$sandbox/wrong-owner-root"
  mkdir -p -- "$wrong_owner_root/core-4174"
  printf '不可删除\n' > "$wrong_owner_root/core-4174/sentinel"
  chmod 0755 "$wrong_owner_root"
  before_mode="$(file_mode "$wrong_owner_root")"
  fake_uid="$(( $(id -u) + 1 ))"
  if (prepare_data_dir "$wrong_owner_root" "4174" "$fake_uid") > /dev/null 2>&1; then
    printf '临时目录安全自检失败：接受了错误 owner\n' >&2
    exit 1
  fi
  [[ -f "$wrong_owner_root/core-4174/sentinel" ]]
  [[ "$(file_mode "$wrong_owner_root")" == "$before_mode" ]]

  # 当前用户的宽权限实目录会先收紧，再且仅清理合法 core-<digits> 子目录。
  secure_root="$sandbox/secure-root"
  mkdir -p -- "$secure_root/core-4174" "$secure_root/core-41740"
  printf '应删除\n' > "$secure_root/core-4174/old"
  printf '不可删除\n' > "$secure_root/core-41740/sentinel"
  printf '不可删除\n' > "$sandbox/outside-sentinel"
  chmod 0777 "$secure_root"
  prepare_data_dir "$secure_root" "4174"
  [[ "$(file_mode "$secure_root")" == "0700" ]]
  [[ -d "$secure_root/core-4174" && ! -e "$secure_root/core-4174/old" ]]
  [[ -f "$secure_root/core-41740/sentinel" ]]
  [[ -f "$sandbox/outside-sentinel" ]]

  printf '临时目录安全自检通过\n'
)

if [[ "${1:-}" == "--self-test" ]]; then
  run_self_test
  exit 0
fi

PORT="${1:-}"
if ! valid_port "$PORT"; then
  printf '用法: serve.sh <port> [ui-token]；port 必须是完整十进制数字\n' >&2
  exit 2
fi
TOKEN="${2:-}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
readonly DATA_ROOT="/private/tmp/social-workflow-e2e"
PREPARED_DATA_DIR=""
prepare_data_dir "$DATA_ROOT" "$PORT"
readonly DATA_DIR="$PREPARED_DATA_DIR"
# 每个实例一份台账副本。用例建的号落在副本里，跑完随 DATA_DIR 一起被清掉
cp -- "$ROOT/accounts.yaml" "$DATA_DIR/accounts.yaml"

cd "$ROOT"
ENV_FILE="$DATA_DIR/e2e.env"
cat > "$ENV_FILE" <<EOF
# 此文件只供 Playwright 的真实 core 实例使用；不读取工作树的 .env。
SW_ENV=dev
SW_DATABASE_URL=sqlite:////private/tmp/social-workflow-e2e/core-$PORT/e2e.db
SW_ACCOUNTS_FILE=/private/tmp/social-workflow-e2e/core-$PORT/accounts.yaml
SW_UI_DIST=ui/out
SW_USE_FAKE_PUBLISHERS=true
SW_SCHEDULER_ENABLED=false
SW_SYNC_ACCOUNTS_ON_START=true
SW_SIDECAR_DRIVER=none
SW_IMAGEGEN_ENABLED=false
SW_TELEGRAM_ENABLED=false
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
FEISHU_WEBHOOK=
SW_LLM_BACKEND=anthropic
# 留空会让 /dev/* 联调走 ScriptedLLM，不能放一个会触发真实 API 请求的占位 key。
ANTHROPIC_API_KEY=
LLM_MODEL=claude-e2e-safe
SW_DSH_PROVIDER=deepseek-official
SW_DSH_MODEL=e2e-safe-model
SW_DSH_DEEPSEEK_BASE_URL=http://127.0.0.1:9
DEEPSEEK_API_KEY=
SW_DSH_GATEWAY_API_KEY=
SW_DSH_GATEWAY_BASE_URL=
SW_DSH_GATEWAY_MODEL=
WECHAT_APP_ID=
WECHAT_APP_SECRET=
PEXELS_API_KEY=
PIXABAY_API_KEY=
MPT_BASE_URL=http://127.0.0.1:9
TRENDRADAR_BASE_URL=
DOUYIN_SERVICE_URL=http://127.0.0.1:9
SW_TELEGRAM_STATE_FILE=/private/tmp/social-workflow-e2e/core-$PORT/telegram_state.json
EOF

# 走查截图必须同时固定浏览器与 core 的时钟；允许验收时从外部切到另一日期。
TIME_ANCHOR="${SW_E2E_TIME_ANCHOR:-2026-08-19T11:00:00.000Z}"
# 用空环境启动，避免工作树 .env 以外的宿主机变量也盖进 E2E 配置。时间锚点
# 与 fake 发布器由 core.models 在导入时直接读取，因此必须留在真实进程环境。
exec env -i \
  PATH="$PATH" \
  HOME="$HOME" \
  TMPDIR="${TMPDIR:-/tmp}" \
  SW_CONFIG_ENV_FILE="$ENV_FILE" \
  SW_USE_FAKE_PUBLISHERS=true \
  SW_E2E_TIME_ANCHOR="$TIME_ANCHOR" \
  SW_UI_TOKEN="$TOKEN" \
  SW_UI_DIST=ui/out \
  uv run uvicorn core.main:app --host 127.0.0.1 --port "$PORT" --log-level warning
