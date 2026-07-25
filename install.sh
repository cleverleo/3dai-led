#!/usr/bin/env bash
# 3dai-led 安装:把本仓库接进 Claude Code。
#
# 采用"就地引用"—— hook 里写的是本仓库中 led.sh 的绝对路径,不复制、不做软链接。
# 改了代码立刻生效,代价是仓库不能挪窝:移动或删除之后要重跑一次本脚本。
#
# 代码和数据都留在仓库里(数据落 <repo>/data/,已 gitignore),~/.claude 下只留两样
# 东西 —— settings.json 里的 hook,和 Claude Code 强制要求的技能加载点。换个编码
# 工具接入时,前者换成那个工具的配置即可,其余原样不动。
#
# SKILL.md 是那个加载点:Claude Code 只从 ~/.claude/skills/ 读技能,那里放不下一个
# "指针"。默认复制过去(纯文档,无路径依赖),改了 SKILL.md 重跑本脚本即可;想要它
# 跟着仓库自动更新就用 --skill link,不接 Claude Code 就用 --skill skip。

set -euo pipefail

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
SKILL_SRC="$REPO/skills/3dai-led"
LED_SH="$SKILL_SRC/led.sh"
HOOKS_CFG="$REPO/scripts/hooks_config.py"

SETTINGS="${HOME}/.claude/settings.json"
SKILL_DST="${HOME}/.claude/skills/3dai-led"
DEFAULT_DATA_DIR="$REPO/data"
DATA_DIR="$DEFAULT_DATA_DIR"
LEGACY_DIR="${HOME}/.claude/3dai-led"   # 旧版把脚本和数据都塞在这
HOST=""
SKILL_MODE="copy"
DRY_RUN=""

usage() {
  cat <<EOF
用法: ./install.sh [选项]

  --host <ip>            设备地址,写进 settings.json 的 env.LED_HOST
                         (不给则保留现有值;从未配过时落占位符 192.168.1.100,
                          那多半不是你的设备,首次安装建议显式指定)
  --skill copy|link|skip SKILL.md 的安装方式,默认 copy
  --skill-dst <path>     技能安装位置,默认 ~/.claude/skills/3dai-led
  --data-dir <path>      租约表和日志的目录,默认 <repo>/data
  --settings <path>      目标 settings.json,默认 ~/.claude/settings.json
  --dry-run              只打印将要做的改动,不落盘
  -h, --help             显示本帮助

装完用 ./uninstall.sh 卸载。
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --host)      HOST="${2:?--host 需要一个值}"; shift 2 ;;
    --skill)     SKILL_MODE="${2:?--skill 需要 copy|link|skip}"; shift 2 ;;
    --skill-dst) SKILL_DST="${2:?--skill-dst 需要一个路径}"; shift 2 ;;
    --data-dir)  DATA_DIR="${2:?--data-dir 需要一个路径}"; shift 2 ;;
    --settings)  SETTINGS="${2:?--settings 需要一个路径}"; shift 2 ;;
    --dry-run)   DRY_RUN=1; shift ;;
    -h|--help)   usage; exit 0 ;;
    *) echo "未知参数: $1" >&2; usage >&2; exit 2 ;;
  esac
done

case "$SKILL_MODE" in
  copy|link|skip) ;;
  *) echo "--skill 只接受 copy|link|skip,收到: $SKILL_MODE" >&2; exit 2 ;;
esac

ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$*"; }
step() { printf '\n\033[1m%s\033[0m\n' "$*"; }
run()  { [ -n "$DRY_RUN" ] && { printf '  [dry-run] %s\n' "$*"; return 0; }; eval "$@"; }
# dry-run 下 run 已经把命令打出来了,再补一行 ✓ 只会让人以为真做了
okr()  { [ -n "$DRY_RUN" ] || ok "$@"; }

# ---------- 1. 前置检查 ----------
step "检查环境"

for f in "$LED_SH" "$SKILL_SRC/lease.py" "$HOOKS_CFG"; do
  [ -f "$f" ] || { bad "缺少 ${f#$REPO/} —— 仓库不完整?"; exit 1; }
done
ok "仓库文件齐全($REPO)"

PY="${LED_PYTHON:-$(command -v python3 2>/dev/null || echo /usr/bin/python3)}"
if [ ! -x "$PY" ]; then
  bad "找不到可执行的 python3(lease.py 依赖它)。装好后重跑,或用 LED_PYTHON 指定"
  exit 1
fi
ok "python3: $PY ($("$PY" -V 2>&1))"

# hook 由 Claude Code 直接 exec,不经过 shell 的 PATH 查找,所以 +x 是硬要求
[ -x "$LED_SH" ] || run "chmod +x '$LED_SH'"
ok "led.sh 可执行"

# ---------- 2. 数据目录 ----------
step "准备数据目录"
run "mkdir -p '$DATA_DIR'"
okr "$DATA_DIR"
[ "$DATA_DIR" = "$DEFAULT_DATA_DIR" ] || ok "非默认路径,会写进 settings.json 的 env.LED_DATA_DIR"

# ---------- 3. 清理旧安装 ----------
# 旧版把脚本和数据一起塞在 ~/.claude/3dai-led。脚本副本必须清掉 —— 留着的话,
# 哪天有人手动敲了那个路径,跑的就是一份没人维护的旧代码;租约表和日志则搬到新
# 数据目录,槽位分配和排查记录不会因为这次搬家断掉。
step "清理 ~/.claude 下的旧安装"
if [ ! -d "$LEGACY_DIR" ]; then
  ok "没有需要清理的残留"
