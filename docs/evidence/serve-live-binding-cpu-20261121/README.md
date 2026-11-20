# Live 原生主连接绑定 CPU 准备

12 项针对性 CPU 全部通过：新增 9 项原生绑定组合、3 项直接受影响的配置/Realtime 保护。正式宿主构建 35.791 秒、零警告；执行 0.113778 秒。完整原生服务构建亦通过（163.330 秒、零警告），真实 SDK/WSS 业务另行记录，本证据不把受控模型回调算作真实语音验收。

- 启动历史允许 SDK 合法省略 type；developer 映射为内部应用指令角色，用户不提升权限，实际提示保留顺序与文本。
- PCM16/24k、G.711 对外格式与内部 24k PCM 语音回执分离；原生 frontend/context/LiveSession 真实组合。
- 静音/输入、指令追加后配置更新、客户端委派回填、已知准备失败重试、未知发布终止不重放、断线 Join 后释放均检查。
- WSS 代码绑定实际 tokenizer、当前客户身份与原生 ASR/LLM/TTS；真实 GPU/HTTP 尚未验。主路由登记为开发中的部分能力，录制、其他委派和媒体继续实现，不能按路由存在宣布完整兼容。

8 个源码锚点经语义复核；清单新增实际主后端路由，原生入口已有该路由。28 项反例覆盖遗漏路由、实际执行、tokenizer、身份与排空终态绑定。本组默认 rev426、客户状态不变；GPU 新增 0。累计 112 项不同 Live CPU 准备场景，总目标仍 active。

[evidence.tar.gz](evidence.tar.gz) 包含冻结清单、正式命令/日志、确切源码和语义复核记录；[index.json](index.json) 逐文件校验，[result.json](result.json) 保留原始结果与边界。

[完整原生构建](native-build.tar.gz)及[结果](native-build-result.json)：引擎 `43683066aa79cbd70504eb1acbb63b8347d82a49f095866bd65593446d7506bf`；入口身份保持 `8bd79b7a2c998138934541e6e2813d1d778d908cc84c86abec65ba843f954935`。构建期间源文件与默认发布身份不变，未运行 GPU。
