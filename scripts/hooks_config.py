#!/usr/bin/env python3
"""读写 Claude Code settings.json 里的 3dai-led hook 条目。

install.sh / uninstall.sh 共用。之所以不用 sed/jq 硬凑:settings.json 里还有
用户自己的 env、plugins、语言设置,必须整份读进来改完再写回去,不能整段覆盖。
jq 不是系统自带的,而 python3 是 lease.py 的既有依赖,不额外引入运行时。

    hooks_config.py install --settings P --led-sh P [--data-dir D]
    hooks_config.py remove  --settings P
    hooks_config.py show    --settings P

两条通用约束决定了下面 ASYNC 的取值(细节见 SKILL.md):
  · 点灯要异步 —— 设备离线时 curl 会阻塞满 2 秒,同步调用让每次操作都多等这么久
  · 释放要同步 —— 异步清理在进程退出时可能来不及跑完,槽位就泄漏了
"""

import argparse
import json
import os
import re
import shutil
import sys
import time

# (事件, matcher, 状态, 是否异步) —— 顺序即写入顺序
HOOKS = [
    ("UserPromptSubmit",   None,                     "thinking", True),
    ("PreToolUse",         "Edit|Write|NotebookEdit", "coding",  True),
    ("PreToolUse",         "Bash",                   "busy",     True),
    # 不挂 PostToolUse —— 它每个工具调用都触发一次「切回 thinking」,而 Edit 通常只花
    # 一两秒,coding 的液态呼吸连一个周期都走不完就被彩虹擦掉,肉眼看不见。它也不带
    # 任何新信息(只是把灯还原),去掉之后灯保持在「最近一次动作」的颜色上:连续编辑
    # 期间稳定青紫、跑命令期间稳定黄扫描,到下一个 PreToolUse / Stop 才变。附带好处是
    # PostToolUseFailure 的 error 不会再被同一次调用的 thinking 抢掉。
    ("PostToolUseFailure", None,                     "error",    True),
    ("PermissionRequest",  None,                     "waiting",  True),
    ("Notification",       None,                     "waiting",  True),
    ("SubagentStart",      None,                     "thinking", True),
    ("PreCompact",         None,                     "busy",     True),
    ("PostCompact",        None,                     "thinking", True),
    ("Stop",               None,                     "success",  True),
    ("StopFailure",        None,                     "alarm",    True),
    # SessionEnd 不设 async,留 8 秒让它把槽位还回去
    ("SessionEnd",         None,                     "release",  False),
]

# 平台名,作为第二个参数跟在状态后面。租约按 (platform, cwd) 归属,所以同一目录里
# 并行跑 Claude Code 和 codex 时两边各占一颗灯珠,不会互相覆盖状态、也不会被对方的
# SessionEnd 连带熄灯。写成 hook 命令的参数而不是 settings.json 的 env:一眼就能在
# 配置里看出这条 hook 是谁挂的,换工具接入时也不用记得改另一处。
PLATFORM = "claude"

# 判定一条 hook 是不是我们的:认「led.sh + 一个合法状态词」这个形状,不认具体路径。
# 旧版装在 ~/.claude/3dai-led/led.sh、软链接、另一个仓库副本,都能被认出来,重装
# 才真正幂等。反过来也不能只认路径里的 "3dai-led" —— 仓库目录本身就叫这个名字,
# 用户在同一个目录下挂的其他脚本会被无辜删掉。
STATES = "thinking|coding|busy|waiting|success|error|alarm|off|release"
OURS_RE = re.compile(r"led\.sh[\"']?\s+(%s)\b" % STATES)


def load(path):
    try:
        with open(path) as f:
            got = json.load(f)
        if not isinstance(got, dict):
            die("%s 的内容不是一个 JSON 对象,拒绝改写" % path)
        return got
    except FileNotFoundError:
        return {}
    except ValueError as e:
        die("%s 不是合法 JSON(%s)。先修好它再重跑,以免覆盖掉你的配置" % (path, e))


