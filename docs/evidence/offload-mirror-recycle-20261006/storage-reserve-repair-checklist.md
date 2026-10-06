# m062 启动前磁盘余量修复（仅准备）

具体失败已保留为 `m062-preflight-first-failure.json`。前次压缩结束
free=1,074,106,368 字节，只高出 runner 门 364,544 字节；后续元数据
写入后，m062 在 controller/service/HTTP 启动前触发磁盘断言。
此前两轮存储结果、原 gzip、map、summary、journal 与首失败全部保留。

本修复只把**存储回收目标**设为 1 GiB + 32 MiB，即 1,107,296,256
字节。冻结 runner 的启动门仍为 1,073,741,824 字节，源码和验收计划
不改，不增加 HTTP/模型次数，不把门前失败记录成 HTTP 失败或重跑。

原压缩清单 246 项，减去原 durable map 已处理的唯一一项，恰余 245 项。
明确列表在 `storage-reserve-repair-policy.json`：逻辑 62,383,022 字节，
分配 62,611,456 字节（59.71 MiB）。当前仅核对路径/元数据与已有
preflight 的原 SHA 记录，没有重新读候选内容。实际节省未估定，不能
为满足目标扩大候选范围。原 gzip 只检查元数据和已记录身份，不重读。

执行必须满足：前 16 个服务 stage/audit/decision 均通过且 runtime/plan
身份一致，所有 owned units 已结束；m062 controller、输出目录与服务
尚未启动；全部旧存储 compact 记录及首次失败 SHA 未改；新候选集合
精确等于原 manifest 减旧 map，nlink=1，未被任何当前来源 ledger 绑定。
prepare 只生成最终固定清单；root 再冻结 manifest SHA，execute 才处理。

独审后，root 在服务间隙运行：

```sh
python3 -B "$R/storage_reserve_repair.py" prepare \
  --policy-sha256 e041a16f5045abe2996967281269f673ed4afc6ae56567750ae9eaad38ca0939
```

冻结输出的 `storage-reserve-repair-plan.json` 与其真实 manifest SHA，
替换下方 HASH 后执行一次：

```sh
python3 -B "$R/storage_reserve_repair.py" execute \
  --policy-sha256 e041a16f5045abe2996967281269f673ed4afc6ae56567750ae9eaad38ca0939 \
  --manifest-sha256 HASH
```

新产物只进入 `R/storage-reserve-repair`。新目录及父目录先 fsync；
原 SHA/长度→gzip level1→压缩文件 fsync→流式解压 SHA/长度核对→
持久 map→原件 unlink→原目录 fsync。新 map 与前次 map 分开。达到
存储目标即停；即使剩余清单耗尽仍不足，也不能降门或继续扩大删除。
逐步骤 journal 与新独立首失败保留，不自动重试。恢复时按对应 map
创建独立临时原格式文件、核对 SHA/长度后原子替换，需要额外空间。

summary 分别记录 runner 原门是否满足和 32 MiB 余量目标是否满足，
净 payload 回收与真实 free 前后分别报告。原两轮 summary 不改。
最终归档包含本次 failure/repair 计划、独审、脚本、map/journal/summary
和 owned receipts；gzip 只留本地并列 locator/SHA。未运行任何新测试。
