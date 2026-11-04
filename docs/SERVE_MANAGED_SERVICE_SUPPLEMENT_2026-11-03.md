# SRV-14 新默认上的实际托管补验

原首轮4生成/1启动/3负面及FAILED记录保持。新默认d66f2bb9已完成
[交接修复限定交付](SERVE_STRUCTURED_HANDOFF_RESULT_2026-11-03.md)，本次
只补齐托管上下文中的交接及未执行恢复/停止场景；不重跑模型数值、
长稳或性能。包装、监督器、共享guard的原29/30 CPU实现身份保持；
原8项验收驱动检查复用，补验只选择6次业务序列。

重新从新默认生成独立源码包，实际临时systemd unit使用同生产模板，
Restart=no、KillMode=control-group；包内控制器持有完整生命周期的
同一GPU lease。按原合同核对同controller、锁dev/inode、三方FD和
cgroup，两次ready之间不释放锁。没有持久/开机服务安装。

1. 第一个后端：严格JSON紧接真实资料工具/答案，共3生成；对账3成功、
   owner归零、grammar全回收。只SIGKILL本批已核实且空闲的后端。
2. 等监督器清理旧组并恢复第二后端；普通JSON与真实资料工具/答案，
   共3生成。本实例只对账3，不能把两个实例指标拼接比较。
3. 再次SIGKILL空闲后端，在RESTART_WAIT调用正式stop；验证没有第三次
   启动，guard COMPLETED、worker STOPPED且cleanup_confirmed。
4. 幂等stop及10秒停止观察；MainPID0、NRestarts0、原锁可取得、owned
   进程/监听/GPU均无，删除本批runtime链接。原SOAR mask保持。

计划2启动/6生成/3终止场景；硬限3/6/5、240控制、1200秒，实际GPU
业务窗口220秒、显式控制48含8清理预留、监督GET保守188，均不扩。
原抢锁/同目录/错误manifest三拒绝的实现身份未变，直接复用原真实
证据，不再执行；普通JSON/SSE质量另有当前同ELF的修复验收。

累计托管为原4+补6=10次生成尝试，明确超过原8次，不能追认原首轮
通过；交接修复独立9次另列。没有HTTP自动重试或未知工具执行重放。
停止期间预占restart2代表1次实际替换+1次被取消的等待，不是2次替换。
不验证独立前台控制器SIGKILL时的真实GPU清理、常驻生产访问、多人/
通知、物理RAM保护或新ELF小时/天级稳定。
