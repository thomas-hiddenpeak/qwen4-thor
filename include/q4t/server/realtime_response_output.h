// Realtime text/function events from the shared parser's actual model deltas.
#pragma once

#include <functional>
#include <optional>
#include <vector>

#include "q4t/server/actual_usage.h"
#include "q4t/server/chat_response_parser.h"
#include "q4t/server/object_store.h"

namespace q4t::server {
struct RealtimeResponseUsage {
  ActualUsage total;
  // Missing categories remain unknown, not fabricated zeros or text tokens.
  std::optional<uint64_t> input_text, input_audio, input_image;
  std::optional<uint64_t> output_text, output_audio;
};
struct RealtimeResponseEnd {
  enum class State { kCompleted, kIncomplete, kCancelled, kFailed };
  State state = State::kCompleted;
  std::string reason, error_code, error_message;
  std::optional<RealtimeResponseUsage> usage;
  // Completed/incomplete generation must match the actual finite Chat message.
  // Cancelled/failed execution has no invented successful final message.
  const io::Json* message = nullptr;
};
struct RealtimeResponseOutputOptions {
  std::string response_id, conversation_id;
  bool in_conversation = true;
  io::Json metadata;
  size_t maximum_bytes = 1ULL << 20, maximum_items = 128;
};
class RealtimeResponseOutput {
 public:
  using EventSink = std::function<ObjectReply(const io::Json&)>;
  // The session owner reserves/updates the actual item before publication.
  // On begin, value.previous_item_id may supply the actual history predecessor.
  using ItemSink =
      std::function<ObjectReply(const io::Json&, bool begin, bool terminal)>;
  using IdFactory = std::function<std::string(std::string_view prefix)>;
  RealtimeResponseOutput(RealtimeResponseOutputOptions options, EventSink emit,
                         ItemSink item, IdFactory ids);
  ObjectReply Begin();
  ObjectReply Append(const ResponseDelta& delta);
  ObjectReply Finish(const RealtimeResponseEnd& end);
  bool Terminal() const { return terminal_; }
  bool UsageAvailable() const { return usage_available_; }
  const ObjectReply& Error() const { return error_; }
  io::Json Output() const;

 private:
  struct Item {
    io::Json value;
    std::string previous;
    std::optional<size_t> call_index;
  };
  ObjectReply Fail(std::string_view code, std::string_view message);
  bool AcceptSink(ObjectReply reply);
  ObjectReply Emit(std::string_view type, io::Json fields);
  ObjectReply AddItem(io::Json item, std::optional<size_t> call_index,
                      size_t* output);
  io::Json Response(std::string_view status) const;
  io::Json Position(size_t index, bool content) const;
  bool Matches(const io::Json& message, bool truncated) const;
  RealtimeResponseOutputOptions options_;
  EventSink emit_;
  ItemSink item_;
  IdFactory ids_;
  std::vector<Item> items_;
  size_t bytes_ = 0;
  bool begun_ = false, terminal_ = false, sink_failed_ = false;
  bool usage_available_ = false;
  ObjectReply error_;
};
}  // namespace q4t::server
