# 严格长输出背压：复制候选限定通过

2026-11-03，**ACCEPT_BOUNDED_STRICT_COPY_TRANSPORT**。固定12443字节严格
JSON在真实模型中完成；完整校验后的实际TCP EAGAIN、SSE取消、JSON写截止、
排队普通请求与短strict恢复均已证明。默认程序不变，专用程序不安装。
这是业务可靠性证据，不是性能接受或整个serve工业化完成。

## 身份与范围

| 项目 | 记录 |
|---|---|
| 冻结源码/驱动 | 531752ba；[先验与冻结合同](SERVE_STRICT_TRANSPORT_2026-11-03.md) |
| 验证ELF | f07117f384d7bcbe18345f3d437ed26c03dfd74b174aecb5228022f3f3f287d0 |
| 验证协议SHA | 130468da654e20b8ce71fac2405588283ae05182f0b37fa7da30fed85d8021ca |
| 保持默认ELF | ed672a65b586f8445e51e3ba6d63659b4a098386e87e1ed38e3c8893a352a139 |
| 保持默认sidecar | 67897c817dfccd6db3d4e2c0d0af3cfc70fb97a2c840887905c03fbf0bd6be23 |
| 默认协议SHA | 0e252868586d1d78e59fd951f8e327d7cb5f6a0e903148a622198decd6645a8f |
| 实际 / 硬限 | 6生成尝试、4负例、2启动；1800秒限额，首轮完成，无重试 |
| 结果分类 | 4完整生成、2故意不完整的长响应传输；不把后两者记业务成功 |
| 执行 | 业务138.809秒，首次锁到finalize176.750秒；COMPLETED、ENV_ABORT0 |

两个plain单槽进程，8192上下文、2048prefill、loopback18104。专用构建
仅开启已有小socket缓冲/真实EAGAIN握手，新增指定请求的有界token日志；
没有替换模型输出或调整logits。193运行时文件中仅chat_generation.cpp与
serve_fault.h增加故障宏诊断，模型、模板、分词、grammar和parser未变。
生产调用点在宏关闭时完全排除，原默认pair及完整回退链保持。

## 为什么先做CPU核对

[旧03b](SERVE_BACKPRESSURE_RESULT_2026-11-03.md)只有“符合给定schema”提示，
原生模板未把response_format schema放入提示；旧请求在1024-token预算内
未闭合，缺少实际生成IDs，所以**旧失败的具体原因仍未知**。

本候选只改变用户提示：给出完整JSON供逐字复制，保留原48×256等号数组、
12443字节内容、schema、1024-token及180秒请求上限。提示从21token变为
289token，原/候选structural_tag逐字相同。

生产分词器加隔离grammar重放证明：canonical243个内容token加EOS可以
完成；同内容1100个单字节token前缀也全部合法，但EOS被拒绝。合成有限
logits只证明可行性，canonical编码不是生成长度上界。两个worker均reap。
生产parser在Finish前不释放内容，完整校验后单delta；MessageJson12574
字节、固定CPU封套SseDeltaChunk12751字节，截断前缀拒绝。

新验证日志仅对Q4T_SERVE_GENERATED_IDS指定请求输出最多4096个ID，明确
total/captured/truncated。ON/OFF与缺省/错请求/匹配请求6项CPU通过。
驱动9项首轮8通过/1缺socket import失败，保存原驱动/日志，修复后只重验
该项通过；4种计划请求均通过生产native合同。所有构建零警告。

## 真实长内容、写失败和恢复

两组实际IDs都与CPU canonical243内容token加EOS248046逐项一致，共244。
解码内容均12443字节，与原expected-content逐字相同，SHA256为
e42bbe5498638cd19c7ca236ce63dba4a705b4baa2e734f6e834574904109a3c。
这些是本次真实模型记录，不是CPU推导或向服务注入的内容。

| 实际观察 | 严格SSE取消 | 严格JSON写截止 |
|---|---|---|
| 请求到首次EAGAIN | 13.667秒 | 13.646秒 |
| EAGAIN到资源清理 | 0.667秒，取消202后 | 30.585秒，保持客户端不读 |
| 首次失败 | handler / explicit_cancel | handler / write_deadline |
| 失败逻辑写的本地进度 | 2304字节 | 2880字节 |
| 同连接最终前缀 | 2740字节（body2569） | 2880字节（body2735） |
| 客户端结论 | SSE事件不完整 | JSON字符串未闭合；声明body12823字节 |

每组先观察实际EAGAIN与response阶段，再提交普通请求：active=2、槽仍占用，
普通请求未提前完成。独立strict请求503/structured_capacity_exhausted，
错误取消凭据404/request_not_found，health/ready可用。SSE正确取消后释放
握手；JSON释放握手但保持客户端不读，30秒逻辑写截止先于180秒请求截止。

只有所有owner排空后才读取长连接。原wire含strict内容前缀，排除仅心跳/
头部阻塞；没有成功finish、usage或[DONE]，没有第二HTTP响应。每组唯一
http-write日志与响应trace、固定指标对账；chat中断原因分别为explicit_cancel
和client_disconnected，后者与底层write_deadline分列，request deadline计数0。
transferred仅为失败逻辑写送入本地socket的进度，不代表用户收到完整结果。

普通请求均准确回复710003，随后strict均为{"ok":true}。每实例最终成功2、
中断1，worker2/2已reap，active/queue/body/strict/preparing/vocab/worker为0、
槽1/1空闲。4完整生成封套与4错误封套通过机器合同，两份部分wire单列。
普通工具、SDK和生产健康复用[01b同默认身份的证据](SERVE_OBSERVABILITY_RESULT_2026-11-03.md)，
没有额外默认模型启动。

## 内存、失败保留和剩余边界

36个稀疏样本：parent RSS最大944.883MiB，已观测worker RSS最大224.547MiB，
系统MemAvailable最低39.736GiB。四个退出中/僵尸worker快照没有VmRSS，
保留null；worker退出后尚待handler销毁reap，active指标表示未回收所有权，
不能由它推断OS仍在计算。另4份退出报告post-exec VmHWM最大224.563MiB，
wait4 lifetime最大1818.238MiB含exec前继承峰值，不相加或当整机物理RAM保证。

离线摘要初版假设每个status有VmRSS而失败；修正为缺失值null，重用原样本，
保留原分析失败记录。没有因此重跑模型或修改原始证据。

08:30:00 CST GPU计算PID空、共享锁可用，无模型/worker/恢复标记，
MemAvailable117.004GiB。未drop_caches、未干预其他服务。证据归档
[186原记录、190文件约4.21MB](evidence/serve-strict-transport-20261103/README.md)，
6份大产物登记本机身份；package/execution及原seal保持，复制按字节核验。
[入库核对](evidence/serve-strict-transport-custody-20261103.json)确认190份Git blob
与本地原字节一致、572个本地文档链接有效；首次漏入被忽略的server.log
已记录并通过显式加入整个封存目录修正，日志与.bin wire未改写。

结论限于明确复制提示、该有限schema和本机缩小窗口的真实TCP；不保证任意
strict请求都在指定token预算内完成，不证明全部代理/网络、长稳、整机RAM
保护、多租户访问或跨进程任务恢复。旧03b的失败/预算未改、未追认通过。
下一步推进资源保护与持续混合负载的独立合同，工业化目标保持active。
