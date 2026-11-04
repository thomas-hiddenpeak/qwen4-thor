# SRV-20：当前serve合同与证据入口复核

2026-11-04，复核起点93d4fac4。结论是修正能力表与源码之间的漂移，
并把下一项基础工作收敛到准备阶段等待的可取消性。本轮没有修改运行时、
启动服务或新增业务接受；不能据此宣称serve全层没有遗漏或工业化完成。

后续[SRV-03c](SERVE_PREPARE_LOCK_RESULT_2026-11-04.md)已限定修复R1并
交付8e9b4ce7/a56aed21。下文R1保留93d4fac4复核时的发现；不再代表
当前未修复清单。11k/12l证据仍绑定15d7ccb1，不能自动外推到新身份。

## 已修正的当前状态

| 旧入口的说法 | 当前源码与接受证据 | 本次处理 |
|---|---|---|
| 加载后才监听 | `HttpListeners`在Start加载前实际保留端口；Run接管后才处理HTTP，[12k](SERVE_STARTUP_LISTENERS_RESULT_2026-11-04.md)已验 | 区分TCP连接、HTTP可用与ready |
| 新ELF托管及当前任务协议交接待验 | [12l](SERVE_SUPERVISOR_DEADLINES_RESULT_2026-11-04.md)限定接受托管截止/停止；[11k](SERVE_TASK_STARTUP_PROTOCOL_RESULT_2026-11-04.md)已验保存工具结果交接到15d7ccb1 | 链接各自范围，保留12l首轮ENV_ABORT和补验口径 |
| 无持久幂等存储、无租户配额 | 应用任务已有client键/事件日志/永久保键；runner已有client并发/正文/token预算与状态域 | 明确runner、应用和网关的职责，不改写成通用exactly-once或公平性 |
| 网关未含完整鉴权配置 | 项目网关已有TLS、独立Key、client绑定和显式CIDR的本机短验 | 实际LAN与正式策略按需启用；没有配置不阻塞基础开发 |
| 35字段/6路由仍是当前清单 | 当前38字段、10路由、20处白名单、112源码锚点 | 修正工具README；旧探针仍是旧缺陷观察，不重跑作为新版本正确性证明 |
| 阶段完成记录和剩余项混在页首 | 主矩阵原402行，多处“当前默认/待办”属于不同历史阶段 | 主矩阵按运行方式、API、客户端和剩余项组织；原文独立快照保存 |

入口为[应用服务矩阵](APPLICATION_SERVICE.md)，对应
[vLLM对照](VLLM_SERVICE_GAPS_2026-11-02.md)只更新Q4T侧状态。
本地vLLM仍为67111973eeb104e83bc6707a2d0f139737b711a0；没有刷新网页、
拉取参考库或推导最新外部版本。旧[全链路审计](SERVE_COMPLETENESS_AUDIT_2026-11-02.md)
保留原发现，在顶部明确它不再是当前未完成清单。

## 复核时发现与后续边界

### R1：准备阶段等待自身不能及时取消

源码位置：[RequestDiagnostics::Lock](../include/q4t/server/request_diagnostics.h)、
[thinking准备与prompt encode](../src/server/chat_generation.cpp)、
[preflight](../src/server/chat_preflight.cpp)。前两处使用阻塞
`std::unique_lock<std::mutex>`，preflight使用阻塞`lock_guard`。持锁
tokenization内部虽检查取消，等待另一个请求释放tokenizer锁时，没有
检查等待者自己的deadline、断连或显式取消。

因此，等待者的响应与其准入资源可能要等持锁者结束才能释放。当前
源码足以证明等待路径没有取消检查，但本轮没有构造竞争负载，尚未
测得实际延迟或影响上界。已有合同是协作式取消，不将这个边界伪称为
已复现的硬截止承诺回归，也不假定GPU在途计算可以被即时抢占。

下一批SRV-03c限定thinking准备、prompt encode与preflight：先用可控
持锁者复现等待者过期/取消仍阻塞，再补有界等待检查；测试正常获取、
等待期取消、获取同时取消、异常与资源回收。实际工具闭环短验沿用当前
模型业务，开始前另冻结请求清单、启动次数、token及墙钟预算，同阶段
持GPU原锁。不先改变模型锁、调度顺序或扩大成全部生命周期硬截止。

