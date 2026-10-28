// Independent effective-state reference: real four-layer target (PLE, GDN,
// full attention) and real draft; no model/CUDA skips. Run under the stage lease.
#include "q4t/model/model_owner.h"
#include "q4t/server/session_state.h"

#include <cuda_runtime.h>
#include <sys/stat.h>

#include <algorithm>
#include <bit>
#include <cstdio>
#include <filesystem>
#include <numeric>
#include <stdexcept>

namespace {
using namespace q4t;
void Check(const Status& s) { if (!s) throw std::runtime_error(s.message()); }
void Require(bool ok, const char* text) {
  if (!ok) throw std::runtime_error(text);
}
void Cuda(cudaError_t e) { Require(e == cudaSuccess, cudaGetErrorString(e)); }
struct Buffer {
  uint16_t* p = nullptr;
  explicit Buffer(size_t n) { Cuda(cudaMalloc(&p, n * 2)); }
  ~Buffer() { cudaFree(p); }
};
std::vector<uint8_t> Read(void* p, size_t bytes) {
  std::vector<uint8_t> result(bytes);
  Cuda(cudaMemcpy(result.data(), p, bytes, cudaMemcpyDeviceToHost));
  return result;
}
struct Piece { void* ptr; std::vector<uint8_t> bytes; };
using Image = std::vector<Piece>;
[[maybe_unused]] void Put(const Image& image) {
  for (const auto& part : image)
    Cuda(cudaMemcpy(part.ptr, part.bytes.data(), part.bytes.size(),
                    cudaMemcpyHostToDevice));
}
void Equal(const Image& a, const Image& b) {
  Require(a.size() == b.size(), "reference field count");
  for (size_t i = 0; i < a.size(); ++i) {
    if (a[i].bytes != b[i].bytes) {
      std::fprintf(stderr, "state field mismatch index=%zu bytes=%zu\n", i,
                    a[i].bytes.size());
      throw std::runtime_error("complete state differs");
    }
  }
}
int32_t Argmax(const std::vector<uint8_t>& row) {
  int32_t best = 0;
  const auto* p = reinterpret_cast<const uint16_t*>(row.data());
  auto value = [&](int i) { return std::bit_cast<float>(uint32_t(p[i]) << 16); };
  for (size_t i = 1; i < row.size() / 2; ++i)
    if (value(i) > value(best)) best = i;
  return best;
}

struct Fixture {
  model::ModelOwner owner;
  mtp::MtpModel draft;
  Buffer logits{248320}, trunk{4096u * 10240}, rolling{4u * 10240},
         last{4u * 10240};
  const std::vector<int> caps{16384, 12288, 10240, 8192};
  std::vector<model::ModelSequence> seq{4};
  std::vector<int32_t> bonus{0, 0, 0, 0}, d0{0, 0, 0, 0};
  std::vector<std::vector<uint16_t>> reference_trunks;
  explicit Fixture(const std::string& model_dir, const std::string& subset) {
    model::ModelConfig cfg;
    cfg.model_dir = model_dir;
    cfg.index_path = model_dir + "/model.safetensors.index.json";
    cfg.ple_sidecar = model_dir + "/ple/qwen3.8-flash-next-ple-fp8.bin";
    cfg.num_layers = 4;
    cfg.max_seq = 4;
    cfg.max_len = 16384;
    cfg.max_prefill = 4096;
    cfg.ple_capacity_tokens = 4096;
    cfg.seq_capacities = caps;
    Check(owner.Load(cfg, nullptr));
    mtp::MtpConfig dc;
    dc.mtp_dir = model_dir + "/mtp";
    dc.max_seq = 4;
    dc.max_len = 16384;
    dc.max_prefill = 4096;
    dc.seq_capacities = caps;
    dc.subset_head_dir = subset;
    Check(mtp::LoadMtp(dc, main().head.embed_tokens, main().head.lm_head,
                        &draft, nullptr));
    Check(model::ModelReserveVerifyCheckpoints(main(), 3));
    Check(mtp::MtpReserveScratch(draft, 4));
  }
  ~Fixture() { draft.Free(); }
  model::Model& main() { return owner.Get(); }
  uint16_t* G(int slot) { return rolling.p + slot * 10240; }
  uint16_t* H(int slot) { return last.p + slot * 10240; }
  void Begin(int slot) {
    Check(model::ModelBeginSequence(main(), &seq[slot], nullptr, slot));
    Check(mtp::MtpResetState(draft, nullptr, slot));
  }
  void Prefill(int slot, const std::vector<int32_t>& ids, bool capture = false) {
    while (seq[slot].position < static_cast<int>(ids.size())) {
      const int base = seq[slot].position;
      const int n = std::min(4096, static_cast<int>(ids.size()) - base);
      const bool final = base + n == static_cast<int>(ids.size());
      Check(model::ModelPrefillTextChunk(main(), &seq[slot], ids.data(),
            ids.size(), n, final ? logits.p : nullptr, nullptr, trunk.p,
            model::LogitsRows::kLastRow));
      if (final) bonus[slot] = Argmax(Read(logits.p, 248320u * 2));
      std::vector<int32_t> shifted(n);
      std::vector<int> positions(n);
      for (int i = 0; i < n; ++i) {
        positions[i] = base + i;
        shifted[i] = base + i + 1 < static_cast<int>(ids.size())
            ? ids[base + i + 1] : bonus[slot];
      }
      if (capture) {
        std::vector<uint16_t> frame(size_t(n) * 10240);
        Cuda(cudaMemcpy(frame.data(), trunk.p, frame.size() * 2,
                        cudaMemcpyDeviceToHost));
        reference_trunks.push_back(std::move(frame));
      }
      Check(mtp::MtpDraftExtend(draft, shifted.data(), trunk.p, positions.data(),
                                 n, &d0[slot], G(slot), nullptr, slot, final));
      Cuda(cudaStreamSynchronize(nullptr));
      if (final) Cuda(cudaMemcpy(H(slot), trunk.p + size_t(n - 1) * 10240,
                                  10240 * 2, cudaMemcpyDeviceToDevice));
    }
  }
  // Independently enumerate state using architecture dimensions and explicit
  // slot offsets. Do not call the production SessionStateRanges helper here.
  Image Capture(int slot, int n) {
    Image image;
    auto add = [&](void* p, size_t bytes) {
      image.push_back({p, Read(p, bytes)});
    };
    const size_t token_offset = std::accumulate(caps.begin(), caps.begin()+slot,
                                               size_t{0});
    auto attention = [&](uint16_t* kv, uint16_t* raw, uint16_t* comp) {
      add(kv + token_offset * 1024, size_t(n) * 2048);
      add(raw + token_offset * 128, size_t(n) * 256);
      add(comp + token_offset * 128, size_t(n / 4) * 256);
    };
    for (const auto& layer : main().layers) {
      if (layer.is_full_attention) {
        attention(layer.kv_cache, layer.idx_raw, layer.idx_comp);
      } else {
        add(layer.ssm_state + size_t(slot) * 48 * 128 * 128,
              size_t(48) * 128 * 128 * 4);
        add(layer.conv_state + size_t(slot) * 10240 * 3, 10240 * 3 * 2);
      }
      if (layer.has_ple)
        add(layer.ple_conv_state + size_t(slot) * 10240 * 9, 10240 * 9 * 2);
    }
    for (int row = 0; row < 3; ++row)
      add(main().d_rope_pos + (slot * 3 + row) * 16384, size_t(n) * 4);
    attention(draft.kv_cache, draft.idx_raw, draft.idx_comp);
    add(G(slot), 10240 * 2);
    add(H(slot), 10240 * 2);
    return image;
  }
  void ReplayDraft(int slot, const std::vector<int32_t>& ids, int32_t pending) {
    Check(mtp::MtpResetState(draft, nullptr, slot));
    int base = 0;
    for (const auto& frame : reference_trunks) {
      const int n = frame.size() / 10240;
      Cuda(cudaMemcpy(trunk.p, frame.data(), frame.size()*2,
                      cudaMemcpyHostToDevice));
      std::vector<int32_t> shifted(n);
      std::vector<int> positions(n);
      for (int i = 0; i < n; ++i) {
        positions[i] = base + i;
        shifted[i] = base+i+1 < static_cast<int>(ids.size())
            ? ids[base+i+1] : pending;
      }
      const bool final = base+n == static_cast<int>(ids.size());
      Check(mtp::MtpDraftExtend(draft, shifted.data(), trunk.p, positions.data(),
                                 n, &d0[slot], G(slot), nullptr, slot, final));
      base += n;
    }
    Cuda(cudaStreamSynchronize(nullptr));
  }
  int32_t Step(int slot, std::span<const int32_t> stop, bool complete) {
    const model::ModelSequence* ptr = &seq[slot];
    const uint16_t* in = G(slot);
    uint16_t* out = G(slot);
    int32_t accepted[4], next = -1, next_d0 = -1;
    int count = 0;
    const uint8_t finish = complete;
    Check(mtp::MtpSpeculativeStepMulti(main(), draft, &ptr, &bonus[slot],
        &d0[slot], &in, 1, 3, accepted, &count, &next, &next_d0, &out, nullptr,
        stop, {}, {}, {&finish, 1}));
    const int32_t first_prediction = Argmax(Read(draft.d_ms_vlogits, 248320u*2));
    Cuda(cudaMemcpy(H(slot), draft.d_ms_vtrunk + size_t(count-1)*10240,
                    10240*2, cudaMemcpyDeviceToDevice));
    seq[slot].history.insert(seq[slot].history.end(), accepted, accepted+count);
    seq[slot].position += count;
    bonus[slot] = next;
    d0[slot] = next_d0;
    return first_prediction;
  }
};
}  // namespace

