# GPU-covered mirror recycle：冻结协议草案

本草案已按 root 2026-10-06 范围决定收敛；不授权执行。第一项运行时测试
必须是新版同 binary 的 on 质量 HTTP 11 请求。构建和静态审查不计测试。

## 唯一干预与固定项

- 新开关 Q4T_MOE_MIRROR_GPU_RECYCLE=0/1，默认 0。
- 仅显式 single-decode 且 T=1；prefill singleton、未知相位和批量路径不参与。
- off/on 同 binary、同输入、同精度、GEMM、C=256、L2=16、mirrorK=8、
  max_open=200、max_seq=1、max_prefill=8192、max_len=262144、MTP off。
- partition/request_partition/decode-log-quiet/chunk_order 全 0，不捆绑旧 NO_GO。
- supply observer、route trace、阶段诊断关闭；既有 per-request stats 保留。
- 性能服务 host/cache memory.max=16 GiB，memory.swap.max=0；此非整机 RAM 限制。

## 最小冻结请求上限：131

1. q01-quality-on：固定质量集 11 请求，不清 cache、不限 16 GiB host/cache；
   精确匹配固定旧质量 oracle 的输出、usage、finish 与 HTTP/SSE 合同。
2. 首测通过才执行新 selector C++ 合同、受改动工具合同及唯一新真实权重
   moe_mirror_gpu_recycle_numerical_contract：全驻留reference 对比 same-cache
   OFF/ON，要求 ON changed==1，完整 BF16/router 与 OFF/ON FP32 相等，并核对 slot 身份/tick；
   不称全 slot raw payload 字节比较。旧三项 GEMM/格式
   证据只在身份不变覆盖内复用，不能覆盖新策略；只运行冻结受影响清单。
3. history 四服务固定 ABBA：h01-off、h02-on、h03-on、h04-off，每服务
   [16385,8192,8193,1024,45056,4096,8192] 连续三轮，共 84 请求。
   配对区块 B1=(h01,h02)、B2=(h04,h03) 分别判定全部七位置。
4. matrix 六档各两服务，每服务连续同输入 3 请求，共 36。组序：
   1024 off/on；4096 on/off；8192 off/on；45056 on/off；
   204800 off/on；261887 on/off。每档输出 256，目标 261887 输出 257。
   总 17 服务、131 请求；不自动增加第二矩阵或有利样本。

每个性能服务开始前目标模型文件只读 cache advice，最多两轮；最终 payload
resident bytes 必须为 0，advice 成功本身不是冷态证明。每服务新 GPU/L2/mirror
缓存；服务内无清缓存、无生成热身、不删除首请求。history 同位置不同轮
共享服务历史，两个 8192 位置独立。matrix 每档单独服务，首请求是真正该
服务首请求，与历史整六档单服务数据不混算。

## 预先门与有界出口

每个 history 配对区块每位置、matrix 每档均要求：on 首 TTFT <= off 首 TTFT；
on 后两次 TTFT 最大值 <= off 后两次最大值；on 三次 decode TPS 最低值 >=
off 三次最低值。所有输出、usage、身份、容量、路径、冷态、cleanup 合同通过。
任一速度门失败永久否决本候选，仍完成原先冻结 matrix 覆盖；不修改 epsilon、
不追加样本。history 第二区块也保留，即使第一速度 NO_GO。
非速度合同失败停止依赖工作，保留第一次失败，只在具体缺陷修复后以新身份
重验受影响项；不能拿无效运行进入性能判断。没有第二策略。

这沿用已有 observed-range 门，三样本不构成稳定尾延迟或微小提速的统计证明；
无 epsilon 的微小失败只表示门未过。history ABBA 降低线性组序影响但不消除
所有时间漂移；matrix 在档间交替顺序，单档仍可能受组序/时间混杂，不能作
纯回收策略速度因果分解。全 54 GB 物理并集继续 INDETERMINATE。

## Runner 适配最小范围

run_budget_experiment.experiment_environment 会清除所有继承 Q4T_*，因此仅在
shell 设置新变量无效。需 offload_policy 新 mirror-recycle axis 和独立 switch，
严格拒绝旧 partition/quiet/diagnostics 混合；run_budget_experiment 增加 CLI、
显式 env/协议记录和允许复用 request-policy-sequence 的固定 history 输入。
run_acceptance 已转发净化后的环境，既有 fixture/重复/HTTP/16GiB/cleanup 不改。
新增 mirror_recycle_protocol 用启动及逐请求汇总验证 off/on、显式相位、
activation/counters；启动行绑定 schema=q4t.mirror_gpu_recycle.v1；每请求 id 与七字段
plans/attempts/preferred/changed/fallback/unavailable/published 严格闭合，
off 全零，changed>0 证明实际改槽，单请求可为零。无需逐 token 日志。

R 内专用控制器复用 supply observer 的 owned run/cleanup，调用 cwd 必须显式
传新 W；不复用其硬编码 5 服务/27 请求 gate。旧 decode-log 的 audit_group、
compare_groups 硬编码 ABC 和旧候选，必须做薄适配，不可当现成 acceptance。
新资源审计只改 terminal/group/axis 门，复用已有严格 raw counter/window 算术。

## 可复用证据与磁盘

复用固定11输入/oracle、六档与 history fixtures、hot-list、model config/index
SHA；复用不变 cleanup/资源算术/输出与 history/matrix 指标函数的既有合同，
新工具改动依赖项重新执行相应合同。旧 HTTP 性能和旧 runtime 数值仅作历史
参考，不能代替同 binary off/on。旧模型文件只做 metadata（不 hash payload）。

历史 history-a 21 请求证据约 86 MiB、matrix-a 18 请求约 160 MiB；本协议
预计 HTTP 原始证据在约 1 GiB 量级，但这不是执行容量保证。前置剩余空间
至少 1 GiB，每服务前检查；不复制 source tar，不开 route trace，不压删旧证据。
资源采样采用既有 interval=1s、GPU=10s、file-cache=endpoints；原始 counters
只单次审计，不反复深读。每请求 stats 可解释写回选择频率但不是 causal READ
节省或提速证据，PID/device/software IO 不混加。
