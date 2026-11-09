#include "q4t/server/client_request.h"

#include <sys/socket.h>
#include <sys/stat.h>
#include <unistd.h>

#include <barrier>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <future>
#include <stdexcept>
#include <thread>

#include "q4t/server/chat_plan.h"
#include "q4t/server/client_policy_admin.h"
#include "q4t/server/client_rate_state.h"
#include "q4t/server/file_limits.h"
#include "q4t/server/http_response.h"
#include "q4t/server/lifetime.h"
#include "q4t/server/request_control.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using q4t::io::Json;
const std::string kSecret(64, 'a');
const auto kDeadline = std::chrono::steady_clock::time_point::max();

std::string Entry(const std::string& id) {
  return R"({"id":")" + id +
         R"(","max_inflight":2,"max_control_inflight":1,)"
         R"("max_prompt_tokens":64,"max_output_tokens":16,)"
         R"("max_body_bytes":1024})";
}
std::string Policy(const std::string& entries = Entry("alpha") + ',' +
                                                Entry("beta")) {
  return R"({"version":1,"gateway_token":")" + kSecret + R"(","clients":[)" +
         entries + "]}";
}
bool Configure(ClientAdmission* admission) {
  GatewayClientPolicy policy;
  return ParseGatewayClientPolicy(Policy(), &policy) &&
         admission->Configure(std::move(policy));
}
GatewayClientPolicy RatePolicy(uint32_t rpm, uint32_t tpm, bool fair = true) {
  GatewayClientPolicy policy;
  if (!ParseGatewayClientPolicy(Policy(), &policy))
    throw std::runtime_error("rate policy fixture");
  policy.version = 2;
  policy.fair_queue = fair;
  for (auto& client : policy.clients) {
    client.requests_per_minute = rpm;
    client.tokens_per_minute = tpm;
  }
  return policy;
}
std::string Replace(std::string value, const std::string& old,
                    const std::string& replacement) {
  const auto pos = value.find(old);
  if (pos == value.npos) throw std::runtime_error("invalid test replacement");
  value.replace(pos, old.size(), replacement);
  return value;
}
std::string DynamicRaw(uint32_t rpm, uint32_t tpm, uint64_t revision = 0) {
  auto raw = Replace(Policy(), "\"version\":1",
                     "\"version\":3,\"fair_queue\":true,\"revision\":" +
                         std::to_string(revision));
  const std::string field = "\"max_body_bytes\":1024";
  size_t at = 0;
  while ((at = raw.find(field, at)) != raw.npos) {
    at += field.size();
    const auto rates = ",\"requests_per_minute\":" + std::to_string(rpm) +
                       ",\"tokens_per_minute\":" + std::to_string(tpm);
    raw.insert(at, rates);
    at += rates.size();
  }
  return raw;
}
GatewayClientPolicy DynamicPolicy(uint32_t rpm, uint32_t tpm,
                                  uint64_t revision = 0) {
  GatewayClientPolicy policy;
  if (!ParseGatewayClientPolicy(DynamicRaw(rpm, tpm, revision), &policy))
    throw std::runtime_error("dynamic policy fixture");
  return policy;
}
std::string FilePolicyRaw(uint32_t bytes = kFileWireBytes,
                          uint64_t revision = 0) {
  auto raw =
      Replace(DynamicRaw(3, 100, revision), "\"version\":3", "\"version\":4");
  const std::string field = "\"max_body_bytes\":1024";
  size_t at = 0;
  while ((at = raw.find(field, at)) != raw.npos) {
    at += field.size();
    const auto extra = ",\"max_file_body_bytes\":" + std::to_string(bytes);
    raw.insert(at, extra);
    at += extra.size();
  }
  return raw;
}
GatewayClientPolicy FilePolicy(uint32_t bytes = kFileWireBytes,
                               uint64_t revision = 0) {
  GatewayClientPolicy policy;
  if (!ParseGatewayClientPolicy(FilePolicyRaw(bytes, revision), &policy))
    throw std::runtime_error("file policy fixture");
  return policy;
}
struct Files {
  std::filesystem::path path;
  Files() {
    const auto* env = std::getenv("Q4T_TEST_OUTPUT_DIR");
    const auto base =
        std::filesystem::path(env ? env : ".q4t-work/client-policy-host");
    std::filesystem::create_directories(base);
    std::string name = (base / "private-XXXXXX").string();
    if (!::mkdtemp(name.data())) throw std::runtime_error("test directory");
    path = name;
  }
  ~Files() { std::filesystem::remove_all(path); }
  void Write(const std::string& name, const std::string& bytes,
             int mode = 0600) {
    std::ofstream(path / name, std::ios::binary) << bytes;
    ::chmod((path / name).c_str(), mode);
  }
};
struct ReadResult {
  bool ok = false;
  int status = 0;
  bool admitted = false;
  bool scoped = false;
  size_t active = 0;
  size_t bytes = 0;
  std::string body;
  HttpRequestHeaders headers;
};
ReadResult Read(ClientAdmission& clients, const std::string& wire) {
  int sockets[2];
  if (::socketpair(AF_UNIX, SOCK_STREAM, 0, sockets) != 0)
    throw std::runtime_error("socketpair");
  ScopeExit close([&] {
    ::close(sockets[0]);
    ::close(sockets[1]);
  });
  if (::send(sockets[0], wire.data(), wire.size(), MSG_NOSIGNAL) !=
      static_cast<ssize_t>(wire.size()))
    throw std::runtime_error("fixture send");
  ::shutdown(sockets[0], SHUT_WR);
  ReadResult result;
  std::string method, path;
  HttpBodyBudget budget(kAggregateChatBodyBytes);
  HttpBodyLease body(budget);
  ClientLease lease;
  ClientIdentity identity;
  result.ok = ReadHttpRequest(sockets[1], &method, &path, &result.body, 1000,
                              &body, &result.status, &result.headers, [&] {
                                result.admitted = true;
                                return AdmitClientRequest(
                                    method, path, &result.headers, clients,
                                    &lease, &identity, &result.status);
                              });
  result.scoped = identity.Scoped();
  result.active = clients.Enabled() ? clients.Usage(0).data_active : 0;
  result.bytes = budget.Used();
  return result;
}
std::string Headers(const std::string& id = "alpha") {
  return "X-Q4T-Client-Id: " + id + "\r\nX-Q4T-Gateway-Token: " + kSecret +
         "\r\n";
}
std::string Wire(const std::string& headers,
                 const std::string& path = "/v1/chat/completions",
                 const std::string& method = "POST") {
  return method + ' ' + path + " HTTP/1.1\r\nHost: local\r\n" + headers +
         "Content-Length: 2\r\n\r\n{}";
}
}  // namespace

Q4T_TEST(client_file_policy_explicit_schema_and_legacy_limits) {
  GatewayClientPolicy parsed;
  Q4T_CHECK(ParseGatewayClientPolicy(FilePolicyRaw(), &parsed));
  Q4T_CHECK(parsed.version == 4 &&
            parsed.clients[0].max_file_body_bytes == kFileWireBytes &&
            parsed.clients[0].max_body_bytes == 1024);
  const auto previous = parsed.sha256;
  for (const auto& bad :
       {Replace(FilePolicyRaw(), "\"version\":4", "\"version\":3"),
        Replace(DynamicRaw(0, 0), "\"version\":3", "\"version\":4"),
        FilePolicyRaw(1023), FilePolicyRaw(kFileWireBytes + 1),
        Replace(FilePolicyRaw(2048), "\"max_file_body_bytes\":2048",
                "\"max_file_body_bytes\":true")}) {
    Q4T_CHECK(!ParseGatewayClientPolicy(bad, &parsed));
    Q4T_CHECK(parsed.sha256 == previous);
  }
  for (const auto& raw : {Policy(), DynamicRaw(0, 0)}) {
    Q4T_CHECK(ParseGatewayClientPolicy(raw, &parsed));
    Q4T_CHECK(parsed.clients[0].max_file_body_bytes == 0);
  }
  auto invalid = DynamicPolicy(0, 0);
  invalid.clients[0].max_file_body_bytes = 2048;
  Q4T_CHECK(ClientPolicyDigest(invalid).empty());
  return true;
}

Q4T_TEST(client_file_admission_separates_json_and_exact_upload_route) {
  ClientAdmission current, old;
  Q4T_CHECK(current.Configure(FilePolicy()));
  Q4T_CHECK(old.Configure(DynamicPolicy(3, 100)));
  for (auto* admission : {&current, &old}) {
    for (const auto* path : {"/v1/files", "/v1/files-extra", "/v1/responses",
                             "/v1/files/file_x/content"}) {
      HttpRequestHeaders headers;
      headers.client_id = "alpha";
      headers.gateway_token = kSecret;
      headers.content_length = 17ULL << 20;
      ClientLease lease;
      ClientIdentity identity;
      int status = 0;
      const bool allowed = AdmitClientRequest(
          "POST", path, &headers, *admission, &lease, &identity, &status);
      const bool upload =
          admission == &current && std::string_view(path) == "/v1/files";
      Q4T_CHECK(allowed == upload);
      Q4T_CHECK(headers.body_limit == (upload ? kFileWireBytes : 1024));
      if (!allowed) Q4T_CHECK(status == 413);
    }
  }
  Q4T_CHECK(current.Usage(0).data_active == 0 &&
            current.RateUsage(0).requests == 0);
  return true;
}

Q4T_TEST(client_upload_parts_use_file_limit_and_cancel_reserved_lane) {
  ClientAdmission current, old;
  Q4T_CHECK(current.Configure(FilePolicy()));
  Q4T_CHECK(old.Configure(DynamicPolicy(3, 100)));
  for (auto* admission : {&current, &old}) {
    for (const auto* path : {"/v1/uploads", "/v1/uploads/upload_x/parts",
                             "/v1/uploads/upload_x/complete",
                             "/v1/uploads/upload_x/parts/extra"}) {
      HttpRequestHeaders headers;
      headers.client_id = "alpha";
      headers.gateway_token = kSecret;
      headers.content_length = 17ULL << 20;
      ClientLease lease;
      ClientIdentity identity;
      int status = 0;
      const bool allowed = AdmitClientRequest(
          "POST", path, &headers, *admission, &lease, &identity, &status);
      const bool part = admission == &current &&
                        std::string_view(path) == "/v1/uploads/upload_x/parts";
      Q4T_CHECK(allowed == part);
      if (!allowed) Q4T_CHECK(status == 413);
    }
  }
  const auto identity = current.Authenticate("alpha", kSecret);
  std::vector<std::unique_ptr<ClientLease>> holders;
  for (unsigned i = 0; i < identity.Limits()->max_inflight; ++i) {
    auto lease = std::make_unique<ClientLease>();
    Q4T_CHECK(lease->Acquire(current, identity, ClientLane::kData));
    holders.push_back(std::move(lease));
  }
  HttpRequestHeaders headers;
  headers.client_id = "alpha";
  headers.gateway_token = kSecret;
  ClientLease cancel;
  ClientIdentity admitted;
  int status = 0;
  Q4T_CHECK(AdmitClientRequest("POST", "/v1/uploads/upload_x/cancel", &headers,
                               current, &cancel, &admitted, &status));
  Q4T_CHECK(current.Usage(0).cancel_active == 1 &&
            current.RateUsage(0).requests == 0);
  return true;
}

