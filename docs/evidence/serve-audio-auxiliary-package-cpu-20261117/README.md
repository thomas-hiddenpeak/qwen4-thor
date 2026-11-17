# 对齐/VAD服务包与发布协议CPU（2026-11-17）

17项一次通过、0.522246秒、0模型/GPU；host和CUDA隔离构建零警告。
固定alignment/VAD独立身份、目录和预算进入ASR可选子后端，package
prepare/verify/command、核心和原生CLI均有入口。辅助资产缺失仍可
离线验证核心包，不读取/下载或设成核心readiness依赖。
错误目录/身份/布尔伪整数/容量、孤立参数、不符真实声明与静默丢失
已配置后端均明确拒绝；旧ASR包与CLI五项保护通过。

实际已保留的两段JSON/全部SSE事件/静音零用量以及VAD故障中与恢复后
配置均通过发布schema；verbose含完整segment字段，OpenAPI包含
SRT/VTT、真实对齐与VAD接入。说话人仍保留必做，不改称全部音频完成。
候选engine-04相对已接受engine-03只增VAD固定身份的能力声明，数值/
请求执行不变；直接业务按身份差分复用，服务与恢复组合仍须验证。

[结果](result.json)、[计划](plan.json)、[源码](identity.json)、
[Python逐项](python-cpu.log)、[C++能力身份](host-cpu.log)、
[候选](candidate.json)。默认rev276未变，下一步主轻/版本/原生
接入资格与可回退发布，全部master继续；未运行长稳或完整矩阵。
