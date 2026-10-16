import re
def validate(text,mode):
 assert '[scratch-layout] INVALID' not in text
 reports=[l for l in text.splitlines() if l.startswith('[scratch-layout]')]
 assert len(reports)==2,reports
 m=re.fullmatch(r'\[scratch-layout\] mode=(control|fixed) arena=(0x[0-9a-f]+) aligned=(0x[0-9a-f]+) bytes=(\d+)',reports[0]);assert m
 actual,arena,aligned,size=m.groups();arena=int(arena,16);aligned=int(aligned,16);size=int(size)
 assert actual==mode and size==562036736
 assert aligned%(2*1024*1024)==0 and arena<=aligned<arena+2*1024*1024
 assert reports[1]=='[scratch-layout] complete allocations=6600 frees=6600'
 return {'passed':True,'mode':mode,'arena':hex(arena),'aligned':hex(aligned),'bytes':size,'allocations':6600,'frees':6600}
