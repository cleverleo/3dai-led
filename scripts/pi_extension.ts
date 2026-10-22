/**
 * 3dai-led 的 pi 接入。
 *
 * pi 和 opencode 一样没有 shell hook —— 没有「在某个事件上执行一条命令」的配置项,
 * 只有跑在自己进程里的 TypeScript 扩展,用 pi.on() 订阅生命周期事件。所以这一层也只能
 * 是代码。职责边界和 hook 完全一样:把 pi 的事件映射成状态词,再 spawn 仓库里那个
 * led.sh,租约和 HTTP 一概不碰。
 *
 * 两条通用约束(见 SKILL.md)在这里的落法和 opencode 插件相同:
 *   · 点灯要异步 —— detached + unref 甩出去,设备离线时 curl 的 2 秒不会拖住 pi。
 *     这一条在 pi 上更要紧:pi 会 await 扩展的事件处理器,同步调用会直接卡住对话
 *   · 释放要同步 —— 归还跑不完就泄漏槽位
 *
 * 和 opencode 不同,pi 有会话结束的钩子(session_shutdown),但它只在正常退出和切换
 * 会话时触发,关终端窗口(SIGHUP)、kill -9 都等不到它。所以照搬了 opencode 插件里那个
 * 管道守护进程兜底,详见 watch()。
 *
 * 模块只默认导出工厂函数,辅助函数一律不导出,和 opencode 插件保持同一个习惯。
 * 只有 `import type` 能引 pi 的包 —— 仓库里没有 node_modules,类型导入会被 jiti 擦掉,
 * 值导入不会。
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent"
import { spawn, spawnSync, type ChildProcess } from "node:child_process"
import { fileURLToPath } from "node:url"

// 就地引用:扩展文件在 <repo>/scripts/,led.sh 在邻居目录里。settings.json 里写的就是
// 这个文件的绝对路径,改了代码重开 pi(或 /reload)即生效,代价是仓库不能挪窝。
const LED_SH = fileURLToPath(new URL("../skills/3dai-led/led.sh", import.meta.url))

// 租约按 (平台, 会话) 归属,平台名进 key 是因为 session_id 跨工具不保证不撞。
const PLATFORM = "pi"

// 工具 → 状态。**故意只列这三个**,和 Claude Code 的 PostToolUse 只挂 Bash、opencode
// 插件只列 bash/edit/write/patch 是同一条取舍:read / grep / find / ls 不挂灯,期间停在
// thinking 的彩虹上;全挂的话 coding 的呼吸连一个周期都走不完就被擦掉。
const TOOL_STATE: Record<string, string> = {
  bash: "busy",
  edit: "coding",
  write: "coding",
}

/**
 * 子进程的环境。cwd 只是 status 里的展示字段,但 pi 的 ctx.cwd 未必等于进程的 $PWD
 * (session 可以换目录),所以显式给。LED_SESSION_ID 和 stdin 里的 session_id 给两遍,
 * 理由同 opencode 插件:异步那条路的管道写丢了还有环境变量兜底。
 */
function childEnv(cwd: string, sessionID: string) {
  return {
    ...process.env,
    PWD: cwd,
    LED_PLATFORM: PLATFORM,
    LED_SESSION_ID: sessionID,
  }
}

/**
 * 调一次 led.sh。异常一律吞掉 —— 灯的死活不能影响 pi 干活。
 */
function poke(cwd: string, sessionID: string, state: string, event: string,
              extra: { tool?: string; reason?: string } = {}, sync = false): void {
  const payload = JSON.stringify({
    session_id: sessionID,
    cwd,
    hook_event_name: event,
    tool_name: extra.tool ?? "",
    reason: extra.reason ?? "",
  })
  const options = { env: childEnv(cwd, sessionID), cwd } as const

  try {
    if (sync) {
      spawnSync(LED_SH, [state, PLATFORM], {
        ...options,
        input: payload,
        stdio: ["pipe", "ignore", "ignore"],
        timeout: 8000,
      })
      return
    }

    const child = spawn(LED_SH, [state, PLATFORM], {
      ...options,
      stdio: ["pipe", "ignore", "ignore"],
      detached: true,
    })
    child.on("error", () => {})
    child.stdin?.on("error", () => {})
    child.stdin?.end(payload)
    child.unref()
  } catch {
    // led.sh 不存在(仓库被挪走了)、fork 失败 —— 都不该冒泡到 pi
  }
}

