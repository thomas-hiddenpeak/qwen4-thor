// Fixed-k3 suffix contract on the actual scheduler entry point (B=1).
// A two-layer controlled target permits exact accepted-prefix state replay.
// Constructed proposals cover each acceptance count without changing weights.
// The independent target reference never verifies or restores checkpoints;
// the independent draft reference only extends committed, shifted inputs.
// This is not whole-model quality or plain/MTP equivalence evidence.
#include "q4t/model/model_owner.h"
#include "q4t/mtp/mtp.h"
#include "q4t/server/mtp_policy.h"
#include "q4t/test.h"

#include <cuda_runtime.h>

#include <algorithm>
#include <array>
#include <bit>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <span>
#include <stdexcept>
#include <string>
#include <vector>

namespace {
using q4t::model::ModelSequence;
using q4t::model::ModelStateSnapshot;
constexpr int k = 3;
constexpr int kRows = 4;
constexpr int kCapacity = 64;
constexpr std::array<int32_t, 4> kPrompt{846, 25, 1203, 321};
const std::string kModelDir =
    "/home/rm01/models/dev/llm/garnermccloud/"
    "Qwen3.8-Flash-Next-NVFP4-SSD-Stream";

void Require(bool ok, const std::string& label) {
  if (!ok) throw std::runtime_error(label);
}
void Check(const q4t::Status& status, const char* label) {
  Require(status.ok(), std::string(label) + ": " + status.message());
}
void Cuda(cudaError_t status, const char* label) {
  Require(status == cudaSuccess,
          std::string(label) + ": " + cudaGetErrorString(status));
}
template <typename T>
std::vector<T> Read(const T* device, size_t count) {
  std::vector<T> result(count);
  if (count)
    Cuda(cudaMemcpy(result.data(), device, count * sizeof(T),
                    cudaMemcpyDeviceToHost),
         "read state");
  return result;
}
template <typename T>
void Write(T* device, const std::vector<T>& data) {
  if (!data.empty())
    Cuda(cudaMemcpy(device, data.data(), data.size() * sizeof(T),
                    cudaMemcpyHostToDevice),
         "write state");
}
template <typename T>
void Exact(const std::vector<T>& actual, const std::vector<T>& expected,
           const char* label) {
  Require(actual.size() == expected.size(), std::string(label) + " shape");
  const bool equal =
      actual.empty() || std::memcmp(actual.data(), expected.data(),
                                    actual.size() * sizeof(T)) == 0;
  if (!equal) {
    size_t first = 0;
    while (first < actual.size() &&
           std::memcmp(&actual[first], &expected[first], sizeof(T)) == 0)
      ++first;
    std::fprintf(stderr, "suffix mismatch=%s first=%zu elements=%zu\n", label,
                 first, actual.size());
  }
  Require(equal, label);
}
int32_t Argmax(const uint16_t* logits, int vocab) {
  int32_t best = 0;
  const auto value = [](uint16_t x) {
    return std::bit_cast<float>(static_cast<uint32_t>(x) << 16);
  };
  for (int i = 0; i < vocab; ++i) {
    Require(std::isfinite(value(logits[i])), "finite target logits");
    if (value(logits[i]) > value(logits[best])) best = i;
  }
  return best;
}
class Buffer {
 public:
  explicit Buffer(size_t elements) {
    Cuda(cudaMalloc(reinterpret_cast<void**>(&data_), elements * 2),
         "allocate test buffer");
  }
  ~Buffer() { cudaFree(data_); }
  uint16_t* data() const { return data_; }

