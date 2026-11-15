#include "q4t/server/mcp_transport.h"

#include <signal.h>
#include <sys/resource.h>

#include <algorithm>
#include <cstring>
#include <mutex>

#include "file_fetch_internal.h"
#include "mcp_transport_internal.h"
#include "q4t/server/mcp_content.h"
#include "q4t/server/http_response.h"
#include "q4t/server/input_snapshot_bindings.h"
#include "q4t/server/lifetime.h"
#include "object_protocol.h"

namespace q4t::server {
namespace {
using namespace object_detail;
using Clock = std::chrono::steady_clock;
constexpr size_t kInputBytes = 1U << 20;
constexpr size_t kOutputBytes = 5U << 20;
ObjectReply Failed(GrammarFailure failure) {
  if (failure == GrammarFailure::kCancelled)
    return Error(499, "request_cancelled", "MCP request cancelled");
  if (failure == GrammarFailure::kTimeout)
    return Error(408, "request_deadline_exceeded", "MCP request timed out");
  if (failure == GrammarFailure::kUnavailable)
    return Error(503, "mcp_worker_capacity", "MCP worker capacity occupied");
  return Error(503, "mcp_worker_failed", "MCP worker unavailable");
}
bool ValidOperation(std::string_view method, const Json& params) {
  if (!params.IsObject()) return false;
  if (method == "tools/list") return params.object.empty();
  if (method == "resources/read")
    return params.object.size() == 1 && McpResourceUri(params.GetString("uri"));
  if (method != "tools/call" || params.object.size() != 2) return false;
  const auto* name = params.Find("name");
  const auto* arguments = params.Find("arguments");
  return name && name->IsString() && !name->str.empty() &&
         name->str.size() <= 256 && arguments && arguments->IsObject();
}
bool WriteWorker(const Json& value) {
  auto bytes = OutputJson(value);
  Frame frame{kVersion, 1, bytes.size()};
  return bytes.size() <= kOutputBytes &&
         detail::WorkerTransfer(&frame, sizeof(frame), true) &&
         detail::WorkerTransfer(bytes.data(), bytes.size(), true);
}
struct Owner {
  std::mutex mutex;
  std::unique_ptr<detail::GrammarProcess> worker;
  uint64_t address_space_bytes = 0;
};
Owner& SessionOwner() {
  // Shutdown must not race a request still draining. The exec child's parent
  // death signal remains the last resort; normal idle collection is below.
  static auto* owner = new Owner;
  return *owner;
}
ObjectReply BindSession(const BinaryAccess& access, const McpEndpoint& endpoint,
                        const FileFetchOptions& deployment,
                        McpSession* session) {
  if (!session) return {};
  if (session->idle_ms < 1 || session->idle_ms > 300000 ||
      session->lifetime_ms < 1 || session->lifetime_ms > 3600000)
    return Error(500, "mcp_session_invalid", "Invalid MCP session limits");
  auto headers = Object();
  for (const auto& [key, value] : endpoint.headers)
    Set(&headers, key, String(value));
  std::string binding, scope;
  if (!InputValueIdentity(String(access.scope), &scope) ||
      !InputValueIdentity(
          Object({{"url", String(endpoint.url)},
                  {"headers", std::move(headers)},
                  {"authorization", String(endpoint.authorization)},
                  {"policy", file_detail::OptionsJson(deployment)}}),
          &binding))
    return Error(503, "mcp_session_invalid",
                 "MCP session identity unavailable");
  if (session->state.IsNull()) {
    if (!session->allow_new)
      return Error(409, "mcp_session_lost", "Original MCP session unavailable");
    const auto id = detail::NewRequestId("mcps_");
    if (id.empty())
      return Error(503, "mcp_entropy_unavailable", "MCP ID unavailable");
    session->state = Object({{"id", String(id)},
                             {"scope", String(scope)},
                             {"binding", String(binding)}});
  }
  if (!session->state.IsObject() ||
      session->state.GetString("scope") != scope ||
      session->state.GetString("binding") != binding)
    return Error(403, "mcp_session_binding_changed",
                 "MCP session does not match current authority");
  if (OutputJson(session->state).size() > 4096)
    return Error(413, "mcp_session_capacity", "MCP session state too large");
  return {};
}
}  // namespace

void PollMcpSessions() {
  auto& owner = SessionOwner();
  const std::unique_lock lock(owner.mutex, std::try_to_lock);
  if (lock.owns_lock() && owner.worker && owner.worker->Exited())
    owner.worker.reset();
}

bool CloseMcpSessions(bool release_remote) {
  auto& owner = SessionOwner();
  const std::unique_lock lock(owner.mutex, std::try_to_lock);
  if (!lock.owns_lock()) return false;
  if (!owner.worker) return true;
  // A bounded best-effort DELETE/stream close. A remote cleanup failure never
  // changes a known invocation result and cannot stop the healthy model.
  const auto deadline = Clock::now() + std::chrono::milliseconds(250);
  auto bytes =
      OutputJson(Object({{"type", String("close")},
                         {"release_remote", Boolean(release_remote)}}));
  Frame frame{kVersion, 1, bytes.size()};
  if (owner.worker->Transfer(&frame, sizeof(frame), true, deadline) &&
      owner.worker->Transfer(bytes.data(), bytes.size(), true, deadline) &&
      owner.worker->Transfer(&frame, sizeof(frame), false, deadline) &&
      frame.version == kVersion && frame.sequence == 1 &&
      frame.bytes <= kOutputBytes) {
    bytes.assign(frame.bytes, '\0');
    owner.worker->Transfer(bytes.data(), bytes.size(), false, deadline);
  }
  owner.worker.reset();
  return true;
}

ObjectReply InvokeMcp(const BinaryAccess& access, const McpEndpoint& endpoint,
                      const FileFetchOptions& deployment,
                      std::string_view method, const Json& params,
                      const std::function<ObjectReply()>& before_call,
                      McpTransportReceipt* receipt, GrammarControl control,
                      McpSession* session) {
  if (!receipt) return Error(500, "mcp_state_invalid", "Missing MCP output");
  *receipt = {};
  if (access.scope.empty())
    return Error(503, "object_scope_unavailable",
                 "Authenticated scope required");
  auto checked = mcp_detail::CheckEndpoint(endpoint, deployment);
  if (!checked.ok()) return checked;
  if (!ValidOperation(method, params) ||
      (method != "tools/list" && !before_call))
    return Error(400, "mcp_operation_invalid", "Invalid MCP operation");
  checked = BindSession(access, endpoint, deployment, session);
  if (!checked.ok()) return checked;
  control.domain = IsolatedWorkerDomain::kMcp;
  control.address_space_bytes =
      std::min<uint64_t>(control.address_space_bytes, 512U << 20);
  control.compile_deadline = std::min(
      {access.deadline, control.compile_deadline,
       Clock::now() + std::chrono::milliseconds(deployment.timeout_ms)});
  const auto prior_cancelled = control.cancelled;
  control.cancelled = [&] {
    return (prior_cancelled && prior_cancelled()) ||
           (access.cancelled && access.cancelled());
  };
  if (control.cancelled()) return Failed(GrammarFailure::kCancelled);
  if (Clock::now() >= control.compile_deadline)
    return Failed(GrammarFailure::kTimeout);
  auto& owner = SessionOwner();
  const std::unique_lock lock(owner.mutex, std::try_to_lock);
  if (!lock.owns_lock()) return Failed(GrammarFailure::kUnavailable);
  if (owner.worker && owner.worker->Exited()) owner.worker.reset();
  if (owner.worker && control.address_space_bytes < owner.address_space_bytes)
    return Failed(GrammarFailure::kUnavailable);
  bool keep = owner.worker != nullptr;
  ScopeExit close([&] {
    if (!keep) owner.worker.reset();
  });
  auto headers = Object();
  for (const auto& [key, value] : endpoint.headers)
    Set(&headers, key, String(value));
  const auto timeout = std::min<int64_t>(
      deployment.timeout_ms, std::chrono::ceil<std::chrono::milliseconds>(
                                 control.compile_deadline - Clock::now())
                                 .count());
  if (timeout < 1) return Failed(GrammarFailure::kTimeout);
  auto request =
      Object({{"url", String(endpoint.url)},
              {"headers", std::move(headers)},
              {"authorization", String(endpoint.authorization)},
              {"options", file_detail::OptionsJson(deployment)},
              {"method", String(std::string(method))},
              {"params", params},
              {"timeout_ms", Number(timeout)},
              {"session", session ? session->state : Json()},
              {"allow_new", Boolean(session && session->allow_new)},
              {"idle_ms", Number(session ? session->idle_ms : 0)},
              {"lifetime_ms", Number(session ? session->lifetime_ms : 0)}});
  auto bytes = OutputJson(request);
  if (bytes.size() > kInputBytes)
    return Error(413, "mcp_request_capacity", "MCP request budget exceeded");
  if (!owner.worker) {
    auto lifetime = control;
    if (session) {
      // A persistent owner retains no request callbacks or cancellation state.
      lifetime.cancelled = {};
      lifetime.worker_started = {};
      lifetime.worker_ready = {};
      lifetime.compile_started = {};
      lifetime.worker_finished = {};
    }
    GrammarFailure failure;
    if (!detail::GrammarProcess::Start("--mcp-worker", lifetime, &owner.worker,
                                       &failure))
      return Failed(failure);
    owner.address_space_bytes = control.address_space_bytes;
    if (session && control.worker_started) control.worker_started();
  }
  auto& worker = owner.worker;
  // From the first frame onwards an interrupted exchange must discard the
  // IPC stream; preflight rejections above leave unrelated sessions intact.
  keep = false;
  auto transfer = [&](void* data, size_t size, bool write) {
    return worker->Transfer(data, size, write, control.compile_deadline,
                            control.cancelled);
  };
  auto failed = [&] {
    receipt->outcome_unknown = receipt->call_permitted;
    return Failed(worker->Failure());
  };
  Frame frame{kVersion, 1, bytes.size()};
  if (!transfer(&frame, sizeof(frame), true) ||
      !transfer(bytes.data(), bytes.size(), true))
    return failed();
  bool session_confirmed = false;
  for (int messages = 0; messages < 3; ++messages) {
    if (!transfer(&frame, sizeof(frame), false) || frame.version != kVersion ||
        frame.sequence != 1 || !frame.bytes || frame.bytes > kOutputBytes)
      return failed();
    bytes.assign(frame.bytes, '\0');
    Json wire;
    if (!transfer(bytes.data(), bytes.size(), false) || !Parse(bytes, &wire))
      return failed();
    if (wire.GetString("type") == "session") {
      if (!session || messages || !wire.Find("session")) return failed();
      session->state = *wire.Find("session");
      auto confirmed = BindSession(access, endpoint, deployment, session);
      if (confirmed.ok() && session->checkpoint)
        confirmed = session->checkpoint(session->state);
      if (confirmed.commit_unknown) {
        confirmed.code = "mcp_state_commit_unknown";
        confirmed.message = "MCP session publication receipt unavailable";
      }
      if (!confirmed.ok() || confirmed.commit_unknown) return confirmed;
      session_confirmed = true;
      uint8_t ack = 1;
      if (!transfer(&ack, sizeof(ack), true)) return failed();
      continue;
    }
    if (wire.GetString("type") == "before_call") {
      if (receipt->call_permitted || method == "tools/list" ||
          (session && !session_confirmed))
        return failed();
      auto permitted = before_call();
      if (permitted.commit_unknown) {
        permitted.code = "mcp_state_commit_unknown";
        permitted.message = "MCP dispatch permission receipt unavailable";
        return permitted;
      }
      if (!permitted.ok()) return permitted;
      receipt->dispatch_id = permitted.value.GetString("dispatch_id");
      if (control.cancelled()) return Failed(GrammarFailure::kCancelled);
      if (Clock::now() >= control.compile_deadline)
        return Failed(GrammarFailure::kTimeout);
      receipt->call_permitted = receipt->outcome_unknown = true;
      uint8_t ack = 1;
      if (!transfer(&ack, sizeof(ack), true)) return failed();
      continue;
    }
    if (wire.GetString("type") != "result") return failed();
    const auto* value = wire.Find("reply");
    const auto* result = wire.Find("result");
    const auto* error = wire.Find("error");
    const auto* saved_session = wire.Find("session");
    const auto sessions = wire.GetInt("sessions", -1);
    if (!value || !result || !error || !saved_session || sessions < 0 ||
        sessions > 16 ||
        wire.GetBool("call_permitted") != receipt->call_permitted)
      return failed();
    auto reply = DecodeReply(*value);
    if (session) {
      if (reply.ok() && !session_confirmed) return failed();
      session->state = *saved_session;
      const auto bound = BindSession(access, endpoint, deployment, session);
      if (!bound.ok()) return failed();
    }
    receipt->protocol = wire.GetString("protocol");
    receipt->legacy_sse = wire.GetBool("legacy_sse");
    receipt->outcome_unknown = wire.GetBool("outcome_unknown");
    receipt->result = *result;
    receipt->error = *error;
    keep = sessions > 0;
    return reply;
  }
  return failed();
}

int MaybeRunMcpWorker(int argc, char** argv) {
  if (argc < 2 || std::strcmp(argv[1], "--mcp-worker")) return -1;
  if (!detail::EnterGrammarWorker(argc, argv)) return 2;
  struct sigaction ignored {};
  ignored.sa_handler = SIG_IGN;
  sigemptyset(&ignored.sa_mask);
  const rlimit cpu{35, 35};
  if (sigaction(SIGPIPE, &ignored, nullptr) || setrlimit(RLIMIT_CPU, &cpu))
    return 2;
  try {
    mcp_detail::Sessions sessions;
    for (size_t operations = 0; operations < 65536; ++operations) {
      // Idle cleanup happens inside isolation; DNS/TLS/DELETE cannot block the
      // model loop. Each retained connection has an idle and absolute limit.
      while (operations) {
        const auto ready = detail::WaitWorkerInput(1000);
        if (ready < 0) return 0;
        sessions.Collect();
        if (ready) break;
        // Keep the fixed, empty owner ready until the next exchange/shutdown.
        // Exiting exactly at idle expiry would race a new parent's write.
      }
      Frame frame;
      if (!detail::WorkerTransfer(&frame, sizeof(frame), false) ||
          frame.version != kVersion || frame.sequence != 1 || !frame.bytes ||
          frame.bytes > kInputBytes)
        return 3;
      std::string bytes(frame.bytes, '\0');
      Json request;
      if (!detail::WorkerTransfer(bytes.data(), bytes.size(), false) ||
          !Parse(bytes, &request))
        return 3;
      if (request.GetString("type") == "close") {
        sessions.Clear(request.GetBool("release_remote"));
        WriteWorker(Object({{"type", String("closed")}}));
        return 0;
      }
      McpEndpoint endpoint;
      endpoint.url = request.GetString("url");
      endpoint.authorization = request.GetString("authorization");
      const auto* headers = request.Find("headers");
      const auto* options = request.Find("options");
      const auto* params = request.Find("params");
      const auto* session = request.Find("session");
      const auto timeout = request.GetInt("timeout_ms", -1);
      const auto method = request.GetString("method");
      FileFetchOptions deployment;
      if (!headers || !headers->IsObject() || !options || !params || !session ||
          !ParseFileFetchOptions(*options, &deployment).ok() || timeout < 1 ||
          timeout > 30000 || !ValidOperation(method, *params))
        return 3;
      for (const auto& [key, value] : headers->object) {
        if (!value.IsString()) return 3;
        endpoint.headers.emplace(key, value.str);
      }
      auto reply = mcp_detail::CheckEndpoint(endpoint, deployment);
      if (!reply.ok()) return 3;
      McpTransportReceipt receipt;
      auto next_session = *session;
      const auto permit = [] {
        uint8_t ack = 0;
        return WriteWorker(Object({{"type", String("before_call")}})) &&
               detail::WorkerTransfer(&ack, sizeof(ack), false) && ack == 1;
      };
      const auto checkpoint = [](const Json& value) {
        uint8_t ack = 0;
        return WriteWorker(
                   Object({{"type", String("session")}, {"session", value}})) &&
               detail::WorkerTransfer(&ack, sizeof(ack), false) && ack == 1;
      };
      try {
        const auto deadline = Clock::now() + std::chrono::milliseconds(timeout);
        reply = session->IsNull()
                    ? mcp_detail::Exchange(endpoint, deployment, method,
                                           *params, deadline, permit, &receipt)
                    : sessions.Exchange(endpoint, deployment, method, *params,
                                        deadline, &next_session,
                                        request.GetBool("allow_new"),
                                        request.GetInt("idle_ms"),
                                        request.GetInt("lifetime_ms"),
                                        checkpoint, permit, &receipt);
      } catch (const std::bad_alloc&) {
        reply = Error(503, "mcp_memory_capacity", "MCP worker memory limit");
      } catch (...) {
        reply =
            Error(503, "mcp_dependency_failed", "MCP dependency unavailable");
      }
      // Never expose URLs/headers/body bytes through shared HTTP diagnostics.
      if (reply.code.starts_with("file_")) {
        reply.code.replace(0, 5, "mcp_");
        reply.message = "MCP transport failed";
      }
      if (!WriteWorker(
              Object({{"type", String("result")},
                      {"reply", WireReply(reply)},
                      {"result", receipt.result},
                      {"error", receipt.error},
                      {"protocol", String(receipt.protocol)},
                      {"session", std::move(next_session)},
                      {"sessions", Number(sessions.Size())},
                      {"legacy_sse", Boolean(receipt.legacy_sse)},
                      {"call_permitted", Boolean(receipt.call_permitted)},
                      {"outcome_unknown", Boolean(receipt.outcome_unknown)}})))
        return 3;
      if (!sessions.Size()) return 0;
    }
    return 0;
  } catch (...) {
    return 4;
  }
}
}  // namespace q4t::server
