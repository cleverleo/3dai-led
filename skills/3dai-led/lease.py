#!/usr/bin/env python3
"""3dai-led 槽位租约 —— 单个 JSON 文件,无锁。

    $LED_DATA_DIR/leases.json   (默认 <repo>/data/leases.json)
    {"<cwd>": {"slot": 0, "ts": <epoch 秒>, "sids": ["<session_id>", ...]}}

归属粒度是**工作目录**:同一目录的多个会话共享一颗灯珠,靠 sids 做引用计数,
最后一个退出时才熄灯归还。ts 是该目录的最近活动时间,兼作心跳。

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
        return got if isinstance(got, dict) else {}
    except Exception:
        return {}          # 不存在 / 读坏 -> 当空表,下次写入自动重建


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
    """删掉整体过期的目录条目,回收灯珠。返回需要熄灯的槽位号。"""
    off = []
    for cwd in list(table):
        entry = table[cwd]
        if not isinstance(entry, dict) or now - entry.get("ts", 0) > STALE:
            slot = slot_of(entry) if isinstance(entry, dict) else None
            if slot is not None:
                off.append(slot)
            del table[cwd]
    return off


def normalize_single(table):
    """只剩一个目录时把它归到 0 号灯珠,回需要熄灯的旧槽位。

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
    """0 = 单项目(整条灯带一个状态),1 = 多项目(每颗灯珠独立)。"""
    return 0 if len(table) <= 1 else 1


def acquire(cwd, sid):
    """回 (灯珠号 or None, 需要熄灯的槽位号, 目标模式 or None)。

    满槽时灯珠号为 None,静默不点灯。目标模式为 None 表示走了快路径,不碰 /mode。
    """
    now = int(time.time())
    table = load()
    entry = table.get(cwd)

    # 快路径:一个字节都不写,也不碰设备的 /mode
    if isinstance(entry, dict) and slot_of(entry) is not None \
            and sid in entry.get("sids", []) \
            and now - entry.get("ts", 0) < THROTTLE:
        return slot_of(entry), [], None

    off = sweep(table, now)
    entry = table.get(cwd)         # sweep 可能把本目录也清掉了(空闲超 STALE)

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
        table[cwd] = entry

    off += normalize_single(table)
    save(table)
    # 写路径无条件回一个目标模式,不去持久化"当前模式"再比对:写路径本来就稀疏
    # (每目录每 THROTTLE 秒一次),多发一个请求换来的是自动纠偏 —— 有人从 WebUI
    # 手动切过、或某次请求丢了,下次写路径就会掰回来。
    return slot_of(entry), off, target_mode(table)


def release(cwd, sid, reason):
    """回需要熄灯的槽位号。"""
    now = int(time.time())
    table = load()
    entry = table.get(cwd)
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
    if sids:                       # 同目录还有别的会话在跑 -> 不熄灯
        entry["ts"] = now
        save(table)
        return []

    slot = slot_of(entry)
    del table[cwd]
    save(table)
    return [slot] if slot is not None else []


def status():
    table = load()
    now = int(time.time())
    by_slot = {}
    for cwd, entry in table.items():
        if isinstance(entry, dict) and slot_of(entry) is not None:
            by_slot[slot_of(entry)] = (cwd, entry)
    print("灯珠  归属")
    for n in range(NSLOTS):
        if n in by_slot:
            cwd, entry = by_slot[n]
            print("  %d   %s  (%d 个会话, %d 秒前活动)"
                  % (n, cwd, len(entry.get("sids", [])), now - entry.get("ts", 0)))
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

    if cmd == "release":
        # release 不切模式。此刻切到单项目模式,整条灯带会去表现"最后一次 set"的值
        # —— 也就是刚发出去的那个 off,剩下那个目录的灯会莫名熄灭。留给它下次点灯时
        # 切:那时切模式和点亮状态在同一次调用里,不留空档。
        slot, off, mode = None, release(cwd, sid, reason), None
    else:
        slot, off, mode = acquire(cwd, sid)

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
