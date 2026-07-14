# browser-use CDP 活性诊断

本文说明 browser-use CDP 活性探针的用途、启用方式、日志字段和证据边界。

## 用途

该探针用于回答一个具体问题：

> `BrowserStateRequestEvent` 的 watchdog 正在等待 `get_all_frames()` 时，当前主页面的
> target session 是否仍能通过同一条 WebSocket 接受重型 CDP 请求并返回响应？

探针不是超时修复。它不会修改 `Page.getFrameTree`、`BrowserSession.get_all_frames()` 的原始串行算法、`DOMWatchdog` 或 30 秒 event timeout。
探针会真实抓取 AX tree、DOM snapshot 和完整 DOM，因此会增加 renderer、网络和 Python
反序列化负载；启用后的耗时不能直接作为“无探针”基线。
本文日志沿用 `rtt_ms` 命名，但它表示从 `send_raw()` 到完整响应解码结束的端到端时间，
包含排队、WebSocket 传输、Chromium/renderer 执行和 Python 反序列化，不是纯网络 RTT。

## 所在层级

代码位于 bubench 的 browser-use agent 集成层：

```text
bubench BrowserUseAgent
  -> 按环境变量安装运行时诊断
  -> 包装 BrowserSession.get_all_frames 及其两个阶段方法
  -> 取得当前 agent-focus page 的真实 sessionId
  -> 复用当前 cdp-use CDPClient 和底层 WebSocket
  -> 每轮并发发送 Accessibility.getFullAXTree、DOMSnapshot.captureSnapshot、DOM.getDocument
```

实现位置：

- `browseruse_bench/agents/browser_use.py::_install_browser_use_diagnostics`
- `browseruse_bench/agents/browser_use.py::_run_browser_use_cdp_liveness_probe`
- `browseruse_bench/agents/browser_use.py::_patch_browser_use_cdp_diagnostics`
- `browseruse_bench/agents/browser_use.py::_patch_browser_use_frame_diagnostics`

browser-use、bubus 和 cdp-use 的安装包源码没有被直接修改。
frame 包装只标记 `target_discovery`、`frame_tree` 和 `metadata` 阶段，并在
`get_all_frames()` 生命周期内启动和停止探针；它没有重写原始 frame 遍历实现。

## 开关

| 环境变量 | 作用 | 是否启动活性探针 |
| --- | --- | --- |
| `BUBENCH_BROWSER_USE_CDP_LIVENESS_DIAG=1` | 在每次 `get_all_frames()` 期间反复发送三请求重型 bundle；每轮完成后间隔 1 秒 | 是 |
| `BUBENCH_BROWSER_USE_DIAG=1` | 输出每条 CDP 请求的 start/finish/interrupted 时间线 | 否 |

两个开关都接受 `1`、`true`、`yes`、`on`，大小写不敏感。

未设置两个开关时，不安装诊断 monkeypatch。单独设置
`BUBENCH_BROWSER_USE_DIAG=1` 只安装 CDP 请求包装，不会包装 frame 方法或启动
liveness 探针。诊断日志不记录 CDP params、页面 URL、cookie 或响应正文。重型探针只
记录方法名、端到端耗时以及 nodes/documents/strings 等响应规模。

## 运行

任务 `3012` 的固定复现入口是：

```bash
scripts/run_browser_use_cdp_liveness_3012.sh
```

该脚本会从任意工作目录切换到仓库根目录，并启用两个诊断开关。它固定使用
`grok-4.5`、`1200` 秒任务超时和单并发，不包含 `--dry-run`。任务结束后，脚本会
从本次进程的 `Logging to file:` 输出中取得精确日志路径，并自动打印探针、watchdog
和响应越过记录；它不会按文件修改时间猜测“最新日志”。
脚本还会明确输出 `TRIGGERED` 或 `NOT TRIGGERED`；后者下的全零请求计数表示
没有进入发送阶段，不表示发送后失败。

使用单行 `env` 命令可以避免 shell 换行或环境变量未传递：

