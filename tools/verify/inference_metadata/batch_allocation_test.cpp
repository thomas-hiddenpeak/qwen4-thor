// Reuse the existing delayed-reader CUDA mocks, but run only missing cases.
#define main BatchOriginalMain
#include "batch_host_test.cpp"
#undef main

#include <new>

namespace {
bool fail_new = false;
bool fail_array = false;
}  // namespace
extern "C" {
void* __real__Znwm(size_t size);
void* __real__Znam(size_t size);
void* __wrap__Znwm(size_t size) {
  if (fail_new) {
    fail_new = false;
    throw std::bad_alloc();
  }
  return __real__Znwm(size);
}
void* __wrap__Znam(size_t size) {
  if (fail_array) {
    fail_array = false;
    throw std::bad_alloc();
  }
  return __real__Znam(size);
}
}
int main() {
  using q4t::model::MetadataUploadBatch;
  try {
    Reset();
    int source[] = {61, 73}, first = 0, second[2]{};
    {
      MetadataUploadBatch batch;
      Require(batch.Begin(nullptr).ok());
      Require(batch.Copy(&first, source, sizeof(int)).ok());
      fail_new = true;
      const auto status = batch.Copy(second, source, sizeof(int));
      Require(!fail_new && !status.ok());
      Require(copies.empty() && copy_count == 1 && first == 61);
      Require(!batch.Begin(nullptr).ok());
    }
    Reset();
    {
      MetadataUploadBatch batch;
      Require(batch.Begin(nullptr).ok());
      Require(batch.Copy(&first, source, sizeof(int)).ok());
      Require(batch.Copy(second, source, sizeof(int)).ok());
      Require(batch.Finish().ok());
      Require(batch.Begin(nullptr).ok());
      source[0] = 83;
      Require(batch.Copy(&first, source, sizeof(int)).ok());
      fail_array = true;
      const auto status = batch.Copy(second, source, sizeof(source));
      Require(!fail_array && !status.ok());
      Require(copies.empty() && copy_count == 3 && first == 83);
      Require(!batch.Begin(nullptr).ok());
    }
    Reset();
    std::cout << "PASS 2 host allocation failures with prior uploads\n";
  } catch (const std::exception& error) {
    std::cerr << "FAIL " << error.what() << '\n';
    return 1;
  }
}
