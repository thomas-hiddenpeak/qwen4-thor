# 原生注册ICL真实SDK业务（2026-11-19）

实际授权样本→原生ASR/声纹/Mimi编码→私有voice持久化→原生Base ICL
合成的完整用户路径通过。原生TLS/API Key前置下使用官方Python SDK
3.55.1，未用Python生成模型音频。实际样本107520个24kHz采样，保存
56×16=896个参考码及实际转写、来源SHA和speaker condition；公共
元数据未暴露这些内部资料，来源/所有权/codec/长度逐项核对。

英文Speech SSE生成38帧/3.04秒，input920/output609/total1529；
中文Chat SSE生成44帧/3.52秒，TTS input920/output705，整个Chat
prompt1087/completion713/total1800。输出均自然EOS。独立固定tokenizer
给出的输入数920=6模板ID+11参考文本ID+7目标ID+896参考码，与服务
实际usage和TPM结算一致，未拿预留预算当用量。默认文本路径未改。

两段输出经实际官方SDK上传到原生ASR，不提供prompt、原文或语言
提示，分别逐字得到“The blue train arrives at noon.”和“你好，会议
下午三点开始。”。Chat历史音频ID保留；跨客户Speech404，撤销consent
后Speech立即404，再删除voice；同一模型PID最后正确回答42。实际
注册状态另存私有快照供后续恢复/回退组合，不含新默认状态写入。

单阶段1模型+1原生入口，9业务POST/78控制请求，104.537秒，96次
NVML查询均正常；guard COMPLETED/child0，模型/入口正常退出，自有
工作进程、端口、GPU锁释放，默认rev326保持STOPPED且身份不变。
租约仅约束合作会话；轮询不能排除短于采样周期的干扰。

本验收是固定场景内容/协议/计量通过，不宣称通用音色质量或性能收益。
用户暂不试听，听感待验；原始授权录音机器声纹FAIL、原4/2336编码码
ID不一致FAIL仍保留。严格浮点边界资格见前一独立报告，本次未改变
模型数值/原始码。PID准入夹具不是Guardian恢复验收，后者及Realtime
历史、剩余master任务继续；总目标没有完成。

原始现场：`.q4t-work/serve-tts-icl-20261119/service-business-01`。
证据SHA、范围、默认和实际音频/用量见receipt/identity；原录音与
私有持久化状态留本机。准备时使用错manifest路径、旧tokenizer构造
参数的两项拒绝记录保留；均发生在GPU启动前，未作为业务通过依据。
