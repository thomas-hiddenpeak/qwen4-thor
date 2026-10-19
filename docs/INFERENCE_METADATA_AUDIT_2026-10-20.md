# 推理元数据生命周期审计

状态：静态审计与局部合同验证完成，运行时整体验收未完成。
范围与固定Megatron版本见[计划](INFERENCE_METADATA_PLAN_2026-10-20.md)。
这份结论不把一次成功的stream同步扩大为设备故障后的可恢复保证。

## 所有权和完成边界

| 路径 | 源、目标与复用条件 | 结论 |
|---|---|---|
| 普通多序列decode候选 | owner复制token/position/slot及RoPE至自有pinned源；上传事件完成后才重填；GPU目标仍为Model scratch | CPU来源寿命独立于调用方；事件不覆盖后续kernel，调用方仍需串行化GPU消费者 |
| ModelVerifyMulti | positions/token_seq/rope_positions在finish同步之后销毁；失败调用finish并失效checkpoint | 正常完成及提交失败路径有显式边界；同步失败返回错误，不声称GPU仍可恢复复用 |
| MtpSpeculativeStepMulti | step-owned数组到cleanup同步后离开作用域，错误使输出失效 | 仅覆盖本层持有的数组，不延长嵌套函数内部临时数组寿命 |
| ModelSequence | Submit持有submitted_；CompleteSequenceWork先完成再发布成功前缀或失败 | 保护已提交token及逻辑状态；不能代替底层临时源所有权 |
| PLE pinned staging | 既有upload_complete事件管理源重填 | 复用既有修复与证据，本阶段不改其数据路径 |
| ModelDecodeStep/ModelDecodeBatch | 标量token/position/RoPE及临时数组用于Async H2D | 依赖pageable staging；本机诊断未复现覆盖，不能据此证明通用异步寿命安全 |
| RunPrefill | 局部rope_pos、多模态mm_pos上传后离开局部scope | 外层之后排空不能延长这些scope；不迁移至本次普通decode owner |
| ModelPrefillBatch/ModelMixedBatch | ragged描述符及循环复用rope_row | 同样保留pageable依赖；未证明实际损坏，不宣称全路径已修复 |
| MtpDraftExtend chunked | 每块seqid临时vector用于Async上传 | 外层cleanup与device缓冲释放不构成CPU源寿命证明；非chunk路径使用同步cudaMemcpy，需分开看待 |

固定Megatron参考的可借鉴之处是固定GPU视图加明确的源所有权与
事件完成协议；仅拼成连续buffer不足以证明生命周期。当前只迁移一个
入口，未引入图执行、调度策略或额外框架。

## 取消和槽位复用

`src/server/chat_scheduler.cpp`的普通decode在model_mu下提交、读取并
FinishHostReadback，再通知done；chunk prefill和batched prefill亦先
完成再通知。FinishHostReadback调用CompleteSequenceWork并同步默认
stream；错误标记gpu_healthy=false。`chat_generation.cpp`提交后等待
done的谓词不因取消而提前退出，因此正常取消不会跳过这道完成边界。
请求cleanup释放请求资源后FreeSeqId；ModelBeginSequence在复位层状态
后同步，成功才Begin，复位失败使sequence失败。

scheduler_stop分支唤醒的是调度循环尚未执行的pending请求；同一个
调度线程不能同时在该分支和正在执行的forward里，不能把它误读为
任意打断已提交GPU工作的通知。设备同步失败时不得推论scratch已安全
排空；服务失败策略和源保留策略是不同合同。

上传owner在完成失败时保持pending并禁止再次写源；析构仍无法确认
完成时保留源和事件，避免释放可能在使用的CPU数据。此策略会保留
资源，不提供设备故障恢复能力，也不保护已释放的GPU目标。
host故障注入覆盖这些owner行为；实际HTTP取消矩阵尚未运行，静态
调用顺序不能替代该验收。

## 独立发现的容量缺口

ModelDecodeBatchMulti原先只拒绝B>max_seq，但ids/positions/embedding/
trunk及workspace按max_prefill分配；server配置、预算和普通decode组批
没有保证两者大小关系。新增B>max_prefill拒绝位于任何输入访问和
上传之前，属于既有内存容量缺陷的最小防护；不修改调度或扩大分配。

6项测试直接调用真实函数，拒绝用例使用空输入指针；边界内用例到
owner缺失检查即停止，不加载模型、不执行GPU操作。零警告构建及
完整guard终态通过。首次未重新配置CMake导致新目标不存在，原失败
日志保留。见[证据](evidence/inference-metadata-20261020/bounds-host-v1/result.json)。

## 验收边界与剩余工作

普通decode owner四层差分和HTTP证据对应容量修复前的候选；对象本身
未变，可复用其对象级证据，但新源码不能据此标为整模型通过。
当前独立build中的q4t仍为旧候选，只有bounds目标和关联静态库已重建；
不得把该目录视为全部目标同一源码身份。默认build/q4t保持原部署。

两次MTP质量阶段均ENV_ABORT，唯一一次恢复预算用尽。取消与五档未
运行，未建立性能分辨带，不接受性能结论；后续驱动必须重新冻结
候选身份及受影响合同，不能直接运行旧tail作为新源码验收。
剩余项为环境超时诊断、运行时验收及候选保留/回退决定。其他pageable
入口保留明确限制，不借本次局部通过宣布全引擎异步寿命可移植安全。
