// Fixed last-row draft-initialization diagnostic. Run only after HTTP E2E.
// The same candidate binary's retained all-rows contract is the structural
// reference; this is not a comparison against old executable machine code.
// All paths use identical chunk boundaries and full upstream row shapes.
// Real two-layer main trunk -> real MTP mixer -> actual BF16 lm_head inputs.
// This finite fixture does not prove full-model or long-matrix equivalence.
#include "q4t/model/model_owner.h"
#include "q4t/mtp/mtp.h"
#include "q4t/test.h"

#include <cuda_runtime.h>

#include <algorithm>
#include <array>
#include <bit>
#include <cfenv>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <limits>
#include <stdexcept>
#include <string>
#include <vector>

namespace {
using q4t::model::LogitsRows;
using q4t::mtp::MtpModel;
constexpr int kChunk = 8, kScratch = 4, kPrompt = 16;
constexpr int kHs = 2560, kVocab = 248320;
constexpr std::array<int, 6> kLengths{1, 4, 8, 9, 10, 16};
constexpr size_t kGuard = 128;  // Preserve cudaMalloc's 256-byte alignment.
constexpr uint16_t kCanary = 0x5a5a, kUnwritten = 0x7fc1;
constexpr size_t kArtifactLimit = 64u * 1024u * 1024u;

void Require(bool condition, const std::string& message) {
  if (!condition) throw std::runtime_error(message);
}
void RequireStatus(const q4t::Status& status, const char* operation) {
  Require(status.ok(), std::string(operation) + ": " + status.message());
}
void RequireCuda(cudaError_t status, const char* operation) {
  Require(status == cudaSuccess,
          std::string(operation) + ": " + cudaGetErrorString(status));
}
template <typename T>
class DeviceBuffer {
 public:
  explicit DeviceBuffer(size_t elements) {
    RequireCuda(
        cudaMalloc(reinterpret_cast<void**>(&data_), elements * sizeof(T)),
        "allocate diagnostic buffer");
  }
  ~DeviceBuffer() {
    if (cudaFree(data_) != cudaSuccess) std::abort();
  }
  DeviceBuffer(const DeviceBuffer&) = delete;
  DeviceBuffer& operator=(const DeviceBuffer&) = delete;
  T* data() const { return data_; }

 private:
  T* data_ = nullptr;
};
struct DraftOwner {
  MtpModel model;
  ~DraftOwner() { model.Free(); }
};
template <typename T>
std::vector<T> Read(const T* pointer, size_t count) {
  std::vector<T> result(count);
  RequireCuda(cudaMemcpy(result.data(), pointer, count * sizeof(T),
                         cudaMemcpyDeviceToHost),
              "read diagnostic output");
  return result;
}
double Value(uint16_t bits) {
  return std::bit_cast<float>(static_cast<uint32_t>(bits) << 16);
}
void Finite(const std::vector<uint16_t>& values, const std::string& label) {
  for (uint16_t value : values)
    if (!std::isfinite(Value(value)))
      throw std::runtime_error("non-finite BF16: " + label);
}
template <typename T>
bool Compare(const std::string& label, const std::vector<T>& actual,
             const std::vector<T>& expected) {
  Require(actual.size() == expected.size(), "shape mismatch: " + label);
  size_t unequal = 0, first = actual.size();
  for (size_t i = 0; i < actual.size(); ++i) {
    if (std::memcmp(&actual[i], &expected[i], sizeof(T)) == 0) continue;
    if (first == actual.size()) first = i;
    ++unequal;
  }
  std::printf(
      "  init_compare=%s elements=%zu width=%zu unequal=%zu first=%zu\n",
      label.c_str(), actual.size(), sizeof(T), unequal, first);
  return unequal == 0;
}
int32_t Argmax(const std::vector<uint16_t>& logits) {
  Require(logits.size() == kVocab, "argmax requires the complete vocabulary");
  Finite(logits, "argmax");
  int32_t best = 0;
  for (int i = 1; i < kVocab; ++i)
    if (Value(logits[i]) > Value(logits[best])) best = i;
  return best;  // Lowest ID wins exact ties.
}
void TopFive(const std::string& label, const std::vector<uint16_t>& logits) {
  std::array<int, 5> top{-1, -1, -1, -1, -1};
  for (int i = 0; i < kVocab; ++i) {
    for (size_t j = 0; j < top.size(); ++j) {
      if (top[j] >= 0 && Value(logits[i]) <= Value(logits[top[j]])) continue;
      for (size_t k = top.size() - 1; k > j; --k) top[k] = top[k - 1];
      top[j] = i;
      break;
    }
  }
  std::printf("  init_top5=%s margin=%.17g", label.c_str(),
              Value(logits[top[0]]) - Value(logits[top[1]]));
  for (int id : top)
    std::printf(" id=%d value=%.17g bits=%04x", id, Value(logits[id]),
                static_cast<unsigned>(logits[id]));
  std::printf("\n");
}
class Evidence {
 public:
  explicit Evidence(const char* path) : directory_(path) {
    Require(std::filesystem::create_directory(directory_),
            "Q4T_MTP_INIT_LAST_DIR must name a new directory");
  }
  template <typename T>
  void Save(const std::string& name, const std::vector<T>& values) {
    const size_t bytes = values.size() * sizeof(T);
    Require(bytes <= kArtifactLimit - bytes_, "artifact budget exceeded");
    std::ofstream file(directory_ / name, std::ios::binary);
    file.exceptions(std::ios::failbit | std::ios::badbit);
    file.write(reinterpret_cast<const char*>(values.data()),
               static_cast<std::streamsize>(bytes));
    file.close();
    bytes_ += bytes;
  }
  size_t bytes() const { return bytes_; }

