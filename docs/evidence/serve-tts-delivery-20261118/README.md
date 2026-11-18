# 语音输入与输出可回退交付 rev316

默认服务包 e992bae3e6a090c595605ff1fbb4c122c96838510bef1f3f6ec6f3c7b4fd111d，引擎 5130cae65358f151cbf9c055af9d2c521e6acc46183c32aa70b6581ab39f6fee。
19项STOPPED步骤（3项复用、16项续办）、整根回退与再前进完成，290.446秒，
0新增GPU；outer启用且累计启动0，服务保持STOPPED。

Speech/Chat真实音频输出、私有音频历史和重叠分离进入默认；主轻
配置均验证输入/输出、完整音频、usage及辅助权重释放后按需重载。
TLS入口恢复与整包回退前后实际收音/发声，双SDK、证书、独立Key、
活动取消、存储Chat和原恢复历史/冷却保持。复用
[主轻26条](../serve-tts-input-profiles-20261118/README.md)、
[TLS与恢复40条](../serve-tts-service-20261118/README.md)及直接
[Speech](../serve-speech-http-20261118/README.md)/
[Chat](../serve-chat-audio-output-http-20261118/README.md)精确身份记录。

核心/usage/outer预算字节保持；client仅显式将alpha输出上限从256
升至2817，策略revision 0→1，使用正式持久化API，不重置用量。
首次发布因初始化配额不匹配被拒，原FAILED保留；续办原事务，
复用3项已完成检查，仅执行余下16命令。profile显式加入分离/TTS合同；
原history/pending/selected/deadline保持。人工根 ffc8be5a4ab4a20b8fe46463365346287538d877fb1969c0b4bb455c7df08bfa、
自动payload a526d4f291dce3b33d4791b5caae95f473e18c6fdad01800f98319e0664e03d0均使用当前引擎与保留控制；
前驱rev306及day入口保留。5旧intent、2旧generation和2旧registration
按现有工具可恢复归档，0删除，无模型动作。

按用户决定，仅指定混音的hurt→hurts词形误差可接受，保留WER=1/7
与原失败；不放宽漏句、说话人/重叠或改变句意。4倍内容、完整
voice/style/注册voice、非语言声学、Responses/Batch/Realtime及全部
master剩余项继续。下一步主线检查合入；未宣称完整OpenAI兼容、
全模型质量、性能或长稳通过，总目标仍active。
