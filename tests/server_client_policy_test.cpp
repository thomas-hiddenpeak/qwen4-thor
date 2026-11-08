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

Q4T_TEST(client_token_budget_defaults_aliases_and_encoded_boundaries) {
  ClientAdmission admission;
  Q4T_CHECK(Configure(&admission));
  ChatPlanConfig config;
  config.client_limits = &admission.Limits(0);
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
