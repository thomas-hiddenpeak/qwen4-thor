# 组合验收驱动与压缩夹具CPU结果

2026-11-03，运行时父39cb0ba3。只新增[组合验收](SERVE_MEDIA_COMPOSITE_2026-11-03.md)
工具与探针，生产280b95dc/validation00718dea、protocol29810d03及211
运行时来源保持，默认仍3fd31a21/3ba0b8d3。没有GPU/模型/原生HTTP尝试。

两个探针直接链接相同生产静态库，零警告：

- host夹具探针`93386f913c0953c72f724eb760f71b2b33896580803bfd1436f3a7e937a70ad8`。
- 设备探针`59aba92a2b6179e299d10c9b69bb1cd731b469959484cf90c5a62fe6e3ab7dd9`，
  已构建，**尚未执行**；实际函数能力与模拟查询失败须在GPU lease内验证。

11个固定负例已走真实host合同、URL/头部/几何/容量代码，code与param
逐项匹配。计划中的attention上限在CPU夹具为假想47104字节，不能当
作实际Thor测量。GPU验收另查询生产函数并与配置发现和HTTP拒绝对账。

新增PNG宣称1×1，IDAT流式生成256MiB+1零字节的压缩数据；输入不到
1MiB，生成器每次使用1MiB块。实际生产processor触发media_decode_limit，
解码器可见所有权峰值134,478,682字节，低于268,435,456字节额度，失败
输出清空；随后正常红图成功。这个结果证明本夹具的拒绝/恢复，不证明
所有恶意压缩输入或进程/整机物理RAM峰值，未制造物理OOM。

首轮10项离线驱动检查和11个host夹具全部通过。收尾静态审查发现继承
安装器硬编码1500/1800秒，与事前冻结2400秒不符；GPU尚未执行。
本批使用独立事务入口，保持既有pair安装/失败回退流程，将安装预留和
最终截止绑定本批限制。增加3个模拟事务检查：1600秒可继续、2100秒后
在修改默认pair前拒绝、安装健康失败必须调用已知pair恢复；总13项通过。
模拟检查只在临时目录修改假pair，不当作实际发布/回退验收。

早期构建/10项结果保留于`cpu-before-deadline-review/`；两个C++文件仅
换行整理，重建后ELF SHA均相同。新驱动重验13项，实际解压夹具按同ELF
及未变输入明确复用，没有为工具/排版再解压或重跑整模型。

工具目录为`tools/serve_media_composite/`，本机记录在
`.q4t-work/serve-media-composite-20261103/`。完整错误wire先读至EOF再
解析，拒绝缺DONE、重复错误、错误param或伪成功；实际GPU候选还需
32生成/21负场景/11启动矩阵、原生25/权重与上传等范围全部完成。
本批未扩媒体+显式seq-capacities组合，未把CPU成功当业务交付。

组合包已按工具提交`abb653c8263d721babc640b3438b74872e83df5b`封存，
准备后来源/CPU门禁核对通过。候选、旧默认pair、夹具、SDK/模型只读
输入与工具来源均固定在`package/`，没有`execution/`或安装恢复标记。
[原始记录归档](evidence/serve-media-composite-cpu-20261103/README.md)保留
140份记录，9份Git文件共424,928字节；19个本机ELF/bundle身份逐项匹配。
[暂存保管核验](evidence/serve-media-composite-cpu-custody-20261103.json)
9份Git blob逐项通过，654个变更文档链接无缺失；本次封存不重复CPU、
解压或模型验证。

18:45:13 CST只读预检仍有vLLM PID2336565占93,864,288,256字节，
环境入口记ENV_ABORT，继续保留服务。只有GPU空闲或该服务已完成授权
交接，才执行下列入口；launch内部持有一把完整阶段lease，成功后立即
finalize，仍计入原2400秒。失败保留现场，不能自动重试或扩充预算。

```bash
python3 -B tools/serve_media_composite/stage.py launch &&
python3 -B tools/serve_media_composite/stage.py finalize
```

旧02h包与02i CPU包保持原seal，不删除失败或覆盖旧来源身份。当前
工作包已完成不依赖GPU的准备；下一步是原生HTTP、设备探针及32/21/11
真实业务矩阵，满足门禁后才按既有授权替换默认并交付。实际网络入口、
通知接收与多人策略仍待部署范围确定，不从本批CPU成功推导为已支持。
