# Serve 阶段 A：请求身份、就绪与流式保障

2026-11-03 00:02 CST 安装，00:03 finalize。SRV-01～03按以下限定
范围交付，SRV-08完成响应schema与Python客户端证据子集。剩余任务
见[队列](PHASES.md#队列与依赖)，不宣称完整OpenAI或生产多租户兼容。

## 当前交付

- 默认binary：`142bb9d41b2b972d61d995f391cb890c4068b3fd6b7a68b71aa9dafe1f506317`。
- sidecar：`94ce08253aa345db4775de3b1002c9daf29a91d058fea682ede946c6bcde5fdf`。
- 运行时源码e0d9f9d；驱动修正c7987ab，收尾62eea34。此前已交付
  c8c412c4/da9f3ac5及完整回退链保留。14个运行时变更均在server
  目录；模型/数值源码未变，复用已有数值证据，不作性能接受声明。
- 完整head、普通请求默认MTP k3；strict/结构化仍plain masked
  greedy，完整payload验证后发布，模型目录与reference未修改。

## 实现与明确边界

| 合同 | 当前行为 | 限制 |
|---|---|---|
| 关联身份 | 有界X-Request-Id接收/生成/回传，日志关联completion；自动ID使用128随机位 | 仅ASCII字母数字及`-_.:`，1～128字节；重复/非法头400；随机身份不依赖重启计数器，但不是幂等或权限凭据 |
| 取消身份 | JSON request_id与cancel_token保持原合同 | HTTP追踪头独立；相同追踪头不会去重或改变取消权限 |
| 错误 | 固定error封套、显式code，能定位的输入合同填写param，SDK可读取响应头ID | schema/tool内部若未明确提供位置仍为null；不从错误文本猜路径 |
| 就绪 | 新增livez/readyz；普通槽忙仍ready，GPU unhealthy使ready503 | 模型加载后才监听，启动期间不是可查询的ready503；livez是服务存活检查，不是全部依赖健康 |
| 控制入口 | healthz/livez/readyz/cancel共用4个预留连接名额；普通业务另有上限 | 总连接仍有界；未分类慢头可占总额度，不保证抵御连接洪泛；metrics不属于预留入口 |
| 关闭 | 取消现有及晚注册请求；活动连接排空期间保留至多2秒控制窗口 | 可能提前关闭监听；不是全部GPU工作在2秒内结束的承诺 |
| HTTP输入 | Chat/cancel要求application/json、UTF-8；已知路由错误方法405＋Allow，query400，GET非空body400 | Connection:close；不增加chunked上传、keep-alive复用、网关鉴权或TLS |
| SSE首帧 | 模板与结构化准入后、编译/encode/排队/prefill前发送header/role | 首帧后失败以HTTP200中的SSE error报告；不能再把所有400/503当成HTTP状态 |
| SSE存活 | 空闲5秒注释心跳、单次心跳写截止1秒，与payload共用串行writer | 不计token或首内容TTFT；长GPU操作仍按已有可取消边界回收 |
| SSE尾帧 | choices显式logprobs:null；请求usage时普通块usage:null，最后独立usage | 写失败后关闭，不追加第二终态；严格工具参数仍非逐字增量 |

[机器响应schema](serve-transport-v1.schema.json)覆盖error、completion、
chunk和readiness封套。流内身份、顺序、nullable字段由独立wire检查
补足；这不是完整请求OpenAPI。能力端点同步上述实现边界。

## 验证结果

CPU OFF162/162、ON169/169；Thor发布前128/128及后端7/7；原生
HTTP生命周期7/7，末次日志出口改动针对性1/1；最终驱动9/9。
构建零警告。接口清单35字段/8路由/59源码锚点，3种漂移变异均拒绝。
CPU合同覆盖重复/非法头、显式param、空闲心跳与帧串行、写入绝对
截止、控制连接饱和、就绪/排空状态，以及关闭后晚注册请求。

真实验收采用单槽、max_prefill=2048、max_len=8192。累计12次生成
相关请求尝试（含一次过载拒绝、一次180秒未到写阶段的取消）、9次
专门负例、3次模型启动。所有模型阶段持完整共享GPU lease。

- 生产ELF的普通SDK回复710003正确；strict SSE真实读取
  docs/MODEL.md第1～8行并回填，回答Qwen3.8-Flash-Next (qwen4_exp)
  与引用`[docs/MODEL.md:L1-L8]`正确。工具/回填为3.548/3.282秒，
  仅作本次业务观察；SDK 3.42.1严格解析及响应头request ID通过。
- 专用构建中的真实loopback转发代理设置7秒上游idle截止；收到了
  5.00017秒间隔的注释心跳，代理和客户端字节一致。严格请求取消后
  收到error＋DONE，普通排队请求正常完成；负载下ready200、忙时
  structured_capacity_exhausted、短deadline的SSE错误均正确。
- 普通增量SSE使用小发送/接收缓冲，真实send返回EAGAIN后才创建
  测试握手。取消202后约12ms释放握手，接收2774字节的截断流，
  无成功finish/DONE；随后普通回复710003和schema结果323/中文
  通过。12ms是这次握手观察，不是通用取消延迟上界。
- 活动strict请求收到SIGTERM后报告server_shutdown，worker reap、
  0 in-flight、全部1606项startup allocation释放。取消后资源归零
  分别观察，未把202响应当成已释放。
- 19份响应记录中的30个完整封套通过响应schema；故意截断的慢读
  响应由专门规则检查，未冒充完整JSON。

生产ELF没有故障宏。代理/混合/背压/活动关闭的最终组合使用与生产
同源的专用构建，仅缩小发送缓冲并在真实EAGAIN处执行有界握手。
普通/严格工具的真实质量与头协议有独立生产ELF证据。CPU状态机和
这些本机链路不代表所有反向代理、长prefill或满租户组合已验证。

## 失败、复用与未证明项

1. 首次guard相对路径导致祖先进程校验拒绝：0请求/0启动；改绝对
   路径。原lease记录保留，最终总时间也计入这次launch。
2. 第一轮3个成功请求后，驱动只等GPU slot释放就发起下一严格请求，
   但旧worker/租约仍在清理，服务器正确503。修正驱动等待owner
   清理；复用已过普通/工具/回填和8负例，未改运行时。驱动CPU检查
   另暴露同名verify模块导入，修正为精确路径；失败日志保留。
3. 第二轮严格长输出180秒仍在decode，完整payload尚未发布，故没有
   形成预期TCP背压；按预算停止，完整关闭。该项保留为未观察，未
   改写成功。最后改用普通增量SSE专测共用writer的真实背压。

**严格长payload的实际背压组合尚未证明**；逐token工具参数流、
所有长prefill/代理配置、完整嵌套错误定位、所有SDK、HTTP复用、公平
排队、多租户与网关鉴权均不在本次接受范围。严格缓冲造成的长等待
仍需客户端理解，不能把角色/心跳到达算成模型首内容。

安装后校验pair身份、运行安装路径CLI与私有worker握手（VmHWM报告
约174.58MiB）。HTTP健康复用安装前已通过的**同SHA生产ELF**普通/
strict工具与回填；没有额外声称安装后重新加载模型。工作进程仍受
2GiB RLIMIT_AS，不是整机RAM限额。首次launch到finalize1199.49秒，
未扩大30分钟/3启动预算；最终guard COMPLETED，ENV_ABORT0。

00:03 CST复查GPU计算PID空、锁可获取、无恢复标记和常驻模型服务；
MemAvailable约116.27GiB。未主动丢弃系统文件页缓存。

## 证据和回退

[363份原始记录及封存清单](evidence/serve-stage-a-20261103/sealed-files.json)
含CPU/构建、失败、三阶段计划、SDK/HTTP、metrics、proxy、实际EAGAIN
握手、资源回收、guard与交付收据。ELF、CMake缓存与Git bundle保留
在本机`.q4t-work/serve-stage-a-{20261102,finish-20261102,final-20261102}/`，
仓库另存它们的身份收据。最终封存目录是
`.q4t-work/serve-stage-a-final-20261102/{package,execution}`。

旧pair为c8c412c4/da9f3ac5，`package/previous`及其缓存经hash核对。
回退入口`tools/serve_stage_a/closeout.py rollback --output ABS_DIR`要求
冻结的62eea34源码、同包输入与新完整GPU guard；源码漂移或未知pair
拒绝覆盖。源码bundle和绑定old/new哈希的替换记录均已封存。收尾
文档/清单更新不等于冻结源码更新，回退时须先恢复对应工具环境。
