#!/usr/bin/env bash
# 3dai-led 卸载:摘掉 hook、移除技能、熄灯。
#
# 默认保留数据目录里的 leases.json / debug.log(在 <repo>/data)—— 重装后槽位和
# 排查记录还在。要连数据一起删,加 --purge。仓库本身不动,删不删由你。

set -euo pipefail

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
SKILL_SRC="$REPO/skills/3dai-led"
LED_SH="$SKILL_SRC/led.sh"
HOOKS_CFG="$REPO/scripts/hooks_config.py"
CODEX_HOOKS_CFG="$REPO/scripts/codex_hooks_config.py"

SETTINGS="${HOME}/.claude/settings.json"
CODEX_HOOKS="${CODEX_HOME:-${HOME}/.codex}/hooks.json"
SKILL_DST="${HOME}/.claude/skills/3dai-led"
DATA_DIR="${LED_DATA_DIR:-$REPO/data}"
PURGE=""
KEEP_LIT=""
DRY_RUN=""

usage() {
  cat <<EOF
用法: ./uninstall.sh [选项]

  --purge            连数据目录一起删($DATA_DIR)
  --keep-lit         不熄灯(默认会先跑一次 led.sh reset 清空租约并熄灭全部灯珠)
  --skill-dst <path> 技能安装位置,默认 ~/.claude/skills/3dai-led
  --data-dir <path>  数据目录,默认 <repo>/data
  --settings <path>  目标 settings.json,默认 ~/.claude/settings.json
  --codex-hooks <path> 目标 Codex hooks.json,默认 ~/.codex/hooks.json
  --dry-run          只打印将要做的改动,不落盘
  -h, --help         显示本帮助
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --purge)     PURGE=1; shift ;;
    --keep-lit)  KEEP_LIT=1; shift ;;
    --skill-dst) SKILL_DST="${2:?--skill-dst 需要一个路径}"; shift 2 ;;
    --data-dir)  DATA_DIR="${2:?--data-dir 需要一个路径}"; shift 2 ;;
    --settings)  SETTINGS="${2:?--settings 需要一个路径}"; shift 2 ;;
    --codex-hooks) CODEX_HOOKS="${2:?--codex-hooks 需要一个路径}"; shift 2 ;;
    --dry-run)   DRY_RUN=1; shift ;;
    -h|--help)   usage; exit 0 ;;
    *) echo "未知参数: $1" >&2; usage >&2; exit 2 ;;
  esac
done

ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
step() { printf '\n\033[1m%s\033[0m\n' "$*"; }
run()  { [ -n "$DRY_RUN" ] && { printf '  [dry-run] %s\n' "$*"; return 0; }; eval "$@"; }

PY="${LED_PYTHON:-$(command -v python3 2>/dev/null || echo /usr/bin/python3)}"

# ---------- 1. 熄灯 ----------
# 必须赶在摘 hook 和删文件之前:一旦 led.sh 不可用,设备上就会留着几颗
# 没人负责熄掉的灯,只能等 idle_timeout 或手动 curl。
step "熄灭全部灯珠"
if [ -n "$KEEP_LIT" ]; then
  ok "跳过(--keep-lit)"
elif [ -x "$LED_SH" ]; then
  if [ -n "$DRY_RUN" ]; then
    printf '  [dry-run] %s reset\n' "$LED_SH"
  else
    LED_DATA_DIR="$DATA_DIR" "$LED_SH" reset 2>&1 | sed 's/^/  /' \
      || warn "reset 失败(设备不可达?),继续卸载"
  fi
else
  warn "$LED_SH 不可用,跳过熄灯"
fi

# ---------- 2. hooks ----------
step "清理 settings.json"
if [ -f "$SETTINGS" ] && [ -f "$HOOKS_CFG" ]; then
  CFG_ARGS=(remove --settings "$SETTINGS")
  [ -n "$DRY_RUN" ] && CFG_ARGS+=(--dry-run)
  printf '  '
  "$PY" "$HOOKS_CFG" "${CFG_ARGS[@]}"
else
  warn "找不到 $SETTINGS 或 hooks_config.py,跳过"
fi

step "清理 Codex hooks.json"
if [ -f "$CODEX_HOOKS" ] && [ -f "$CODEX_HOOKS_CFG" ]; then
  CODEX_CFG_ARGS=(remove --hooks "$CODEX_HOOKS")
  [ -n "$DRY_RUN" ] && CODEX_CFG_ARGS+=(--dry-run)
  printf '  '
  "$PY" "$CODEX_HOOKS_CFG" "${CODEX_CFG_ARGS[@]}"
else
  warn "找不到 $CODEX_HOOKS 或 codex_hooks_config.py,跳过"
fi

# ---------- 3. 技能 ----------
step "移除技能"
if [ -L "$SKILL_DST" ]; then
  run "rm -f '$SKILL_DST'"
  ok "移除软链接 $SKILL_DST"
elif [ -d "$SKILL_DST" ]; then
  # 只删我们装的那份。目录里若混进了别的文件,保留并提示,不替用户做主
  leftover=$(find "$SKILL_DST" -mindepth 1 ! -name SKILL.md | head -1 || true)
  if [ -n "$leftover" ]; then
    run "rm -f '$SKILL_DST/SKILL.md'"
    warn "$SKILL_DST 里还有其他文件,只删了 SKILL.md,目录保留"
  else
    run "rm -rf '$SKILL_DST'"
    ok "删除 $SKILL_DST"
  fi
else
  ok "技能未安装"
fi

# ---------- 4. 数据 ----------
step "数据目录"
if [ ! -d "$DATA_DIR" ]; then
  ok "$DATA_DIR 不存在"
elif [ -n "$PURGE" ]; then
  run "rm -rf '$DATA_DIR'"
  ok "已删除 $DATA_DIR"
else
  ok "保留 $DATA_DIR(要删加 --purge)"
fi

step "完成"
if [ -n "$DRY_RUN" ]; then
  echo "  [dry-run] 以上改动均未落盘"
else
  echo "  仓库本身未改动:$REPO"
  echo "  分别在 Claude Code / Codex 里打开一次 /hooks,重载配置。"
fi
