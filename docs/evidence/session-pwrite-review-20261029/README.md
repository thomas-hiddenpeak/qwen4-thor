# pwrite等待离线复核证据

结论与尚未执行的60分钟方案见
[分层复核](../../SESSION_PWRITE_WAIT_REVIEW_2026-10-29.md)。

- `review.json`：复算8份主快照，绑定原独立归档中的16份输入和4份
  受测源码/依赖；包括旧1Hz窗口、推定写线程和明确缺失的观测字段。
- `host-inventory.json`：2026-10-29只读采集的设备/挂载/SMART、内核配置
  和事件format。它是测试之后的盘点，不能倒填旧保存区间。缺失的
  wbt节点对应内核未启用BLK_WBT，不是一次运行时监控失败。
- `validation.json`：另核对4份原server.log与全部8份分段记录，校验
  候选/default/sidecar SHA、运行时无差分及实际模型/缓存挂载位置。
- `files.json`：以上三个副本的字节数、SHA和原始分析目录。

离线复算命令（输出必须为新路径，禁止写入原证据/原归档）：

```bash
python3 -B tools/session_cache/review_pwrite_wait.py \
  --output .q4t-work/<新的离线目录>/review.json
```

原始依据仍位于`.q4t-work/session-preallocate-20261029`及其独立archive；
本轮没有改动旧证据或受测运行时代码。没有新模型、HTTP、存储负载或
启用追踪，没有声称已经验证BPF附着成功、定位SSD固件内部机制或修复。
