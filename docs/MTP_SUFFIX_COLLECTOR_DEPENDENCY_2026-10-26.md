# 固定 k3 suffix：采集合同复核与固定版本工具候选

2026-10-26，继续完整交付目标，入口 `10668cf8` / codex/suffix-k3，工作区
干净。上一轮真实控制失败修正了原因判断，但没有完成子集200K/8K GPU
归因或优化接受；不把负向归档改写为完整目标已完成。

## 完成度与本次范围

[逐项矩阵](evidence/mtp-suffix-contract-20261026/completion-matrix.md)核对
原任务、现有实现和证据。算法/HTTP接入、有限接受前缀状态合同、两head
重复收益、资源评估及当前规则250HTTP/18启动五档测量已有可复用证据。
缺的是保护项接受、最新要求的可信GPU区间对照和适用程序交付；不能把
五档“测齐但保护未通过”误写成“未测”。两层GDN oracle、合成终止及故障
路径覆盖限制保留，不外推任意输入一致性。

本轮先冻结1200秒资料核对：8个检索查询、6次页面打开、4页成功保存；
两项本地审阅分别限10/12份来源，实际8/12。没有测试、新profiler调用、
GPU、模型HTTP、export或运行时源码修改。

## 新证据与因果修正

本机2026.1.3安装包guide与start help在结束策略选项/默认值上存在差异。
官方论坛350407直接以profile、end=none及manual stop处理多进程刷新，
随后有成功反馈。因此此前“profile和interactive stop一般不兼容”的
判断过强；本机正确interactive仍被RangeCollectionState拒绝是确定观察，
内部原因仍未知。[本地核对](evidence/mtp-suffix-contract-20261026/local-contract.md)、
[官方支持记录](https://forums.developer.nvidia.com/t/question-when-prifilling-megatron-lm/350407)。

2026.5.1发布说明列出cudaProfilerStop结束多进程采集时的CUPTI刷新改进。
论坛DTSP-21073涉及非调用方进程未充分flush。这支持一个固定版本工具
候选，但没有证明两份资料对应同一修复，也没有证明本机单CUDA fixture
拒绝或旧实际目标warning已修。[发布说明](https://docs.nvidia.com/nsight-systems/ReleaseNotes/index.html)。

安装指南一般区分Embedded与SBSA，而JetPack页明确Thor基于SBSA并要求
SBSA installer。当前target-linux-sbsa-armv8本身不是装错平台的证据；
具体驱动/CUPTI及实际加载库仍待检查。
[安装指南](https://docs.nvidia.com/nsight-systems/InstallationGuide/index.html)、
[JetPack/Thor](https://developer.nvidia.com/embedded/jetpack)。

## 唯一依赖准备

依据新证据，另冻结600秒、单官方仓库、单2026.5.1版本、2个元数据请求、
最多1个1GiB软件包。实际185.682747秒，2次元数据/1次软件包下载，0网络
重试。只隔离解包，没有执行包内程序或安装维护脚本、没有系统安装或
全局PATH/符号链接改动。

| 项目 | 身份 |
|---|---|
| 包 | nsight-systems-cli-2026.5.1，arm64 |
| 版本 | 2026.5.1.161-265138896106v0 |
| deb字节数 | 208229554 |
| deb SHA256 | 087c08b5ddf3ed6f913748f8206d6bbdc225f2aa275ff33af4c487e8b319c8ab |
| target CLI SHA256 | dbe02661b00efbfb4c9c3cd3c2d8df6fecd6e80ac794305892e233e0d712a527 |
| 解包清单 | 1765文件，manifest 876d4201d6d7d5dba20eeaa77d988c722fbc0862dfc4d6dc015ed47352d7a1d1 |

来源为官方developer.download.nvidia.com/devtools的ubuntu2404/arm64仓库。
核对HTTPS的Release→Packages.gz→deb SHA256/长度及包内控制字段，不宣称
独立签名认证。隔离根为`.q4t-work/suffix-nsys-dependency-20261026/extracted/`，
CLI相对路径`opt/nvidia/nsight-systems-cli/2026.5.1/target-linux-sbsa-armv8/nsys`。
包内13.4/12.9等CUPTI库清单不能证明实际加载哪份。

首次准备把不带版本的2024字节元包误算作第二个CLI构建而停止；元包实际
只依赖唯一版本化CLI。保留原脚本/失败收据，针对选择器作一次修正，复用
原元数据、不延长原600秒期限、不增加版本或软件包次数。当前状态为
**PREPARED_NOT_QUALIFIED**；不是采集修复通过。
[候选边界](evidence/mtp-suffix-contract-20261026/candidate-assessment.md)、
[结果与身份](evidence/mtp-suffix-contract-20261026/summary.json)。

## 下一步及仍需满足的条件

只推进此固定构建：核对静态CLI/依赖/schema与控制契约；如有实际适配，
统一验证受影响CPU合同，再冻结完整持锁资格阶段。已有2026.3.1不作为
额外版本插入，不扫描选项寻找通过结果。本轮未消费任何新GPU预算。

新工具需要自己的direct/wrapper已知工作、真实负控、目标身份/退出、
warning与清理资格，不能复用旧collector作为新版本已通过。通过后才有
条件采集指定子集8K/200K。旧6381个成功memset无活动仍需语义解释，不能
靠新版本或固定fixture豁免；工具通过也不能替代最终E2E保护接受。

两head重复收益按原身份复用，默认f356、sidecar和子集保持。完整目标仍
未完成。本轮是新证据及工具候选准备，没有新的性能结论或部署。

后续同日已完成该固定包的一次独立资格：首interactive stop仍拒绝，
ENV_ABORT、0模型/0HTTP；未接受该工具或交付suffix。新的实际CUPTI/backend、
审计与归档见[资格结果](MTP_SUFFIX_NSYS_265_2026-10-26.md)，不改写本准备
阶段的零执行记录。