 private:
  uint16_t* data_ = nullptr;
};
struct DraftOwner {
  q4t::mtp::MtpModel model;
  ~DraftOwner() { model.Free(); }
};
struct DraftCache {
  std::vector<uint16_t> kv, raw, compressed;
  std::vector<int> pages, rope;
};
struct Frame {
  std::vector<int32_t> shifted;
  std::vector<int> positions;
  std::vector<uint16_t> trunk;
};

class Fixture {
 public:
  Fixture() : logits_(16u * 248320), trunk_(16u * 10240), g_(10240) {
    q4t::model::ModelConfig cfg;
    cfg.model_dir = kModelDir;
    cfg.index_path = kModelDir + "/model.safetensors.index.json";
    cfg.ple_sidecar = kModelDir + "/ple/qwen3.8-flash-next-ple-fp8.bin";
    cfg.num_layers = 2;
    cfg.max_prefill = 16;
    cfg.max_len = kCapacity;
    cfg.ple_capacity_tokens = 16;
    cfg.max_seq = 1;
    Check(owner_.Load(cfg, nullptr), "load controlled target");
    q4t::mtp::MtpConfig draft_cfg;
    draft_cfg.mtp_dir = kModelDir + "/mtp";
    draft_cfg.max_prefill = 16;
    draft_cfg.max_len = kCapacity;
    draft_cfg.max_seq = 1;
    if (const char* subset = std::getenv("Q4T_TEST_SUBSET_HEAD"))
      draft_cfg.subset_head_dir = subset;
    Check(q4t::mtp::LoadMtp(draft_cfg, main().head.embed_tokens,
                            main().head.lm_head, &draft_.model, nullptr),
          "load actual draft");
    Check(q4t::model::ModelReserveVerifyCheckpoints(main(), k),
          "reserve target checkpoints");
    Check(q4t::mtp::MtpReserveScratch(mtp(), kRows), "reserve MTP scratch");
    if (mtp().subset_ids)
      subset_ids_ = Read(mtp().subset_ids, mtp().DraftVocab());
  }

  q4t::model::Model& main() { return owner_.Get(); }
  q4t::mtp::MtpModel& mtp() { return draft_.model; }
  int vocab() { return main().cfg.vocab; }

  void Start(int32_t controlled_bonus = -1) {
    accepted_batches_.clear();
    frames_.clear();
    Check(q4t::model::ModelBeginSequence(main(), &seq_, nullptr, 0),
          "fresh target sequence");
    Check(q4t::model::ModelPrefill(main(), &seq_, kPrompt.data(), kRows,
                                   logits_.data(), nullptr, trunk_.data()),
          "prompt prefill");
    const auto logits = Read(logits_.data(), size_t(kRows) * vocab());
    bonus_ = controlled_bonus >= 0
                 ? controlled_bonus
                 : Argmax(logits.data() + size_t(kRows - 1) * vocab(), vocab());
    Frame initial;
    initial.shifted.assign(kPrompt.begin() + 1, kPrompt.end());
    initial.shifted.push_back(bonus_);
    initial.positions = {0, 1, 2, 3};
    initial.trunk = Read(trunk_.data(), size_t(kRows) * main().hc_dim());
    frames_.push_back(initial);
    Check(q4t::mtp::MtpResetState(mtp(), nullptr, 0), "fresh draft state");
    Extend(initial, &d0_);
  }

  int32_t OutsideSubset(int32_t different = -1) {
    // Deterministic ascending global ID absent from the 65536-row head.
    // Full-head runs exercise the same legal high-ID input boundary.
    for (int32_t token = vocab() - 1; token >= 0; --token) {
      if (token != different && token != main().cfg.eos_token_id &&
          !std::binary_search(subset_ids_.begin(), subset_ids_.end(), token))
        return token;
    }
    throw std::runtime_error("no token outside the subset");
  }

  std::array<int32_t, kRows> Probe(const std::array<int32_t, k>& drafts,
                                   std::vector<uint16_t>* observed = nullptr) {
    const auto before = Snapshot();
    const std::array<int32_t, kRows> input{bonus_, drafts[0], drafts[1],
                                           drafts[2]};
    const int slot = 0;
    Check(q4t::model::ModelVerifyMulti(main(), input.data(), &seq_.position,
                                       &slot, seq_.history.data(),
                                       static_cast<int>(seq_.history.size()), 1,
                                       kRows, logits_.data(), nullptr),
          "controlled target probe");
    const auto logits = Read(logits_.data(), size_t(kRows) * vocab());
    if (observed) *observed = logits;
    std::array<int32_t, kRows> predictions;
    for (int row = 0; row < kRows; ++row)
      predictions[row] = Argmax(logits.data() + size_t(row) * vocab(), vocab());
    Restore(before);
    return predictions;
  }

