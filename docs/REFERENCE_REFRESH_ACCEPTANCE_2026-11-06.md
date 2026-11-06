# 参考增量 R1/R2 有界短验

用户2026-11-06恢复此前暂缓的短验。任务是配置兼容缺陷修复和观察
有效性验收，非性能优化；没有新算法、精度或默认head切换。
开发来源见[开发准备](REFERENCE_REFRESH_DEVELOPMENT_2026-11-06.md)。

## 冻结范围

- CPU：7项配置用例（含真实只读配置）和7项QSA事件合同；来源清单
  及其3项变异检查。当前14/14通过、零跳过；来源38字段/10路由/
  112锚点通过。fake CUDA仅证明事件管理，不证明设备行为。
- 全量独立CMake构建，CUDA13.3/SM110a/C++23，XGrammar固定原依赖；
  配置和构建零警告。候选协议为
  `55da359517f9f4a2109afc6e242bd6d74086eee44f239c5adf6aadb75188500d`。
- GPU最多6启动、12 HTTP请求、1200秒；驱动工作窗口1140秒，剩余
  60秒留给清理。整个阶段同一租约，不重试/补样，不做长稳。
  ready从服务日志读取，不发健康探针、capabilities或预热请求。

| 顺序 | 程序/观察 | head | 请求 |
|---|---|---|---|
| 0/1 | 接受默认c1bdb0a7 | 完整/已验BF16 subset | 各1K、45K |
| 2 | 新候选OFF | 完整 | 1K、45K、真实资料工具两跳 |
| 3 | 新候选OFF | subset | 1K、45K |
| 4/5 | 新候选ON | 完整/subset | 各45K |

全部使用单流、MTP k3、max_prefill8192、max_len208896、startup-plan
off；文本请求各256输出，沿用已冻结输入第一条，不产生新随机输入。
工具两跳各256输出上限：named read_project_record一次读取真实
docs/MODEL.md第1–24行；回填实际结果后tool_choice=none回答架构名称
及原引用。记录原始SSE、调用参数、真实返回、历史与最终答案。
该直接客户端检查不等于托管旧包已迁移到新协议。

ON只设Q4T_LAYER_SUBTIMING=1与Q4T_QSA_SUBTIMING=1；其他研究/观察
开关清除。ON/OFF墙钟差异是整组观察开销，不能孤立归于新增事件。
同head所有可比输出、token数、finish及MTP接受直方图应一致。
45K已知prefill应为5×8192+4084，每行12个QSA层、五段完整有效；
五段和不得超过对应full_core（考虑打印精度）。缺失/错误拒绝，
CUDA event区间不是纯kernel时间，嵌套项不与父项重复相加。

## 执行与证据

[冻结准备](../tools/reference_refresh/prepare.py)、
[驱动](../tools/reference_refresh/stage.py)、
[离线复核](../tools/reference_refresh/analyze.py)复用现有EvalScope、
进程归属、资源采样和最终屏障。GPU阶段必须由main的绝对路径
tools/evalscope/gpu_exclusive.py启动，plan与驱动参数也用绝对路径。
原件在有效研究工作区的`.q4t-work/reference-refresh-acceptance-20261106/`。
CPU/构建记录为build-cpu.json；冻结后plan绑定输入/二进制/模型元数据，
结束后只读复核SQLite、SSE、服务日志、原件封印、监控与外层租约。

默认c1bdb0a7及sidecar0efc2c0d保持；阶段不自动放行旧托管包协议，
不整体合并研究分支。GPU结果、资源释放与下一候选取舍待本次运行后
独立追加结果文档；本清单冻结后不改写为结果，以保留事前边界。
