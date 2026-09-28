#include "q4t/trace/router_trace.h"

#include <sstream>
#include <streambuf>

#include "q4t/test.h"

namespace {
using namespace q4t::trace;
RouterTraceConfig Config() {
  RouterTraceConfig c{2, 8, 2, 4, {}, {}, {}};
  c.binary_sha256[0] = 1;
  c.model_sha256[0] = 2;
  c.workload_sha256[0] = 3;
  return c;
}
RouteEvent Begin(uint64_t id = 1, uint64_t rows = 3) {
  RouteEvent e;
  e.kind = RouteRecord::kRequestBegin;
  e.request_id = id;
  e.prompt_rows = rows;
  e.prompt_sha256[0] = 4;
  return e;
}
RouteEvent Forward(uint64_t id, uint64_t position, uint32_t rows,
                   RouteStage stage = RouteStage::kPrefill) {
  RouteEvent e;
  e.kind = RouteRecord::kForwardBegin;
  e.forward_id = id;
  e.position = position;
  e.rows = rows;
  e.stage = stage;
  return e;
}
RouteEvent Layer(uint32_t layer, std::span<const uint16_t> ids) {
  RouteEvent e;
  e.kind = RouteRecord::kLayer;
  e.layer = layer;
  e.expert_ids = ids;
  return e;
}
RouteEvent Commit() {
  RouteEvent e;
  e.kind = RouteRecord::kForwardEnd;
  e.submission_ok = e.gpu_complete = e.committed = true;
  return e;
}
RouteEvent End(RequestOutcome outcome = RequestOutcome::kSuccess) {
  RouteEvent e;
  e.kind = RouteRecord::kRequestEnd;
  e.outcome = outcome;
  e.output_tokens = 2;
  return e;
}
const uint16_t kIds[] = {0, 7, 3, 2, 1, 4};
bool Prefill(RouterTraceValidator& v) {
  return v.Observe(Begin()).ok() && v.Observe(Forward(1, 0, 3)).ok() &&
         v.Observe(Layer(0, kIds)).ok() && v.Observe(Layer(1, kIds)).ok() &&
         v.Observe(Commit()).ok();
}
}  // namespace

Q4T_TEST(router_trace_chunked_prefill_and_decode) {
  RouterTraceValidator v(Config());
  Q4T_CHECK(v.Observe(Begin()).ok());
  const uint16_t ids[] = {1, 2, 3, 4};
  for (int i = 0; i < 3; ++i) {
    const int rows = i == 0 ? 2 : 1;
    Q4T_CHECK(
        v.Observe(Forward(i + 1, i == 0 ? 0 : i + 1, rows,
                          i == 2 ? RouteStage::kDecode : RouteStage::kPrefill))
            .ok());
    for (int layer = 0; layer < 2; ++layer)
      Q4T_CHECK(v.Observe(Layer(layer, {ids, size_t(rows * 2)})).ok());
    Q4T_CHECK(v.Observe(Commit()).ok());
  }
  Q4T_CHECK(v.Observe(End()).ok());
  Q4T_CHECK(v.Observe(RouteEvent{}).ok() && v.Finish().ok());
  const auto s = v.summary();
  Q4T_CHECK(s.successful_requests == 1 && s.committed_prefill_rows == 3);
  Q4T_CHECK(s.committed_decode_rows == 1 && s.output_tokens == 2);
  Q4T_CHECK(s.route_ids == 16 && s.committed_forwards == 3);
  return true;
}

Q4T_TEST(router_trace_commit_requires_all_evidence) {
  for (int failure = 0; failure < 3; ++failure) {
    RouterTraceValidator v(Config());
    Q4T_CHECK(v.Observe(Begin()).ok());
    Q4T_CHECK(v.Observe(Forward(1, 0, 3)).ok());
    Q4T_CHECK(v.Observe(Layer(0, kIds)).ok());
    if (failure != 2) Q4T_CHECK(v.Observe(Layer(1, kIds)).ok());
    auto end = Commit();
    if (failure == 0) end.submission_ok = false;
    if (failure == 1) end.gpu_complete = false;
    Q4T_CHECK(!v.Observe(end).ok());
    Q4T_CHECK(v.summary().committed_prefill_rows == 0);
    Q4T_CHECK(!v.Observe(Commit()).ok() && !v.Finish().ok());
  }
  return true;
}

