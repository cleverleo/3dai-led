#!/usr/bin/env python3
"""Idempotently install/remove 3dai-led entries in Codex hooks.json."""

import argparse
import copy
import json
import os
import re
import shlex
import shutil
import sys
import time


OURS_RE = re.compile(
    r"codex_hook\.py[\"']?\s+"
    r"(thinking|coding|busy|waiting|success|error|alarm|off|release)\b"
)


def die(message):
    sys.stderr.write("错误: %s\n" % message)
    sys.exit(1)


def load(path):
    try:
        with open(path) as stream:
            value = json.load(stream)
    except FileNotFoundError:
        return {}
    except ValueError as error:
        die("%s 不是合法 JSON(%s)。先修好它再重跑,以免覆盖配置" % (path, error))
    if not isinstance(value, dict):
        die("%s 的内容不是一个 JSON 对象,拒绝改写" % path)
    return value


def save(path, value):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    temporary = "%s.tmp.%d" % (path, os.getpid())
    with open(temporary, "w") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    os.replace(temporary, path)


def backup(path):
    if not os.path.exists(path):
        return None
    destination = "%s.bak-%s" % (path, time.strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(path, destination)
    return destination


def is_ours(handler):
    return (
        isinstance(handler, dict)
        and bool(OURS_RE.search(handler.get("command", "")))
    )


def strip(config):
    hooks = config.get("hooks")
    if not isinstance(hooks, dict):
        return 0

    removed = 0
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        kept_groups = []
        for group in groups:
            if not isinstance(group, dict):
                kept_groups.append(group)
                continue
            handlers = group.get("hooks")
            if not isinstance(handlers, list):
                kept_groups.append(group)
                continue
            kept = [handler for handler in handlers if not is_ours(handler)]
            removed += len(handlers) - len(kept)
            if kept:
                group["hooks"] = kept
                kept_groups.append(group)
        if kept_groups:
            hooks[event] = kept_groups
        else:
            del hooks[event]
    if not hooks:
        config.pop("hooks", None)
    return removed


def source_groups(path, python, adapter):
    source = load(path)
    hooks = source.get("hooks")
    if not isinstance(hooks, dict):
        die("%s 缺少 hooks 对象" % path)

    result = copy.deepcopy(hooks)
    prefix = "%s %s" % (shlex.quote(python), shlex.quote(adapter))
    count = 0
    for groups in result.values():
        if not isinstance(groups, list):
            die("%s 包含非数组 hook 事件" % path)
        for group in groups:
            for handler in group.get("hooks", []):
                command = handler.get("command", "")
                marker = "codex_hook.py\""
                position = command.find(marker)
                if position < 0:
                    die("模板里存在无法识别的命令: %s" % command)
                suffix = command[position + len(marker):]
                handler["command"] = prefix + suffix
                count += 1
    return result, count


def install(config, source, python, adapter):
    removed = strip(config)
    generated, count = source_groups(source, python, adapter)
    hooks = config.setdefault("hooks", {})
    for event, groups in generated.items():
        existing = hooks.setdefault(event, [])
        if not isinstance(existing, list):
            die("hooks.%s 不是数组,拒绝改写" % event)
        existing.extend(groups)
    return removed, count


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("install", "remove", "show"))
    parser.add_argument("--hooks", required=True)
    parser.add_argument("--source")
    parser.add_argument("--python")
    parser.add_argument("--adapter")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    config = load(args.hooks)
    if args.action == "show":
        found = []
        for event, groups in config.get("hooks", {}).items():
            for group in groups:
                for handler in group.get("hooks", []):
                    if is_ours(handler):
                        found.append((event, group.get("matcher", "—"),
                                      handler.get("command", "")))
        print("已配置 %d 条 Codex hook —— %s" % (len(found), args.hooks))
        for event, matcher, command in found:
            print("  %-18s %-16s %s" % (event, matcher, command))
        return

    if args.action == "install":
        if not all((args.source, args.python, args.adapter)):
            die("install 需要 --source、--python 和 --adapter")
        removed, count = install(
            config, args.source, args.python, os.path.realpath(args.adapter)
        )
        summary = "替换 %d 条旧 Codex hook,写入 %d 条" % (removed, count)
    else:
        removed = strip(config)
        summary = "移除 %d 条 Codex hook" % removed

    if args.dry_run:
        print("[dry-run] %s(未落盘)" % summary)
        return
    saved = backup(args.hooks)
    save(args.hooks, config)
    print(summary + (",原文件备份为 %s" % os.path.basename(saved) if saved else ""))


if __name__ == "__main__":
    main()
