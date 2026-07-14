# browser-use 跨域 iframe 超时根因调查

- 日期：2026-07-13
- 状态：`BrowserStateRequestEvent` 30 秒超时的根因已确认
- 复现任务：LexBench-Browser `3012`
- 当前环境版本：browser-use `0.13.4`、bubus `1.5.6`、cdp-use `1.4.5`、lexmount `0.5.12`
- Chromium 源码证据来自与线上镜像同版本的本地检出；本文只记录源码相对路径

## 结论摘要

在将 IGN 返回的页面结构视为合法输入、将远程 CDP 单命令约 100 ms 的端到端耗时视为合理的前提下，当前 browser-use `0.13.4` 的 cross-origin iframe DOM 构建路径存在性能和容量缺陷。

完整触发链是：

```text
browser-use Agent 获取当前页面状态
  -> BrowserStateRequestEvent
  -> DOMWatchdog.on_BrowserStateRequestEvent
  -> DomService.get_serialized_dom_tree
  -> 遇到可见的 cross-origin iframe
  -> BrowserSession.get_all_frames
  -> 对所有 target 串行 Page.getFrameTree
  -> 对所有子 frame 串行 DOM.enable + DOM.getFrameOwner
  -> 累计超过 BrowserStateRequestEvent 的 30 秒预算
```

本次完整诊断中，`75` 个 target session 和 `132` 个 metadata candidate 至少产生 `75 + 132 * 2 = 339` 个显式串行 CDP 命令。实测 `get_all_frames` 为 `36.62s`，其中 metadata 补全为 `30.91s`。

这不是 Chromium 或 CDP 连接永久阻塞。超时取消当前 await 后，同一 CDP 连接仍继续处理后续命令。

## 复现命令

完整 benchmark 原生路径：

```bash
uv run bubench run --agent browser-use --data LexBench-Browser --split All --mode by_id --id 3012 --model-name grok-4.5 --timeout 1200 --concurrency 1
```

同一 WebSocket CDP 活性诊断仍使用上述原生路径，只增加只读探针：

```bash
BUBENCH_BROWSER_USE_DIAG=1 \
BUBENCH_BROWSER_USE_CDP_LIVENESS_DIAG=1 \
uv run bubench run --agent browser-use --data LexBench-Browser --split All \
  --mode by_id --id 3012 --model-name grok-4.5 --timeout 1200 --concurrency 1
```

当前探针在每次 `get_all_frames()` 内通过当前 `self.cdp_client`
并发发送 `Accessibility.getFullAXTree`、`DOMSnapshot.captureSnapshot` 和
`DOM.getDocument`。它不新建 WebSocket，不修改 frame 遍历、watchdog event
timeout 或 benchmark 任务。详细用法和证据边界见
`docs_4_codeagent/browser-use-cdp-liveness-diagnostics.md`。

仅使用 Lexmount SDK 和 raw CDP 复现 browser-use frame 遍历：

```bash
uv run python scripts/lexmount_browser_use_iframe_probe.py \
  --profile en \
  --log-file output/lexmount_cdp_probe/browser_use_iframe_manual_recheck.log
```

Lexmount official proxy 对照：

```bash
uv run python scripts/lexmount_browser_use_iframe_probe.py \
  --profile en \
  --official-proxy \
  --log-file output/lexmount_cdp_probe/browser_use_iframe_official_proxy_manual_recheck.log
```

脚本使用真实 Lexmount session 和真实 CDP，不导入 browser-use、bubus、Agent 或 LLM。它复现的是 browser-use 的串行 frame fan-out，不复现 30 秒 watchdog 本身。脚本说明见 `scripts/lexmount_browser_use_iframe_probe.py:1-27`。

## 组件和超时归属

```text
bubench
  -> 每个任务启动独立 agent_runner 子进程
  -> LexmountBackend 通过 Lexmount SDK 创建云浏览器并取得 CDP URL
  -> BrowserUseAgent 创建 browser-use Browser/Agent
  -> browser-use BrowserSession
     -> watchdogs / events
     -> bubus EventBus
     -> cdp-use CDP client
  -> Lexmount 云 Chromium
```

