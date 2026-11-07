# Serve阶段收口入口与研究保留

2026-11-07，按用户要求停止扩展范围，已有serve成果独立交付，
视觉研究与原始证据保留，新能力进入后续清单。

## 接手入口

- 独立[PR #21](https://github.com/thomas-hiddenpeak/qwen4-thor/pull/21)：
  `codex/serve-resilience-closeout`，提交`96afb42c`，基于主线f7dcd8f9。
  提取PLE永久故障健康传播、NVML分类/有界监控恢复、辅助依赖失败
  隔离和资源控制面隔离。没有提取视觉行最大值内核或新客户端改动。
- [统一能力、版本、回退与发布清单](https://github.com/thomas-hiddenpeak/qwen4-thor/blob/96afb42c671fbaa7e62a59f62e3c6c91a97305f2/docs/SERVE_RESILIENCE_CLOSEOUT_2026-11-07.md)
  与[本轮CPU原件](https://github.com/thomas-hiddenpeak/qwen4-thor/blob/96afb42c671fbaa7e62a59f62e3c6c91a97305f2/docs/evidence/serve-resilience-closeout-20261107/README.md)
  以提取提交为准；不能用研究分支工作树冒充该提交的已构建来源。
- 本机工作区为`.q4t-work/serve-closeout-20261107/source`，其同级
  host/ple/runtime保留独立产物。原研究路径与证据目录不迁移、不删。

## 已完成与未发布边界

506项不同CPU用例通过：331公开host、7真实PLE文件I/O、122监控/
保护、46去重监督器/期限。三个目标构建零警告，124来源锚点/3负控
及生成合同检查通过；137份旧CPU记录保留，本轮30份原件封存。
新生产ELF`478a325e…`只编译未执行，协议`7c91ab7d…`的107项来源
无漂移。PR为草稿，CPU CI以其检查页为准；没有合main或部署。

默认`ea1b474d…`/sidecar `3eb46385…`、旧v7包`f69cb406…`及两份
视觉程序摘要未变。旧包继续固定来源，旧v7的限定业务接受不扩展到
新v9/v10；v10与独立host exporter尚不兼容。新ELF不能配旧sidecar
作为接受包。实际模型业务、故障/清理与部署健康仍需另冻有界短验。
本轮没有模型/GPU/真实NVML测试、真实cgroup或生产服务操作。

## 保留与后置

[PR #20](https://github.com/thomas-hiddenpeak/qwen4-thor/pull/20)继续
草稿/HOLD，保存视觉候选`34e7ceef…`、父`982bba07…`及原失败、
先导和已完成数值/plain证据。原分支仍有独有提交与路径依赖，
不能删除或整体合并。NVML根因未决，不因研究归档标为已修复。

[exporter/语法worker计划](SERVE_RESILIENCE_NEXT_PLAN_2026-11-07.md)
已移入后续清单；阻塞I/O、控制器接管及用户负责的长稳分别保留。
这些不是本轮源码交接的前置条件。应用视觉目标仍暂停，其登记
限制不再作为仓库工作的阻塞；不能据本轮收口标记视觉验收通过。

本轮出口是独立可审查PR、CPU证据和交接清单。后续恢复业务验收
须明确候选、请求/启动/时间预算与GPU测试授权；失败保留未发布
结果，不自动追加驱动排障或新功能。
