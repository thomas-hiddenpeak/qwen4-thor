#pragma once

#include <map>
#include <string>
#include <utility>

#include "q4t/server/chatkit.h"

namespace q4t::server::chatkit_detail {
// A browser stream numbers only the citations it can display, independently
// for each assistant item and content part in the Responses journal.
class GenerationStream {
 public:
  io::Json Event(const io::Json& event, const std::string& thread,
                 int64_t created);

 private:
  std::map<std::pair<std::string, int64_t>, int64_t> annotation_counts_;
};

// Finish an already-started browser stream with one typed error event.
void FinishStreamError(int fd, const StateAccess& access,
                       const ObjectReply& error, bool allow_retry);
}  // namespace q4t::server::chatkit_detail
