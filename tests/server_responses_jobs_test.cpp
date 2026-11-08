#include "q4t/server/responses_jobs.h"

#include <unistd.h>

#include <atomic>
#include <cstdlib>
#include <filesystem>
#include <mutex>
#include <thread>

#include "q4t/server/http_response.h"
#include "q4t/server/lifetime.h"
#include "q4t/server/output_schema.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using q4t::io::Json;
using Clock = std::chrono::steady_clock;
using namespace std::chrono_literals;
Json Parse(std::string text) {
  Json value;
  if (!q4t::io::ParseJson(text, &value, true))
    throw std::runtime_error("job fixture JSON");
  return value;
}
bool Ok(const ObjectReply& reply) {
  if (!reply.ok())
    std::fprintf(stderr, "%s: %s\n", reply.code.c_str(), reply.message.c_str());
  return reply.ok();
}
bool Wait(const std::function<bool()>& condition) {
  const auto until = Clock::now() + 5s;
  while (!condition() && Clock::now() < until) std::this_thread::sleep_for(1ms);
  return condition();
}
StateAccess Access(std::string client = "alpha") {
  return {"client:" + client, Clock::now() + 5s, {}};
}
ChatPlanConfig Config() {
  ChatPlanConfig result;
  result.model = "cpu";
  return result;
}
struct Fixture {
  ObjectStoreOptions options;
  ClientAdmission clients;
  const std::string token = std::string(64, 'a');
  Fixture() {
    const char* output = std::getenv("Q4T_TEST_OUTPUT_DIR");
    const auto root = std::filesystem::absolute(
        output ? output : ".q4t-work/responses-jobs-cpu");
    std::filesystem::create_directories(root);
    auto directory = (root / "jobs-XXXXXX").string();
    if (!mkdtemp(directory.data())) throw std::runtime_error("jobs path");
    options.directory = directory + "/objects";
    options.restart_backoff_ms = 0;
    GatewayClientPolicy policy;
    std::string entries;
    for (const auto* id : {"alpha", "beta", "gamma"}) {
      if (!entries.empty()) entries += ',';
      entries += std::string("{\"id\":\"") + id +
          R"(","max_inflight":8,"max_control_inflight":2,)"
          R"("max_body_bytes":16384,"max_prompt_tokens":512,)"
          R"("max_output_tokens":64})";
    }
    const auto raw = R"({"version":1,"gateway_token":")" + token +
                     R"(","clients":[)" + entries + "]}";
    if (!ParseGatewayClientPolicy(raw, &policy) ||
        !clients.Configure(std::move(policy)))
      throw std::runtime_error("jobs policy");
  }
  ClientIdentity Identity(std::string client = "alpha") {
    return clients.Authenticate(client, token);
  }
  ObjectReply Submit(ResponsesJobs& jobs, ObjectStore& store,
                     std::string client = "alpha") {
    ClientLease lease;
    const auto identity = Identity(client);
    if (!lease.Acquire(clients, identity, ClientLane::kData))
      throw std::runtime_error("job client admission");
    auto run = std::make_unique<ResponsesExecution>(store);
    auto prepared =
        run->Prepare(Parse(R"({"input":"question","background":true,
                  "reasoning":{"effort":"none"}})"),
                     Config(), Access(client), std::time(nullptr), true);
    if (!prepared.ok()) return prepared;
    return jobs.Submit(std::move(run), identity, [&](ClientLease& target) {
      return lease.TransferTo(target);
    });
  }
};
void Complete(ResponsesJobs::Job& job) {
  ResponseDelta delta;
  delta.kind = ResponseDelta::Kind::kContent;
  delta.text = "actual worker output";
  std::string wire;
  if (!job.run->Append(delta, "null", &wire) ||
      !job.run->Finish(false, 0, {5, 0, 2, 0}, std::time(nullptr), &wire).ok())
    throw std::runtime_error("job generation fixture");
}
}  // namespace

Q4T_TEST(responses_jobs_permit_transfer_keeps_charge_and_rejects_failed_lease) {
  Fixture f;
  {
    ClientLease owner;
    {
      ClientLease request;
      Q4T_CHECK(request.Acquire(f.clients, f.Identity(), ClientLane::kData));
      Q4T_CHECK(request.TransferTo(owner));
      ClientLease duplicate;
      Q4T_CHECK(!request.TransferTo(duplicate));
      Q4T_CHECK(!request.Acquire(f.clients, f.Identity(), ClientLane::kData));
    }
    Q4T_CHECK(f.clients.Usage(0).data_active == 1);
  }
  Q4T_CHECK(f.clients.Usage(0).data_active == 0);
  ClientLease failed, destination;
  Q4T_CHECK(!failed.Acquire(f.clients, ClientIdentity(), ClientLane::kData));
  Q4T_CHECK(!failed.TransferTo(destination));
  return true;
}

