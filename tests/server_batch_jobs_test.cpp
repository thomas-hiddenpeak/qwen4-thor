#include "q4t/server/batch_jobs.h"
#include "q4t/server/application_owner.h"

#include <sys/wait.h>
#include <unistd.h>

#include <algorithm>
#include <atomic>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <mutex>
#include <thread>
#include <tuple>

#include "../src/server/object_protocol.h"
#include "q4t/server/files.h"
#include "q4t/server/batch_api.h"
#include "q4t/server/client_request.h"
#include "q4t/server/http_connection.h"
#include "q4t/server/chat_capabilities.h"
#include "q4t/server/object_store_options.h"
#include "q4t/server/http_response.h"
#include "q4t/server/lifetime.h"
#include "q4t/server/responses_owner.h"
#include "q4t/server/responses_jobs.h"
#include "q4t/server/responses_api.h"
#include "q4t/server/object_retention.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
struct Fixture {
  ObjectStoreOptions objects;
  BatchJobsOptions jobs;
  ChatPlanConfig config;
  ClientAdmission clients;
  ClientIdentity alpha, beta;
  explicit Fixture(uint32_t rpm = 0, uint32_t tpm = 0) {
    const auto* out = std::getenv("Q4T_TEST_OUTPUT_DIR");
    auto base = std::filesystem::absolute(out ? out : ".q4t-work/batch-jobs");
    std::filesystem::create_directories(base);
    auto path = (base / "batch-jobs-XXXXXX").string();
    if (!mkdtemp(path.data())) throw std::runtime_error("Batch jobs fixture");
    objects.directory = path + "/objects";
    objects.restart_backoff_ms = 0;
    objects.restart_limit = 10;
    objects.io_timeout_ms = 250;
    config.model = "jobs-fixture";
    config.structured = true;
    jobs.retry_delay = 5ms;
    jobs.retry_backoff = 25ms;
    jobs.scan_interval = 5ms;
    jobs.turn_timeout = 5s;
    const std::string secret(64, 'a');
    const auto entry = [](const std::string& id) {
      return "{\"id\":\"" + id +
             "\",\"max_inflight\":1,\"max_control_inflight\":1,"
             "\"max_prompt_tokens\":4096,\"max_output_tokens\":64,"
             "\"max_body_bytes\":4096}";
    };
    const auto raw = "{\"version\":1,\"gateway_token\":\"" + secret +
                     "\",\"clients\":[" + entry("alpha") + ',' + entry("beta") +
                     "]}";
    GatewayClientPolicy policy;
    if (!ParseGatewayClientPolicy(raw, &policy))
      throw std::runtime_error("Batch jobs client policy");
    if (rpm || tpm) {
      policy.version = 3;
      policy.revision = 1;
      policy.fair_queue = true;
      for (auto& limits : policy.clients) {
        limits.requests_per_minute = rpm;
        limits.tokens_per_minute = tpm;
      }
    }
    if (!clients.Configure(std::move(policy)))
      throw std::runtime_error("Batch jobs rate policy");
    alpha = clients.Authenticate("alpha", secret);
    beta = clients.Authenticate("beta", secret);
  }
};
BinaryAccess Access(std::string scope = "client:alpha") {
  return {std::move(scope), Clock::now() + 5s, {}};
}
bool Ok(const ObjectReply& reply) {
  if (!reply.ok())
    std::fprintf(stderr, "%s: %s\n", reply.code.c_str(), reply.message.c_str());
  return reply.ok();
}
bool Wait(const std::function<bool()>& predicate) {
  const auto until = Clock::now() + 5s;
  while (Clock::now() < until) {
    if (predicate()) return true;
    std::this_thread::sleep_for(1ms);
  }
  return false;
}
std::string Input(size_t rows, std::string prefix = "item") {
  std::string text;
  for (size_t i = 0; i < rows; ++i)
    text += OutputJson(Object(
                {{"custom_id", String(prefix + "-" + std::to_string(i))},
                 {"method", String("POST")},
                 {"url", String("/v1/responses")},
                 {"body", Object({{"model", String("jobs-fixture")},
                                  {"input", String("Read project record 42")},
                                  {"max_output_tokens", Number(16)}})}})) +
            '\n';
  return text;
}
std::string File(ObjectStore& objects, const std::string& body,
                 std::string scope = "client:alpha") {
  FileUpload upload(objects, Access(scope), "multipart/form-data; boundary=b");
  const auto wire =
      "--b\r\nContent-Disposition: form-data; name=\"purpose\"\r\n\r\nbatch"
      "\r\n--b\r\nContent-Disposition: form-data; name=\"file\"; "
      "filename=\"input.jsonl\"\r\n\r\n" +
      body + "\r\n--b--\r\n";
  if (!Ok(upload.Feed(wire, Access().deadline)))
    throw std::runtime_error("Batch file fixture");
  const auto result = upload.Finish();
  if (!Ok(result)) throw std::runtime_error("Batch file fixture finish");
  return result.value.GetString("id");
}
BatchCreate Options(const std::string& file) {
  BatchCreate options;
  options.input_file_id = file;
  options.endpoint = "/v1/responses";
  options.metadata = Object();
  return options;
}
std::string BatchRequest(const std::string& file) {
  return OutputJson(Object({{"input_file_id", String(file)},
                            {"endpoint", String("/v1/responses")},
                            {"completion_window", String("24h")}}));
}
// Actual finite response, admission and durable lifecycle; generation is an
// explicit CPU fixture, not a model quality or real GPU business assertion.
BatchJobs::Executor Native(std::atomic<int>& calls,
                           std::function<void()> before = {},
                           ClientAdmission* clients = nullptr) {
  return [&calls, before, clients](const auto&, const auto&, const auto&,
                                   const auto& client, auto& permit,
                                   const auto& owner, auto rate) {
    return ExecuteBatchEndpoint(
        owner->id(), client, permit, owner,
        [&] {
          ++calls;
          if (clients) {
            const auto& charge =
                detail::HttpResponseContext::Current(-1)->BatchRate();
            if (!charge || charge->Reserve(*clients, client, 4, 16) !=
                               ClientRateResult::kAllowed)
              throw std::runtime_error("native fixture lost original rate");
            charge->Start();
            charge->Completion(2);
          }
          if (before) before();
          detail::SendSimple(
              -1, 200, "OK",
              R"({"object":"response","output":[{"type":"function_call","call_id":"call_cpu","name":"lookup","arguments":"{\"key\":42}"}],"usage":{"input_tokens":4,"output_tokens":2,"total_tokens":6,"input_tokens_details":{"cached_tokens":0},"output_tokens_details":{"reasoning_tokens":0}}})",
              "application/json");
        },
        std::move(rate));
  };
}
BatchRatePlanner RatePlanner(std::atomic<int>* calls = nullptr) {
  return
      [calls](const auto&, const auto&, const auto&,
              const ChatPlanConfig& config, const auto&, BatchRateBudget* out) {
        if (calls) ++*calls;
        if (config.client_limits && config.client_limits->max_prompt_tokens < 4)
          return Error(400, "client_prompt_limit_exceeded",
                       "CPU fixture current input limit");
        *out = {4, 16, 0, 0, true};
        return ObjectReply{};
      };
}
ClientRateUsage BatchUsage(Fixture& fixture, size_t index = 0) {
  return fixture.clients.RateUsage(index, Clock::now(),
                                   ClientRateDomain::kBatch);
}
ObjectReply Finished(ObjectStore& objects, BatchJobs& jobs,
                     const std::string& id,
                     std::string scope = "client:alpha") {
  ObjectReply final;
  if (!Wait([&] {
        final = BatchState(objects).Retrieve(Access(scope), id);
        return final.ok() && BatchTerminal(final.value.GetString("status")) &&
               jobs.Health().owned == 0;
      }))
    return Error(500, "fixture_job_timeout", "Batch did not reach terminal");
  return final;
}
std::string Content(ObjectStore& objects, const std::string& id) {
  BinaryRead read;
  BinaryStore store(objects);
  if (!Ok(store.Open(Access(), ObjectKind::kFile, id, 60, &read)))
    throw std::runtime_error("Batch result file");
  std::string out;
  for (size_t i = 0; i < read.manifest.chunks.size(); ++i) {
    std::string bytes;
    if (!Ok(store.Read(Access(), read, i, &bytes)))
      throw std::runtime_error("Batch result bytes");
    out += bytes;
  }
  if (!Ok(store.Close(Access(), &read)))
    throw std::runtime_error("Batch result lease close");
  return out;
}
bool LostReceipt(const char* entry, int expected_calls, int failed) {
  Fixture fixture;
  std::string file;
  {
    ObjectStore objects(fixture.objects);
    file = File(objects, Input(2));
  }
  ObjectWorkerEntryForTesting(entry);
  ScopeExit reset([] { ObjectWorkerEntryForTesting({}); });
  ObjectStore objects(fixture.objects);
  std::atomic<int> calls{0};
  BatchJobs jobs(objects, fixture.clients, fixture.config, fixture.jobs,
                 Native(calls));
  const auto accepted = jobs.Submit(Access(), Options(file), fixture.alpha);
  Q4T_CHECK(accepted.ok() || accepted.commit_unknown);
  Q4T_CHECK(!accepted.value.GetString("id").empty());
  const auto final = Finished(objects, jobs, accepted.value.GetString("id"));
  Q4T_CHECK(Ok(final) && final.value.GetString("status") == "completed" &&
            final.value.Find("request_counts")->GetInt("total") == 2 &&
            final.value.Find("request_counts")->GetInt("failed") == failed &&
            calls == expected_calls &&
            jobs.Health().reserved_result_bytes == 0);
  const auto id =
      final.value.GetString(failed ? "error_file_id" : "output_file_id");
  const auto bytes = Content(objects, id);
  Q4T_CHECK(std::count(bytes.begin(), bytes.end(), '\n') == 2 &&
            bytes.find("item-0") != std::string::npos &&
            bytes.find("item-1") != std::string::npos);
  if (failed)
    Q4T_CHECK(bytes.find("batch_execution_interrupted") != std::string::npos);
  return true;
}
}  // namespace

Q4T_TEST(batch_jobs_actual_native_finite_rows_and_output_file) {
  Fixture fixture;
  ObjectStore objects(fixture.objects);
  const auto file = File(objects, Input(3));
  std::atomic<int> calls{0};
  BatchJobs jobs(objects, fixture.clients, fixture.config, fixture.jobs,
                 Native(calls));
  const auto accepted = jobs.Submit(Access(), Options(file), fixture.alpha);
  Q4T_CHECK(Ok(accepted));
  const auto final = Finished(objects, jobs, accepted.value.GetString("id"));
  Q4T_CHECK(Ok(final) && calls == 3 &&
            final.value.Find("request_counts")->GetInt("completed") == 3 &&
            fixture.clients.Usage(0).data_active == 0 &&
            jobs.Health().reserved_result_bytes == 0);
  const auto body = Content(objects, final.value.GetString("output_file_id"));
  Q4T_CHECK(std::count(body.begin(), body.end(), '\n') == 3 &&
            body.find("\"arguments\":\"{\\\"key\\\":42}\"") !=
                std::string::npos &&
            body.find("\"total_tokens\":6") != std::string::npos);
  return true;
}

