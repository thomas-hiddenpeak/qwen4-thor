// Session-local inputs shared by explicit Decisions and transcript triggers.
#pragma once

#include "q4t/server/live_session.h"

namespace q4t::server {
class LiveDecisionItems {
 public:
  // Called on the session thread with a wire-validated command.
  ObjectReply Apply(const io::Json& event, const LiveEventSink& emit);
  // Each evaluation owns its snapshot, including items deleted subsequently.
  io::Json Snapshot() const;
  ObjectReply Restore(const io::Json& items);

 private:
  ObjectReply Insert(const io::Json& item, const std::string& id);
  struct Entry {
    io::Json item;
    size_t bytes;
  };
  std::vector<Entry> items_;
  size_t bytes_ = 0;
};
}  // namespace q4t::server
