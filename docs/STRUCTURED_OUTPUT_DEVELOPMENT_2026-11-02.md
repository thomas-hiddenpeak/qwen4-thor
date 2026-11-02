# 工具控制与结构化输出：开发快照，未验收

最初按用户要求仅开发。用户随后明确授权停止 vLLM、清理内存，并
允许开展测试；17:12 CST 正常停服、清理和资源释放完成。下文实现
边界保持，测试结果单独记录，不用原基线的通过替代新路径验收。

基线：74579d5；已交付默认 c3772d11 / c80f1f14。开发状态与该基线的
历史验收分开；新代码、后端和组合没有继承此前通过结论。

## 实现合同

| 请求 | 开发实现；全部待验 |
|---|---|
| 普通 auto、无 strict | 保留原生成 / 解析路径 |
| tool_choice=none | 从当轮模板工具列表移除定义；禁止正文工具标记；成功响应无工具调用 |
| tool_choice=required | 至少一个声明的 function；输出 token 受 XML grammar 限制 |
| 指定 function | 仅允许该名称，恰好一个调用；未知名称拒绝 |
| parallel_tool_calls=false | 最多一个调用；与客户端是否并发执行是不同合同 |
| function.strict=true | 参数 schema 进入 token grammar，最终参数对象再次完整校验 |
| response_format=json_object | JSON 对象语法约束；没有额外字段语义保证 |
| response_format=json_schema | 下述声明子集；strict=true 时所有对象属性必须列入 required |

新约束路径为 parsed messages / 文本 / greedy / 单 choice。JSON 响应与
非空 tools 同用时必须显式 `tool_choice=none`；session / prefix cache
和媒体组合拒绝。原路径的默认 MTP、缓存与工具行为不随本阶段开发变化。

工具采用现有 Qwen XML。必选 / 指定模式在初始 reasoning 结束后只
生成调用；none / auto 可生成正文。模板保持原生渲染，只为 none /
named 过滤当前工具定义，完整历史仍保留。thinking 开关遵循
chat_template_kwargs 优先的现有规则。

## 约束位置与依赖