### R2：客户端能力不能相互外推

- [标准库ChatClient](../tools/examples/chat_client.py)仍经urllib单次
  socket超时；[原客户端合同](AGENT_TOOL_CLIENT_2026-11-02.md)已明确
  不保证整轮墙钟截止。不能把[11j](SERVE_TASK_DEADLINE_RESULT_2026-11-04.md)
  独立持久客户端的全程网络截止当作这个示例也已经具备。
- [JS辅助类](../tools/examples/js/chat_client.mjs)有总截止和零重试，
  但构造SDK时固定本机占位Key；没有显式Key/CA配置或UI delta回调。
  这是项目辅助类的范围，不代表官方SDK缺少TLS或流式能力。选择该
  辅助类接入独立Key网关时需先补齐，不能静默回退为匿名访问。
- 已验证的Python3.42.1、Node24.18.0/openai7.47.0不代表全部版本和
  agent框架。普通工具完整调用后发布、受约束输出完整验证后发布的
  时延边界保持；服务端SSE不等于逐token工具参数增量。

### R3：资源与运营证明仍分层

[client策略](../src/server/client_policy.cpp)限制并发及单请求预算，
[容量队列](../include/q4t/server/sequence_admission.h)按可适配槽位排队；
两者不提供每client长期公平、RPM/TPM或计费合同。单机单客户端开发不
以这些可选业务策略为前置；多人SLO应独立明确后再实现与验收。

MemAvailable准入、CUDA申请额度和可选host memcg域已接线并有各自
接受证据；Thor上CUDA分配与memcg计量的差异仍在，不能合称整机物理
RAM硬保护。专家容量offload只能到SSD，CPU/managed/staging仍占统一RAM。
真实LAN、证书/来源、正式通知和访问/保留策略选择时再部署验收。

当前ELF小时/天级稳定由用户验证；三份day计划未运行且原文件保持。
旧ELF一小时结果、短期压力恢复及当前短验不能替代当前版本长稳。
备份为源可核对的同机交接，源盘丢失灾备尚不在已验范围。

## 静态核对与证据范围

原矩阵正文SHA256为
`b2812d0568fc75b06980bb0cefd6120c9071840bc96b17ef7b5fee325f2f7529`；
[历史快照](APPLICATION_SERVICE_HISTORY_2026-11-04.md)只在原文前增加
历史提示，原402行逐字节保留。现有日志、接受结果及封存归档均未修改。

本轮核对项及可复核摘要见
[snapshot.json](evidence/serve-contract-reaudit-20261104/snapshot.json)：

可在同一源码/发布身份下运行
`python3 -B docs/evidence/serve-contract-reaudit-20261104/verify.py`复核。
脚本只读文件/归档与Git历史，输出JSON；后续源码或包变化将拒绝，
这表示当前身份已变化，不倒推本次历史接受失效。脚本不调用服务/GPU。

- 默认ELF/sidecar/协议与当前接受身份一致；223运行来源未变；112
  审计锚点与清单一致。38字段/10路由/20处白名单只是清单覆盖，不是
  逐行审查或语义覆盖率。人工复核重点为接入、等待、取消、输出终态、
  client策略、示例客户端、托管及其声明边界。
- 当前托管包29文件按包内manifest核对；当前持久应用17来源与11k
  封存读取器逐字节匹配。原包、Key、恢复入口和三份day计划不改。
- 12k/12l/11k三个归档共810个文件：分别核对归档摘要、结果摘要、
  成员唯一性和逐文件内容。只确认既有证据完整性，不重新执行接受
  验证器、CPU业务测试、HTTP、CUDA或模型，也不把旧失败改判通过。
- 只读查询恢复单元仍为masked/inactive/dead、MainPID=0；未启动、
  停止、安装任何服务，也未获取或重建GPU锁。未新增GPU空闲证明。

工业化持续目标仍active。下一项是R1的有界复现与修复；R2按客户端
入口推进，R3按实际资源与多人要求推进。可选部署与用户长验不作为
基础开发停工理由，也不被当前文档核对记为已完成。
