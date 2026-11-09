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
    const auto& usage = client.usage;
    if (!ValidClientId(client.id) || !ids.insert(client.id).second ||
        client.window_ns < 0 || client.window_ns > state.captured_ns ||
        usage.reserved || usage.window_remaining_ms ||
        usage.requests > usage.admitted ||
        usage.tokens > usage.settled_tokens ||
        (!client.window_started &&
         (client.window_ns || usage.requests || usage.tokens)))
      return false;
  }
  return true;
}

std::string EncodeClientRateState(const ClientRateState& state) {
  if (!ValidClientRateState(state)) return {};
  std::string out =
      R"({"version":1,"captured_ns":")" + std::to_string(state.captured_ns) +
      R"(","policy_revision":")" + std::to_string(state.policy_revision) +
      R"(","policy_sha256":")" + state.policy_sha256 + R"(","clients":[)";
  for (size_t i = 0; i < state.clients.size(); ++i) {
    if (i) out += ',';
    const auto& client = state.clients[i];
    out += R"({"id":")" + client.id + R"(","window_started":)" +
           (client.window_started ? "true" : "false") + R"(,"window_ns":")" +
           std::to_string(client.window_ns) + '"';
    const auto number = [&](const char* key, uint64_t value) {
      out += std::string(",\"") + key + "\":\"" + std::to_string(value) + '"';
    };
    number("requests", client.usage.requests);
    number("tokens", client.usage.tokens);
    number("rpm_rejected", client.usage.rpm_rejected);
    number("tpm_rejected", client.usage.tpm_rejected);
    number("admitted", client.usage.admitted);
    number("settled_tokens", client.usage.settled_tokens);
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
  const io::JsonParseLimits limits{.max_depth = 4,
                                   .max_values = 2048,
                                   .max_string_bytes = kClientRateStateBytes,
                                   .reject_duplicate_keys = true};
  if (!io::ParseJson(std::string(raw), &value, true, limits) ||
      !Fields(value, {"version", "captured_ns", "policy_revision",
                      "policy_sha256", "clients"}))
    return invalid();
  const auto* version = value.Find("version");
  const auto* clients = value.Find("clients");
  ClientRateState result;
  if (!version || !version->IsNumber() || version->str != "1" || !clients ||
      !clients->IsArray() || clients->array.size() > kMaxClients ||
      !Decimal(value, "captured_ns", &result.captured_ns) ||
      !Decimal(value, "policy_revision", &result.policy_revision))
    return invalid();
  result.policy_sha256 = value.GetString("policy_sha256");
  for (const auto& entry : clients->array) {
    if (!Fields(entry,
                {"id", "window_started", "window_ns", "requests", "tokens",
                 "rpm_rejected", "tpm_rejected", "admitted", "settled_tokens"}))
      return invalid();
    ClientRateRecord record;
    const auto* started = entry.Find("window_started");
    record.id = entry.GetString("id");
    if (!started || !started->IsBool() ||
        !Decimal(entry, "window_ns", &record.window_ns) ||
        !Decimal(entry, "requests", &record.usage.requests) ||
        !Decimal(entry, "tokens", &record.usage.tokens) ||
        !Decimal(entry, "rpm_rejected", &record.usage.rpm_rejected) ||
        !Decimal(entry, "tpm_rejected", &record.usage.tpm_rejected) ||
        !Decimal(entry, "admitted", &record.usage.admitted) ||
        !Decimal(entry, "settled_tokens", &record.usage.settled_tokens))
      return invalid();
    record.window_started = started->boolean;
    result.clients.push_back(std::move(record));
  }
  if (!ValidClientRateState(result)) return invalid();
  *output = std::move(result);
  return Status();
}

}  // namespace q4t::server