Q4T_TEST(client_file_chunked_cannot_bypass_current_or_legacy_limit) {
  for (const auto bytes : {2048, 2049}) {
    for (const bool legacy : {false, true}) {
      ClientAdmission admission;
      Q4T_CHECK(
          admission.Configure(legacy ? DynamicPolicy(0, 0) : FilePolicy(2048)));
      int sockets[2];
      Q4T_CHECK(socketpair(AF_UNIX, SOCK_STREAM, 0, sockets) == 0);
      ScopeExit close([&] {
        ::close(sockets[0]);
        ::close(sockets[1]);
      });
      std::string wire = "POST /v1/files HTTP/1.1\r\nHost: local\r\n" +
                         Headers() + "Transfer-Encoding: chunked\r\n\r\n" +
                         (bytes == 2048 ? "800\r\n" : "801\r\n") +
                         std::string(bytes, 'x') + "\r\n0\r\n\r\n";
      Q4T_CHECK(send(sockets[0], wire.data(), wire.size(), MSG_NOSIGNAL) ==
                static_cast<ssize_t>(wire.size()));
      HttpRequestHeaders headers;
      HttpBodyBudget budget(65536);
      HttpBodyLease memory(budget);
      HttpBodyStream stream;
      ClientLease lease;
      ClientIdentity identity;
      std::string method, path, body;
      int status = 0;
      size_t consumed = 0;
      const bool result = ReadHttpRequest(
          sockets[1], &method, &path, &body, 1000, &memory, &status, &headers,
          [&] {
            if (!AdmitClientRequest(method, path, &headers, admission, &lease,
                                    &identity, &status))
              return false;
            stream.max_bytes = kFileWireBytes;
            stream.reservation_bytes = 65536;
            stream.consume = [&](auto bytes, auto) {
              consumed += bytes.size();
              return true;
            };
            return true;
          },
          {}, nullptr, {}, &stream);
      Q4T_CHECK(result == (!legacy && bytes == 2048));
      Q4T_CHECK(body.empty() && consumed == (result ? 2048 : 0));
      if (!result) Q4T_CHECK(status == 413);
    }
  }
  return true;
}

Q4T_TEST(client_file_revision_upgrade_preserves_inflight_and_rate_recovery) {
  ClientAdmission admission;
  Q4T_CHECK(admission.Configure(DynamicPolicy(3, 100)));
  Q4T_CHECK(admission.EnableRateRecovery());
  Q4T_CHECK(admission.RestoreRates(nullptr, std::string(64, '1')));
  const auto original = admission.Authenticate("alpha", kSecret);
  ClientRateLease request;
  Q4T_CHECK(request.Reserve(admission, original, 3, 5) ==
            ClientRateResult::kAllowed);
  request.Start();
  request.Completion(2);
  Q4T_CHECK(admission.Publish(FilePolicy(4096, 1), 0, 8, 8));
  Q4T_CHECK(
      original.Limits()->max_file_body_bytes == 0 &&
      admission.Authenticate("alpha", kSecret).Limits()->max_file_body_bytes ==
          4096);
  Q4T_CHECK(admission.RateUsage(0).requests == 1);
  Q4T_CHECK(!admission.Publish(DynamicPolicy(3, 100, 2), 1, 8, 8));
  request.Settle();
  ClientRateState state;
  Q4T_CHECK(admission.FreezeRates(&state));
  Q4T_CHECK(state.clients[0].usage.tokens == 5);
  ClientAdmission restarted;
  Q4T_CHECK(restarted.Configure(FilePolicy(4096, 1)));
  Q4T_CHECK(restarted.EnableRateRecovery());
  Q4T_CHECK(restarted.RestoreRates(&state, std::string(64, '2')));
  Q4T_CHECK(restarted.RateUsage(0).tokens == 5 &&
            restarted.RateUsage(0).requests == 1);
  return true;
}

Q4T_TEST(client_file_policy_discovery_remains_mutable_and_credential_free) {
  const auto policy = FilePolicy();
  ClientAdmission admission;
  Q4T_CHECK(admission.Configure(policy));
  ChatPlanConfig config;
  config.client_policy_version = 4;
  config.client_policy_sha256 = policy.sha256;
  config.client_policy_content_sha256 = ClientPolicyDigest(policy);
  config.client_count = policy.clients.size();
  Json value;
  const auto raw = EffectiveServeConfigJson({}, config, true, true, 8);
  Q4T_CHECK(q4t::io::ParseJson(raw, &value));
  Q4T_CHECK(value.Find("client_policy")->GetBool("mutable") &&
            value.Find("client_governance")->GetInt("policy_version") == 4 &&
            raw.find(kSecret) == raw.npos);
  Q4T_CHECK(
      q4t::io::ParseJson(DescribeClientPolicy(admission, 8, 8, true), &value));
  Q4T_CHECK(value.GetBool("mutable") &&
            value.GetString("content_sha256") == ClientPolicyDigest(policy));
  return true;
}

Q4T_TEST(client_file_legacy_migration_preserves_capacity_and_usage) {
  ClientAdmission admission;
  Q4T_CHECK(admission.Configure(FilePolicy(4096)));
  Q4T_CHECK(admission.EnableRateRecovery());
  Q4T_CHECK(admission.RestoreRates(nullptr, std::string(64, '1')));
  ClientRateLease request;
  Q4T_CHECK(request.Reserve(admission,
              admission.Authenticate("alpha", kSecret), 3, 5) ==
            ClientRateResult::kAllowed);
  request.Start();
  request.Completion(2);
  Q4T_CHECK(!admission.Publish(DynamicPolicy(3, 100, 1), 0, 8, 8));
  Q4T_CHECK(admission.Publish(FilePolicy(1024, 1), 0, 8, 8));
  Q4T_CHECK(admission.Publish(DynamicPolicy(3, 100, 2), 1, 8, 8));
  request.Settle();
  Q4T_CHECK(admission.Version() == 3 &&
            admission.Limits(0).max_body_bytes == 1024 &&
            admission.Limits(0).max_file_body_bytes == 0);
  ClientRateState checkpoint;
  Q4T_CHECK(admission.FreezeRates(&checkpoint));
  ClientAdmission legacy;
  Q4T_CHECK(legacy.Configure(DynamicPolicy(3, 100, 2)));
  Q4T_CHECK(legacy.EnableRateRecovery());
  Q4T_CHECK(legacy.RestoreRates(&checkpoint, std::string(64, '2')));
  Q4T_CHECK(legacy.RateUsage(0).requests == 1 &&
            legacy.RateUsage(0).tokens == 5);
  return true;
}

Q4T_TEST(client_file_configuration_emits_actual_schema_value) {
  const auto policy = FilePolicy();
  ChatPlanConfig config;
  config.client_policy_version = 4;
  config.client_policy_sha256 = policy.sha256;
  config.client_policy_content_sha256 = ClientPolicyDigest(policy);
  config.client_count = policy.clients.size();
  const auto* output = std::getenv("Q4T_TEST_OUTPUT_DIR");
  const auto directory = std::filesystem::path(
      output ? output : ".q4t-work/files-contract-cpu");
  std::filesystem::create_directories(directory);
  std::ofstream file(directory / "files-config.json");
  file << EffectiveServeConfigJson({}, config, true, true, 8);
  Q4T_CHECK(file.good());
  return true;
}

Q4T_TEST(client_dynamic_revision_parser_is_strict_and_transactional) {
  GatewayClientPolicy parsed;
  Q4T_CHECK(ParseGatewayClientPolicy(DynamicRaw(10, 200), &parsed));
  Q4T_CHECK(parsed.version == 3 && parsed.revision == 0);
  const auto previous = parsed.sha256;
  const auto raw = DynamicRaw(10, 200);
  for (const auto& bad : {"-1", "true", "0.0", "9007199254740992",
                          "18446744073709551616", "null"}) {
    Q4T_CHECK(!ParseGatewayClientPolicy(
        Replace(raw, "\"revision\":0", "\"revision\":" + std::string(bad)),
        &parsed));
    Q4T_CHECK(parsed.sha256 == previous && parsed.revision == 0);
  }
  Q4T_CHECK(!ParseGatewayClientPolicy(
      Replace(raw, "\"revision\":0", "\"revision\":0,\"revision\":1"),
      &parsed));
  Q4T_CHECK(
      ParseGatewayClientPolicy(DynamicRaw(0, 0, kMaxClientRevision), &parsed));
  Q4T_CHECK(parsed.revision == kMaxClientRevision);
  return true;
}

Q4T_TEST(client_dynamic_update_preserves_snapshots_slots_and_move_ownership) {
  ClientAdmission admission;
  Q4T_CHECK(admission.Configure(DynamicPolicy(10, 200)));
  auto previous = admission.Authenticate("alpha", kSecret);
  auto candidate = DynamicPolicy(10, 200, 1);
  candidate.clients[0].max_prompt_tokens = 32;
  Q4T_CHECK(admission.Publish(candidate, 0, 8, 8));
  const auto current = admission.Authenticate("alpha", kSecret);
  Q4T_CHECK(previous.Limits()->max_prompt_tokens == 64);
  Q4T_CHECK(current.Limits()->max_prompt_tokens == 32);
  Q4T_CHECK(previous.Id() == current.Id() &&
            previous.Index() == current.Index());
  const auto copied = previous;
  const auto moved = std::move(previous);
  Q4T_CHECK(!previous.Allowed() && !previous.Scoped());
  Q4T_CHECK(copied.Id() == "alpha" && moved.Id() == "alpha");
  ClientLease denied;
  Q4T_CHECK(!denied.Acquire(admission, previous, ClientLane::kData));
  const auto accepted = admission.PolicySha256();
  Q4T_CHECK(!admission.Publish(candidate, 0, 8, 8));
  candidate.revision = 2;
  for (int corruption = 0; corruption < 5; ++corruption) {
    auto bad = candidate;
    if (corruption == 0) bad.clients.pop_back();
    if (corruption == 1) std::swap(bad.clients[0], bad.clients[1]);
    if (corruption == 2) bad.gateway_token = std::string(64, 'b');
    if (corruption == 3) bad.fair_queue = false;
    if (corruption == 4) bad.clients[0].max_inflight = 9;
    Q4T_CHECK(!admission.Publish(std::move(bad), 1, 8, 8));
    Q4T_CHECK(admission.PolicySha256() == accepted && admission.Size() == 2);
  }
  auto extra = candidate.clients.back();
  extra.id = "gamma";
  candidate.clients.push_back(extra);
  Q4T_CHECK(!admission.Publish(candidate, 1, 8, 2));
  Q4T_CHECK(admission.Publish(candidate, 1, 8, 8));
  Q4T_CHECK(admission.Authenticate("gamma", kSecret).Index() == 2);
  Q4T_CHECK(admission.Usage(2).data_active == 0 && moved.Id() == "alpha");
  return true;
}