 private:
  std::filesystem::path directory_;
  size_t bytes_ = 0;
};
class Guarded {
 public:
  explicit Guarded(size_t elements)
      : elements_(elements), storage_(elements + 2 * kGuard) {
    std::vector<uint16_t> values(elements + 2 * kGuard, kCanary);
    std::fill(values.begin() + kGuard, values.end() - kGuard, kUnwritten);
    RequireCuda(cudaMemcpy(storage_.data(), values.data(), values.size() * 2,
                           cudaMemcpyHostToDevice),
                "initialize output guards");
  }
  uint16_t* data() const { return storage_.data() + kGuard; }
  std::vector<uint16_t> Check(const std::string& label, bool written) const {
    const auto values = Read(storage_.data(), elements_ + 2 * kGuard);
    for (size_t i = 0; i < kGuard; ++i)
      Require(values[i] == kCanary && values[kGuard + elements_ + i] == kCanary,
              "output guard changed: " + label);
    std::vector<uint16_t> payload(values.begin() + kGuard,
                                  values.end() - kGuard);
    if (written) {
      Finite(payload, label);
    } else {
      Require(std::all_of(payload.begin(), payload.end(),
                          [](uint16_t v) { return v == kUnwritten; }),
              "compute_logits=false wrote output: " + label);
    }
    return payload;
  }

