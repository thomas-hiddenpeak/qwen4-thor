# 原生 MCP 隧道接入与容量分离 CPU 证据

2026-11-16，26项C++合同首轮全过（0.336秒），4项真实原生ingress
首轮全过（unittest 5.768秒）；另补1项无URL的native策略通过实际
引擎CLI和私有封包验证（wrapper 0.536秒）。7项来源负控、680锚点
通过。主机（174.151秒）、完整引擎及原生ingress（113.455秒）构建
零警告；此次无失败重测。全部均为CPU，0模型启动/0GPU，rev126
默认与发布记录保持；精确引擎及helper SHA见index.json。

新增原生transport与HTTP映射保持独立，目标指纹包括传输类型和
channel，旧HTTP绑定保持。原生路由不接受URL；客户与agent作用域
分别配置、私有封包保存，公开配置不输出私网名称和作用域。

公开metadata/poll/response走已有原生鉴权/TLS/前缀解析，受保护
X-Tunnel-Client-Instance-Id与X-Tunnel-Shard-Token只从HTTP头取值，
重复、Connection提名和trailer覆盖被拒绝；原RPC精确整数保留。
CPU broker/API是实际实现，真实ingress对端为CPU协议oracle，未
冒充完整GPU服务或完整MCP业务链验证。

轮询/响应分别每客户1/2、全局4/4，不借生成、取消或健康名额；
正文独立17MiB预算，响应4MiB。实际入口在生成占满、轮询占满、
两个响应通道同时持有时，仍可回传及查看metadata/model；额外
请求按各自容量拒绝。TLS实跑证明认证作用域和原token/body转发。
模型readiness不依赖broker/agent在线；停止在普通控制线程唤醒
broker，不在signal-safe RequestStop里加mutex。

下一步：隔离MCP worker的原审批/会话与native队列接线、自有agent、
提供方适配、SDK/真实业务、恢复资格、可回退发行及main。当前仅
完成接入部件验证，完整MCP/隧道仍未交付，全部master继续。
