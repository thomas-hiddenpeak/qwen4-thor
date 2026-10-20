# 元数据加固后续交付

用户授权按建议推进并设置新目标。复用main，默认模型目录与reference
只读，不扩展Graph、offload或调度。先部署已接受身份，再分组处理
其他上传源；性能方法审查先离线，不自动启动性能采样。

## 阶段一：默认入口部署（执行前冻结）

来源为恢复验收封存的candidate-build/q4t，SHA c8f4b241f67b2818e92791f688082e338f6981a5db923cf09fb07e30b99e887d，
源码3d5564e，用户接受记录92a42f9。旧默认44abd8b1及release sidecar
逐字备份，保留真实构建缓存；不重新编译，不启动常驻服务。

完整驱动在单NVML gpu_exclusive lease内：核对身份、打包、替换默认
binary及sidecar，然后用既有run_acceptance做两个短健康检查：
默认MTP（不传MTP开关）与显式--no-mtp各1次加载、1条固定1024-token
质量题，输出上限32，复用对应模式已验收输入与参考文本。使用原
max_seq1/max_prefill8192/max_len208896与端口8740，加载上限300秒，
每组驱动上限600秒，无重试，总2请求/2加载。检查参考文本、输入
长度、SSE终止与正常关停；不把此子集标为完整质量或性能重验。

任何身份、健康或关停失败停止，保存记录并恢复旧默认pair；监控
失败/竞争按ENV_ABORT，只清理自己的进程。旧binary、sidecar、缓存
与候选封存目录保留，显式回退脚本校验当前和备份身份，不覆盖证据。
成功后退出临时服务，默认入口就绪不等于常驻HTTP服务上线。

产物.q4t-work/metadata-followup-20261020/deploy-v1，发布包在
.q4t-work/releases/metadata-20261020。关停和guard终态后封存；更新
STATUS/日志并提交推送。必要工具检查不运行GPU或新性能测试。

## 阶段二：其他入口

先从既有审计定位真实scope与调用者依赖，区分RunPrefill、单步decode、
batch/mixed及MTP chunk源。不将其批量改成pinned；每组先列所有权、
完成、错误退出合同与最小改动理由，再冻结直接测试和HTTP范围。
当前此阶段新增模型/GPU预算0，具体实现卡确定后再执行。

## 阶段三：性能方法

先审查已有跨服务启动信息能支持什么推断、不可辨识的漂移/顺序因素，
评估交错或随机化启动、独立重复单元、逐档保护与多指标结论。不能
由三样本极差设容差；严格零退化无法由有限测量证明。非劣界限涉及
用户允许的实际损失，不能擅设。无依据或无授权则交付方法限制与
可执行条件，不自动采样；性能样本预算目前0。

### 第二阶段首次源码核对

当前源码复核model.cu的RunPrefill、ModelDecodeStep、ModelDecodeBatch、
ModelPrefillBatch/ModelMixedBatch及mtp.cu chunked MtpDraftExtend。
没有把已接受multi owner的事件当作所有scratch的通用完成事件。

| 分组 | 具体源寿命缺口 | 下一步设计约束 |
|---|---|---|
| 单步与单序列batch | token/position、局部scope的rp/rope_pos；batch RoPE现为3次连续H2D | 优先明确同一组源的持有与完成；不能直接转调multi而改变kernel映射，不能退回每token三次拷贝 |
| RunPrefill | rope_pos和mm_pos在后续kernel前离开局部scope，input_ids为调用方来源 | 保留完整三轴多模态布局、ids/position/RoPE及image position有效长度；独立保护失败路径 |
| ragged prefill/mixed | descriptors局部vector及循环中resize/改写的rope_row | 每序列RoPE源不得提前重用；保留ragged offset与token-local合同，不改变分组调度 |
| MTP chunked extend | 每chunk的seqid在MtpForward前离开scope；外层cleanup不能延长此scope | 独立处理draft对象，不能借主模型owner保活；保留非chunk同步路径及chunk目标地址 |

单步复核另见ModelDecodeStep缺少seq_id边界检查，在读取rope_delta
前即使用它。下一组实现前应确认调用方可达性和API合同，区分真实
边界缺口与预防性寿命加固；不在当前部署中改动已测身份。

设计必须同时处理正常重用、提交中途失败和完成无法确认时的源
保留。仅把变量移至函数顶层只能延长到return；在异步返回合同下
仍不充分。简单在每个小H2D后同步虽能保活，却可能增加全流等待，
不能未经评估当作无代价替代。具体方案、验证卡尚待下一阶段冻结。

## 阶段一结果

DEPLOYED_HEALTH_CHECKED。默认build/q4t为c8f4b241，release sidecar
引用发布包中的真实CMakeCache；旧44abd8b1及sidecar完整保留。
两次加载、默认MTP与普通模式各1条请求通过文本/长度/终止检查，
两服务正常退出。完整NVML guard COMPLETED/exit0，83文件关停后
封存复核通过。没有常驻服务、新五档采样或统计非劣结论。
见[部署证据](evidence/metadata-followup-20261020/deploy-v1/deployment.json)。

默认入口后续启动即可使用新版。需要回退时，先停止使用该入口的
服务，再通过新lease执行以下驱动（不覆盖本轮现场）：

```bash
python3 tools/evalscope/gpu_exclusive.py \
  --output .q4t-work/metadata-followup-20261020/rollback-lease-v1 -- \
  python3 -B tools/verify/inference_metadata/rollback_accepted.py
```

回退驱动校验当前binary和原pair及构建缓存身份；本轮未实际执行
回退，不把静态审查当回退演练。部署失败恢复分支本轮未触发。
性能方法审查见[方法边界](INFERENCE_METADATA_PERFORMANCE_METHOD_2026-10-20.md)。
其余入口仍待设计实施与验证，后续目标保持active，不以部署完成
替代全部目标完成。
