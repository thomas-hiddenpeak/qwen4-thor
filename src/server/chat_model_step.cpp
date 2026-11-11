#include "chat_model_step.h"

#include "object_protocol.h"
#include "q4t/server/lifetime.h"

namespace q4t::server {
ObjectReply ExecuteInternalModelStep(
    int fd, InternalModelStep* step,
    const std::shared_ptr<RequestControl> control,
    const InternalModelExecutor& execute) {
  if (!step || !control || !step->before_model || !execute)
    return object_detail::Error(500, "file_preparation_state_invalid",
                                "Invalid native model step ownership");
  auto* parent = detail::HttpResponseContext::Current(fd);
  if (!parent || !parent->Client().Allowed())
    return object_detail::Error(503, "object_scope_unavailable",
                                "Authenticated model owner unavailable");
  const auto progress = control->Progress();
  ScopeExit release([&] {
    control->BindProgress(progress);
    step->accounting.CloseStep();
  });
  detail::HttpResponseContext context(
      detail::HttpResponseContext::Detached{fd});
  ScopeExit usage([&] {
    context.RefreshUsage();
    parent->Usage().Merge(context.Usage().Total());
    parent->RefreshUsage();
  });
  context.SetClient(parent->Client(), nullptr, parent->RateDomain());
  context.SetTraceId(parent->TraceId());
  context.SetErrorObserver(
      [](void* owner, int status, std::string_view message,
         std::string_view code) noexcept {
        try {
          auto* work = static_cast<InternalModelStep*>(owner);
          if (work->error.ok())
            work->error = object_detail::Error(status, std::string(code),
                                               std::string(message));
        } catch (...) {
          // The generic incomplete-step result below still fails closed.
        }
      },
      step);
  // Keep our strong reference until the exit guard has restored progress.
  execute(context.State(), control);
  if (!step->error.ok()) return step->error;
  if (!step->completed)
    return object_detail::Error(503, "file_preparation_incomplete",
                                "Native model preparation did not complete");
  return {};
}
}  // namespace q4t::server
