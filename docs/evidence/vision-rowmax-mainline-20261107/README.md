# 视觉最终主线组合与交付证据

最终候选60b1c4c1对比已验主线父478a325e，默认已安装
60b1c4c1/d2ae8db2；旧ea1b474d/3eb46385及依赖完整保留。

- decision.json：本项性能工程接受，部署字段保留当时的待健康状态。
- acceptance-audit.json：104条接受记录、44质量检查、52对响应及
  所有五档正负计时。每个接受生命周期完整且无NVML缺口。
- acceptance-abort.json：原4210秒末尾NVML故障；旧父生命周期
  全部排除。三组78条封存复用，父补验26条/1015.651秒通过。
- deployment-finalized.json、delivery-audit.json：实际交付最终凭据，
  图像JSON/SSE、严格工具及真实文档引用、健康端点全部通过。
  1启动4Chat6控制/47.175秒、46次NVML全OK、49自有PID退出；
  GPU/端口/原锁释放，没有常驻服务，不自动迁移旧托管包。
- delivery-cpu.log、completion-source-controls.json：5项文件事务
  CPU合同及2项禁止失败证据复用的负控。
- raw-records.tar.gz与manifest.json：963份原始请求、响应数据库、
  日志、计划、原件清单、构建记录和交付元数据。打包后逐文件
  解包核对SHA256；未把ELF或模型权重放进仓库。

本机原件位于.q4t-work/vision-rowmax-mainline-20261107：
中断阶段aborted-originals（4172路径）、完整矩阵originals（4359路径）、
健康health-originals（2387路径）按SHA256保留。历史源码按所记Git
commit与CAS核对，不能把后来更改的工作区直接当作旧现场。
默认和回退ELF保留在release/candidate、release/previous；新旧包
与其原依赖路径均保留。audit_health.py为实际离线重解析脚本。

仅接受这一局部可回退优化，不声称统计非劣、统一性能容差或NVIDIA
NVML根因已修复。视频时序任务质量、硬解码、视觉并发和长稳不扩展。
