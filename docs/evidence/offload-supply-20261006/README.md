# Offload decode 供给诊断证据归档

本目录记录 2026-10-06 已闭合的离线供给分析与窄观测阶段。原件保留在
`/home/rm01/models/dev/qwen4-thor/.q4t-work/offload-supply-20261006/`。
[archive-manifest.json](archive-manifest.json) 逐项记录原件绝对路径、归档相对路径、
SHA256 和字节数；精确副本保持原字节。此前已有的九份历史证据未改写。

## 身份与验收范围

实际运行时源码为 `75d26ff22391a4fd7549b1c39bf2b9c0f2cbbf89`，binary SHA256 为
`72a321a422facac605c13740ac8eb78e253056a6719c88bea71e3bbae9ef843f`。
执行计划 SHA256 为
`907111ee1b731521c286e90df3718457c11c25c340a3f66fabc90a03a7584b24`。
文档归档后的提交身份与上述已运行身份分开记录。

共五个服务、27 个 HTTP 请求：首先 quality11，再按 S off/on、L on/off
顺序运行四组诊断，每组四个请求。诊断固定输出 256；quality 输出长度按各题
既有合同核对。四组旧 phase/cache/timing/router 观测均开启，仅新供给 observer
开关不同。没有增加连接探测请求。

本 Goal 新增合同共 87 项：离线阶段 41 项，以及 quality11 通过后首批运行的
23 项 observer host 合同与 23 项协议合同。已有 55 项 GPU 回放合同按未变身份
复用，不计作这 87 项新合同。所有执行、分析、独立审查与资源审计已闭合。
[final-protection.json](final-protection.json) 为首次最终保护 PASS 原件；其运行中
自收尾豁免由另存的 [final-protection-01-exit.json](final-protection-01-exit.json)
补足，记录 rc0、failure null、cleanup complete。

## 文件范围与读取方式

- [observer-delivery-summary.json](observer-delivery-summary.json) 是从已闭合分析
  与资源证据生成的小型交付投影，保留来源 SHA 和结果边界。
- [observer-result-resource-review-extract.json](observer-result-resource-review-extract.json)
  是已有约 628 KB 资源审查摘录的精确副本。约 117 MB 的完整资源报告仍留本地；
  不把摘录当成完整原始采样流。
- [final-protection.json](final-protection.json) 约 503 KB，低于 5 MiB 归档上限，
  因而原样复制，本次没有另造保护报告的紧凑投影。
- 其他 JSON 保存范围冻结、执行准入、独立审查、构建收尾与合同结论。完整来源
  账指向原 R 布局，未递归复制账中所有对象。
- `harness/` 内 13 份 Python 文件为当时 R 层执行/审计脚本的精确副本。
  它们记录原始目录布局与参数约定，不是迁移到此目录后可直接使用的通用 CLI；
  不应从归档路径执行这些脚本。

本目录不包含私有 prompt 文本、HTTP group decision 全记录、请求/响应原文、
trace 二进制、原始资源采样、大型资源报告或模型 payload。归档只复制与核对
上述小型证据，没有重新运行测试、推理、trace 分析或资源分析。

## 结论边界

新 observer-on 诊断的八个请求实际 decode 中确认 22 次直接 READ 损失，
其软件 READ 总数为 50,365；这是这组观测中的事件频率，不是全局反事实节省
上限或加速比。quality11 的另三次 decode 见证单独保留，不混入诊断频率。
主 k1 的 468 次 mirror 缺口可写为 470 次入口机会差减去 2 次直接损失差；
S/L 的直接损失分别为 6/4，因此本机制不能解释主要缺口。各位置与方向反转
均保留在结果投影，旧 16 请求不与新请求混池。

只能提出一个未来 guard 候选；本阶段没有实施优化或接受性能收益。Observer
可能影响 worker 顺序，off/on 单服务差异不能当作纯观测开销或稳定性能回退。
累积及嵌套计时不能互斥分解 SSD/H2D 等待，软件 READ 不等于物理 SSD 流量。

资源证据包含五个服务生命周期、27 个 HTTP 时间窗和 17 个客户端 I/O 外窗。
quality11 共用一个 I/O 外窗，不能逐题分摊读取。PID、cgroup、设备计数分别
保留；PSI、live file-cache 峰及平台计数缺口仍为未知。memory.current 采样峰
与 memory.peak 计费峰分别对照 16 GiB，NVIDIA/进程/cgroup 峰不可相加。
整体物理 RAM 54GB 继续为 `INDETERMINATE`；原性能 NO_GO、默认关闭、GEMM/
精度以及 MTP/Phase D 约束保持原验收结论。
