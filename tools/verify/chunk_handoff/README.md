# 同分块状态交接回归

运行方式同sequence_contract，指定与当前model.h/model.cu匹配的模型库：

```bash
python3 tools/verify/chunk_handoff/run.py \
  --build .q4t-work/sequence-slot-build-20260927 \
  --output .q4t-work/new-handoff-test \
  --model /path/to/read-only/checkpoint
```

完整48层，2槽位，max_len=272、max_prefill=128；固定257-token输入，
127/1/128/1分块及固定decode token97。比较typed chunk API与
ModelPrefill + ModelDecodeBatch + ModelDecodeStep相同算术调用链，
每个边界全部层状态/缓存和全词表末行logits要求逐位相同。
检查主机history/position/stage、设备绝对位置、有效文本三路RoPE、
过期rope_delta的重置、另一槽位隔离及交错执行。

缓存尺寸沿用本模型固定形状，indexer压缩率4与生产加载参数一致。
KV页表验证恒等映射；KV/raw indexer当前位置之后、compressed
indexer已完成组之后应仍为reset后的0。未对未使用RoPE尾部要求0。
这检测越界写入，不能独立证明attention没有读取无效区。
不比较不同分块策略，不覆盖长上下文稀疏选择、视觉或MTP。
测试中的cudaMemcpy/同步是观察边界，不代表生产异步时序证明。