Q4T_TEST(
    client_dynamic_lowered_limits_retain_data_control_cancel_and_transfer) {
  ClientAdmission admission;
  Q4T_CHECK(admission.Configure(DynamicPolicy(0, 0)));
  const auto alpha = admission.Authenticate("alpha", kSecret);
  auto first = std::make_unique<ClientLease>();
  auto second = std::make_unique<ClientLease>();
  Q4T_CHECK(first->Acquire(admission, alpha, ClientLane::kData));
  Q4T_CHECK(second->Acquire(admission, alpha, ClientLane::kData));
  ClientLease control, cancel;
  Q4T_CHECK(control.Acquire(admission, alpha, ClientLane::kControl));
  Q4T_CHECK(cancel.Acquire(admission, alpha, ClientLane::kCancel));
  auto candidate = DynamicPolicy(0, 0, 1);
  candidate.clients[0].max_inflight = 1;
  Q4T_CHECK(admission.Publish(candidate, 0, 8, 8));
  Q4T_CHECK(admission.Usage(0).data_active == 2);
  ClientLease no_data, no_control, no_cancel, beta;
  Q4T_CHECK(!no_data.Acquire(admission, alpha, ClientLane::kData));
  Q4T_CHECK(!no_control.Acquire(admission, alpha, ClientLane::kControl));
  Q4T_CHECK(!no_cancel.Acquire(admission, alpha, ClientLane::kCancel));
  Q4T_CHECK(beta.Acquire(admission, admission.Authenticate("beta", kSecret),
                         ClientLane::kData));
  {
    ClientLease transferred;
    Q4T_CHECK(first->TransferTo(transferred));
    first.reset();
    second.reset();
    Q4T_CHECK(admission.Usage(0).data_active == 1);
    ClientLease full;
    Q4T_CHECK(!full.Acquire(admission, alpha, ClientLane::kData));
  }
  ClientLease available;
  Q4T_CHECK(available.Acquire(admission, alpha, ClientLane::kData));
  Q4T_CHECK(admission.Usage(0).control_active == 1 &&
            admission.Usage(0).cancel_active == 1);
  return true;
}

Q4T_TEST(client_dynamic_quota_off_on_keeps_usage_and_rollover_reservations) {
  ClientAdmission admission;
  Q4T_CHECK(admission.Configure(DynamicPolicy(2, 100)));
  const auto alpha = admission.Authenticate("alpha", kSecret);
  const auto now = ClientClock::time_point{} + std::chrono::seconds(10);
  ClientRateLease first;
  Q4T_CHECK(first.Reserve(admission, alpha, 5, 10, now) ==
            ClientRateResult::kAllowed);
  first.Start();
  first.Completion(4);
  first.Settle(now);
  Q4T_CHECK(admission.Publish(DynamicPolicy(0, 0, 1), 0, 8, 8));
  ClientRateLease during_disabled;
  Q4T_CHECK(during_disabled.Reserve(admission, alpha, 5, 10, now) ==
            ClientRateResult::kAllowed);
  during_disabled.Start();
  during_disabled.Completion(2);
  Q4T_CHECK(admission.Publish(DynamicPolicy(2, 100, 2), 1, 8, 8));
  const auto before = admission.RateUsage(0, now);
  Q4T_CHECK(before.requests == 2 && before.tokens == 9 &&
            before.reserved == 15);
  ClientRateLease no_rpm;
  Q4T_CHECK(no_rpm.Reserve(admission, alpha, 0, 1, now) ==
            ClientRateResult::kRpm);
  Q4T_CHECK(admission.Publish(DynamicPolicy(10, 10, 3), 2, 8, 8));
  const auto next = now + std::chrono::seconds(61);
  ClientRateLease no_tpm;
  Q4T_CHECK(no_tpm.Reserve(admission, alpha, 0, 1, next) ==
            ClientRateResult::kTpm);
  during_disabled.Settle(next);
  const auto after = admission.RateUsage(0, next);
  Q4T_CHECK(after.requests == 0 && after.tokens == 7 && after.reserved == 0);
  ClientRateLease remaining;
  Q4T_CHECK(remaining.Reserve(admission, alpha, 0, 3, next) ==
            ClientRateResult::kAllowed);
  remaining.Settle(next);
  Q4T_CHECK(admission.RateUsage(0, next).reserved == 0);
  return true;
}

Q4T_TEST(client_dynamic_stale_identity_uses_current_limits_legacy_stays_fixed) {
  ClientAdmission admission;
  Q4T_CHECK(admission.Configure(DynamicPolicy(20, 1000)));
  const auto original = admission.Authenticate("alpha", kSecret);
  auto candidate = DynamicPolicy(1, 1000, 1);
  candidate.clients[0].max_prompt_tokens = 32;
  Q4T_CHECK(admission.Publish(candidate, 0, 8, 8));
  ClientRateLease oversized, first, second;
  Q4T_CHECK(oversized.Reserve(admission, original, 33, 1) ==
            ClientRateResult::kInvalid);
  Q4T_CHECK(first.Reserve(admission, original, 1, 1) ==
            ClientRateResult::kAllowed);
  Q4T_CHECK(second.Reserve(admission, original, 1, 1) ==
            ClientRateResult::kRpm);
  ClientAdmission legacy;
  Q4T_CHECK(legacy.Configure(RatePolicy(2, 1000)));
  Q4T_CHECK(!legacy.Publish(candidate, 0, 8, 8));
  Q4T_CHECK(legacy.Version() == 2 && legacy.Limits(0).max_prompt_tokens == 64);
  const auto metrics = ClientPolicyMetrics(admission);
  Q4T_CHECK(metrics.find("q4t_client_rate_requests{client=\"alpha\"} 1\n") !=
            std::string::npos);
  Q4T_CHECK(metrics.find(kSecret) == std::string::npos);
  return true;
}

Q4T_TEST(client_dynamic_concurrent_authentication_observes_whole_snapshots) {
  ClientAdmission admission;
  Q4T_CHECK(admission.Configure(DynamicPolicy(0, 0)));
  std::atomic<bool> done{false}, failed{false};
  std::atomic<uint64_t> observed{0};
  std::barrier ready(9);
  std::vector<std::thread> readers;
  for (int i = 0; i < 8; ++i)
    readers.emplace_back([&] {
      ready.arrive_and_wait();
      do {
        const auto identity = admission.Authenticate("alpha", kSecret);
        const auto* limits = identity.Limits();
        if (!identity.Allowed() || identity.Id() != "alpha" ||
            identity.Index() != 0 || !limits ||
            !((limits->max_prompt_tokens == 64 &&
               limits->max_output_tokens == 16) ||
              (limits->max_prompt_tokens == 32 &&
               limits->max_output_tokens == 8)))
          failed.store(true);
        observed.fetch_add(1);
      } while (!done.load());
    });
  ready.arrive_and_wait();
  for (uint64_t revision = 1; revision <= 200; ++revision) {
    auto candidate = DynamicPolicy(0, 0, revision);
    if (revision % 2) {
      candidate.clients[0].max_prompt_tokens = 32;
      candidate.clients[0].max_output_tokens = 8;
    }
    if (!admission.Publish(candidate, revision - 1, 8, 8)) failed.store(true);
  }
  done.store(true);
  for (auto& reader : readers) reader.join();
  Q4T_CHECK(!failed.load() && observed.load() >= 8);
  Q4T_CHECK(admission.Snapshot()->revision == 200);
  return true;
}

Q4T_TEST(client_rate_policy_v2_strict_and_v1_unchanged) {
  auto v2 =
      Replace(Policy(), "\"version\":1", "\"version\":2,\"fair_queue\":true");
  const auto field = std::string("\"max_body_bytes\":1024");
  size_t at = 0;
  while ((at = v2.find(field, at)) != v2.npos) {
    at += field.size();
    const std::string rates =
        ",\"requests_per_minute\":0,\"tokens_per_minute\":100";
    v2.insert(at, rates);
    at += rates.size();
  }
  GatewayClientPolicy parsed;
  Q4T_CHECK(ParseGatewayClientPolicy(v2, &parsed));
  Q4T_CHECK(parsed.version == 2 && parsed.fair_queue &&
            parsed.clients[0].tokens_per_minute == 100);
  const auto hash = parsed.sha256;
  for (const auto& raw :
       {Replace(v2, "\"fair_queue\":true", "\"fair_queue\":1"),
        Replace(v2, "\"version\":2", "\"version\":1"),
        Replace(v2, "\"requests_per_minute\":0", "\"requests_per_minute\":-1"),
        Replace(v2, "\"requests_per_minute\":0", "\"requests_per_minute\":1.0"),
        Replace(v2, "\"requests_per_minute\":0",
                "\"requests_per_minute\":1000001"),
        Replace(v2, "\"tokens_per_minute\":100",
                "\"tokens_per_minute\":1000000001"),
        Replace(v2, "\"tokens_per_minute\":100",
                "\"tokens_per_minute\":null")}) {
    Q4T_CHECK(!ParseGatewayClientPolicy(raw, &parsed) && parsed.sha256 == hash);
  }
  Q4T_CHECK(ParseGatewayClientPolicy(Policy(), &parsed));
  Q4T_CHECK(parsed.version == 1 && !parsed.fair_queue &&
            parsed.clients[0].tokens_per_minute == 0);
  return true;
}

Q4T_TEST(client_rate_atomic_concurrency_and_per_client_isolation) {
  ClientAdmission admission;
  Q4T_CHECK(admission.Configure(RatePolicy(4, 128)));
  const auto alpha = admission.Authenticate("alpha", kSecret);
  const auto now = ClientClock::now();
  std::atomic<int> accepted{0};
  std::barrier ready(17), release(17);
  std::vector<std::thread> threads;
  for (int i = 0; i < 16; ++i)
    threads.emplace_back([&] {
      ClientRateLease lease;
      if (lease.Reserve(admission, alpha, 16, 16, now) ==
          ClientRateResult::kAllowed)
        ++accepted;
      ready.arrive_and_wait();
      release.arrive_and_wait();
    });
  ready.arrive_and_wait();
  const auto full = admission.RateUsage(0, now);
  ClientRateLease beta;
  const auto separate = beta.Reserve(
      admission, admission.Authenticate("beta", kSecret), 16, 16, now);
  release.arrive_and_wait();
  for (auto& thread : threads) thread.join();
  Q4T_CHECK(accepted == 4 && full.reserved == 128 && full.requests == 4);
  Q4T_CHECK(separate == ClientRateResult::kAllowed);
  const auto ended = admission.RateUsage(0, now);
  Q4T_CHECK(ended.reserved == 0 && ended.tokens == 0 && ended.requests == 4 &&
            ended.rpm_rejected == 12);
  return true;
}

