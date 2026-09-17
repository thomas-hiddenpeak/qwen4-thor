// PLE SSD page reader implementation (io_uring).
#include "q4t/ple/page_reader.h"

#include <fcntl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

#include <algorithm>
#include <cerrno>
#include <chrono>
#include <cstdlib>
#include <cstring>
#include <string>

#ifdef Q4T_HAS_LIBURING
#include <liburing.h>
#endif

namespace q4t {
namespace ple {

namespace {

// Max re-submit rounds per wave before failing closed (short-read fill +
// transient-error retry). 8 tolerates brief EAGAIN/EINTR bursts.
constexpr int kMaxReadRetries = 8;

uint64_t NowNs() {
  using namespace std::chrono;
  return static_cast<uint64_t>(
      duration_cast<nanoseconds>(steady_clock::now().time_since_epoch()).count());
}

}  // namespace

struct PlePageReader::Impl {
  int fd = -1;
  uint64_t file_len = 0;
  size_t row_bytes = 0;
  int64_t file_row_start = 0;
  int64_t row_range_begin = 0;
  int64_t row_range_end = 0;

  uint8_t* pool = nullptr;
  size_t pool_len = 0;
  bool registered_buffers = false;
  bool broken = false;
  std::string broken_reason;
  mutable int fault_inject_reads = 0;  // test-only (Q4T_PLE_FAULT_INJECT)

#ifdef Q4T_HAS_LIBURING
  mutable io_uring ring;
  bool ring_initialized = false;
#endif

