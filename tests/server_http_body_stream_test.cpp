#include "q4t/server/http_request.h"

#include <algorithm>
#include <cstring>
#include <stdexcept>
#include <thread>

#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
constexpr size_t kWindow = 65536;
const std::string kHead = "POST /v1/files HTTP/1.1\r\nHost: local\r\n";
struct Reader {
  std::string wire;
  size_t offset = 0, step = 4096;
  HttpRequestHeaders headers;
  HttpRequestState state;
  HttpBodyStream stream;
  std::string method, path, body;
  int status = 0, interim = 0, timeout = 1000;
  size_t calls = 0, accepted = 0, largest = 0;
  Reader() {
    stream.max_bytes = 32ULL << 20;
    stream.reservation_bytes = kWindow;
    stream.consume = [&](std::string_view bytes, auto deadline) {
      ++calls;
      accepted += bytes.size();
      largest = std::max(largest, bytes.size());
      return deadline > Clock::now();
    };
  }
  bool Run(HttpBodyLease* lease = nullptr,
           const std::function<bool()>& admission = {}) {
    return ReadHttpRequest(
        -1, &method, &path, &body, timeout, lease, &status, &headers,
        [&] {
          headers.body_limit = stream.max_bytes;
          return !admission || admission();
        },
        [&](void* bytes, size_t count, auto) -> ssize_t {
          count = std::min({count, step, wire.size() - offset});
          std::memcpy(bytes, wire.data() + offset, count);
          offset += count;
          return count;
        },
        &state,
        [&](auto) {
          ++interim;
          return true;
        },
        &stream);
  }
};
}  // namespace

Q4T_TEST(http_body_stream_large_payload_uses_fixed_window_and_no_body_copy) {
  // Generate network bytes on demand; neither the source nor the sink holds
  // the complete 17 MiB body. Length and chunked requests share the reader.
  constexpr size_t total = (17ULL << 20) + 13;
  for (bool chunked : {false, true}) {
    const auto head =
        kHead + (chunked
                     ? "Transfer-Encoding: chunked\r\n\r\n110000d\r\n"
                     : "Content-Length: " + std::to_string(total) + "\r\n\r\n");
    const std::string tail = chunked ? "\r\n0\r\n\r\n" : "";
    size_t prefix = 0, produced = 0, suffix = 0, consumed = 0, largest = 0;
    bool exact = true;
    HttpBodyBudget budget(kWindow);
    {
      HttpBodyLease lease(budget);
      HttpBodyStream stream;
      stream.max_bytes = total;
      stream.reservation_bytes = kWindow;
      stream.consume = [&](std::string_view bytes, auto) {
        largest = std::max(largest, bytes.size());
        for (const unsigned char byte : bytes) {
          exact &= byte == consumed % 256;
          ++consumed;
        }
        exact &= budget.Used() == kWindow;
        return true;
      };
      HttpRequestHeaders headers;
      std::string method, path, body;
      int status = 0;
      Q4T_CHECK(ReadHttpRequest(
          -1, &method, &path, &body, 3000, &lease, &status, &headers,
          [&] {
            headers.body_limit = total;
            return true;
          },
          [&](void* data, size_t size, auto) -> ssize_t {
            if (prefix < head.size()) {
              size = std::min(size, head.size() - prefix);
              std::memcpy(data, head.data() + prefix, size);
              prefix += size;
            } else if (produced < total) {
              size = std::min(size, total - produced);
              auto* bytes = static_cast<unsigned char*>(data);
              for (size_t i = 0; i < size; ++i) bytes[i] = produced++ % 256;
            } else {
              size = std::min(size, tail.size() - suffix);
              std::memcpy(data, tail.data() + suffix, size);
              suffix += size;
            }
            return size;
          },
          nullptr, {}, &stream));
      Q4T_CHECK(exact && consumed == total && largest <= 8192 &&
                headers.content_length == total && body.empty() &&
                budget.Used() == kWindow);
    }
    Q4T_CHECK(budget.Used() == 0);
  }
  return true;
}

Q4T_TEST(http_body_stream_admission_capacity_and_continue_order) {
  for (int failure = 0; failure < 5; ++failure) {
    Reader reader;
    reader.wire =
        kHead + "Expect: 100-continue\r\nContent-Length: 3\r\n\r\nabc";
    HttpBodyBudget budget(failure == 1 ? kWindow - 1 : kWindow);
    HttpBodyLease lease(budget);
    if (failure == 2) reader.stream.reservation_bytes = 1;
    if (failure == 3) reader.stream.max_bytes = 2;
    const bool ok =
        reader.Run(&lease, [&] { return failure != 0 && budget.Used() == 0; });
    Q4T_CHECK(ok == (failure == 4));
    Q4T_CHECK(reader.interim == (failure == 4 ? 1 : 0) &&
              reader.calls == (failure == 4 ? 1 : 0));
    if (failure < 4) Q4T_CHECK(budget.Used() == 0);
  }
  return true;
}

