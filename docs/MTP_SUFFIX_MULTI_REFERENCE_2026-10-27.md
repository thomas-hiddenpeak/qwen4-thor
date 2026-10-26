# 4槽位目标参考首失败与限定修复

c158f195 / F-v2 首完整head ON完成28HTTP（9精确质量、16性能、3取消恢复），
28 host suffix及服务选项合同通过，实际B4和混合命中审计通过。随后旧单流
状态参考通过，新multi参考在首个全suffix批次的目标FP32 SSM逐位比较失败；
元素首次差异byte1/12582912，尚未运行其后OFF/子集。原始FAIL和211.018秒
阶段、监控无失败终态完整保留。不以HTTP或同路径verify通过掩盖该失败。

原新夹具用四次不同M的单序列ModelDecodeBatch回放接受前缀，实际目标用
一次16行ModelVerifyMulti。既有EVALUATION不承诺跨M/不同GDN路径浮点
逐位相同。当前仅有这一不满足同形状前提的反例，不能直接证明运行时错误，
也不能未经验证把它归为误报。

唯一第二次针对修复：只改测试参考，保留零容差，使用ModelMixedBatch
（不保存/恢复checkpoint）一次执行相同总M、相同packed prefix行位置。
真实4槽只消费接受前缀；拒绝尾部放入4个测试专用fresh padding槽；因此
参考仍独立于恢复和speculative step，拒绝尾部不会更新真实槽位。检查
全部真实槽的SSM/conv/PLE状态及独立draft proposal/extend。若仍失败，
本轮停止，不能放宽容差或据HTTP通过交付。此参考仍为两层GDN的局部合同。

推理程序c158不变，已完成28HTTP及full单流合同复用。第四次构建只更新
测试程序；只补full/subset两个multi fixture，连首轮2个fixture合计仍4次。
旧子集单流EOS/尾部合同在未改变的全局ID/停止分支上作为既有证据复用，
新子集multi重新验证0/1/2/3接受、切换、稀疏单槽和所有有效draft状态。
后续3服务启动/108生成HTTP，F累计仍136请求/5尝试（含0加载首失败），
总任务20启动/401请求及原绝对期限保持。原F7200秒包含诊断/修复停顿。
