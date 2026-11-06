// Reuse the actual full-tower numerical test and unchanged 0.05 limits.
// Direct entry never skips absent hardware/model/reference fixtures.
#include "../../tests/vision_forward_test.cpp"

int main(int argc, char** argv) {
  if (argc != 3 || !CudaAvailable() || !FileExists(kIndex) ||
      !FileExists(argv[1]))
    return 2;
  kRefJson = argv[1];
  output_prefix = argv[2];
  return q4t_test_fn_vision_forward() ? 0 : 1;
}
