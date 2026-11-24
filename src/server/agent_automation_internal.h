#pragma once

#include <optional>

#include "q4t/server/agent_automation.h"
#include "q4t/server/application_routes.h"

namespace q4t::server {
class ResponsesJobs;
}

namespace q4t::server::automation_detail {
// Only caller-supplied clock fields are applied. A metadata-only update keeps
// the existing schedule, even if its single occurrence has already completed.
ObjectReply ApplyClock(const io::Json& patch, int64_t now, io::Json* resource);
ObjectReply ProjectTarget(ObjectStore& objects, const StateAccess& access,
                          const ChatPlanConfig& config, const io::Json& target);
// A model can only operate on wakeups created by its own session. Model
// mutations are staged for a joint commit with the call and its result.
struct SessionAutomationChange {
  std::string session;
  std::string call_id;
  std::optional<ObjectRequest> write;
};
ObjectReply List(ObjectStore& objects, const StateAccess& access,
                 const AutomationRoute& route, std::string_view raw,
                 std::string_view creator_session = {});
ObjectReply StillQueued(ObjectStore& objects, const StateAccess& access,
                        const io::Json& data, const std::string& id,
                        bool* queued);
ObjectReply Queue(ObjectStore& objects, const StateAccess& access,
                  const io::Json& record, int64_t now, int64_t occurrence);
ObjectReply Schedule(ObjectStore& objects, const StateAccess& access,
                     const io::Json& record, int64_t now);
ObjectReply Deliver(ObjectStore& objects, ClientAdmission& clients,
                    const ClientIdentity& client, ResponsesJobs& jobs,
                    ChatPlanConfig config, const StateAccess& access,
                    const io::Json& record, int64_t now);
ObjectReply PublicAutomation(const io::Json& data, const std::string& id);
ObjectReply Registry(ObjectStore& objects, const StateAccess& access,
                     const ChatPlanConfig& config, const AutomationRoute& route,
                     std::string_view method, const io::Json& body, int64_t now,
                     SessionAutomationChange* caller = nullptr);
}  // namespace q4t::server::automation_detail
