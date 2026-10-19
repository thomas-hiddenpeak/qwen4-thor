# 推理元数据与异步缓冲生命周期

状态：ACTIVE_AUDIT。用户2026-10-20授权按计划设置目标并推进。
起点52e3cdc，复用main，默认binary保持44abd8b1，不部署候选。
身份见[evidence](evidence/inference-metadata-20261020/baseline.json)。

## 有界范围与出口

本阶段只处理现有推理元数据：token、logical position、sequence slot、
ragged descriptor、RoPE上传源，以及其完成/错误退出/复用关系。
审计普通decode、prefill、MTP调用方和服务完成边界；PLE已有上传事件
作为所有权先例，不能借此改写PLE数据路径。CUDA Graph、多请求调度
策略、offload、强制跨形状逐位一致均不在本阶段。

先完成代码生命周期矩阵，区分已证明安全、依赖调用方、待核实与
真实缺陷。若真实缺陷成立，最小修复优先；如需统一快照，首先限定
一个入口，明确host所有权、GPU固定地址、有效元素和完成事件。
不为统一风格迁移所有Model scratch，不把CPU元数据快照与KV/SSM
持久状态混成一个对象。

当前预算：源码/历史/参考只读审计，GPU、模型加载、HTTP均0。
实现与采样前再冻结具体基线/候选、受影响合同、运行次数与停止条件。
性能目标（包括合并H2D减少开销）按EVALUATION的HTTP优先顺序；
缺陷修复可先直接合同测试。所有GPU阶段完整持gpu_exclusive锁。
不自动追加计时样本，不把有限工程接受旧授权套到新候选。

完成出口：提供审计结果、每项安全依据、最小改动或不实施理由、
对应验证及局限，更新状态/日志并分阶段提交推送。仅审计阶段完成
不等于目标完成；未经运行时验收的候选不替换默认binary。

## 已核对的参考

固定Megatron-LM 3e2807ce5e787f81b7275be2383092fe0c3bfc71：
`contexts/gpu_view.py`将字段映射至固定地址连续GPU buffer；
`dynamic_context.py:2793`明确legacy阻塞上传，异步调用记录event并在
源复用前等待。关键是所有权与完成协议，不是单纯拼成一次memcpy。
只借鉴机制，不运行Megatron或引入PyTorch依赖，不修改参考检出。

## 第一轮代码矩阵

| 路径 | 已确认机制 | 尚需处理/核对 |
|---|---|---|
| 服务plain decode | scheduler的tokens/positions/seq_ids活到FinishHostReadback；model_mu覆盖forward、readback与排空 | 内部ModelDecodeBatchMulti循环局部rope_position不受调用方vector寿命保护 |
| ModelVerifyMulti | positions/token_seq/rope_positions持有到finish(stream sync)，失败也经过finish，checkpoint失败失效 | 复用已有B1证据；不为统一外观删除其完成边界 |
| ModelDecodeStep | 上传按值token_id、pos和局部rp，随后RunLayers | 逐项核对源scope与实际CUDA传输合同；不能用外层稍后sync延长内部局部变量寿命 |
| ModelDecodeBatch/RunPrefill | 临时positions及RoPE数组用于H2D | 核对正常/失败返回与数组作用域、相邻上传复用 |
| ModelPrefillBatch/ModelMixedBatch | 临时ragged描述符，rope_row在循环内resize/复用 | 核对pageable staging保证；单独登记reset返回值检查，避免顺带改其他问题 |
| ModelSequence | Submit保留submitted_；CompleteSequenceWork排空后提交或失败；End拒绝pending | submitted_不能自动保护底层函数另建的临时上传源 |
| 槽位复用 | 请求清理FreeSeqId；ModelBeginSequence复位并sync后Begin | 继续逐个错误/取消分支核对清理先后，不凭正常路径推断所有路径 |
| PLE embedding | pinned staging配upload_complete事件，重用前等待 | 已修复机制复用；不把pinned与pageable语义混同 |

CUDA文档对Async pageable传输只说可能对host同步，不能由Async名称
证明未完成，也不能由pageable名称证明已经完成。下一步需要固定
本机CUDA13.3适用合同与受影响源的具体行为；当前不宣称已经复现
运行时数据损坏，不用历史PLE缺陷代替本次证据。
参考：[CUDA同步语义](https://docs.nvidia.com/cuda/cuda-runtime-api/api-sync-behavior.html)。

当前设计倾向：先补齐上传源寿命与完成协议，再决定是否有必要合并
H2D布局。单纯固定GPU指针不能解决CPU源提前复用；增加pinned buffer
而遗漏event反而会引入历史已发生的风险。

## 有界传输寿命诊断（执行前冻结）

已核对CUDA13.3归档同步合同，Async pageable没有无条件host完成保证。
为区分本机是否立即暂存，仅运行独立transfer_probe（不跑模型）：
4种int计数1/4/8192/626688 × pageable/pinned × 提前复用/完成后复用，
共16次H2D。每次stream前置50ms host callback延迟，不调用CUDA API；
源始终活着，不访问已释放内存。提前复用为故意错误的负控，记录
原值/覆盖值数量及event是否完成；完成后复用必须全部原值。
记录pageable access/host page table属性；不把本机未复现作为通用
安全保证，也不把负控损坏宣称已经复现模型路径损坏。

构建g++-14 C++23 -O2 -Wall -Wextra -Werror，零警告；一次进程，
60秒上限，无重试。run_transfer_probe.py完整经gpu_exclusive.py持锁，
输出.q4t-work/inference-metadata-20261020/transfer-probe-v1/run，
锁记录同父目录lease。GPU竞争/监控失败ENV_ABORT，保留全部现场。
本阶段是正确性诊断，不形成性能结论；copy调用时间仅帮助解释是否
阻塞，不是benchmark。生产/默认binary保持，HTTP/模型加载0。

## 传输诊断结果与下一步决定

[原始16例](evidence/inference-metadata-20261020/transfer-probe-v1/raw.json)
已完成，单lease COMPLETED、子进程0、7文件摘要复核通过。
本机pageable_access=1、host_page_tables=1。四种pageable源早复用
均保留原值；前三种event尚未完成但值保留，与内部暂存解释一致，
最大2,506,752字节的调用约50ms、event已完成。只描述本次观察，
不能证明未来驱动、其他大小或任意错误路径同样安全。

四种pinned源早复用全部变为覆盖值；8种等待event的正控全部原值。
因此不能直接将局部vector替换为复用pinned staging。必须将完成
事件与缓冲所有权绑定。该负控证明风险机制，不是现有模型错误复现。
CUDA13.3来源已明确固定：
https://docs.nvidia.com/cuda/archive/13.3.0/cuda-runtime-api/api-sync-behavior.html

候选设计收敛到单个入口ModelDecodeBatchMulti：拥有本步token、
position、slot及RoPE源的上传对象；复用前完成检查，失败路径也保留
源，析构排空。GPU字段有效范围和目标地址显式列出。先保留现有
计算形状与kernel，不把KV/SSM加入快照，也不改ModelVerifyMulti已
明确的finish协议。是否合并GPU布局仍待实现评估，不能以名字叫
snapshot就宣称降低复杂度。不会因当前未复现pageable错误而跳过
其余路径与取消/复用审计。

若候选目标包含减少H2D/执行开销，第一项测试仍必须HTTP E2E，
本次诊断不算未来候选的前置性能通过。候选实施与准确采样预算尚未
冻结，当前不运行新的模型/GPU测试。目标保持active。
