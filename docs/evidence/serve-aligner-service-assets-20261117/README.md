# 独立对齐资产、资源和健康合同

15项针对性CPU全部通过，0.572057秒、0GPU、0完整权重读取。
其中7项隔离资产（5新对齐+2原ASR）和8项服务合同（6新+2原ASR）。
构建零警告。随后仅clang-format空白调整，逐文件去空白字节相同；
保留测试时原源码和前后摘要，未重复模型或完整测试。

固定对齐catalog验证每个文件的长度与SHA256；元数据加载不打开
checkpoint（FIFO负控）。同尺寸破坏、ASR混用、symlink、工作进程
死亡/错profile、取消/deadline和staging容量拒绝均保留原owner。
完整checkpoint读取用例已实现，留到独占租约内的实际加载验证。

服务预算计入24层encoder、全部时间槽logits、host staging和可能
文件页，使用同一物理RAM估算。400秒训练类别边界、context/词数/
workspace和原输出保持已验；实际BPE不足空间不会截断或注入marker。
独立health实例具备5秒重试冷却，辅助失败不改变健康转录实例；
真正GPU故障保持sticky保护。此处是CPU组件，实际ChatServer接入和
双模型同服务业务仍须执行，不能提前宣称完整故障组合通过。

默认rev276保持，继续实际时间戳/字幕、业务交付与全部master任务。
