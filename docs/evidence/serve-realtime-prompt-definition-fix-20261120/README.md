# Realtime Prompt 注册失败与精确夹具修正

原业务失败保留：1次模型启动、1条普通Chat通过、1次模板注册400，
0次Realtime生成，36.140秒；36次NVML查询，模型正常
退出，自有进程/端口清理且锁已释放。更早一次命令遗漏`--inherit-lease`
在驱动构造前退出，0次模型启动，也保留原守卫记录。预算预记的
10个response/16个HTTP不是实际请求数，以SDK原始回执为准。

原因已定位到驱动：Responses的`reasoning.effort=none`不能直接作为
固定OpenAI 7.47.0 Realtime枚举。原生schema按合同拒绝；不放宽服务
校验、不修改接口默认值或推理程序。驱动改为共享JSON中的5个精确
模板，省略该字段，仍由Realtime现有默认映射处理内部推理设置。

新增CPU通过真实ObjectStore/PromptRegistry校验这5个模板，逐一加入
原`effort=none`作为5个拒绝反例，另JS语法通过：0.063秒/
0GPU，构建11.037秒零警告。原6项验收器反例身份不变复用。

同一候选`f20c3b76`保留。随后单独冻结最多1模型/13 Realtime+2 Chat/
2WSS/32原生HTTP/900秒，完成尚未执行的业务；不覆盖原失败、不重放
已完成Prompt请求。默认rev356和全部master保持，业务资格仍未取得。