Q4T_TEST(responses_jobs_detached_context_observes_errors_without_transport) {
  detail::HttpResponseContext http(detail::HttpResponseContext::Detached{});
  int errors = 0;
  http.SetErrorObserver(
      [](void* owner, int status, std::string_view, std::string_view) noexcept {
        if (status == 503) ++*static_cast<int*>(owner);
      },
      &errors);
  auto control =
      std::make_shared<RequestControl>("req-job", "", Clock::now() + 1s);
  Q4T_CHECK(detail::BeginStreamingResponse(-1, http.State()));
  http.StartHeartbeats(control);
  Q4T_CHECK(detail::WriteAll(-1, "no socket data"));
  detail::SendError(-1, 503, "actual dependency failure", {},
                    "dependency_failed");
  Q4T_CHECK(errors == 1);
  Q4T_CHECK(http.WriteFailure().reason == HttpWriteFailure::kNone);
  Q4T_CHECK(!control->Cancelled());
  return true;
}

Q4T_TEST(
    responses_jobs_acceptance_outlives_request_and_releases_after_completion) {
  Fixture f;
  ObjectStore objects(f.options);
  std::atomic<bool> entered{false}, proceed{false};
  ResponsesJobs jobs(objects, {}, [&](auto& job) {
    entered = true;
    while (!proceed && !job.control->Cancelled())
      std::this_thread::sleep_for(1ms);
    if (!job.control->Cancelled()) Complete(job);
  });
  const auto accepted = f.Submit(jobs, objects);
  Q4T_CHECK(Ok(accepted) && accepted.value.GetString("status") == "queued");
  Q4T_CHECK(Wait([&] { return entered.load(); }));
  Q4T_CHECK(f.clients.Usage(0).data_active == 1);
  proceed = true;
  Q4T_CHECK(Wait([&] {
    return jobs.Health().owned == 0 && f.clients.Usage(0).data_active == 0;
  }));
  const auto actual = ResponsesState(objects).Retrieve(
      accepted.value.GetString("id"), Access());
  Q4T_CHECK(Ok(actual) && actual.value.GetString("status") == "completed");
  return true;
}

Q4T_TEST(
    responses_jobs_queued_cancel_skips_executor_and_remains_client_scoped) {
  Fixture f;
  ObjectStore objects(f.options);
  std::atomic<int> calls{0};
  ResponsesJobs jobs(objects, {}, [&](auto& job) {
    ++calls;
    while (!job.control->Cancelled()) std::this_thread::sleep_for(1ms);
  });
  const auto first = f.Submit(jobs, objects);
  Q4T_CHECK(Ok(first) && Wait([&] { return calls == 1; }));
  const auto second = f.Submit(jobs, objects);
  Q4T_CHECK(Ok(second));
  const auto id = second.value.GetString("id");
  Q4T_CHECK(jobs.Cancel(id, Access("beta")).http_status == 404);
  const auto cancelled = jobs.Cancel(id, Access());
  Q4T_CHECK(Ok(cancelled) &&
            cancelled.value.GetString("status") == "cancelled");
  Q4T_CHECK(calls == 1 && f.clients.Usage(0).data_active == 1);
  Q4T_CHECK(Ok(jobs.Cancel(id, Access())));
  Q4T_CHECK(Ok(jobs.Cancel(first.value.GetString("id"), Access())));
  return true;
}

Q4T_TEST(responses_jobs_cancel_wins_against_late_success_commit) {
  Fixture f;
  ObjectStore objects(f.options);
  std::atomic<bool> ready{false}, success_rejected{false};
  ResponsesJobs jobs(objects, {}, [&](auto& job) {
    ready = true;
    while (!job.control->Cancelled()) std::this_thread::sleep_for(1ms);
    std::string wire;
    const auto result =
        job.run->Finish(false, 0, {5, 0, 0, 0}, std::time(nullptr), &wire);
    success_rejected = result.code == "response_cancelled" && wire.empty();
  });
  const auto accepted = f.Submit(jobs, objects);
  Q4T_CHECK(Ok(accepted) && Wait([&] { return ready.load(); }));
  const auto result = jobs.Cancel(accepted.value.GetString("id"), Access());
  Q4T_CHECK(Ok(result) && success_rejected &&
            result.value.GetString("status") == "cancelled");
  return true;
}

Q4T_TEST(responses_jobs_count_client_and_input_limits_are_real_admission) {
  Fixture f;
  ObjectStore objects(f.options);
  ResponsesJobsOptions limits;
  limits.max_jobs = 3;
  limits.max_client_jobs = 2;
  ResponsesJobs jobs(objects, limits, [](auto& job) {
    while (!job.control->Cancelled()) std::this_thread::sleep_for(1ms);
  });
  Q4T_CHECK(Ok(f.Submit(jobs, objects)) && Ok(f.Submit(jobs, objects)));
  Q4T_CHECK(f.Submit(jobs, objects).code == "background_capacity_exhausted");
  Q4T_CHECK(Ok(f.Submit(jobs, objects, "beta")));
  Q4T_CHECK(f.Submit(jobs, objects, "beta").code ==
            "background_capacity_exhausted");
  Q4T_CHECK(jobs.Health().owned == 3);
  jobs.RequestStop();
  jobs.Join();
  Q4T_CHECK(jobs.Health().owned == 0);
  limits.max_input_bytes = 1;
  ResponsesJobs small(objects, limits, Complete);
  Q4T_CHECK(f.Submit(small, objects).code == "background_capacity_exhausted");
  Q4T_CHECK(small.Health().owned == 0 && f.clients.Usage(0).data_active == 0);
  return true;
}

