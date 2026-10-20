# 元数据加固后续交付结果

本轮约定的三项工作完成：部署原已接受版本；对剩余推理元数据入口
实施最小所有权加固并完成对应验证；交付零新增性能采样的方法审查。
这不表示新的入口加固已取得性能接受或已部署。

## 当前默认与新源码分开记录

默认build/q4t为已获用户接受的c8f4b241，对应3d5564e；部署时普通
和默认MTP各1条短健康请求通过，临时服务正常关停。旧44abd8b1、
sidecar和真实缓存保持可回退，收尾再次核对摘要一致；未执行回退
演练，也没有常驻HTTP服务。见[部署及回退](INFERENCE_METADATA_FOLLOWUP_2026-10-20.md)。

新增入口源码b6870c0已在main，新候选binary
753d9769f96e9848800eae88b4800bef65e20885fd2ec025b8f3bc91fc497542
位于.q4t-work/metadata-followup-20261020/build/q4t，未替换默认。
后续工具/文档提交未改变src/include；候选完成本轮数值/布局、HTTP
质量与普通模式取消合同验证。状态VALIDATED_RUNTIME_CANDIDATE，
不是ENGINEERING_ACCEPTED或统计性能非劣结论；旧加固的用户接受
不自动扩展为新候选部署批准。

## 实施与验证

MetadataUploadBatch持有可复用的pageable快照块，Begin先完成前批
上传再重填，Copy保有独立来源，Finish记录最后一批完成事件。未
Finish早退仍由owner持有来源，下一次Begin或析构排空；提交/记录/
完成失败禁止继续重填，无法确认完成时保留源，不能宣称设备可恢复。

主模型RunPrefill、ModelDecodeStep、ModelDecodeBatch、
ModelPrefillBatch和ModelMixedBatch接入同一个模型自有批次对象；
MTP chunked extend由draft独立对象保活ids/positions/seqid。原
GPU目标、计算形状和拷贝粒度保留，没有全面转pinned、合并kernel
或改变调度。另补入口seq_id/token容量及ragged/Mixed溢出拒绝。
这些验证只证明测试覆盖的合同，不证明所有输入或设备故障情形。

| 要求 | 已核对的证据 |
|---|---|
| 事前分组范围与预算 | [对象和接入卡](METADATA_UPLOAD_BATCH_2026-10-20.md)、[多路径卡](METADATA_BATCH_REPLAY_2026-10-20.md)、[HTTP卡](METADATA_BATCH_HTTP_2026-10-20.md) |
| 源生命周期与失败保护 | 10组无cudart host合同，加2项new/new[]失败补验；已提交源排空及失败后禁止重填 |
| 真实GPU上传 | 4批次/8次H2D、64个int，含早退无Finish、跨stream源复用、析构完成；不证明GPU消费者并发安全 |
| API边界 | 19次真实入口检查，无模型/GPU工作；包括seq_id、容量、INT_MAX累计和位置范围拒绝 |
| 主模型与MTP接入 | 精确父48185ed/候选b6870c0同夹具78文件逐位一致，独立元数据断言通过；涵盖固定4层、真实draft层、多模态位置和拒绝后复用 |
| HTTP质量 | 普通/MTP各11/11，文本、输入长度、终止/响应合同符合固定参考；on启动日志确认effective mtp=1及k=3 |
| 取消与服务退出 | 普通模式8组取消/deadline/FIN/RST/ID复用/关停，failure=null；3服务exit0且清理成功 |
| 环境及证据 | GPU全阶段各持完整NVML lease；HTTP约605秒、556查询全部OK、guard COMPLETED；关停后162文件封存核验；差分174文件也已核验 |
| 性能方法与预算 | [方法审查](INFERENCE_METADATA_PERFORMANCE_METHOD_2026-10-20.md)说明跨启动重复、漂移、非劣界限与区间方法缺口；新性能样本0，未设容差或追加校准 |
| 交付与回退 | 阶段提交推送main，STATUS/当天日志更新；默认与回退身份一致，新源码候选保留未部署 |

HTTP详细结果见[审计](evidence/metadata-followup-20261020/batch-http-v1/audit.json)，
差分见[78项比较](evidence/metadata-followup-20261020/batch-replay-v1/comparison.json)。
原始文件保留在.q4t-work/metadata-followup-20261020，各阶段manifest
互不覆盖。首次EOS窄化和组合mock main返回值的构建失败保留；修正
夹具后只重建受影响项，没有失败模型结果被删除或用额外采样替换。

## 仍保留的边界

- 本轮没有新性能接受结论。pageable快照复制、容量保留和事件等待
  都有成本；没有用旧c8f4b241的五档结果为753d9769宣称持平。
- MTP multi-step和ModelVerifyMulti的既有scope/finish同步协议保留；
  不能把某次同步成功泛化为设备故障后仍可恢复或目标GPU内存安全。
- LoadMtp加载期临时权重的pageable依赖不属本轮推理元数据；
  DecoderLayer::ResetState仍为void，未声称修复底层复位错误传播。
- 人工视觉特征只检查位置布局和注入后同路径数值，不是视觉塔或
  多模态语义质量验收。HTTP取消矩阵为普通模式，不冒充MTP取消矩阵。

后续若要部署753d9769或声明性能保持，需要针对这个新身份明确接受
路线及所需证据；不是本轮偷偷替换默认入口。若要求统计非劣，先
确定实际损失界限与有依据的有界设计，有限测量不能证明严格零退化。
