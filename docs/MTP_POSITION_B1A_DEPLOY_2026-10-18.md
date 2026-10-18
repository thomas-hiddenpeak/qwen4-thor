# B1a默认二进制部署

2026-10-18。DEPLOYED_DEFAULT_EXECUTABLE。用户要求继续部署后，将已验证
B候选部署到build/q4t，并更新q4t.release.json；未重建二进制。
当前无运行中的q4t服务，因此保持停止状态，不新建常驻服务或systemd单元。
“部署”在此指默认可执行入口已更新，不表示HTTP服务已上线。

## 身份与结果

- B SHA256：44abd8b1a7edaca41620b87b4cf894427f8397c1e3529ef56512940ef1b44e03。
- A SHA256：8685d8bf4068b81df0f5162d6c55924d93eae793d17664a1b465ca7587d6c6ee。
- 包目录：.q4t-work/releases/b1a-20261018，包含已验证二进制、真实构建缓存、
  原验收服务参数和清单；缓存SHA ca2e60a10bb4c6c3d653a73437283e543797d119b5fbd7e9cdc82835af549e06。
- 运行时源码相对实现提交1c6e792无变化，模型config/index摘要匹配原验收。
- gpu_exclusive完整驱动COMPLETED/exit 0，无竞争；新增HTTP/GPU请求0。

原build/q4t.release.json仍指向9月的3b414633版本，早已不匹配A；先完整
保留原文件，再更新B的真实身份，防止旧验收身份误用。新元数据明确有限
工程接受及默认二进制范围，不伪称旧text-v1监督服务的完整部署资格。
未改变模型、服务参数、默认MTP k=3或--no-mtp回退语义；不扩展B1b。

[部署记录](evidence/b1a-deploy-20261018/deployment.json)、
[执行驱动](evidence/b1a-deploy-20261018/deploy.py)、
[互斥退出](evidence/b1a-deploy-20261018/lease/exit.json)。
已接受的计时不确定性、合同边界继续以[主线交付](MTP_POSITION_B1A_MERGE_2026-10-18.md)
及最终审查为准，不因部署提高证据等级。没有追加采样或校准。

## 使用和回退

后续正常启动build/q4t即使用B；没有为此次文件切换新建服务、发送请求
或采集新的健康/性能数据。若要常驻服务上线，应明确该服务生命周期与
端口配置，不能把文件切换记录当作线上健康证明。

A保存在.q4t-work/b1a-deploy-20261018/previous/q4t，原先保存的
.q4t-work/b1a-20261017/q4t-baseline也保持不变。发生明确计算错误或
可归因退化时，在停止相应服务后按以下入口恢复默认A：

```bash
python tools/evalscope/gpu_exclusive.py \
  --output .q4t-work/b1a-deploy-20261018/rollback-lease \
  -- python .q4t-work/b1a-deploy-20261018/rollback.py
```

[回退脚本](evidence/b1a-deploy-20261018/rollback.py)校验A/B和缓存身份后
恢复A及匹配元数据，不复用失配的9月sidecar。回退入口已保留、未执行；
当前仍是B。若需要重复回退应使用新的lease输出路径，不覆盖旧现场。