  std::array<int32_t, k> Proposal(int acceptance) {
    const int32_t outside = OutsideSubset();
    std::array<int32_t, k> drafts{d0_, outside, outside};
    auto predictions = Probe(drafts);
    drafts[0] =
        acceptance == 0 ? (predictions[0] + 1) % vocab() : predictions[0];
    if (acceptance >= 1) {
      predictions = Probe(drafts);
      drafts[1] =
          acceptance == 1 ? OutsideSubset(predictions[1]) : predictions[1];
    }
    if (acceptance >= 2) {
      predictions = Probe(drafts);
      drafts[2] =
          acceptance == 2 ? OutsideSubset(predictions[2]) : predictions[2];
    }
    // d0 is an API input. Constructing it covers every acceptance boundary;
    // natural switching below separately retains the computed draft d0.
    d0_ = drafts[0];
    return drafts;
  }

  void Step(std::span<const int32_t> suffix, int expected_acceptance = -1,
            std::span<const int32_t> stop = {}) {
    const auto previous = seq_;
    const auto old_g = Read(g_.data(), main().hc_dim());
    const auto old_cache = CaptureCache();
    const int32_t old_bonus = bonus_;
    const int32_t old_d0 = d0_;
    std::vector<uint16_t> expected_verify;
    if (!suffix.empty()) {
      Require(suffix.size() == k, "test suffix size");
      const std::array<int32_t, k> proposal{suffix[0], suffix[1], suffix[2]};
      // Same-shape target probe checks that all global IDs, including
      // rejected non-subset tails, reach verify without remapping.
      Probe(proposal, &expected_verify);
    }
    const ModelSequence* seqs[] = {&seq_};
    const uint16_t* input_g[] = {g_.data()};
    uint16_t* output_g[] = {g_.data()};
    std::array<int32_t, kRows> accepted{-1, -1, -1, -1};
    int count = 0;
    int32_t next = -1, next_d0 = -1;
    Check(q4t::mtp::MtpSpeculativeStepMulti(main(), mtp(), seqs, &bonus_, &d0_,
                                            input_g, 1, k, accepted.data(),
                                            &count, &next, &next_d0, output_g,
                                            nullptr, stop, suffix),
          "actual scheduler suffix step");
    Require(seq_.history == previous.history &&
                seq_.position == previous.position &&
                seq_.stage == previous.stage && seq_.seq_id == previous.seq_id,
            "Multi preserves caller host state");
    const auto proposals = suffix.empty()
                               ? Read(mtp().d_ms_drafts, k)
                               : std::vector(suffix.begin(), suffix.end());
    Require(proposals.front() == old_d0, "first candidate d0 gate");
    const auto logits = Read(mtp().d_ms_vlogits, size_t(kRows) * vocab());
    if (!suffix.empty())
      Exact(logits, expected_verify, "target receives global suffix IDs");
    const auto target_trunk =
        Read(mtp().d_ms_vtrunk, size_t(kRows) * main().hc_dim());
    std::array<int32_t, kRows> predictions;
    for (int row = 0; row < kRows; ++row)
      predictions[row] = Argmax(logits.data() + size_t(row) * vocab(), vocab());
    const auto is_stop = [&](int32_t token) {
      return std::find(stop.begin(), stop.end(), token) != stop.end();
    };
    // Independent prefix definition, not the implementation's accept loop.
    int expected = k;
    for (int prefix = k; prefix >= 0; --prefix) {
      const bool exact = std::equal(
          proposals.begin(), proposals.begin() + prefix, predictions.begin());
      const bool terminal = std::any_of(predictions.begin(),
                                        predictions.begin() + prefix, is_stop);
      if (exact && !terminal) {
        expected = prefix;
        break;
      }
    }
    Require(count == expected + 1 && next == predictions[expected],
            "independent acceptance and correction");
    Require(expected_acceptance < 0 || expected == expected_acceptance,
            "frozen acceptance coverage");
    Require(accepted[0] == old_bonus &&
                std::equal(proposals.begin(), proposals.begin() + expected,
                           accepted.begin() + 1),
            "accepted global prefix");
    const auto actual_main = Snapshot();
    const auto actual_cache = CaptureCache();
    const auto actual_g = Read(g_.data(), main().hc_dim());
    ModelSequence committed = previous;
    committed.position += count;
    committed.history.insert(committed.history.end(), accepted.begin(),
                             accepted.begin() + count);
    const auto actual_boundary = BoundaryLogits(committed, next);
    Restore(actual_main);
    accepted_batches_.emplace_back(accepted.begin(), accepted.begin() + count);
    // Target reference uses only committed inputs, no draft, verify or restore.
    ReplayMain();
    Exact(Snapshot().data, actual_main.data, "independent main prefix state");
    Exact(BoundaryLogits(committed, next), actual_boundary,
          "independent target boundary logits");
    Restore(actual_main);

    if (is_stop(next)) {
      Require(next_d0 == -1, "terminal correction has no next draft");
      Exact(actual_g, old_g, "terminal leaves draft trunk untouched");
      // A suffix terminal does no draft work at all, including no extend.
      if (!suffix.empty()) ExactCache(actual_cache, old_cache, kCapacity);
    } else {
      Frame frame;
      frame.shifted.assign(proposals.begin(), proposals.begin() + expected);
      frame.shifted.push_back(next);
      for (int row = 0; row < count; ++row)
        frame.positions.push_back(previous.position + row);
      frame.trunk.assign(
          target_trunk.begin(),
          target_trunk.begin() + size_t(count) * main().hc_dim());
      frames_.push_back(std::move(frame));
      Check(q4t::mtp::MtpResetState(mtp(), nullptr, 0), "oracle draft reset");
      int32_t independent_d0 = -1;
      for (const auto& committed : frames_) Extend(committed, &independent_d0);
      Require(next_d0 == independent_d0, "independent next_d0");
      Exact(Read(g_.data(), main().hc_dim()), actual_g, "independent next_g");
      ExactCache(CaptureCache(), actual_cache, previous.position + count);
      // Restore actual future rows too; only committed rows are oracle-valid.
      RestoreCache(actual_cache);
      Write(g_.data(), actual_g);
    }
    seq_.position += count;
    seq_.history.insert(seq_.history.end(), accepted.begin(),
                        accepted.begin() + count);
    bonus_ = next;
    d0_ = next_d0;
    std::printf(
        "SUFFIX_STATE source=%s accepted=%d terminal=%d "
        "main_prefix_exact=1 boundary_logits_exact=1 "
        "draft_prefix_exact=1 global_ids=1\n",
        suffix.empty() ? "neural" : "suffix", expected, is_stop(next));
  }

