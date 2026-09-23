# 完整PLE卷积边界采集

模板复制至prepared新目录并去掉.in，更新输出目录后重跑，不覆盖
冻结证据。本次目录ple-full-conv-20260924。

observer.cpp使用g++-14 C++23 -O2 -shared -fPIC -ffp-contract=off
-Wall -Wextra、CUDA include和dl构建observer.so，日志build.log
须为空。第一项测试为run.py的tools/evalscope HTTP关闭/开启/关闭
观测对照，每组一个原4K请求，MTP关闭。原600440错答保持并非
质量通过；完整请求、输出、usage、stop和服务退出均核对。

采集完整4096位置卷积输入/gated/trunk/output及首个decode，
实际权重与历史状态保存。对照完成后运行analyze.py：直接匹配
checkpoint权重、全4K HC输入/输出交接、历史更新及此前16份
末尾/小张量证据，并生成完整manifest。本阶段没有卷积算术参考；
给定实际输入的后续算术核对仍需另做，不能把边界一致视为正确。

采集预算340MB，参考600MB，预留20GiB磁盘。模型和生产二进制
不改，原错题及完整QSA/PLE上游仍待确认。
