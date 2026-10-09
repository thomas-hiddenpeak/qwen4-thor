// Read-only MTP verify-logits dump for T1/T4 numerical divergence analysis.
//
// Enabled only when Q4T_MTP_LOGITS_DUMP=<dir> is set at model load; off by
// default. It never changes numerics: it performs a synchronous D2H copy of
// each verify row's logits (after the row's argmax readback, before the
// buffer is reused) and appends a record to <dir>/mtp-verify-logits.bin.
//
// Record format (little-endian), one per verify row:
//   uint32 magic        0x51345444 ("Q4TD")
//   int32  position     step base position (first verified token's position)
//   int32  step         0-based speculative step within the current request
//                       (resets when the base position regresses)
//   int32  row          row within the step (0 = bonus, 1..k = drafts)
//   int32  verified     token fed to the target at this row
//   int32  argmax       target prediction (ArgmaxBf16Rows) for this row
//   uint16 logits[vocab] BF16, exactly as produced by the verify forward
//
// The T4 (fast multi) path records all k+1 rows of its single packed
// forward; the strict T1 server path (MtpSequentialVerify) records
// each executed row (0..accepted_count-1) as its per-row B=1 forward
// completes; the CLI batched T1 path (MtpSpeculativeStep) records all
// k+1 rows of its batched verify. Rows align across paths by absolute
// position (position + row).
#pragma once

#include <cuda_runtime.h>

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <vector>

namespace q4t::mtp {

class MtpLogitsDump {
 public:
  explicit MtpLogitsDump(int vocab) {
    vocab_ = vocab;
    const char* dir = std::getenv("Q4T_MTP_LOGITS_DUMP");
    if (dir == nullptr || dir[0] == '\0' || vocab <= 0) return;
    const std::string path = std::string(dir) + "/mtp-verify-logits.bin";
    file_ = std::fopen(path.c_str(), "ab");
    if (file_ == nullptr) return;
    host_logits_.resize(static_cast<size_t>(vocab));
    enabled_ = true;
  }
  ~MtpLogitsDump() {
    if (file_ != nullptr) std::fclose(file_);
  }
  MtpLogitsDump(const MtpLogitsDump&) = delete;
  MtpLogitsDump& operator=(const MtpLogitsDump&) = delete;

  bool enabled() const { return enabled_; }

  // Append one verify row. `logits` must stay valid until this call returns
  // (the copy is synchronous). Row 0 of a step advances the step counter;
  // a base-position regression starts a new request at step 0.
  void Record(int step_base_position, int row, int32_t verified,
              int32_t argmax, const uint16_t* logits, cudaStream_t stream) {
    if (!enabled_ || row < 0 || logits == nullptr) return;
    if (row == 0) {
      if (step_base_position < last_base_) step_ = 0;
      last_base_ = step_base_position;
      ++step_;
    }
    if (cudaMemcpyAsync(host_logits_.data(), logits,
                        static_cast<size_t>(vocab_) * sizeof(uint16_t),
                        cudaMemcpyDeviceToHost, stream) != cudaSuccess ||
        cudaStreamSynchronize(stream) != cudaSuccess)
      return;
    struct RecordHeader {
      uint32_t magic;
      int32_t position;
      int32_t step;
      int32_t row;
      int32_t verified;
      int32_t argmax;
    };
    const RecordHeader header{0x51345444u, step_base_position, step_ - 1, row,
                              verified, argmax};
    std::fwrite(&header, sizeof(header), 1, file_);
    std::fwrite(host_logits_.data(), sizeof(uint16_t),
                static_cast<size_t>(vocab_), file_);
    std::fflush(file_);
  }

 private:
  int vocab_ = 0;
  bool enabled_ = false;
  std::FILE* file_ = nullptr;
  int last_base_ = -1;
  int step_ = 0;
  std::vector<uint16_t> host_logits_;
};

}  // namespace q4t::mtp
