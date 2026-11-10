# 静态WebP原生转换CPU验收

2026-11-10。13/13针对性CPU通过，0.965秒；0网络/模型/GPU。
首host构建因测试计数辅助函数signed/unsigned比较被Werror拒绝，修复
为uint64_t后4.094秒通过；实际引擎构建103.989秒通过。最终零警告，
原失败完整保留。12个源码锚点经审查，452总锚点及5漂移保护一致。

五组原创lossless/lossy/透明/metadata/噪声WebP由真实owned子进程
解码，最终全部RGB像素与独立Pillow参考逐位一致。完整RIFF/单图像
准入、全部截断前缀、动画拒绝及容器合法但压缩数据缺失均有实际检查。
像素/输出预算、预取消和ready后取消、超时/异常退出、实际缺库均按
预期失败且回收所有权；hang/exit测试明确使用故障夹具，不冒充真实
codec故障。普通PNG像素与文本prompt仍可处理，此项是CPU组件证据。

系统libwebpdecoder 3与zlib 1只在独立worker内按固定已核对inode
加载，随后进入纯内存沙箱；父端要求完整输出、EOF和正常退出才发布。
真实库版本和SHA、原件SHA与转换PNG SHA一起保存。核心ELF没有新增
这两个库的DT_NEEDED。许可/接口来源见source-review及README。

v2图片快照保留原件和转换PNG，实际File删除、ObjectStore重启且codec
不可用时仍恢复完全相同内容；范围隔离、预算降低、回执损坏拒绝通过。
共同展开按原件+PNG计图像字节，保留detail，向实际processor传PNG。
已存在PNG/JPEG/GIF、PDF、未知提交及sandbox直接受影响项一起通过。

候选f54ef95310943ad344a7ebc03ccdc20f9cf3e29d00a3cff4635e381e6687eebd；
身份与原始记录见本目录。完整Chat/Responses/计数/后台/历史接线、
真实表格模型步骤及业务/格式兼容回退仍必须完成；未发行/合main，
默认Uploads cdb5ced9保持STOPPED。继续全部master任务。
