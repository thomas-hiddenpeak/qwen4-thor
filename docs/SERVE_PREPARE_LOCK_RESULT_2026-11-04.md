# SRV-03c：准备阶段等待取消限定交付

2026-11-04。生产修复d4b5c3f3，原生夹具修正530824ba。
范围见[冻结合同](SERVE_PREPARE_LOCK_2026-11-04.md)。默认已替换为
`8e9b4ce7…`，sidecar `e61bed7c…`，协议`a56aed21…`；完整身份见
[机器结果](evidence/serve-prepare-lock-20261104/result.json)。

thinking准备、prompt encode和preflight等待tokenizer锁时，现在检查
等待请求自己的取消、断连、截止和关闭状态。等待者可在owner仍持锁时
返回，owner的工作保持。获得锁后再次检查；既有JSON/SSE错误、计数及
清理路径继续使用。检查间隔5ms属于协作机制，不是OS实时保证，也不
中断运行中的tokenizer或GPU计算。preflight仍没有显式取消注册。

## 验证与失败保留

| 证据 | 实际结果与范围 |
|---|---|
| 旧生产锁 | 显式取消/截止两项均在owner额外持有200ms期间未返回；释放后取得锁，分别260.354/260.236ms。退出0仅表示复现缺陷 |
| 构建 | 主程序及真实handler探针零警告；初次探针缺生产reservation/quota/cleanup链接失败保留，修复后定向构建 |
| CPU | 294公共host、260必需合同、9结构化通过，数量有重叠；覆盖正常/已取消、等待取消/到期/断连/关闭、handoff取消和异常后锁可用、等待诊断 |
| 清单/生成物 | 38字段/10路由/20白名单/112锚点保持；首次遗漏CMake锚点失败保留，评审后只重验清单，其他未变身份CPU证据复用 |
| 原生handler | 23场景实际使用Chat/preflight/HTTP代码与socketpair；确认owner仍持锁时返回、正确JSON/SSE终态、body/准入/registry/client配额释放与锁复用；无模型加载 |
| 原生首轮 | 前16个普通/thinking JSON/SSE场景完成；scoped夹具未填策略hash而失败，guard COMMAND_FAILED，0模型/0生成，未改默认 |
| 原生补验 | 用生产策略parser修正夹具；原16观察按未变行为复用，仅运行scoped四项及未执行的preflight三项。不能称为一次23项全组成功 |
| 真实业务 | 1模型启动、6生成尝试：1预期取消、1 thinking budget=0恢复、两客户端各工具调用/最终作答共4成功。实际读取docs/MODEL.md 1–24行并核对SHA/引用及架构字段；4个grammar worker全部回收 |
| 安装健康 | 相同已验ELF替换默认pair，CLI身份通过，复用该ELF已验HTTP；未再启动模型 |

固定业务配置为本机Thor、MTP k3完整head、单槽、8192上下文、2048
预填充、本机临时TLS双Key及独立运维端口。第二阶段53.465秒业务、
55.931秒租约，52次观察未见竞争/不可读进程；首轮失败3.437秒、4次
观察同样未见竞争。轮询不能排除短于采样间隔的干扰。

收尾核对3个自有子进程均已退出，18136/18137/18456释放，GPU计算
上下文空，原锁dev66305/inode3294067可获取。恢复单元仍masked/
inactive/dead、MainPID0，容器空；原包/Key和三份仅plan的day计划保持。
没有安装常驻服务或执行小时/天级测试。

## 交付、复用与后续

223个运行来源中218个不变，差异仅两准备源文件、两个控制/诊断头和
探针CMake接线；模型数值、tokenizer算法、模板、缓存及调度实现未改。
本轮是可靠性业务修复，无性能优化或新数值通过声明。原生探针未创建
缓存快照，缓存lease析构继续依赖未变合同，不将其计入新增实测覆盖。

回退pair与声明/schema、来源manifest、CMake及原接受依赖保存在
`.q4t-work/serve-prepare-lock-20261104/package-native-fix/previous/`；
原默认61abe09f/ce925d51及相关路径保持。新协议a56aed21尚未登记持久
任务迁移，也未对新ELF重做托管生命周期；11k/12l的15d7ccb1旧包证据
继续按原身份使用，不自动升级或重放任务。

[283条封存记录](evidence/serve-prepare-lock-20261104/manifest.json)对应
311个本机文件身份，归档SHA256
`c2f59bc80b92072a7c00b14323fb7588099719e798be65e7bf261d54ec03823c`。
逐条字节/hash、原始SSE与工具来源、guard和安装记录已离线重建核对；
含secret的请求、Key/私钥和ELF仅保留本机hash引用。归档不是灾备包。
[核对工具](../tools/serve_prepare_lock/report.py)不新增HTTP/GPU业务。

下一项基础工作是标准库客户端总截止，以及JS辅助类可选Key/CA接入；
协议迁移、多人公平/物理RAM分别按限定范围推进。高级部署仍由实际
执行时选择，长验由用户开展，不宣称serve全层无遗漏。
