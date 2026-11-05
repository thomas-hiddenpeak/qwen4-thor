# 独立 NVFP4 草稿 head：Thor 可行性关卡

2026-11-05，用户授权开始推进 vLLM 特性吸收。当前阶段是性能优化的
可行性关卡，不重启 serve 工业化，不自动合入或替换默认程序。

## 身份与范围

- 父源码 main `d8880693d050385cf155655b62196aee1ff50b7a`，已接受默认
  ELF `c1bdb0a7599ba3f61d9cb48597069a66f6e27e02f38cf32e495fc80059008636`。
- 单工作分支 `codex/mtp-quantized-head`，隔离工作区
  `.q4t-work/mtp-quantized-head-20261105/source`；不切换共享根工作区。
- 上游固定 `4ba787ec9959e566d3b587a796dff07673324da5`，
  [PR 59973](https://github.com/vllm-project/vllm/pull/59973)。
  `online/lm_head.py` 从实际加载的 BF16 权重派生副本，NVFP4 路径为
  W4A16。其 SM121/DGX Spark 数字不能推导 Thor 收益。
- 当前已有 BF16 子集 head 与已合入的 prefill 评分优化保持。新候选
  只改草稿 head；目标 head、主干、验证、分词、终止与状态更新不改。
- 首版显式 `--mtp-head-quantization nvfp4`，只支持单槽文本 MTP k=3，
  与子集 head、suffix、prefix/session cache 和 startup-plan required
  互斥。上述组合未纳入本关卡，不将编译成功视为已支持。

## 源码可行性与数值合同

项目既有 `DequantFp4ToBf16Async` 会物化完整 BF16 矩阵，不能直接实现
本任务减少 head 读取量的目的。CUDA 13.3 本机头文件提供
`__nv_cvt_fp4x2_to_halfraw2`，SM110a 可编译原生 FP4 转换；已有
BF16 GEMV 使用的 f32x2 FMA 可复用执行方式。上游 Marlin capability
谓词接受 CUDA >=7.5，但这不是本项目依赖已集成、当前安装有兼容
cubin、或该 shape 在 Thor 已测快的证明。本阶段实现小范围原生
解包/反量化/点积融合内核，不引入整个 vLLM/Marlin 运行栈。

1. 量化源是实际已加载的完整 BF16 head，不再从模型目录写回或转换
   外部文件。拒绝非有限权重；所有临时资源在失败/成功后明确释放。
2. E2M1 低 nibble 对应偶数 K；每 16 权重一个非负 E4M3 scale。
   全局反量化系数 `g=max(max(abs(W))/(6*448),1e-12)`；组 scale 为
   `RN_E4M3(min(448, max(abs(group))/6/g))`，权重为
   `RN_E2M1(W/(scale*g))`。scale 舍入为零时该组编码为零。
3. 推理激活仍为 BF16。解码权重明确按
   `RN_BF16(E2M1 * (E4M3 * g))`，点积 FP32 f32x2 FMA、固定归约顺序，
   输出 RN_BF16。每一输入行独立执行，行数不改变归约；不声称与
   上游 Marlin 的中间舍入逐位等价，也不声称草稿 logits 等于原 BF16。
4. 目标验证 head 保持 BF16，所有最终 token 必须仍通过现有验证。
   HTTP 质量、完整输出及 greedy 对照独立检查；不能凭“目标没改”
   忽略近 tie、实际形状、状态或输出差异。

## 资源合同

形状 248320×2560：BF16 目标 head 1271398400 bytes（1212.5 MiB）
继续驻留；量化草稿 packed 317849600 + scale 39731200 =
357580800 bytes（341.015625 MiB），另 4-byte host 全局系数。
加载峰值另含 4096-byte device 最大值暂存和同尺寸 host 数组；融合
点积仅每 CTA 5120-byte shared activation，不分配全词表 BF16 副本。
现有 BF16 子集 head 连 ID 为 320.25 MiB，因此新方案不宣称降低
整个模型 RAM，也不宣称比子集 head 更省内存。预算器必须加入这些
字节；实际整机资源仍要记录，公式不代替实测。

## 冻结首轮关卡与出口

必要构建不算测试。第一项测试是 tools/evalscope 的真实 HTTP E2E，
不先跑单测、kernel 微测、bench 或 profile。GPU 阶段完整驱动持有
原共享锁，端口 8762；发生竞争/监控失败为 ENV_ABORT，仅清理自有进程。

首轮使用既有 snap-records 的 1K 固定输入，每臂三次、256 输出上限；
四臂固定顺序：已接受 plain、已接受 full MTP、候选 NVFP4、已接受
BF16 subset。每臂再跑同一既有 11 题质量集；上限 4 次模型加载、
56 次生成 HTTP、1800 秒整阶段，不补样寻找有利结果。确切输入、
命令、模型/ELF/工具身份在执行前写入机器可读 plan。
四臂统一 `--startup-plan off`，max_seq=1/max_prefill=8192/max_len=208896；
量化加载尚未接入预留计划，不能让父侧预留、候选侧自动回退造成
启动分配路径混杂。进程CUDA配额和内存预算保持启用，退出检查配额
live/charged/pending/unknown 全归零；此关卡不验收 auto/required 组合。

- 输出/质量失败：保留首次失败，停止性能接受；只有具体缺陷才做
  一次有界定位，未授权的额外模型请求不自动追加。
- 无有效正收益或明显劣化：本轮 NO_GO，不进入完整矩阵，不部署。
- 可行性继续条件：候选完整 HTTP/质量通过，1K decode 中位数相对
  full MTP 至少改善 5%，并披露相对 subset 的原值。5% 是本候选
  新内核维护成本的事前投入目标，不是可接受回退容差。
- 通过只记 GO_FOR_FULL_EVALUATION；不能写成优化已验收。统计测量
  能力未建立，单启动三请求不证明非劣；完整接受另需适用五档逐档
  TTFT/decode、plain/full/subset 对照与保护项，以及下述直接合同。
- 直接合同在 HTTP 关卡之后：量化编码/舍入/零与非有限、独立高精度
  反量化点积、M=1/2/4 行不变性、越界 canary、失败清理；完整矩阵
  前另外冻结真实 MTP 接受/拒绝和恢复覆盖。未跑项明确保留。

缓存正确性对照仅核对现有 scoring/state/cache 的身份与证据，不因
vLLM 修复标题重做已通过项目；新精度组合的状态验收不能继承旧结论。
阶段提交、推送保留研究入口，默认 pair 和旧客户端固定包保持。
