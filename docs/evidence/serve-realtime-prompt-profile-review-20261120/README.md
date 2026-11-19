# Prompt主轻首轮失败与有界定位

原资格组FAIL：1模型、实际3 Chat+2 Realtime工具+3 Prompt=8次
外部生成（驱动预留计数9不是实际执行数）、3注册HTTP、2WSS，
64.708秒。Chat schema/真实工具闭环、Realtime真实工具闭环及
Prompt固定v1=42/单轮v2=377通过；删除后的固定v1回答错误，
并以incomplete refusal frame / response_parse_failed终止。
CSV、旧Responses模板和轻配置尚未执行，整组不记通过。

实际会话设置删除前后完全相同；原生重建两份提示逐字节一致，
均174 tokens，temperature=1/top_p=1/no seed。CPU两项0.383秒，
未启动模型；早期导入路径准备错误保留。

单次固定诊断1模型/3HTTP/45.796秒：seed0、greedy、相同174-token
提示的原始Completion均返回42。不改变默认采样，不据此宣布唯一
根因，更不把原错误及畸形拒答记为正确。两组共107次NVML正常，
所有自有进程、端口与锁释放。默认rev356不变。

下一步只续未完成Prompt组件/Responses模板及轻配置；原主配置
Chat/Realtime工具实跑可按相同候选和严格来源复用，原失败不改写。
所有原始输出、实际失败终态和诊断命令均在raw.tar.gz，index.json
提供路径、长度和SHA256；不含私钥、入口凭据或二进制。
