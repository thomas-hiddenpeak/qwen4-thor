# main复审四项遗留修复与交付

R1～R4均已修复并通过限定验收，默认程序已替换为
`0c5ab1de85ff68f9d757ac3bfad04187756a1b2c6634108c3112a5861c209483`，sidecar为
`e6fdd13b7d140926ed4a0d42bcda37b6ec6689305960e3deab03b3f73dc294d6`，源码/工具锚点55299a0。
代码提交在codex/main-reaudit-fixes，main仍为ef88d97；未自动合入main。
默认仍为**全部专家驻留（moe-resident-slots=0）、完整head、MTP k3**。
SSD按需模式及其他可选功能仍需显式配置，没有安装常驻服务。

## 四项修复

| 项 | 实现与直接证据 | 结果 |
|---|---|---|
| R1 目的张量合同 | 全驻留/按需共享dtype、二维shape及精确字节校验；标量只接受F32标量或单元素向量。定长读取在写入前拒绝大小不符；12种合法文件内extent但错误消费者张量均拒绝且零payload分配/拷贝 | 已交付 |
| R2 热专家重复 | 解析按首次出现顺序去重并以不同专家数计容量；InitHot直接调用去重、只填空槽，重复调用保留已有专家。两种派发方式下容量、保护计数、重试和逐字节payload正确 | 已交付 |
| R3 SSD加载线程异常 | StageTask捕获所有host异常、不在异常边界分配错误消息；等待者收到完成通知。claim作用域回收，CommitExpert事务涵盖部分DMA后的异常；排空成功可重试，未知完成保留payload并拒绝继续加载 | 已交付 |
| R4 监督器后代回收 | subreaper回收孤儿，pidfd加启动身份发信号；停止和重启以整个受管组为空及reader结束为准。清理失败保留状态并拒绝新启动/切换；5种真实CPU进程场景通过 | 已交付 |

实现见[专家张量合同](../include/q4t/quant/moe_tensor_contract.h)、
[定长读取](../src/io/weight_loader.cpp)、
[驻留与任务回收](../src/quant/moe_residency.cpp)、
[监督停止](../tools/release/release.py)。原审计在修复前后的反例均保留，
本轮直接验证消除了其对应失败；不声明项目所有异常路径均已覆盖。

## 验证与工程接受

| 范围 | 实际结果 |
|---|---|
| CPU直接合同 | IO/专家4/4 CTest、31合同；预算29；Thor host109（含发布要求73）；监督10；回退5 |
| 公共host入口 | 本机22/22 CTest通过，含上述部分重叠工具/host检查，不能重复累加为独立覆盖 |
| 构建 | IO＋真实CUDA变体、预算、公共host、Thor整机四套构建均零警告 |
| 真实GPU | 2进程、21合同；11合成分片/分配与异常合同＋10真实模型专家与部分payload失败合同 |
| HTTP对照 | 父/候选plain、full、subset、SSD plain，各1K/8K；16请求/8模型启动，完整text/usage/finish逐项相同，答案710003/710765 |
| 安装健康 | 2启动/6请求；实际208896＋3×65536请求槽，prefill4096；两head答案831907/44，公共8192/私有9230缓存命中 |
| 生命周期 | 每次正常结束均释放请求，启动预留最终handed_live/unhanded_remaining为0；最终GPU进程为空，恢复标记清除 |

从prepare到finalize约10.73分钟，合计22HTTP/10模型
启动，未使用26/12上限中的重验额度；GPU进程2/3。两个完整驱动各
持共享GPU锁，guard均COMPLETED/rc0，ENV_ABORT为0，无非预期失败。
监控为合作锁加轮询，不能排除短于采样间隔的干扰。原8项修复与
原复审失败证据均保持，未变的163项运行时文件按身份复用既有算子/
MTP/缓存数值依据；本轮没有重开五档性能研究。

证据有效性：身份、guard、输出和资源合同通过。测量完整性：冻结
的21 GPU合同、16对照HTTP和6安装健康全部完成。统计结论：单配对
不支持性能持平/非劣声明。工程决定：以正确性修复及资源回收为目标，
按既有授权接受本候选；小幅观察波动不作为重复扩样理由。

