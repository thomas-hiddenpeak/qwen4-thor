// HTTP response helpers shared by the server and host contract tests.
#pragma once

#include <cstddef>
#include <cstdint>
#include <chrono>
#include <condition_variable>
#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <string_view>
#include <thread>
#include <vector>

#include "q4t/server/request_control.h"
#include "q4t/server/client_policy.h"
#include "q4t/server/http_observation.h"
#include "q4t/server/finite_response.h"

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
  struct Detached {
    // Input preparation borrows the parent descriptor only for cancellation
    // checks. All response writes remain detached, including error paths.
    int borrowed_fd = -1;
    FiniteResponse* output = nullptr;
  };
  // Native background execution observes errors/admission without a socket,
  // transport worker, heartbeat or loopback request.
  explicit HttpResponseContext(Detached);
  ~HttpResponseContext();
  HttpResponseContext(const HttpResponseContext&) = delete;
  HttpResponseContext& operator=(const HttpResponseContext&) = delete;
  static HttpResponseContext* Current(int fd);
  bool CapturesJson() const { return captured_ != nullptr; }
  bool CaptureJson(int status, std::string_view content_type,
                   std::string_view body) {
    return captured_ && captured_->Record(status, content_type, body);
  }
  void RejectCapturedJson() noexcept {
    if (captured_) captured_->Reject();
  }
  const std::string& TraceId() const { return trace_id_; }
  bool SetTraceId(std::string_view id);
  struct PreparationUsage {
    uint64_t input_tokens = 0, output_tokens = 0, model_steps = 0;
  };
  // Actual auxiliary work, separate from count's standard input_tokens body.
  // Set before the first header; never accepts caller-controlled header text.
  void SetPreparationUsage(PreparationUsage usage) noexcept {
    preparation_usage_ = usage;
  }
  const PreparationUsage& InputPreparationUsage() const {
    return preparation_usage_;
  }
  // Handler-thread-only identity, minted by header admission.
  const ClientIdentity& Client() const { return client_; }
  ClientRateDomain RateDomain() const { return rate_domain_; }
  // Trusted native execution only. The context settles on destruction unless
  // ownership is explicitly moved to the detached background job.
  bool BindBatchRate(std::shared_ptr<ClientRateLease> rate) {
    if (batch_rate_ || rate_domain_ != ClientRateDomain::kBatch || !rate ||
        !rate->PrechargedFor(client_))
      return false;
    batch_rate_ = std::move(rate);
    return true;
  }
  const std::shared_ptr<ClientRateLease>& BatchRate() const {
    return batch_rate_;
  }
  std::shared_ptr<ClientRateLease> TakeBatchRate() {
    return std::move(batch_rate_);
  }
  void SetClient(ClientIdentity client, ClientLease* lease,
                 ClientRateDomain domain = ClientRateDomain::kOnline) {
    client_ = client;
    client_lease_ = lease;
    rate_domain_ = domain;
  }
  bool TransferClientLease(ClientLease& destination) noexcept {
    return client_lease_ && client_lease_->TransferTo(destination);
  }
  ResponseState* State() { return &state_; }
  using ErrorObserver = void (*)(void*, int, std::string_view,
                                 std::string_view) noexcept;
  void SetErrorObserver(ErrorObserver observer, void* owner) noexcept {
    error_observer_ = observer;
    error_observer_owner_ = owner;
  }
  void ObserveError(int status, std::string_view message,
                    std::string_view code) noexcept {
    if (error_observer_)
      error_observer_(error_observer_owner_, status, message, code);
  }
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
  using StreamErrorRenderer = std::string (*)(void*, int, const std::string&,
                                              std::string_view,
                                              std::string_view);
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
  void SetKeepAlive(bool enabled, bool http11 = true) {
    keep_alive_ = enabled;
    http11_ = http11;
  }
  bool KeepAlive() const { return keep_alive_; }
  // Enable chunking only after the unencoded response headers were written.
  void StartChunkedBody() { chunked_ = keep_alive_ && http11_; }
  bool Http11() const { return http11_; }
  void LengthResponse() { length_response_ = true; }
  // Join heartbeats and terminate framing only after a complete logical
  // response. False means the owner must close, never parse another request.
  bool FinishResponse();
  bool Write(const char* data, size_t size, const WriteOptions& options,
             ResponseState* header_state = nullptr);

 private:
  void HeartbeatLoop();
  bool WritePayload(const char* data, size_t size,
                    const WriteOptions& options,
                    ResponseState* header_state,
                    HttpWriteObservation* observation);
  // Caller holds write_mu_. Only the first failed write is retained.
  void RecordWriteFailure(HttpWriteObservation value, HttpWriteOrigin origin);
  const int fd_;
  bool detached_ = false;
  FiniteResponse* captured_ = nullptr;
  bool keep_alive_ = false;
  bool http11_ = true;
  bool chunked_ = false;
  bool length_response_ = false;
  bool terminal_written_ = false;
  bool framing_finished_ = false;
  HttpResponseContext* const previous_;
  std::string trace_id_;
  PreparationUsage preparation_usage_;
  ClientIdentity client_;
  ClientRateDomain rate_domain_ = ClientRateDomain::kOnline;
  ClientLease* client_lease_ = nullptr;  // Owner outlives this context.
  std::shared_ptr<ClientRateLease> batch_rate_;
  ResponseState state_ = ResponseState::kUnsent;  // handler thread only
  ErrorObserver error_observer_ = nullptr;
  void* error_observer_owner_ = nullptr;
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
// Fixed-length response from bounded chunks (<=1 MiB). A failed or
// short source closes the frame; never append JSON/SSE or reuse the connection.
bool SendBinaryResponse(int fd, uint64_t length,
                         const std::function<bool(std::string*)>& next,
                         const WriteOptions& options = {},
                         std::string_view content_type =
                             "application/octet-stream");
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
