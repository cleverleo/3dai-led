# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 这是什么

主机侧的接入代码,把 AI 编码工具的生命周期事件翻译成一条 ESP32 + WS2812 灯带(8 颗灯珠)的灯效。**设备固件不在本仓库**,是买来的成品,HTTP 接口是既有事实,不可改 —— 所有设计约束都来自它。

没有构建系统、没有依赖、没有测试套件。只有 bash + python3 标准库,macOS/Linux 自带即可跑。

## 常用命令

```bash
./install.sh --host <ip>           # 首次安装必须给 --host;之后重装可省略
./install.sh --dry-run             # 看会改什么,不落盘(uninstall.sh 也支持)
./uninstall.sh                     # 卸载(--purge 连 data/ 一起删)

skills/3dai-led/led.sh status      # 槽位分配表 —— 改完租约逻辑第一个该看的
skills/3dai-led/led.sh reset       # 清空全部租约并熄灯,回到干净状态
LED_DEBUG=1 skills/3dai-led/led.sh thinking   # 手动打一次灯,往 data/debug.log 记一行

python3 scripts/hooks_config.py show --settings ~/.claude/settings.json   # 核对已装的 hook
echo '{"session_id":"t1"}' | skills/3dai-led/led.sh coding claude         # 模拟 hook 调用
```

**验证改动没有跑测试这条路**,只能实机跑:`./install.sh --dry-run` 看配置变化,`LED_DEBUG=1` 跑一轮真实对话再读 `data/debug.log`,配合 `led.sh status` 看租约表。改完 hook 表要在 Claude Code 里打开一次 `/hooks` 菜单触发重载。

改了 `skills/3dai-led/SKILL.md` 必须重跑 `./install.sh` —— 它是复制到 `~/.claude/skills/` 的,不是软链接(除非装的时候用了 `--skill link`)。其他代码就地引用,改完立即生效。

## 架构

三层,层间接口窄且刻意与具体编码工具无关:

```
编码工具 hook  ──stdin JSON──▶  led.sh  ──stdin 透传──▶  lease.py
(settings.json)                    │                        │
                                   │      ◀──\037 分隔的一行──┘
                                   └──curl──▶  设备 HTTP API
```

- `scripts/hooks_config.py` —— 只在安装/卸载时跑,读写 `~/.claude/settings.json`。`HOOKS` 表(事件 → 状态 → 是否异步)是 Claude Code 接入的唯一真相来源。
- `skills/3dai-led/led.sh` —— 唯一发 HTTP 和写日志的地方。不解析租约,不做决策。
- `skills/3dai-led/lease.py` —— 唯一读写 `data/leases.json` 的地方。不发 HTTP,不知道设备存在。

一切路径从 `led.sh` 自身位置(逐级解析符号链接后)推出,**从不读 `~/.claude`**。`~/.claude` 下只有两样东西:`settings.json` 里的 hook,和技能加载点 `skills/3dai-led/SKILL.md`。换一个编码工具接入只需换前者。

### 就地引用

hook 里写的是本仓库 `led.sh` 的**绝对路径**,不复制、不软链接。因此仓库一旦移动或删除,所有目录的灯集体失效 —— 这是最常见的故障,排查先跑 `hooks_config.py show` 看路径是不是死链。

### 租约:按 (平台, 工作目录) 抢灯珠

`data/leases.json` 形如 `{"<platform>:<cwd>": {"slot": 0, "ts": <epoch>, "sids": [...]}}`。归属粒度是 **(平台, 工作目录)** —— 同一目录里并行的 claude 和 codex 各占一颗灯珠,`release` 只影响自己那条;同平台同目录的多个会话才共享灯珠,靠 `sids` 引用计数,最后一个退出才熄灯归还。

平台名的取值链是 `led.sh <state> [platform]` 的第二个参数 > `LED_PLATFORM` 环境变量 > `cli`。写成 hook 命令的参数而非 settings.json 的 env,是为了在配置里一眼看出哪条 hook 是谁挂的(`hooks_config.py` 里的 `PLATFORM` 常量)。旧格式的裸 cwd key 由 `migrate()` 在内存里认作 `claude` 的租约,下次写路径自然落盘。

无锁是刻意的,推导写在 `lease.py` 顶部注释里 —— 动这块之前先读完。三条不变量:

