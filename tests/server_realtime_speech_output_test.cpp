#include "q4t/server/realtime_response_output.h"

#include <algorithm>
#include <cstdio>
#include <map>

#include "q4t/server/realtime_shape.h"
#include "q4t/server/realtime_speech_history.h"
#include "q4t/test.h"
#include "../src/server/object_protocol.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
RealtimeResponseOutputOptions Options(RealtimeAudioFormat format) {
  RealtimeResponseOutputOptions result;
  result.response_id = "resp_a";
  result.conversation_id = "conv_a";
  result.audio_format = format;
  result.maximum_audio_bytes = 65536;
  return result;
}
ResponseDelta Text(std::string text) {
  ResponseDelta delta;
  delta.text = std::move(text);
  return delta;
}
Json Message(std::string text) {
  return Object(
      {{"role", String("assistant")}, {"content", String(std::move(text))}});
}
RealtimeResponseEnd End(const Json* message = nullptr) {
  RealtimeResponseEnd result;
  result.message = message;
  RealtimeResponseUsage usage;
  usage.total = {11, 7, 0, 0, 0};
  result.usage = usage;
  return result;
}
bool Decode(std::string_view encoded, std::string* bytes) {
  constexpr std::string_view alphabet =
      "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  Q4T_CHECK(!encoded.empty() && encoded.size() % 4 == 0);
  for (size_t i = 0; i < encoded.size(); i += 4) {
    uint32_t value = 0;
    size_t padding = 0;
    for (size_t j = 0; j < 4; ++j) {
      if (encoded[i + j] == '=') {
        Q4T_CHECK(j >= 2 && i + 4 == encoded.size());
        ++padding;
        value <<= 6;
      } else {
        const auto digit = alphabet.find(encoded[i + j]);
        Q4T_CHECK(!padding && digit != alphabet.npos);
        value = value * 64 + digit;
      }
    }
    bytes->push_back(char(value >> 16));
    if (padding < 2) bytes->push_back(char(value >> 8));
    if (!padding) bytes->push_back(char(value));
  }
  return true;
}
struct Fixture {
  RealtimeAudioFormat format;
  RealtimeConversation history{"sess_a"};
  RealtimeSpeechHistory speech{"sess_a"};
  std::map<std::string, RealtimeItemTicket> tickets;
  size_t ids = 0, audio_calls = 0;
  bool unknown_audio_write = false;
  std::vector<Json> events;
  RealtimeResponseOutput output;
  explicit Fixture(RealtimeAudioFormat selected = {})
      : format(selected),
        output(
            Options(selected),
            [this](const Json& event) {
              ChatPlanError error;
              const auto status = ValidateRealtimeShape(
                  event, RealtimeShape::kServerEvent, &error);
              if (!status) {
                std::fprintf(stderr, "%s at %s: %s\n",
                             event.GetString("type").c_str(),
                             error.param.c_str(), status.message().c_str());
                return Error(500, "fixture_shape", status.message());
              }
              events.push_back(event);
              return ObjectReply{};
            },
            [this](const Json& item, bool begin, bool terminal) {
              const auto id = item.GetString("id");
              q4t::Status status;
              if (begin) {
                RealtimeItemTicket ticket;
                status = history.BeginOutput(item, id, true, 16384, &ticket);
                if (status) {
                  tickets.emplace(id, ticket);
                  if (item.GetString("type") == "message")
                    status = speech.Begin(ticket, format, 65536);
                }
              } else {
                status = history.Update(tickets.at(id), item, terminal);
              }
              return status ? ObjectReply{}
                            : Error(503, "fixture_history", status.message());
            },
            [this](std::string_view prefix) {
              return std::string(prefix) + std::to_string(++ids);
            },
            [this](std::string_view id, std::string_view bytes,
                   RealtimeAudioCommit commit) {
              ++audio_calls;
              const auto& ticket = tickets.at(std::string(id));
              auto status = commit == RealtimeAudioCommit::kAppend
                                ? speech.Append(ticket, bytes)
                            : commit == RealtimeAudioCommit::kComplete
                                ? speech.Complete(ticket)
                                : speech.Interrupt(ticket);
              auto reply = status
                               ? ObjectReply{}
                               : Error(503, "fixture_audio", status.message());
              if (unknown_audio_write && commit == RealtimeAudioCommit::kAppend)
                reply.commit_unknown = true;
              return reply;
            }) {}
  size_t Count(std::string_view type) const {
    return std::count_if(events.begin(), events.end(), [&](const auto& event) {
      return event.GetString("type") == type;
    });
  }
  std::string AudioId() const {
    for (const auto& event : events)
      if (event.GetString("type") == "response.content_part.added")
        return event.GetString("item_id");
    return {};
  }
  bool Audio(std::string_view pcm, std::string_view transcript) {
    RealtimeSpeechEncoder encoder(format, pcm.size() / 2, 65536);
    const auto sink = [&](std::string_view bytes) {
      return output.AppendAudio(bytes);
    };
    for (size_t i = 0; i < pcm.size(); i += 997)
      Q4T_CHECK(encoder.Append(pcm.substr(i, 997), sink).ok());
    RealtimeSpeechReceipt receipt;
    Q4T_CHECK(encoder.Finish(sink, &receipt).ok());
    Q4T_CHECK(output.CompleteAudio(receipt, transcript).ok());
    return true;
  }
};
}  // namespace

