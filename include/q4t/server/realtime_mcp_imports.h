// One session owns publication; finite workers import real native catalogs.
#pragma once

#include <deque>
#include <memory>

#include "q4t/server/realtime_conversation.h"
#include "q4t/server/realtime_mcp_catalog.h"

namespace q4t::server {
struct RealtimeMcpImportCallbacks {
  // The connected native handler binds authenticated scope and deployment.
  // Run outside the session thread; honor BOTH cancellation and deadline.
  // This callback only lists tools, never dispatches a model-selected call.
  std::function<ObjectReply(const ResponsesMcpDefinition&, std::string,
                            std::chrono::steady_clock::time_point,
                            const std::function<bool()>&, RealtimeMcpCatalog*)>
      import;
  std::function<ObjectReply(const io::Json&)> emit;
  std::function<std::string(std::string_view)> new_id;
  // Thread safe, signal only. It must not touch session-owned mutable state.
  std::function<bool()> interrupted;
};
struct RealtimeMcpImportLimits {
  size_t workers = 4;
  size_t entries = 64;
  size_t retained_bytes = 16ULL << 20;
  std::chrono::milliseconds timeout{30000};
};

class RealtimeMcpImports {
 public:
  RealtimeMcpImports(RealtimeConversation& history,
                     RealtimeMcpImportCallbacks callbacks,
                     RealtimeMcpImportLimits limits = {});
  ~RealtimeMcpImports();
  RealtimeMcpImports(const RealtimeMcpImports&) = delete;
  RealtimeMcpImports& operator=(const RealtimeMcpImports&) = delete;
  // All public methods run on the session owner. Begin reserves history and
  // publishes added/in_progress before any I/O. Tick starts at most four
  // workers; queued jobs share the original finite deadline.
  ObjectReply Begin(const ResponsesMcpDefinition& definition, std::string id,
                    bool in_conversation, RealtimeItemTicket* ticket);
  ObjectReply Tick();
  // Retire closes an unfinished import as failed, preserves its public
  // history and immediately revokes private authority. An old worker keeps
  // its slot until joined; a late result never changes the terminal item.
  ObjectReply Retire(const RealtimeItemTicket& ticket);
  // Delete exact history generation and revoke authority before cancelling.
  ObjectReply Erase(const RealtimeItemTicket& ticket);
  bool Owns(const RealtimeItemTicket& ticket) const;
  // Same complete request definition, including private credentials. An
  // earlier catalog with the same label but another target is not reusable.
  bool Find(const ResponsesMcpDefinition& definition,
            RealtimeItemTicket* ticket) const;
  // Only completed, successfully imported, still-retained native authority.
  // Public JSON, empty placeholders and failed lists grant no capability.
  ObjectReply Get(const RealtimeItemTicket& ticket,
                  RealtimeMcpCatalog* catalog) const;
  void Cancel() noexcept;
  void Join() noexcept;
  size_t Pending() const;
  size_t Workers() const;
  size_t Entries() const { return entries_.size(); }
  size_t RetainedBytes() const { return retained_bytes_; }

 private:
  struct Import;
  ObjectReply Event(std::string_view type, io::Json fields);
  ObjectReply Finish(Import& work);
  ObjectReply Revoke(const RealtimeItemTicket& ticket, bool erase);
  void Start(Import& work);
  void Drop(size_t index);
  RealtimeConversation& history_;
  const RealtimeMcpImportCallbacks callbacks_;
  const RealtimeMcpImportLimits limits_;
  std::deque<std::unique_ptr<Import>> entries_;
  size_t retained_bytes_ = 0;
  bool valid_ = false, cancelled_ = false, broken_ = false;
};
}  // namespace q4t::server