  void InvalidInputs() {
    const auto before = Snapshot();
    const auto cache = CaptureCache();
    const auto g = Read(g_.data(), main().hc_dim());
    const ModelSequence* seqs[] = {&seq_};
    const uint16_t* input_g[] = {g_.data()};
    uint16_t* output_g[] = {g_.data()};
    const auto reject = [&](std::span<const int32_t> suffix, int drafts,
                            std::span<const int32_t> stop = {}) {
      std::array<int32_t, kRows> accepted{};
      int count = 97;
      int32_t next = 97, next_d0 = 97;
      Require(!q4t::mtp::MtpSpeculativeStepMulti(
                   main(), mtp(), seqs, &bonus_, &d0_, input_g, 1, drafts,
                   accepted.data(), &count, &next, &next_d0, output_g, nullptr,
                   stop, suffix)
                   .ok(),
              "unsupported suffix fails");
      Require(count == 0 && next == -1 && next_d0 == -1,
              "invalid suffix clears published outputs");
    };
    const std::array<int32_t, k> valid{d0_, 7, 9};
    reject(std::span(valid).first(2), k);
    reject(valid, 2);
    auto invalid = valid;
    invalid[0] = (d0_ + 1) % vocab();
    reject(invalid, k);
    invalid = valid;
    invalid[1] = -1;
    reject(invalid, k);
    invalid[1] = vocab();
    reject(invalid, k);
    const std::array<int32_t, 1> pending_stop{bonus_};
    reject(valid, k, pending_stop);
    // A lightweight descriptor exercises the B gate before any device or
    // pooled state access; both host output rows are real allocations.
    q4t::mtp::MtpModel two_slots;
    two_slots.max_seq = 2;
    two_slots.k_max = kRows;
    const ModelSequence* two_seqs[] = {&seq_, &seq_};
    const int32_t two_bonus[] = {bonus_, bonus_};
    const int32_t two_d0[] = {d0_, d0_};
    const uint16_t* two_input_g[] = {g_.data(), g_.data()};
    uint16_t* two_output_g[] = {g_.data(), g_.data()};
    std::array<int32_t, 2 * kRows> two_accepted{};
    std::array<int, 2> two_count{97, 97};
    std::array<int32_t, 2> two_next{97, 97}, two_seed{97, 97};
    Require(!q4t::mtp::MtpSpeculativeStepMulti(
                 main(), two_slots, two_seqs, two_bonus, two_d0, two_input_g, 2,
                 k, two_accepted.data(), two_count.data(), two_next.data(),
                 two_seed.data(), two_output_g, nullptr, {}, valid)
                 .ok(),
            "suffix rejects B=2 before device access");
    Require(two_count == std::array<int, 2>{0, 0} &&
                two_next == std::array<int32_t, 2>{-1, -1} &&
                two_seed == std::array<int32_t, 2>{-1, -1},
            "B=2 suffix clears both output rows");
    Exact(Snapshot().data, before.data, "invalid target untouched");
    ExactCache(CaptureCache(), cache, kCapacity);
    Exact(Read(g_.data(), main().hc_dim()), g, "invalid draft untouched");
    std::printf("SUFFIX_INVALID cases=7 fail_closed=1\n");
  }

