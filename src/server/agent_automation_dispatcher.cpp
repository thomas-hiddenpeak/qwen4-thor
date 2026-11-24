#include "agent_automation_dispatcher.h"

#include <algorithm>
#include <array>
#include <chrono>
#include <ctime>

#include "agent_automation_internal.h"
#include "object_protocol.h"

namespace q4t::server {
namespace {
using namespace object_detail;
using Clock = std::chrono::steady_clock;
using namespace std::chrono_literals;

struct Cursor {
  std::string after;
  int64_t next_epoch = 0;
  int64_t earliest = 0;
};
struct Tenant {
  std::string scope;
  Cursor schedules, invocations;
};

bool Page(ObjectStore& objects, ClientAdmission& clients, ResponsesJobs& jobs,
          const ClientIdentity& client, const ChatPlanConfig& config,
          const StateAccess& access, ObjectKind kind, int64_t now,
          Cursor* cursor, bool* queued) {
  if (cursor->next_epoch > now) return true;
  if (cursor->after.empty()) cursor->earliest = now + 60;
  ObjectRequest request;
  request.kind = kind;
  request.operation = ObjectOperation::kList;
  request.scope = access.scope;
  request.after = cursor->after;
  request.limit = 8;
  request.descending = false;
  auto page = objects.Execute(request, access.deadline, access.cancelled);
  if (!page.ok()) {
    // The cursor can be deleted between pages.
    if (page.code == "invalid_object_cursor" || page.http_status == 404)
      cursor->after.clear();
    cursor->next_epoch = now + 3;
    return false;
  }
  const auto* records = page.value.GetArray("data");
  if (!records) {
    cursor->next_epoch = now + 3;
    return false;
  }
  bool ok = true;
  for (const auto& row : records->array) {
    if (access.cancelled()) return true;
    const auto* data = row.Find("data");
    const auto* resource = data ? data->Find("resource") : nullptr;
    if (!resource) {
      ok = false;
      continue;
    }
    const auto status = resource->GetString("status");
    ObjectReply result;
    if (kind == ObjectKind::kAutomation && status == "active") {
      const auto at = resource->GetString("type") == "once"
                          ? resource->GetInt("run_at")
                          : (now / 60 + 1) * 60;
      cursor->earliest = std::min(cursor->earliest, std::max(now + 1, at));
      result = automation_detail::Schedule(objects, access, row, now);
      if (result.ok() &&
          result.value.GetString("object") == "agent.automation.invocation") {
        *queued = true;
        cursor->earliest = now + 1;
      }
    } else if (kind == ObjectKind::kAutomationInvocation &&
               status == "queued") {
      cursor->earliest = std::min(
          cursor->earliest, std::max(now + 1, data->GetInt("next_attempt")));
      result = automation_detail::Deliver(objects, clients, client, jobs,
                                          config, access, row, now);
    }
    if (!result.ok() && result.http_status != 409) ok = false;
  }
  const auto last = page.value.GetString("last_id");
  if (page.value.GetBool("has_more") && !last.empty() &&
      last != cursor->after) {
    cursor->after = last;
    cursor->next_epoch = now;  // Rotate across owners before the next page.
  } else {
    cursor->after.clear();
    cursor->next_epoch = cursor->earliest;
  }
  return ok;
}
}  // namespace

AutomationDispatcher::AutomationDispatcher(ObjectStore& objects,
                                           ClientAdmission& clients,
                                           ResponsesJobs& jobs,
                                           ChatPlanConfig config)
    : thread_([this, &objects, &clients, &jobs,
               config = std::move(config)](std::stop_token stop) {
        Run(objects, clients, jobs, config, stop);
      }) {}

void AutomationDispatcher::Wake() {
  {
    std::lock_guard lock(mutex_);
    ++epoch_;
  }
  changed_.notify_all();
}
void AutomationDispatcher::RequestStop() { thread_.request_stop(); }
void AutomationDispatcher::Join() {
  if (thread_.joinable()) thread_.join();
}

void AutomationDispatcher::Run(ObjectStore& objects, ClientAdmission& clients,
                               ResponsesJobs& jobs,
                               const ChatPlanConfig& config,
                               std::stop_token stop) {
  std::array<Tenant, kMaxClients> tenants;
  uint64_t epoch = 0;
  const auto cancelled = [&] { return stop.stop_requested(); };
  while (!cancelled()) {
    auto delay = 60s;
    try {
      bool wake;
      {
        std::lock_guard lock(mutex_);
        wake = epoch != epoch_;
        epoch = epoch_;
      }
      const int64_t now = std::time(nullptr);
      const size_t count = std::max<size_t>(1, clients.Size());
      for (size_t i = 0; i < count && !cancelled(); ++i) {
        const auto identity = clients.RecoveryIdentity(i);
        auto& tenant = tenants.at(i);
        if (!identity.Allowed()) {
          tenant = {};
          continue;
        }
        const auto scope = identity.Scoped()
                               ? "client:" + std::string(identity.Id())
                               : "local:anonymous";
        if (tenant.scope != scope) tenant = {scope, {}, {}};
        if (wake) {
          tenant.schedules.next_epoch = 0;
          tenant.invocations.next_epoch = 0;
        }
        for (const auto kind :
             {ObjectKind::kAutomation, ObjectKind::kAutomationInvocation}) {
          auto& cursor = kind == ObjectKind::kAutomation ? tenant.schedules
                                                         : tenant.invocations;
          StateAccess access{scope, Clock::now() + 8s, cancelled};
          bool queued = false;
          if (!Page(objects, clients, jobs, identity, config, access, kind, now,
                    &cursor, &queued))
            ++errors_;
          if (queued) tenant.invocations.next_epoch = 0;
          delay = std::min(delay, std::chrono::seconds(std::max<int64_t>(
                                      0, cursor.next_epoch - now)));
        }
      }
    } catch (const std::exception&) {
      ++errors_;
      delay = 3s;
    }
    auto next = std::max(
        std::chrono::duration_cast<std::chrono::milliseconds>(delay), 25ms);
    std::unique_lock lock(mutex_);
    changed_.wait_for(lock, stop, next, [&] { return epoch != epoch_; });
  }
}
}  // namespace q4t::server
