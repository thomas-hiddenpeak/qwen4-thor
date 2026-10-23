# 子集head完整校验的SHA2启动优化

2026-10-23，性能优化，首轮待测。父binary `75e85bac`，源码父提交
`ef6b6577`；根main仍为`ab762aa`，不能视为运行binary源码。
工作区与分支复用`codex/mtp-subset-head`。

## 冻结范围与合同

只替换SHA-256实现：Linux AArch64小端检测HWCAP_SHA2，有能力时使用
ARM SHA256指令；否则保留原标量轮函数。硬件指令在独立target函数内，
不提高全程序指令集要求。输入按64字节块处理，尾部和长度编码保持
标准SHA-256；原64KiB文件缓冲、错误状态与失败不写digest合同保留。

源分片3,683,689,888字节、tokenizer/config、ID和320MiB head仍完整
读取并校验，不使用跳过、持久缓存或弱摘要。唯一65536行BF16产物
及映射不变；主head、GPU计算形状/kernels、固定k3及状态恢复不变。
完整head仍是默认。新增常驻模型内存0，已有320.25MiB副本照常计费；
物理RAM包含文件页缓存，不声称SSD冷态读取或容量卸载改善。

参考[ARM intrinsics](https://arm-software.github.io/acle/neon_intrinsics/advsimd.html)
和[GCC函数target属性](https://gcc.gnu.org/onlinedocs/gcc/AArch64-Attributes.html)。

## 事前验收计划

启动目标：相同子集配置，startup_total较75e缩短至少20秒。保护项：
全部摘要逐bit相同、所有非法hash仍拒绝；两模式质量与输出一致；
两模式五档TTFT/final decode无可归因确认退化；容量208896、所有权、
统一RAM预算保持。不拿单独hash计时代替实际启动收益。

总预算90分钟、6次HTTP服务启动、54请求、0重试/额外warmup：

1. 新候选子集真实HTTP质量11条（第一项测试）。
2. CPU摘要合同：22输入、边界/未对齐/分块/并发/重复/文件错误，
   标量与硬件对Python独立摘要；实际源分片和head各两路径一次。
3. 全65536行与special复核、14个非法加载合同（含全部hash字段）。
4. 父75e子集启动A：1条HTTP健康。
5. 候选普通模式质量11条。
6. 候选普通模式五档，每档3请求，共15条。
7. 父75e子集启动B：1条HTTP健康。
8. 候选子集模式五档，每档3请求，共15条。

父75e两模式五档、质量和未改GPU数值/接受/恢复/释放证据复用上一轮
acceptance-v2，冻结引用文件摘要；不将历史5a完整head误作本次父。
请求保持1024/4096/8192/45056/204800，单流，256输出。
候选两次子集启动与父两次启动提供有界描述性对照，无清页缓存，
不把请求内重复当独立启动，也不宣称跨启动统计非劣。

第一失败、身份变化、超时或ENV_ABORT立即停止，保留75e及现场；
不扫描词表大小、不自动补样。完整阶段同一GPU lease，CPU合同也
在该阶段内。派生产物只在`.q4t-work/sha2-startup-20261023`及
隔离source的`.q4t-work/sha2-startup-v1`，旧build/封存包不覆盖。
实际机器计划由`tools/verify/prepare_sha2_startup.py`生成，含文件SHA、
源快照、命令、顺序与预算；运行前先完成零警告构建。

若验收通过，按既有授权完成可核验75e回退包、binary/sidecar同步，
部署阶段最多3启动/3请求（子集/完整head/普通）独立完整lease；
失败则按已知身份回退。验收完按四字段记录结论，不追加性能采样。
