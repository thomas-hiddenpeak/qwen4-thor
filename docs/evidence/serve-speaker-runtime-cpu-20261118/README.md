# Speaker运行时接入针对性CPU验证（2026-11-18）

13C++/0.164秒、4schema/0.214秒通过，0GPU/HTTP。
真实PCM编解码校验2/10秒两端及边外拒绝，私有作用域/取消、内联
数据URL、配置依赖/预算、可选健康与delta/segment关联通过；原
ASR/VAD/aligner配置和实际历史JSON/流合同保护通过，自有worker清理。

运行时已连接原生ECAPA、真实VAD/对齐和diarized_json，独立错误
不改变核心readiness；CUDA故障仍保留保护。所有编译源码SHA与当前
文件一致、零警告，22锚点/7门禁负控通过。一次schema生成选错旧
type graph失败保留，改用与既有schema身份一致的保留图后重新生成。

本组仍不是实际语音或HTTP验收，默认rev286未变。3段官方说话人
样例共257092字节按固定commit/git blob/SHA取得，只保存身份记录，
音频留本地。下一步真实语音/双SDK及服务恢复交付，重叠检测分离与
全部master继续；未做完整性能/模型质量或长稳。
