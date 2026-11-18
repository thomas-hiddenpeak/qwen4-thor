# Voice/Consent输入与持久登记CPU验收

21项新增与5项直接受影响旧合同全部通过，0.765秒；0模型/GPU。
必要host构建401.262秒，审查后增量构建25.615秒，均零警告。首份
25项计划未执行，审查补充未知提交负控后冻结26项；没有测试失败。
[结果](result.json)、[逐件身份](index.json)、[原始记录与源码](records.tar.gz)。

输入支持sample默认/显式类型、prompt JSON与multipart、consent创建/
改名，保留全部已知字段并拒绝重复、跨类型混用、伪声纹、非法UTF8。
音频上限10MiB；语言按[RFC 5646](https://www.rfc-editor.org/rfc/rfc5646.html)
检查完整结构与重复变体/扩展，语法通过不表示特定模型已能识别。
实际音频仍必须由有界codec及模型验证，MIME/文件名不代表真实内容。

持久层保留真实样本块、摘要、时长、独立192维身份与2048维Base条件，
公开metadata不泄漏这些私有信息。测试夹具只验证存储合同，不充当
真实音频/模型证据。条件逐值跨重启保持；注册与consent版本CAS原子
提交，竞争只成功一个，配额/取消/截止失败不发布伪对象。consent
撤销后每个新请求都失败，已取得条件的有界请求保留自己的快照。
未知提交保留公开ID及私有stage排查ID，不重放；损坏音色可由所属
客户删除，缺依赖保守保护，音频及consent依赖纳入原生保留图。

来源为已固定当前官方[音色创建](https://developers.openai.com/api/reference/resources/audio/subresources/voices/methods/create)、
[授权录音](https://developers.openai.com/api/reference/resources/audio/subresources/voice_consents/methods/create)
及本地SDK；prompt后端、真实授权短语/说话人验证、HTTP新文本合成、
参考音频ICL、听感/相似度仍继续，不能由CPU注册夹具替代。

源码审查发现历史存储读取器拒绝新voice/voice_consent种类，会影响
旧包读取含新对象的目录。当前默认未产生新对象；发布前须建立兼容
回退包，并以保留实际数据的回退验证闭合，不能只检验空目录。旧CPU
读取器独立保留用于后续对照。默认rev316及五份身份未变，全部master
仍active。Base真实合成提交047fc13b在前两次408后第三次推送成功。
