#include "q4t/server/batch_execution.h"

#include <sys/socket.h>
#include <unistd.h>

#include <barrier>
#include <cerrno>
#include <cstdlib>
#include <ctime>
#include <filesystem>
#include <future>
#include <thread>

#include "../src/server/object_protocol.h"
#include "q4t/server/http_response.h"
#include "q4t/server/lifetime.h"
#include "q4t/server/responses_api.h"
#include "q4t/server/responses_jobs.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
struct Fixture {
  ClientAdmission clients;
  ClientIdentity alpha, beta;
  explicit Fixture(bool rate_limits = false) {
    GatewayClientPolicy policy;
    const std::string secret(64, 'a');
    const auto entry = [rate_limits](std::string name) {
      return "{\"id\":\"" + name +
             "\",\"max_inflight\":2,\"max_control_inflight\":1,"
             "\"max_prompt_tokens\":4096,\"max_output_tokens\":64,"
             "\"max_body_bytes\":4096" +
             (rate_limits ? ",\"requests_per_minute\":10,"
                            "\"tokens_per_minute\":8192}"
                          : "}");
    };
    const auto raw = std::string("{\"version\":") +
                     (rate_limits ? "2,\"fair_queue\":true" : "1") +
                     ",\"gateway_token\":\"" + secret + "\",\"clients\":[" +
                     entry("alpha") + ',' + entry("beta") + "]}";
    if (!ParseGatewayClientPolicy(raw, &policy) ||
        !clients.Configure(std::move(policy)))
      throw std::runtime_error("Batch execution policy fixture");
    alpha = clients.Authenticate("alpha", secret);
    beta = clients.Authenticate("beta", secret);
  }
};
std::shared_ptr<RequestControl> Control(
    std::chrono::milliseconds lifetime = 5s) {
  return std::make_shared<RequestControl>("req_tool_run_fixture", "",
                                           Clock::now() + lifetime, "alpha");
}
Json Parsed(const std::string& text) {
  Json value;
  if (!Parse(text, &value)) throw std::runtime_error("finite JSON fixture");
  return value;
}
bool Ok(const ObjectReply& reply) {
  if (!reply.ok())
    std::fprintf(stderr, "%s: %s\n", reply.code.c_str(), reply.message.c_str());
  return reply.ok();
}
bool Wait(const std::function<bool()>& predicate) {
  const auto deadline = Clock::now() + 3s;
  while (Clock::now() < deadline) {
    if (predicate()) return true;
    std::this_thread::sleep_for(1ms);
  }
  return false;
}
ObjectStoreOptions Storage() {
  const auto* output = std::getenv("Q4T_TEST_OUTPUT_DIR");
  const auto root = std::filesystem::absolute(
      output ? output : ".q4t-work/batch-execution");
  std::filesystem::create_directories(root);
  auto path = (root / "batch-execution-XXXXXX").string();
  if (!mkdtemp(path.data())) throw std::runtime_error("Batch storage fixture");
  ObjectStoreOptions options;
  options.directory = path + "/objects";
  options.restart_backoff_ms = 0;
  return options;
}
constexpr const char* kBody =
    R"({"id":"chatcmpl_cpu_fixture","choices":[{"message":{"role":"assistant","content":null,"tool_calls":[{"id":"call_1","type":"function","function":{"name":"lookup","arguments":"{\"id\":42}"}}]},"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":19,"completion_tokens":7,"total_tokens":26},"exact":1e-999})";
std::shared_ptr<ClientRateLease> Precharge(Fixture& fixture,
                                           const ClientIdentity& client) {
  auto rate = std::make_shared<ClientRateLease>(ClientRateDomain::kBatch);
  if (rate->PrechargeBatch(fixture.clients, client, 128, 32) !=
      ClientRateResult::kAllowed)
    throw std::runtime_error("CPU Batch precharge");
  return rate;
}
ClientRateUsage Usage(Fixture& fixture, size_t client = 0) {
  return fixture.clients.RateUsage(client, Clock::now(),
                                   ClientRateDomain::kBatch);
}
std::unique_ptr<ResponsesExecution> Background(ObjectStore& objects) {
  auto run = std::make_unique<ResponsesExecution>(objects);
  ChatPlanConfig config;
  config.model = "cpu";
  if (!run->Prepare(
              Parsed(
                  R"({"input":"question","background":true,"max_output_tokens":32,"reasoning":{"effort":"none"}})"),
              config, {"client:alpha", Clock::now() + 5s, {}},
              std::time(nullptr), true)
           .ok() ||
      !run->BindRateDomain(ClientRateDomain::kBatch))
    throw std::runtime_error("CPU Batch background");
  return run;
}
}  // namespace

