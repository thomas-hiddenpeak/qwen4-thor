#include "q4t/audio/tts_generation.h"

#include <algorithm>
#include <bit>
#include <cmath>
#include <limits>
#include <string>

namespace q4t::audio {
namespace {

std::string Lower(std::string_view value) {
  std::string result(value);
  for (char& c : result)
    if (c >= 'A' && c <= 'Z') c += 'a' - 'A';
  return result;
}

bool Content(std::span<const int32_t> ids) {
  return std::all_of(ids.begin(), ids.end(),
                     [](auto id) { return id >= 0 && id < 151643; });
}

}  // namespace

Status PlanTtsPrompt(TtsVariant variant, std::span<const int32_t> text,
                     std::span<const int32_t> instruction,
                     std::string_view voice, std::string_view language,
                     bool non_streaming, size_t max_context, TtsPrompt* out) {
  if (!out || text.empty() || text.size() > 32700 ||
      instruction.size() > 32700 || !Content(text) || !Content(instruction) ||
      max_context > 32768 ||
      (variant != TtsVariant::kCustomVoice06B &&
       variant != TtsVariant::kCustomVoice17B))
    return Status::Fail("TTS literal content or context invalid");
  if (variant == TtsVariant::kCustomVoice06B && !instruction.empty())
    return Status::Fail("TTS instructions require the qualified 1.7B model");
  constexpr std::pair<std::string_view, int> voices[] = {
      {"serena", 3066}, {"vivian", 3065}, {"uncle_fu", 3010},
      {"ryan", 3061}, {"aiden", 2861}, {"ono_anna", 2873},
      {"sohee", 2864}, {"eric", 2875}, {"dylan", 2878}};
  constexpr std::pair<std::string_view, int> languages[] = {
      {"auto", -1}, {"chinese", 2055}, {"english", 2050}, {"german", 2053},
      {"italian", 2070}, {"portuguese", 2071}, {"spanish", 2054},
      {"japanese", 2058}, {"korean", 2064}, {"french", 2061},
      {"russian", 2069}, {"beijing_dialect", 2074}, {"sichuan_dialect", 2062}};
  const auto speaker = Lower(voice), lang = Lower(language);
  int speaker_id = -1, language_id = -2;
  for (const auto& [name, id] : voices)
    if (name == speaker) speaker_id = id;
  for (const auto& [name, id] : languages)
    if (name == lang) language_id = id;
  if (speaker_id < 0 || language_id == -2)
    return Status::Fail("TTS voice or language is not in the pinned model");
  if (lang == "auto" || lang == "chinese") {
    if (speaker == "eric") language_id = 2062;
    if (speaker == "dylan") language_id = 2074;
  }
  TtsPrompt p;
  const auto pure_text = [&](int32_t id) {
    p.prefill.push_back({static_cast<int32_t>(p.text_ids.size()), -1});
    p.text_ids.push_back(id);
  };
  if (!instruction.empty()) {
    for (const auto id : {151644, 872, 198}) pure_text(id);
    for (const auto id : instruction) pure_text(id);
    for (const auto id : {151645, 198}) pure_text(id);
  }
  for (const auto id : {151644, 77091, 198}) pure_text(id);
  p.pad_index = static_cast<int32_t>(p.text_ids.size());
  p.text_ids.insert(p.text_ids.end(), {151671, 151672, 151673});
  const int32_t bos = p.pad_index + 1, eos = p.pad_index + 2;
  const int32_t text_start = static_cast<int32_t>(p.text_ids.size());
  p.text_ids.insert(p.text_ids.end(), text.begin(), text.end());
  std::vector<int32_t> prefix;
  if (language_id == -1) prefix = {2155, 2156, 2157};
  else prefix = {2154, 2156, language_id, 2157};
  prefix.push_back(speaker_id);
  for (const auto id : prefix) p.prefill.push_back({p.pad_index, id});
  p.prefill.push_back({bos, 2148});
  if (non_streaming) {
    for (size_t i = 0; i < text.size(); ++i)
      p.prefill.push_back({text_start + static_cast<int32_t>(i), 2148});
    p.prefill.push_back({eos, 2148});
    p.prefill.push_back({p.pad_index, 2149});
    p.trailing_text.push_back(p.pad_index);
  } else {
    p.prefill.push_back({text_start, 2149});
    for (size_t i = 1; i < text.size(); ++i)
      p.trailing_text.push_back(text_start + static_cast<int32_t>(i));
    p.trailing_text.push_back(eos);
  }
  if (p.prefill.size() > max_context || p.text_ids.size() > 32768)
    return Status::Fail("TTS prompt exceeds the configured context budget");
  *out = std::move(p);
  return {};
}

Status ValidateTtsPrompt(const TtsPrompt& p) {
  const auto index = [&](int32_t i) {
    return i >= 0 && static_cast<size_t>(i) < p.text_ids.size();
  };
  if (p.text_ids.empty() || p.text_ids.size() > 32768 ||
      p.prefill.size() < 8 || p.prefill.size() > 32768 ||
      p.trailing_text.empty() || p.trailing_text.size() > 32768 ||
      !index(p.pad_index) || p.text_ids[p.pad_index] != 151671)
    return Status::Fail("TTS prompt layout invalid");
  for (const auto id : p.text_ids)
    if (id < 0 || id >= 151676)
      return Status::Fail("TTS text ID exceeds tokenizer vocabulary");
  for (const auto& row : p.prefill)
    if (!index(row.text_index) || row.codec_id < -1 || row.codec_id >= 3072)
      return Status::Fail("TTS dual-track row outside its storage");
  for (const auto i : p.trailing_text)
    if (!index(i)) return Status::Fail("TTS trailing text outside storage");
  return {};
}

Status TtsCodeSampler::Init(const TtsSampling& options, uint64_t seed) {
  if (!std::isfinite(options.temperature) || options.temperature <= 0 ||
      !std::isfinite(options.top_p) || options.top_p <= 0 || options.top_p > 1 ||
      options.top_k < 0 || options.top_k > 3072 ||
      !std::isfinite(options.repetition_penalty) ||
      options.repetition_penalty <= 0)
    return Status::Fail("TTS sampling parameters invalid");
  options_ = options;
  random_.seed(seed);
  initialized_ = true;
  return {};
}

Status TtsCodeSampler::Select(std::span<const uint16_t> logits,
                              bool group_zero, bool allow_eos,
                              std::span<const int32_t> history,
                              int32_t* token) {
  const size_t count = group_zero ? 3072 : 2048;
  if (!initialized_ || !token || logits.size() != count || history.size() > 4096)
    return Status::Fail("TTS logits or sampler state invalid");
  std::array<bool, 2048> repeated{};
  for (const int32_t id : history) {
    if (id < 0 || id >= 2048)
      return Status::Fail("TTS repetition history contains non-audio token");
    repeated[id] = true;
  }
  struct Candidate { int32_t id; float score; double mass = 0; };
  std::vector<Candidate> candidates;
  for (size_t i = 0; i < count; ++i) {
    float score = std::bit_cast<float>(static_cast<uint32_t>(logits[i]) << 16);
    if (!std::isfinite(score))
      return Status::Fail("TTS model logits are not finite");
    if (i >= 2048 && !(i == TtsConfig::kCodecEos && allow_eos)) continue;
    if (group_zero && i < repeated.size() && repeated[i])
      score = score < 0 ? score * options_.repetition_penalty
                       : score / options_.repetition_penalty;
    if (options_.sample) score /= options_.temperature;
    if (!std::isfinite(score))
      return Status::Fail("TTS processed logits are not finite");
    candidates.push_back({static_cast<int32_t>(i), score});
  }
  std::sort(candidates.begin(), candidates.end(), [](auto a, auto b) {
    return a.score == b.score ? a.id < b.id : a.score > b.score;
  });
  if (!options_.sample) { *token = candidates.front().id; return {}; }
  if (options_.top_k > 0 &&
      candidates.size() > static_cast<size_t>(options_.top_k)) {
    const auto threshold = candidates[options_.top_k - 1].score;
    std::erase_if(candidates, [&](auto x) { return x.score < threshold; });
  }
  double total = 0;
  const float maximum = candidates.front().score;
  for (auto& c : candidates) {
    c.mass = std::exp(static_cast<double>(c.score) - maximum);
    total += c.mass;
  }
  // Retain the smallest descending prefix reaching top_p, including the
  // threshold token. At least one remains; a zero-mass tail never wins.
  if (options_.top_p < 1) {
    double cumulative = 0;
    size_t keep = 0;
    do { cumulative += candidates[keep++].mass; }
    while (keep < candidates.size() && cumulative < options_.top_p * total);
    candidates.resize(keep);
    total = cumulative;
  }
  const double draw = std::generate_canonical<double, 53>(random_) * total;
  double cumulative = 0;
  for (const auto& c : candidates) {
    cumulative += c.mass;
    if (draw < cumulative) { *token = c.id; return {}; }
  }
  *token = candidates.back().id;
  return {};
}

Status TtsGenerationState::Init(size_t max_frames) {
  if (initialized_ || max_frames < 2 || max_frames > 4096)
    return Status::Fail("TTS generation frame budget invalid");
  frames_.reserve(max_frames);
  first_codes_.reserve(max_frames);
  max_frames_ = max_frames;
  initialized_ = true;
  return {};
}

Status TtsGenerationState::Accept(size_t group, int32_t token) {
  if (!initialized_ || eos_ || at_limit() || group != next_group_)
    return Status::Fail("TTS generation step out of order or exhausted");
  if (group == 0 && token == TtsConfig::kCodecEos && allow_eos()) {
    eos_ = true;
    return {};
  }
  if (token < 0 || token >= TtsConfig::kCodebook)
    return Status::Fail("TTS frame contains invalid audio code");
  pending_[next_group_++] = token;
  if (next_group_ == TtsConfig::kCodeGroups) {
    frames_.push_back(pending_);
    first_codes_.push_back(pending_[0]);
    next_group_ = 0;
  }
  return {};
}

}  // namespace q4t::audio
