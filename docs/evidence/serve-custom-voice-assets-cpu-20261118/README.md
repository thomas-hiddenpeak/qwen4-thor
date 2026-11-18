# Base资产选择与统一RAM：12项CPU通过

资产worker新增固定Base身份，完整source SHA与metadata分别验证。
Base合成和声纹登记共用同一源文件，登记不要求codec目录，也不
加载talker/codec到GPU。完整加载才解析真实speaker及talker extents；
成功后一次发布所选变体，取消或后续codec失败保持旧输出。
上传按既有2MiB BF16/4MiB FP32上限展开，失败不发布设备绑定。

6新增与6受影响CPU合同全部通过，1.417秒；0checkpoint读取、
0模型/GPU。覆盖实际Base tokenizer元数据、无codec登记、跨模型
替代拒绝、codec失败原子性、预算/取消/错误选择不启动worker，以及
精确资源边界。原内置资源总额保持；Base合成多计源文件差值、
8KiB设备条件及16KiB主机条件；登记计48,004,352字节FP32权重、
5至5625帧有界工作区、PCM/logmel/固定表和完整3.857GB源staging。
这些都属于统一RAM，不能称为CPU卸载。

必要host及CUDA upload构建20.856秒、零警告；923来源锚点和13
漏项负控通过。现有codec权重与metadata按Base固定源revision的
LFS SHA和Git blob ID逐件核对后复用，未凭形状判为可替换。
测试前修正只读夹具替换方式，原未执行计划保留；未跑完整质量或
性能矩阵。实际完整Base权重上传/音频业务仍待验，不能由CPU通过
推定接口交付。默认rev316保持，继续真实数值与注册音色业务。

[结果](result.json)、[逐件SHA](index.json)、[原件](records.tar.gz)。
