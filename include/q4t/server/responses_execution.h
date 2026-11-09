#pragma once

#include <array>

#include "q4t/server/responses_output.h"
#include "q4t/server/responses_compaction.h"
#include "q4t/server/responses_state.h"
#include "q4t/server/request_control.h"
#include "q4t/server/state_cipher.h"
#include "q4t/server/prompt_registry.h"

namespace q4t::server {

// Internal ownership proof, never an HTTP action. Reserve before the first
// durable mutation; Close only after model/grammar execution has drained.
class IResponsesCleanup {
 public:
  virtual ~IResponsesCleanup() = default;
  virtual ObjectReply Reserve(const std::string& scope, const std::string& id,
                              uint64_t* ticket) = 0;
  virtual void Close(uint64_t ticket, bool known_terminal) noexcept = 0;
};

// Request-owned protocol/state lifecycle. The model executor feeds actual
// deltas and usage; a success terminal is published only after durable commit.
// This owner must outlive the HTTP exception renderer and generation cleanup.
class ResponsesExecution {
 public:
  explicit ResponsesExecution(ObjectStore& objects,
                              IResponsesCleanup* cleanup = nullptr,
                              bool require_cleanup = false,
                              bool standalone_compaction = false)
      : state_(objects),
        cipher_(objects),
        prompts_(objects),
        cleanup_(cleanup),
        require_cleanup_(require_cleanup),
        standalone_compaction_(standalone_compaction) {}
  ResponsesExecution(const ResponsesExecution&) = delete;
  ResponsesExecution& operator=(const ResponsesExecution&) = delete;
  ObjectReply Prepare(const io::Json& request, const ChatPlanConfig& config,
                      StateAccess access, int64_t created,
                      bool background_executor = false);
  ObjectReply RestoreQueued(const std::string& id, const ChatPlanConfig& config,
                            StateAccess access);
  ObjectReply FitContext(const ChatPlanConfig& config,
                         const ResponsesContextCheck& check);
  ObjectReply Begin(std::string* wire);
  ObjectReply StartRunning(std::string* wire);
  ObjectReply Cancel();
  ObjectReply Checkpoint();
  void BindCancellation(std::chrono::steady_clock::time_point deadline,
                        std::function<bool()> cancelled) {
    access_.deadline = deadline;
    access_.cancelled = std::move(cancelled);
  }
  void BindBackgroundControl(std::shared_ptr<RequestControl> control);
  int64_t ExecutionDeadlineAt() const { return plan_.execution_deadline_at; }
  Status Append(const ResponseDelta& delta, std::string_view probabilities,
                std::string* wire);
  Status AppendSummary(std::string_view text, std::string* wire);
  // Selection uses the real expanded input count, before rate reservation.
  ObjectReply SelectCompaction(size_t input_tokens,
                               const ChatPlanConfig& config);
  ObjectReply StartCompaction(std::string* wire);
  using ContextMeasure = std::function<Status(const ResponsesGenerationPlan&,
                                              size_t*, ChatPlanError*)>;
  ObjectReply CompleteCompaction(std::string_view memory, int64_t output_used,
                                 const ChatPlanConfig& config,
                                 const ContextMeasure& measure,
                                 std::string* wire);
  const ResponsesCompactionPlan& Compaction() const { return compaction_; }
  bool CompactionSelected() const { return compaction_selected_; }
  bool StandaloneCompaction() const { return standalone_compaction_; }
  std::string ResultJson() const;
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
  void CacheDiagnostics(io::Json value) noexcept {
    output_.CacheDiagnostics(std::move(value));
  }
  int64_t* Sequence() { return output_.Sequence(); }
  const std::string& Id() const { return plan_.id; }
  const std::string& Scope() const { return plan_.scope; }
  size_t RetainedInputBytes() const;
  bool Begun() const { return begun_; }
  bool Prepared() const { return prepared_; }
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
  ObjectReply RequiredExecutors(bool background_executor) const;
  ObjectReply PlanGeneration(const ChatPlanConfig& config, int64_t created);
  Status RecordOutput(Status status, std::string* wire);
  ResponsesState state_;
  StateCipher cipher_;
  PromptRegistry prompts_;
  io::Json generation_history_;
  ChatPlanConfig cipher_config_;
  IResponsesCleanup* cleanup_ = nullptr;
  bool require_cleanup_ = false;
  uint64_t cleanup_ticket_ = 0;
  bool durable_begin_attempted_ = false;
  StateAccess access_;
  ResponsesEnvelope envelope_;
  ResponsesGenerationPlan generation_;
  ResponseStatePlan plan_;
  ResponsesOutput output_;
  ResponsesCompactionPlan compaction_;
  io::Json compaction_window_;
  std::string compaction_id_;
  size_t compaction_source_tokens_ = 0;
  bool standalone_compaction_ = false;
  bool compaction_trigger_ = false;
  bool compaction_selected_ = false;
  bool compaction_started_ = false;
  bool compaction_completed_ = false;
  io::Json events_;
  size_t event_bytes_ = 0;
  bool prepared_ = false;
  bool begun_ = false;
  bool commit_unknown_ = false;
  std::chrono::steady_clock::time_point checkpoint_time_;
  size_t checkpoint_bytes_ = 0;
  // Valid generated events withheld by a failed checkpoint. Error rendering
  // sends this original prefix before its terminal; no sequence is invented.
  std::string failure_prefix_;
  std::shared_ptr<RequestControl> background_control_;
  int failure_status_ = 0;
  std::array<char, 128> failure_code_{};
  std::array<char, 1024> failure_message_{};
};

}  // namespace q4t::server