不要添加 `--dry-run`。dry-run 会在启动 `agent_runner.py` 之前返回，因此不会创建
浏览器、运行 browser-use、进入 watchdog 或触发探针。

```bash
env BUBENCH_BROWSER_USE_DIAG=1 BUBENCH_BROWSER_USE_CDP_LIVENESS_DIAG=1 \
  uv run bubench run \
  --agent browser-use \
  --data LexBench-Browser \
  --split All \
  --mode by_id \
  --id 3012 \
  --model-name grok-4.5 \
  --timeout 1200 \
  --concurrency 1
```

任务启动时会打印本次日志路径，例如：

```text
Logging to file: /path/to/repo/output/logs/run/20260714_100245.log
```

只需要汇总证据、不需要逐请求日志时，可以省略 `BUBENCH_BROWSER_USE_DIAG=1`：

```bash
env BUBENCH_BROWSER_USE_CDP_LIVENESS_DIAG=1 uv run bubench run \
  --agent browser-use --data LexBench-Browser --split All \
  --mode by_id --id 3012 --model-name grok-4.5 --timeout 1200 --concurrency 1
```

## 确认是否触发

将 `log` 设置为启动信息中的实际文件：

```bash
log=output/logs/run/20260714_100245.log

if rg -q '\[browser-use cdp-liveness\] start' "$log"; then
  echo "liveness probe triggered"
  rg -n '\[browser-use cdp-liveness\] (start|sample|summary)' "$log"
else
  echo "liveness probe not triggered"
fi
```

探针成功启动时必须出现：

```text
[browser-use cdp-liveness] start methods=['Accessibility.getFullAXTree',
'DOMSnapshot.captureSnapshot', 'DOM.getDocument'] interval=1.0s
target=... session=... client=... ws=... root_client_match=True
```

如果日志只有 `TIMEOUT HERE`，没有上述 `start`，该次运行不能作为 liveness 证据。

## 观测命令

查看探针与 watchdog 的相对时间线：

```bash
rg -n '\[browser-use cdp-liveness\] (start|sample|summary)|TIMEOUT HERE' "$log"
```

启用了完整诊断时，查看重型方法和 frame 请求是否使用相同连接。这里同时包含
browser-use 业务请求；探针自身以随后的 `cdp-liveness-request` 标记为准：

```bash
rg -n '\[browser-use cdp\].*method=(Accessibility.getFullAXTree|DOMSnapshot.captureSnapshot|DOM.getDocument|Page.getFrameTree|DOM.getFrameOwner)' "$log"
```

检查三个重型方法的 start/finish/error/interrupted 数量。使用带 logger 名的 canonical
行，避免 runner 转发日志造成重复计数：

```bash
prefix='browseruse_bench\.agents\.browser_use.*\[browser-use cdp-liveness-request\]'
for method in Accessibility.getFullAXTree DOMSnapshot.captureSnapshot DOM.getDocument; do
  printf '%s start=%s finish=%s error=%s interrupted=%s\n' \
    "$method" \
    "$(rg -c "${prefix} start method=${method}" "$log" || true)" \
    "$(rg -c "${prefix} finish method=${method}" "$log" || true)" \
    "$(rg -c "${prefix} error method=${method}" "$log" || true)" \
    "$(rg -c "${prefix} interrupted method=${method}" "$log" || true)"
done
```

## 日志字段

单次 sample 示例：

```text
[browser-use cdp-liveness] sample=17 phase=metadata
target=... session=... client=0x... ws=0x... bundle_rtt_ms=842.3
method_rtt_ms={'Accessibility.getFullAXTree': 311.2,
'DOMSnapshot.captureSnapshot': 842.1, 'DOM.getDocument': 477.4}
responses={'Accessibility.getFullAXTree': {'nodes': 2196},
'DOMSnapshot.captureSnapshot': {'documents': 1, 'strings': 8743},
'DOM.getDocument': {'root_node_id': 1, 'child_node_count': 2}} errors={}
pending_before=['1135:DOM.getFrameOwner@1575F1B3']
still_pending_after=[]
```

