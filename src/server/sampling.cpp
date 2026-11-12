#include "q4t/server/sampling.h"

#include <algorithm>
#include <bit>
#include <charconv>
#include <cmath>
#include <limits>
#include <tuple>
#include <unordered_set>

#include "q4t/server/input_contract.h"

namespace q4t::server {
namespace {

// Use the original JSON number. Binary64 rounds INT64_MAX up to 2^63;
// converting that rounded value would accept/reject the wrong seed.
bool SignedSeed(const io::Json& value, int64_t* out) {
  if (!IsInputInteger(value)) return false;
  if (value.str.empty()) {
    if (std::abs(value.number) > 9007199254740991.0) return false;
    *out = static_cast<int64_t>(value.number);
    return true;
  }
  std::string_view wire(value.str);
  const bool negative = wire.front() == '-';
  if (negative) wire.remove_prefix(1);
  const auto e = wire.find_first_of("eE");
  const auto mantissa = wire.substr(0, e);
  std::string digits;
  int64_t fraction = 0;
  bool dot = false;
  for (const char c : mantissa) {
    if (c == '.') {
      dot = true;
    } else {
      if (!digits.empty() || c != '0') digits += c;
      if (dot) ++fraction;
    }
  }
  if (digits.empty()) {
    *out = 0;
    return true;
  }
  int64_t exponent = 0;
  if (e != wire.npos) {
    auto suffix = wire.substr(e + 1);
    if (suffix.starts_with('+')) suffix.remove_prefix(1);
    const auto parsed =
        std::from_chars(suffix.data(), suffix.data() + suffix.size(), exponent);
    if (parsed.ec != std::errc() || parsed.ptr != suffix.end()) return false;
  }
  // Request parser budgets bound fraction/digits far below INT64_MAX.
  if (exponent < -100000000 || exponent > 100000000) return false;
  int64_t scale = exponent - fraction;
  while (digits.back() == '0') {
    digits.pop_back();
    ++scale;
  }
  if (scale < 0 || scale > 19 || digits.size() + scale > 19) return false;
  digits.append(static_cast<size_t>(scale), '0');
  uint64_t magnitude = 0;
  const auto parsed =
      std::from_chars(digits.data(), digits.data() + digits.size(), magnitude);
  const uint64_t limit = uint64_t{1} << 63;
  if (parsed.ec != std::errc() || parsed.ptr != digits.data() + digits.size() ||
      magnitude > limit || (!negative && magnitude == limit))
    return false;
  *out = negative ? (magnitude == limit ? std::numeric_limits<int64_t>::min()
                                        : -static_cast<int64_t>(magnitude))
                  : static_cast<int64_t>(magnitude);
  return true;
}

uint64_t Mix(uint64_t seed) {
  seed += 0x9e3779b97f4a7c15ULL;
  seed = (seed ^ (seed >> 30)) * 0xbf58476d1ce4e5b9ULL;
  seed = (seed ^ (seed >> 27)) * 0x94d049bb133111ebULL;
  return seed ^ (seed >> 31);
}

bool Range(double value, double low, double high) {
  return std::isfinite(value) && value >= low && value <= high;
}

// Bounds are small exact integers. Check a rounded endpoint against the
// original decimal, so e.g. 1.00000000000000001 cannot become valid top_p=1.
bool InputRange(const io::Json& value, int low, int high) {
  if (!value.IsNumber() || !Range(value.number, low, high)) return false;
  if (value.str.empty()) return true;
  std::string_view wire(value.str);
  const bool negative = wire.starts_with('-');
  if (negative) wire.remove_prefix(1);
  const auto e = wire.find_first_of("eE");
  const auto mantissa = wire.substr(0, e);
  std::string digits;
  for (char c : mantissa)
    if (c != '.') digits += c;
  const auto nonzero = digits.find_first_not_of('0');
  if (nonzero == digits.npos) return true;
  if (value.number == 0) return false;  // Nonzero decimal underflow.
  if (value.number != low && value.number != high) return true;
  int64_t exponent = 0;
  if (e != wire.npos) {
    auto suffix = wire.substr(e + 1);
    if (suffix.starts_with('+')) suffix.remove_prefix(1);
    const auto parsed = std::from_chars(suffix.data(), suffix.end(), exponent);
    if (parsed.ec != std::errc()) return false;
  }
  const auto dot = mantissa.find('.');
  const int64_t places =
      static_cast<int64_t>(dot == mantissa.npos ? mantissa.size() : dot) +
      exponent - static_cast<int64_t>(nonzero);
  digits.erase(0, nonzero);
  const auto compare = [&](int bound) {
    if (bound == 0) return negative ? -1 : 1;
    if (negative != (bound < 0)) return negative ? -1 : 1;
    const auto limit = std::to_string(std::abs(bound));
    int magnitude = places < static_cast<int64_t>(limit.size())   ? -1
                    : places > static_cast<int64_t>(limit.size()) ? 1
                                                                  : 0;
    for (size_t i = 0;
         magnitude == 0 && i < std::max(digits.size(), limit.size()); ++i) {
      const char a = i < digits.size() ? digits[i] : '0';
      const char b = i < limit.size() ? limit[i] : '0';
      magnitude = a < b ? -1 : a > b ? 1 : 0;
    }
    return negative ? -magnitude : magnitude;
  };
  return compare(low) >= 0 && compare(high) <= 0;
}

}  // namespace

Status ParseSamplingOptions(const io::Json& request, SamplingOptions* options,
                            std::string* error_param) {
  *options = {};
  if (error_param) error_param->clear();
  const auto invalid = [&](std::string param, std::string message) {
    if (error_param) *error_param = std::move(param);
    return Status::Fail(std::move(message));
  };
  const auto number = [&](const char* key, int low, int high, double* result) {
    const auto* value = request.Find(key);
    if (!value || value->IsNull()) return true;
    if (!InputRange(*value, low, high)) return false;
    *result = value->number;
    return true;
  };
  if (!number("temperature", 0, 2, &options->temperature))
    return invalid("temperature", "temperature must be in [0,2] or null");
  if (!number("top_p", 0, 1, &options->top_p))
    return invalid("top_p", "top_p must be in [0,1] or null");
  if (!number("frequency_penalty", -2, 2, &options->frequency_penalty))
    return invalid("frequency_penalty", "frequency_penalty must be in [-2,2]");
  if (!number("presence_penalty", -2, 2, &options->presence_penalty))
    return invalid("presence_penalty", "presence_penalty must be in [-2,2]");
  for (const auto& [key, limit, target] :
       {std::tuple{"n", 128, &options->choices},
        std::tuple{"top_logprobs", 20, &options->top_logprobs}}) {
    const auto* value = request.Find(key);
    if (!value || value->IsNull()) continue;
    if (!IsInputInteger(*value) ||
        !Range(value->number, std::string_view(key) == "n" ? 1 : 0, limit))
      return invalid(key, "invalid integer selection option");
    *target = static_cast<int>(value->number);
  }
  if (const auto* value = request.Find("logprobs"); value && !value->IsNull()) {
    if (!value->IsBool())
      return invalid("logprobs", "logprobs must be boolean");
    options->logprobs = value->boolean;
  }
  if (const auto* value = request.Find("top_logprobs");
      value && !value->IsNull() && !options->logprobs)
    return invalid("top_logprobs", "top_logprobs requires logprobs=true");
  if (const auto* value = request.Find("seed"); value && !value->IsNull()) {
    int64_t seed;
    if (!SignedSeed(*value, &seed))
      return invalid("seed", "seed must be an exact signed 64-bit integer");
    options->seed = seed;
  }
  if (const auto* value = request.Find("logit_bias");
      value && !value->IsNull()) {
    if (!value->IsObject())
      return invalid("logit_bias", "logit_bias must be a map");
    std::unordered_set<int32_t> ids;
    for (const auto& [key, bias] : value->object) {
      int32_t token = -1;
      const auto parsed =
          std::from_chars(key.data(), key.data() + key.size(), token);
      if (key.empty() || key.front() == '-' || parsed.ec != std::errc() ||
          parsed.ptr != key.data() + key.size() || token < 0 ||
          !ids.insert(token).second || !InputRange(bias, -100, 100))
        return invalid(InputParamPath("logit_bias", key), "invalid token bias");
      options->logit_bias.emplace_back(token, bias.number);
    }
  }
  return Status();
}

Status TokenSampler::Init(const SamplingOptions& options, int vocab,
                          uint64_t entropy_seed, uint64_t choice_index,
                          int token_vocab) {
  counts_.clear();
  biases_.clear();
  if (token_vocab == 0) token_vocab = vocab;
  if (token_vocab <= 0 || token_vocab > vocab || vocab <= 0 || vocab > 300000 ||
      !Range(options.temperature, 0, 2) || !Range(options.top_p, 0, 1) ||
      !Range(options.frequency_penalty, -2, 2) ||
      !Range(options.presence_penalty, -2, 2) || options.top_logprobs < 0 ||
      options.top_logprobs > 20 || options.choices < 1 ||
      options.choices > 128 ||
      (!options.logprobs && options.top_logprobs != 0) ||
      choice_index >= static_cast<uint64_t>(options.choices))
    return Status::Fail("invalid sampler configuration");
  for (const auto& [token, bias] : options.logit_bias)
    if (token < 0 || token >= token_vocab || !Range(bias, -100, 100))
      return Status::Fail("logit_bias token outside model vocabulary");
  options_ = options;
  token_vocab_ = token_vocab;
  counts_.assign(vocab, 0);
  biases_.assign(vocab, 0);
  for (const auto& [token, bias] : options.logit_bias) biases_[token] = bias;
  const uint64_t seed =
      options.seed ? static_cast<uint64_t>(*options.seed) : entropy_seed;
  random_.seed(Mix(seed + choice_index));
  return Status();
}

Status TokenSampler::Select(std::span<const uint16_t> logits,
                            SampledToken* sample,
                            std::span<const uint8_t> allowed,
                            int32_t forced_token) {
  *sample = {};
  if (counts_.empty() || logits.size() != counts_.size() ||
      (!allowed.empty() && allowed.size() != logits.size()) ||
      forced_token < -1 || forced_token >= static_cast<int32_t>(logits.size()))
    return Status::Fail("invalid sampler logits/mask");
  struct Candidate {
    int32_t token;
    double value;
  };
  std::vector<Candidate> candidates, raw;
  candidates.reserve(logits.size());
  if (options_.logprobs) raw.reserve(logits.size());
  const auto higher = [](const Candidate& a, const Candidate& b) {
    return a.value == b.value ? a.token < b.token : a.value > b.value;
  };
  for (int32_t i = 0; i < token_vocab_; ++i) {
    const double value =
        std::bit_cast<float>(static_cast<uint32_t>(logits[i]) << 16);
    if (!std::isfinite(value)) continue;
    const auto token = static_cast<int32_t>(i);
    if (options_.logprobs) raw.push_back({token, value});
    if ((!allowed.empty() && !allowed[i]) ||
        (forced_token >= 0 && token != forced_token))
      continue;
    double adjusted = value + biases_[i] -
                      options_.frequency_penalty * counts_[i] -
                      (counts_[i] ? options_.presence_penalty : 0);
    // Subtract the maximum before dividing by a tiny positive temperature.
    candidates.push_back({token, adjusted});
  }
  if (candidates.empty()) return Status::Fail("no finite allowed token");
  std::sort(candidates.begin(), candidates.end(), higher);
  sample->token = candidates.front().token;
  if (options_.temperature > 0 && candidates.size() > 1) {
    const double maximum = candidates.front().value;
    double sum = 0;
    for (auto& candidate : candidates) {
      candidate.value =
          std::exp((candidate.value - maximum) / options_.temperature);
      sum += candidate.value;
    }
    size_t keep = 0;
    double mass = 0;
    do {
      mass += candidates[keep++].value;
    } while (keep < candidates.size() && mass < options_.top_p * sum);
    const double draw = static_cast<double>(random_() >> 11) * 0x1.0p-53 * mass;
    double cumulative = 0;
    sample->token = candidates[keep - 1].token;
    for (size_t i = 0; i < keep; ++i) {
      cumulative += candidates[i].value;
      if (draw < cumulative) {
        sample->token = candidates[i].token;
        break;
      }
    }
  }
  if (options_.logprobs) {
    std::sort(raw.begin(), raw.end(), higher);
    double sum = 0;
    for (const auto& candidate : raw)
      sum += std::exp(candidate.value - raw[0].value);
    const double normalizer = std::log(sum);
    sample->raw_logprob =
        std::bit_cast<float>(static_cast<uint32_t>(logits[sample->token]) << 16)
        - raw[0].value - normalizer;
    for (size_t i = 0; i < raw.size() && i < 20; ++i) {
      const double logprob = raw[i].value - raw[0].value - normalizer;
      if (raw[i].token == sample->token) sample->logprob = logprob;
      if (i < static_cast<size_t>(options_.top_logprobs))
        sample->top.push_back({raw[i].token, logprob});
    }
  }
  return Status();
}

Status TokenSampler::Observe(int32_t token) {
  if (token < 0 || token >= static_cast<int32_t>(counts_.size()) ||
      counts_[token] == std::numeric_limits<uint64_t>::max())
    return Status::Fail("invalid generated-token history");
  ++counts_[token];
  return Status();
}

}  // namespace q4t::server
