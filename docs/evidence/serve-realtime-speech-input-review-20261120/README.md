# Realtime语音内容失败：输入对照与有界补验准备

2026-11-20。从首轮实际SDK事件重建工具后、下一条PCM响应的完整历史，
各比对audio/text/native执行及新user反例，共6个CPU输入。探针6.530秒
零警告构建；真实只读tokenizer计数、生成JSON与渲染提示逐字节一致：
音频选项及原生TTS接入没有改变主模型提示。原工具后末项是tool，下一
格式场景末项是assistant；只有新增用户项才改变当前实际询问。

原错误仍是原错误，不将正常新用户轮次当作旧输入的通过。补验驱动
显式保留原失败，固定同两条旧提示做temperature0/seed0的HTTP诊断；
该对照不改变Realtime默认temperature1/无显式seed。新的实际对话用
明确user项请求工具版本和三种格式的目标句，不改变底层模型/模板。
6项CPU反例和JS语法通过0.346秒，旧协议/配额/截断证据按身份复用。

新增GPU为0。补验固定最多1模型、1WSS、5次response.create、2条
固定种子Completions、3条不带原文ASR、600秒。首轮81.532秒/1模型
原租约独立保留，不能冒充与补验共享的连续租约。候选cfae4d60和
默认rev366均未改；真实内容仍待验，恢复、交付与全部master继续。

[离线结论](acceptance.json)、[索引](index.json)、[原始记录](records.tar.gz)。
