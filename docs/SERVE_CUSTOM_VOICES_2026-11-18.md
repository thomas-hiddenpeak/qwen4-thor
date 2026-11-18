# 原生自定义音色实施计划

承接Serve总计划A01/A10/A12。样本音色的真实授权、持久登记、
Speech/Chat、客户隔离、撤销、SDK/TLS及主轻/实际恢复已验并进入
[默认rev326](evidence/serve-voice-delivery-20261119/README.md)。
[Base-only已有音色消费](evidence/serve-voice-consumer-business-20261119/README.md)
两档真实资格和可选封装也已完成，不依赖注册用ASR/VAD/speaker。
参考音频ICL、提示词音色/Live、完整风格及听感/相似度继续；不推定
与OpenAI专有音色音质或身份相同。以下实施分项及历史证据保留。

1. 固定Qwen3-TTS-12Hz-1.7B-Base配置、480张量与76张量声纹分支。
   官方revision `fd4b254389122332181a7c3db7f27e918eec64e3`，完整权重
   3,857,413,744字节，SHA256为
   `38fc7fc51c5e776e840414b6fd443962e9411b9654888fd7913e4da643cb857c`。
   完整只读权重已取得且全SHA通过，134项Base条件数值对照已验收。
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

12份同SHA历史head已保留备份与恢复后共享存储，回收约3.42GB；
完整Base权重已通过全SHA，原下载三次中断保持，不覆盖失败。
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
早期源SHA通过后仅留声纹前缀；随后完整Base资产已取得并通过全SHA。
36数值比较和8边界/16生命周期通过，官方FP32前端差异对最终向量
的影响在冻结门内。下一组实现Base声纹条件、持久登记和实际合成，
不以组件通过代替自定义音色交付。

Base独立权重/2048条件接线22项CPU已通过，见[条件证据](evidence/serve-custom-voice-conditioning-cpu-20261118/README.md)。
实际Base数值/自然EOS、持久注册和客户隔离继续，不改变总目标。

2026-11-18再次核对当前官方指南及SDK：consent有创建、查询、列表、
名称更新和删除；样本创建默认`type=audio_sample`，返回完整voice
metadata。样本/consent不超过60秒/10MiB，需同一声音；登记不能
仅存ID而忽略真实音频与consent关联。客户归属用鉴权scope，不能
采用请求中的name/user字段；状态用现有有界ObjectStore原子提交，
删除consent后新请求不可继续使用其音色，未知提交通过原ID核对。

当前官方还包含`type=prompt`、prompt/model/script_hint创建音色，
其使用入口目前为Live。该输入/backend及相关语音入口保留为后续
实现子任务，与样本声纹合成分开验收；不得用当前样本路径代替。
新增依据以本地api-source-02保存的当前官方指南/OpenAPI和SDK为准。

完整Base条件已通过[134项官方数值对照](evidence/serve-custom-voice-base-component-20261118/README.md)。
本阶段仅2帧，继续自然EOS、真实音频及持久注册业务，不以数值代替交付。

[实际原始样本到完整Base语音已通过](evidence/serve-custom-voice-base-utterance-20261118/README.md)：
两句自然EOS和独立ASR内容通过；接着持久API与新文本，听感/相似度、
风格、参考音频ICL及提示词音色仍分别保留，不能由本阶段替代。

[输入与持久登记26项CPU已通过](evidence/serve-custom-voice-registry-cpu-20261118/README.md)。
尚未接入实际HTTP。保留原始24kHz条件输入，真实授权与同人验证后
才登记；新增对象类型需要兼容读取回退包，有状态回退是发行必要项。

服务源码与27项针对CPU已完成，见[服务CPU证据](evidence/serve-custom-voice-service-cpu-20261118/README.md)。
`--tts-base-model-dir`独立选择固定Base资产；启用登记还需已有ASR、
VAD和说话人资产。元数据/撤销不依赖模型健康；合成与注册分别按
真实统一RAM预算准入。候选未发布，真实HTTP与有状态兼容回退继续。

2026-11-19：ICL编码器225张量与预算合同已完成，[14项CPU](evidence/serve-tts-icl-metadata-20261119/README.md)通过；实际编码和参考条件接线继续，不替代已交付x-vector，也不提前更换默认。固定Mimi eager实际为full causal，后续长样本数值覆盖250行之外。

2026-11-19：注册音色rev326与Base-only消费已随[PR49合入main](evidence/serve-voice-mainline-20261119/README.md)。[ICL实际编码器](evidence/serve-tts-icl-encoder-20261119/README.md)末层复制填充修复；222浮点及2全码比较通过，长例一帧4码浮点边界待决定，原FAIL保留。继续提示、持久化和实际业务，不提前发布ICL。
