# 修正实际 HTTP 返回的 Live 与 Realtime 能力

此前两套配置都能运行真实会话，客户端的能力端点却仍返回 `pending_execution: ["conversation", "live"]`。现在真实端点列出 Live 会话、attach 与 fork 路径、WebRTC 创建、SIP 控制和临时凭据兑换范围。会话凭据的模型换代边界与 SIP 的中继依赖单独列出。旧回退包继续使用自身的历史观察。[返回字段](declared.json)

16 项主机能力测试及 6 项迁移检查通过。编译来源清单对照确认，上次主轻短验以来，引擎协议来源只改了能力描述文件。在严格租约内用重建后的实际引擎分别读取两套配置的认证 HTTP 声明；均完成 SDK WebRTC 会话与 Chat=42，并验证了跨代会话读取权限。原安装媒体程序及其摘要沿用。[主机检查](host.txt) · [迁移检查](migration.txt) · [真实业务](business.json)
