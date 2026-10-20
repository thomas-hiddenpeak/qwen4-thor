# 加载期异步源与调度交接审计

审计起点5047202；加固候选0494f21（binary bc348458）。
结论是源生命周期合同存在缺口并已修改；不声称在实际模型权重上
捕获过本次缺口造成的数据损坏。GPU/HTTP终态另见阶段结果。

## 判定依据

[CUDA 13.3同步语义](https://docs.nvidia.com/cuda/archive/13.3.0/cuda-runtime-api/api-sync-behavior.html)
对pageable内存的异步传输只说可能对host同步，不能用作源可立即
销毁或覆盖的普遍保证。源码中函数尾等待不能保护已退出的内部lambda
源；线程join后的等待也不能保护已销毁或被下一expert覆盖的worker源。
已有Thor正常运行不是推翻这一合同缺口的依据。

## 有界审计结果与处理

| 路径 | 原完成边界 | 本次处理 |
|---|---|---|
| HC、linear/full attention、MoE extra、PLE、head、MTP dense、vision的8个load lambda | 局部vector在lambda返回时销毁，外层才等待或依赖后续CUDA调用 | 统一LoadTensorToDevice：校验准确byte_size、owned source、同stream复制并检查完成 |
| MTP BF16 gate_up/down | 上传gate_up后立即resize/覆盖同vector，末尾只等待非null stream | 分别使用owned tensor加载，两个张量都完成后才返回 |
| NVFP4并行expert加载 | worker重用向量和栈scalar；join后才同步且失败提前返回 | typed worker storage拥有所有向量/scalar；每expert后Drain再复用；提前返回析构Drain；完成失败保留源 |
| WeightLoader/Safetensors ReadTensorToDevice借用接口 | 调用方拥有源，接口没有说明 | 保留接口语义，补明确源/目标完成前存活合同；当前生产无直接调用点 |
| 推理MetadataUploadBatch/DecodeMetadataUpload | 源快照和事件owner | 原有实现及证据复用，不改推理上传策略 |
| ModelVerifyMulti | 函数scope向量+所有提交后返回均finish等待 | 已覆盖正常Status返回路径，保留 |
| MtpSpeculativeStepMulti | step scope向量+cleanup等待，再发布结果 | 保留；GPU目标scratch仍受model_mu_串行保护 |
| PLE SSD staging | 持久源与已有事件完成保护 | 不改动，复用已修复所有权证据 |

该清单不覆盖新增offload策略、设备故障恢复或任意C++异常恢复。
PendingHostStorage只保护其host源，不拥有GPU目标或CUDA stream；
GPU目标仍由各loader/model owner管理。同步失败且析构仍不能完成时
故意保留host源，防止在途释放，不宣称失败context可以继续安全服务。

## 成本与行为边界

- dense源直接ReadTensor到owned vector，无新增整tensor快照复制；
  每tensor增加明确完成等待。使用原stream，默认stream也覆盖。
- NVFP4保持原线程数、读取布局、9次/每expert上传粒度和数据类型，
  每expert增加完成边界；worker仍并行读盘，shared stream等待可能
  牵连其他worker已排队上传，需如实记录启动成本。
- 不改推理kernel、路由/概率算术、cache布局、调度策略、默认MTP。
  这不是提速任务，不以启动一次快慢或HTTP小幅波动宣称优化。
- 输入tensor长度不符现在在读取/上传前拒绝，防止目标尺寸与文件
  payload不一致；合法模型应保持相同字节。

## 验证设计

7组host延迟读取及错误注入，覆盖默认/非默认stream、非法尺寸/名称/
目标拒绝、可能已提交的copy失败、同步失败析构重试、源复用/提前返回、
持续失败保留源。真实GPU再验证默认/非阻塞stream各8字节上传。
MoE固定层全部原始输出与首组父版对照；HTTP普通/MTP各11题。
失败保留不自动重试；直接合同不冒充完整视觉任务质量。


## 范围外待继续审查的入口

本次冻结范围是加载期以及上表已有推理owner/调度交接，不代表全仓
所有异步host源都已闭合。静态浏览还看到普通MoERoutedForward的
局部offset_h，以及residency分块的rw_sub等源；其作用域内未见直接
源完成保护，需要结合实际分派、调用方同步及失败路径单独确认并
处理。它们不属于本次加载期接入，未修改、未以本次测试宣称安全。
已有offload研究/接受边界仍按原记录；后续应另冻有界清单，不在
当前验收运行中临时修改热路径或扩大为新性能任务。
