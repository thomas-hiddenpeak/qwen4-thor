# PR38实际合入main

最终head `8f55b64351cf85f0525b63700918addc7febe5b7` 两组公共host检查
全部通过，随后按该head实际合并。合并commit
`0e4f5ac2cd20fec9f273b3b03251c7020f61f233`。

媒体状态缓存、视频prefill修复、真实业务、rev214可回退交付及
独立day交接进入main。原CI失败保留，针对性的正常预算/过期夹具
修复没有删除断言或跳过检查。Stop的rev224交付另在下一PR，
网页搜索属于后续开发，全部master继续。
