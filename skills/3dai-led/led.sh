#!/usr/bin/env bash
# 3dai-led 槽位租约管理
#
#   led.sh <state> [platform]   按 (platform, session_id) 抢占/复用灯珠并点灯
#   led.sh release [platform]   归还该会话的租约并熄灯
#   led.sh status               打印槽位表(调试用)
#   led.sh reset                清空全部租约并熄灭所有灯珠(调试用)
#
# platform 区分同一目录下并行的不同工具(claude / codex / ...),让它们各占一颗灯珠
# 而不是互相覆盖状态。省略时取 $LED_PLATFORM,再没有就归到 cli。
#
# 租约存在单个 JSON(leases.json)里,由 lease.py 读写,没有锁 —— 稳定态只读、
# 写入靠 os.replace 原子替换,取舍见 lease.py 顶部注释。
# 本脚本只负责发 HTTP 和记日志。

set -u

NSLOTS="${LED_SLOTS:-8}"

# 全部路径都从本脚本的真实位置推出来,不碰 ~/.claude —— 整套东西只在仓库里活动,
# 与哪个编码工具在调用它无关。逐级解析符号链接后再取 dirname,所以从任何位置
# (PATH、软链接、绝对路径)调用都能找到 lease.py 和数据目录。
#   SELF_DIR  = <repo>/skills/3dai-led,lease.py 是它的邻居
#   DATA_DIR  = <repo>/data,租约表和日志的落脚点;lease.py 读同一个环境变量,
#               这里 export 出去保证两边看到的是同一个目录
SELF="${BASH_SOURCE[0]}"
while [ -L "$SELF" ]; do
  _t=$(readlink "$SELF")
  case "$_t" in
    /*) SELF="$_t" ;;
    *)  SELF="$(dirname "$SELF")/$_t" ;;
  esac
done
SELF_DIR=$(cd "$(dirname "$SELF")" && pwd)
LEASE="$SELF_DIR/lease.py"

LED_DATA_DIR="${LED_DATA_DIR:-$(cd "$SELF_DIR/../.." && pwd)/data}"
export LED_DATA_DIR

# 设备地址,三级回退:环境变量 > 数据目录里的 host 文件(install.sh 写的) > 占位符。
# 中间那级是关键 —— 从 hook 调用时地址由工具的 env 注入,但你在终端里手动敲
# `led.sh status` 排查问题时没有那个 env,少了它就会静默打向一个不存在的地址。
LED_HOST="${LED_HOST:-$(cat "$LED_DATA_DIR/host" 2>/dev/null || true)}"
LED_HOST="${LED_HOST:-192.168.1.100}"
# hook 继承的 PATH 未必含 homebrew,回退到 CLT 自带的 python3
PY="${LED_PYTHON:-$(command -v python3 2>/dev/null || echo /usr/bin/python3)}"

HOOK_EVENT=""
HOOK_TOOL=""
HOOK_REASON=""
PLATFORM="${2:-${LED_PLATFORM:-cli}}"   # 只用于日志;归属判定以 lease.py 的结果为准

# 唯一的日志出口,LED_DEBUG=1 才落盘;平时一个字节都不写。
# reason 只有 SessionEnd 会带,那行顺带记 cwd —— 多目录并行时才分得清是谁结束了。
logline() { # <led> <state> <rc>
  [ "${LED_DEBUG:-0}" = "1" ] || return 0
  [ -d "$LED_DATA_DIR" ] || mkdir -p "$LED_DATA_DIR" 2>/dev/null
  local extra=""
  [ -n "${HOOK_REASON:-}" ] && extra=$(printf '\treason=%s\tcwd=%s' "$HOOK_REASON" "$PWD")
  printf '%s\tled=%-2s\ts=%-8s\tplat=%-6s\tevent=%-18s\ttool=%-10s\trc=%s%s\n' \
    "$(date '+%Y-%m-%d %H:%M:%S')" "$1" "$2" "$PLATFORM" \
    "${HOOK_EVENT:-—}" "${HOOK_TOOL:-—}" "$3" "$extra" \
    >> "$LED_DATA_DIR/debug.log" 2>/dev/null
  return 0
}

# curl 永不阻断调用方:超时 2s,失败静默
poke() {
  local rc
  curl -s -m 2 "http://${LED_HOST}/set?led=${1}&s=${2}" >/dev/null 2>&1
  rc=$?
  logline "$1" "$2" "$rc"
  return 0
}

# 0 = 单项目,整条灯带表现一个状态;1 = 多项目,8 颗灯珠各自独立
poke_mode() {
  local rc name
  curl -s -m 2 "http://${LED_HOST}/mode?m=${1}" >/dev/null 2>&1
  rc=$?
  [ "$1" = "0" ] && name=single || name=multi
  logline "all" "$name" "$rc"
  return 0
}

case "${1:-}" in
  status)
    "$PY" "$LEASE" status
    ;;

  reset)
    rm -f "$LED_DATA_DIR/leases.json" 2>/dev/null
    rm -rf "$LED_DATA_DIR/slots" "$LED_DATA_DIR/.lock" 2>/dev/null   # 旧版目录租约的残留
    n=0
    while [ "$n" -lt "$NSLOTS" ]; do poke "$n" off; n=$((n + 1)); done
    poke_mode 0        # 回到单项目模式,和"没有任何目录持有租约"一致
    echo "已清空全部租约并熄灯"
    ;;

  "")
    echo "用法: led.sh <state|release> [platform] | led.sh <status|reset>" >&2
    exit 2
    ;;

  *)
    # lease.py 从 stdin 读 hook JSON,回一行:slot ⋅ 要熄灯的槽位 ⋅ event ⋅ tool ⋅ reason
    # release 走同一条路,只是回来的 slot 为空。
    # 分隔符是 \037 而不是制表符:tab 属于 IFS 白空格,read 会合并连续的 tab,
    # slot 或 off 为空时字段会整体左移(曾导致 release 发出 s=release 而不熄灯)。
    out=$("$PY" "$LEASE" "$1" "$PLATFORM" 2>/dev/null) || exit 0
    IFS=$'\037' read -r slot offs HOOK_EVENT HOOK_TOOL HOOK_REASON mode <<< "$out"
    # 三步的顺序是有讲究的:
    #   1. 先熄灯 —— 此时还没切模式,led 参数才有效,不会打到全局
    #   2. 再切模式
    #   3. 最后点灯 —— 紧跟切模式,单项目模式下不留"整条灯带是灭的"空档
    for n in ${offs:-}; do poke "$n" off; done
    [ -n "${mode:-}" ] && poke_mode "$mode"
    if [ -n "${slot:-}" ]; then
      poke "$slot" "$1"
    elif [ -z "${offs:-}" ] && [ -z "${mode:-}" ]; then
      # 一个请求都没发:release 时 reason=clear/resume,或者槽位满了没抢到灯珠。
      # 这些恰恰是最需要看的情况,补一行痕迹,免得日志里凭空断掉
      logline "—" "—" "—"
    fi
    ;;
esac

exit 0
