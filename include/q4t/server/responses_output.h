// Native Responses items and event lifecycle, shared by JSON and SSE.
#pragma once

#include <cstddef>
#include <cstdint>
#include <functional>
#include <string>
#include <string_view>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#include "q4t/io/json.h"
#include "q4t/server/chat_response_parser.h"
#include "q4t/server/moderation.h"
#include "q4t/server/responses_input.h"
#include "q4t/status.h"

namespace q4t::server {
struct ResponsesUsage {
  int64_t input_tokens = 0;
  int64_t cached_tokens = 0;
  int64_t output_tokens = 0;
  int64_t reasoning_tokens = 0;
  int64_t cache_write_tokens = 0;
};
using ResponsesReasoningSeal =
    std::function<Status(size_t index, std::string* opaque)>;

// The pinned SDK's ResponseCustomToolCall omits status/created_by, while its
// stored ResponseItem requires status. Retain that lifecycle internally and
// project at the Responses output/event boundary. Conversation views likewise
// use the base type; Responses input_items preserves the Item lifecycle.
io::Json ResponsesPublicValue(io::Json value);

class ResponsesOutput {
 public:
  static constexpr size_t kStorageLimit = 32 * 1024 * 1024;
  static constexpr size_t kMaxItems = 512;
  // options is the validated effective public request, not a Chat request.
  Status Init(const io::Json& options, std::string id, int64_t created,
              bool obfuscation = true, size_t limit = kStorageLimit);
  Status Start(std::string* wire);
  Status StartRunning(std::string* wire);
  // Only queued work can resume. Already executed output is never replayed.
  Status RestoreQueued(const io::Json& snapshot, int64_t next_sequence);
  // The official SSE union has no response.cancelled event. Cancellation
  // persists its resource and ends subscribers without inventing such an event.
  Status Cancel();
  Status Append(const ResponseDelta& delta, std::string_view chat_logprobs,
                std::string* wire);
  // Actual separately generated summary text. Never reinterpret a reasoning
  // delta as a summary. This creates one derived reasoning item, without raw
  // reasoning content, and preserves its identity across summary deltas.
  Status AppendSummary(std::string_view text, std::string* wire);
  Status StartCompaction(std::string id, std::string* wire);
  Status CompleteCompaction(const io::Json& item, std::string* wire);
  Status StartFileSearch(size_t* index, std::string* wire);
  Status SearchFileSearch(size_t index, const io::Json& queries,
                          std::string* wire);
  Status CompleteFileSearch(size_t index, const io::Json& results,
                            std::string* wire);
  Status FailFileSearch(size_t index, std::string* wire);
  Status SkipFileSearch(size_t index, const io::Json& queries,
                        std::string* wire);
  // Private effective identities can grow without rewriting public tools.
  Status BindTools(const io::Json& active);
  Status AppendProgramItems(const io::Json& items, std::string* wire);
  Status StartToolSearch(const ResponseToolCall& call, bool server,
                         size_t* index, std::string* wire);
  Status CompleteToolSearchCall(size_t index, const io::Json& arguments,
                                bool incomplete, std::string* wire);
  Status CompleteToolSearchOutput(size_t call_index, const io::Json& tools,
                                  std::string* wire);
  // Annotations bind explicit markers to actual prior completed native
  // retrieval items. Text bytes/logprobs remain unchanged.
  Status CiteFileSearchSources(std::string* wire);
  // Close validated async calls before independent answer generation. Mixed
  // synchronous calls leave the response at the normal client-tool boundary.
  Status CompleteAsyncCalls(size_t completed_calls, bool* continue_answer,
                            std::string* wire);
  // Only calls with a validated closing delimiter may receive arguments.done.
  Status Finish(bool incomplete, size_t completed_calls,
                const ResponsesUsage& usage, int64_t completed_at,
                std::string* wire, const ResponsesReasoningSeal& seal = {},
                bool content_filtered = false);
  Status Fail(std::string_view code, std::string_view message,
              std::string* wire);
  // Used immediately before a failure/cancellation terminal, never as a
  // live checkpoint that could be mistaken for complete usage after a crash.
  Status SetUsage(const ResponsesUsage& usage);
  static std::string RenderStreamError(void* owner, int status,
                                       const std::string& message,
                                       std::string_view code,
                                       std::string_view param);
  std::string Json() const;
  const io::Json& Snapshot() const { return response_; }
  int64_t* Sequence() { return &sequence_; }
  bool Terminal() const { return terminal_; }
  bool HasSummary() const { return summary_item_ < kMaxItems; }
  void CacheDiagnostics(io::Json value) noexcept;
  Status Moderation(const io::Json& results, const ModerationUsage& usage);

 private:
  Status Charge(size_t bytes);
  void Event(std::string_view type, io::Json fields, std::string* wire);
  io::Json Location(size_t index, bool content = false) const;
  io::Json& Items();
  Status AddItem(ResponseDelta::Kind kind, const ResponseToolCall* call,
                 size_t* index, std::string* wire);
  void CloseItem(size_t index, bool incomplete, std::string* wire);
  io::Json response_;
  std::unordered_map<size_t, size_t> calls_;
  std::unordered_map<size_t, std::string> model_names_;
  std::vector<ResponsesToolIdentity> tool_identities_;
  std::unordered_set<size_t> tool_search_results_;
  std::vector<size_t> call_indices_;
  std::unordered_set<size_t> closed_items_;
  size_t summary_item_ = kMaxItems;
  size_t compaction_item_ = kMaxItems;
  bool compaction_done_ = false;
  size_t limit_ = kStorageLimit;
  size_t bytes_ = 0;
  int64_t sequence_ = 0;
  bool initialized_ = false;
  bool started_ = false;
  bool terminal_ = false;
  bool obfuscation_ = true;
};
}  // namespace q4t::server
