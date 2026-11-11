# Batch三文本端点的可回退交付

2026-11-11。15条随包检查/发布命令全部首次通过，命令19.447秒、完整驱动21.989秒，0新增模型/生成/GPU。默认引擎f23be45c、元数据226c812b、原生包25061a30、generation223a915c，publication revision44，STOPPED。原API Key、显式客户端/入口容量、对象存储配额和用量账本逐步哈希保持。

先封存并登记实际跨版本业务通过的fc69f823独立引擎312678bc，包0146e62c、generation773b6eb1、revision42，再发布候选。回退包含新Batch持久记录、v2在线/Batch双池账本、原后台父关联和永久容量失败读取格式。旧file-inputs包仍归档，但不能作为新Batch/配额格式的回退；不能删除业务数据或清零额度来降级。原策略/容量适用于两个版本，所有版本源码及二进制分别记录。

[真实业务](../serve-batch-business-20261111/README.md)99.300秒、2启动/8生成/8步骤，Chat/Completions/Responses Batch、实际文档工具SSE、结构化失败及运行中取消实际计量、原件和双池账本连续均过。原生入口路由/独立观察取消通道复用已有41个官方SDK核心/TLS-prefix实际HTTP请求CPU证据；仅控制脚本usage_policy改为接受双池v2快照和跨启动过期，Guardian/CUDA探针二进制保持。声明保留实际验收配置，但默认仍text-MTP/output256、对象64/256MiB，不复制短验1536输出/256/512MiB对象参数。

三类文本Batch及其恢复/取消/配额已可用。其余五类Batch后端、A10多模态接口和全部Serve总目标仍需实现；当前不声称全面OpenAI兼容、完整性能/质量矩阵或长稳完成，未合main。接着实现独立moderations及Batch接入；此交付是阶段边界，不是总任务终点。