Q4T_TEST(http_body_stream_total_and_metadata_limits_bound_fragmented_input) {
  Reader reader;
  reader.wire = kHead +
                "Transfer-Encoding: chunked\r\n\r\n"
                "2\r\nab\r\n2\r\ncd\r\n0\r\n\r\n";
  reader.stream.max_bytes = 3;
  HttpBodyBudget budget(kWindow);
  HttpBodyLease lease(budget);
  Q4T_CHECK(!reader.Run(&lease) && reader.status == 413 &&
            reader.accepted == 2 && reader.body.empty() &&
            budget.Used() == kWindow);
  for (bool larger_metadata : {false, true}) {
    Reader many;
    many.wire = kHead + "Transfer-Encoding: chunked\r\n\r\n";
    for (int i = 0; i < 9; ++i)
      many.wire += "1;x=" + std::string(8000, 'x') + "\r\nx\r\n";
    many.wire += "0\r\n\r\n";
    if (larger_metadata) many.stream.max_metadata_bytes = 128 * 1024;
    Q4T_CHECK(many.Run() == larger_metadata && many.body.empty());
    if (larger_metadata) Q4T_CHECK(many.accepted == 9);
  }
  return true;
}

Q4T_TEST(http_body_stream_sink_failure_and_exception_stop_further_reads) {
  for (bool throws : {false, true}) {
    Reader reader;
    reader.wire =
        kHead + "Content-Length: 20000\r\n\r\n" + std::string(20000, 'a');
    reader.stream.failure_status = 429;
    reader.stream.failure_code = "file_quota_exceeded";
    reader.stream.consume = [&](std::string_view, auto) {
      ++reader.calls;
      if (throws) throw std::runtime_error("storage failure");
      return false;
    };
    Q4T_CHECK(!reader.Run() && reader.calls == 1 &&
              reader.offset < reader.wire.size() && reader.body.empty());
    Q4T_CHECK(reader.headers.failure == HttpReceiveFailure::kBodyConsumer &&
              reader.status == (throws ? 503 : 429) &&
              reader.headers.error_code ==
                  (throws ? "body_consumer_exception" : "file_quota_exceeded"));
  }
  return true;
}

Q4T_TEST(http_body_stream_all_payload_does_not_authorize_truncated_message) {
  for (const auto* ending :
       {"", "\r\n", "\r\n0\r\n", "\r\n0\r\nAuthorization: injected\r\n\r\n"}) {
    Reader reader;
    reader.wire = kHead + "Transfer-Encoding: chunked\r\n\r\n3\r\nabc" + ending;
    reader.step = 1;
    Q4T_CHECK(!reader.Run() && reader.accepted == 3 && reader.body.empty() &&
              reader.state.pending.empty());
  }
  Reader short_body;
  short_body.wire = kHead + "Content-Length: 4\r\n\r\nabc";
  Q4T_CHECK(!short_body.Run() && short_body.accepted == 3 &&
            short_body.headers.failure == HttpReceiveFailure::kBodyEof);
  return true;
}

Q4T_TEST(http_body_stream_pipeline_and_binary_bytes_are_exact) {
  const std::string bytes("a\0\xff\r\nb", 6);
  const std::string next = "GET /v1/models HTTP/1.1\r\nHost: local\r\n\r\n";
  for (bool chunked : {false, true}) {
    Reader reader;
    reader.wire = kHead +
                  (chunked ? "Transfer-Encoding: chunked\r\n\r\n6\r\n"
                           : "Content-Length: 6\r\n\r\n") +
                  bytes + (chunked ? "\r\n0\r\n\r\n" : "") + next;
    std::string received;
    reader.stream.consume = [&](std::string_view chunk, auto) {
      received.append(chunk);
      return true;
    };
    Q4T_CHECK(reader.Run() && received == bytes &&
              reader.state.pending == next);
    reader.stream.consume = {};
    Q4T_CHECK(reader.Run() && reader.method == "GET" &&
              reader.path == "/v1/models" && reader.body.empty());
  }
  return true;
}

Q4T_TEST(http_body_stream_sink_time_counts_toward_absolute_deadline) {
  Reader reader;
  reader.timeout = 10;
  reader.wire = kHead + "Content-Length: 3\r\n\r\nabc";
  reader.stream.consume = [&](std::string_view, auto) {
    ++reader.calls;
    std::this_thread::sleep_for(25ms);
    return true;
  };
  Q4T_CHECK(!reader.Run() && reader.calls == 1 && reader.status == 408 &&
            reader.headers.failure == HttpReceiveFailure::kBodyDeadline);
  return true;
}

Q4T_TEST(http_body_stream_disabled_sink_preserves_buffered_route_limits) {
  Reader reader;
  reader.wire = kHead + "Content-Length: 4097\r\n\r\n" + std::string(4097, 'x');
  reader.stream.consume = {};
  reader.stream.max_bytes = 1ULL << 30;
  Q4T_CHECK(!reader.Run() && reader.status == 413 && reader.body.empty());
  return true;
}