  std::array<int32_t, k> NaturalSuffix() {
    return {d0_, OutsideSubset(),
            static_cast<int32_t>(main().cfg.eos_token_id)};
  }

 private:
  ModelStateSnapshot Snapshot() {
    ModelStateSnapshot state;
    Check(q4t::model::ModelSnapshotState(main(), &state, nullptr),
          "snapshot target recurrent state");
    Require(state.valid, "valid target snapshot");
    return state;
  }
  void Restore(const ModelStateSnapshot& state) {
    Check(q4t::model::ModelRestoreState(main(), state, nullptr),
          "restore controlled target state");
  }
  std::vector<uint16_t> BoundaryLogits(ModelSequence committed, int32_t next) {
    Check(q4t::model::ModelDecodeStepSeq(main(), &committed, next,
                                         logits_.data(), nullptr, nullptr, 0),
          "accepted-prefix target boundary");
    return Read(logits_.data(), vocab());
  }
  void ReplayMain() {
    ModelSequence reference;
    Check(q4t::model::ModelBeginSequence(main(), &reference, nullptr, 0),
          "independent target begin");
    Check(q4t::model::ModelPrefill(main(), &reference, kPrompt.data(), kRows,
                                   logits_.data(), nullptr),
          "independent target prefill");
    for (const auto& accepted : accepted_batches_) {
      Check(q4t::model::ModelDecodeBatch(
                main(), accepted.data(), static_cast<int>(accepted.size()),
                reference.position, reference.history.data(),
                static_cast<int>(reference.history.size()), logits_.data(),
                nullptr, nullptr, false, 0),
            "independent accepted-only batch");
      reference.position += static_cast<int>(accepted.size());
      reference.history.insert(reference.history.end(), accepted.begin(),
                               accepted.end());
    }
  }
  void Extend(const Frame& frame, int32_t* d0) {
    Write(trunk_.data(), frame.trunk);
    Check(q4t::mtp::MtpDraftExtend(mtp(), frame.shifted.data(), trunk_.data(),
                                   frame.positions.data(),
                                   static_cast<int>(frame.shifted.size()), d0,
                                   g_.data(), nullptr, 0),
          "independent accepted-only draft extend");
  }
  DraftCache CaptureCache() {
    return {Read(mtp().kv_cache, mtp().kv_bytes / 2),
            Read(mtp().idx_raw, mtp().idx_bytes / 2),
            Read(mtp().idx_comp, mtp().idx_bytes / 2),
            Read(mtp().page_table, kCapacity),
            Read(mtp().d_rope_pos, 3u * kCapacity)};
  }
  void RestoreCache(const DraftCache& cache) {
    Write(mtp().kv_cache, cache.kv);
    Write(mtp().idx_raw, cache.raw);
    Write(mtp().idx_comp, cache.compressed);
    Write(mtp().page_table, cache.pages);
    Write(mtp().d_rope_pos, cache.rope);
  }
  void ExactCache(const DraftCache& actual, const DraftCache& expected,
                  int committed) {
    const auto prefix = [](const auto& data, size_t count) {
      return std::vector(data.begin(), data.begin() + count);
    };
    // Identity page mapping is checked before comparing contiguous KV rows.
    for (int position = 0; position < kCapacity; ++position)
      Require(actual.pages[position] == position / q4t::model::kKvPageSize,
              "identity draft page table");
    Exact(actual.pages, expected.pages, "draft page table");
    Exact(actual.rope, expected.rope, "draft RoPE table");
    const size_t kv = size_t(committed) * mtp().cfg.nkv * 2 * mtp().cfg.hd;
    const size_t raw = size_t(committed) * mtp().cfg.idx_head_dim;
    const size_t comp =
        size_t(committed / mtp().cfg.idx_compress) * mtp().cfg.idx_head_dim;
    Exact(prefix(actual.kv, kv), prefix(expected.kv, kv), "draft valid KV");
    Exact(prefix(actual.raw, raw), prefix(expected.raw, raw),
          "draft valid raw");
    Exact(prefix(actual.compressed, comp), prefix(expected.compressed, comp),
          "draft valid compressed index");
  }

