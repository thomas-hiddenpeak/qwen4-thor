# SRV-02j2 生产CUDA申请额度接线合同

继续工业化资源可靠性工作：把02j1额度核心接入生产GNU link wrapper，
覆盖实际物理启动预留、运行期device/pinned/managed/async申请。核心
请求字节额度不等于物理RAM硬限额，DSO内部、driver、host堆、文件
缓存及CUDA池保留仍独立。模型数值、模板、分词与GPU kernel不改。

默认`--cuda-allocation-limit-mib auto`按mem_fraction×MemTotal解析；
0显式关闭，正整数明确指定MiB。该额度独立于--no-budget，不自动
退回无保护路径。配置公开来源/有效上限和覆盖边界，metrics公开
实际charged/live/pending/unknown/peak、表容量与拒绝原因。

生产quota采用进程生命周期：既有argmax/MoE静态缓存也必须计额，
不能在ChatServer析构时遗失账本后允许第二轮重新占满。配置仅一次，
已有进程缓存的剩余live明确报告；不把process退出前的live全称泄漏
或宣称逐字节物理回收。重配通过受控新进程完成，不新增热改额度API。

同一预留块物理申请只记一次，Take交付不记第二次。保留CUDA flags、
零字节/null free和原cleanup-fatal合同。异步free必须保留pending到
同步成功；stream creation/destroy映射有界代次，原始handle复用不能
误退旧额度。legacy与显式per-thread stream分别处理；event和stream
destroy不冒充完成，默认流同步不能消掉其他流pending。

拒绝分配映射现有capacity失败路径；可恢复的serve缓冲拒绝返回
503并保留quota原因，内部forward部分执行失败继续按既有GPU不健康
退场，不能用一次容量拒绝掩盖部分模型状态。未知释放/完成沿用
停止/退场合同。指标读取不等待长CUDA同步持有的全局锁。

## 成组验收

先完成原wrapper fake CUDA、纯host选项/配置/指标与协议schema核对，
再同一GPU租约做原生接线和真实业务，不在中间释放锁。必要构建不
算测试；零警告，按源/依赖身份复用既有数值和核心证据。失败保留，
只修正与重验受影响项；没有性能收益或非劣声明。

计划上限：6个原生接线模式（含关闭、混合/flags/零值、async/代次、
startup owner组合等）；真实serve3次启动尝试，其中1MiB额度启动
拒绝、默认auto/full-head MTP k3五生成（普通JSON/SSE、strict JSON、
真实项目工具与回填）、正显式额度/startup-plan off/no-MTP两并发生成。
最多7生成、120控制请求、600秒总GPU窗口；另在锁内复用25原生HTTP
生命周期检查。每短进程采用已验的WNOWAIT保管，未知身份记ENV_ABORT。
不做物理OOM、不执行小时/day验证、不启动/恢复vLLM。

完成后才按既定授权封存回退包、发布适用默认pair、做有界安装检查，
并为新默认准备新的用户长验交接，旧day计划/证据不覆盖。若接线尚
未通过业务验收，默认保持，开发提交明确标记未交付。实际部署LAN/
证书/来源限制、通知接收方及整机RAM硬保护仍是独立未完成项。
