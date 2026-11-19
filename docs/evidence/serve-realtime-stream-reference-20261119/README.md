# Realtime输入流参考复核（2026-11-19）

本轮2个有界只读请求确认Qwen3-ASR main仍为
`7c6daf77a2421100f5fb066495372c00129d39ff`，推理源码31163字节与
此前已固定原件逐字节一致；SHA见identity.json。未拉新模型、未测试GPU。
[官方固定源码](https://github.com/QwenLM/Qwen3-ASR/blob/7c6daf77a2421100f5fb066495372c00129d39ff/qwen_asr/inference/qwen3_asr.py)
采用累计实际音频与可回退的转录尾部，默认2秒片、前2片不固定前缀、
后续保留5token修订。每次重复输入累计音频，结束排实际尾部，不补假
样本。其Python缓冲增长与可修订文本接口不能直接当成本项目的有界
存储和只增delta协议；q4t需独立完成前缀/UTF8、借用、配额与恢复合同。

项目现有Apache-2.0许可记录保持；这里只参考设计，不复制/链接其
vLLM执行代码，vLLM仍不是运行依赖。原件保留在私有stream-reference-01
以及此前official-src，reference/目录未修改。

协议核对使用本机固定OpenAIJS7.47.0及官方
[转录指南](https://developers.openai.com/api/docs/guides/realtime-transcription)、
[VAD说明](https://developers.openai.com/api/docs/guides/realtime-vad)和
[客户端事件](https://developers.openai.com/api/reference/resources/realtime/client-events)。
转录有输入到达时增量处理与commit后转录两种行为；delay属于指定模型
条件，连续输入型号要求turn_detection=null。Qwen原生配置不得冒充
远程模型权重或专有算法；目标是实际处理相应音频上下文并正确映射事件。
当前已验的是commit/VAD后的原生转录，尚未实现前者，不据此重标已完成。
