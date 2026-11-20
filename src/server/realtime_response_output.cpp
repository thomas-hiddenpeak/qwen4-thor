#include "q4t/server/realtime_response_output.h"

#include <algorithm>

#include "object_protocol.h"
#include "q4t/io/base64.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/lifetime.h"
#include "q4t/server/realtime_shape.h"
#include "q4t/server/request_json.h"
#include "q4t/server/responses_mcp_binding.h"

namespace q4t::server {
namespace {
using namespace object_detail;
std::string_view StateName(RealtimeResponseEnd::State state) {
  switch (state) {
    case RealtimeResponseEnd::State::kCompleted:
      return "completed";
    case RealtimeResponseEnd::State::kIncomplete:
      return "incomplete";
    case RealtimeResponseEnd::State::kCancelled:
      return "cancelled";
    case RealtimeResponseEnd::State::kFailed:
      return "failed";
  }
  return "";
}
Json TextPart(std::string text) {
  return Object(
      {{"type", String("output_text")}, {"text", String(std::move(text))}});
}
Json EventTextPart(std::string text) {
  // The pinned GA item and content-part event have distinct discriminator
  // values: output_text in conversation items, text in content-part events.
  return Object({{"type", String("text")}, {"text", String(std::move(text))}});
}
Json AudioPart(std::string transcript, bool event = false) {
  return Object({{"type", String(event ? "audio" : "output_audio")},
                 {"transcript", String(std::move(transcript))}});
}
Json TextMessage(std::string id) {
  return Object({{"id", String(std::move(id))},
                 {"object", String("realtime.item")},
                 {"type", String("message")},
                 {"role", String("assistant")},
                 {"status", String("in_progress")},
                 {"content", Array({TextPart("")})}});
}
bool MetadataBounded(const Json& metadata) {
  if (metadata.IsNull()) return true;
  if (!metadata.IsObject() || metadata.object.size() > 16) return false;
  const auto characters = [](std::string_view text) {
    return std::count_if(text.begin(), text.end(),
                         [](unsigned char c) { return (c & 0xc0) != 0x80; });
  };
  for (const auto& [key, value] : metadata.object)
    if (characters(key) > 64 || !value.IsString() ||
        characters(value.str) > 512)
      return false;
  return true;
}
bool Categories(uint64_t total,
                std::initializer_list<std::optional<uint64_t>> values) {
  bool complete = true;
  for (const auto value : values) {
    complete &= value.has_value();
    if (!value) continue;
    if (*value > total) return false;
    total -= *value;
  }
  return !complete || total == 0;
}
bool UsageValid(const RealtimeResponseUsage& usage) {
  return ValidActualUsage(usage.total) &&
         Categories(usage.total.input,
                    {usage.input_text, usage.input_audio, usage.input_image}) &&
         Categories(usage.total.output,
                    {usage.output_text, usage.output_audio});
}
void Category(Json* object, std::string_view name,
              std::optional<uint64_t> value) {
  if (value) Set(object, name, Number(*value));
}
}  // namespace

bool RealtimeMetadataBounded(const Json& metadata) {
  return MetadataBounded(metadata);
}

RealtimeResponseOutput::RealtimeResponseOutput(
    RealtimeResponseOutputOptions options, EventSink emit, ItemSink item,
    IdFactory ids, AudioSink audio)
    : options_(std::move(options)),
      emit_(std::move(emit)),
      item_(std::move(item)),
      ids_(std::move(ids)),
      audio_(std::move(audio)) {}

ObjectReply RealtimeResponseOutput::Fail(std::string_view code,
                                         std::string_view message) {
  if (error_.ok())
    error_ = object_detail::Error(503, std::string(code), std::string(message));
  return error_;
}
bool RealtimeResponseOutput::AcceptSink(ObjectReply reply) {
  if (reply.ok() && reply.http_status >= 200 && reply.http_status < 300 &&
      !reply.commit_unknown)
    return true;
  if (reply.code.empty()) {
    reply.code = "realtime_sink_result_invalid";
    reply.message = "Output sink did not confirm acceptance";
    reply.http_status = 503;
  }
  sink_failed_ = true;
  error_ = std::move(reply);
  return false;
}
ObjectReply RealtimeResponseOutput::Emit(std::string_view type, Json fields) {
  if (!error_.ok()) return error_;
  const auto id = ids_("event_");
  if (!ValidRequestId(id))
    return Fail("realtime_event_identity_invalid", "Invalid event identity");
  Set(&fields, "type", String(std::string(type)));
  Set(&fields, "event_id", String(id));
  AcceptSink(emit_(fields));
  return error_;
}
Json RealtimeResponseOutput::Output() const {
  auto result = Array();
  for (const auto& item : items_) result.array.push_back(item.value);
  return result;
}
Json RealtimeResponseOutput::Response(std::string_view status) const {
  auto result =
      Object({{"id", String(options_.response_id)},
              {"object", String("realtime.response")},
              {"status", String(std::string(status))},
              {"output", Output()},
              {"output_modalities",
               Array({String(options_.audio_format ? "audio" : "text")})}});
  if (options_.audio_format) {
    const auto& format = *options_.audio_format;
    const bool pcm = format.encoding == RealtimeAudioEncoding::kPcm16;
    auto encoded = Object(
        {{"type", String(pcm ? "audio/pcm"
                         : format.encoding == RealtimeAudioEncoding::kMuLaw
                             ? "audio/pcmu"
                             : "audio/pcma")}});
    if (pcm) Set(&encoded, "rate", Number(format.rate));
    Set(&result, "audio", Object({{"output", Object({{"format", encoded}})}}));
  }
  if (options_.in_conversation)
    Set(&result, "conversation_id", String(options_.conversation_id));
  if (!options_.metadata.IsNull()) Set(&result, "metadata", options_.metadata);
  return result;
}
ObjectReply RealtimeResponseOutput::Begin() {
  if (begun_ || terminal_ || !emit_ || !item_ || !ids_ ||
      !ValidRequestId(options_.response_id) ||
      (options_.in_conversation && !ValidRequestId(options_.conversation_id)) ||
      !options_.maximum_bytes || options_.maximum_bytes > (16ULL << 20) ||
      !options_.maximum_items || options_.maximum_items > 128 ||
      !MetadataBounded(options_.metadata))
    return Fail("realtime_response_owner_invalid", "Invalid response owner");
  if (options_.audio_format &&
      (!audio_ || !RealtimeSpeechEncoder(*options_.audio_format, 1,
                                         options_.maximum_audio_bytes)
                       .Valid()))
    return Fail("realtime_response_owner_invalid", "Invalid audio owner");
  if (options_.mcp_bindings.size() > 128)
    return Fail("realtime_mcp_binding_invalid",
                "MCP output bindings exceed capacity");
  for (size_t i = 0; i < options_.mcp_bindings.size(); ++i) {
    const auto& binding = options_.mcp_bindings[i];
    if (!ValidInputName(binding.model_name) || binding.server_label.empty() ||
        binding.server_label.size() > 256 || binding.name.empty() ||
        binding.name.size() > 256 || binding.model_name.capacity() > 1024 ||
        binding.server_label.capacity() > 1024 ||
        binding.name.capacity() > 1024 ||
        binding.model_name !=
            ResponsesMcpModelName(binding.server_label, binding.name))
      return Fail("realtime_mcp_binding_invalid", "Invalid MCP output binding");
    for (size_t j = 0; j < i; ++j) {
      const auto& previous = options_.mcp_bindings[j];
      if (binding.model_name == previous.model_name ||
          (binding.server_label == previous.server_label &&
           binding.name == previous.name))
        return Fail("realtime_mcp_binding_invalid",
                    "Duplicate MCP output binding");
    }
  }
  auto response = Response("in_progress");
  ChatPlanError shape;
  if (!ValidateRealtimeShape(response, RealtimeShape::kResponse, &shape))
    return Fail("realtime_response_invalid", "Invalid response metadata");
  begun_ = true;
  return Emit("response.created", Object({{"response", std::move(response)}}));
}
Json RealtimeResponseOutput::Position(size_t index, bool content) const {
  auto fields =
      Object({{"response_id", String(options_.response_id)},
              {"item_id", String(items_[index].value.GetString("id"))},
              {"output_index", Number(index)}});
  if (content) Set(&fields, "content_index", Number(0));
  return fields;
}
ObjectReply RealtimeResponseOutput::AddItem(Json value,
                                            std::optional<size_t> call_index,
                                            size_t* output) {
  if (items_.size() == options_.maximum_items)
    return Fail("realtime_output_capacity_exhausted",
                "Output item limit exceeded");
  const auto id = value.GetString("id");
  if (!ValidRequestId(id) || std::ranges::any_of(items_, [&](const auto& item) {
        return item.value.GetString("id") == id;
      }))
    return Fail("realtime_item_identity_invalid",
                "Invalid output item identity");
  const auto stored = item_(value, true, false);
  if (!AcceptSink(stored)) return error_;
  const auto index = items_.size();
  items_.push_back({std::move(value),
                    stored.value.GetString("previous_item_id"), call_index});
  auto reply = Emit("response.output_item.added",
                    Object({{"response_id", String(options_.response_id)},
                            {"output_index", Number(index)},
                            {"item", items_.back().value}}));
  if (!reply.ok()) return reply;
  if (options_.in_conversation) {
    auto event = Object({{"item", items_.back().value}});
    if (!items_.back().previous.empty())
      Set(&event, "previous_item_id", String(items_.back().previous));
    reply = Emit("conversation.item.added", std::move(event));
    if (!reply.ok()) return reply;
  }
  if (!call_index) {
    if (options_.audio_format) audio_index_ = index;
    auto event = Position(index, true);
    Set(&event, "part",
        options_.audio_format ? AudioPart("", true) : EventTextPart(""));
    reply = Emit("response.content_part.added", std::move(event));
    if (!reply.ok()) return reply;
  }
  *output = index;
  return {};
}
ObjectReply RealtimeResponseOutput::Append(const ResponseDelta& delta) {
  if (!error_.ok()) return error_;
  if (!begun_ || terminal_)
    return Fail("realtime_response_state_invalid", "Response is not active");
  if (audio_truncated_) return {};
  if (delta.kind == ResponseDelta::Kind::kReasoning) return {};
  const bool tool = delta.kind == ResponseDelta::Kind::kTool;
  const auto& piece = tool ? delta.call.arguments : delta.text;
  if (!tool && audio_complete_ && !piece.empty())
    return Fail("realtime_audio_transcript_sealed",
                "Transcript changed after speech completed");
  if (piece.size() > options_.maximum_bytes - bytes_)
    return Fail("realtime_output_capacity_exhausted",
                "Output text limit exceeded");
  if (piece.empty() && !tool) return {};
  size_t index = items_.size();
  if (tool) {
    if (delta.call.custom || delta.legacy_function)
      return Fail("realtime_tool_shape_invalid",
                  "Expected a typed function call");
    for (size_t i = 0; i < items_.size(); ++i)
      if (items_[i].call_index == delta.call.index) index = i;
    if (delta.tool_start) {
      if (index != items_.size() || !ValidRequestId(delta.call.id) ||
          !ValidInputName(delta.call.name))
        return Fail("realtime_tool_identity_invalid",
                    "Invalid function identity");
      std::optional<size_t> mcp;
      for (size_t i = 0; i < options_.mcp_bindings.size(); ++i)
        if (options_.mcp_bindings[i].model_name == delta.call.name) mcp = i;
      Json value;
      if (mcp) {
        const auto& binding = options_.mcp_bindings[*mcp];
        value = Object({{"id", String(ids_("mcp_"))},
                        {"type", String("mcp_call")},
                        {"server_label", String(binding.server_label)},
                        {"name", String(binding.name)},
                        {"arguments", String("")}});
      } else {
        value = Object({{"id", String(ids_("item_"))},
                        {"object", String("realtime.item")},
                        {"type", String("function_call")},
                        {"status", String("in_progress")},
                        {"call_id", String(delta.call.id)},
                        {"name", String(delta.call.name)},
                        {"arguments", String("")}});
      }
      auto reply = AddItem(std::move(value), delta.call.index, &index);
      if (!reply.ok()) return reply;
      items_[index].mcp_binding = mcp;
      items_[index].model_call_id = delta.call.id;
      items_[index].model_name = delta.call.name;
    } else if (index == items_.size() ||
               (!delta.call.id.empty() &&
                delta.call.id != items_[index].model_call_id) ||
               (!delta.call.name.empty() &&
                delta.call.name != items_[index].model_name)) {
      return Fail("realtime_tool_identity_invalid",
                  "Unbound function fragment");
    }
  } else if (options_.audio_format && audio_index_) {
    index = *audio_index_;
  } else if (items_.empty() || items_.back().call_index) {
    auto message = TextMessage(ids_("item_"));
    if (options_.audio_format) Set(&message, "content", Array({AudioPart("")}));
    auto reply = AddItem(std::move(message), {}, &index);
    if (!reply.ok()) return reply;
  } else {
    index = items_.size() - 1;
  }
  auto next = items_[index].value;
  if (tool)
    Field(next, "arguments").str += piece;
  else
    Field(Field(next, "content").array[0],
          options_.audio_format ? "transcript" : "text")
        .str += piece;
  const auto stored = item_(next, false, false);
  if (!AcceptSink(stored)) return error_;
  items_[index].deleted |= stored.value.GetBool("deleted");
  items_[index].value = std::move(next);
  bytes_ += piece.size();
  auto event = Position(index, !tool);
  Set(&event, "delta", String(piece));
  const bool mcp = tool && items_[index].mcp_binding.has_value();
  if (tool && !mcp)
    Set(&event, "call_id", String(items_[index].value.GetString("call_id")));
  return Emit(mcp                     ? "response.mcp_call_arguments.delta"
              : tool                  ? "response.function_call_arguments.delta"
              : options_.audio_format ? "response.output_audio_transcript.delta"
                                      : "response.output_text.delta",
              std::move(event));
}
ObjectReply RealtimeResponseOutput::AppendAudio(std::string_view bytes) {
  if (!error_.ok()) return error_;
  if (!begun_ || terminal_ || !options_.audio_format || !audio_index_)
    return Fail("realtime_audio_owner_invalid", "No audio utterance owner");
  if (audio_truncated_) return {};
  const size_t width =
      options_.audio_format->encoding == RealtimeAudioEncoding::kPcm16 ? 2 : 1;
  if (audio_complete_ || bytes.empty() || bytes.size() % width ||
      bytes.size() > RealtimeSpeechEncoder::kMaximumChunkBytes ||
      bytes.size() > options_.maximum_audio_bytes - audio_bytes_)
    return Fail("realtime_audio_extent_invalid", "Invalid audio chunk extent");
  std::string encoded;
  if (!io::EncodeBase64(bytes, 4 * ((bytes.size() + 2) / 3), &encoded))
    return Fail("realtime_audio_encoding_failed", "Audio encoding failed");
  const auto& item = items_[*audio_index_].value;
  if (!AcceptSink(
          audio_(item.GetString("id"), bytes, RealtimeAudioCommit::kAppend)))
    return error_;
  audio_bytes_ += bytes.size();
  auto event = Position(*audio_index_, true);
  Set(&event, "delta", String(std::move(encoded)));
  return Emit("response.output_audio.delta", std::move(event));
}
ObjectReply RealtimeResponseOutput::CompleteAudio(
    const RealtimeSpeechReceipt& receipt, std::string_view spoken_input) {
  if (!error_.ok()) return error_;
  if (!begun_ || terminal_ || !options_.audio_format || !audio_index_)
    return Fail("realtime_audio_owner_invalid", "No audio utterance owner");
  if (audio_truncated_) return {};
  const auto format = *options_.audio_format;
  const size_t width = format.encoding == RealtimeAudioEncoding::kPcm16 ? 2 : 1;
  if (audio_complete_ || !audio_bytes_ || receipt.bytes != audio_bytes_ ||
      receipt.output_samples != audio_bytes_ / width ||
      !receipt.input_samples || receipt.input_samples > 600ULL * 24000 ||
      receipt.output_samples != receipt.input_samples * format.rate / 24000 ||
      spoken_input !=
          items_[*audio_index_].value.GetArray("content")->array[0].GetString(
              "transcript"))
    return Fail("realtime_audio_receipt_invalid", "Audio receipt differs");
  if (!AcceptSink(audio_(items_[*audio_index_].value.GetString("id"), {},
                         RealtimeAudioCommit::kComplete)))
    return error_;
  audio_complete_ = true;
  return {};
}
ObjectReply RealtimeResponseOutput::TruncateAudio(std::string_view item_id) {
  if (!error_.ok()) return error_;
  if (!begun_ || terminal_ || !audio_index_ ||
      items_[*audio_index_].value.GetString("id") != item_id)
    return Fail("realtime_audio_owner_invalid", "No matching audio owner");
  auto& part = Field(items_[*audio_index_].value, "content").array[0];
  Field(part, "transcript").str.clear();
  audio_truncated_ = true;
  return {};
}
bool RealtimeResponseOutput::Matches(const Json& message,
                                     bool truncated) const {
  if (!message.IsObject() || message.GetString("role") != "assistant")
    return false;
  std::string text;
  size_t call = 0;
  const auto* calls = message.GetArray("tool_calls");
  for (const auto& item : items_) {
    if (!item.call_index) {
      text += item.value.GetArray("content")->array[0].GetString(
          options_.audio_format ? "transcript" : "text");
      continue;
    }
    if (!calls || call == calls->array.size()) {
      // Chat's finite message intentionally contains only closed calls. The
      // shared parser can expose one trailing provisional call before length
      // exhaustion; preserve it only in an explicit incomplete Realtime item.
      if (truncated && &item == &items_.back()) continue;
      return false;
    }
    const auto& actual = calls->array[call++];
    const auto* function = actual.Find("function");
    if (actual.GetString("type") != "function" || !function ||
        actual.GetString("id") != item.model_call_id ||
        function->GetString("name") != item.model_name ||
        function->GetString("arguments") != item.value.GetString("arguments"))
      return false;
  }
  return call == (calls ? calls->array.size() : 0) &&
         text == message.GetString("content") + message.GetString("refusal");
}
ObjectReply RealtimeResponseOutput::Finish(const RealtimeResponseEnd& end) {
  if (!error_.ok() &&
      (sink_failed_ || end.state != RealtimeResponseEnd::State::kFailed))
    return error_;
  if (!begun_ || terminal_)
    return Fail("realtime_response_state_invalid", "Response is not active");
  // A local resource/model failure still needs a failed response.done while
  // the transport is healthy. Failed event/history callbacks are never blindly
  // replayed: a partial finalization may already have published earlier items.
  // The connection owner closes when acceptance cannot be established.
  auto prior_error = std::move(error_);
  error_ = {};
  ScopeExit restore_error([&] {
    if (!prior_error.ok() && error_.ok()) error_ = std::move(prior_error);
  });
  const bool has_message =
      end.state == RealtimeResponseEnd::State::kCompleted ||
      end.state == RealtimeResponseEnd::State::kIncomplete;
  const bool truncated = end.state == RealtimeResponseEnd::State::kIncomplete &&
                         end.reason == "max_output_tokens";
  if (has_message && (!end.message || !Matches(*end.message, truncated)))
    return Fail("realtime_output_mismatch",
                "Actual response does not match deltas");
  if (has_message && audio_index_ && (!audio_complete_ || audio_truncated_))
    return Fail("realtime_audio_incomplete", "Audio did not reach natural EOF");
  if (end.state == RealtimeResponseEnd::State::kCompleted) {
    for (const auto& item : items_) {
      if (!item.mcp_binding || item.deleted) continue;
      Json arguments;
      if (!io::ParseJson(item.value.GetString("arguments"), &arguments, true,
                         kSchemaJsonLimits) ||
          !arguments.IsObject())
        return Fail("realtime_mcp_arguments_invalid",
                    "Invalid complete MCP arguments");
    }
  }
  auto response = Response(StateName(end.state));
  if (end.state != RealtimeResponseEnd::State::kCompleted) {
    auto details =
        Object({{"type", String(std::string(StateName(end.state)))}});
    if (!end.reason.empty()) Set(&details, "reason", String(end.reason));
    if (end.state == RealtimeResponseEnd::State::kFailed)
      Set(&details, "error",
          Object({{"type", String("server_error")},
                  {"code", String(end.error_code)},
                  {"message", String(end.error_message.substr(0, 1024))}}));
    Set(&response, "status_details", std::move(details));
  }
  // Missing/bad observation cannot turn a healthy model answer into a failed
  // response. Quota settlement remains the primary request owner's job; never
  // replace unavailable observations with fabricated zero usage.
  usage_available_ = end.usage && UsageValid(*end.usage);
  if (usage_available_) {
    const auto& u = end.usage->total;
    auto input = Object({{"cached_tokens", Number(u.cached)}});
    auto output = Object();
    Category(&input, "text_tokens", end.usage->input_text);
    Category(&input, "audio_tokens", end.usage->input_audio);
    Category(&input, "image_tokens", end.usage->input_image);
    Category(&output, "text_tokens", end.usage->output_text);
    Category(&output, "audio_tokens", end.usage->output_audio);
    Set(&response, "usage",
        Object({{"input_tokens", Number(u.input)},
                {"output_tokens", Number(u.output)},
                {"total_tokens", Number(u.input + u.output)},
                {"input_token_details", std::move(input)},
                {"output_token_details", std::move(output)}}));
  }
  ChatPlanError shape;
  if (!ValidateRealtimeShape(response, RealtimeShape::kResponse, &shape))
    return Fail("realtime_terminal_invalid",
                "Invalid response terminal metadata");
  if (audio_index_ && !audio_complete_ &&
      !AcceptSink(audio_(items_[*audio_index_].value.GetString("id"), {},
                         RealtimeAudioCommit::kInterrupted)))
    return error_;
  const auto status = end.state == RealtimeResponseEnd::State::kCompleted
                          ? "completed"
                          : "incomplete";
  for (size_t i = 0; i < items_.size(); ++i) {
    auto& item = items_[i];
    if (item.mcp_binding) {
      auto reply = FinishMcpArguments(
          i, end.state == RealtimeResponseEnd::State::kCompleted);
      if (!reply.ok()) return reply;
      continue;
    }
    auto final = item.value;
    Set(&final, "status", String(status));
    const auto stored = item_(final, false, true);
    if (!AcceptSink(stored)) return error_;
    item.deleted |= stored.value.GetBool("deleted");
    item.value = std::move(final);
    auto event = Position(i, !item.call_index);
    if (item.call_index) {
      for (const auto* field : {"call_id", "name", "arguments"})
        Set(&event, field, String(item.value.GetString(field)));
      auto reply =
          Emit("response.function_call_arguments.done", std::move(event));
      if (!reply.ok()) return reply;
    } else {
      const auto& part = item.value.GetArray("content")->array[0];
      if (options_.audio_format) {
        auto reply = Emit("response.output_audio.done", event);
        if (!reply.ok()) return reply;
      }
      const auto text =
          part.GetString(options_.audio_format ? "transcript" : "text");
      Set(&event, options_.audio_format ? "transcript" : "text", String(text));
      auto reply =
          Emit(options_.audio_format ? "response.output_audio_transcript.done"
                                     : "response.output_text.done",
               std::move(event));
      if (!reply.ok()) return reply;
      event = Position(i, true);
      Set(&event, "part",
          options_.audio_format ? AudioPart(text, true) : EventTextPart(text));
      reply = Emit("response.content_part.done", std::move(event));
      if (!reply.ok()) return reply;
    }
    auto reply = Emit("response.output_item.done",
                      Object({{"response_id", String(options_.response_id)},
                              {"output_index", Number(i)},
                              {"item", item.value}}));
    if (!reply.ok()) return reply;
    if (options_.in_conversation && !item.deleted) {
      event = Object({{"item", item.value}});
      if (!item.previous.empty())
        Set(&event, "previous_item_id", String(item.previous));
      reply = Emit("conversation.item.done", std::move(event));
      if (!reply.ok()) return reply;
    }
  }
  Set(&response, "output", Output());
  terminal_ = true;
  return Emit("response.done", Object({{"response", std::move(response)}}));
}

ObjectReply RealtimeResponseOutput::FinishMcpArguments(size_t index,
                                                       bool complete) {
  auto& item = items_[index];
  auto final = item.value;
  if (!complete)
    Set(&final, "error",
        Object({{"type", String("protocol_error")},
                {"code", Number(-32603)},
                {"message",
                 String("Generation did not complete; tool not executed")}}));
  // Keep a successful generated call pending: response.done is not a remote
  // tool completion. The new owner must finish the exact history ticket.
  const auto stored = item_(final, false, !complete);
  if (!AcceptSink(stored)) return error_;
  item.deleted |= stored.value.GetBool("deleted");
  item.value = std::move(final);
  auto fields = Position(index, false);
  Set(&fields, "arguments", *item.value.Find("arguments"));
  auto reply = Emit("response.mcp_call_arguments.done", std::move(fields));
  if (!reply.ok()) return reply;
  if (complete) {
    if (!item.deleted)
      mcp_calls_.push_back({item.value, index, *item.mcp_binding});
    return {};
  }
  reply = Emit("response.mcp_call.failed",
               Object({{"item_id", *item.value.Find("id")},
                       {"output_index", Number(index)}}));
  if (!reply.ok()) return reply;
  reply = Emit("response.output_item.done",
               Object({{"response_id", String(options_.response_id)},
                       {"output_index", Number(index)},
                       {"item", item.value}}));
  if (!reply.ok()) return reply;
  if (options_.in_conversation && !item.deleted) {
    auto event = Object({{"item", item.value}});
    if (!item.previous.empty())
      Set(&event, "previous_item_id", String(item.previous));
    reply = Emit("conversation.item.done", std::move(event));
  }
  return reply;
}
std::vector<RealtimeMcpGeneratedCall> RealtimeResponseOutput::TakeMcpCalls() {
  if (!terminal_ || sink_failed_ || !error_.ok()) return {};
  auto result = std::move(mcp_calls_);
  mcp_calls_.clear();
  return result;
}
}  // namespace q4t::server
