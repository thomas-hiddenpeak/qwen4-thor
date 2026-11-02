# serve 输入合同修复 A 组

日期：2026-11-02；起点 `795d980`，对应 serve 审计 F1/F2/F3/F4/F7。
这是业务正确性修复，无性能优化接受声明。先完成本组验收、交付，再
推进 F5 观测与 F6 资源边界；本文件的开发计划不是通过证明。

## 冻结实现边界

- 工具定义：普通 auto、raw、none、required/named 共用外层及 function
  白名单。拒绝未知 / 错位字段、错误类型与重复名称；description 只能
  为字符串，parameters 若出现必须为对象，strict 可为 bool/null。
  null 等于未请求 strict；普通 schema 保持提示语义，有限 schema
  保证仍仅适用于已声明的受约束模式，不宣称完整 JSON Schema 支持。
- 历史：带 ID 与无 ID 都检查调用形状、function 类型、名称及 arguments。
  arguments 必须存在，为对象或非空的 JSON 对象文本；拒绝未知字段、
  重复 JSON 键、错误 / 缺失参数，空参数请传 `{}`。名称与参数键禁止
  XML 定界符和控制字符。无 ID 兼容 wrapped / flat 原生格式，保留
  一次调用多个结果的历史行为；全部批次最多 128 次调用。带 ID 的
  完整唯一结果关联及文本乱序归一保持。tool.content 必须存在且为
  字符串或数组；空字符串 / 数组合法，null / 缺失拒绝。
- 可空字段：temperature/top_p/n/frequency_penalty/presence_penalty
  的 null 使用当前 greedy 默认（0/1/1/0/0）；stream:null=false，
  stream_options:null 不请求 usage，user:null 不附加用户元数据。
  保持非中性采样值拒绝。function.strict / schema.strict:null=false。
  不对 model、扩展控制 / cache / 模板参数统一放行 null；原先已支持
  的 seed、token limit、tools 等可空语义不变。
- 取消：仅接受 request_id/cancel_token，和生成共享 ID / token 格式。
  形状错误及未知字段 400，合法但无匹配凭据 404，匹配 202；无效请求
  必须在 registry 变更前拒绝，202 只代表取消已提出。
- 数字：保留原 JSON 数字文本，在转换前校验数学整数性；覆盖 token
  预算、seed、deadline、prefix 写入预算、中性选项和 schema 数组界限。
  `1.0` / `10e-1` 有效；舍入后才等于整数的输入拒绝。

## 冻结验收与交付

1. 公共 host ON/OFF 全注册表、Thor 必需 host 门及后端 5 项；增加
   针对形状、历史、null、精确数字的回归；生产 HTTP 取消副作用检查。
   21 份原生模板夹具保持字节与 token 完全相同。构建零警告。
2. 复用固定 XGrammar / DLPack 源码和模型身份，数值源码未变则复用
   既有数值证明。客户端 / stage / 回退检查对应实际修改选取；默认
   binary/sidecar 旧 pair 与源码 bundle 先封存。
3. 单 GPU 锁阶段，30 分钟、至多 3 次启动、10 次生成、24 个拒绝请求：
   普通 auto JSON 和 strict required SSE 各完成真实 docs/MODEL.md
   读取、正式客户端校验和结果回填；SDK null 请求及整数合法写法；
   受影响输入 400 / 取消 400、404；安装后 MTP 普通文本与约束输出
   健康。只执行预先列出的请求；失败保留现场，仅修复后重验受影响项。
4. 成功才替换默认 pair、绑定新协议身份；失败按已知 pair 回退。
   结束检查自有进程 / 请求资源归零，不启动常驻服务。不作并发、
   全上下文质量、媒体组合或性能接受声明。

开发检查：host ON 152 / OFF 147、Thor 必需 113＋后端 5、原生
模板 21 个逐字节对照、客户端 23、输入驱动 5 / 回退 6 均通过。
tokenizer 源码、模型 tokenizer 与夹具身份未变，模板 token 对照沿用
原同身份结果，不宣称本轮额外执行 21 次模型推理。

首次完整构建中 q4t 与 host 成功且零警告，HTTP 支架因原先遗漏的
HandleCapabilities stub 链接失败；补齐该 stub 后只重建 HTTP 支架，
原日志保留。无引擎修补，已通过 host 结果直接复用。

## 交付结果

源码 `b71f026`（运行时实现 `a137ca0`）；默认二进制
`ea678009413bd4d63a2b456a697e9c7844b47f71e6e2b3e3447b5044f9cbdf0a`，
sidecar `90372798114d61fac6a15a409169596248716c6553561aa26bfa739ab1ca07ce`。
2026-11-02 18:47 CST 完成安装健康及 finalize，普通请求继续完整
head / MTP k3；受约束路径维持 plain masked decode。

- 生产 HTTP 支架 5 项通过，包括未知取消字段 400 且不改变请求状态、
  错误凭据 404、匹配凭据 202。
- 实际 8 生成 / 19 拒绝 / 3 启动，在 128.90 秒执行窗口内完成；
  guard COMPLETED，ENV_ABORT 0。业务 89.84 秒、安装健康 38.48 秒。
- strict SSE：工具响应 3.635 秒、结果回填后回答 3.241 秒；普通 auto
  JSON / MTP：1.969 / 2.292 秒。每条链只执行一次真实只读工具，
  均正确回答 Qwen3.8-Flash-Next，并引用 `[docs/MODEL.md:L1-L8]`。
- SDK 八项 null 请求输出 710003，合法整数指数写法输出一致；安装
  健康普通 MTP 输出 710003，SSE schema 输出 answer=323/label=中文。
  16 个 Chat 输入错误与 3 个取消输入 / 凭据负例按预期拒绝；负例
  前后 prompt / generation token 计数完全一致，无误触发生成。
- 三次服务退出均释放全部 startup reservation，0 in-flight；最终
  NVML 计算 PID 为空，锁已释放，无自有恢复标记，没有常驻模型服务。

首次 prepare 缺 compile_commands 记录，部分包保留，补导出后封存。
首次 guard 使用相对 argv，不满足部署保护的绝对路径祖先检查，
COMMAND_FAILED；尚未进入 begin / HTTP / 模型请求。保留整份失败
现场后，用绝对路径执行原清单。未放宽部署保护，未重复生成请求。
两个准备 / 启动问题与早期支架链接失败均不计为模型失败。

原始日志、请求/响应 bytes、SDK 消费结果、工具结果、metrics、GPU
监控、构建记录与身份已封存于
[证据清单](evidence/serve-input-contract-20261102/sealed-files.json)。
本机 `.q4t-work/input-contract-20261102/package/` 保留旧默认
957211c7 / 9eba5472、当前 pair、源码 bundle 与既有完整回退链。
安装沿用经过 6 项合同的身份绑定事务，未知状态不覆盖；回退须在
对应冻结源码与 GPU guard 下执行，不能跳过 source / pair 检查。

仅服务源码 9 项变化并新增共用合同头/实现，模型与数值源码未变。
这关闭审计 F1/F2/F3/F4/F7 的冻结范围，不意味着 OpenAI 全兼容，
也不推广到媒体、多槽混合、所有 schema 或全上下文质量。F5/F6
观测及编译资源边界仍需下一阶段独立验收。