Q4T_TEST(client_rate_refund_cancel_failure_and_preterminal_idempotence) {
  ClientAdmission admission;
  Q4T_CHECK(admission.Configure(RatePolicy(10, 100)));
  const auto alpha = admission.Authenticate("alpha", kSecret);
  const auto now = ClientClock::now();
  ClientRateLease first, rejected;
  Q4T_CHECK(first.Reserve(admission, alpha, 50, 16, now) ==
            ClientRateResult::kAllowed);
  Q4T_CHECK(rejected.Reserve(admission, alpha, 50, 16, now) ==
            ClientRateResult::kTpm);
  first.Start();
  first.Completion(3);
  first.Settle(now);
  first.Settle(now);
  auto value = admission.RateUsage(0, now);
  Q4T_CHECK(value.tokens == 53 && value.reserved == 0 && value.requests == 1 &&
            value.tpm_rejected == 1);
  try {
    ClientRateLease queued;
    Q4T_CHECK(queued.Reserve(admission, alpha, 20, 16, now) ==
              ClientRateResult::kAllowed);
    throw std::runtime_error("cancel or exception before slot");
  } catch (const std::runtime_error&) {
  }
  value = admission.RateUsage(0, now);
  Q4T_CHECK(value.tokens == 53 && value.reserved == 0 && value.requests == 2);
  {
    ClientRateLease running;
    Q4T_CHECK(running.Reserve(admission, alpha, 20, 16, now) ==
              ClientRateResult::kAllowed);
    running.Start();
    running.Completion(7);
  }
  value = admission.RateUsage(0, now);
  Q4T_CHECK(value.tokens == 80 && value.settled_tokens == 80 &&
            value.requests == 3);
  return true;
}

Q4T_TEST(client_rate_pending_survives_window_and_settles_in_current_window) {
  ClientAdmission admission;
  Q4T_CHECK(admission.Configure(RatePolicy(1, 100)));
  const auto alpha = admission.Authenticate("alpha", kSecret);
  const auto now = ClientClock::now();
  ClientRateLease old;
  Q4T_CHECK(old.Reserve(admission, alpha, 60, 16, now) ==
            ClientRateResult::kAllowed);
  ClientRateLease early, blocked, next;
  Q4T_CHECK(
      early.Reserve(admission, alpha, 10, 10, now + std::chrono::seconds(59)) ==
      ClientRateResult::kRpm);
  const auto later = now + std::chrono::seconds(60);
  Q4T_CHECK(admission.RateUsage(0, later).reserved == 76);
  Q4T_CHECK(blocked.Reserve(admission, alpha, 20, 16, later) ==
            ClientRateResult::kTpm);
  old.Start();
  old.Completion(4);
  old.Settle(later);
  Q4T_CHECK(admission.RateUsage(0, later).tokens == 64);
  Q4T_CHECK(next.Reserve(admission, alpha, 20, 16, later) ==
            ClientRateResult::kAllowed);
  next.Settle(later);
  Q4T_CHECK(admission.RateUsage(0, later).tokens == 64 &&
            admission.RateUsage(0, later).requests == 1);
  return true;
}

Q4T_TEST(client_rate_disabled_restart_authentication_and_fair_capacity) {
  ClientAdmission disabled, fresh;
  Q4T_CHECK(disabled.Configure(RatePolicy(0, 0)) &&
            fresh.Configure(RatePolicy(1, 100)));
  ClientRateLease no_rate, invalid, wrong, valid;
  Q4T_CHECK(no_rate.Reserve(disabled, disabled.Authenticate("alpha", kSecret),
                            10, 5) == ClientRateResult::kAllowed);
  Q4T_CHECK(disabled.RateUsage(0).admitted == 0);
  Q4T_CHECK(invalid.Reserve(fresh, {}, 10, 5) == ClientRateResult::kInvalid);
  Q4T_CHECK(wrong.Reserve(fresh, disabled.Authenticate("alpha", kSecret), 10,
                          5) == ClientRateResult::kInvalid);
  Q4T_CHECK(fresh.RateUsage(0).requests == 0 &&
            valid.Reserve(fresh, fresh.Authenticate("alpha", kSecret), 10, 5) ==
                ClientRateResult::kAllowed);
  Q4T_CHECK(fresh.FairCapacity(4, 2) && !fresh.FairCapacity(3, 2) &&
            !fresh.FairCapacity(4, 1));
  return true;
}

Q4T_TEST(client_rate_tpm_only_atomic_and_restart_reset) {
  ClientAdmission first;
  Q4T_CHECK(first.Configure(RatePolicy(0, 32)));
  const auto alpha = first.Authenticate("alpha", kSecret);
  std::barrier ready(17), release(17);
  std::atomic<int> accepted{0};
  std::vector<std::thread> threads;
  for (int i = 0; i < 16; ++i)
    threads.emplace_back([&] {
      ClientRateLease lease;
      if (lease.Reserve(first, alpha, 16, 16) == ClientRateResult::kAllowed)
        ++accepted;
      ready.arrive_and_wait();
      release.arrive_and_wait();
    });
  ready.arrive_and_wait();
  const auto full = first.RateUsage(0);
  release.arrive_and_wait();
  for (auto& thread : threads) thread.join();
  Q4T_CHECK(accepted == 1 && full.reserved == 32 && full.tpm_rejected == 15);
  ClientAdmission restarted;
  Q4T_CHECK(restarted.Configure(RatePolicy(0, 32)));
  Q4T_CHECK(restarted.RateUsage(0).admitted == 0 &&
            first.RateUsage(0).admitted == 1);
  ClientRateLease fresh;
  Q4T_CHECK(fresh.Reserve(restarted, restarted.Authenticate("alpha", kSecret),
                          16, 16) == ClientRateResult::kAllowed);
  fresh.Start();
  fresh.Completion(2);
  fresh.Settle();
  const auto metrics = ClientPolicyMetrics(restarted);
  Q4T_CHECK(metrics.find(
                "q4t_client_rate_settled_tokens_total{client=\"alpha\"} 18") !=
            metrics.npos);
  Q4T_CHECK(metrics.find(kSecret) == metrics.npos);
  return true;
}

Q4T_TEST(client_rate_configuration_discloses_optional_contract) {
  ServerOptions options;
  ChatPlanConfig config;
  config.client_policy_version = 2;
  config.client_fair_queue = true;
  Json value;
  Q4T_CHECK(q4t::io::ParseJson(
      EffectiveServeConfigJson(options, config, true, true, 8), &value));
  Q4T_CHECK(value.Find("client_governance")->GetInt("policy_version") == 2);
  Q4T_CHECK(value.Find("client_governance")->GetBool("fair_queue"));
  Q4T_CHECK(value.Find("limits")->GetString("queue_policy") ==
            "client_round_robin_best_fit");
  return true;
}

Q4T_TEST(client_dynamic_startup_binding_and_runtime_discovery_are_distinct) {
  const auto policy = DynamicPolicy(5, 200);
  ClientAdmission admission;
  Q4T_CHECK(admission.Configure(policy));
  ChatPlanConfig config;
  config.client_policy_version = 3;
  config.client_policy_sha256 = policy.sha256;
  config.client_policy_content_sha256 = ClientPolicyDigest(policy);
  config.client_policy_revision = policy.revision;
  config.client_count = policy.clients.size();
  const auto startup = EffectiveServeConfigJson({}, config, true, true, 8);
  Json decoded;
  Q4T_CHECK(q4t::io::ParseJson(startup, &decoded));
  const auto* found = decoded.Find("client_policy");
  Q4T_CHECK(found && found->GetBool("mutable") &&
            found->GetString("snapshot") == "startup" &&
            found->GetString("sha256") == policy.sha256 &&
            found->GetInt("revision") == 0 &&
            found->GetString("content_sha256") == ClientPolicyDigest(policy));
  const auto update = DynamicPolicy(1, 20, 1);
  Q4T_CHECK(admission.Publish(update, 0, 8, 8));
  Json current;
  Q4T_CHECK(q4t::io::ParseJson(DescribeClientPolicy(admission, 8, 8, false),
                               &current));
  Q4T_CHECK(current.GetInt("revision") == 1 &&
            current.GetString("content_sha256") == ClientPolicyDigest(update) &&
            !current.GetBool("publisher_alive"));
  Q4T_CHECK(EffectiveServeConfigJson({}, config, true, true, 8) == startup);
  Q4T_CHECK(startup.find(kSecret) == startup.npos);
  return true;
}

Q4T_TEST(client_policy_strict_transactional_schema) {
  GatewayClientPolicy parsed;
  Q4T_CHECK(ParseGatewayClientPolicy(Policy(), &parsed));
  Q4T_CHECK(parsed.clients.size() == 2 && parsed.gateway_token == kSecret);
  const auto original = parsed.sha256;
  Q4T_CHECK(original.size() == 64);
  for (const auto& raw :
       {std::string("{}"), Policy(Entry("alpha") + ',' + Entry("alpha")),
        Policy(Entry("Alpha")), Policy(Entry("x\\u0000y")),
        Replace(Policy(), "\"version\":1", "\"version\":1,\"version\":1"),
        Replace(Policy(), "\"version\":1", "\"version\":1.0"),
        Replace(Policy(), "\"version\":1", "\"version\":true"),
        Replace(Policy(), "\"max_body_bytes\":1024", "\"max_body_bytes\":1023"),
        Replace(Policy(), "\"max_inflight\":2", "\"max_inflight\":0"),
        Replace(Policy(), "\"max_inflight\":2", "\"max_inflight\":2e0"),
        Replace(Policy(), "\"max_inflight\":2", "\"max_inflight\":4294967296"),
        Replace(Policy(), "\"max_output_tokens\":16", "\"other\":16"),
        Replace(Policy(), kSecret, std::string(64, 'A')), Policy("")}) {
    const auto status = ParseGatewayClientPolicy(raw, &parsed);
    Q4T_CHECK(!status && parsed.sha256 == original &&
              parsed.clients.size() == 2);
    Q4T_CHECK(status.message().find(kSecret) == std::string::npos);
  }
  Q4T_CHECK(!ParseGatewayClientPolicy(Policy(), nullptr));
  Q4T_CHECK(!ParseGatewayClientPolicy(std::string(kClientPolicyBytes + 1, ' '),
                                      &parsed));
  return true;
}

Q4T_TEST(client_policy_fixed_inventory_and_raw_identity) {
  std::string entries;
  for (size_t i = 0; i < 64; ++i) {
    if (i) entries += ',';
    entries += Entry("client" + std::to_string(i));
  }
  GatewayClientPolicy policy, same;
  Q4T_CHECK(ParseGatewayClientPolicy(Policy(entries), &policy));
  Q4T_CHECK(ParseGatewayClientPolicy(Policy(entries) + '\n', &same));
  Q4T_CHECK(policy.sha256 != same.sha256);
  ClientAdmission admission;
  Q4T_CHECK(admission.Configure(std::move(policy)) && admission.Size() == 64);
  Q4T_CHECK(admission.Authenticate("client63", kSecret).Allowed());
  Q4T_CHECK(!admission.Authenticate("client64", kSecret).Allowed());
  Q4T_CHECK(!admission.Configure(std::move(same)));
  Q4T_CHECK(!ParseGatewayClientPolicy(Policy(entries + ',' + Entry("extra")),
                                      &policy));
  return true;
}

