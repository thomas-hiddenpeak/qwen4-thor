# Serve B2：推理协议与控制

基线ff7e750，默认f1133bcc/2edfb882。本页是SRV-05的成组开发合同；
整体工业化目标见[验收账](SERVE_INDUSTRIAL_PLAN_2026-11-03.md)。

05a已于01:38 CST交付，见[实际结果与回退](SERVE_STAGE_B2A_RESULT_2026-11-03.md)。
以下保留开发/验收合同；05b仍未实现。

## 05a：字段和隐藏

1. assistant历史接受`reasoning`和`reasoning_content`字符串/null；
   null/缺省表示没有历史推理。两个非null字段相等可接受，只渲染一次；
   不同则400定位reasoning。非assistant携带任一字段拒绝。原模板仍
   使用单一原生推理区，preserve_thinking选择保持。
2. 新顶层`reasoning_field`选择`reasoning`或`reasoning_content`；
   默认后者保留现有客户端行为。JSON message与SSE delta每个响应
   只使用一个字段，流中不能切换。两种都是兼容扩展，不冒充Responses。
3. `include_reasoning`为bool，默认true；false不发布/收集推理正文，
   仍完整解析标记、验证工具/schema和终态。usage与生成计算保持；
   不把正文中模型自述当隐藏推理，不将此选项宣称为计算预算或隐私隔离。
4. 这些控制只支持parsed messages，显式null也属非法类型。raw prompt与parse_response=false
   不能以隐藏选项伪装为已过滤；非法类型、未知字段选择明确400定位。
   模板和HTTP入口共用历史校验，不能只有请求字段白名单放开。
5. 项目客户端同时消费两种字段，JSON/SSE保持字段身份；拒绝混合/切换，
   工具结果回填保留已返回的历史字段。原客户端行为不静默改变。

证据目录`.q4t-work/serve-stage-b2a-20261103/`；CPU覆盖所有块切分、
UTF-8、隐藏开关、别名冲突、模板原生字节/ID、严格输出失败不泄漏及
客户端流重组。新增机器schema是明确子集，与原响应封套一起校验。

真实验收先冻结驱动，最多30分钟、3启动、14生成、12专门负例。
计划在默认MTP下对照公开/隐藏JSON与SSE、reasoning字段输出、真实
工具回填及schema；plain复核隐藏；安装后实际HTTP健康。单条输出
最多512token，简单问答最多128；关闭自动重试，保存所有失败。
固定驱动为`tools/serve_stage_b2a/`，计划13次生成：默认/别名两字段×
JSON/SSE×公开/隐藏共8次，真实严格工具两轮、隐藏schema、plain隐藏
和安装HTTP各一次。12负例在生成前拒绝，检验对应错误位置与无生成
计数增长；驱动关闭SDK自动重试，3次启动覆盖MTP/plain/安装。

这一批不改变选词/数值计算；不以05a通过标记05b或SRV-05整体完成。

## 05b：预算接入设计中的必要合同

预算必须参与模型下一token选择，不能只截断返回的reasoning文本。
边界要以实际生成token计量并定义标记token口径；总输出/上下文限制
仍优先，无法给出完整受约束结果时保留错误终态。强制闭合必须写入
真实模型序列，grammar matcher和历史/KV不能停留在被替换的分支。

当前MTP一次可接受k+1个token；接近预算应在下一批开始前进入能够
强制闭合的路径，已有批次不得只修改输出而留下超界KV状态。自然提前
闭合、重新进入think、跨token标记、预算0、UTF-8碎片、工具标记和
schema正文中的字面标记都需要独立状态合同。仅服务以MTP配置启动，
不证明请求实际使用了MTP；两者须在证据中区分。

05b在实现和直接边界验证就绪后单独冻结真实请求预算。本页没有宣布
thinking_token_budget已支持，也不授权无上限测试。