def save(path, data):
    """先写同目录临时文件再 os.replace:中途失败不会留下半截 settings.json。"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def backup(path):
    if not os.path.exists(path):
        return None
    dst = "%s.bak-%s" % (path, time.strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(path, dst)
    return dst


def is_ours(entry):
    cmd = entry.get("command", "") if isinstance(entry, dict) else ""
    return bool(OURS_RE.search(cmd))


def strip(settings):
    """摘掉所有 3dai-led 的 hook 条目,返回摘掉的条数。

    只删我们自己的那几条,同一事件下用户挂的其他命令原样留着;整组、整个事件
    被清空时才把空壳一并删掉,免得 settings.json 里堆一堆空数组。
    """
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return 0

    removed = 0
    for event in list(hooks):
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        kept_groups = []
        for group in groups:
            if not isinstance(group, dict):
                kept_groups.append(group)
                continue
            entries = group.get("hooks")
            if not isinstance(entries, list):
                kept_groups.append(group)
                continue
            kept = [e for e in entries if not is_ours(e)]
            removed += len(entries) - len(kept)
            if kept:
                group["hooks"] = kept
                kept_groups.append(group)
        if kept_groups:
            hooks[event] = kept_groups
        else:
            del hooks[event]

    if not hooks:
        settings.pop("hooks", None)
    return removed


def build(settings, led_sh):
    """把 HOOKS 表写进 settings,追加在各事件已有条目的后面。"""
    hooks = settings.setdefault("hooks", {})
    for event, matcher, state, is_async in HOOKS:
        entry = {"type": "command",
                 "command": '"%s" %s %s' % (led_sh, state, PLATFORM)}
        if is_async:
            entry["async"] = True
        else:
            entry["timeout"] = 8
        group = {"hooks": [entry]}
        if matcher:
            group["matcher"] = matcher
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            die("settings.json 的 hooks.%s 不是数组,拒绝改写" % event)
        groups.append(group)


def set_env(settings, data_dir):
    env = settings.get("env")
    if env is None:
        env = {} if data_dir else None
    elif not isinstance(env, dict):
        die("settings.json 的 env 不是对象,拒绝改写")

    if env is not None:
        # 设备地址现在存在数据目录的 host 文件里,由 led.sh 自己读 —— 留在这儿
        # 反而会盖掉它,而且是两处配置源、迟早不一致。旧版装的要主动清掉。
        env.pop("LED_HOST", None)
        if data_dir:
            env["LED_DATA_DIR"] = data_dir
        else:
            env.pop("LED_DATA_DIR", None)   # 回到默认值时别留下过期的指向

    if env:
        settings["env"] = env
    else:
        settings.pop("env", None)


def show(path):
    settings = load(path)
    hooks = settings.get("hooks", {})
    found = []
    for event, groups in (hooks.items() if isinstance(hooks, dict) else []):
        for group in groups if isinstance(groups, list) else []:
            for entry in (group.get("hooks", []) if isinstance(group, dict) else []):
                if is_ours(entry):
                    found.append((event, group.get("matcher", "—"),
                                  entry.get("command", "")))
    if not found:
        print("settings.json 里没有 3dai-led 的 hook")
        return
    print("已配置 %d 条 hook —— %s" % (len(found), path))
    for event, matcher, cmd in found:
        print("  %-18s %-24s %s" % (event, matcher, cmd))


def die(msg):
    sys.stderr.write("错误: %s\n" % msg)
    sys.exit(1)


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("action", choices=["install", "remove", "show"])
    ap.add_argument("--settings", required=True)
    ap.add_argument("--led-sh")
    ap.add_argument("--data-dir", help="非默认数据目录;留空则从 env 里移除该项")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.action == "show":
        show(args.settings)
        return

    settings = load(args.settings)
    removed = strip(settings)

    if args.action == "install":
        if not args.led_sh:
            die("install 需要 --led-sh")
        build(settings, args.led_sh)
        set_env(settings, args.data_dir)
        summary = "替换 %d 条旧 hook,写入 %d 条" % (removed, len(HOOKS))
    else:
        summary = "移除 %d 条 hook" % removed
        env = settings.get("env")
        if isinstance(env, dict):
            env.pop("LED_HOST", None)
            env.pop("LED_DATA_DIR", None)
            if not env:
                settings.pop("env", None)

    if args.dry_run:
        print("[dry-run] %s(未落盘)" % summary)
        return

    bak = backup(args.settings)
    save(args.settings, settings)
    print(summary + (",原文件备份为 %s" % os.path.basename(bak) if bak else ""))


if __name__ == "__main__":
    main()