Q4T_TEST(client_policy_private_file_boundary) {
  Files files;
  files.Write("policy", Policy());
  GatewayClientPolicy result;
  Q4T_CHECK(LoadGatewayClientPolicy((files.path / "policy").string(), &result));
  const auto digest = result.sha256;
  ::chmod((files.path / "policy").c_str(), 0644);
  Q4T_CHECK(
      !LoadGatewayClientPolicy((files.path / "policy").string(), &result));
  ::chmod((files.path / "policy").c_str(), 0600);
  std::filesystem::create_symlink("policy", files.path / "link");
  Q4T_CHECK(!LoadGatewayClientPolicy((files.path / "link").string(), &result));
  std::filesystem::create_hard_link(files.path / "policy", files.path / "hard");
  Q4T_CHECK(
      !LoadGatewayClientPolicy((files.path / "policy").string(), &result));
  Q4T_CHECK(::mkfifo((files.path / "fifo").c_str(), 0600) == 0);
  Q4T_CHECK(!LoadGatewayClientPolicy((files.path / "fifo").string(), &result));
  Q4T_CHECK(!LoadGatewayClientPolicy(files.path.string(), &result));
  files.Write("large", std::string(kClientPolicyBytes + 1, ' '));
  Q4T_CHECK(!LoadGatewayClientPolicy((files.path / "large").string(), &result));
  Q4T_CHECK(result.sha256 == digest);
  return true;
}

Q4T_TEST(client_authentication_legacy_and_forgery_boundary) {
  ClientAdmission legacy, policy;
  Q4T_CHECK(Configure(&policy));
  Q4T_CHECK(legacy.Authenticate("", "").Allowed());
  Q4T_CHECK(!legacy.Authenticate("alpha", kSecret).Allowed());
  Q4T_CHECK(!policy.Authenticate("", "").Allowed());
  Q4T_CHECK(!policy.Authenticate("alpha", "").Allowed());
  Q4T_CHECK(!policy.Authenticate("unknown", kSecret).Allowed());
  for (size_t position : {size_t{0}, size_t{31}, size_t{63}}) {
    auto wrong = kSecret;
    wrong[position] = 'b';
    Q4T_CHECK(!policy.Authenticate("alpha", wrong).Allowed());
  }
  const auto valid = policy.Authenticate("alpha", kSecret);
  Q4T_CHECK(valid.Allowed() && valid.Scoped() && valid.Id() == "alpha");
  ClientLease foreign, untrusted, off;
  Q4T_CHECK(!foreign.Acquire(legacy, valid, ClientLane::kData));
  Q4T_CHECK(!untrusted.Acquire(policy, ClientIdentity(), ClientLane::kData));
  Q4T_CHECK(
      off.Acquire(legacy, legacy.Authenticate("", ""), ClientLane::kData));
  return true;
}

Q4T_TEST(client_quota_parallel_atomic_ownership) {
  ClientAdmission admission;
  Q4T_CHECK(Configure(&admission));
  const auto alpha = admission.Authenticate("alpha", kSecret);
  std::barrier start(16), acquired(16);
  std::atomic<int> accepted{0};
  std::vector<std::thread> threads;
  for (int i = 0; i < 16; ++i) {
    threads.emplace_back([&] {
      ClientLease lease;
      start.arrive_and_wait();
      if (lease.Acquire(admission, alpha, ClientLane::kData)) ++accepted;
      acquired.arrive_and_wait();
    });
  }
  for (auto& thread : threads) thread.join();
  Q4T_CHECK(accepted == 2 && admission.Usage(0).data_active == 0);
  Q4T_CHECK(admission.Usage(0).data_rejected == 14);
  return true;
}

Q4T_TEST(client_quota_independent_clients_control_and_unwind) {
  ClientAdmission admission;
  Q4T_CHECK(Configure(&admission));
  const auto alpha = admission.Authenticate("alpha", kSecret);
  const auto beta = admission.Authenticate("beta", kSecret);
  try {
    ClientLease a, b, full, control, control_full, other;
    Q4T_CHECK(a.Acquire(admission, alpha, ClientLane::kData));
    Q4T_CHECK(b.Acquire(admission, alpha, ClientLane::kData));
    Q4T_CHECK(!full.Acquire(admission, alpha, ClientLane::kData));
    Q4T_CHECK(!a.Acquire(admission, alpha, ClientLane::kData));
    Q4T_CHECK(control.Acquire(admission, alpha, ClientLane::kControl));
    Q4T_CHECK(!control_full.Acquire(admission, alpha, ClientLane::kControl));
    Q4T_CHECK(other.Acquire(admission, beta, ClientLane::kData));
    throw std::runtime_error("response failure");
  } catch (const std::runtime_error&) {
  }
  Q4T_CHECK(admission.Usage(0).data_active == 0 &&
            admission.Usage(0).control_active == 0 &&
            admission.Usage(1).data_active == 0);
  ClientLease recovery;
  Q4T_CHECK(recovery.Acquire(admission, alpha, ClientLane::kData));
  const auto metrics = ClientPolicyMetrics(admission);
  Q4T_CHECK(metrics.find("{client=\"alpha\",lane=\"data\"} 1") != metrics.npos);
  Q4T_CHECK(metrics.find(kSecret) == metrics.npos);
  return true;
}

Q4T_TEST(client_handoff_keeps_quota_until_cleanup_then_admits_successor) {
  ClientAdmission admission;
  Q4T_CHECK(Configure(&admission));
  const auto alpha = admission.Authenticate("alpha", kSecret);
  auto first = std::make_unique<ClientLease>();
  ClientLease second;
  Q4T_CHECK(first->Acquire(admission, alpha, ClientLane::kData));
  Q4T_CHECK(second.Acquire(admission, alpha, ClientLane::kData));
  first->BeginHandoff();
  first->BeginHandoff();  // Idempotent publication, no duplicate finishing
                          // owner.
  std::promise<void> entering;
  auto entered = entering.get_future();
  auto successor = std::async(std::launch::async, [&] {
    ClientLease lease;
    entering.set_value();
    return lease.Acquire(admission, alpha, ClientLane::kData);
  });
  entered.wait();
  Q4T_CHECK(successor.wait_for(std::chrono::milliseconds(10)) ==
            std::future_status::timeout);
  Q4T_CHECK(admission.Usage(0).data_active == 2);
  ClientLease control, beta;
  Q4T_CHECK(control.Acquire(admission, alpha, ClientLane::kControl));
  Q4T_CHECK(beta.Acquire(admission, admission.Authenticate("beta", kSecret),
                         ClientLane::kData));
  first.reset();
  Q4T_CHECK(successor.get());
  Q4T_CHECK(admission.Usage(0).data_active == 1 &&
            admission.Usage(0).data_rejected == 0);
  return true;
}

Q4T_TEST(client_handoff_stalled_terminal_has_fixed_wait_and_retains_charge) {
  ClientAdmission admission;
  Q4T_CHECK(Configure(&admission));
  const auto alpha = admission.Authenticate("alpha", kSecret);
  ClientLease first, second, denied;
  Q4T_CHECK(first.Acquire(admission, alpha, ClientLane::kData));
  Q4T_CHECK(second.Acquire(admission, alpha, ClientLane::kData));
  first.BeginHandoff();
  const auto start = std::chrono::steady_clock::now();
  Q4T_CHECK(!denied.Acquire(admission, alpha, ClientLane::kData));
  const auto elapsed = std::chrono::steady_clock::now() - start;
  Q4T_CHECK(elapsed >= kClientHandoffWait && elapsed < std::chrono::seconds(2));
  Q4T_CHECK(admission.Usage(0).data_active == 2 &&
            admission.Usage(0).data_rejected == 1);
  return true;
}

Q4T_TEST(client_handoff_http_terminal_publishes_without_releasing_owner) {
  ClientAdmission admission;
  Q4T_CHECK(Configure(&admission));
  const auto alpha = admission.Authenticate("alpha", kSecret);
  auto first = std::make_unique<ClientLease>();
  ClientLease second;
  Q4T_CHECK(first->Acquire(admission, alpha, ClientLane::kData));
  Q4T_CHECK(second.Acquire(admission, alpha, ClientLane::kData));
  int pair[2];
  Q4T_CHECK(::socketpair(AF_UNIX, SOCK_STREAM, 0, pair) == 0);
  ScopeExit close([&] {
    ::close(pair[0]);
    ::close(pair[1]);
  });
  {
    detail::HttpResponseContext response(pair[0]);
    response.SetClient(alpha, first.get());
    Q4T_CHECK(detail::SendSimple(pair[0], 200, "OK", "{}", "application/json"));
  }
  Q4T_CHECK(admission.Usage(0).data_active == 2);
  auto successor = std::async(std::launch::async, [&] {
    ClientLease lease;
    return lease.Acquire(admission, alpha, ClientLane::kData);
  });
  Q4T_CHECK(successor.wait_for(std::chrono::milliseconds(10)) ==
            std::future_status::timeout);
  first.reset();
  Q4T_CHECK(successor.get() && admission.Usage(0).data_active == 1);
  return true;
}

Q4T_TEST(client_namespaces_and_cancellation_ownership) {
  Q4T_CHECK(ClientScopedKey("alpha", "").empty());
  Q4T_CHECK(ClientScopedKey("", "legacy") == "legacy");
  Q4T_CHECK(ClientScopedKey("ab", "c") != ClientScopedKey("a", "bc"));
  Q4T_CHECK(ClientScopedKey("alpha", "same") !=
            ClientScopedKey("beta", "same"));
  RequestRegistry registry;
  auto a = registry.Register("same", "a-key", kDeadline, "alpha");
  auto b = registry.Register("same", "b-key", kDeadline, "beta");
  auto legacy = registry.Register("same", "a-key", kDeadline);
  Q4T_CHECK(a && b && legacy && a->id() == "same" && b->id() == "same");
  Q4T_CHECK(!registry.Register("same", "new", kDeadline, "alpha"));
  Q4T_CHECK(!registry.Cancel("same", "a-key", "beta"));
  Q4T_CHECK(!a->Cancelled() && !b->Cancelled());
  Q4T_CHECK(registry.Cancel("same", "a-key", "alpha"));
  Q4T_CHECK(a->Cancelled() && !b->Cancelled() && !legacy->Cancelled());
  registry.Remove(a);
  auto replacement = registry.Register("same", "fresh", kDeadline, "alpha");
  registry.Remove(a);
  Q4T_CHECK(replacement && !registry.Cancel("same", "a-key", "alpha"));
  Q4T_CHECK(registry.Cancel("same", "fresh", "alpha"));
  registry.CancelAll();
  Q4T_CHECK(b->Cancelled() && legacy->Cancelled());
  return true;
}

