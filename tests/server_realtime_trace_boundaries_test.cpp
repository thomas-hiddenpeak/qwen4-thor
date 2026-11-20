#include "q4t/server/realtime_trace.h"

#include "../src/server/object_protocol.h"
#include "q4t/server/realtime_conversation.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
}  // namespace

Q4T_TEST(realtime_trace_full_capacity_produces_bounded_inspectable_snapshot) {
  const auto settings = Object({{"workflow_name", String(std::string(256, 'w'))},
                                {"group_id", String(std::string(256, 'g'))},
                                {"metadata", String(std::string(60000, 'm'))}});
  RealtimeTraceConfiguration config;
  Q4T_CHECK(ParseRealtimeTraceConfiguration(&settings, &config).ok());
  RealtimeTraceCollector trace("sess_capacity", "conv_capacity", std::move(config),
                               {1795185600000, 0});
  Q4T_CHECK(trace.Valid());
  const auto details = Object({{"text_tokens", Number(1)}, {"audio_tokens", Number(2)},
                               {"image_tokens", Number(3)}, {"cached_tokens", Number(4)}});
  const auto usage = Object({{"input_tokens", Number(6)}, {"output_tokens", Number(6)},
                             {"total_tokens", Number(12)}, {"input_token_details", details},
                             {"output_token_details", details}});
  for (size_t i = 0; i < RealtimeTraceCollector::kMaximumSpans; ++i) {
    auto id = std::string(250, 'r') + std::to_string(i);
    Q4T_CHECK(trace.Begin("mcp_call", id, std::string(256, 'p'), {1795185600000, 1}));
    Q4T_CHECK(trace.LinkApproval(id, std::string(256, 'a')));
    Q4T_CHECK(trace.Phase("mcp_call", id, "dispatched", {1795185600000, 2}));
    Q4T_CHECK(trace.End("mcp_call", id, "completed", {1795185600000, 3}, &usage));
  }
  const auto snapshot = trace.Snapshot();
  Q4T_CHECK(snapshot.IsObject());
  Q4T_CHECK(snapshot.GetArray("spans")->array.size() == 256);
  size_t bytes = 0;
  Q4T_CHECK(MeasureRealtimeStorage(snapshot, RealtimeTraceCollector::kMaximumRecordBytes, &bytes));
  std::printf("maximum trace charged bytes: %zu\n", bytes);
  return true;
}

Q4T_TEST(realtime_trace_maximum_session_id_fits_application_object_identity) {
  const auto settings = String("auto");
  RealtimeTraceConfiguration config;
  Q4T_CHECK(ParseRealtimeTraceConfiguration(&settings, &config).ok());
  const auto session = std::string(128, 's');
  RealtimeTraceCollector trace(session, "conv_identity", std::move(config),
                               {1795185600000, 0});
  Q4T_CHECK(trace.Valid());
  const auto snapshot = trace.Snapshot();
  Q4T_CHECK(snapshot.IsObject());
  Q4T_CHECK(ValidApplicationId(snapshot.GetString("id")));
  Q4T_CHECK(snapshot.GetString("session_id") == session);
  return true;
}
