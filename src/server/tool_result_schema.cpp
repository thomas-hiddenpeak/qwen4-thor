#include "q4t/server/tool_result_schema.h"

#include <unistd.h>

#include <algorithm>
#include <cstring>

#include "object_protocol.h"
#include "q4t/server/request_json.h"
#include "q4t/server/schema_graph.h"
#include "schema_string.h"

namespace q4t::server {
namespace {
using namespace object_detail;
using Clock = std::chrono::steady_clock;
constexpr size_t kBytes = 8 * 1024 * 1024;
constexpr size_t kChecks = 4096;
ObjectReply Failure(GrammarFailure failure) {
  if (failure == GrammarFailure::kCancelled)
    return Error(499, "request_cancelled", "Tool result validation cancelled");
  if (failure == GrammarFailure::kTimeout)
    return Error(408, "request_deadline_exceeded",
                 "Tool result validation expired");
  return Error(503,
               failure == GrammarFailure::kResource
                   ? "tool_result_validation_capacity"
                   : "tool_result_validation_unavailable",
               "Tool result validator unavailable");
}
Json Result(int status, std::string code, size_t index,
            std::string param = {}) {
  return Object({{"status", Number(status)},
                 {"code", String(std::move(code))},
                 {"index", Number(index)},
                 {"param", String(std::move(param))}});
}
Json Check(const Json& checks) {
  for (size_t i = 0; i < checks.array.size(); ++i) {
    const auto& check = checks.array[i];
    const auto* schema = check.Find("schema");
    if (!schema) return Result(503, "tool_result_validation_unavailable", i);
    OutputSchemaGraph graph;
    std::string param;
    auto status = graph.Init(*schema, false, &param, "schema",
                             OutputSchemaPurpose::kToolResult);
    // Compile every pattern even if the result omits that property. A broken
    // declared result contract cannot become an accepted pending tool call.
    if (status) {
#ifdef Q4T_ENABLE_XGRAMMAR
      size_t work = 8 * 1024 * 1024;
#endif
      for (const auto* node : graph.Nodes()) {
        if (!node->Has("pattern")) continue;
#ifndef Q4T_ENABLE_XGRAMMAR
        return Result(503, "tool_result_validation_unavailable", i);
#else
        bool accepted = false;
        status =
            CheckSchemaString(Object({{"pattern", *node->Find("pattern")}}), "",
                              &work, &accepted);
        if (!status) break;
#endif
      }
    }
    const auto* output = check.Find("output");
    const bool definition = !status;
    if (status && output) {
      Json value;
      status = output->IsString()
                   ? io::ParseJson(output->str, &value, true, kSchemaJsonLimits)
                   : Status::Fail("expected JSON text");
      if (status) status = graph.Check(value, CheckSchemaString);
    }
    if (!status) {
      const bool resource =
          status.message().starts_with("schema_resource_exhausted:");
      const bool dependency =
          status.message().starts_with("schema_dependency_unavailable:");
      return Result(resource || dependency ? 503 : 400,
                    resource     ? "tool_result_validation_capacity"
                    : dependency ? "tool_result_validation_unavailable"
                    : definition ? "invalid_tool_output_schema"
                                 : "invalid_tool_output",
                    i, std::move(param));
    }
  }
  return Result(200, "", 0);
}
}  // namespace

ObjectReply ValidateToolResultSchemas(const io::Json& checks,
                                      const BinaryAccess& access,
                                      GrammarControl control) {
  if (!checks.IsArray() || checks.array.size() > kChecks)
    return Failure(GrammarFailure::kResource);
  if (checks.array.empty()) return {};
  if (access.cancelled && access.cancelled())
    return Failure(GrammarFailure::kCancelled);
  const auto bytes = OutputJson(checks);
  if (bytes.size() > kBytes) return Failure(GrammarFailure::kResource);
  control.domain = IsolatedWorkerDomain::kToolResults;
  control.address_space_bytes =
      std::min<uint64_t>(control.address_space_bytes, 512ULL * 1024 * 1024);
  control.compile_deadline =
      std::min({control.compile_deadline, access.deadline,
                Clock::now() + std::chrono::seconds(2)});
  const auto cancelled = control.cancelled;
  control.cancelled = [&] {
    return (cancelled && cancelled()) ||
           (access.cancelled && access.cancelled());
  };
  GrammarFailure failure;
  std::unique_ptr<detail::GrammarProcess> process;
  if (!detail::GrammarProcess::Start("--tool-result-worker", control, &process,
                                     &failure))
    return Failure(failure);
  uint64_t size = bytes.size();
  if (!process->Transfer(&size, sizeof(size), true, control.compile_deadline,
                         control.cancelled) ||
      !process->Transfer(const_cast<char*>(bytes.data()), bytes.size(), true,
                         control.compile_deadline, control.cancelled))
    return Failure(process->Failure());
  std::string wire;
  if (!process->CollectOutput(&wire, 8192, control.compile_deadline,
                              control.cancelled))
    return Failure(process->Failure());
  Json result;
  if (!io::ParseJson(wire, &result, true, kSchemaJsonLimits))
    return Failure(GrammarFailure::kWorker);
  const auto status = result.GetInt("status", -1);
  if (status == 200) return {};
  const auto index = result.GetInt("index", -1);
  if ((status != 400 && status != 503) || index < 0 ||
      static_cast<size_t>(index) >= checks.array.size())
    return Failure(GrammarFailure::kWorker);
  const auto& check = checks.array[index];
  auto path = check.GetString("param");
  const auto suffix = result.GetString("param");
  if (!check.Has("output") && suffix.starts_with("schema"))
    path += suffix.substr(6);
  auto reply = Error(status, result.GetString("code"),
                     status == 400 ? "Tool result contract was not satisfied"
                                   : "Tool result validator unavailable");
  reply.value = Object({{"param", String(std::move(path))}});
  return reply;
}

int MaybeRunToolResultWorker(int argc, char** argv) {
  if (argc < 2 || std::strcmp(argv[1], "--tool-result-worker")) return -1;
  if (!detail::EnterGrammarWorker(argc, argv)) return 2;
  try {
    uint64_t size = 0;
    if (!detail::WorkerTransfer(&size, sizeof(size), false) || !size ||
        size > kBytes)
      return 3;
    std::string bytes(size, '\0');
    if (!detail::WorkerTransfer(bytes.data(), bytes.size(), false)) return 3;
    Json checks;
    auto limits = kSchemaJsonLimits;
    limits.max_string_bytes = kBytes;
    limits.max_values = 512 * 1024;
    if (!io::ParseJson(bytes, &checks, true, limits) || !checks.IsArray() ||
        checks.array.size() > kChecks)
      return 3;
    auto result = OutputJson(Check(checks));
    return detail::WorkerTransfer(result.data(), result.size(), true) ? 0 : 3;
  } catch (...) {
    return 4;
  }
}
}  // namespace q4t::server
