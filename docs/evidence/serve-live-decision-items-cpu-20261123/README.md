# Live Decisions 输入条目

主连接可追加和删除 Decision 输入。每个会话拥有独立的有序条目集合，给调用方返回本地 ID 和命令关联。评估用快照保持当时顺序和内容。条目上限 128，总计 8 MiB；删除回收额度。错误覆盖重复或不存在的 ID、工具标识符、图像地址及未确认的发布。单独编辑输入不会触发资料访问、模型推理或语音。针对性 CPU 7/7 通过，真实引擎构建完成，新增 GPU 启动 0。Decision 评估、自动委派、转录触发器与实际 SDK 组合验收仍待完成。

[原始测试日志](cpu.log) · [文件与构建身份](result.json) · [官方主连接合同](https://developers.openai.com/api/reference/resources/live/primary-websocket)
