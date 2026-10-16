#include <cstdio>
#include <ctime>
#include <thread>
#include <chrono>
#include <cstdint>
uint64_t Cpu() {
  timespec t{}; clock_gettime(CLOCK_THREAD_CPUTIME_ID, &t);
  return uint64_t(t.tv_sec) * 1000000000 + t.tv_nsec;
}
void Report(int n) {
  std::fprintf(stderr, "[layer-timing] T=%d embed=0.00ms lin=1.00ms full=2.00ms head=0.00ms total=3.00ms top5=", n);
  std::fprintf(stderr, " L%d(%.2f)\n", 1, 3.0);
}
void Work() {
  Report(8192);
  const uint64_t start = Cpu();
  while (Cpu() - start < 100000000) {}
  Report(8192);
  std::this_thread::sleep_for(std::chrono::milliseconds(100));
  Report(8181);
  Report(1);
}
int main() {
  std::fprintf(stderr, "ordinary format %s %d %.1f\n", "intact", 7, 1.5);
  Work(); std::thread t(Work); t.join();
}