Q4T_TEST(realtime_speech_output_actual_pcm_roundtrips_standard_events) {
  Fixture f;
  Q4T_CHECK(f.output.Begin().ok());
  Q4T_CHECK(f.output.Append(Text("Hello ")).ok());
  Q4T_CHECK(f.output.Append(Text("世界")).ok());
  std::string pcm;
  for (size_t i = 0; i < 12000; ++i) pcm.push_back(char(i & 255));
  Q4T_CHECK(f.Audio(pcm, "Hello 世界"));
  auto message = Message("Hello 世界");
  Q4T_CHECK(f.output.Finish(End(&message)).ok());
  std::string actual;
  for (const auto& event : f.events) {
    if (event.GetString("type") == "response.output_audio.delta")
      Q4T_CHECK(Decode(event.GetString("delta"), &actual));
    if (event.GetString("type") == "response.done") {
      const auto& response = *event.Find("response");
      Q4T_CHECK(response.GetString("status") == "completed");
      Q4T_CHECK(response.GetArray("output_modalities")->array[0].str ==
                "audio");
      Q4T_CHECK(!response.GetArray("output")
                     ->array[0]
                     .GetArray("content")
                     ->array[0]
                     .Has("audio"));
    }
  }
  Q4T_CHECK(actual == pcm);
  Q4T_CHECK(f.Count("response.output_audio.done") == 1);
  Q4T_CHECK(f.Count("response.output_audio_transcript.delta") == 2);
  Q4T_CHECK(f.Count("response.output_audio_transcript.done") == 1);
  Q4T_CHECK(f.Count("response.output_text.delta") == 0);
  RealtimeSpeechView view;
  Q4T_CHECK(f.speech.Get(f.tickets.at(f.AudioId()), &view));
  Q4T_CHECK(view.complete && view.bytes == pcm);
  return true;
}

Q4T_TEST(realtime_speech_output_g711_actual_bytes_and_format_remain_bound) {
  for (const auto kind :
       {RealtimeAudioEncoding::kMuLaw, RealtimeAudioEncoding::kALaw}) {
    Fixture f({kind, 8000});
    Q4T_CHECK(f.output.Begin().ok());
    Q4T_CHECK(f.output.Append(Text("hello")).ok());
    Q4T_CHECK(f.Audio(std::string(6002, '\0'), "hello"));
    auto message = Message("hello");
    Q4T_CHECK(f.output.Finish(End(&message)).ok());
    std::string bytes;
    for (const auto& event : f.events)
      if (event.GetString("type") == "response.output_audio.delta")
        Q4T_CHECK(Decode(event.GetString("delta"), &bytes));
    Q4T_CHECK(
        bytes ==
        std::string(1000,
                    char(kind == RealtimeAudioEncoding::kMuLaw ? 255 : 213)));
    RealtimeSpeechView view;
    Q4T_CHECK(f.speech.Get(f.tickets.at(f.AudioId()), &view));
    Q4T_CHECK(view.bytes == bytes && view.format.encoding == kind);
    Q4T_CHECK(view.generated_samples == 1000 && view.complete);
  }
  return true;
}

Q4T_TEST(realtime_speech_output_missing_eof_fails_without_fake_completion) {
  Fixture f;
  Q4T_CHECK(f.output.Begin().ok());
  Q4T_CHECK(f.output.Append(Text("hello")).ok());
  Q4T_CHECK(f.output.AppendAudio(std::string(1000, '\0')).ok());
  auto message = Message("hello");
  Q4T_CHECK(!f.output.Finish(End(&message)).ok());
  auto end = End();
  end.state = RealtimeResponseEnd::State::kFailed;
  end.error_code = "fixture_no_eof";
  end.error_message = "Actual synthesis did not finish";
  Q4T_CHECK(f.output.Finish(end).ok());
  Q4T_CHECK(f.output.Terminal() && f.Count("response.done") == 1);
  Q4T_CHECK(f.events.back().Find("response")->GetString("status") == "failed");
  RealtimeSpeechView view;
  Q4T_CHECK(f.speech.Get(f.tickets.at(f.AudioId()), &view));
  Q4T_CHECK(view.bytes.size() == 1000 && view.interrupted && !view.complete);
  return true;
}

