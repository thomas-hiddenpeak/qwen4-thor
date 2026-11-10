# Responses原生文件准备真实业务验收

2026-11-10。PASS，严格连续GPU租约70.847秒，1模型启动/4业务请求/
6模型步骤，236次有界控制查询。实际Node SDK7.47.0、JSON/SSE及后台
任务；无性能主张，不跑完整质量或性能矩阵，未修改默认发行。

| 请求 | 实际任务 | 模型步骤 | input/output tokens |
|---|---|---|---|
| initial | 原CSV alpha=42，真实摘要后回答value=42 | 2 | 856/176 |
| follow | previous冻结摘要，SSE回答beta=7 | 1 | 512/6 |
| background | 后台原CSV摘要后回答value=42 | 2 | 856/176 |
| Chat | 同一PID普通MTP算术回答42 | 1 | 见原始wire/metrics |

实际记录验证：准备摘要不以CPU假回执代替；原历史复用不再次生成；
后台所有权正常，SSE序号/created/completed及done items一致。4次RPM
与全部准备+主回答的实际token用量一致，无剩余配额/grammar/request
占用，普通Chat同PID成功。此组SSE使用已冻结的previous摘要，后续
组合仍应覆盖新表格的SSE准备；不能据此宣称全部文件API已经验收。

候选2e896950c16e0e3344e3586fa25054bd9e8138da0b8c7e8f1be37316e570a4a8，
来源提交68e08a27。模型正常exit0，自有进程无残留，端口释放，guard
COMPLETED。独立复核源码/二进制和4个默认文件身份保持，锁可获取。
原始请求、SDK结果、wire、模型log、metrics、租约与清理回执保留。

继续Chat文件/preflight、Responses input_tokens及共同配置/媒体组合、
实际业务与格式兼容回退，然后Batch和全部master任务。未发行/未合main，
此链通过不等于A09.5或总目标完成，默认Uploads仍STOPPED。
