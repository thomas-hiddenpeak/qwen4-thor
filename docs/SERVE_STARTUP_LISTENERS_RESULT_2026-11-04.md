# SRV-12k 原生启动监听所有权验收

2026-11-04。结论：**ACCEPT_BOUNDED_STARTUP_LISTENERS / DELIVERED**。
生产和驱动提交 `d073eb8f794956158bd646017e80692439f588c7`，
[冻结合同](SERVE_STARTUP_LISTENERS_2026-11-04.md)。这是服务可靠性修复，
不作性能优化、全模型质量或工业化全部完成声明。

## 实现与实际边界

旧版在 Start 完成模型加载后、Run 中才绑定端口。本次在选项及客户端
策略合法后、tokenizer/cache/模型/CUDA 资源申请前实际 bind+listen，
公共与可选运维 socket 全程保留，避免 bind/close 预检的竞争窗口。
保留 SO_REUSEADDR、NONBLOCK、CLOEXEC；运维仍为 loopback 可选入口。

不可复制 RAII 所有权沿局部 pending → server → Run 移交。第二端口
失败、异常和加载失败关闭已取得的监听；Start 后不 Run、Run 前停止、
发布前异常及正常排空均回收。Start 单次使用，失败后的部分模型/调度
资源仍由 server 析构释放。错误携带角色、地址、操作及 errno。

启动期间内核 TCP backlog 为128，没有 HTTP handler/readiness，
**TCP connect 成功不能表示启动完成**。客户端须有界等待真正 HTTP
健康响应。本轮没有增加启动期503线程，SIGINT/SIGTERM安装时机保持，
也不声称已验模型加载中途的合作式取消或所有分配故障。

## 验证结果

- Release 与独立 host 构建零警告。42项发布/包 Python、289公共host、
  255必需host、9结构化通过；最终驱动5项通过。公共与必需有重叠，
  不把这些数字相加当作唯一用例总数。
- 首轮 inventory 发现5条源码锚点过时而拒绝；原失败保存。审查新增
  所有权、移交、字段和路由后登记112来源，3反例检查通过；其余已过
  组复用。最终门禁保留首次清单核对及完整来源，未重跑无关推理。
- 同租约41项原生生命周期全部通过，含复用地址竞争、socket flags、
  部分/完整双监听异常回滚、move/重复close/FD复用、无Run析构、Run前
  停止、启动队列在Run前无HTTP、EMFILE及原有请求/认证/取消/排空。
  原生fixture不加载模型；Start实际顺序另由以下CLI负例覆盖。

| 无模型CLI探针 | 实际结果 | 进程总时长 |
|---|---|---:|
| 旧默认占用端口、无效模型路径 | 仍先到tokenizer失败，证实旧顺序 | 0.036614秒 |
| 新版公共端口冲突 | tokenizer/内存观测前拒绝，errno98 | 0.040338秒 |
| 新版运维端口冲突 | 同上，已取得公共端口释放 | 0.040641秒 |
| 新版无效模型路径 | tokenizer失败后双端口释放 | 0.035123秒 |
| 无效选项 | parser退出2，未启动 | 0.032896秒 |

五次进程均使用不存在的模型路径；三个占用场景原socket保持并能接收
连接，退出后两个端点均实际重新listen。计时包含进程及取证，不是
模型加载提速对照，也不是争用环境的普遍时延保证。

真实业务1模型/1TLS网关、6生成尝试：5成功、1取消；6类HTTP拒绝，
23控制请求。135公共慢头加1活跃生成占满136时，独立运维诊断HEALTHY、
监督探针READY及发现可用；取消后普通恢复输出 `710003`。alpha/beta
各1工具请求+1最终回答，输出预算512/64；两次工具真实只读
`docs/MODEL.md:1..24`，返回 `Qwen4ExpForConditionalGeneration` 并引用
`[docs/MODEL.md:L1-L24]`。四次工具阶段响应约3.725–4.052秒；不是TTFT。
最终4个grammar全部回收、请求/准入/缓冲归零，运维采集自身计数保持1。

## 资源、交付与证据

业务驱动56.161914秒；GPU原共享锁持有 **58.011374秒**，54次monitor，
COMPLETED、子命令0；未观察到竞争或监控失败。轮询不能排除短于采样
间隔的不可见干扰。8个自有进程身份保留到guard确认后回收；主/运维/
TLS三监听和GPU释放，原锁dev66305/inode3294067可取得，恢复SOAR unit
仍masked/inactive，容器空。没有小时/天级验证或安装常驻服务。

默认ELF：`61abe09fb0d2585c4f8efa3e5740fa4d0ea5af06971e4490df2d17000453ecce`。

默认sidecar：`ce925d517adee1b7dfdc17c8c90b11e4bd06260e5a87701b6e6268a0fb0329fc`。

协议：`15d7ccb103258ea5ec8ca3cfa66c9bfe1245eadb7d38ad6ee149ad97075ec30e`。

223运行来源中219不变，3修改和1新增仅为监听生命周期；数值、模板、
缓存和调度证据按身份复用。实际capabilities/config与七schema封存并
校验新发布声明；同ELF默认替换后CLI健康通过，不重复加载模型。
旧7eff020d/cc83bf3a完整pair、cache、声明、源码清单和接受依据保存在
`.q4t-work/serve-startup-listeners-20261104/package/previous/`；本轮保留
并核验回退材料，没有执行额外回退推理。

新v6包manifest为
`51ccb951073cb923a7dc38c4b4ab9e072bb87649b13a77ed3efc1790d3c8ef46`，
已静态准备/核对；**不作为新ELF托管生命周期已验**。旧封存包、原Key
与三份用户day计划保持，day均未跑；持久任务的旧协议不会自动迁移。

[机器结果](evidence/serve-startup-listeners-20261104/result.json)、
[清单](evidence/serve-startup-listeners-20261104/manifest.json)及
[原始记录包](evidence/serve-startup-listeners-20261104/records.tar.gz)：
291份原件、323本机引用，压缩420733字节，SHA256
`9ed4a1dd316d0b919c83fec95969851dafaa10177c821c06da75939e675b0905`。
独立新进程离线重建通过。凭据/私钥/带取消凭据请求及ELF只保留本机
摘要引用，未导出；此归档不是源盘丢失灾备包。核对命令：

```bash
python3 -B tools/serve_startup_listeners/report.py verify
```

下一项先审核新启动行为在托管监督器的启动/停止及健康边界，再处理
新源码协议的持久任务交接。TLS/Key/CIDR/host域及实际LAN按需启用；
物理RAM硬保护和用户长验边界保持，持续目标仍active。