Q4T_TEST(batch_execution_complete_tools_usage_and_parent_transport_are_preserved) {
  Fixture fixture;
  ClientLease permit;
  Q4T_CHECK(permit.Acquire(fixture.clients, fixture.alpha, ClientLane::kData));
  int sockets[2];
  Q4T_CHECK(socketpair(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0, sockets) == 0);
  ScopeExit close([&] { ::close(sockets[0]); ::close(sockets[1]); });
  detail::HttpResponseContext parent(sockets[0]);
  parent.SetTraceId("original-parent");
  auto control = Control();
  bool owner_seen = false;
  const auto result = ExecuteBatchEndpoint(
      control->id(), fixture.alpha, permit, control, [&] {
        const auto* current = detail::HttpResponseContext::Current(-1);
        owner_seen = current && current->Client().Id() == "alpha" &&
                     current->RateDomain() == ClientRateDomain::kBatch &&
                     current->TraceId() == control->id() &&
                     fixture.clients.Usage(0).data_active == 1;
        detail::SendSimple(-1, 200, "OK", kBody, "application/json");
      });
  Q4T_CHECK(owner_seen && result.error.ok() && result.http_status == 200 &&
            result.request_id == control->id() && result.response_body == kBody);
  Q4T_CHECK(detail::HttpResponseContext::Current(sockets[0]) == &parent &&
            *parent.State() == detail::ResponseState::kUnsent &&
            parent.RateDomain() == ClientRateDomain::kOnline &&
            parent.TraceId() == "original-parent");
  char byte;
  Q4T_CHECK(recv(sockets[1], &byte, 1, MSG_DONTWAIT) == -1 &&
            (errno == EAGAIN || errno == EWOULDBLOCK));
  Q4T_CHECK(permit.Holds(fixture.alpha, ClientLane::kData) &&
            fixture.clients.Usage(0).data_active == 1);
  return true;
}

Q4T_TEST(batch_execution_http_errors_are_complete_responses_not_orchestration_errors) {
  Fixture fixture;
  ClientLease permit;
  Q4T_CHECK(permit.Acquire(fixture.clients, fixture.alpha, ClientLane::kData));
  auto control = Control();
  const auto result = ExecuteBatchEndpoint(
      control->id(), fixture.alpha, permit, control, [] {
        detail::SendError(-1, 429, "Actual quota boundary", {},
                          "client_tpm_exhausted", "max_tokens");
      });
  Q4T_CHECK(result.error.ok() && result.http_status == 429);
  const auto body = Parsed(result.response_body);
  Q4T_CHECK(body.Find("error")->GetString("code") == "client_tpm_exhausted" &&
            body.Find("error")->GetString("param") == "max_tokens");
  return true;
}

Q4T_TEST(batch_execution_wrong_or_unowned_permit_never_invokes_endpoint) {
  Fixture fixture;
  for (int i = 0; i < 4; ++i) {
    ClientLease permit;
    auto control = Control();
    auto identity = fixture.alpha;
    if (i == 1)
      Q4T_CHECK(permit.Acquire(fixture.clients, identity, ClientLane::kControl));
    if (i >= 2)
      Q4T_CHECK(permit.Acquire(fixture.clients, identity, ClientLane::kData));
    if (i == 2) identity = fixture.beta;
    const auto id = i == 3 ? "req_different" : control->id();
    bool invoked = false;
    const auto result = ExecuteBatchEndpoint(id, identity, permit, control,
                                              [&] { invoked = true; });
    Q4T_CHECK(!invoked && result.response_body.empty() &&
              result.error.code == "batch_execution_owner_invalid");
  }
  return true;
}