Q4T_TEST(router_trace_rejects_layer_and_id_corruption) {
  const uint16_t duplicates[] = {0, 0, 1, 2, 3, 4};
  const uint16_t outside[] = {0, 8, 1, 2, 3, 4};
  for (int failure = 0; failure < 4; ++failure) {
    RouterTraceValidator v(Config());
    Q4T_CHECK(v.Observe(Begin()).ok());
    Q4T_CHECK(v.Observe(Forward(1, 0, 3)).ok());
    auto e = Layer(0, kIds);
    if (failure == 0) e.layer = 1;
    if (failure == 1) e.expert_ids = duplicates;
    if (failure == 2) e.expert_ids = outside;
    if (failure == 3) e.expert_ids = {kIds, 4};
    Q4T_CHECK(!v.Observe(e).ok());
    Q4T_CHECK(!v.Finish().ok());
  }
  RouterTraceValidator v(Config());
  Q4T_CHECK(v.Observe(Begin()).ok());
  Q4T_CHECK(v.Observe(Forward(1, 0, 3)).ok());
  Q4T_CHECK(v.Observe(Layer(0, kIds)).ok());
  Q4T_CHECK(!v.Observe(Layer(0, kIds)).ok());
  return true;
}

Q4T_TEST(router_trace_positions_and_request_identity) {
  for (int failure = 0; failure < 5; ++failure) {
    RouterTraceValidator v(Config());
    Q4T_CHECK(Prefill(v));
    if (failure == 0) {
      Q4T_CHECK(!v.Observe(Forward(2, 4, 1, RouteStage::kDecode)).ok());
    } else if (failure == 1) {
      Q4T_CHECK(!v.Observe(Forward(2, 3, 1)).ok());
    } else if (failure == 2) {
      Q4T_CHECK(!v.Observe(Forward(2, 3, 2, RouteStage::kDecode)).ok());
    } else if (failure == 3) {
      Q4T_CHECK(!v.Observe(Forward(1, 3, 1, RouteStage::kDecode)).ok());
    } else {
      Q4T_CHECK(v.Observe(End()).ok());
      Q4T_CHECK(!v.Observe(Begin()).ok());
    }
  }
  RouterTraceValidator v(Config());
  Q4T_CHECK(Prefill(v) && v.Observe(End()).ok());
  Q4T_CHECK(v.Observe(Begin(2, 1)).ok());
  Q4T_CHECK(v.Observe(Forward(2, 0, 1)).ok());
  Q4T_CHECK(!v.Observe(End()).ok());  // Cannot end pending GPU work.
  return true;
}

Q4T_TEST(router_trace_failed_forward_is_diagnostic) {
  for (auto outcome : {RequestOutcome::kFailed, RequestOutcome::kCancelled}) {
    RouterTraceValidator v(Config());
    Q4T_CHECK(v.Observe(Begin()).ok());
    Q4T_CHECK(v.Observe(Forward(1, 0, 3)).ok());
    Q4T_CHECK(v.Observe(Layer(0, kIds)).ok());
    auto end = Commit();
    end.submission_ok = end.committed = false;
    Q4T_CHECK(v.Observe(end).ok());
    Q4T_CHECK(v.Observe(End(outcome)).ok());
    Q4T_CHECK(v.Observe(RouteEvent{}).ok() && v.Finish().ok());
    Q4T_CHECK(v.summary().successful_requests == 0);
    Q4T_CHECK(v.summary().committed_forwards == 0);
  }
  RouterTraceValidator v(Config());
  Q4T_CHECK(v.Observe(Begin()).ok());
  Q4T_CHECK(v.Observe(Forward(1, 0, 3)).ok());
  auto end = Commit();
  end.committed = false;
  Q4T_CHECK(v.Observe(end).ok());
  Q4T_CHECK(!v.Observe(End()).ok());
  return true;
}