| 组件 | 本次职责 | 结论 |
| --- | --- | --- |
| bubench | 任务编排、backend 生命周期、`1200s` Agent 外层超时 | 不是本次 `30s` 的来源 |
| Lexmount SDK/backend | 创建远程浏览器并返回 CDP URL | 原生路径正常执行 |
| browser-use | BrowserState、DOM、frame/OOPIF 收集 | 缺陷所在 |
| DOMWatchdog | BrowserState handler 和 DOM 构建入口 | 超时发生的 handler 边界 |
| bubus | 执行 event handler、应用 event timeout、打印超时树 | 按 browser-use 配置正常工作 |
| cdp-use | CDP 请求 ID 匹配和 WebSocket 复用 | 没有强制全局串行 |
| Chromium | target/session 路由和 CDP backend | 未发现连接级死锁 |

代码证据：

- bubench 每个任务使用独立子进程：`browseruse_bench/cli/run.py:888-892`、`1075-1173`。
- Lexmount backend 调用 SDK：`browseruse_bench/browsers/providers/lexmount.py:178-266`。
- BrowserUseAgent 将 CDP URL 交给 browser-use：`browseruse_bench/agents/browser_use.py:934-945`。
- `--timeout 1200` 最终包裹 `agent.run()`：`browseruse_bench/agents/browser_use.py:1046`。
- `BrowserStateRequestEvent` 的 `30s` 来自 browser-use：`.venv/lib/python3.14/site-packages/browser_use/browser/events.py:194-201`。
- bubus 使用 `asyncio.wait_for` 实施该超时：`.venv/lib/python3.14/site-packages/bubus/service.py:1097-1128`。
- cdp-use 按 request ID 保存和分发多个 pending future：`.venv/lib/python3.14/site-packages/cdp_use/client.py:229-241`、`302-327`、`361-389`。

## 表象证据

原生 benchmark 日志明确显示 `DOMWatchdog.on_BrowserStateRequestEvent` 超过 30 秒：

```text
output/logs/run/20260713_142921.log:693
TIMEOUT ERROR - Handling took more than 30.0s for
DOMWatchdog.on_BrowserStateRequestEvent

output/logs/run/20260713_142921.log:700
DOMWatchdog.on_BrowserStateRequestEvent ... 31s/30s ... TIMEOUT HERE
```

超时时正在等待 `DOM.getFrameOwner`，上层 frame 和 DOM 构建被取消：

```text
output/logs/run/20260713_142921.log:684
cancelled id=710 method=DOM.getFrameOwner ... elapsed=1.36s

output/logs/run/20260713_142921.log:686-691
get_all_frames ... CancelledError
get_dom_tree ... CancelledError
get_serialized_dom_tree ... CancelledError
```

所以用户看到的是导航后 Agent 状态获取长时间停顿，而不是 `Page.navigate` 本身持续 30 秒。

## 定量证据

### browser-use 原生路径

`20260713_144744` 是为取得完整阶段耗时而运行的诊断样本；默认 30 秒超时是否触发应以 `20260713_142921` 的原生 timeout 日志为证，不能只由该诊断样本推断。

`output/logs/run/20260713_144744.log:565-568`：

| 指标 | 第一轮 |
| --- | ---: |
| target sessions | 75 |
| frames | 133 |
| cross-origin frames | 116 |
| metadata candidates/enriched | 132/132 |
| frame-tree pass | 5.71s |
| metadata pass | 30.91s |
| get_all_frames | 36.62s |

同一运行的第二轮位于 `output/logs/run/20260713_144744.log:1149-1152`：

| 指标 | 第二轮 |
| --- | ---: |
| target sessions | 80 |
| frames | 138 |
| cross-origin frames | 121 |
| metadata candidates/enriched | 137/137 |
| frame-tree pass | 6.14s |
| metadata pass | 20.71s |
| get_all_frames | 26.85s |

frame 数量稳定处于同一数量级，耗时随单命令延迟波动，因此该问题不是每轮都必然超过 30 秒，但容量边界不稳定。

### 独立 raw CDP 复现