Q4T_TEST(batch_execution_transferred_permit_has_one_bound_owner) {
  Fixture fixture;
  ClientLease original, destination;
  Q4T_CHECK(original.Acquire(fixture.clients, fixture.alpha, ClientLane::kData));
  Q4T_CHECK(original.TransferTo(destination));
  Q4T_CHECK(!original.Holds(fixture.alpha, ClientLane::kData) &&
            destination.Holds(fixture.alpha, ClientLane::kData) &&
            !destination.Holds(fixture.beta, ClientLane::kData) &&
            !destination.Holds(fixture.alpha, ClientLane::kControl));
  auto control = Control();
  bool invoked = false;
  Q4T_CHECK(!ExecuteBatchEndpoint(control->id(), fixture.alpha, original,
                                  control, [&] { invoked = true; }).error.ok());
  Q4T_CHECK(!invoked && fixture.clients.Usage(0).data_active == 1);
  const auto result = ExecuteBatchEndpoint(
      control->id(), fixture.alpha, destination, control, [] {
        detail::SendSimple(-1, 200, "OK", "{}", "application/json");
      });
  Q4T_CHECK(result.error.ok() && result.http_status == 200);
  return true;
}

Q4T_TEST(batch_execution_cancel_deadline_and_drained_incomplete_work_never_replay) {
  Fixture fixture;
  ClientLease permit;
  Q4T_CHECK(permit.Acquire(fixture.clients, fixture.alpha, ClientLane::kData));
  for (int i = 0; i < 3; ++i) {
    auto control = Control(i == 1 ? -1ms : 5s);
    if (i == 0) control->Cancel(RequestControl::State::kExplicit);
    int calls = 0;
    bool drained = false;
    auto result = ExecuteBatchEndpoint(
        control->id(), fixture.alpha, permit, control, [&] {
          ++calls;
          ScopeExit cleanup([&] { drained = true; });
          control->Cancel(RequestControl::State::kExplicit);
        });
    Q4T_CHECK(calls == (i == 2 ? 1 : 0) && drained == (i == 2) &&
              result.http_status == 0 && result.response_body.empty() &&
              result.error.code == (i == 1 ? "request_deadline_exceeded"
                                          : "batch_cancelled"));
    result = ExecuteBatchEndpoint(control->id(), fixture.alpha, permit,
                                    control, [&] { ++calls; });
    Q4T_CHECK(!result.error.ok() && calls == (i == 2 ? 1 : 0));
  }
  return true;
}

Q4T_TEST(batch_execution_concurrent_claim_and_lost_result_cannot_invoke_twice) {
  Fixture fixture;
  ClientLease permit;
  Q4T_CHECK(permit.Acquire(fixture.clients, fixture.alpha, ClientLane::kData));
  auto control = Control();
  std::barrier start(2);
  std::atomic<int> invoked = 0;
  auto task = [&] {
    start.arrive_and_wait();
    return ExecuteBatchEndpoint(control->id(), fixture.alpha, permit, control,
                                 [&] { ++invoked; });
  };
  auto a = std::async(std::launch::async, task);
  auto b = std::async(std::launch::async, task);
  const auto first = a.get(), second = b.get();
  Q4T_CHECK(invoked == 1 &&
            ((first.error.code == "batch_response_incomplete" &&
              second.error.code == "batch_execution_already_claimed") ||
             (second.error.code == "batch_response_incomplete" &&
              first.error.code == "batch_execution_already_claimed")));
  const auto again = ExecuteBatchEndpoint(
      control->id(), fixture.alpha, permit, control, [&] { ++invoked; });
  Q4T_CHECK(invoked == 1 &&
            again.error.code == "batch_execution_already_claimed");
  return true;
}

