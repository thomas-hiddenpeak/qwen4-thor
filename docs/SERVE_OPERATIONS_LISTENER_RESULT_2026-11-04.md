# SRV-12g：独立本机运维入口限定交付

2026-11-04。按[冻结合同](SERVE_OPERATIONS_LISTENER_2026-11-04.md)推进
serve可靠性，**限定接受并已替换默认程序**；工业化持续目标仍active。
新增入口须显式启用，未配置时operations-port为0，不自动启动服务。

## 交付行为与身份

- `--operations-port`固定绑定127.0.0.1；主入口与运维入口分别计量，
  主入口默认共136个handler，运维独立8个（观测4/控制4）。主入口未完成
  请求头不能占用这8额；运维自身未分类头仍共用8额，不保证抵御本机洪泛。
- 运维仅健康、观测、发现、取消，生成/preflight/未知方法路径在body前
  拒绝。客户端认证、控制额、取消凭据及跨client隔离保持；本机管理
  GET仍不接受客户端身份。两个socket同时建立后发布，异常/退出共同回收。
- observability v5提供101条固定series和显式配置；旧v3/v4规则单独保留。
  诊断可带私有策略，只在能力发现发送内部身份，发送前核对release/ELF/
  双监听并复查归属；不把身份失败当健康，也不向已知错误监听发凭据。
- 托管包v6显式封存运维端口；监督器核对两个监听并经运维口取样，inspect
  同时检查两个socket。policy v2可选`upstream_operations_port`，TLS控制
  请求走运维口，生成/preflight走业务口。地址只由固定配置选择，旧包不升级。

| 身份 | SHA-256 |
|---|---|
| 当前默认ELF | `7eff020de66b96f3175b3e123909be9e2f58142d54d1dd5992750d23cbe9c769` |
| 当前sidecar | `abfaeff90bf460f1ff0cf0ee1f32da70d186e5ada0ab735e4576fc0d51c78e53` |
| 当前协议 | `ddfba8c002d8fb899b4c00d42612b0a26669036a1acc65e8292a10c0a9b69342` |
| 前版ELF | `49cc78cdf03ba56f7a9b51f682cc61e636f6b36d2784289071b3c6ac5352c38a` |
| 新v6包manifest | `c4775a10cf6c2d308729a0e6decd0f1178d4aa8f27f50391d7b090e96e1a349a` |

生产实现5e52dc17；eb86676b修正启动命令、f25e65a8修正原生夹具、
be6f6345修正业务验收器。三次修正均不改变候选生产ELF。222运行来源
中212项完全不变，10个文件涉及服务入口/选项/发现/指标及格式。
模型数学、分词、模板、MTP/grammar调度和cache路径保持；无新数值或
性能优化接受结论。默认替换沿原pair事务，同ELF的实际HTTP证据复用，
安装后CLI健康通过，guard完成后解除本阶段恢复标记。

## 检查与真实业务

- 两套主机/产品构建及定向原生重建零警告。173个唯一Python基础用例，
  289公共/255必需host合同、9结构化后端检查通过；诊断凭据复查33项、
  最终驱动5项、各次修正2/2/3项通过。Prometheus v3/v4/v5分别26/31/41
  个官方时序场景通过。CPU检查不启动HTTP或模型。
- 修正后的原生组33/33：双bind回滚、小容量主入口慢头隔离、
  两入口分类额独立、运维8额及释放、body前路由、认证/跨client取消、
  发布前异常与双入口退出。原生HTTP与模型HTTP均在GPU独占保护内。
- 首个真实模型窗口中，135慢头加1活跃生成占满136主handler：运维
  诊断HEALTHY、监督READY，TLS models/capabilities200；公共溢出503、
  运维匿名发现401、带身份metrics403、错误Key401。后续复用这些原件。
- 补验只重建取消所需压力，TLS本机管理路由404、跨client取消404，
  同client经运维上游取消202；SSE明确`explicit_cancel`错误终态与DONE，
  不伪装成功。释放慢头后普通恢复回答`710003`正确。
- alpha/beta各2个SSE响应、1次真实`read_project_record`执行，读取
  `docs/MODEL.md`第1..24行并给出正确architectures与引用。
  两客户端查询/回答各55/58输出token，分别遵守512/64预算；prompt
  为564/890。合计5成功、1明确取消、2真实工具，grammar开始/回收4/4。
- 最终业务/主连接/body/grammar owner归零、slot归还；运维连接1是
  最后一次metrics抓取自身。随后两个后端监听与TLS监听全部退出。

## 失败保留与范围收敛

| 窗口 | 原始状态 | 实际工作与处置 |
|---|---|---|
| execution | COMMAND_FAILED | 相对守护脚本路径不符合部署祖先检查；begin前退出，0 HTTP/模型 |
| execution-02 | COMMAND_FAILED | 进程内一次性注入已被旧用例消耗，新增夹具等待60秒超时；0模型/生成，旧stdout未刷出，不推断单项通过 |
| execution-03 | COMMAND_FAILED | 33原生通过，1模型/1生成尝试/18控制；压力观测通过后，正确网关401被误套runner schema，驱动清理退出；0成功/工具 |
| execution-04 | COMPLETED / DELIVERED | 复用原生和已完成观测/4拒绝，仅补1模型/6尝试/8控制、取消及5成功/2工具；通过后交付 |

分别改用绝对路径、可重复测试钩子/逐项刷盘、固定网关错误验证器；
没有放宽后端协议或生产行为。原包/原轮原路径保留，各次修正另建包与
输出并封存范围；未把失败guard改写为COMPLETED。累计2模型、7生成
尝试、26父控制计数；其中首轮未完成的生成单列，不记成功或明确取消。
6个原生/模型/网关自有进程身份均退出。

四个lease共159.294358秒、150次monitor观察，均无记录的竞争或监控
失败且原锁释放；轮询不证明采样间隔内无短暂外部干扰。最终
18132/18133/18452无监听且可SO_REUSEADDR绑定，GPU计算上下文空，
容器为空；SOAR恢复unit仍masked/inactive/dead、MainPID0。

## 证据、回退与边界

[结构化结果](evidence/serve-operations-listener-20261104/result.json)与
[逐文件清单](evidence/serve-operations-listener-20261104/manifest.json)
包含337份原件、5,033,292字节；893个本机引用逐项核对，未公开部分
含二进制、源快照及私有材料。原始API Key、内部token、取消凭据和
TLS私钥不入公共证据；仅有摘要的本机引用不是可离机恢复备份。

```bash
python3 -B tools/serve_operations_listener/report.py verify
```

旧pair、cache、v4规则保存在
`.q4t-work/serve-operations-listener-20261104/package-business-fix/previous/`；
已核验身份，未为回退包检查追加模型推理。新显式v6包为
`.q4t-work/serve-operations-listener-20261104/accepted-backend/`，已prepare/
verify，**没有启动或安装常驻服务，也不把该新包标为托管生命周期已验收**。

实际LAN地址/网段/DNS/CA/防火墙与远端客户端、正式通知/容量/备份
介质、整机物理RAM硬保护及新v6托管生命周期仍待。旧三份day计划和
私有输入保持、均未执行；旧day身份不能当本次ELF的长稳证据。小时/
天级验证仍由用户开展。持久任务的fc63db28→ddfba8c0协议交接尚未
登记，不自动放行旧任务到新协议；此项与真实部署/长稳分别推进。
