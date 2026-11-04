# 工具控制与结构化输出交付结果

2026-11-02：有界业务验收与安装健康通过，默认程序 **957211c7** /
sidecar **9eba5472**，源码锚点 **6bf61b7**。固定 XGrammar 后端已启用，
普通请求仍默认 full head / MTP k3；受约束请求使用普通掩码解码。
没有启动常驻服务。实现与有限 schema profile 见
[开发合同](STRUCTURED_OUTPUT_DEVELOPMENT_2026-11-02.md)。

## 本次完成

`tool_choice=none/required/named`、`parallel_tool_calls=false`、strict
工具参数及 `response_format=json_object/json_schema` 均经过真实
OpenAI Python 3.42.1 HTTP 检查。约束作用于 token 选择，最终再完整
校验；无效或截断响应不发布可执行工具、成功 finish 或 usage。

真实项目闭环只执行一次 `read_project_record`，读取本仓库
`docs/MODEL.md` 第 1～8 行，按 ID 回填后在 none 模式回答
**Qwen3.8-Flash-Next (qwen4_exp)**，引用 **[docs/MODEL.md:L1-L8]**。
核对实际文件 SHA256 与工具记录一致。这个成功覆盖限定读取场景，
不代表任意资料检索或逐句事实验证。

## 验证与预算

| 检查 | 结果 / 范围 |
|---|---|
| CPU host，后端开启 / 关闭 | 145 / 140 项全部通过；两组有重叠，不相加为独立合同 |
| Thor 发布门 | 106 项通过，包含真实 tokenizer；另执行 5 项后端要求列表 |
| 项目客户端 | 23 项通过，包含能力扩展兼容 |
| 新阶段驱动 / 复用回退代码 | 6 / 6 项通过，覆盖错误发布门、真实文件回填、预算及无锁拒绝 |
| 构建 | Thor runner / Thor host / 两种公共 host 均零警告 |
| 业务 HTTP | 16 次生成、12 个请求拒绝、2 次启动，116.49 秒 |
| 安装健康 | 2 次生成、1 次启动，39.44 秒；普通 MTP 与 JSON Schema SSE |
| 总 GPU 阶段 | 18 生成 / 12 拒绝 / 3 启动，156.42 秒；未扩样，30 分钟预算内 |
| 环境与退出 | guard COMPLETED，子进程 0；3 次资源 finalized PASS、未交接 / 存活分配均归零 |

16 次业务生成包含两次故意 max_completion_tokens=1 的截断请求，
分别验证 JSON 502 与 SSE error + DONE。其他场景覆盖 none、required
SSE、指定工具、auto + 单调用、嵌套 array/object/null/中文参数、
json_object、JSON Schema 两种传输与 thinking、实际文档回填，以及
MTP 服务下普通文本 / legacy auto 工具 / 新约束请求。

12 个负例均为 400：未知指定名称、required 无工具、parallel 类型错误、
raw 解析组合、JSON 与 tools 冲突、session / prefix cache、媒体、
不支持的 schema 关键字、strict 缺 required、重复工具、根数组 schema。
日志均在验证阶段结束，未进入 tokenization / prefill；未访问媒体 URL。

最初 CMake 配置发现上游 normal variable 覆盖关闭 Python binding 的
cache 选项，导致查找不存在的 tvm_ffi。已改用独立构建目录内受控
config.cmake，固定 C++ 静态依赖；原失败日志保留。随后配置与构建
通过。本轮 CPU / GPU / 业务没有失败重试，没有 ENV_ABORT。

## 时延、兼容和未覆盖边界

本轮是业务功能交付，没有五档性能优化或非劣接受结论。仅记录本次
输入的实际值：受约束成功请求约 0.78～4.07 秒；语法准备阶段约
158～199 ms，包含词表解码、tokenizer 信息与 grammar 编译，不能
解释为纯编译时长或尾延迟上界。完整原值在审计记录中。

