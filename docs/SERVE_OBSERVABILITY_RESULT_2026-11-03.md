# SRV-01b结果：接收拒绝与首次输出失败已限定交付

2026-11-03，**ACCEPT_BOUNDED_OBSERVABILITY / DELIVERED**。接收拒绝有固定
原因，body聚合容量耗尽有专属错误；输出保留首次失败原因、来源与部分
写进度，心跳join后发布计数和trace日志。生产候选已成为默认程序。
这是业务可靠性修复，不作性能非劣或整个serve工业化完成声明。

## 身份与执行

| 项目 | 身份或结果 |
|---|---|
| 冻结运行时/驱动 | 2425044b49aaab3b8754319b5bf33a135e9c5911 |
| 默认生产ELF | ed672a65b586f8445e51e3ba6d63659b4a098386e87e1ed38e3c8893a352a139 |
| 默认sidecar | 67897c817dfccd6db3d4e2c0d0af3cfc70fb97a2c840887905c03fbf0bd6be23 |
| 专用验证ELF | 4da3c5d095e4723a60a87e51f9aa681ac1b6a13ab45fdd7fa926dabb2e892762；不安装 |
| 协议SHA | 0e252868586d1d78e59fd951f8e327d7cb5f6a0e903148a622198decd6645a8f |
| 运行时文件 | 193；新增观测头文件，模型/数值/模板/分词/grammar源码未变 |
| 实际尝试 | 8生成、16接收拒绝或业务边界用例、2模型启动；首轮完成，无重试 |
| 成功与预期中断 | 5个完整生成，1个主动TCP reset、2个有意失败的普通增量流 |
| 时间与环境 | 业务148.955秒，首次锁到finalize179.515秒；guard COMPLETED、ENV_ABORT0 |
| 预算 | 原硬限10生成/20拒绝/3启动/1800秒保持，备用额度未用 |

原生HTTP生命周期16、两次模型启动、安装健康由同一GPU互斥驱动持锁。
生产与专用构建的运行时源码和协议相同，仅验证宏不同；专用缩小socket
发送缓冲，并在首次真实EAGAIN处有界暂停。没有注入生成字节或修改logits。

## 接收侧实际证据

生产组完整head、默认MTP k3、单槽8192/prefill2048、inflight8/queued2，
只监听loopback18103。三个只发送头的16MiB请求占满48MiB预留，chat与
preflight随后各被拒绝一次：HTTP503、code=body_capacity_exhausted、
param=body。拒绝没有释放其他请求的额度，健康/ready/live仍可访问。
三个连接半关闭后收到body_eof错误，所有body额度归零。

这只验证了聚合预留，不是实际分配了48MiB payload，也不是物理RAM上限。
后续真实HTTP逐项拒绝malformed、错误媒体、未知路由、method、query、
GET body、body超限、header EOF；并行header/body慢读均按原30秒截止。
合法HTTP的空messages业务校验返回400，但不进入receive计数。

16个用例与15条接收日志对账：body_eof=3、body_capacity=2，malformed、
body_too_large、header_deadline、body_deadline、header_eof、unsupported_media、
method、query、unexpected_body、route各1。其余固定原因0；模型prompt/
generation计数在本组前后未增，拒绝响应均保留trace与原始wire。

## 输出侧与真实工具闭环

合法strict SSE请求占槽后主动TCP reset，已收前缀保留为未知、不完整
轮次。实际首次写失败为handler/peer_closed，errno=104，失败逻辑写
transferred=0；chat结果为client_disconnected。随后slot、queue、body、
strict/worker全部回收。普通JSON恢复准确回复710003。

固定Node24.18.0/openai7.47.0、零自动重试完成严格read_project_record，
只读真实docs/MODEL.md第1..24行；按调用ID回填，最终准确回答
Qwen4ExpForConditionalGeneration，包含实际引用[docs/MODEL.md:L1-L24]。
参数、原始wire、工具结果、历史与usage均保留，生产组worker3/3已reap。

专用plain同容量/准入组使用普通增量SSE逐行列整数，客户端connect前
缩小接收窗口且不读。首次实际EAGAIN后控制健康可用、取消202成功；
记录handler/explicit_cancel一次，失败逻辑写已有157字节发送进度。
客户端最终获得3428字节、13个完整事件及末尾前缀，未出现成功
finish/usage/[DONE]；全过程约1.611秒，不把它记为成功assistant轮次。

