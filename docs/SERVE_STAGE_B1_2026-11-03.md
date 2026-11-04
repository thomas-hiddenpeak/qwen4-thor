# Serve 阶段 B1：消息角色与模型模板

本批是SRV-04业务兼容开发，不是性能优化。基线源10b3442，默认
ELF142bb9d4，完整SHA和sidecar在冻结包记录。本页保留开发冻结合同，
后续已按范围[完成验收与交付](SERVE_STAGE_B1_RESULT_2026-11-03.md)。

## 允许与拒绝

- Profile `qwen3.8-native-v2`；模板ID `qwen4_exp-native-v2`。
  checkpoint原始Jinja不变，sha256为
  `c3cf9e34abf4f9e36c2d72165aa9c132d3e2a725b6c2586aaa3a8af9d7a81041`。
- 只有第0条可以是system或developer；developer映射到同一个system
  槽。两者混用、多条指令、对话中插入任一种都400拒绝，param定位role。
  不合并指令，不保留额外developer层级，不重排消息来获得可接受输入。
- `name`只可省略或null；包括空字符串在内的其他值均400，param定位name。
  模板不表达参与者名；tool结果身份继续由tool_call_id关联，name不能替代它。
- 指令content仅文本、文本片段数组、null或省略；禁止媒体，即使实验媒体
  开关开启。未知片段字段/类型拒绝，在解码、注册和GPU工作前处理。
- 其他历史规则不变：至少一个真实user query；保留thinking选择和完整
  工具批次ID校验/文本结果排序；无ID历史只保留原生兼容，不新增关联保证。
  `reasoning`别名、隐藏、推理预算、stop与预检本批不实现。

共享profile校验用于HTTP合同、输入适配器和直接模板入口。开发者消息
不直接送入原始Jinja，而是通过明示的角色适配；差分参考先仅将首条
developer改为system，再用checkpoint模板渲染。不能将此结论外推为
任意HF模板或所有OpenAI角色组合兼容。

## 证据与出口

构建和证据进入`.q4t-work/serve-stage-b1-20261103/`。
成组完成CPU ON/OFF合同、指令位置/name/媒体负例、原21个fixture复用
校验、新角色在thinking/工具历史下逐字节和逐token-ID差分；参考版本
固定记录，参考文件只读。新增消息schema只是SRV-08的相关子集，
工具关联等语义仍需运行时检查，不宣称完整OpenAPI。

真实验证上限30分钟、3次模型启动、9次生成尝试、12次专门负例；
计划7生成（MTP下developer/system对照2、thinking SSE1、真实资料
工具调用及回填2、plain developer SSE1、安装后developer健康1）。
每条最多512输出token，简单回答最多128；请求截止180秒。
失败不自动重试，修复后只能用剩余额度，保留全部原始记录。
HTTP前核对GPU业务和跨会话锁；全阶段持同一GPU锁，仅停止自己服务。

继续使用完整head、单槽、max_len8192、max_prefill2048；普通路径默认
MTP k3，plain单独启动，严格工具走既有plain约束路径。记录HTTP质量、
时延和资源回收，不宣称五档性能。模型/数值未改则复用相同源证据。

全部出口通过后保留原默认pair和源码回退包，核对身份后替换默认及
元数据，在同锁内完成有界健康检查；封存、更新文档并提交推送当前分支。
任一必要合同失败则保留现场及默认/恢复已知原版本，不自动扩展任务范围。
