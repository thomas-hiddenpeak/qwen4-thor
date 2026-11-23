#pragma once

#include <cstdint>
#include <map>
#include <string>
#include <string_view>
#include <vector>

#include "q4t/io/json.h"
#include "q4t/status.h"

namespace q4t::server {

// Owned by one Responses execution. The GPU executor claims a turn and
// supplies its actual result; a queued or interrupted turn has no answer.
class ResponsesAgentTree {
 public:
  explicit ResponsesAgentTree(uint64_t capacity) : capacity_(capacity) {}

  static const io::Json& ModelTools();
  static std::string_view ActionName(std::string_view model_name);

  struct Turn {
    std::string name;
    uint64_t generation = 0;
    io::Json messages;
  };

  Status Act(std::string_view caller, std::string_view action,
             const io::Json& arguments, io::Json* result);
  bool Claim(const std::string& name, Turn* turn);
  Status RecordExchange(Turn* turn, const io::Json& messages);
  size_t StoredText() const { return stored_text_; }
  const io::Json& RootMessages() const { return root_messages_; }
  std::string FirstQueued() const;
  std::vector<std::string> QueuedNames() const;
  bool Queued(const std::string& name) const;
  Status Finish(const Turn& turn, std::string answer, bool completed);

 private:
  enum class State { kQueued, kRunning, kCompleted, kInterrupted, kIncomplete };
  struct Agent {
    State state = State::kQueued;
    std::string parent;
    io::Json messages;
    uint64_t generation = 0;
    size_t delivered = 0;
  };
  static bool Active(const Agent& agent);
  static io::Json Describe(const std::string& name, const Agent& agent);
  bool HasSlot() const;
  std::map<std::string, Agent> agents_;
  uint64_t capacity_;
  size_t stored_text_ = 0;
  io::Json root_messages_;
};

}  // namespace q4t::server
