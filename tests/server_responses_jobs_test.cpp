#include "q4t/server/responses_jobs.h"

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
                        bool expired = false) {
  ResponsesExecution run(objects);
  if (!run.Prepare(
              Parse(
                  R"({"input":"private original input","background":true,"reasoning":{"effort":"none"}})"),
              Config(), {scope, Clock::now() + 5s, {}}, std::time(nullptr),
              true)
           .ok())
    throw std::runtime_error("recovery preparation");
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
  Q4T_CHECK(Wait([&] { return jobs.Health().recovery_scanned; }));
  Q4T_CHECK(executed == 1 && jobs.Health().owned == 1 &&
            jobs.Health().recovery_pending == 1);
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
