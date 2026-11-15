# 原生 MCP 反向隧道接入

沿用 A07/A11/A12 与 R01/R03/Q01—Q03，总目标持续推进。HTTP部署
映射已完成CPU验证（50bef003），它连接已存在的MCP适配入口；本组
补齐私网主动连接的传输，不能以把tunnel_id改名为URL作为完成。

## 固定来源与职责

参考 OpenAI tunnel-client `fb6c47fbdf156451137e2c00a55d485be42f03a3`
的[协议说明](https://github.com/openai/tunnel-client/blob/fb6c47fbdf156451137e2c00a55d485be42f03a3/docs/protocol.md)
及[OpenAPI](https://github.com/openai/tunnel-client/blob/fb6c47fbdf156451137e2c00a55d485be42f03a3/docs/openapi.json)，
Apache-2.0。源码及许可快照仅在`.q4t-work/serve-mcp-20261115/`
`tunnel-reference-fb6c47fb`，有逐文件SHA256。当前学习协议，不复制
其Go实现、不新增Go/Cloudflare程序运行依赖；后续如移植代码则
先保留对应许可与NOTICE。README登记参考来源。

q4t拥有原生broker、入口路由和可选私网agent。既有远端MCP仍走
隔离传输，HTTP映射仍可选择已部署外部适配器。隧道是工具数据源
能力；禁用或失能不改变模型健康，也不能触发模型/整树自动重启。
控制面组织/账单、Cloudflare托管等属于G00待决云管理范围，不因
协议参考而隐式引入这些外部服务。

## 实施出口

1. **有界broker与固定wire合同。** 认证调用方、授权agent、目标
   generation分别绑定。任务只被poll一次；响应由原request_id与
   shard_token确认。处理中断/取走后丢失不重新入队；关闭唤醒等待。
   所有容量、字节、等待和保留有硬上限，不保存请求回调/FD到会话。
2. **原生入口与实际MCP传输。** GET metadata/poll、POST response，
   已有TLS/API Key/CIDR/前缀能力复用。私网凭据只作为受保护任务
   内容传递；agent凭据与用户MCP OAuth分离。actual tools/list→call→
   resources/read通过原会话/权限链执行，旧审批不可随映射迁移。
3. **自有agent与运行生命周期。** 在可达私网MCP的一侧主动访问
   q4t控制面；HTTP/SSE会话、JSON-RPC通知/终态、DELETE与deadline
   保持；结果上传可确认但外部工具不能重放。退出/失联有明确状态，
   同一有状态绑定的agent实例必须有独占/亲和约束，不能随机分发
   初始化与后续操作给不同进程。第三方客户端仅作可选互操作。
4. **交付接线。** 策略、封包、原生命令、健康投影、真实业务/SDK、
   恢复资格与可回退发行/main；完成后继续所有master任务。

## Broker与权限边界

- 路由配置由管理员提供，调用客户与agent客户分别精确授权，不能
  根据未认证body或headers建立身份。HTTP映射策略与本地隧道配置
  使用可区分的transport，旧HTTP配置保持语义；原目标指纹包括
  传输类型、ID及generation，不包含轮换凭据。
- 请求携带原JSON-RPC及MCP相关头，不能携带可选择任意私网目标的
  URL/命令。私网agent使用自己的固定目标和网络策略。策略不可由
  model/tool arguments覆盖。
- 排队与在途总数、单客户数、聚合字节、单请求/响应、进度数量和
  最大期限分别约束；等待使用单调时钟，通知不延长原期限。配置
  超限在入队前拒绝，不能靠后续大分配失败保护统一RAM。
- Poll遵守channel过滤；原request_id和独立随机shard_token绑定
  同一任务。错误agent/token不能完成别人的任务，未poll不能答复。
  终态重复上传最多确认同一已知结果，不能重新执行或替换结果。
- 精确合法MCP错误/结构化数据保持；无法交付的已认证终态用固定
  错误有界完成，不把任意远端错误文本写入指标/普通日志。
- 关闭、取消和期限均停止本任务等待；记录是否已交给agent，接入
  现有MCP未知结果/不重放合同。进程重启不重放内存队列中的工具。
  远端会话存活与原资源权限继续由已实现的加密会话链证明。

## Wire与必要验证

采用固定来源中的plural `/v1/tunnels/{id}`、`/poll`、`/response`；
response支持jsonrpc_response、jsonrpc_notify、notify_ack及
session_termination_response；channel缺省main。可选诊断/timing
不影响任务权限，未知可选元数据不能破坏合法主业务。

先冻结并成组验证broker的真实并发取走、精确响应、取消/超时、
失联/关闭、作用域/token/generation、超限和进度终态边界。接线后
用本机真实HTTP/HTTPS私网MCP与原生agent验证列表/调用/资源、审批
和重启不重放；再做必要官方SDK/真实模型业务。非性能任务不跑
完整性能/全模型质量矩阵，已验且身份未变的证据复用；GPU通过
严格同一阶段租约，当前尚未启动GPU、rev126默认保持。

内部broker已完成[16项针对性CPU验证](evidence/serve-mcp-tunnel-broker-cpu-20261116/README.md)。
原生HTTP/策略与独立准入已完成[26项CPU及5项入口/封包验证](evidence/serve-mcp-tunnel-api-cpu-20261116/README.md)。
隔离传输已有[38项CPU证据](evidence/serve-mcp-tunnel-transport-cpu-20261116/README.md)，
自有agent已有[32项CPU/4项实际CLI证据](evidence/serve-mcp-tunnel-agent-cpu-20261116/README.md)，
完整隧道业务/发行仍需完成。
保留内容预算含封套、原RPC ID及失败收尾空间，不等于物理RAM硬限额。
无外部IO的broker复用已认证调用方；接线继续保持隔离worker的审批/
会话链与单次派发，不能另建绕过原权限的执行入口。

## 原生接入配置与后续传输

`--mcp-policy-file`的version1新增可选native目标，例如：

```json
{"version":1,"targets":[{"kind":"tunnel_id","id":"tunnel_project",
 "transport":"native","generation":"project-1","scopes":["client:user"],
 "agents":["client:private_agent"],"channel":"main"}]}
```

该文件仍须私有/owned常规文件，可通过`q4t mcp-policy-check FILE`
离线验证并打包。HTTP目标未写transport时保持原语义；native不接受
URL，agent固定目标在其本地配置中定义。此例当前用于接线准备，
实际MCP传输和自有agent已接线并通过CPU，尚未完成完整业务发行，
不应当作已验生产部署配方。

采用官方`X-Tunnel-Client-Instance-Id`和原shard响应头，无需自定义
客户端身份头替代wire。poll/response的每客户容量为1/2，全局4/4，
17MiB独立正文预算，metadata走观察名额；只有配置native路由才在
core增加8个公开连接名额。R08物理RAM计量/硬限额仍另行完成。

现有隔离MCP worker已通过有界IPC交给本进程broker；复用
原会话、checkpoint与before_call，不在主进程另建外部HTTP解析器。
worker仍不持有调用方回调；当前请求的取消/绝对期限由父进程验证。
原agent实例已进入加密会话快照，旧资源/审批不能迁移到新实例。
无结果的派发不重放，已知MCP错误的结构化data不能被HTTP状态遮蔽。

父进程native会话最多16项/每客户4项，清理不持有请求回调；最多
1秒异步termination，完成即释放容量，agent缺席不会阻塞模型。
已知终态回传的隔离解析有100毫秒收尾，随后不能再派发外部操作。
自有agent已验证固定私网目标、网络/TLS策略、HTTP/SSE与会话终止，
实跑TLS入口到真实HTTP对端；后续仍须完整SDK/模型业务及发行。

## 自有私网agent（当前候选，待完整业务发行）

使用候选的`q4t mcp-tunnel-agent check FILE`离线校验，
`q4t mcp-tunnel-agent run FILE`前台运行。FILE必须是当前用户拥有、
无组/其他用户权限、无符号链接且单硬链接的常规文件，最大64KiB。
配置和凭据通过私有IPC进入子进程，不出现在子进程参数或普通输出。
下例均为占位配置；实际地址、端口、CA与凭据由部署者提供：

```json
{
  "version": 1,
  "api_base": "https://q4t.example/private/v1",
  "api_key": "agent-api-key",
  "tunnel_id": "tunnel_project",
  "channel": "main",
  "control_fetch": {
    "public_networks": false, "allow_http": false,
    "allowed_networks": ["10.10.0.0/24"], "ports": [443],
    "ca_file": "/etc/q4t/control-ca.pem", "timeout_ms": 15000
  },
  "target_fetch": {
    "public_networks": false, "allow_http": false,
    "allowed_networks": ["10.20.0.0/24"], "ports": [443],
    "ca_file": "/etc/q4t/private-ca.pem", "timeout_ms": 15000
  },
  "target": {"url": "https://mcp.internal/mcp",
             "authorization": "private-mcp-token", "headers": {}},
  "restart_limit": 3
}
```

`api_base`包含实际入口前缀与`/v1`；只写scheme/host时补`/v1`。
两侧独立网络/TLS策略复用Files传输，默认允许公网上的HTTP/HTTPS
80/443，私网须明确放行；本示例显式仅TLS。agent不跟随HTTP重定向，
旧SSE协商只接受原目标同源endpoint。私网目标只能来自本地配置，
任务不能选择URL。固定凭据与请求委托凭据冲突时在私网I/O前拒绝。
控制面API Key与工具凭据分离，前者不转发给私网MCP。

前台父进程只监管自己的隔离agent子进程：1GiB虚拟地址空间、32个FD，
35秒无阶段进展则有界回收；初次启动之外每5分钟最多restart_limit次
（0—5，默认3），用尽后等待窗口恢复。主动SIGINT/SIGTERM停止时给
2秒清理，不再重启；明确的控制面401/403/404返回退出码2，需修正
配置后再运行，清理失能不能改变该决定。重启产生新实例，原有状态
会话和资源不能迁移或重放；不会启动/重启模型或其他系统服务。

私网会话最多16项、闲置5分钟/绝对1小时；DELETE失败只影响有界
清理，不改变已知工具结果。现代HTTP会话与旧SSE连接保留原RPC ID、
错误data和会话权限。相同终态可在原deadline内最多尝试上传3次；
这是结果交付重试，工具只调用一次。通知上传不确定时不重传，
仍读取最终结果。poll返回超过limit但不超过25条时逐项处理，原响应
头到达时刻锚定每条可选response_timeout，通知与重试均不续期。

此组件已通过32项CPU及4项实际CLI验证。完整业务链、SDK、恢复
资格与可回退发行/main仍属于后续必须完成的任务；不能把该命令
出现或单项通过写成总目标完成。

真实引擎ELF的cuBLAS只读映射已超过512MiB，agent/MCP共享的1GiB
上限包含该基线，沿用既有工具worker做法；不是1GiB常驻RAM预算。
R08物理内存硬限额另行实现。首次实际CLI失败及诊断保留。

q4t agent使用可选wire扩展`q4t_outcome_unknown:true`区别传输合成错误
与远端真实RPC错误；只允许错误终态，broker把它纳入重复结果身份。
隔离调用与Responses持久状态保留unknown并禁止再次许可；无效终态
同样按未知处理。远端RPC错误data里的同名内容不作为传输权限。
这是q4t原生扩展，不宣称该字段属于所参考的官方wire标准。

## 公开合同与可选前置

三条路由已登记到[OpenAPI](serve-openapi-v1.json)和独立
[wire schema](serve-mcp-tunnel-v1.schema.json)。显式nginx模式的两个
渲染器均保留实例/结果凭据，poll与response使用独立槽位和速率；
配置仍可选，默认原生入口不依赖nginx。实际TLS、容量、重复/指定头
拒绝和原生回归见[22项CPU证据](evidence/serve-mcp-tunnel-contract-cpu-20261116/README.md)。
完整SDK/实际模型业务、提供方与恢复发行继续，0新增GPU。
