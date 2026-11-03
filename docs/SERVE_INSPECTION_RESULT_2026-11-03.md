# SRV-17 只读运维核验限定接受

冻结提交`5ec78b45`，v3包manifest
`160796c3fc65d44cb36967952ca300abd48eb8228f59a8afee597a6b685e8606`。
51项CPU和隔离入口首轮通过，旧实际v1/v2包核验通过。新元数据记录
观察范围；`inspect`把历史与当前资源分开，不修补或覆盖旧终态。

## 实际结果

1启动/3成功生成：普通JSON回答正确，模型严格请求读取本项目
`docs/MODEL.md`第1—24行，客户端真实执行一次并回填，模型回答
`Qwen4ExpForConditionalGeneration`且引用正确。3完整wire/HTTP200，
2个grammar worker全部回收，业务owner与队列归零，无重试或重放。

| 一次性检查 | 返回 | 现场结论 | 历史与文件保护 |
|---|---|---|---|
| SRV-16失联目录 | exit2，UNKNOWN | 实际锁可用、GPU空；旧记录缺namespace，不能完整认定 | 原held文本及失联原件逐SHA保持 |
| 新v3运行中 | exit0，OWNER_PRESENT/BUSY | 完整匹配controller、实际GPU后端、监听与busy锁 | 未修改历史终态 |
| 正式stop后10秒 | exit0，INACTIVE_OBSERVED/AVAILABLE | 已记录进程、同包候选、监听、GPU均空，原锁可用 | 运行目录及锁文本逐SHA保持 |

三次inspect耗时分别0.167/0.160/0.160秒。此次live没有观测变化或
缺证；合同允许的并发变化UNKNOWN没有被实际触发，相关边界由CPU
覆盖。上述AVAILABLE只是观测，不预留未来容量或证明采样间无干扰。

生产controller持有同一GPU lease覆盖加载、业务和停止排空；guard
48次观察无竞争，COMPLETED；terminal STOPPED/cleanup_confirmed，
1 attempt/0 restart。GPU窗口50.699秒，总61.344秒；5显式控制，监督
HTTP仅保守上限188，合计上限193，不能写成实际193次HTTP。普通/
工具请求/回填分别0.433/3.732/4.034秒，不作性能非劣或优化接受。

## 交付、证据与回退

[封存清单](evidence/serve-inspection-20261103/manifest.json)包含80原件、
1本机ELF身份，8份Git文件166,092字节；包括CPU来源/日志、冻结计划、
完整wire、三次CLI、原件SHA、48个guard样本及最终环境。归档及本机
身份逐项复核通过，没有补跑模型或HTTP。伴随
[包接受记录](evidence/serve-inspection-20261103/accepted_package.json)
绑定上述v3 manifest；prepare的未验声明保持原样。

默认ELF仍`d66f2bb9`、sidecar仍`42d7672a`，211个C++来源未改，
无需替换或重新构建默认程序；旧v1/v2独立包和既有回退链完整保留。
新增入口位于`tools/serve_service/service.py inspect --run DIR`，新包
包含相同入口。可使用已封存旧v2包启动新代次回退托管Python实现；
旧包不具备新inspect/观察范围，不能用回退覆盖既有运行目录。

最终GPU空、原锁inode3294067可取得、无恢复标记，容器列表空；SOAR
恢复服务仍持久masked/inactive，MainPID0。本批未安装常驻unit。
长稳、实际部署访问/鉴权/通知/多人策略及物理RAM硬保护继续待办，
不能据本批通过宣布完整工业化目标完成。
