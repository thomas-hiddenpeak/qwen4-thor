# 长短 agent suffix 已交付

2026-10-27，**DEPLOYED_HEALTH_CHECKED**。用户在收益和负点披露后明确
接受，按[本项接受决定](MTP_SUFFIX_AGENT_ACCEPTANCE_2026-10-27.md)交付。
`build/q4t` 已替换为
`f2a6de360e05676c5fee2f86fbd314c9a9ff3ea0378fa104e0185cef6b4ec164`，
发布元数据SHA为
`26325699af40325e384c4aae4d615679f2d7e2aa69f9eecbc5743e68db6032cf`。
完整head与MTP k3仍默认，suffix默认关闭，子集仍独立显式选择；无常驻服务。

实际构建基点c1ad148及工作区编译输入已绑定，160项运行时源码逐项核对。
实现已包含于1fefbc1；本次只包装、安装、健康检查和文档收尾，没有新构建
或推理实现变更，不把最终文档提交当作程序构建身份。

## 接受依据与限制

原320条HTTP、22精确质量、独立目标/草稿接受前缀状态、完整attention
布局、分块prefill、32短请求准入、取消复用及五档保护按同身份复用。
19组单流保护共57对请求输出一致。两轮并发重复代码/文本的E2E收益为
full +1.9793%/+1.8337%，subset +1.1176%/+1.0534%；decode分别
+5.4412%/+5.2203%、+2.9917%/+2.8471%。

原优化目标仍FAIL，原确认报告和机器决定不追改为通过。单流子集200K
decode −0.3926%、8K E2E −0.0239%及TTFT +0.1315%等全部负点保留；
总体统计非劣和旧TTFT根因修复未证明。部分混合/非重复自由输出差异仍在，
不承诺任意输入输出逐位相同。用户此次接受的是这个身份及其已披露边界，
不设新的全局容差。性能原值见[完整确认报告](MTP_SUFFIX_AGENT_CONFIRM_RESULT_2026-10-27.md)。

## 使用

从仓库根运行，完整head＋显式suffix：

```bash
./build/q4t serve --host 127.0.0.1 --port 8000 \
  --max-seq 4 --max-prefill 4096 --max-len 208896 \
  --seq-capacities 208896,65536,65536,65536 --mtp-suffix
```

子集head在同一命令末尾添加：

```bash
--mtp-subset-head .q4t-work/suffix-agent-delivery-20261027/package-v2/artifact
```

一个长槽容量208896，三个短槽各65536；均包含输入与最大输出预算。
超过65536预算的活动请求最多一个。4执行槽与最多32个解析/排队/生成
请求分开；16/32突发验证为短请求，未保证32个长请求的延迟或完成时限。
单body16MiB、全局body48MiB上限保持。长prefill仍会影响其他请求TTFT；
默认请求总期限20分钟，客户端需为排队留足时间。单流配置继续使用
`--max-seq 1 --max-prefill 8192 --max-len 208896`。

功能回退：移除`--mtp-suffix`并重启，容量/队列和head选择不变。
回完整head再移除`--mtp-subset-head DIR`；普通decode再添加`--no-mtp`。
新配置不要求默认开启suffix，不跨请求共享历史，也不扩展到media或动态k。

## 交付健康与准备失败

原2启动/3请求/900秒健康清单漏了port，首驱动在构造服务命令时退出，
实际0模型/0请求；旧程序90f及sidecar45cfe自动恢复。调度器的
loads_started=1是尝试计数，不能伪称模型已启动。首次包、日志及
COMMAND_FAILED均保留，没有覆盖为成功。

补齐端口，CPU检查实际两种命令均到达受控的Popen边界（拦截，不启动
进程），缺端口负例拒绝；原预算检查也保留。执行原未使用健康额度，
绝对截止不延长，没有重采任何已完成模型请求。这里修复的是0加载的
交付准备错误；并未将“无重试”放宽为可重复模型测量。

- 默认MTP/完整head（不传mtp、mtp-k、suffix、subset）：1请求通过。
- 指定4槽/4096 prefill，子集＋suffix：2并发请求通过，各54次命中；
  实际MTP同时覆盖两行、神经/suffix混合及全suffix工作。
- 三条均744输入/256输出、length结束，输出SHA与冻结参考一致；
  本健康仅确认安装，不替代复用的四路长输入验收。
- 实际2模型启动，服务都正常退出；1621项子集预留全部消费/释放，
  unknown release=0。无自有服务进程残留。
- 健康阶段109.529秒；包含首次失败和修复间隔共266.851秒<900秒。
  110/110监控全flush，pending=0，103次NVML查询OK，最终屏障通过，
  无ENV_ABORT。923绑定文件、两包封条、160源码及安装配对终审通过。

原完整测量320＋健康3＝323生成请求；HTTP服务实际15＋2＝17次启动。
原4次GPU fixture保持，本次0；新的0加载准备失败单独记录，不掩盖为
一次成功服务。交付终审见[审计账本](evidence/suffix-agent-delivery-20261027/final-audit.json)。

## 回退及证据

`.q4t-work/suffix-agent-delivery-20261027/package-v2/`为独立回退包：
包含旧90f程序/原发布元数据/实际cache，新f2a6程序/cache/160项源码快照，
子集产物副本、原确认决定、本次用户接受、工具及封条。
包封条SHA `93a5b05a9fd263507e67f613f9ace4f3c64c1f35fc308cede62aea5dbd64d8d5`。
逐文件原子替换，不是跨文件掉电事务；先写恢复标记，只覆盖已知身份，
健康和外层guard全部成功后才清除标记。首次失败已实际验证旧配对恢复。

整版回退会恢复旧单流版本；须停用新的四槽启动配置。需要回退时运行
（输出目录必须全新；此命令仅记录，当前保持新交付版）：

```bash
python3 -B /home/rm01/models/dev/qwen4-thor/tools/evalscope/gpu_exclusive.py \
  --output /home/rm01/models/dev/qwen4-thor/.q4t-work/suffix-agent-delivery-20261027/manual-rollback/lease -- \
  python3 -B /home/rm01/models/dev/qwen4-thor/.q4t-work/suffix-agent-delivery-20261027/package-v2/delivery.py \
  deploy --rollback-output /home/rm01/models/dev/qwen4-thor/.q4t-work/suffix-agent-delivery-20261027/manual-rollback
```

模型和历史证据目录保持原路径；工具依赖本工作区的监控/原子替换模块，
不宣称可任意搬移后运行。package/与deployment/保存首次失败，v2目录
保存修复后真实健康。原两个研究归档完整保留。当前工作分支有独有实现
与证据依赖，继续使用codex/suffix-k3提交推送，不自动合入main或删除分支。
阶段已收尾，不追加性能采样；后续新身份或实际合同问题另行评估。

本次健康原件、首次失败、CPU修复、最终文档和审计另封存在本阶段
`archive/`，与两个已封存程序包分别计数，见
[归档封条](evidence/suffix-agent-delivery-20261027/archive-audit.json)。
