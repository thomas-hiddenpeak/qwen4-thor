# MCP容量修复：主轻资格与rev416可回退交付

2026-11-20，非性能缺陷修复。默认和保留包都包含绑定实际分配容量
校验，避免复制容器先缩小capacity而绕过资源合同。

- [24项容量/封包CPU](../serve-realtime-mcp-ci-capacity-20261120/README.md)、
  [11项资格CPU](../serve-realtime-mcp-binding-gate-20261120/README.md)通过。
- [真实主轻短验](../serve-realtime-mcp-binding-http-20261120/README.md)
  含原驱动失败共3启动/5生成/159.322秒；修正后两档均真实审批、读取
  项目资料、返回完整标题与引用，结算正确，150次NVML正常且完成清理。
- 新资格registry为`82be1c49a5cc21cbebadb1a7fffbc3ef60f58467937d9ae6de258392ee59a615`。
  原业务叶子和错误回答不改写；真实新身份短验单独绑定。
- 19条原生管理命令通过，350.683秒：406→408→410→412→414→416，
  包含实际整根回退和再前进，最终STOPPED。客户凭据、配额、用量、
  核心/外层状态与profile历史、时钟、预算保持。发布新增GPU启动0。
- 当前包`4ca02574aca05e98081f3db5fde79ca40ef5549f051c03af2684139df84a5568`，
  保留包`2fc04b4767f7b3cc7750442af253c80147c323fe1fcdd9b2d953f75713db94d3`；
  两者同用引擎`40359a27d7c59bf892a85aa239de3db81b9670305170849d6c9118088f99ec95`。

控制器、Guardian、TLS、模型资产和业务配置未变部分复用原实际恢复
证据，原证据保留当时包/引擎身份；没有新跑托管故障组合，不将新包
宣称为原GPU运行的被测对象。原模型质量FAIL保持，本次两个回答PASS
不替代完整模型质量或性能验收。总计划仍active，继续main及剩余任务。

发布前7条命令可逆归档4项intent、4项generation和4项version记录，
141.668秒，删除文件0、当前状态保持。离线组合曾用错资产字段及
遗留旧发布元数据，两次未发布草稿保留，最终使用第3版正确组合。

[验收摘要](acceptance.json)、[逐文件SHA256](index.json)和
[原始证据包](evidence.tar.gz)包含资格、命令输出、组合及驱动源码；
私有策略、凭据、完整运行状态不在公共归档中。旧rev406原件保留。

完整主轻资格文件保留在实际当前/回退包及本机原路径。公共包以原
已公开资格、新短验证据和构造来源去除重复大文件；
[重建脚本](reconstruct.py)已实际重建两份完整文件，字节数和SHA256
均与发布原件一致，且再次通过资格校验。原错误回答和旧业务没有删减。
可在仓库运行，输出目录必须尚不存在：

```bash
python3 -B docs/evidence/serve-realtime-mcp-binding-delivery-20261120/reconstruct.py \
  --output .q4t-work/mcp-qualified-reconstruction
```
