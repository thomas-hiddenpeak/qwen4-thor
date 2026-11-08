// HTTP response helpers shared by the server and host contract tests.
#pragma once

#include <cstddef>
#include <cstdint>
#include <chrono>
#include <condition_variable>
#include <memory>
#include <mutex>
#include <string>
#include <string_view>
#include <thread>
#include <vector>

#include "q4t/server/request_control.h"
#include "q4t/server/client_policy.h"
#include "q4t/server/http_observation.h"

namespace q4t::server::detail {

enum class ResponseState {
  kUnsent,
  kHeadersStarted,
  kStreaming,
  kTerminal,
  kComplete
};
// Allocation-free best effort after an exception; never adds a second terminal.
enum class ExceptionKind { kInternal, kHostMemory };
void SendExceptionResponse(
    int fd, ResponseState state,
    ExceptionKind kind = ExceptionKind::kInternal) noexcept;

// Emits complete UTF-8 scalars, replacing each malformed maximal subpart with
// U+FFFD. At most three bytes survive Append; Finish resolves an incomplete
// final scalar. One instance belongs to one response, across token boundaries.
struct Utf8Span {
  size_t input_begin;
  size_t input_end;
  size_t output_begin;
  size_t output_end;
};

class Utf8Stream {
 public:
  std::string Append(std::string_view bytes,
                     std::vector<Utf8Span>* spans = nullptr);
  std::string Finish(std::vector<Utf8Span>* spans = nullptr);

 private:
  std::string pending_;
  size_t input_bytes_ = 0;
  size_t output_bytes_ = 0;
};

std::string NormalizeUtf8(std::string_view bytes);
std::string JsonEscape(const std::string& s);
struct WriteOptions {
  std::chrono::steady_clock::time_point deadline =
      std::chrono::steady_clock::now() + std::chrono::seconds(30);
  RequestControl* control = nullptr;
};

// 128 random bits from the OS; never falls back to a process-local counter.
std::string NewRequestId(std::string_view prefix = "req-");

// Bound to one handler thread/connection. All writes, including heartbeat
// comments, share one mutex. The helper thread never reads handler state or
// touches model resources. It is joined before the socket owner can return.
class HttpResponseContext {
 public:
  // Metrics must outlive the context. Publishing occurs after heartbeat join.
  explicit HttpResponseContext(int fd, HttpTransportMetrics* metrics = nullptr);
  ~HttpResponseContext();
  HttpResponseContext(const HttpResponseContext&) = delete;
  HttpResponseContext& operator=(const HttpResponseContext&) = delete;
  static HttpResponseContext* Current(int fd);
  const std::string& TraceId() const { return trace_id_; }
  bool SetTraceId(std::string_view id);
  // Handler-thread-only identity, minted by header admission.
  const ClientIdentity& Client() const { return client_; }
  void SetClient(ClientIdentity client, ClientLease* lease) {
    client_ = client;
    client_lease_ = lease;
  }
  ResponseState* State() { return &state_; }
  // Handler-thread-only, non-owning hook. Clear it before its owner dies.
  // Invoked once before terminal publication, including error responses.
  void SetBeforeTerminal(void (*hook)(void*) noexcept, void* owner) noexcept {
    terminal_hook_ = hook;
    terminal_owner_ = owner;
  }
  void BeforeTerminal() noexcept {
    if (client_lease_) client_lease_->BeginHandoff();
    auto* hook = terminal_hook_;
    terminal_hook_ = nullptr;
    if (hook) hook(terminal_owner_);
  }
  // Owned by the outer handler, including exception unwinding. The renderer
  // emits native Responses failure events; sequence also supports a fixed-
  // storage error event when allocation fails. Clear before the owner dies.
  using StreamErrorRenderer = std::string (*)(
      void*, int, const std::string&, std::string_view, std::string_view);
  void SetResponsesStream(StreamErrorRenderer renderer, void* owner,
                          int64_t* sequence) noexcept {
    const bool enabled = renderer && sequence;
    stream_error_renderer_ = enabled ? renderer : nullptr;
    stream_error_owner_ = enabled ? owner : nullptr;
    responses_sequence_ = enabled ? sequence : nullptr;
  }
  int64_t* ResponsesSequence() const { return responses_sequence_; }
  std::string StreamError(int status, const std::string& message,
                          std::string_view code, std::string_view param = {});
  void StartHeartbeats(
      std::shared_ptr<RequestControl> control,
      std::chrono::milliseconds interval = std::chrono::seconds(5));
  void StopHeartbeats();
  void RecordReceiveFailure(HttpReceiveFailure reason, int status,
                            int system_error = 0);
  HttpWriteObservation WriteFailure();
  bool Write(const char* data, size_t size, const WriteOptions& options,
             ResponseState* header_state = nullptr);

