# 8K suffix 候选离线取证

结论：当前固定 k3 在这条输入中把“数字、逗号、空格”的短重复当作续写
来源，首 token 与 d0 一致仍不足以保证后两 token。通过原始 SSE token
恢复、客户端步骤推断和生产 SuffixDrafter，可复核两 head 各三请求的
全部 15 次 suffix 选择以及原日志聚合计数。未做 GPU/HTTP，没有改运行时。
这定位了提案质量问题；不单凭此量化所有耗时回退归因。

## 身份及方法

- “8K”是档位名；实际输入 8159 token，输出 256 token。
- 从原始 benchmark_data.db 只读提取 response_messages；限制反序列化
  禁止 pickle global/class。排除初始role和终止finish消息后，每请求恰
  256个逐token content chunk；拼接等于保存输出、与usage=256一致。
- 复用受测libq4t_text/libq4t_io，穷举冻结模型域 [0,248320) 的生产
  Decode([id],skip_special=true)。Tokenizer::VocabSize为248077，
  248044基础ID+33个连续added IDs；248077..248319共243个padding ID
  全部Decode失败。248077个可解码ID中，每个观测chunk恰匹配一个ID。
  因此没有把“整段文本重新Encode”当作原输出token序列。
- 4臂×3请求的prompt IDs和256个output IDs完全相同；prompt生产Encode
  恰8159。C++helper链接仅ICU/text/io/标准库，无CUDA依赖。
- 原始SSE单token边界由chat_generation.cpp逐token Decode/Write验证。
  步骤starts来自独立client-bursts-8192.json的ITL自然间隙及末尾批次拆分，
  是客户端推断，不是新增或原生d0/step trace。SHA256：
  `de99d605c9428b931fa62f448a92b6f0c6fe9a6c769e8c2c414a26bcb1d11d7e`。
- 在这些starts使用真实SuffixDrafter选择候选；a>=1时d0必是该步第二个
  已发token。六个a0步骤均无三元组匹配，无须猜测其d0。每步查询上下文
  是已消费prefix+pending，逐token枚举与批量Append建立相同有序索引。
- 六条ON请求均完整复现：queries73、hits15、no_match24、d0_mismatch34、
  incomplete0、candidates117、compared642、matched201，suffix接受分布
  [a0,a1,a2,a3]=[0,7,3,5]。不是只凑中15这个数字。
- 当前suffix/tokenizer/server相关源码逐字节等于受测source-snapshot。
  文件、库、helper binary、tokenizer及原SQLite SHA见伴随JSON。

## 全部15次重建选择

位置是输出pending token的0基索引。P是prompt绝对0基结束位置；G是
生成序列内0基结束位置。实际续写列始终显示后三token，a<3时包括
被target更正后的续写；它不是“被接受的三个草稿”。所有六条ON请求相同。

| pending位置 | 类型 | 匹配token数 | 历史结束 | 精确匹配文本 | suffix三token | 实际后三token | 接受a |
|---:|---|---:|---|---|---|---|---:|
| 16 | 数字/字段格式 | 3 | P8093 | ` 25` | `1: A` | `1 records,` | 1 |
| 23 | 提示指令复述 | 3 | P8149 | ` measurement, storage` | `, scheduling,` | `, scheduling,` | 3 |
| 27 | 提示指令复述 | 7 | P8153 | ` measurement, storage, scheduling, and` | ` correctness.\n` | ` correctness. Let` | 2 |
| 62 | 模板引用 | 3 | P7833 | ` cache can reuse` | ` recently accessed data` | ` recently accessed data` | 3 |
| 66 | 模板引用 | 7 | P7837 | ` cache can reuse recently accessed data.` | ` An estimate of` | ` An estimate of` | 3 |
| 70 | 模板引用 | 11 | P7841 | ` cache can reuse recently accessed data. An estimate of required` | ` bytes is different` | ` bytes is different` | 3 |
| 74 | 模板引用 | 15 | P7845 | ` cache can reuse recently accessed data. An estimate of required bytes is different from` | ` a measurement of` | ` a measurement of` | 3 |
| 78 | 模板引用 | 19 | P7849 | ` cache can reuse recently accessed data. An estimate of required bytes is different from a measurement of physical` | ` memory traffic.` | ` memory traffic."` | 2 |
| 157 | 数字/字段格式 | 3 | P3455 | ` 10` | `9: A` | `9, ` | 1 |
| 171 | 数字/字段格式 | 3 | G166 | `, 1` | `23,` | `24,` | 1 |
| 181 | 数字/字段格式 | 3 | G176 | `, 1` | `32,` | `36,` | 1 |
| 199 | 数字/字段格式 | 3 | G114 | `52,` | ` 58` | ` 15` | 1 |
| 205 | 数字/字段格式 | 3 | G195 | `5, ` | `152` | `163` | 1 |
| 215 | 数字/字段格式 | 3 | G205 | `5, ` | `163` | `166` | 2 |
| 226 | 数字/字段格式 | 3 | G221 | `, 1` | `71,` | `72,` | 1 |

数字/字段共8次，都是3-token匹配：7次a1、1次a2，无a3。提示指令复述
2次为a3/a2；缓存模板引用5次为四次a3、一次a2。十次3-token匹配中仍有
两次完全接受，且19-token匹配也会在结尾标点处分歧；不能据此宣称某个
新的最小长度阈值已验证、更不能离线改阈值冒充性能优化通过。

## d0及最长/最新规则的具体保护边界

- 位置16正在说“251 records”，历史最新匹配来自“Record 251: A ...”。
  匹配只是三个token“ 25”，候选首token“1”通过d0门，冒号和“A”仍错误。
- 位置157枚举“109, ...”，却接到prompt的“Record 109: A ...”；正确的
  末位数字“9”同样不能保护后续字段分隔符。
- 位置171的四个桶候选都匹配“, 1”三个token，续写依次“23,”、“16,”、
  “09,”、“7, ”。严格最长相同则最新，选刚写过的123；当前实际是124，
  首位“2”通过d0但第二位“3”错。完整候选位置和token IDs保留在JSON。
- 位置215同样的“5, ”把之前“...155, 163”延续搬到“...165, 166”，
  首两位“16”都匹配，第三位错。选择确定性和逐token精确匹配保证查找
  本身不造假；它不赋予短数字前缀语义预测能力。
- 有34次精确匹配被d0门排除，门确实发挥作用；不能把门扩展理解为
  后两token也经过神经draft认证。5个完全接受选择是其正收益来源。

## 可证明的边界

这是原始token+客户端推断步骤的一致性重建：全部可观测聚合计数闭合，
且同臂三次、两head复现；仍不把客户端推断改称原生GPU trace。候选在
各实际prefix的错误续接可核对，但离线证据不能计算该prefix若改回神经
草稿的反事实接受率，更不能拆分attention阈值、GPU排程等耗时贡献。
ON总73步相对OFF69步是原日志事实；“全部额外时间都由这8次短格式引起”
不是本取证已证明的结论。任何新筛选规则仍需另行冻结实现和有界验收。

产物：extract_chunks.py / chunk-inputs.json；replay_index.cpp和CPU executable；
index-replay.json（所有256个位置）；correlate_replay.py；token-burst-replay.json
（6请求、15命中表、竞争候选、身份及限制）。无新增GPU/HTTP。