随后独立请求不再暂停或取消，保持不读直至原30秒逻辑写截止；记录
handler/write_deadline一次，同样157字节部分进度。约31.480秒后取回
3430字节、13个完整事件及末尾前缀，没有成功终态。后续短路写不增加
失败计数。普通JSON和短strict {"ok":true}恢复通过，worker1/1已reap。

两个实例最后均槽1/1空闲，active/queue/body/strict/worker计数0。
接收15条、写3条日志分别与固定34个metric序列精确对账。5份完整生成
wire/11封套、16份拒绝封套通过机器合同；3个预期不完整生成单独保留。

## 观测解释、CPU与未证明范围

- `q4t_http_receive_failures_total{reason}`：每连接首次传输/路由拒绝，
  不包括chat业务校验。`q4t_http_write_failures_total{origin,reason}`：
  每连接首次写失败，包括错误响应本身的写失败。不能与chat成功率相加。
- labels固定，不包含trace、path、prompt、工具参数或errno文本。
  trace只关联日志；transferred只表示失败逻辑写送入本地socket的字节，
  不是连接累计量或客户端收到的承诺。context先join心跳再发布观测。
- OFF214/ON223、必需180、原生写异常2、grammar后端9、锁内HTTP完整16
  通过；驱动10项、失败取证修订受影响1项与计划输入7项native通过。
  生产/专用/probe均零警告。heartbeat、部分头异常和取消原因排列等
  由CPU生产函数/socketpair验证，不声称每项都单独跑过模型HTTP。
- 必需门初次实际180项全部通过，但合并stdout/stderr让诊断插入PASS
  名称，检查器误判。原证据完整保留；分流采集后同二进制180定向通过。
  其余已通过项复用；模型数值、模板等未变依赖复用既有证据。
- 这是普通增量输出的实际背压。**SRV-03b严格长payload仍未证明**，
  原失败和预算不改；本批不证明公网鉴权、长稳、整机RAM执法或任务恢复。

15个稀疏阶段观测中parent RSS最大944.492MiB，MemAvailable最低
34.558GiB，无采样错误。这些时间点没有活跃grammar worker，worker
采样最大值明确为null，不填0。另4条退出日志的post-exec VmHWM最大
224.012MiB；wait4 lifetime最大3903.125MiB包含exec前继承峰值，单列。
不把退出日志改称采样峰值，不相加当唯一物理RAM或整体运行峰值。

## 交付、回退与证据

仅安装生产ed672a65与sidecar67897c81；安装路径CLI和隔离worker握手
通过，HTTP健康明确复用同SHA生产候选的普通/工具证据，无额外安装模型
启动。旧c16caf05/be4cd021、build cache和完整历史回退链保留。
07:55:29 CST GPU计算PID空，共享锁可用，无模型/worker/恢复标记，
MemAvailable117.013GiB。没有drop_caches或干预其他服务。

需要回退时，在本仓库根目录使用新的独占输出目录：

```bash
Q4T_ROLLBACK_OUTPUT="$(mktemp -d "$PWD/.q4t-work/serve-observability-rollback.XXXXXX")"
/usr/bin/python3 -B "$PWD/tools/evalscope/gpu_exclusive.py" \
  --output "$Q4T_ROLLBACK_OUTPUT/lease" -- \
  /usr/bin/python3 -B "$PWD/tools/serve_observability/stage.py" rollback \
  --output "$Q4T_ROLLBACK_OUTPUT"
```

回退检查已封存pair/cache与已知新旧身份，不依赖后续开发工作区；
未知身份或外来恢复标记拒绝覆盖。没有为本次交付实际执行回退。

[原始归档](evidence/serve-observability-20261103/README.md)按字节复制303
份记录，共307文件/7339032字节；含原始检查器失败、CPU/真实wire、
metrics/日志、源码/模型身份、GPU guard、安装和回退绑定。8份ELF/
object/source.bundle登记本机身份，原路径和所有回退包继续保留。
仓库副本、package和execution各有seal；本地根目录
`.q4t-work/serve-observability-20261103`不得删除。
307份Git暂存blob逐字节核对通过，原.bin wire已显式加入；原始
CMakeCache末尾空行保持。558处本地文档链接核对通过，封存回退计划
按只读方式核验，未运行模型或实际回退；见[入库核对](evidence/serve-observability-custody-20261103.json)。

下一步先离线核对03b载荷生成、真实线长和截断取证前提，不自动恢复
旧实验或只增加token预算。访问运维、长稳、通用任务恢复仍继续排期；
长期工业化目标保持active。
