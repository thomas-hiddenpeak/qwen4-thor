#include "q4t/server/asr_request.h"

#include <algorithm>
#include <array>
#include <charconv>
#include <cmath>
#include <set>

#include "q4t/audio/asr_config.h"
#include "q4t/server/http_response.h"
#include "q4t/server/multipart.h"

namespace q4t::server {
namespace {
constexpr std::pair<std::string_view, std::string_view> kLanguages[] = {
    {"zh", "Chinese"},  {"en", "English"},    {"yue", "Cantonese"},
    {"ar", "Arabic"},   {"de", "German"},     {"fr", "French"},
    {"es", "Spanish"},  {"pt", "Portuguese"}, {"id", "Indonesian"},
    {"it", "Italian"},  {"ko", "Korean"},     {"ru", "Russian"},
    {"th", "Thai"},     {"vi", "Vietnamese"}, {"ja", "Japanese"},
    {"tr", "Turkish"},  {"hi", "Hindi"},      {"ms", "Malay"},
    {"nl", "Dutch"},    {"sv", "Swedish"},    {"da", "Danish"},
    {"fi", "Finnish"},  {"pl", "Polish"},     {"cs", "Czech"},
    {"tl", "Filipino"}, {"fa", "Persian"},    {"el", "Greek"},
    {"ro", "Romanian"}, {"hu", "Hungarian"},  {"mk", "Macedonian"}};

Status Invalid(ChatPlanError* error, std::string param, std::string message,
               int status = 400, std::string code = "invalid_request") {
  if (error) *error = {std::move(code), std::move(param), status};
  return Status::Fail(std::move(message));
}
bool Text(std::string_view s) {
  return s.find('\0') == s.npos && detail::NormalizeUtf8(s) == s;
}
bool Language(std::string_view s) {
  // ISO 639-1 plus the model's explicit Cantonese extension. Backend model
  // selection decides which languages it can execute; parsing preserves it.
  return s == "yue" ||
         (s.size() == 2 && std::all_of(s.begin(), s.end(), [](char c) {
            return c >= 'a' && c <= 'z';
          }));
}
template <typename T>
bool Number(std::string_view s, T* value) {
  if (s.empty()) return false;
  const auto result = std::from_chars(s.data(), s.data() + s.size(), *value);
  return result.ec == std::errc{} && result.ptr == s.data() + s.size();
}
bool Boolean(std::string_view s, bool* value) {
  if (s == "true") {
    *value = true;
    return true;
  }
  if (s == "false" || s == "null") {
    *value = false;
    return true;
  }
  return false;
}
}  // namespace

std::string_view AsrLanguageName(std::string_view code) {
  for (const auto& [key, name] : kLanguages)
    if (key == code) return name;
  return {};
}
std::string_view AsrLanguageCode(std::string_view name) {
  for (const auto& [code, key] : kLanguages)
    if (key == name) return code;
  return {};
}

Status ParseAsrRequest(std::string_view content_type, std::string_view body,
                       AsrRequest* out, ChatPlanError* error) {
  if (!out) return Invalid(error, {}, "Missing transcription plan owner", 500);
  if (error) *error = {};
  if (body.size() > kAsrFormBytes)
    return Invalid(error, "file", "Transcription form exceeds byte limit", 413,
                   "request_too_large");
  std::string boundary;
  auto status = MultipartBoundary(content_type, &boundary);
  if (!status) return Invalid(error, {}, "Expected multipart/form-data");
  AsrRequest plan;
  std::set<std::string> fields;
  std::string name, value;
  bool file = false;
  size_t part_limit = 0;
  io::Json chunk;
  chunk.type = io::Json::Type::kObject;
  constexpr io::JsonParseLimits json_limits{4, 32, 4096, true};
  bool chunk_json = false, chunk_fields = false;
  const auto fail = [&](std::string message, int code = 400) {
    return Invalid(error, name, std::move(message), code,
                   code == 413 ? "request_too_large" : "invalid_request");
  };
  MultipartCallbacks callbacks;
  callbacks.begin = [&](const MultipartPart& part) -> Status {
    name = part.name;
    value.clear();
    file = name == "file";
    if (file) {
      if (!fields.insert(name).second) return fail("Duplicate file");
      if (!Text(part.filename) || part.filename.size() > 1024)
        return fail("Invalid audio filename");
      plan.filename = part.filename;
      part_limit = kAsrFileBytes;
      return {};
    }
    if (part.has_filename) return fail("Only file may contain binary data");
    const bool bracket = name.ends_with("[]");
    if (bracket) name.resize(name.size() - 2);
    const bool array =
        name == "include" || name == "languages" || name == "keywords" ||
        name == "timestamp_granularities" || name == "known_speaker_names" ||
        name == "known_speaker_references";
    const bool nested = name == "chunking_strategy[type]" ||
                        name == "chunking_strategy[prefix_padding_ms]" ||
                        name == "chunking_strategy[silence_duration_ms]" ||
                        name == "chunking_strategy[threshold]";
    if ((!array && bracket) ||
        (!array && !nested && name != "model" && name != "language" &&
         name != "prompt" && name != "temperature" &&
         name != "response_format" && name != "stream" &&
         name != "chunking_strategy"))
      return fail("Unknown transcription field");
    if (!array && !fields.insert(name).second) return fail("Duplicate field");
    part_limit = name == "prompt"                     ? kAsrPromptBytes
                 : name == "known_speaker_references" ? (1ULL << 20)
                                                      : 4096;
    return {};
  };
  callbacks.data = [&](std::string_view bytes) -> Status {
    auto& target = file ? plan.file : value;
    if (bytes.size() > part_limit - target.size())
      return fail("Transcription field exceeds byte limit", 413);
    target.append(bytes);
    return {};
  };
  callbacks.end = [&]() -> Status {
    if (file) return plan.file.empty() ? fail("Audio file is empty") : Status{};
    if (!Text(value)) return fail("Expected valid UTF-8 text without NUL");
    if (name == "model") {
      if (value.empty() || value.size() > 256) return fail("Invalid model");
      plan.model = value;
    } else if (name == "prompt") {
      plan.prompt = value;
    } else if (name == "language") {
      if (!Language(value)) return fail("Expected a language code");
      plan.language = value;
    } else if (name == "temperature") {
      if (!Number(value, &plan.temperature) ||
          !std::isfinite(plan.temperature) || plan.temperature < 0 ||
          plan.temperature > 1)
        return fail("Temperature must be in [0,1]");
    } else if (name == "stream") {
      if (!Boolean(value, &plan.stream)) return fail("Expected a boolean");
    } else if (name == "response_format") {
      if (value == "json")
        plan.format = AsrFormat::kJson;
      else if (value == "text")
        plan.format = AsrFormat::kText;
      else if (value == "srt")
        plan.format = AsrFormat::kSrt;
      else if (value == "verbose_json")
        plan.format = AsrFormat::kVerboseJson;
      else if (value == "vtt")
        plan.format = AsrFormat::kVtt;
      else if (value == "diarized_json")
        plan.format = AsrFormat::kDiarizedJson;
      else
        return fail("Invalid transcription response format");
    } else if (name == "include") {
      if (value != "logprobs" || plan.logprobs)
        return fail("Expected logprobs once");
      plan.logprobs = true;
    } else if (name == "timestamp_granularities") {
      if (value == "word" && !plan.word_timestamps)
        plan.word_timestamps = true;
      else if (value == "segment" && !plan.segment_timestamps)
        plan.segment_timestamps = true;
      else
        return fail("Expected unique word/segment timestamp granularities");
    } else if (name == "chunking_strategy") {
      if (chunk_fields) return fail("Mixed chunking strategy encodings");
      chunk_json = true;
      if (value == "auto")
        plan.chunking.mode = AsrChunking::Mode::kAuto;
      else if (value != "null") {
        if (!io::ParseJson(value, &chunk, true, json_limits) ||
            !chunk.IsObject())
          return fail("Expected auto or a server_vad object");
        plan.chunking.mode = AsrChunking::Mode::kServerVad;
      }
    } else if (name.starts_with("chunking_strategy[")) {
      if (chunk_json) return fail("Mixed chunking strategy encodings");
      chunk_fields = true;
      const auto key = name.substr(18, name.size() - 19);
      if (key == "type") {
        io::Json type;
        type.type = io::Json::Type::kString;
        type.str = value;
        chunk.object.emplace_back(key, std::move(type));
      } else {
        io::Json number;
        if (!io::ParseJson(value, &number, true, json_limits) ||
            !number.IsNumber())
          return fail("Expected numeric chunking parameter");
        chunk.object.emplace_back(key, std::move(number));
      }
      plan.chunking.mode = AsrChunking::Mode::kServerVad;
    } else {
      auto* target = name == "languages"             ? &plan.languages
                     : name == "keywords"            ? &plan.keywords
                     : name == "known_speaker_names" ? &plan.speaker_names
                                                     : &plan.speaker_references;
      const size_t max = name == "keywords" ? 64 : name == "languages" ? 64 : 4;
      if (target->size() >= max || value.empty() ||
          ((name == "languages") && !Language(value)) ||
          ((name == "keywords" || name == "known_speaker_names") &&
           value.size() > 256) ||
          std::find(target->begin(), target->end(), value) != target->end())
        return fail("Invalid, duplicate or excessive array value");
      target->push_back(value);
    }
    return {};
  };
  MultipartDecoder decoder(boundary, kAsrFormBytes, 256, std::move(callbacks));
  // Keep the MIME parser's temporary buffer bounded independently of the
  // caller's body and our owned audio bytes.
  for (size_t offset = 0; offset < body.size(); offset += 4096) {
    status = decoder.Feed(
        body.substr(offset, std::min<size_t>(4096, body.size() - offset)));
    if (!status) break;
  }
  if (status) status = decoder.Finish();
  if (!status) return status;
  if (!fields.contains("file") || plan.file.empty())
    return Invalid(error, "file", "Audio file is required");
  if (plan.model.empty()) return Invalid(error, "model", "Model is required");
  if ((plan.word_timestamps || plan.segment_timestamps) &&
      plan.format != AsrFormat::kVerboseJson)
    return Invalid(error, "timestamp_granularities",
                   "Timestamps require verbose_json");
  if (plan.format == AsrFormat::kVerboseJson && !plan.word_timestamps)
    plan.segment_timestamps = true;
  if (plan.logprobs && plan.format != AsrFormat::kJson)
    return Invalid(error, "include", "Logprobs require json");
  if (plan.speaker_names.size() != plan.speaker_references.size())
    return Invalid(error, "known_speaker_references",
                   "Speaker names and references must have matching lengths");
  if (!plan.speaker_names.empty() && plan.format != AsrFormat::kDiarizedJson)
    return Invalid(error, "known_speaker_names",
                   "Speaker references require diarized_json");
  if (plan.chunking.mode == AsrChunking::Mode::kServerVad) {
    name = "chunking_strategy";
    if (chunk.GetString("type") != "server_vad")
      return fail("Chunking type must be server_vad");
    for (const auto& [key, v] : chunk.object) {
      if (key == "type") continue;
      if (key == "threshold") {
        if (!v.IsNumber() || !std::isfinite(v.number) || v.number < 0 ||
            v.number > 1)
          return fail("Invalid VAD threshold");
        plan.chunking.threshold = v.number;
      } else if (key == "prefix_padding_ms" || key == "silence_duration_ms") {
        if (!v.IsNumber() || !std::isfinite(v.number) ||
            std::floor(v.number) != v.number || v.number < 0 ||
            v.number > 30000)
          return fail("Invalid VAD duration");
        if (key == "prefix_padding_ms")
          plan.chunking.prefix_padding_ms = v.number;
        else
          plan.chunking.silence_duration_ms = v.number;
      } else
        return fail("Unknown VAD parameter");
    }
  }
  *out = std::move(plan);
  return {};
}

Status BuildAsrPrompt(const AsrRequest& request, size_t audio_tokens,
                      const AsrServiceLimits& limits, const AsrEncode& encode,
                      AsrPrompt* out, ChatPlanError* error) {
  if (!out || !encode || !limits.Valid())
    return Invalid(error, {}, "Invalid ASR prompt owner", 500);
  if (!audio_tokens || audio_tokens > size_t(limits.max_audio_seconds) * 13)
    return Invalid(error, "file", "Audio exceeds configured context capacity");
  const auto language = AsrLanguageName(request.language);
  if (!request.language.empty() && language.empty())
    return Invalid(error, "language", "Language unavailable in selected model");
  std::string context = request.prompt;
  if (!request.languages.empty()) {
    if (!request.language.empty() &&
        std::find(request.languages.begin(), request.languages.end(),
                  request.language) == request.languages.end())
      return Invalid(error, "languages", "Language hints conflict");
    context += "\nLanguages: ";
    for (size_t i = 0; i < request.languages.size(); ++i) {
      const auto name = AsrLanguageName(request.languages[i]);
      if (name.empty())
        return Invalid(error, "languages",
                       "Language unavailable in selected model");
      if (i) context += ", ";
      context += name;
    }
  }
  if (!request.keywords.empty()) {
    context += "\nKeywords: ";
    for (size_t i = 0; i < request.keywords.size(); ++i) {
      if (i) context += ", ";
      context += request.keywords[i];
    }
  }
  AsrPrompt plan;
  const auto append = [&](std::string_view value, bool special) -> Status {
    std::vector<uint32_t> ids;
    auto status = encode(value, special, &ids);
    if (!status) return status;
    if (ids.size() > size_t(limits.max_context_tokens) - plan.tokens.size())
      return Invalid(error, "prompt", "ASR prompt exceeds context capacity");
    for (const auto id : ids) {
      if (id >= audio::AsrConfig::kVocab)
        return Invalid(error, {}, "ASR tokenizer returned invalid ID", 500);
      plan.tokens.push_back(static_cast<int32_t>(id));
    }
    return {};
  };
  auto status = append("<|im_start|>system\n", true);
  if (status) status = append(context, false);
  if (status)
    status = append("<|im_end|>\n<|im_start|>user\n<|audio_start|>", true);
  if (!status) return status;
  plan.audio_start = plan.tokens.size();
  plan.audio_tokens = audio_tokens;
  plan.output_tokens = limits.max_output_tokens;
  if (audio_tokens > size_t(limits.max_context_tokens) - plan.tokens.size())
    return Invalid(error, "file", "ASR audio exceeds context capacity");
  plan.tokens.insert(plan.tokens.end(), audio_tokens,
                     audio::AsrConfig::kAudioPad);
  status = append("<|audio_end|><|im_end|>\n<|im_start|>assistant\n", true);
  if (status && !language.empty())
    status = append("language " + std::string(language) + "<asr_text>", true);
  if (!status) return status;
  if (size_t(limits.max_output_tokens) >
      size_t(limits.max_context_tokens) - plan.tokens.size())
    return Invalid(error, "prompt",
                   "ASR prompt leaves insufficient output capacity");
  *out = std::move(plan);
  return {};
}
}  // namespace q4t::server
