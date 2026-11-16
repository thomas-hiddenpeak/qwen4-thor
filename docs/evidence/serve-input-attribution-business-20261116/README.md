# 输入归属、实际档位和Responses默认：实际业务接受

2026-11-16，候选7a23cb30累计2启动/7生成，覆盖六项不同业务；两个
严格租约分别56.882/41.575秒，NVML
54/40项正常。运行时及二进制未改，第一轮四条Chat
JSON/SSE/实际档位/归属/一次文档工具/SHA续答按精确身份复用。
第二轮Responses省略与null实际medium、回答42、JSON/SSE事件/usage、
存储GET原始快照完全一致，核心请求计数2/2成功。

两次驱动失败均保留，不修改原始result/guard：第一次GET遗漏include
导致logprobs投影；补验第二次GET的SDK附加output_text，流式原始结果
没有该客户端便利字段。现在显式include，并分别严格校验实际wire
相等、SDK派生text正确；一项CPU正/负控和实际捕获重验证通过，0新增
GPU。原始wire没有变化，未通过删除字段来放松原始响应判定。

第一项对象HTTP DELETE已过。第二项因脚本提前结束，使用相同编译
来源的生产PlanResponsesRead/ReadResponsesState在CPU上读取真实自有
对象并删除，GET/DELETE/再次GET为200/200/404；原状态私有备份保留。
这不是宣称又跑过HTTP DELETE。自有模型/端口/锁清理通过，失败现场
私有fixture数据按证据保留，默认rev158未改。

已冻结后续主轻2启动/10请求，以及精确保留/正式包版本恢复短验。
未改native/control/client证据复用；无全模型质量、性能或长稳结论。
继续可回退发行和全部master，阶段通过不等于完整兼容。
