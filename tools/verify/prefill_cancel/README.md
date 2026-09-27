# 分块prefill取消回归

本次修复只识别POLLERR/POLLHUP确认的失败连接；普通FIN与合法
写半关闭不可区分，不承诺所有断连方式都能提前取消。

直接连接测试提取生产谓词，用真实loopback TCP验证正常、写半关闭、
RST三种情况，编译零警告：

```bash
python3 tools/verify/prefill_cancel/check_connection.py \
  --output .q4t-work/new-cancel-connection-test
```

完整模型HTTP回归使用tools/evalscope/run_state_lifecycle.py的
`--require-early-cancel`，其余参数见该工具。额外要求44K首响应前RST
的终止日志position小于45056；不把只释放槽位当成提前停止通过。
它保存原始SSE和计数，但不是性能基准。性能仍用run_acceptance.py
五档各三次、MTP关闭，不能拿生命周期请求耗时代替正式性能。

seal.py只封存2026-09-27这次有界实验，输入路径固定；必须等所有
服务、驱动和子进程终态再运行。封存stdout应写在输出目录之外。
