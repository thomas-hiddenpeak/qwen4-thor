# A06：Completions与Models原生执行

2026-11-08，承接A05交付，属于业务协议补齐，不声明性能优化。
总目标持续active，本项的子模块提交不是任务终点。

## 固定合同与发现

以本机已固定OpenAI官方JS SDK 7.47.0的`resources/completions.ts`、
`models.ts`为逐字段依据，原件和SHA256在
`.q4t-work/serve-completions-20261108/reference/manifest.json`。
对应[Completions参考](https://developers.openai.com/api/reference/resources/completions/methods/create)
和[Models参考](https://developers.openai.com/api/reference/resources/models/methods/retrieve)。
本地vLLM固定快照只作实现比较，不当作最新协议。

现有Chat已有实际采样/choice/取消/配额，但仅添加路由转发会丢失
原始prompt、token ID、echo、prompt logprobs、best_of和批量usage。
Models每次查询重算created且没有详情/别名；必须补共享模型目录。
采样器现有Chat对前20名之外选中token返回-9999，这是已明确的
Chat协议约定，不能直接用于Completions best_of内部计分。新增实际
完整选中项概率供补全/选优使用，保留Chat公开语义并直接验证。

模型tokenizer实际含FIM prefix/suffix/middle token，因此suffix不以
“模型不支持”直接结案：实现保留原始前缀token的FIM适配并做真实
填充短验。token存在不单独证明填充质量通过。

## 必做实现

1. Models：稳定的id/created/owned_by，原生列表/详情、URL解码和
   已配置别名；同一注册表用于Chat、preflight及Completions。
   `--served-model-name`与可重复`--model-alias`进入CLI/发行合同。
   已加载只读基础模型不可DELETE，返回真实权限错误；不存在404，
   不假装删除只读资产，训练管理范围仍属G00。
2. Completions输入：string/string[]/token IDs/token ID[][]与null/空
   文档，整数/词表边界、默认/null、max_tokens=0、echo、n/best_of、
   logprobs 0—5、采样/stop/seed、流式选项及取消扩展。批量预先完成
   token/上下文/客户端总预算核对，避免首个prompt已执行后才发现
   后面非法。原始ID直接执行，不decode后再encode。
3. 原生执行复用现有模型/租约/健康/取消生命周期；按prompt与候选
   独立状态，保持原始文本，不加入Chat模板或剥离其字面think标签。
   echo+logprobs真实计分提示token，首个无条件token概率为null；
   按有界行数读取实际logits，不为长prompt保存T×vocab。
4. best_of使用实际生成的平均token logprob选优，稳定处理并列。
   usage/TPM包含未返回候选的实际生成工作，prompt按输入计数。
   JSON/SSE输出text_completion、逐choice索引、finish_reason、
   Unicode字符偏移和概率数组；SSE只成功终态后发最终usage/DONE，
   截断/错误/取消不得伪造成功。best_of选优按官方合同不能流式。
5. 路由同时进入后端和原生入口，包含鉴权、受控内网、CORS、body
   准入和操作端口分类；公开schema/capabilities/配置/示例/SDK及
   发布身份同步。没有外部代理也能直接使用。

## 有界验证与交付

成组开发后做针对性CPU：形状/默认/词表/组合、固定logits概率及
候选排序、UTF-8/stop/echo/分块重组、usage/预算、原生HTTP/SDK和
Models权限/别名/稳定created。既有Chat数值、工具、schema等证据
仅在依赖未改时复用；本组改变的直接行为补验。

必要真实短验最多2启动、8条生成请求、900秒，同一严格GPU租约：
原始文本和token输入、批量多choice JSON/SSE、echo/prompt概率、
best_of选择及实际消耗、suffix真实填充、取消和随后同PID Chat。
具体驱动冻结请求、来源和预期后再执行，零自动重试；具体失败
保留并只修复/补验受影响项。不跑完整性能或全模型正确性矩阵。

通过后保留A05回退包，更新适用默认/协议/发行元数据，维持原
STOPPED意图及客户端策略/配额；提交推送，立即继续A07等总计划。
原生实现及接入开发已完成；50项不同C++（47 host、3真实HTTP生命周期）、
51项schema/生产语义、6项官方SDK、3项实际native入口、1项实际TLS/CORS、
7项驱动/托管/可选nginx与3项发行声明CPU检查已通过。
原始记录在`.q4t-work/serve-completions-20261108/`。保留一次新增HTTP
夹具编译失败，以及2项夹具预期失败；修复后只重验失败项。
后者包括原有A03测试仍假设A05结构化JSON不会增量发送，按当前
已交付合同改为直接核对隐藏推理、增量正文与真实概率来源。

新增发现并闭合：批量取消只结算已准入的prompt；未返回候选的
生成token仍计入TPM/usage。错误状态在编码阶段保持类型。托管
发行可选模型名称/别名经过校验进入持久启动参数，恢复继续使用
同一配置；无配置保持默认。发行声明v2随包携带两份Completions
schema，旧v1及历史回退包仍可独立验证。

下一步冻结1次计划启动/7条生成，保留总上限2启动/8条/900秒：
原始Unicode echo+实际提示概率；两prompt真实token ID批量n=3；
同seed JSON/SSE逐choice/概率完全对照；best_of=3选优与24实际
输出token计费；真实FIM加法代码填充；显式取消后同PID Chat。
Models目录/详情/只读删除及预分词只读控制请求另记。当前尚无
A06 GPU证据，默认继续A05；全部总目标仍active。


## 真实短验与原始证据复核

1启动/7生成已完成，驱动51.954秒、连续严格租约52.503秒，50次NVML
全OK。37个真实提示token的原始Unicode echo和真实teacher-forced概率
通过；两prompt、n=3共6候选的JSON/SSE文本与概率完全相同。best_of
按6个实际不同候选的平均概率选出两条结果，usage仍包含全部24输出
token。FIM真实填充为`a + b`；显式取消后同PID Chat返回42并走MTP。
Models稳定目录/详情、不存在404及只读403通过，进程退出0、端口/锁释放。

原驱动最后错误地要求取消计入requests_error，因而原result仍保留FAILED；
真实计数是7总请求=6成功+1明确取消、0错误，符合早已实现的合同。
修复仅改驱动断言，新增1项含五种反例的CPU验证；原ELF和所有运行时
源码未变。`review.py`重新核对原始JSON/SSE、SDK、候选计分、计数、
原commit源码和清理，形成独立`PASS_COMPLETIONS_MODELS_REVIEW`回执，
未覆盖原始失败记录、未追加GPU运行。初次遗漏租约继承参数的0启动/
0生成拒绝也保留。接下来可回退发行，随后继续A07；总目标不结束。


## 可回退交付

默认已替换为已验ELF `073b273d` / 元数据 `0dc4ad2e`，v4服务包
`8d1f6b50` / 服务元数据 `1575d877`；原生入口更新、Guardian字节不变，
继续复用已验CUDA探针。原A05 `46d7bf70/fe70b669` 和
`51d90f4c/b1734181` 完整回退保留，receipt `1f4a670b`。
原service_id、ledger字节/预算、策略/Key/配额保留，仍STOPPED；安装
只做封包核验和help/status，0模型/0Chat。原件见
[evidence/serve-completions-20261108](evidence/serve-completions-20261108/README.md)。
A06已交付，继续A07 Responses及其余总计划；未合main，非全OpenAI兼容
结论，不追加无关全矩阵或长稳。