Q4T_TEST(client_http_strict_identity_headers) {
  ClientAdmission admission;
  Q4T_CHECK(Configure(&admission));
  const auto valid = Read(admission, Wire(Headers()));
  Q4T_CHECK(valid.ok && valid.scoped && valid.body == "{}" &&
            valid.active == 1);
  Q4T_CHECK(admission.Usage(0).data_active == 0);
  for (const auto& headers :
       {Headers() + "x-q4t-client-id: alpha\r\n",
        Headers() + "x-Q4T-gateway-token: " + kSecret + "\r\n",
        Replace(Headers(), "alpha", "Alpha"), Replace(Headers(), "alpha", ""),
        Replace(Headers(), kSecret, ""),
        Replace(Headers(), kSecret, std::string(64, 'A')),
        Replace(Headers(), kSecret, std::string(65, 'a'))}) {
    const auto result = Read(admission, Wire(headers));
    Q4T_CHECK(!result.ok && !result.admitted && result.body.empty());
    Q4T_CHECK(result.headers.error_code == "invalid_client_header");
  }
  Q4T_CHECK(Read(admission,
                 Wire(Replace(Headers(), "X-Q4T-Client-Id", "x-q4t-client-id")))
                .ok);
  return true;
}

Q4T_TEST(client_http_auth_and_body_reject_before_reservation) {
  ClientAdmission admission, legacy;
  Q4T_CHECK(Configure(&admission));
  for (const auto& headers :
       {std::string(), Headers("unknown"),
        Replace(Headers(), kSecret, std::string(64, 'b'))}) {
    const auto result = Read(admission, Wire(headers));
    Q4T_CHECK(!result.ok && result.status == 401 && result.bytes == 0);
    Q4T_CHECK(result.body.empty() && admission.Usage(0).data_active == 0);
  }
  const auto huge = Read(
      admission,
      Replace(Wire(Headers()), "Content-Length: 2", "Content-Length: 1025"));
  Q4T_CHECK(!huge.ok && huge.status == 413 && huge.bytes == 0);
  Q4T_CHECK(huge.headers.error_code == "client_body_limit_exceeded");
  const auto chunk_head =
      "POST /v1/chat/completions HTTP/1.1\r\nHost: local\r\n" + Headers() +
      "Transfer-Encoding: chunked\r\n\r\n";
  const auto chunk_ok = Read(admission, chunk_head + "2\r\n{}\r\n0\r\n\r\n");
  Q4T_CHECK(chunk_ok.ok && chunk_ok.bytes == 2 && chunk_ok.body == "{}");
  const auto chunk_huge = Read(admission, chunk_head + "401\r\n");
  Q4T_CHECK(!chunk_huge.ok && chunk_huge.status == 413 &&
            chunk_huge.bytes == 0 && admission.Usage(0).data_active == 0);
  const auto truncated =
      Read(admission,
           Replace(Wire(Headers()), "Content-Length: 2", "Content-Length: 10"));
  Q4T_CHECK(!truncated.ok &&
            truncated.headers.failure == HttpReceiveFailure::kBodyEof);
  Q4T_CHECK(admission.Usage(0).data_active == 0);
  Q4T_CHECK(Read(legacy, Wire("")).ok);
  Q4T_CHECK(!Read(legacy, Wire(Headers())).ok);
  return true;
}

Q4T_TEST(client_http_local_admin_exact_routes_and_reserved_control) {
  ClientAdmission admission;
  Q4T_CHECK(Configure(&admission));
  for (const auto* path :
       {"/healthz", "/livez", "/readyz", "/metrics", "/v1/config"}) {
    Q4T_CHECK(Read(admission, Wire("", path, "GET")).ok);
    Q4T_CHECK(Read(admission, Wire(Headers(), path, "GET")).status == 403);
    Q4T_CHECK(
        Read(admission, Wire("", std::string(path) + "?x", "GET")).status ==
        401);
    Q4T_CHECK(Read(admission, Wire("", path, "POST")).status == 401);
  }
  const auto alpha = admission.Authenticate("alpha", kSecret);
  ClientLease first, second;
  Q4T_CHECK(first.Acquire(admission, alpha, ClientLane::kData));
  Q4T_CHECK(second.Acquire(admission, alpha, ClientLane::kData));
  const auto data = Read(admission, Wire(Headers()));
  Q4T_CHECK(!data.ok && data.status == 429 && data.bytes == 0);
  Q4T_CHECK(Read(admission, Wire(Headers(), "/v1/requests/cancel")).ok);
  Q4T_CHECK(Read(admission, Wire(Headers(), "/v1/models", "GET")).ok);
  Q4T_CHECK(Read(admission, Wire(Headers("beta"))).ok);
  Q4T_CHECK(Read(admission, Wire("", "/v1/capabilities", "GET")).status == 401);
  return true;
}

Q4T_TEST(client_response_cancel_reserved_when_data_and_observation_full) {
  ClientAdmission admission;
  Q4T_CHECK(Configure(&admission));
  const auto alpha = admission.Authenticate("alpha", kSecret);
  ClientLease data1, data2, observation;
  Q4T_CHECK(data1.Acquire(admission, alpha, ClientLane::kData));
  Q4T_CHECK(data2.Acquire(admission, alpha, ClientLane::kData));
  Q4T_CHECK(Read(admission, Wire(Headers(), "/v1/responses/resp_1", "GET")).ok);
  Q4T_CHECK(observation.Acquire(admission, alpha, ClientLane::kControl));
  Q4T_CHECK(
      Read(admission, Wire(Headers(), "/v1/responses/resp_1", "GET")).status ==
      429);
  Q4T_CHECK(Read(admission, Wire(Headers(), "/v1/responses/resp_1/cancel")).ok);
  Q4T_CHECK(Read(admission, Wire(Headers(), "/v1/requests/cancel")).ok);
  {
    ClientLease cancel;
    Q4T_CHECK(cancel.Acquire(admission, alpha, ClientLane::kCancel));
    Q4T_CHECK(Read(admission, Wire(Headers(), "/v1/responses/resp_1/cancel"))
                  .status == 429);
    Q4T_CHECK(
        Read(admission, Wire(Headers("beta"), "/v1/responses/resp_1/cancel"))
            .ok);
    const auto metrics = ClientPolicyMetrics(admission);
    Q4T_CHECK(
        metrics.find(
            "q4t_client_requests_active{client=\"alpha\",lane=\"cancel\"} 1") !=
        metrics.npos);
    Q4T_CHECK(metrics.find("q4t_client_capacity_rejected_total{client="
                           "\"alpha\",lane=\"cancel\"} 1") != metrics.npos);
  }
  Q4T_CHECK(admission.Usage(0).cancel_active == 0);
  Q4T_CHECK(Read(admission, Wire(Headers(), "/v1/responses/resp_1/cancel")).ok);
  return true;
}

Q4T_TEST(client_stored_chat_control_scope_is_independent_of_generation) {
  ClientAdmission admission;
  Q4T_CHECK(Configure(&admission));
  const auto alpha = admission.Authenticate("alpha", kSecret);
  ClientLease data1, data2, observation;
  Q4T_CHECK(data1.Acquire(admission, alpha, ClientLane::kData));
  Q4T_CHECK(data2.Acquire(admission, alpha, ClientLane::kData));
  Q4T_CHECK(Read(admission, Wire(Headers(), "/v1/chat/completions")).status ==
            429);
  for (const auto* target :
       {"/v1/chat/completions?limit=1", "/v1/chat/completions/chat_1",
        "/v1/chat/completions/chat_1/messages?order=asc"})
    Q4T_CHECK(Read(admission, Wire(Headers(), target, "GET")).ok);
  Q4T_CHECK(Read(admission, Wire(Headers(), "/v1/chat/completions/chat_1")).ok);
  Q4T_CHECK(observation.Acquire(admission, alpha, ClientLane::kControl));
  Q4T_CHECK(
      Read(admission, Wire(Headers(), "/v1/chat/completions", "GET")).status ==
      429);
  Q4T_CHECK(
      Read(admission, Wire(Headers("beta"), "/v1/chat/completions", "GET")).ok);
  Q4T_CHECK(Read(admission, Wire(Headers(), "/v1/requests/cancel")).ok);
  Q4T_CHECK(
      Read(admission, Wire(Headers("foreign"), "/v1/chat/completions", "GET"))
          .status == 401);
  return true;
}

Q4T_TEST(client_token_budget_defaults_aliases_and_encoded_boundaries) {
  ClientAdmission admission;
  Q4T_CHECK(Configure(&admission));
  ChatPlanConfig config;
  const auto identity = admission.RecoveryIdentity(0);
  config.client_limits = identity.Limits();
  ChatRequestPlan plan;
  ChatPromptBudget budget;
  ChatPlanError error;
  for (const auto* extra :
       {"", R"(,"max_tokens":null)", R"(,"max_completion_tokens":null)",
        R"(,"max_tokens":16)", R"(,"max_completion_tokens":16)"}) {
    Json req;
    Q4T_CHECK(q4t::io::ParseJson(std::string(R"({"prompt":"x")") + extra + '}',
                                 &req));
    Q4T_CHECK(PlanChatRequest(req, config, &plan, &error));
    Q4T_CHECK(ChatOutputLimit(req, config) == 16);
    std::vector<uint32_t> tokens;
    Q4T_CHECK(EncodeChatPrompt(
        req, config, "rendered template",
        [](std::string_view, std::vector<uint32_t>* out) {
          out->assign(64, 7);
          return q4t::Status();
        },
        &tokens, &budget, &error));
    Q4T_CHECK(budget.requested_output == 16 && budget.available_output == 16);
    Q4T_CHECK(!PlanChatBudget(req, config, 65, &budget, &error));
    Q4T_CHECK(error.code == "client_prompt_limit_exceeded" &&
              error.param == "prompt");
  }
  for (const auto* field : {"max_tokens", "max_completion_tokens"}) {
    Json req;
    Q4T_CHECK(q4t::io::ParseJson(
        std::string(R"({"prompt":"x",")") + field + "\":17}", &req));
    Q4T_CHECK(!PlanChatRequest(req, config, &plan, &error));
    Q4T_CHECK(error.code == "client_output_limit_exceeded" &&
              error.param == field);
    Q4T_CHECK(!PlanChatBudget(req, config, 1, &budget, &error));
  }
  config.default_max_tokens = 8;
  Json req;
  Q4T_CHECK(q4t::io::ParseJson(R"({"prompt":"x"})", &req));
  Q4T_CHECK(ChatOutputLimit(req, config) == 8);
  return true;
}

Q4T_TEST(client_policy_config_redaction_and_cli_boundary) {
  ClientAdmission admission;
  Q4T_CHECK(Configure(&admission));
  ServerOptions options;
  options.model_dir = "readonly";
  const std::string_view args[] = {"--client-policy-file", "secret-path"};
  Q4T_CHECK(ParseServerOptions(args, &options));
  Q4T_CHECK(options.client_policy_file == "secret-path");
  Q4T_CHECK(ValidateServerOptions(options));
  options.host = "0.0.0.0";
  Q4T_CHECK(!ValidateServerOptions(options));
  options.host = "127.0.0.1";
  options.allow_media = true;
  Q4T_CHECK(!ValidateServerOptions(options));
  options.allow_media = false;
  ChatPlanConfig config;
  config.client_policy_sha256 = admission.PolicySha256();
  config.client_count = admission.Size();
  const auto encoded = EffectiveServeConfigJson(options, config, true, true, 8);
  Json parsed;
  Q4T_CHECK(q4t::io::ParseJson(encoded, &parsed));
  Q4T_CHECK(parsed.Find("client_policy")->GetBool("enabled"));
  Q4T_CHECK(parsed.Find("client_policy")->GetInt("clients") == 2);
  for (const auto& secret :
       {kSecret, std::string("secret-path"), std::string("alpha")})
    Q4T_CHECK(encoded.find(secret) == encoded.npos);
  return true;
}

