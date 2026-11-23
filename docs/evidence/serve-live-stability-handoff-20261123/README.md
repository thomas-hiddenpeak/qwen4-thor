# rev432 短稳、停止发布与长稳交接

默认部署 rev432 的版本注册为 145，主轻配置仍为 36。主包和 rev430 回退包逐文件对照，两个封包层的实际文件各只有一处差异，均为同一份 v5 指标解析器。默认 CLI 的检查通过，发布未启动模型，部署保持 STOPPED，13 份受保护文件与恢复预算一致。回退至 rev430 时，其包内旧观测器仍受本次指标解析问题影响。

此前 rev430 短稳的首个业务周期完成，随后旧观测器拒绝了来自原生监听器的旁路拒绝计数。补齐指标组后，原始 HTTP 快照可解析出 113 个固定指标和 6 次成功请求。40 项 CPU 检查通过。修复包的正式短稳在一次模型启动中完成 3 个周期、15 次业务生成和 3 次真实项目文档工具调用，覆盖 JSON、SSE、结构化输出与工具续答。业务窗口 60 秒，严格租约 115.36 秒，独立回读确认清理。

## 运行 24 小时验收

已固定同一主包、695 个源文件、独立服务状态和跨工作区共用的 GPU 锁。该计划仍未执行。操作员从仓库根目录运行以下命令，并为最后一条预留 24 小时。每分钟一组，首小时和最终各写一个检查点。

```bash
cd .q4t-work/serve-live-registry-20261123/observer-src
/usr/bin/python3 -I -B tools/serve_soak/native_stage.py check --plan .q4t-work/day-01/plan.json
/usr/bin/python3 -I -B tools/serve_soak/native_stage.py run --plan .q4t-work/day-01/plan.json
```

完成后在同一目录运行 `/usr/bin/python3 -I -B tools/serve_soak/native_report.py --output .q4t-work/day-01`。若需中途停止，使用该目录 `.q4t-work/day-01/handoff.json` 中绑定到私有状态的 stop 命令。长稳负载覆盖文本、结构化和工具业务；WebRTC、SIP 及音视频连续负载仍需各自的长稳场景。

启动期通用 503 的诊断留有两份原始记录。第一轮因内核没有按线程列举子进程的接口，未发出目标请求。第二轮已定位到自有入口进程，在 SDK 发起请求前，严格租约检测到系统相机进程的 GPU 上下文并判为 ENV_ABORT。私有端口关闭、锁释放。该次监测无法确定原 503 的原因。

[实际包身份、回执摘要及交接计划](handoff.json) · [同一引擎的 WebRTC 与致命故障接管](../serve-live-default-20261123/README.md)
