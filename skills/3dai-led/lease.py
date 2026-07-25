#!/usr/bin/env python3
"""3dai-led 槽位租约 —— 单个 JSON 文件,无锁。

    $LED_DATA_DIR/leases.json   (默认 <repo>/data/leases.json)
    {"<platform>:<cwd>": {"slot": 0, "ts": <epoch 秒>, "sids": ["<session_id>", ...]}}

归属粒度是**(平台, 工作目录)**:同一目录里 claude 和 codex 各抢一颗灯珠,状态互
不覆盖;同一平台同一目录的多个会话才共享灯珠,靠 sids 做引用计数,最后一个退出时
才熄灯归还。ts 是该租约的最近活动时间,兼作心跳。

平台必须进 key 而不是只作为一个字段:两个工具在同一目录并行时,谁的 SessionEnd
先到就会把另一个的灯一起熄掉,而单独一个 platform 字段也放不下两条并存的记录。

## 为什么不需要锁

1. 稳定态只读。cwd 已持有灯珠、sid 已登记、ts 还新鲜(< THROTTLE 秒)时,
   直接回灯珠号,一个字节都不写。一轮对话十几次 hook,只有第一次会写。
2. 写入原子。先写同目录临时文件再 os.replace(),POSIX rename 保证读者要么
   看到旧的完整 JSON、要么看到新的,绝不会读到半截文件。
3. 丢更新可以接受。两个进程恰好落在同一个几毫秒的读-改-写窗口里时,后写的
   会覆盖前者。最坏结果是两个目录映射到同一颗灯珠(状态互相覆盖),或某个
   目录的条目丢失(下次点灯重新抢占,自愈)。对一个状态指示灯来说,这个代价
   远小于为它维护一把锁 —— 旧版用 mkdir 做的锁会把 curl 的 2 秒超时算进
   持锁时间,设备离线时反过来让其他目录的灯集体不亮。

## 过期回收的时机

ts 的节流(THROTTLE)顺带解决了 sweep 的时机问题:活跃目录每 THROTTLE 秒
必然走一次写路径,sweep 就挂在写路径上。所以别人崩溃残留的灯珠,最迟在
STALE 到期后再过 THROTTLE 秒就会被回收,不必每次点灯都扫。

被 SIGKILL 的会话其 sid 会永久留在 sids 里,导致同目录最后一个会话退出时
也不熄灯;此后 ts 不再刷新,整条租约会在 STALE 之后被 sweep 清掉并熄灯。
"""

import json
import os
import signal
import sys
import time

# 数据落在仓库自己的 data/ 里,不进 ~/.claude —— 本文件在 <repo>/skills/3dai-led/,
# 往上两级就是仓库根。realpath 是因为可能经软链接调用。led.sh 会 export 同一个
# 变量,正常调用链上这里读到的就是它算好的值。
DATA_DIR = os.environ.get("LED_DATA_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__)))),
    "data")
LEASES = os.path.join(DATA_DIR, "leases.json")

NSLOTS = int(os.environ.get("LED_SLOTS", "8"))
STALE = int(os.environ.get("LED_STALE_MIN", "30")) * 60
THROTTLE = int(os.environ.get("LED_TS_THROTTLE", "60"))

# 没报平台的调用方(手动敲 led.sh、没传第二个参数的脚本)统一归到这个名字下
DEFAULT_PLATFORM = "cli"
# 升级前的租约表 key 就是裸 cwd,那时只有 Claude Code 一个调用方
LEGACY_PLATFORM = "claude"


def make_key(platform, cwd):
    """key = '<platform>:<cwd>'。cwd 一定以 / 开头,所以按第一个 : 切一定切得对。"""
    return "%s:%s" % (platform, cwd)


def split_key(key):
    platform, _, cwd = key.partition(":")
    return platform, cwd


def norm_platform(name):
    """: 是 key 的分隔符,平台名里不能有;空白和空值都退回默认名。"""
    name = (name or "").strip().replace(":", "-")
    return name or DEFAULT_PLATFORM


