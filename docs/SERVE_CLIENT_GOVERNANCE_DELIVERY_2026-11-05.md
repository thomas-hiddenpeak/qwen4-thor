# RPM/TPM 与公平准入：有界交付完成

2026-11-05，**ACCEPT_BOUNDED_CLIENT_GOVERNANCE / DELIVERED**。
授权补验通过，同一候选已成为本机默认程序，精确父版本及依赖已保留。
配额和公平准入均显式可选；不提供策略文件时维持原先的准入行为。
主线入口为[PR #12](https://github.com/thomas-hiddenpeak/qwen4-thor/pull/12)，
合并及检查状态见PR；本记录的发布身份不依赖后续文档提交。

## 已交付合同

| 能力 | 已接受范围 | 必须保持的边界 |
|---|---|---|
| RPM/TPM | v2每身份原子预留1请求和真实模板prompt+可用输出；终态结算实际逻辑token | 单进程60秒固定窗口，重启清零；不是任意滚动60秒或分布式计费 |
| 拒绝与取消 | SSE前429及稳定原因码；等槽取消全退token，已准入按实际使用结算 | 已接受请求的RPM不退；缓存命中仍计完整逻辑prompt |
| 客户端准入 | 已准备请求按身份轮转，每身份floor(global_queue/client_count)等候份额 | 无抢占、不借份额，不承诺准备/GPU时间/吞吐/时延公平 |
| 默认关闭 | 同ELF的JSON/SSE、内容、usage、发现声明及指标实机通过 | 不传client-policy-file；v1严格合同保持，v2须显式启用 |
| 实际工具业务 | 两个真实项目资料查询闭环与引用独立重建 | 复用原身份的4完整工具响应，不把原失败尝试改写为整体通过 |

配置见[策略用法](../tools/serve_gateway/README.md#可选-rpmtpm-与准备后公平准入)。
启用公平队列时，客户端max_inflight之和不得超过全局并发，等待总数至少
覆盖客户端数；不满足会在启动时拒绝。限额为0表示该分钟限额关闭。
实际资源保护仍受全局并发、队列、输出与CUDA申请额度等已有合同约束。

## 短期验证与接受依据

复用零警告构建、303公共host/269必需/9结构化、24个不同Python案例，
协议生成与库存检查。补验驱动的6个CPU合同通过，最终来源冻结后再次
核对；案例按6个不同案例计数，不把两次执行相加。运行来源223项中只有
10项serve变化，213项不变；数学、kernel、tokenizer、模板、缓存计算证据
按来源复用。业务功能交付不声称性能优化或全模型质量重新通过。

| 阶段 | 生成与业务结果 | GPU租约 |
|---|---|---|
| 先前限定观察 | 2模型/16尝试，12完整响应、2真实资料工具、2取消、2配额拒绝 | 含入口失败累计155.816秒 |
| 授权无策略补验 | 1模型/2尝试，JSON和SSE各1，均准确回复710003 | 41.487秒，39次观察，COMPLETED |
| 累计 | 3模型/18尝试/31控制；14完整响应、2工具、2取消、2拒绝 | 197.303秒，小于原480秒上限 |

新增两响应各22输入/7输出token，usage与最终指标一致。handler分别
431.107/325.677ms；JSON收到响应头后正文读取0.235ms不当作E2E时延。
补验业务驱动39.401秒；安装健康复用相同ELF的HTTP，只增加CLI检查，
没有再次启动模型。实际无策略配置为policy关闭、fair_queue=false、
policy_version=1、capacity_fifo_best_fit，分钟指标无client标签样本。

旧夹具/入口失败按[前轮结果](SERVE_CLIENT_GOVERNANCE_RESULT_2026-11-05.md)
保留，只有已完成且身份不变的观察复用。本轮没有失败重试或增加请求。
发布事务核对新旧pair后替换；guard完成后才确认DELIVERED并清除标记。
新增模型正常drain、进程消失，端口18138/18458、GPU、原共享锁
dev66305/inode3294067释放；SOAR恢复单元仍masked，容器空，无常驻服务。
轮询未观察到竞争，不能排除采样间隔内的不可见干扰。

## 发布与核验入口

- 默认ELF：`c1bdb0a7599ba3f61d9cb48597069a66f6e27e02f38cf32e495fc80059008636`。
- sidecar：`0efc2c0dc309e42a7b7761860edce7888aa8139f5ea305f54eae054a81fdbcf3`。
- 协议：`9c8758c4f85e1d4f7621bd8854c88f64d9cc739744669754aab18c7e183e9706`。
- 实现首提交ca93be03；完成阶段冻结来源95f86318。发布元数据绑定本轮
  实际无策略config/capabilities及冻结Schema，并引用原治理和工具证据。
- [最终结果](evidence/serve-client-governance-completion-20261105/result.json)、
  [清单](evidence/serve-client-governance-completion-20261105/manifest.json)、
  [原件](evidence/serve-client-governance-completion-20261105/records.tar.gz)：
  110原件/118本机引用，SHA256为ad0c43ffb3e4092c8d18562991dc8e96d1510ff9d2e694613f9411b99a4afa6f。
  原415原件/506引用归档保持不变并被独立重建。所有轮次的凭据及取消
  token都参与发布扫描；含秘密请求、私钥、ELF留本机，归档不是灾备包。
- 本机精确父pair `a56bd448/914e067f`、依赖及候选保存在
  `.q4t-work/serve-client-governance-20261105/completion/package/{previous,candidate}/`；
  不能只回退ELF或覆盖已经封存的包。原package-04及失败现场同样保留。

```bash
python3 -B tools/serve_client_governance/completion_report.py verify
```

该离线入口从原始wire、额度账、工具材料、进程记录、租约和发布pair
重建结论，不启动模型。旧观察的默认身份核对封存previous，当前安装
身份另核对新pair，防止把历史“默认未变”误当作当前状态。

## 后续接手边界

旧托管包`41dc8fdb`、持久任务和Key/day计划继续固定其已验身份；新协议
不自动加入持久迁移兼容矩阵，不就地替换旧包ELF。当前API功能可用于
新的直接serve进程；托管升级或持久任务迁移须另冻结明确组合。

有界队列的实现项至此完成：部署profile已交付；内存退场组合仍为
NO_GO_MEMORY_STOP_COMBINATION，c1653470未接受；整机物理RAM关卡仍为
NO_GO_CURRENT_CONTRACT。不得把有界退出结论改称这两项已实现。
小时/天级长稳由用户执行，旧day不直接套用本轮hash。实际LAN、正式通知
及灾备按选用场景验收；本轮没有扩展这部分或增加性能优化实验。