  q4t::model::ModelOwner owner_;
  DraftOwner draft_;
  Buffer logits_, trunk_, g_;
  ModelSequence seq_;
  int32_t bonus_ = -1, d0_ = -1;
  std::vector<int32_t> subset_ids_;
  std::vector<std::vector<int32_t>> accepted_batches_;
  std::vector<Frame> frames_;
};
}  // namespace

Q4T_TEST(mtp_suffix_states) {
  q4t::test::GdnRegOff same_gdn_kernel;
  Cuda(cudaSetDevice(0), "CUDA required; no skip");
  Fixture fixture;
  for (int acceptance = 0; acceptance <= k; ++acceptance) {
    fixture.Start();
    const auto proposal = fixture.Proposal(acceptance);
    fixture.Step(proposal, acceptance);
  }
  fixture.Start();
  fixture.InvalidInputs();
  // Uninterrupted production proposals keep the previous computed d0/g.
  // The explicit EOS tail is a global ID, regardless of subset membership.
  for (int step = 0; step < 4; ++step) {
    const std::array<int32_t, 1> eos{
        static_cast<int32_t>(fixture.main().cfg.eos_token_id)};
    const auto suffix = fixture.NaturalSuffix();
    fixture.Step(step % 2 == 0 ? std::span<const int32_t>(suffix)
                               : std::span<const int32_t>(),
                 -1, eos);
  }
  // Synthetic stop ID exercises the same terminal boundary without relying
  // on this two-layer fixture naturally predicting EOS in a short sample.
  fixture.Start(27);
  const auto terminal = fixture.Proposal(3);
  Require(terminal[0] != 27, "frozen terminal pending differs from stop");
  const std::array<int32_t, 1> stop{terminal[0]};
  fixture.Step(terminal, 0, stop);
  // A new request reuses slot zero after terminal, resetting both states.
  fixture.Start();
  fixture.Step(fixture.NaturalSuffix());
  for (int remaining = 1; remaining <= 4; ++remaining) {
    Require(!q4t::server::MtpGenerationStepFits(4, 64, 16, 3, remaining),
            "output tail must decode plainly");
    Require(!q4t::server::MtpGenerationStepFits(64 - remaining, 64, 16, 3, 5),
            "context tail must decode plainly");
  }
  std::printf(
      "SUFFIX_CONTRACT accept=0,1,2,3 switches=3 terminal=1 "
      "slot_reuse=1 length_tail=1 full_target_layers=2 subset=%d\n",
      fixture.mtp().subset_head != nullptr);
  return true;
}
