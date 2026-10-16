# 旧trace离线设备区间与PLE空隙审计

只读2026-10-16已保存SQLite，不新增推理或修改原trace。source-identity
锁定输入；analyze.py按EmbedLookupKernel将prefill分为25个chunk，cut为
首个496640字节D2H完成。每chunk计算kernel/copy/memset区间并集及补集，
校验两者之和等于chunk跨度；kernel总耗时可因并发重复计数，不能作wall。
copy_context.py审计chunk2/13/25最大空隙后的拷贝与前后API/kernel。

A的已知竞争加载结束约trace相对99.135秒，chunk2开始103.947秒，
所以直接时间重叠集中于chunk1；后续残留效应仍未量化，不能宣布干净。
chunk2–25中A−B的span约+635.558ms，其中活动并集+494.505ms、
空隙+141.053ms；方向与原HC变慢相反，不能用它直接解释历史回归。
A首chunk空隙93.662秒，B0.175秒，原trace总量受污染非常明显。

大空隙后的H2D为8192×2560或8181×2560字节，紧随FP8转换，前一API
是短cudaEventSynchronize。对应src/ple/ple_embedding.cpp GatherRows的
upload_complete等待之后、cudaMemcpyAsync之前的reader->Gather。
这是路径定位；不是磁盘I/O、CPU散拷贝、内存压力的进一步归因，也不能
将全部GPU区间空隙叫整卡空闲。next API进入前的时间只是时序证据，
不排除其他进程/未追踪活动。原NO_GO保留，无数值或性能接受结论。
下一步仅补Gather等待/CPU观测，不开展无关prefill优化。
