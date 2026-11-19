// Observe a native object executor without controlling its lifetime/recovery.
#pragma once

#include <utility>

#include "q4t/server/object_store.h"
#include "q4t/server/request_diagnostics.h"

namespace q4t::server {
template <typename Function>
ObjectReply RunDiagnosedObjectRequest(RequestDiagnostics& diagnostic,
                                      const RequestControl& control,
                                      Function&& execute,
                                      bool* execution_success = nullptr) {
  // An exception keeps this bounded fallback. Never finish/cancel a request,
  // expire its deadline or alter a model's health for diagnostic bookkeeping.
  diagnostic.Outcome("error", "handler_exception");
  if (execution_success) *execution_success = false;
  auto reply = std::forward<Function>(execute)();
  const std::string_view reason = control.Reason();
  if (reason != "not_cancelled") {
    diagnostic.Outcome(control.Failed() ? "error" : "aborted", reason);
  } else {
    if (execution_success) *execution_success = reply.ok();
    diagnostic.Outcome(reply.ok() ? "success" : "error",
                       reply.ok() ? "execution_complete" : reply.ClientCode());
  }
  // This labels executor completion. The protocol owner still arbitrates the
  // final event against cancellation and output backpressure independently.
  return reply;
}
}  // namespace q4t::server
