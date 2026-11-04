# SRV-12j：托管双端口启动前检查限定交付

2026-11-04。`8c052db6`修复监督器遗漏独立运维端口预检的问题；遇到
已存在的监听冲突，新的托管包在启动后端前拒绝并给出角色、地址与
端口。每次允许恢复也重新检查，失败不继续重试，不停止原占用者。
默认ELF/sidecar和原生引擎没有变化，旧封存包保持原行为；修复随新
prepare包启用。范围见[冻结合同](SERVE_PORT_PREFLIGHT_2026-11-04.md)。

## 直接合同与实际拒绝

38项成组CPU首次全过：7项端口合同、26项既有监督生命周期、3项host
组合和2项驱动故障。覆盖主/运维占用、wildcard监听冲突、EMFILE、
socket释放、未启用运维以及信号恢复前重新检查。拒绝发生在backend
Popen之前，成功预检不保留socket，也不保证消除之后的端口竞争。

缺陷复现的第一版夹具缺少python字段，产生KeyError；原receipt曾将
“1失败”误分类为复现成功，该收据保留并明确不作为缺陷证据。补齐
独立backend命令替身后，第二次复现才确认旧实现越过占用检查到达
被禁止的Popen入口。两次均无模型或HTTP；修复后相同合同通过。

在同一个实际服务租约内执行两个包内生产supervisor函数检查：

| 冲突入口 | 实际拒绝时间 | 后端启动 | 原占用者 | 清理 |
|---|---|---|---|---|
| 主端口127.0.0.1:18132 | 0.387914秒 | 0次 | 原PID/双监听身份保持 | FAILED/cleanup，scope退出 |
| 运维端口127.0.0.1:18133 | 0.387640秒 | 0次 | 原PID/双监听身份保持 | FAILED/cleanup，scope退出 |

两个检查均无backend日志/attempt记录，包内实际监督器摘要与manifest
一致；它们验证生产函数的拒绝路径，不称为两次额外完整controller
生命周期。完整controller/guard/worker生命周期由下述成功业务验证。

## 成功业务与资源回收

- 一个当前模型、两代托管TLS网关；两种错误运维上游仍在nginx子进程
  启动前拒绝。136公共慢头满额下新监督样本READY、诊断HEALTHY、TLS
  models/capabilities成功，实际capabilities与包内发布声明一致。
- alpha512、beta64各完成2响应/1真实项目资料工具；两者均返回
  `Qwen4ExpForConditionalGeneration`和`[docs/MODEL.md:L1-L24]`。
  两轮分别564/890输入token、55/58输出token。四次响应读取耗时
  3.837/4.053/3.740/4.043秒，属于业务记录，不作性能非劣或TTFT结论。
- 88份host样本覆盖模型及四个真实grammar子进程的payload归属，
  grammar4/4回收、终态活动/连接/资源计数归零。主服务scope及两个
  拒绝检查scope均退出，网关/后端STOPPED/cleanup，10秒未复起。
- host high16GiB/max20GiB/pids512、swap0，control512MiB/pids64；
  内核记账峰值17,108,643,840字节（约15.934GiB），采样期pids最高141。
  high/max/OOM/pids拒绝事件均0。memcg仍不能代表Thor全部物理RAM。
- GPU/主端口/运维端口/TLS/原锁均释放，恢复unit保持masked/inactive/
  dead/MainPID0，容器空。默认pair、222引擎来源、旧包、原Key和三份
  day计划保持且day未运行；未安装常驻服务。

## 身份与证据

| 项目 | 身份或结果 |
|---|---|
| 主包manifest | `14f8d6fc96f0ef10d228cc13ad6325d39ddec1f84d14a3777370c73ad0d22604` |
| 运维冲突检查包manifest | `4cb6294f4a5098fb1cb0308d164a2437d898f4a108959b8100624a9a83d8d9d9` |
| 默认ELF，保持 | `7eff020de66b96f3175b3e123909be9e2f58142d54d1dd5992750d23cbe9c769` |
| 默认sidecar，保持 | `cc83bf3a36c2b9658221554f39a7e6bb807f2618cb37b87b2f19fd7d7ea91d04` |
| 协议，保持 | `ddfba8c002d8fb899b4c00d42612b0a26669036a1acc65e8292a10c0a9b69342` |
| 生产guard | COMPLETED，64.416826秒，60次观察 |
| 同flock保管至清理 | COMPLETED，64.645733秒，23次额外重叠观察 |
| 驱动总时间/父HTTP控制 | 76.047347秒（含10秒停后观察）/13 |

与12i主包的运行文件比较，只有`source/tools/serve_service/supervisor.py`
变化。原dev66305/inode3294067锁保持；guard与custody重叠时长不能相加，
轮询也不能证明采样间隔内没有短暂干扰。监督器HTTP事前上界204、两次
成功网关绑定共8调用另列，不冒充实测父控制数。

首次离线报告在样本50的空argv上取首项失败；该PID/start_ticks/boot_id
与样本35～49中15份已确认grammar-worker记录完全相同。原始采样没有
保存该时刻的进程状态，因此不补称为已观察到Z状态。独立finisher仅
允许从更早、相同完整进程身份继承角色证据；无历史空argv、PID复用、
非空命令行变更仍拒绝，所有资源/成员检查保持。4项定向CPU通过，原
38项及真实业务直接复用，不追加模型/HTTP/GPU。原报告与失败记录不改。

[机器结果](evidence/serve-port-preflight-20261104/result.json)、
[清单](evidence/serve-port-preflight-20261104/manifest.json)和
[182份原始记录](evidence/serve-port-preflight-20261104/records.tar.gz)
通过独立离线核对，345份本机引用逐项对账。压缩334,786字节，SHA-256
`b18c0d16653b53b01d49fc608bd972a6b806ff6076c87ffb4701b165a4b881fd`。
API Key、内部/取消token和TLS私钥不公开；仅摘要的二进制与私有请求
不是可离机恢复备份。最终复核入口使用修正后的finisher：

```bash
python3 -B tools/serve_port_preflight/finish.py verify
```

本机包在`.q4t-work/serve-port-preflight-20261104/backend-package`；
原12i包仍为可核验回退入口，切换需先停新网关/后端、确认GPU/锁释放，
再在新运行目录启动旧包，不复用或修改活跃目录。原prepare未验标记
保持，接受由本页及独立结果定义。

本次只修复托管入口。原生`q4t serve`仍在加载模型后绑定端口，其启动
资源与监听生命周期是下一阶段工作；本次预检也不消除后续竞争。
实际LAN/正式策略按需部署，长验仍由用户执行，工业化持续目标active。
