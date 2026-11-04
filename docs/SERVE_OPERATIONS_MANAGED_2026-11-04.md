# SRV-12h：独立运维入口的托管生命周期短验

2026-11-04。可靠性和部署接线验收，无性能优化或长稳结论。
上一阶段已交付默认7eff020d，单独运维入口及真实双Key工具闭环已验；
本阶段补齐实际v6托管包与监督器/托管TLS网关的组合，不重复原生组。

## 冻结范围与出口

- 使用已准备的v6包，manifest为
  `c4775a10cf6c2d308729a0e6decd0f1178d4aa8f27f50391d7b090e96e1a349a`。
  业务18132、运维18133、TLS18452；full-head MTP k3、seq1、8192上下文，
  alpha512/beta64预算。后端restart-limit为0。本包不含host memcg域，
  不能用此次验证替代v6+host资源组合或整机物理内存保护。
- 生产controller/guard/worker启动模型，真实共享锁贯穿所有HTTP和
  网关清理；驱动复制同一个flock描述符并独立监控直至自有资源退场。
  不新增锁，不删除原锁。竞争/监控失败记ENV_ABORT，仅清理自有进程。
- 两种错误上游（缺失运维端口、端口不符）应在nginx启动前拒绝，
  不干扰后端。正确网关绑定两监听的PID/start/ELF/inode。
- 136个公共慢头占满主入口后，等待实际监督器新样本：READY且同后端、
  operations_port=18133、公共连接136；不能只读取旧READY标签。
  同时诊断HEALTHY和TLS models/capabilities成功，之后释放慢头恢复。
- 两个客户端各两次真实SSE响应与一次只读资料工具，架构值和引用正确。
  alpha通过第一代网关；明确停止后，以第二代网关完成beta，后端保持
  同一PID，不把重建网关解释为重放未知请求。合计4成功、2工具、grammar4/4。
- 双监听live inspect，停止网关后停止后端，终态STOPPED/cleanup，
  10秒不复起；监听/计算进程消失、原锁可用。检查GPU、容器、恢复unit。

预算：1模型、2成功网关启动、2拒绝启动、4生成尝试、80父控制计数，
GPU窗口最多240秒，驱动最多300秒（清理保留自己的必要退出时间）。
监督器额外HTTP上界204；2次网关绑定各4个HTTP另列，不混为父控制。
通过后记录精确包摘要和运行结果；不修改原准备包中的未验标记，
不安装常驻服务、不跑小时/天级测试，不改默认ELF、旧Key或三份day计划。
失败原件保留，具体失败后另行冻结修复范围，不自动重复整轮。

旧任务fc63db28→ddfba8c0的协议迁移仍未登记：模型/模板相同只是必要
条件，还需审查协议变更、引用证据和迁移验证。此项与实际LAN地址/
CIDR/DNS/CA/防火墙、正式容量/通知/备份策略和用户长验分别推进。

## 入口

```bash
python3 -B tools/serve_operations_managed/cpu.py
python3 -B tools/serve_operations_managed/stage.py prepare
python3 -B tools/serve_operations_managed/stage.py check
python3 -B tools/serve_operations_managed/stage.py run
```

`run`只通过已封存生产service入口启动，内部调用gpu_exclusive guard并
继承租约，不在外面再获取第二把锁。实际接受必须同时核对driver、
生产guard、custody和两个托管网关终态，prepare/check不代表通过。