#ifndef Q4T_SESSION_REFERENCE_ONLY
int main(int argc, char** argv) {
  try {
    Require(argc == 3 || argc == 4, "model cache-dir [subset]");
    Fixture f(argv[1], argc == 4 ? argv[3] : "");
    std::filesystem::create_directory(argv[2]);
    chmod(argv[2], 0700);
    server::SessionCache cache;
    Check(cache.Open(argv[2]));
    std::vector<int32_t> prompt(8193);
    for (size_t i = 0; i < prompt.size(); ++i) prompt[i] = 100 + i % 101;
    f.Begin(0); f.Prefill(0, prompt);
    f.Begin(1); f.Prefill(1, prompt, true);
    Equal(f.Capture(0, 8193), f.Capture(1, 8193));
    Require(cache.Acquire("A"), "acquire");
    auto prefix = prompt;
    prefix.push_back(f.bonus[0]);
    std::vector<server::SessionRange> ranges;
    Check(server::SessionStateRanges(f.main(), f.draft, 0, 8193,
                                      f.G(0), f.H(0), &ranges));
    const auto copy = [](void* host, void* state, size_t n, bool restore) {
      const auto e = restore ? cudaMemcpy(state, host, n, cudaMemcpyHostToDevice)
                             : cudaMemcpy(host, state, n, cudaMemcpyDeviceToHost);
      return e == cudaSuccess ? Status() : Status::Fail(cudaGetErrorString(e));
    };
    const auto active = [] { return false; };
    Check(cache.Save("A", prefix, ranges, copy, active, f.d0[0]));
    cache.Release("A", true);
    f.Begin(0); f.Prefill(0, std::vector<int32_t>(8201, 444));
    f.Begin(2); f.Prefill(2, std::vector<int32_t>(257, 888));
    f.Begin(2);
    Require(cache.Acquire("A") && cache.Match("A", prefix) == 8193,
              "restore match");
    Check(server::SessionStateRanges(f.main(), f.draft, 2, 8193,
                                      f.G(2), f.H(2), &ranges));
    Check(cache.Restore("A", ranges, copy, active, &f.d0[2], &f.bonus[2]));
    f.seq[2].history = prompt;
    f.seq[2].position = 8193;
    f.main().rope_delta[2] = 0;
    Equal(f.Capture(2, 8193), f.Capture(1, 8193));
    Require(f.bonus[2] == f.bonus[1] && f.d0[2] == f.d0[1], "host seed");
    // Deliberately replace the predicted next token. Rebuild the independent
    // draft from all saved target frames, versus repairing just the last row.
    const int32_t changed = (f.bonus[2]+17) % 248320;
    f.ReplayDraft(1, prompt, changed);
    const int pos = 8192;
    Check(mtp::MtpDraftExtend(f.draft, &changed, f.H(2), &pos, 1,
                               &f.d0[2], f.G(2), nullptr, 2));
    Equal(f.Capture(2, 8193), f.Capture(1, 8193));
    auto continuation = prompt;
    continuation.push_back(changed);
    continuation.insert(continuation.end(), 16, 555);
    f.seq[1].stage = model::ModelSequence::Stage::kPrefill;
    f.Prefill(1, continuation);
    const auto expected_logits = Read(f.logits.p, 248320u*2);
    f.Prefill(2, continuation);
    Require(Read(f.logits.p, 248320u*2) == expected_logits, "continuation logits");
    Equal(f.Capture(2, 8210), f.Capture(1, 8210));
    // Terminal target acceptance is identical with/without cache. Independently
    // extend its committed shifted row and compare every effective state field.
    f.bonus[1] = f.bonus[2] = 27;  // Controlled legal pending, as legacy oracle.
    const auto before = f.Capture(1, 8210);
    const auto host = f.seq[1];
    const int32_t old_bonus = f.bonus[1], old_d0 = f.d0[1];
    const int32_t terminal = f.Step(1, {}, false);
    Require(terminal != old_bonus, "terminal fixture pending differs from stop");
    Put(before); f.seq[1] = host; f.bonus[1] = old_bonus; f.d0[1] = old_d0;
    f.Step(1, {&terminal, 1}, false);
    Require(f.seq[1].position == 8211 && f.bonus[1] == terminal,
              "controlled terminal prefix");
    const int last_pos = 8210;
    Check(mtp::MtpDraftExtend(f.draft, &terminal, f.H(1), &last_pos, 1,
                               &f.d0[1], f.G(1), nullptr, 1));
    f.Step(2, {&terminal, 1}, true);
    Equal(f.Capture(2, 8211), f.Capture(1, 8211));
    Require(f.d0[1] == f.d0[2], "terminal draft seed");
    cache.Release("A", true);
    std::printf("PASS full_state=1 reused_source_slot=0 restored_slot=2 "
                "prefix=8193 continuation=17 terminal_extend=1 disk_bytes=%zu "
                "head=%s\n", cache.Bytes(), argc == 4 ? "subset" : "full");
    return 0;
  } catch (const std::exception& e) {
    std::fprintf(stderr, "FAIL session state: %s\n", e.what());
    return 1;
  }
}

#endif  // Q4T_SESSION_REFERENCE_ONLY
