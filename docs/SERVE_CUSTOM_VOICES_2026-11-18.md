# 原生自定义音色实施计划

承接Serve总计划A01/A10/A12。当前Speech与Chat能解析`voice.id`，但
真实注册/条件后端缺失；本项要实际合成注册音色，不能用换成内置
speaker、只保存一个ID或报unsupported代替。内置9音色已实现，完整
音色/风格业务覆盖继续，不推定与OpenAI专有音色音质或身份相同。

1. 固定Qwen3-TTS-12Hz-1.7B-Base配置、480张量与76张量声纹分支。
   官方revision `fd4b254389122332181a7c3db7f27e918eec64e3`，完整权重
   3,857,413,744字节，SHA256为
   `38fc7fc51c5e776e840414b6fd443962e9411b9654888fd7913e4da643cb857c`。
   已取得68,512字节配置/头部；未下载完整权重，不是资产或运行验收。
   本组10新CPU+2直接受影响旧合同，120秒、0GPU、产物8MiB、磁盘
   余量256MiB；不接通未完成的HTTP执行，不改默认rev316。
2. 原生24kHz声纹前端、ECAPA-TDNN和连续speaker向量条件。复用现有
   CUDA运算、TTS talker/预测器/codec，固定独立数值与实际音频合同。
   官方Base支持x-vector及参考音频上下文，按源码实现并分别验收；
   不把原有192维说话人识别向量当成2048维TTS条件。
3. 注册/撤销/持久化、真实样本与consent关联、客户归属与有限存储，
   复用现有文件/鉴权/受管worker。状态提交成功才可见；失败/取消/
   重启不留下伪成功或跨客户可用ID。Speech/Chat通过ID取真实条件。
4. 逐字段schema、SDK、真实输出与风格、TLS、配额、缺资产/工作进程
   失败隔离、主轻配置和整包恢复，形成可核验回退发行。随后继续
   Realtime和其余master，阶段不是总目标终点。

当前可用磁盘不足以下载Base完整权重；先盘点本任务可重建缓存与
可无损归档数据，不删模型、历史证据或其他任务工作，开发独立继续。
库和权重仅启用自定义音色时按需装载，失能不得停止健康文本业务。
不增加Python推理服务或外部前置依赖。

协议依据：[Speech请求](https://developers.openai.com/api/reference/resources/audio/subresources/speech/methods/create)、
[音色注册](https://developers.openai.com/api/reference/resources/audio/subresources/voices/methods/create)、
[自定义音色](https://developers.openai.com/api/docs/guides/custom-voices)。
OpenAI音色ID及服务权限语义作接口参考，不调用外部语音API。
模型依据：[Qwen官方Base模型](https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-Base)、
[官方实现](https://github.com/QwenLM/Qwen3-TTS)。元数据按固定SHA/版本
保存，许可沿用Apache-2.0记录；本地MIT参考qwen35-thor只读研究，
不直接复制其缺少原子性/隔离的文件注册实现。

前端阶段证据见[原生计算与舍入诊断](evidence/serve-custom-voice-frontend-20261118/README.md)。
原FP32 log门失败原样保留；高精度方程通过，模型数值与音色业务仍
待实际后端验证。checkpoint BF16存储不代表原生激活采用BF16。

2048维声纹组件已经实际验收：见[组件证据](evidence/serve-custom-voice-speaker-20261118/README.md)。
完整源SHA通过后仅保留声纹前缀；全Base合成资产仍未保留。
36数值比较和8边界/16生命周期通过，官方FP32前端差异对最终向量
的影响在冻结门内。下一组实现Base声纹条件、持久登记和实际合成，
不以组件通过代替自定义音色交付。