Q4T_TEST(batch_jobs_invalid_later_line_never_invokes_and_keeps_error) {
  Fixture fixture;
  ObjectStore objects(fixture.objects);
  const auto file = File(objects, Input(1) + "{broken}\n");
  std::atomic<int> calls{0};
  BatchJobs jobs(objects, fixture.clients, fixture.config, fixture.jobs,
                 Native(calls));
  const auto accepted = jobs.Submit(Access(), Options(file), fixture.alpha);
  Q4T_CHECK(Ok(accepted));
  const auto final = Finished(objects, jobs, accepted.value.GetString("id"));
  Q4T_CHECK(
      Ok(final) && calls == 0 && final.value.GetString("status") == "failed" &&
      final.value.Find("errors")->GetArray("data")->array[0].GetInt("line") ==
          2);
  return true;
}

Q4T_TEST(batch_jobs_enqueued_quota_failure_keeps_line_and_releases_prefix) {
  Fixture fixture;
  fixture.objects.batch_model_queue_tokens = 6;
  fixture.objects.batch_client_model_queue_tokens = 6;
  ObjectStore objects(fixture.objects);
  const auto too_large = File(objects, Input(2));
  const auto fits = File(objects, Input(1));
  std::atomic<int> calls{0}, measured{0};
  BatchInputMeter meter = [&](const auto&, const auto&, const auto&,
                              const auto&, const auto&,
                              BatchInputBudget* output) {
    ++measured;
    *output = {4, true};
    return ObjectReply{};
  };
  BatchJobs jobs(objects, fixture.clients, fixture.config, fixture.jobs,
                 Native(calls), meter);
  auto submitted = jobs.Submit(Access(), Options(too_large), fixture.alpha);
  Q4T_CHECK(Ok(submitted));
  auto final = Finished(objects, jobs, submitted.value.GetString("id"));
  Q4T_CHECK(Ok(final) && final.value.GetString("status") == "failed" &&
            calls == 0 && measured == 2);
  const auto& error = final.value.Find("errors")->GetArray("data")->array[0];
  Q4T_CHECK(error.GetString("code") == "batch_enqueued_token_limit_exceeded" &&
            error.GetInt("line") == 2);
  submitted = jobs.Submit(Access(), Options(fits), fixture.alpha);
  Q4T_CHECK(Ok(submitted));
  final = Finished(objects, jobs, submitted.value.GetString("id"));
  Q4T_CHECK(Ok(final) && final.value.GetString("status") == "completed" &&
            calls == 1 && measured == 3 &&
            fixture.clients.Usage(0).data_active == 0);
  return true;
}

Q4T_TEST(batch_jobs_reserves_capacity_before_create_and_binds_admission_owner) {
  Fixture fixture;
  fixture.jobs.max_jobs = fixture.jobs.max_client_jobs = 1;
  ObjectStore objects(fixture.objects);
  const auto file = File(objects, Input(1));
  std::atomic<int> calls{0};
  std::atomic<bool> release{false};
  BatchJobs jobs(objects, fixture.clients, fixture.config, fixture.jobs,
                 Native(calls, [&] { Wait([&] { return release.load(); }); }));
  ScopeExit unblock([&] { release = true; });
  const auto accepted = jobs.Submit(Access(), Options(file), fixture.alpha);
  Q4T_CHECK(Ok(accepted) && Wait([&] { return calls == 1; }));
  Q4T_CHECK(jobs.Submit(Access(), Options(file), fixture.alpha).http_status ==
            429);
  const auto listed = BatchState(objects).List(Access(), Object());
  Q4T_CHECK(Ok(listed) && listed.value.GetArray("data")->array.size() == 1);
  ClientAdmission foreign;
  Q4T_CHECK(foreign.Configure(*fixture.clients.Snapshot()));
  const auto same_name = foreign.Authenticate("alpha", std::string(64, 'a'));
  Q4T_CHECK(jobs.Submit(Access(), Options(file), same_name).http_status ==
                403 &&
            jobs.Submit(Access("client:beta"), Options(file), fixture.alpha)
                    .http_status == 403);
  release = true;
  Q4T_CHECK(Ok(Finished(objects, jobs, accepted.value.GetString("id"))));
  return true;
}

Q4T_TEST(batch_jobs_fair_client_turns_and_queued_jobs_hold_no_data_permit) {
  Fixture fixture;
  ObjectStore objects(fixture.objects);
  const auto alpha = File(objects, Input(3, "alpha"));
  const auto beta = File(objects, Input(1, "beta"), "client:beta");
  std::atomic<int> calls{0};
  std::atomic<bool> release{false};
  std::mutex mutex;
  std::vector<std::string> order;
  const auto native = Native(calls);
  BatchJobs jobs(
      objects, fixture.clients, fixture.config, fixture.jobs,
      [&](const auto& endpoint, const auto& row, const auto& frozen,
          const auto& client, auto& permit, const auto& control, auto rate) {
        bool first;
        {
          const std::lock_guard lock(mutex);
          first = order.empty();
          order.emplace_back(client.Id());
        }
        if (first) Wait([&] { return release.load(); });
        return native(endpoint, row, frozen, client, permit, control,
                      std::move(rate));
      });
  ScopeExit unblock([&] { release = true; });
  const auto a = jobs.Submit(Access(), Options(alpha), fixture.alpha);
  Q4T_CHECK(Ok(a) && Wait([&] {
              const std::lock_guard lock(mutex);
              return !order.empty();
            }));
  const auto a2 = jobs.Submit(Access(), Options(alpha), fixture.alpha);
  const auto b =
      jobs.Submit(Access("client:beta"), Options(beta), fixture.beta);
  Q4T_CHECK(Ok(a2) && Ok(b) && fixture.clients.Usage(0).data_active == 1 &&
            fixture.clients.Usage(1).data_active == 0);
  release = true;
  Q4T_CHECK(Wait([&] { return jobs.Health().owned == 0; }) && calls == 7);
  const std::lock_guard lock(mutex);
  const auto it = std::find(order.begin(), order.end(), "beta");
  Q4T_CHECK(it != order.end() && it != order.end() - 1);
  return true;
}

Q4T_TEST(batch_jobs_cancel_waits_for_drain_and_publishes_partial_files) {
  Fixture fixture;
  ObjectStore objects(fixture.objects);
  const auto file = File(objects, Input(3));
  std::atomic<int> calls{0};
  std::atomic<bool> cancelled{false}, release{false};
  BatchJobs jobs(objects, fixture.clients, fixture.config, fixture.jobs,
                 [&](const auto&, const auto&, const auto&, const auto& client,
                     auto& permit, const auto& owner, auto rate) {
                   return ExecuteBatchEndpoint(
                       owner->id(), client, permit, owner,
                       [&] {
                         ++calls;
                         Wait([&] { return owner->Cancelled(); });
                         cancelled = owner->Cancelled();
                         Wait([&] { return release.load(); });
                         detail::SendError(-1, 499, "CPU fixture drained", {},
                                           "cancelled");
                       },
                       std::move(rate));
                 });
  ScopeExit unblock([&] { release = true; });
  const auto accepted = jobs.Submit(Access(), Options(file), fixture.alpha);
  Q4T_CHECK(Ok(accepted) && Wait([&] { return calls == 1; }));
  const auto id = accepted.value.GetString("id");
  Q4T_CHECK(jobs.Cancel(Access("client:beta"), id).http_status == 404);
  Q4T_CHECK(Ok(jobs.Cancel(Access(), id)) &&
            Wait([&] { return cancelled.load(); }));
  const auto during = BatchState(objects).Load(Access(), id);
  Q4T_CHECK(Ok(during) && during.value.Find("data")->GetInt("running") == 1 &&
            fixture.clients.Usage(0).data_active == 1 &&
            jobs.Health().owned == 1);
  release = true;
  const auto final = Finished(objects, jobs, id);
  Q4T_CHECK(Ok(final) && final.value.GetString("status") == "cancelled" &&
            calls == 1 &&
            final.value.Find("request_counts")->GetInt("failed") == 3 &&
            fixture.clients.Usage(0).data_active == 0);
  Q4T_CHECK(Content(objects, final.value.GetString("error_file_id"))
                .find("batch_cancelled") != std::string::npos);
  return true;
}

Q4T_TEST(batch_jobs_actual_unknown_create_keeps_reserved_owner) {
  return LostReceipt("--q4t-object-worker-batch-create-lost-ack", 2, 0);
}
Q4T_TEST(batch_jobs_actual_unknown_start_never_invokes) {
  return LostReceipt("--q4t-object-worker-batch-start-lost-ack", 0, 2);
}
Q4T_TEST(batch_jobs_actual_unknown_result_reconciles_without_replay) {
  return LostReceipt("--q4t-object-worker-batch-result-lost-ack", 2, 0);
}

Q4T_TEST(batch_jobs_throw_or_invalid_endpoint_output_closes_without_replay) {
  Fixture fixture;
  ObjectStore objects(fixture.objects);
  const auto file = File(objects, Input(2));
  std::atomic<int> calls{0};
  BatchJobs jobs(objects, fixture.clients, fixture.config, fixture.jobs,
                 [&](const auto&, const auto&, const auto&, const auto&, auto&,
                     const auto& control, const auto&) -> BatchRowResult {
                   if (++calls == 1)
                     throw std::runtime_error("drained CPU fixture");
                   BatchRowResult invalid;
                   invalid.request_id = control->id();
                   invalid.http_status = 200;
                   invalid.response_body = "{broken}";
                   return invalid;
                 });
  const auto accepted = jobs.Submit(Access(), Options(file), fixture.alpha);
  Q4T_CHECK(Ok(accepted));
  const auto final = Finished(objects, jobs, accepted.value.GetString("id"));
  Q4T_CHECK(Ok(final) && calls == 2 &&
            final.value.Find("request_counts")->GetInt("failed") == 2 &&
            jobs.Health().reserved_result_bytes == 0);
  const auto bytes = Content(objects, final.value.GetString("error_file_id"));
  Q4T_CHECK(bytes.find("batch_execution_interrupted") != std::string::npos &&
            bytes.find("batch_response_invalid") != std::string::npos);
  return true;
}

namespace {
bool RecoverDeadBatch(bool recount) {
  Fixture fixture;
  if (recount) {
    fixture.objects.batch_model_queue_tokens = 6;
    fixture.objects.batch_client_model_queue_tokens = 6;
  }
  int channel[2];
  Q4T_CHECK(pipe(channel) == 0);
  const auto pid = fork();
  Q4T_CHECK(pid >= 0);
  if (pid == 0) {
    close(channel[0]);
    int result = 0;
    try {
      ObjectStore objects(fixture.objects);
      const auto file = File(objects, Input(3));
      auto created = BatchState(objects).Create(Access(), Options(file));
      if (!created.ok()) throw std::runtime_error("child Batch");
      const auto id = created.value.GetString("id");
      BatchValidation validation(objects, id, fixture.config);
      for (int i = 0; i < 3; ++i)
        if (!validation.Step(Access()).ok())
          throw std::runtime_error("child validation");
      if (!validation.Complete() ||
          !BatchState(objects).StartRow(Access(), id, 0).ok() ||
          write(channel[1], id.data(), id.size()) !=
              static_cast<ssize_t>(id.size()))
        throw std::runtime_error("child execution receipt");
    } catch (...) {
      result = 2;
    }
    close(channel[1]);
    _exit(result);
  }
  close(channel[1]);
  char id_bytes[128]{};
  const auto bytes = read(channel[0], id_bytes, sizeof(id_bytes));
  close(channel[0]);
  int status;
  Q4T_CHECK(waitpid(pid, &status, 0) == pid && WIFEXITED(status) &&
            WEXITSTATUS(status) == 0 && bytes > 0);
  const std::string id(id_bytes, bytes);
  ObjectStore objects(fixture.objects);
  std::atomic<int> calls{0}, measured{0};
  BatchInputMeter meter;
  if (recount)
    meter = [&](const auto&, const BatchInputRow& row, const auto&,
                const ChatPlanConfig& config, const auto&,
                BatchInputBudget* result) {
      const auto count = ++measured;
      if (row.line != static_cast<size_t>(count) || config.client_limits)
        return Error(500, "fixture_recount", "Recount order or policy changed");
      *result = {4, true};
      return ObjectReply{};
    };
  BatchJobs jobs(objects, fixture.clients, fixture.config, fixture.jobs,
                 Native(calls), meter);
  const auto final = Finished(objects, jobs, id);
  Q4T_CHECK(Ok(final) && calls == 2 &&
            final.value.Find("request_counts")->GetInt("failed") == 1 &&
            final.value.Find("request_counts")->GetInt("completed") == 2 &&
            jobs.Health().recovered == 1 && jobs.Health().interrupted == 1);
  const auto error = Content(objects, final.value.GetString("error_file_id"));
  Q4T_CHECK(error.find("item-0") != std::string::npos &&
            error.find("batch_execution_interrupted") != std::string::npos);
  const auto root = BatchState(objects).Load(Access(), id);
  Q4T_CHECK(Ok(root) && measured == (recount ? 3 : 0) &&
            root.value.Find("data")->Has("enqueued_input_tokens") == recount);
  if (recount)
    Q4T_CHECK(root.value.Find("data")->GetInt("enqueued_input_tokens") == 12);
  return true;
}
}  // namespace

