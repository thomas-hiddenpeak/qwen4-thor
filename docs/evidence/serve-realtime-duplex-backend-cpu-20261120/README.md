# 原生对话输入后端：构建与针对CPU

日期：2026-11-20。ChatServer的对话与独立转写共享PrepareRealtimeInput，
复用实际ASR/VAD、请求登记、客户端准入、计量和辅助健康处理。VAD的
有界CPU准备在101前完成，回调按值保留实际客户端和后端；没有额外
外部进程或第二socket。无ASR配置仍可纯文字；配置ASR时默认手动输入、
公开转写可选，VAD显式设置经真实后端验证。对话同时预留输入组件的
存储，不将其放在原文字预算之外隐式增长。

规划器只接受连接owner提供的私有native_input证明；客户端原始音频
和自填transcript仍须真实处理。能力声明区分配置、实现和继续开发的
空闲触发/直接音频项/管线恢复，不能把后端接线写成全部输入已完成。

16项首轮14PASS/2FAIL原件保留：旧夹具要求ASR配置前后整份能力相同，
遗漏新输入能力/预算；新夹具将已规范化内容数组误作字符串。只修正
测试源，重验2项/0.472秒和20来源反例通过；14项按精确未变运行时
源码复用。最终16个不同用例/18次执行均有有效PASS，新增4项。

host构建9.928秒零警告：
`1c0f42e5bde7c95c60687fd820cf81458a2cdbbb134efa4992e0ddc7f9090e2c`。
实际q4t构建147.196秒零警告：
`1f01ec621cd7a79ca037e3f5308325c0b3e10d9f00c66da04896ea03f6c5e577`。
候选保存在`.q4t-work/serve-realtime-duplex-20261120/backend-runtime-01/q4t`。
没有启动模型、运行GPU或替换默认；rev376/STOPPED不变。真实WSS/
ASR/TTS及受影响恢复/交付将随剩余输入实现统一开展，全部master继续。

[结果](acceptance.json)、[索引](index.json)、[原始记录](records.tar.gz)、
[完整输入计划](../../SERVE_REALTIME_DUPLEX_2026-11-20.md)。
