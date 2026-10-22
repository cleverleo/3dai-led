#!/usr/bin/env python3
"""读写 pi 配置里的 3dai-led 扩展条目。

install.sh / uninstall.sh 共用,和 opencode_config.py 是同一个角色。

    pi_config.py install --settings P --extension P
    pi_config.py remove  --settings P
    pi_config.py show    --settings P

pi 没有 shell hook,只能挂 TypeScript 扩展,所以这里写的不是一张事件表,而是
settings.json 里 `extensions` 数组的一个条目 —— 事件到状态的映射在
scripts/pi_extension.ts 里。

条目是扩展文件的绝对路径。pi 的用户级 settings 原样接受绝对路径,用 jiti 直接加载
TypeScript,所以能就地引用仓库里那一份,不必复制到 ~/.pi/agent/extensions/ —— 和
hook 里写绝对路径一样,改了代码立刻生效,代价同样是仓库不能挪窝。用户级扩展也不走
项目信任(trust.json)那一套,写上就加载。
"""

import argparse
import json
import os
import re
import shutil
import sys
import time

# 判定一个条目是不是我们的:认文件名这个形状,不认具体路径,理由同 opencode_config.py
# —— 旧安装、软链接、另一份副本都能认出来;也不能只认路径里的 "3dai-led",那会误删
# 用户挂在同目录下的其他扩展。前面可选的 + / - / ! 是 pi 资源数组的包含、排除语法。
OURS_RE = re.compile(r"^[+!-]?(.*/)?pi_extension\.ts$")


def die(msg):
    sys.stderr.write("错误: %s\n" % msg)
    sys.exit(1)


def load(path):
    """文件不存在时当空配置。"""
    try:
        with open(path) as f:
            raw = f.read()
    except FileNotFoundError:
        return {}
    except OSError as e:
        die("读不了 %s(%s)" % (path, e))
    try:
        got = json.loads(raw) if raw.strip() else {}
    except ValueError as e:
        die("%s 不是合法 JSON(%s)。先修好它再重跑,以免覆盖掉你的配置" % (path, e))
    if not isinstance(got, dict):
        die("%s 的内容不是一个 JSON 对象,拒绝改写" % path)
    return got


def save(path, data):
    """先写同目录临时文件再 os.replace:中途失败不会留下半截配置。

    pi 自己也会改写这个文件(lastChangelogVersion 之类),json 模块保持键的原有顺序,
    缩进和它一样用 2 格,改完 diff 只有我们那一行。
    """
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


def is_ours(item):
    return isinstance(item, str) and bool(OURS_RE.search(item))


def entries(config):
    exts = config.get("extensions")
    if exts is None:
        return []
    if not isinstance(exts, list):
        die("配置里的 extensions 不是数组,拒绝改写")
    return exts


def strip_ours(config):
    """摘掉所有 3dai-led 的扩展条目,返回摘掉的条数。用户其他条目原样留着。"""
    exts = entries(config)
    kept = [item for item in exts if not is_ours(item)]
    removed = len(exts) - len(kept)
    if kept:
        config["extensions"] = kept
    else:
        config.pop("extensions", None)   # 空数组不留在配置里
    return removed


def show(path):
    found = [item for item in entries(load(path)) if is_ours(item)]
    if not found:
        print("pi 配置里没有 3dai-led 扩展 —— %s" % path)
        return
    print("已配置 %d 个 pi 扩展 —— %s" % (len(found), path))
    for item in found:
        print("  %s" % item)


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("action", choices=["install", "remove", "show"])
    ap.add_argument("--settings", required=True)
    ap.add_argument("--extension", help="pi_extension.ts 的绝对路径")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.action == "show":
        show(args.settings)
        return

    config = load(args.settings)
    before = json.dumps(config)
    removed = strip_ours(config)

    if args.action == "install":
        if not args.extension:
            die("install 需要 --extension")
        spec = os.path.realpath(args.extension)
        config.setdefault("extensions", []).append(spec)
        summary = "替换 %d 个旧扩展条目,写入 %s" % (removed, spec)
    else:
        summary = "移除 %d 个扩展条目" % removed

    # 已经是想要的样子就一个字节都不写,重装不产生无谓的备份文件。比较时不排序键:
    # 我们的条目被挪到了数组末尾也算改动,但那只发生在第一次。
    if json.dumps(config) == before:
        print("pi 配置已是最新,未改动")
        return

    if args.dry_run:
        print("[dry-run] %s(未落盘)" % summary)
        return

    bak = backup(args.settings)
    save(args.settings, config)
    print(summary + (",原文件备份为 %s" % os.path.basename(bak) if bak else ""))


if __name__ == "__main__":
    main()
