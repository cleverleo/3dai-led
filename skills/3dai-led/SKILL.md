---
name: 3dai-led
description: 3dai-led 硬件状态灯 — ESP32 + WS2812 灯带,HTTP 控制 8 颗灯珠实时反映 AI 工作状态。按工作目录抢占灯珠槽位,支持多目录 / 多会话并行。用于查询灯的状态、调试灯不亮、接入编码工具、调整灯珠分配或亮度。
---

# 3dai-led 状态灯

ESP32 + WS2812 灯带(8 颗灯珠),通过局域网 HTTP 控制。让 AI 编码工具在各生命周期事件切换灯效,一眼就能看到它当前在做什么。

两层结构,都与具体工具无关:

- **HTTP API** — 设备本身的接口,任何能发 HTTP 请求的东西都能驱动它
- **`led.sh`**(+ 同目录的 `lease.py`)— 槽位租约脚本,解决多个工具 / 多个工作目录 / 多个会话并行时争抢灯珠的问题

接入方式取决于你用的工具能不能在生命周期事件上执行命令。下面给了 Claude Code 的完整配置作为示例,其他工具同理。

---

## 安装

```bash
./install.sh --host 192.168.1.42   # 首次安装:必须指定设备地址
./install.sh                       # 重装:沿用 settings.json 里已有的地址
./install.sh --dry-run             # 先看会改什么
./uninstall.sh                     # 卸载(默认保留 leases.json / debug.log,加 --purge 一起删)
```

`install.sh` 做四件事:清理旧版残留的脚本副本、建数据目录、装 `SKILL.md`、把 13 条 hook 写进 `~/.claude/settings.json`(写前自动备份成 `settings.json.bak-<时间戳>`,只动 3dai-led 自己的条目,同一事件下你挂的其他命令原样保留)。可以反复跑,不会累积重复项。

**代码是就地引用的** —— hook 里写的是本仓库中 `led.sh` 的绝对路径,不复制、不做软链接。改了代码立刻生效,代价是仓库不能挪窝:移动或删除之后重跑一次 `./install.sh` 即可。

唯一的例外是 `SKILL.md`:Claude Code 只从 `~/.claude/skills/` 加载技能,那里放不下一个"指针",所以默认复制过去(纯文档,无路径依赖),改了它要重跑安装。想让它跟着仓库自动更新就用 `--skill link`,不装技能用 `--skill skip`。

运行时数据(`leases.json`、`debug.log`)和设备地址(`host`)都落在 `--data-dir`,默认就是仓库里的 `data/`(已 gitignore)。代码、数据、配置全在仓库内,`~/.claude` 下只剩两样东西:`settings.json` 里的 hook,和上面那个技能加载点 —— 连设备地址都不写进去。换别的编码工具接入时,把 hook 换成那个工具的配置即可,其余原样不动。

从旧版(脚本和数据都在 `~/.claude/3dai-led`)升上来时,`install.sh` 会把 `leases.json`、`debug.log` 搬到新位置再删掉那个目录,槽位分配和排查记录不会断。

装完在 Claude Code 里打开一次 `/hooks` 菜单可触发配置重载。其余选项见 `./install.sh --help`。

---

## HTTP API

设备自带 WebUI 和 HTTP 接口,curl 即可控制,不依赖任何额外服务。

| 端点 | 参数 | 说明 |
|------|------|------|
| `/` | `?lang=zh\|en` | WebUI 控制面板(浏览器打开) |
| `/status` | — | JSON 设备状态 |
| `/set` | `?led=<0-7>&s=<state>` | 设置指定灯珠的状态 |
| `/mode` | `?m=<0\|1>` | `0` 单项目(整条灯带一个状态),`1` 多项目(8 颗灯珠独立) |
| `/brightness` | `?b=<1-255>` | 全局亮度,默认 128 |
| `/idle_timeout` | `?t=<0-3600>` | 闲置自动待机秒数,0=常亮 |
| `/ble/clearbonds` | — | 清除 BLE 配对 |

### 状态值

`/set` 的 `s=` 只接受这 8 个值:

| 状态 | 灯效 |
|------|------|
| `thinking` | 高速彩虹旋转 |
| `coding` | 青→紫液态呼吸 |
| `busy` | 黄色双向扫描 |
| `waiting` | 红色呼吸 |
| `success` | 绿色呼吸 |
| `error` | 红→橙三连快闪 |
| `alarm` | 红蓝全灯带翻转 |
| `off` | 全灭待机 |

