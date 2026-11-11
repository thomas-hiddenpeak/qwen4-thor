#include "q4t/server/responses_jobs.h"
#include "q4t/server/application_owner.h"

#include <unistd.h>
#include <poll.h>
#include <sys/socket.h>
#include <sys/wait.h>
#include <sys/file.h>
#include <fcntl.h>
#include <arpa/inet.h>

#include <atomic>
#include <cstdlib>
#include <cerrno>
#include <ctime>
#include <filesystem>
#include <fstream>
#include <mutex>
#include <thread>

#include "q4t/server/http_response.h"
#include "q4t/server/http_request.h"
#include "q4t/server/lifetime.h"
#include "q4t/server/output_schema.h"
#include "q4t/server/responses_api.h"
#include "q4t/server/responses_owner.h"
#include "q4t/service/guardian_process.h"
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
  explicit Fixture(bool minute_quota = false) {
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
    if (!ParseGatewayClientPolicy(raw, &policy))
      throw std::runtime_error("jobs policy");
    if (minute_quota) {
      policy.version = 3;
      policy.revision = 1;
      policy.fair_queue = true;
      for (auto& limits : policy.clients) {
        limits.max_inflight = 1;
        limits.requests_per_minute = 10;
        limits.tokens_per_minute = 512;
      }
    }
    if (!clients.Configure(std::move(policy)))
      throw std::runtime_error("jobs rate policy");
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

void Set(Json* value, std::string key, Json field) {
  for (auto& entry : value->object)
    if (entry.first == key) {
      entry.second = std::move(field);
      return;
    }
  value->object.emplace_back(std::move(key), std::move(field));
}
Json* Field(Json* value, const std::string& key) {
  for (auto& entry : value->object)
    if (entry.first == key) return &entry.second;
  throw std::runtime_error("missing CPU recovery field");
}
ObjectReply Raw(ObjectStore& objects, std::string id,
                std::string scope = "client:alpha") {
  ObjectRequest request;
  request.id = std::move(id);
  request.scope = std::move(scope);
  return objects.Execute(request, Clock::now() + 5s);
}
void EditRecord(ObjectStore& objects, const std::string& id,
                const std::string& scope,
                const std::function<void(Json&)>& edit) {
  auto record = Raw(objects, id, scope);
  if (!record.ok()) throw std::runtime_error("recovery record read");
  ObjectRequest write;
  write.operation = ObjectOperation::kPut;
  write.id = id;
  write.scope = scope;
  write.expected_revision = record.value.GetInt("revision");
  write.data = *record.value.Find("data");
  edit(write.data);
  if (!objects.Execute(write, Clock::now() + 5s).ok())
    throw std::runtime_error("recovery record edit");
}
std::string Interrupted(ObjectStore& objects, bool started = false,
                        std::string scope = "client:alpha", bool dead = true,
                        bool expired = false, bool batch = false) {
  ResponsesExecution run(objects);
  auto request = Parse(
      R"({"input":"private original input","background":true,"reasoning":{"effort":"none"}})");
  if (batch) Set(&request, "max_output_tokens", Parse("32"));
  if (!run.Prepare(request, Config(), {scope, Clock::now() + 5s, {}},
                   std::time(nullptr), true)
           .ok())
    throw std::runtime_error("recovery preparation");
  if (batch && !run.BindRateDomain(ClientRateDomain::kBatch))
    throw std::runtime_error("recovery Batch origin");
  run.BindBackgroundControl(
      std::make_shared<RequestControl>(run.Id(), "", Clock::now() + 30s));
  std::string wire;
  if (!run.Begin(&wire).ok()) throw std::runtime_error("recovery begin");
  if (started) {
    ResponseDelta text;
    text.kind = ResponseDelta::Kind::kContent;
    text.text = "original partial output";
    if (!run.StartRunning(&wire).ok() || !run.Append(text, "null", &wire) ||
        !run.Checkpoint().ok())
      throw std::runtime_error("recovery partial");
  }
  if (dead || expired)
    EditRecord(objects, run.Id(), scope, [&](Json& data) {
      auto* continuation = Field(&data, "continuation");
      if (dead) {
        auto* owner = Field(continuation, "owner");
        Set(owner, "start_ticks",
            Parse(std::to_string(owner->GetInt("start_ticks") + 1)));
      }
      if (expired) Set(continuation, "execution_deadline_at", Parse("1"));
    });
  return run.Id();
}

std::string InterruptedBatch(ObjectStore& objects) {
  return Interrupted(objects, false, "client:alpha", true, false, true);
}
ResponsesJobs::RateAdmission BatchAdmission(Fixture& fixture,
                                            std::atomic<int>* plans = nullptr) {
  return [&fixture, plans](ResponsesJobs::Job& job) {
    return AdmitBackgroundBatchRate(
        fixture.clients, job, Config(),
        [plans](auto& run, const auto& config, const auto& access,
                auto* budget) {
          if (plans) ++*plans;
          // Explicit CPU codec; actual native request/template planning, not
          // a claim about model tokens or generation quality.
          BatchTokenCodec codec;
          codec.encode = [](std::string_view text, auto* tokens) {
            tokens->assign(text.begin(), text.end());
            return q4t::Status{};
          };
          codec.decode = [](const auto& tokens, std::string* text) {
            text->assign(tokens.size(), 't');
            return q4t::Status{};
          };
          return MeasureBatchResponseRate(run, config, access, codec, budget);
        });
  };
}
void CompleteBatch(Fixture& fixture, ResponsesJobs::Job& job) {
  if (!job.rate || !job.planned_rate || !job.permit ||
      !job.permit->Holds(job.client, ClientLane::kData))
    throw std::runtime_error("CPU Batch recovery owner");
  const auto& budget = *job.planned_rate;
  if (job.rate->Reserve(fixture.clients, job.client, budget.prompt_capacity,
                        budget.output, ClientClock::now(),
                        budget.auxiliary_output, budget.preparation_output) !=
      ClientRateResult::kAllowed)
    throw std::runtime_error("CPU Batch recovery precharge claim");
  job.rate->Start();
  job.rate->Completion(2);
  ResponseDelta delta;
  delta.kind = ResponseDelta::Kind::kContent;
  delta.text = "CPU recovered Batch fixture";
  std::string wire;
  if (!job.run->Append(delta, "null", &wire) ||
      !job.run
           ->Finish(false, 0,
                    {static_cast<int64_t>(budget.prompt_capacity), 0, 2, 0},
                    std::time(nullptr), &wire)
           .ok())
    throw std::runtime_error("CPU Batch recovery completion");
}
ClientRateUsage BatchUsage(Fixture& fixture) {
  return fixture.clients.RateUsage(0, ClientClock::now(),
                                   ClientRateDomain::kBatch);
}

std::string Exchange(
    const std::function<void(int)>& handler,
    const std::function<bool(std::string_view)>& received = {}) {
  int sockets[2];
  if (socketpair(AF_UNIX, SOCK_STREAM, 0, sockets))
    throw std::runtime_error("HTTP fixture socketpair");
  ScopeExit close([&] {
    ::close(sockets[0]);
    ::close(sockets[1]);
  });
  std::exception_ptr error;
  std::thread serving([&] {
    try {
      detail::HttpResponseContext http(sockets[1]);
      handler(sockets[1]);
    } catch (...) {
      error = std::current_exception();
    }
    shutdown(sockets[1], SHUT_WR);
  });
  ScopeExit join([&] {
    shutdown(sockets[0], SHUT_RDWR);
    serving.join();
  });
  std::string wire;
  char buffer[4096];
  const timeval timeout{6, 0};
  setsockopt(sockets[0], SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
  for (ssize_t n; (n = read(sockets[0], buffer, sizeof(buffer))) > 0;) {
    wire.append(buffer, n);
    if (received && received(wire)) break;
  }
  join.Run();
  if (error) std::rethrow_exception(error);
  return wire;
}
}  // namespace

// Actual shared native handlers and owned jobs, with explicitly synthetic CPU
// deltas. No model, external gateway, TLS or production authentication claim.
int ResponsesJobsFixture(int argc, char** argv) {
  if (argc != 3 || std::string_view(argv[1]) != "--responses-jobs-fixture")
    return -1;
  try {
    const auto root = std::filesystem::absolute(argv[2]);
    std::filesystem::create_directories(root);
    Fixture f;
    f.options.directory = (root / "objects").string();
    ObjectStore objects(f.options);
    ResponsesJobsOptions options;
    options.timeout = 10s;
    ResponsesJobs jobs(objects, options, [&](auto& job) {
      std::ofstream(root / "executed.jsonl", std::ios::app)
          << "{\"id\":\"" << job.run->Id() << "\"}\n";
      ResponseDelta content;
      content.kind = ResponseDelta::Kind::kContent;
      content.text = "CPU background answer";
      std::string wire;
      if (!job.run->Append(content, "null", &wire) ||
          !job.run->Checkpoint().ok())
        throw std::runtime_error("background CPU generation fixture");
      const auto* metadata = job.run->Envelope().options.Find("metadata");
      if (metadata && metadata->GetString("fixture") == "hold") {
        while (!job.control->Cancelled()) std::this_thread::sleep_for(2ms);
      } else {
        std::this_thread::sleep_for(100ms);
        if (!job.run->Finish(false, 0, {5, 0, 3, 0}, std::time(nullptr), &wire)
                 .ok())
          throw std::runtime_error("CPU background completion");
      }
    });
    jobs.StartRecovery(f.clients, Config());
    const int listener = socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (listener < 0) return 1;
    ScopeExit close_listener([&] { close(listener); });
    sockaddr_in address{};
    address.sin_family = AF_INET;
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    if (bind(listener, reinterpret_cast<sockaddr*>(&address),
             sizeof(address)) ||
        listen(listener, 16))
      return 1;
    socklen_t size = sizeof(address);
    if (getsockname(listener, reinterpret_cast<sockaddr*>(&address), &size))
      return 1;
    std::ofstream(root / "ready.json")
        << "{\"port\":" << ntohs(address.sin_port) << ",\"pid\":" << getpid()
        << "}\n";
    std::atomic<bool> stopping{false};
    std::atomic<int> active{0};
    HttpBodyBudget budget(kAggregateChatBodyBytes);
    std::vector<std::jthread> handlers;
    ScopeExit stop([&] {
      stopping = true;
      jobs.RequestStop();
    });
    const auto until = Clock::now() + 25s;
    while (!stopping && Clock::now() < until && handlers.size() < 128) {
      pollfd pending{listener, POLLIN, 0};
      if (poll(&pending, 1, 100) <= 0) continue;
      const int fd = accept4(listener, nullptr, nullptr, SOCK_CLOEXEC);
      if (fd < 0) return 1;
      if (active.fetch_add(1) >= 16) {
        --active;
        close(fd);
        continue;
      }
      handlers.emplace_back([&, fd] {
        ScopeExit closed([&] {
          close(fd);
          --active;
        });
        ClientLease permit;
        detail::HttpResponseContext http(fd);
        try {
          std::string method, target, body;
          HttpRequestHeaders headers;
          HttpBodyLease body_lease(budget);
          int failure = 400;
          if (!ReadHttpRequest(fd, &method, &target, &body, 5000, &body_lease,
                               &failure, &headers)) {
            detail::SendError(fd, failure, "CPU fixture request rejected");
            return;
          }
          const std::string client =
              headers.authorization == "Bearer cpu-a"   ? "alpha"
              : headers.authorization == "Bearer cpu-b" ? "beta"
                                                        : "";
          if (client.empty()) {
            detail::SendError(fd, 401, "CPU fixture key");
            return;
          }
          if (target == "/__fixture/stop" && method == "POST") {
            stopping = true;
            jobs.RequestStop();
            detail::SendSimple(fd, 200, "OK", "{}", "application/json");
            return;
          }
          const auto identity = f.Identity(client);
          const auto lane =
              IsCancellationRequest(method, target)    ? ClientLane::kCancel
              : IsResponsesObservation(method, target) ? ClientLane::kControl
                                                       : ClientLane::kData;
          if (!permit.Acquire(f.clients, identity, lane)) {
            detail::SendError(fd, 429, "CPU fixture client capacity");
            return;
          }
          http.SetClient(identity, &permit);
          auto access = Access(client);
          access.cancelled = [&, fd] {
            pollfd peer{fd, POLLRDHUP, 0};
            return stopping.load() ||
                   (poll(&peer, 1, 0) > 0 &&
                    (peer.revents & (POLLRDHUP | POLLERR | POLLHUP)));
          };
          if (method == "POST" && target == "/v1/responses")
            ServeBackgroundResponse(fd, objects, jobs, Parse(body), Config(),
                                    access);
          else
            ServeResponsesState(fd, objects, method, target, body, access,
                                &jobs);
        } catch (...) {
          detail::SendExceptionResponse(fd, *http.State());
        }
      });
    }
    return stopping ? 0 : 2;
  } catch (const std::exception& error) {
    std::fprintf(stderr, "Responses jobs fixture: %s\n", error.what());
    return 1;
  }
}

Q4T_TEST(
    responses_jobs_http_creation_transfers_owner_and_cancel_is_idempotent) {
  Fixture f;
  ObjectStore objects(f.options);
  std::atomic<bool> entered{false};
  ResponsesJobs jobs(objects, {}, [&](auto& job) {
    entered = true;
    while (!job.control->Cancelled()) std::this_thread::sleep_for(1ms);
  });
  const auto created = Exchange([&](int fd) {
    ClientLease permit;
    const auto client = f.Identity();
    if (!permit.Acquire(f.clients, client, ClientLane::kData))
      throw std::runtime_error("HTTP create admission");
    detail::HttpResponseContext::Current(fd)->SetClient(client, &permit);
    ServeBackgroundResponse(
        fd, objects, jobs,
        Parse(
            R"({"input":"actual CPU fixture","background":true,"store":false,"reasoning":{"effort":"none"}})"),
        Config(), Access());
  });
  Q4T_CHECK(created.starts_with("HTTP/1.1 200"));
  const auto response = Parse(created.substr(created.find("\r\n\r\n") + 4));
  const auto id = response.GetString("id");
  Q4T_CHECK(response.GetString("status") == "queued" && !id.empty());
  Q4T_CHECK(!response.GetBool("store", true));
  Q4T_CHECK(Wait([&] { return entered.load(); }));
  Q4T_CHECK(f.clients.Usage(0).data_active == 1);
  const auto other = Exchange([&](int fd) {
    ServeResponsesState(fd, objects, "POST", "/v1/responses/" + id + "/cancel",
                        "", Access("beta"), &jobs);
  });
  Q4T_CHECK(other.starts_with("HTTP/1.1 404"));
  for (int i = 0; i < 2; ++i) {
    const auto cancelled = Exchange([&](int fd) {
      ServeResponsesState(fd, objects, "POST",
                          "/v1/responses/" + id + "/cancel", "", Access(),
                          &jobs);
    });
    Q4T_CHECK(cancelled.starts_with("HTTP/1.1 200"));
    const auto value = Parse(cancelled.substr(cancelled.find("\r\n\r\n") + 4));
    Q4T_CHECK(value.GetString("status") == "cancelled");
    Q4T_CHECK(value.Find("error")->IsNull());
  }
  Q4T_CHECK(Wait([&] { return f.clients.Usage(0).data_active == 0; }));
  return true;
}

Q4T_TEST(
    responses_jobs_http_stream_disconnect_retains_job_and_original_replay) {
  Fixture f;
  ObjectStore objects(f.options);
  std::atomic<bool> entered{false};
  std::string id;
  ResponsesJobs jobs(objects, {}, [&](auto& job) {
    ResponseDelta delta;
    delta.kind = ResponseDelta::Kind::kContent;
    delta.text = "actual partial generation";
    std::string wire;
    if (!job.run->Append(delta, "null", &wire) || !job.run->Checkpoint().ok())
      throw std::runtime_error("partial generation fixture");
    id = job.run->Id();
    entered.store(true, std::memory_order_release);
    while (!job.control->Cancelled()) std::this_thread::sleep_for(1ms);
  });
  const auto stream = Exchange(
      [&](int fd) {
        ClientLease permit;
        const auto client = f.Identity();
        if (!permit.Acquire(f.clients, client, ClientLane::kData))
          throw std::runtime_error("stream admission");
        detail::HttpResponseContext::Current(fd)->SetClient(client, &permit);
        auto access = Access();
        access.cancelled = [fd] {
          pollfd peer{fd, POLLRDHUP, 0};
          return poll(&peer, 1, 0) > 0 &&
                 (peer.revents & (POLLRDHUP | POLLERR | POLLHUP));
        };
        ServeBackgroundResponse(
            fd, objects, jobs,
            Parse(
                R"({"input":"question","background":true,"stream":true,"stream_options":{"include_obfuscation":false},"reasoning":{"effort":"none"}})"),
            Config(), access);
      },
      [](std::string_view wire) {
        return wire.find("actual partial generation") != wire.npos;
      });
  // The subscriber closes its socket; generation still owns its permit.
  Q4T_CHECK(entered.load(std::memory_order_acquire));
  Q4T_CHECK(stream.starts_with("HTTP/1.1 200"));
  Q4T_CHECK(stream.find("event: response.created") != stream.npos);
  Q4T_CHECK(stream.find("actual partial generation") != stream.npos);
  Q4T_CHECK(jobs.Health().owned == 1 && f.clients.Usage(0).data_active == 1);
  Q4T_CHECK(Ok(jobs.Cancel(id, Access())));
  const auto resumed = Exchange([&](int fd) {
    ServeResponsesState(
        fd, objects, "GET",
        "/v1/responses/" + id +
            "?stream=true&starting_after=1&include_obfuscation=false",
        "", Access(), &jobs);
  });
  Q4T_CHECK(resumed.starts_with("HTTP/1.1 200"));
  Q4T_CHECK(resumed.find("response.created") == resumed.npos);
  Q4T_CHECK(resumed.find("actual partial generation") != resumed.npos);
  Q4T_CHECK(resumed.find("response.completed") == resumed.npos);
  Q4T_CHECK(resumed.find("response.cancelled") == resumed.npos);
  Q4T_CHECK(resumed.find("[DONE]") == resumed.npos);
  return true;
}

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

Q4T_TEST(responses_recovery_owner_requires_actual_process_exit_or_reuse) {
  auto owner = CurrentResponseOwner();
  Q4T_CHECK(ResponseOwnerStatus(owner) == ResponseOwnerState::kAlive);
  auto changed = owner;
  Set(&changed, "start_ticks",
      Parse(std::to_string(owner.GetInt("start_ticks") + 1)));
  Q4T_CHECK(ResponseOwnerStatus(changed) == ResponseOwnerState::kGone);
  changed = owner;
  Set(&changed, "pid_namespace",
      Parse(std::to_string(owner.GetInt("pid_namespace") + 1)));
  Q4T_CHECK(ResponseOwnerStatus(changed) == ResponseOwnerState::kUnknown);
  Q4T_CHECK(ResponseOwnerStatus(Json()) == ResponseOwnerState::kUnknown);
  int pipefd[2];
  Q4T_CHECK(pipe(pipefd) == 0);
  const pid_t child = fork();
  if (child == 0) {
    close(pipefd[1]);
    char byte;
    const auto ignored = read(pipefd[0], &byte, 1);
    (void)ignored;
    _exit(0);
  }
  close(pipefd[0]);
  ScopeExit cleanup([&] {
    close(pipefd[1]);
    if (child > 0) {
      int status;
      while (waitpid(child, &status, 0) < 0 && errno == EINTR) {
      }
    }
  });
  Q4T_CHECK(child > 0);
  q4t::service::ProcessRecord record;
  Q4T_CHECK(q4t::service::ReadProcess(child, &record));
  Set(&owner, "pid", Parse(std::to_string(child)));
  Set(&owner, "start_ticks",
      Parse(std::to_string(record.identity.start_ticks)));
  Q4T_CHECK(ResponseOwnerStatus(owner) == ResponseOwnerState::kAlive);
  Q4T_CHECK(write(pipefd[1], "x", 1) == 1);
  Q4T_CHECK(Wait(
      [&] { return ResponseOwnerStatus(owner) == ResponseOwnerState::kGone; }));
  return true;
}

Q4T_TEST(responses_recovery_scan_redacts_payload_and_survives_cursor_deletion) {
  Fixture f;
  ObjectStore objects(f.options);
  for (int i = 0; i < 10; ++i) Interrupted(objects);
  auto page = objects.ScanResponses("", Clock::now() + 5s);
  Q4T_CHECK(Ok(page) && page.value.GetBool("has_more"));
  Q4T_CHECK(page.value.GetArray("data")->array.size() == 8);
  const auto wire = OutputJson(page.value);
  Q4T_CHECK(wire.find("private original input") == wire.npos);
  for (const auto& row : page.value.GetArray("data")->array)
    Q4T_CHECK(row.GetString("scope") == "client:alpha" && !row.Has("request") &&
              !row.Has("output"));
  const auto cursor = page.value.GetString("cursor");
  ObjectRequest erase;
  erase.operation = ObjectOperation::kErase;
  erase.scope = "client:alpha";
  erase.id = page.value.GetArray("data")->array.back().GetString("id");
  Q4T_CHECK(Ok(objects.Execute(erase, Clock::now() + 5s)));
  page = objects.ScanResponses(cursor, Clock::now() + 5s);
  Q4T_CHECK(Ok(page) && !page.value.GetBool("has_more"));
  Q4T_CHECK(page.value.GetArray("data")->array.size() == 2);
  Q4T_CHECK(
      objects.ScanResponses("../../foreign", Clock::now() + 5s).http_status ==
      400);
  return true;
}

Q4T_TEST(
    responses_recovery_resumes_queued_identity_and_fails_started_without_replay) {
  Fixture f;
  ObjectStore objects(f.options);
  const auto queued = Interrupted(objects);
  const auto started = Interrupted(objects, true);
  const auto deadline = Raw(objects, queued)
                            .value.Find("data")
                            ->Find("continuation")
                            ->GetInt("execution_deadline_at");
  std::atomic<int> executions{0};
  std::string executed;
  ResponsesJobs jobs(objects, {}, [&](auto& job) {
    executed = job.run->Id();
    ++executions;
    Complete(job);
  });
  jobs.StartRecovery(f.clients, Config());
  Q4T_CHECK(Wait([&] {
    return jobs.Health().recovered == 1 && jobs.Health().interrupted == 1 &&
           jobs.Health().owned == 0;
  }));
  Q4T_CHECK(executions == 1 && executed == queued);
  const auto complete = Raw(objects, queued);
  Q4T_CHECK(Ok(complete));
  const auto* data = complete.value.Find("data");
  Q4T_CHECK(data->Find("response")->GetString("status") == "completed");
  Q4T_CHECK(data->Find("continuation")->GetInt("execution_deadline_at") ==
            deadline);
  Q4T_CHECK(OutputJson(*data->Find("history")).find("private original input") !=
            std::string::npos);
  const auto failure = ResponsesState(objects).Retrieve(started, Access());
  Q4T_CHECK(Ok(failure) && failure.value.GetString("status") == "failed");
  Q4T_CHECK(failure.value.Find("error")->GetString("code") ==
            "background_execution_interrupted");
  Q4T_CHECK(OutputJson(failure.value).find("original partial output") !=
            std::string::npos);
  Q4T_CHECK(f.clients.Usage(0).data_active == 0);
  return true;
}

Q4T_TEST(
    responses_recovery_preserves_live_and_unknown_owners_without_blocking_jobs) {
  Fixture f;
  ObjectStore objects(f.options);
  const auto live = Interrupted(objects, true, "client:alpha", false);
  const auto unknown = Interrupted(objects, false, "client:alpha", false);
  EditRecord(objects, unknown, "client:alpha", [](Json& data) {
    Set(Field(&data, "continuation"), "owner", Json());
  });
  std::atomic<int> executed{0};
  ResponsesJobs jobs(objects, {}, [&](auto& job) {
    ++executed;
    Complete(job);
  });
  jobs.StartRecovery(f.clients, Config());
  Q4T_CHECK(Wait([&] { return jobs.Health().recovery_scanned; }));
  Q4T_CHECK(jobs.Health().recovery_pending == 2 && executed == 0);
  Q4T_CHECK(ResponsesState(objects)
                .Retrieve(live, Access())
                .value.GetString("status") == "in_progress");
  Q4T_CHECK(ResponsesState(objects)
                .Retrieve(unknown, Access())
                .value.GetString("status") == "queued");
  const auto accepted = f.Submit(jobs, objects);
  Q4T_CHECK(Ok(accepted));
  Q4T_CHECK(Wait([&] { return executed == 1 && jobs.Health().owned == 0; }));
  Q4T_CHECK(jobs.Health().recovered == 0 && jobs.Health().interrupted == 0);
  return true;
}

Q4T_TEST(responses_recovery_revoked_expired_and_spoofed_scope_never_execute) {
  Fixture f;
  ObjectStore objects(f.options);
  const auto revoked = Interrupted(objects, false, "client:removed");
  const auto expired = Interrupted(objects, false, "client:alpha", true, true);
  // A scope in user metadata cannot grant another client's authority. Even
  // damaged internal continuation is checked against the durable owner hash.
  EditRecord(objects, revoked, "client:removed", [](Json& data) {
    Set(Field(&data, "continuation"), "scope", Parse("\"client:alpha\""));
  });
  const auto revoked_valid = Interrupted(objects, false, "client:removed");
  std::atomic<int> executed{0};
  ResponsesJobs jobs(objects, {}, [&](auto&) { ++executed; });
  jobs.StartRecovery(f.clients, Config());
  Q4T_CHECK(Wait([&] { return jobs.Health().recovery_scanned; }));
  Q4T_CHECK(executed == 0 && jobs.Health().interrupted == 2 &&
            jobs.Health().recovery_pending == 1);
  Q4T_CHECK(ResponsesState(objects)
                .Retrieve(revoked_valid, Access("removed"))
                .value.Find("error")
                ->GetString("code") == "background_client_revoked");
  Q4T_CHECK(ResponsesState(objects)
                .Retrieve(expired, Access())
                .value.Find("error")
                ->GetString("code") == "deadline_exceeded");
  Q4T_CHECK(ResponsesState(objects).Retrieve(revoked, Access()).http_status ==
            404);
  return true;
}

Q4T_TEST(responses_recovery_capacity_defers_then_resumes_without_new_deadline) {
  Fixture f;
  ObjectStore objects(f.options);
  Interrupted(objects);
  Interrupted(objects);
  ResponsesJobsOptions options;
  options.max_jobs = 1;
  options.max_client_jobs = 1;
  std::atomic<int> executed{0};
  std::atomic<bool> release{false};
  ResponsesJobs jobs(objects, options, [&](auto& job) {
    ++executed;
    while (!release && !job.control->Cancelled())
      std::this_thread::sleep_for(1ms);
    if (!job.control->Cancelled()) Complete(job);
  });
  jobs.StartRecovery(f.clients, Config());
  Q4T_CHECK(Wait([&] {
    const auto health = jobs.Health();
    return health.recovery_scanned && executed == 1 && health.owned == 1 &&
           health.recovery_pending == 1;
  }));
  release = true;
  Q4T_CHECK(Wait([&] {
    return jobs.Health().recovered == 2 && jobs.Health().owned == 0;
  }));
  Q4T_CHECK(executed == 2 && f.clients.Usage(0).data_active == 0);
  return true;
}

Q4T_TEST(
    responses_recovery_storage_failure_retries_without_starting_execution) {
  Fixture f;
  auto previous_store = std::make_unique<ObjectStore>(f.options);
  const auto id = Interrupted(*previous_store);
  previous_store.reset();
  // Model controllers are intentionally unique per process. Inject the real
  // directory flock conflict rather than constructing a forbidden controller.
  const int lock_fd = open((f.options.directory + "/.q4t-lock").c_str(),
                           O_RDWR | O_CLOEXEC | O_NOFOLLOW);
  ScopeExit close_lock([&] {
    if (lock_fd >= 0) close(lock_fd);
  });
  Q4T_CHECK(lock_fd >= 0 && flock(lock_fd, LOCK_EX | LOCK_NB) == 0);
  ObjectStore objects(f.options);
  std::atomic<int> executed{0};
  ResponsesJobs jobs(objects, {}, [&](auto& job) {
    ++executed;
    Complete(job);
  });
  jobs.StartRecovery(f.clients, Config());
  Q4T_CHECK(Wait([&] { return jobs.Health().recovery_errors > 0; }));
  Q4T_CHECK(executed == 0 && !jobs.Health().failed);
  close_lock.Run();  // Release only this fixture's injected file lock.
  Q4T_CHECK(Wait([&] {
    return jobs.Health().recovered == 1 && jobs.Health().owned == 0;
  }));
  Q4T_CHECK(executed == 1);
  Q4T_CHECK(ResponsesState(objects)
                .Retrieve(id, Access())
                .value.GetString("status") == "completed");
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

Q4T_TEST(responses_jobs_large_retained_input_has_exact_admission_and_release) {
  Fixture f;
  f.options.max_client_bytes = 256ULL << 20;
  f.options.max_total_bytes = 512ULL << 20;
  ObjectStore objects(f.options);
  auto request = Parse(R"({"input":"","background":true,
                          "reasoning":{"effort":"none"}})");
  Field(&request, "input")->str.assign(17ULL << 20, 'x');
  const auto prepare = [&] {
    auto run = std::make_unique<ResponsesExecution>(objects);
    auto access = Access();
    access.deadline = Clock::now() + 30s;
    const auto result =
        run->Prepare(request, Config(), access, std::time(nullptr), true);
    if (!Ok(result)) throw std::runtime_error("large job preparation");
    return run;
  };
  auto run = prepare();
  const size_t bytes = run->RetainedInputBytes();
  Q4T_CHECK(bytes > (64ULL << 20) && bytes < kBackgroundInputBytes);
  const auto submit = [&](ResponsesJobs& jobs,
                          std::unique_ptr<ResponsesExecution> input) {
    const auto client = f.Identity();
    ClientLease permit;
    if (!permit.Acquire(f.clients, client, ClientLane::kData))
      throw std::runtime_error("large job permit");
    return jobs.Submit(std::move(input), client, [&](ClientLease& target) {
      return permit.TransferTo(target);
    });
  };
  ResponsesJobsOptions options;
  options.max_input_bytes = bytes - 1;
  ResponsesJobs small(objects, options, Complete);
  Q4T_CHECK(submit(small, std::move(run)).code ==
            "background_capacity_exhausted");
  Q4T_CHECK(small.Health().owned == 0 && small.Health().input_bytes == 0);
  options.max_input_bytes = bytes;
  std::atomic<bool> executing{false};
  ResponsesJobs exact(objects, options, [&](auto& job) {
    executing = true;
    while (!job.control->Cancelled()) std::this_thread::sleep_for(1ms);
  });
  const auto accepted = submit(exact, prepare());
  Q4T_CHECK(Ok(accepted) && Wait([&] { return executing.load(); }));
  Q4T_CHECK(exact.Health().owned == 1 && exact.Health().input_bytes == bytes);
  Q4T_CHECK(f.clients.Usage(0).data_active == 1);
  exact.RequestStop();
  exact.Join();
  Q4T_CHECK(exact.Health().owned == 0 && exact.Health().input_bytes == 0);
  Q4T_CHECK(f.clients.Usage(0).data_active == 0);
  const auto restored = ResponsesState(objects).Retrieve(
      accepted.value.GetString("id"), Access());
  Q4T_CHECK(Ok(restored) && restored.value.GetString("status") == "cancelled");
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

Q4T_TEST(responses_batch_recovery_real_exit_waits_and_yields_to_online) {
  Fixture f(true);
  int channel[2];
  Q4T_CHECK(pipe2(channel, O_CLOEXEC) == 0);
  const pid_t child = fork();
  Q4T_CHECK(child >= 0);
  if (child == 0) {
    close(channel[0]);
    try {
      std::string id;
      {
        ObjectStore objects(f.options);
        id = Interrupted(objects, false, "client:alpha", false, false, true);
      }
      const bool sent = write(channel[1], id.data(), id.size()) ==
                        static_cast<ssize_t>(id.size());
      _exit(sent ? 0 : 2);
    } catch (...) {
      _exit(3);
    }
  }
  close(channel[1]);
  bool reaped = false;
  ScopeExit cleanup([&] {
    close(channel[0]);
    if (!reaped) {
      kill(child, SIGKILL);
      int status;
      while (waitpid(child, &status, 0) < 0 && errno == EINTR) {
      }
    }
  });
  pollfd ready{channel[0], POLLIN, 0};
  Q4T_CHECK(poll(&ready, 1, 5000) > 0);
  char id_buffer[256];
  const auto length = read(channel[0], id_buffer, sizeof(id_buffer));
  Q4T_CHECK(length > 0);
  const std::string id(id_buffer, length);
  int status = 0;
  Q4T_CHECK(waitpid(child, &status, 0) == child);
  reaped = true;
  Q4T_CHECK(WIFEXITED(status) && WEXITSTATUS(status) == 0);
  ObjectStore objects(f.options);
  const auto before = Raw(objects, id);
  Q4T_CHECK(Ok(before));
  ClientRateLease blocker(ClientRateDomain::kBatch);
  Q4T_CHECK(blocker.Reserve(f.clients, f.Identity(), 500, 0) ==
            ClientRateResult::kAllowed);
  std::atomic<int> batch{0}, online{0}, plans{0};
  ResponsesJobs jobs(
      objects, {},
      [&](auto& job) {
        if (job.run->RateDomain() == ClientRateDomain::kBatch) {
          ++batch;
          CompleteBatch(f, job);
        } else {
          ++online;
          Complete(job);
        }
      },
      BatchAdmission(f, &plans));
  jobs.StartRecovery(f.clients, Config());
  Q4T_CHECK(Wait([&] { return jobs.Health().rate_deferrals >= 2; }));
  Q4T_CHECK(jobs.Health().rate_waiting == 1 && batch == 0 && plans == 1 &&
            f.clients.Usage(0).data_active == 0);
  const auto waiting = Raw(objects, id);
  Q4T_CHECK(Ok(waiting) &&
            OutputJson(before.value) == OutputJson(waiting.value));
  Q4T_CHECK(Ok(f.Submit(jobs, objects, "beta")));
  Q4T_CHECK(Wait([&] { return online == 1 && jobs.Health().owned == 1; }));
  Q4T_CHECK(batch == 0 && BatchUsage(f).admitted == 1);
  blocker.Settle();
  Q4T_CHECK(Wait([&] { return jobs.Health().owned == 0; }));
  const auto after = Raw(objects, id);
  Q4T_CHECK(Ok(after) && batch == 1 && plans == 1 &&
            after.value.Find("data")->Find("response")->GetString("status") ==
                "completed");
  Q4T_CHECK(after.value.Find("data")
                ->Find("continuation")
                ->GetInt("execution_deadline_at") ==
            before.value.Find("data")
                ->Find("continuation")
                ->GetInt("execution_deadline_at"));
  const auto usage = BatchUsage(f);
  Q4T_CHECK(usage.admitted == 2 && usage.reserved == 0 &&
            usage.settled_tokens > 2 && jobs.Health().rate_waiting == 0 &&
            f.clients.Usage(0).data_active == 0 && !jobs.Health().failed);
  Q4T_CHECK(f.clients.RateUsage(0).admitted == 0);
  return true;
}

Q4T_TEST(responses_batch_recovery_minute_wait_cancel_and_original_deadline) {
  for (const bool cancel : {true, false}) {
    Fixture f(true);
    ObjectStore objects(f.options);
    const auto id = InterruptedBatch(objects);
    if (!cancel)
      EditRecord(objects, id, "client:alpha", [](Json& data) {
        Set(Field(&data, "continuation"), "execution_deadline_at",
            Parse(std::to_string(std::time(nullptr) + 2)));
      });
    const auto original = Raw(objects, id);
    ClientRateLease blocker(ClientRateDomain::kBatch);
    Q4T_CHECK(blocker.Reserve(f.clients, f.Identity(), 500, 0) ==
              ClientRateResult::kAllowed);
    std::atomic<int> calls{0};
    ResponsesJobs jobs(objects, {}, [&](auto&) { ++calls; }, BatchAdmission(f));
    jobs.StartRecovery(f.clients, Config());
    Q4T_CHECK(Wait([&] { return jobs.Health().rate_waiting == 1; }));
    Q4T_CHECK(f.clients.Usage(0).data_active == 0);
    if (cancel) Q4T_CHECK(Ok(jobs.Cancel(id, Access())));
    Q4T_CHECK(Wait([&] { return jobs.Health().owned == 0; }));
    const auto reply = ResponsesState(objects).Retrieve(id, Access());
    Q4T_CHECK(Ok(reply) && calls == 0 && !jobs.Health().failed &&
              reply.value.GetString("status") ==
                  (cancel ? "cancelled" : "failed"));
    if (!cancel)
      Q4T_CHECK(reply.value.Find("error")->GetString("code") ==
                "deadline_exceeded");
    const auto final = Raw(objects, id);
    Q4T_CHECK(Ok(final) && final.value.Find("data")
                                   ->Find("continuation")
                                   ->GetInt("execution_deadline_at") ==
                               original.value.Find("data")
                                   ->Find("continuation")
                                   ->GetInt("execution_deadline_at"));
    Q4T_CHECK(BatchUsage(f).admitted == 1 && BatchUsage(f).reserved == 500 &&
              jobs.Health().rate_waiting == 0 &&
              f.clients.Usage(0).data_active == 0);
  }
  return true;
}

Q4T_TEST(responses_batch_recovery_refreshes_policy_and_replans_original_input) {
  for (const bool reject_input : {false, true}) {
    Fixture f(true);
    auto policy = *f.clients.Snapshot();
    policy.revision = 2;
    policy.clients[0].requests_per_minute = 1;
    Q4T_CHECK(f.clients.Publish(std::move(policy), 1, 8, 8));
    ClientRateLease blocker(ClientRateDomain::kBatch);
    Q4T_CHECK(blocker.Reserve(f.clients, f.Identity(), 1, 0) ==
              ClientRateResult::kAllowed);
    blocker.Settle();
    ObjectStore objects(f.options);
    const auto id = InterruptedBatch(objects);
    std::atomic<int> calls{0}, plans{0};
    ResponsesJobs jobs(
        objects, {},
        [&](auto& job) {
          ++calls;
          CompleteBatch(f, job);
        },
        BatchAdmission(f, &plans));
    jobs.StartRecovery(f.clients, Config());
    Q4T_CHECK(Wait([&] { return jobs.Health().rate_waiting == 1; }));
    Q4T_CHECK(calls == 0 && plans == 1);
    policy = *f.clients.Snapshot();
    policy.revision = 3;
    policy.clients[0].requests_per_minute = 2;
    if (reject_input) policy.clients[0].max_prompt_tokens = 1;
    Q4T_CHECK(f.clients.Publish(std::move(policy), 2, 8, 8));
    Q4T_CHECK(Wait([&] { return jobs.Health().owned == 0; }));
    const auto reply = ResponsesState(objects).Retrieve(id, Access());
    Q4T_CHECK(Ok(reply) && plans == 2 && calls == (reject_input ? 0 : 1) &&
              reply.value.GetString("status") ==
                  (reject_input ? "failed" : "completed"));
    Q4T_CHECK(BatchUsage(f).admitted == (reject_input ? 1 : 2) &&
              BatchUsage(f).reserved == 0 &&
              f.clients.Usage(0).data_active == 0);
  }
  return true;
}

Q4T_TEST(responses_batch_recovery_wait_reacquires_data_without_spending_rpm) {
  Fixture f(true);
  ObjectStore objects(f.options);
  const auto id = InterruptedBatch(objects);
  ClientRateLease blocker(ClientRateDomain::kBatch);
  Q4T_CHECK(blocker.Reserve(f.clients, f.Identity(), 500, 0) ==
            ClientRateResult::kAllowed);
  std::atomic<int> calls{0}, plans{0};
  ResponsesJobs jobs(
      objects, {},
      [&](auto& job) {
        ++calls;
        CompleteBatch(f, job);
      },
      BatchAdmission(f, &plans));
  jobs.StartRecovery(f.clients, Config());
  Q4T_CHECK(Wait([&] { return jobs.Health().rate_waiting == 1; }));
  std::optional<ClientLease> online(std::in_place);
  Q4T_CHECK(online->Acquire(f.clients, f.Identity(), ClientLane::kData));
  const auto deferred = jobs.Health().rate_deferrals;
  blocker.Settle();
  Q4T_CHECK(Wait([&] { return jobs.Health().rate_deferrals > deferred; }));
  Q4T_CHECK(calls == 0 && plans == 1 && BatchUsage(f).admitted == 1 &&
            BatchUsage(f).reserved == 0 && f.clients.Usage(0).data_active == 1);
  Q4T_CHECK(ResponsesState(objects)
                .Retrieve(id, Access())
                .value.GetString("status") == "queued");
  online.reset();
  Q4T_CHECK(Wait([&] { return jobs.Health().owned == 0; }));
  Q4T_CHECK(calls == 1 && plans == 1 && BatchUsage(f).admitted == 2 &&
            BatchUsage(f).reserved == 0 && f.clients.Usage(0).data_active == 0);
  return true;
}

Q4T_TEST(responses_batch_recovery_unknown_start_refunds_and_never_replays) {
  Fixture f(true);
  f.options.io_timeout_ms = 250;
  std::string id;
  {
    ObjectStore original(f.options);
    id = InterruptedBatch(original);
  }
  ObjectWorkerEntryForTesting("--q4t-object-worker-lost-ack");
  ScopeExit reset([] { ObjectWorkerEntryForTesting({}); });
  ObjectStore objects(f.options);
  std::atomic<int> calls{0};
  ResponsesJobs jobs(objects, {}, [&](auto&) { ++calls; }, BatchAdmission(f));
  jobs.StartRecovery(f.clients, Config());
  Q4T_CHECK(Wait([&] {
    return jobs.Health().recovered == 1 && jobs.Health().owned == 0;
  }));
  const auto reply = ResponsesState(objects).Retrieve(id, Access());
  Q4T_CHECK(Ok(reply) && calls == 0 &&
            reply.value.GetString("status") == "failed");
  Q4T_CHECK(reply.value.Find("error")->GetString("code") ==
            "background_execution_interrupted");
  Q4T_CHECK(BatchUsage(f).admitted == 1 && BatchUsage(f).reserved == 0 &&
            BatchUsage(f).settled_tokens == 0 && !jobs.Health().failed &&
            f.clients.Usage(0).data_active == 0);
  return true;
}

Q4T_TEST(responses_batch_recovery_missing_admission_waits_and_shutdown_drains) {
  Fixture f(true);
  ObjectStore objects(f.options);
  const auto id = InterruptedBatch(objects);
  std::atomic<int> calls{0};
  ResponsesJobs jobs(objects, {}, [&](auto&) { ++calls; });
  jobs.StartRecovery(f.clients, Config());
  Q4T_CHECK(Wait([&] { return jobs.Health().rate_waiting == 1; }));
  Q4T_CHECK(calls == 0 && f.clients.Usage(0).data_active == 0 &&
            BatchUsage(f).admitted == 0 && !jobs.Health().failed);
  jobs.RequestStop();
  jobs.Join();
  Q4T_CHECK(jobs.Health().owned == 0 && jobs.Health().rate_waiting == 0);
  const auto reply = ResponsesState(objects).Retrieve(id, Access());
  Q4T_CHECK(Ok(reply) && reply.value.GetString("status") == "cancelled" &&
            calls == 0);
  return true;
}

Q4T_TEST(responses_batch_recovery_impossible_window_fails_without_waiting) {
  Fixture f(true);
  auto policy = *f.clients.Snapshot();
  policy.revision = 2;
  policy.clients[0].tokens_per_minute = 1;
  Q4T_CHECK(f.clients.Publish(std::move(policy), 1, 8, 8));
  ObjectStore objects(f.options);
  const auto id = InterruptedBatch(objects);
  std::atomic<int> calls{0};
  ResponsesJobs jobs(objects, {}, [&](auto&) { ++calls; }, BatchAdmission(f));
  jobs.StartRecovery(f.clients, Config());
  Q4T_CHECK(Wait([&] {
    return jobs.Health().recovered == 1 && jobs.Health().owned == 0;
  }));
  const auto reply = ResponsesState(objects).Retrieve(id, Access());
  Q4T_CHECK(Ok(reply) && calls == 0 &&
            reply.value.GetString("status") == "failed");
  Q4T_CHECK(reply.value.Find("error")->GetString("code") ==
            "batch_request_token_reservation_exceeded");
  Q4T_CHECK(jobs.Health().rate_deferrals == 0 && BatchUsage(f).admitted == 0 &&
            f.clients.Usage(0).data_active == 0);
  return true;
}

Q4T_TEST(responses_batch_recovery_invalid_owner_or_budget_never_starts) {
  for (const bool missing_owner : {true, false}) {
    Fixture f(true);
    ObjectStore objects(f.options);
    const auto id = InterruptedBatch(objects);
    std::atomic<int> calls{0};
    ResponsesJobs jobs(
        objects, {}, [&](auto&) { ++calls; },
        [&](auto& job) {
          if (missing_owner) return ObjectReply{};
          return AdmitBackgroundBatchRate(
              f.clients, job, Config(),
              [](auto&, const auto&, const auto&, auto* budget) {
                *budget = {1, kBatchTokenLimit, 0, 0, true};
                return ObjectReply{};
              });
        });
    jobs.StartRecovery(f.clients, Config());
    Q4T_CHECK(Wait([&] {
      return jobs.Health().recovered == 1 && jobs.Health().owned == 0;
    }));
    const auto reply = ResponsesState(objects).Retrieve(id, Access());
    Q4T_CHECK(Ok(reply) && calls == 0 &&
              reply.value.GetString("status") == "failed");
    Q4T_CHECK(reply.value.Find("error")->GetString("code") ==
              (missing_owner ? "background_rate_invalid"
                             : "background_rate_budget_invalid"));
    Q4T_CHECK(BatchUsage(f).admitted == 0 &&
              f.clients.Usage(0).data_active == 0);
  }
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

namespace {
bool PrepareForeground(ResponsesExecution& run, std::string client = "alpha",
                       bool store = true) {
  auto request = Parse(R"({"input":"question","reasoning":{"effort":"none"}})");
  if (!store) Set(&request, "store", Parse("false"));
  return Ok(run.Prepare(request, Config(), Access(client), std::time(nullptr)));
}
ResponseDelta ForegroundText() {
  ResponseDelta result;
  result.kind = ResponseDelta::Kind::kContent;
  result.text = "actual foreground text";
  return result;
}
struct ResetForegroundWorker {
  ~ResetForegroundWorker() { ObjectWorkerEntryForTesting({}); }
};
}  // namespace

Q4T_TEST(
    responses_foreground_reserves_before_write_and_never_closes_live_work) {
  Fixture f;
  ObjectStore objects(f.options);
  ResponsesJobsOptions limits;
  limits.max_jobs = 2;
  limits.max_client_jobs = 1;
  ResponsesJobs jobs(objects, limits, [](auto&) {
    throw std::runtime_error("unexpected execution");
  });
  ResponsesExecution a(objects, &jobs, true), duplicate(objects, &jobs, true),
      b(objects, &jobs, true), full(objects, &jobs, true),
      stateless(objects, &jobs, true);
  Q4T_CHECK(PrepareForeground(a) && PrepareForeground(duplicate) &&
            PrepareForeground(b, "beta") && PrepareForeground(full, "gamma") &&
            PrepareForeground(stateless, "alpha", false));
  std::string wire;
  Q4T_CHECK(Ok(a.Begin(&wire)));
  Q4T_CHECK(duplicate.Begin(&wire).http_status == 429);
  Q4T_CHECK(
      ResponsesState(objects).Retrieve(duplicate.Id(), Access()).http_status ==
      404);
  Q4T_CHECK(Ok(b.Begin(&wire)) && full.Begin(&wire).http_status == 429);
  Q4T_CHECK(Ok(stateless.Begin(&wire)) && Ok(stateless.CloseUnfinished()));
  std::this_thread::sleep_for(300ms);
  Q4T_CHECK(jobs.Health().foreground_owned == 2 &&
            jobs.Health().foreground_active == 2 &&
            jobs.Health().foreground_pending == 0 &&
            jobs.Health().foreground_reconciled == 0);
  Q4T_CHECK(ResponsesState(objects)
                .Retrieve(a.Id(), Access())
                .value.GetString("status") == "in_progress");
  Q4T_CHECK(Ok(a.CloseUnfinished()) && Ok(b.CloseUnfinished()));
  Q4T_CHECK(jobs.Health().foreground_owned == 0);
  uint64_t old = 0, current = 0;
  Q4T_CHECK(Ok(jobs.Reserve("client:alpha", "resp_old", &old)));
  jobs.Close(old, true);
  Q4T_CHECK(Ok(jobs.Reserve("client:alpha", "resp_new", &current)) &&
            old != current);
  jobs.Close(old, false);
  Q4T_CHECK(jobs.Health().foreground_active == 1);
  jobs.Close(current, true);
  return true;
}

Q4T_TEST(responses_foreground_unknown_begin_is_closed_without_model_execution) {
  ResetForegroundWorker reset;
  ObjectWorkerEntryForTesting("--q4t-object-worker-lost-ack");
  Fixture f;
  f.options.io_timeout_ms = 100;
  ObjectStore objects(f.options);
  std::atomic<int> calls{0};
  ResponsesJobs jobs(objects, {}, [&](auto&) { ++calls; });
  ResponsesExecution run(objects, &jobs, true);
  Q4T_CHECK(PrepareForeground(run));
  std::string wire;
  const auto result = run.Begin(&wire);
  Q4T_CHECK(!result.ok() && result.commit_unknown && !run.Begun());
  Q4T_CHECK(jobs.Health().foreground_active == 1);
  Q4T_CHECK(Ok(run.CloseUnfinished()));
  Q4T_CHECK(Wait([&] { return jobs.Health().foreground_owned == 0; }));
  const auto saved = ResponsesState(objects).Retrieve(run.Id(), Access());
  Q4T_CHECK(Ok(saved) && saved.value.GetString("status") == "failed");
  Q4T_CHECK(saved.value.Find("error")->GetString("code") ==
            "response_execution_interrupted");
  Q4T_CHECK(calls == 0 && jobs.Health().foreground_reconciled == 1);
  return true;
}

Q4T_TEST(responses_foreground_unknown_success_preserves_committed_result) {
  ResetForegroundWorker reset;
  ObjectWorkerEntryForTesting("--q4t-object-worker-commit-pause");
  Fixture f;
  f.options.io_timeout_ms = 100;
  ObjectStore objects(f.options);
  ResponsesJobs jobs(objects, {}, [](auto&) {
    throw std::runtime_error("unexpected execution");
  });
  ResponsesExecution run(objects, &jobs, true);
  Q4T_CHECK(PrepareForeground(run));
  std::string wire;
  Q4T_CHECK(Ok(run.Begin(&wire)) &&
            run.Append(ForegroundText(), "null", &wire));
  const auto result =
      run.Finish(false, 0, {7, 0, 2, 0}, std::time(nullptr), &wire);
  Q4T_CHECK(!result.ok() && result.commit_unknown && wire.empty());
  const auto before = Raw(objects, run.Id());
  Q4T_CHECK(Ok(before));
  Q4T_CHECK(Ok(run.CloseUnfinished()));
  Q4T_CHECK(Wait([&] { return jobs.Health().foreground_owned == 0; }));
  const auto after = Raw(objects, run.Id());
  Q4T_CHECK(Ok(after) && OutputJson(before.value) == OutputJson(after.value));
  Q4T_CHECK(after.value.Find("data")->Find("response")->GetString("status") ==
            "completed");
  Q4T_CHECK(jobs.Health().foreground_reconciled == 1 && !jobs.Health().failed);
  return true;
}

Q4T_TEST(responses_foreground_unknown_checkpoint_preserves_partial_output) {
  ResetForegroundWorker reset;
  ObjectWorkerEntryForTesting("--q4t-object-worker-second-put-lost-ack");
  Fixture f;
  f.options.io_timeout_ms = 100;
  ObjectStore objects(f.options);
  ResponsesJobs jobs(objects, {}, [](auto&) {
    throw std::runtime_error("unexpected execution");
  });
  ResponsesExecution run(objects, &jobs, true);
  Q4T_CHECK(PrepareForeground(run));
  std::string wire;
  Q4T_CHECK(Ok(run.Begin(&wire)) &&
            run.Append(ForegroundText(), "null", &wire));
  const auto checkpoint = run.Checkpoint();
  Q4T_CHECK(!checkpoint.ok() && checkpoint.commit_unknown);
  Q4T_CHECK(Ok(run.CloseUnfinished()));
  Q4T_CHECK(Wait([&] { return jobs.Health().foreground_owned == 0; }));
  const auto actual = ResponsesState(objects).Retrieve(run.Id(), Access());
  Q4T_CHECK(Ok(actual) && actual.value.GetString("status") == "failed" &&
            OutputJson(actual.value).find("actual foreground text") !=
                std::string::npos);
  return true;
}

Q4T_TEST(responses_foreground_close_failure_transfers_only_after_cleanup) {
  Fixture f;
  ObjectStore objects(f.options);
  ResponsesJobs jobs(objects, {}, [](auto&) {
    throw std::runtime_error("unexpected execution");
  });
  ResponsesExecution run(objects, &jobs, true);
  Q4T_CHECK(PrepareForeground(run));
  std::string wire;
  Q4T_CHECK(Ok(run.Begin(&wire)) &&
            run.Append(ForegroundText(), "null", &wire));
  Q4T_CHECK(Ok(run.Checkpoint()));
  FailObjectControllerForTesting();
  Q4T_CHECK(!run.CloseUnfinished().ok());
  Q4T_CHECK(Wait([&] { return jobs.Health().foreground_owned == 0; }));
  const auto actual = ResponsesState(objects).Retrieve(run.Id(), Access());
  Q4T_CHECK(Ok(actual) && actual.value.GetString("status") == "failed" &&
            OutputJson(actual.value).find("actual foreground text") !=
                std::string::npos);
  Q4T_CHECK(objects.Health().controller_starts == 1);
  return true;
}

Q4T_TEST(
    responses_foreground_unavailable_store_retains_bounded_owner_and_backoff) {
  ObjectStoreOptions invalid;
  invalid.directory = "invalid";
  ObjectStore objects(invalid);
  ResponsesJobs jobs(objects, {}, [](auto&) {
    throw std::runtime_error("unexpected execution");
  });
  uint64_t ticket = 0;
  Q4T_CHECK(Ok(jobs.Reserve("client:alpha", "resp_pending", &ticket)));
  jobs.Close(ticket, false);
  Q4T_CHECK(Wait([&] { return jobs.Health().foreground_errors >= 3; }));
  std::this_thread::sleep_for(300ms);
  Q4T_CHECK(jobs.Health().foreground_errors == 3 &&
            jobs.Health().foreground_pending == 1 && !jobs.Health().failed);
  ResponsesExecution stateless(objects, &jobs, true);
  Q4T_CHECK(PrepareForeground(stateless, "alpha", false));
  std::string wire;
  Q4T_CHECK(Ok(stateless.Begin(&wire)) && Ok(stateless.CloseUnfinished()));
  jobs.RequestStop();
  jobs.Join();
  Q4T_CHECK(jobs.Health().foreground_pending == 1);
  return true;
}

Q4T_TEST(responses_foreground_missing_coordinator_preserves_stateless_service) {
  Fixture f;
  ObjectStore objects(f.options);
  ResponsesExecution stored(objects, nullptr, true),
      stateless(objects, nullptr, true);
  Q4T_CHECK(PrepareForeground(stored) &&
            PrepareForeground(stateless, "alpha", false));
  std::string wire;
  Q4T_CHECK(stored.Begin(&wire).code == "response_reconciliation_unavailable");
  Q4T_CHECK(Ok(stateless.Begin(&wire)) && Ok(stateless.CloseUnfinished()));
  Q4T_CHECK(objects.Health().workers_started == 0);
  return true;
}

Q4T_TEST(
    responses_application_owner_partial_thread_failure_recovers_real_task) {
  Fixture f;
  ObjectStore objects(f.options);
  ApplicationOwner<ResponsesJobs> owner({1ms, 20ms});
  ResponsesJobsOptions options;
  options.workers = 2;
  const auto threads = [] {
    return std::distance(std::filesystem::directory_iterator("/proc/self/task"),
                         std::filesystem::directory_iterator());
  };
  const auto before = threads();
  std::atomic<int> calls{0};
  const auto factory = [&] {
    return std::make_unique<ResponsesJobs>(objects, options, [&](auto& job) {
      ++calls;
      Complete(job);
    });
  };
  FailApplicationThreadForTesting(ApplicationThreadKind::kResponseWorker, 1);
  ScopeExit reset([] { application_failed_thread.store(-1); });
  Q4T_CHECK(!owner.TryStart(factory) && !owner &&
            owner.StartupHealth().failures == 1 &&
            std::string(owner.StartupHealth().last_failure) ==
                "thread_or_system");
  Q4T_CHECK(Wait([&] { return threads() == before; }));
  Q4T_CHECK(Wait([&] {
    owner.TryStart(factory);
    return bool(owner);
  }));
  const auto submitted = f.Submit(*owner, objects);
  Q4T_CHECK(Ok(submitted) && Wait([&] { return owner->Health().owned == 0; }));
  const auto response = ResponsesState(objects).Retrieve(
      submitted.value.GetString("id"), Access());
  Q4T_CHECK(Ok(response) && response.value.GetString("status") == "completed" &&
            calls == 1 && owner.StartupHealth().recoveries == 1 &&
            f.clients.Usage(0).data_active == 0);
  auto* original = owner.Get();
  owner.RequestStop();
  owner.Join();
  Q4T_CHECK(!owner.TryStart(factory) && owner.Get() == original &&
            owner->Health().stopping && calls == 1);
  return true;
}

Q4T_TEST(responses_application_scanner_retry_uses_same_owner_and_original_id) {
  Fixture f;
  ObjectStore objects(f.options);
  const auto queued = Interrupted(objects);
  const auto started = Interrupted(objects, true);
  std::atomic<int> calls{0};
  ResponsesJobs owner(objects, {}, [&](auto& job) {
    ++calls;
    Complete(job);
  });
  ApplicationStartup scanner({1ms, 20ms});
  const auto start = [&] { owner.StartRecovery(f.clients, Config()); };
  FailApplicationThreadForTesting(ApplicationThreadKind::kResponseScanner);
  ScopeExit reset([] { application_failed_thread.store(-1); });
  Q4T_CHECK(!scanner.Try(start) && !owner.Health().recovery_configured &&
            !owner.Health().stopping && scanner.Health().failures == 1);
  // The existing executor remains usable while only its scanner backs off.
  const auto online = f.Submit(owner, objects);
  Q4T_CHECK(Ok(online) && Wait([&] { return owner.Health().owned == 0; }) &&
            calls == 1);
  Q4T_CHECK(Wait([&] {
    scanner.Try(start);
    return scanner.Health().ready;
  }));
  Q4T_CHECK(Wait([&] {
    const auto health = owner.Health();
    return health.recovered == 1 && health.interrupted == 1 &&
           health.owned == 0;
  }));
  Q4T_CHECK(ResponsesState(objects)
                    .Retrieve(queued, Access())
                    .value.GetString("status") == "completed" &&
            ResponsesState(objects)
                    .Retrieve(started, Access())
                    .value.GetString("status") == "failed" &&
            calls == 2 && scanner.Health().recoveries == 1 &&
            !scanner.Try(start));
  owner.RequestStop();
  owner.Join();
  return true;
}