Q4T_TEST(batch_execution_exceptions_drain_then_keep_native_error_contract) {
  Fixture fixture;
  for (bool memory : {false, true}) {
    ClientLease permit;
    Q4T_CHECK(permit.Acquire(fixture.clients, fixture.alpha, ClientLane::kData));
    auto control = Control();
    bool drained = false;
    const auto result = ExecuteBatchEndpoint(
        control->id(), fixture.alpha, permit, control, [&] {
          ScopeExit cleanup([&] { drained = true; });
          if (memory) throw std::bad_alloc();
          throw std::runtime_error("CPU execution fixture failure");
        });
    Q4T_CHECK(drained && result.error.ok() &&
              result.http_status == (memory ? 503 : 500));
    Q4T_CHECK(Parsed(result.response_body).Find("error")->GetString("code") ==
              (memory ? "host_memory_exhausted" : "internal_error"));
  }
  return true;
}

Q4T_TEST(batch_execution_stream_and_missing_response_fail_without_synthetic_success) {
  Fixture fixture;
  ClientLease permit;
  Q4T_CHECK(permit.Acquire(fixture.clients, fixture.alpha, ClientLane::kData));
  for (bool stream : {false, true}) {
    auto control = Control();
    const auto result = ExecuteBatchEndpoint(
        control->id(), fixture.alpha, permit, control, [&] {
          if (stream) {
            auto* http = detail::HttpResponseContext::Current(-1);
            detail::BeginStreamingResponse(-1, http->State());
          }
        });
    Q4T_CHECK(result.error.code == "batch_response_incomplete" &&
              result.http_status == 0 && result.response_body.empty());
  }
  return true;
}

Q4T_TEST(batch_execution_prepared_background_response_transfers_real_lifecycle) {
  Fixture fixture(true);
  auto rate = Precharge(fixture, fixture.alpha);
  ObjectStore objects(Storage());
  StateAccess access{"client:alpha", Clock::now() + 5s, {}};
  ChatPlanConfig config;
  config.model = "cpu-native-background";
  std::atomic<bool> entered = false, proceed = false;
  std::atomic<bool> correct_origin = false;
  ResponsesJobs jobs(objects, {}, [&](auto& job) {
    correct_origin = job.run->RateDomain() == ClientRateDomain::kBatch &&
                     job.control->deadline() <= access.deadline &&
                     job.rate == rate;
    if (!job.rate || job.rate->Reserve(fixture.clients, job.client, 7, 32) !=
                         ClientRateResult::kAllowed)
      throw std::runtime_error("background original precharge claim");
    job.rate->Start();
    entered = true;
    while (!proceed && !job.control->Cancelled())
      std::this_thread::sleep_for(1ms);
    if (job.control->Cancelled()) return;
    job.rate->Completion(3);
    ResponseDelta delta;
    delta.kind = ResponseDelta::Kind::kContent;
    delta.text = "explicit CPU background fixture";
    std::string wire;
    if (!job.run->Append(delta, "null", &wire) ||
        !job.run->Finish(false, 0, {7, 0, 3, 0}, std::time(nullptr), &wire).ok())
      throw std::runtime_error("native background fixture completion");
  });
  ScopeExit release([&] { proceed = true; });
  ResponsesExecution preparation(objects);
  Q4T_CHECK(Ok(preparation.Prepare(
      Parsed(R"({"input":"original private input","background":true,"store":false,"reasoning":{"effort":"none"}})"),
      config, access, 42, true)));
  Json frozen;
  Q4T_CHECK(Ok(preparation.FreezeInputs(&frozen)));
  const auto id = preparation.Id();
  Q4T_CHECK(ResponsesState(objects).Retrieve(id, access).http_status == 404);
  auto run = std::make_unique<ResponsesExecution>(objects);
  Q4T_CHECK(Ok(run->RestoreInputs(frozen, config, access, 43)));
  ClientLease permit;
  Q4T_CHECK(permit.Acquire(fixture.clients, fixture.alpha, ClientLane::kData));
  auto control = Control();
  const auto result = ExecuteBatchEndpoint(
      control->id(), fixture.alpha, permit, control,
      [&] {
        ServePreparedBackgroundResponse(-1, objects, jobs, std::move(run),
                                         access);
      },
      rate);
  Q4T_CHECK(result.error.ok() && result.http_status == 200 && !run);
  const auto accepted = Parsed(result.response_body);
  Q4T_CHECK(accepted.GetString("id") == id &&
            accepted.GetString("status") == "queued" &&
            accepted.GetBool("background") && !accepted.GetBool("store"));
  Q4T_CHECK(Wait([&] { return entered.load(); }) && correct_origin &&
            !permit.Holds(fixture.alpha, ClientLane::kData) &&
            fixture.clients.Usage(0).data_active == 1 &&
            jobs.Health().foreground_owned == 0 &&
            Usage(fixture).reserved == 160 && Usage(fixture).admitted == 1 &&
            fixture.clients.RateUsage(0).admitted == 0);
  proceed = true;
  Q4T_CHECK(Wait([&] { return jobs.Health().owned == 0; }));
  Q4T_CHECK(fixture.clients.Usage(0).data_active == 0);
  Q4T_CHECK(Usage(fixture).reserved == 0 && Usage(fixture).tokens == 10 &&
            Usage(fixture).admitted == 1);
  const auto terminal = ResponsesState(objects).Retrieve(id, access);
  Q4T_CHECK(Ok(terminal) && terminal.value.GetString("status") == "completed" &&
            terminal.value.Find("usage")->GetInt("total_tokens") == 10);
  return true;
}

