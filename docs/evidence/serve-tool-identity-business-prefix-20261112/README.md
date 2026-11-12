# 真实函数工具链路已通过，桥接分页错误保留

首轮1模型启动、3次create、52.758秒。namespaced function SSE输出正确，
实际读取本项目MODEL文档，故意无效JSON结果返回400 invalid_tool_output，
删除当前tools后有效结果仍按原合同校验，并真实生成标题/引用JSON。

整体驱动保持FAILED：input_items原始响应有first_id/last_id，但SDK的
CursorPage不暴露这两个字段，验收桥接误构造了缺字段的对象。原始线包
严格schema通过；前台/后台桥接均改从已收完的线包保留元数据，并与SDK
实际data/has_more核对。6条实际分页/空页CPU回放通过，未修改服务实现。

CUDA/模型进程清理、端口释放及互斥锁释放已确认，NVML监控无冲突或
失败；没有自动重试或重启模型。有效前缀逐文件hash绑定复用，首轮
失败不改写。下一轮只补custom链路：最多1启动、3create、600秒，两轮
累计2启动/6create；完成后再作整体接受与可回退发行，默认rev94保持。
全部master继续。
