# MoE热路径异步host存储审计

状态：冻结范围审计、确认缺陷修复及有界验收完成。基线63d090a，默认bc348458保持。
用户授权审计，确认缺陷后修复，按可靠性任务有界验收。

## 冻结范围与判定

- 普通MoERoutedForward：offset_h正常返回/错误返回没有host完成边界；
  cudaFreeAsync仅保护device routing。调用方后续同步晚于局部源销毁。
  M=1及2..8固定形状设备路径绕过此分支，普通prefill仍可到达。
- 驻留分块：toks、sub.slots、rw_sub上传后，singleton设备路径无host
  同步；最后一块没有下一轮加载等待可借用，错误出口也可提前销毁。
  分组路径的counts同步只能偶然覆盖部分情形，不是全出口保证。
- 紧邻D2H：counts_h及ids_h/rw_h正常路径已有等待，copy或wait失败
  返回仍可能销毁在途目标。本次纳入同一局部所有权范围。

判定依据为[CUDA13.3同步语义](https://docs.nvidia.com/cuda/archive/13.3.0/cuda-runtime-api/api-sync-behavior.html)：
pageable异步传输可能同步，不保证所有调用都同步。这是确认的API
生命周期合同缺陷，不声称已捕获实际模型数据损坏。

## 最小修复及成本

offset与D2H复用PendingHostStorage；offset复制后立即检查完成，
避免等待后续GEMM，增加每次host分组调用一次stream等待。D2H保留
原正常等待数，失败出口析构再等待，持续失败保留host存储。
驻留三类H2D使用既有MetadataUploadBatch快照与完成事件，保持原
复制/计算顺序，每块显式Drain上传事件，错误出口同样保护快照。
增加快照、event开销；不改变kernel、路由算术、分块或加载策略。
不处理任意设备故障恢复，不宣称所有仓库异步入口已闭合。

## 有界验收

一次零警告候选构建；现有host存储延迟复制/失败注入、batch合同
复用并运行受影响工具。真实Thor同一GPU lease内运行普通MoE独立
CPU参考测试、驻留bitexact及partition数值合同，再运行普通/MTP
HTTP各11条质量请求，记录启动/请求原值但不主张性能改善。
总HTTP22请求、2整模型加载，无性能矩阵、无自动重试；明确失败
才定向修复并重验受影响项。CPU测试也不在GPU lease外并行运行。
默认部署不在本次范围；证据身份固定后提交推送。

## 最终结果与工程决定

候选binary：
`d4232b63e53bece8c82bd87d8dcac628fb089f4f7eddc163535030133107f7a6`。
测试时基线commit为63d090a，实际修复由源码快照、sources.json和
source.patch明确；验收后仅将owner头文件一处85列日志语句换行，
空白分词完全一致，执行逻辑不变，保存post-validation-format证据。
构建零警告。未改kernel、路由算法、计算形状或默认服务行为。

| 检查 | 结果 |
|---|---|
| D2H延迟写/失败目标所有权 | 8组通过，默认及非默认模拟stream；失败重试与持续失败保留 |
| 已有H2D批次合同/加载源合同 | 10组及7组通过，包含延迟读取/提前退出/失败保留 |
| 必跑host清单 | 51/51通过，无跳过 |
| 普通分组MoE独立CPU参考 | M=4、k=3通过既定容差，max_abs=0.0063768；不将近似参考称逐位一致 |
| 驻留bitexact | C=64，T=32多块及T=1均BF16逐位一致 |
| 分块运行时合同 | C=256/T=1024、T=1及C=15/T=4混合singleton均通过；路由FP32差异0、BF16一致 |
| HTTP质量 | 普通/MTP各11/11，覆盖1024至204800-token夹具，输出文本和长度一致，两服务exit0 |
| 监控与封存 | 单一NVML lease约528.07秒，guard COMPLETED/child0；281文件核验，无遗留服务 |

[审计JSON](evidence/moe-hot-upload-20261020/audit.json)、
[计时原值](evidence/moe-hot-upload-20261020/timing-observations.json)、
[执行工具与复核方式](../tools/verify/inference_metadata/HOT_SOURCE_README.md)。
本轮无失败重试，无额外性能矩阵；全部测试一次通过。

与历史同夹具单次运行相比，普通TTFT观察变化−0.2704%～+0.4556%，
MTP为−1.1574%～+0.4401%。普通startup_total 29.841秒，MTP
30.343秒。不同启动/缓存环境且非配对设计，这些仅为有界测量观察，
不是性能非劣、优化收益或真实损失上界。按照用户非性能任务原则，
正确性与适用质量满足，接受本次可靠性修复，不为小幅波动继续补测。

交付为已验收候选及源码，默认程序仍bc348458，未部署新候选。
驻留检查是层级数值合同，不替代完整offload服务质量验收；未扩大
为MTP取消矩阵、视觉语义或设备故障恢复。本次仅闭合冻结的热路径
host存储清单，后续新入口仍需独立审查。