Q4T_TEST(responses_jobs_executor_exception_does_not_stop_following_work) {
  Fixture f;
  ObjectStore objects(f.options);
  std::atomic<int> calls{0};
  ResponsesJobs jobs(objects, {}, [&](auto& job) {
    if (++calls == 1) throw std::runtime_error("fixture executor failed");
    Complete(job);
  });
  const auto failed = f.Submit(jobs, objects);
  Q4T_CHECK(Ok(failed) && Wait([&] { return jobs.Health().owned == 0; }));
  const auto actual =
      ResponsesState(objects).Retrieve(failed.value.GetString("id"), Access());
  Q4T_CHECK(Ok(actual) && actual.value.GetString("status") == "failed");
  Q4T_CHECK(actual.value.Find("error")->GetString("code") ==
            "background_execution_failed");
  const auto completed = f.Submit(jobs, objects);
  Q4T_CHECK(Ok(completed) && Wait([&] { return jobs.Health().owned == 0; }));
  Q4T_CHECK(!jobs.Health().failed && calls == 2);
  Q4T_CHECK(ResponsesState(objects)
                .Retrieve(completed.value.GetString("id"), Access())
                .value.GetString("status") == "completed");
  return true;
}

Q4T_TEST(responses_jobs_unknown_start_is_reconciled_without_execution) {
  struct Reset {
    ~Reset() { ObjectWorkerEntryForTesting({}); }
  } reset;
  ObjectWorkerEntryForTesting("--q4t-object-worker-second-put-lost-ack");
  Fixture f;
  f.options.io_timeout_ms = 250;
  ObjectStore objects(f.options);
  std::atomic<int> calls{0};
  ResponsesJobs jobs(objects, {}, [&](auto&) { ++calls; });
  const auto accepted = f.Submit(jobs, objects);
  Q4T_CHECK(Ok(accepted));
  Q4T_CHECK(Wait([&] { return jobs.Health().owned == 0; }));
  const auto result = ResponsesState(objects).Retrieve(
      accepted.value.GetString("id"), Access());
  Q4T_CHECK(Ok(result) && result.value.GetString("status") == "failed");
  Q4T_CHECK(result.value.Find("error")->GetString("code") ==
            "background_execution_interrupted");
  Q4T_CHECK(calls == 0 && !jobs.Health().failed);
  return true;
}

Q4T_TEST(responses_jobs_waiting_clients_get_round_robin_turns) {
  Fixture f;
  ObjectStore objects(f.options);
  std::atomic<bool> proceed{false};
  std::mutex mutex;
  std::vector<std::string> order;
  ResponsesJobs jobs(objects, {}, [&](auto& job) {
    {
      const std::lock_guard lock(mutex);
      order.emplace_back(job.client.Id());
    }
    while (!proceed && !job.control->Cancelled())
      std::this_thread::sleep_for(1ms);
    if (!job.control->Cancelled()) Complete(job);
  });
  Q4T_CHECK(Ok(f.Submit(jobs, objects)));
  Q4T_CHECK(Wait([&] {
    const std::lock_guard lock(mutex);
    return order.size() == 1;
  }));
  Q4T_CHECK(Ok(f.Submit(jobs, objects)) && Ok(f.Submit(jobs, objects)));
  Q4T_CHECK(Ok(f.Submit(jobs, objects, "beta")));
  Q4T_CHECK(Ok(f.Submit(jobs, objects, "gamma")));
  proceed = true;
  Q4T_CHECK(Wait([&] { return jobs.Health().owned == 0; }));
  const std::lock_guard lock(mutex);
  Q4T_CHECK(order == std::vector<std::string>(
                         {"alpha", "beta", "gamma", "alpha", "alpha"}));
  return true;
}

Q4T_TEST(responses_jobs_shutdown_cancels_owned_work_and_rejects_submission) {
  Fixture f;
  ObjectStore objects(f.options);
  ResponsesJobs jobs(objects, {}, [](auto& job) {
    while (!job.control->Cancelled()) std::this_thread::sleep_for(1ms);
  });
  const auto a = f.Submit(jobs, objects), b = f.Submit(jobs, objects);
  Q4T_CHECK(Ok(a) && Ok(b));
  jobs.RequestStop();
  jobs.Join();
  Q4T_CHECK(jobs.Health().owned == 0 && f.clients.Usage(0).data_active == 0);
  for (const auto* item : {&a, &b})
    Q4T_CHECK(ResponsesState(objects)
                  .Retrieve(item->value.GetString("id"), Access())
                  .value.GetString("status") == "cancelled");
  Q4T_CHECK(f.Submit(jobs, objects).code == "background_unavailable");
  return true;
}