Q4T_TEST(batch_jobs_actual_dead_owner_recovery_skips_started_row) {
  return RecoverDeadBatch(false);
}
Q4T_TEST(batch_jobs_actual_dead_owner_recounts_without_replaying_started_row) {
  return RecoverDeadBatch(true);
}

Q4T_TEST(batch_jobs_live_or_unknown_owner_never_grants_recovery) {
  Fixture fixture;
  ObjectStore objects(fixture.objects);
  const auto file = File(objects, Input(1));
  const auto live = BatchState(objects).Create(Access(), Options(file));
  const auto unknown = BatchState(objects).Create(Access(), Options(file));
  Q4T_CHECK(Ok(live) && Ok(unknown));
  ObjectRequest request;
  request.kind = ObjectKind::kBatch;
  request.scope = Access().scope;
  request.id = unknown.value.GetString("id");
  auto stored = objects.Execute(request, Access().deadline);
  Q4T_CHECK(Ok(stored));
  request.operation = ObjectOperation::kPut;
  request.expected_revision = stored.value.GetInt("revision");
  request.created_at = stored.value.GetInt("created_at");
  request.expires_at = stored.value.GetInt("expires_at");
  request.data = *stored.value.Find("data");
  Set(&Field(request.data, "owner"), "boot_id", String("unconfirmed"));
  Q4T_CHECK(Ok(objects.Execute(request, Access().deadline)));
  std::atomic<int> calls{0};
  BatchJobs jobs(objects, fixture.clients, fixture.config, fixture.jobs,
                 Native(calls));
  Q4T_CHECK(Wait([&] {
    const auto health = jobs.Health();
    return health.recovery_scanned && health.recovery_pending >= 1;
  }));
  Q4T_CHECK(calls == 0 && jobs.Health().recovered == 0 &&
            jobs.Health().owned == 0);
  const auto unchanged =
      BatchState(objects).Retrieve(Access(), live.value.GetString("id"));
  Q4T_CHECK(Ok(unchanged) &&
            unchanged.value.GetString("status") == "validating");
  return true;
}

Q4T_TEST(batch_jobs_stop_cancels_owned_execution_and_keeps_pending_rows) {
  Fixture fixture;
  ObjectStore objects(fixture.objects);
  const auto file = File(objects, Input(2));
  std::atomic<int> calls{0};
  BatchJobs jobs(objects, fixture.clients, fixture.config, fixture.jobs,
                 [&](const auto&, const auto&, const auto&, const auto& client,
                     auto& permit, const auto& owner, auto rate) {
                   return ExecuteBatchEndpoint(
                       owner->id(), client, permit, owner,
                       [&] {
                         ++calls;
                         Wait([&] { return owner->Cancelled(); });
                         detail::SendError(-1, 503, "CPU fixture drained", {},
                                           "server_stopping");
                       },
                       std::move(rate));
                 });
  const auto accepted = jobs.Submit(Access(), Options(file), fixture.alpha);
  Q4T_CHECK(Ok(accepted) && Wait([&] { return calls == 1; }));
  jobs.RequestStop();
  jobs.Join();
  const auto stored =
      BatchState(objects).Load(Access(), accepted.value.GetString("id"));
  Q4T_CHECK(Ok(stored) && calls == 1 &&
            stored.value.Find("data")->GetInt("running") == 0 &&
            stored.value.Find("data")->GetInt("next_row") == 1 &&
            stored.value.Find("data")->Find("batch")->GetString("status") ==
                "in_progress" &&
            fixture.clients.Usage(0).data_active == 0);
  Q4T_CHECK(jobs.Submit(Access(), Options(file), fixture.alpha).http_status ==
            503);
  return true;
}

Q4T_TEST(batch_jobs_scanner_advances_pages_and_recovers_controller_failure) {
  Fixture fixture;
  {
    ObjectStore objects(fixture.objects);
    for (int i = 0; i < 80; ++i) {
      ObjectRequest request;
      request.operation = ObjectOperation::kPut;
      request.kind = ObjectKind::kPrompt;
      request.scope = Access().scope;
      request.id = "prompt_scan_" + std::to_string(i);
      request.data = Object({{"cpu_fixture", Boolean(true)}});
      Q4T_CHECK(Ok(objects.Execute(request, Access().deadline)));
    }
  }
  ObjectStore objects(fixture.objects);
  FailObjectControllerForTesting();
  std::atomic<int> calls{0};
  BatchJobs jobs(objects, fixture.clients, fixture.config, fixture.jobs,
                 Native(calls));
  Q4T_CHECK(Wait([&] {
    const auto health = jobs.Health();
    return health.recovery_errors > 0 && health.recovery_scanned;
  }));
  Q4T_CHECK(calls == 0 && !jobs.Health().failed &&
            objects.Health().controller_recoveries >= 1);
  return true;
}

Q4T_TEST(batch_jobs_anonymous_local_mode_uses_actual_owner_and_executes) {
  Fixture fixture;
  ClientAdmission local, foreign;
  const auto identity = local.Authenticate("", "");
  Q4T_CHECK(identity.Allowed() && !identity.Scoped() &&
            local.OwnsIdentity(identity) &&
            !local.OwnsIdentity(foreign.Authenticate("", "")));
  ObjectStore objects(fixture.objects);
  const auto access = Access("local:anonymous");
  const auto file = File(objects, Input(1), access.scope);
  std::atomic<int> calls{0};
  BatchJobs jobs(objects, local, fixture.config, fixture.jobs, Native(calls));
  const auto accepted = jobs.Submit(access, Options(file), identity);
  Q4T_CHECK(Ok(accepted));
  const auto final =
      Finished(objects, jobs, accepted.value.GetString("id"), access.scope);
  Q4T_CHECK(Ok(final) && calls == 1 &&
            final.value.Find("request_counts")->GetInt("completed") == 1 &&
            jobs.Health().reserved_result_bytes == 0);
  return true;
}

Q4T_TEST(batch_jobs_two_workers_share_bounded_result_capacity) {
  Fixture fixture;
  fixture.jobs.workers = 2;
  fixture.jobs.max_result_bytes = kChatBodyBytes;
  ObjectStore objects(fixture.objects);
  const auto first = File(objects, Input(1));
  const auto second = File(objects, Input(1), "client:beta");
  std::atomic<int> calls{0};
  std::atomic<bool> release{false};
  BatchJobs jobs(objects, fixture.clients, fixture.config, fixture.jobs,
                 Native(calls, [&] { Wait([&] { return release.load(); }); }));
  ScopeExit unblock([&] { release = true; });
  const auto a = jobs.Submit(Access(), Options(first), fixture.alpha);
  Q4T_CHECK(Ok(a) && Wait([&] { return calls == 1; }));
  const auto b =
      jobs.Submit(Access("client:beta"), Options(second), fixture.beta);
  Q4T_CHECK(Ok(b) && Wait([&] { return jobs.Health().errors > 0; }));
  Q4T_CHECK(calls == 1 &&
            jobs.Health().reserved_result_bytes == kChatBodyBytes &&
            jobs.Health().owned == 2);
  release = true;
  Q4T_CHECK(Wait([&] { return jobs.Health().owned == 0; }) && calls == 2 &&
            jobs.Health().reserved_result_bytes == 0 &&
            fixture.clients.Usage(0).data_active == 0 &&
            fixture.clients.Usage(1).data_active == 0);
  return true;
}

Q4T_TEST(
    batch_jobs_minute_wait_yields_before_ticket_and_keeps_other_client_live) {
  Fixture fixture(10, 100);
  fixture.jobs.max_result_bytes = kChatBodyBytes;
  ObjectStore objects(fixture.objects);
  const auto alpha = File(objects, Input(1));
  const auto beta = File(objects, Input(1, "beta"), "client:beta");
  ClientRateLease held(ClientRateDomain::kBatch), online;
  Q4T_CHECK(held.Reserve(fixture.clients, fixture.alpha, 90, 0) ==
                ClientRateResult::kAllowed &&
            online.Reserve(fixture.clients, fixture.alpha, 90, 0) ==
                ClientRateResult::kAllowed);
  std::atomic<int> calls = 0, plans = 0;
  BatchJobs jobs(objects, fixture.clients, fixture.config, fixture.jobs,
                 Native(calls, {}, &fixture.clients), {}, RatePlanner(&plans));
  const auto a = jobs.Submit(Access(), Options(alpha), fixture.alpha);
  Q4T_CHECK(Ok(a) && Wait([&] { return jobs.Health().rate_deferrals >= 3; }));
  auto row = BatchState(objects).Row(Access(), a.value.GetString("id"), 0);
  Q4T_CHECK(Ok(row) && row.value.GetString("status") == "pending" &&
            row.value.GetString("request_id").empty() && calls == 0 &&
            plans == 1 && jobs.Health().rate_waiting == 1 &&
            jobs.Health().errors == 0 &&
            jobs.Health().reserved_result_bytes == 0 &&
            fixture.clients.Usage(0).data_active == 0);
  const auto b =
      jobs.Submit(Access("client:beta"), Options(beta), fixture.beta);
  Q4T_CHECK(Ok(b) && Wait([&] {
              const auto value = BatchState(objects).Retrieve(
                  Access("client:beta"), b.value.GetString("id"));
              return value.ok() &&
                     value.value.GetString("status") == "completed";
            }) &&
            calls == 1 && plans == 2 && BatchUsage(fixture, 1).tokens == 6);
  held.Settle();
  const auto finished = Finished(objects, jobs, a.value.GetString("id"));
  Q4T_CHECK(Ok(finished) && calls == 2 && plans == 2 &&
            BatchUsage(fixture).admitted == 2 &&
            BatchUsage(fixture).tokens == 6 &&
            BatchUsage(fixture).reserved == 0 &&
            fixture.clients.RateUsage(0).reserved == 90 &&
            jobs.Health().rate_waiting == 0);
  return true;
}