 private:
  void HeartbeatLoop();
  // Caller holds write_mu_. Only the first failed write is retained.
  void RecordWriteFailure(HttpWriteObservation value, HttpWriteOrigin origin);
  const int fd_;
  HttpResponseContext* const previous_;
  std::string trace_id_;
  ClientIdentity client_;
  ClientLease* client_lease_ = nullptr;  // Owner outlives this context.
  ResponseState state_ = ResponseState::kUnsent;  // handler thread only
  void (*terminal_hook_)(void*) noexcept = nullptr;
  void* terminal_owner_ = nullptr;
  StreamErrorRenderer stream_error_renderer_ = nullptr;
  void* stream_error_owner_ = nullptr;
  int64_t* responses_sequence_ = nullptr;
  std::mutex write_mu_;
  std::condition_variable wake_;
  std::thread heartbeat_;
  bool stop_ = false;
  HttpWriteObservation write_failure_;
  HttpTransportMetrics* const metrics_;
  HttpReceiveFailure receive_failure_ = HttpReceiveFailure::kNone;
  int receive_status_ = 0;
  int receive_system_error_ = 0;
  std::shared_ptr<RequestControl> control_;
  std::chrono::milliseconds interval_{5000};
  std::chrono::steady_clock::time_point last_write_ =
      std::chrono::steady_clock::now();
};
// State tracks successful socket writes, including partial headers;
// incomplete headers cannot be followed by another HTTP or SSE response.
bool BeginStreamingResponse(int fd, ResponseState* state,
                            const WriteOptions& options = {});
// One deadline covers the entire logical write, including partial progress.
bool WriteAll(int fd, const char* data, size_t len,
              const WriteOptions& options = {});
bool WriteAll(int fd, const std::string& s, const WriteOptions& options = {});
bool SendSimple(int fd, int code, const char* status, const std::string& body,
                const std::string& content_type,
                const WriteOptions& options = {}, std::string_view allow = {});
void SendError(int fd, int code, const std::string& message,
               const WriteOptions& options = {},
               std::string_view error_code = {}, std::string_view param = {},
               std::string_view allow = {});
// Stable Q4T codes inside the OpenAI error envelope. No retry guarantee.
std::string ErrorJson(int status, const std::string& message,
                      std::string_view error_code = {},
                      std::string_view param = {});
void SendStreamError(int fd, ResponseState* state, int status,
                     const std::string& message, const WriteOptions& options,
                     std::string_view error_code);
// A malformed model response is an error terminal, never stop/tool_calls.
void SendResponseParseError(int fd, bool stream, ResponseState* state,
                            const std::string& message,
                            const WriteOptions& options = {},
                            std::string_view code = "response_parse_failed",
                            int http_status = 502);
// delta_json is an object serialized by the response parser, never raw input.
std::string SseDeltaChunk(const std::string& id, const std::string& model,
                          const std::string& delta_json,
                          const std::string& finish_reason, int index,
                          int64_t created = 0, bool include_usage = false,
                          std::string_view logprobs_json = "null");
std::string SseChunk(const std::string& id, const std::string& model,
                     const std::string& delta_role, const std::string& content,
                     const std::string& finish_reason, int index,
                     int64_t created = 0, bool include_usage = false);

}  // namespace q4t::server::detail
