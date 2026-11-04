# SRV-12j：托管双监听启动前冲突检查

2026-11-04。已确认监督器只提前bind主端口，遗漏可选运维端口；原生
serve在模型加载后的Run阶段才绑定两个监听。已有运维端口冲突会白白
触发加载。修复仅涉及托管监督器，不改默认ELF、模型协议或原生计算。

## 冻结合同

- 每次启动/允许恢复前，检查loopback主端口及非零运维端口；未启用
  运维时不创建额外探针。保持SO_REUSEADDR及IPv4语义，错误指出角色/
  地址/端口。任何检查失败都在backend Popen前退出，attempts不增加，
  不停止占用端口的服务，不自动恢复；自身检查socket和资源域正常清理。
- 检查不保留socket、不跨exec交接fd。成功仅代表该时刻可bind；后续
  竞争仍由原生bind裁决，不宣称无TOCTOU或保护未知外部进程。
- 旧sealed包保持原源码和行为；新prepare绑定修正后的监督器。当前
  默认7eff020d/cc83bf3a及222引擎来源、原Key/旧包/三份day保持。

## 证据与预算

先以禁止实际Popen的CPU复现证明旧实现越过已占用运维端口。初次
夹具缺少python字段误报KeyError，不算缺陷复现；原记录保留。补齐
独立backend命令替身后的复现才观察到launch入口。成组验证新端口
合同及受影响监督生命周期；已有未变模型数值/协议证据直接复用。

实机沿用12i可选host/客户端/独立运维组合，1模型、两代TLS网关、
两错误上游启动拒绝、4生成尝试/2真实工具、136公共慢头饱和。额外
两个包内生产supervisor函数短检查，在同租约内分别遇到活跃本机
后端的主端口和运维端口；各15秒内退出1、attempts0、无backend
进程/日志/尝试记录，scope退场，原占用者PID/双监听身份不变。
这两个拒绝检查不称为额外完整controller生命周期，也不加载模型。

全驱动持有生产guard的同一原flock，custody保管到清理；GPU最多240秒、
总300秒，父HTTP控制80次，监督器额外HTTP上界204、两个成功网关
绑定共8次另列。沿用host high16GiB/max20GiB/pids512、control512MiB/
pids64；只作本次可选短验，不作全部物理RAM、正式LAN或长稳结论。
原始失败保持，不自动扩大预算；通过后记录新包精确摘要和离线证据。

入口：`tools/serve_port_preflight/stage.py`的`cpu/prepare/check/run`及
`report.py`的`postflight/archive/verify`。归档输出直接返回调用端，
不能重定向到被采集的阶段目录，避免再次采到自身尚未关闭的输出流。
