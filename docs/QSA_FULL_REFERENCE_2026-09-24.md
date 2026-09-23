# 完整4K QSA选择与attention核对

## 已完成：全query边界与KV写入

生产二进制2392f6b1未改。只读观测器零警告构建后，首项为
关闭/开启/关闭tools/evalscope原4K HTTP，三次均600440、
4096/7 token、stop、服务0、质量驱动1；36份元数据通过且
采集均在监听后。原错答保持不代表模型质量通过。

本次12层×4096共49152个query，保存完整评分、选择列表、
长度、Q/gate/attention输出、写入K/V、位置、页表和cache。
实际选择/cache到attention输入的指针及完整字节均接通，没有
复制第二份选择/cache。seq id全为0；指针为空时保存默认零值，
元数据seq_ids_null明确标记。实际页表映射恒等且槽0..4095
完整覆盖，50331648个KV BF16值逐项匹配写入K/V。

100859904个选择槽中75546624个有效索引，全部长度符合分组/
尾部规则、唯一且不指向未来，未使用槽均-1。这不证明选择排序
符合评分，完整排序参考仍待运行。Q、gate和attention输出各
301989888个BF16值完整保留且有限，attention算术尚未重算。

132份旧末query/完整张量逐字节一致。初版分析误将旧manifest
当作直接绑定原始文件而KeyError，analyze-v1.py/log保留；旧
manifest实际绑定capture-binding.json，再由其绑定原始文件。
修正后两层摘要链全部核对，无来源内容变化，无需重跑HTTP。

证据：`.q4t-work/e2e/qsa-full-selection-20260924/`；模板：
`tools/verify/qsa_full_selection/`。含完整HTTP、采集、旧来源
绑定及manifest，归档后可用58702303232字节。采集预算3.2GB、
参考1GB、另保留20GiB；未来超出页表预算必须停止，不能截断。

## 未完成与下一步

先用冻结的完整评分重建所有query的选择顺序，再核对完整
attention。评分计算、Q/gate来源、完整投影和out_proj消费者
仍需逐步接通/重算。此前末query/首decode局部参考不能代替
全query覆盖。本阶段是给定实际输入的边界证据，不是独立整模型
前向；原错答、已知量化尺度缺陷及其因果关系仍未解决。
