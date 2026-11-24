#include "chat_server_internal.h"

#include <algorithm>

#include "chat_model_step.h"
#include "object_protocol.h"
#include "q4t/server/asr_alignment_output.h"

namespace q4t::server {
ObjectReply ChatServer::TranslateTranscript(
    int fd, const AsrRequest& request, std::string_view guidance,
    const AsrTranscript& transcript, int64_t duration_ms, uint64_t budget,
    std::span<const audio::AlignerWord> words, const audio::AlignerTimes& times,
    const std::shared_ptr<RequestControl>& control, ClientRateLease& rate,
    std::string* response) {
  using namespace object_detail;
  auto* http = detail::HttpResponseContext::Current(fd);
  const auto prior = http->Usage().Total();
  if (!prior || !transcript.Complete())
    return Error(502, "translation_source_incomplete",
                 "Source transcription did not finish");
  if (prior->output >= budget)
    return Error(502, "translation_output_limit", "Output budget exhausted");
  const auto remaining = budget - prior->output;
  ModelPreparation accounting(clients_, http->Client(), rate, budget, 0,
                              remaining);
  if (!accounting.ContinuePublicGeneration(prior->input, prior->output))
    return Error(503, "translation_quota_unavailable",
                 "Cannot continue the audio request's quota");
  const auto config = PlanningConfig(fd);
  ObjectReply result;
  const auto translate = [&](std::string_view source, std::string* text) {
    if (source.empty()) {
      text->clear();
      return Status{};
    }
    const auto spent = accounting.OutputTokens();
    if (spent >= remaining) {
      result = Error(502, "translation_output_limit", "Output budget exhausted");
      return Status::Fail(result.message);
    }
    // Give the model literal speech in a separate message. Imperatives and
    // questions in the transcript are examples of language to translate.
    std::string task = "You translate recorded speech into English. Preserve "
                       "questions as questions and commands as commands. "
                       "Return only the English translation.";
    auto messages = Array({Object({{"role", String("system")},
                                   {"content", String(std::move(task))}})});
    const auto message = [&](std::string role, std::string text) {
      messages.array.push_back(Object({{"role", String(std::move(role))},
                                       {"content", String(std::move(text))}}));
    };
    message("user", "Translate into English:\n请打开窗户。");
    message("assistant", "Please open the window.");
    std::string input;
    if (!guidance.empty())
      input = "English style or preceding context:\n" + std::string(guidance) + "\n\n";
    input += "Translate into English:\n";
    input += source;
    message("user", std::move(input));
    Json temperature;
    temperature.type = Json::Type::kNumber;
    temperature.number = request.temperature;
    const auto body = Object({{"model", String(config.model)},
                              {"messages", messages},
                              {"reasoning_effort", String("none")},
                              {"temperature", temperature},
                              {"max_tokens", Number(remaining - spent)}});
    InternalModelStep step{
        body, config, accounting,
        [&] {
          if (stop_requested_.load() || detail::RequestCancelled(control.get(), fd))
            return Error(409, "translation_cancelled", "Translation cancelled");
          if (!clients_.OwnsIdentity(http->Client()))
            return Error(401, "client_authentication_failed", "Client unavailable");
          return ObjectReply{};
        },
        {}, false, {}, {}, 0, 0};
    result = RunInternalModelStep(fd, &step, control);
    if (result.ok() && (step.finish_reason != "stop" || step.content.empty()))
      result = Error(502, "translation_incomplete",
                     "Native translation did not produce a complete answer");
    if (!result.ok()) return Status::Fail(result.ClientMessage());
    *text = std::move(step.content);
    return Status{};
  };
  if (request.format == AsrFormat::kSrt || request.format == AsrFormat::kVtt) {
    const auto status = AlignedAsrSubtitle(
        transcript.Text(), duration_ms, words, times, request.format == AsrFormat::kVtt,
        response, 0, 1, true, nullptr, translate);
    if (!result.ok()) return result;
    return status ? ObjectReply{} : Error(502, "translation_subtitle_invalid", status.message());
  }
  std::string text;
  if (!translate(transcript.Text(), &text)) return result;
  if (request.format == AsrFormat::kText) {
    *response = std::move(text);
  } else {
    auto value = Object({{"text", String(std::move(text))}});
    if (request.format == AsrFormat::kVerboseJson) {
      Json seconds;
      seconds.type = Json::Type::kNumber;
      seconds.number = duration_ms / 1000.;
      Set(&value, "language", String("english"));
      Set(&value, "duration", std::move(seconds));
    }
    *response = OutputJson(value);
  }
  return {};
}
}  // namespace q4t::server