| 路径 | targets | frames | cross-origin | metadata attempts | get_all_frames-like |
| --- | ---: | ---: | ---: | ---: | ---: |
| Lexmount affected route | 56 | 117 | 98 | 116 | 30.25s |
| Lexmount affected route recheck | 58 | 121 | 104 | 120 | 27.36s |
| Lexmount official proxy | 4 | 12 | 3 | 11 | 1.76s |
| local `.com` with proxy | 3 | 11 | 2 | 10 | 0.05-0.08s |
| `.cn` | 1 | 1 | 0 | 0 | 0.01s |

原始证据：

- `output/lexmount_cdp_probe/browser_use_iframe_en_20260713_144220.log:25`
- `output/lexmount_cdp_probe/browser_use_iframe_en_recheck.log:26`
- `output/lexmount_cdp_probe/browser_use_iframe_en_official_proxy_recheck.log:26`
- `output/lexmount_cdp_probe/local_headless_socks_browser_use_iframe_long_20260713_162930.log:14-20`
- `output/lexmount_cdp_probe/local_headless_browser_use_iframe_20260713_162600.log:14-17`

受影响路径的 frame 域名主要来自 `pubmatic.com`、`amazon-adsystem.com`、`gumgum.com`、`casalemedia.com`、`doubleclick.net`、`googlesyndication.com`、`rubiconproject.com`、`3lift.com` 和 `about:blank`。这支持“frame fan-out 主要来自广告技术链路”的判断，但不能据此断言 IGN 页面有错误。

### 延迟预算计算

第一轮源码循环至少产生：

```text
Page.getFrameTree: 75 次
DOM.enable: 132 次
DOM.getFrameOwner: 132 次
总计: 339 个串行 CDP 命令
```

对应平均值：

```text
frame tree: 5.71s / 75 = 76.1ms/target
metadata: 30.91s / 264 = 117.1ms/命令
overall: 36.62s / 最低339个显式命令 = 108.0ms/显式命令
```

若按合理的远程 CDP 单命令端到端耗时 `100ms` 计算：

```text
339 * 100ms = 33.9s
```

这还没有计入 BrowserState 中的其他工作，已经超过 `30s` event timeout。这里使用的是 CDP 命令端到端耗时，不等同于纯网络 RTT，它还包含 Chromium 路由和 renderer 处理。

## browser-use 源码证据

### 1. 功能目的

browser-use 的目标是为 Agent 构建包含可操作跨域 iframe 内容的统一 DOM：

- `cross_origin_iframes` 默认值为 `True`：`.venv/lib/python3.14/site-packages/browser_use/browser/profile.py:664-667`。
- DOMWatchdog 创建 DomService 并传入该配置：`.venv/lib/python3.14/site-packages/browser_use/browser/watchdogs/dom_watchdog.py:538-560`。
- DomService 只在跨域 iframe 可见且至少 `50x50` 时准备递归：`.venv/lib/python3.14/site-packages/browser_use/dom/service.py:922-959`。
- 它通过 `frameId -> frameTargetId` 找到 OOPIF target，然后递归采集该 target 的 DOM：`.venv/lib/python3.14/site-packages/browser_use/dom/service.py:960-1012`。

因此，“获得相关 frame 到 target/session 的映射”对跨域 iframe 支持是必要的；“执行当前完整的全量 metadata 补全”不是概念上的必要条件。

### 2. 全量串行 frame-tree pass

`BrowserSession.get_all_frames()` 先枚举全部 page/iframe target，并逐个等待 `Page.getFrameTree`：

```python
# .venv/.../browser_use/browser/session.py:3680-3710
for target in all_targets:
    ...
    cdp_session = await self.get_or_create_cdp_session(target_id, focus=False)
    ...
    frame_tree_result = await cdp_session.cdp_client.send.Page.getFrameTree(...)
```

每个 target 在前一个 target 返回后才开始，远程 CDP 往返延迟被线性累加。

### 3. 全量串行 metadata pass

第二遍逐 frame 执行两个 await：