  mutable std::vector<Piece> pieces;
  mutable std::vector<PageGroup> groups;
};

PlePageReader::PlePageReader(Impl* impl) : impl_(impl) {}

PlePageReader::~PlePageReader() {
  if (!impl_) return;
  if (impl_->registered_buffers) {
#ifdef Q4T_HAS_LIBURING
    io_uring_unregister_buffers(&impl_->ring);
#endif
  }
#ifdef Q4T_HAS_LIBURING
  if (impl_->ring_initialized) io_uring_queue_exit(&impl_->ring);
#endif
  if (impl_->pool) {
    munmap(impl_->pool, impl_->pool_len);
    impl_->pool = nullptr;
  }
  if (impl_->fd >= 0) close(impl_->fd);
  delete impl_;
  impl_ = nullptr;
}

Status PlePageReader::Create(const std::string& path, size_t row_bytes,
                              int64_t file_row_start, int64_t row_range_begin,
                              int64_t row_range_end, PlePageReader** out) {
  *out = nullptr;
  if (row_bytes == 0) return Status::Fail("row_bytes must be positive");
  if (row_range_begin < file_row_start || row_range_end <= row_range_begin) {
    return Status::Fail("invalid row range");
  }

  Impl* impl = new Impl();
  impl->row_bytes = row_bytes;
  impl->file_row_start = file_row_start;
  impl->row_range_begin = row_range_begin;
  impl->row_range_end = row_range_end;

  impl->fd = open(path.c_str(), O_RDONLY);
  if (impl->fd < 0) {
    std::string msg = "open(" + path + ") failed: ";
    msg += std::strerror(errno);
    delete impl;
    return Status::Fail(msg);
  }
  struct stat st;
  if (fstat(impl->fd, &st) != 0) {
    close(impl->fd);
    delete impl;
    return Status::Fail("fstat failed");
  }
  impl->file_len = static_cast<uint64_t>(st.st_size);
  // Hint random access; the PLE table is read as scattered 4 KiB pages.
  posix_fadvise(impl->fd, 0, 0, POSIX_FADV_RANDOM);

  const size_t pool_len = kDefaultPagePoolMiB * 1024 * 1024;
  void* raw = mmap(nullptr, pool_len, PROT_READ | PROT_WRITE,
                   MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
  if (raw == MAP_FAILED) {
    close(impl->fd);
    delete impl;
    return Status::Fail("mmap page pool failed");
  }
  madvise(raw, pool_len, MADV_DONTDUMP);
  impl->pool = static_cast<uint8_t*>(raw);
  impl->pool_len = pool_len;

#ifdef Q4T_HAS_LIBURING
  if (io_uring_queue_init(kDefaultQueueDepth, &impl->ring, 0) != 0) {
    munmap(raw, pool_len);
    close(impl->fd);
    delete impl;
    return Status::Fail("io_uring_queue_init failed");
  }
  impl->ring_initialized = true;

  // Register the page pool as a single fixed buffer (index 0). On failure
  // (ENOMEM/EPERM/EAGAIN) fall back to plain reads.
  struct iovec iov;
  iov.iov_base = impl->pool;
  iov.iov_len = pool_len;
  int rc = io_uring_register_buffers(&impl->ring, &iov, 1);
  if (rc == 0) {
    impl->registered_buffers = true;
  } else if (rc != -ENOMEM && rc != -EPERM && rc != -EAGAIN) {
    io_uring_queue_exit(&impl->ring);
    impl->ring_initialized = false;
    munmap(raw, pool_len);
    close(impl->fd);
    delete impl;
    return Status::Fail("io_uring_register buffers failed");
  }
#else
  return Status::Fail("built without liburing; PLE reader unavailable");
#endif

  *out = new PlePageReader(impl);
  return Status();
}

size_t PlePageReader::pool_bytes() const {
  return impl_->pool_len;
}
size_t PlePageReader::scratch_bytes() const {
  return impl_->pieces.size() * sizeof(Piece) +
         impl_->groups.size() * sizeof(PageGroup);
}
Status PlePageReader::Gather(const int64_t* row_ids, size_t row_count,
                              uint8_t* output, ReadStats* stats) const {
  Impl* const impl = impl_;
  if (impl->broken) {
    return Status::Fail("PLE page reader unusable after I/O failure: " +
                        impl->broken_reason);
  }
  // Test-only: fake the first N successful reads as transient (EAGAIN) to
  // exercise the retry / short-read recovery path.
  if (const char* fi = std::getenv("Q4T_PLE_FAULT_INJECT"))
    impl->fault_inject_reads = std::atoi(fi);
  const uint64_t started = NowNs();
  const uint64_t expected_len =
      static_cast<uint64_t>(row_count) * impl->row_bytes;
  if (expected_len > static_cast<uint64_t>(UINT32_MAX)) {
    return Status::Fail("one gather cannot exceed 4 GiB");
  }
  std::fill_n(output, static_cast<size_t>(expected_len), 0);
  impl->pieces.clear();
  impl->groups.clear();

  size_t valid_rows = 0;
  for (size_t row_index = 0; row_index < row_count; ++row_index) {
    const int64_t global_id = row_ids[row_index];
    if (global_id < impl->row_range_begin || global_id >= impl->row_range_end) {
      continue;
    }
    ++valid_rows;
    const uint64_t file_row =
        static_cast<uint64_t>(global_id - impl->file_row_start);
    const uint64_t row_start = file_row * impl->row_bytes;
    const uint64_t row_end = row_start + impl->row_bytes;
    if (row_end > impl->file_len) {
      return Status::Fail("row extends past the PLE table");
    }
    const uint32_t output_row_start =
        static_cast<uint32_t>(row_index * impl->row_bytes);
    uint64_t file_offset = row_start;
    size_t row_offset = 0;
    while (row_offset < impl->row_bytes) {
      const size_t page_offset = file_offset & (kPageSize - 1);
      const size_t piece_len =
          std::min(kPageSize - page_offset, impl->row_bytes - row_offset);
      impl->pieces.push_back(Piece{
          static_cast<uint64_t>(file_offset / kPageSize),
          output_row_start + static_cast<uint32_t>(row_offset),
          static_cast<uint16_t>(page_offset),
          static_cast<uint16_t>(piece_len)});
      file_offset += piece_len;
      row_offset += piece_len;
    }
  }

  std::sort(impl->pieces.begin(), impl->pieces.end(),
            [](const Piece& a, const Piece& b) { return a.page_id < b.page_id; });
  size_t piece_start = 0;
  while (piece_start < impl->pieces.size()) {
    const uint64_t page_id = impl->pieces[piece_start].page_id;
    size_t piece_end = piece_start + 1;
    while (piece_end < impl->pieces.size() &&
           impl->pieces[piece_end].page_id == page_id) {
      ++piece_end;
    }
    impl->groups.push_back(PageGroup{page_id,
                                     static_cast<uint32_t>(piece_start),
                                     static_cast<uint32_t>(piece_end)});
    piece_start = piece_end;
  }

  ReadStats result;
  result.rows = row_count;
  result.valid_rows = valid_rows;
  result.unique_pages = impl->groups.size();
  result.submitted_bytes =
      static_cast<uint64_t>(impl->groups.size()) * kPageSize;

  uint64_t io_ns = 0;
  uint64_t scatter_ns = 0;

  for (size_t batch_start = 0; batch_start < impl->groups.size();
       batch_start += kDefaultMaxBatchPages) {
    const size_t batch_end = std::min(
        batch_start + kDefaultMaxBatchPages, impl->groups.size());

    for (size_t wave_start = batch_start; wave_start < batch_end;
         wave_start += kDefaultQueueDepth) {
      const size_t wave_end =
          std::min(wave_start + kDefaultQueueDepth, batch_end);
      const size_t wave_len = wave_end - wave_start;
      result.peak_queue_depth = std::max(result.peak_queue_depth, wave_len);
      ++result.read_batches;

#ifdef Q4T_HAS_LIBURING
      const uint64_t io_started = NowNs();
      // Per-page bytes read so far in this wave, for short-read fill +
      // transient-error retry (ds4-style "exact recovery"). A page's valid byte
      // count is min(kPageSize, file_len - page_start): the last page of a
      // non-page-multiple file is legitimately short at EOF, not a failure.
      // wave_len <= kDefaultQueueDepth. When every read returns its full valid
      // length on the first attempt (the normal path) this reduces to a single
      // submit+reap, so successful reads are byte-identical to before.
      uint32_t page_done[kDefaultQueueDepth] = {0};
      auto valid_bytes = [&](size_t slot) -> uint32_t {
        const uint64_t page_start = impl->groups[slot].page_id * kPageSize;
        return static_cast<uint32_t>(
            std::min<uint64_t>(kPageSize, impl->file_len - page_start));
      };
      for (int attempt = 0;; ++attempt) {
        unsigned inflight = 0;
        for (size_t slot = wave_start; slot < wave_end; ++slot) {
          const uint32_t want = valid_bytes(slot);
          const uint32_t have = page_done[slot - wave_start];
          if (have >= want) continue;
          uint8_t* dst = impl->pool + (slot - batch_start) * kPageSize + have;
          const uint64_t offset = impl->groups[slot].page_id * kPageSize + have;
          io_uring_sqe* sqe = io_uring_get_sqe(&impl->ring);
          if (!sqe) return Status::Fail("io_uring submission queue is full");
          if (impl->registered_buffers) {
            io_uring_prep_read_fixed(sqe, impl->fd, dst, want - have, offset, 0);
          } else {
            io_uring_prep_read(sqe, impl->fd, dst, want - have, offset);
          }
          sqe->user_data = slot;
          ++inflight;
        }
        if (inflight == 0) break;  // every page has its full valid length
        if (attempt > kMaxReadRetries) {
          impl->broken = true;
          impl->broken_reason = "PLE page read unfinished after retries";
          return Status::Fail(impl->broken_reason);
        }
        if (io_uring_submit_and_wait(&impl->ring, inflight) < 0) {
          impl->broken = true;
          impl->broken_reason = "io_uring_submit_and_wait failed";
          return Status::Fail(impl->broken_reason);
        }
        for (unsigned reaped = 0; reaped < inflight; ++reaped) {
          io_uring_cqe* cqe = nullptr;
          if (io_uring_peek_cqe(&impl->ring, &cqe) != 0) break;
          const size_t slot = static_cast<size_t>(cqe->user_data);
          const int res = cqe->res;
          io_uring_cqe_seen(&impl->ring, cqe);
          if (slot < wave_start || slot >= wave_end) {
            impl->broken = true;
            impl->broken_reason = "io_uring returned an unknown page slot";
            return Status::Fail(impl->broken_reason);
          }
          if (res > 0 && impl->fault_inject_reads > 0) {
            // Fake a short read (advance 1 byte) so the next attempt re-reads
            // [have, want): exercises both the retry loop and the offset fill.
            --impl->fault_inject_reads;
            page_done[slot - wave_start] += 1;
          } else if (res > 0) {
            page_done[slot - wave_start] += static_cast<uint32_t>(res);
          } else if (res == -EAGAIN || res == -EINTR || res == -EBUSY ||
                     res == -ECANCELED) {
            // Transient: leave page_done unchanged so this page is re-read.
          } else {
            // res == 0 (unexpected EOF before the valid end) or a persistent
            // errno: fail closed rather than scatter stale page bytes.
            impl->broken = true;
            impl->broken_reason =
                "io_uring read failed (res=" + std::to_string(res) + ")";
            return Status::Fail(impl->broken_reason);
          }
        }
      }
      io_ns += NowNs() - io_started;
#else
      return Status::Fail("built without liburing");
#endif
    }

    const uint64_t scatter_started = NowNs();
    for (size_t group_index = batch_start; group_index < batch_end;
         ++group_index) {
      const PageGroup& group = impl->groups[group_index];
      const uint8_t* page =
          impl->pool + (group_index - batch_start) * kPageSize;
      for (uint32_t p = group.piece_start; p < group.piece_end; ++p) {
        const Piece& piece = impl->pieces[p];
        std::memcpy(output + piece.output_offset, page + piece.page_offset,
                    piece.len);
      }
    }
    scatter_ns += NowNs() - scatter_started;
  }

  result.io_ns = io_ns;
  result.scatter_ns = scatter_ns;
  result.total_ns = NowNs() - started;
  if (stats) *stats = result;
  return Status();
}

}  // namespace ple
}  // namespace q4t
