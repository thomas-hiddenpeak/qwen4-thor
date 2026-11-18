# Base-only已有音色消费：真实资格与可选封装

两档配置同一严格GPU租约，2启动/26业务POST/126.176秒，
118次NVML正常；最终资格、来源、进程/端口/锁清理通过。
引擎仍是C05 `943964c4208b1b802ca04562155e203e797d891196978881031e36e588e71ab9`，编译714来源未变。

从原真实SDK注册快照分别复制两个独立状态目录，不修改原始授权、
音色ID、客户所有权和2048维条件。仅启用Base、原核心/embedding/媒体；
ASR、alignment、VAD、speaker、separator和内置TTS均disabled且零加载，
注册条件encoder保持configured且零加载。实际Base Speech/Chat均完成，
四份完整WAV与原独立无提示ASR及两档注册测试见证逐字节一致；本轮
没有新ASR。完整音频历史、跨客户404、缺注册资产503、授权撤销后
Speech/Chat双路404，以及原schema/真实文档工具/embedding/视频通过。

可选原生封装 `4325b7d6b8f7f56b216ff8020de3b92aecc85ac5afb137145de20da4a6df402f` 已按v12资格准备；私有
状态init/check/status/profiles/observe共5步骤完成，没有启动服务。
首次断言误将HTTP包的credentials_checked当成客户鉴权检查；该字段
实际表示TLS证书检查，原check已PASS且client_state_valid=true。原失败
保留，仅离线确认适用性并完成剩余3命令，未重做init/check或GPU。
封装为隔离夹具，私有API Key没有写入证据；其完整源码/资格可校验。

已有音色部署可只配置`--tts-base-model-dir`，并沿用原`--object-store-dir`
和客户ID对应的`--client-policy-file`。变更API Key不应变更音色所有者ID；
重命名客户不会转移授权。创建新consent/voice仍需配置原生ASR/VAD/
speaker；Base-only不会伪造登记成功。此模式是显式部署选择，不能用作
自动删减现有完整服务能力的许可。默认rev326和其恢复预算保持。

此次未重新注入Base-only自动监护故障，不扩张原受监护默认资格；
原型正常播放听感/相似度、参考音频ICL、提示词音色、Live及总计划
剩余项继续。4倍内容正反例已按用户口径通过，见[既有证据](../serve-speech-clock-content-20261118/README.md)，仅正常播放听感待验；接着推进ICL/实时。
