# SRV-10b网关托管、依赖身份与有界日志

用户目标仍是完整serve工业化；上一轮是实际进展，已交付10a的TLS/
独立Key和本机双client业务。本轮补已确认的运营缺口：此前入口只
前台exec，日志需外部收集，启动后不再核对后端。实际LAN地址/域名/
证书路径及来源网段已异步询问，先完成不依赖这些参数的开发和短验。
长稳继续由用户专门执行，不启动SRV-18 day。

## 产品合同

- 新`tools/serve_gateway/managed.py prepare/run/unit/status/stop`。
  prepare复制固定nginx、当前网关与托管依赖源码、OpenAPI，私有包
  绑定Python/源码/输入SHA；run转到包内入口，后续工作区变化不暗中
  改写运行代码。既有gateway.py与runner保持，不改10a/day冻结依赖。
- 每代次用新私有运行目录，禁止覆盖；同包持flock并向nginx继承，
  主/子边界设置parent-death。stop按PID/start/boot与pidfd通知托管
  进程，先持久化意图，返回STOP_REQUESTED不等于清理确认。失联是
  OWNER_ABSENT，历史终态不被修复或覆写，遗留nginx PID文件不自动删。
- 初始READY后端必须是已核验托管实例、固定ELF/同namespace/loopback。
  运行中每秒核对controller/worker/backend和监听inode/ELF，后端
  停止/更换或观察不可信即关闭网关。忙或DEGRADED且身份保持不会
  当成崩溃。检查不是原子网络隔离，轮询间隙不能排除可信本机进程
  短时替换端口；不宣称能隔离不可信同机用户。
- 同时核对nginx监听属于自有master及固定IP/ELF、私有配置/证书/key
  内容身份。只在校验后报告READY；失败锁存，不自动重启master、
  不刷新绑定到新后端、不重放HTTP/工具。nginx内部worker管理仍由
  nginx负责，本合同不把worker正常替换描述为新模型启动。
- 正常stop给master SIGQUIT排空，[nginx官方信号语义](https://nginx.org/en/docs/control.html)
  支持该行为；最多35秒后记录排空失败并进行TERM/KILL有界回收。
  异常直接回收本轮nginx组，只处理自有PID，不向模型后端发信号。
- stdout/stderr在私有目录轮转为2×4MiB，事件2×256KiB，共8.5MiB，
  状态JSON另计且覆盖为完整快照；写日志失败停本轮网关。字节轮转
  可切断记录，不等于完整历史；跨运行目录总磁盘、日志外送及告警
  接收仍属实际部署策略，不能凭每代次上限声明全盘有界。
- unit使用包内固定入口、Restart=no、KillMode=mixed与60秒停止窗；
  mixed先通知主进程让其排空，随后由systemd兜底清理剩余组，见
  [官方生命周期说明](https://www.freedesktop.org/software/systemd/man/latest/systemd.kill.html)。
  没有Install段、timer或自动部署；本轮仅临时unit验收后删除。

同用户/管理员为可信控制面；manifest SHA是身份锚点而非签名认证。
私有Key/配置/私钥不入Git。nginx外部依赖的系统动态库仍属本机环境。
证书有效期在prepare/启动核验；本批不承诺零停机证书/Key轮换。

## 冻结验收

业务可靠性任务，不是性能优化；模型数值、模板、GPU算法、默认
d66f2bb9/42d7672a及10a输入输出合同不改。先统一18项CPU（真实子
进程/孤儿清理、排空/超时、日志失败/轮转、互斥/拒绝symlink、停止
幂等/PID复用/失联、后端身份、真实nginx包/隔离入口/systemd语法）。
没有HTTP或GPU的CPU可先执行；失效case修复后只重验受影响项。

模型短验一次加载、四个显式网关代次，全部127.0.0.1:18444，复用
已接受v3后端160796c3（18123、seq1/8192/full-head MTP k3）。总计
8生成：6成功、2预期断流；1真实资料工具与引用回填、1strict JSON。

1. 临时systemd网关：普通、strict、真实工具/回填共4成功；第5个
   活跃SSE在正式停止网关时排空成功，确认backend保持READY/资源回收。
2. 独立前台网关：活跃SSE时仅SIGKILL网关托管进程。parent-death
   清理nginx，无辅助信号；历史保持失联，无伪造终态/成功响应。
3. 独立前台网关：活跃SSE时仅SIGKILL nginx master。托管进程清理
   残余worker并写FAILED/cleanup，不能自动重启，backend保持正常。
4. 新网关普通请求成功证明同后端恢复；最后显式停止backend，网关
   发现依赖消失自行停止、失败与清理分列，自己不发送backend信号。

GPU≤360秒、总≤420秒、显式控制≤128（留8给清理）、1模型/4网关
启动、8生成，无自动重试/扩样；生产监督GET保守上界300另列。整体
使用后端生产guard租约，并复制**同一flock描述符**独立监控到最后
网关清理，避免后端先停时出现GPU租约空档；不另取一把锁。
最终无自有进程/监听、GPU空、原锁释放、10秒无复起、临时unit删除。
所有失败/断流wire保留，失败停止自有实例；不会干预别的GPU任务。

通过后更新结果/状态、运行说明与阶段提交推送；实际LAN/CA/DNS/
防火墙、常驻通知、整机物理RAM硬保护与小时/天级稳定仍需独立证据，
工业化目标不能由本轮窄范围托管证明为已完成。
