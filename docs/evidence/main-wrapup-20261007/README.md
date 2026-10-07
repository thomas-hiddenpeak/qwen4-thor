# 主线收尾证据

报告：[MAIN_WRAPUP_2026-10-07.md](../../MAIN_WRAPUP_2026-10-07.md)。

- `validation-summary.json`：本次通过范围、首次失败与未申请的结论。
- `build-identity.json`、`freeze.json`：运行源码、二进制、构建及冻结清单。
- `tools-integration-notes-final.json`：精选工具的来源与适配；初版 notes 保留。
- `review-*.json`：独立 I/O/预算/范围/验证审查；审查中待修项看后续 remediation。
- `test-public-host-01.log` 与 `test-supply-repair-02.log`：首次失败与唯一受影响
  组重验；通过且未改动的其余组直接复用，不混称首轮全部通过。
- `startup-contracts.json`、`startup-contracts-02.json`、
  `startup-observation-audit.json`：脚本文案首错、修正复验及真实日志格式审计。
- `quality-audit.json`、`http-quality-results.json`：11 条真实 HTTP 质量及
  原始数据库/请求/日志路径和摘要。原始大文件仅保留本机，不复制到 Git。
- `protection-before-integration.json`：原开发目录七项修改与旧二进制保护。
- `archive-manifest.json`：本目录封存文件摘要。此 README 与后续交接文档
  由 Git 记录，不要求递归自引用。

`validation-corrections.json` 保留冻结清单公共组数 19→18 的更正。
源码整合、测试通过与默认二进制部署分开：本轮未覆盖旧默认入口，未申请
性能持平、MTP/媒体质量、完整 MTP 内存估算或物理 RAM 54 GB 结论。
日志与配置信息可能包含本机路径；不把历史路径当成通用执行入口。
