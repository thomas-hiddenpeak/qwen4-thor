#pragma once

#include <array>
#include <map>
#include <optional>
#include <stdexcept>

#include "q4t/server/file_expansion.h"

#include "q4t/server/actual_usage.h"
#include "q4t/server/responses_output.h"
#include "q4t/server/responses_compaction.h"
#include "q4t/server/responses_state.h"
#include "q4t/server/request_control.h"
#include "q4t/server/state_cipher.h"
#include "q4t/server/prompt_registry.h"
#include "q4t/server/responses_file_search.h"
#include "q4t/server/vector_search_execution.h"

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
      : objects_(objects),
        state_(objects),
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
  // Private, unexecuted queue state. Freeze publishes no Response resource;
  // the caller must retain all input_snapshots before releasing the holds.
  ObjectReply FreezeInputs(io::Json* inputs);
  void InputsPublished(bool uncertain) noexcept;
  ObjectReply RestoreInputs(const io::Json& inputs,
                            const ChatPlanConfig& config, StateAccess access,
                            int64_t created,
                            const BatchResponseLink* batch_parent = nullptr);
  bool GenerationReady() const { return generation_ready_; }
  bool PendingFileSheets() const { return files_ && files_->PendingSheets(); }
  bool VariableInputSteps() const {
    const auto* context = envelope_.request.GetArray("context_management");
    return PendingFileSheets() ||
           (!envelope_.cache.prewarm && HasFileSearch()) ||
           envelope_.moderation.enabled ||
           (!envelope_.cache.prewarm &&
            (envelope_.reasoning_mode == "pro" || !envelope_.summary.empty() ||
             compaction_trigger_ || (context && !context->array.empty())));
  }
  uint64_t FileOutputBudget() const {
    return files_ ? files_->PendingOutputBudget() : 0;
  }
  const FileExpansionUsage* FileUsage() const {
    return files_ ? &files_->Usage() : nullptr;
  }
  ObjectReply NextFileSheet(const ChatPlanConfig& config, FileSheetStep* step);
  ObjectReply CompleteFileSheet(const FileSheetStep& step,
                                std::string_view output,
                                std::string_view finish_reason,
                                uint64_t input_tokens, uint64_t output_tokens,
                                const ChatPlanConfig& config);
  ObjectReply FitContext(const ChatPlanConfig& config,
                         const ResponsesContextCheck& check);
  ObjectReply Begin(std::string* wire);
  ObjectReply StartRunning(std::string* wire);
  ObjectReply CheckBatchParent(bool* cancelled);
  ObjectReply Cancel();
  ObjectReply Checkpoint();
  // Block policy keeps all generated items/events private until the actual
  // classifier approves them. GET/replay continue to see the safe checkpoint.
  ObjectReply HoldUnreviewedOutput();
  ObjectReply PublishReviewedOutput(const io::Json& moderation,
                                    std::string* wire);
  void DiscardUnreviewedOutput() noexcept;
  bool OutputHeld() const { return moderation_safe_.has_value(); }
  void BindCancellation(std::chrono::steady_clock::time_point deadline,
                        std::function<bool()> cancelled) {
    access_.deadline = deadline;
    access_.cancelled = std::move(cancelled);
  }
  void BindBackgroundControl(std::shared_ptr<RequestControl> control);
  int64_t ExecutionDeadlineAt() const { return plan_.execution_deadline_at; }
  ClientRateDomain RateDomain() const { return plan_.rate_domain; }
  std::chrono::steady_clock::time_point InputDeadline() const {
    return access_.deadline;
  }
  // Trusted service origin. It cannot be changed once execution is published.
  bool BindRateDomain(ClientRateDomain domain) {
    if (domain != ClientRateDomain::kOnline &&
        domain != ClientRateDomain::kBatch)
      return false;
    if (begun_ || commit_unknown_) return domain == plan_.rate_domain;
    plan_.rate_domain = domain;
    return true;
  }
  Status Append(const ResponseDelta& delta, std::string_view probabilities,
                std::string* wire);
  Status AppendSummary(std::string_view text, std::string* wire);
  Status CiteFileSearchSources(std::string* wire);
  bool HasFileSearch() const { return !file_search_bindings_.empty(); }
  bool PendingFileSearch() const { return !file_search_pending_.empty(); }
  static constexpr size_t kMaxNativeToolCalls = 64;
  ObjectReply RunFileSearches(
      size_t completed_calls, const VectorSearchCallbacks& callbacks,
      VectorSearchUsage* usage, bool* continue_answer,
      const std::function<bool(const std::string&)>& publish);
  ObjectReply ContinueFileSearch(int64_t output_used,
                                 const ChatPlanConfig& config,
                                 const ResponsesContextCheck& check,
                                 ResponsesGenerationPlan* next);
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
  // Storage keeps full result evidence; create-stream projection obeys include.
  std::string PublicWire(std::string_view wire) const;
  io::Json PublicResponse() const;
  ObjectReply Finish(bool incomplete, size_t completed_calls,
                     const ResponsesUsage& usage, int64_t completed_at,
                     std::string* wire, bool content_filtered = false);
  Status SetModerationResults(const io::Json& results,
                              const ModerationUsage& usage);
  // After model/grammar cleanup, close ordinary error/exception paths without
  // inheriting a disconnected client's cancellation or already-expired timer.
  // A lost mutation receipt is never retried or overwritten speculatively.
  ObjectReply CloseUnfinished();

  const ResponsesEnvelope& Envelope() const { return envelope_; }
  const ResponsesGenerationPlan& Generation() const {
    if (!generation_ready_)
      throw std::logic_error("File preparation has not completed");
    return generation_;
  }
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
  static void ObserveUsage(void* owner,
                           const std::optional<ActualUsage>& usage) noexcept;

 private:
  Status Capture(std::string_view wire, io::Json* events, size_t* bytes) const;
  ObjectReply BindMutationResult(ObjectReply reply);
  ObjectReply RequiredExecutors(bool background_executor) const;
  ObjectReply PlanGeneration(const ChatPlanConfig& config, int64_t created);
  ObjectReply BuildPreparedGeneration(const ChatPlanConfig& config);
  ObjectReply PersistPreparedInputs();
  void ReleasePreparedInputs() noexcept;
  Status RecordOutput(Status status, std::string* wire);
  Status AppendFileSearch(const ResponseDelta& delta, bool* handled,
                          std::string* wire);
  Status ApplyActualUsage(ResponsesOutput* output) const;
  ObjectStore& objects_;
  std::unique_ptr<FileExpansion> files_;
  io::Json file_history_;
  bool require_frozen_inputs_ = false;
  bool generation_ready_ = false;
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
  std::vector<ResponsesFileSearchBinding> file_search_bindings_;
  struct NativeFileCall {
    ResponseToolCall call;
    size_t binding = 0;
    size_t output_index = 0;
    bool dispatched = false;
  };
  std::map<size_t, NativeFileCall> file_search_pending_;
  size_t file_search_calls_ = 0;
  size_t file_search_argument_bytes_ = 0;
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
  std::optional<ResponsesOutput> moderation_safe_;
  size_t moderation_safe_events_ = 0;
  size_t moderation_safe_bytes_ = 0;
  std::string moderation_wire_;
  std::shared_ptr<RequestControl> background_control_;
  int failure_status_ = 0;
  std::optional<ActualUsage> actual_usage_;
  std::array<char, 128> failure_code_{};
  std::array<char, 1024> failure_message_{};
};

}  // namespace q4t::server
