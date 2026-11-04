# SRV-18 当前版本短期验证限定接受

按用户明确分工，本批完成开发与短期验证，长时间运行由用户专门
开展。冻结提交`0bd0e8fe`，复用SRV-17已接受v3包manifest
`160796c3fc65d44cb36967952ca300abd48eb8228f59a8afee597a6b685e8606`。
CPU11与真实short均首轮通过，未重复模型或HTTP寻找有利结果。

## 真实业务与清理

| 项目 | 本次实测 |
|---|---|
| 模型/时长 | 1次启动、同一PID/start，8周期，业务窗口120.000秒 |
| 成功响应 | 40/40，普通JSON/SSE、strict JSON/SSE交替、工具请求和回填 |
| 真实工具 | 8次读取`docs/MODEL.md`第1—24行，8次架构回答及引用校验 |
| 结构化资源 | 24个grammar worker全部回收；逐轮owner/queue归零、slot可用 |
| 连续证据 | 119资源样本；guard 154记录中150次观察无竞争 |
| 生命周期 | STOPPED、guard COMPLETED、1 attempt/0 restart、清理确认 |
| 退场 | 停止后10秒无复起；临时unit移除，GPU空、原锁可取得 |

每周期先并发普通JSON、普通SSE、strict三个请求，再真实执行项目
资料工具并回填。40个request_id/取消凭据互不复用；完整wire重新
解析与存档response逐项一致，均HTTP200并读到EOF。普通答案710003、
strict对象`{"ok":true}`、工具架构答案及引用均满足冻结合同。
工具执行来自客户端真实只读访问，不是模型编造返回内容。

模型加载、业务及停止排空全程在生产控制器的同一GPU lease内；GPU
窗口162.046秒，总172.580秒。46次显式控制，监督HTTP保守上限492，
合计538为上限而非实际HTTP次数。不作吞吐SLO、性能非劣或优化接受。
轮询没有竞争不证明采样间不存在短暂干扰。

## 开发交付和证据

新驱动区分short/day独立目录、固定预算和停止条件，临时unit带最长
时限；新增轮转日志复制、缺段拒绝、独立资源采样、小时检查点及
只读结果复核。CPU11覆盖真实文件轮转/UTF-8分割、缺段/截断/容量
拒绝、采样故障传播、资源包络和时间计数门槛、profile隔离、unit语法。

[封存清单](evidence/serve-soak-short-20261103/manifest.json)保留311原件，
另绑定已接受包的ELF与manifest两项本机身份，10份Git文件450,878字节。
包含CPU来源、冻结short/day计划、全部wire、工具结果、每轮指标、连续
资源与完整guard日志。归档及本机身份逐项核对通过；只读复核不新增
模型或HTTP。追加的[交接记录](evidence/serve-soak-short-20261103/handoff.json)
明确day未执行及长期验证归用户。
暂存Git字节、文档链接及最终环境另见
[交付核对](evidence/serve-soak-short-custody-20261103.json)。

实际短验没有发生日志轮转；轮转处理的证明限于CPU测试，不能宣称
生产日级轮转已验收。8周期不足固定16周期预热，因此资源只记录，
没有接受增长包络；没有生成小时检查点或天级通过结论。cgroup、RSS
与全局RAM存在重叠/漏计，本项不证明CUDA物理RAM硬保护。

默认ELF仍`d66f2bb9`、sidecar仍`42d7672a`，211个运行时来源不变；
无需重构建或替换默认程序，原发布回退链和已接受v3包完整保留。
最终原锁inode3294067保持、无恢复标记；SOAR持久masked/inactive，
容器列表空，本批没有安装常驻服务或恢复入口。

## 用户长测入口

独立day计划已准备，execution尚未创建；不重复CPU/prepare，也不
自动启动。启动前从项目根目录核对身份并显式运行：

```bash
python3 -B tools/serve_soak/stage.py check --profile day
python3 -B tools/serve_soak/stage.py run --profile day
```

计划1启动/5760周期/28800成功生成，周期15秒；满第1小时保留240周期
检查点，满24小时并完成全部业务/资源/清理合同后才能判day通过。
来源或默认版本变化时check拒绝沿用旧计划，需重新审定受影响范围。
前台运行、终端中断后的正式stop/inspect、进度与结果复核方法见
[运行说明](../tools/serve_soak/README.md)。长稳、实际部署访问/通知/
多人策略及物理RAM硬保护仍未完成，工业化目标继续active。
