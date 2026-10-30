# 审计8项P2修复与最终交付

8项全部修复，按权重/驻留、服务配置/监督、验证工具三阶段完成。
默认二进制b11ed66c、最终sidecar 982c888c；运行时源码98c42bf，
工具源码76963ad。第三阶段只同步工具接受元数据，复用第二阶段
相同程序字节的运行时验收，无新增模型HTTP。

工作分支codex/audit-p2-fixes按阶段提交推送，未自动合入main；
历史研究分支/工作区与其他任务内容保留，无常驻服务。

## 逐项状态

| 审计项 | 修复与主要证据 | 结果 |
|---|---|---|
| A1 权重区间/整数溢出 | 精确整数、shape字节数/区间/重叠及文件边界；非法输入写入前拒绝 | 已交付 |
| A2 SSD专家跨分片误读 | 连续/合并快读核对weight、SF和标量分片；异分片同offset毒化夹具逐字节oracle | 已交付 |
| A3 Open失败泄漏 | fd/map RAII覆盖早退和分配异常；重复失败前后资源一致 | 已交付 |
| A4 部分Init回收 | device/pinned/event、mutex/worker/后续线程故障回滚；重试、幂等Free及在途DMA保留 | 已交付 |
| A5 decode容量准入 | server加载前/预算API校验有效forward行数，默认2048和关闭预算等路径覆盖 | 已交付 |
| A6 长请求监督误杀 | 成功执行边界计数＋监督消费者；实际在途采样和受控时钟停滞/取消反例 | 已交付 |
| A7 IO测试重复符号 | 4个独立目标，24项直接合同；真实CUDA版本另按lease执行 | 已交付 |
| A8 pidfd能力缺失 | API/内核实际探测先于监控/子进程；公共CMake解释器选择及显式拒绝 | 已交付 |

## 验证与接受边界

- 第一阶段：24项IO/资源CPU、108 host、15真实GPU合同；16父候选
  HTTP＋6实际4槽安装健康。累计22HTTP/12启动，含2次环境中断。
  详见[第一阶段结果](AUDIT_P2_STAGE1_RESULT_2026-10-31.md)。
- 第二阶段：109 host（73发布要求）、28预算、5监督、4无效CLI；
  12父候选HTTP＋6实际4槽健康，共18HTTP/8启动，无ENV_ABORT。
  三模式完整输出相同，且实测结束前执行计数推进。详见
  [第二阶段结果](AUDIT_P2_STAGE2_RESULT_2026-10-31.md)。
- 第三阶段：公共host构建零警告，22/22 CTest通过，含102项公共
  C++、30项guard、5项supervisor、5项rollback及其他工具组。实际
  UV Python 3.13能力不足时ENV_ABORT/75、child_returncode为空；
  CMake显式不支持者失败，默认自动选/usr/bin/python3。新增受支持
  guard用于元数据安装并正常完成，无模型加载/生成。

全程40生成HTTP/20模型启动、2个GPU合同进程共15项检查；两个
运行时阶段分别保持原90分钟绝对截止和冻结预算。各次完整驱动持
共享GPU锁。最终程序/元数据配对、169项运行时源码和全部封条核验
通过，恢复标记清除，已知默认可回退。

第一阶段两次4槽安装伴随OOM/NVML失败，均ENV_ABORT并自动恢复旧
默认；用户授权后正常停止约8.2GiB的OpenCode服务，保持原4槽配置
重验通过。另约2.1GiB Node实际属于Codex路由，纠正此前误分类并
保留。原失败、相对guard路径的零启动失败以及CPU修复过程均保留。
第三阶段的预期能力拒绝是负向合同，不是新增安装环境故障。

本轮属于正确性/可靠性修复；时延均保留原值，单配对不支持统计
非劣或性能优化结论。SSD逻辑读取/继承页缓存不证明严格物理SSD
供给或整机RAM上限。长监督时间由生产循环的受控时钟覆盖，真实
HTTP证明生产指标在途更新，不声称21分钟或3小时实际生成长跑。

## 封存与默认回退

原始证据清单：
[第一阶段180份](evidence/audit-p2-stage1-20261031/files.json)、
[第二阶段133份](evidence/audit-p2-stage2-20261031/files.json)、
[工具与最终元数据](evidence/audit-p2-stage3-20261031/files.json)。
每份均记录来源、SHA256及字节数；本地各阶段有独立封条副本。
最终身份收据见
[finalized.json](evidence/audit-p2-stage3-20261031/metadata-archive/finalized.json)。

最终封条包位于
`.q4t-work/audit-p2-20261031/stage3/delivery-package/`。candidate为
b11ed66c/982c888c，previous为上一运行时5e6249fc/cb90c388，真实
构建缓存及源码清单齐全；before-metadata.json保留本次更新前6ed16aec。
head、运行时接受记录和工具检查由元数据的路径/哈希绑定至阶段包，
stage1/stage2的完整源码、工具、head与回退链继续保留。

只读检查：

```bash
/usr/bin/python3 -B /home/rm01/models/dev/qwen4-thor/tools/verify/audit_p2/rollback.py \
  --package /home/rm01/models/dev/qwen4-thor/.q4t-work/audit-p2-20261031/stage3/delivery-package \
  --check
```

需要回退默认程序时，在新输出目录下持完整lease执行以下命令，将
默认恢复为5e6249fc/cb90c388。未知默认、损坏包或外来恢复标记
拒绝覆盖；分文件原子替换不声称跨文件断电事务。

```bash
audit_rollback_out=/home/rm01/models/dev/qwen4-thor/.q4t-work/audit-p2-rollback-manual-01
/usr/bin/python3 -B /home/rm01/models/dev/qwen4-thor/tools/evalscope/gpu_exclusive.py \
  --output "$audit_rollback_out/lease" -- \
  /usr/bin/python3 -B /home/rm01/models/dev/qwen4-thor/tools/verify/audit_p2/rollback.py \
  --package /home/rm01/models/dev/qwen4-thor/.q4t-work/audit-p2-20261031/stage3/delivery-package \
  --output "$audit_rollback_out"
```

每次使用新的输出目录，保留旧证据。若需进一步回到审计前037328bb/
3200266e，在已恢复5e6249fc后使用stage1/delivery-package与另一个
新lease目录；原冻结驱动不再要求作为当前回退入口。
