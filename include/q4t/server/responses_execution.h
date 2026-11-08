#pragma once

#include <array>

#include "q4t/server/responses_output.h"
#include "q4t/server/responses_state.h"

namespace q4t::server {

// Request-owned protocol/state lifecycle. The model executor feeds actual
// deltas and usage; a success terminal is published only after durable commit.
// This owner must outlive the HTTP exception renderer and generation cleanup.
class ResponsesExecution {
 public:
  explicit ResponsesExecution(ObjectStore& objects) : state_(objects) {}
  ObjectReply Prepare(const io::Json& request, const ChatPlanConfig& config,
                      StateAccess access, int64_t created);
  ObjectReply Begin(std::string* wire);
  ObjectReply Checkpoint();
  void BindCancellation(std::chrono::steady_clock::time_point deadline,
                        std::function<bool()> cancelled) {
    access_.deadline = deadline;
    access_.cancelled = std::move(cancelled);
  }
  Status Append(const ResponseDelta& delta, std::string_view probabilities,
                std::string* wire);
  ObjectReply Finish(bool incomplete, size_t completed_calls,
                     const ResponsesUsage& usage, int64_t completed_at,
                     std::string* wire);
  // After model/grammar cleanup, close ordinary error/exception paths without
  // inheriting a disconnected client's cancellation or already-expired timer.
  // A lost mutation receipt is never retried or overwritten speculatively.
  ObjectReply CloseUnfinished();

  const ResponsesEnvelope& Envelope() const { return envelope_; }
  const ResponsesGenerationPlan& Generation() const { return generation_; }
  const ResponsesOutput& Output() const { return output_; }
  int64_t* Sequence() { return output_.Sequence(); }
  const std::string& Id() const { return plan_.id; }
  bool Begun() const { return begun_; }
  bool Finished() const { return plan_.finished; }
  bool CommitUnknown() const { return commit_unknown_; }
  static std::string RenderStreamError(void* owner, int status,
                                       const std::string& message,
                                       std::string_view code,
                                       std::string_view param);
  static void ObserveError(void* owner, int status, std::string_view message,
                           std::string_view code) noexcept;

 private:
  Status Capture(std::string_view wire, io::Json* events, size_t* bytes) const;
  ObjectReply BindMutationResult(ObjectReply reply);
  ResponsesState state_;
  StateAccess access_;
  ResponsesEnvelope envelope_;
  ResponsesGenerationPlan generation_;
  ResponseStatePlan plan_;
  ResponsesOutput output_;
  io::Json events_;
  size_t event_bytes_ = 0;
  bool prepared_ = false;
  bool begun_ = false;
  bool commit_unknown_ = false;
  std::chrono::steady_clock::time_point checkpoint_time_;
  size_t checkpoint_bytes_ = 0;
  int failure_status_ = 0;
  std::array<char, 128> failure_code_{};
  std::array<char, 1024> failure_message_{};
};

}  // namespace q4t::server