| 字段 | 含义 |
| --- | --- |
| `phase` | 当前处于 `frame_tree`、`target_discovery` 或 `metadata` |
| `target` / `session` | 重型请求实际发送到的 agent-focus page target/session |
| `client` / `ws` | 当前进程内的 `CDPClient` 和 WebSocket 对象标识 |
| `bundle_rtt_ms` | 从并发发送到三个请求全部成功或失败返回的总耗时 |
| `method_rtt_ms` | 每条重型 CDP 请求从 `send_raw()` 到完整响应的端到端耗时 |
| `responses` | 响应规模摘要；不包含响应正文 |
| `errors` | 本轮按方法记录的 CDP 错误；空字典表示三个请求都返回 |
| `pending_before` | 探针发送前，同一 `CDPClient` 中仍在等待的请求 |
| `still_pending_after` | 探针返回后仍未完成的更早请求 |

summary 示例：

```text
[browser-use cdp-liveness] summary
bundle_latency_ms={'count': 4, 'min': 721.0, 'p50': 842.3, 'p95': 1104.8, 'max': 1104.8}
method_latency_ms={...} overlap_samples=4 overtake_samples=1
failed_samples=0 failed_requests=0 cancelled=True
```

| 字段 | 含义 |
| --- | --- |
| `overlap_samples` | 发送探针时至少有一个其他 CDP 请求 pending 的样本数 |
| `overtake_samples` | 探针已经返回、但至少一个更早请求仍 pending 的样本数 |
| `bundle_latency_ms` | 三请求 bundle 总耗时的 min/p50/p95/max |
| `method_latency_ms` | 三种方法各自耗时的 min/p50/p95/max |
| `failed_samples` | 至少一个重型请求返回错误的采样轮数 |
| `failed_requests` | 所有采样轮中失败的重型请求总数 |
| `cancelled` | `get_all_frames` 结束或被 watchdog 取消后，伴随探针任务已停止；不代表单次请求失败 |

## 如何形成证据

证明连接在 watchdog 阶段仍然可用，至少需要同时满足：

1. 日志包含 liveness `start` 和一个或多个 `sample`。
2. sample 的 `pending_before` 非空，证明探针与 frame 工作重叠。
3. `method_rtt_ms` 同时包含三个方法，且 `responses` 包含实际响应规模。
4. `errors={}`，并且 summary 中 `failed_samples=0`、`failed_requests=0`。
5. 如需证明响应越过，必须有 `still_pending_after` 非空或 `overtake_samples>0`。
6. 如需关联 30 秒超时，liveness sample/summary 与同一轮 `TIMEOUT HERE` 必须位于同一时间窗口。

## 结论边界

三个命令都携带当前 agent-focus page 的真实 `sessionId`。该实验能够证明：

- 同一 WebSocket 可以同时承载多个在途 CDP request。
- cdp-use reader 可以继续接收响应并按 request ID 分发。
- 当前主页面 session 能实际完成 AX、DOM snapshot 和 DOM document 三种重型工作。
- 如果 `still_pending_after` 中请求的 session 后缀与探针 `session` 相同，还能证明同一
  target session 中较晚发送的重型请求越过了更早请求。

该实验不能单独证明：

- 其他 OOPIF renderer 或其他 session 不会局部排队。
- `DOM.getFrameOwner` 应该与这三个方法具有相同延迟。
- 提高或取消 watchdog timeout 可以修复 browser-use 的串行 frame fan-out。
- 启用重型探针不会改变原问题的耗时；它本身会占用 renderer、网络和事件循环资源。

完整 CDP 时间线会产生大量日志。共享日志前仍应检查运行框架的其他日志，并脱敏 token、cookie 和其他凭据。
两个独立 Lexmount 探针只记录 endpoint 的 scheme、host 和 port，不记录 CDP/inspect URL
的路径或查询串；实际连接仍使用 SDK 返回的完整 URL。
