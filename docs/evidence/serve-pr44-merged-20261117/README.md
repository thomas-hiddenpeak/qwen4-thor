# PR44实际合入主线

[PR44](https://github.com/thomas-hiddenpeak/qwen4-thor/pull/44)已合并，
main提交`8de16675df10ecefa818a682d745539063b84cfc`；接受head为
`5784729c4bc79fd1d210299ca10294a5071b0ca4`。两个host-contracts
检查均SUCCESS，实际检查/merge前后回执保留，不以关PR替代合入。

此PR只交付已接受原生ASR及其接入、恢复、发行和对应CI修复。
后续alignment/VAD未混入该PR。默认rev276身份仍相同，0新增GPU。
main已fetch并合回当前工作分支，未改已冻结运行时和业务证据。

分支检查：当前工作分支比main有11个独有提交，含正在推进的
对齐与VAD实现、验收和master文档，保留并继续使用，不删除远端。
PR44修复worktree本来为detached且干净；该路径仍保留本机CI修复
现场，不把分支整理等同于证据目录清理。全部master仍active。
