# 语义VAD回合判定状态机（仅CPU模块）

12项针对性CPU全部通过，0.003461秒/0GPU；独立构建
2.267秒，-Wall/-Wextra/-Werror零警告。

覆盖实际音频时钟、eagerness最大等待、明确内容概率与绝对截止、
不完整/缺失/非法模型结果、ASR覆盖不足、续说与换回合后的迟到结果、
精确worker回收、重复/伪造票据、原点与采样网格、无输入不造静默。
采用本项目明确的概率→等待线性策略，不宣称复现官方私有分类器。

协议依据：[OpenAI VAD指南](https://developers.openai.com/api/docs/guides/realtime-vad)
和[GA客户端事件](https://developers.openai.com/api/reference/resources/realtime/client-events)：
auto等于medium，low/medium/high最大等待8/4/2秒；根据实际语义
模型概率调整静默等待。文本/模型评分、ASR连续链路和路由尚未连接，
这些CPU结果不能证明语义识别能力或真实接口通过。

模块当前未纳入CMake/默认程序，PR #50固定已验head不包含本阶段；
继续接本机实际模型与会话，再冻结直接受影响的CPU/真实语音短验。