```python
# .venv/.../browser_use/browser/session.py:3801-3822
for frame_id_iter, frame_info in all_frames.items():
    ...
    await self.cdp_client.send.DOM.enable(session_id=parent_session_id)
    frame_owner = await self.cdp_client.send.DOM.getFrameOwner(...)
```

问题包括：

1. 所有 candidate 串行处理。
2. 同一 parent session 可能被反复执行 `DOM.enable`。
3. 一个相关的可见 iframe 触发后，会补全全部 frame，而不是只处理 Agent 实际需要的 frame。

### 4. 最耗时 metadata 在当前 BrowserState 路径没有消费者

第二遍写入：

```text
parentTargetId
backendNodeId
nodeId
```

写入位置：`.venv/lib/python3.14/site-packages/browser_use/browser/session.py:3808-3826`。

当前 DomService 跨域递归只读取第一遍已经产生的 `frameTargetId`：`.venv/lib/python3.14/site-packages/browser_use/dom/service.py:982-1008`。

对当前安装包进行静态搜索，没有发现该 BrowserState 路径读取 `frame_info['parentTargetId']`、`frame_info['backendNodeId']` 或 `frame_info['nodeId']`。因此可以确认：对本次内部调用路径，耗时最大的 metadata pass 没有被消费。

边界说明：`get_all_frames()` 也可能被外部调用方作为 API 使用；本结论只覆盖当前安装包和本次 BrowserState 内部路径，不声明这些字段对所有潜在外部调用方都无用。

### 5. `max_iframes` 保护生效过晚

BrowserProfile 定义：

```text
max_iframes = 100
max_iframe_depth = 5
```

位置：`.venv/lib/python3.14/site-packages/browser_use/browser/profile.py:668-675`。

但 `max_iframes` 只在 DomService 已取得 snapshot 后裁剪 `snapshot['documents']`：`.venv/lib/python3.14/site-packages/browser_use/dom/service.py:634-642`。它不限制更早执行的 `get_all_frames()` 和 `_populate_frame_metadata()`，所以无法阻止当前超时。

## CDP 是否被阻塞

### 协议和 Chromium 设计

同一 CDP WebSocket 可以承载多个 `sessionId`。Chromium 源码按 `sessionId` 将消息路由到 `child_sessions_`：

```text
content/browser/devtools/devtools_session.cc:441-478
```

每个 DevToolsSession 用 `waiting_for_response_` 保存多个 call ID，而不是要求前一个请求返回后才能接收下一个请求：

```text
content/browser/devtools/devtools_session.cc:594-615
content/browser/devtools/devtools_session.cc:796-840
```

renderer 侧命令会投递到 InspectorTaskRunner，同一 renderer 上仍可能局部排队：

```text
third_party/blink/renderer/core/inspector/devtools_session.cc:101-125
third_party/blink/renderer/core/inspector/inspector_task_runner.cc:34-57
```

所以结论是：CDP 连接层没有全局“一个请求未返回就禁止发送其他请求”的设计；同一 renderer/session 的具体命令仍可能局部等待。browser-use 当前没有利用可并发的 session，而是在应用层逐个 await。

### Watchdog 运行中的同一 WebSocket 实测

下面的 `Browser.getVersion` 数据是 2026-07-13 轻量探针的历史实验证据。
当前分支已将该探针升级为带真实 page `sessionId` 的三条重型 CDP 请求；
历史数据仍用于证明 browser-level dispatcher 的连接活性，不代表当前
探针的请求集。

诊断实现位置：

- CDP 活性探针：`browseruse_bench/agents/browser_use.py:603-689`。
- 探针直接使用当前 BrowserSession 的 `cdp_client`：`browseruse_bench/agents/browser_use.py:604-605`、`638`。
- 探针只在 `get_all_frames()` 执行期间启动：`browseruse_bench/agents/browser_use.py:774-790`。
- 普通 frame 命令和探针命令同时记录 `CDPClient`、底层 `ws` 对象标识：`browseruse_bench/agents/browser_use.py:692-750`。

为什么 `Browser.getVersion` 的完成能够证明收到了远端 CDP 响应，而不是本地函数立即返回：

