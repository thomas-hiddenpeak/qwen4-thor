# 原生时间戳、字幕和VAD默认交付 rev286

默认包`93f00500c4c985c24ce771fe2d3358516c724a10d653b2a3161d83fe870405b0`，引擎`bcf1c0f7beef47d21bb29539b498796714fea5d8d209910b9d09d516f5c018d2`。
19项STOPPED检查/发布、实际整根回退与再前进完成，275.847秒，
0新增GPU；outer保持启用且累计启动0次。服务默认保持STOPPED。

实际词时间戳、完整segment统计、SRT/VTT和VAD切片已接入默认。
[主轻](../serve-audio-auxiliary-profiles-20261117/README.md)和
[原生TLS/整包恢复](../serve-audio-auxiliary-service-20261118/README.md)
有真实双段语音业务；后者6计费尝试/5实际加载/19创建/1探针，
315.834秒、305次NVML正常。原exec失败及旧控制argv核对失败保留，
路径等价审查的依据/限制明确，未将其追认成原自动finalizer通过。

核心/client/outer预算字节保持，profile仅显式增加已验辅助身份与预算；
原history/pending/selected/deadline保持。实际整根回退恢复各根原周期。
人工根`deef6d754bb4ce2872f5eeb3a385fb38758f5bf8bb57e028291939dbb555d4c5`和自动payload`afe22b3b3149043c7844e8774b16b4bed97395454806116bfea589c6fc09a934`
均为当前引擎与保留控制，不声称独立旧引擎回退。前驱rev276及day入口保留。

4意图/2副本/2登记可恢复归档，0删除；版本归档首轮30秒超时保留，
按同一sealed计划续办，累计122.277秒，未改变默认和恢复预算。
说话人、Chat音频、合成、Realtime和全部master任务继续；不是完整模型
质量、性能或长稳通过。下一步完成PR45实际合入，并推进剩余实现。
