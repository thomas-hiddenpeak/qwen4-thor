#include "q4t/server/sampling.h"

#include <bit>
#include <cmath>
#include <limits>

#include "q4t/test.h"

namespace {
using namespace q4t::server;

std::vector<uint16_t> Logits(std::initializer_list<float> values) {
  std::vector<uint16_t> result;
  for (float value : values)
    result.push_back(
        static_cast<uint16_t>(std::bit_cast<uint32_t>(value) >> 16));
  return result;
}

bool Read(const std::string& body, SamplingOptions* options,
          std::string* param = nullptr) {
  q4t::io::Json request;
  return q4t::io::ParseJson(body, &request, true).ok() &&
         ParseSamplingOptions(request, options, param).ok();
}
}  // namespace

Q4T_TEST(sampling_standard_defaults_and_null) {
  SamplingOptions options;
  Q4T_CHECK(Read("{}", &options));
  Q4T_CHECK(options.temperature == 1 && options.top_p == 1 &&
            options.choices == 1 && !options.seed && !options.logprobs);
  Q4T_CHECK(Read(R"({"temperature":null,"top_p":null,"n":null,"seed":null,
                    "presence_penalty":null,"frequency_penalty":null,
                    "logprobs":null,"top_logprobs":null,"logit_bias":null})",
                 &options));
  Q4T_CHECK(options.temperature == 1 && options.top_p == 1 &&
            options.presence_penalty == 0 && options.frequency_penalty == 0);
  Q4T_CHECK(Read(R"({"temperature":0,"top_p":0,"n":128,"logprobs":true,
                    "top_logprobs":20,"logit_bias":{"17":-100}})",
                 &options));
  Q4T_CHECK(options.choices == 128 && options.logit_bias[0].first == 17);
  return true;
}

Q4T_TEST(sampling_seed_preserves_signed_64_bit_integer) {
  const std::pair<const char*, int64_t> valid[] = {
      {"9223372036854775807", std::numeric_limits<int64_t>::max()},
      {"-9223372036854775808", std::numeric_limits<int64_t>::min()},
      {"9.223372036854775807e18", std::numeric_limits<int64_t>::max()},
      {"-9.223372036854775808e18", std::numeric_limits<int64_t>::min()},
      {"9007199254740993", 9007199254740993LL},
      {"0.001e3", 1},
      {"1.000e0", 1},
      {"-0.00", 0}};
  SamplingOptions options;
  for (const auto& [wire, expected] : valid) {
    Q4T_CHECK(Read(std::string("{\"seed\":") + wire + "}", &options));
    Q4T_CHECK(options.seed == expected);
  }
  for (const auto* wire :
       {"9223372036854775808", "-9223372036854775809", "1.00000000000000000001",
        "true", "\"17\"", "1e20"}) {
    std::string param;
    Q4T_CHECK(!Read(std::string("{\"seed\":") + wire + "}", &options, &param));
    Q4T_CHECK(param == "seed");
  }
  return true;
}

Q4T_TEST(sampling_invalid_fields_have_specific_paths) {
  const std::pair<const char*, const char*> rows[] = {
      {R"({"temperature":2.1})", "temperature"},
      {R"({"top_p":-0.1})", "top_p"},
      {R"({"n":0})", "n"},
      {R"({"n":1.000000000000000001})", "n"},
      {R"({"frequency_penalty":-3})", "frequency_penalty"},
      {R"({"presence_penalty":true})", "presence_penalty"},
      {R"({"logprobs":1})", "logprobs"},
      {R"({"top_logprobs":0})", "top_logprobs"},
      {R"({"logprobs":true,"top_logprobs":21})", "top_logprobs"},
      {R"({"logit_bias":{"1":101}})", "logit_bias[\"1\"]"},
      {R"({"logit_bias":{"1":0,"01":1}})", "logit_bias[\"01\"]"},
      {R"({"logit_bias":{"-1":0}})", "logit_bias[\"-1\"]"},
      {R"({"logit_bias":{"x":0}})", "logit_bias.x"}};
  for (const auto& [wire, expected] : rows) {
    SamplingOptions options;
    std::string param;
    Q4T_CHECK(!Read(wire, &options, &param));
    Q4T_CHECK(param == expected);
  }
  return true;
}

Q4T_TEST(sampling_penalties_bias_and_accept_history) {
  const auto logits = Logits({3, 2, 1});
  for (bool presence : {false, true}) {
    SamplingOptions options;
    options.temperature = 0;
    options.presence_penalty = presence ? 2 : 0;
    options.frequency_penalty = presence ? 0 : 1;
    TokenSampler sampler;
    Q4T_CHECK(sampler.Init(options, 3, 0));
    SampledToken sample;
    Q4T_CHECK(sampler.Select(logits, &sample) && sample.token == 0);
    // Selection itself must not poison history before the decoder accepts.
    Q4T_CHECK(sampler.Select(logits, &sample) && sample.token == 0);
    Q4T_CHECK(sampler.Observe(0));
    if (!presence) Q4T_CHECK(sampler.Observe(0));
    Q4T_CHECK(sampler.Select(logits, &sample) && sample.token == 1);
  }
  SamplingOptions options;
  options.temperature = 0;
  options.frequency_penalty = -2;
  TokenSampler sampler;
  Q4T_CHECK(sampler.Init(options, 3, 0));
  Q4T_CHECK(sampler.Observe(2) && sampler.Observe(2));
  SampledToken sample;
  Q4T_CHECK(sampler.Select(logits, &sample) && sample.token == 2);
  options.logit_bias = {{1, 100}};
  Q4T_CHECK(sampler.Init(options, 3, 0));
  Q4T_CHECK(sampler.Select(logits, &sample) && sample.token == 1);
  return true;
}

