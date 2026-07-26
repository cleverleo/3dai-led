# 3dai-led

> **说明**:硬件不是我做的 —— 灯是在淘宝买的成品,固件和 HTTP 接口都是卖家的。只是配套软件用着不顺手,才自己写了这套主机侧的接入。本仓库只包含这部分,不含设备固件。

让 AI 编码工具的状态离开屏幕,变成桌上一条看得见的灯。

ESP32 + WS2812 灯带(8 颗灯珠),局域网 HTTP 控制。编码工具在各生命周期事件上打一次 HTTP,灯就跟着变 —— 不用切回终端窗口,余光扫一眼就知道它在想、在写、在跑命令,还是卡在那儿等你点确认。

```
thinking   高速彩虹旋转      收到你的输入,正在想
coding     青→紫液态呼吸     在编辑文件
busy       黄色双向扫描      在跑终端命令
waiting    红色呼吸          等你确认
success    绿色呼吸          一轮做完了
error      红→橙三连快闪     出错了
alarm      红蓝全灯带翻转    崩了
off        全灭              待机
```

## 它解决的问题

一颗灯很好点。麻烦的是**多个工作目录、多个会话、多个工具同时在跑**:直接写死灯珠索引的话,灯反映的永远是最后一个发消息的那个,失去参考价值。

所以 8 颗灯珠按 **(平台, 工作目录)** 动态分配:每个组合抢占一颗,同一组合下的多个会话共享同一颗并做引用计数,最后一个退出时才熄灯归还。平台名让同一个目录里并行的 Claude Code 和 codex 各占一颗灯,状态不互相覆盖,一方结束也不会顺手熄掉另一方的灯。只有一条租约在跑时自动切成整条灯带表现同一个状态(比只亮一颗醒目得多),第二条进来时自动切回独立模式。

租约表是一个无锁 JSON:稳定态纯读不写(一轮对话十几次 hook,只有第一次落盘),写入靠 `os.replace()` 原子替换,崩溃残留的租约由超时回收。取舍的完整推导写在 [`skills/3dai-led/lease.py`](skills/3dai-led/lease.py) 顶部。

## 前提

- 一台刷好固件的 ESP32 + WS2812 灯带,和电脑在同一局域网。**本仓库是主机侧的接入部分,不含设备固件。**
- `bash` 和 `python3`(macOS / Linux 自带即可)

设备自带 WebUI 和 HTTP 接口,浏览器打开 `http://<设备IP>/` 就能手动控制,不依赖本仓库。

## 安装

```bash
git clone https://github.com/cleverleo/3dai-led.git
cd 3dai-led
./install.sh --host <你的设备IP>
```

装完分别在 Claude Code 和 Codex 里打开一次 `/hooks`,重载并信任 hook,然后随便说句话 —— 灯应该开始转彩虹。

`--host` 首次安装必填 —— 猜一个默认值只会装出一套指向不存在设备、灯不亮也不报错的配置。之后重装可以省略,沿用 `settings.json` 里已有的地址。

`install.sh` 会把 Claude 的 13 条 hook 写进 `~/.claude/settings.json`,并把 Codex hooks 写进 `~/.codex/hooks.json`。两边写前都会自动备份,只动本项目自己的条目,用户已有命令原样保留;可以反复运行,不会累积重复项。`./uninstall.sh` 会同时卸载两边配置。两个脚本都支持 `--dry-run`。

**代码是就地引用的** —— hook 里写的是本仓库中 `led.sh` 的绝对路径,不复制、不做软链接。改了代码立刻生效,代价是仓库不能挪窝:移动之后重跑一次 `./install.sh`。

`--host` 接 IP 或主机名。用主机名先确认解析得通,而且要用真正能解析的那个形式 —— 同一台设备,裸主机名(走网络 DNS)和 `<主机名>.local`(走 mDNS)未必都行,`curl -s -m 3 http://<主机名>/status` 通了才用它。

`~/.claude` 下只留两样东西:`settings.json` 里的 hook,和 Claude Code 强制要求的技能加载点 `skills/3dai-led/SKILL.md`。代码、运行时数据、设备地址全在仓库内(`data/`,已 gitignore)。

## 接 Claude Code 以外的工具

核心是两个与工具无关的层:HTTP API,和 `led.sh` 这个槽位租约脚本。只要你的工具能在生命周期事件上执行命令,就能接:

```bash
led.sh <state> [platform]   # 抢占或复用当前 (平台, 目录) 的槽位并点灯
led.sh release [platform]   # 归还租约;该 (平台, 目录) 已无会话时熄灯并释放槽位
led.sh status               # 查看槽位分配表
led.sh reset                # 清空全部租约并熄灭所有灯珠
```

平台名从第二个参数或 `LED_PLATFORM` 环境变量取,不给则归到 `cli`。会话标识从 stdin 的 JSON(`session_id` 字段)或 `LED_SESSION_ID` 环境变量取,两种都不给就退化成同一租约下一个会话。没有 hook 机制的场景直接用:

```bash
export LED_SESSION_ID=$$
export LED_PLATFORM=build
trap 'led.sh release' EXIT

led.sh busy
./long-running-build.sh || led.sh error
led.sh success
```

两条通用约束:**点灯要异步**(设备离线时 curl 会阻塞满 2 秒),**释放要同步**(异步清理在进程退出时可能来不及跑完,槽位就泄漏了)。

## 直接用 HTTP

```bash
DEV=http://192.168.1.100                 # 换成你的设备地址

curl -s $DEV/status                      # JSON 设备状态
curl -s "$DEV/set?led=0&s=coding"        # 0 号灯珠设为 coding
curl -s "$DEV/mode?m=1"                  # 8 颗灯珠独立(m=0 则整条一个状态)
curl -s "$DEV/brightness?b=64"           # 调暗
curl -s "$DEV/idle_timeout?t=0"          # 关闭闲置待机
```

接口无认证,仅限可信内网使用,**不要做端口转发暴露到公网**。

## 目录

```
install.sh                     安装:写 hook、装技能、迁移旧数据、自检
uninstall.sh                   卸载:熄灯、摘 hook、移除技能
scripts/hooks_config.py        settings.json 的 hook 读写(两个脚本共用)
scripts/codex_hooks_config.py  Codex hooks.json 的幂等安装/卸载
scripts/codex_hook.py          Codex stdin/异步适配,最终调用 led.sh
config/codex-hooks.json        Codex 生命周期事件模板(安装后写入用户配置)
skills/3dai-led/
  SKILL.md                     完整文档,也是给 Claude Code 读的技能说明
  led.sh                       槽位租约 + HTTP
  lease.py                     租约表读写
data/                          运行时数据(gitignore)
```

完整的状态值表、环境变量、单/多项目模式的切换时序、以及"灯不亮 / 状态不对"的排查步骤,都在 [`skills/3dai-led/SKILL.md`](skills/3dai-led/SKILL.md)。
