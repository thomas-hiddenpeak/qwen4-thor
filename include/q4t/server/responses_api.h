#pragma once

#include "q4t/server/application_routes.h"
#include "q4t/server/conversation_state.h"

namespace q4t::server {

struct ResponsesReadPlan {
  enum class Operation { kRetrieve, kInputItems, kDelete };
  Operation operation = Operation::kRetrieve;
  std::string id;
  ItemViewOptions view;
  bool stream = false;
  bool obfuscation = true;
  int64_t starting_after = -1;
};

struct ResponsesReplay {
  std::string wire;
  int64_t last_sequence = -1;
  bool terminal = false;
};

// Native read/delete protocol, independently testable without a model.
ObjectReply PlanResponsesRead(std::string_view method, std::string_view target,
                              std::string_view body, ResponsesReadPlan* plan);
ObjectReply ReadResponsesState(ObjectStore& objects,
                               const ResponsesReadPlan& plan,
                               const StateAccess& access,
                               ResponsesReplay* replay);
// Follow retained events until a terminal or this subscriber's own deadline.
// Disconnect never cancels or rewrites the underlying generation.
void ServeResponsesState(int fd, ObjectStore& objects, std::string_view method,
                         std::string_view target, std::string_view body,
                         const StateAccess& access);

}  // namespace q4t::server