以下为同配置单次原值，单位秒；每格为父版/候选，未设置事后统一
容差，也不把某档改善抵消其他档损失。对照请求输出均为7 tokens。

| 模式 | 输入tokens | 请求总时长（父/候选） | TTFT（父/候选） |
|---|---:|---:|---:|
| 普通decode | 1024 | 1.1985 / 1.2356 | 0.8606 / 0.8997 |
| 普通decode | 8192 | 5.7062 / 5.7305 | 5.3723 / 5.3956 |
| MTP完整head | 1024 | 1.1715 / 1.1685 | 1.0818 / 1.0803 |
| MTP完整head | 8192 | 6.0945 / 6.0948 | 6.0034 / 6.0054 |
| MTP子集head | 1024 | 1.1301 / 1.1471 | 1.0492 / 1.0664 |
| MTP子集head | 8192 | 5.9608 / 5.9576 | 5.8778 / 5.8749 |
| SSD C256普通decode | 1024 | 19.2478 / 19.3543 | 17.0010 / 17.0406 |
| SSD C256普通decode | 8192 | 28.7357 / 28.5848 | 26.2375 / 26.0828 |

普通decode 1K总时长增加约37ms（单点+3.09%），其他观察变化见表；
不将点差解释为已确认性能回归或统计噪声界。SSD组固定C256、L2=16、
mirror=8、workers=16、streams=1，继承页缓存；逻辑读取不证明持续
物理SSD供给或严格整机RAM上限，host/device/cache仍共用Thor DRAM。
监督器CPU检查覆盖同session/受管进程组的后代，不替代外部服务管理
对监督器自身SIGKILL或主机故障的恢复责任。

源码提交的[公共host自动检查](https://github.com/thomas-hiddenpeak/qwen4-thor/actions/runs/41135985870)通过，
只作为补充记录，不替代上述Thor实测。

## 回退与证据保留

[原始证据172份及SHA256清单](evidence/main-reaudit-fixes-20261031/README.md)
含全部构建、CPU、GPU、HTTP、guard、部署收据和原始计时。
170项运行时源码、13项工具及226个冻结输入均核验；交付包、测量
归档和健康归档分别封条核验通过。原路径统一保留在
`.q4t-work/main-reaudit-fixes-20261031/`。

完整回退包为该目录下`delivery-package/`：candidate为0c5ab1de/
e6fdd13b，previous为b11ed66c/982c888c，保留真实构建缓存、源码、
工具、head及接受记录。旧审计stage1～3回退链和研究工作区均保留。
另保留已验证的`source.bundle`（候选55299a0，前提ef88d97仍在main）。
本轮工作分支尚有独有修复提交，继续保留；未清理其工作区或证据。

只读检查：

```bash
/usr/bin/python3 -B tools/verify/main_reaudit/rollback.py \
  --package .q4t-work/main-reaudit-fixes-20261031/delivery-package --check
```

实际回退使用新输出目录并完整持锁，将默认恢复为b11ed66c/982c888c：

```bash
reaudit_rollback_out=/home/rm01/models/dev/qwen4-thor/.q4t-work/main-reaudit-rollback-manual-01
/usr/bin/python3 -B /home/rm01/models/dev/qwen4-thor/tools/evalscope/gpu_exclusive.py \
  --output "$reaudit_rollback_out/lease" -- \
  /usr/bin/python3 -B /home/rm01/models/dev/qwen4-thor/tools/verify/main_reaudit/rollback.py \
  --package /home/rm01/models/dev/qwen4-thor/.q4t-work/main-reaudit-fixes-20261031/delivery-package \
  --output "$reaudit_rollback_out"
```

未知默认、损坏包或外来恢复标记均拒绝覆盖。分文件原子替换保留
持久恢复标记，不声称程序/sidecar跨文件断电事务。实际回退尚未
执行；只读真实包核验与CPU合成包恢复合同均通过。
