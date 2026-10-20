# 完整热缓存的物理回收诊断

本工具用于2026-10-20已冻结的可靠性阶段，不是默认启动入口。
完整范围和结果见[报告](../../../docs/WARM_RECLAIM_TIMELINE_2026-10-20.md)。
默认binary、模型、CUDA分配实现与NVML时限均未修改。

## 入口

`run.py --stage baseline-v1` 由supervisor取得共享GPU lease，创建独立
trace实例和只读资源观察器，再启动原GPU guard及单次完整热加载。
`--stage precompact-v1` 仅增加一次完整预热后的node0/compact写入。
所有证据目录独占创建，已有目录拒绝覆盖；本轮预算已使用完毕，
这些命令不是授权重复运行的采样队列。

模型之前/之后均核验原67987791872-byte完整热向量（“之后”指
前置规整之后、模型启动之前）；过程中正常回收文件页另行记录。
清零模型payload只是建立统一起点的准备步骤，随后必须完整预热。
最终加载不会加--clear-model-cache。无缺页补读和较低压力fallback。

复用`.q4t-work/vmm-compare-20261020/runtime/q4t`，SHA2a66ee2f；它由
与默认19ea相同对象加独立应用API日志包装链接，未启用VMM。
依赖前阶段build.json、scope-integration-v5的质量夹具与reference。
文件缺失时先恢复相同身份的证据，不能换成其他构建/夹具继续。

## 观测工具与限制

- `driver_identity.py`：只读module build-id/srcversion、ELF重定位及
  已加载命名函数，识别pool count/scan回调，保存%pS符号间隙映射。
- `kernel_capture.py`：自己的tracefs instance、mono时钟、21事件，
  order>=4页分配过滤；每CPU丢失统计、字节/时间上限和实例清理。
  不修改全局trace配置。SHRINKER_DEBUG/kprobe/function tracer不可用。
- `resources.py`：0.2s系统计数/目标栈，1s完整非faulting cachestat
  及实际模型memcg。CONFIG_PSI未编译明确记unsupported，不能当零。
- `compact_once.py`：唯一干预，只对node0 compact写一次；本机一个
  内存节点，所以影响全机物理布局，没有持久参数但不能精确回滚布局。
- `analyze_kernel.py`、`analyze_resources.py`：离线读取单阶段，提供
  `--stage PATH --output JSON`，不触发GPU、NVML或回收。
- `audit.py --output PATH`：重验两阶段487份封存文件、缓存向量、
  collector/guard/清理结果及默认身份，不重新采样。

tracepipe是全机事件，按tid/已核实callback/时间窗归属。start/end的
shrinker区间不包括前置count_objects；end的retval是最后一次scan
返回值，不是该区间总回收量。池对象计数单位依赖其order，未直接
取得池order，不将对象数乘4KiB。API async free返回仍只代表提交。
栈采样不能排除间隔内短暂事件，也未识别RM锁的完整持有者链。

外层supervisor覆盖guard结束后的scope清理，两个collector为guard
兄弟进程。清理未确认则设置所有GPU guard检查的recovery-required
标记；需要按记录恢复自身残留，不能直接删除标记绕过。SIGKILL/
机器故障仍可能留下trace实例，普通stop/TERM路径有清理证据。

## 离线核验

```bash
python3 -B -m unittest discover -s tools/verify/warm_reclaim -p 'test_*.py'
python3 -B tools/verify/warm_reclaim/analyze_kernel.py \
  --stage .q4t-work/warm-reclaim-20261020/baseline-v1 \
  --output .q4t-work/warm-reclaim-20261020/review-kernel.json
python3 -B tools/verify/warm_reclaim/analyze_resources.py \
  --stage .q4t-work/warm-reclaim-20261020/baseline-v1 \
  --output .q4t-work/warm-reclaim-20261020/review-resources.json
python3 -B tools/verify/warm_reclaim/audit.py \
  --output .q4t-work/warm-reclaim-20261020/review-audit.json
```

两阶段ENV_ABORT保留：完整热启动未被接受，前置规整不是修复；
工具和诊断收尾不等于模型质量、默认部署或性能验收通过。
