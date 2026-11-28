// Exercise the real admission and diagnostic paths without loading a model.
#include <unistd.h>

#include <chrono>
#include <cstdio>
#include <memory>
#include <string>

#include "q4t/server/chat_server.h"
#include "q4t/server/embeddings_execution.h"
#include "q4t/server/lifetime.h"
#include "q4t/server/request_diagnostics.h"
#include "q4t/test.h"

namespace q4t::server {
struct ServeContractAccess {
  static ObjectReply RefusedEmbedding(
      ChatServer& server, const std::shared_ptr<RequestControl>& control,
      bool device_failure, EmbeddingStepResult* output) {
    // These are terminal admission conditions; neither path enters CUDA.
    server.seq_stopping_ = !device_failure;
    server.gpu_healthy_.store(!device_failure);
    ClientLease lease;
    return server.RunVectorEmbedding({1}, {}, lease, control, output);
  }
};
}  // namespace q4t::server

namespace {
using namespace q4t::server;

bool CheckAdmission(bool device_failure) {
  const std::string id =
      device_failure ? "tool_run_device_fault" : "tool_run_cancel";
  auto control = std::make_shared<RequestControl>(
      id, "", std::chrono::steady_clock::now() + std::chrono::seconds(2));
  if (!device_failure) control->Cancel(RequestControl::State::kExplicit);
  FILE* log = std::tmpfile();
  Q4T_CHECK(log);
  ScopeExit close_file([&] { std::fclose(log); });
  const int original = dup(STDERR_FILENO);
  Q4T_CHECK(original >= 0);
  ScopeExit close_original([&] { close(original); });
  EmbeddingStepResult output;
  ObjectReply reply;
  {
    std::fflush(stderr);
    Q4T_CHECK(dup2(fileno(log), STDERR_FILENO) >= 0);
    ScopeExit restore([&] {
      std::fflush(stderr);
      dup2(original, STDERR_FILENO);
    });
    ChatServer server;
    reply = ServeContractAccess::RefusedEmbedding(server, control,
                                                  device_failure, &output);
  }
  Q4T_CHECK(reply.http_status == (device_failure ? 503 : 409));
  Q4T_CHECK(reply.ClientCode() ==
            (device_failure ? "gpu_unhealthy" : "explicit_cancel"));
  Q4T_CHECK(control->Cancelled() == !device_failure);
  Q4T_CHECK(output.committed_input == 0);
  std::rewind(log);
  std::string records;
  char line[1024];
  while (std::fgets(line, sizeof(line), log)) records += line;
  const auto begin = records.find(" event=end ");
  Q4T_CHECK(begin != records.npos &&
            records.find(" event=end ", begin + 1) == records.npos);
  const auto end = records.substr(begin, records.find('\n', begin) - begin);
  Q4T_CHECK(end.find("trace_id=" + id + " ") != end.npos);
  Q4T_CHECK(end.find("released=0 ") != end.npos);
  Q4T_CHECK(end.find(device_failure
                         ? "outcome=error reason=gpu_unhealthy "
                         : "outcome=aborted reason=explicit_cancel ") !=
            end.npos);
  return true;
}

Q4T_TEST(vector_ingestion_diagnostics_preserve_explicit_cancel_at_admission) {
  return CheckAdmission(false);
}
Q4T_TEST(vector_ingestion_diagnostics_preserve_device_failure_at_admission) {
  return CheckAdmission(true);
}
}  // namespace
