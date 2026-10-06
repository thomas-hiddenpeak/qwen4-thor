# Mirror保留研究证据 — 2026-10-06

本目录只支持[本阶段报告](../../OFFLOAD_MIRROR_RETENTION_2026-10-06.md)的有限
离线结论。工具a2fdcfd、固定8请求、45项新合同；零新增HTTP/模型/runtime。
结果提名一个未来候选，未接受性能，旧NO_GO和整体54GB未知保持。

- `scope-plan.json`、`source-contract.json`：冻结问题、来源和未知时序。
- `validation-*.json`、合同日志及静审：26核心/19runner首次45通过。
- `execution-plan.json`、`analysis-admission.json`：固定来源与一次执行。
- `retention-summary.json`：8请求、24端点与192逐层区间的精确投影。
- `result-independent-review.json`：独立算术及摘要复核；其余result文件
  提供紧凑重算、退出复核和辅助零键比较的首次失败/限定修正。
- `final-protection*.json`及退出记录：MAIN/旧工作树、模型元数据与自有
  进程有限保护；静审基准绑定缺口在执行前修复。entry/model-entry是
  基准元数据，不包含模型payload或MAIN原七项改动的patch。
- `harness/`：实际输入/摘要生成、独审和保护脚本的精确副本。脚本绑定
  原始R布局与旧证据路径；归档目录不冒充可独立执行的完整数据包。

`archive-manifest.json`列出逐份原绝对路径、大小及SHA。除本说明和清单，
文件均为原件逐字节副本；生成脚本不替代结果，紧凑投影不替代完整原件。

原始R：`/home/rm01/models/dev/qwen4-thor/.q4t-work/offload-mirror-retention-20261006`。
31.55MB retention-analysis、7.87MB decode-plans、3.94MB retention-inputs
留本地，摘要/执行账/独审记录绑定其摘要。原trace与旧资源原件不重复归档，
也不为交付重解析。所有位置与反转均保留，八请求不是独立性能重复。

潜在支持/容量不等于真实ring保留或可避免READ。共同区间为保守外包络；
入口混合占位不证明实际挤占。模型只核228项元数据及config/index摘要，
不读权重payload，元数据一致不证明逐字节相同或不存在瞬时写入。
reference只核tracked Git状态；原trace以SHA与未变stat复用，不排除
同stat替换。旧进程退出记录只覆盖自有组，不声明全机GPU空闲。

最终文档提交之后的有限Git/远端交付复核保留本地R/
`delivery-independent-review.json`，不自引用进本提交；已过分析与保护不重跑。
