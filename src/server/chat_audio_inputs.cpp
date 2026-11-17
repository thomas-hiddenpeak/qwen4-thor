#include "q4t/server/chat_audio_inputs.h"

#include <algorithm>

#include "file_audio_protocol.h"
#include "object_protocol.h"
#include "q4t/server/file_audio.h"
#include "q4t/server/http_response.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/chat_plan.h"
#include "q4t/server/moderation.h"

namespace q4t::server {
namespace {
using namespace object_detail;
ObjectReply At(ObjectReply reply, const std::string& path) {
  reply.value = Object({{"param", String(path)}});
  return reply;
}
ObjectReply Invalid(const std::string& path, const char* message) {
  return At(Error(400, "invalid_request", message), path);
}
ObjectReply State() {
  return Error(500, "chat_audio_state_invalid",
               "Audio preparation state is inconsistent");
}
ObjectReply CheckAccess(const BinaryAccess& access) {
  if (access.cancelled && access.cancelled())
    return Error(499, "request_cancelled", "Audio preparation cancelled");
  if (std::chrono::steady_clock::now() >= access.deadline)
    return Error(408, "request_deadline_exceeded",
                 "Audio preparation deadline exceeded");
  return {};
}
bool Audio(const Json& part) {
  return part.GetString("type") == "input_audio" || part.Has("input_audio");
}
std::string Path(size_t message, size_t part) {
  return "messages[" + std::to_string(message) + "].content[" +
         std::to_string(part) + ']';
}
std::string TranscriptText(const ChatAudioInput& source,
                           const std::string& transcript) {
  // Keep the source modality and part boundary visible to the text model.
  // JSON quoting and escaped angle brackets keep recognized control-like
  // strings as user data. Public storage retains the original audio part.
  const auto data = OutputJson(Object(
      {{"source", String(source.param)}, {"transcript", String(transcript)}}));
  std::string escaped;
  for (const auto c : data) {
    if (c == '<')
      escaped += "\\u003c";
    else if (c == '>')
      escaped += "\\u003e";
    else
      escaped += c;
  }
  return "\n\n[Audio input transcript: recognized speech from the supplied "
         "recording]\n" +
         escaped + "\n[End audio input transcript]\n\n";
}
}  // namespace

bool HasChatAudioInputs(const Json& request) {
  if (const auto* messages = request.GetArray("messages"))
    for (const auto& message : messages->array)
      if (const auto* content = message.GetArray("content"))
        for (const auto& part : content->array)
          if (Audio(part)) return true;
  return false;
}

ObjectReply ParseChatAudioSource(const Json& part, std::string_view role,
                                 const std::string& path,
                                 FileInputSource* source) {
  if (!source) return State();
  if (role != "user")
    return Invalid(path, "Input audio requires a user message");
  std::string param;
  auto fields = InputFields(
      part, {"type", "input_audio", "prompt_cache_breakpoint"}, &param, path);
  if (!fields) return Invalid(param, "Invalid audio content part");
  if (part.GetString("type") != "input_audio")
    return Invalid(path + ".type", "Expected input_audio type");
  const auto* audio = part.Find("input_audio");
  if (!audio || !audio->IsObject())
    return Invalid(path + ".input_audio", "Expected input_audio object");
  fields =
      InputFields(*audio, {"data", "format"}, &param, path + ".input_audio");
  if (!fields) return Invalid(param, "Invalid input_audio field");
  const auto format = audio->GetString("format");
  if (format != "wav" && format != "mp3")
    return Invalid(path + ".input_audio.format", "Expected wav or mp3");
  const auto* data = audio->Find("data");
  if (!data || !data->IsString() || data->str.empty())
    return Invalid(path + ".input_audio.data", "Expected base64 audio");
  FileInputSource parsed;
  parsed.value = data->str;
  parsed.param = path + ".input_audio.data";
  parsed.filename = "input." + format;
  parsed.mime_type = format == "wav" ? "audio/wav" : "audio/mpeg";
  *source = std::move(parsed);
  return {};
}

ObjectReply ChatAudioInputs::Prepare(const Json& request,
                                     const BinaryAccess& access,
                                     ChatAudioLimits limits) {
  if (prepared_ || !limits.max_parts || limits.max_parts > 8 ||
      !limits.max_source_bytes || limits.max_source_bytes > (25ULL << 20) ||
      !limits.max_transcript_bytes ||
      limits.max_transcript_bytes > (1ULL << 20))
    return State();
  auto active = CheckAccess(access);
  if (!active.ok()) return active;
  std::vector<ChatAudioInput> inputs;
  FileInputBudget budget{.max_bytes = limits.max_source_bytes,
                         .max_files = limits.max_parts};
  if (const auto* messages = request.GetArray("messages")) {
    for (size_t i = 0; i < messages->array.size(); ++i) {
      const auto& message = messages->array[i];
      const auto* content = message.GetArray("content");
      if (!content) continue;
      for (size_t j = 0; j < content->array.size(); ++j) {
        const auto& part = content->array[j];
        if (!Audio(part)) continue;
        const auto path = Path(i, j);
        FileInputSource source;
        auto parsed = ParseChatAudioSource(part, message.GetString("role"),
                                           path, &source);
        if (!parsed.ok()) return parsed;
        const auto format = part.Find("input_audio")->GetString("format");
        ChatAudioInput input{i, j, path, format, Digest(OutputJson(part)), {}};
        auto read =
            ReadLocalFileInput(nullptr, access, source, &budget, &input.source);
        if (!read.ok()) {
          if (read.http_status == 413)
            return At(Error(413, "chat_audio_capacity_exceeded",
                            "Audio input shared budget exceeded"),
                      path);
          return At(std::move(read), source.param);
        }
        const auto kind = audio_detail::Detect(input.source.bytes);
        if (!IsAudioContainer(input.source.bytes) ||
            kind != (format == "wav" ? audio_detail::Container::kWave
                                     : audio_detail::Container::kMpeg))
          return Invalid(path + ".input_audio.format",
                         "Audio container does not match the declared format");
        inputs.push_back(std::move(input));
      }
    }
  }
  active = CheckAccess(access);
  if (!active.ok()) return active;
  std::vector<std::optional<std::string>> transcripts(inputs.size());
  limits_ = limits;
  inputs_ = std::move(inputs);
  transcripts_ = std::move(transcripts);
  prepared_ = true;
  return {};
}

ObjectReply ChatAudioInputs::Complete(size_t index, std::string transcript) {
  if (!prepared_ || index >= inputs_.size() || transcripts_[index])
    return State();
  if (transcript.find('\0') != transcript.npos ||
      detail::NormalizeUtf8(transcript) != transcript)
    return Error(502, "chat_audio_transcript_invalid",
                 "Audio backend returned invalid transcript text");
  if (transcript.size() > limits_.max_transcript_bytes - transcript_bytes_)
    return Error(413, "chat_audio_transcript_capacity",
                 "Audio transcripts exceed the shared text budget");
  transcript_bytes_ += transcript.size();
  transcripts_[index] = std::move(transcript);
  return {};
}

ObjectReply ChatAudioInputs::Render(const Json& request,
                                    const BinaryAccess& access,
                                    Json* output) const {
  return RenderBound(request, access, output, true);
}

ObjectReply ChatAudioInputs::RenderExpanded(const Json& request,
                                            const BinaryAccess& access,
                                            Json* output) const {
  return RenderBound(request, access, output, false);
}

ObjectReply ChatAudioInputs::RenderBound(const Json& request,
                                         const BinaryAccess& access,
                                         Json* output,
                                         bool exact_coordinates) const {
  if (!output || !prepared_) return State();
  auto active = CheckAccess(access);
  if (!active.ok()) return active;
  auto result = request;
  size_t index = 0;
  if (result.GetArray("messages")) {
    auto& messages = Field(result, "messages").array;
    for (size_t i = 0; i < messages.size(); ++i) {
      auto& message = messages[i];
      if (!message.GetArray("content")) continue;
      auto& content = Field(message, "content").array;
      for (size_t j = 0; j < content.size(); ++j) {
        auto& part = content[j];
        if (!Audio(part)) continue;
        active = CheckAccess(access);
        if (!active.ok()) return active;
        if (index >= inputs_.size() || !transcripts_[index]) return State();
        const auto& source = inputs_[index];
        if (source.message_index != i ||
            (exact_coordinates && source.part_index != j) ||
            message.GetString("role") != "user" ||
            source.part_sha256 != Digest(OutputJson(part)))
          return State();
        Json projected = Object(
            {{"type", String("text")},
             {"text", String(TranscriptText(source, *transcripts_[index]))}});
        if (const auto* cache = part.Find("prompt_cache_breakpoint"))
          Set(&projected, "prompt_cache_breakpoint", *cache);
        part = std::move(projected);
        ++index;
      }
    }
  }
  if (index != inputs_.size()) return State();
  active = CheckAccess(access);
  if (!active.ok()) return active;
  *output = std::move(result);
  return {};
}
ObjectReply PlanChatAudioBudget(const Json& request,
                                const ChatPlanConfig& config,
                                const ChatAudioInputs& inputs,
                                ChatAudioBudget* output) {
  if (!output || !inputs.Active() || !config.asr_limits.Valid() ||
      config.max_len < 1 || config.count_input_only)
    return State();
  ChatRequestPlan plan;
  ChatPlanError error;
  auto validation = config;
  validation.preparing_audio_inputs = true;
  validation.preparing_file_inputs = true;
  const auto status = PlanChatRequest(request, validation, &plan, &error);
  if (!status)
    return At(Error(error.http_status, error.code, status.message()),
              error.param);
  ChatAudioBudget budget;
  budget.prompt_capacity =
      uint64_t(config.max_len) * (1 + 2 * plan.moderation.enabled) +
      inputs.Inputs().size() * uint64_t(config.asr_limits.max_context_tokens);
  if (config.client_limits)
    budget.prompt_capacity = std::min<uint64_t>(
        budget.prompt_capacity, config.client_limits->max_prompt_tokens);
  if (!budget.prompt_capacity) return State();
  budget.output =
      uint64_t(ChatOutputLimit(request, config)) * plan.sampling.choices;
  budget.auxiliary_output =
      2 * plan.moderation.enabled * kMaxModerationStepTokens;
  budget.preparation_output = config.asr_limits.max_output_tokens;
  *output = budget;
  return {};
}

Json ChatAudioPreflight(const ChatAudioInputs& inputs,
                        const ChatAudioBudget& budget) {
  return Object(
      {{"object", String("q4t.preflight")},
       {"profile", String(std::string(kPreflightProfile))},
       {"status", String("requires_audio_preparation")},
       {"input", Object({{"tokens", Json()},
                         {"mode", String("context_upper_bound")},
                         {"capacity", Number(budget.prompt_capacity)}})},
       {"output",
        Object({{"reserved_tokens", Number(budget.output)},
                {"auxiliary_tokens", Number(budget.auxiliary_output)},
                {"preparation_tokens", Number(budget.preparation_output)}})},
       {"audio", Object({{"parts", Number(inputs.Inputs().size())},
                         {"transcribed", Boolean(false)}})},
       {"checks", Object({{"input", Boolean(true)},
                          {"file_inputs", Boolean(true)},
                          {"container_headers", Boolean(true)},
                          {"audio_decoded", Boolean(false)},
                          {"media", Boolean(false)},
                          {"web_search", Boolean(false)},
                          {"template", Boolean(false)},
                          {"tokenizer", Boolean(false)},
                          {"budget", Boolean(false)},
                          {"request_registered", Boolean(false)},
                          {"gpu_executed", Boolean(false)}})},
       {"execution_guaranteed", Boolean(false)}});
}
}  // namespace q4t::server