Q4T_TEST(batch_execution_precharge_one_claim_settles_after_cleanup) {
  Fixture fixture(true);
  auto rate = Precharge(fixture, fixture.alpha);
  ClientLease permit;
  Q4T_CHECK(permit.Acquire(fixture.clients, fixture.alpha, ClientLane::kData));
  auto control = Control();
  bool claimed = false, drained = false, held_during_cleanup = false;
  auto result = ExecuteBatchEndpoint(
      control->id(), fixture.alpha, permit, control,
      [&] {
        auto* http = detail::HttpResponseContext::Current(-1);
        ScopeExit cleanup([&] {
          held_during_cleanup = Usage(fixture).reserved == 160;
          drained = true;
        });
        claimed = http && http->BatchRate() == rate &&
                  rate->Reserve(fixture.clients, http->Client(), 19, 32) ==
                      ClientRateResult::kAllowed;
        if (!claimed) throw std::runtime_error("native precharge claim");
        rate->Start();
        rate->Completion(7);
        detail::SendSimple(-1, 200, "OK", kBody, "application/json");
      },
      rate);
  Q4T_CHECK(claimed && drained && held_during_cleanup && result.error.ok() &&
            result.response_body == kBody && Usage(fixture).reserved == 0 &&
            Usage(fixture).tokens == 26 && Usage(fixture).admitted == 1 &&
            fixture.clients.RateUsage(0).admitted == 0 &&
            !rate->PrechargedFor(fixture.alpha));
  return true;
}

Q4T_TEST(batch_execution_precharge_binding_rejects_scope_domain_and_reuse) {
  Fixture fixture(true);
  auto rate = Precharge(fixture, fixture.alpha);
  ClientLease permit;
  Q4T_CHECK(permit.Acquire(fixture.clients, fixture.beta, ClientLane::kData));
  auto control = Control();
  int calls = 0;
  auto result = ExecuteBatchEndpoint(
      control->id(), fixture.beta, permit, control, [&] { ++calls; }, rate);
  Q4T_CHECK(result.error.code == "batch_execution_owner_invalid" &&
            calls == 0 && Usage(fixture).reserved == 160 &&
            control->ClaimInvocation());
  {
    detail::HttpResponseContext http(detail::HttpResponseContext::Detached{});
    http.SetClient(fixture.alpha, nullptr);
    Q4T_CHECK(!http.BindBatchRate(rate) && !http.BatchRate());
    http.SetClient(fixture.beta, nullptr, ClientRateDomain::kBatch);
    Q4T_CHECK(!http.BindBatchRate(rate) && !http.BatchRate());
    http.SetClient(fixture.alpha, nullptr, ClientRateDomain::kBatch);
    Q4T_CHECK(http.BindBatchRate(rate) && !http.BindBatchRate(rate));
    http.SetClient(fixture.beta, nullptr, ClientRateDomain::kBatch);
    Q4T_CHECK(http.BatchRate() &&
              !http.BatchRate()->PrechargedFor(http.Client()));
  }
  Q4T_CHECK(Usage(fixture).reserved == 0 && Usage(fixture).tokens == 0);
  return true;
}