- 受约束请求不使用 MTP / packed decode，不接 session / prefix
  cache 或媒体。本次实测为 full head、max_seq=1、上下文容量 8192；
  没有扩张到并发混合请求、subset head 或长上下文约束验收。
- 受约束 SSE 的正文、推理及工具 payload 在完整验证后集中发布，
  可提前发 role；尚非逐 token 的结构化流。这会增加可用首字等待。
- 仅保证 `q4t-json-schema-v1` 明确子集。未知 keyword 拒绝；工具
  业务参数、读路径和授权仍由应用验证，结构正确不等于答案正确。
- 完成编译后的 grammar 限 64 MiB，不是编译峰值内存上限。当前
  不能中途抢占编译；本次未隔离测量编译峰值 RSS，也未做不可信
  schema 压力测试或生产多租户资源隔离验收。
- 数值 / 模型代码与前默认一致：旧运行时清单中仅 7 个服务文件
  修改，新增结构化服务文件另入清单。复用原数值证据；本次新业务
  结果与历史五档性能 / 缓存 / subset 证据分别保存。

## 服务释放、证据与回退

用户先授权停止 vLLM，再授权测试。17:12 CST 正常停监督器，其
StopRequested 退出使 unit 显示 failed，但自身记录确认容器清理成功、
恢复标记删除。独立检查 GPU 计算进程为空、锁可取；可用 RAM 从约
23 GiB 回升 117 GiB。保留镜像 / 模型 / 恢复资料，未清除全局文件缓存。
17:27:33 CST 安装收尾后再次确认 GPU 空、锁释放、恢复标记均无，
可用 RAM 约 114 GiB；vLLM 与 q4t 均未重启为常驻服务。

- [原始记录封条](evidence/structured-output-20261102/sealed-files.json)：
  151 个文件，含请求、SDK 结果、原始 wire、服务日志、guard、CPU /
  构建与停服记录。二进制仅保留于 `.q4t-work/`，位置与身份另记录。
- [结果审计](evidence/structured-output-20261102/execution/audit.json)、
  [冻结计划](evidence/structured-output-20261102/plan.json)、
  [旧数值证据复用边界](evidence/structured-output-20261102/numerical-reuse.json)。
- 默认 binary SHA256：
  `957211c7807a5189e5c124b13d52c4fe5a95a4213dfbce087007783cb479d5fc`；
  sidecar SHA256：
  `9eba5472f952372464bf0a903c63074b2e73cd51321d0d19a0601f7f05426fad`。
- 旧 c3772d11 / c80f1f14、真实 CMake cache、源码 bundle、候选与发布
  绑定均在 `.q4t-work/structured-control-20261102/package/` 和
  `execution/` 保留；上阶段回退链继续保留。
- XGrammar / DLPack / picojson 的原始许可与归属声明保留于
  [依赖 notices](../third_party/licenses/structured_output/README.md)，本机
  分发副本位于 `build/licenses/structured_output/`，导出程序时一同保留。

仅在需要回退、现场满足租约准入时，运行独立入口；该入口核验冻结
源码及旧 / 新 pair 身份，遇到其他身份拒绝覆盖，不进行模型请求：

```bash
/usr/bin/python3 -B /home/rm01/models/dev/qwen4-thor/tools/evalscope/gpu_exclusive.py \
  --output /home/rm01/models/dev/qwen4-thor/.q4t-work/structured-control-rollback-20261102/lease -- \
  /usr/bin/python3 -B /home/rm01/models/dev/qwen4-thor/tools/structured_output/stage.py rollback \
  --output /home/rm01/models/dev/qwen4-thor/.q4t-work/structured-control-rollback-20261102
```

源码继续演进后不能绕过该身份门；保留的 bundle / 已知 pair 是
后续协调恢复的依据，不自动 stash、覆盖别的阶段或清除外来锁。
