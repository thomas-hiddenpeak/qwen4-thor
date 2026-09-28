#include "q4t/trace/router_collector.h"
#include "q4t/trace/sha256.h"

#include <algorithm>
#include <bit>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <stdexcept>
#include <string_view>
#include <utility>

extern char** environ;

namespace q4t::trace {
namespace fs = std::filesystem;
namespace {
void WriteBytes(std::ostream& out, const void* data, size_t bytes) {
  out.write(static_cast<const char*>(data), bytes);
  if (!out) throw std::runtime_error("trace write failed");
}
}  // namespace
RouterCollector::~RouterCollector() {
  Stop();
  // Users have already checked completion at every forward boundary. If a
  // CUDA failure was reported, drain before releasing any trace allocation.
  if (device_ && cudaStreamSynchronize(nullptr) != cudaSuccess) {
    std::fprintf(stderr,
                 "[q4t][trace] failed final drain; retain pools "
                 "until process exit\n");
    return;
  }
  if (device_) cudaFree(device_);
  for (auto& slot : slots_)
    if (slot.host) cudaFreeHost(slot.host);
}
const char* RouterCollector::Reason(Failure f) {
  switch (f) {
    case Failure::kNone:
      return "none";
    case Failure::kQueueFull:
      return "queue_full";
    case Failure::kQuota:
      return "byte_quota";
    case Failure::kIO:
      return "writer_io";
    case Failure::kContract:
      return "capture_contract";
    case Failure::kCuda:
      return "cuda_failure";
    case Failure::kRequestLimit:
      return "request_limit";
    case Failure::kAllocation:
      return "allocation_failed";
  }
  return "unknown";
}
void RouterCollector::Fail(Failure reason) {
  auto expected = Failure::kNone;
  failure_.compare_exchange_strong(expected, reason);
}
Status RouterCollector::AllocationFailure(cudaError_t error) {
  // Optional allocation exhaustion must not leak a stale CUDA error into
  // the first business launch. Never downgrade other CUDA errors to OOM.
  if (error == cudaErrorMemoryAllocation) {
    const auto last = cudaGetLastError();
    Fail(last == cudaSuccess || last == cudaErrorMemoryAllocation
             ? Failure::kAllocation
             : Failure::kCuda);
  } else {
    Fail(Failure::kCuda);
  }
  Manifest(false);
  return Status::Fail("trace pool allocation: " +
                      std::string(cudaGetErrorString(error)));
}
Status RouterCollector::Start(const std::string& directory,
                              const std::string& workload,
                              const std::string& model_index,
                              RouterTraceConfig config, uint32_t max_length,
                              uint64_t max_bytes) {
  static_assert(std::endian::native == std::endian::little);
  if (!directory_.empty()) return Status::Fail("trace already initialized");
  const uint64_t elements =
      uint64_t{config.layers} * config.max_rows * config.top_k;
  if (!elements || elements > 4 * 1024 * 1024 || !max_length ||
      max_length > 4 * 1024 * 1024 || max_bytes < 65536)
    return Status::Fail("trace pool/quota outside supported bounds");
  Status s = HashFile("/proc/self/exe", &config.binary_sha256);
  if (!s.ok()) return s;
  s = HashFile(model_index, &config.model_sha256);
  if (!s.ok()) return s;
  s = HashFile(workload, &config.workload_sha256);
  if (!s.ok()) return s;
  try {
    const auto path = fs::weakly_canonical(directory);
    const auto root = fs::weakly_canonical(".q4t-work");
    const auto relative = path.lexically_relative(root);
    if (relative.empty() || relative == "." || *relative.begin() == "..")
      return Status::Fail("trace directory must be under cwd/.q4t-work");
    if (!fs::is_directory(path.parent_path()) || !fs::create_directory(path))
      return Status::Fail("trace directory exists or parent is missing");
    directory_ = path.string();
    config_ = config;
    max_length_ = max_length;
    max_bytes_ = max_bytes;
    slot_bytes_ = std::max(elements, uint64_t{max_length}) * sizeof(int32_t);
    Manifest(false);
    written_ = fs::file_size(model_index) + fs::file_size(workload) + 65536;
    if (written_ >= max_bytes_) {
      Fail(Failure::kQuota);
      Manifest(false);
      return Status::Fail("trace metadata exceeds quota");
    }
    fs::copy_file(model_index, path / "model-index.json");
    fs::copy_file(workload, path / "workload.json");
    // Record actual launch/precision context. Raw NUL-separated bytes avoid
    // lossy shell reconstruction; no unrelated environment secrets are saved.
    std::ifstream command("/proc/self/cmdline", std::ios::binary);
    std::array<char, 32768> command_bytes;
    command.read(command_bytes.data(), command_bytes.size());
    const size_t command_size = command.gcount();
    if (!command.eof())
      throw std::runtime_error("trace command metadata limit");
    std::string selected_env;
    for (char** entry = environ; *entry; ++entry) {
      const std::string_view value(*entry);
      if (value.starts_with("Q4T_") || value.starts_with("LD_PRELOAD=") ||
          value.starts_with("LD_LIBRARY_PATH=") ||
          value.starts_with("CUDA_VISIBLE_DEVICES=")) {
        if (selected_env.size() + value.size() + 1 > 16384)
          throw std::runtime_error("trace environment metadata limit");
        selected_env.append(value);
        selected_env.push_back('\0');
      }
    }
    const auto model_config =
        fs::path(model_index).parent_path() / "config.json";
    if (fs::file_size(model_config) > 8192)
      throw std::runtime_error("trace model config metadata limit");
    fs::copy_file(model_config, path / "model-config.json");
    for (const auto& item :
         {std::pair{"command.bin",
                    std::string_view(command_bytes.data(), command_size)},
          std::pair{"environment.bin", std::string_view(selected_env)}}) {
      std::ofstream file(path / item.first, std::ios::binary);
      WriteBytes(file, item.second.data(), item.second.size());
      file.close();
      if (!file) throw std::runtime_error("trace launch metadata write");
    }
    if (!HashFile((path / "model-config.json").string(), &model_config_digest_)
             .ok() ||
        !HashFile((path / "command.bin").string(), &command_digest_).ok() ||
        !HashFile((path / "environment.bin").string(), &environment_digest_)
             .ok())
      throw std::runtime_error("trace launch metadata hash");
    Manifest(false);
    auto allocation =
        cudaMalloc(reinterpret_cast<void**>(&device_), slot_bytes_);
    if (allocation != cudaSuccess) return AllocationFailure(allocation);
    for (auto& slot : slots_) {
      allocation =
          cudaMallocHost(reinterpret_cast<void**>(&slot.host), slot_bytes_);
      if (allocation != cudaSuccess) return AllocationFailure(allocation);
    }
    worker_ = std::thread([this] { Worker(); });
    std::fprintf(stderr,
                 "[q4t][trace] enabled full-request slots=%zu "
                 "device_bytes=%zu pinned_bytes=%zu quota=%llu\n",
                 kSlots, slot_bytes_, slot_bytes_ * kSlots,
                 static_cast<unsigned long long>(max_bytes_));
    return Status();
  } catch (const std::exception& e) {
    Fail(Failure::kIO);
    return Status::Fail(std::string("trace startup: ") + e.what());
  }
}
RouterCollector::Slot* RouterCollector::Reserve(Kind kind) {
  if (failure_.load() != Failure::kNone || stopping_.load()) return nullptr;
  auto& slot = slots_[producer_];
  if (slot.ready.load(std::memory_order_acquire)) {
    Fail(Failure::kQueueFull);
    return nullptr;
  }
  slot.kind = kind;
  slot.request = request_id_;
  slot.layers = 0;
  return &slot;
}
void RouterCollector::Publish() {
  slots_[producer_].ready.store(true, std::memory_order_release);
  producer_ = (producer_ + 1) % kSlots;
}
void RouterCollector::BeginRequest(std::span<const int32_t> tokens,
                                   const std::string& http_id) {
  if (failure_.load() != Failure::kNone) return;
  if (request_open_ || tokens.empty() || tokens.size() > max_length_ ||
      http_id.size() > 128) {
    Fail(Failure::kContract);
    return;
  }
  if (++request_id_ > 1024) {
    Fail(Failure::kRequestLimit);
    return;
  }
  auto* slot = Reserve(Kind::kBegin);
  if (!slot) return;
  slot->rows = tokens.size();
  std::memcpy(slot->host, tokens.data(), tokens.size_bytes());
  std::memcpy(slot->http_id.data(), http_id.data(), http_id.size());
  slot->http_id[http_id.size()] = 0;
  request_open_ = true;
  Publish();
}
void RouterCollector::EndRequest(RequestOutcome outcome, uint64_t tokens) {
  if (!request_open_) return;
  request_open_ = false;
  if (active_) {
    Fail(Failure::kContract);
    return;
  }
  auto* slot = Reserve(Kind::kEnd);
  if (!slot) return;
  slot->outcome = outcome;
  slot->output_tokens = tokens;
  Publish();
}
void RouterCollector::BeginForward(RouteStage stage, uint64_t position,
                                   uint32_t rows) {
  if (failure_.load() != Failure::kNone) return;
  if (!request_open_ || active_ || !rows || rows > config_.max_rows) {
    Fail(Failure::kContract);
    return;
  }
  active_error_ = cudaSuccess;
  active_ = Reserve(Kind::kForward);
  if (!active_) return;
  active_->stage = stage;
  active_->position = position;
  active_->rows = rows;
  active_->forward = ++forward_id_;
}
Status RouterCollector::CaptureLayer(uint32_t layer, const int32_t* ids,
                                     int rows, int top_k, cudaStream_t stream) {
  if (!active_) return Status();
  if (layer != active_->layers || layer >= config_.layers ||
      rows != static_cast<int>(active_->rows) ||
      top_k != static_cast<int>(config_.top_k)) {
    Fail(Failure::kContract);
    return Status();  // Observation contract failure stops capture only.
  }
  const size_t count = size_t(rows) * top_k;
  const auto error =
      cudaMemcpyAsync(device_ + layer * count, ids, count * sizeof(int32_t),
                      cudaMemcpyDeviceToDevice, stream);
  if (error != cudaSuccess) {
    active_error_ = error;
    Fail(Failure::kCuda);
    return Status::Fail("trace device copy: " +
                        std::string(cudaGetErrorString(error)));
  }
  ++active_->layers;
  return Status();
}
cudaError_t RouterCollector::Readback(cudaStream_t stream) {
  if (!active_) return cudaSuccess;
  if (active_error_ != cudaSuccess) return active_error_;
  const size_t bytes =
      size_t(active_->layers) * active_->rows * config_.top_k * 4;
  const auto error = bytes ? cudaMemcpyAsync(active_->host, device_, bytes,
                                             cudaMemcpyDeviceToHost, stream)
                           : cudaSuccess;
  if (error != cudaSuccess) Fail(Failure::kCuda);
  return error;
}
void RouterCollector::Complete(bool submitted, bool gpu_complete,
                               bool committed) {
  if (!active_) return;
  active_->submitted = submitted;
  active_->complete = gpu_complete;
  active_->committed = committed;
  if (!gpu_complete) Fail(Failure::kCuda);
  // The worker must never read host memory while CUDA might still write it.
  // Leave this slot unpublished/quarantined if completion failed.
  if (gpu_complete && failure_.load() == Failure::kNone) Publish();
  active_ = nullptr;
}
void RouterCollector::Stop() {
  stopping_.store(true);
  if (worker_.joinable()) worker_.join();
}
void RouterCollector::Manifest(bool complete) {
  std::ofstream file(directory_ + "/manifest.partial", std::ios::trunc);
  file << "{\"schema\":1,\"mode\":\"full-controlled\",\"complete\":"
       << (complete ? "true" : "false") << ",\"failure\":\""
       << Reason(failure_.load()) << "\",\"requests_started\":" << request_id_
       << ",\"requests_published\":" << finished_ << ",\"binary_sha256\":\""
       << HexDigest(config_.binary_sha256) << "\",\"model_index_sha256\":\""
       << HexDigest(config_.model_sha256) << "\",\"workload_sha256\":\""
       << HexDigest(config_.workload_sha256) << "\",\"model_config_sha256\":\""
       << HexDigest(model_config_digest_) << "\",\"command_sha256\":\""
       << HexDigest(command_digest_) << "\",\"environment_sha256\":\""
       << HexDigest(environment_digest_) << "\",\"layers\":" << config_.layers
       << ",\"experts\":" << config_.experts << ",\"top_k\":" << config_.top_k
       << ",\"max_rows\":" << config_.max_rows
       << ",\"max_length\":" << max_length_ << ",\"slots\":" << kSlots
       << ",\"device_bytes\":" << slot_bytes_
       << ",\"pinned_bytes\":" << slot_bytes_ * kSlots
       << ",\"quota_bytes\":" << max_bytes_
       << ",\"accounted_bytes\":" << written_ << "}\n";
  file.flush();
  if (!file) throw std::runtime_error("trace manifest write failed");
  file.close();
  if (!file) throw std::runtime_error("trace manifest close failed");
  fs::rename(directory_ + "/manifest.partial", directory_ + "/manifest.json");
}
void RouterCollector::Worker() {
  size_t consumer = 0;
  std::unique_ptr<RouterTraceWriter> writer;
  std::ofstream output;
  std::string prefix;
  std::vector<uint16_t> ids;
  const auto write = [&](const RouteEvent& e) {
    if (!writer || !writer->Write(e).ok())
      throw std::runtime_error("trace contract/write");
  };
  try {
    for (;;) {
      auto& slot = slots_[consumer];
      if (failure_.load() != Failure::kNone) break;
      if (!slot.ready.load(std::memory_order_acquire)) {
        if (stopping_.load()) break;
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
        continue;
      }
      const uint64_t charge =
          slot.kind == Kind::kForward
              ? uint64_t(slot.layers) *
                        (slot.rows * config_.top_k * 2ull + 24) +
                    128
              : (slot.kind == Kind::kBegin ? uint64_t(slot.rows) * 4 + 1024
                                           : 512);
      if (charge > max_bytes_ - written_) {
        Fail(Failure::kQuota);
        break;
      }
      written_ += charge;
      RouteEvent e;
      if (slot.kind == Kind::kBegin) {
        if (writer) throw std::runtime_error("request overlap");
        prefix = directory_ + "/request-" + std::to_string(slot.request);
        output.open(prefix + ".partial", std::ios::binary);
        writer = std::make_unique<RouterTraceWriter>(output, config_);
        const size_t bytes = size_t(slot.rows) * 4;
        std::ofstream tokens(prefix + ".tokens", std::ios::binary);
        WriteBytes(tokens, slot.host, bytes);
        tokens.close();
        if (!tokens) throw std::runtime_error("token file close");
        std::ofstream metadata(prefix + ".json");
        metadata << "{\"request_id\":" << slot.request << ",\"http_id\":\""
                 << slot.http_id.data() << "\",\"prompt_tokens\":" << slot.rows
                 << "}\n";
        metadata.close();
        if (!metadata) throw std::runtime_error("request metadata write");
        e.kind = RouteRecord::kRequestBegin;
        e.request_id = slot.request;
        e.prompt_rows = slot.rows;
        e.prompt_sha256 =
            Sha256({reinterpret_cast<uint8_t*>(slot.host), bytes});
        write(e);
      } else if (slot.kind == Kind::kForward) {
        e.kind = RouteRecord::kForwardBegin;
        e.forward_id = slot.forward;
        e.stage = slot.stage;
        e.position = slot.position;
        e.rows = slot.rows;
        write(e);
        const size_t count = size_t(slot.rows) * config_.top_k;
        ids.resize(count);
        for (uint32_t layer = 0; layer < slot.layers; ++layer) {
          for (size_t j = 0; j < count; ++j) {
            const auto id = slot.host[layer * count + j];
            if (id < 0 || static_cast<uint32_t>(id) >= config_.experts)
              throw std::runtime_error("invalid captured expert ID");
            ids[j] = id;
          }
          e.kind = RouteRecord::kLayer;
          e.layer = layer;
          e.expert_ids = ids;
          write(e);
        }
        e.kind = RouteRecord::kForwardEnd;
        e.submission_ok = slot.submitted;
        e.gpu_complete = slot.complete;
        e.committed = slot.committed;
        write(e);
      } else {
        e.kind = RouteRecord::kRequestEnd;
        e.outcome = slot.outcome;
        e.output_tokens = slot.output_tokens;
        write(e);
        write(RouteEvent{});
        if (!writer->Finish().ok()) throw std::runtime_error("trace finish");
        writer.reset();
        output.close();
        if (!output) throw std::runtime_error("trace close");
        fs::rename(prefix + ".partial", prefix + ".bin");
        ++finished_;
      }
      slot.ready.store(false, std::memory_order_release);
      consumer = (consumer + 1) % kSlots;
    }
    if (writer) {
      writer.reset();
      output.close();
      Fail(Failure::kContract);
    }
    Manifest(failure_.load() == Failure::kNone && finished_ > 0);
  } catch (const std::exception& error) {
    Fail(Failure::kIO);
    std::fprintf(stderr, "[q4t][trace] writer stopped: %s\n", error.what());
    try {
      Manifest(false);
    } catch (...) {
    }
  }
  std::fprintf(stderr, "[q4t][trace] published=%llu stopped=%s\n",
               static_cast<unsigned long long>(finished_),
               Reason(failure_.load()));
}
}  // namespace q4t::trace
