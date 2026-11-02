# Serve SRV-05a：推理字段与输出隐藏交付

2026-11-03 01:38 CST完成交付。SRV-05a已按以下合同验收；SRV-05b
真实推理预算、SRV-06/07、完整客户端矩阵及工业化运维要求仍未完成。
[长期验收账](SERVE_INDUSTRIAL_PLAN_2026-11-03.md)保持active，子项通过
不代表整个SRV-05或工业化目标完成。

## 发布身份与合同

- 默认ELF：`ecd5d21f7b3a9adb221b17a352499df5a5803020f15149ed6cef978c916f8dcc`。
- sidecar：`91ce0ed94e4ee2c9bcb30895258a6bb30bef3cfe8e37f0dd6ac9160fb314ecfb`。
- 源码及验收驱动：`24c831da04d89d680196103e63fe7889432805f9`。
  旧f1133bcc/2edfb882、真实构建缓存和完整回退链保留。
- 5个已有运行时文件变化、2个新文件均位于server；模型、GPU计算、
  调度和MTP源码不变，复用同身份数值证据，不作性能接受声明。
  普通默认仍完整head/MTP k3；严格请求仍plain host-masked greedy。

能力profile为`q4t-reasoning-v1`，原生消息profile仍是角色合同v2。

| 接口 | 行为 | 明确边界 |
|---|---|---|
| assistant历史reasoning / reasoning_content | string/null；缺省/null为空；两者非null且相等只渲染一次 | 不相等400定位messages[i].reasoning；非assistant携带任一字段拒绝；HTTP入口与直接模板共享校验 |
| reasoning_field | reasoning或reasoning_content，默认后者；JSON/SSE每个响应固定一个字段 | 只支持parsed messages；null/未知值拒绝；不等于Responses reasoning item |
| include_reasoning | 默认true；false不向解析器输出缓冲收集/发布推理正文 | 保留标记、工具/schema、长度与错误终态校验；不减少模型计算或usage，不是内存清除或隐私隔离 |
| 项目客户端 | JSON/SSE消费两种字段，工具回填保留已返回字段 | 响应两个非null字段即使相等也拒绝；流中不能切换；允许null占位 |
| raw prompt / parse_response=false | 保持原有原始输出 | 显式使用上述控制拒绝，不能把raw输出宣称为已过滤 |

普通正文中的模型自述不属于已解析的推理区，不能依赖此选项过滤；
结构化JSON字符串中的`<think>`等字面值仍作为数据。隐藏不会增加或
改变生成token预算，`thinking_token_budget`仍不支持，能力声明为false。

## 实际验收

CPU OFF171/171、ON178/178、Thor必需137/137及后端7/7；客户端26/26、
驱动6/6通过，构建零警告。唯一CPU命令错误为首次使用不支持的
`--filter`被CLI拒绝，未进入测试；原日志保留，修正为支持的子串参数
后7项通过。HTTP全部首轮通过，无自动重试。

模板差分120项：108正例逐字节/逐token-ID与固定checkpoint Jinja一致，
12角色负例拒绝；包括21旧fixture、字段别名、相等双字段、null历史、
Unicode及工具批次。别名在独立Python参考中归一至checkpoint单一
reasoning_content槽，原始Jinja与tokenizer只读且hash保持。
Transformers5.16.1/tokenizers0.23.2/Jinja3.1.2；21旧fixture的原
5.17.0字节期望也核对。历史冲突/非法类型由native和HTTP负例验证，
JSON Schema不宣称能比较任意两个字段的动态值。

真实HTTP使用OpenAI Python3.42.1严格响应解析和项目wire重组器，
同一GPU lease覆盖全部模型加载、业务、默认替换及实际安装路径健康。
13生成、12专门负例、3启动；执行129.47秒，首次launch至finalize
147.28秒，低于冻结30分钟/14生成/12负例/3启动上限。
guard COMPLETED，ENV_ABORT0；模型/参考目录未写入。

| 场景 | prompt / completion tokens | 响应秒 | 结果 |
|---|---:|---:|---|
| 默认/别名 × JSON/SSE × 公开/隐藏，共8请求 | 每次55 / 26 | 0.855–0.990 | 各公开/隐藏对正文、finish、usage完全一致；710003正确；字段和隐藏符合请求 |
| 严格真实资料工具，SSE、reasoning字段 | 424 / 104 | 6.289 | docs/MODEL.md，start_line=1，line_count=8，完整调用后执行只读工具 |
| 保留真实reasoning历史后回填，隐藏输出 | 454 / 48 | 3.240 | Qwen3.8-Flash-Next（qwen4_exp）及[docs/MODEL.md:L1-L8]正确 |
| thinking开启、隐藏、严格schema SSE | 60 / 39 | 2.436 | answer=710003的JSON对象、stop，无推理字段 |
| plain隐藏SSE | 55 / 36 | 2.068 | 710003、stop，无推理字段；命令明确--no-mtp |
| 实际build/q4t安装后reasoning JSON | 55 / 26 | 0.959 | 710003、stop、独立reasoning，重新加载安装路径 |

隐藏前后usage一致的证明来自同一MTP模式内的配对请求；plain记录只
验证隐藏业务正确，不宣称跨解码模式生成序列相同。少量响应时延是
业务观察，不是五档吞吐或统计非劣证明。严格工具/schema实际走plain。

12负例覆盖控制字段类型/null、raw/unparsed组合、历史字段冲突/类型/
角色，JSON/SSE均在首帧前400且error.param精确；prompt/generation
计数与grammar worker启动数未增长。每组active chats、严格租约、
worker和词表存储归零；3次服务正常关闭，startup分配全部释放。

25份原始响应、115个封套通过响应schema；13个成功响应中的95份
message/delta另通过[推理投影schema](serve-reasoning-v1.schema.json)
及请求关联的字段/隐藏检查。它与既有消息profile、响应封套共同组成
明确的子集，仍不是完整输入OpenAPI。防遗漏门为37字段、8路由、
61源码锚点及3漂移负控；清单不代替语义验收。

## 封存、回退与后续

[174份原始记录封条](evidence/serve-stage-b2a-20261103/sealed-files.json)，
仓库共178文件，覆盖CPU、模板参考与原生token、SDK/wire、服务日志、
资源、guard和交付收据；另记录本机大产物身份。
`.q4t-work/serve-stage-b2a-20261103/{package,execution}`均已封存，
保留旧/新pair、CMake缓存、compile_commands、协议manifest与源码bundle。

回退在冻结24c831da源码环境运行
`tools/serve_stage_b2a/stage.py rollback --output ABS_DIR`，新GPU guard
覆盖全程并保留原绝对证据路径。当前后续文档/审计清单变化不能冒充
冻结源码身份；工具会拒绝漂移与未知pair。每文件原子替换，fsync
恢复标记处理中断，不宣称跨文件断电事务。

01:39 CST GPU计算PID为空、共享锁可获取、无恢复标记/常驻模型，
MemAvailable约116.41GiB；未主动丢弃文件缓存。

接下来SRV-05b须实现真实选词预算、强制闭合入模型/KV、grammar状态
和MTP批次边界，再推进文本stop、预检/有效配置、JS SDK与完整协议。
阶段A严格长payload背压、多人资源、鉴权/网关及长期稳定等缺口仍保留。
