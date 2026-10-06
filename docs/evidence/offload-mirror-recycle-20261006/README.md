# Mirror GPU 回收阶段证据

本目录保存有界元数据与带源 SHA 的摘要。归档不改变任何验收结论。

- `delivery-summary.json` 列出全部 17 组的完成、失败及未运行状态。
- `groups/` 省略请求正文，保留输入身份、输出 SHA、计数与计时。
- `resource-summary.json` 保留资源边界与已记录的缺失；没有复算资源。
- `archive-manifest.json` 区分逐字副本与投影，绑定本地原件 SHA。
- 首次失败、静态发现和审查记录按原文件保留，不能称审查全部首过。
- raw 路径及现有 SHA 见交付摘要；缺少哈希不在归档时补读。

- 存储计划、独审、首失败、map/journal/summary 与 owned receipts 保留。
- gzip 只留本地；原路径需按 durable map 还原，归档不读取压缩正文。

完整本地证据：`/home/rm01/models/dev/qwen4-thor/.q4t-work/offload-mirror-recycle-20261006/source/.q4t-work/evidence`。模型 payload、raw 日志、
prompts/responses、大型资源流及 build 不进入 Git。`harness/` 按原
R/W 布局运行，归档路径不构成新的执行入口。本阶段默认关闭；
性能、质量、资源与整体物理 54 GB 的结论分别记录。
