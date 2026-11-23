// One native Responses invocation, receiving the ordinary executor's events.
#pragma once

#include <functional>

#include "q4t/server/object_store.h"
#include "q4t/server/request_limits.h"

namespace q4t::server {
// Transport-only targets can forward or reject a stream without linking the
// model-side Responses validator into each utility executable.
class IResponsesStreamCapture {
 public:
  virtual ~IResponsesStreamCapture() = default;
  virtual bool Begin() = 0;
  virtual bool Append(std::string_view wire) = 0;
  virtual bool RecordError(int status, std::string_view content_type,
                           std::string_view body) = 0;
  virtual void Reject() noexcept = 0;
};
class NativeResponsesStream final : public IResponsesStreamCapture {
 public:
  // The synchronous sink must bound its queue and observe the request's
  // cancellation. A lost acceptance receipt is never retried. It does not own
  // generation, admission, usage settlement, or a second model request.
  using Sink = std::function<ObjectReply(const io::Json&)>;
  explicit NativeResponsesStream(Sink sink,
                                 size_t maximum_bytes = kChatBodyBytes,
                                 size_t event_bytes = 32ULL << 20);
  bool Begin() override;
  // Only complete native SSE event batches, without HTTP headers/chunking.
  // The same public projection as HTTP is retained; no snapshot is rebuilt
  // from partial deltas and no sequence number is synthesized.
  bool Append(std::string_view wire) override;
  bool RecordError(int status, std::string_view content_type,
                   std::string_view body) override;
  void Reject() noexcept override { failed_ = true; }
  // Called after the actual executor has drained. A terminal event alone does
  // not permit releasing its worker or billing ownership early.
  void Seal() noexcept;
  bool Complete() const { return sealed_ && terminal_ && !failed_; }
  bool Failed() const { return failed_; }
  const std::string& Id() const { return id_; }
  const io::Json& Response() const { return response_; }
  const ObjectReply& Error() const { return error_; }
  size_t Bytes() const { return bytes_; }
  int64_t Events() const { return sequence_; }

 private:
  bool Accept(io::Json event);
  bool Invalid(std::string_view message);
  const Sink sink_;
  const size_t maximum_bytes_, event_bytes_;
  size_t bytes_ = 0;
  int64_t sequence_ = 0;
  std::string id_;
  io::Json response_;
  ObjectReply error_;
  bool started_ = false, terminal_ = false, failed_ = false, sealed_ = false;
};
}  // namespace q4t::server