Q4T_TEST(batch_jobs_minute_wait_replans_after_actual_policy_update) {
  for (bool lower_input : {false, true}) {
    Fixture fixture(1, 100);
    ObjectStore objects(fixture.objects);
    const auto file = File(objects, Input(1));
    ClientRateLease used(ClientRateDomain::kBatch);
    Q4T_CHECK(used.Reserve(fixture.clients, fixture.alpha, 1, 0) ==
              ClientRateResult::kAllowed);
    used.Settle();
    std::atomic<int> calls = 0, plans = 0;
    BatchJobs jobs(objects, fixture.clients, fixture.config, fixture.jobs,
                   Native(calls, {}, &fixture.clients), {},
                   RatePlanner(&plans));
    const auto accepted = jobs.Submit(Access(), Options(file), fixture.alpha);
    Q4T_CHECK(Ok(accepted) &&
              Wait([&] { return jobs.Health().rate_deferrals >= 3; }) &&
              calls == 0 && plans == 1);
    auto policy = *fixture.clients.Snapshot();
    policy.revision = 2;
    policy.clients[0].requests_per_minute = 2;
    if (lower_input) policy.clients[0].max_prompt_tokens = 1;
    Q4T_CHECK(fixture.clients.Publish(std::move(policy), 1, 8, 8));
    const auto final = Finished(objects, jobs, accepted.value.GetString("id"));
    Q4T_CHECK(Ok(final) && plans == 2 && calls == (lower_input ? 0 : 1) &&
              final.value.Find("request_counts")->GetInt("failed") ==
                  (lower_input ? 1 : 0) &&
              BatchUsage(fixture).admitted == (lower_input ? 1 : 2) &&
              BatchUsage(fixture).reserved == 0);
    if (lower_input)
      Q4T_CHECK(Content(objects, final.value.GetString("error_file_id"))
                    .find("client_prompt_limit_exceeded") != std::string::npos);
  }
  return true;
}

Q4T_TEST(batch_jobs_minute_wait_cancel_never_reserves_execution_or_starts_row) {
  Fixture fixture(10, 100);
  ObjectStore objects(fixture.objects);
  const auto file = File(objects, Input(1));
  ClientRateLease held(ClientRateDomain::kBatch);
  Q4T_CHECK(held.Reserve(fixture.clients, fixture.alpha, 90, 0) ==
            ClientRateResult::kAllowed);
  std::atomic<int> calls = 0;
  BatchJobs jobs(objects, fixture.clients, fixture.config, fixture.jobs,
                 Native(calls, {}, &fixture.clients), {}, RatePlanner());
  const auto accepted = jobs.Submit(Access(), Options(file), fixture.alpha);
  Q4T_CHECK(Ok(accepted) &&
            Wait([&] { return jobs.Health().rate_waiting == 1; }));
  const auto id = accepted.value.GetString("id");
  const auto pending = BatchState(objects).Row(Access(), id, 0);
  Q4T_CHECK(Ok(pending) && pending.value.GetString("status") == "pending" &&
            pending.value.GetString("request_id").empty());
  Q4T_CHECK(Ok(jobs.Cancel(Access(), id)));
  const auto final = Finished(objects, jobs, id);
  Q4T_CHECK(Ok(final) && final.value.GetString("status") == "cancelled" &&
            calls == 0 && BatchUsage(fixture).reserved == 90 &&
            BatchUsage(fixture).admitted == 1 &&
            fixture.clients.Usage(0).data_active == 0 &&
            jobs.Health().reserved_result_bytes == 0);
  const auto line = Content(objects, final.value.GetString("error_file_id"));
  Json record;
  Q4T_CHECK(Parse(line, &record) && record.Find("response") &&
            record.Find("response")->IsNull() && record.Find("error") &&
            record.Find("error")->GetString("code") == "batch_cancelled");
  return true;
}

Q4T_TEST(batch_jobs_minute_reservation_above_whole_window_is_finite_row_error) {
  Fixture fixture(10, 5);
  ObjectStore objects(fixture.objects);
  const auto file = File(objects, Input(1));
  std::atomic<int> calls = 0;
  BatchJobs jobs(objects, fixture.clients, fixture.config, fixture.jobs,
                 Native(calls, {}, &fixture.clients), {}, RatePlanner());
  const auto accepted = jobs.Submit(Access(), Options(file), fixture.alpha);
  Q4T_CHECK(Ok(accepted));
  const auto final = Finished(objects, jobs, accepted.value.GetString("id"));
  Q4T_CHECK(Ok(final) && final.value.GetString("status") == "completed" &&
            final.value.Find("request_counts")->GetInt("failed") == 1 &&
            calls == 0 && BatchUsage(fixture).admitted == 0 &&
            jobs.Health().rate_deferrals == 0 &&
            fixture.clients.Usage(0).data_active == 0);
  Q4T_CHECK(Content(objects, final.value.GetString("error_file_id"))
                .find("batch_request_token_reservation_exceeded") !=
            std::string::npos);
  return true;
}

Q4T_TEST(
    batch_jobs_minute_precharge_unknown_start_releases_without_invocation) {
  Fixture fixture(10, 100);
  std::string file;
  {
    ObjectStore objects(fixture.objects);
    file = File(objects, Input(1));
  }
  ObjectWorkerEntryForTesting("--q4t-object-worker-batch-start-lost-ack");
  ScopeExit reset([] { ObjectWorkerEntryForTesting({}); });
  ObjectStore objects(fixture.objects);
  std::atomic<int> calls = 0;
  BatchJobs jobs(objects, fixture.clients, fixture.config, fixture.jobs,
                 Native(calls, {}, &fixture.clients), {}, RatePlanner());
  const auto accepted = jobs.Submit(Access(), Options(file), fixture.alpha);
  Q4T_CHECK(Ok(accepted));
  const auto final = Finished(objects, jobs, accepted.value.GetString("id"));
  Q4T_CHECK(Ok(final) && calls == 0 &&
            final.value.Find("request_counts")->GetInt("failed") == 1 &&
            BatchUsage(fixture).reserved == 0 &&
            BatchUsage(fixture).admitted == 1 &&
            BatchUsage(fixture).tokens == 0 &&
            jobs.Health().reserved_result_bytes == 0);
  Q4T_CHECK(Content(objects, final.value.GetString("error_file_id"))
                .find("batch_execution_interrupted") != std::string::npos);
  return true;
}

Q4T_TEST(
    batch_jobs_minute_admission_requires_planner_and_respects_frozen_rates) {
  for (bool missing : {true, false}) {
    Fixture fixture(10, 100);
    ObjectStore objects(fixture.objects);
    const auto file = File(objects, Input(1));
    if (!missing) {
      ClientRateState rates;
      Q4T_CHECK(fixture.clients.EnableRateRecovery() &&
                fixture.clients.RestoreRates(nullptr, std::string(64, '1')) &&
                fixture.clients.FreezeRates(&rates));
    }
    std::atomic<int> calls = 0;
    BatchJobs jobs(objects, fixture.clients, fixture.config, fixture.jobs,
                   Native(calls, {}, &fixture.clients), {},
                   missing ? BatchRatePlanner{} : RatePlanner());
    const auto accepted = jobs.Submit(Access(), Options(file), fixture.alpha);
    Q4T_CHECK(Ok(accepted) && Wait([&] { return jobs.Health().errors >= 2; }));
    jobs.RequestStop();
    jobs.Join();
    const auto row =
        BatchState(objects).Row(Access(), accepted.value.GetString("id"), 0);
    Q4T_CHECK(Ok(row) && row.value.GetString("status") == "pending" &&
              row.value.GetString("request_id").empty() && calls == 0 &&
              BatchUsage(fixture).admitted == 0 &&
              jobs.Health().reserved_result_bytes == 0 &&
              fixture.clients.Usage(0).data_active == 0);
  }
  return true;
}

Q4T_TEST(batch_api_actual_lifecycle_scope_query_and_output) {
  Fixture f;
  ObjectStore objects(f.objects);
  const auto file = File(objects, Input(2));
  std::atomic<int> calls{0};
  BatchJobs jobs(objects, f.clients, f.config, f.jobs, Native(calls));
  const auto accepted =
      HandleBatchRequest(objects, &jobs, "POST", "/v1/batches",
                         BatchRequest(file), f.alpha, Access());
  Q4T_CHECK(Ok(accepted) && accepted.value.GetString("status") == "validating");
  const auto id = accepted.value.GetString("id");
  Q4T_CHECK(HandleBatchRequest(objects, &jobs, "GET", "/v1/batches/" + id, "",
                               f.beta, Access("client:beta"))
                .http_status == 404);
  Q4T_CHECK(HandleBatchRequest(objects, &jobs, "GET", "/v1/batches/" + id, "",
                               f.alpha, Access("client:beta"))
                .http_status == 403);
  Q4T_CHECK(Ok(Finished(objects, jobs, id)));
  const auto result = HandleBatchRequest(
      objects, &jobs, "GET", "/v1/batches/" + id, "", f.alpha, Access());
  Q4T_CHECK(Ok(result) && calls == 2 &&
            result.value.Find("request_counts")->GetInt("completed") == 2);
  const auto content =
      Content(objects, result.value.GetString("output_file_id"));
  Q4T_CHECK(content.find("item-0") != content.npos &&
            content.find("item-1") != content.npos);
  const auto listed = HandleBatchRequest(
      objects, &jobs, "GET", "/v1/batches?limit=1", "", f.alpha, Access());
  Q4T_CHECK(Ok(listed) && listed.value.GetArray("data")->array.size() == 1 &&
            listed.value.GetArray("data")->array[0].GetString("id") == id);
  const auto empty = HandleBatchRequest(
      objects, &jobs, "GET", "/v1/batches?after=" + id, "", f.alpha, Access());
  Q4T_CHECK(Ok(empty) && empty.value.GetArray("data")->array.empty());
  const auto* output = std::getenv("Q4T_TEST_OUTPUT_DIR");
  const auto directory =
      std::filesystem::path(output ? output : ".q4t-work/batch-api-cpu");
  std::filesystem::create_directories(directory);
  std::ofstream stream(directory / "batches-contract.json");
  stream << OutputJson(Object({{"created", accepted.value},
                               {"completed", result.value},
                               {"list", listed.value},
                               {"empty", empty.value}}));
  Q4T_CHECK(stream.good());
  return true;
}

Q4T_TEST(batch_api_cancel_drains_running_row_and_skips_remaining) {
  Fixture f;
  ObjectStore objects(f.objects);
  const auto file = File(objects, Input(2));
  std::atomic<int> calls{0};
  std::atomic<bool> release{false};
  BatchJobs jobs(objects, f.clients, f.config, f.jobs,
                 Native(calls, [&] { Wait([&] { return release.load(); }); }));
  ScopeExit unblock([&] { release = true; });
  const auto accepted =
      HandleBatchRequest(objects, &jobs, "POST", "/v1/batches",
                         BatchRequest(file), f.alpha, Access());
  Q4T_CHECK(Ok(accepted) && Wait([&] { return calls == 1; }));
  const auto id = accepted.value.GetString("id");
  const auto cancel = HandleBatchRequest(objects, &jobs, "POST",
                                         "/v1/batches/" + id + "/cancel", "",
                                         f.alpha, Access());
  Q4T_CHECK(Ok(cancel) && cancel.value.GetString("status") == "cancelling");
  Q4T_CHECK(jobs.Health().active == 1 && f.clients.Usage(0).data_active == 1);
  release = true;
  const auto final = Finished(objects, jobs, id);
  Q4T_CHECK(Ok(final) && calls == 1 &&
            final.value.GetString("status") == "cancelled" &&
            f.clients.Usage(0).data_active == 0);
  const auto again = HandleBatchRequest(objects, &jobs, "POST",
                                        "/v1/batches/" + id + "/cancel", "{}",
                                        f.alpha, Access());
  Q4T_CHECK(Ok(again) && OutputJson(again.value) == OutputJson(final.value));
  return true;
}

