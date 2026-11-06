# 视觉 attention 行最大值：有界优化判定卡

2026-11-07，父提交f7dcd8f9。用户授权推进优化目标；本阶段唯一工作分支
`codex/vision-attention-rowmax`。任务分类为性能优化，第一项测试必须为
tools/evalscope真实HTTP E2E，必要构建不算测试。

## 改动与身份

只将AttentionKernel每个线程重复扫描S个分数，改为线程局部最大值与
256线程block归约。复用原sum的1KB共享scratch，不新增分派、环境开关、
精度或缓冲。保留`-1e30f`初值、fmaxf语义、QK点积顺序、exp、原sum
归约树、归一化、V累加顺序与BF16舍入。最大值读取结束后加明确屏障，
再将scratch用于sum。新增归约屏障的收益尚未证明；不能仅凭源码接受。

父程序复用`.q4t-work/vision-forward-timing-20261107/engine/q4t`，SHA256
`982bba071e68e807cda653fa32ab76bed692808aa2d9325876ac09ea9f9daa1d`；
其运行时来源与当前main一致，后续只有文档。候选独立构建到
`.q4t-work/vision-attention-rowmax-20261107/engine/`。启动前机器plan冻结
父/候选、源码、编译参数、模型配置、evalscope源与输入的SHA256。
默认ea1b474d及sidecar3eb46385保留；旧包不迁移。

## 第一阶段：HTTP投入门

最多4模型启动、64次Chat、32次控制、0原生worker、1200秒，同一GPU
独占租约。固定ABBA：parent-0、candidate-0、candidate-1、parent-1。
每次启动只有一个evalscope client，单流16请求，无连接探测/隐式warmup：
首个JPEG地球单列冷请求，其后按小图、大图、多图、8帧视频、文本42
顺序重复3轮。输入/真值复用前一诊断，仅改请求ID/取消令牌并统一SSE，
以便同口径记录TTFT和decode；原各题max_tokens、提示/schema保持。

两侧`--allow-media --no-mtp --max-seq 1 --max-prefill 8192 --max-len 16384`，
两个视觉计时开关均关闭；无profile、调频、cache开关。每启动记录模型
身份、实际请求顺序、首请求、原始evalscope数据库、答案/终态/usage，
HTTP latency、TTFT及总decode token/总decode时间。客户端不访问CUDA，
保持模型独立退出台账，完整退出后新建台账而不释放租约。

本候选改变执行映射，按提速型评价。**投入目标**：大图与多图各自的
热请求HTTP平均耗时，在AB与BA两个配对块中均至少降低3%。这是本任务
是否值得继续完整验收的投入门，不是全项目门槛，也不是真实损失容差。
小图/视频/文本、全部TTFT/decode及首请求逐项保留；已确认的可归因
退化独立否决，不用平均收益抵消。全部64题真值/schema/stop/usage和
同题message必须一致，失败保留原件。

两启动/身份只能形成描述性先导，测量分辨带、δ和有效置信区间均
**未建立**。不把请求当独立启动、不做请求bootstrap、不将点估计写成
统计非劣/长期提速。门未达或证据无效即停止，不追加相同样本求通过。
ENV_ABORT单列，仅清理自有进程，不干预其他服务。

## 条件式后续与交付边界

首轮投入门通过后，才冻结第二阶段的具体命令/计数并执行：针对归约的
数值边界与旧实现逐位差分、现有独立HF全塔合同、host与来源检查、固定
HTTP质量，以及父/候选plain与默认MTP各五档（每档3正式请求）。两模式
分别报告全部TTFT/decode与负点，不能以视觉子集代替五档。

第二阶段上限4模型启动、104Chat、64控制、4原生worker、7200秒；
只在第一阶段通过后启用，不为统计不足扩大预算。数值、质量、已确认
性能退化独立否决，依据规范明确接受或不接受；未完成不得替换默认。
必要部署健康检查另冻结具体已验包及最多1模型/4Chat/300秒，无常驻。

首轮未达时交付研究代码、完整原件与NO_GO结论，不合入未验收运行时。
通过完整验收后按既有授权保留回退包、同步适用默认/元数据并提交推送。
不扩展nvJPEG、视频硬解码、视觉并发、FlashAttention或长稳；全局
media_acceptance的既有限制不因性能结果改变。
