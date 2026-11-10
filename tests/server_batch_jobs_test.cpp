#include "q4t/server/batch_jobs.h"

#include <sys/wait.h>
#include <unistd.h>

#include <algorithm>
#include <atomic>
#include <cstdlib>
#include <filesystem>
#include <mutex>
#include <thread>

#include "../src/server/object_protocol.h"
#include "q4t/server/files.h"
#include "q4t/server/http_response.h"
#include "q4t/server/lifetime.h"
#include "q4t/server/responses_owner.h"
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
              R"({"object":"response","output":[{"type":"function_call","call_id":"call_cpu","name":"lookup","arguments":"{\"key\":42}"}],"usage":{"input_tokens":4,"output_tokens":2,"total_tokens":6}})",
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