Q4T_TEST(batch_execution_precharge_exception_missing_and_cancelled_refunds) {
  Fixture fixture(true);
  ClientLease permit;
  Q4T_CHECK(permit.Acquire(fixture.clients, fixture.alpha, ClientLane::kData));
  for (int mode = 0; mode < 4; ++mode) {
    auto rate = Precharge(fixture, fixture.alpha);
    auto control = Control();
    bool drained = false;
    auto result = ExecuteBatchEndpoint(
        control->id(), fixture.alpha, permit, control,
        [&] {
          ScopeExit cleanup([&] { drained = true; });
          if (mode == 0) throw std::bad_alloc();
          if (mode == 1) throw std::runtime_error("CPU precharge failure");
          if (mode == 2) control->Cancel();
        },
        rate);
    Q4T_CHECK(drained && Usage(fixture).reserved == 0 &&
              Usage(fixture).tokens == 0 &&
              Usage(fixture).admitted == static_cast<uint64_t>(mode + 1));
    Q4T_CHECK(
        mode < 2
            ? result.error.ok() && result.http_status == (mode == 0 ? 503 : 500)
            : result.error.code == (mode == 2 ? "batch_cancelled"
                                              : "batch_response_incomplete"));
  }
  return true;
}

Q4T_TEST(batch_execution_precharge_background_failed_handoff_releases) {
  Fixture fixture(true);
  ObjectStore objects(Storage());
  std::atomic<int> calls = 0;
  ResponsesJobs jobs(objects, {}, [&](auto&) { ++calls; });
  for (int mode = 0; mode < 3; ++mode) {
    auto run = Background(objects);
    const auto id = run->Id();
    auto rate = Precharge(fixture, fixture.alpha);
    const auto identity = mode == 0 ? fixture.beta : fixture.alpha;
    if (mode == 1) Q4T_CHECK(run->BindRateDomain(ClientRateDomain::kOnline));
    bool transfer = false;
    auto result = jobs.Submit(
        std::move(run), identity,
        [&](ClientLease&) {
          transfer = true;
          return false;
        },
        rate);
    Q4T_CHECK(!result.ok() && transfer == (mode == 2) &&
              Usage(fixture).reserved == 0 && Usage(fixture).tokens == 0 &&
              Wait([&] { return jobs.Health().owned == 0; }) && calls == 0);
    const auto saved = ResponsesState(objects).Retrieve(
        id, {"client:alpha", Clock::now() + 5s, {}});
    Q4T_CHECK(mode < 2
                  ? saved.http_status == 404
                  : saved.ok() && saved.value.GetString("status") == "failed");
  }
  return true;
}

