# 增量会话保存证据

结果见[完整报告](../../SESSION_INCREMENTAL_RESULT_2026-10-29.md)。
`decision.json`为最终配对和门槛；`original-decision.json`/`original-postflight.json`
保留原ENV_ABORT；`baseline-window.json`说明可复用的独立基线窗口及预算偏差。
`final-integrity.json`包含928输入、825运行seal和默认程序身份核验。
`resources.json`只含续行资源；原基线资源与完整raw在独立原归档。
`review-source.json`记录离线资源字段schema适配，未更改冻结文件或接受门槛。

所有摘要均对应归档中的原始文件，绝不是新的GPU测试。两个archive receipt
分别绑定失败现场和有效续行，保留模型只读元数据、程序/子集身份、命令、
HTTP输出、监控和CPU合同。原始路径保留，不重跑已通过且依赖未变的证据。
本轮LOCAL_GO是研究候选判断，默认发布未变。