def read_hook_json():
    """hook 会从 stdin 喂 JSON;不喂的调用方靠 1 秒超时兜底,不挂住。"""
    if sys.stdin.isatty():
        return {}

    def on_alarm(_sig, _frame):
        raise IOError("stdin timeout")

    raw = ""
    try:
        signal.signal(signal.SIGALRM, on_alarm)
        signal.alarm(1)
        raw = sys.stdin.read()
    except Exception:
        return {}
    finally:
        signal.alarm(0)
    try:
        got = json.loads(raw)
        return got if isinstance(got, dict) else {}
    except Exception:
        return {}


def load():
    try:
        with open(LEASES) as f:
            got = json.load(f)
    except Exception:
        return {}          # 不存在 / 读坏 -> 当空表,下次写入自动重建
    return migrate(got) if isinstance(got, dict) else {}


def migrate(table):
    """把旧格式的裸 cwd key 就地改成 'claude:<cwd>'。

    只在内存里转,下次走写路径时自然落盘。sids 里存的是 session_id,不受 key 变化
    影响,所以升级时正在跑的会话不会掉租约、不会换灯珠。
    """
    for key in [k for k in table if k.startswith("/")]:
        entry = table.pop(key)
        table.setdefault(make_key(LEGACY_PLATFORM, key), entry)
    return table


