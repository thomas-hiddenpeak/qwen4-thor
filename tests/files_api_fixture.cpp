// Real HTTP/storage fixture. No model, generated answers, or service claims.
#include <arpa/inet.h>
#include <poll.h>
#include <sys/socket.h>
#include <unistd.h>

#include <array>
#include <atomic>
#include <filesystem>
#include <fstream>
#include <thread>

#include "q4t/server/client_request.h"
#include "q4t/server/application_routes.h"
#include "q4t/server/files_http.h"
#include "q4t/server/batch_api.h"
#include "q4t/server/http_response.h"
#include "q4t/server/lifetime.h"
#include "q4t/server/output_schema.h"

namespace {
using namespace q4t::server;
using Clock = std::chrono::steady_clock;
using namespace std::chrono_literals;
namespace fs = std::filesystem;

std::string Read(const fs::path& path) {
  std::ifstream input(path);
  if (!input) throw std::runtime_error("fixture configuration missing");
  return {std::istreambuf_iterator<char>(input), {}};
}
}  // namespace

int FilesApiFixture(int argc, char** argv) {
  if (argc != 3 || (std::string_view(argv[1]) != "--files-api-fixture" &&
                    std::string_view(argv[1]) != "--batch-api-fixture"))
    return -1;
  const bool batch_fixture = std::string_view(argv[1]) == "--batch-api-fixture";
  try {
    const auto directory = fs::absolute(argv[2]);
    GatewayClientPolicy policy;
    if (!ParseGatewayClientPolicy(Read(directory / "policy.json"), &policy))
      return 1;
    const auto token = policy.gateway_token;
    ClientAdmission clients;
    if (!clients.Configure(std::move(policy))) return 1;
    ObjectStoreOptions options;
    options.directory = (directory / "objects").string();
    options.max_client_bytes = 256ULL << 20;
    options.max_total_bytes = 512ULL << 20;
    ObjectStore objects(options);
    std::unique_ptr<BatchJobs> batches;
    if (batch_fixture) {
      ChatPlanConfig config;
      config.model = "batch-cpu";
      config.structured = true;
      BatchJobsOptions jobs;
      jobs.turn_timeout = 30s;
      jobs.scan_interval = 100ms;
      batches = std::make_unique<BatchJobs>(
          objects, clients, config, jobs,
          [&](const auto&, const auto& row, const auto&, const auto& client,
              auto& permit, const auto& control, auto rate) {
            return ExecuteBatchEndpoint(
                control->id(), client, permit, control,
                [&] {
                  const auto& charge =
                      detail::HttpResponseContext::Current(-1)->BatchRate();
                  if (!charge || charge->Reserve(clients, client, 4, 16) !=
                                     ClientRateResult::kAllowed)
                    throw std::runtime_error("CPU fixture rate ownership");
                  charge->Start();
                  const auto* metadata = row.body.Find("metadata");
                  if (metadata && metadata->GetString("fixture") == "hold") {
                    while (!control->Cancelled())
                      std::this_thread::sleep_for(1ms);
                    detail::SendError(-1, 499, "CPU fixture cancelled", {},
                                      "batch_cancelled");
                    return;
                  }
                  // Explicit CPU generation fixture; native HTTP/task/storage
                  // are real. Actual model quality is accepted separately.
                  charge->Completion(2);
                  detail::SendSimple(
                      -1, 200, "OK",
                      R"({"object":"response","output":[{"type":"function_call","call_id":"call_cpu","name":"lookup","arguments":"{\"key\":42}"}],"usage":{"input_tokens":4,"output_tokens":2,"total_tokens":6,"input_tokens_details":{"cached_tokens":0},"output_tokens_details":{"reasoning_tokens":0}}})",
                      "application/json");
                },
                std::move(rate));
          },
          [](const auto&, const auto&, const auto&, const auto&, const auto&,
             BatchInputBudget* output) {
            *output = {4, true};  // The explicit CPU fixture's input count.
            return ObjectReply{};
          },
          [](const auto&, const auto&, const auto&, const auto&, const auto&,
             BatchRateBudget* output) {
            *output = {4, 16};  // Same count/output cap used by the fixture.
            return ObjectReply{};
          });
    }
    std::array<int, 2> listeners{-1, -1};
    ScopeExit close_listener([&] {
      for (int fd : listeners)
        if (fd >= 0) close(fd);
    });
    std::array<int, 2> ports{};
    for (size_t i = 0; i < listeners.size(); ++i) {
      listeners[i] = socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0);
      if (listeners[i] < 0) return 1;
      sockaddr_in address{};
      address.sin_family = AF_INET;
      address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
      if (bind(listeners[i], reinterpret_cast<sockaddr*>(&address),
               sizeof(address)) ||
          listen(listeners[i], 8))
        return 1;
      socklen_t size = sizeof(address);
      if (getsockname(listeners[i], reinterpret_cast<sockaddr*>(&address),
                      &size))
        return 1;
      ports[i] = ntohs(address.sin_port);
    }
    std::ofstream(directory / "ready.json")
        << "{\"port\":" << ports[0] << ",\"operations\":" << ports[1]
        << ",\"pid\":" << getpid() << "}\n";
    HttpBodyBudget budget(kAggregateChatBodyBytes);
    HttpBodyBudget cancel_budget(kCancelBodyBudgetBytes);
    std::atomic<int> active{0};
    std::atomic<bool> stopping{false};
    std::vector<std::thread> workers;
    ScopeExit stop_workers([&] {
      stopping.store(true);
      if (batches) batches->RequestStop();
      for (auto& worker : workers) worker.join();
      if (batches) batches->Join();
    });
    const auto end = Clock::now() + 120s;
    for (int count = 0; count < 256 && Clock::now() < end;) {
      pollfd pending[] = {{STDIN_FILENO, POLLIN, 0},
                          {listeners[0], POLLIN, 0},
                          {listeners[1], POLLIN, 0}};
      const int ready = poll(pending, 3, 100);
      if (ready < 0 && errno == EINTR) continue;
      if (ready < 0) return 1;
      if (pending[0].revents) return 0;  // Parent closes private stdin to stop.
      for (size_t i = 0; i < listeners.size(); ++i) {
        if (!(pending[i + 1].revents & POLLIN)) continue;
        const int fd = accept4(listeners[i], nullptr, nullptr, SOCK_CLOEXEC);
        if (fd < 0) return 1;
        ++count;
        if (active.load() >= 8) {
          close(fd);
          continue;
        }
        active.fetch_add(1);
        workers.emplace_back([&, fd, operations = i == 1] {
          ScopeExit close_peer([&] {
            DrainHttpClose(fd);
            close(fd);
            active.fetch_sub(1);
          });
          detail::HttpResponseContext context(fd);
          try {
            ClientLease client_lease;
            ClientIdentity identity;
            HttpBodyLease body_lease(budget);
            HttpRequestHeaders headers;
            HttpBodyStream stream;
            std::unique_ptr<FileHttpRequest> file;
            ObjectReply preparation;
            std::string method, target, body;
            int failure = 400;
            const auto admit = [&] {
              // Only direct SDK fixture calls use this synthetic mapping.
              // Native ingress supplies the real authenticated private pair.
              if (headers.client_id.empty() && headers.gateway_token.empty()) {
                if (headers.authorization == "Bearer cpu-client-a")
                  headers.client_id = "alpha";
                else if (headers.authorization == "Bearer cpu-client-b")
                  headers.client_id = "beta";
                if (!headers.client_id.empty()) headers.gateway_token = token;
              }
              if (operations && !IsFileMetadata(method, target) &&
                  !IsUploadCancellation(method, target) &&
                  !(batch_fixture && (IsBatchObservation(method, target) ||
                                      IsBatchCancellation(method, target)))) {
                failure = 404;
                return false;
              }
              if (!AdmitClientRequest(method, target, &headers, clients,
                                      &client_lease, &identity, &failure))
                return false;
              if (IsCancellationRequest(method, target))
                body_lease.SelectBudget(cancel_budget);
              if (batch_fixture && IsBatchTarget(target)) return true;
              BinaryAccess access{"client:" + std::string(identity.Id()),
                                  Clock::now() + 30s,
                                  [&] { return stopping.load(); }};
              file = std::make_unique<FileHttpRequest>(objects, access);
              preparation =
                  file->Prepare(method, target, headers.content_type, &stream);
              if (!preparation.ok()) {
                failure = preparation.http_status;
                return false;
              }
              headers.body_limit =
                  std::min<uint64_t>(headers.body_limit, file->WireLimit());
              return true;
            };
            if (!ReadHttpRequest(fd, &method, &target, &body, 5000, &body_lease,
                                 &failure, &headers, admit, {}, nullptr, {},
                                 &stream)) {
              const auto* error = !preparation.ok() ? &preparation
                                  : file            ? file->UploadFailure()
                                                    : nullptr;
              if (error)
                detail::SendError(
                    fd, error->http_status, error->ClientMessage(), {},
                    error->ClientCode(), error->value.GetString("param"),
                    error->value.GetString("allow"));
              else
                detail::SendError(fd, failure, "Fixture transport rejected", {},
                                  headers.error_code);
              return;
            }
            if (file) {
              file->Serve(fd);
            } else {
              const auto reply = HandleBatchRequest(
                  objects, batches.get(), method, target, body, identity,
                  {"client:" + std::string(identity.Id()), Clock::now() + 30s,
                   [&] { return stopping.load(); }});
              if (reply.ok())
                detail::SendSimple(fd, 200, "OK", OutputJson(reply.value),
                                   "application/json");
              else
                detail::SendError(fd, reply.http_status, reply.ClientMessage(),
                                  {}, reply.ClientCode(),
                                  reply.value.GetString("param"),
                                  reply.value.GetString("allow"));
            }
          } catch (const std::exception&) {
            detail::SendExceptionResponse(fd, *context.State());
          }
        });
      }
    }
    return 2;
  } catch (const std::exception& error) {
    std::fprintf(stderr, "Files fixture: %s\n", error.what());
    return 1;
  }
}
