set pagination off
set confirm off
set disable-randomization off
set breakpoint pending on
break cuInit
commands
silent
printf "CU_INIT_BEFORE_MAIN\n"
bt 4
continue
end
tbreak main
run
printf "STOPPED_AT_MAIN_BEFORE_FIRST_CALL\n"
info sharedlibrary libcuda
python
import gdb,json
pid=gdb.selected_inferior().pid
with open('/proc/%d/maps'%pid) as f:
    paths=sorted(set(line.split()[-1] for line in f if 'libcuda' in line))
print('CUDA_MAPPING_PATHS='+json.dumps(paths))
end
kill
quit