Q4T_TEST(sampling_masks_forced_tokens_and_nonfinite_logits) {
  SamplingOptions options;
  options.temperature = 0;
  options.logit_bias = {{0, 100}};
  TokenSampler sampler;
  Q4T_CHECK(sampler.Init(options, 3, 0));
  const auto logits = Logits({3, 2, 1});
  const std::vector<uint8_t> mask{0, 1, 1};
  SampledToken sample;
  Q4T_CHECK(sampler.Select(logits, &sample, mask) && sample.token == 1);
  Q4T_CHECK(!sampler.Select(logits, &sample, mask, 0) && sample.token == -1);
  Q4T_CHECK(sampler.Select(logits, &sample, mask, 2) && sample.token == 2);
  Q4T_CHECK(!sampler.Select(Logits({0}), &sample));
  const float nan = std::numeric_limits<float>::quiet_NaN();
  const float inf = std::numeric_limits<float>::infinity();
  Q4T_CHECK(!sampler.Select(Logits({nan, inf, -inf}), &sample));
  options.logit_bias = {{3, 0}};
  Q4T_CHECK(!sampler.Init(options, 3, 0));
  Q4T_CHECK(!sampler.Select(logits, &sample));
  return true;
}

Q4T_TEST(sampling_nucleus_zero_and_tiny_temperature) {
  TokenSampler sampler;
  SampledToken sample;
  for (const double temperature :
       {1., std::numeric_limits<double>::denorm_min()}) {
    SamplingOptions options;
    options.temperature = temperature;
    options.top_p = temperature == 1 ? 0 : 1;
    Q4T_CHECK(sampler.Init(options, 3, 5));
    for (int i = 0; i < 16; ++i)
      Q4T_CHECK(sampler.Select(Logits({2, 1, 0}), &sample) &&
                sample.token == 0);
  }
  return true;
}

Q4T_TEST(sampling_seed_and_choice_are_independent) {
  SamplingOptions options;
  options.seed = 9223372036854775807LL;
  options.choices = 2;
  TokenSampler first, repeat, second;
  Q4T_CHECK(first.Init(options, 3, 0, 0));
  Q4T_CHECK(repeat.Init(options, 3, 12345, 0));
  Q4T_CHECK(second.Init(options, 3, 0, 1));
  int different = 0;
  const auto logits = Logits({0, 0, 0});
  for (int i = 0; i < 64; ++i) {
    SampledToken a, b, c;
    Q4T_CHECK(first.Select(logits, &a) && repeat.Select(logits, &b) &&
              second.Select(logits, &c));
    Q4T_CHECK(a.token == b.token);
    different += a.token != c.token;
  }
  Q4T_CHECK(different > 0);
  return true;
}

Q4T_TEST(sampling_logprobs_are_raw_and_top20_bounded) {
  SamplingOptions options;
  options.temperature = 0;
  options.logprobs = true;
  options.top_logprobs = 3;
  options.logit_bias = {{0, 100}};
  TokenSampler sampler;
  Q4T_CHECK(sampler.Init(options, 3, 0));
  SampledToken sample;
  Q4T_CHECK(sampler.Select(Logits({0, 1, 2}), &sample));
  const double log_z = std::log(1 + std::exp(1.) + std::exp(2.));
  Q4T_CHECK(sample.token == 0 && std::abs(sample.logprob + log_z) < 1e-12);
  Q4T_CHECK(sample.top.size() == 3 && sample.top[0].token == 2 &&
            sample.top[1].token == 1 && sample.top[2].token == 0);
  double sum = 0;
  for (const auto& token : sample.top) sum += std::exp(token.logprob);
  Q4T_CHECK(std::abs(sum - 1) < 1e-12);
  options.top_logprobs = 20;
  options.logit_bias.clear();
  Q4T_CHECK(sampler.Init(options, 21, 0));
  std::vector<uint16_t> logits;
  for (int i = 0; i < 21; ++i)
    logits.push_back(Logits({static_cast<float>(i)})[0]);
  Q4T_CHECK(sampler.Select(logits, &sample, {}, 0));
  Q4T_CHECK(sample.token == 0 && sample.logprob == -9999 &&
            sample.top.size() == 20 && sample.top.back().token == 1);
  double mass = 0;
  for (int i = 0; i < 21; ++i) mass += std::exp(double(i));
  Q4T_CHECK(std::abs(sample.raw_logprob + std::log(mass)) < 1e-12);
  return true;
}
