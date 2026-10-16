#include <linux/perf_event.h>
#include <sys/syscall.h>
#include <unistd.h>
#include <cerrno>
#include <cstdio>
#include <cstdlib>
int main(int argc,char** argv) {
  if(argc!=2)return 2;
  perf_event_attr attr{};
  attr.size=sizeof(attr);attr.type=std::strtoul(argv[1],nullptr,10);
  attr.config=0x100000000ULL;
  attr.disabled=1;
  attr.read_format=PERF_FORMAT_TOTAL_TIME_ENABLED|PERF_FORMAT_TOTAL_TIME_RUNNING;
  int fd=static_cast<int>(syscall(SYS_perf_event_open,&attr,-1,0,-1,PERF_FLAG_FD_CLOEXEC));
  const int error=fd<0?errno:0;
  std::printf("{\"type\":%u,\"config\":%llu,\"pid_scope\":-1,\"cpu\":0,\"disabled\":true,\"opened\":%s,\"errno\":%d}\n",attr.type,static_cast<unsigned long long>(attr.config),fd>=0?"true":"false",error);
  if(fd>=0)close(fd);
  return 0;
}