 private:
  size_t elements_;
  DeviceBuffer<uint16_t> storage_;
};
struct Cache {
  std::vector<uint16_t> kv, raw, compressed;
  std::vector<int> pages, rope;
};
Cache CaptureCache(const MtpModel& model) {
  Require(model.max_seq == 1, "cache capture requires S1");
  Cache result{Read(model.kv_cache, model.kv_bytes / 2),
               Read(model.idx_raw, model.idx_bytes / 2),
               Read(model.idx_comp, model.idx_bytes / 2),
               Read(model.page_table, model.cfg.max_len),
               Read(model.d_rope_pos, 3u * model.cfg.max_len)};
  Finite(result.kv, "KV");
  Finite(result.raw, "index raw");
  Finite(result.compressed, "index compressed");
  return result;
}
bool CompareCache(const std::string& label, const Cache& a, const Cache& b) {
  bool exact = Compare(label + ".kv", a.kv, b.kv);
  exact &= Compare(label + ".index_raw", a.raw, b.raw);
  exact &= Compare(label + ".index_comp", a.compressed, b.compressed);
  exact &= Compare(label + ".pages", a.pages, b.pages);
  exact &= Compare(label + ".rope", a.rope, b.rope);
  return exact;
}
struct Observation {
  std::vector<uint16_t> sample, multi, last_logits, final_logits;
  Cache cache;
};
void SaveObservation(Evidence& evidence, const std::string& label,
                     const Observation& result) {
  evidence.Save(label + ".sample.bf16", result.sample);
  evidence.Save(label + ".multi.bf16", result.multi);
  evidence.Save(label + ".last_logits.bf16", result.last_logits);
}
Observation Manual(const MtpModel& model, const std::vector<int32_t>& ids,
                   const std::vector<int>& positions, const uint16_t* trunk,
                   int rows, LogitsRows mode, const std::string& label) {
  RequireStatus(q4t::mtp::MtpResetState(model, nullptr, 0), "reset manual");
  Observation result;
  DeviceBuffer<int32_t> input(kChunk);
  for (int base = 0; base < rows; base += kChunk) {
    const int count = std::min(kChunk, rows - base);
    const bool final = base + count == rows;
    Guarded sample(static_cast<size_t>(count) * model.cfg.hs);
    Guarded multi(static_cast<size_t>(count) * model.hc_dim());
    const size_t output_rows = mode == LogitsRows::kLastRow ? 1 : count;
    Guarded logits(output_rows * model.cfg.vocab);
    RequireCuda(cudaMemcpy(input.data(), ids.data() + base,
                           static_cast<size_t>(count) * sizeof(int32_t),
                           cudaMemcpyHostToDevice),
                "manual ids");
    const auto* hidden = trunk + static_cast<size_t>(base) * model.hc_dim();
    if (mode == LogitsRows::kAllRows) {
      // Exercise the retained default argument, not an explicit All alias.
      RequireStatus(
          q4t::mtp::MtpForward(model, input.data(), positions.data() + base,
                               hidden, sample.data(), multi.data(),
                               logits.data(), count, nullptr, nullptr, final),
          "manual default all-rows forward");
    } else {
      RequireStatus(q4t::mtp::MtpForward(
                        model, input.data(), positions.data() + base, hidden,
                        sample.data(), multi.data(), logits.data(), count,
                        nullptr, nullptr, final, LogitsRows::kLastRow),
                    "manual compact last-row forward");
    }
    RequireCuda(cudaStreamSynchronize(nullptr), "manual completion");
    const auto sample_values = sample.Check(label + ".sample", true);
    const auto multi_values = multi.Check(label + ".multi", true);
    result.sample.insert(result.sample.end(), sample_values.begin(),
                         sample_values.end());
    result.multi.insert(result.multi.end(), multi_values.begin(),
                        multi_values.end());
    const auto logit_values = logits.Check(label + ".logits", final);
    if (final) {
      result.final_logits = logit_values;
      result.last_logits.assign(logit_values.end() - model.cfg.vocab,
                                logit_values.end());
    }
  }
  result.cache = CaptureCache(model);
  return result;
}
bool Wrapper(const MtpModel& model, const std::vector<int32_t>& ids,
             const std::vector<int>& positions, const uint16_t* trunk, int rows,
             bool explicit_last, const Observation& expected,
             Evidence& evidence, const std::string& label) {
  RequireStatus(q4t::mtp::MtpResetState(model, nullptr, 0), "reset wrapper");
  if (rows <= kScratch)
    RequireCuda(cudaMemset(model.d_spec_logits, 0xff,
                           static_cast<size_t>(kScratch) * model.cfg.vocab * 2),
                "poison scratch logits before independent wrapper call");
  Guarded output(model.hc_dim());
  int32_t seed = -1;
  if (explicit_last) {
    RequireStatus(q4t::mtp::MtpDraftExtend(
                      model, ids.data(), trunk, positions.data(), rows, &seed,
                      output.data(), nullptr, 0, LogitsRows::kLastRow),
                  "wrapper explicit last row");
  } else {
    RequireStatus(
        q4t::mtp::MtpDraftExtend(model, ids.data(), trunk, positions.data(),
                                 rows, &seed, output.data(), nullptr, 0),
        "wrapper retained default all rows");
  }
  RequireCuda(cudaStreamSynchronize(nullptr), "wrapper completion");
  const auto g = output.Check(label + ".out_g", true);
  evidence.Save(label + ".out_g.bf16", g);
  evidence.Save(label + ".seed.i32", std::vector<int32_t>{seed});
  const std::vector<uint16_t> expected_g(expected.multi.end() - model.hc_dim(),
                                         expected.multi.end());
  bool exact = Compare(label + ".out_g", g, expected_g);
  exact &= CompareCache(label, CaptureCache(model), expected.cache);
  const int32_t host_seed = Argmax(expected.last_logits);
  exact &= seed == host_seed;
  if (rows <= kScratch) {
    // Last is deliberately ignored by the wrapper's legacy scratch branch.
    const auto sample =
        Read(model.d_spec_sample, static_cast<size_t>(rows) * model.cfg.hs);
    const auto multi =
        Read(model.d_spec_multi, static_cast<size_t>(rows) * model.hc_dim());
    const auto logits =
        Read(model.d_spec_logits, static_cast<size_t>(rows) * model.cfg.vocab);
    Finite(sample, label + ".scratch_sample");
    Finite(multi, label + ".scratch_multi");
    Finite(logits, label + ".scratch_logits");
    exact &= Compare(label + ".scratch_sample", sample, expected.sample);
    exact &= Compare(label + ".scratch_multi", multi, expected.multi);
    exact &=
        Compare(label + ".scratch_all_logits", logits, expected.final_logits);
  }
  std::printf(
      "MTP_INIT_WRAPPER label=%s T=%d scratch=%d seed=%d "
      "host_seed=%d exact=%d\n",
      label.c_str(), rows, rows <= kScratch, seed, host_seed, exact);
  return exact;
}

double Up(double value) {
  return std::nextafter(value, std::numeric_limits<double>::infinity());
}
double Down(double value) {
  return std::nextafter(value, -std::numeric_limits<double>::infinity());
}
double GammaUpper(int operations, int precision) {
  const double nu = operations * std::ldexp(1.0, -precision);
  Require(nu < 1, "invalid analytic gamma domain");
  return Up(nu / (1 - nu));
}
bool NormalOrZero(uint16_t bits) {
  const uint16_t magnitude = bits & 0x7fff;
  return magnitude == 0 || (magnitude >= 0x0080 && magnitude < 0x7f80);
}
// Direct binary64 -> BF16 RNE. Casting through FP32 could double-round an
// interval endpoint. BF16 values and midpoints are exactly binary64 values.
uint16_t RoundBf16(double value) {
  Require(std::isfinite(value), "non-finite rounding endpoint");
  const uint16_t sign = std::signbit(value) ? 0x8000 : 0;
  const double magnitude = std::abs(value);
  constexpr uint16_t kMax = 0x7f7f;
  if (magnitude > Value(kMax)) {
    const double threshold = std::ldexp(1.0, 128) - std::ldexp(1.0, 119);
    return sign | (magnitude >= threshold ? 0x7f80 : kMax);
  }
  int low = 0, high = kMax;
  while (low < high) {
    const int middle = low + (high - low) / 2;
    if (Value(static_cast<uint16_t>(middle)) < magnitude)
      low = middle + 1;
    else
      high = middle;
  }
  if (low == 0 || Value(static_cast<uint16_t>(low)) == magnitude)
    return sign | static_cast<uint16_t>(low);
  const double midpoint = (Value(static_cast<uint16_t>(low - 1)) +
                           Value(static_cast<uint16_t>(low))) *
                          0.5;
  if (magnitude < midpoint || (magnitude == midpoint && (low & 1))) --low;
  return sign | static_cast<uint16_t>(low);
}
struct NumericCase {
  int rows;
  std::vector<double> x;
  double min_nonzero = std::numeric_limits<double>::infinity();
  std::vector<uint16_t> all, last, lower, upper;
  std::vector<double> reference;
  size_t all_outside = 0, last_outside = 0, unequal = 0;
  double max_difference = 0, max_fp32_bound = 0;
};
NumericCase NumericInput(int rows, const Observation& all,
                         const Observation& last) {
  NumericCase result;
  result.rows = rows;
  result.all = all.last_logits;
  result.last = last.last_logits;
  for (auto it = all.sample.end() - kHs; it != all.sample.end(); ++it) {
    Require(NormalOrZero(*it), "head input outside frozen normal/zero domain");
    const double value = Value(*it);
    result.x.push_back(value);
    if (value != 0)
      result.min_nonzero = std::min(result.min_nonzero, std::abs(value));
  }
  result.lower.resize(kVocab);
  result.upper.resize(kVocab);
  result.reference.resize(kVocab);
  return result;
}
bool CheckNumeric(const MtpModel& model, std::vector<NumericCase>& cases,
                  Evidence& evidence) {
  Require(cases.size() == 4 && cases[0].rows == 4 && cases[1].rows == 8 &&
              cases[2].rows == 10 && cases[3].rows == 16,
          "numeric case identity changed");
  Require(std::fegetround() == FE_TONEAREST &&
              std::numeric_limits<double>::is_iec559 &&
              std::numeric_limits<double>::digits == 53,
          "FP64 reference requires IEEE nearest rounding");
  // Freeze before examining differences. BF16 products have <=16 significant
  // bits. FP32 alpha=1/beta=0 accumulation admits gamma_(K+2)*sum_abs,
  // independently of the chosen reduction order. Enclose the FP64 dot's own
  // error with gamma_K. The separately rounded positive sum is bounded by
  // sum_abs_hat/(1-gamma_K), with every final bound rounded outwards.
  const double gamma32 = GammaUpper(kHs + 2, 24);
  const double gamma64 = GammaUpper(kHs, 53);
  const double abs_denominator = Down(1 - gamma64);
  constexpr int kWeightRows = 1024;
  std::vector<uint16_t> weights(static_cast<size_t>(kWeightRows) * kHs);
  std::array<double, kHs> decoded;
  std::printf(
      "MTP_INIT_NUMERIC cases=4 vocab=%d K=%d gamma32=%.17g "
      "gamma64=%.17g weight_block_rows=%d payload_saved=0\n",
      kVocab, kHs, gamma32, gamma64, kWeightRows);
  std::fflush(stdout);
  for (int base = 0; base < kVocab; base += kWeightRows) {
    const int count = std::min(kWeightRows, kVocab - base);
    RequireCuda(cudaMemcpy(weights.data(),
                           model.lm_head + static_cast<size_t>(base) * kHs,
                           static_cast<size_t>(count) * kHs * 2,
                           cudaMemcpyDeviceToHost),
                "read loaded lm_head block, no checkpoint payload audit");
    for (int row = 0; row < count; ++row) {
      double min_weight = std::numeric_limits<double>::infinity();
      for (int k = 0; k < kHs; ++k) {
        const uint16_t bits = weights[static_cast<size_t>(row) * kHs + k];
        if (!NormalOrZero(bits))
          throw std::runtime_error("weight outside frozen normal/zero domain");
        decoded[k] = Value(bits);
        if (decoded[k] != 0)
          min_weight = std::min(min_weight, std::abs(decoded[k]));
      }
      const int id = base + row;
      for (auto& c : cases) {
        // A product with <=16 significant bits and magnitude >=2^-111 has
        // a bit grid >=2^-126. Thus every FP32 partial sum is zero or normal
        // for any reduction order, without assuming subnormal/FTZ behavior.
        if (std::isfinite(min_weight) && std::isfinite(c.min_nonzero))
          Require(min_weight * c.min_nonzero >= std::ldexp(1.0, -111),
                  "product grid outside frozen FP32 underflow domain");
        double reference = 0, sum_abs = 0;
        for (int k = 0; k < kHs; ++k) {
          const double product = decoded[k] * c.x[k];
          reference += product;
          sum_abs += std::abs(product);
        }
        const double abs_upper = Up(sum_abs / abs_denominator);
        const double error32 = Up(gamma32 * abs_upper);
        const double error64 = Up(gamma64 * abs_upper);
        const double error = Up(error32 + error64);
        Require(std::isfinite(reference) && std::isfinite(abs_upper) &&
                    Up(abs_upper + error) <
                        static_cast<double>(std::numeric_limits<float>::max()),
                "sum outside frozen FP32 overflow domain");
        c.reference[id] = reference;
        c.lower[id] = RoundBf16(Down(reference - error));
        c.upper[id] = RoundBf16(Up(reference + error));
        const double low = Value(c.lower[id]), high = Value(c.upper[id]);
        Require(std::isfinite(low) && std::isfinite(high),
                "envelope outside finite BF16 domain");
        const double old_value = Value(c.all[id]);
        const double new_value = Value(c.last[id]);
        if ((old_value < low || old_value > high || new_value < low ||
             new_value > high) &&
            c.all_outside + c.last_outside < 8)
          std::printf(
              "  init_outside T=%d id=%d all=%.17g last=%.17g "
              "lower=%.17g upper=%.17g reference=%.17g\n",
              c.rows, id, old_value, new_value, low, high, reference);
        c.all_outside += old_value < low || old_value > high;
        c.last_outside += new_value < low || new_value > high;
        c.unequal += c.all[id] != c.last[id];
        c.max_difference =
            std::max(c.max_difference, std::abs(new_value - old_value));
        c.max_fp32_bound = std::max(c.max_fp32_bound, error32);
      }
    }
    if ((base / kWeightRows) % 64 == 0) {
      std::printf("  init_numeric_vocab_completed=%d/%d\n", base + count,
                  kVocab);
      std::fflush(stdout);
    }
  }
  bool passed = true;
  for (const auto& c : cases) {
    const std::string label = "T" + std::to_string(c.rows);
    evidence.Save(label + ".reference.f64", c.reference);
    evidence.Save(label + ".envelope_lower.bf16", c.lower);
    evidence.Save(label + ".envelope_upper.bf16", c.upper);
    std::printf(
        "MTP_INIT_ENVELOPE T=%d outputs=%d all_outside=%zu "
        "last_outside=%zu unequal=%zu max_abs=%.17g "
        "max_fp32_bound=%.17g all_seed=%d last_seed=%d\n",
        c.rows, kVocab, c.all_outside, c.last_outside, c.unequal,
        c.max_difference, c.max_fp32_bound, Argmax(c.all), Argmax(c.last));
    passed &= c.all_outside == 0 && c.last_outside == 0;
  }
  return passed;
}
}  // namespace

