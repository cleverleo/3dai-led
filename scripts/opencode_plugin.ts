/**
 * 3dai-led 的 opencode 接入。
 *
 * Claude Code 和 Codex 都是「在生命周期事件上执行一条命令」,配置里写死 led.sh 的
 * 绝对路径就够了。opencode 没有这种 shell hook —— 它只有跑在自己进程里的 JS 插件,
 * 所以这一层必须是代码而不是一张配置表。插件本身仍然只做翻译:把 opencode 的事件
 * 映射成状态词,再 spawn 出仓库里那个 led.sh,租约和 HTTP 一概不碰。
 *
 * 两条通用约束(见 SKILL.md)在这里的落法:
 *   · 点灯要异步 —— detached + unref 甩出去,设备离线时 curl 的 2 秒不会拖住 opencode
 *   · 释放要同步 —— 归还跑不完就泄漏槽位,所以走 spawnSync
 *
 * 最麻烦的一点是**没有会话结束的钩子**。opencode 确实有 server.instance.disposed 事件,
 * 但实测(1.18.5)插件收不到它 —— 看实现,disposeContext 是先把实例的作用域拆掉、再发
 * 这个事件,而插件的事件订阅正挂在那个作用域上,拆完就没人听了。实测跑完一次 opencode
 * 租约确实留在 leases.json 里没还。所以归还靠下面 watch() 里那个管道守护进程,那条路
 * 连 SIGKILL 都盖得住。
 *
 * 模块里只能导出这一个函数:opencode 的加载器在模块没有 `server` 导出时,会把**每个**
 * 导出的函数都当成插件调用一遍。辅助函数一律不导出。
 */

import type { Plugin } from "@opencode-ai/plugin"
import { spawn, spawnSync, type ChildProcess } from "node:child_process"
import { fileURLToPath } from "node:url"

// 就地引用:插件文件在 <repo>/scripts/,led.sh 是它的邻居目录里的。和 hook 里写死
// 绝对路径是同一个取舍 —— 改了代码立刻生效,代价是仓库不能挪窝。
const LED_SH = fileURLToPath(new URL("../skills/3dai-led/led.sh", import.meta.url))

// 租约按 (平台, 工作目录) 归属,这个名字决定了 opencode 在每个目录里独占一颗灯珠,
// 不会和同目录的 claude / codex 抢同一颗。
const PLATFORM = "opencode"

// 工具 → 状态。**故意只列这四个**,理由和 Claude Code 那边 PostToolUse 只挂 Bash 是
// 同一条:read / grep / glob / list / task 这些工具不挂灯,期间灯停在 thinking 的彩虹
// 上 —— 那些阶段本来就该算「在想」。全挂的话每个工具都来一下,coding 的液态呼吸连一个
// 周期都走不完就被擦掉,等于看不见。
const TOOL_STATE: Record<string, string> = {
  bash: "busy",
  edit: "coding",
  write: "coding",
  patch: "coding",
}

type Info = {
  sessionID?: string
  event: string
  tool?: string
  reason?: string
}

/**
 * 子进程的环境。
 *
 * lease.py 取 cwd 是读 $PWD 而不是 getcwd()(后者会解析符号链接,同一个目录会被算成
 * 两个归属)。opencode 的服务进程未必跑在项目目录里,所以 cwd 和 PWD 两个都要显式给,
 * 否则租约会落到进程启动时的那个目录上。
 * LED_SESSION_ID 和 stdin 里的 session_id 是同一个值,给两遍是因为异步那条路是往一个
 * 已经 detach 的子进程的管道里写,写丢了也还有环境变量兜底。
 */
function childEnv(directory: string, sessionID?: string) {
  return {
    ...process.env,
    PWD: directory,
    LED_PLATFORM: PLATFORM,
    LED_SESSION_ID: sessionID ?? "",
  }
}

/**
 * 调一次 led.sh。异常一律吞掉 —— 灯的死活不能影响 opencode 干活。
 */
function poke(directory: string, state: string, info: Info, sync = false): void {
  const payload = JSON.stringify({
    session_id: info.sessionID ?? "",
    cwd: directory,
    hook_event_name: info.event,
    tool_name: info.tool ?? "",
    reason: info.reason ?? "",
  })

  const options = { env: childEnv(directory, info.sessionID), cwd: directory } as const

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
    // led.sh 不存在(仓库被挪走了)、fork 失败 —— 都不该冒泡到 opencode
  }
}