### 常用命令

```bash
DEV=http://192.168.1.100                 # 换成你的设备地址

curl -s $DEV/status                      # 查设备状态
curl -s "$DEV/set?led=0&s=coding"        # 0 号灯珠设为 coding
curl -s "$DEV/set?led=3&s=off"           # 熄灭 3 号灯珠
curl -s "$DEV/mode?m=0"                  # 切单项目模式(整条灯带一个状态)
curl -s "$DEV/mode?m=1"                  # 切多项目模式(8 颗灯珠独立)
curl -s "$DEV/brightness?b=64"           # 调暗
curl -s "$DEV/idle_timeout?t=0"          # 关闭闲置待机
```

`/status` 返回的 `led_states` 是长度为 8 的数组,即每颗灯珠的当前状态:

```json
{"state":"coding","brightness":128,"ip":"192.168.1.100","multi_led":true,
 "idle_timeout":1800,"led_states":["coding","off","off","off","off","off","off","off"],
 "states":["thinking","coding","busy","waiting","success","error","alarm","off"]}
```

`multi_led` 就是 `/mode` 的当前值(`true` = 多项目)。**单项目模式下 `/set` 的 `led` 参数会被忽略** —— 请求打到全局状态,整条灯带统一表现它,回读时落在 `led_states[0]`。所以单项目模式下发 `?led=3&s=busy`,变的是 0 号而不是 3 号。

**接口无认证**,仅限可信内网使用,不要做端口转发暴露到公网。

---

## Shell:槽位租约

直接写死灯珠索引的话,多个工作目录(git worktree、不同项目)、多个会话、或同一目录里并行的多个工具会互相覆盖,灯反映的是最后一个发消息的那个,失去参考价值。

`skills/3dai-led/led.sh` 用租约机制解决:**按 (平台, 工作目录) 动态抢占一颗空闲灯珠,会话结束归还**。脚本靠自身路径找同目录的 `lease.py`,所以放哪、怎么调用(绝对路径、软链接、加进 `PATH`)都行。

### 命令

```bash
led.sh <state> [platform]   # 抢占或复用当前 (平台, 目录) 的槽位并点灯
led.sh release [platform]   # 归还租约;该 (平台, 目录) 已无会话时熄灯并释放槽位
led.sh status               # 查看槽位分配表
led.sh reset                # 清空全部租约并熄灭所有灯珠
```

```
$ led.sh status
灯珠  归属
  0   [claude] /Users/me/work/project-a  (2 个会话, 3 秒前活动)
  1   [codex ] /Users/me/work/project-a  (1 个会话, 41 秒前活动)
  2   [claude] /Users/me/work/project-b  (1 个会话, 12 秒前活动)
  3   —
```

### 平台:同一目录里并行的不同工具

同一个目录同时开着 Claude Code 和 codex 是常态。如果归属只按目录算,两边会共用一颗灯珠、状态互相覆盖,而且谁先结束会话就把对方的灯一起熄了。

所以归属粒度是 **(平台, 工作目录)**:两个工具各抢一颗灯珠,`release` 只影响自己那条租约。平台名按以下优先级取:

1. **第二个位置参数** — `led.sh coding codex`。hook 配置里推荐用这种,一眼能看出这条 hook 是谁挂的
2. **`LED_PLATFORM` 环境变量** — 不方便加参数的调用方用这个
3. 都没有则归到 `cli`,和任何带平台名的租约互不干扰(所以在终端手动敲 `led.sh thinking` 排查时,不会顶掉正在跑的会话的灯)

平台名是自由字符串,只有一条限制:`:` 是租约表 key 的分隔符,出现时会被替换成 `-`。

从旧版升级时,租约表里裸目录名的 key 会被自动认作 `claude` 的租约,正在跑的会话不掉租约、不换灯珠。

### 会话标识的两种来源

引用计数需要区分同一平台、同一目录下的不同会话,脚本按以下优先级取标识:

1. **stdin 的 JSON** — 读 `session_id` 字段(工具以 hook 形式喂入时用这个)
2. **`LED_SESSION_ID` 环境变量** — 不走 stdin 的调用方用这个
3. 都没有则退化为 `anon`,同目录所有会话被当成一个