Q4T_TEST(batch_api_closed_http_contract_never_publishes_invalid_requests) {
  Fixture f;
  ObjectStore objects(f.objects);
  std::atomic<int> calls{0};
  BatchJobs jobs(objects, f.clients, f.config, f.jobs, Native(calls));
  for (const auto* body :
       {"", "[]", "null",
        "{\"completion_window\":\"24h\",\"completion_window\":\"24h\"}",
        "{\"input_file_id\":\"file_a\",\"endpoint\":\"/v1/"
        "responses\",\"completion_window\":\"24h\",\"owner\":\"beta\"}"})
    Q4T_CHECK(HandleBatchRequest(objects, &jobs, "POST", "/v1/batches", body,
                                 f.alpha, Access())
                  .http_status == 400);
  for (const auto* path :
       {"/v1/batches?limit=0", "/v1/batches?limit=101",
        "/v1/batches?limit=1&limit=2", "/v1/batches?before=batch_x",
        "/v1/batches?limit=1e1", "/v1/batches?limit=%GG",
        "/v1/batches/batch_x?limit=1"})
    Q4T_CHECK(
        HandleBatchRequest(objects, &jobs, "GET", path, "", f.alpha, Access())
            .http_status == 400);
  for (const auto* path :
       {"/v1/batches/", "/v1/batches/x/unknown", "/v1/batches/x/cancel/",
        "/v1/batches/x#fragment", "/v1/batches/x%2fy", "/v1/not-batches"})
    Q4T_CHECK(
        HandleBatchRequest(objects, &jobs, "GET", path, "", f.alpha, Access())
            .http_status == 404);
  const auto method = HandleBatchRequest(
      objects, &jobs, "DELETE", "/v1/batches/x", "", f.alpha, Access());
  Q4T_CHECK(method.http_status == 405 &&
            method.value.GetString("allow") == "GET");
  Q4T_CHECK(HandleBatchRequest(objects, &jobs, "GET", "/v1/batches", "{}",
                               f.alpha, Access())
                .http_status == 400);
  Q4T_CHECK(HandleBatchRequest(objects, &jobs, "POST", "/v1/batches/x/cancel",
                               "{\"force\":true}", f.alpha, Access())
                .http_status == 400);
  Q4T_CHECK(BatchState(objects)
                .List(Access(), Object())
                .value.GetArray("data")
                ->array.empty() &&
            calls == 0);
  return true;
}

Q4T_TEST(batch_api_unavailable_executor_keeps_original_read_and_cancel) {
  Fixture f;
  ObjectStore objects(f.objects);
  const auto file = File(objects, Input(1));
  Q4T_CHECK(HandleBatchRequest(objects, nullptr, "POST", "/v1/batches",
                               BatchRequest(file), f.alpha, Access())
                .code == "batch_executor_unavailable");
  Q4T_CHECK(BatchState(objects)
                .List(Access(), Object())
                .value.GetArray("data")
                ->array.empty());
  const auto original = BatchState(objects).Create(Access(), Options(file));
  Q4T_CHECK(Ok(original));
  const auto path = "/v1/batches/" + original.value.GetString("id");
  Q4T_CHECK(Ok(HandleBatchRequest(objects, nullptr, "GET", path, "", f.alpha,
                                  Access())));
  const auto cancelled = HandleBatchRequest(
      objects, nullptr, "POST", path + "/cancel", "", f.alpha, Access());
  Q4T_CHECK(Ok(cancelled) &&
            cancelled.value.GetString("status") == "cancelling");
  return true;
}

Q4T_TEST(batch_api_saturated_data_preserves_control_and_cancel_lanes) {
  Fixture f;
  ClientLease held;
  Q4T_CHECK(held.Acquire(f.clients, f.alpha, ClientLane::kData));
  for (const auto& [method, target, allowed, classification] : std::vector<
           std::tuple<std::string, std::string, bool, HttpConnectionClass>>{
           {"POST", "/v1/batches", false, HttpConnectionClass::kBusiness},
           {"GET", "/v1/batches?limit=2", false,
            HttpConnectionClass::kBusiness},
           {"GET", "/v1/batches/batch_x", true,
            HttpConnectionClass::kObservation},
           {"POST", "/v1/batches/batch_x/cancel", true,
            HttpConnectionClass::kControl}}) {
    Q4T_CHECK(ClassifyHttpConnection(method, target) == classification &&
              BatchMethodAllowed(method, target));
    HttpRequestHeaders headers;
    headers.client_id = "alpha";
    headers.gateway_token = std::string(64, 'a');
    ClientLease request;
    ClientIdentity identity;
    int failure = 0;
    Q4T_CHECK(AdmitClientRequest(method, target, &headers, f.clients, &request,
                                 &identity, &failure) == allowed);
    if (!allowed) Q4T_CHECK(failure == 429);
  }
  Q4T_CHECK(!IsCancellationRequest("POST", "/v1/batches/x/cancel/extra") &&
            !IsBatchObservation("GET", "/v1/batches/x%2fy"));
  return true;
}

Q4T_TEST(batch_config_native_cli_budget_and_effective_discovery) {
  ServerOptions options;
  options.model_dir = "/unused/model";
  const std::vector<std::string_view> args{"--batch-workers",
                                           "2",
                                           "--batch-max-jobs",
                                           "6",
                                           "--batch-max-client-jobs",
                                           "3",
                                           "--batch-max-result-mib",
                                           "192",
                                           "--batch-model-queue-tokens",
                                           "9007199254740991",
                                           "--batch-client-model-queue-tokens",
                                           "4294967296"};
  Q4T_CHECK(ParseServerOptions(args, &options));
  const auto resolved =
      ResolveObjectStoreOptions(options, "/tmp/cpu-state", "");
  Q4T_CHECK(resolved.batch_model_queue_tokens == (1ULL << 53) - 1 &&
            resolved.batch_client_model_queue_tokens == 4294967296ULL &&
            options.batch_workers == 2);
  for (const auto& pair :
       std::vector<std::pair<std::string_view, std::string_view>>{
           {"--batch-workers", "0"},
           {"--batch-workers", "9"},
           {"--batch-max-jobs", "1"},
           {"--batch-max-client-jobs", "7"},
           {"--batch-max-result-mib", "95"},
           {"--batch-max-result-mib", "1025"},
           {"--batch-model-queue-tokens", "1"},
           {"--batch-client-model-queue-tokens", "0"},
           {"--batch-model-queue-tokens", "9007199254740992"},
           {"--batch-model-queue-tokens", "18446744073709551616"},
           {"--batch-model-queue-tokens", "-1"},
           {"--batch-model-queue-tokens", "1.5"},
           {"--batch-model-queue-tokens", "true"}}) {
    const std::array invalid{pair.first, pair.second};
    Q4T_CHECK(!ParseServerOptions(invalid, &options));
    Q4T_CHECK(options.batch_workers == 2 &&
              options.batch_model_queue_tokens == (1ULL << 53) - 1);
  }
  ChatPlanConfig config;
  q4t::io::Json effective, capabilities;
  const auto wire = EffectiveServeConfigJson(options, config, true, true, 8);
  Q4T_CHECK(q4t::io::ParseJson(wire, &effective) &&
            q4t::io::ParseJson(ChatCapabilitiesJson("cpu", false, true),
                               &capabilities));
  Q4T_CHECK(effective.Find("limits")->GetInt("batch_workers") == 2 &&
            effective.Find("limits")->GetInt("batch_result_bytes") ==
                (192LL << 20));
  const auto* batches = capabilities.Find("batches");
  Q4T_CHECK(batches && batches->GetBool("create") &&
            batches->GetBool("cancel") &&
            OutputJson(*batches->GetArray("execution_endpoints")) ==
                R"(["/v1/responses","/v1/chat/completions","/v1/completions",)"
                R"("/v1/moderations","/v1/embeddings"])" &&
            !batches->GetBool("core_readiness_dependency"));
  const auto* out = std::getenv("Q4T_TEST_OUTPUT_DIR");
  const auto directory =
      std::filesystem::path(out ? out : ".q4t-work/batch-api-cpu");
  std::filesystem::create_directories(directory);
  std::ofstream file(directory / "batches-config.json");
  file << wire;
  Q4T_CHECK(file.good());
  return true;
}

Q4T_TEST(batch_jobs_dead_owner_recounts_original_usage_before_next_invocation) {
  Fixture fixture;
  int channel[2];
  Q4T_CHECK(pipe(channel) == 0);
  const auto pid = fork();
  Q4T_CHECK(pid >= 0);
  if (!pid) {
    close(channel[0]);
    int status = 0;
    try {
      ObjectStore objects(fixture.objects);
      BatchState batches(objects);
      const auto file = File(objects, Input(2));
      const auto created = batches.Create(Access(), Options(file));
      if (!created.ok()) throw std::runtime_error("child batch");
      const auto id = created.value.GetString("id");
      BatchValidation validation(objects, id, fixture.config);
      for (int turn = 0; turn < 4 && !validation.Complete(); ++turn)
        if (!validation.Step(Access()).ok())
          throw std::runtime_error("child validation");
      const auto started = batches.StartRow(Access(), id, 0);
      BatchInputRow row;
      if (!started.ok() || !batches.ReadInputRow(Access(), id, 0, &row).ok())
        throw std::runtime_error("child row");
      BatchRowResult result;
      result.request_id = started.value.GetString("request_id");
      result.http_status = 200;
      result.response_body =
          R"({"usage":{"input_tokens":10,"output_tokens":4,"total_tokens":14,"input_tokens_details":{"cached_tokens":3},"output_tokens_details":{"reasoning_tokens":2}}})";
      if (!batches.FinishRow(Access(), id, 0, row.custom_id, result).ok())
        throw std::runtime_error("child finish");
      const auto loaded = batches.Load(Access(), id);
      ObjectRequest legacy;
      legacy.scope = Access().scope;
      legacy.id = id;
      legacy.kind = ObjectKind::kBatch;
      legacy.operation = ObjectOperation::kPut;
      legacy.expected_revision = loaded.value.GetInt("revision");
      legacy.created_at = loaded.value.GetInt("created_at");
      legacy.expires_at = loaded.value.GetInt("expires_at");
      legacy.data = *loaded.value.Find("data");
      std::erase_if(legacy.data.object, [](const auto& item) {
        return item.first == "usage_accounting";
      });
      std::erase_if(Field(legacy.data, "batch").object,
                    [](const auto& item) { return item.first == "usage"; });
      if (!objects.Execute(legacy, Access().deadline).ok() ||
          write(channel[1], id.data(), id.size()) !=
              static_cast<ssize_t>(id.size()))
        throw std::runtime_error("child legacy result");
    } catch (...) {
      status = 2;
    }
    close(channel[1]);
    _exit(status);
  }
  close(channel[1]);
  char bytes[128]{};
  const auto count = read(channel[0], bytes, sizeof(bytes));
  close(channel[0]);
  int status = 0;
  Q4T_CHECK(waitpid(pid, &status, 0) == pid && WIFEXITED(status) &&
            WEXITSTATUS(status) == 0 && count > 0);
  ObjectStore objects(fixture.objects);
  std::atomic<int> calls{0};
  BatchJobs jobs(objects, fixture.clients, fixture.config, fixture.jobs,
                 Native(calls));
  const auto final = Finished(objects, jobs, std::string(bytes, count));
  Q4T_CHECK(Ok(final) && calls == 1 && final.value.Find("usage") &&
            final.value.Find("usage")->GetInt("input_tokens") == 14 &&
            final.value.Find("usage")->GetInt("output_tokens") == 6 &&
            final.value.Find("usage")
                    ->Find("input_tokens_details")
                    ->GetInt("cached_tokens") == 3 &&
            final.value.Find("usage")
                    ->Find("output_tokens_details")
                    ->GetInt("reasoning_tokens") == 2 &&
            jobs.Health().reserved_result_bytes == 0 &&
            jobs.Health().recovered == 1);
  return true;
}