else
  for name in led.sh lease.py; do
    legacy="$LEGACY_DIR/$name"
    [ -e "$legacy" ] || [ -L "$legacy" ] || continue
    if [ -L "$legacy" ]; then
      run "rm -f '$legacy'"
      okr "移除软链接 $legacy"
    else
      run "mv '$legacy' '$legacy.bak-$(date +%Y%m%d-%H%M%S)'"
      warn "$legacy 是普通文件,已改名备份(未删除)"
    fi
  done

  if [ "$DATA_DIR" != "$LEGACY_DIR" ]; then
    for name in leases.json debug.log; do
      [ -f "$LEGACY_DIR/$name" ] || continue
      if [ -e "$DATA_DIR/$name" ]; then
        # 新位置已经在用了(比如上一轮装完就跑过几次)。哪份更新无从判断,所以旧的
        # 不覆盖也不就地留着 —— 改名搬走,数据不丢,旧目录也能清空
        keep="$DATA_DIR/$name.old-$(date +%Y%m%d-%H%M%S)"
        run "mv '$LEGACY_DIR/$name' '$keep'"
        warn "$DATA_DIR/$name 已存在,旧的那份改名搬到 $(basename "$keep")"
      else
        run "mv '$LEGACY_DIR/$name' '$DATA_DIR/$name'"
        okr "迁移 $name → $DATA_DIR"
      fi
    done
  fi

  # 只在空了的时候删,里面若还有别的东西就留着并说一声,不替用户做主
  if [ -n "$DRY_RUN" ]; then
    printf '  [dry-run] rmdir %s(仅当已清空)\n' "$LEGACY_DIR"
  elif rmdir "$LEGACY_DIR" 2>/dev/null; then
    ok "移除空目录 $LEGACY_DIR"
  else
    warn "$LEGACY_DIR 里还有其他文件,已保留"
  fi
fi

# ---------- 4. 技能 ----------
step "安装技能 SKILL.md"
case "$SKILL_MODE" in
  copy)
    run "mkdir -p '$SKILL_DST'"
    # 目标可能是上一轮 --skill link 留下的软链接,先摘掉再复制
    [ -L "$SKILL_DST/SKILL.md" ] && run "rm -f '$SKILL_DST/SKILL.md'"
    run "cp '$SKILL_SRC/SKILL.md' '$SKILL_DST/SKILL.md'"
    okr "复制到 $SKILL_DST/SKILL.md"
    warn "这是副本 —— 改了仓库里的 SKILL.md 要重跑 ./install.sh"
    ;;
  link)
    [ -e "$SKILL_DST" ] && [ ! -L "$SKILL_DST" ] && run "rm -rf '$SKILL_DST'"
    run "ln -sfn '$SKILL_SRC' '$SKILL_DST'"
    okr "软链接 $SKILL_DST -> $SKILL_SRC"
    ;;
  skip)
    ok "跳过(--skill skip)"
    ;;
esac

# ---------- 5. hooks ----------
step "写入 settings.json"

# 已有的 LED_HOST:没给 --host 时沿用它,一次都没配过才落默认值
EXISTING_HOST=$("$PY" - "$SETTINGS" <<'PYEOF' 2>/dev/null || true
import json, sys
try:
    with open(sys.argv[1]) as f:
        print((json.load(f).get("env") or {}).get("LED_HOST", ""))
except Exception:
    pass
PYEOF
)

CFG_ARGS=(install --settings "$SETTINGS" --led-sh "$LED_SH")
[ "$DATA_DIR" = "$DEFAULT_DATA_DIR" ] || CFG_ARGS+=(--data-dir "$DATA_DIR")
[ -n "$DRY_RUN" ] && CFG_ARGS+=(--dry-run)

if [ -n "$HOST" ]; then
  CFG_ARGS+=(--host "$HOST")
elif [ -z "$EXISTING_HOST" ]; then
  HOST=192.168.1.100
  CFG_ARGS+=(--host "$HOST")
  warn "settings.json 里没有 LED_HOST,按默认值 $HOST 写入(可用 --host 改)"
else
  ok "沿用已有的 LED_HOST=$EXISTING_HOST"
fi

printf '  '
"$PY" "$HOOKS_CFG" "${CFG_ARGS[@]}"
ok "hook 指向 $LED_SH"

# ---------- 6. 自检 ----------
step "自检"

EFF_HOST="${HOST:-${EXISTING_HOST:-192.168.1.100}}"

if curl -s -m 3 "http://${EFF_HOST}/status" >/dev/null 2>&1; then
  ok "设备可达: http://${EFF_HOST}/status"
else
  warn "设备 http://${EFF_HOST} 不可达 —— 装是装好了,确认在同一局域网后再试"
fi

if [ -n "$DRY_RUN" ]; then
  printf '\n[dry-run] 以上改动均未落盘\n'
  exit 0
fi

printf '  槽位表:\n'
LED_DATA_DIR="$DATA_DIR" "$LED_SH" status 2>&1 | sed 's/^/    /' || warn "led.sh status 执行失败"

step "完成"
cat <<EOF
  代码就地引用: $SKILL_SRC
  运行时数据:   $DATA_DIR
  配置:         $SETTINGS

  仓库移动或删除后灯会失效,届时重跑本脚本即可。
  在 Claude Code 里打开一次 /hooks 菜单可触发配置重载。
EOF