```bash
# 形式一:管道喂 JSON
echo '{"session_id":"abc123"}' | led.sh coding

# 形式二:环境变量,适合脚本 / 编辑器插件 / 任意进程
LED_SESSION_ID=$$ led.sh coding
LED_SESSION_ID=$$ led.sh release
```

stdin 不是终端时脚本会尝试读取,但有 1 秒超时,调用方不喂数据也不会挂住。

### 机制

租约全部存在一个 JSON 里。`led.sh` 只管发 HTTP 和记日志,读写租约交给同目录的 `lease.py`:

```json
// $LED_DATA_DIR/leases.json,默认 <repo>/data/leases.json
{"claude:/Users/me/work/project-a": {"slot": 0, "ts": 1784962322, "sids": ["<session_id>"]}}
```

- **归属按 (平台, 工作目录)** — 同一平台同一目录的多个会话共享一颗灯珠,靠 `sids` 做引用计数,**最后一个**退出时才熄灯并归还;不同平台是两条独立租约
- **没有锁,稳定态只读** — cwd 已持有灯珠、会话已登记、`ts` 距今不到 `LED_TS_THROTTLE` 秒时,直接用记录里的灯珠号,一个字节都不写。一轮对话十几次 hook,只有第一次会写文件
- **写入原子** — 先写同目录临时文件再 `os.replace()`,读者要么看到旧的完整 JSON、要么看到新的,绝不会读到半截。两个进程恰好撞进同一个读-改-写窗口时,后写的会覆盖前者:最坏是两个目录映射到同一颗灯(状态互相覆盖),或某个条目丢失(下次点灯重新抢占,自愈)。对一个状态指示灯来说,这比维护一把锁划算 —— 锁会把 `curl` 的 2 秒超时算进持锁时间,设备离线时反过来让其他目录的灯集体不亮
- **过期回收** — 崩溃或 `kill -9` 时清理逻辑跑不到,租约会残留。`ts` 超过 `LED_STALE_MIN` 未更新即视为泄漏,由下一次走写路径的调用顺手回收并熄灯
- **满槽降级** — 8 颗灯全被占用时,新会话静默不点灯,不报错、不影响工作

### 单 / 多项目模式自动切换

灯珠模式跟着**租约条数**自动走,不需要手动切:

| 租约数 | 模式 | 效果 |
|--------|------|------|
| ≤ 1 | `m=0` 单项目 | 整条灯带一起表现这一个状态,视觉上比只亮一颗醒目得多 |
| ≥ 2 | `m=1` 多项目 | 8 颗灯珠各自独立,一颗对一条租约 |

数的是租约条数而不是目录数 —— 同一目录里 claude 和 codex 并行也是两颗灯,同样要切到多项目模式。

三个实现要点:

- **只在写路径发 `/mode`,且无条件发** —— 快路径完全不碰它。不去持久化"当前模式"再比对,因为写路径本来就稀疏(每目录每 `LED_TS_THROTTLE` 秒一次),多发一个请求换来的是自动纠偏:有人从 WebUI 手动切过、或某次请求丢了,下次写路径就掰回来
- **单项目模式强制归到 0 号灯珠** —— 因为该模式下 `led` 参数被忽略、状态落在 `led_states[0]`。如果唯一的目录持有的是 3 号,切回多项目模式时 0 号会亮着一个没有主人的状态,而真正的主人在 3 号上却是灭的。所以退回单项目时会把它迁到 0 号,并熄掉旧槽位
- **顺序是「先熄灯 → 再切模式 → 再点灯」** —— 熄灯必须发生在切模式之前(那时 `led` 参数还有效,不会打到全局),点灯必须紧跟切模式(否则单项目模式下整条灯带会空着)

`release` 时**不**切模式。此刻切到单项目模式,整条灯带会去表现"最后一次 `set`"的值 —— 也就是刚发出去的那个 `off`,剩下那个目录的灯会莫名熄灭。留给它下次点灯时切,那时切模式和点亮状态在同一次调用里,不留空档。代价是从多项目退回单项目最多迟 `LED_TS_THROTTLE` 秒生效,期间维持原样。

`ts` 的节流顺带定了回收时机:活跃目录每 `LED_TS_THROTTLE` 秒必然走一次写路径,回收就挂在写路径上,不必每次点灯都扫一遍。所以别人残留的灯珠最迟在 `LED_STALE_MIN` 到期后再过 `LED_TS_THROTTLE` 秒被熄掉。

