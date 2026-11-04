# Serve 阶段 B1：角色与模板 profile 交付

2026-11-03 00:51 CST finalize，SRV-04按以下模型profile完成；
SRV-08同步完成消息schema子集。SRV-05～07、完整输入OpenAPI和JS SDK
仍待后续，见[任务队列](PHASES.md#队列与依赖)。

## 发布身份

- 默认ELF：`f1133bcc539caa5148200e2603269646ab46047fb32ff22f722ac4830b88a305`。
- sidecar：`2edfb882a543195cf03608c8ad5ed1876aa66c3c991a81e9b5227a25000c1507`。
- 源码/验收驱动：c299c647。旧142bb9d4/94ce0825与完整回退链保留。
  5个运行时变化全部位于server；模型、数值和GPU调度源码未变，
  复用同身份数值证据，不宣称性能优化或五档性能通过。
- 默认完整head/普通MTP k3；受约束请求仍plain host-masked greedy，
  完整payload验证后发布。模型和reference目录只读。

## 实际合同

`qwen3.8-native-v2`消息profile、`qwen4_exp-native-v2`模板ID：

| 输入 | 结果 | 限制 |
|---|---|---|
| 第0条developer | 作为原生system槽渲染，字节/token与对应system一致 | 是单一指令槽的替代写法，不引入第二层指令优先级 |
| system/developer混用、重复或错位 | HTTP400，param指向messages[i].role | 不拼接、不重排、不静默覆盖 |
| 消息name | 仅省略或null允许，其余400且定位name | 空串也拒绝；模型模板不能表达参与者名，工具关联继续使用ID |
| 指令content | 文本/文本片段数组/null/省略；媒体和未知片段字段拒绝 | 在媒体解码、请求登记及GPU工作前校验，实验媒体开关也不豁免 |
| 历史 | thinking开关/历史保留、完整工具ID批次与文本结果排序保持 | 仍要求真实user query；无ID旧格式不提供完整关联保证 |

HTTP合同、输入适配器和直接模板共用角色校验。能力端点显式声明
profile、允许角色、指令槽、developer映射及name/媒体限制。
[机器schema](serve-message-profile-v2.schema.json)只描述本批消息形状；
工具关联、真实user、预算和其他请求字段仍由运行时检查，不能作为
完整Chat/OpenAPI校验器使用。

## 验收

全部首轮通过，无重试。CPU OFF166/166、ON173/173；Thor发布清单
132/132、后端7/7；驱动6/6；构建零警告。防遗漏清单35字段、8路由、
59源码锚点及3个漂移负控通过，清单本身不代替语义验证。

模板差分共58项：46正例逐字节/逐ID一致、12拒绝形状与schema一致。
正例包含原21个fixture及developer变体、Unicode/空内容、工具历史和
乱序ID结果。参考只把首条developer变为system，使用checkpoint原始
Jinja；工具JSON文本参数转为其原生mapping输入，乱序结果有单独冻结
参考顺序。Transformers5.16.1/tokenizers0.23.2/Jinja3.1.2固定；原
21个fixture由5.17.0生成，其期望字节也全部核对。模板与tokenizer
文件hash均与原冻结值一致，不宣称任意HF模板兼容。

真实HTTP使用Python SDK3.42.1严格响应解析、关闭自动重试。所有
模型加载、请求和安装健康在同一个GPU lease内完成：7次生成、12个
专门负例、3次启动；执行117.20秒，首次launch至finalize138.08秒，
低于冻结30分钟/3启动/9生成/12负例上限。guard COMPLETED、ENV_ABORT0。

| 代表请求 | prompt / completion tokens | 完整响应秒 | 质量与协议 |
|---|---:|---:|---|
| developer JSON / MTP | 31 / 7 | 0.479 | 710003、stop |
| 对应system JSON / MTP | 31 / 7 | 0.342 | 正文、finish和usage与developer完全一致 |
| developer thinking SSE / MTP | 55 / 26 | 0.871 | 独立reasoning_content及710003正文 |
| developer严格工具 SSE | 400 / 54 | 3.543 | read_project_record，docs/MODEL.md，start_line=1，line_count=8 |
| 保留developer的工具回填 SSE | 406 / 45 | 3.024 | Qwen3.8-Flash-Next (qwen4_exp)及[docs/MODEL.md:L1-L8]正确 |
| developer SSE / plain | 31 / 7 | 0.558 | 710003、stop，启动参数明确--no-mtp |
| 默认安装路径developer JSON | 31 / 7 | 0.442 | 重新加载实际build/q4t，710003、stop |

这些时延是少量业务观察，不是吞吐/非劣证明。12负例分别覆盖混用、
重复、错位、name、未知角色、指令媒体和未知片段；JSON/SSE请求均在
首帧前400，错误位置一致，prompt/generation token计数和worker启动
计数未增加。每段检查active chat、严格租约、worker及词表存储归零；
3次关闭均0 in-flight，startup allocation全释放。

19份原始响应中的59个完整封套通过响应schema，额外检查nullable、
流身份/终态和X-Request-Id。实际只读项目工具按完整调用校验后执行，
保存真实文件hash、行号和引用。此证据不扩张到自主检索或所有模型任务。

## 封存、回退与剩余项

[137份原始记录与完整封条](evidence/serve-stage-b1-20261103/sealed-files.json)
包括CPU、模板输入/参考/原生token、SDK/HTTP字节、服务日志、模型
资源、guard及交付收据。仓库共141文件，另存本机大产物身份。
本机`.q4t-work/serve-stage-b1-20261103/{package,execution}`已封存，
包含旧/新ELF、sidecar、真实CMake缓存、协议manifest和源码bundle。

回退在冻结c299c647源码环境执行
`tools/serve_stage_b1/stage.py rollback --output ABS_DIR`，用新的完整
GPU guard包裹并保留原绝对证据路径。工具拒绝未知pair和源码漂移；
每个文件原子替换，fsync恢复标记处理中间状态，不声称跨文件断电事务。

00:52 CST复查GPU计算PID为空、锁可获取、无恢复标记/常驻模型；
MemAvailable约116.20GiB。未主动丢弃系统文件缓存。

下一批SRV-05仍需reasoning字段适配、独立输出隐藏和推理预算的完整
合同，之后是SRV-06文本stop、SRV-07预检/有效配置。非null name、
混合指令层级、assistant continuation、完整OpenAPI与JS SDK未支持或
未验证；阶段A严格长payload背压等保留项不因本批通过而消失。
