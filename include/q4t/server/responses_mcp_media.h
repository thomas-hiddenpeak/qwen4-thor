#pragma once

#include <functional>
#include <map>

#include "q4t/server/file_expansion.h"
#include "q4t/server/responses_mcp_state.h"

namespace q4t::server {
// Shared by input attachments, program results and native MCP preparation.
// Counts survive each model turn; a new FileExpansion is not a new budget.
struct ResponsesMediaBudget {
  FileExpansionUsage usage;
  uint64_t retained = 0, rendered = 0, pending_output = 0;
};
void AddResponsesMediaBudget(const ResponsesMediaBudget& source,
                             ResponsesMediaBudget* destination);
FileExpansionOptions RemainingResponsesMediaOptions(
    const FileExpansionOptions& limits, const ResponsesMediaBudget& used);
ObjectReply CheckResponsesMediaBudget(const FileExpansionOptions& limits,
                                      const ResponsesMediaBudget& used);

// Request-owned private projections. Public MCP items and program values stay
// native. Resources are resolved by the invocation owner before this class
// prepares typed media; it never grants a tools/call or resources/read itself.
class ResponsesMcpMedia {
 public:
  explicit ResponsesMcpMedia(ObjectStore& objects) : objects_(objects) {}
  using Resolve =
      std::function<ObjectReply(const std::string&, const std::string&)>;
  ObjectReply Prepare(ResponsesMcpState& state, const io::Json& history,
                      const FileExpansionOptions& limits,
                      const ResponsesMediaBudget& other,
                      const StateAccess& access, bool require_frozen,
                      const Resolve& resolve = {});
  ObjectReply Persist(ResponsesMcpState& state, const StateAccess& access);
  ObjectReply PrepareSheets(
      const std::function<ObjectReply(FileExpansion&)>& prepare);
  ObjectReply Render(io::Json* content) const;
  bool PendingSheets() const;
  uint64_t PendingOutputBudget() const;
  ResponsesMediaBudget Budget() const;
  ObjectReply NextSheet(const ChatPlanConfig& config, FileSheetStep* step);
  ObjectReply CompleteSheet(const FileSheetStep& step, std::string_view output,
                            std::string_view finish_reason,
                            uint64_t input_tokens, uint64_t output_tokens);
  void RetainUncertainHolds() noexcept;

 private:
  struct Entry {
    io::Json source;
    io::Json rendered;
    std::unique_ptr<FileExpansion> files;
    int64_t revision = 0;
    bool dirty = false;
  };
  ObjectStore& objects_;
  FileExpansionOptions limits_;
  ResponsesMediaBudget other_;
  std::map<std::string, Entry> entries_;
  std::string issued_;
};
}  // namespace q4t::server
