# SRV-12l 加载期停止定向补验

2026-11-04。首轮驱动8790c8e1两attempt、4业务响应/2真实工具和3传输
请求后，在第二代加载时触发custody ENV_ABORT，原记录不可修改或算通过。
第一代生产guard COMPLETED，完整业务与公共136满额均在该段内；四
grammar已回收。第二代guard也COMPLETED，最终GPU/监听/原锁释放。

归因边界：驱动把自身PID传给要求PGID的inspect；驱动不是会话组长，
新controller却setsid，因此未被监控的初始根集合包含。原故障没有保存
conflict列表，不能声称直接读到了该PID；以进程关系/源码/隔离CPU复现
验证该解释，原ENV_ABORT不追改。末次NVML为空，第二代自身guard观测
无竞争，后端在预算/分配前后退出，尚未取得加载期真实连接验收。

只补该未完成项：用同一个fcff7218封装包和未改的生产controller_exec
启动单代controller，监控根为它真实的独立PGID；不切换根、不借用桥
启动第二代。取得真实supervisor运维ESTABLISHED、双监听身份和未READY
状态后，复制controller原flock FD留作清理托管，显式stop并要求5秒内
完整退出。原GPU锁dev66305/inode3294067连续持有至此项清理完成；
与首轮是两次分开的租约，不能宣称合成一次连续的成功全流程。

本项最多1次backend启动，0完整模型、0生成、0工具；20秒取得启动
观测，60秒GPU定时器，总100秒含清理和10秒无复起。正常入口的4路
健康探针上界60次；禁止为报告补验再次生成原4业务响应。高级配置
按需，既有默认/旧包/day保持；不做长验或性能接受。

入口`python3 -B tools/serve_supervisor_deadlines/finish.py`，依次cpu、
prepare、check、run。新的来源单独冻结，旧plan/CPU/业务原件只读；
离线closeout核对第一代成功段、3探针与本项实际停止，整轮失败单列。
同阶段原锁仅释放后再执行本项，不删除或替换锁文件，不启动恢复服务。
