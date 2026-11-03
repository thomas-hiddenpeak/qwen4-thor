// Real tokenizer/grammar/parser prerequisites; no model weights or GPU calls.
#include <algorithm>
#include <chrono>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <iterator>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>

#include "q4t/server/chat_plan.h"
#include "q4t/server/chat_prompt.h"
#include "q4t/server/chat_response_parser.h"
#include "q4t/server/http_response.h"
#include "q4t/server/output_schema.h"
#include "q4t/server/structured_decoder.h"
#include "q4t/text/tokenizer.h"

namespace {
using q4t::io::Json;
using q4t::server::ChatResponseParser;
using q4t::server::OutputConstraints;
using q4t::server::ResponseDelta;
using q4t::server::StructuredDecoder;
namespace fs = std::filesystem;

void Require(bool condition, const std::string& message) {
  if (!condition) throw std::runtime_error(message);
}

void Check(const q4t::Status& status) {
  Require(status.ok(), status.message());
}

std::string Quote(const std::string& text) {
  return '"' + q4t::server::detail::JsonEscape(text) + '"';
}

std::string Read(const fs::path& path) {
  Require(fs::file_size(path) <= 64'000'000, "input file too large");
  std::ifstream input(path, std::ios::binary);
  Require(input.is_open(), "cannot open " + path.string());
  const std::string result((std::istreambuf_iterator<char>(input)), {});
  Require(!input.bad(), "cannot read " + path.string());
  return result;
}

void Save(const fs::path& path, const std::string& text) {
  Require(!fs::exists(path), "preserve existing output " + path.string());
  std::ofstream output(path, std::ios::binary);
  output << text;
  output.close();
  Require(!output.fail(), "cannot save " + path.string());
}

Json Parse(const std::string& text) {
  Json value;
  Check(q4t::io::ParseJson(text, &value, true));
  return value;
}

template <class T>
std::string Ids(const std::vector<T>& ids) {
  std::string text = "[";
  for (const auto id : ids) {
    if (text.size() > 1) text += ',';
    text += std::to_string(id);
  }
  return text + ']';
}

struct Planned {
  Json request;
  OutputConstraints constraints;
  std::string prompt;
};

Planned Plan(const fs::path& input, const std::string& name,
             const q4t::text::Tokenizer& tokenizer, const std::string& expected,
             const fs::path& output) {
  Planned result;
  result.request = Parse(Read(input));
  q4t::server::ChatPlanConfig config;
  config.model = "qwen3.8-flash-next";
  config.structured = true;
  q4t::server::ChatRequestPlan plan;
  q4t::server::ChatPlanError error;
  Check(q4t::server::PlanChatRequest(result.request, config, &plan, &error));
  result.constraints = plan.constraints;
  std::vector<q4t::server::VisionItem> media;
  Check(q4t::server::PrepareChatPrompt(result.request, result.constraints,
                                       &result.prompt, &media, &error.param));
  Require(media.empty() && result.constraints.json_output,
          "expected text JSON");
  std::vector<uint32_t> ids;
  Check(tokenizer.Encode(result.prompt, &ids));
  Save(output / (name + "-prompt.txt"), result.prompt);
  Save(output / (name + "-prompt-ids.json"), Ids(ids) + '\n');
  Save(output / (name + "-structural-tag.json"),
       result.constraints.structural_tag + '\n');
  std::cout << "{\"phase\":\"plan\",\"name\":" << Quote(name)
            << ",\"prompt_bytes\":" << result.prompt.size()
            << ",\"prompt_tokens\":" << ids.size() << ",\"contains_expected\":"
            << (result.prompt.find(expected) != std::string::npos ? "true"
                                                                  : "false")
            << ",\"max_tokens\":" << result.request.GetInt("max_tokens") << "}"
            << std::endl;
  return result;
}

void Replay(const std::string& name, const OutputConstraints& plan,
            const std::vector<std::string>& vocabulary, int model_vocab,
            const std::vector<int32_t>& stops, const std::vector<uint32_t>& ids,
            bool complete) {
  using Clock = std::chrono::steady_clock;
  const auto started = Clock::now();
  Require(!ids.empty() && ids.size() <= 1200, "CPU replay step bound");
  q4t::server::GrammarControl control;
  control.cancelled = [&] {
    return Clock::now() - started > std::chrono::seconds(60);
  };
  q4t::server::GrammarWorkerReport report;
  int workers = 0, reaped = 0;
  control.worker_started = [&] { ++workers; };
  control.worker_finished = [&](const auto& value) {
    report = value;
    if (value.reaped) ++reaped;
  };
  std::unique_ptr<StructuredDecoder> decoder;
  Check(StructuredDecoder::Create(plan, vocabulary, model_vocab, stops,
                                  &decoder, control));
  // Synthetic finite logits check admissibility only, never model quality.
  std::vector<uint16_t> logits(model_vocab, 0);
  int32_t selected = -1;
  for (size_t i = 0; i < ids.size(); ++i) {
    const auto status = decoder->Select(logits, &selected, ids[i]);
    Require(status.ok() && selected == static_cast<int32_t>(ids[i]),
            name + " rejected step " + std::to_string(i) + " id " +
                std::to_string(ids[i]) + ": " + status.message());
    Require(!decoder->Terminated(), "text unexpectedly terminated grammar");
  }
  const auto stop_status = decoder->Select(logits, &selected, stops.front());
  Require(stop_status.ok() == complete && decoder->Terminated() == complete,
          "EOS acceptance did not match complete document");
  decoder.reset();
  Require(workers == 1 && reaped == 1, "grammar worker was not reaped");
  std::cout << "{\"phase\":\"replay\",\"name\":" << Quote(name)
            << ",\"accepted_text_tokens\":" << ids.size()
            << ",\"eos_accepted\":" << (complete ? "true" : "false")
            << ",\"workers_started\":" << workers
            << ",\"workers_reaped\":" << reaped
            << ",\"reported_post_exec_rss_bytes\":" << report.reported_rss_bytes
            << ",\"lifetime_max_rss_bytes\":" << report.max_rss_bytes
            << ",\"wait_status\":" << report.exit_status << ",\"seconds\":"
            << std::chrono::duration<double>(Clock::now() - started).count()
            << "}" << std::endl;
}

void Parser(const Planned& planned, const std::string& expected,
            const std::vector<std::string>& vocabulary,
            const std::vector<uint32_t>& ids, const fs::path& output) {
  ChatResponseParser parser;
  const bool stream = planned.request.GetBool("stream");
  const std::string name = stream ? "sse" : "json";
  Check(parser.Init(planned.request, planned.prompt, "cpu-payload", !stream));
  std::vector<ResponseDelta> deltas;
  for (const auto id : ids) {
    Check(parser.Append(vocabulary[id], &deltas));
    Require(deltas.empty(), "strict content escaped before validation");
  }
  Check(parser.Finish(false, &deltas));
  Require(deltas.size() == 1 &&
              deltas.front().kind == ResponseDelta::Kind::kContent &&
              deltas.front().text == expected,
          "canonical content changed or did not coalesce");
  const auto message = parser.MessageJson();
  Require(Parse(message).GetString("content") == expected,
          "message JSON changed content");
  const auto frame = q4t::server::detail::SseDeltaChunk(
      "cpu-payload", "qwen3.8-flash-next", deltas.front().ToJson(), "", 0, 0,
      true);
  Save(output / (name + "-message.json"), message);
  Save(output / (name + "-content-frame.txt"), frame);
  ChatResponseParser incomplete;
  Check(
      incomplete.Init(planned.request, planned.prompt, "cpu-prefix", !stream));
  deltas.clear();
  Check(incomplete.Append(expected.substr(0, 1100), &deltas));
  Require(deltas.empty() && !incomplete.Finish(true, &deltas).ok() &&
              deltas.empty(),
          "incomplete strict prefix released content");
  std::cout << "{\"phase\":\"parser\",\"name\":" << Quote(name)
            << ",\"content_bytes\":" << expected.size()
            << ",\"message_bytes\":" << message.size()
            << ",\"content_frame_bytes\":" << frame.size()
            << ",\"content_deltas\":1,\"incomplete_prefix_released\":false}"
            << std::endl;
}

void Run(const fs::path& model, const fs::path& output) {
  auto config = Parse(Read(model / "config.json"));
  if (const auto* text = config.Find("text_config")) {
    auto copy = *text;
    config = std::move(copy);
  }
  const auto size = config.GetInt("vocab_size");
  Require(size > 0 && size <= 300000, "invalid model vocabulary");
  const auto generation = Parse(Read(model / "generation_config.json"));
  const auto* end = generation.Find("eos_token_id");
  Require(end && end->IsArray() && !end->array.empty(),
          "expected pinned EOS list");
  std::vector<int32_t> stops;
  for (const auto& token : end->array) {
    const auto id = token.AsInt(-1);
    Require(id >= 0 && id < size, "invalid EOS id");
    if (std::find(stops.begin(), stops.end(), id) == stops.end())
      stops.push_back(static_cast<int32_t>(id));
  }
  std::unique_ptr<q4t::text::Tokenizer> tokenizer;
  Check(q4t::text::Tokenizer::Load((model / "tokenizer.json").string(), {},
                                   &tokenizer));
  std::vector<std::string> vocabulary(tokenizer->VocabSize());
  for (uint32_t i = 0; i < vocabulary.size(); ++i)
    Check(tokenizer->Decode({i}, true, &vocabulary[i]));
  const auto expected = Read(output / "expected-content.json");
  Require(expected.size() == 12443, "frozen content length changed");
  std::vector<uint32_t> canonical;
  Check(tokenizer->Encode(expected, &canonical));
  std::string decoded;
  Check(tokenizer->Decode(canonical, true, &decoded));
  Require(decoded == expected, "canonical encoding changed bytes");
  Save(output / "canonical-ids.json", Ids(canonical) + '\n');
  // A valid noncanonical prefix is evidence against a token-count upper bound,
  // not evidence that the model followed this path in the old failed request.
  std::vector<uint32_t> bytewise;
  for (const char c : expected.substr(0, 1100)) {
    const auto found =
        std::find(vocabulary.begin(), vocabulary.end(), std::string(1, c));
    Require(found != vocabulary.end(), "missing single-byte vocabulary piece");
    bytewise.push_back(static_cast<uint32_t>(found - vocabulary.begin()));
  }
  Save(output / "bytewise-prefix-ids.json", Ids(bytewise) + '\n');
  std::cout << "{\"phase\":\"tokenizer\",\"model_vocab\":" << size
            << ",\"decoded_vocab\":" << vocabulary.size()
            << ",\"stop_ids\":" << Ids(stops)
            << ",\"canonical_text_tokens\":" << canonical.size()
            << ",\"content_bytes\":" << expected.size() << "}" << std::endl;
  const auto original = Plan(output / "original-sse-request.json", "original",
                             *tokenizer, expected, output);
  const auto candidate = Plan(output / "sse-request.json", "copy-sse",
                              *tokenizer, expected, output);
  const auto json = Plan(output / "json-request.json", "copy-json", *tokenizer,
                         expected, output);
  Require(original.constraints.structural_tag ==
                  candidate.constraints.structural_tag &&
              candidate.constraints.structural_tag ==
                  json.constraints.structural_tag,
          "conditioning edit changed the grammar");
  Replay("canonical", candidate.constraints, vocabulary, size, stops, canonical,
         true);
  Replay("bytewise-prefix", candidate.constraints, vocabulary, size, stops,
         bytewise, false);
  Parser(candidate, expected, vocabulary, canonical, output);
  Parser(json, expected, vocabulary, canonical, output);
}
}  // namespace

int main(int argc, char** argv) {
  const int worker = q4t::server::MaybeRunGrammarWorker(argc, argv);
  if (worker >= 0) return worker;
  try {
    Require(argc == 3, "usage: payload_probe MODEL_DIR OUTPUT_DIR");
    Run(argv[1], argv[2]);
    return 0;
  } catch (const std::exception& error) {
    std::cerr << "payload probe failed: " << error.what() << '\n';
    return 1;
  }
}