Q4T_TEST(
    batch_execution_precharge_queued_background_cancel_refunds_only_its_owner) {
  Fixture fixture(true);
  ObjectStore objects(Storage());
  std::atomic<int> calls = 0;
  std::atomic<bool> entered = false, proceed = false;
  ResponsesJobs jobs(objects, {}, [&](auto& job) {
    ++calls;
    if (!job.rate || job.rate->Reserve(fixture.clients, job.client, 7, 32) !=
                         ClientRateResult::kAllowed)
      throw std::runtime_error("queued precharge claim");
    job.rate->Start();
    entered = true;
    while (!proceed && !job.control->Cancelled())
      std::this_thread::sleep_for(1ms);
    job.rate->Completion(3);
    std::string wire;
    if (!job.run->Finish(false, 0, {7, 0, 3, 0}, std::time(nullptr), &wire)
             .ok())
      throw std::runtime_error("queued fixture completion");
  });
  ScopeExit release([&] { proceed = true; });
  const auto submit = [&]() {
    ClientLease permit;
    if (!permit.Acquire(fixture.clients, fixture.alpha, ClientLane::kData))
      throw std::runtime_error("queued precharge permit");
    auto rate = Precharge(fixture, fixture.alpha);
    return jobs.Submit(
        Background(objects), fixture.alpha,
        [&](ClientLease& destination) {
          return permit.TransferTo(destination);
        },
        std::move(rate));
  };
  auto first = submit();
  Q4T_CHECK(Ok(first) && Wait([&] { return entered.load(); }));
  auto second = submit();
  Q4T_CHECK(Ok(second) && Usage(fixture).reserved == 320 &&
            jobs.Health().queued == 1);
  auto cancelled = jobs.Cancel(second.value.GetString("id"),
                               {"client:alpha", Clock::now() + 5s, {}});
  Q4T_CHECK(Ok(cancelled) &&
            cancelled.value.GetString("status") == "cancelled" && calls == 1 &&
            Usage(fixture).reserved == 160 && Usage(fixture).tokens == 0);
  proceed = true;
  Q4T_CHECK(Wait([&] { return jobs.Health().owned == 0; }) &&
            Usage(fixture).reserved == 0 && Usage(fixture).tokens == 10 &&
            Usage(fixture).admitted == 2 && calls == 1);
  return true;
}

Q4T_TEST(
    batch_execution_precharge_unknown_background_start_does_not_hold_tokens) {
  ScopeExit reset([] { ObjectWorkerEntryForTesting({}); });
  ObjectWorkerEntryForTesting("--q4t-object-worker-second-put-lost-ack");
  auto options = Storage();
  options.io_timeout_ms = 250;
  Fixture fixture(true);
  ObjectStore objects(options);
  std::atomic<int> calls = 0;
  ResponsesJobs jobs(objects, {}, [&](auto&) { ++calls; });
  ClientLease permit;
  Q4T_CHECK(permit.Acquire(fixture.clients, fixture.alpha, ClientLane::kData));
  auto rate = Precharge(fixture, fixture.alpha);
  const auto result = jobs.Submit(
      Background(objects), fixture.alpha,
      [&](ClientLease& destination) { return permit.TransferTo(destination); },
      rate);
  Q4T_CHECK(Ok(result) && Wait([&] { return jobs.Health().owned == 0; }) &&
            calls == 0 && Usage(fixture).reserved == 0 &&
            Usage(fixture).tokens == 0 && Usage(fixture).admitted == 1);
  const auto terminal = ResponsesState(objects).Retrieve(
      result.value.GetString("id"), {"client:alpha", Clock::now() + 5s, {}});
  Q4T_CHECK(Ok(terminal) && terminal.value.GetString("status") == "failed" &&
            terminal.value.Find("error")->GetString("code") ==
                "background_execution_interrupted");
  return true;
}

Q4T_TEST(
    batch_execution_precharge_unknown_background_begin_refunds_without_replay) {
  ScopeExit reset([] { ObjectWorkerEntryForTesting({}); });
  ObjectWorkerEntryForTesting("--q4t-object-worker-lost-ack");
  auto options = Storage();
  options.io_timeout_ms = 250;
  Fixture fixture(true);
  ObjectStore objects(options);
  std::atomic<int> calls = 0;
  ResponsesJobs jobs(objects, {}, [&](auto&) { ++calls; });
  auto run = Background(objects);
  const auto id = run->Id();
  auto rate = Precharge(fixture, fixture.alpha);
  bool transfer = false;
  const auto result = jobs.Submit(
      std::move(run), fixture.alpha,
      [&](ClientLease&) {
        transfer = true;
        return false;
      },
      rate);
  Q4T_CHECK(!result.ok() && result.commit_unknown && !transfer && calls == 0 &&
            Usage(fixture).reserved == 0 && Usage(fixture).tokens == 0 &&
            Wait([&] { return jobs.Health().owned == 0; }));
  const auto terminal = ResponsesState(objects).Retrieve(
      id, {"client:alpha", Clock::now() + 5s, {}});
  Q4T_CHECK(Ok(terminal) && terminal.value.GetString("status") == "failed");
  return true;
}