1. 一个 `CDPClient` 只有一个 `self.ws` 和一个 `pending_requests` 表：`.venv/lib/python3.14/site-packages/cdp_use/client.py:229-242`。
2. `send_raw()` 分配 request ID、写入 `pending_requests`、通过 `self.ws.send()` 发送，然后等待对应 future：`.venv/lib/python3.14/site-packages/cdp_use/client.py:361-389`。
3. 只有 WebSocket reader 从 `self.ws.recv()` 收到相同 request ID 的响应后，才会完成该 future：`.venv/lib/python3.14/site-packages/cdp_use/client.py:302-323`。

`output/logs/run/20260713_203719.log` 中存在一个完整的响应越过样本：

1. `:3017`：`get_all_frames` 开始。
2. `:3023`：request `320` 的 `Page.getFrameTree` 开始，连接标识为 `client=0x7bba060dfb60`、`ws=0x7bba04cdd400`。
3. `:3035`：request `325` 的 `Browser.getVersion` 通过完全相同的 `client/ws` 发出。
4. `:3045`：request `325` 在约 `0.09s` 后返回。
5. `:3047`：精确 RTT 为 `86.7ms`，并记录 request `320` 在 probe 返回后仍为 pending，返回内容为 `Chrome/149.0.7827.3`。
6. `:3051`：更早发出的 request `320` 此后才完成，总耗时约 `0.20s`。

这组顺序直接证明：同一 WebSocket 上，后发的 CDP request `325` 可以在先发的 request `320` 尚未完成时收到响应，不存在连接级的严格串行或 head-of-line 阻塞。

两轮汇总：

| BrowserState 轮次 | `Browser.getVersion` 成功/失败 | 发送时有其他请求 pending | 越过更早 pending 请求 | RTT min/p50/p95/max | 结果 |
| --- | ---: | ---: | ---: | --- | --- |
| 第一轮 | 15/0 | 15 | 4 | 57.1/57.7/221.3/221.3ms | `get_all_frames=16.18s`，正常完成 |
| 第二轮 | 27/0 | 20 | 0 | 56.8/58.0/59.6/133.0ms | `BrowserStateRequestEvent=30s` 超时 |

第一轮汇总和完成记录位于 `output/logs/run/20260713_203719.log:4884-4886`。

汇总中的 `cancelled=True` 表示 `get_all_frames` 正常结束或被 watchdog 取消时，伴随它运行的无限探针任务也被停止；单次请求失败由独立的 `failed_samples` 统计，两轮均为 `0`。

第二轮是更关键的超时边界证据：

- `:11271`、`:11279`：第二轮 `get_all_frames` 和同一 WS 探针启动。
- `:13331`：metadata 在处理 `151` 个 candidate、完成 `100` 个后被 watchdog 取消。
- `:13333`：在该轮内，27 次探针全部成功，`failed_samples=0`，其中 20 次发送时存在其他 pending 请求。
- `:13274-13282`：超时前最后一个完整探针 request `1328` 在 `DOM.getFrameOwner` pending 时以 `59.6ms` 返回，时间为 `20:38:32`。
- `:13335`：`get_all_frames` 在 `28.41s` 被取消。
- `:13342`、`:13349`：一秒后的 `20:38:33`，外层 `DOMWatchdog.on_BrowserStateRequestEvent` 精确触发 `30.0s` timeout。

对 `:11279-13334` 原始日志进行独立解析的复核结果为：27 个 `Browser.getVersion` start、27 个 finish、无不匹配 request ID、只有一个 `client/ws` 组合；重新计算得到 `min=56.8ms`、`p50=58.0ms`、`p95=59.6ms`、`max=133.0ms`，与探针 summary 一致。

因此可以确认：**即使在最终触发 30 秒 watchdog timeout 的同一轮处理中，该 WebSocket CDP 通道仍持续接受新消息并在本次采样中以约 57-133ms 返回 browser-level CDP 响应。**

证据边界：`Browser.getVersion` 是无 `sessionId` 的 browser-level 命令。它证明 WebSocket、cdp-use reader/request-ID 分发和 Chromium browser-level dispatcher 没有被 frame 循环全局堵塞；它不证明每个 renderer、每个 session 或任意 CDP 命令都不会发生局部排队。第一轮的越过样本证明连接支持多请求并行在途，但不应据此断言慢 `DOM.getFrameOwner` 的 renderer 本身也始终立即响应。

