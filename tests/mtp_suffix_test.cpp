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
  explicit Fixture(int slots = 1, bool uneven = false)
      : logits_(16u * 248320), trunk_(16u * 10240), g_(10240) {
    q4t::model::ModelConfig cfg;
    cfg.model_dir = kModelDir;
    cfg.index_path = kModelDir + "/model.safetensors.index.json";
    cfg.ple_sidecar = kModelDir + "/ple/qwen3.8-flash-next-ple-fp8.bin";
    cfg.num_layers = 2;
    cfg.max_prefill = 16;
    cfg.max_len = kCapacity;
    cfg.ple_capacity_tokens = 16;
    cfg.max_seq = slots;
    if (uneven) cfg.seq_capacities = {64, 48, 48, 48, 64, 64, 64, 64};
    Check(owner_.Load(cfg, nullptr), "load controlled target");
    q4t::mtp::MtpConfig draft_cfg;
    draft_cfg.mtp_dir = kModelDir + "/mtp";
    draft_cfg.max_prefill = 16;
    draft_cfg.max_len = kCapacity;
    draft_cfg.max_seq = slots;
    draft_cfg.seq_capacities = cfg.seq_capacities;
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
    const auto reject_batch = [&](std::span<const q4t::mtp::SuffixProposal> p) {
      Require(!q4t::mtp::MtpSpeculativeStepMulti(
                   main(), two_slots, two_seqs, two_bonus, two_d0, two_input_g,
                   2, k, two_accepted.data(), two_count.data(), two_next.data(),
                   two_seed.data(), two_output_g, nullptr, {}, {}, p).ok(),
              "invalid batch suffix rejected before device access");
      Require(two_count == std::array<int, 2>{0, 0} &&
                  two_next == std::array<int32_t, 2>{-1, -1},
              "invalid batch clears all outputs");
    };
    std::array<q4t::mtp::SuffixProposal, 2> batch{};
    batch[0].ready = true;
    batch[0].tokens = valid;
    reject_batch(std::span(batch).first(1));  // incorrect row count
    batch[0].tokens[0] = (d0_ + 1) % vocab();
    reject_batch(batch);
    batch[0].tokens = valid;
    batch[0].tokens[2] = vocab();
    reject_batch(batch);
    batch[0].tokens = valid;
    reject_batch(batch);  // duplicate slots, even if one proposal is inactive
    Exact(Snapshot().data, before.data, "invalid target untouched");
    ExactCache(CaptureCache(), cache, kCapacity);
    Exact(Read(g_.data(), main().hc_dim()), g, "invalid draft untouched");
    std::printf("SUFFIX_INVALID cases=11 fail_closed=1\n");
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

namespace {
// Capture every pooled slice explicitly: legacy ModelSnapshotState covers
// slot zero only. These copies are test references, never production work.
struct Regions {
  std::vector<std::pair<uint8_t*, size_t>> regions;
  std::vector<std::vector<uint8_t>> ReadAll() const {
    std::vector<std::vector<uint8_t>> result;
    for (const auto& [p, n] : regions) result.push_back(Read(p, n));
    return result;
  }
  void Restore(const std::vector<std::vector<uint8_t>>& data) const {
    for (size_t i = 0; i < regions.size(); ++i)
      Write(regions[i].first, data[i]);
  }
  void Add(void* p, size_t n) {
    if (p) regions.emplace_back(static_cast<uint8_t*>(p), n);
  }
};

class MultiFixture {
 public:
  explicit MultiFixture(bool uneven = false)
      : fixture_(8, uneven),
        logits_(16u * 248320),
        trunk_(16u * 10240),
        g_(4u * 10240),
        packed_(16u * 10240),
        multi_(16u * 10240),
        sample_(16u * 2560) {
    for (auto& layer : main().layers) {
      target_.Add(layer.ssm_state,
                  4u * layer.linear.nv * layer.linear.kd * layer.linear.vd * 4);
      target_.Add(layer.conv_state,
                  4u * layer.linear.in_qkv() * (layer.linear.conv_k - 1) * 2);
      target_.Add(layer.ple_conv_state,
                  4u * layer.hc_dim * (layer.ple.conv_kernel - 1) *
                      layer.ple.conv_dilation * 2);
    }
    const size_t tokens = q4t::runtime::SequenceOffset(mtp().cfg.seq_capacities,
                                                       4, mtp().cfg.max_len);
    cache_.Add(mtp().kv_cache, tokens * 2048);
    cache_.Add(mtp().idx_raw, tokens * 256);
    cache_.Add(mtp().idx_comp, tokens * 256);
    if (mtp().subset_ids) subset_ = Read(mtp().subset_ids, mtp().DraftVocab());
    Start();
  }

  void Start() {
    for (int slot = 0; slot < 4; ++slot) {
      std::array<int32_t, 4> prompt{846 + slot, 25, 1203, 321 + slot};
      Check(q4t::model::ModelBeginSequence(main(), &seqs_[slot], nullptr, slot),
            "multi fresh target");
      Check(q4t::model::ModelPrefill(main(), &seqs_[slot], prompt.data(), 4,
                                     logits_.data(), nullptr, trunk_.data(),
                                     nullptr, slot), "multi prefill");
      const auto lg = Read(logits_.data(), 4u * vocab());
      bonus_[slot] = Argmax(lg.data() + 3u * vocab(), vocab());
      const std::array<int32_t, 4> shifted{
          prompt[1], prompt[2], prompt[3], bonus_[slot]};
      const std::array<int, 4> positions{0, 1, 2, 3};
      Check(q4t::mtp::MtpResetState(mtp(), nullptr, slot), "multi draft reset");
      Check(q4t::mtp::MtpDraftExtend(
                mtp(), shifted.data(), trunk_.data(), positions.data(), 4,
                &d0_[slot], G(slot), nullptr, slot), "multi initial extend");
    }
  }

  void StreamInit() {
    constexpr int T = 33;  // 16 + 16 + 1; last token crosses two chunk edges.
    const int slot = 3;
    const int hc = main().hc_dim();
    Buffer hidden(size_t(T) * hc), expected_g(hc), actual_g(hc);
    std::vector<uint16_t> input(size_t(T) * hc);
    for (size_t i = 0; i < input.size(); ++i)
      input[i] = 0x3d00 + (i * 17 % 256);  // finite deterministic BF16
    Write(hidden.data(), input);
    std::vector<int32_t> ids(T);
    std::vector<int> pos(T);
    for (int i = 0; i < T; ++i) {
      ids[i] = 321 + i;
      pos[i] = i;
    }
    const auto before = cache_.ReadAll();
    int32_t expected = -1, actual = -1;
    Check(q4t::mtp::MtpResetState(mtp(), nullptr, slot), "reference reset");
    Check(q4t::mtp::MtpDraftExtend(mtp(), ids.data(), hidden.data(), pos.data(),
                                   T, &expected, expected_g.data(), nullptr,
                                   slot),
          "legacy init");
    const auto reference = cache_.ReadAll();
    cache_.Restore(before);
    Check(q4t::mtp::MtpResetState(mtp(), nullptr, slot), "stream reset");
    for (int base = 0; base < T; base += 16) {
      const int n = std::min(16, T - base);
      const bool last = base + n == T;
      Check(q4t::mtp::MtpDraftExtend(
                mtp(), ids.data() + base, hidden.data() + size_t(base) * hc,
                pos.data() + base, n, last ? &actual : nullptr,
                last ? actual_g.data() : nullptr, nullptr, slot, last),
            "streamed init");
    }
    const auto result = cache_.ReadAll();
    for (size_t i = 0; i < result.size(); ++i)
      Exact(result[i], reference[i], "stream init cache plus untouched peers");
    Require(actual == expected, "stream init global d0");
    Exact(Read(actual_g.data(), hc), Read(expected_g.data(), hc),
          "stream init g");
    // At the short boundary, fail before any metadata upload or cache write.
    const int invalid_pos = mtp().cfg.Capacity(slot);
    Require(!q4t::mtp::MtpDraftExtend(mtp(), ids.data(), hidden.data(),
                                      &invalid_pos, 1, &actual, actual_g.data(),
                                      nullptr, slot)
                 .ok(),
            "short slot bounds");
    const auto after_rejection = cache_.ReadAll();
    for (size_t i = 0; i < result.size(); ++i)
      Exact(after_rejection[i], result[i], "no writes past short slot");
    Check(q4t::mtp::MtpResetState(mtp(), nullptr, slot),
          "cancelled slot reset");
    const auto reset = cache_.ReadAll();
    const size_t prefix = q4t::runtime::SequenceOffset(mtp().cfg.seq_capacities,
                                                       slot, mtp().cfg.max_len);
    for (int i = 0; i < 3; ++i) {
      const size_t offset = prefix * (i == 0 ? 2048 : 256);
      Exact(std::vector<uint8_t>(reset[i].begin(), reset[i].begin() + offset),
            std::vector<uint8_t>(result[i].begin(), result[i].begin() + offset),
            "reset leaves all preceding slots untouched");
      Require(std::all_of(reset[i].begin() + offset, reset[i].end(),
                          [](uint8_t b) { return b == 0; }),
              "reset own slot");
    }
    std::printf(
        "AGENT_STREAM_INIT chunks=3 cache_exact=1 d0_exact=1 "
        "g_exact=1 bounds=1 isolated_reset=1 subset=%d\n",
        mtp().subset_head != nullptr);
    cache_.Restore(before);
  }

  // The reference creates compact neural rows on CPU, runs MtpForward
  // directly, and never uses production gather/scatter/rolling code.
  void Step(const std::vector<int>& slots, unsigned mask) {
    const int B = static_cast<int>(slots.size());
    const int hc = main().hc_dim();
    const auto before_target = target_.ReadAll();
    const auto before_cache = cache_.ReadAll();
    const auto before_host = seqs_;
    std::vector<q4t::mtp::SuffixProposal> suffix(B);
    std::vector<int32_t> drafts(B * 3, vocab() - 1), bonus(B), seed(B);
    std::vector<int> positions(B), neural;
    std::vector<const ModelSequence*> seq(B);
    std::vector<const uint16_t*> input(B);
    std::vector<uint16_t*> output(B);
    for (int row = 0; row < B; ++row) {
      const int slot = slots[row];
      seq[row] = &seqs_[slot];
      positions[row] = seqs_[slot].position;
      bonus[row] = bonus_[slot];
      seed[row] = d0_[slot];
      input[row] = output[row] = G(slot);
      suffix[row].ready = (mask >> row) & 1;
      if (!suffix[row].ready) neural.push_back(row);
    }
    // Construct simultaneous 0/1/2/3 accepts for suffix rows. Probe only
    // supplies predictions; the state reference below uses accepted inputs.
    for (int col = 0; col < 3; ++col) {
      auto predictions = Probe(slots, bonus, drafts);
      target_.Restore(before_target);
      for (int row = 0; row < B; ++row) {
        if (!suffix[row].ready) continue;
        const int wanted = slots[row] % 4;
        drafts[row * 3 + col] = col < wanted
            ? predictions[row * 4 + col]
            : (col == wanted ? (predictions[row * 4 + col] + 1) % vocab()
                              : vocab() - 1);
      }
    }
    for (int row = 0; row < B; ++row) {
      if (!suffix[row].ready) continue;
      std::copy_n(drafts.begin() + row * 3, 3, suffix[row].tokens.begin());
      seed[row] = drafts[row * 3];  // controlled API input, as single fixture
    }
    const int N = static_cast<int>(neural.size());
    std::vector<uint16_t> rolling;
    std::vector<int32_t> ids;
    std::vector<int> nslots, npos;
    for (int row : neural) {
      auto hidden = Read(G(slots[row]), hc);
      rolling.insert(rolling.end(), hidden.begin(), hidden.end());
      drafts[row * 3] = seed[row];
      ids.push_back(seed[row]);
      nslots.push_back(slots[row]);
      npos.push_back(positions[row]);
    }
    for (int col = 1; col < 3 && N; ++col) {
      auto result = Forward(ids, npos, nslots, rolling);
      rolling = std::move(result.second);
      for (int n = 0; n < N; ++n) {
        ids[n] = DraftToken(result.first.data() + size_t(n) * mtp().DraftVocab());
        drafts[neural[n] * 3 + col] = ids[n];
        ++npos[n];
      }
    }
    cache_.Restore(before_cache);
    const auto predictions = Probe(slots, bonus, drafts);
    const auto expected_verify = Read(logits_.data(), size_t(B * 4) * vocab());
    target_.Restore(before_target);
    std::vector<int32_t> accepted(B * 4), next(B), next_d0(B);
    std::vector<int> count(B);
    Check(q4t::mtp::MtpSpeculativeStepMulti(
              main(), mtp(), seq.data(), bonus.data(), seed.data(), input.data(),
              B, 3, accepted.data(), count.data(), next.data(), next_d0.data(),
              output.data(), nullptr, {}, {}, suffix), "mixed actual step");
    if (N) Exact(Read(mtp().d_ms_g_pool, size_t(N) * hc), rolling,
                 "packed neural trunk independent reference");
    Exact(Read(mtp().d_ms_vlogits, size_t(B * 4) * vocab()), expected_verify,
          "mixed global IDs and neural candidates");
    const auto actual_target = target_.ReadAll();
    const auto actual_cache = cache_.ReadAll();
    const auto actual_g = Read(g_.data(), 4u * hc);
    const auto verify_trunk = Read(mtp().d_ms_vtrunk, size_t(B * 4) * hc);
    std::vector<int32_t> extend_ids;
    std::vector<int> extend_pos, extend_slots, last;
    std::vector<uint16_t> extend_hidden;
    target_.Restore(before_target);
    std::vector<int32_t> reference_ids, reference_prior;
    std::vector<int> reference_lengths, reference_slots, reference_positions;
    const int hist_width = main().ple_hash.ngram_size - 1;
    for (int row = 0; row < B; ++row) {
      const int slot = slots[row];
      int accept = 0;
      while (accept < 3 && drafts[row * 3 + accept] ==
                               predictions[row * 4 + accept]) ++accept;
      Require(count[row] == accept + 1 && next[row] ==
                  predictions[row * 4 + accept], "mixed independent acceptance");
      if (suffix[row].ready)
        Require(accept == slot % 4, "multi acceptance coverage");
      Require(accepted[row * 4] == bonus[row], "multi pending commit");
      Require(std::equal(drafts.begin() + row * 3,
                          drafts.begin() + row * 3 + accept,
                          accepted.begin() + row * 4 + 1), "multi accepted IDs");
      Require(seqs_[slot].history == before_host[slot].history &&
                  seqs_[slot].position == before_host[slot].position,
              "multi leaves caller state unchanged");
      // Keep the same dense M and packed prefix row offsets as verify.
      // Only accepted inputs execute against the real sequence. Rejected
      // tails belong to fresh, isolated padding slots (4..7), never to it.
      reference_lengths.push_back(count[row]);
      reference_slots.push_back(slot);
      reference_positions.push_back(positions[row]);
      reference_ids.insert(reference_ids.end(), accepted.begin() + row * 4,
                           accepted.begin() + row * 4 + count[row]);
      for (int h = positions[row] - hist_width; h < positions[row]; ++h)
        reference_prior.push_back(h >= 0 ? seqs_[slot].history[h]
                                         : main().cfg.eos_token_id);
      if (count[row] < 4) {
        reference_lengths.push_back(4 - count[row]);
        reference_slots.push_back(4 + row);
        reference_positions.push_back(0);
        for (int j = count[row]; j < 4; ++j)
          reference_ids.push_back(drafts[row * 3 + j - 1]);
        reference_prior.insert(reference_prior.end(), hist_width,
                                main().cfg.eos_token_id);
      }
      for (int j = 0; j < count[row]; ++j) {
        extend_ids.push_back(j < accept ? drafts[row * 3 + j] : next[row]);
        extend_pos.push_back(positions[row] + j);
        extend_slots.push_back(slot);
        const auto start = verify_trunk.begin() + size_t(row * 4 + j) * hc;
        extend_hidden.insert(extend_hidden.end(), start, start + hc);
      }
      last.push_back(static_cast<int>(extend_ids.size()) - 1);
    }
    Check(q4t::model::ModelMixedBatch(main(), reference_ids.data(),
              reference_lengths.data(), reference_slots.data(),
              reference_positions.data(), reference_prior.data(),
              static_cast<int>(reference_lengths.size()), logits_.data(),
              nullptr), "independent accepted-only packed target");
    const auto reference_target = target_.ReadAll();
    for (size_t i = 0; i < actual_target.size(); ++i)
      Exact(actual_target[i], reference_target[i], "all pooled target prefixes");
    target_.Restore(actual_target);
    // Discard ALL speculative writes, then extend only the accepted shifted
    // inputs using the same total M. This reference does not call Multi.
    cache_.Restore(before_cache);
    const auto extended = Forward(extend_ids, extend_pos, extend_slots,
                                   extend_hidden);
    for (int row = 0; row < B; ++row) {
      Require(next_d0[row] == DraftToken(extended.first.data() +
                  size_t(last[row]) * mtp().DraftVocab()), "mixed next d0");
      const auto start = extended.second.begin() + size_t(last[row]) * hc;
      Exact(Read(G(slots[row]), hc), std::vector<uint16_t>(start, start + hc),
            "mixed next g");
      const int committed = positions[row] + count[row];
      const auto reference_cache = cache_.ReadAll();
      const size_t lengths[] = {
          size_t(committed) * mtp().cfg.nkv * 2 * mtp().cfg.hd * 2,
          size_t(committed) * mtp().cfg.idx_head_dim * 2,
          size_t(committed / mtp().cfg.idx_compress) * mtp().cfg.idx_head_dim * 2};
      for (int i = 0; i < 3; ++i) {
        const size_t offset =
            q4t::runtime::SequenceOffset(mtp().cfg.seq_capacities, slots[row],
                                         mtp().cfg.max_len) *
            (i == 0 ? 2048 : 256);
        Exact(std::vector<uint8_t>(actual_cache[i].begin() + offset,
                                   actual_cache[i].begin() + offset + lengths[i]),
              std::vector<uint8_t>(reference_cache[i].begin() + offset,
                                   reference_cache[i].begin() + offset + lengths[i]),
              "mixed committed draft cache");
      }
      seqs_[slots[row]].position += count[row];
      seqs_[slots[row]].history.insert(seqs_[slots[row]].history.end(),
          accepted.begin() + row * 4, accepted.begin() + row * 4 + count[row]);
      bonus_[slots[row]] = next[row];
      d0_[slots[row]] = next_d0[row];
    }
    cache_.Restore(actual_cache);
    Write(g_.data(), actual_g);
    std::printf("SUFFIX_MULTI B=%d neural=%d mask=%u target_exact=1 "
                "draft_exact=1 packed_trunk_exact=1\n", B, N, mask);
  }

 private:
  q4t::model::Model& main() { return fixture_.main(); }
  q4t::mtp::MtpModel& mtp() { return fixture_.mtp(); }
  int vocab() { return main().cfg.vocab; }
  uint16_t* G(int slot) { return g_.data() + size_t(slot) * main().hc_dim(); }
  int32_t DraftToken(const uint16_t* row) {
    const int local = Argmax(row, mtp().DraftVocab());
    return subset_.empty() ? local : subset_[local];
  }
  std::vector<int32_t> Probe(const std::vector<int>& slots,
                             const std::vector<int32_t>& bonus,
                             const std::vector<int32_t>& drafts) {
    const int B = static_cast<int>(slots.size());
    int max_pos = 0;
    for (int slot : slots) max_pos = std::max(max_pos, seqs_[slot].position);
    std::vector<int32_t> input, history(size_t(B) * max_pos, 0);
    std::vector<int> positions;
    for (int row = 0; row < B; ++row) {
      positions.push_back(seqs_[slots[row]].position);
      std::copy(seqs_[slots[row]].history.begin(), seqs_[slots[row]].history.end(),
                 history.begin() + size_t(row) * max_pos);
      input.push_back(bonus[row]);
      input.insert(input.end(), drafts.begin() + row * 3,
                    drafts.begin() + row * 3 + 3);
    }
    Check(q4t::model::ModelVerifyMulti(main(), input.data(), positions.data(),
              slots.data(), history.data(), max_pos, B, 4, logits_.data(), nullptr),
          "multi same-shape probe");
    const auto lg = Read(logits_.data(), size_t(B * 4) * vocab());
    std::vector<int32_t> result;
    for (int row = 0; row < B * 4; ++row)
      result.push_back(Argmax(lg.data() + size_t(row) * vocab(), vocab()));
    return result;
  }
  std::pair<std::vector<uint16_t>, std::vector<uint16_t>> Forward(
      const std::vector<int32_t>& ids, const std::vector<int>& positions,
      const std::vector<int>& slots, const std::vector<uint16_t>& hidden) {
    Write(mtp().d_ms_ext_ids, ids);
    Write(mtp().d_ms_ext_pos, positions);
    Write(mtp().d_ms_ext_seq, slots);
    Write(packed_.data(), hidden);
    Check(q4t::mtp::MtpForward(mtp(), mtp().d_ms_ext_ids, mtp().d_ms_ext_pos,
              packed_.data(), sample_.data(), multi_.data(), logits_.data(),
              static_cast<int>(ids.size()), nullptr, mtp().d_ms_ext_seq, true,
              *std::max_element(positions.begin(), positions.end())),
          "independent packed draft forward");
    return {Read(logits_.data(), ids.size() * mtp().DraftVocab()),
            Read(multi_.data(), ids.size() * main().hc_dim())};
  }
  Fixture fixture_;
  Buffer logits_, trunk_, g_, packed_, multi_, sample_;
  Regions target_, cache_;
  std::array<ModelSequence, 4> seqs_;
  std::array<int32_t, 4> bonus_{}, d0_{};
  std::vector<int32_t> subset_;
};
}  // namespace

Q4T_TEST(mtp_suffix_multi_states) {
  q4t::test::GdnRegOff same_gdn_kernel;
  Cuda(cudaSetDevice(0), "CUDA required; no skip");
  MultiFixture f;
  f.Step({3, 1, 0, 2}, 15);  // all-hit, accept 3/1/0/2
  f.Step({3, 1, 0, 2}, 5);   // two neural rows, sparse pooled slots
  f.Step({2, 0, 3, 1}, 0);   // all-neural, reordered slots
  f.Step({1, 3, 2, 0}, 10);  // opposite mixed membership
  f.Step({3, 1}, 1);        // contraction, one neural row in nonzero slot
  f.Step({3}, 0);           // lone remaining nonzero slot
  f.Start();               // all slots reused by new histories
  f.Step({2, 3, 1, 0}, 6);
  return true;
}

Q4T_TEST(mtp_suffix_agent_states) {
  q4t::test::GdnRegOff same_gdn_kernel;
  Cuda(cudaSetDevice(0), "CUDA required; no skip");
  MultiFixture f(true);
  f.StreamInit();
  f.Step({3, 1, 0, 2}, 15);
  f.Step({3, 1, 0, 2}, 5);
  f.Step({2, 0, 3, 1}, 0);
  f.Step({1, 3, 2, 0}, 10);
  f.Step({3, 1}, 1);
  f.Step({3}, 0);
  f.Start();
  f.Step({2, 3, 1, 0}, 6);
  return true;
}
