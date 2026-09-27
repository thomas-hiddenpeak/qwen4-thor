# 序列槽位合同回归

这是Bug/状态合同测试，不是性能bench，也不使用语义答案判定。
链接指定构建的真实模型库，默认加载完整48层、两个槽位。

```bash
python3 tools/verify/sequence_contract/run.py \
  --build .q4t-work/sequence-slot-build-20260927 \
  --output .q4t-work/new-sequence-test \
  --model /path/to/read-only/checkpoint
```

新输出目录保存源码、库摘要、编译命令和退出记录。源头文件必须与
指定库版本匹配；不能用新默认参数的头文件去代表旧版API调用。
已冻结旧版复现保留自己的check.cu及model.h/model.cu快照。

固定13-token prompt，7/5/1分块、decode token97；17-token干扰。
比较显式槽位1和省略参数的状态/全词表logits；检查槽位0哨兵、
另槽位prefill穿插、同槽位A→B→A及四个拒绝场景。
状态包括全部层SSM/conv/PLE、KV、原始/压缩indexer缓存；不含
RoPE缓存、所有主机管理字段、异步调度器/断连路径。

相同计算路径要求逐位一致，不使用容忍阈值。prefill两侧同为
全行logits，只比较最后一行。先前将全行和末行模式直接比较的
171项差异单独保留；不同head GEMM形状不是本测试的相同路径合同。
不同分块数值等价、长上下文、批处理和MTP仍未覆盖。

exit0为全部已测合同通过；exit1保留量化后的差异计数；exit2为
前置/拒绝/复用合同失败，不能当作正常数值差异或通过。
