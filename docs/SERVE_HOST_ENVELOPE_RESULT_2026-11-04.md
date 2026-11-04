# SRV-02k：托管后端host记账域与进程树边界限定接受

2026-11-04。冻结实现`70b629db`，业务可靠性任务。显式v5后端包与
托管网关v3、155项CPU、六类真实内核边界、1模型/1TLS网关短业务
已通过。新增host memcg/pids约束不等于完整统一物理RAM硬上限；
此前Thor CUDA漏计结论保持。长稳、正式LAN及完整物理RAM保护仍待办。

## 实现与兼容

`prepare`同时显式提供host高水位、最大MiB和任务数时生成v5包；
可叠加原客户端策略。工具身份和资源策略绑定manifest，旧v1-v4包
仍可核对并保持原行为。托管网关新增v3快照携带v5校验依赖；旧网关
包不自动更新，不把新manifest兼容能力反写到旧包。

每代次由systemd user manager建立唯一委派scope。监督worker进入
control，后端helper在exec前回读限额并进入payload，子孙继承。
GPU guard/controller在scope外，进程父子关系及原flock保持。
只操作本次token/InvocationID、inode和UID核对过的目录；无法建立
或确认限额时拒绝启动。payload设置swap0、OOM按组终止；control
另有512MiB/64任务限制。pids同时计线程，不是仅限制主进程数量。

OOM、pids拒绝、限额漂移或资源观测失败均终止且不自动重启；回收成功
与业务失败分别记录。退出后保留内核计量、确认空payload并移除，再由
外层controller确认scope消失。没有安装自动服务或恢复业务的脚本。

## CPU和真实内核证据

155项CPU首轮通过，包含原托管/内存/死亡/核验、包和网关/留存合同，
以及新增资源策略、v5/旧包、OOM禁重启与观测失败边界。新增真实短验
驱动2项CPU通过，验证环境占用分类及应用→网关→后端清理顺序。

| 内核场景 | 实际观察 | 生命周期 |
|---|---|---|
| normal | 正常分配与停止 | 0.469秒，scope退场 |
| high | 32MiB高水位，18次high事件，无OOM | 0.470秒，正常停止 |
| OOM | 64MiB上限；oom1、oom_kill3、oom_group_kill1 | 0.419秒，FAILED，0重启 |
| tasks | 8任务上限；实际fork返回EAGAIN，max事件1 | 0.419秒，FAILED，0重启 |
| drift | payload pids.max被探针修改后拒绝继续 | 0.419秒，观测失败，0重启 |
| death | 对自有监督worker实际SIGKILL，payload退场 | 0.310秒，原worker终态缺失保持 |

原high探针等待40MiB分配完成，内核持续节流导致25秒超时。原失败和
清理记录完整保留，未计通过。只修正测试收尾为“观察到实际high事件
后正常停止”，1定向回归通过；生产实现未改，normal及155项CPU复用，
只续验high和未运行的四类场景。未放宽原30秒边界，也未让探针造成
全局OOM。六类接受证据之外，这次原失败单独保留。

## 实际TLS与工具业务

候选策略明确为payload high16GiB/max20GiB、pids512，swap0。旧默认
ELF/sidecar及API Key保持，两个独立客户端通过loopback TLS访问。
alpha512、beta64输出额度下，各完成2生成/1真实资料读取；共4成功、
2只读工具。两次都返回`Qwen4ExpForConditionalGeneration`，并引用
`[docs/MODEL.md:L1-L24]`，与实际源文件及保存的工具结果匹配。

每客户端两轮输出55/58 token、输入647/890 token；SQLite事件、
保存的响应、实际backend request ID及TLS日志相互对应。10条网关
记录为各客户端1能力查询、2预检与2生成。终场metrics记录4成功、
4结构化worker启动并回收，0活跃请求/排队/结构化资源。

host记账峰值17,109,327,872字节，约15.934GiB；memory high/max/OOM
及pids max事件均0，swap0。70份短业务采样观察到主后端与4个实际
结构化子进程，全部处于同一payload；此期间任务计数最高8，不能
外推为包含模型启动的完整线程峰值。内核memory.peak覆盖该组全生命周期，
但不等同全部物理RAM；历史文件页缓存和未计费CUDA尤其不能据此相加。

总71.024193秒、GPU租约60.127035秒、57次guard观察、5个父控制调用。
一把原锁贯穿后端生命周期，没有中途交接。轮询未发现竞争，不证明
采样间隔内没有短暂干扰。不作性能优化、五档性能或长时间稳定性结论。

## 清理、证据与交付身份

两个受管代次SEALED，11个自有进程身份退出，backend/control/payload
及scope退场，10秒无复起。GPU计算上下文空、原锁dev66305/inode3294067
可用且未删除，容器空，SOAR恢复单元仍masked/inactive/dead/MainPID0。
三份原day计划摘要未改且没有执行，原固定包/Key/默认pair保持。

- 后端v5：`.q4t-work/serve-host-envelope-20261104/backend-package`，
  manifest `87980cdf4e990d3f88d7b778a92083a89bd0a983fb180c418e8c6c1c135c35cc`。
- 托管网关v3：同阶段`gateway-package`，manifest
  `db0aaaa1a2232bc45d671bb7767cec87c95d97e1840be220622dc0e242cc125e`；
  内层网关`40be54f9c63b126903a641a8f78635067099508f9ccb9eef19ff09e2be5be9c8`。
- ELF保持`7619966ef2a5101e9a4bbc56d20805d980f85324e0313a71c248c701df6aa501`，
  sidecar保持`a2f9cb8feb2fab94917b4d4ad1b22020efe42c2416b87306873d15d26b71a263`。
- [385条原始记录与源码快照、2个ELF身份](evidence/serve-host-envelope-20261104/README.md)
  独立离线重建通过；压缩504329字节，SHA256
  `22a44409eea5ad5ec86bfea01490f1de48d69578a9d788dc5207a46077e96c20`。
  不包含API Key、gateway token或私钥；两个ELF留在本机，归档只记身份。

接受范围仅为这组具体包及策略，prepare自身未验声明不被改写。原v4
后端/网关v2包是保留的回退入口；切换必须先停止新网关再停止新后端，
确认GPU/锁释放，使用全新的运行目录启动旧组合，不能覆盖旧验收目录。
本次没有重写默认ELF或强制迁移任何day计划。

[操作入口](../tools/serve_service/README.md)提供显式准备/运行/停止；
新资源策略需在新运行目录启用，不能在活跃代次修改限额。实际内网
地址、DNS/CA、网段及正式配额/备份介质仍须按部署确定；本机临时
证书与loopback成功不代表正式LAN验收。用户长稳不在本阶段执行。
