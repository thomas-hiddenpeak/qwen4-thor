#include "q4t/trace/router_trace.h"

#include <algorithm>
#include <istream>
#include <limits>
#include <ostream>
#include <string>

namespace q4t::trace {
namespace {
constexpr uint32_t kMaxPayload = 16 * 1024 * 1024;
constexpr char kMagic[] = "Q4TRTE01";
using Bytes = std::vector<uint8_t>;

bool HasDigest(const Digest& digest) {
  return std::any_of(digest.begin(), digest.end(),
                     [](auto v) { return v != 0; });
}
Status ValidateConfig(const RouterTraceConfig& c) {
  if (c.layers == 0 || c.layers > 4096 || c.experts == 0 || c.experts > 65536 ||
      c.top_k == 0 || c.top_k > 16 || c.top_k > c.experts || c.max_rows == 0 ||
      uint64_t{c.max_rows} * c.top_k * 2 + 16 > kMaxPayload ||
      !HasDigest(c.binary_sha256) || !HasDigest(c.model_sha256) ||
      !HasDigest(c.workload_sha256))
    return Status::Fail("trace: invalid dimensions/identity");
  return Status();
}
void Put(Bytes& out, uint64_t value, int bytes) {
  for (int i = 0; i < bytes; ++i) out.push_back((value >> (8 * i)) & 255);
}
void PutDigest(Bytes& out, const Digest& digest) {
  out.insert(out.end(), digest.begin(), digest.end());
}
struct Cursor {
  std::span<const uint8_t> data;
  size_t position = 0;
  bool valid = true;
  uint64_t Get(size_t bytes) {
    if (!valid || bytes > data.size() - position) {
      valid = false;
      return 0;
    }
    uint64_t value = 0;
    for (size_t i = 0; i < bytes; ++i)
      value |= uint64_t{data[position++]} << (i * 8);
    return value;
  }
  Digest GetDigest() {
    Digest out{};
    for (auto& value : out) value = Get(1);
    return out;
  }
  bool Done() const { return valid && position == data.size(); }
};
uint32_t Crc32(std::span<const uint8_t> bytes) {
  uint32_t crc = ~uint32_t{0};
  for (auto byte : bytes) {
    crc ^= byte;
    for (int i = 0; i < 8; ++i)
      crc = (crc >> 1) ^ (0xedb88320u & (0u - (crc & 1)));
  }
  return ~crc;
}
void WriteFrame(std::ostream& output, const Bytes& data) {
  Bytes prefix;
  Put(prefix, data.size(), 4);
  Put(prefix, Crc32(data), 4);
  output.write(reinterpret_cast<const char*>(prefix.data()), prefix.size());
  output.write(reinterpret_cast<const char*>(data.data()), data.size());
}
Status ReadFrame(std::istream& input, Bytes* data, uint32_t limit) {
  std::array<uint8_t, 8> prefix{};
  if (!input.read(reinterpret_cast<char*>(prefix.data()), prefix.size()))
    return Status::Fail("trace: truncated frame header");
  Cursor cursor{prefix};
  const uint32_t size = cursor.Get(4);
  const uint32_t crc = cursor.Get(4);
  if (size == 0 || size > limit)
    return Status::Fail("trace: invalid frame size");
  data->resize(size);
  if (!input.read(reinterpret_cast<char*>(data->data()), size))
    return Status::Fail("trace: truncated frame payload");
  if (Crc32(*data) != crc) return Status::Fail("trace: CRC mismatch");
  return Status();
}
}  // namespace

RouterTraceValidator::RouterTraceValidator(const RouterTraceConfig& config)
    : config_(config), status_(ValidateConfig(config)) {}

Status RouterTraceValidator::Fail(const char* message) {
  status_ = Status::Fail(message);
  return status_;
}

Status RouterTraceValidator::Observe(const RouteEvent& e) {
  if (!status_.ok()) return status_;
  if (ended_) return Fail("trace: record after run end");
  switch (e.kind) {
    case RouteRecord::kRequestBegin:
      if (request_open_ || gpu_unresolved_ ||
          e.request_id <= last_request_id_ || e.prompt_rows == 0 ||
          !HasDigest(e.prompt_sha256))
        return Fail("trace: invalid request begin");
      request_open_ = true;
      last_request_id_ = e.request_id;
      prompt_rows_ = e.prompt_rows;
      committed_position_ = 0;
      decode_started_ = false;
      request_broken_ = false;
      break;
    case RouteRecord::kForwardBegin:
      if (!request_open_ || forward_open_ || request_broken_ ||
          e.forward_id <= last_forward_id_ || e.rows == 0 ||
          e.rows > config_.max_rows || e.position != committed_position_ ||
          e.rows > std::numeric_limits<uint64_t>::max() - e.position)
        return Fail("trace: invalid forward identity/position");
      if (e.stage == RouteStage::kPrefill) {
        if (decode_started_ || e.position >= prompt_rows_ ||
            e.rows > prompt_rows_ - e.position)
          return Fail("trace: prefill range/stage");
      } else if (e.stage == RouteStage::kDecode) {
        if (e.position < prompt_rows_ || e.rows != 1)
          return Fail("trace: decode before prefill or packed decode");
        decode_started_ = true;
      } else {
        return Fail("trace: unknown stage");
      }
      forward_open_ = true;
      last_forward_id_ = e.forward_id;
      stage_ = e.stage;
      rows_ = e.rows;
      next_layer_ = 0;
      break;
    case RouteRecord::kLayer:
      if (!forward_open_ || e.layer != next_layer_ ||
          e.layer >= config_.layers ||
          e.expert_ids.size() != uint64_t{rows_} * config_.top_k)
        return Fail("trace: missing/duplicate layer or wrong payload shape");
      for (size_t i = 0; i < e.expert_ids.size(); ++i) {
        if (e.expert_ids[i] >= config_.experts)
          return Fail("trace: expert ID outside range");
        const size_t row_start = i - i % config_.top_k;
        for (size_t j = row_start; j < i; ++j)
          if (e.expert_ids[j] == e.expert_ids[i])
            return Fail("trace: duplicate expert within row");
      }
      ++next_layer_;
      summary_.route_ids += e.expert_ids.size();
      break;
    case RouteRecord::kForwardEnd:
      if (!forward_open_ ||
          (e.committed && (!e.submission_ok || !e.gpu_complete ||
                           next_layer_ != config_.layers)))
        return Fail("trace: commit without submission/completion/all layers");
      forward_open_ = false;
      gpu_unresolved_ = !e.gpu_complete;
      if (e.committed) {
        committed_position_ += rows_;
        ++summary_.committed_forwards;
        if (stage_ == RouteStage::kPrefill)
          summary_.committed_prefill_rows += rows_;
        else
          summary_.committed_decode_rows += rows_;
      } else {
        request_broken_ = true;
      }
      break;
    case RouteRecord::kRequestEnd:
      if (!request_open_ || forward_open_ ||
          e.output_tokens >
              std::numeric_limits<uint64_t>::max() - summary_.output_tokens)
        return Fail("trace: request end with unfinished forward");
      if (e.outcome == RequestOutcome::kSuccess) {
        if (request_broken_ || committed_position_ < prompt_rows_)
          return Fail("trace: successful request missing committed prefill");
        ++summary_.successful_requests;
      } else if (e.outcome == RequestOutcome::kCancelled) {
        ++summary_.cancelled_requests;
      } else if (e.outcome == RequestOutcome::kFailed) {
        ++summary_.failed_requests;
      } else {
        return Fail("trace: unknown request outcome");
      }
      ++summary_.requests;
      summary_.output_tokens += e.output_tokens;
      request_open_ = false;
      break;
    case RouteRecord::kRunEnd:
      if (request_open_ || forward_open_ || summary_.requests == 0)
        return Fail("trace: empty/unfinished run");
      ended_ = true;
      break;
    default:
      return Fail("trace: unknown record type");
  }
  ++summary_.records;
  return Status();
}
Status RouterTraceValidator::Finish() const {
  if (!status_.ok()) return status_;
  return ended_ ? Status() : Status::Fail("trace: missing run end");
}

RouterTraceWriter::RouterTraceWriter(std::ostream& output,
                                     const RouterTraceConfig& config)
    : output_(output), validator_(config), status_(ValidateConfig(config)) {
  if (!status_.ok()) return;
  output_.write(kMagic, 8);
  Bytes header;
  Put(header, 1, 4);
  Put(header, config.layers, 4);
  Put(header, config.experts, 4);
  Put(header, config.top_k, 4);
  Put(header, config.max_rows, 4);
  PutDigest(header, config.binary_sha256);
  PutDigest(header, config.model_sha256);
  PutDigest(header, config.workload_sha256);
  WriteFrame(output_, header);
  if (!output_) status_ = Status::Fail("trace: header write failed");
}

Status RouterTraceWriter::Write(const RouteEvent& e) {
  if (!status_.ok()) return status_;
  status_ = validator_.Observe(e);
  if (!status_.ok()) return status_;
  Bytes payload;
  Put(payload, static_cast<uint32_t>(e.kind), 4);
  Put(payload, sequence_++, 8);
  switch (e.kind) {
    case RouteRecord::kRequestBegin:
      Put(payload, e.request_id, 8);
      Put(payload, e.prompt_rows, 8);
      PutDigest(payload, e.prompt_sha256);
      break;
    case RouteRecord::kForwardBegin:
      Put(payload, e.forward_id, 8);
      Put(payload, static_cast<uint32_t>(e.stage), 4);
      Put(payload, e.position, 8);
      Put(payload, e.rows, 4);
      break;
    case RouteRecord::kLayer:
      Put(payload, e.layer, 4);
      for (auto id : e.expert_ids) Put(payload, id, 2);
      break;
    case RouteRecord::kForwardEnd:
      Put(payload, e.submission_ok, 1);
      Put(payload, e.gpu_complete, 1);
      Put(payload, e.committed, 1);
      break;
    case RouteRecord::kRequestEnd:
      Put(payload, static_cast<uint32_t>(e.outcome), 4);
      Put(payload, e.output_tokens, 8);
      break;
    case RouteRecord::kRunEnd:
      break;
  }
  WriteFrame(output_, payload);
  if (!output_) status_ = Status::Fail("trace: record write failed");
  return status_;
}
Status RouterTraceWriter::Finish() {
  if (!status_.ok()) return status_;
  status_ = validator_.Finish();
  if (!status_.ok()) return status_;
  output_.flush();
  if (!output_) status_ = Status::Fail("trace: flush failed");
  return status_;
}

Status CheckRouterTrace(std::istream& input, TraceSummary* summary) {
  if (!summary) return Status::Fail("trace: null summary");
  *summary = {};
  char magic[8];
  if (!input.read(magic, 8) || std::string(magic, 8) != kMagic)
    return Status::Fail("trace: missing/wrong magic");
  Bytes bytes;
  Status s = ReadFrame(input, &bytes, 116);
  if (!s.ok()) return s;
  Cursor header{bytes};
  if (header.Get(4) != 1) return Status::Fail("trace: unsupported version");
  RouterTraceConfig config;
  config.layers = header.Get(4);
  config.experts = header.Get(4);
  config.top_k = header.Get(4);
  config.max_rows = header.Get(4);
  config.binary_sha256 = header.GetDigest();
  config.model_sha256 = header.GetDigest();
  config.workload_sha256 = header.GetDigest();
  if (!header.Done()) return Status::Fail("trace: invalid header length");
  s = ValidateConfig(config);
  if (!s.ok()) return s;
  RouterTraceValidator validator(config);
  const uint32_t limit =
      std::max(uint64_t{60}, uint64_t{config.max_rows} * config.top_k * 2 + 16);
  uint64_t sequence = 0;
  std::vector<uint16_t> ids;
  while (true) {
    s = ReadFrame(input, &bytes, limit);
    if (!s.ok()) return s;
    Cursor cursor{bytes};
    RouteEvent e;
    e.kind = static_cast<RouteRecord>(cursor.Get(4));
    if (cursor.Get(8) != sequence++)
      return Status::Fail("trace: event sequence gap/duplicate");
    switch (e.kind) {
      case RouteRecord::kRequestBegin:
        e.request_id = cursor.Get(8);
        e.prompt_rows = cursor.Get(8);
        e.prompt_sha256 = cursor.GetDigest();
        break;
      case RouteRecord::kForwardBegin:
        e.forward_id = cursor.Get(8);
        e.stage = static_cast<RouteStage>(cursor.Get(4));
        e.position = cursor.Get(8);
        e.rows = cursor.Get(4);
        break;
      case RouteRecord::kLayer:
        e.layer = cursor.Get(4);
        if (!cursor.valid || (bytes.size() - cursor.position) % 2 != 0)
          return Status::Fail("trace: invalid ID payload");
        ids.resize((bytes.size() - cursor.position) / 2);
        for (auto& id : ids) id = cursor.Get(2);
        e.expert_ids = ids;
        break;
      case RouteRecord::kForwardEnd: {
        const auto submitted = cursor.Get(1);
        const auto completed = cursor.Get(1);
        const auto committed = cursor.Get(1);
        if (submitted > 1 || completed > 1 || committed > 1)
          return Status::Fail("trace: noncanonical completion flags");
        e.submission_ok = submitted;
        e.gpu_complete = completed;
        e.committed = committed;
        break;
      }
      case RouteRecord::kRequestEnd:
        e.outcome = static_cast<RequestOutcome>(cursor.Get(4));
        e.output_tokens = cursor.Get(8);
        break;
      case RouteRecord::kRunEnd:
        break;
      default:
        return Status::Fail("trace: unknown record type");
    }
    if (!cursor.Done()) return Status::Fail("trace: invalid record length");
    s = validator.Observe(e);
    if (!s.ok()) return s;
    if (e.kind == RouteRecord::kRunEnd) break;
  }
  if (input.peek() != std::char_traits<char>::eof() || input.bad())
    return Status::Fail("trace: trailing data/read failure");
  s = validator.Finish();
  if (s.ok()) *summary = validator.summary();
  return s;
}
}  // namespace q4t::trace
