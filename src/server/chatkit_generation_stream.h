#pragma once

#include <functional>
#include <map>
#include <set>
#include <string>
#include <utility>

#include "q4t/server/chatkit.h"

namespace q4t::server::chatkit_detail {
// A browser stream numbers only the citations it can display, independently
// for each assistant item and content part in the Responses journal.
class GenerationStream {
 public:
  using Emitter = std::function<bool(const io::Json&)>;
  explicit GenerationStream(io::Json previous_workflow = {})
      : workflow_(std::move(previous_workflow)) {}
  bool EmitEvent(const io::Json& event, const std::string& thread,
                 int64_t created, const io::Json* times, const Emitter& emit);

  bool FinishedMessage(const std::string& id) const {
    return finished_messages_.contains(id);
  }

 private:
  io::Json MessageEvent(const io::Json& event, const std::string& thread,
                        int64_t created);

  io::Json workflow_;
  std::set<std::string> reasoning_ids_;
  std::set<std::string> finished_messages_;
  std::string streaming_item_;
  int64_t streaming_index_ = -1;
  std::map<std::pair<std::string, int64_t>, int64_t> annotation_counts_;
};

// Finish an already-started browser stream with one typed error event.
void FinishStreamError(int fd, const StateAccess& access,
                       const ObjectReply& error, bool allow_retry);
}  // namespace q4t::server::chatkit_detail
