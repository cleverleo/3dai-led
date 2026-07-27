#!/usr/bin/env bash
# 3dai-led 安装:把本仓库接进 Claude Code、Codex 和 opencode。
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
CODEX_HOOKS_CFG="$REPO/scripts/codex_hooks_config.py"
CODEX_HOOK_SOURCE="$REPO/config/codex-hooks.json"
CODEX_ADAPTER="$REPO/scripts/codex_hook.py"
OPENCODE_CFG_PY="$REPO/scripts/opencode_config.py"
OPENCODE_PLUGIN="$REPO/scripts/opencode_plugin.ts"

SETTINGS="${HOME}/.claude/settings.json"
CODEX_HOOKS="${CODEX_HOME:-${HOME}/.codex}/hooks.json"

# opencode 的配置文件叫 opencode.jsonc 或 opencode.json,两个都合法。已经存在哪个就用
# 哪个,一个都没有才新建 .json —— 别在用户已有的 .jsonc 旁边再造一份,那样两份配置都
# 会被读到,排查时极难发现。OPENCODE_CONFIG 是 opencode 自己认的环境变量,优先。
OPENCODE_DIR="${XDG_CONFIG_HOME:-${HOME}/.config}/opencode"
if [ -n "${OPENCODE_CONFIG:-}" ]; then
  OPENCODE_CFG="$OPENCODE_CONFIG"
elif [ -f "$OPENCODE_DIR/opencode.jsonc" ]; then
  OPENCODE_CFG="$OPENCODE_DIR/opencode.jsonc"
else
  OPENCODE_CFG="$OPENCODE_DIR/opencode.json"
fi
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

  --host <ip>            设备地址,写进数据目录的 host 文件。
                         首次安装必填;之后重装可以省略,沿用已有值
  --skill copy|link|skip SKILL.md 的安装方式,默认 copy
  --skill-dst <path>     技能安装位置,默认 ~/.claude/skills/3dai-led
  --data-dir <path>      租约表和日志的目录,默认 <repo>/data
  --settings <path>      目标 settings.json,默认 ~/.claude/settings.json
  --codex-hooks <path>   目标 Codex hooks.json,默认 ~/.codex/hooks.json
  --opencode-config <path>
                         目标 opencode 配置,默认 ~/.config/opencode/opencode.jsonc
                         (不存在则 opencode.json)
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
    --codex-hooks) CODEX_HOOKS="${2:?--codex-hooks 需要一个路径}"; shift 2 ;;
    --opencode-config) OPENCODE_CFG="${2:?--opencode-config 需要一个路径}"; shift 2 ;;
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

for f in "$LED_SH" "$SKILL_SRC/lease.py" "$HOOKS_CFG" \
         "$CODEX_HOOKS_CFG" "$CODEX_HOOK_SOURCE" "$CODEX_ADAPTER" \
         "$OPENCODE_CFG_PY" "$OPENCODE_PLUGIN"; do
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

# 设备地址。首次安装必须显式给 —— 猜一个默认值只会装出一套指向不存在的设备、
# 灯不亮也不报错的配置。已经配过的重装可以省略,沿用旧值。
# 这一步必须赶在动任何文件之前:参数错了就该立刻退出,而不是复制完、迁移完才说。
EXISTING_HOST=$(cat "$DATA_DIR/host" 2>/dev/null || true)
HOST_SRC="$DATA_DIR/host"
if [ -z "$EXISTING_HOST" ]; then
  HOST_SRC="settings.json 的 env(旧版留下的)"
  # 旧版把地址放在 settings.json 的 env 里,升级时捞回来,免得再问一次
  EXISTING_HOST=$("$PY" - "$SETTINGS" <<'PYEOF' 2>/dev/null || true
import json, sys
try:
    with open(sys.argv[1]) as f:
        print((json.load(f).get("env") or {}).get("LED_HOST", ""))
except Exception:
    pass
PYEOF
)
fi

if [ -z "$HOST" ] && [ -z "$EXISTING_HOST" ]; then
  bad "缺少 --host:首次安装必须指定设备地址"
  echo "    例:./install.sh --host 192.168.1.42" >&2
  echo "        ./install.sh --host 3dai-led-xxxxxxxx.local   # mDNS 主机名也行" >&2
  echo "    浏览器打开 http://<地址>/ 能看到控制面板,就是对的" >&2
  exit 2
fi
if [ -n "$HOST" ]; then
  ok "设备地址: $HOST"
else
  ok "设备地址: $EXISTING_HOST(沿用 $HOST_SRC)"
fi

# ---------- 2. 数据目录 ----------
step "准备数据目录"
run "mkdir -p '$DATA_DIR'"
okr "$DATA_DIR"
[ "$DATA_DIR" = "$DEFAULT_DATA_DIR" ] || ok "非默认路径,会写进 settings.json 的 env.LED_DATA_DIR"

# 地址落在仓库里而不是只放进 hook 的 env:这样在终端手动跑 led.sh 排查时,
# 它也知道该打向哪台设备。settings.json 那边因此不需要 LED_HOST。
EFF_HOST="${HOST:-$EXISTING_HOST}"
run "printf '%s\n' '$EFF_HOST' > '$DATA_DIR/host'"
okr "设备地址写入 $DATA_DIR/host"

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
step "写入 Claude Code settings.json"

CFG_ARGS=(install --settings "$SETTINGS" --led-sh "$LED_SH")
[ "$DATA_DIR" = "$DEFAULT_DATA_DIR" ] || CFG_ARGS+=(--data-dir "$DATA_DIR")
[ -n "$DRY_RUN" ] && CFG_ARGS+=(--dry-run)

printf '  '
"$PY" "$HOOKS_CFG" "${CFG_ARGS[@]}"
ok "hook 指向 $LED_SH"

step "写入 Codex hooks.json"

CODEX_CFG_ARGS=(install --hooks "$CODEX_HOOKS" --source "$CODEX_HOOK_SOURCE" \
  --python "$PY" --adapter "$CODEX_ADAPTER")
[ -n "$DRY_RUN" ] && CODEX_CFG_ARGS+=(--dry-run)

printf '  '
"$PY" "$CODEX_HOOKS_CFG" "${CODEX_CFG_ARGS[@]}"
ok "Codex hook 通过 $CODEX_ADAPTER 复用 $LED_SH"

step "写入 opencode 配置"

OPENCODE_CFG_ARGS=(install --config "$OPENCODE_CFG" --plugin "$OPENCODE_PLUGIN")
[ -n "$DRY_RUN" ] && OPENCODE_CFG_ARGS+=(--dry-run)

printf '  '
"$PY" "$OPENCODE_CFG_PY" "${OPENCODE_CFG_ARGS[@]}"
ok "opencode 插件 $OPENCODE_PLUGIN 复用 $LED_SH"

# ---------- 6. 自检 ----------
step "自检"

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
  Codex 配置:   $CODEX_HOOKS
  opencode 配置:$OPENCODE_CFG

  仓库移动或删除后灯会失效,届时重跑本脚本即可。
  分别在 Claude Code / Codex 里打开一次 /hooks,重载并信任 hook。
  opencode 的插件在启动时加载,重开一个 opencode 即可生效。
EOF
