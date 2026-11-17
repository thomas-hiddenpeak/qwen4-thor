# 原生ASR基础层：24项针对性CPU通过

本组完成固定真实资产、配置/权重布局、原生tokenizer与log-mel，
**尚未实现完整ASR推理和HTTP交付**。A10及Serve总目标继续active。

- Qwen3-ASR-0.6B固定revision `5eb144179a02acc5e5ba31e748d22b0cf3e303b0`，
  9个文件共1,880,618,159字节，1532.417秒取得；每个文件Git blob/LFS
  摘要均通过。权重SHA256为
  `79d6cbd4c98c7bbffe9db2edac07f56cd6637d0d5944b27f6c2b8353840323ea`。
- 配置/布局8项（含完整实际checkpoint元数据）通过；612个BF16
  tensor、独立存储的输入embedding/输出head、精确分块长度，漂移/
  重复键/错dtype/shape/重叠/文件范围拒绝。
- tokenizer8项通过；16组官方中英文/Unicode/特殊token编码与解码
  一致，原主模型和embedding直接回归正常；限额、取消及失败保留
  原对象。运行时直接读官方vocab/merges/config，无转换工具依赖。
- 特征8项通过；6个合成PCM的完整128维log-mel矩阵在固定夹具中
  最大绝对误差为0（事前门槛1e-5）；最短/30秒最大预算、非整hop、
  非有限输入、取消及失败不污染输出。仅证明这些特征用例。

合计24项、2.571秒、0模型/GPU动作。三目标构建零警告，来源库存
及7种变异检查通过。首次未重新生成CMake目标、测试夹具CTAD编译
错误及库存首次未更新的记录保留；修正后才构建/验证。参考tokenizer
首次驱动错误要求Torch模块不被导入，原失败保留；修正后明确禁用
可见GPU并核对CUDA未初始化。参考版本及来源摘要固定，不成为运行
依赖，也没有用CPU参考代替模型数值或HTTP业务验收。

[接受记录](acceptance.json)与[原始记录/精确来源](records.tar.gz)含77个
文件，secret scan为0。原默认引擎、rev266包、发布元数据及账本摘要
与独立day冻结身份一致，服务保持STOPPED；没有启动长稳。

本机资产：`.q4t-work/serve-audio-native-20261117/assets-01/`；
已验CPU程序另保存在该阶段`validated-binaries/`。下一步实际音频
解码、原生encoder/decoder、标准HTTP/SDK和恢复组合；转录后仍要
继续Chat音频、语音输出、对齐/说话人、Realtime及其余master任务。
