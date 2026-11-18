# Base原始样本到完整语音：内容通过，注册API继续

本阶段走原生生产asset worker与上传函数，固定Base完整源与相同
codec均校验；两worker完成并回收后，实际加载声纹、talker和codec。
从既有原始中英文PCM重新计算logmel和2048声纹，两向量与之前独立
验收结果逐值一致。只将声纹条件加入Base，不借用内置speaker ID，
不传参考音频codes，也不向ASR提供原文或强制语言。

两句各40帧/76,800样本/3.2秒，自然EOS且未触及160帧上限。
完整浮点波形经固定独立CPU特征提取，原生ASR两句均逐字归一化
还原原文：Hello, this is a native speech test.；你好，会议下午三点开始。
此处是原速输出，无4倍时钟变换或后处理内容修正。

同一严格租约1声纹/1talker/1codec/1ASR，21.172秒，22次NVML查询
全部正常、退出0、租约COMPLETED。0主LLM/HTTP，默认五项身份保持。
94声纹步骤、9578生成步骤及702codec步骤均在冻结预算内。
3项验收驱动CPU反例0.064秒通过；必要程序29.327秒零警告。
测试辅助Python依赖的运行时提示单独保留，不作为生产依赖引入。

[英文原WAV](english.wav)和[中文原WAV](chinese.wav)供正常播放复核；
没有声称已完成人工听感、声音相似度、风格或任意文本质量验收。
参考音频ICL路径也仍在计划内，本阶段实际使用x-vector模式。
持久注册/consent、客户隔离、撤销、Speech/Chat通过ID合成新文本
及受管恢复继续；本组件不等于完整OpenAI音色接口交付。

[结果](result.json)、[逐件SHA](index.json)、[完整输出与来源](records.tar.gz)。