/**
 * 给一个会话挂一个守护进程,opencode 一死就替它把租约还回去。
 *
 * 这是本文件里唯一一处「别的平台不需要」的东西。Claude Code 有 SessionEnd、Codex 有
 * SessionEnd,opencode 一个都没有:唯一的候选 server.instance.disposed 发得比插件的
 * 拆卸还晚,插件收不到(见文件顶部)。没有它,退出 opencode 后那颗灯要等 LED_STALE_MIN
 * (默认 30 分钟)过期才熄。
 *
 * 做法是一根管道当"生命线":守护进程 `cat` 我们持有写端的那根管道,平时永远读不到东西,
 * 一旦 opencode 进程消失、内核把写端关掉,cat 读到 EOF,接着执行 release。
 * 相比轮询 `kill -0` 的写法,这条路不占 CPU、反应是即时的,而且**连 SIGKILL 都盖得住**
 * —— 进程怎么死的不重要,fd 总会被内核回收。反过来说它比 hook 更可靠:Claude Code 的
 * SessionEnd 在 SIGKILL 时是跑不到的。
 *
 * 不用信号处理器(process.on("SIGINT") 之类)是刻意的:在 Node/Bun 里挂上监听会顶掉
 * 默认的终止行为,弄不好把 opencode 变成 Ctrl-C 杀不掉的进程。宁可多一个 sh。
 */
function watch(directory: string, sessionID: string): ChildProcess | null {
  try {
    // $0 = led.sh,$1 = 平台名,$2 = 喂给 led.sh 的 hook JSON。走 sh 是因为要的就是
    // "读到 EOF 再执行"这个顺序。JSON 走参数而不是写死在脚本里,省掉一层引号转义。
    const payload = JSON.stringify({
      session_id: sessionID,
      cwd: directory,
      hook_event_name: "guard",   // debug.log 的 event= 列上认得出是守护进程还的
    })
    // 读到 EOF 之后先等一秒再还。opencode 死的那一刻,前面甩出去的那些异步点灯可能还没
    // 跑到 lease.py:抢在它们前面还,租约会被后到的 acquire 重新建出来,灯就一直亮到
    // LED_STALE_MIN 过期。等一秒让它们落地,代价只是灯晚灭一秒,看不出来。
    // 这一秒里若在同一目录重开了 opencode 也不会误熄:release 只摘自己那个 sid,新会话
    // 的 sid 还在 sids 里,租约就留着。
    const child = spawn("/bin/sh", ["-c", 'cat >/dev/null 2>&1; sleep 1; printf "%s" "$2" | "$0" release "$1"',
                                    LED_SH, PLATFORM, payload], {
      env: childEnv(directory, sessionID),
      cwd: directory,
      stdio: ["pipe", "ignore", "ignore"],
      detached: true,
    })
    child.on("error", () => {})
    child.stdin?.on("error", () => {})
    // 故意**不**关 stdin —— 那根管道就是生命线,关了守护进程立刻就把灯灭了。
    // 两个 unref 都要:否则这个不会自己结束的子进程和这根一直开着的管道会把
    // opencode 的事件循环钉住,退不出去。
    child.unref()
    child.stdin?.unref?.()
    return child
  } catch {
    return null
  }
}

