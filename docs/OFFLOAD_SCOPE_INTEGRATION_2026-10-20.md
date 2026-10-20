# Offload限额工具与GPU lease接入

状态：草稿CPU合同已验证，尚未接入生产工具/真实GPU，目标仍active。
旧systemd service由PID1托管，外层GPU guard不能仅凭其名称证明归属。
不能将任意NVML PID或同名unit列入白名单解决。

候选采用systemd scope，执行器保留调用者后代关系，同时设置memcg
MemoryMax/MemorySwapMax与IOAccounting。机制见
[systemd-run官方说明](https://www.freedesktop.org/software/systemd/man/latest/systemd-run.html)。
当前草稿位于.q4t-work/offload-foundation-20261020/scope-staging，
SHA清单见同阶段scope-draft-manifest.json。未改动正在执行基线的源码。

候选合同：随机Description token证明unit所有权，拒绝复用既存unit；
payload PID/starttime/cgroup绑定，pidfd发送定向信号；独立helper记录
真实子进程退出并保留cgroup至多15秒，客户端仍在scope外。helper的
少量host charge计入资源观测，memcg仍不等于Thor物理RAM总量。

已完成CPU实机：正常退出及后代归属、TERM、KILL、执行路径不存在
四种情况均清理scope；MemoryMax=128MiB、swap=0实测属性符合。
7项主机合同覆盖非法名称/限额、既有unit、替换所有者拒绝清理、
缺失退出记录、错误token和pidfd发送。它们不证明GPU载荷与监控兼容。
CPU探针发生于描述性基线期间，无GPU载荷；全局设备/系统计数不作
独占归因，不能据本轮记录声称纯测量环境或严格性能比较。

剩余：基线封存后接入两个旧runner及run_acceptance，完整纳入工具
身份清单；核对异常清理、旧证据兼容及相关host合同，再以单一GPU
lease下有界HTTP验证真实进程归属和资源保留。无需为工具接入重建或
替换已验收运行时；基线身份与新工具验证分别记录。
