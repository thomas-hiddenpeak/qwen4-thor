#include "chat_server_internal.h"

#include <ctime>

#include "q4t/server/output_schema.h"
#include "q4t/server/responses_api.h"
#include "q4t/server/request_json.h"

namespace q4t::server {

bool ChatServer::TryHandleBackgroundResponse(int fd, const std::string& body) {
  io::Json request;
  const auto parsed = io::ParseJson(body, &request, true, kSchemaJsonLimits);
  // Foreground handling owns complete JSON/type diagnostics.
  if (!parsed || !request.GetBool("background")) return false;
  auto* http = detail::HttpResponseContext::Current(fd);
  if (!background_ || !objects_ || !http || !http->Client().Allowed()) {
    detail::SendError(fd, 503, "Background executor unavailable", {},
                      "background_unavailable");
    return true;
  }
  const auto client = http->Client();
  StateAccess access{client.Scoped() ? "client:" + std::string(client.Id())
                                     : "local:anonymous",
                     std::chrono::steady_clock::now() +
                         std::chrono::milliseconds(request_deadline_ms_),
                     [this, fd] {
                       return stop_requested_.load() ||
                              detail::ClientConnectionFailed(fd);
                     }};
  // The new owner takes the existing permit; there is no release/reacquire
  // gap. Subscriber disconnect and its HTTP deadline do not own generation.
  ServeBackgroundResponse(fd, *objects_, *background_, request,
                          PlanningConfig(fd), access);
  return true;
}

void ChatServer::RunBackgroundResponse(ResponsesJobs::Job& job) {
  detail::HttpResponseContext http(detail::HttpResponseContext::Detached{});
  http.SetClient(job.client, &job.permit);
  http.SetTraceId(job.run->Id());
  // Observe actual failures. Do not use the live SSE error renderer here:
  // cancellation is finalized by the owner after all generation cleanup.
  http.SetErrorObserver(&ResponsesExecution::ObserveError, job.run.get());
  HandleChatImpl(-1, "", http.State(), false, job.run.get(), job.control);
}

}  // namespace q4t::server
