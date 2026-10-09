# 基础正确性修复证据

范围与决定见[交付报告](../../MTP_FOUNDATION_2026-10-08.md)。

- `preserved-evidence-review.json`：独立重读原始日志、36 个 SQLite
  和 SSE 后的摘要；3,201 项检查、619 项原始来源绑定，76 条正常
  HTTP。完整审计文件与脚本路径/SHA 在摘要中，不复制模型载荷。
- 原始大文件保留在本机 `.q4t-work/mtp-admission-20261007/`；
  JSON 的绝对路径描述历史受测身份，不是通用运行命令。
- `source-target-audit-01.json`、`build-result-01.json`：基础来源、
  精确测试编组变更与本机零警告构建。
- `build-identity-audit-01.json`：首次识别新旧服务 SHA 不同，结论
  为需继续核验；后续核验不覆盖此原记录。
- `execution-identity-review.json`：03 完整执行内容审计的可读
  副本，重复的完整异常表保留在绑定的本机原件中。
- `fatbin-container-audit-01.json`、`cpu-binding-review.json`：
  直接容器/解压和当前二进制重新提取来源，分别 1418/503 项。
- `independent-method-review.json`：独立方法审查与补齐容器缺口
  后的接受边界。`fatbin-parser-first-attempt.json` 保留 parser
  首错；其他工具首错的原件与摘要在相关报告中引用。
- `*.py.txt` 是本次使用的复核/构建脚本原样快照，供代码审查；
  其路径假设对应本机原 artifact 目录，不作为通用安装工具。

通过的是基础修复证据的可核验性。跨模式诊断 FAIL、首次预算
缩容失败、取消专项整体 FAIL 均在摘要中保留。普通五档只作
修复后的描述性对照，不将审计通过扩大为 MTP 正式支持或严格
性能接受。新构建身份与复用范围见交付报告中的最终记录。
