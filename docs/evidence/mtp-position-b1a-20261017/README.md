# B1a精确位置下传证据

结论：[阶段报告](../../MTP_POSITION_B1A_2026-10-17.md)。计算/HTTP通过，
性能证据不足；候选不替换main或已保留生产binary。

- `summary.json`：两组五档、逐次计时/实际长度/首次请求、质量和直接合同；
  所有性能分辨带/差值不确定区间均未建立，不宣称性能接受。
- `on/`、`off/`：完整五档结果、容量、模式/命令、binary及退出记录。
- `quality-on/`、`quality-off/`：各11题结果与身份；schema的HTTP状态码为
  null，使用EvalScope实际success字段，不补造200。
- `contracts.log`：9/9、0 skipped；56组位置/形状逐位合同、k1/3、
  a=0/1/3、全终止/混合终止。required-list和执行binary身份单独保存。
- `boundaries/exit.json`：6个尾部/恢复生成、取消中的生成及取消控制通过；
  原始HTTP状态/响应在本地`boundaries/responses.json`与`cancellation.json`。
- `lease/exit.json`与`direct-exit.json`：完整驱动成功且未检测到竞争；
  不证明轮询间隔内绝无外部干扰。
- `identity-before-http.json`、`contracts-identity-*.json`：HTTP候选与直接
  测试身份分开，准备中未执行的旧测试身份也保留；执行身份以executed为准。
  后续生产源码仅整理同步注释，未改变执行代码。
- `build*.log`：各次构建原始日志；runtime首构建和最终合同构建零警告。
- `raw-manifest.json`：本机`.q4t-work/b1a-20261017/`全部原始文件的大小与SHA。
  模型/参考目录未写入；大体积DB、请求响应和binary不放入Git。

run_phase.py / run_contracts.py / boundaries.py是本次实际驱动快照，包含
固定本机路径；不可在本证据目录执行。复验应在新build/或.q4t-work/目录，
绑定对应源码和binary，替换输出目录并经gpu_exclusive运行，不能覆盖本次
证据或借用原结论。analyze.py为离线汇总；archive.py复制摘要并生成清单。
旧参考是MTP数值收口phase04，不是此次新采集的基线。
