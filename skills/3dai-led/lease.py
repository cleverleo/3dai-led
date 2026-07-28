#!/usr/bin/env python3
"""3dai-led 槽位租约 —— 单个 JSON 文件,无锁。

    $LED_DATA_DIR/leases.json   (默认 <repo>/data/leases.json)
    {"<platform>:<session_id>": {"slot": 0, "ts": <epoch 秒>, "cwd": "<仅展示>"}}

归属粒度是**(平台, 会话)**:一个会话一颗灯珠,从第一次点灯到退出为止都是同一颗,
中途换工作目录也不换灯。ts 是该租约的最近活动时间,兼作心跳。

早先按 (平台, 工作目录) 归属,换掉是因为「一个会话只待在一个目录里」根本不成立:
Claude Code 进 worktree、Bash 里 cd 到子目录,hook 进程的 $PWD 就变了,同一个会话
会拿新 cwd 再抢一颗灯珠。归还更糟 —— release 只删当前 $PWD 算出的那一条,退出时
$PWD 只可能落在其中一个目录上,其余几条永远等不到 release,只能熬满 STALE。实测
一个会话吃掉三颗灯珠、其中两颗冻在半路的状态上再也不动,就是这么来的。

平台仍然必须进 key:session_id 只保证在自己那个工具里唯一,跨工具不保证不撞。

cwd 降级成纯展示字段(status 里给人看的),不参与归属判定。它只在写路径上顺手刷新,
所以会话换了目录后,status 里的路径最多滞后 THROTTLE 秒才跟上 —— 无所谓,灯不动。

## 为什么不需要锁

1. 稳定态只读。会话已持有灯珠、ts 还新鲜(< THROTTLE 秒)时,直接回灯珠号,
   一个字节都不写。一轮对话十几次 hook,只有第一次会写。
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

被 SIGKILL 的会话没机会 release,租约会一直占着灯珠;此后 ts 不再刷新,整条
租约会在 STALE 之后被 sweep 清掉并熄灯。这是唯一的泄漏路径,而且有上界。
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


def make_key(platform, sid):
    """key = '<platform>:<session_id>'。

    平台名里的 : 已被 norm_platform() 换掉,所以按第一个 : 切一定切得对;sid 里
    真出现 : 也无所谓,partition 只切第一个,剩下的原样留给 sid。
    """
    return "%s:%s" % (platform, sid)


def split_key(key):
    platform, _, sid = key.partition(":")
    return platform, sid


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
    """把按 cwd 归属的老租约标记成过期,交给写路径上的 sweep() 去删并熄灯。

    老 key 有两代:更早的裸 cwd,和后来的 '<platform>:<cwd>'。两代的归属都是目录,
    没法映射到某个 session_id —— 一条 cwd 租约背后可能压着好几个会话。所以不迁移,
    只把 ts 置 0 让 sweep 当过期条目处理:灯珠腾出来、灯也灭掉,活着的会话下一次
    hook 就用新 key 重抢一颗。升级只闪这一下,比留着两套 key 并存干净。

    判据是 key 里 : 后面那截以 / 开头 —— 新 key 那截是 session_id,不会以 / 开头。
    """
    for key, entry in table.items():
        if not isinstance(entry, dict):
            continue
        _, _, tail = key.partition(":")
        if key.startswith("/") or tail.startswith("/"):
            entry["ts"] = 0
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

    数的是租约条数,不是目录数:同一目录里并行的两个会话是两颗灯,必须切到多项目
    模式,否则两个状态会在整条灯带上互相覆盖。
    """
    return 0 if len(table) <= 1 else 1


