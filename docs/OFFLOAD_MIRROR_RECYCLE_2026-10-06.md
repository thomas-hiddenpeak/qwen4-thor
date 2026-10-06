# GPU 冗余 mirror 回收：默认关闭的单一候选

更新：2026-10-06。承接3a838473，分支`codex/offload-mirror-recycle-20261006`。
默认关闭实现与工具接入已完成，runtime/协议静审通过；14项host、10项
协议合同及一项真实权重数值合同源码已备齐。数值夹具首次静审发现Free遗漏，已增加局部RAII并独立复核；
原发现保留，不称全部静审首过。尚未构建或运行测试，不能
视为质量/性能通过。

## 改动范围

唯一开关`Q4T_MOE_MIRROR_GPU_RECYCLE`严格接受0/1，默认0。仅明确标记的
单请求decode且T=1、无prefill请求上下文时生效；shape-single本身不是门。
完整计划选定victims后，将未被本计划驱逐的GPU居民做成不可变覆盖集合；
不加入未完成上传的missing专家。worker按该集合优先选择event就绪、
未claimed的GPU冗余mirror槽，没有则沿用cursor顺序第一个可用槽。

保留原claim、event、stream、publication、fallback与字节复制路径；不
读取其他worker正在修改的GPU映射。关闭时不分配覆盖集合，不增加ring
扫描或event查询；仍有模式分支和计数存储，不宣称绝对零指令开销。
每请求记录七项计数，其中changed才证明选槽实际偏离原规则；preferred
包含与原首个可用槽相同的选择，不能代替changed。

源码设计与冻结范围见[scope](evidence/offload-mirror-recycle-20261006/scope-plan.json)
及[source-design](evidence/offload-mirror-recycle-20261006/source-design.json)。
上一轮只证明相关端点冗余存在及共同miss候选差下界为正，没有证明可实现
收益；新策略可能因额外工作或后续保留历史变差而被拒绝。

## 有界验收

先批量实现/静审与干净源码Thor零警告构建。第一项测试为固定11题候选
HTTP E2E；通过后才运行相关直接工具合同及确实激活候选的数值合同。
输入、命令、工具身份和精确合同清单在首次执行前另行冻结。

同binary两臂仅回收开关不同。partition/request_partition/quiet/chunk_order
均0，不叠加旧NO_GO策略。C256/L2=16/mirrorK8、max_seq1、max_prefill8192、
max_len262144、MTP/Phase D关闭，保持GEMM与精度。性能服务host/cache限制
16GiB、swap0，质量不设host/cache限制；这不等于整体物理RAM限制。

- 质量11请求。
- history固定[16385,8192,8193,1024,45056,4096,8192]，每服务连续三轮；
  四服务OFF→ON→ON→OFF，共84请求，两个方向区块分别按七个位置验收。
- 完整五档1024/4096/8192/45056/204800，加目标261887；每档OFF/ON各3，
  按档交替组序，共36请求。目标输出257，其余性能请求256。

合计最多17服务/131HTTP，禁止为有利结果补样。每位置/档使用预定
first TTFT、later2最大TTFT、三次最低decode门，无临时epsilon。
有效history速度失败仍完成已冻结矩阵；身份/输出/容量/冷态/清理失败
停止依赖工作，保留首次失败，仅修复重验受影响项。不把微小失败当作
统计显著回退；ABBA和跨档交替仍不能完全排除时间漂移。

## 保护与交付

新隔离工作树，MAIN原七项修改/完整diff/主binary与旧三工作树已建立
入口保护。模型/reference只读；审计只核228项元数据与config/index摘要，
不读权重payload，真实HTTP/数值运行正常读权重。磁盘每服务至少保留1GiB，
不复制旧raw trace，不删除旧证据。资源口径分列，物理54GB保持未知。

完成后独立核结果与交付，按阶段commit/push；默认始终关闭，不合并或
部署。当前旧NO_GO结论保持，最终新候选GO/NO_GO尚待实测。
