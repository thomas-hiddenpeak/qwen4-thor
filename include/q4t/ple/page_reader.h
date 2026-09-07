// PLE SSD page reader: async 4 KiB page reads from the PLE sidecar via
// io_uring, with page dedup and a registered page pool.
//
// Mirrors the gather semantics of sglang-ssd-stream's Rust PageReader:
//   - each requested row is split into 4 KiB page-aligned pieces;
//   - pieces sharing a page are deduplicated into one read;
//   - pages are read in batches (<= max_batch_pages) with io_uring waves of
//     <= queue_depth, into a 32 MiB aligned page pool (registered with
//     io_uring when possible, falling back to plain reads otherwise);
//   - pieces are scattered into the caller's output buffer (row-major,
//     row_bytes each); out-of-range rows leave their output zeroed.
#pragma once

#include <cstdint>
#include <cstddef>
#include <string>
#include <vector>

#include "q4t/status.h"

namespace q4t {
namespace ple {

constexpr size_t kPageSize = 4096;
constexpr size_t kDefaultQueueDepth = 256;
constexpr size_t kDefaultPagePoolMiB = 32;
constexpr size_t kDefaultMaxBatchPages = 4096;
// io_uring ring footprint estimate for the working-memory budget. liburing
// mmaps a single region holding the SQ array (4 B/entry), the SQEs (64
// B/entry), the CQEs (16 B/entry) and the ring bookkeeping; for
// kDefaultQueueDepth this is ~25 KiB. Counted as an estimate (it is not
// introspectable from the ring) — negligible against the 32 MiB pool.
constexpr size_t kRingBytesEstimate =
    kDefaultQueueDepth * (64 + 16 + 4) + 4096;

// Statistics for one Gather call.
struct ReadStats {
  size_t rows = 0;
  size_t valid_rows = 0;
  size_t unique_pages = 0;
  uint64_t submitted_bytes = 0;
  size_t peak_queue_depth = 0;
  size_t read_batches = 0;
  uint64_t io_ns = 0;
  uint64_t scatter_ns = 0;
  uint64_t total_ns = 0;
};

// Reads PLE rows from a backing file. Not thread-safe; one reader per PLE
// layer. The file must remain open and unmodified for the reader's lifetime.
class PlePageReader {
 public:
  // Open `path` and prepare the page pool + io_uring ring.
  // `row_bytes` is the PLE row width (160 for this model). `file_row_start`
  // is the table row offset within the file (0 for the full sidecar).
  // `row_range_begin` / `row_range_end` delimit the valid global row-id range
  // [begin, end); ids outside are skipped (output left zeroed). For TP=1 this
  // is [0, total_rows).
  static Status Create(const std::string& path, size_t row_bytes,
                        int64_t file_row_start, int64_t row_range_begin,
                        int64_t row_range_end, PlePageReader** out);

  ~PlePageReader();
  PlePageReader(const PlePageReader&) = delete;
  PlePageReader& operator=(const PlePageReader&) = delete;

  // Read `row_ids` (global row ids) into `output`, which must be at least
  // row_ids.size() * row_bytes bytes. Rows whose id falls outside
  // [row_range_begin, row_range_end) leave their output region zeroed.
  Status Gather(const int64_t* row_ids, size_t row_count, uint8_t* output,
                ReadStats* stats) const;

  // Memory footprint of the reader (for the PLE working-memory budget).
  // `pool_bytes` is the mmap'd page pool; `scratch_bytes` is the current
  // pieces + groups vectors (populated after a Gather; 0 before the first).
  size_t pool_bytes() const;
  size_t scratch_bytes() const;

 private:
  struct Piece {
    uint64_t page_id;
    uint32_t output_offset;
    uint16_t page_offset;
    uint16_t len;
  };
  struct PageGroup {
    uint64_t page_id;
    uint32_t piece_start;
    uint32_t piece_end;
  };

  struct Impl;
  explicit PlePageReader(Impl* impl);
  // Mutable: Gather is logically const (no observable state change on the
  // caller's view) but mutates the broken flag and scratch buffers.
  mutable Impl* impl_;
};

}  // namespace ple
}  // namespace q4t
