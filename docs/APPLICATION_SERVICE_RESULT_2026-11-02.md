# 应用服务首批交付结果

**DELIVERED**。能力盘点及首批协议 / 错误 / 真实项目查询已交付，
不是 OpenAI 全接口或通用生产平台的完整兼容声明。
范围见[主矩阵](APPLICATION_SERVICE.md)、
[32项 vLLM 对照与遗漏](VLLM_SERVICE_GAPS_2026-11-02.md)。

## 当前身份与变化

- 默认程序：`c3772d111088f8dbd7f07b6b482367da05c50112dd6502f047cd4627da2ea23a`。
- 发布 sidecar：`c80f1f14b1abe39a24e45a9fe37a0fa4e9b8a5fd47a4537799f816dca852e5a1`。
- 协议身份：`b3ea31054e08629acd711aa85f0386e509d335e597f509c4662ed706b749a1b2`。
- 运行时 / 初始应用提交 f9e8b96，验收驱动 2fe6cdc，预算修正
  5340cc4，监控与收尾工具 8922c44；均在 codex/main-reaudit-fixes。
- 164 项运行时清单中仅 6 项服务代码变化；模型 / 数值代码未改。
  默认仍为 full head、MTP k3，缓存与 suffix 仍需显式启用。

首批实现：max_completion_tokens 与 legacy max_tokens 共用生成预算；
双非空字段拒绝歧义。HTTP/SSE 错误统一 message/type/code/param，
服务错误不再冒充参数错误；错误终态不发成功 finish / usage。
SSE 各块 created 保持同一请求身份。未实现的嵌套消息字段非空时
明确拒绝，避免静默丢失 name/refusal/audio 等语义。

真实应用入口为 `tools/examples/project_query.py`。工具只读 docs 下
Markdown/JSON 及 build/q4t.release.json，禁止路径越界 / 符号链接 /
特殊文件，限制文件、行数、结果与轮次。返回路径、行号、文件 SHA256，
答案引用必须属于已返回来源。预期读取错误作为工具结果回填；长度
截断、传输失败或预算耗尽时保留记录，不执行不完整调用，不自动重放。

## 验证结果

| 项目 | 结果 / 边界 |
|---|---|
| 必要编译 | server / Thor host 零警告；独立链接候选，验收前未覆盖默认 |
| CPU 合同 | 138 host + 22 客户端 + 13 恢复 + 9 项目工具 + 6 回退 + 3 SDK mock + 3 收尾失败门，共 194 项通过 |
| SDK | OpenAI Python 3.42.1，真实 loopback，strict response validation，max_retries=0；未调用 OpenAI 云端 |
| 标准接入 | models、JSON/SSE 文本、同流 created、1-token 上限、function tools 完整往返通过 |
| 负例 | 8 参数 / 消息拒绝 + 1 未实现路由，共 9 次；SDK BadRequestError 与稳定错误字段可读 |
| 当前发布查询 | 2 轮，实际读到当时已安装 830694ce 完整哈希，引用 build/q4t.release.json:L1-L20；这是替换前事实 |
| 历史验收查询 | 3 轮，实际读历史交付文档两段，给出 831907 与来源行号 |
| 不存在资料 | 2 轮，真实 record_not_found 回填，最终说明无法核实；没有虚构记录 |
| MTP 工具往返 | 2 次 SDK 请求，JSON function call → 真实文件结果 → SSE 最终哈希，通过 |
| 安装健康 | 1 启动 / 1 SDK SSE，710003、stop、usage、模型资源归零；单槽 8192 配置，不外推并发 / subset 新验收 |
| 总计 | 含首轮失败累计 18 生成 / 9 负例 / 4 模型启动；无重编 / 性能扩样 |
| 最终收尾 | 0 新模型请求 / 启动；同身份健康复用，重新安装已知 pair，前后 NVML 与共享 guard 通过 |

