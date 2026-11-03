# SRV-10c客户端隔离与请求预算

当前已完成[本机限定接受](SERVE_CLIENT_RESULT_2026-11-04.md)及
[默认交付](SERVE_CLIENT_DELIVERED_2026-11-04.md)。下文保留开发合同
与首轮记录；其中“尚未运行”只描述首轮时点，不覆盖后续实际验收。

上一回合12d已限定接受并推送，是实际进展。用户确定受控内网、多个
客户端、TLS网关及独立API Key。本批继续补访问与资源要求，不把
既有并发/速率限制当成token预算或客户端状态隔离。

## 当前事实与目标

本批前的nginx按认证client限制data/control连接与请求速率；不传递客户端
身份。runner的request_id、session_id及prefix_cache_scope共用命名空间。
取消已有独立256位能力凭据，缓存还核对prefix；共用命名空间本身不是
已证实的数据泄露或无凭据取消漏洞。问题是没有独立客户端状态域和
输入/输出工作量策略，不能宣称已有完整多人资源隔离。

本批目标：网关认证后传递已配置client及独立后端共享凭据；runner
在body读取前验证，拒绝伪造/缺失/重复头。在同一个私有固定policy中
定义1..64个client的数据/控制在途数、body上限、输入/输出token上限。
请求/取消/私有与共享prefix键均按client隔离，wire ID保持客户端原值。
token上限作用于真实模板后的输入和总生成预算，preflight与chat使用
同一实现；明确拒绝超额，不通过静默截短改变业务输入。默认省略输出
预算时受客户端上限约束。控制通道不消耗data额度，计数保持到终态清理。

单客户端未启用policy的既有行为保持；提供身份头但未配置policy时
拒绝，不能悄悄忽略。policy只在启动前加载，更新需新包/显式换代。
网关必须核对实际后端policy摘要，防止入口配置与运行时额度错配。
凭据不进入wire错误/发现/日志。只监听loopback的后端与同机可信运维
边界保持，实际LAN/CA/来源控制和部署验收仍需单独完成。

## 分步实现与验收

1. 先完成私有policy解析/身份/RAII请求额度及命名空间核心的host合同。
   核心通过不等于serve已接线；固定64个计数槽，不随未知ID增长。
2. 接入HTTP头、配置、请求/取消与缓存键、真实token规划和指标；补
   网关及托管包固定policy/旧包兼容。成组构建与针对性CPU验证，保持
   旧失败及未变证据，不随每个文件反复推理。
3. 冻结真实TLS双client短验：同ID独立请求、跨client取消拒绝与自身
   取消/恢复，额度与preflight一致，真实只读资料工具/引用回填。
   会话/prefix隔离需直接存储合同及适用实际缓存证据，不能用无缓存
   默认请求代替。每项未证明的边界分别列出。
4. 按既有授权完成有界部署/回退与阶段提交；新ELF长验仅交接给用户，
   不自动跑小时/天级工作。GPU/HTTP始终单租约，不干预外部业务。

任务类型为业务可靠性，不宣称吞吐优化。请求额度与单次token上限
不等于按分钟token计费、显存/整机RAM按租户硬切分、长期公平调度、
分布式租户库或未知工具任务自动重放；这些要求仍按工业化矩阵独立推进。

## 已接线的实现与首轮host范围

2026-11-04成组修改：`client_policy`加载owned/single-link/0600或0400的
常规文件，拒绝叶symlink、重复JSON字段、浮点/指数额度和未知字段。
固定64计数槽；身份只能由已配置client和64位小写hex网关凭据共同
验证产生。该凭据应由`secrets.token_hex(32)`生成，不使用外部API Key。
无policy时提供内部身份头会拒绝，空/重复头不能退回legacy。

`--client-policy-file FILE`要求127.0.0.1且禁用实验media。精确GET
healthz/livez/readyz/config/metrics保留同机可信运维入口；有身份头的
请求不能进入这些入口。所有其他路径在policy模式先鉴权。网关删除
所有传入头后仅重建必要头，内部client来自API Key映射，token来自
私有包。外部请求不能选择内部client。独立control额度不占用data。

运行时普通JSON错误：401鉴权、403客户端访问本地管理、413客户端body
上限、429客户端并发、400输入/输出token上限。并发额度保持到响应与
heartbeat清理完毕，包括读取body/解析/排队/异常路径；仍同时受全局
连接、正文、请求及内存边界限制。客户端预算不是在途数量的保留份额。
固定client标签仅出现在本机metrics；发现只给开关/数量/policy摘要。

策略格式version1，根字段仅`version/gateway_token/clients`。每个client
必须提供`id/max_inflight/max_control_inflight/max_prompt_tokens/`
`max_output_tokens/max_body_bytes`；上下界见生产`client_policy.cpp`。
网关prepare和模型托管prepare都新增`--client-policy FILE`，必须输入
相同原始字节；网关client集合必须一致。新backend包version4、新网关
包version2、新托管网关包version2；已有包继续通过包内固定入口工作。
策略改变需新包/新后端，不能把旧进程配上新policy；不自动轮换或重启。

首轮CPU出口冻结：公共host全量；必需host包含12组新身份/私有文件/
并发/取消/HTTP头/正文前拒绝/token预算/发现合同；既有structured
合同；真实SSD存储的双client同key保存/恢复和共享prefix命中/拒绝；
8组新包合同与受影响网关/托管后端回归。构建零警告，代码身份和默认
pair记录。host socketpair无监听、storage仅host copy，不计模型/GPU
或真实业务通过。实际TLS/模型/缓存/工具闭环须后续单租约冻结短验。

## 首轮开发验证记录

`.q4t-work/serve-clients-20261104/build-v1/`保留首次独立HTTP测试target
漏链失败；生产q4t与host编译成功，但整组构建不记通过。补入两个
client实现及SHA依赖，并增加2组真实HTTP入口合同后，`build-v2/`
零警告通过。候选ELF SHA256为
`5487bc672348e1f176f14d459b9097a7f14010226fd5005b22f2332df5696f31`。

`cpu-v1/`第一次执行全部通过：280公共host、246必需host（包含12组
新合同，不与公共host相加宣称独立用例数）、9 structured后端；真实
SSD存储28检查点，双client独立恢复、共享scope隔离和失败不发布；
70组新旧包/网关/托管回归，其中8组新增。inventory三种变异拒绝和
生成合同核对通过。nginx仅执行语法检查，不监听；模型/生成/GPU均0。

新HTTP生命周期binary有27项，尚未运行；实际双client模板预算、取消、
cache模型状态与工具闭环须下一步短验，host通过不批准替换默认。
当前GPU计算进程空、SOAR恢复unit masked/inactive、sudo只读确认容器
空，原锁仍device66305/inode3294067。本批未启用服务或执行day。
