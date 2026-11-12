#include "q4t/server/client_rate_state.h"

#include <algorithm>
#include <charconv>
#include <initializer_list>
#include <set>

#include "q4t/io/json.h"

namespace q4t::server {
namespace {
bool Fields(const io::Json& value,
            std::initializer_list<std::string_view> fields) {
  if (!value.IsObject() || value.object.size() != fields.size()) return false;
  for (const auto& [key, unused] : value.object) {
    (void)unused;
    if (std::find(fields.begin(), fields.end(), key) == fields.end())
      return false;
  }
  return true;
}
template <typename T>
bool Decimal(const io::Json& value, const char* key, T* output) {
  const auto* field = value.Find(key);
  if (!field || !field->IsString() || field->str.empty() ||
      field->str.size() > 20 ||
      (field->str.size() > 1 && field->str[0] == '0') ||
      field->str.find_first_not_of("0123456789") != std::string::npos)
    return false;
  const auto end = field->str.data() + field->str.size();
  const auto result = std::from_chars(field->str.data(), end, *output);
  return result.ec == std::errc() && result.ptr == end;
}
bool ValidWindow(bool started, int64_t window, const ClientRateUsage& usage,
                 int64_t captured) {
  return window >= 0 && window <= captured && !usage.reserved &&
         !usage.window_remaining_ms && usage.requests <= usage.admitted &&
         usage.tokens <= usage.settled_tokens &&
         (started || !(window || usage.requests || usage.tokens));
}
bool BatchUsed(const ClientRateRecord& record) {
  const auto& usage = record.batch_usage;
  return record.batch_window_started || record.batch_window_ns ||
         usage.requests || usage.tokens || usage.rpm_rejected ||
         usage.tpm_rejected || usage.admitted || usage.settled_tokens;
}
std::string Window(bool started, int64_t window, const ClientRateUsage& usage) {
  std::string out = R"("window_started":)" +
                    std::string(started ? "true" : "false") +
                    R"(,"window_ns":")" + std::to_string(window) + '"';
  const auto number = [&](const char* key, uint64_t value) {
    out += std::string(",\"") + key + "\":\"" + std::to_string(value) + '"';
  };
  number("requests", usage.requests);
  number("tokens", usage.tokens);
  number("rpm_rejected", usage.rpm_rejected);
  number("tpm_rejected", usage.tpm_rejected);
  number("admitted", usage.admitted);
  number("settled_tokens", usage.settled_tokens);
  return out;
}
bool ParseWindow(const io::Json& entry, bool* started, int64_t* window,
                 ClientRateUsage* usage) {
  const auto* flag = entry.Find("window_started");
  if (!flag || !flag->IsBool() || !Decimal(entry, "window_ns", window) ||
      !Decimal(entry, "requests", &usage->requests) ||
      !Decimal(entry, "tokens", &usage->tokens) ||
      !Decimal(entry, "rpm_rejected", &usage->rpm_rejected) ||
      !Decimal(entry, "tpm_rejected", &usage->tpm_rejected) ||
      !Decimal(entry, "admitted", &usage->admitted) ||
      !Decimal(entry, "settled_tokens", &usage->settled_tokens))
    return false;
  *started = flag->boolean;
  return true;
}
}  // namespace

bool ValidClientRateState(const ClientRateState& state) {
  if (state.captured_ns <= 0 || state.policy_revision > kMaxClientRevision ||
      state.policy_sha256.size() != 64 ||
      state.policy_sha256.find_first_not_of("0123456789abcdef") !=
          std::string::npos ||
      state.clients.empty() || state.clients.size() > kMaxClients)
    return false;
  std::set<std::string> ids;
  for (const auto& client : state.clients) {
    if (!ValidClientId(client.id) || !ids.insert(client.id).second ||
        !ValidWindow(client.window_started, client.window_ns, client.usage,
                     state.captured_ns) ||
        !ValidWindow(client.batch_window_started, client.batch_window_ns,
                     client.batch_usage, state.captured_ns))
      return false;
  }
  return true;
}

std::string EncodeClientRateState(const ClientRateState& state) {
  if (!ValidClientRateState(state)) return {};
  const bool batch =
      std::any_of(state.clients.begin(), state.clients.end(), BatchUsed);
  std::string out =
      "{\"version\":" + std::string(batch ? "2" : "1") + R"(,"captured_ns":")" +
      std::to_string(state.captured_ns) + R"(","policy_revision":")" +
      std::to_string(state.policy_revision) + R"(","policy_sha256":")" +
      state.policy_sha256 + R"(","clients":[)";
  for (size_t i = 0; i < state.clients.size(); ++i) {
    if (i) out += ',';
    const auto& client = state.clients[i];
    out += R"({"id":")" + client.id + "\"," +
           Window(client.window_started, client.window_ns, client.usage);
    if (batch)
      out += ",\"batch\":{" +
             Window(client.batch_window_started, client.batch_window_ns,
                    client.batch_usage) +
             '}';
    out += '}';
  }
  out += "]}";
  return out.size() <= kClientRateStateBytes ? out : std::string();
}

Status ParseClientRateState(std::string_view raw, ClientRateState* output) {
  const auto invalid = [] { return Status::Fail("invalid client rate state"); };
  if (!output || raw.empty() || raw.size() > kClientRateStateBytes)
    return invalid();
  io::Json value;
  const io::JsonParseLimits limits{.max_depth = 5,
                                   .max_values = 4096,
                                   .max_string_bytes = kClientRateStateBytes,
                                   .reject_duplicate_keys = true};
  if (!io::ParseJson(std::string(raw), &value, true, limits) ||
      !Fields(value, {"version", "captured_ns", "policy_revision",
                      "policy_sha256", "clients"}))
    return invalid();
  const auto* version = value.Find("version");
  const auto* clients = value.Find("clients");
  ClientRateState result;
  if (!version || !version->IsNumber() ||
      (version->str != "1" && version->str != "2") || !clients ||
      !clients->IsArray() || clients->array.size() > kMaxClients ||
      !Decimal(value, "captured_ns", &result.captured_ns) ||
      !Decimal(value, "policy_revision", &result.policy_revision))
    return invalid();
  result.policy_sha256 = value.GetString("policy_sha256");
  const bool batch = version->str == "2";
  for (const auto& entry : clients->array) {
    if (!(batch
              ? Fields(entry, {"id", "window_started", "window_ns", "requests",
                               "tokens", "rpm_rejected", "tpm_rejected",
                               "admitted", "settled_tokens", "batch"})
              : Fields(entry, {"id", "window_started", "window_ns", "requests",
                               "tokens", "rpm_rejected", "tpm_rejected",
                               "admitted", "settled_tokens"})))
      return invalid();
    ClientRateRecord record;
    record.id = entry.GetString("id");
    if (!ParseWindow(entry, &record.window_started, &record.window_ns,
                     &record.usage))
      return invalid();
    if (batch) {
      const auto* window = entry.Find("batch");
      if (!window ||
          !Fields(*window, {"window_started", "window_ns", "requests", "tokens",
                            "rpm_rejected", "tpm_rejected", "admitted",
                            "settled_tokens"}) ||
          !ParseWindow(*window, &record.batch_window_started,
                       &record.batch_window_ns, &record.batch_usage))
        return invalid();
    }
    result.clients.push_back(std::move(record));
  }
  if (!ValidClientRateState(result)) return invalid();
  *output = std::move(result);
  return Status();
}

}  // namespace q4t::server