Q4T_TEST(realtime_speech_output_tool_only_and_mixed_utterances_keep_identity) {
  for (bool spoken : {false, true}) {
    Fixture f;
    Q4T_CHECK(f.output.Begin().ok());
    if (spoken) Q4T_CHECK(f.output.Append(Text("before ")).ok());
    ResponseDelta tool;
    tool.kind = ResponseDelta::Kind::kTool;
    tool.tool_start = true;
    tool.call.id = "call_a";
    tool.call.name = "lookup";
    tool.call.arguments = "{}";
    Q4T_CHECK(f.output.Append(tool).ok());
    if (spoken) {
      Q4T_CHECK(f.output.Append(Text("after")).ok());
      Q4T_CHECK(f.Audio(std::string(1000, '\0'), "before after"));
    }
    auto message = Message(spoken ? "before after" : "");
    Set(&message, "tool_calls",
        Array({Object({{"id", String("call_a")},
                       {"type", String("function")},
                       {"function", Object({{"name", String("lookup")},
                                            {"arguments", String("{}")}})}})}));
    Q4T_CHECK(f.output.Finish(End(&message)).ok());
    Q4T_CHECK(f.output.Output().array.size() == size_t(spoken ? 2 : 1));
    Q4T_CHECK(f.Count("response.output_audio.done") == size_t(spoken ? 1 : 0));
    Q4T_CHECK(f.Count("response.function_call_arguments.done") == 1);
    if (!spoken) Q4T_CHECK(f.speech.Items() == 0 && f.audio_calls == 0);
  }
  return true;
}

Q4T_TEST(realtime_speech_output_truncate_discards_late_audio_and_transcript) {
  Fixture f;
  Q4T_CHECK(f.output.Begin().ok());
  Q4T_CHECK(f.output.Append(Text("partly spoken")).ok());
  Q4T_CHECK(f.output.AppendAudio(std::string(2000, '\1')).ok());
  const auto id = f.AudioId();
  Q4T_CHECK(f.speech.Truncate(f.tickets.at(id), 20));
  Q4T_CHECK(f.output.TruncateAudio(id).ok());
  const auto count = f.events.size();
  Q4T_CHECK(f.output.AppendAudio(std::string(1000, '\2')).ok());
  Q4T_CHECK(f.output.Append(Text("late unplayed text")).ok());
  Q4T_CHECK(f.events.size() == count);
  auto end = End();
  end.state = RealtimeResponseEnd::State::kCancelled;
  end.reason = "client_cancelled";
  Q4T_CHECK(f.output.Finish(end).ok());
  Q4T_CHECK(f.events.back().Find("response")->GetString("status") ==
            "cancelled");
  RealtimeSpeechView view;
  Q4T_CHECK(f.speech.Get(f.tickets.at(id), &view));
  Q4T_CHECK(view.bytes.size() == 960 && view.generated_samples == 1000);
  Q4T_CHECK(view.truncated && !view.complete);
  Json item;
  Q4T_CHECK(f.history.Get(f.tickets.at(id), &item));
  Q4T_CHECK(item.GetArray("content")->array[0].GetString("transcript").empty());
  return true;
}

Q4T_TEST(realtime_speech_output_unknown_history_write_is_never_replayed) {
  Fixture f;
  Q4T_CHECK(f.output.Begin().ok());
  Q4T_CHECK(f.output.Append(Text("hello")).ok());
  f.unknown_audio_write = true;
  Q4T_CHECK(!f.output.AppendAudio(std::string(1000, '\0')).ok());
  Q4T_CHECK(f.output.Error().commit_unknown);
  Q4T_CHECK(!f.output.AppendAudio(std::string(1000, '\0')).ok());
  auto end = End();
  end.state = RealtimeResponseEnd::State::kFailed;
  Q4T_CHECK(!f.output.Finish(end).ok());
  Q4T_CHECK(!f.output.Terminal() && f.audio_calls == 1);
  Q4T_CHECK(f.Count("response.output_audio.delta") == 0);
  Q4T_CHECK(f.Count("response.done") == 0);
  return true;
}

Q4T_TEST(realtime_speech_output_receipt_binds_extent_and_spoken_input) {
  for (int bad = 0; bad < 4; ++bad) {
    Fixture f;
    Q4T_CHECK(f.output.Begin().ok());
    Q4T_CHECK(f.output.Append(Text("actual speech")).ok());
    Q4T_CHECK(f.output.AppendAudio(std::string(1000, '\0')).ok());
    RealtimeSpeechReceipt receipt{500, 500, 1000};
    if (bad == 0) ++receipt.bytes;
    if (bad == 1) ++receipt.input_samples;
    if (bad == 2) ++receipt.output_samples;
    Q4T_CHECK(!f.output
                   .CompleteAudio(
                       receipt, bad == 3 ? "invented speech" : "actual speech")
                   .ok());
    RealtimeSpeechView view;
    Q4T_CHECK(f.speech.Get(f.tickets.at(f.AudioId()), &view));
    Q4T_CHECK(!view.complete);
  }
  Fixture f;
  Q4T_CHECK(f.output.Begin().ok());
  Q4T_CHECK(f.output.Append(Text("sealed")).ok());
  Q4T_CHECK(f.Audio(std::string(1000, '\0'), "sealed"));
  Q4T_CHECK(!f.output.Append(Text(" unspoken")).ok());
  return true;
}
