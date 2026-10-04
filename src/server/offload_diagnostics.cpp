#include "q4t/server/offload_diagnostics.h"

#include <algorithm>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <iomanip>
#include <locale>
#include <sstream>
#include <utility>

namespace q4t::server {
namespace {

uint64_t MonotonicNs() {
  return std::chrono::duration_cast<std::chrono::nanoseconds>(
             std::chrono::steady_clock::now().time_since_epoch())
      .count();
}

uint64_t RealtimeNs() {
  return std::chrono::duration_cast<std::chrono::nanoseconds>(
             std::chrono::system_clock::now().time_since_epoch())
      .count();
}

std::optional<uint64_t> ReadProcessBytes() {
  FILE* file = std::fopen("/proc/self/io", "r");
  if (!file) return std::nullopt;
  std::optional<uint64_t> value;
  char line[256];
  while (std::fgets(line, sizeof(line), file)) {
    unsigned long long parsed = 0;
    if (std::strncmp(line, "read_bytes:", 11) == 0 &&
        std::sscanf(line + 11, "%llu", &parsed) == 1) {
      value = static_cast<uint64_t>(parsed);
      break;
    }
  }
  std::fclose(file);
  return value;
}

template <typename T>
void WriteArray(std::ostream& out, const std::vector<T>& values) {
  out << '[';
  for (size_t i = 0; i < values.size(); ++i) {
    if (i) out << ',';
    out << +values[i];
  }
  out << ']';
}

void WriteStats(std::ostream& out, const quant::MoEResidency::Stats& s) {
  out << "{\"resolve_calls\":" << s.resolve_calls;
#define Q4T_STAT(key, member) out << ",\"" key "\":" << s.member
  Q4T_STAT("expert_lookups", expert_lookups);
  Q4T_STAT("hits", hits);
  Q4T_STAT("misses", misses);
  Q4T_STAT("loads", loads);
  Q4T_STAT("evictions", evictions);
  Q4T_STAT("load_bytes", load_bytes);
  Q4T_STAT("nvme_read_bytes", nvme_read_bytes);
  Q4T_STAT("shape_single_lookups", decode_lookups);
  Q4T_STAT("shape_single_misses", decode_misses);
  Q4T_STAT("shape_multi_lookups", prefill_lookups);
  Q4T_STAT("shape_multi_misses", prefill_misses);
  Q4T_STAT("l2_hits", l2_hits);
  Q4T_STAT("l2_misses", l2_misses);
  Q4T_STAT("l2_evictions", l2_evictions);
  Q4T_STAT("l2_shape_single_hits", l2_decode_hits);
  Q4T_STAT("l2_shape_single_misses", l2_decode_misses);
  Q4T_STAT("l2_shape_single_evictions", l2_decode_evictions);
  Q4T_STAT("l2_shape_multi_hits", l2_prefill_hits);
  Q4T_STAT("l2_shape_multi_misses", l2_prefill_misses);
  Q4T_STAT("l2_shape_multi_evictions", l2_prefill_evictions);
  Q4T_STAT("mirror_hits", mirror_hits);
  Q4T_STAT("mirror_writebacks", mirror_writebacks);
  Q4T_STAT("mirror_skips", mirror_skips);
  Q4T_STAT("pread_merge_runs", pread_merge_runs);
  Q4T_STAT("pread_merge_experts", pread_merge_experts);
#undef Q4T_STAT
  out << '}';
}

void WriteTiming(std::ostream& out, const model::ResidencyTimingSnapshot& t,
                 uint64_t merge_count, uint64_t merge_ns,
                 uint64_t merge_max_ns) {
  if (!t.enabled) {
    out << "null";
    return;
  }
  out << "{\"enabled\":true";
#define Q4T_TIME(key, member) out << ",\"" key "\":" << t.member
#define Q4T_TIMES(key, member)            \
  Q4T_TIME(key "_count", member##_count); \
  Q4T_TIME(key "_ns", member##_ns);       \
  Q4T_TIME(key "_max_ns", member##_max_ns)
  Q4T_TIMES("stage", stage);
  Q4T_TIMES("pread", pread);
  Q4T_TIMES("swz", swz);
  Q4T_TIMES("phase1", phase1);
  Q4T_TIMES("d2h", d2h);
  Q4T_TIMES("shape_single_stage", dstage);
  Q4T_TIMES("shape_single_pread", dpread);
  Q4T_TIMES("shape_single_phase1", dphase1);
#undef Q4T_TIMES
#undef Q4T_TIME
  out << ",\"pread_merge_count\":" << merge_count
      << ",\"pread_merge_ns\":" << merge_ns
      << ",\"pread_merge_max_ns\":" << merge_max_ns << '}';
}

void WriteCache(std::ostream& out,
                const quant::MoEResidency::DiagnosticState& s, size_t layer) {
  out << "{\"layer\":" << layer << ",\"slot_experts\":";
  WriteArray(out, s.slot_experts);
  out << ",\"slot_ticks\":";
  WriteArray(out, s.slot_ticks);
  out << ",\"slot_protected\":";
  WriteArray(out, s.slot_protected);
  out << ",\"l2_experts\":";
  WriteArray(out, s.l2_experts);
  out << ",\"l2_ticks\":";
  WriteArray(out, s.l2_ticks);
  out << ",\"mirror_experts\":";
  WriteArray(out, s.mirror_experts);
  out << ",\"slot_clock\":" << s.slot_clock << ",\"l2_clock\":" << s.l2_clock
      << ",\"mirror_cursor\":" << s.mirror_cursor << '}';
}

}  // namespace

OffloadDiagnostics::OffloadDiagnostics(std::string request_id)
    : request_id_(std::move(request_id)) {
  snapshots_.reserve(6);
}

void OffloadDiagnostics::Capture(const model::Model& model, const char* event,
                                 uint64_t decode_forwards, bool full_cache,
                                 bool gpu_work_complete) {
  Snapshot snapshot;
  snapshot.monotonic_ns = MonotonicNs();
  snapshot.realtime_ns = RealtimeNs();
  snapshot.event = event;
  snapshot.decode_forwards = decode_forwards;
  snapshot.full_cache = full_cache;
  snapshot.gpu_work_complete = gpu_work_complete;
  snapshot.pid_read_bytes = ReadProcessBytes();
  snapshot.stats = model::SumResidencyStats(model);
  snapshot.timing = model::SumResidencyTiming(model);
  if (full_cache) snapshot.layers.reserve(model.layers.size());
  for (const auto& layer : model.layers) {
    const auto& residency = layer.moe_residency;
    const auto& stats = residency.GetStats();
    snapshot.stats.pread_merge_runs += stats.pread_merge_runs;
    snapshot.stats.pread_merge_experts += stats.pread_merge_experts;
    const auto& timing = residency.GetTiming();
    snapshot.merge_count += timing.pread_merge_count.load();
    snapshot.merge_ns += timing.pread_merge_ns.load();
    snapshot.merge_max_ns =
        std::max(snapshot.merge_max_ns, timing.pread_merge_max_ns.load());
    if (full_cache) snapshot.layers.push_back(residency.CopyDiagnosticState());
  }
  snapshot.end_realtime_ns = RealtimeNs();
  snapshot.end_monotonic_ns = MonotonicNs();
  snapshots_.push_back(std::move(snapshot));
}

void OffloadDiagnostics::Emit(const char* outcome, uint64_t output_tokens,
                              uint64_t decode_forwards) const {
  const bool complete = std::strcmp(outcome, "success") == 0 &&
                        snapshots_.size() >= 3 &&
                        snapshots_[0].event == "prefill_begin" &&
                        snapshots_[1].event == "prefill_end_decode_begin" &&
                        snapshots_.back().event == "inference_end";
  std::ostringstream out;
  out.imbue(std::locale::classic());
  out << std::setprecision(17)
      << "[q4t][offload_diag] {\"schema\":\"q4t.offload_phase.v1\","
      << "\"request_id\":\"" << request_id_ << "\",\"outcome\":\"" << outcome
      << "\",\"completeness\":\"" << (complete ? "complete" : "partial")
      << "\",\"input_tokens\":";
  if (input_tokens_)
    out << *input_tokens_;
  else
    out << "null";
  out << ",\"output_tokens\":" << output_tokens
      << ",\"decode_forwards_completed\":" << decode_forwards
      << ",\"snapshots\":[";
  for (size_t i = 0; i < snapshots_.size(); ++i) {
    const auto& s = snapshots_[i];
    if (i) out << ',';
    out << "{\"event\":\"" << s.event
        << "\",\"decode_forward_count\":" << s.decode_forwards
        << ",\"monotonic_ns\":" << s.monotonic_ns
        << ",\"realtime_ns\":" << s.realtime_ns
        << ",\"capture_end_monotonic_ns\":" << s.end_monotonic_ns
        << ",\"capture_end_realtime_ns\":" << s.end_realtime_ns
        << ",\"capture_ns\":" << s.end_monotonic_ns - s.monotonic_ns
        << ",\"pid_read_bytes\":";
    if (s.pid_read_bytes)
      out << *s.pid_read_bytes;
    else
      out << "null";
    out << ",\"pid_read_bytes_scope\":\"process_all_files\","
        << "\"gpu_work_complete\":" << (s.gpu_work_complete ? "true" : "false")
        << ",\"cache_state\":\"" << (s.full_cache ? "full" : "omitted")
        << "\",\"stats\":";
    WriteStats(out, s.stats);
    out << ",\"timing\":";
    WriteTiming(out, s.timing, s.merge_count, s.merge_ns, s.merge_max_ns);
    out << ",\"layers\":";
    if (!s.full_cache)
      out << "null";
    else {
      out << '[';
      for (size_t layer = 0; layer < s.layers.size(); ++layer) {
        if (layer) out << ',';
        WriteCache(out, s.layers[layer], layer);
      }
      out << ']';
    }
    out << '}';
  }
  out << "]}\n";
  const std::string line = out.str();
  // stdio serializes this one write with the server's other log writers.
  std::fwrite(line.data(), 1, line.size(), stderr);
}

}  // namespace q4t::server
