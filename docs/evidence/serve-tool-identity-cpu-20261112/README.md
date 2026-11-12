# 工具命名空间、公开投影与结果合同：CPU候选

37项不同C++用例最终全部通过。`cpu-coverage-final.json`逐项指向最后
有效原始结果；失败原件、构建失败与参数错误一并保存，未覆盖失败。
官方固定JS SDK的2项命名空间流消费测试通过，严格schema检查覆盖
6组JSON/SSE以及Conversations和input_items的不同公开形状。4个实际
业务请求模板通过官方create schema，均为CPU检查、0GPU。

实现包含稳定namespace/name/type模型别名、128项展开总预算、选择
歧义和嵌套错误定位；公开身份与原生file_search绑定保持。custom调用
在Response/Conversations公开形状去除内部status，input_items则保留
官方Item状态，私有存储生命周期不丢失。

output_schema按调用时绑定持久化；改tools、重启、item reference、
显式复制、Conversation及后台快照不能替换原合同。JSON字符串实际
校验，内容数组独立处理。原有schema引擎增加结果用途，不改变生成
用途的数值/封闭对象合同；结果可为任意JSON根、开放对象和bool子
schema。正则/格式通过独立有界worker，取消/超时/资源耗尽局部失败，
不重启模型。认证压缩历史可恢复旧已消费调用，不套用今日声明。

当前复用schema dialect，不宣称已实现全部JSON Schema关键词；其余
扩展仍是实现任务。A07的async/caller/defer及依赖媒体/内建工具继续。

下一步最多1次模型启动、6次create、900秒：命名空间function/custom、
实际项目资料读取、结果拒绝、JSON/SSE、历史/Conversation以及截断
不可执行。通过后再可回退发行；本归档不是业务/发行验收，默认rev94
保持。全部master目标仍active。

原始SSE以`.wire.gz`无损归档，解压后保留真实CRLF及终止空行。
