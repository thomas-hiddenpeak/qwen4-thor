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
#include "q4t/server/responses_web_search.h"
#include "q4t/server/responses_agent_tree.h"
#include "q4t/server/responses_tool_search.h"
#include "q4t/server/responses_program.h"
#include "q4t/server/responses_mcp_binding.h"
#include "q4t/server/responses_mcp_state.h"
#include "q4t/server/responses_mcp_media.h"
#include "q4t/server/vector_search_execution.h"

namespace q4t::server {

// Internal ownership proof, never an HTTP action. Reserve before the first
// durable mutation; Close only after model/grammar execution has drained.
class IResponsesCleanup {
 public:
  virtual ~IResponsesCleanup() = default;
  virtual ObjectReply Reserve(const std::string& scope, const std::string& id,
                              uint64_t* ticket) = 0;
  virtual ObjectReply TrackMcp(uint64_t ticket, const std::string& model,
                               const io::Json& owner,
                               const std::string& authority_id) = 0;
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
                      bool background_executor = false,
                      const CancelledAgentHistory* cancelled = nullptr);
  ObjectReply RestoreQueued(const std::string& id, const ChatPlanConfig& config,
                            StateAccess access);
  // Private, unexecuted queue state. Freeze publishes no Response resource;
  // the caller must retain all input_snapshots before releasing the holds.
  ObjectReply FreezeInputs(io::Json* inputs);
  void InputsPublished(bool uncertain) noexcept;
  ObjectReply RestoreInputs(const io::Json& inputs,
                            const ChatPlanConfig& config, StateAccess access,
                            int64_t created,
                            const BatchResponseLink* batch_parent = nullptr,
                            const io::Json* credential_source = nullptr);
  ObjectReply BindAssistantRun(std::string thread_id, std::string run_id,
                               std::vector<ObjectRequest> writes);
  bool GenerationReady() const { return generation_ready_; }
  bool PendingFileSheets() const {
    return (files_ && files_->PendingSheets()) ||
           (mcp_media_ && mcp_media_->PendingSheets());
  }
  bool VariableInputSteps() const {
    const auto* context = envelope_.request.GetArray("context_management");
    return PendingFileSheets() ||
           (!envelope_.cache.prewarm && HasNativeTools()) ||
           (!envelope_.cache.prewarm && HasAsyncTools()) ||
           envelope_.moderation.enabled ||
           (!envelope_.cache.prewarm &&
            (envelope_.reasoning_mode == "pro" || !envelope_.summary.empty() ||
             compaction_trigger_ || (context && !context->array.empty())));
  }
  uint64_t FileOutputBudget() const;
  const FileExpansionUsage* FileUsage() const;
  ObjectReply PrepareToolMedia(
      const ChatPlanConfig& config,
      const std::function<bool(const std::string&)>& publish = {});
  ObjectReply NextFileSheet(const ChatPlanConfig& config, FileSheetStep* step);
  ObjectReply CompleteFileSheet(const FileSheetStep& step,
                                std::string_view output,
                                std::string_view finish_reason,
                                uint64_t input_tokens, uint64_t output_tokens,
                                const ChatPlanConfig& config);
  // Count preparation reuses the same owned files and MCP state without
  // publishing a Response or granting any new tools/call.
  ObjectReply PrepareInputSheets(
      const ChatPlanConfig& config,
      const std::function<ObjectReply(FileExpansion&)>& prepare);
  ObjectReply FitContext(const ChatPlanConfig& config,
                         const ResponsesContextCheck& check);
  ObjectReply Begin(std::string* wire);
  ObjectReply StartRunning(std::string* wire);
  ObjectReply CheckParentCancellation(bool* cancelled);
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
  ObjectReply BindAgentSession(ObjectRequest mutation,
                               std::optional<ObjectRequest> environment = {},
                               std::shared_ptr<environment_detail::WorkspaceCopies> workspace_copies = {});
  // Bind only the owned Agent settings for this model step, before preparation.
  void ConfigureAgentSession(const io::Json& session, std::string sealed_http);
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
  Status CiteWebSearchSources(std::string* wire);
  bool HasWebSearch() const { return !web_bindings_.empty(); }
  bool PendingWebSearch() const { return !web_pending_.empty(); }
  using AgentModelStep = std::function<ObjectReply(
      const ResponsesAgentTree::Turn&, bool allow_tools, io::Json*, bool*)>;
  ObjectReply RunAgents(size_t completed_calls, const AgentModelStep& step,
                        const std::function<bool(const std::string&)>& publish);
  void ConfigureAgentModelStep(const ResponsesAgentTree::Turn& turn, bool allow_tools,
                               io::Json* request) const;
  bool PendingAgents() const { return !agent_pending_.empty(); }
  bool QueuedAgents() const { return agents_ && !agents_->FirstQueued().empty(); }
  ObjectReply RunWebSearches(size_t completed_calls,
                             const std::function<bool(const std::string&)>& publish);
  bool HasFileSearch() const { return !file_search_bindings_.empty(); }
  bool HasNativeTools() const {
    return HasFileSearch() || HasWebSearch() || HasToolSearch() ||
           HasPrograms() || HasMcp();
  }
  bool HasAsyncTools() const { return has_async_tools_ || program_async_wait_; }
  bool HasToolSearch() const { return !tool_search_catalog_.search.IsNull(); }
  bool PendingToolSearch() const { return !tool_search_pending_.empty(); }
  bool HasPrograms() const { return program_enabled_ || !programs_.empty(); }
  bool HasMcp() const { return !mcp_bindings_.empty(); }
  bool PendingMcp() const { return !mcp_pending_.empty(); }
  bool PendingMcpResume() const { return !mcp_resume_.array.empty(); }
  ObjectReply RunMcpApprovals(
      const std::function<bool(const std::string&)>& publish);
  ObjectReply RunMcp(size_t completed_calls, bool* client_wait,
                     const std::function<bool(const std::string&)>& publish);
  bool PendingPrograms() const { return !program_pending_.empty(); }
  bool PendingProgramResume() const;
  size_t PendingNativeCalls() const {
    return file_search_pending_.size() + tool_search_pending_.size() +
           program_pending_.size() + mcp_pending_.size() + web_pending_.size() +
           agent_pending_.size();
  }
  ObjectReply RunPrograms(
      size_t completed_calls, bool* client_wait,
      const std::function<bool(const std::string&)>& publish);
  ObjectReply RunToolSearches(
      size_t completed_calls, bool* client_wait,
      const std::function<bool(const std::string&)>& publish);
  bool PendingFileSearch() const { return !file_search_pending_.empty(); }
  static constexpr size_t kMaxNativeToolCalls = 64;
  ObjectReply RunFileSearches(
      size_t completed_calls, const VectorSearchCallbacks& callbacks,
      VectorSearchUsage* usage, bool* continue_answer,
      const std::function<bool(const std::string&)>& publish);
  ObjectReply ContinueFileSearch(
      int64_t output_used, const ChatPlanConfig& config,
      const ResponsesContextCheck& check, ResponsesGenerationPlan* next,
      const std::function<bool(const std::string&)>& publish = {});
  ObjectReply ContinueAsyncTools(size_t completed_calls, int64_t output_used,
                                 const ChatPlanConfig& config,
                                 const ResponsesContextCheck& check,
                                 ResponsesGenerationPlan* next,
                                 bool* continue_answer, std::string* wire);
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
  ObjectReply RunAgentLifecycle(
    const std::string& owner, const std::string& call_id, std::string_view action,
    const io::Json& args, const std::string& arguments, bool available, io::Json* result,
    const std::function<bool(const std::string&)>& publish);
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
  Status AppendWebSearch(const ResponseDelta& delta, bool* handled,
                         std::string* wire);
  Status AppendToolSearch(const ResponseDelta& delta, bool* handled,
                          std::string* wire);
  Status AppendProgram(const ResponseDelta& delta, bool* handled,
                       std::string* wire);
  Status AppendMcp(const ResponseDelta& delta, bool* handled,
                   std::string* wire);
  ObjectReply ExecuteMcpCall(
      const io::Json& call, const ResponsesMcpDefinition& definition,
      size_t output_index,
      const std::function<bool(const std::string&)>& publish);
  ObjectReply ExecuteProgramMcp(
      const ResponsesProgramState& program, const io::Json& intent,
      const std::function<bool(const std::string&)>& publish);
  Status BindActiveTools(const io::Json& active, ChatPlanError* error,
                         const io::Json* history = nullptr);
  ObjectReply RestoreAgentSession();
  ObjectReply AuthorizeAgentMcp(ResponsesMcpDefinition* definition,
                                bool dispatch = true);
  bool BeforeModelOutput() const;
  ObjectReply CaptureMcpCredentials();
  ObjectReply CloseMcpExecution(const StateAccess& access);
  ObjectReply TrackMcpCall(const std::string& id);
  ObjectReply PrepareMcp(const ChatPlanConfig& config,
                         io::Json* continuation = nullptr);
  ObjectReply ResolveMcpResource(const std::string& id, const std::string& uri);
  ObjectReply PrepareMcpMedia(const ChatPlanConfig& config,
                              const io::Json& history);
  ResponsesMediaBudget OtherMcpMediaBudget() const;
  Status BeginMcpCatalogs(ResponsesOutput* output, std::string* wire,
                          size_t begin = 0);
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
  std::unique_ptr<ResponsesMcpState> mcp_state_;
  std::unique_ptr<ResponsesMcpMedia> mcp_media_;
  mutable FileExpansionUsage file_usage_;
  bool mcp_sheet_issued_ = false;
  io::Json agent_mcp_tools_, agent_vaults_;
  bool agent_time_awareness_ = false;
  std::string agent_session_id_, agent_http_sealed_;
  std::map<std::string, std::string> agent_queued_items_;
  std::vector<ResponsesMcpDefinition> mcp_credentials_;
  // Digest of the caller's original request, not the expanded/stored one.
  // Batch may resupply that authenticated File row, never persisted secrets.
  std::string mcp_credential_source_sha256_;
  std::vector<ResponsesMcpDefinition> mcp_definitions_;
  std::vector<ResponsesMcpBinding> mcp_bindings_;
  FileFetchOptions mcp_fetch_;
  struct NativeMcpCall {
    ResponseToolCall call;
    std::string public_id;
    size_t binding = 0;
    size_t output_index = ResponsesOutput::kMaxItems;
    bool dispatched = false;
  };
  std::map<size_t, NativeMcpCall> mcp_pending_;
  size_t mcp_argument_bytes_ = 0;
  io::Json mcp_resume_;
  std::vector<ResponsesFileSearchBinding> file_search_bindings_;
  struct NativeFileCall {
    ResponseToolCall call;
    size_t binding = 0;
    size_t output_index = 0;
    bool dispatched = false;
  };
  std::map<size_t, NativeFileCall> file_search_pending_;
  std::shared_ptr<const WebDeploymentPolicy> web_policy_;
  std::vector<ResponsesWebBinding> web_bindings_;
  std::map<size_t, NativeFileCall> web_pending_;
  Status AppendAgent(const ResponseDelta& delta, bool* handled, std::string* wire);
  std::optional<ResponsesAgentTree> agents_;
  std::map<size_t, ResponseToolCall> agent_pending_;
  size_t agent_argument_bytes_ = 0;
  size_t web_argument_bytes_ = 0;
  WebExecutionBudget web_budget_;
  std::vector<WebPage> web_sources_;
  size_t native_tool_calls_ = 0;
  size_t file_search_argument_bytes_ = 0;
  ResponsesToolCatalog tool_search_catalog_;
  struct NativeSearchCall {
    ResponseToolCall call;
    size_t output_index = 0;
    bool dispatched = false;
  };
  std::map<size_t, NativeSearchCall> tool_search_pending_;
  size_t tool_search_argument_bytes_ = 0;
  bool program_enabled_ = false;
  io::Json program_tools_;
  std::vector<ResponsesProgramState> programs_;
  bool program_resumed_ = false;
  bool program_client_wait_ = false;
  bool program_async_wait_ = false;
  std::map<size_t, ResponseToolCall> program_pending_;
  size_t program_argument_bytes_ = 0;
  FileExpansionOptions program_file_options_;
  FileExpansionUsage program_media_usage_;
  uint64_t program_media_retained_ = 0, program_media_rendered_ = 0;
  ObjectReply PrepareProgramMedia(io::Json* items);
  bool has_async_tools_ = false;
  bool async_continued_ = false;
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