namespace {
std::string BackgroundInput() {
  auto input = Input(1);
  const auto at = input.find("\"max_output_tokens\":16");
  if (at == input.npos) throw std::runtime_error("background fixture body");
  input.insert(at, "\"background\":true,\"store\":false,");
  return input;
}
bool RecoverBackground(const std::string& mode);
StateAccess ResponseAccess(std::string scope = "client:alpha") {
  return {std::move(scope), Clock::now() + 5s, {}};
}
BatchJobs::Executor BackgroundNative(ObjectStore& objects, Fixture& fixture,
                                     ResponsesJobs& responses,
                                     std::atomic<int>& submissions) {
  return [&](const auto&, const auto& row, const auto& frozen,
             const auto& client, auto& permit, const auto& control, auto rate) {
    ++submissions;
    return ExecuteBatchEndpoint(
        control->id(), client, permit, control,
        [&] {
          if (!row.background_response)
            throw std::runtime_error("missing durable Batch parent");
          auto run = std::make_unique<ResponsesExecution>(objects);
          const StateAccess access{row.background_response->scope,
                                   control->deadline(),
                                   [control] { return control->Cancelled(); }};
          const auto restored =
              run->RestoreInputs(frozen, fixture.config, access,
                                 std::time(nullptr), &*row.background_response);
          if (!restored.ok()) {
            detail::SendError(-1, restored.http_status,
                              restored.ClientMessage(), {},
                              restored.ClientCode());
            return;
          }
          ServePreparedBackgroundResponse(-1, objects, responses,
                                          std::move(run), access);
        },
        std::move(rate));
  };
}
BatchJobs::Continuation Observe(ResponsesJobs& responses) {
  return [&](const auto& link, const auto& access, bool cancel) {
    return responses.ObserveBatch(link, access, cancel);
  };
}
void CompleteBackground(ResponsesJobs::Job& job, Fixture& fixture) {
  if (!job.rate || job.rate->Reserve(fixture.clients, job.client, 4, 16) !=
                       ClientRateResult::kAllowed)
    throw std::runtime_error("background CPU rate claim");
  job.rate->Start();
  job.rate->Completion(2);
  ResponseDelta delta;
  delta.kind = ResponseDelta::Kind::kTool;
  delta.call = {0, "call_continuation", "lookup", "{\"key\":42}"};
  std::string wire;
  if (!job.run->Append(delta, "null", &wire) ||
      !job.run->Finish(false, 1, {4, 1, 2, 0}, std::time(nullptr), &wire).ok())
    throw std::runtime_error("background CPU actual terminal");
}
struct BoundBackground {
  BatchResponseLink link;
  Json frozen;
};
BoundBackground PrepareBoundBackground(ObjectStore& objects, Fixture& fixture) {
  const auto file = File(objects, BackgroundInput());
  const auto accepted = BatchState(objects).Create(Access(), Options(file));
  if (!Ok(accepted)) throw std::runtime_error("bound Batch create");
  BoundBackground bound;
  bound.link.batch_id = accepted.value.GetString("id");
  BatchValidation validation(objects, bound.link.batch_id, fixture.config);
  while (!validation.Complete())
    if (!Ok(validation.Step(Access())))
      throw std::runtime_error("bound Batch validation");
  BatchInputRow input;
  BatchState state(objects);
  const auto row = state.Row(Access(), bound.link.batch_id, 0);
  if (!Ok(row) ||
      !Ok(state.ReadInputRow(Access(), bound.link.batch_id, 0, &input)) ||
      !Ok(BatchPreparation(objects).Read(Access(), bound.link.batch_id,
                                         "/v1/responses", input, row.value,
                                         &bound.frozen)))
    throw std::runtime_error("bound Batch original input");
  bound.link.response_id = bound.frozen.GetString("id");
  bound.link.request_id = "req_" + NewObjectId(ObjectKind::kToolRun);
  bound.link.scope = Access().scope;
  if (!Ok(state.StartRow(Access(), bound.link.batch_id, 0,
                         bound.link.request_id, &bound.link)))
    throw std::runtime_error("bound Batch Start");
  return bound;
}
}  // namespace

Q4T_TEST(batch_background_waits_for_terminal_and_actual_cleanup_before_copy) {
  Fixture fixture(10, 100);
  fixture.jobs.max_result_bytes = kChatBodyBytes;
  ObjectStore objects(fixture.objects);
  const auto file = File(objects, BackgroundInput());
  const auto beta = File(objects, Input(1), "client:beta");
  std::atomic<int> submissions = 0, other = 0;
  std::atomic<bool> entered = false, complete = false, terminal = false,
                    drained = false;
  ResponsesJobs responses(objects, {}, [&](auto& job) {
    entered = true;
    Wait([&] { return complete.load() || job.control->Cancelled(); });
    if (job.control->Cancelled()) return;
    CompleteBackground(job, fixture);
    terminal = true;
    Wait([&] { return drained.load(); });
  });
  auto background = BackgroundNative(objects, fixture, responses, submissions);
  auto foreground = Native(other, {}, &fixture.clients);
  BatchJobs jobs(
      objects, fixture.clients, fixture.config, fixture.jobs,
      [&](const auto& endpoint, const auto& row, const auto& frozen,
          const auto& client, auto& permit, const auto& control, auto rate) {
        return (row.background_response ? background : foreground)(
            endpoint, row, frozen, client, permit, control, std::move(rate));
      },
      {}, RatePlanner(), Observe(responses));
  ScopeExit release([&] { complete = drained = true; });
  const auto accepted = jobs.Submit(Access(), Options(file), fixture.alpha);
  Q4T_CHECK(Ok(accepted) && Wait([&] { return entered.load(); }));
  const auto id = accepted.value.GetString("id");
  const auto row = BatchState(objects).Row(Access(), id, 0);
  BatchResponseLink link;
  Q4T_CHECK(
      Ok(row) && row.value.GetString("status") == "running" &&
      ParseBatchResponseLink(*row.value.Find("background_response"), &link));
  Q4T_CHECK(ResponsesState(objects)
                .Retrieve(link.response_id, ResponseAccess("client:beta"))
                .http_status == 404);
  auto wrong = link;
  wrong.request_id = "req_" + NewObjectId(ObjectKind::kToolRun);
  Q4T_CHECK(responses.ObserveBatch(wrong, ResponseAccess(), false).code ==
            "batch_response_binding_invalid");
  const auto b =
      jobs.Submit(Access("client:beta"), Options(beta), fixture.beta);
  Q4T_CHECK(Ok(b) && Wait([&] {
              const auto value = BatchState(objects).Retrieve(
                  Access("client:beta"), b.value.GetString("id"));
              return value.ok() &&
                     value.value.GetString("status") == "completed";
            }));
  complete = true;
  Q4T_CHECK(Wait([&] { return terminal.load(); }));
  Q4T_CHECK(
      ResponsesState(objects).Erase(link.response_id, ResponseAccess()).code ==
          "response_batch_result_pending" &&
      responses.ObserveBatch(link, ResponseAccess(), false).code ==
          "batch_response_pending" &&
      fixture.clients.Usage(0).data_active == 1 &&
      BatchState(objects).Row(Access(), id, 0).value.GetString("status") ==
          "running");
  drained = true;
  const auto final = Finished(objects, jobs, id);
  Q4T_CHECK(Ok(final) && submissions == 1 && other == 1 &&
            final.value.Find("usage")->GetInt("total_tokens") == 6 &&
            BatchUsage(fixture).admitted == 1 &&
            BatchUsage(fixture).tokens == 6 &&
            BatchUsage(fixture).reserved == 0 &&
            fixture.clients.Usage(0).data_active == 0 &&
            jobs.Health().reserved_result_bytes == 0);
  const auto output = Content(objects, final.value.GetString("output_file_id"));
  Q4T_CHECK(
      output.find("call_continuation") != output.npos &&
      output.find("\"status\":\"queued\"") == output.npos &&
      output.find(link.response_id) != output.npos &&
      Ok(ResponsesState(objects).Erase(link.response_id, ResponseAccess())));
  return true;
}

Q4T_TEST(
    batch_background_cancel_and_revocation_reach_original_owner_and_drain) {
  for (bool revoke : {false, true}) {
    if (revoke) return RecoverBackground("revoked");
    Fixture fixture(10, 100);
    ObjectStore objects(fixture.objects);
    const auto file = File(objects, BackgroundInput());
    std::atomic<int> submissions = 0;
    std::atomic<bool> entered = false, cancelled = false, drain = false;
    ResponsesJobs responses(objects, {}, [&](auto& job) {
      entered = true;
      Wait([&] { return job.control->Cancelled(); });
      cancelled = job.control->Cancelled();
      Wait([&] { return drain.load(); });
    });
    BatchJobs jobs(objects, fixture.clients, fixture.config, fixture.jobs,
                   BackgroundNative(objects, fixture, responses, submissions),
                   {}, RatePlanner(), Observe(responses));
    ScopeExit release([&] { drain = true; });
    const auto accepted = jobs.Submit(Access(), Options(file), fixture.alpha);
    Q4T_CHECK(Ok(accepted) && Wait([&] { return entered.load(); }));
    const auto id = accepted.value.GetString("id");
    Q4T_CHECK(Ok(jobs.Cancel(Access(), id)));
    Q4T_CHECK(Wait([&] { return cancelled.load(); }));
    Q4T_CHECK(
        BatchState(objects).Row(Access(), id, 0).value.GetString("status") ==
            "running" &&
        fixture.clients.Usage(0).data_active == 1);
    drain = true;
    const auto final = Finished(objects, jobs, id);
    Q4T_CHECK(Ok(final) && submissions == 1 &&
              final.value.GetString("status") == "cancelled" &&
              fixture.clients.Usage(0).data_active == 0 &&
              Content(objects, final.value.GetString("output_file_id"))
                      .find("\"status\":\"cancelled\"") != std::string::npos);
  }
  return true;
}

Q4T_TEST(
    batch_background_submission_failure_does_not_lose_original_http_error) {
  Fixture fixture;
  ObjectStore objects(fixture.objects);
  const auto file = File(objects, BackgroundInput());
  std::atomic<int> calls = 0;
  ResponsesJobs responses(objects, {}, [&](auto&) { ++calls; });
  BatchJobs jobs(
      objects, fixture.clients, fixture.config, fixture.jobs,
      [&](const auto&, const auto&, const auto&, const auto& client,
          auto& permit, const auto& control, auto rate) {
        return ExecuteBatchEndpoint(
            control->id(), client, permit, control,
            [&] {
              detail::SendError(-1, 400, "CPU explicit rejection", {},
                                "fixture_rejected");
            },
            std::move(rate));
      },
      {}, RatePlanner(), Observe(responses));
  const auto accepted = jobs.Submit(Access(), Options(file), fixture.alpha);
  Q4T_CHECK(Ok(accepted));
  const auto final = Finished(objects, jobs, accepted.value.GetString("id"));
  Q4T_CHECK(Ok(final) && calls == 0 &&
            final.value.Find("request_counts")->GetInt("failed") == 1 &&
            Content(objects, final.value.GetString("error_file_id"))
                    .find("fixture_rejected") != std::string::npos);
  return true;
}

