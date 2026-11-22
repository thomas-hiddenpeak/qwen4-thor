// Owns the Live projection and client continuations for one native Response.
#pragma once

#include <map>
#include <optional>

#include "q4t/server/live_session.h"

namespace q4t::server {
class LiveResponsesTurn {
 public:
  LiveResponsesTurn(std::string delegation, std::string client_event,
                    LiveEventSink emit);
  // Only called with events accepted by NativeResponsesStream on the session
  // thread. An output_item.done, never an arguments delta, registers a call.
  ObjectReply Event(const io::Json& native, uint64_t offset_ms);
  ObjectReply Result(const io::Json& item);
  ObjectReply RequestContinuation(std::string client_event);
  // The caller must join the actual worker before supplying its terminal.
  // A successful stream receipt alone does not authorize another invocation.
  ObjectReply Finish(const io::Json& response);
  void Abort() noexcept;
  bool WaitingForResults() const;
  bool HasCalls() const { return !calls_.empty(); }
  bool Finished() const { return finished_; }
  std::optional<std::string> TakeContinuation();
  // Returns actual terminal output, followed by matching client results in
  // output-index order. No credentials or frontend-only instructions enter it.
  ObjectReply AppendHistory(io::Json* input) const;
  const std::string& ResponseId() const { return response_id_; }

 private:
  struct Call {
    std::string expected;
    size_t index;
    io::Json result;
  };
  ObjectReply Emit(io::Json event, bool correlate = true);
  ObjectReply Register(const io::Json& item, size_t index);
  ObjectReply Missing() const;
  const std::string delegation_, client_event_;
  const LiveEventSink emit_;
  std::string response_id_, terminal_;
  std::map<size_t, io::Json> items_;
  std::map<std::string, Call> calls_;
  std::optional<std::string> continuation_;
  size_t bytes_ = 0;
  bool failed_ = false, finished_ = false, consumed_ = false;
};
}  // namespace q4t::server