被 `SIGKILL` 的会话,其 `session_id` 会一直留在 `sids` 里,导致同目录最后一个会话退出时也不熄灯;此后 `ts` 不再刷新,整条租约会在 `LED_STALE_MIN` 之后被回收。

### 环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `LED_HOST` | 见下 | 设备地址(IP 或主机名) |
| `LED_DATA_DIR` | `<repo>/data` | 租约表和日志的目录;默认由脚本自身位置推出,不依赖 `~/.claude` |
| `LED_SLOTS` | `8` | 灯珠总数 |
| `LED_STALE_MIN` | `30` | `ts` 过期分钟数 |
| `LED_TS_THROTTLE` | `60` | 刷新 `ts` 的最小间隔秒数,期间只读不写 |
| `LED_PLATFORM` | `cli` | 平台名,见上;第二个位置参数优先于它 |
| `LED_SESSION_ID` | — | 会话标识,见上 |
| `LED_PYTHON` | — | 指定 python3 路径;默认取 `PATH` 里的,回退 `/usr/bin/python3` |
| `LED_DEBUG` | `0` | 日志总开关。设为 `1` 时每次点灯往 `debug.log` 追加一行(含触发事件、工具名、curl 返回码);默认一个字节都不落盘 |

设备地址按三级回退取:`LED_HOST` 环境变量 > `$LED_DATA_DIR/host`(`install.sh --host` 写的)> 占位符 `192.168.1.100`。中间那级是关键 —— 从 hook 调用时地址可以由工具的 env 注入,但你在终端里手动敲 `led.sh status` 排查问题时没有那个 env,少了它就会静默打向一个不存在的地址,灯不亮也不报错。改设备地址重跑 `./install.sh --host <新地址>` 即可,或直接改 `data/host` 这一行。

用主机名要先确认解析得通,而且**要用真正能解析的那个形式**:

```bash
curl -s -m 3 http://<主机名>/status     # 通了才用它
```

同一台设备,裸主机名和 `<主机名>.local` 未必都行 —— 前者走网络里的 DNS(路由器把 DHCP 主机名注册进去),后者走 mDNS,两条路径互相独立。哪个通用哪个,别想当然加 `.local`。

`LED_STALE_MIN` 是个权衡:调小能更快回收崩溃残留,但**空闲超过该时长的活跃会话也会被回收**(`ts` 只在有活动时刷新)。被回收后下次操作会自动重新抢占,功能不受损,但可能换到另一颗灯珠。

`LED_TS_THROTTLE` 调大能进一步减少写盘和撞车概率,代价是过期回收更迟;调到 0 就退化成每次点灯都写文件。

---

## 接入编码工具

通用做法:在工具的生命周期事件上执行 `led.sh <state> <platform>`,并在会话结束时执行 `led.sh release <platform>`。只要能挂命令,就能接。平台名随便起,同一个工具的所有 hook 用同一个即可 —— 它决定了这个工具在每个目录里独占一颗灯珠。

事件到灯效的映射建议:

| 时机 | 状态 |
|------|------|
| 收到用户输入 | `thinking` |
| 编辑 / 写入文件 | `coding` |
| 执行终端命令 | `busy` |
| 等待用户确认 | `waiting` |
| 一轮完成 | `success` |
| 出错 | `error` |
| 崩溃 / 异常 | `alarm` |
| 会话结束 | `release` |

两条通用约束:

1. **点灯要异步** — 设备离线时 `curl -m 2` 会阻塞满 2 秒,同步调用会让每次操作都多等这么久
2. **释放要同步** — 异步的清理在进程退出时可能来不及跑完,槽位就泄漏了

### 示例:Claude Code

配置在 `~/.claude/settings.json`,全局生效(所有项目共用这 8 颗灯珠)。**这份配置由 `./install.sh` 自动写入**,下面列出来是为了说明它长什么样、以及为什么这么挂 —— 手改也行,但重跑安装会以这张表为准覆盖回去。Claude Code 会把含 `session_id` 的 JSON 从 stdin 喂给 hook,所以无需设 `LED_SESSION_ID`;平台名 `claude` 作为第二个参数写死在每条命令里。

| 事件 | matcher | → 状态 |
|------|---------|--------|
| UserPromptSubmit | — | thinking |
| PreToolUse | `Edit\|Write\|NotebookEdit` | coding |
| PreToolUse | `Bash` | busy |
| PostToolUseFailure | — | error |
| PermissionRequest / Notification | — | waiting |
| SubagentStart | — | thinking |
| PreCompact | — | busy |
| PostCompact | — | thinking |
| Stop | — | success |
| StopFailure | — | alarm |
| SessionEnd | — | `release` |

