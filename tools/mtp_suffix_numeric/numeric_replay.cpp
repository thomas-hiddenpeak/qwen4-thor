// CPU-only development regression over the already inspected 8K trace.
// Run only after the stage's first HTTP quality gate. This is not a new
// performance sample or a counterfactual neural-draft acceptance oracle.
#include <algorithm>
#include <array>
#include <fstream>
#include <iostream>
#include <iterator>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>

#include "q4t/io/json.h"
#include "q4t/mtp/suffix_drafter.h"
#include "q4t/text/tokenizer.h"

namespace {
void Require(bool condition, const char* message) {
  if (!condition) throw std::runtime_error(message);
}

void Check(const q4t::Status& status) {
  if (!status.ok()) throw std::runtime_error(status.message());
}

q4t::io::Json ReadJson(const char* path) {
  std::ifstream input(path);
  Require(input.good(), "missing frozen input");
  const std::string text{std::istreambuf_iterator<char>(input), {}};
  q4t::io::Json result;
  Check(q4t::io::ParseJson(text, &result));
  return result;
}

std::vector<int32_t> Ids(const q4t::io::Json& parent, const char* key) {
  const auto* values = parent.GetArray(key);
  Require(values != nullptr, "missing token array");
  std::vector<int32_t> result;
  for (const auto& value : values->array) {
    const auto id = value.AsInt(-1);
    Require(id >= 0 && id < 248320, "invalid frozen global ID");
    result.push_back(static_cast<int32_t>(id));
  }
  return result;
}
}  // namespace

int main(int argc, char** argv) {
  try {
    if (argc != 4) return 2;
    const auto index_trace = ReadJson(argv[2]);
    const auto selection_trace = ReadJson(argv[3]);
    const auto* requests = index_trace.GetArray("requests");
    const auto* results = selection_trace.GetArray("results");
    Require(requests && requests->array.size() == 12, "12 source requests");
    Require(results && results->array.size() == 6, "six source ON requests");
    const auto prompt = Ids(requests->array.front(), "prompt_ids");
    const auto output = Ids(requests->array.front(), "output_ids");
    Require(prompt.size() == 8159 && output.size() == 256,
            "frozen prompt/output identity");
    for (const auto& request : requests->array) {
      Require(Ids(request, "prompt_ids") == prompt, "same source prompt");
      Require(Ids(request, "output_ids") == output, "same source output");
    }

    std::unique_ptr<q4t::text::Tokenizer> tokenizer;
    Check(q4t::text::Tokenizer::Load(argv[1], {}, &tokenizer));
    std::vector<uint8_t> classes(248320);
    std::vector<uint32_t> one(1);
    std::string decoded;
    size_t unknown = 0;
    for (size_t id = 0; id < classes.size(); ++id) {
      one[0] = static_cast<uint32_t>(id);
      if (!tokenizer->Decode(one, false, &decoded).ok() || decoded.empty()) {
        ++unknown;
      } else {
        classes[id] = q4t::mtp::ClassifySuffixToken(decoded);
      }
    }
    Require(unknown == 243, "frozen padding classification");
    constexpr std::array<int, 8> kRejectedPositions{
        16, 157, 171, 181, 199, 205, 215, 226};
    size_t total_rejected = 0;
    for (const auto& request : results->array) {
      const auto* hits = request.GetArray("hits");
      Require(hits && hits->array.size() == 15, "15 inspected source hits");
      size_t rejected = 0;
      for (const auto& hit : hits->array) {
        const int position = static_cast<int>(hit.GetInt("position", -1));
        Require(position >= 0 && position < 256, "source output position");
        std::vector<int32_t> history = prompt;
        history.insert(history.end(), output.begin(), output.begin() + position);
        q4t::mtp::SuffixDrafter drafter;
        Check(drafter.Init(history, classes.size(), 208896, classes));
        const auto proposal = drafter.ProposeAfter(
            output[position], static_cast<int32_t>(hit.GetInt("d0", -1)));
        Require(proposal.match_length == hit.GetInt("match_length"),
                "unchanged selected match length");
        Require(proposal.match_end == hit.GetInt("match_end"),
                "unchanged selected source");
        Require(std::vector<int32_t>(proposal.tokens.begin(),
                                      proposal.tokens.end()) ==
                    Ids(hit, "proposal_ids"), "unchanged global proposal");
        const bool should_reject =
            std::find(kRejectedPositions.begin(), kRejectedPositions.end(),
                      position) != kRejectedPositions.end();
        Require(proposal.ready == !should_reject, "numeric admission result");
        Require(proposal.reason ==
                    (should_reject ? q4t::mtp::SuffixReason::kNumericRejected
                                   : q4t::mtp::SuffixReason::kReady),
                "explicit admission reason");
        Require(drafter.Counters().numeric_checks == 1,
                "one admission check per source hit");
        rejected += should_reject;
      }
      Require(rejected == 8, "eight known numeric-format hits blocked");
      total_rejected += rejected;
    }
    std::cout << "{\"source_requests\":6,\"source_hits_per_request\":15,"
                 "\"numeric_rejected_per_request\":8,"
                 "\"retained_per_request\":7,\"unknown_ids\":"
              << unknown << ",\"total_rejected\":" << total_rejected
              << ",\"new_gpu_requests\":0,\"new_http_requests\":0,"
                 "\"scope\":\"inspected development prefixes only; "
                 "no new-path or timing prediction\"}\n";
    return 0;
  } catch (const std::exception& error) {
    std::cerr << error.what() << '\n';
    return 1;
  }
}