### 超时后的实测证据

`output/logs/run/20260713_142921.log` 显示：

1. `:684` 当前 `DOM.getFrameOwner` 被取消。
2. `:682` 迟到响应到达，cdp-use 因对应 future 已结束而忽略。
3. `:704` 以后立即继续发送新的 CDP 请求。
4. `:1405` 后续一次 `get_all_frames` 在 `24.01s` 完成。
5. `:1429-1432` 后续 DOM serialization 完成。
6. `:1435` Agent 继续进入下一步。

因此，30 秒超时会中断当前 BrowserState handler 和尚未发送的串行请求，但不会永久堵死 CDP WebSocket。

## 根因归属

已确认的根因可以表述为：

> browser-use `0.13.4` 在 cross-origin iframe BrowserState 路径中，用全量、串行、包含重复和当前路径未消费工作的 CDP fan-out，处理高 frame 页面；该算法在合理的远程 CDP 延迟下与 browser-use 自己的固定 30 秒 event timeout 存在确定性的容量冲突。

具体缺陷：

1. 工作范围过大：一个相关 iframe 触发全部 target/frame 扫描。
2. 串行放大：`Page.getFrameTree` 和 metadata 均逐个 await。
3. 重复工作：同一 session 反复 `DOM.enable`。
4. 当前路径无效工作：最耗时的 metadata 字段没有被 DomService 消费。
5. 容量保护位置错误：`max_iframes` 没有覆盖前置 frame 收集。
6. timeout contract 不匹配：正常远程延迟乘以当前请求数量即可超过 30 秒。

不应将根因归到：

- IGN 页面错误：100+ frame 很高，但在受影响路由上可重复，且是 Chromium 的合法页面状态。
- Lexmount SDK：独立 SDK + raw CDP 复现了相同 fan-out，说明 SDK 没有制造 browser-use watchdog。
- Chromium/CDP 永久阻塞：超时后后续 CDP 请求成功。
- bubus：它执行了 browser-use 配置的 timeout。
- bubench `--timeout 1200`：这是 Agent 外层预算，不是 BrowserState 的 30 秒预算。

## 修复方向和非修复项

以下是根因对应的修复方向，尚未在本调查中实现或验证：

1. 当前 BrowserState 调用只获取相关 frame 的 `frameId -> target/session` 映射，不执行未消费的全量 metadata pass。
2. 如果其他调用方确实需要 owner metadata，应按需获取，而不是每次 BrowserState 全量获取。
3. `DOM.enable` 每个 session 至多执行一次。
4. 对相互独立的 target/session 使用有界并发，并正确处理 target detach。
5. 在 frame discovery/metadata 之前应用数量和深度预算，而不是 snapshot 完成后才裁剪。
6. 增加高 frame 数和远程延迟组合下的容量测试。

单纯提高 `TIMEOUT_BrowserStateRequestEvent` 只能扩大预算，是缓解措施，不解决串行 fan-out 和无效工作。

直接关闭 `cross_origin_iframes` 会丢失跨域 iframe 的 DOM 和操作能力，不作为根因修复。

## 尚未证明和调查边界

1. 最初日志中的 `AgentFocusChangedEvent` `10s` 超时是另一条路径。该 handler 主要执行 session 获取和 viewport 设置，当前证据不能把它直接归因于 `get_all_frames()`。
2. 已证明 frame fan-out 与地区/代理/广告返回内容强相关，但尚未隔离出导致 affected route 获得大量广告 frame 的唯一网络条件。
3. 已证明当前内部 BrowserState 路径不消费 owner metadata；尚未评估所有外部调用方是否依赖 `get_all_frames()` 返回这些字段。
4. 本文确认根因但未实现修复，因此没有修复后原生 benchmark 通过的证据。
5. `output/` 日志和 `.venv/` 安装源码是当前工作区调查证据；若归档或迁移本报告，应同时保存对应日志及依赖版本。