Q4T_TEST(batch_background_actual_lost_start_begin_and_finish_never_resubmit) {
  for (const auto* selector :
       {"--q4t-object-worker-batch-start-lost-ack",
        "--q4t-object-worker-batch-response-begin-lost-ack",
        "--q4t-object-worker-batch-result-lost-ack"}) {
    Fixture fixture(10, 100);
    std::string file;
    {
      ObjectStore objects(fixture.objects);
      file = File(objects, BackgroundInput());
    }
    ObjectWorkerEntryForTesting(selector);
    ScopeExit reset([] { ObjectWorkerEntryForTesting({}); });
    ObjectStore objects(fixture.objects);
    std::atomic<int> submissions = 0, calls = 0;
    ResponsesJobs responses(objects, {}, [&](auto& job) {
      ++calls;
      CompleteBackground(job, fixture);
    });
    BatchJobs jobs(objects, fixture.clients, fixture.config, fixture.jobs,
                   BackgroundNative(objects, fixture, responses, submissions),
                   {}, RatePlanner(), Observe(responses));
    const auto accepted = jobs.Submit(Access(), Options(file), fixture.alpha);
    Q4T_CHECK(Ok(accepted));
    const auto final = Finished(objects, jobs, accepted.value.GetString("id"));
    const bool start = std::string_view(selector).find("batch-start") !=
                       std::string_view::npos;
    const bool finish = std::string_view(selector).find("batch-result") !=
                        std::string_view::npos;
    Q4T_CHECK(Ok(final) && submissions == (start ? 0 : 1) &&
              calls == (finish ? 1 : 0) &&
              fixture.clients.Usage(0).data_active == 0 &&
              BatchUsage(fixture).reserved == 0 &&
              jobs.Health().reserved_result_bytes == 0);
    const auto output = Content(
        objects,
        final.value.GetString(start ? "error_file_id" : "output_file_id"));
    Q4T_CHECK(output.find(start    ? "batch_execution_interrupted"
                          : finish ? "call_continuation"
                                   : "background_execution_interrupted") !=
              output.npos);
    if (finish)
      Q4T_CHECK(final.value.Find("usage")->GetInt("total_tokens") == 6);
    Q4T_CHECK(objects.Health().workers_started > 1);
  }
  return true;
}

Q4T_TEST(batch_background_binding_is_private_immutable_and_retains_result) {
  Fixture fixture;
  ObjectStore objects(fixture.objects);
  const auto bound = PrepareBoundBackground(objects, fixture);
  const auto& link = bound.link;
  BatchResponseLink parsed;
  auto wire = BatchResponseLinkValue(link);
  Q4T_CHECK(ParseBatchResponseLink(wire, &parsed) && parsed == link);
  Json huge;
  Q4T_CHECK(q4t::io::ParseJson("{\"index\":1e20}", &huge, true));
  Set(&wire, "index", *huge.Find("index"));
  Q4T_CHECK(!ParseBatchResponseLink(wire, &parsed));
  auto wrong = link;
  wrong.scope = "client:beta";
  ResponsesExecution invalid(objects);
  Q4T_CHECK(invalid
                .RestoreInputs(bound.frozen, fixture.config, ResponseAccess(),
                               std::time(nullptr), &wrong)
                .code == "batch_response_binding_invalid");
  ResponsesExecution run(objects);
  Q4T_CHECK(Ok(run.RestoreInputs(bound.frozen, fixture.config, ResponseAccess(),
                                 std::time(nullptr), &link)));
  std::string events;
  Q4T_CHECK(
      Ok(run.Begin(&events)) && Ok(run.StartRunning(&events)) &&
      Ok(run.Finish(false, 0, {4, 1, 2, 0}, std::time(nullptr), &events)));
  Q4T_CHECK(
      ResponsesState(objects).Erase(link.response_id, ResponseAccess()).code ==
      "response_batch_result_pending");
  auto root = BatchState(objects).Load(Access(), link.batch_id);
  Q4T_CHECK(Ok(root));
  ObjectRequest request;
  request.kind = ObjectKind::kToolRun;
  request.scope = Access().scope;
  request.id = root.value.Find("data")->GetArray("pages")->array[0].str;
  auto page = objects.Execute(request, Access().deadline);
  Q4T_CHECK(Ok(page));
  auto retained = InspectObjectRetention("tool_run", *page.value.Find("data"));
  Q4T_CHECK(!retained.uncertain &&
            std::find(retained.references.begin(), retained.references.end(),
                      link.response_id) != retained.references.end());
  request.operation = ObjectOperation::kPut;
  request.expected_revision = page.value.GetInt("revision");
  request.data = *page.value.Find("data");
  auto& saved = Field(request.data, "rows").array[0];
  Set(&Field(saved, "background_response"), "response_id",
      String(NewObjectId(ObjectKind::kResponse)));
  Q4T_CHECK(objects.Execute(request, Access().deadline).code ==
            "batch_checkpoint_immutable");
  BatchRowResult result{
      link.request_id, 200, OutputJson(run.Output().Snapshot()), {}};
  Q4T_CHECK(Ok(BatchState(objects).FinishRow(Access(), link.batch_id, 0,
                                             "item-0", result)));
  request.operation = ObjectOperation::kGet;
  page = objects.Execute(request, Access().deadline);
  Q4T_CHECK(Ok(page));
  retained = InspectObjectRetention("tool_run", *page.value.Find("data"));
  Q4T_CHECK(
      !retained.uncertain &&
      std::find(retained.references.begin(), retained.references.end(),
                link.response_id) == retained.references.end() &&
      Ok(ResponsesState(objects).Erase(link.response_id, ResponseAccess())));
  return true;
}

namespace {
bool RecoverBackground(const std::string& mode) {
  Fixture fixture(10, 100);
  int channel[2];
  Q4T_CHECK(pipe(channel) == 0);
  const auto pid = fork();
  Q4T_CHECK(pid >= 0);
  if (pid == 0) {
    close(channel[0]);
    int status = 0;
    try {
      ObjectStore objects(fixture.objects);
      const auto bound = PrepareBoundBackground(objects, fixture);
      if (mode != "missing") {
        ResponsesExecution run(objects);
        if (!Ok(run.RestoreInputs(bound.frozen, fixture.config,
                                  ResponseAccess(), std::time(nullptr),
                                  &bound.link)) ||
            !run.BindRateDomain(ClientRateDomain::kBatch))
          throw std::runtime_error("dead child bound preparation");
        auto control = std::make_shared<RequestControl>(
            run.Id(), "", Clock::now() + 5s, fixture.alpha.Id());
        run.BindBackgroundControl(control);
        std::string wire;
        if (!Ok(run.Begin(&wire))) throw std::runtime_error("child Begin");
        if (mode == "running" || mode == "terminal")
          if (!Ok(run.StartRunning(&wire)))
            throw std::runtime_error("child Start");
        if (mode == "terminal")
          if (!Ok(run.Finish(false, 0, {7, 2, 3, 1}, std::time(nullptr),
                             &wire)))
            throw std::runtime_error("child Finish");
      }
      if (mode == "cancelled")
        if (!Ok(BatchState(objects).Cancel(Access(), bound.link.batch_id)))
          throw std::runtime_error("child Cancel");
      const auto text = OutputJson(BatchResponseLinkValue(bound.link));
      if (write(channel[1], text.data(), text.size()) !=
          static_cast<ssize_t>(text.size()))
        throw std::runtime_error("child identity channel");
    } catch (...) {
      status = 2;
    }
    close(channel[1]);
    _exit(status);
  }
  close(channel[1]);
  char bytes[2048]{};
  const auto length = read(channel[0], bytes, sizeof(bytes));
  close(channel[0]);
  int status;
  Q4T_CHECK(waitpid(pid, &status, 0) == pid && WIFEXITED(status) &&
            WEXITSTATUS(status) == 0 && length > 0);
  Json value;
  BatchResponseLink link;
  Q4T_CHECK(q4t::io::ParseJson(std::string(bytes, length), &value, true) &&
            ParseBatchResponseLink(value, &link));
  ObjectStore objects(fixture.objects);
  ClientAdmission revoked;
  auto* clients = &fixture.clients;
  if (mode == "revoked") {
    auto policy = *fixture.clients.Snapshot();
    policy.clients.erase(policy.clients.begin());
    Q4T_CHECK(revoked.Configure(std::move(policy)));
    clients = &revoked;
  }
  std::atomic<int> submissions = 0, calls = 0;
  ResponsesJobs responses(
      objects, {},
      [&](auto& job) {
        ++calls;
        CompleteBackground(job, fixture);
      },
      [&](auto& job) {
        return AdmitBackgroundBatchRate(
            *clients, job, fixture.config,
            [](auto&, const auto&, const auto&, BatchRateBudget* budget) {
              *budget = {4, 16, 0, 0, true};
              return ObjectReply{};
            });
      });
  responses.StartRecovery(*clients, fixture.config);
  BatchJobs jobs(objects, *clients, fixture.config, fixture.jobs,
                 BackgroundNative(objects, fixture, responses, submissions), {},
                 RatePlanner(), Observe(responses));
  const auto final = Finished(objects, jobs, link.batch_id);
  Q4T_CHECK(Ok(final) && submissions == 0 &&
            calls == (mode == "queued" ? 1 : 0) &&
            fixture.clients.Usage(0).data_active == 0 &&
            jobs.Health().reserved_result_bytes == 0);
  const auto output = Content(
      objects, final.value.GetString(mode == "missing" ? "error_file_id"
                                                       : "output_file_id"));
  if (mode != "missing")
    Q4T_CHECK(output.find(link.response_id) != output.npos);
  if (mode == "queued")
    Q4T_CHECK(output.find("call_continuation") != output.npos);
  if (mode == "terminal")
    Q4T_CHECK(final.value.Find("usage")->GetInt("total_tokens") == 10);
  if (mode == "running")
    Q4T_CHECK(output.find("background_execution_interrupted") != output.npos);
  if (mode == "revoked")
    Q4T_CHECK(final.value.GetString("status") == "cancelled" &&
              output.find("background_client_revoked") != output.npos);
  if (mode == "cancelled")
    Q4T_CHECK(final.value.GetString("status") == "cancelled" &&
              output.find("\"status\":\"cancelled\"") != output.npos);
  return true;
}
}  // namespace
Q4T_TEST(batch_background_actual_dead_owner_resumes_same_queued_response) {
  return RecoverBackground("queued");
}
Q4T_TEST(batch_background_actual_dead_owner_keeps_original_terminal_usage) {
  return RecoverBackground("terminal");
}
Q4T_TEST(batch_background_actual_dead_owner_never_replays_started_or_missing) {
  return RecoverBackground("running") && RecoverBackground("missing");
}
Q4T_TEST(batch_background_actual_dead_owner_cancelled_parent_never_executes) {
  return RecoverBackground("cancelled");
}