/**
 * 挂一个守护进程,pi 一死就替它把租约还回去。
 *
 * 和 opencode 插件里的 watch() 是同一个东西,推导见那边:守护进程 `cat` 一根我们持有
 * 写端的管道,pi 不管怎么死的,内核回收 fd 时 cat 读到 EOF,接着 release。sleep 1 是
 * 等 pi 死前甩出去的异步点灯落地,抢在它们前面还,租约会被后到的 acquire 重新建出来。
 *
 * pi 明明有 session_shutdown 还要它,是因为那个事件只盖住 /quit、Ctrl-D 这类正常退出;
 * 直接关终端窗口是最常见的退出方式之一,而那时扩展根本没机会跑。反过来,正常退出时也
 * 交给它还(见 session_shutdown),省得在退出路径上同步等一次 curl。
 *
 * 不用 process.on("SIGHUP") 之类的信号处理器,理由同 opencode 插件:挂上监听会顶掉
 * 默认的终止行为。
 */
function watch(cwd: string, sessionID: string): ChildProcess | null {
  try {
    const payload = JSON.stringify({
      session_id: sessionID,
      cwd,
      hook_event_name: "guard",   // debug.log 的 event= 列上认得出是守护进程还的
    })
    const child = spawn("/bin/sh", ["-c", 'cat >/dev/null 2>&1; sleep 1; printf "%s" "$2" | "$0" release "$1"',
                                    LED_SH, PLATFORM, payload], {
      env: childEnv(cwd, sessionID),
      cwd,
      stdio: ["pipe", "ignore", "ignore"],
      detached: true,
    })
    child.on("error", () => {})
    child.stdin?.on("error", () => {})
    // 故意**不**关 stdin —— 那根管道就是生命线。两个 unref 都要,否则这个不会自己结束的
    // 子进程会钉住 pi 的事件循环,退不出去。
    child.unref()
    child.stdin?.unref?.()
    return child
  } catch {
    return null
  }
}

