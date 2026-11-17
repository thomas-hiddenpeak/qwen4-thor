# ASR03服务合同的针对性CPU验证

34个独立用例全部完成，含原失败共35次执行，驱动累计2.167秒。
30项host（健康/资源/选项、multipart、模板、输出/UTF-8/实际概率/
usage、转录错误终态及直接Chat回归）首次通过。4项实际tokenizer中
3项首次通过，literal预算失败仍留部分ID；修复仅新literal模式的
失败清空，原断言不变，只补该1项，0.464秒通过。其余33项复用。

原FAILED结果、源码/生成身份及不可变程序路径保留。此处是组件CPU
接受，不是实际HTTP/SDK或模型业务验收。0模型构造、0GPU；默认
rev266及用户day保持，全部master继续。必要构建发现测试复制警告和
缺准入include，两处修复及原日志均保留，最终构建零警告。

- [接受记录](acceptance.json)
- [原始34项](service-cpu-01/result.json)
- [仅失败项补验](service-cpu-02/result.json)
- [服务计划](../../SERVE_NATIVE_AUDIO_2026-11-17.md)

保留本机程序与原路径：`.q4t-work/serve-audio-native-20261117/validated-binaries/`；
`q4t_asr_service-host-01`、原`tokenizer-01`与修复`tokenizer-02`不覆盖。
对齐/字幕/说话人/VAD执行器仍待实现；明确拒绝不作为目标完成。