Q4T_TEST(batch_background_actual_ttl_retains_until_result_copy_then_collects) {
  Fixture fixture;
  ObjectStore objects(fixture.objects);
  const auto bound = PrepareBoundBackground(objects, fixture);
  ResponsesExecution run(objects);
  Q4T_CHECK(Ok(run.RestoreInputs(bound.frozen, fixture.config, ResponseAccess(),
                                 std::time(nullptr), &bound.link)));
  std::string events;
  Q4T_CHECK(
      Ok(run.Begin(&events)) && Ok(run.StartRunning(&events)) &&
      Ok(run.Finish(false, 0, {4, 1, 2, 0}, std::time(nullptr), &events)));
  ObjectRequest request;
  request.kind = ObjectKind::kResponse;
  request.scope = Access().scope;
  request.id = bound.link.response_id;
  const auto stored = objects.Execute(request, Access().deadline);
  Q4T_CHECK(Ok(stored));
  request.operation = ObjectOperation::kPut;
  request.expected_revision = stored.value.GetInt("revision");
  request.created_at = stored.value.GetInt("created_at");
  request.expires_at = std::time(nullptr) + 1;
  request.data = *stored.value.Find("data");
  // CPU clock fixture: production ephemeral polling still lasts 600 seconds.
  Q4T_CHECK(Ok(objects.Execute(request, Access().deadline)) &&
            Wait([&] { return std::time(nullptr) >= request.expires_at; }));
  const auto retained = ResponsesState(objects).Retrieve(bound.link.response_id,
                                                         ResponseAccess());
  Q4T_CHECK(Ok(retained) && retained.value.GetString("status") == "completed" &&
            ResponsesState(objects)
                    .Erase(bound.link.response_id, ResponseAccess())
                    .code == "response_batch_result_pending");
  BatchRowResult result{
      bound.link.request_id, 200, OutputJson(retained.value), {}};
  Q4T_CHECK(Ok(BatchState(objects).FinishRow(Access(), bound.link.batch_id, 0,
                                             "item-0", result)));
  Q4T_CHECK(ResponsesState(objects)
                .Retrieve(bound.link.response_id, ResponseAccess())
                .http_status == 404);
  Q4T_CHECK(Ok(BatchState(objects).BeginFinalization(
      Access(), bound.link.batch_id, "completed")));
  ObjectReply final;
  Q4T_CHECK(Wait([&] {
    final = BatchState(objects).Finalize(Access(), bound.link.batch_id);
    return final.ok() && final.value.GetString("status") == "completed";
  }));
  Q4T_CHECK(final.value.Find("usage")->GetInt("total_tokens") == 6 &&
            Content(objects, final.value.GetString("output_file_id"))
                    .find(bound.link.response_id) != std::string::npos);
  return true;
}

Q4T_TEST(batch_background_original_parent_expiry_cancels_queued_execution) {
  Fixture fixture;
  ObjectStore objects(fixture.objects);
  auto bound = PrepareBoundBackground(objects, fixture);
  const auto old = bound.link;
  const auto loaded = BatchState(objects).Load(Access(), old.batch_id);
  Q4T_CHECK(Ok(loaded));
  ObjectRequest page;
  page.kind = ObjectKind::kToolRun;
  page.scope = Access().scope;
  page.id = loaded.value.Find("data")->GetArray("pages")->array[0].str;
  const auto old_page = objects.Execute(page, Access().deadline);
  Q4T_CHECK(Ok(old_page));
  // New historical fixture; never edit an existing creation/deadline. Keep
  // the original frozen input alive while creating the prior-day parent.
  bound.link.batch_id = NewObjectId(ObjectKind::kBatch);
  ObjectRequest root;
  root.operation = ObjectOperation::kPut;
  root.kind = ObjectKind::kBatch;
  root.scope = Access().scope;
  root.id = bound.link.batch_id;
  root.created_at = std::time(nullptr) - kBatchWindowSeconds + 2;
  const auto expires = root.created_at + kBatchWindowSeconds;
  root.data = *loaded.value.Find("data");
  auto& resource = Field(root.data, "batch");
  Set(&resource, "id", String(root.id));
  Set(&resource, "created_at", Number(root.created_at));
  Set(&resource, "expires_at", Number(expires));
  page.operation = ObjectOperation::kPut;
  page.id = NewObjectId(ObjectKind::kToolRun);
  page.ttl_seconds = 0;
  page.data = *old_page.value.Find("data");
  Set(&page.data, "batch_id", String(root.id));
  Set(&Field(page.data, "rows").array[0], "background_response",
      BatchResponseLinkValue(bound.link));
  Set(&root.data, "pages", Array({String(page.id)}));
  const std::array mutations{page, root};
  Q4T_CHECK(Ok(objects.ExecuteBatch(mutations, Access().deadline)));
  std::atomic<bool> entered = false, release = false;
  std::atomic<int> alpha_calls = 0;
  ResponsesJobs responses(objects, {}, [&](auto& job) {
    if (job.client.Id() == "alpha") {
      ++alpha_calls;
      return;
    }
    entered = true;
    Wait([&] { return release.load(); });
  });
  ScopeExit unblock([&] { release = true; });
  auto blocker = std::make_unique<ResponsesExecution>(objects);
  Q4T_CHECK(Ok(blocker->Prepare(Object({{"input", String("CPU queue blocker")},
                                        {"background", Boolean(true)}}),
                                fixture.config, ResponseAccess("client:beta"),
                                std::time(nullptr), true)));
  ClientLease beta, alpha;
  Q4T_CHECK(beta.Acquire(fixture.clients, fixture.beta, ClientLane::kData) &&
            alpha.Acquire(fixture.clients, fixture.alpha, ClientLane::kData));
  Q4T_CHECK(Ok(responses.Submit(std::move(blocker), fixture.beta,
                                [&](ClientLease& target) {
                                  return beta.TransferTo(target);
                                })) &&
            Wait([&] { return entered.load(); }));
  auto run = std::make_unique<ResponsesExecution>(objects);
  Q4T_CHECK(
      Ok(run->RestoreInputs(bound.frozen, fixture.config, ResponseAccess(),
                            std::time(nullptr), &bound.link)));
  Q4T_CHECK(Ok(responses.Submit(
      std::move(run), fixture.alpha,
      [&](ClientLease& target) { return alpha.TransferTo(target); })));
  Q4T_CHECK(Wait([&] { return std::time(nullptr) >= expires; }));
  release = true;
  Q4T_CHECK(Wait([&] { return responses.Health().owned == 0; }) &&
            alpha_calls == 0 && fixture.clients.Usage(0).data_active == 0);
  const auto result =
      responses.ObserveBatch(bound.link, ResponseAccess(), false);
  Q4T_CHECK(Ok(result) && result.value.GetString("status") == "cancelled");
  Q4T_CHECK(
      Ok(BatchState(objects).FinishRow(
          Access(), root.id, 0, "item-0",
          {bound.link.request_id, 200, OutputJson(result.value), {}})) &&
      Ok(BatchState(objects).BeginFinalization(Access(), root.id, "expired")));
  ObjectReply final;
  Q4T_CHECK(Wait([&] {
    final = BatchState(objects).Finalize(Access(), root.id);
    return final.ok() && final.value.GetString("status") == "expired";
  }));
  return true;
}

Q4T_TEST(
    batch_background_absent_observer_closes_only_proven_missing_submission) {
  for (bool callback : {false, true}) {
    Fixture fixture;
    ObjectStore objects(fixture.objects);
    const auto file = File(objects, BackgroundInput());
    std::atomic<int> calls = 0;
    BatchJobs::Continuation observer;
    if (callback)
      observer = [](const auto&, const auto&, bool) {
        return Error(503, "batch_response_observer_unavailable",
                     "CPU unavailable observer");
      };
    BatchJobs jobs(
        objects, fixture.clients, fixture.config, fixture.jobs,
        [&](const auto&, const auto&, const auto&, const auto& client,
            auto& permit, const auto& control, auto rate) {
          ++calls;
          return ExecuteBatchEndpoint(
              control->id(), client, permit, control,
              [&] {
                detail::SendError(-1, 503, "Background unavailable", {},
                                  "background_unavailable");
              },
              std::move(rate));
        },
        {}, RatePlanner(), std::move(observer));
    const auto accepted = jobs.Submit(Access(), Options(file), fixture.alpha);
    Q4T_CHECK(Ok(accepted));
    const auto final = Finished(objects, jobs, accepted.value.GetString("id"));
    Q4T_CHECK(Ok(final) && calls == 1 &&
              final.value.Find("request_counts")->GetInt("failed") == 1 &&
              Content(objects, final.value.GetString("error_file_id"))
                      .find("background_unavailable") != std::string::npos &&
              fixture.clients.Usage(0).data_active == 0 &&
              jobs.Health().reserved_result_bytes == 0);
  }
  return true;
}

Q4T_TEST(
    batch_background_existing_result_waits_for_original_observer_recovery) {
  Fixture fixture;
  ObjectStore objects(fixture.objects);
  const auto file = File(objects, BackgroundInput());
  std::atomic<int> submissions = 0, calls = 0, observations = 0;
  std::atomic<bool> available = false;
  ResponsesJobs responses(objects, {}, [&](auto& job) {
    ++calls;
    CompleteBackground(job, fixture);
  });
  BatchJobs jobs(objects, fixture.clients, fixture.config, fixture.jobs,
                 BackgroundNative(objects, fixture, responses, submissions), {},
                 RatePlanner(),
                 [&](const auto& link, const auto& access, bool cancel) {
                   ++observations;
                   if (!available)
                     return Error(503, "batch_response_observer_unavailable",
                                  "CPU unavailable observer");
                   return responses.ObserveBatch(link, access, cancel);
                 });
  const auto accepted = jobs.Submit(Access(), Options(file), fixture.alpha);
  Q4T_CHECK(Ok(accepted) && Wait([&] {
              return calls == 1 && responses.Health().owned == 0 &&
                     observations >= 3;
            }));
  const auto id = accepted.value.GetString("id");
  const auto row = BatchState(objects).Row(Access(), id, 0);
  Q4T_CHECK(Ok(row) && row.value.GetString("status") == "running" &&
            submissions == 1);
  available = true;
  const auto final = Finished(objects, jobs, id);
  Q4T_CHECK(Ok(final) && submissions == 1 && calls == 1 &&
            final.value.Find("usage")->GetInt("total_tokens") == 6);
  return true;
}

Q4T_TEST(
    batch_application_owner_partial_threads_drain_then_real_task_recovers) {
  Fixture f;
  ObjectStore objects(f.objects);
  const auto file = File(objects, Input(2));
  ApplicationOwner<BatchJobs> owner({1ms, 20ms});
  f.jobs.workers = 2;
  std::atomic<int> calls{0};
  const auto factory = [&] {
    return std::make_unique<BatchJobs>(objects, f.clients, f.config, f.jobs,
                                       Native(calls));
  };
  const auto threads = [] {
    return std::distance(std::filesystem::directory_iterator("/proc/self/task"),
                         std::filesystem::directory_iterator());
  };
  const auto before = threads();
  FailApplicationThreadForTesting(ApplicationThreadKind::kBatchScanner);
  ScopeExit reset([] { application_failed_thread.store(-1); });
  Q4T_CHECK(!owner.TryStart(factory) && !owner && calls == 0 &&
            owner.StartupHealth().failures == 1);
  Q4T_CHECK(Wait([&] { return threads() == before; }));
  Q4T_CHECK(Wait([&] {
    owner.TryStart(factory);
    return bool(owner);
  }));
  const auto submitted = owner->Submit(Access(), Options(file), f.alpha);
  Q4T_CHECK(Ok(submitted));
  const auto final = Finished(objects, *owner, submitted.value.GetString("id"));
  Q4T_CHECK(
      Ok(final) && final.value.GetString("status") == "completed" &&
      final.value.Find("request_counts")->GetInt("completed") == 2 &&
      calls == 2 &&
      !Content(objects, final.value.GetString("output_file_id")).empty() &&
      owner.StartupHealth().recoveries == 1 &&
      owner->Health().reserved_result_bytes == 0 &&
      f.clients.Usage(0).data_active == 0);
  auto* original = owner.Get();
  owner.RequestStop();
  owner.Join();
  Q4T_CHECK(!owner.TryStart(factory) && owner.Get() == original &&
            owner->Health().stopping && calls == 2);
  return true;
}