```json
{
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [ { "type": "command", "async": true,
                     "command": "\"/path/to/repo/skills/3dai-led/led.sh\" thinking claude" } ] }
    ],
    "PreToolUse": [
      { "matcher": "Bash", "hooks": [ { "type": "command", "async": true,
                     "command": "\"/path/to/repo/skills/3dai-led/led.sh\" busy claude" } ] }
    ],
    "SessionEnd": [
      { "hooks": [ { "type": "command", "timeout": 8,
                     "command": "\"/path/to/repo/skills/3dai-led/led.sh\" release claude" } ] }
    ]
  }
}
```

路径是安装时填进去的绝对路径 —— hook 由 Claude Code 直接执行,不经过 shell 展开,所以不能写 `~`,`$HOME` 也只是碰巧被支持,不如绝对路径可靠。

除 `SessionEnd` 外全部 `async: true`;`SessionEnd` 不设 `async`,对应上面那两条通用约束。

**只能配一处** — 全局和项目级同时配会双重触发,异步的时序不确定会导致熄灯被后到的请求覆盖。

**不要给 `SubagentStop` 挂灯效** — 实测它在每轮结束时都会触发,即使这一轮压根没启动过 subagent,而且**排在 `Stop` 之后约 1 秒**。给它挂 `thinking` 会把 `Stop` 刚点亮的 `success` 盖掉,表现为「一轮结束后灯还在转彩虹,从来不变绿」。subagent 结束不需要单独的灯效,下一个 `PreToolUse` 或 `Stop` 自然会接上。

**也不要给 `PostToolUse` 挂 `thinking`** — 曾经挂过,是「`coding` 根本看不见」的根因。它每个工具调用都触发一次,而 `Edit` 通常只花一两秒:

```
15:11:19	s=coding  	PreToolUse 	Edit
15:11:21	s=thinking	PostToolUse	Edit    ← 2 秒后就被擦回彩虹
15:11:25	s=coding  	PreToolUse 	Edit
15:11:27	s=thinking	PostToolUse	Edit
```

液态呼吸一个周期就要一两秒,连一次完整呼吸都走不完,视觉上只是彩虹里偶尔闪一下别的颜色,读不出信息。而且这条 hook 不带任何新信息 —— 它只是把灯还原。

去掉之后灯保持在**最近一次动作**的颜色上:连续编辑期间稳定青紫呼吸,跑命令期间稳定黄色扫描,直到下一个 `PreToolUse`、`Stop` 或 `PermissionRequest` 才变。代价是 `thinking` 彩虹只在「提交提问后到第一次工具调用前」和纯对话轮次出现,读文件、搜索这类无 hook 的工具期间灯不变 —— 恰好也对,那些阶段本来就该算「在想」。附带好处:`PostToolUseFailure` 的 `error` 不会再被同一次调用的 `thinking` 抢掉。

同理,`PostCompact → thinking` 要留着 —— 它不是高频还原,而是配对 `PreCompact → busy` 的收尾,少了它压缩结束后黄扫描会一直卡住。

同理,别试图用 `sleep` 给 `Stop` 争抢最后一棒——延后只会让它更容易撞上后面的事件。要排查覆盖顺序,设 `LED_DEBUG=1` 看 `debug.log` 里的 `event=` 字段,直接定位是哪个事件发的。

#### SessionEnd 的语义

`/clear` 也会触发 `SessionEnd`,但会话其实还在继续。脚本据 stdin JSON 里的 `reason` 区分:

| `reason` | 会话真的结束了 | 脚本行为 |
|----------|--------------|---------|
| `clear` | 否,`/clear` 后继续 | 保住租约,只刷新心跳 |
| `resume` | 否,稍后恢复 | 保住租约,只刷新心跳 |
| `logout` | 是 | 释放并熄灯 |
| `prompt_input_exit` | 是(Ctrl+D) | 释放并熄灯 |
| `bypass_permissions_disabled` | 是 | 释放并熄灯 |
| `other` | 是 | 释放并熄灯 |

不区分的话,按一次 `/clear` 灯会灭一下再亮,且**可能换灯珠** —— 空档期里别的目录可能抢先占了原槽位。

