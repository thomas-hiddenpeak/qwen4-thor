# 原生参考音频ICL：编码器合同

新增codec编码器225个FP32张量的精确名字、形状、extent和紧凑绑定。
保留整个496张量文件的已有校验与SHA要求；encoder占224937216字节，
不需要下载新模型。运行计划选择1个semantic及15个acoustic量化码，
两套projection独立；其余16个acoustic码本仍逐项校验而非误作执行。
LayerNorm的bias、4/5/6/8与最终2倍降采样、左补kernel-stride及向上
取整保留。全部wave/残差/注意力/RVQ/GEMM工作区计入统一RAM上限；
最多60秒单声道24kHz，非法PCM/溢出/跨图绑定在GPU前拒绝。

针对CPU14项（10新增、4直接codec回归）通过，0.032秒/0GPU；本机构建
零警告，943来源锚点及17负控通过。含相同字节数但错shape/错名字、
未使用码本、整文件decoder破坏、短尾长度、工作区上限与输出原子性。
只证明元数据及预算，实际神经编码、ICL prompt和Speech/Chat未接通，
没有宣称真实参考音频合成通过。默认rev326不变，PR49独立检查继续。

后续按implementation-plan.json继续CUDA计算、固定官方参考数值、
持久音色/标准接口和实际可回退交付。固定transformers4.57.6的Mimi
非流式eager路径实际使用完整causal mask，虽然配置保留sliding_window
250；后续实现与数值门须以这一实际调用路径为准，不能只看配置
套用decoder72窗口或推定encoder250窗口。长参考样本需跨250行核验。
不新增Python推理服务，也不移植Orator的AGPL代码。
