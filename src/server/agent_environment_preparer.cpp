#include "q4t/server/agent_environment_preparer.h"

#include <algorithm>
#include <ctime>

#include "agent_environment_internal.h"
#include "agent_environment_journal.h"
#include "object_protocol.h"

namespace q4t::server {
using namespace object_detail;
using namespace environment_detail;
namespace {
using Clock = std::chrono::steady_clock;
using namespace std::chrono_literals;

std::string ClientScope(const ClientIdentity& identity) {
  return identity.Scoped() ? "client:" + std::string(identity.Id())
                           : "local:anonymous";
}
}  // namespace

ObjectReply CompleteEnvironmentPreparation(ObjectStore& objects,
                                           const StateAccess& access,
                                           const std::string& id) {
  ObjectRequest change;
  change.kind = ObjectKind::kEnvironment;
  change.scope = access.scope;
  change.id = id;
  auto row = objects.Execute(change, access.deadline, access.cancelled);
  if (!row.ok()) return row;
  auto& data = Field(row.value, "data");
  const auto* resource = data.Find("resource");
  if (!resource || resource->GetString("id") != id)
    return Error(503, "environment_record_invalid", "Invalid workspace");
  if (resource->GetString("status") != "pending") return {};
  const auto* receipts = data.GetArray("preparation_sources");
  const auto* sealed = data.Find("sealed_definition");
  const auto* members = data.GetArray("attached_sessions");
  const auto expires = data.GetInt("prepare_before");
  if (!receipts || !sealed || !members || !data.GetArray("vault_ids") ||
      members->array.size() > kMaxAttachedSessions || expires <= 0)
    return Error(503, "environment_record_invalid", "Invalid preparation");
  WorkspaceCopies copies(objects, access, id);
  Json config;
  ObjectReply installed;
  std::string failure;
  if (std::time(nullptr) < expires) {
    installed = OpenDefinition(objects, access, id, *sealed, &config);
    if (installed.ok()) installed = copies.ResumeSources(*receipts);
    if (installed.ok()) installed = InstallWorkspace(id, &config, copies);
    if (!installed.ok()) {
      if (installed.commit_unknown) return installed;
      if (installed.code == "environment_template_invalid") {
        failure = "workspace_definition_invalid";
      } else if (installed.code == "binary_state_invalid" ||
                 installed.code == "environment_file_state_invalid" ||
                 installed.http_status == 404) {
        failure = "workspace_source_invalid";
      } else {
        return installed;
      }
    }
  } else {
    failure = "workspace_preparation_expired";
  }
  if (std::time(nullptr) >= expires) failure = "workspace_preparation_expired";
  const auto now = std::time(nullptr);
  change.operation = ObjectOperation::kPut;
  change.expected_revision = row.value.GetInt("revision");
  const bool attached = !members->array.empty();
  change.ttl_seconds = attached ? 0 : 86400;
  if (!failure.empty()) {
    change.data = Object({{"resource", EnvironmentResource(id, "failed")},
                          {"attached_sessions", *members},
                          {"failed_at", Number(now)},
                          {"failure_code", String(failure)}});
  } else {
    Json snapshot;
    auto secure = SealDefinition(objects, access, id, config, &snapshot);
    if (!secure.ok()) return secure;
    change.mutate_before = expires;
    change.data = Object({{"resource", std::move(installed.value)},
                          {"attached_sessions", *members},
                          {"sealed_definition", std::move(snapshot)},
                          {"vault_ids", Field(data, "vault_ids")},
                          {"prepared_at", Number(now)},
                          {"warm_until", Number(now + 300)}});
    copies.Bind(&change.data);
  }
  if (attached) {
    // The attachment already owns this session. Commit both preparation and
    // connection transitions with the installed files, or one terminal error.
    if (!failure.empty()) {
      AppendEnvironmentStatus(&change.data, "failed");
      auto& entry = Field(change.data, "environment_events").array.back();
      Set(&entry, "code", Field(change.data, "failure_code"));
    } else {
      AppendEnvironmentStatus(&change.data, "ready");
      AppendEnvironmentStatus(&change.data, "connected");
      Set(&Field(change.data, "resource"), "status", String("connected"));
      Set(&change.data, "last_activity_at", Number(now));
    }
  }
  auto committed = objects.Execute(change, access.deadline, access.cancelled);
  copies.Settle(committed);
  if (committed.ok()) copies.ReleaseSources(receipts);
  return committed;
}

EnvironmentPreparer::EnvironmentPreparer(ObjectStore& objects,
                                         ClientAdmission& clients)
    : thread_([this, &objects, &clients](std::stop_token stop) {
        Run(objects, clients, stop);
      }) {}
void EnvironmentPreparer::RequestStop() { thread_.request_stop(); }
void EnvironmentPreparer::Join() {
  if (thread_.joinable()) thread_.join();
}
io::Json EnvironmentPreparer::Health() const {
  return Object({{"completed_attempts", Number(completed_.load())},
                 {"retryable_errors", Number(errors_.load())}});
}
void EnvironmentPreparer::Run(ObjectStore& objects, ClientAdmission& clients,
                              std::stop_token stop) {
  std::string cursor;
  const auto cancelled = [&] { return stop.stop_requested(); };
  while (!cancelled()) {
    auto delay = 1000ms;
    try {
      auto page = objects.NextEnvironmentPreparation(cursor, Clock::now() + 2s,
                                                     cancelled);
      if (page.ok()) {
        cursor = page.value.GetString("after");
        const auto owner = page.value.GetString("owner");
        for (size_t i = 0;
             !cursor.empty() && i < std::max<size_t>(1, clients.Size()); ++i) {
          auto identity = clients.RecoveryIdentity(i);
          if (!identity.Allowed()) continue;
          const auto scope = ClientScope(identity);
          if (Digest(scope) != owner) continue;
          const auto revoked = [&] {
            const auto current = clients.RecoveryIdentity(i);
            return cancelled() || !current.Allowed() ||
                   ClientScope(current) != scope;
          };
          auto done = CompleteEnvironmentPreparation(
              objects, {scope, Clock::now() + 45s, revoked},
              page.value.GetString("id"));
          if (done.ok()) {
            ++completed_;
            delay = 1ms;
          } else if (!revoked() && !done.commit_unknown &&
                     done.code != "object_revision_conflict") {
            ++errors_;
          }
          break;
        }
      } else if (!cancelled()) {
        ++errors_;
      }
    } catch (...) {
      ++errors_;
      // A transient preparation failure leaves the durable job for retry.
    }
    std::unique_lock lock(mutex_);
    changed_.wait_for(lock, stop, delay, [] { return false; });
  }
}
}  // namespace q4t::server