Q4T_TEST(client_rate_completion_batch_charges_only_admitted_prompts_once) {
  ClientAdmission admission;
  Q4T_CHECK(admission.Configure(RatePolicy(10, 200)));
  const auto alpha = admission.Authenticate("alpha", kSecret);
  const auto now = ClientClock::now();
  ClientRateLease batch;
  Q4T_CHECK(batch.Reserve(admission, alpha, 50, 16, now) ==
            ClientRateResult::kAllowed);
  batch.StartPrompt(10);
  batch.Completion(7);
  batch.StartPrompt(10);  // Another candidate for the same prompt.
  batch.Completion(12);
  batch.StartPrompt(25);  // Second prompt admitted; third never starts.
  batch.Completion(14);
  batch.Settle(now);
  batch.Settle(now);
  const auto value = admission.RateUsage(0, now);
  Q4T_CHECK(value.tokens == 39 && value.settled_tokens == 39 &&
            value.reserved == 0 && value.requests == 1);
  return true;
}

Q4T_TEST(client_rate_auxiliary_output_is_bounded_charged_and_shares_one_rpm) {
  ClientAdmission admission;
  Q4T_CHECK(admission.Configure(DynamicPolicy(1, 120)));
  const auto client = admission.Authenticate("alpha", kSecret);
  const auto now = ClientClock::now();
  ClientRateLease rate;
  Q4T_CHECK(rate.Reserve(admission, client, 20, 16, now, 64) ==
            ClientRateResult::kAllowed);
  Q4T_CHECK(admission.RateUsage(0, now).reserved == 100 &&
            admission.RateUsage(0, now).requests == 1);
  rate.Start();
  rate.Completion(12);  // Actual classification output.
  Q4T_CHECK(rate.ExtendPrompt(admission, client, 10, now) ==
            ClientRateResult::kAllowed);
  rate.StartPrompt(30);
  rate.Completion(12 + 6);  // Classification plus business generation.
  rate.Settle(now);
  const auto usage = admission.RateUsage(0, now);
  Q4T_CHECK(usage.requests == 1 && usage.admitted == 1 && usage.reserved == 0 &&
            usage.tokens == 48 && usage.settled_tokens == 48);
  return true;
}

Q4T_TEST(client_rate_auxiliary_cannot_bypass_tpm_or_public_output_limit) {
  ClientAdmission admission;
  Q4T_CHECK(admission.Configure(DynamicPolicy(10, 60)));
  const auto client = admission.Authenticate("alpha", kSecret);
  const auto now = ClientClock::now();
  ClientRateLease quota, capacity, public_limit, overflow;
  Q4T_CHECK(quota.Reserve(admission, client, 20, 16, now, 64) ==
            ClientRateResult::kTpm);
  Q4T_CHECK(capacity.Reserve(admission, client, 1, 1, now,
                             kMaxAuxiliaryOutputTokens + 1) ==
            ClientRateResult::kInvalid);
  Q4T_CHECK(public_limit.Reserve(admission, client, 1, 17, now, 1) ==
            ClientRateResult::kInvalid);
  Q4T_CHECK(overflow.Reserve(admission, client, UINT64_MAX, 1, now, 1) ==
            ClientRateResult::kInvalid);
  const auto usage = admission.RateUsage(0, now);
  Q4T_CHECK(usage.requests == 0 && usage.admitted == 0 && usage.reserved == 0);
  return true;
}

Q4T_TEST(client_rate_internal_step_preserves_one_rpm_and_charges_actual_input) {
  for (const bool admitted : {false, true}) {
    ClientAdmission admission;
    Q4T_CHECK(admission.Configure(DynamicPolicy(1, 100)));
    const auto client = admission.Authenticate("alpha", kSecret);
    const auto now = ClientClock::now();
    ClientRateLease rate;
    Q4T_CHECK(rate.Reserve(admission, client, 20, 16, now) ==
              ClientRateResult::kAllowed);
    rate.Start();
    rate.Completion(6);
    Q4T_CHECK(rate.ExtendPrompt(admission, client, 30, now) ==
              ClientRateResult::kAllowed);
    Q4T_CHECK(admission.RateUsage(0, now).reserved == 66 &&
              admission.RateUsage(0, now).requests == 1);
    if (admitted) {
      rate.StartPrompt(50);
      rate.Completion(10);
    }
    rate.Settle(now);
    const auto usage = admission.RateUsage(0, now);
    Q4T_CHECK(usage.requests == 1 && usage.admitted == 1 &&
              usage.reserved == 0 && usage.tokens == (admitted ? 60u : 26u));
    Q4T_CHECK(rate.ExtendPrompt(admission, client, 1, now) ==
              ClientRateResult::kInvalid);
  }
  return true;
}

Q4T_TEST(client_rate_internal_step_rejection_keeps_original_reservation) {
  ClientAdmission admission;
  Q4T_CHECK(admission.Configure(DynamicPolicy(1, 60)));
  const auto client = admission.Authenticate("alpha", kSecret);
  const auto now = ClientClock::now();
  ClientRateLease rate;
  Q4T_CHECK(rate.Reserve(admission, client, 20, 16, now) ==
            ClientRateResult::kAllowed);
  Q4T_CHECK(rate.ExtendPrompt(admission, client, 1, now) ==
            ClientRateResult::kInvalid);
  rate.Start();
  Q4T_CHECK(rate.ExtendPrompt(admission, client, 30, now) ==
            ClientRateResult::kTpm);
  Q4T_CHECK(rate.ExtendPrompt(admission, client, 45, now) ==
            ClientRateResult::kInvalid);
  Q4T_CHECK(rate.ExtendPrompt(admission,
                              admission.Authenticate("beta", kSecret), 1,
                              now) == ClientRateResult::kInvalid);
  const auto failed = admission.RateUsage(0, now);
  Q4T_CHECK(failed.reserved == 36 && failed.requests == 1 &&
            failed.tpm_rejected == 1);
  Q4T_CHECK(rate.ExtendPrompt(admission, client, 24, now) ==
            ClientRateResult::kAllowed);
  rate.StartPrompt(44);
  rate.Completion(8);
  rate.Settle(now);
  Q4T_CHECK(admission.RateUsage(0, now).tokens == 52);
  return true;
}

Q4T_TEST(client_rate_internal_step_legacy_is_bound_and_cumulative) {
  ClientAdmission admission;
  Q4T_CHECK(Configure(&admission));
  const auto client = admission.Authenticate("alpha", kSecret);
  const auto wrong = admission.Authenticate("beta", kSecret);
  ClientRateLease failed;
  Q4T_CHECK(failed.Reserve(admission, client, 65, 16) ==
            ClientRateResult::kInvalid);
  failed.Start();
  Q4T_CHECK(failed.ExtendPrompt(admission, client, 1) ==
            ClientRateResult::kInvalid);
  ClientRateLease rate;
  Q4T_CHECK(rate.Reserve(admission, client, 20, 16) ==
            ClientRateResult::kAllowed);
  rate.Start();
  Q4T_CHECK(rate.ExtendPrompt(admission, wrong, 10) ==
            ClientRateResult::kInvalid);
  Q4T_CHECK(rate.ExtendPrompt(admission, client, 30) ==
            ClientRateResult::kAllowed);
  Q4T_CHECK(rate.ExtendPrompt(admission, client, 15) ==
            ClientRateResult::kInvalid);
  Q4T_CHECK(rate.ExtendPrompt(admission, client, 14) ==
            ClientRateResult::kAllowed);
  rate.Settle();
  Q4T_CHECK(rate.ExtendPrompt(admission, client, 0) ==
            ClientRateResult::kInvalid);
  return true;
}

Q4T_TEST(client_rate_internal_step_window_and_freeze_do_not_reset_admission) {
  ClientAdmission admission;
  Q4T_CHECK(admission.Configure(DynamicPolicy(1, 100)));
  Q4T_CHECK(admission.EnableRateRecovery());
  Q4T_CHECK(admission.RestoreRates(nullptr, std::string(64, '1')));
  const auto client = admission.Authenticate("alpha", kSecret);
  const auto now = ClientClock::now();
  ClientRateLease rate;
  Q4T_CHECK(rate.Reserve(admission, client, 20, 16, now) ==
            ClientRateResult::kAllowed);
  rate.Start();
  const auto later = now + std::chrono::seconds(60);
  Q4T_CHECK(rate.ExtendPrompt(admission, client, 20, later) ==
            ClientRateResult::kAllowed);
  const auto usage = admission.RateUsage(0, later);
  Q4T_CHECK(usage.requests == 0 && usage.admitted == 1 && usage.reserved == 56);
  ClientRateState state;
  Q4T_CHECK(!admission.FreezeRates(&state, later));
  Q4T_CHECK(rate.ExtendPrompt(admission, client, 1, later) ==
            ClientRateResult::kFrozen);
  rate.StartPrompt(40);
  rate.Completion(9);
  rate.Settle(later);
  Q4T_CHECK(admission.FreezeRates(&state, later));
  Q4T_CHECK(state.clients[0].usage.tokens == 49 &&
            state.clients[0].usage.reserved == 0);
  return true;
}

Q4T_TEST(client_rate_checkpoint_drains_reservations_and_never_reopens) {
  ClientAdmission admission;
  Q4T_CHECK(admission.Configure(DynamicPolicy(3, 100)));
  Q4T_CHECK(admission.EnableRateRecovery());
  Q4T_CHECK(admission.RestoreRates(nullptr, std::string(64, '1')));
  const auto alpha = admission.Authenticate("alpha", kSecret);
  ClientRateLease pending;
  Q4T_CHECK(pending.Reserve(admission, alpha, 0, 0) ==
            ClientRateResult::kAllowed);
  ClientRateState state;
  Q4T_CHECK(!admission.FreezeRates(&state));
  Q4T_CHECK(admission.RatesFrozen() && state.clients.empty());
  ClientRateLease denied;
  Q4T_CHECK(denied.Reserve(admission, alpha, 3, 5) ==
            ClientRateResult::kFrozen);
  Q4T_CHECK(!admission.Publish(DynamicPolicy(10, 100, 1), 0, 8, 8));
  ClientLease cancel;
  Q4T_CHECK(cancel.Acquire(admission, alpha, ClientLane::kCancel));
  pending.Settle();
  Q4T_CHECK(admission.FreezeRates(&state));
  Q4T_CHECK(state.clients[0].usage.requests == 1 &&
            state.clients[0].usage.reserved == 0);
  Q4T_CHECK(admission.RestoreRates(nullptr, std::string(64, '1')));
  Q4T_CHECK(admission.RatesFrozen());  // Idempotent reply does not thaw.
  return true;
}

