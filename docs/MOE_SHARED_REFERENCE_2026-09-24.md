# 完整4K共享专家与最终MoE合并核对

## 边界采集：通过观测一致性，尚未验证算术

2026-09-24，生产二进制2392f6b1未改。新只读观测器零警告构建后，
首项为tools/evalscope原4K HTTP关闭/开启/关闭观测三次对照。
三次均4096输入、7输出、stop、服务退出0，仍输出错误答案600440，
期望360284；质量驱动退出1是原失败，不是质量验收通过。

48层240份边界元数据全部通过。实际共享GU输入全字节等于已冻结
HC mixed MLP输入；SwiGLU输出接通down，down输出接通最终
MoECombine；routed输入全字节等于此前CombineGrouped输出，
最终BF16输出全字节等于HC write.block。已有大缓冲只比对不复制。

192个实际BF16权重张量、472104960字节直接匹配checkpoint：
每层gate/up/down及shared_expert_gate。保存全部251658240个GU、
125829120个SwiGLU、503316480个down输出以及96份实际算法。
三种算术参考尚未运行，不能用交接一致代替计算正确。

证据：`.q4t-work/e2e/moe-full-shared-20260924/`，含HTTP原始记录、
checkpoint偏移/摘要、上下游来源绑定和完整manifest。复现模板：
`tools/verify/moe_full_shared/`。采集后可用22645972992字节；后续
参考逐层生成，以实际输出加无损差异保存，预留20GiB磁盘容量。

原错答、已知E4M3编码缺陷及因果关系仍未解决。下一步完整共享
GU/down记录算法与FP64参考，再核对SwiGLU和最终门控合并。