注意 `Stop` 和 `SessionEnd` 粒度不同:`Stop` 是**每轮回答结束**都触发,`SessionEnd` 是整个会话终止时才触发一次。

设 `LED_DEBUG=1` 时,每次 `SessionEnd` 会在 `debug.log` 留一行带 `reason=` 和 `cwd=` 的记录。官方文档未说明「直接关终端窗口」属于哪种 reason,查这个字段可以确认;如果开着 debug 却没有这行,说明进程被 SIGKILL、清理没跑到,只能靠 `ts` 过期兜底回收。

### 示例:任意进程 / 脚本

没有 hook 机制的工具,或者想在自己的构建脚本里用:

```bash
export LED_SESSION_ID=$$
export LED_PLATFORM=build      # 不写就归到 cli,和其他 cli 调用共用一颗灯珠
trap 'led.sh release' EXIT     # 退出时归还,含异常退出

led.sh busy
./long-running-build.sh || led.sh error
led.sh success
```

两个环境变量都可以换成位置参数(`led.sh busy build`),环境变量的好处是 `trap` 那行不用重复写平台名。

---

## 排查

**灯完全不亮**

```bash
curl -s -m 3 http://192.168.1.100/status    # 1. 设备是否可达
led.sh status                               # 2. 当前目录是否拿到槽位
```

设备不可达先确认在同一局域网(设备 SSID 见 `/status` 的 `ssid` 字段)。槽位表满了则是 8 颗灯都被占,`reset` 或等 `ts` 过期。若用 Claude Code 且改了配置不生效,打开一次 `/hooks` 菜单可触发重载。

**所有目录的灯突然全不亮** — 多半是仓库被移动或删除了,hook 里的绝对路径成了死链。核对一下:

```bash
python3 scripts/hooks_config.py show --settings ~/.claude/settings.json
```

打印出来的路径若指向一个不存在的位置,在仓库新位置重跑 `./install.sh` 即可。

若 `led.sh status` 报错或没有输出,确认 python3 在 hook 继承的 `PATH` 里(`lease.py` 靠它跑),必要时用 `LED_PYTHON` 显式指定:

```bash
command -v python3 || ls -l /usr/bin/python3    # 至少要有一个
LED_PYTHON=/usr/bin/python3 led.sh status
```

注意 `led.sh <state>` 的状态值是直接透传给设备的,拼错不会报错,设备只是忽略——灯不变是正常表现。

**灯亮着但状态不对** — 先设 `LED_DEBUG=1` 跑一轮,看 `$LED_DATA_DIR/debug.log`(默认 `<repo>/data/debug.log`)里最后落地的是哪个 `event=`:

```
2026-07-25 15:13:01	led=all	s=single  	plat=claude	event=PreToolUse	tool=Edit	rc=0
2026-07-25 15:13:01	led=0 	s=coding  	plat=claude	event=PreToolUse	tool=Edit	rc=0
2026-07-25 15:13:05	led=1 	s=busy    	plat=codex 	event=PreToolUse	tool=Bash	rc=0
2026-07-25 15:14:20	led=—	s=—     	plat=claude	event=SessionEnd	tool=—	rc=—	reason=clear	cwd=/Users/me/work/project-a
```

- `led=all` 是 `/mode` 请求,`s=single` / `s=multi` 对应 `m=0` / `m=1`
- `plat=` 是调用方自报的平台名 —— 同一目录里两个工具并行时,靠它分辨每一行是谁发的
- `led=— s=— rc=—` 是占位行,表示这次一个 HTTP 请求都没发 —— `reason=clear` 保住租约、满槽降级、以及"同目录还有别的会话在跑"都是这个形状
- 连续两行只有一行带 `led=all`,说明后面那次走了快路径(既没写 `leases.json`,也没碰 `/mode`)

状态不对常见有两种原因:同一套事件配了两处导致双重触发(检查残留的项目级配置);或者挂了 `SubagentStop` 这类会在 `Stop` 之后触发的事件,见上。

**`/status` 回读比实际慢 1–2 秒** — 设完 `success` 立刻查 `/status` 可能还报上一个状态,再查一次才更新。排查时别拿单次 `/status` 当即时真相,以 `debug.log` 的发送记录为准。

**灯太暗** — `/status` 看 `brightness`,`curl "$DEV/brightness?b=128"` 调整。

**槽位泄漏** — `led.sh reset` 清空全部租约。