Q4T_TEST(client_rate_restart_restores_live_window_and_cumulative_usage) {
  const auto now = ClientClock::now();
  ClientAdmission old, next;
  for (auto* admission : {&old, &next}) {
    Q4T_CHECK(admission->Configure(DynamicPolicy(1, 100)));
    Q4T_CHECK(admission->EnableRateRecovery());
  }
  Q4T_CHECK(old.RestoreRates(nullptr, std::string(64, '1'), now));
  ClientRateLease request;
  Q4T_CHECK(request.Reserve(old, old.Authenticate("alpha", kSecret), 3, 5,
                            now) == ClientRateResult::kAllowed);
  request.Start();
  request.Completion(2);
  request.Settle(now + std::chrono::seconds(1));
  ClientRateState state;
  Q4T_CHECK(old.FreezeRates(&state, now + std::chrono::seconds(2)));
  Q4T_CHECK(next.RestoreRates(&state, std::string(64, '2'),
                              now + std::chrono::seconds(10)));
  ClientRateLease denied;
  Q4T_CHECK(denied.Reserve(next, next.Authenticate("alpha", kSecret), 1, 1,
                           now + std::chrono::seconds(11)) ==
            ClientRateResult::kRpm);
  const auto usage = next.RateUsage(0, now + std::chrono::seconds(11));
  Q4T_CHECK(usage.tokens == 5 && usage.admitted == 1 &&
            usage.settled_tokens == 5 && usage.window_remaining_ms == 49000);
  ClientRateLease fresh;
  Q4T_CHECK(fresh.Reserve(next, next.Authenticate("alpha", kSecret), 1, 1,
                          now + std::chrono::seconds(60)) ==
            ClientRateResult::kAllowed);
  fresh.Settle(now + std::chrono::seconds(60));
  const auto expired = next.RateUsage(0, now + std::chrono::seconds(60));
  Q4T_CHECK(expired.requests == 1 && expired.tokens == 0 &&
            expired.admitted == 2 && expired.settled_tokens == 5);
  return true;
}

Q4T_TEST(client_rate_restore_is_one_shot_transactional_and_identity_bound) {
  ClientAdmission admission;
  Q4T_CHECK(admission.Configure(DynamicPolicy(10, 100)));
  Q4T_CHECK(!admission.RestoreRates(nullptr, std::string(64, '1')));
  Q4T_CHECK(admission.EnableRateRecovery());
  Q4T_CHECK(!admission.EnableRateRecovery());
  ClientRateLease check;
  Q4T_CHECK(check.Reserve(admission, admission.Authenticate("alpha", kSecret),
                          3, 5) == ClientRateResult::kAllowed);
  Q4T_CHECK(!admission.RestoreRates(nullptr, std::string(64, '1')));
  check.Settle();
  ClientAdmission old;
  Q4T_CHECK(old.Configure(DynamicPolicy(10, 100)));
  Q4T_CHECK(old.EnableRateRecovery());
  Q4T_CHECK(old.RestoreRates(nullptr, std::string(64, '2')));
  ClientRateState state;
  Q4T_CHECK(old.FreezeRates(&state));
  for (int fault = 0; fault < 5; ++fault) {
    auto bad = state;
    if (fault == 0) bad.clients[0].id = "foreign";
    if (fault == 1) ++bad.policy_revision;
    if (fault == 2) bad.policy_sha256 = std::string(64, 'f');
    if (fault == 3) bad.captured_ns += 60000000000;
    if (fault == 4) bad.clients[0].usage.reserved = 1;
    Q4T_CHECK(!admission.RestoreRates(&bad, std::string(64, '3')));
    Q4T_CHECK(!admission.RatesRecovered());
    Q4T_CHECK(admission.RateUsage(0).admitted == 1);
  }
  Q4T_CHECK(admission.RestoreRates(&state, std::string(64, '3')));
  Q4T_CHECK(admission.RateUsage(0).admitted == 0);  // Bootstrap only.
  ClientRateLease user;
  Q4T_CHECK(user.Reserve(admission, admission.Authenticate("alpha", kSecret), 3,
                         5) == ClientRateResult::kAllowed);
  user.Settle();
  Q4T_CHECK(admission.RestoreRates(&state, std::string(64, '3')));
  Q4T_CHECK(!admission.RestoreRates(nullptr, std::string(64, '4')));
  Q4T_CHECK(admission.RateUsage(0).admitted == 1);
  return true;
}

Q4T_TEST(client_rate_restore_allows_appended_slots_and_reduced_limits) {
  const auto now = ClientClock::now();
  ClientAdmission old, next;
  Q4T_CHECK(old.Configure(DynamicPolicy(0, 0)));
  Q4T_CHECK(old.EnableRateRecovery());
  Q4T_CHECK(old.RestoreRates(nullptr, std::string(64, '1')));
  ClientRateLease used;
  Q4T_CHECK(used.Reserve(old, old.Authenticate("alpha", kSecret), 3, 5, now) ==
            ClientRateResult::kAllowed);
  used.Start();
  used.Completion(2);
  used.Settle(now);
  ClientRateState snapshot;
  Q4T_CHECK(old.FreezeRates(&snapshot, now));
  auto policy = DynamicPolicy(0, 4, 1);
  auto extra = policy.clients.front();
  extra.id = "gamma";
  policy.clients.push_back(extra);
  Q4T_CHECK(next.Configure(policy));
  Q4T_CHECK(next.EnableRateRecovery());
  Q4T_CHECK(next.RestoreRates(&snapshot, std::string(64, '2'), now));
  Q4T_CHECK(next.RateUsage(0, now).tokens == 5);
  ClientRateLease denied, gamma;
  Q4T_CHECK(denied.Reserve(next, next.Authenticate("alpha", kSecret), 1, 1,
                           now) == ClientRateResult::kTpm);
  Q4T_CHECK(gamma.Reserve(next, next.Authenticate("gamma", kSecret), 1, 1,
                          now) == ClientRateResult::kAllowed);
  gamma.Settle(now);
  return true;
}

Q4T_TEST(client_rate_snapshot_wire_strict_large_and_uint64_exact) {
  ClientAdmission admission;
  Q4T_CHECK(admission.Configure(DynamicPolicy(0, 0)));
  Q4T_CHECK(admission.EnableRateRecovery());
  Q4T_CHECK(admission.RestoreRates(nullptr, std::string(64, '1')));
  ClientRateState snapshot, parsed;
  Q4T_CHECK(admission.FreezeRates(&snapshot));
  auto record = snapshot.clients[0];
  record.window_started = true;
  record.window_ns = snapshot.captured_ns;
  record.usage.requests = record.usage.admitted = UINT64_MAX;
  record.usage.tokens = record.usage.settled_tokens = UINT64_MAX;
  record.usage.rpm_rejected = record.usage.tpm_rejected = UINT64_MAX;
  snapshot.clients.clear();
  for (size_t i = 0; i < kMaxClients; ++i) {
    record.id = "client_" + std::to_string(i);
    snapshot.clients.push_back(record);
  }
  const auto raw = EncodeClientRateState(snapshot);
  Q4T_CHECK(!raw.empty() && raw.size() < kClientRateStateBytes);
  Q4T_CHECK(ParseClientRateState(raw, &parsed));
  Q4T_CHECK(parsed.clients.size() == 64 &&
            parsed.clients[0].usage.tokens == UINT64_MAX);
  for (const auto& bad :
       {Replace(raw, "\"version\":1", "\"version\":1,\"version\":1"),
        Replace(raw, "\"18446744073709551615\"", "18446744073709551615"),
        Replace(raw, "18446744073709551615", "18446744073709551616"),
        Replace(raw, "\"policy_revision\":\"0\"", "\"policy_revision\":\"00\""),
        Replace(raw, "client_1\"", "client_0\""), raw + "true"}) {
    Q4T_CHECK(!ParseClientRateState(bad, &parsed));
    Q4T_CHECK(parsed.clients[0].usage.tokens == UINT64_MAX);
  }
  return true;
}

Q4T_TEST(client_rate_restored_counter_saturation_cannot_bypass_tpm) {
  ClientAdmission old, next;
  Q4T_CHECK(old.Configure(DynamicPolicy(0, 0)));
  Q4T_CHECK(old.EnableRateRecovery());
  Q4T_CHECK(old.RestoreRates(nullptr, std::string(64, '1')));
  ClientRateState state;
  Q4T_CHECK(old.FreezeRates(&state));
  auto& record = state.clients[0];
  record.window_started = true;
  record.window_ns = state.captured_ns;
  record.usage.requests = record.usage.admitted = UINT64_MAX;
  record.usage.tokens = record.usage.settled_tokens = UINT64_MAX;
  Q4T_CHECK(next.Configure(DynamicPolicy(0, 0)));
  Q4T_CHECK(next.EnableRateRecovery());
  Q4T_CHECK(next.RestoreRates(&state, std::string(64, '2')));
  ClientRateLease accepted;
  Q4T_CHECK(accepted.Reserve(next, next.Authenticate("alpha", kSecret), 3, 5) ==
            ClientRateResult::kAllowed);
  accepted.Start();
  accepted.Completion(2);
  accepted.Settle();
  Q4T_CHECK(next.RateUsage(0).admitted == UINT64_MAX &&
            next.RateUsage(0).tokens == UINT64_MAX);
  Q4T_CHECK(next.Publish(DynamicPolicy(0, 10, 1), 0, 8, 8));
  ClientRateLease denied;
  Q4T_CHECK(denied.Reserve(next, next.Authenticate("alpha", kSecret), 3, 5) ==
            ClientRateResult::kTpm);
  return true;
}

Q4T_TEST(client_rate_freeze_race_preserves_every_admitted_request) {
  ClientAdmission admission;
  Q4T_CHECK(admission.Configure(DynamicPolicy(0, 0)));
  Q4T_CHECK(admission.EnableRateRecovery());
  Q4T_CHECK(admission.RestoreRates(nullptr, std::string(64, '1')));
  std::barrier start(5);
  std::atomic<uint64_t> accepted{0};
  std::vector<std::jthread> workers;
  for (int i = 0; i < 4; ++i) {
    workers.emplace_back([&] {
      const auto identity = admission.Authenticate("alpha", kSecret);
      start.arrive_and_wait();
      for (int j = 0; j < 100; ++j) {
        ClientRateLease lease;
        if (lease.Reserve(admission, identity, 3, 5) !=
            ClientRateResult::kAllowed)
          break;
        ++accepted;
        lease.Start();
        lease.Completion(2);
      }
    });
  }
  start.arrive_and_wait();
  ClientRateState snapshot;
  (void)admission.FreezeRates(&snapshot);
  workers.clear();
  Q4T_CHECK(admission.FreezeRates(&snapshot));
  Q4T_CHECK(snapshot.clients[0].usage.admitted == accepted.load() &&
            snapshot.clients[0].usage.tokens == accepted.load() * 5);
  return true;
}