export default function (pi: ExtensionAPI) {
  // pi-subagents 起的子代理是独立的 pi 进程,会照常加载用户级扩展。不拦的话每个子代理
  // 各抢一颗灯珠,还各自在跑完时亮一下 success —— 子代理是父会话「在干活」的一部分,
  // 灯应该只属于父会话。(后台子代理带 --no-extensions 启动,本来就不会走到这里;
  // 这条拦的是其余那些。)
  if (process.env.PI_SUBAGENT_CHILD === "1") return

  // 一个扩展运行时只对应一个会话:/new、/resume、/fork 和 /reload 都会先发
  // session_shutdown 拆掉整个运行时,再用新会话重新调一遍这个工厂函数。所以这些状态
  // 直接放在闭包里,不用按会话分桶。
  let sessionID = ""
  let sessionFile: string | undefined
  let cwd = process.cwd()
  let guard: ChildProcess | null | undefined   // undefined = 还没点过灯,没挂守护进程

  // 本轮对话是否在跑。等待确认和压缩收尾都要看它,见下。
  let running = false
  // 嵌套的 UI 提示层数。一个确认框里还能再弹一个,只有最外层开、关时才动灯。
  let prompts = 0
  // 本轮最后一条助手消息是不是以出错结束,留给 agent_settled 决定亮 alarm 还是 success。
  let failed = false

  const bind = (ctx: { cwd: string; sessionManager: { getSessionId(): string; getSessionFile(): string | undefined } }) => {
    try {
      sessionID = ctx.sessionManager.getSessionId() || sessionID
      sessionFile = ctx.sessionManager.getSessionFile() ?? sessionFile
    } catch {
      // 拿不到就沿用上次的
    }
    cwd = ctx.cwd || cwd
  }

  // 没有会话号就不点灯:lease.py 拿不到 session_id 会退化成按目录归属,两个同目录的
  // pi 会挤到同一颗灯上,而且我们没法给那条租约挂守护进程。
  const fire = (state: string, event: string, extra: { tool?: string; reason?: string } = {}) => {
    if (!sessionID) return
    if (guard === undefined) guard = watch(cwd, sessionID)
    poke(cwd, sessionID, state, event, extra)
  }

  pi.on("session_start", (_event, ctx) => {
    bind(ctx)
  })

  // 一轮开始,对应 Claude Code 的 UserPromptSubmit。用 agent_start 而不是 input /
  // before_agent_start:扩展用 sendMessage(triggerTurn) 发起的轮次也算在干活。
  pi.on("agent_start", (_event, ctx) => {
    bind(ctx)
    running = true
    prompts = 0
    fire("thinking", "agent_start")
  })

  pi.on("tool_execution_start", (event, ctx) => {
    const state = TOOL_STATE[event.toolName]
    if (!state) return
    bind(ctx)
    fire(state, "tool_execution_start", { tool: event.toolName })
  })

  // 只还原 bash,理由同 Claude Code 的 PostToolUse matcher:bash 常跑几十秒,跑完回
  // thinking 才准;edit 只有一两秒,还原反而把 coding 擦掉。
  pi.on("tool_execution_end", (event, ctx) => {
    if (event.toolName !== "bash") return
    bind(ctx)
    fire("thinking", "tool_execution_end", { tool: event.toolName })
  })

  // pi 本身不弹权限框,审批是 pi-automode 这类扩展用 ctx.ui.confirm() 做的,而所有阻塞
  // 式 UI 提示都会发这对事件 —— 对应 Claude Code 的 PermissionRequest。只在一轮进行中
  // 才算:空闲时用户自己敲命令弹出的选择框不是「AI 在等你」。
  pi.on("ui_prompt_start", (_event, ctx) => {
    if (!running) return
    bind(ctx)
    if (prompts++ === 0) fire("waiting", "ui_prompt_start")
  })

  pi.on("ui_prompt_end", (_event, ctx) => {
    if (!running || prompts === 0) return
    bind(ctx)
    if (--prompts === 0) fire("thinking", "ui_prompt_end")
  })

  // 和下面两个收尾事件配对,对应 PreCompact / PostCompact。少了收尾黄扫描会卡住。
  pi.on("session_before_compact", (_event, ctx) => {
    bind(ctx)
    fire("busy", "session_before_compact")
  })

  // 收尾时不能一律回 thinking:手动 /compact 是在空闲时跑的,回 thinking 会让灯在什么
  // 都没干的时候一直转彩虹。
  const compacted = (event: string) => fire(running ? "thinking" : "success", event)
  pi.on("session_compact", (_event, ctx) => { bind(ctx); compacted("session_compact") })
  pi.on("session_compact_failed", (_event, ctx) => { bind(ctx); compacted("session_compact_failed") })

  // 只记账不点灯。agent_end 之后 pi 还可能自动重试、压缩后续跑,这时候亮 success /
  // alarm 都为时过早 —— 真正的结束是 agent_settled。重试的那一轮会再来一次 agent_end,
  // 所以这里是赋值不是累加:最后一次说了算。
  pi.on("agent_end", (event) => {
    const last = [...event.messages].reverse().find((m: any) => m?.role === "assistant") as any
    failed = last?.stopReason === "error"
  })

  // 一轮彻底结束,对应 Stop。用户按 Esc 中断给 success 而不是 alarm,和 opencode 插件
  // 对 MessageAbortedError 的处理一致:那是用户自己的决定,不是故障。出错的 alarm 会一直
  // 留到下一个动作。
  pi.on("agent_settled", (event, ctx) => {
    bind(ctx)
    running = false
    prompts = 0
    const state = !event.aborted && failed ? "alarm" : "success"
    failed = false
    fire(state, "agent_settled")
  })

  pi.on("session_shutdown", (event) => {
    running = false
    prompts = 0
    if (guard === undefined || !sessionID) return

    // reload 换的是扩展运行时,不是会话 —— 和 Claude Code 的 SessionEnd 在 clear /
    // resume 时不能释放是同一个道理,还了灯会灭一下再亮,还可能换一颗灯珠。新运行时
    // 会自己再挂守护进程,这边的撤掉,免得退出时还两次。/resume 回到的若就是当前这个
    // 会话文件,同理。
    const keeps = event.reason === "reload" ||
      (event.reason === "resume" && !!sessionFile && event.targetSessionFile === sessionFile)
    if (keeps) {
      try { guard?.kill() } catch {}
      guard = undefined
      return
    }

    // 真退出:交给守护进程。进程马上就没了,内核关管道、守护进程等一秒再还 —— 那一秒
    // 正好让前面甩出去的异步点灯落地,比在这里同步还更不容易被它们抢先。
    // 守护进程没挂上(spawn 失败)才自己同步还。
    if (event.reason === "quit" && guard) return

    // /new、/resume、/fork:进程还活着,只是换了会话,守护进程等不到 EOF,得当场还。
    // 同步是因为接下来新会话马上会抢灯,先还完再抢,两条租约不会同时在表上把模式切错。
    try { guard?.kill() } catch {}
    guard = undefined
    poke(cwd, sessionID, "release", "session_shutdown", { reason: event.reason }, true)
  })
}