1. **稳定态零写入**。cwd 已持有灯珠 + sid 已登记 + `ts` 距今 < `THROTTLE` 时走快路径,直接返回灯珠号,不写文件、不发 `/mode`。一轮对话十几次 hook 只有第一次落盘。
2. **写入靠 `os.replace()` 原子替换**,丢更新可接受(最坏是两目录撞同一颗灯,或条目丢失后下次自愈)。不要为它引入锁 —— 锁会把 curl 的 2 秒超时算进持锁时间,设备离线时反而让所有目录的灯集体不亮。
3. **过期回收挂在写路径上**。活跃目录每 `THROTTLE` 秒必然写一次,`sweep()` 顺手清掉 `ts` 超过 `STALE` 的残留,不必每次点灯都扫。

### 几个容易踩坏的细节

- **`led.sh` 和 `lease.py` 之间用 `\037` 分隔字段,不能用制表符。** tab 属于 IFS 白空格,bash 的 `read` 会合并连续 tab,空字段(slot 或 off 为空)会让后续字段整体左移。曾导致 `release` 发出 `s=release` 而不熄灯。
- **HTTP 请求顺序固定为「先熄灯 → 再切模式 → 再点灯」。** 熄灯必须在切模式之前(那时 `led` 参数还有效);点灯必须紧跟切模式(否则单项目模式下整条灯带留空档)。
- **单项目模式下设备忽略 `/set` 的 `led` 参数**,状态落在 `led_states[0]`。所以 `normalize_single()` 必须把唯一的租约迁到 slot 0,否则切回多项目模式时 0 号会亮着一个无主状态。
- **`target_mode()` 数的是租约条数,不是目录数** —— 同一目录里两个工具并行也必须切多项目模式。
- **`:` 是租约 key 的分隔符**,平台名里出现会被 `norm_platform()` 换成 `-`;cwd 一定以 `/` 开头,所以按第一个 `:` 切一定切得对,也是判定旧格式 key 的依据。
- **`release` 不切模式**,留给下次点灯时切 —— 此刻切成单项目,整条灯带会去表现刚发出的那个 `off`。
- **`SessionEnd` 的 `reason` 是 `clear` / `resume` 时不能释放租约**,会话还在继续,释放会导致灯灭一下再亮且可能换灯珠。
- **`SessionEnd` 的 hook 不能设 `async`**(用 `timeout: 8`),异步清理在进程退出时来不及跑完会泄漏槽位;其余 hook 必须 `async`,否则设备离线时每次操作都多等 2 秒。
- **不要给 `SubagentStop` 挂灯效** —— 它每轮结束都触发,且排在 `Stop` 之后约 1 秒,会把 `success` 盖成 `thinking`。
- **`PostToolUse` 只挂 `Bash`**(Claude 和 Codex 两边一致),`matcher` 既不能放宽也不能整条摘掉 —— 两头都踩过坑。全挂时每个工具都把灯还原成 `thinking`,而 `Edit` 只花一两秒,`coding` 的呼吸连一个周期都走不完就被彩虹擦掉(「coding 从来看不见」);整条摘掉后 `Bash` 跑完没人还原,而 `Read`/`Grep`/`Task` 压根没挂 hook,灯冻在黄扫描上 —— 实测 `Bash` 密集的会话干活期间 90% 时间是 `busy`,黄色退化成背景色。`Bash` 常跑几十秒、`Edit` 只有一两秒,时长形状相反,所以按工具区别对待。`PostCompact → thinking` 同样要留:它是 `PreCompact → busy` 的收尾,少了它黄扫描会卡住。
- **`hooks_config.py` 认 hook 靠形状**(`OURS_RE`:`led.sh` + 合法状态词),不认路径 —— 旧安装、软链接、另一份副本都能认出来,重装才真正幂等。反过来也不能只认路径里的 `3dai-led`,那会误删用户挂在同目录下的其他脚本。
- **设备地址三级回退**:`LED_HOST` 环境变量 > `data/host` 文件 > 占位符。中间那级是关键,少了它在终端手动排查时会静默打向一个不存在的地址。
- **curl 永不阻断调用方**:`-m 2` 超时,失败静默,`poke()` 永远 `return 0`。

## 写代码的调子

注释密度高,而且**写的是"为什么"不是"是什么"** —— 每个非显然的取舍都留了推导(`lease.py` 顶部的无锁论证、`led.sh` 里三步顺序的解释、`hooks_config.py` 里 `OURS_RE` 的双向约束)。多数注释对应一个真实踩过的坑。改动这些区域时,如果推翻了某条取舍,要连注释一起改,别留下和代码矛盾的说明。

注释、文档、脚本输出全部用中文。

## 文档

`skills/3dai-led/SKILL.md` 是完整文档(状态值表、HTTP API、环境变量、模式切换时序、排查步骤),同时是给 Claude Code 读的技能说明。`README.md` 是面向 GitHub 读者的精简版。两份有意重叠,**改了行为要同时更新**;安装参数或 hook 表变了,`install.sh --help` 也要跟着改。