def save(table):
    tmp = "%s.tmp.%d" % (LEASES, os.getpid())
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(tmp, "w") as f:
            json.dump(table, f, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(tmp, LEASES)        # 原子替换,读者永远看到完整 JSON
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def slot_of(entry):
    slot = entry.get("slot")
    return slot if isinstance(slot, int) and 0 <= slot < NSLOTS else None


def sweep(table, now):
    """删掉整体过期的租约,回收灯珠。返回需要熄灯的槽位号。"""
    off = []
    for key in list(table):
        entry = table[key]
        if not isinstance(entry, dict) or now - entry.get("ts", 0) > STALE:
            slot = slot_of(entry) if isinstance(entry, dict) else None
            if slot is not None:
                off.append(slot)
            del table[key]
    return off


def normalize_single(table):
    """只剩一条租约时把它归到 0 号灯珠,回需要熄灯的旧槽位。

    单项目模式下设备会忽略 /set 的 led 参数,整条灯带统一表现全局状态(回读时
    落在 led_states[0])。所以单项目模式必须配 slot 0,否则切回多项目模式时
    0 号灯会亮着一个没有主人的状态,而真正的主人在自己的槽位上却是灭的。
    """
    if len(table) != 1:
        return []
    entry = table[next(iter(table))]
    old = slot_of(entry)
    if old is None or old == 0:
        return []
    entry["slot"] = 0
    return [old]


def target_mode(table):
    """0 = 单项目(整条灯带一个状态),1 = 多项目(每颗灯珠独立)。

    数的是租约条数而不是目录数:同一目录里 claude 和 codex 并行时是两颗灯,必须
    切到多项目模式,否则两个状态会在整条灯带上互相覆盖。
    """
    return 0 if len(table) <= 1 else 1


def acquire(key, sid):
    """回 (灯珠号 or None, 需要熄灯的槽位号, 目标模式 or None)。

    满槽时灯珠号为 None,静默不点灯。目标模式为 None 表示走了快路径,不碰 /mode。
    """
    now = int(time.time())
    table = load()
    entry = table.get(key)

    # 快路径:一个字节都不写,也不碰设备的 /mode
    if isinstance(entry, dict) and slot_of(entry) is not None \
            and sid in entry.get("sids", []) \
            and now - entry.get("ts", 0) < THROTTLE:
        return slot_of(entry), [], None

    off = sweep(table, now)
    entry = table.get(key)         # sweep 可能把本条也清掉了(空闲超 STALE)

    if isinstance(entry, dict) and slot_of(entry) is not None:
        entry["ts"] = now
        sids = entry.setdefault("sids", [])
        if sid not in sids:
            sids.append(sid)
    else:
        used = set()
        for other in table.values():
            if isinstance(other, dict) and slot_of(other) is not None:
                used.add(slot_of(other))
        free = [n for n in range(NSLOTS) if n not in used]
        if not free:               # 满槽:静默不点灯,也不碰模式(降级要安静)
            if off:
                save(table)
            return None, off, None
        entry = {"slot": free[0], "ts": now, "sids": [sid]}
        table[key] = entry

    off += normalize_single(table)
    save(table)
    # 写路径无条件回一个目标模式,不去持久化"当前模式"再比对:写路径本来就稀疏
    # (每目录每 THROTTLE 秒一次),多发一个请求换来的是自动纠偏 —— 有人从 WebUI
    # 手动切过、或某次请求丢了,下次写路径就会掰回来。
    return slot_of(entry), off, target_mode(table)


def release(key, sid, reason):
    """回需要熄灯的槽位号。"""
    now = int(time.time())
    table = load()
    entry = table.get(key)
    if not isinstance(entry, dict):
        return []

    # /clear 和 resume 也报 SessionEnd,但会话还在继续。保住租约只刷时间戳,
    # 否则灯会灭一下再亮,而且空档期里别的目录可能抢走这颗灯珠。
    if reason in ("clear", "resume"):
        entry["ts"] = now
        save(table)
        return []

    sids = entry.get("sids", [])
    if sid in sids:
        sids.remove(sid)
    if sids:                       # 同平台同目录还有别的会话在跑 -> 不熄灯
        entry["ts"] = now
        save(table)
        return []

    slot = slot_of(entry)
    del table[key]
    save(table)
    return [slot] if slot is not None else []


def status():
    table = load()
    now = int(time.time())
    by_slot = {}
    for key, entry in table.items():
        if isinstance(entry, dict) and slot_of(entry) is not None:
            by_slot[slot_of(entry)] = (key, entry)
    width = max([len(split_key(k)[0]) for k in table] or [0])
    print("灯珠  归属")
    for n in range(NSLOTS):
        if n in by_slot:
            key, entry = by_slot[n]
            platform, cwd = split_key(key)
            print("  %d   [%-*s] %s  (%d 个会话, %d 秒前活动)"
                  % (n, width, platform, cwd,
                     len(entry.get("sids", [])), now - entry.get("ts", 0)))
        else:
            print("  %d   —" % n)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "status":
        status()
        return

    hook = read_hook_json()
    sid = hook.get("session_id") or os.environ.get("LED_SESSION_ID") or "anon"
    reason = hook.get("reason") or ""
    # 用 $PWD 而不是 getcwd():后者会解析符号链接,同一目录会算成两个归属
    cwd = os.environ.get("PWD") or os.getcwd()
    # 平台由调用方自报:hook 命令里的第二个参数最可靠(一眼能在 settings.json 里
    # 看到是谁挂的),脚本/插件这类不方便加参数的走 LED_PLATFORM。两者都没有时
    # 归到 cli —— 手动敲 led.sh 排查时不会顶掉正在跑的会话的灯。
    platform = norm_platform(
        (sys.argv[2] if len(sys.argv) > 2 else "") or os.environ.get("LED_PLATFORM"))
    key = make_key(platform, cwd)

    if cmd == "release":
        # release 不切模式。此刻切到单项目模式,整条灯带会去表现"最后一次 set"的值
        # —— 也就是刚发出去的那个 off,剩下那条租约的灯会莫名熄灭。留给它下次点灯时
        # 切:那时切模式和点亮状态在同一次调用里,不留空档。
        slot, off, mode = None, release(key, sid, reason), None
    else:
        slot, off, mode = acquire(key, sid)

    # 给 led.sh 的一行,字段用 \037 分隔 —— 不能用制表符:tab 属于 IFS 白空格,
    # bash 的 read 会把连续的 tab 合并成一个,空字段(slot 或 off 为空)会被吃掉,
    # 后面的字段整体左移一位。
    # 日志由 led.sh 统一写(它才知道 curl 的返回码),这里只负责把字段传出去。
    sys.stdout.write("\037".join([
        "" if slot is None else str(slot),
        " ".join(str(s) for s in off),
        str(hook.get("hook_event_name") or ""),
        str(hook.get("tool_name") or ""),
        reason,
        "" if mode is None else str(mode),
    ]) + "\n")


if __name__ == "__main__":
    main()
