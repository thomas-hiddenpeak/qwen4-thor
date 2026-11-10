#include "q4t/server/batch_quota.h"

#include "object_protocol.h"
#include "q4t/server/batch_state.h"
#include "q4t/server/input_contract.h"

namespace q4t::server {
namespace {
using namespace object_detail;
bool Native(const Json& data) {
  return data.GetString("format") == "q4t.batch.v1";
}
bool Integer(const Json& data, const std::string& key, uint64_t low,
             uint64_t high) {
  const auto* value = data.Find(key);
  return value && IsInputInteger(*value) && value->AsInt() >= 0 &&
         static_cast<uint64_t>(value->AsInt()) >= low &&
         static_cast<uint64_t>(value->AsInt()) <= high;
}
bool Metered(const Json& data) {
  const auto model = data.GetString("canonical_model");
  return !model.empty() && model.size() <= 256 &&
         Integer(data, "enqueued_input_tokens", 1, kBatchTokenLimit) &&
         Integer(data, "metered_rows", 1, kBatchRequests) &&
         data.GetInt("metered_rows") == data.GetInt("validated_rows");
}
std::string State(const Json& data) {
  const auto* batch = data.Find("batch");
  return batch ? batch->GetString("status") : "";
}
ObjectReply Invalid() {
  return Error(409, "batch_budget_conflict",
               "Batch budget must match its immutable validation prefix");
}
}  // namespace

BatchQueueCharge InspectBatchQueueCharge(std::string_view kind,
                                         const Json& data) {
  if (kind != "batch" || !Native(data)) return {};
  const auto state = State(data);
  // A corrupt terminal marker cannot silently release an executing owner.
  if (BatchTerminal(state) && Integer(data, "running", 0, 0)) return {};
  BatchQueueCharge charge;
  charge.model = data.GetString("canonical_model");
  if (charge.model.empty())
    if (const auto* batch = data.Find("batch"))
      charge.model = batch->GetString("model");
  if (charge.model.size() > 256) charge.model.clear();
  if (Metered(data)) {
    charge.tokens = data.GetInt("enqueued_input_tokens");
  } else {
    // A newly created, empty validating Batch has not reserved any input.
    // Old unmetered frozen rows are unknown, never zero. An unbound model
    // quarantines only further Batch reservations, not online generation.
    charge.unknown =
        !(state == "validating" && Integer(data, "validated_rows", 0, 0) &&
          !data.Has("enqueued_input_tokens") && !data.Has("metered_rows"));
  }
  return charge;
}

ObjectReply CheckBatchBudgetMutation(const Json& data, const Json* previous) {
  if (!Native(data) && (!previous || !Native(*previous))) return {};
  if (!Native(data)) return Invalid();
  const bool has_budget =
      data.Has("enqueued_input_tokens") || data.Has("metered_rows");
  if (has_budget && !Metered(data)) return Invalid();
  if (!previous) return {};
  if (!Native(*previous)) return Invalid();
  const bool old_budget =
      previous->Has("enqueued_input_tokens") || previous->Has("metered_rows");
  if (!old_budget) return {};
  if (!has_budget || !Metered(*previous) ||
      data.GetString("canonical_model") !=
          previous->GetString("canonical_model"))
    return Invalid();
  const auto* pages = data.GetArray("pages");
  const auto* old_pages = previous->GetArray("pages");
  if (!pages || !old_pages || pages->array.size() < old_pages->array.size())
    return Invalid();
  for (size_t i = 0; i < old_pages->array.size(); ++i)
    if (OutputJson(pages->array[i]) != OutputJson(old_pages->array[i]))
      return Invalid();
  const auto rows = data.GetInt("metered_rows");
  const auto old_rows = previous->GetInt("metered_rows");
  const auto tokens = data.GetInt("enqueued_input_tokens");
  const auto old_tokens = previous->GetInt("enqueued_input_tokens");
  if (rows < old_rows || tokens < old_tokens ||
      ((rows == old_rows) != (tokens == old_tokens)) ||
      (rows > old_rows &&
       (State(data) != "validating" || State(*previous) != "validating")))
    return Invalid();
  if (BatchTerminal(State(*previous)) && State(data) != State(*previous))
    return Invalid();
  if (BatchTerminal(State(data)) &&
      (!Integer(data, "running", 0, 0) || !data.GetBool("resources_released")))
    return Invalid();
  if (!BatchTerminal(State(*previous)) && BatchTerminal(State(data))) {
    const auto& batch = *data.Find("batch");
    const auto* counts = batch.Find("request_counts");
    if (!counts) return Invalid();
    const auto total = counts->GetInt("total", -1);
    if (State(data) == "failed") {
      if (State(*previous) != "validating" || total != 0) return Invalid();
    } else if (total != 0) {
      if (State(*previous) != "finalizing" || total != rows ||
          data.GetInt("next_row") != total ||
          counts->GetInt("completed") + counts->GetInt("failed") != total)
        return Invalid();
    } else if (State(data) == "completed") {
      return Invalid();
    }
  }
  return {};
}

void BatchQueueQuota::Add(const std::string& owner,
                          const BatchQueueCharge& charge) {
  if (!charge.tokens && !charge.unknown) return;
  auto& model = models_[charge.model];
  auto& client = model.clients[owner];
  for (auto* usage : {&model.total, &client}) {
    usage->tokens += charge.tokens;
    usage->unknown += charge.unknown;
  }
}
void BatchQueueQuota::Remove(const std::string& owner,
                             const BatchQueueCharge& charge) {
  if (!charge.tokens && !charge.unknown) return;
  auto model = models_.find(charge.model);
  auto client = model->second.clients.find(owner);
  for (auto* usage : {&model->second.total, &client->second}) {
    usage->tokens -= charge.tokens;
    usage->unknown -= charge.unknown;
  }
  if (client->second.Empty()) model->second.clients.erase(client);
  if (model->second.total.Empty()) models_.erase(model);
}
ObjectReply BatchQueueQuota::Check(const std::string& owner,
                                   std::span<const BatchQueueChange> changes,
                                   uint64_t model_limit,
                                   uint64_t client_model_limit) const {
  struct Projection {
    Usage total, client, old_total, old_client;
  };
  std::map<std::string, Projection> projected;
  const auto get = [&](const std::string& name) -> Projection& {
    auto [found, added] = projected.try_emplace(name);
    if (added) {
      const auto model = models_.find(name);
      if (model != models_.end()) {
        found->second.total = found->second.old_total = model->second.total;
        const auto client = model->second.clients.find(owner);
        if (client != model->second.clients.end())
          found->second.client = found->second.old_client = client->second;
      }
    }
    return found->second;
  };
  for (const auto& change : changes) {
    auto& old = get(change.before.model);
    old.total.tokens -= change.before.tokens;
    old.client.tokens -= change.before.tokens;
    old.total.unknown -= change.before.unknown;
    old.client.unknown -= change.before.unknown;
    auto& next = get(change.after.model);
    next.total.tokens += change.after.tokens;
    next.client.tokens += change.after.tokens;
    next.total.unknown += change.after.unknown;
    next.client.unknown += change.after.unknown;
  }
  const auto global_unknown = get("").total.unknown;
  for (const auto& [name, value] : projected) {
    (void)name;
    if (value.total.tokens > value.old_total.tokens &&
        (global_unknown || value.total.unknown))
      return Error(409, "batch_input_budget_unavailable",
                   "Existing Batch input budget needs reconciliation");
    if ((value.total.tokens > model_limit &&
         value.total.tokens > value.old_total.tokens) ||
        (value.client.tokens > client_model_limit &&
         value.client.tokens > value.old_client.tokens))
      return Error(429, "batch_enqueued_token_limit_exceeded",
                   "Batch input exceeds the model or client queue token limit");
  }
  return {};
}
}  // namespace q4t::server
