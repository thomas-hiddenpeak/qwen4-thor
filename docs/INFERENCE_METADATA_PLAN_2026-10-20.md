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
