# Megatron Core Inference 源码评估

2026-10-16；补充KVMem、NInfer、ActKV、Strata评估。关注MoE路由重放，
并评估路由观测、数值合同、异步调度、CUDA Graph与前缀状态管理。

**结论：对本项目价值最高的是受控路由诊断，以及默认MTP下可靠的路由
记录。其次是跨形状数值测试和调度/Graph生命周期。当前实现不能当作
跳过整个router的性能优化，也不是本项目GDN+MTP的现成替代。**

## 版本与范围

这里的Megatron Inference指NVIDIA/Megatron-LM中的`megatron/core/inference`，
以及它调用的`transformer/moe`和`ssm`。固定源码
[`3e2807ce5e787f81b7275be2383092fe0c3bfc71`](https://github.com/NVIDIA/Megatron-LM/commit/3e2807ce5e787f81b7275be2383092fe0c3bfc71)，
本地检出`.q4t-work/megatron-review-20261016/megatron-lm/`。
下列外部代码链接均固定到该版本，不将后续main变化混入结论。

对照本项目main `28a3783350b0c4c3d73fd3d483b5eebe881d2bdb`的运行时代码。
本轮只读关键实现、生产调用链及测试；未构建或执行Megatron，未运行
GPU、HTTP或性能测试，未修改本项目运行时、模型及reference/。
“有测试”表示读到了测试实现，不表示本机或上游本次CI已经通过。
核对文件与SHA256见[来源清单](evidence/megatron-inference-review-20261016/provenance.json)。

## 1. 路由重放真正做了什么

### 1.1 固定专家ID，使用当前分数

[RouterReplay.get_replay_topk](https://github.com/NVIDIA/Megatron-LM/blob/3e2807ce5e787f81b7275be2383092fe0c3bfc71/megatron/core/transformer/moe/router_replay.py#L219-L240)
有三个动作：

| 动作 | 真实行为 | 不包含的能力 |
|---|---|---|
| RECORD | 正常计算top-k，再记录expert IDs | 不记录完整logits、路由权重或请求身份 |
| REPLAY_FORWARD | 取外部设置的IDs，对当前scores做gather | 不冻结当前权重，不自动读取轨迹文件 |
| REPLAY_BACKWARD | 从先前设置的FIFO取IDs，再gather当前scores | 不是推理状态rollback，也不自动执行反向 |

[set_target_indices](https://github.com/NVIDIA/Megatron-LM/blob/3e2807ce5e787f81b7275be2383092fe0c3bfc71/megatron/core/transformer/moe/router_replay.py#L172-L185)
保存tensor引用并入FIFO；REPLAY_FORWARD本身不入队。普通eager RECORD
也持有tensor引用；静态记录缓冲路径才实际复制。不能把它理解为自带
不可变、完整来源绑定的持久化轨迹系统。

[InferenceTopKRouter._forward](https://github.com/NVIDIA/Megatron-LM/blob/3e2807ce5e787f81b7275be2383092fe0c3bfc71/megatron/core/transformer/moe/router.py#L1371-L1414)
仍先调用`self.gating(input)`；
[分数函数](https://github.com/NVIDIA/Megatron-LM/blob/3e2807ce5e787f81b7275be2383092fe0c3bfc71/megatron/core/transformer/moe/moe_utils.py#L916-L964)
仍按配置执行softmax/sigmoid、归一化和缩放。概念上是：

```text
当前hidden → gating logits → 当前scores → 按指定IDs取分数 → 专家计算
                                        ↑
                              外部提供的expert IDs
```

跳过的是该分支的离散top-k/组选择；没有跳过gating投影或全部router。
因此[设计文档第21行](https://github.com/NVIDIA/Megatron-LM/blob/3e2807ce5e787f81b7275be2383092fe0c3bfc71/docs/api-guide/router_replay.md#L21)关于连logits
计算也完全跳过的说法超出了实现。固定ID可以隔离“专家集合改变”这一
变量，但不能由此推出logits、权重、专家输出或最终token逐位相同。

### 1.2 不可忽略的接入边界

- [fused TE分支](https://github.com/NVIDIA/Megatron-LM/blob/3e2807ce5e787f81b7275be2383092fe0c3bfc71/megatron/core/transformer/moe/moe_utils.py#L850-L877)
  在replay wrapper之前直接返回，未向TE传入RouterReplay对象。因此
  不能只凭开启全局flag就宣称该融合路径执行了记录/重放。
- `precomputed_indices`同样绕过该wrapper；普通TopKRouter的
  [sinkhorn/QB分支](https://github.com/NVIDIA/Megatron-LM/blob/3e2807ce5e787f81b7275be2383092fe0c3bfc71/megatron/core/transformer/moe/router.py#L1069-L1103)
  不走同一个入口。训练中的capacity/token dropping仍在记录之后执行
  （[L1105–1114](https://github.com/NVIDIA/Megatron-LM/blob/3e2807ce5e787f81b7275be2383092fe0c3bfc71/megatron/core/transformer/moe/router.py#L1105-L1114)）。
  相同IDs不自动固定完整dispatch结果。
- [全局实例注册与分发](https://github.com/NVIDIA/Megatron-LM/blob/3e2807ce5e787f81b7275be2383092fe0c3bfc71/megatron/core/transformer/moe/router_replay.py#L25-L58)
  依赖router构造顺序；设置表主要检查层数，没有项目级请求、位置、模型
  身份和完整性合同。移植应采用模型/请求作用域，增加shape、ID范围、
  去重、层角色、逻辑位置、生命周期和实际分派命中的检查。

这些是被核对路径的具体限制，不扩展为所有backend都不可用。

### 1.3 本项目的直接价值：诊断，而非替换正常路由

本项目[MoEForward](https://github.com/thomas-hiddenpeak/qwen4-thor/blob/28a3783350b0c4c3d73fd3d483b5eebe881d2bdb/src/model/moe.cu#L437-L454)先算BF16 gating，
再由[RouterTopkKernel](https://github.com/thomas-hiddenpeak/qwen4-thor/blob/28a3783350b0c4c3d73fd3d483b5eebe881d2bdb/src/model/moe.cu#L132-L209)选择512个专家中的
10个并计算选中项softmax。可借鉴的最小诊断接口，是在这一离散选择点
注入固定IDs，同时保留当前logits和现行权重计算顺序。

这能把跨T=1/T=4差异分成“选了不同专家”与“相同专家下算术不同”，
也能给专家执行/驻留研究提供固定需求对照。但固定ID可能掩盖错误router，
必须与正常路由结果并列；不能让重放成为数值验收的替代基线。
本项目已有[router投影快照回放夹具](https://github.com/thomas-hiddenpeak/qwen4-thor/blob/28a3783350b0c4c3d73fd3d483b5eebe881d2bdb/tools/verify/moe_full_router/replay.cpp.in#L32-L74)，
它重放固定输入/权重/算法并比较logits，不是固定专家ID的运行时接口。
新增接口应复用现有采集身份，并保存有序slot；本项目tie-break为
分数降序、expert ID升序，不能把ID列表当无序集合排序后再回放。

如果另做IDs+权重全冻结，那是更强的人工消融，不是Megatron这个原语的
现有语义；其耗时不能当成正常推理的可实现收益。

## 2. Inference里的“replay”主要是记录与导出

[engine warmup](https://github.com/NVIDIA/Megatron-LM/blob/3e2807ce5e787f81b7275be2383092fe0c3bfc71/megatron/core/inference/engines/dynamic_engine.py#L1076-L1085)
及[实际controller forward](https://github.com/NVIDIA/Megatron-LM/blob/3e2807ce5e787f81b7275be2383092fe0c3bfc71/megatron/core/inference/text_generation_controllers/text_generation_controller.py#L2816-L2835)
设置的是`RouterReplayAction.RECORD`，且`is_mtp_layer=False`。
本轮搜索inference目录没有找到`REPLAY_FORWARD`或`set_replay_data`调用。
因此这里的生产链路是采集主模型路由、向消费者导出；没有证据表明
serving直接接收旧路由表并强制执行。MTP草稿router也不在这份记录中。

### 2.1 最值得学：异步与Graph下的缓冲所有权

[RoutingMetadata](https://github.com/NVIDIA/Megatron-LM/blob/3e2807ce5e787f81b7275be2383092fe0c3bfc71/megatron/core/inference/contexts/routing_metadata.py#L46-L102)
给async模式分配`[2, max_tokens, local_moe_layers, topk]`的int32双bank，
配GPU selector与稳定layer index。Graph录到的是实际写buffer的运算，
不会依赖Python hook在每次replay触发。

[选bank](https://github.com/NVIDIA/Megatron-LM/blob/3e2807ce5e787f81b7275be2383092fe0c3bfc71/megatron/core/inference/text_generation_controllers/text_generation_controller.py#L906-L921)
先确认CPU槽所有权已释放（消费或丢弃），再让计算stream等待该bank
上一轮D2H完成；
[复制与发布](https://github.com/NVIDIA/Megatron-LM/blob/3e2807ce5e787f81b7275be2383092fe0c3bfc71/megatron/core/inference/text_generation_controllers/text_generation_controller.py#L2053-L2147)
用独立copy stream、pinned CPU槽、forward-ready/CPU-ready事件，以及
该forward自己的token→block位置副本。后续调度重排不会改写旧记录归属。
转成CPU int16发生在D2H之后，不能声称传输字节减半。

固定版本**支持async + CUDA graph + routing记录**：
[context接线](https://github.com/NVIDIA/Megatron-LM/blob/3e2807ce5e787f81b7275be2383092fe0c3bfc71/megatron/core/inference/contexts/dynamic_context.py#L2736-L2743)
使用async OR graph条件；还有
[组合功能测试配置](https://github.com/NVIDIA/Megatron-LM/blob/3e2807ce5e787f81b7275be2383092fe0c3bfc71/tests/functional_tests/test_cases/moe/gpt_dynamic_inference_tp4_etp1_pp1_ep8_16B_routing_replay_prefix_cache_cudagraph_async_sched/model_config.yaml#L75-L91)。
[用户指南](https://github.com/NVIDIA/Megatron-LM/blob/3e2807ce5e787f81b7275be2383092fe0c3bfc71/docs/mcore-inference-user-guide.md#L564-L566)仍称互斥，
而[examples说明](https://github.com/NVIDIA/Megatron-LM/blob/3e2807ce5e787f81b7275be2383092fe0c3bfc71/examples/inference/README.md#L143)已更新；以代码为准。
这里证明的是记录组合，不是任意强制重放组合。

### 2.2 请求、MTP与prefix生命周期

- async在[接受前缀处理](https://github.com/NVIDIA/Megatron-LM/blob/3e2807ce5e787f81b7275be2383092fe0c3bfc71/megatron/core/inference/text_generation_controllers/text_generation_controller.py#L2455-L2508)
  保留prefill全部行、decode的`min(query_length, accepted_count+1)`行，先过滤拒绝后缀再
  发布。legacy则在[verify判断之前](https://github.com/NVIDIA/Megatron-LM/blob/3e2807ce5e787f81b7275be2383092fe0c3bfc71/megatron/core/inference/text_generation_controllers/text_generation_controller.py#L3446-L3496)
  发布整段路由；不能把两者的逐步trace都称为“仅接受token”。
- chunked prefill使用已完成chunk的token数加prefix跳过量得到绝对逻辑位置
  （[位置计算](https://github.com/NVIDIA/Megatron-LM/blob/3e2807ce5e787f81b7275be2383092fe0c3bfc71/megatron/core/inference/contexts/dynamic_context.py#L3414-L3449)）；
  共享命中块发生冗余重算时，[映射到dummy block](https://github.com/NVIDIA/Megatron-LM/blob/3e2807ce5e787f81b7275be2383092fe0c3bfc71/megatron/core/inference/contexts/dynamic_context.py#L3593-L3605)
  并跳过存储，保留原块历史。[分配器](https://github.com/NVIDIA/Megatron-LM/blob/3e2807ce5e787f81b7275be2383092fe0c3bfc71/megatron/core/inference/contexts/kv_block_allocator.py#L644-L756)
  按block/位置保存；释放保留历史、[重新分配清除](https://github.com/NVIDIA/Megatron-LM/blob/3e2807ce5e787f81b7275be2383092fe0c3bfc71/megatron/core/inference/contexts/kv_block_allocator.py#L220-L222)，避免新请求继承旧内容。
- 完成请求在块复用前重建路由；[长度是prompt+generated−1](https://github.com/NVIDIA/Megatron-LM/blob/3e2807ce5e787f81b7275be2383092fe0c3bfc71/megatron/core/inference/engines/dynamic_engine.py#L2796-L2805)，
  因为最后输出token尚未经过主模型forward。
  [HTTP响应](https://github.com/NVIDIA/Megatron-LM/blob/3e2807ce5e787f81b7275be2383092fe0c3bfc71/megatron/core/inference/text_generation_server/dynamic_text_gen_server/endpoints/completions.py#L464-L472)
  会导出`moe_topk_indices`等字段，并非只存在内部调试对象。

局限：块数组以0初始化，重建检查缺失block，没有逐位置有效位。
诊断证据若照搬该格式，不能把合法expert 0当作“该位置确实记录过”的证明。

### 2.3 对本项目：需要两种路由视图

本项目[正式采集入口](https://github.com/thomas-hiddenpeak/qwen4-thor/blob/28a3783350b0c4c3d73fd3d483b5eebe881d2bdb/src/server/chat_server.cpp#L390-L409)目前要求
单流、MTP关闭；[Collector](https://github.com/thomas-hiddenpeak/qwen4-thor/blob/28a3783350b0c4c3d73fd3d483b5eebe881d2bdb/src/trace/router_collector.cpp#L230-L287)
在forward结束按现有同步点发布，已经区分提交、GPU完成与状态commit。
Megatron最直接的新参考价值，是把这种可靠记录扩展到默认MTP和未来Graph。

应同时保留：

| 视图 | 包含内容 | 用途 |
|---|---|---|
| 实际计算轨迹 | 主模型verify全部行、拒绝草稿、draft/extend，各自明确layer角色 | 专家加载、缓存miss、实际字节和性能成本分析 |
| 已提交序列路由 | 按请求/逻辑位置保留真正接受的前缀 | 数值复现、会话状态、后续训练消费 |

拒绝token虽然未提交，但已经消耗专家计算和潜在加载。只拿Megatron
async过滤后的主模型记录做缓存回放，会漏掉这部分成本及draft本身。
这点不能用最终输出长度补算。当前[trace格式](https://github.com/thomas-hiddenpeak/qwen4-thor/blob/28a3783350b0c4c3d73fd3d483b5eebe881d2bdb/include/q4t/trace/router_trace.h#L27-L65)
只有prefill/decode两阶段，扩展时还需显式增加角色、接受掩码和slot世代。

## 3. 还有哪些值得学习

### 3.1 Batch-invariant MoE：完整算术合同与负控测试

[生产fused MoE](https://github.com/NVIDIA/Megatron-LM/blob/3e2807ce5e787f81b7275be2383092fe0c3bfc71/megatron/core/inference/moe/fused_moe.py#L158-L280)
的invariant分支包含permutation、GEMM、激活/概率舍入及combine约束。
[激活实现](https://github.com/NVIDIA/Megatron-LM/blob/3e2807ce5e787f81b7275be2383092fe0c3bfc71/megatron/core/inference/moe/batch_invariant.py#L118-L160)
把router概率放在FC2之前，并固定FP32计算后转BF16的边界；
[token-local combine](https://github.com/NVIDIA/Megatron-LM/blob/3e2807ce5e787f81b7275be2383092fe0c3bfc71/megatron/core/inference/moe/batch_invariant.py#L418-L477)
按expert顺序FP64累加再转FP32。不能只复制“固定top-k”就宣称batch不变。

值得借鉴的测试是加入[1e20的不相关token](https://github.com/NVIDIA/Megatron-LM/blob/3e2807ce5e787f81b7275be2383092fe0c3bfc71/tests/unit_tests/inference/test_moe_permute.py#L562-L604)，
检查目标token不受跨token累加污染；以及使用
[实际形状和log-normal异常值](https://github.com/NVIDIA/Megatron-LM/blob/3e2807ce5e787f81b7275be2383092fe0c3bfc71/tests/unit_tests/inference/test_moe_dispatching_and_routing.py#L747-L938)
逐位比较training/inference，避免小随机矩阵漏掉BF16舍入差异。

本项目[普通grouped combine](https://github.com/thomas-hiddenpeak/qwen4-thor/blob/28a3783350b0c4c3d73fd3d483b5eebe881d2bdb/src/quant/moe_gemm.cu#L261-L285)和
[decode combine](https://github.com/thomas-hiddenpeak/qwen4-thor/blob/28a3783350b0c4c3d73fd3d483b5eebe881d2bdb/src/quant/moe_decode.cu#L165-L176)已经固定slot顺序，
新增价值是跨形状、量化和概率乘法位置的审查与测试，不能重复建议替换
早已移除的atomic combine。将概率移到NVFP4 FC2之前会改变本项目舍入，
不是透明等价移植；本轮不调整已接受的N=4近tie合同。

硬件边界很具体：[TE device-metadata grouped GEMM白名单](https://github.com/NVIDIA/Megatron-LM/blob/3e2807ce5e787f81b7275be2383092fe0c3bfc71/megatron/core/transformer/custom_layers/batch_invariant_kernels.py#L1671-L1694)
要求SM100与cuBLASLt 13.5.1，Thor SM110a不满足此gate。其他backend另论，
不能把这项确定性保证套到Thor。局部MoE测试也不证明任意整模型恒等。

### 3.2 Async调度：先提交下一轮，再结算上一轮

[普通decode生产路径](https://github.com/NVIDIA/Megatron-LM/blob/3e2807ce5e787f81b7275be2383092fe0c3bfc71/megatron/core/inference/text_generation_controllers/text_generation_controller.py#L3218-L3406)
将GPU采样token直接接入下一轮forward，之后再等待事件、在CPU处理上一轮
完成/停止/压缩；[metadata发布](https://github.com/NVIDIA/Megatron-LM/blob/3e2807ce5e787f81b7275be2383092fe0c3bfc71/megatron/core/inference/text_generation_controllers/text_generation_controller.py#L2767-L2794)
显式跳过input IDs，避免CPU旧副本覆盖GPU刚采样的输入。

这对本项目[host接受与恢复选择](https://github.com/thomas-hiddenpeak/qwen4-thor/blob/28a3783350b0c4c3d73fd3d483b5eebe881d2bdb/src/mtp/mtp.cu#L1523-L1596)有设计参考，
但Megatron本身也仍在[accepted-count回读后由CPU rewind](https://github.com/NVIDIA/Megatron-LM/blob/3e2807ce5e787f81b7275be2383092fe0c3bfc71/megatron/core/inference/text_generation_controllers/text_generation_controller.py#L2493-L2508)。
它不是整轮零host；[资源/暂停等边界](https://github.com/NVIDIA/Megatron-LM/blob/3e2807ce5e787f81b7275be2383092fe0c3bfc71/megatron/core/inference/engines/dynamic_engine.py#L3502-L3522)
会回到非重叠流程。值得学的是pending所有权和安全结算点，收益仍须本机HTTP验证。

### 3.3 CUDA Graph：缓存身份包含语义和存储地址

[捕获前预留最坏容量](https://github.com/NVIDIA/Megatron-LM/blob/3e2807ce5e787f81b7275be2383092fe0c3bfc71/megatron/core/inference/engines/dynamic_engine.py#L1030-L1043)
防止后续grow重分配后graph引用旧地址；
[MTP graph](https://github.com/NVIDIA/Megatron-LM/blob/3e2807ce5e787f81b7275be2383092fe0c3bfc71/megatron/core/inference/engines/dynamic_engine.py#L1093-L1152)
区分cache-free/KV-aware及depth，初始化dummy token、使用scratch KV，
并对[未捕获shape明确eager fallback](https://github.com/NVIDIA/Megatron-LM/blob/3e2807ce5e787f81b7275be2383092fe0c3bfc71/megatron/core/inference/text_generation_controllers/mtp_controller_mixin.py#L565-L627)。

[Graph bucket的64对齐](https://github.com/NVIDIA/Megatron-LM/blob/3e2807ce5e787f81b7275be2383092fe0c3bfc71/megatron/core/inference/batch_dimensions_utils.py#L216-L228)
服务于其TE/invariant算术合同，不能照搬为本项目最优bucket。
本项目未来graph方案应覆盖scratch/checkpoint扩容、PLE staging所有权、
取消后复用和shape切换，不能只测试稳定形状的生成文本。

### 3.4 Hybrid prefix：KV命中和可跳过计算是两件事

[prefix匹配](https://github.com/NVIDIA/Megatron-LM/blob/3e2807ce5e787f81b7275be2383092fe0c3bfc71/megatron/core/inference/contexts/dynamic_context.py#L3156-L3295)
还要寻找正确recurrent checkpoint；续chunk即使KV命中，也可能需要执行
recurrent transition。[MTP块尾处理](https://github.com/NVIDIA/Megatron-LM/blob/3e2807ce5e787f81b7275be2383092fe0c3bfc71/megatron/core/inference/contexts/mtp_context_mixin.py#L150-L200)
识别draft KV依赖下一token、但下一token不属于当前块hash的问题；分叉时
放弃边界块命中并私有重算，不能原地修改共享块。

这是未来会话缓存的高价值状态合同。不过固定版本
[GDN动态推理](https://github.com/NVIDIA/Megatron-LM/blob/3e2807ce5e787f81b7275be2383092fe0c3bfc71/megatron/core/ssm/gated_delta_net/gdn.py#L338-L360)
明确只支持FLA，拒绝speculative decoding、prefix caching与batch-invariant；
[decode](https://github.com/NVIDIA/Megatron-LM/blob/3e2807ce5e787f81b7275be2383092fe0c3bfc71/megatron/core/ssm/gated_delta_net/gdn.py#L401-L419)只接受单token，
不捕获中间conv/SSM状态。Mamba/GDP支持不能扩大为GDN支持，GDP也不是GDN。

Megatron也支持[重复同一MTP层](https://github.com/NVIDIA/Megatron-LM/blob/3e2807ce5e787f81b7275be2383092fe0c3bfc71/megatron/core/inference/text_generation_controllers/text_generation_controller.py#L349-L360)：
此模式把有效depth数设为speculative token数，满足async的逐depth要求。
本项目[重复同一draft层产生k3草稿](https://github.com/thomas-hiddenpeak/qwen4-thor/blob/28a3783350b0c4c3d73fd3d483b5eebe881d2bdb/src/mtp/mtp.cu#L1440-L1457)本身并非兼容性障碍；
真正限制来自上述GDN组合以及未接入的QSA/PLE状态。这些实现不能推翻此前
[GDN候选NO_GO](GDN_RECORD_FOLD_2026-10-16.md)。

## 4. 路由分析脚本：可借方法，不照搬结论

[concentration](https://github.com/NVIDIA/Megatron-LM/blob/3e2807ce5e787f81b7275be2383092fe0c3bfc71/tools/moe_routing/analyze_routing_concentration.py#L104-L143)
用“同step至少两层、token数≤64”近似识别decode；
[top-N覆盖](https://github.com/NVIDIA/Megatron-LM/blob/3e2807ce5e787f81b7275be2383092fe0c3bfc71/tools/moe_routing/analyze_routing_concentration.py#L51-L67)
在同批数据上排序并计覆盖。它适合描述分布，不是跨请求留出验证，也没有
本项目缓存回放的整组保护、真实字节、冷启动和容量合同。

[predictability](https://github.com/NVIDIA/Megatron-LM/blob/3e2807ce5e787f81b7275be2383092fe0c3bfc71/tools/moe_routing/analyze_routing_predictability.py#L207-L229)
将下一被记录MoE层的router权重应用到前一记录层hidden，再聚合专家计数，比较cosine、
Spearman和hot-set重合。两个token预测专家刚好互换，聚合计数仍可完全相同，
因此这些指标不能证明逐token预取命中率、提前量或加载收益。

[简化router计算](https://github.com/NVIDIA/Megatron-LM/blob/3e2807ce5e787f81b7275be2383092fe0c3bfc71/tools/moe_routing/analyze_routing_predictability.py#L77-L93)
不是全部生产路由语义；[数据索引](https://github.com/NVIDIA/Megatron-LM/blob/3e2807ce5e787f81b7275be2383092fe0c3bfc71/tools/moe_routing/analyze_routing_predictability.py#L43-L71)
还按裸layer号归并，混入decoder/MTP同号层时可能覆盖记录。
[hook的logit sidecar](https://github.com/NVIDIA/Megatron-LM/blob/3e2807ce5e787f81b7275be2383092fe0c3bfc71/megatron/core/transformer/moe/router_trace.py#L383-L403)
重新调用gating并存BF16，也不能冒充未经改动的原始FP32 logits。

本项目已有[按需求组计费的固定容量回放](https://github.com/thomas-hiddenpeak/qwen4-thor/blob/28a3783350b0c4c3d73fd3d483b5eebe881d2bdb/tools/trace/replay.py#L40-L82)及
后续offload研究工具，基础缓存分析并不缺这一层。Megatron新增的有用
探索是跨层hidden预测，但必须保留本项目明确阶段、层角色、留出输入、
整组miss和字节/时序计费，并单独估算预测投影与无用预取成本。

## 5. 测试证据边界与采用顺序

| 已读测试 | 能支持的设计判断 | 不能据此声称 |
|---|---|---|
| [router replay单测](https://github.com/NVIDIA/Megatron-LM/blob/3e2807ce5e787f81b7275be2383092fe0c3bfc71/tests/unit_tests/transformer/moe/test_router_replay.py)及[hash/graph测试](https://github.com/NVIDIA/Megatron-LM/blob/3e2807ce5e787f81b7275be2383092fe0c3bfc71/tests/unit_tests/transformer/moe/test_routers.py#L1698-L1820) | RECORD/固定IDs/当前scores/FIFO及部分graph记录合同 | Thor整模型或所有router backend已通过 |
| [routing metadata单测](https://github.com/NVIDIA/Megatron-LM/blob/3e2807ce5e787f81b7275be2383092fe0c3bfc71/tests/unit_tests/inference/contexts/test_routing_metadata.py#L144-L225) | real router下双bank、padding、MTP排除、graph写入 | 低开销或零丢失的全部线上场景 |
| [prefix allocator测试](https://github.com/NVIDIA/Megatron-LM/blob/3e2807ce5e787f81b7275be2383092fe0c3bfc71/tests/unit_tests/inference/contexts/test_dynamic_prefix_caching.py#L2077-L2384) | 保存位置、释放保留/重分配清除、prefix历史 | 本项目PLE/QSA状态兼容 |
| [完成与块复用测试](https://github.com/NVIDIA/Megatron-LM/blob/3e2807ce5e787f81b7275be2383092fe0c3bfc71/tests/unit_tests/inference/engines/test_request_finalization.py#L92-L176) | 完成时先重建、随后块复用及截断长度 | endpoint mock等于真实模型E2E |
| [async调度测试](https://github.com/NVIDIA/Megatron-LM/blob/3e2807ce5e787f81b7275be2383092fe0c3bfc71/tests/unit_tests/inference/engines/test_dynamic_engine_async_sched.py#L2110-L2157) | 非单调survivor顺序下元数据同步压缩 | 所有MTP/GDN组合支持 |

建议按以下优先级使用参考；这是评估建议，本轮没有启动实现或新试验：

| 优先级 | 可收窄的下一项工作 | 主要价值 / 成本 |
|---|---|---|
| P0 | 现有MoE数值工具统一固定IDs接口与身份校验 | 高诊断价值；不进入默认正常路由 |
| P1 | 默认MTP下双视图trace，明确拒绝后缀和draft成本 | 补现有观测盲区；需事件、槽位、取消/异常合同 |
| P1 | 采用异常值、跨shape、padding与不相关token负控 | 提高数值诊断质量；保留本项目算式和N=4合同 |
| P2 | 有界decode子流程的GPU metadata / Graph候选 | 可能减少host等待；先确认可消除的实际等待，再独立验收 |
| 未来 | prefix全状态一致性、跨层预测预取 | 前者属于新会话能力，后者需有时序与成本证据 |

多卡TP/EP/NVLS和BF16/MXFP8后端不是单Thor NVFP4引擎的直接收益来源。
本轮未在核对的`megatron/`和`examples/`源码中找到本项目qwen4_exp或
SM110专用接入，不据此断言所有依赖都无法在Thor运行。
根[LICENSE](https://github.com/NVIDIA/Megatron-LM/blob/3e2807ce5e787f81b7275be2383092fe0c3bfc71/LICENSE)给出默认BSD三条款及例外清单，部分文件另有标注；
本轮未复制第三方实现、未新增运行时依赖。

交付为本报告、参考索引、状态和日志；仅检查来源/引用与文档一致性，
生产运行时与既有性能/数值结论不变。后续如实施候选，仍按EVALUATION
冻结范围和出口；涉及GPU的完整阶段统一经共享GPU lease执行。