export const ledPlugin: Plugin = async ({ directory }) => {
  // 每个见过的会话对应一个守护进程。归还是按会话逐个还的 —— led.sh release 一次只减
  // 一个 sid,同一目录下最后一个会话退出时才真正熄灯。
  const guards = new Map<string, ChildProcess | null>()

  // 出错的会话记在这儿,用来吃掉紧随其后的那次 session.idle,理由见下面 session.error 处。
  const failed = new Set<string>()

  // 没有会话号就不点灯。这不是洁癖:lease.py 在拿不到 session_id 时会退化成 "anon" 把
  // 它记进 sids,而我们也就没法给它挂守护进程,那个 sid 永远还不掉 —— 同目录最后一个
  // 会话退出时也熄不了灯,得等 LED_STALE_MIN 过期。所有钩子里只有 session.error 的
  // sessionID 是可选的,就是冲它来的:不挂在任何会话上的错误,本来也不该占一颗灯珠。
  const fire = (state: string, info: Info, sync = false) => {
    const { sessionID } = info
    if (!sessionID) return
    if (!guards.has(sessionID)) guards.set(sessionID, watch(directory, sessionID))
    poke(directory, state, info, sync)
  }

  const drop = (sessionID: string, event: string, sync: boolean) => {
    if (!guards.has(sessionID)) return
    // 先撤掉守护进程再还租约:让它活着的话,它会在 opencode 退出时再还一次。
    // 那一次是无害的(lease.py 对已经不在 sids 里的会话直接返回空),但日志里会多出
    // 一行看不懂的 release,排查时容易误导。
    const guard = guards.get(sessionID)
    guards.delete(sessionID)
    try {
      guard?.kill()
    } catch {
      // 已经退了
    }
    poke(directory, "release", { sessionID, event }, sync)
  }

  return {
    // 收到用户输入 —— 对应 Claude Code 的 UserPromptSubmit
    "chat.message": async ({ sessionID }) => {
      // 新一轮开始,上一轮的失败标记作废。不清的话,若某次出错后 idle 没跟上来,
      // 那个标记会一直留着,把**下一轮**正常结束的 success 吃掉。
      if (sessionID) failed.delete(sessionID)
      fire("thinking", { sessionID, event: "chat.message" })
    },

    "tool.execute.before": async ({ tool, sessionID }) => {
      const state = TOOL_STATE[tool]
      if (state) fire(state, { sessionID, event: "tool.execute.before", tool })
    },

    // 只还原 bash,和 Claude Code 的 PostToolUse matcher 卡死在 Bash 是同一条取舍:
    // bash 常跑几十秒,黄扫描看得完整,跑完回 thinking 才是准的;edit 只有一两秒,
    // 还原反而把 coding 擦掉。
    "tool.execute.after": async ({ tool, sessionID }) => {
      if (tool === "bash") fire("thinking", { sessionID, event: "tool.execute.after", tool })
    },

    "permission.ask": async ({ sessionID }) => {
      fire("waiting", { sessionID, event: "permission.ask" })
    },

    // 和下面的 session.compacted 配对,对应 Claude Code 的 PreCompact / PostCompact。
    // 少了收尾那半边,压缩结束后黄扫描会一直卡住。
    "experimental.session.compacting": async ({ sessionID }) => {
      fire("busy", { sessionID, event: "session.compacting" })
    },

    event: async ({ event }) => {
      switch (event.type) {
        case "session.idle": {
          // 一轮回答结束,对应 Stop。但出错的那一轮要放过去 —— 见下。
          const sessionID = event.properties.sessionID
          if (sessionID && failed.delete(sessionID)) break
          fire("success", { sessionID, event: event.type })
          break
        }

        case "session.compacted":
          fire("thinking", { sessionID: event.properties.sessionID, event: event.type })
          break

        case "session.error": {
          // 实测出错时的事件序列是 status → error → status → **idle**:idle 一定会跟在
          // error 后面。照直接映射的话,alarm 的红蓝翻转会被 idle 的绿色呼吸秒擦掉,
          // 等于报错根本看不见。所以这里记一笔,把紧随其后的那次 idle 吃掉,让报错的灯
          // 一直留到下一个动作 —— 和 Claude Code 那边「失败走 PostToolUseFailure、没人
          // 还原,error 会留到下一个动作」是同一个效果。
          //
          // MessageAbortedError 是用户自己按了中断,不是故障,所以给 success 而不是 alarm;
          // 但同样要吃掉后面的 idle,免得多发一次一模一样的请求。
          const name = event.properties.error?.name
          const sessionID = event.properties.sessionID
          if (sessionID) failed.add(sessionID)
          const state = name === "MessageAbortedError" ? "success" : "alarm"
          fire(state, { sessionID, event: event.type, reason: name })
          break
        }

        case "session.deleted": {
          const sessionID = event.properties.info.id
          failed.delete(sessionID)
          drop(sessionID, event.type, false)
          break
        }

        case "server.instance.disposed":
          // 实测(1.18.5)这里根本收不到 —— 这个事件是在实例作用域拆完之后才发的,
          // 插件的订阅早没了。留着是因为它一分钱不花,哪天上游把顺序改回来就能省掉一次
          // 绕道守护进程的往返;真正兜底的是 watch() 那根管道。
          // 收到的话此后进程马上就退出,所以必须同步,和 Claude Code 的 SessionEnd
          // 不设 async 是同一个理由。
          for (const sessionID of [...guards.keys()]) drop(sessionID, event.type, true)
          break
      }
    },
  }
}
