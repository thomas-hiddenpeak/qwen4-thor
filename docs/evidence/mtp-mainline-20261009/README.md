# MTP 主线集成首组证据（2026-10-09）

本组修复默认MTP入口、prefill停机准入与诊断编组。整体Goal继续，
不据本包或PR状态宣布MTP整体转正完成。范围/后续缺口见
[主线推进记录](../../MTP_MAINLINE_READINESS_2026-10-09.md)。

- 新构建零警告；23项host、3项生产控制、75项Python、12项加载前
  拒绝通过。六个历史研究诊断只构建，未执行或修改原失败断言。
- 新候选8条HTTP、7对输出比较通过；普通/默认MTP/显式sequential
  的实际路径、短长prefill、支持配置日志与退出均已核对。
- 旧115请求及五档成本仍归原strict候选。130→131个运行来源中
  124不变、6个入口/服务文件变更、新增1个入队helper；18项编译/
  链接配置仅规范化构建目录后相同。CUDA archive并非字节相同，
  未称机器码完全等价或旧HTTP由新binary重新执行。
- `reference_configuration=matched`仅匹配配置，不认证权重内容；
  默认MTP仍关闭，显式T4继续实验。主线尚未合并。

本包保留协议、结果、实际计数/全文/路径及来源SHA。大题面、原始
SQLite、完整响应和服务日志留在本地原目录，由manifest绑定。
模型payload、二进制、CUDA archive不复制入仓库。manifest不自哈希，
由Git提交绑定；原失败证据继续在既有strict证据包及研究提交中保留。