Q4T_TEST(mtp_init_last_row) {
  const char* path = std::getenv("Q4T_MTP_INIT_LAST_DIR");
  Require(path && *path, "set Q4T_MTP_INIT_LAST_DIR to a new directory");
  for (const char* name : {"Q4T_GDN_REG", "Q4T_GDN_CHUNKED", "Q4T_GDN_SPLIT",
                           "Q4T_FP8_PROJ", "Q4T_FP8_HC", "Q4T_FP8_ALL",
                           "Q4T_MOE_STREAMS", "Q4T_LIN_DUMP", "Q4T_MLP_DUMP"})
    Require(std::getenv(name) == nullptr, std::string("unset ") + name);
  Evidence evidence(path);
  RequireCuda(cudaSetDevice(0), "CUDA required; no SKIP");
  q4t::model::ModelConfig cfg;
  cfg.model_dir =
      "/home/rm01/models/dev/llm/garnermccloud/"
      "Qwen3.8-Flash-Next-NVFP4-SSD-Stream";
  cfg.index_path = cfg.model_dir + "/model.safetensors.index.json";
  cfg.ple_sidecar = cfg.model_dir + "/ple/qwen3.8-flash-next-ple-fp8.bin";
  cfg.num_layers = 2;
  cfg.max_len = 32;
  cfg.max_prefill = kPrompt;
  cfg.max_seq = 1;
  cfg.ple_capacity_tokens = kPrompt;
  q4t::model::ModelOwner main_owner;
  RequireStatus(main_owner.Load(cfg, nullptr), "load two-layer main fixture");
  auto& main = main_owner.Get();
  DraftOwner draft_owner;
  q4t::mtp::MtpConfig draft_cfg;
  draft_cfg.mtp_dir = cfg.model_dir + "/mtp";
  draft_cfg.max_len = cfg.max_len;
  draft_cfg.max_prefill = kChunk;
  draft_cfg.max_seq = 1;
  RequireStatus(
      q4t::mtp::LoadMtp(draft_cfg, main.head.embed_tokens, main.head.lm_head,
                        &draft_owner.model, nullptr),
      "load actual MTP");
  auto& draft = draft_owner.model;
  Require(draft.cfg.hs == kHs && draft.cfg.vocab == kVocab &&
              draft.hc_dim() == main.hc_dim(),
          "frozen dimensions changed");
  RequireStatus(q4t::mtp::MtpReserveScratch(draft, kScratch),
                "reserve scratch");
  Require(draft.k_max == kScratch, "scratch identity changed");
  std::vector<int32_t> prompt;
  std::vector<int> positions;
  for (int i = 0; i < kPrompt; ++i) {
    prompt.push_back(42 + 17 * i);
    positions.push_back(i);
  }
  DeviceBuffer<uint16_t> main_logits(kVocab);
  DeviceBuffer<uint16_t> trunk(static_cast<size_t>(kPrompt) * main.hc_dim());
  q4t::model::ModelSequence sequence;
  RequireStatus(q4t::model::ModelBeginSequence(main, &sequence, nullptr, 0),
                "begin fixture sequence");
  RequireStatus(q4t::model::ModelPrefill(
                    main, &sequence, prompt.data(), kPrompt, main_logits.data(),
                    nullptr, trunk.data(), nullptr, 0, LogitsRows::kLastRow),
                "one real 16-token prefill");
  RequireCuda(cudaStreamSynchronize(nullptr), "fixture prefill completion");
  const auto original_trunk = Read(trunk.data(), kPrompt * main.hc_dim());
  Finite(original_trunk, "actual main trunk");
  const int32_t bonus = Argmax(Read(main_logits.data(), kVocab));
  std::vector<int32_t> shifted(prompt.begin() + 1, prompt.end());
  shifted.push_back(bonus);
  evidence.Save("prompt.i32", prompt);
  evidence.Save("shifted.i32", shifted);
  evidence.Save("positions.i32", positions);
  evidence.Save("main_trunk.bf16", original_trunk);
  std::printf(
      "MTP_INIT_SCOPE main_layers=2 prompt=42+17*i prompt_tokens=16 "
      "trunk_prefix_reused=1 C=8 scratch=4 S=1 slot=0 "
      "T=1,4,8,9,10,16 same_candidate_all_vs_last=1 "
      "old_machine_code_comparison=0 performance_evidence=0\n");
  std::fflush(stdout);
  bool structural = true;
  std::vector<NumericCase> numeric;
  for (int rows : kLengths) {
    const std::string label = "T" + std::to_string(rows);
    const auto all = Manual(draft, shifted, positions, trunk.data(), rows,
                            LogitsRows::kAllRows, label + ".all");
    SaveObservation(evidence, label + ".all", all);
    const auto last = Manual(draft, shifted, positions, trunk.data(), rows,
                             LogitsRows::kLastRow, label + ".last");
    SaveObservation(evidence, label + ".last", last);
    structural &= Compare(label + ".sample", last.sample, all.sample);
    structural &= Compare(label + ".multi", last.multi, all.multi);
    structural &= CompareCache(label + ".cache", last.cache, all.cache);
    TopFive(label + ".all", all.last_logits);
    TopFive(label + ".last", last.last_logits);
    if (rows == 1 || rows == 9)
      structural &= Compare(label + ".unchanged_M1_logits", last.last_logits,
                            all.last_logits);
    else
      numeric.push_back(NumericInput(rows, all, last));
    // Scratch deliberately retains all rows even for an explicit Last call.
    const auto& selected = rows <= kScratch ? all : last;
    structural &= Wrapper(draft, shifted, positions, trunk.data(), rows, true,
                          selected, evidence, label + ".wrapper_last");
    structural &= Wrapper(draft, shifted, positions, trunk.data(), rows, false,
                          all, evidence, label + ".wrapper_default");
    std::printf("MTP_INIT_SHAPE T=%d last_chunk=%d structural_so_far=%d\n",
                rows, (rows - 1) % kChunk + 1, structural);
    std::fflush(stdout);
    Require(structural, "fixed initialization structure contract failed");
  }
  structural &=
      Compare("borrowed_main_trunk_unchanged",
              Read(trunk.data(), original_trunk.size()), original_trunk);
  const bool arithmetic = CheckNumeric(draft, numeric, evidence);
  std::printf(
      "MTP_INIT_LAST_ROW shapes=6 manual_paths=12 wrapper_paths=12 "
      "upstream_cache_guards_exact=%d numeric_cases=4 "
      "numeric_vocab_outputs=%d envelopes_pass=%d "
      "cross_shape_seed_equality_required=0 artifact_bytes=%zu\n",
      structural, 4 * kVocab, arithmetic, evidence.bytes());
  return structural && arithmetic;
}