固定 XGrammar `ca1acf28bf35b9ba9092800b69aebc7d4ce1f7a7`，DLPack
`bbd2f4d32427e548797929af08cfe2a9cbb3cf12`。通过 C++ API 编译结构标签，
含 qwen_xml schema、工具选择、次数控制与初始 reasoning 区域；没有
启动 Python 推理服务。参考
[固定版本接口](https://github.com/mlc-ai/xgrammar/tree/ca1acf28bf35b9ba9092800b69aebc7d4ce1f7a7/include/xgrammar)、
[Qwen 结构标签](https://github.com/mlc-ai/xgrammar/blob/ca1acf28bf35b9ba9092800b69aebc7d4ce1f7a7/python/xgrammar/builtin_structural_tag.py)。

`Q4T_ENABLE_XGRAMMAR` 默认 OFF；显式 ON 才拉取 / 链接固定依赖。
默认公共 host 构建不联网。OFF 的 HTTP 服务在 tokenization / 模型工作
前返回 `structured_backend_unavailable`，不退回无约束生成；能力接口
按实际构建标志声明支持，同时提供 profile、依赖 revision 和组合边界。
允许本地 FetchContent source override，但要求两仓库身份匹配且干净。

真实 tokenizer 的 `Decode(skip_special_tokens=true)` 提供逐 token
原始字节，保留 UTF-8 分片；与模型相同的 stop IDs 显式交给 grammar，
空 special / padded ID 不作为正文 token。每次 argmax 前应用掩码，
选中后只推进一次 matcher；空候选、非有限 logits 或 matcher 拒绝不
退回普通 argmax。EOS 也必须通过 grammar，预算耗尽不能冒充完整输出。

受约束请求暂走普通单请求 host logits 路径，关闭该请求的 MTP trunk
分配 / draft 和无掩码 packed decode。普通请求继续原有调度。这个
取舍预计增加受约束请求时延，尚未测量；本阶段没有性能接受结论。

## JSON Schema 子集

profile：`q4t-json-schema-v1`，根为 object。

- 支持 object / array / string / number / integer / boolean / null，
  以及单一类型与 null 的 union。integer 按原始十进制拼写验证，不能
  用 double 舍入把 1.0000000000000001 当成整数。
- object 必须给 properties 和 additionalProperties=false；required
  只含唯一已声明键；strict=true 要求所有键必需。生成按 schema 顺序，
  不启用会削弱键存在性 / 唯一性保证的 any_order。
- array 必须给 items，支持 0..64 的 minItems / maxItems；不声明
  maxItems 不等于长度为 64。总响应另受 1 MiB 限制。
- enum 仅支持同类型的 string / boolean / null，最多 32 项，各字符串
  最多 256 字节。title / description 是注释，不改变验证语义。
- 其余关键字均拒绝：包括 $ref / $defs、组合 schema、pattern、format、
  字符串长度、数值范围、数值 enum 等，不能交给后端静默忽略。
- 每 schema 最多 16 KiB、深度 8、128 节点、每 object 32 属性；整个
  结构标签最多 64 KiB、最多 16 工具。XML 顶层参数名 / 工具名使用
  1..64 ASCII 字母、数字、下划线或连字符。XML 顶层 nullable string
  因 raw string 与 null 的歧义暂拒绝；JSON 模式允许。
- XML 参数值 grammar 排除结构闭合标记；含这些标记的 enum / 属性名
  明确拒绝。JSON 输出中的 `<think>` / `<tool_call>` 字符串仍是数据。

保留最终 JSON / 参数独立验证；grammar 约束不证明业务事实正确，也
不取代应用的路径访问、整数业务范围或工具授权校验。

## 发布、错误与资源边界

受约束 JSON/SSE 都在整份响应验证后发布正文、推理和工具批次。SSE
可先发 role，后续 payload 暂缓；后面的调用错误不能使前面的调用
提前可执行。该实现不是逐 token 的结构化流，能力接口明确声明。

预算耗尽 / 上下文截断为 `structured_output_incomplete`；其他完整性
或掩码错误为 `structured_output_invalid`，HTTP 502 或 SSE error +
DONE，无成功 finish / usage，不保存成功 session。编译失败在模型前
返回 400 `constraint_compile_failed`。取消 / GPU 错误保持原生命周期。

总响应最多 1 MiB，合并连续 deltas 防止逐字输入造成大量对象分配。
后端每请求一个 compiler、一个线程、不使用全局缓存；完成编译后的
grammar 上限 64 MiB。此值**不是编译峰值 RSS 上限**，XGrammar 的缓存
预算也不是内存沙箱。当前编译调用只能前后检查取消，不能中途抢占，
编译尾延迟 / 峰值内存是后续本机验收要记录的风险，未宣称生产级隔离。

## 待验范围与进入条件

首轮真实后端 host 145 / 无后端 140、客户端 23 项通过；已有同身份
通过结果仅作为旧版基线。CPU 覆盖：请求与 schema 拒绝、全部切块边界、批次发布门、
重复 / 缺失 / 额外参数、none / required / named / parallel、截断、
reasoning 内工具字样、JSON 内标记、精确整数、能力扩展客户端兼容。
开启后端后另验真实 XGrammar 掩码：UTF-8 字节分片、EOS、padded ID、
跨边界 token、多调用限制、无有限候选；不能只用 mock 宣称约束有效。

按最新测试授权，先通过上述 CPU 合同，再在共享 GPU 租约内
进行有界 SDK / HTTP：none、required、named、parallel=false、strict
参数、json_object、json_schema、thinking、JSON/SSE 和真实项目读取
回填；检查低预算错误及普通 auto 回归。业务上限 2 次模型启动 /
16 次生成 / 12 个请求负例；另预留 1 次安装启动 / 2 次健康生成，
合计上限 3 启动 / 18 生成 / 12 负例 / 30 分钟，执行前冻结。业务
通过才按既有交付授权安装，失败回退已知 pair，不自动扩样。新路径
记录业务事实质量与请求时延；当前并未启动 GPU 验收。

默认程序与发布元数据未替换。只有这组合同通过、身份固定且保留回退
包后，才可另行推进已授权交付；开发提交不代表可投入业务。
