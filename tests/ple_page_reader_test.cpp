// Tests for the PLE SSD page reader.
//
// Uses a synthetic backing file (not the real 51.2 GB sidecar) to verify:
//   - correct row extraction (page-aligned slicing + scatter);
//   - page dedup (two rows sharing a page produce one read);
//   - out-of-range rows leave output zeroed;
//   - stats (unique_pages, valid_rows).
#include "q4t/ple/page_reader.h"
#include "q4t/test.h"

#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

namespace {

using q4t::ple::PlePageReader;
using q4t::ple::ReadStats;
using q4t::Status;

// Create a temp file of `nbytes` filled with a deterministic pattern:
// byte[i] = (i * 7 + 13) & 0xFF. Returns the path; caller unlinks.
std::string MakePatternFile(size_t nbytes) {
  const char* path = "/tmp/q4t_ple_reader_test.bin";
  int fd = open(path, O_CREAT | O_WRONLY | O_TRUNC, 0644);
  if (fd < 0) return "";
  std::vector<uint8_t> buf(nbytes);
  for (size_t i = 0; i < nbytes; ++i) buf[i] = static_cast<uint8_t>((i * 7 + 13) & 0xFF);
  ssize_t written = write(fd, buf.data(), nbytes);
  close(fd);
  return written == static_cast<ssize_t>(nbytes) ? path : "";
}

// Expected byte for a given file offset.
uint8_t PatternByte(uint64_t offset) {
  return static_cast<uint8_t>((offset * 7 + 13) & 0xFF);
}

}  // namespace

Q4T_TEST(ple_page_reader_basic_rows) {
  // 4 MiB file, row_bytes = 160 (like the real PLE).
  const size_t kRowBytes = 160;
  const size_t kFileSize = 4 * 1024 * 1024;
  const std::string path = MakePatternFile(kFileSize);
  Q4T_CHECK(!path.empty());

  PlePageReader* reader = nullptr;
  Status s = PlePageReader::Create(path, kRowBytes, 0, 0, kFileSize / kRowBytes,
                                    &reader);
  Q4T_CHECK(s.ok());
  Q4T_CHECK(reader != nullptr);

  // Request rows 0, 1, 100, 999999 (last valid).
  const int64_t rows[] = {0, 1, 100, static_cast<int64_t>(kFileSize / kRowBytes - 1)};
  const size_t n = sizeof(rows) / sizeof(rows[0]);
  std::vector<uint8_t> out(n * kRowBytes, 0xAB);  // sentinel
  ReadStats stats{};
  s = reader->Gather(rows, n, out.data(), &stats);
  Q4T_CHECK(s.ok());
  Q4T_CHECK(stats.valid_rows == n);

  // Verify each row matches the file pattern.
  for (size_t i = 0; i < n; ++i) {
    const uint64_t row_start = static_cast<uint64_t>(rows[i]) * kRowBytes;
    for (size_t b = 0; b < kRowBytes; ++b) {
      if (out[i * kRowBytes + b] != PatternByte(row_start + b)) {
        std::printf("  row %lld byte %zu: got %u want %u\n",
                    (long long)rows[i], b, out[i * kRowBytes + b],
                    PatternByte(row_start + b));
        Q4T_CHECK(false);
      }
    }
  }

  delete reader;
  unlink(path.c_str());
  return true;
}

Q4T_TEST(ple_page_reader_page_dedup) {
  // Two rows within the same 4 KiB page → unique_pages should be 1 (or 2 if
  // they straddle a page boundary). With row_bytes=160, rows 0 and 1 are in
  // page 0 (offsets 0..159 and 160..319, both < 4096).
  const size_t kRowBytes = 160;
  const size_t kFileSize = 64 * 1024;
  const std::string path = MakePatternFile(kFileSize);
  Q4T_CHECK(!path.empty());

  PlePageReader* reader = nullptr;
  Status s = PlePageReader::Create(path, kRowBytes, 0, 0, kFileSize / kRowBytes,
                                    &reader);
  Q4T_CHECK(s.ok());

  const int64_t rows[] = {0, 1};  // both in page 0
  std::vector<uint8_t> out(2 * kRowBytes, 0);
  ReadStats stats{};
  s = reader->Gather(rows, 2, out.data(), &stats);
  Q4T_CHECK(s.ok());
  Q4T_CHECK(stats.unique_pages == 1);  // dedup: one page read
  Q4T_CHECK(stats.valid_rows == 2);

  // Verify content.
  for (int i = 0; i < 2; ++i) {
    for (size_t b = 0; b < kRowBytes; ++b) {
      Q4T_CHECK(out[i * kRowBytes + b] == PatternByte(i * kRowBytes + b));
    }
  }

  delete reader;
  unlink(path.c_str());
  return true;
}

Q4T_TEST(ple_page_reader_out_of_range_zeroed) {
  const size_t kRowBytes = 160;
  const size_t kFileSize = 64 * 1024;
  const int64_t total_rows = kFileSize / kRowBytes;
  const std::string path = MakePatternFile(kFileSize);
  Q4T_CHECK(!path.empty());

  PlePageReader* reader = nullptr;
  Status s = PlePageReader::Create(path, kRowBytes, 0, 0, total_rows, &reader);
  Q4T_CHECK(s.ok());

  // Row 0 is valid; row 999999 is out of range.
  const int64_t rows[] = {0, 999999};
  std::vector<uint8_t> out(2 * kRowBytes, 0xFF);  // sentinel
  ReadStats stats{};
  s = reader->Gather(rows, 2, out.data(), &stats);
  Q4T_CHECK(s.ok());
  Q4T_CHECK(stats.valid_rows == 1);

  // Row 0 has data; row 1 (out of range) is zeroed.
  Q4T_CHECK(out[0] == PatternByte(0));
  for (size_t b = 0; b < kRowBytes; ++b) {
    Q4T_CHECK(out[kRowBytes + b] == 0);
  }

  delete reader;
  unlink(path.c_str());
  return true;
}

Q4T_TEST(ple_page_reader_cross_page_row) {
  // A row that straddles a page boundary: row_bytes=4096, row 0 spans
  // offsets 0..4095 (page 0), row 1 spans 4096..8191 (page 1). With
  // row_bytes=4096 each row is exactly one page. Use row_bytes=4100 so row 0
  // spans pages 0 and 1.
  const size_t kRowBytes = 4100;
  const size_t kFileSize = 256 * 1024;
  const std::string path = MakePatternFile(kFileSize);
  Q4T_CHECK(!path.empty());

  PlePageReader* reader = nullptr;
  Status s = PlePageReader::Create(path, kRowBytes, 0, 0, kFileSize / kRowBytes,
                                    &reader);
  Q4T_CHECK(s.ok());

  const int64_t rows[] = {0};
  std::vector<uint8_t> out(kRowBytes, 0);
  ReadStats stats{};
  s = reader->Gather(rows, 1, out.data(), &stats);
  Q4T_CHECK(s.ok());
  // Row 0 spans page 0 (4096 bytes) + page 1 (4 bytes) → 2 unique pages.
  Q4T_CHECK(stats.unique_pages == 2);
  for (size_t b = 0; b < kRowBytes; ++b) {
    Q4T_CHECK(out[b] == PatternByte(b));
  }

  delete reader;
  unlink(path.c_str());
  return true;
}