def acquire(key, cwd):
    """回 (灯珠号 or None, 需要熄灯的槽位号, 目标模式 or None)。

    满槽时灯珠号为 None,静默不点灯。目标模式为 None 表示走了快路径,不碰 /mode。
    cwd 只往 entry 里塞一份给 status 显示,不参与任何判定。
    """
    now = int(time.time())
    table = load()
    entry = table.get(key)

    # 快路径:一个字节都不写,也不碰设备的 /mode。key 里已经带了 sid,命中即是本会话
    if isinstance(entry, dict) and slot_of(entry) is not None \
            and now - entry.get("ts", 0) < THROTTLE:
        return slot_of(entry), [], None

    off = sweep(table, now)
    entry = table.get(key)         # sweep 可能把本条也清掉了(空闲超 STALE)

    if isinstance(entry, dict) and slot_of(entry) is not None:
        entry["ts"] = now
        entry["cwd"] = cwd         # 会话中途换过目录的话,顺手跟上
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
        entry = {"slot": free[0], "ts": now, "cwd": cwd}
        table[key] = entry

    off += normalize_single(table)
    save(table)
    # 写路径无条件回一个目标模式,不去持久化"当前模式"再比对:写路径本来就稀疏
    # (每目录每 THROTTLE 秒一次),多发一个请求换来的是自动纠偏 —— 有人从 WebUI
    # 手动切过、或某次请求丢了,下次写路径就会掰回来。
    return slot_of(entry), off, target_mode(table)


def release(key, reason):
    """回需要熄灯的槽位号。

    按会话归属之后这里就没有引用计数了:key 唯一对应一个会话,它结束就是整条租约
    结束,不必再问「同目录还有没有别人」。
    """
    now = int(time.time())
    table = load()
    entry = table.get(key)
    if not isinstance(entry, dict):
        return []

    # /clear 和 resume 也报 SessionEnd,但会话还在继续。保住租约只刷时间戳,
    # 否则灯会灭一下再亮,而且空档期里别的会话可能抢走这颗灯珠。
    if reason in ("clear", "resume"):
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
            platform, sid = split_key(key)
            # 现在一颗灯珠对一个会话,同一个目录可能占好几颗 —— 光看路径分不清谁是谁,
            # 所以把 sid 的前 8 位也打出来。cwd 是写路径上记的,可能比实际滞后一点。
            # 没有 cwd 字段的只可能是还没被 sweep 掉的老租约,退回整个 key,免得
            # 升级到第一次点灯之间那一小段时间里 status 显示成一片问号。
            who = "按目录" if sid.startswith("cwd:") else (sid[:8] or "—")
            print("  %d   [%-*s] %s  (%s, %d 秒前活动)"
                  % (n, width, platform, entry.get("cwd") or key,
                     who, now - entry.get("ts", 0)))
        else:
            print("  %d   —" % n)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "status":
        status()
        return

    hook = read_hook_json()
    reason = hook.get("reason") or ""
    # 展示用。优先信 hook JSON 里的 cwd(那是工具自己报的工作目录),没有才退回 $PWD;
    # $PWD 而不是 getcwd() 是因为后者会解析符号链接,同一目录会显示成两个样子。
    cwd = hook.get("cwd") or os.environ.get("PWD") or os.getcwd()
    # 归属主键。拿不到会话号时退回 cwd 而不是一个固定的 "anon":固定值会让某个工具
    # 的所有会话挤在同一颗灯珠上,而且谁先结束谁就把灯替所有人熄了。退回 cwd 至少
    # 保住按目录区分 —— 手动敲 led.sh 排查、以及万一哪个工具不报 session_id 时。
    sid = (hook.get("session_id") or os.environ.get("LED_SESSION_ID")
           or "cwd:" + cwd)
    # 平台由调用方自报:hook 命令里的第二个参数最可靠(一眼能在 settings.json 里
    # 看到是谁挂的),脚本/插件这类不方便加参数的走 LED_PLATFORM。两者都没有时
    # 归到 cli —— 手动敲 led.sh 排查时不会顶掉正在跑的会话的灯。
    platform = norm_platform(
        (sys.argv[2] if len(sys.argv) > 2 else "") or os.environ.get("LED_PLATFORM"))
    key = make_key(platform, sid)

    if cmd == "release":
        # release 不切模式。此刻切到单项目模式,整条灯带会去表现"最后一次 set"的值
        # —— 也就是刚发出去的那个 off,剩下那条租约的灯会莫名熄灭。留给它下次点灯时
        # 切:那时切模式和点亮状态在同一次调用里,不留空档。
        slot, off, mode = None, release(key, reason), None
    else:
        slot, off, mode = acquire(key, cwd)

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
