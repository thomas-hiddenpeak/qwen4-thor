# SRV-08 合同与固定 JavaScript SDK

本目录针对已交付文本服务，不是另一个模型 runner。冻结范围、预算和
尚未完成项见[阶段合同](../../docs/SERVE_PROTOCOL_2026-11-03.md)。
已按限定范围完成真实 HTTP 验收：10生成尝试/16拒绝/1启动，包含预期
截断与新凭据恢复；见[结果及边界](../../docs/SERVE_PROTOCOL_RESULT_2026-11-03.md)。
默认 ELF / sidecar 保持 SRV-07 身份，新增客户端有独立版本绑定记录。

## 机器合同

- [完整输入结构](../../docs/serve-chat-request-v1.schema.json)：38 顶层字段，
  嵌套消息、工具与有限 schema，显式 null、默认和组合拒绝。
- [必需语义清单](../../docs/serve-input-semantics-v1.json)：精确 JSON 数字、
  UTF-8 字节、工具历史完整批次、有限 schema 语义和源码身份。
- [OpenAPI 3.1](../../docs/serve-openapi-v1.json)：10 路由、请求及已有响应
  投影。capabilities 是可扩展发现对象，不宣称完整响应字段静态 schema。

标准 JSON Schema 不能独自证明准入；`x-q4t-*` 是有意标明的额外规则。
`check_contract.py` 的受限扩展检查精确数值和字节，其余强制语义由生产
C++ 探针覆盖。探针不分词、不编译 grammar、不加载模型；动态预算使用
真实 `/v1/chat/preflight`，预检也不预留槽位或保证生成成功。

```bash
python3 -B tools/serve_protocol/build_contract.py --check
python3 -B tools/serve_protocol/build_probe.py \
  --output .q4t-work/serve-protocol-20261103/new-probe
python3 -B tools/serve_protocol/check_contract.py \
  --probe .q4t-work/serve-protocol-20261103/new-probe/contract_probe \
  --output .q4t-work/serve-protocol-20261103/new-contract-check
```

已有证据身份不变时直接复用；上述命令不是要求每次重新验收。

## JavaScript 接入

实际验证固定 Node 24.18.0、官方 `openai` 7.47.0。SDK 只装入工作目录，
锁文件随源码保存，禁用安装脚本；不从全局模块或环境中的 API key 回退。

```bash
mkdir -p .q4t-work/js-sdk
cp tools/serve_protocol/package*.json .q4t-work/js-sdk/
npm ci --prefix .q4t-work/js-sdk --ignore-scripts --no-audit --no-fund
```

[`chat_client.mjs`](../examples/js/chat_client.mjs) 接收显式注入的 `OpenAI`
类和 `/v1` base URL，提供 `models()`、`capabilities()`、`chat()`。
固定 SDK 的解析结果与原始 wire 再次对照：响应 ID/model/created、usage、
唯一终态、DONE 和完整 EOF；出现额外尾帧、缺少 usage 或不完整参数即拒绝。
SSE 在内部逐块消费，但返回完整事务；目前没有面向 UI 的 delta 回调。

使用 `makeRequest()` 创建新 request_id/cancel_token；`prepareToolCalls()`
完整校验所有函数及参数后才产生执行计划。`appendToolResults()` 要求
每个 ID 恰有一个结果，并按调用顺序回填。验证器必须纯函数，返回
undefined 或抛错，不能在验证时执行工具。只有完整 stop 才可 appendFinal。

客户端边界为响应 8MiB、单 SSE 事件 1MiB、128 个工具调用、JSON 深度64/
65536节点、JS 安全整数。拒绝重复 JSON 键、非有限值、非法 Unicode 和
整数精度丢失。JS 数学整数可使用精确指数/小数拼写；现有 Python 业务
read 参数还要求原 JSON 解析为 int，所以桥接层会更严格拒绝 `1.0` 等
拼写，不会在两端验证分歧时执行工具。自动重试始终关闭，拒绝重定向。

## 项目查询与恢复

`query_project.py` 将实际 Node SDK 传输接入既有 Python 只读资料执行器。
Node 不执行工具；原始字节通过独立继承管道即时送回日志 sink，子进程
退出也保留已经收到的前缀。通用 sink 的 flush/fsync 由调用者负责；本轮
验收逐块 fsync。已有 run-started/submitted/plan/tool-started/result/history
原子持久记录格式不变，只读恢复检查可直接使用。

```bash
# 对已由本阶段持锁启动的本机服务执行；开发验收必须仍在同一 GPU lease 内。
python3 -B tools/serve_protocol/query_project.py \
  --base-url http://127.0.0.1:18099 \
  --sdk-directory .q4t-work/js-sdk \
  --directory .q4t-work/new-js-query \
  --question '先读取 docs/MODEL.md，再回答 architectures 字段并给出引用。'
python3 -B tools/examples/tool_recovery.py --directory .q4t-work/new-js-query
```

资料不存在是业务结果，模型必须如实报告错误；协议完成不代表资料查询
成功。截断/断连、SDK错误或进程退出不产生成功 receipt，不提交半段历史，
不执行工具、不自动重试。恢复器只给处置计划；跨进程通用恢复执行器尚未
提供。显式新请求必须先核对最后完整历史、已启动工具结果和原请求资源，
使用新凭据。404/202 本身均不能证明未执行。

## 本机验证

CPU mock 是实际固定 SDK + 内存 Fetch，无 HTTP 监听/模型执行；
`fixture_transport.mjs` 仅由离线测试显式 preload，生产桥接移除环境 preload。
测试覆盖完整批次、业务错误、断流、Node退出、日志篡改和零自动重试。

开发完成并提交后，使用绝对路径 guard 统一启动整阶段：

```bash
python3 -B tools/serve_protocol/stage.py prepare
python3 -B tools/serve_protocol/stage.py launch
python3 -B tools/serve_protocol/stage.py finalize
```

launch 全程持锁，CPU 与包身份先验证；同一模型进程完成普通、严格工具、
业务错误、多来源与真实 TCP 切断恢复，最终只结束自有 PID。失败不自动
补跑；原 10 生成 / 20 负例 / 2 启动 / 首次 guard 起30分钟上限不能重置。
本阶段无运行时改动，不复制替换相同默认 ELF，也不改变旧发布接受声明。
