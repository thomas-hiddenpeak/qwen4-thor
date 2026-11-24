#include "q4t/server/eval_dispatcher.h"

#include <algorithm>
#include <array>

#include "eval_runs.h"

namespace q4t::server {
using namespace std::chrono_literals;
EvalDispatcher::EvalDispatcher(ObjectStore& objects, ClientAdmission& clients)
    : thread_([this, &objects, &clients](std::stop_token stop) {
        Run(objects, clients, stop);
      }) {}
void EvalDispatcher::Wake() {
  {
    std::lock_guard lock(mutex_);
    ++epoch_;
  }
  changed_.notify_one();
}
void EvalDispatcher::RequestStop() {
  thread_.request_stop();
  changed_.notify_all();
}
void EvalDispatcher::Join() {
  if (thread_.joinable()) thread_.join();
}

void EvalDispatcher::Run(ObjectStore& objects, ClientAdmission& clients,
                         std::stop_token stop) {
  struct Cursor {
    std::string scope, after;
  };
  std::array<Cursor, kMaxClients> cursors;
  const auto canceled = [&] { return stop.stop_requested(); };
  while (!canceled()) {
    uint64_t epoch;
    {
      std::lock_guard lock(mutex_);
      epoch = epoch_;
    }
    auto delay = 2000ms;
    try {
      const auto count = std::max<size_t>(1, clients.Size());
      for (size_t i = 0; i < count && !canceled(); ++i) {
        auto& cursor = cursors.at(i);
        const auto owner = clients.RecoveryIdentity(i);
        if (!owner.Allowed()) {
          cursor = {};
          continue;
        }
        auto scope = owner.Scoped() ? "client:" + std::string(owner.Id())
                                    : "local:anonymous";
        if (scope != cursor.scope) cursor = {scope, {}};
        StateAccess access{scope, std::chrono::steady_clock::now() + 8s,
                           canceled};
        ObjectRequest scan;
        scan.scope = scope;
        scan.kind = ObjectKind::kEvalRun;
        scan.operation = ObjectOperation::kList;
        scan.after = cursor.after;
        scan.limit = 4;
        scan.descending = false;
        auto page = objects.Execute(scan, access.deadline, canceled);
        if (!page.ok()) {
          if (page.code == "invalid_object_cursor" || page.http_status == 404)
            cursor.after.clear();
          if (!canceled()) ++errors_;
          continue;
        }
        const auto* rows = page.value.GetArray("data");
        if (!rows) {
          ++errors_;
          continue;
        }
        cursor.after = page.value.GetBool("has_more")
                           ? page.value.GetString("last_id")
                           : "";
        if (!cursor.after.empty()) delay = 25ms;
        for (const auto& row : rows->array) {
          const auto* data = row.Find("data");
          const auto* run = data ? data->Find("resource") : nullptr;
          if (!run) {
            ++errors_;
            continue;
          }
          const auto status = run->GetString("status");
          if (!data->GetBool("deleting") && status != "in_progress" &&
              status != "queued")
            continue;
          auto result = eval_detail::AdvanceRun(objects, access, row);
          if (result.ok())
            delay = 25ms;
          else if (result.http_status != 409 && result.http_status != 404 &&
                   !canceled())
            ++errors_;
        }
      }
    } catch (const std::exception&) {
      ++errors_;
      delay = 2000ms;
    }
    std::unique_lock lock(mutex_);
    changed_.wait_for(lock, stop, delay, [&] { return epoch != epoch_; });
  }
}
}  // namespace q4t::server
