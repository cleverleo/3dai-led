#!/usr/bin/env python3
"""读写 opencode 配置里的 3dai-led 插件条目。

install.sh / uninstall.sh 共用,和 hooks_config.py、codex_hooks_config.py 是同一个角色。

    opencode_config.py install --config P --plugin P
    opencode_config.py remove  --config P
    opencode_config.py show    --config P

opencode 没有 shell hook,只能挂 JS 插件,所以这里写的不是一张事件表,而是 `plugin`
数组里的一个条目 —— 事件到状态的映射在 scripts/opencode_plugin.ts 里。

条目形如 `file:///abs/path/opencode_plugin.ts`。opencode 的加载器对 file:// URL 做了
特判:指向的是文件(而不是带 package.json 的目录)时原样 import,所以能直接引用仓库里
的那一份,不必复制或软链接到 ~/.config/opencode/plugin/ —— 和 hook 里写绝对路径一样,
改了代码立刻生效,代价同样是仓库不能挪窝。
"""

import argparse
import json
import os
import re
import shutil
import sys
import time

# 判定一个条目是不是我们的:认文件名这个形状,不认具体路径。旧安装、软链接、仓库的
# 另一份副本都能被认出来,重装才真正幂等。反过来也不能只认路径里的 "3dai-led" ——
# 仓库目录本身就叫这个名字,用户挂在同目录下的其他插件会被无辜删掉。
OURS_RE = re.compile(r"(^|/)opencode_plugin\.ts$")


def die(msg):
    sys.stderr.write("错误: %s\n" % msg)
    sys.exit(1)


def strip_comments(text):
    """去掉 JSONC 的注释。

    用户的配置常见是 opencode.jsonc,json 模块不认注释。这里只做词法级的剔除:逐字符
    扫,字符串字面量内部原样保留(否则 URL 里的 // 会被当成行注释砍掉半截)。
    """
    out = []
    i, n = 0, len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:      # 转义序列整体跳过,别把 \" 当成串尾
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
        elif ch == '"':
            in_string = True
            out.append(ch)
            i += 1
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def load(path):
    """返回 (配置对象, 原文里是否有注释)。文件不存在时当空配置。"""
    try:
        with open(path) as f:
            raw = f.read()
    except FileNotFoundError:
        return {}, False
    except OSError as e:
        die("读不了 %s(%s)" % (path, e))

    stripped = strip_comments(raw)
    try:
        got = json.loads(stripped) if stripped.strip() else {}
    except ValueError as e:
        die("%s 不是合法 JSON/JSONC(%s)。先修好它再重跑,以免覆盖掉你的配置"
            % (path, e))
    if not isinstance(got, dict):
        die("%s 的内容不是一个 JSON 对象,拒绝改写" % path)
    return got, stripped != raw


def save(path, data):
    """先写同目录临时文件再 os.replace:中途失败不会留下半截配置。"""
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


def spec_of(item):
    """取条目的插件标识。条目可以是字符串,也可以是 [标识, 选项] 这种二元组。"""
    if isinstance(item, str):
        return item
    if isinstance(item, list) and item and isinstance(item[0], str):
        return item[0]
    return None


def is_ours(item):
    spec = spec_of(item)
    return bool(spec) and bool(OURS_RE.search(spec.split("?")[0]))


def entries(config):
    plugins = config.get("plugin")
    if plugins is None:
        return []
    if not isinstance(plugins, list):
        die("配置里的 plugin 不是数组,拒绝改写")
    return plugins


def strip_ours(config):
    """摘掉所有 3dai-led 的插件条目,返回摘掉的条数。用户其他插件原样留着。"""
    plugins = entries(config)
    kept = [item for item in plugins if not is_ours(item)]
    removed = len(plugins) - len(kept)
    if kept:
        config["plugin"] = kept
    else:
        config.pop("plugin", None)   # 空数组不留在配置里
    return removed


def show(path):
    config, _ = load(path)
    found = [spec_of(item) for item in entries(config) if is_ours(item)]
    if not found:
        print("opencode 配置里没有 3dai-led 插件 —— %s" % path)
        return
    print("已配置 %d 个 opencode 插件 —— %s" % (len(found), path))
    for spec in found:
        print("  %s" % spec)


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("action", choices=["install", "remove", "show"])
    ap.add_argument("--config", required=True)
    ap.add_argument("--plugin", help="opencode_plugin.ts 的绝对路径")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.action == "show":
        show(args.config)
        return

    config, had_comments = load(args.config)
    before = json.dumps(config, sort_keys=True)
    removed = strip_ours(config)

    if args.action == "install":
        if not args.plugin:
            die("install 需要 --plugin")
        spec = "file://" + os.path.realpath(args.plugin)
        config.setdefault("plugin", []).append(spec)
        summary = "替换 %d 个旧插件条目,写入 %s" % (removed, spec)
    else:
        summary = "移除 %d 个插件条目" % removed

    # 已经是想要的样子就一个字节都不写。这不只是省事:重写会把 .jsonc 里的注释抹掉,
    # 而重装是常态操作 —— 让它只在第一次真正改动时付出这个代价。
    if json.dumps(config, sort_keys=True) == before:
        print("opencode 配置已是最新,未改动")
        return

    if args.dry_run:
        print("[dry-run] %s(未落盘)" % summary)
        return

    bak = backup(args.config)
    save(args.config, config)
    print(summary + (",原文件备份为 %s" % os.path.basename(bak) if bak else ""))
    if had_comments:
        sys.stderr.write(
            "  ! %s 里的注释在改写时被丢弃了,原文见上面那份备份\n" % args.config)


if __name__ == "__main__":
    main()
