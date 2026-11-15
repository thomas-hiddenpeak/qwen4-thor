#include "q4t/server/mcp_transport.h"

#include <signal.h>
#include <sys/resource.h>

#include <algorithm>
#include <cstring>

#include "file_fetch_internal.h"
#include "mcp_transport_internal.h"
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
}  // namespace

ObjectReply InvokeMcp(const BinaryAccess& access, const McpEndpoint& endpoint,
                      const FileFetchOptions& deployment,
                      std::string_view method, const Json& params,
                      const std::function<ObjectReply()>& before_call,
                      McpTransportReceipt* receipt, GrammarControl control) {
  if (!receipt) return Error(500, "mcp_state_invalid", "Missing MCP output");
  *receipt = {};
  if (access.scope.empty())
    return Error(503, "object_scope_unavailable",
                 "Authenticated scope required");
  auto checked = mcp_detail::CheckEndpoint(endpoint, deployment);
  if (!checked.ok()) return checked;
  if (!ValidOperation(method, params) ||
      (method == "tools/call" && !before_call))
    return Error(400, "mcp_operation_invalid", "Invalid MCP operation");
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
  auto headers = Object();
  for (const auto& [key, value] : endpoint.headers)
    Set(&headers, key, String(value));
  auto request = Object({{"url", String(endpoint.url)},
                         {"headers", std::move(headers)},
                         {"authorization", String(endpoint.authorization)},
                         {"options", file_detail::OptionsJson(deployment)},
                         {"method", String(std::string(method))},
                         {"params", params},
                         {"timeout_ms", Number(deployment.timeout_ms)}});
  auto bytes = OutputJson(request);
  if (bytes.size() > kInputBytes)
    return Error(413, "mcp_request_capacity", "MCP request budget exceeded");
  std::unique_ptr<detail::GrammarProcess> worker;
  GrammarFailure failure;
  if (!detail::GrammarProcess::Start("--mcp-worker", control, &worker,
                                     &failure))
    return Failed(failure);
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
  for (int messages = 0; messages < 2; ++messages) {
    if (!transfer(&frame, sizeof(frame), false) || frame.version != kVersion ||
        frame.sequence != 1 || !frame.bytes || frame.bytes > kOutputBytes)
      return failed();
    bytes.assign(frame.bytes, '\0');
    Json wire;
    if (!transfer(bytes.data(), bytes.size(), false) || !Parse(bytes, &wire))
      return failed();
    if (wire.GetString("type") == "before_call") {
      if (messages || method != "tools/call") return failed();
      // The caller must durably bind permission before any external dispatch.
      // Failure/expiry here cannot be turned into an automatic retry.
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
    if (!value || !result || !error ||
        wire.GetBool("call_permitted") != receipt->call_permitted)
      return failed();
    auto reply = DecodeReply(*value);
    receipt->protocol = wire.GetString("protocol");
    receipt->legacy_sse = wire.GetBool("legacy_sse");
    receipt->outcome_unknown = wire.GetBool("outcome_unknown");
    receipt->result = *result;
    receipt->error = *error;
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
    McpEndpoint endpoint;
    endpoint.url = request.GetString("url");
    endpoint.authorization = request.GetString("authorization");
    const auto* headers = request.Find("headers");
    const auto* options = request.Find("options");
    const auto* params = request.Find("params");
    const auto timeout = request.GetInt("timeout_ms", -1);
    const auto method = request.GetString("method");
    FileFetchOptions deployment;
    if (!headers || !headers->IsObject() || !options || !params ||
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
    try {
      reply = mcp_detail::Exchange(
          endpoint, deployment, method, *params,
          Clock::now() + std::chrono::milliseconds(timeout),
          [] {
            uint8_t ack = 0;
            return WriteWorker(Object({{"type", String("before_call")}})) &&
                   detail::WorkerTransfer(&ack, sizeof(ack), false) && ack == 1;
          },
          &receipt);
    } catch (const std::bad_alloc&) {
      reply = Error(503, "mcp_memory_capacity", "MCP worker memory limit");
    } catch (...) {
      reply = Error(503, "mcp_dependency_failed", "MCP dependency unavailable");
    }
    // Shared HTTP code uses file-prefixed internal diagnostics. Public MCP
    // diagnostics expose only classifications, never URLs/headers/body bytes.
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
                    {"legacy_sse", Boolean(receipt.legacy_sse)},
                    {"call_permitted", Boolean(receipt.call_permitted)},
                    {"outcome_unknown", Boolean(receipt.outcome_unknown)}})))
      return 3;
    return 0;
  } catch (...) {
    return 4;
  }
}
}  // namespace q4t::server