Q4T_TEST(router_trace_bounds_and_empty_run) {
  for (int failure = 0; failure < 7; ++failure) {
    auto c = Config();
    if (failure == 0) c.layers = 0;
    if (failure == 1) c.experts = 65537;
    if (failure == 2) c.top_k = 17;
    if (failure == 3) c.max_rows = UINT32_MAX;
    if (failure == 4) c.binary_sha256 = {};
    if (failure == 5) c.model_sha256 = {};
    if (failure == 6) c.workload_sha256 = {};
    RouterTraceValidator v(c);
    Q4T_CHECK(!v.Observe(Begin()).ok());
  }
  RouterTraceValidator v(Config());
  Q4T_CHECK(!v.Finish().ok());
  Q4T_CHECK(!v.Observe(RouteEvent{}).ok());
  return true;
}

Q4T_TEST(router_trace_wire_roundtrip_and_truncation) {
  std::ostringstream output(std::ios::binary);
  RouterTraceWriter writer(output, Config());
  Q4T_CHECK(writer.Write(Begin()).ok());
  Q4T_CHECK(writer.Write(Forward(1, 0, 3)).ok());
  Q4T_CHECK(writer.Write(Layer(0, kIds)).ok());
  Q4T_CHECK(writer.Write(Layer(1, kIds)).ok());
  Q4T_CHECK(writer.Write(Commit()).ok());
  Q4T_CHECK(writer.Write(End()).ok());
  Q4T_CHECK(writer.Write(RouteEvent{}).ok() && writer.Finish().ok());
  const auto bytes = output.str();
  TraceSummary summary;
  std::istringstream input(bytes, std::ios::binary);
  Q4T_CHECK(CheckRouterTrace(input, &summary).ok());
  Q4T_CHECK(summary.requests == 1 && summary.route_ids == 12);
  for (size_t size = 0; size < bytes.size(); ++size) {
    std::istringstream truncated(bytes.substr(0, size), std::ios::binary);
    Q4T_CHECK(!CheckRouterTrace(truncated, &summary).ok());
    Q4T_CHECK(summary.requests == 0);
  }
  for (size_t pos = 0; pos < bytes.size(); ++pos) {
    auto corrupt = bytes;
    corrupt[pos] ^= 1;
    std::istringstream stream(corrupt, std::ios::binary);
    Q4T_CHECK(!CheckRouterTrace(stream, &summary).ok());
  }
  std::istringstream trailing(bytes + "x", std::ios::binary);
  Q4T_CHECK(!CheckRouterTrace(trailing, &summary).ok());
  return true;
}

Q4T_TEST(router_trace_write_failure_cannot_finish) {
  std::ostringstream output;
  RouterTraceWriter writer(output, Config());
  output.setstate(std::ios::badbit);
  Q4T_CHECK(!writer.Write(Begin()).ok());
  output.clear();
  Q4T_CHECK(!writer.Finish().ok());
  std::ostringstream unfinished;
  RouterTraceWriter partial(unfinished, Config());
  Q4T_CHECK(partial.Write(Begin()).ok());
  Q4T_CHECK(!partial.Finish().ok());
  return true;
}

Q4T_TEST(router_trace_undrained_work_prevents_reuse) {
  RouterTraceValidator v(Config());
  Q4T_CHECK(v.Observe(Begin()).ok());
  Q4T_CHECK(v.Observe(Forward(1, 0, 3)).ok());
  auto failed = Commit();
  failed.committed = failed.gpu_complete = false;
  Q4T_CHECK(v.Observe(failed).ok());
  Q4T_CHECK(v.Observe(End(RequestOutcome::kFailed)).ok());
  Q4T_CHECK(!v.Observe(Begin(2)).ok());
  return true;
}

Q4T_TEST(router_trace_counters_and_flush_failure) {
  RouterTraceValidator v(Config());
  Q4T_CHECK(Prefill(v));
  auto end = End();
  end.output_tokens = UINT64_MAX;
  Q4T_CHECK(v.Observe(end).ok());
  Q4T_CHECK(v.Observe(Begin(2)).ok());
  Q4T_CHECK(!v.Observe(End(RequestOutcome::kCancelled)).ok());

  struct FlushFailure : std::stringbuf {
    int sync() override { return -1; }
  } buffer;
  std::ostream output(&buffer);
  RouterTraceWriter writer(output, Config());
  Q4T_CHECK(writer.Write(Begin()).ok());
  Q4T_CHECK(writer.Write(End(RequestOutcome::kCancelled)).ok());
  Q4T_CHECK(writer.Write(RouteEvent{}).ok());
  Q4T_CHECK(!writer.Finish().ok());
  return true;
}
