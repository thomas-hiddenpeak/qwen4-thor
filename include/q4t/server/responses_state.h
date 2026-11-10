#pragma once

#include <chrono>
#include <functional>
#include <optional>
#include <string>

#include "q4t/server/client_policy.h"
#include "q4t/server/batch_response_link.h"
#include "q4t/server/object_store.h"
#include "q4t/server/responses_plan.h"

namespace q4t::server {

// Request-local authority. Never derived from user/metadata/opaque item IDs.
struct StateAccess {
  std::string scope;
  std::chrono::steady_clock::time_point deadline;
  std::function<bool()> cancelled;
};

struct ResponseStatePlan {
  std::string id;
  std::string scope;  // Bound at resolution; never retarget a prepared plan.
  bool store = true;
  bool ephemeral = false;  // Background store=false is retained for polling.
  int64_t execution_deadline_at = 0;  // Unix seconds; restart cannot reset it.
  io::Json execution_owner;
  std::optional<BatchResponseLink> batch_parent = {};
  ClientRateDomain rate_domain = ClientRateDomain::kOnline;
  io::Json history;    // Full authorized input for this generation.
  io::Json own_input;  // New input, without inherited context/instructions.
  io::Json input_snapshots;  // Private exact-part bindings, never API items.
  io::Json request;
  std::string conversation_id;
  int64_t conversation_revision = 0;
  io::Json conversation;
  int64_t response_revision = 0;
  bool finished = false;
};

// JSON object order is not identity; exact numbers and array order remain so.
ObjectReply CompareStoredItem(const io::Json& stored, const io::Json& incoming);

// State operations do no model work and retain no request callbacks. Generation
// still must run the full Responses planner after resolving history/media.
class ResponsesState {
 public:
  explicit ResponsesState(ObjectStore& objects) : objects_(objects) {}
  // Shape-check all input before any storage access, resolve owned references,
  // and populate canonical item metadata. No tool execution or generation.
  ObjectReply ResolveItems(const io::Json& input, const StateAccess& access,
                           size_t maximum = 4096,
                           io::Json* input_snapshots = nullptr);
  ObjectReply Resolve(const ResponsesEnvelope& envelope,
                      const StateAccess& access, ResponseStatePlan* plan);
  // Loads authoritative inputs and revision, without rerunning resolution or
  // any tool/model. The result is the internal stored record, never an API
  // body.
  ObjectReply LoadPlan(const std::string& id, const StateAccess& access,
                       ResponseStatePlan* plan,
                       bool restore_conversation = false);
  // Only after the owner process is known gone or its execution owner has
  // explicitly confirmed model/grammar cleanup: reconcile a checked revision.
  // Never use this to race a live execution.
  // No executed work is replayed and no conversation items are appended.
  ObjectReply Interrupt(const std::string& id, const StateAccess& access,
                        std::string_view code, std::string_view message);
  ObjectReply Begin(const io::Json& snapshot, const StateAccess& access,
                    ResponseStatePlan* plan);
  ObjectReply Begin(const io::Json& snapshot, const io::Json& events,
                    const StateAccess& access, ResponseStatePlan* plan);
  ObjectReply Checkpoint(const io::Json& snapshot, const io::Json& events,
                         const StateAccess& access, ResponseStatePlan* plan);
  // The caller supplies actual generated items and original ordered events.
  // A completed stored response is acknowledged only after the atomic receipt.
  ObjectReply Finish(const io::Json& snapshot, const io::Json& events,
                     const StateAccess& access, ResponseStatePlan* plan);
  // Only after a known quota/size rejection, use the active record's reserved
  // space to retain an honest failure without partial output/events. No retry
  // after an unknown commit, no completed response, no conversation append.
  ObjectReply FailWithinReservation(const io::Json& snapshot,
                                    const StateAccess& access,
                                    ResponseStatePlan* plan);
  ObjectReply Retrieve(std::string id, const StateAccess& access);
  // Internal authorized state, including history/events; never send this
  // record directly over HTTP. The API must project the public resource.
  ObjectReply ReadRecord(const std::string& id, const StateAccess& access);
  ObjectReply Erase(std::string id, const StateAccess& access);

 private:
  ObjectReply Get(ObjectKind kind, const std::string& id,
                  const StateAccess& access);
  ObjectStore& objects_;
};

}  // namespace q4t::server
