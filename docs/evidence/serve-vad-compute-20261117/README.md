# 原生VAD实际CPU计算与参考来源修复

8项结构/取消/预算/原子所有权CPU通过；真实中英文PCM、零PCM和
固定噪声4组、667帧通过固定数值门槛。最大概率误差2.62260437e-06，
终态h/c最大误差8.39233398e-05，0.5判定逐项相同。两轮累计
3.357秒、0GPU，两个必要构建零警告，默认rev276未变。

[原始组FAILED](cpu-01/result.json)保留：同一官方提交的JIT与原先
冻结safetensors有14/15张量不同，不能将不同权重用作差分。原生
计算与8项CPU已成功，未重跑或改门槛。只修复参考参数绑定：保持
官方JIT计算图，用原先固定safetensors逐位装入15张量。该步骤
不是未修改的发行JIT模型资格，实际[逐张量比较](cpu-02/reference-output/packaged-weight-comparison.json)
与[参考身份](cpu-02/reference-output/reference-identity.json)完整保留。

[补验通过](cpu-02/result.json)复用原生4组及8项CPU，新增1次CPU
参考；原目标权重、原生算法、阈值与音频均未改变。固定来源和
操作见[初始计划](compute-plan.json)、[参考修复计划](compute-plan-02.json)。
候选使用原生C++，无PyTorch/tinygrad/ONNX运行依赖；JIT仅用于
开发CPU参考，MIT来源见[许可说明](../../../third_party/licenses/silero-vad/README.md)。

这是逐帧语音概率/循环状态组件资格。真实业务目前只覆盖中英
短录音，不声称全语言分类质量；逐帧posterior不能直接充作整段
no_speech_prob。服务资产隔离、VAD chunking、完整segment统计、
恢复/发布及全部Serve任务继续，本组件尚未发布。