应用预算修正后只重验受影响的 9 项查询、6 项回退、3 项 SDK 驱动。
这些重复执行不重复计入 194 个合同；同身份的其他结果复用。
[8922c44 公共 host 检查通过](https://github.com/thomas-hiddenpeak/qwen4-thor/actions/runs/41409900524)。
本轮为业务推进，不给出性能优化收益或五档统计非劣结论。

## 两次失败与证据边界

1. 初次历史查询在 3 轮内持续检索，未给最终答案；预算门阻止继续
   执行工具。8 次生成后停止，默认未替换。修正为向模型传递剩余
   预算 / 最后回答提示，历史查询上限 4；重验实际 3 轮完成。
2. 修正业务及安装健康通过后，末尾独立 NVML worker 使用 SDK venv，
   缺 pynvml，自动恢复旧 pair。按 **ENV_ABORT_FINAL_MONITOR_IMPORT**
   记录，原共享 guard 的 **COMMAND_FAILED** 原文保留。共享 guard
   自身的系统 NVML 监控正常，不能因此抹去额外检查失败。
   固定系统解释器并前移 preflight；实际 SDK 环境调用修正版返回
   OK/空进程。另一个完整租约仅完成重新安装和元数据修正，最终
   guard **COMPLETED** 后 finalize。

原始包 / 现场均未覆盖。第一轮 3 启动 / 16 生成计划因具体失败修正
明确调整为最多 4 启动 / 20 生成，负例上限仍 12；从首次 07:41:21
UTC 开始到 finalize 为 1245.70 秒（20分45.70秒），原 30 分钟截止
没有重置。监控失败不能记作没有干扰或全部通过。

## 应用质量仍有的边界

这三项任务只证明对应事实、工具关联和来源闭环。引用校验验证来源
范围，不能自动证明每句话都被来源支持；提示词也不能强制模型停止
检索。首次轮次失败正是这种限制，应用预算门仍必须保留。
历史查询答案的附加服务状态说明仍可能混用文档时点与当前时点；
实时 GPU / 服务状态应来自实时工具，不能从发布或验收文档推断。
本轮最终 GPU 状态由独立 NVML / 共享锁核查确认，不依赖模型答案。

搜索只扫描有界 Markdown 集合，truncated / skipped 明确返回，未命中
不是全项目不存在的证明。此次真实模型任务验证 read_project_record，
search_project_docs 的字面查询、界限、引用仅经 CPU 验证，不能宣称
已经完成模型自主广泛检索、跨多文档事实核查或长期业务稳定性验收。

依然不支持 required/named/none 工具保证、parallel=false、JSON Schema
约束解码、developer / name 语义、Responses、持久幂等任务等；reasoning_content
与当前 vLLM reasoning 不同。首批 SDK 验证也不扩张到其他 SDK/框架。
后续优先级与具体组合验收见 vLLM 对照清单。

## 证据与回退

[275份原始记录与封条](evidence/application-service-20261102/README.md)
保留失败与成功请求、SDK 解码 / 原始 wire、工具结果、恢复判定、进程
日志、CPU 合同、构建配置、源代码 bundle、各阶段计划和包封条。
7 份二进制副本仍在本机工作目录，归档只记录身份，不纳入 Git。

完整收尾包：`.q4t-work/application-service-final-20261102/package/`；
旧默认 830694ce/0f28e0f0 pair 保留。该包依赖 correction 与 original
完整包的封条；不清理其路径或旧发布 cache。源码 bundle 为有锚点
的增量包，分别依赖 460ffd0 / 5340cc4，不是完整仓库镜像。

需要回退时，在保留对应工具身份的工作区执行（新输出目录）：

```bash
/usr/bin/python3 -B /home/rm01/models/dev/qwen4-thor/tools/evalscope/gpu_exclusive.py \
  --output /home/rm01/models/dev/qwen4-thor/.q4t-work/application-service-rollback/lease -- \
  /usr/bin/python3 -B /home/rm01/models/dev/qwen4-thor/tools/application_service/finish_delivery.py \
  rollback --output /home/rm01/models/dev/qwen4-thor/.q4t-work/application-service-rollback
```

写前核对完整已知 pair / 源包 / 租约；未知身份拒绝。两文件分别原子
替换，持久恢复标记覆盖中途失败，不声明断电事务。本阶段 finalize
时 NVML 计算进程为空、共享锁可获取、自有恢复标记清除；没有启动
常驻 vLLM / Q4T 服务，58001 代理未操作。

交付后的独立变化：16:09:06 CST，外部服务恢复进程创建共享恢复
标记并持锁；16:11:28 只读观察 GPU PID 1187183、锁不可获取，标记
owner PID 1186282。该进程始于本阶段 finalize 之后，原完成证据不
重写为当前空闲状态。未停止它、未删除外来标记，也未继续任何模型
或 CPU 测试。新 GPU 验证须等该流程释放租约。
见[交付后设备观察](evidence/application-service-post-delivery-20261102/README.md)。
