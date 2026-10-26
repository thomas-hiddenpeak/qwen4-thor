// CPU-only regression on inspected numeric-stage prefixes. Run after the new
// stage's first HTTP quality gate. This is neither a new generation trajectory
// nor a counterfactual neural-draft acceptance or performance oracle.
#include <algorithm>
#include <cstdint>
#include <fstream>
#include <iostream>
#include <iterator>
#include <memory>
#include <stdexcept>
#include <string>
#include <unordered_map>
#include <vector>

#include "q4t/io/json.h"
#include "q4t/mtp/suffix_drafter.h"
#include "q4t/text/tokenizer.h"
#include "q4t/server/suffix_token_cache.h"

namespace {
constexpr int kModelVocab = 248320;

void Require(bool condition, const char* message) {
  if (!condition) throw std::runtime_error(message);
}

void Check(const q4t::Status& status) {
  if (!status.ok()) throw std::runtime_error(status.message());
}

q4t::io::Json ReadJson(const char* path) {
  std::ifstream input(path);
  Require(input.good(), "missing frozen replay input");
  const std::string text{std::istreambuf_iterator<char>(input), {}};
  q4t::io::Json result;
  Check(q4t::io::ParseJson(text, &result));
  return result;
}

std::vector<int32_t> Ids(const q4t::io::Json& parent, const char* key) {
  const auto* values = parent.GetArray(key);
  Require(values != nullptr, "missing global token array");
  std::vector<int32_t> result;
  for (const auto& value : values->array) {
    const auto id = value.AsInt(-1);
    Require(id >= 0 && id < kModelVocab, "invalid frozen global token ID");
    result.push_back(static_cast<int32_t>(id));
  }
  return result;
}

struct Sequence {
  std::vector<int32_t> prompt;
  std::vector<int32_t> output;
};
}  // namespace

int main(int argc, char** argv) {
  try {
    if (argc != 4) return 2;
    Require(q4t::mtp::SuffixDrafter::kMinAdmittedMatch == 32,
            "one frozen admission length");
    const auto index_trace = ReadJson(argv[2]);
    const auto selected_trace = ReadJson(argv[3]);
    const auto* sequences_json = index_trace.GetArray("requests");
    const auto* results = selected_trace.GetArray("results");
    Require(sequences_json && sequences_json->array.size() == 8,
            "eight frozen prompt/output sequences");
    Require(results && results->array.size() == 116,
            "116 source requests, including suffix OFF");
    std::unordered_map<std::string, Sequence> sequences;
    for (const auto& sequence : sequences_json->array) {
      Sequence ids{Ids(sequence, "prompt_ids"), Ids(sequence, "output_ids")};
      Require(ids.output.size() == 256, "256 recorded output IDs");
      Require(
          sequences.emplace(sequence.GetString("tag"), std::move(ids)).second,
          "unique sequence identity");
    }

    std::unique_ptr<q4t::text::Tokenizer> tokenizer;
    Check(q4t::text::Tokenizer::Load(argv[1], {}, &tokenizer));
    std::vector<uint8_t> classes(kModelVocab);
    std::vector<uint32_t> one(1);
    std::string decoded;
    size_t unknown_classes = 0;
    for (int id = 0; id < kModelVocab; ++id) {
      one[0] = static_cast<uint32_t>(id);
      if (!tokenizer->Decode(one, false, &decoded).ok() || decoded.empty()) {
        ++unknown_classes;
      } else {
        classes[id] = q4t::mtp::ClassifySuffixToken(decoded);
      }
    }
    Require(unknown_classes == 243, "frozen padded ID classification");
    q4t::server::SuffixTokenCache cache;
    Check(cache.Configure(tokenizer.get(), kModelVocab));
    Require(cache.MemoryBytes() == 0, "configure does not allocate");
    uint8_t flag = 0;
    Require(!cache.Classify(-1, &flag).ok() &&
                !cache.Classify(kModelVocab, &flag).ok(),
            "invalid global IDs");
    Require(cache.MemoryBytes() == 0, "invalid IDs do not allocate");
    for (int pass = 0; pass < 2; ++pass) {
      for (int id = 0; id < kModelVocab; ++id) {
        Check(cache.Classify(id, &flag));
        Require(flag == classes[id], "exhaustive eager/cached flags");
      }
      Require(cache.Counters().decodes == kModelVocab,
              "including zero flags, second pass never decodes again");
      Require(cache.Counters().unknown == 243, "cached padding/unknown domain");
    }
    Require(
        cache.MemoryBytes() == kModelVocab && cache.Counters().allocations == 1,
        "one bounded lazy allocation");
    cache.Reset();
    Require(cache.MemoryBytes() == 0, "cache releases payload");
    Require(!cache.Configure(tokenizer.get(), 262145).ok(), "reject over cap");
    Check(cache.Configure(tokenizer.get(), 262144));
    for (int id = 0; id < 262144; ++id) {
      Check(cache.Classify(id, &flag));
      Require(flag == (id < kModelVocab ? classes[id] : 0),
              "full cache capacity retains exact global flags");
    }
    Require(
        cache.MemoryBytes() == 262144 && !cache.Classify(262144, &flag).ok(),
        "full cache never grows");
    Check(cache.Configure(tokenizer.get(), kModelVocab));
    Require(cache.Counters().decodes == 0 && cache.MemoryBytes() == 0,
            "fresh model/config starts empty");

    size_t source_on_requests = 0;
    size_t known_hits = 0;
    size_t retained = 0;
    size_t rejected = 0;
    size_t probe_rejected = 0;
    size_t unknown_d0_steps = 0;
    for (const auto& request : results->array) {
      const std::string group = request.GetString("group");
      if (!group.ends_with("-on")) continue;
      ++source_on_requests;
      const auto found = sequences.find(request.GetString("sequence_key"));
      Require(found != sequences.end(), "bound source sequence");
      const auto& sequence = found->second;
      Require(request.GetInt("prompt_tokens") ==
                  static_cast<int64_t>(sequence.prompt.size()),
              "recorded prompt count");
      const auto* closure = request.Find("closure");
      Require(closure && closure->IsObject(), "explicit replay uncertainty");
      const auto* unknown = closure->GetArray("matched_a0_unknown_positions");
      Require(unknown, "explicit unknown native d0 positions");
      unknown_d0_steps += unknown->array.size();
      // No ProposeAfter call is made for these positions. Their d0 is not
      // reconstructed from output or assigned using aggregate counters.
      const auto* hits = request.GetArray("hits");
      Require(hits, "known source hits");
      const std::string fixture = request.GetString("case");
      const bool probe = fixture == "context-1024" ||
                         fixture == "context-8192" ||
                         fixture == "context-45056" || fixture == "old-8k";
      if (probe) Require(unknown->array.empty(), "probe has known gates");
      size_t request_retained = 0;
      for (const auto& hit : hits->array) {
        const int64_t position = hit.GetInt("position", -1);
        Require(position >= 0 && position < 253,
                "source hit has a complete output continuation");
        const auto pos = static_cast<size_t>(position);
        const int accepted = static_cast<int>(hit.GetInt("accepted", -1));
        Require(
            pos + 3 < sequence.output.size() && accepted >= 1 && accepted <= 3,
            "recorded known-d0 source hit");
        const int32_t d0 = static_cast<int32_t>(hit.GetInt("d0", -1));
        Require(d0 == sequence.output[pos + 1], "observable source d0");
        std::vector<int32_t> history = sequence.prompt;
        history.insert(history.end(), sequence.output.begin(),
                       sequence.output.begin() + pos);
        const auto expected_tokens = Ids(hit, "proposal_ids");
        Require(expected_tokens.size() == 3, "complete fixed-k3 source hit");
        int prefix = 0;
        while (prefix < 3 &&
               expected_tokens[prefix] == sequence.output[pos + 1 + prefix]) {
          ++prefix;
        }
        Require(prefix == accepted, "recorded source acceptance prefix");

        q4t::mtp::SuffixDrafter drafter;
        Check(drafter.Init(
            history, kModelVocab, 208896, {},
            {&cache,
             [](void* context, std::span<const int32_t> ids, bool* numeric) {
               return static_cast<q4t::server::SuffixTokenCache*>(context)
                   ->Check(ids, numeric);
             }}));
        const auto proposal = drafter.ProposeAfter(sequence.output[pos], d0);
        q4t::mtp::SuffixDrafter eager;
        Check(eager.Init(history, kModelVocab, 208896, classes));
        const auto previous = eager.ProposeAfter(sequence.output[pos], d0);
        Require(proposal.ready == previous.ready &&
                    proposal.match_length == previous.match_length &&
                    proposal.match_end == previous.match_end &&
                    proposal.tokens == previous.tokens,
                "recorded trajectory lazy/eager selected proposal equality");
        Require(proposal.match_length == hit.GetInt("match_length") &&
                    proposal.match_end == hit.GetInt("match_end"),
                "selected longest/latest source unchanged");
        Require(std::vector<int32_t>(proposal.tokens.begin(),
                                     proposal.tokens.end()) == expected_tokens,
                "complete global-ID source continuation unchanged");
        Require(drafter.History().size() == history.size() &&
                    std::equal(history.begin(), history.end(),
                               drafter.History().begin()),
                "query leaves committed history unchanged");
        const size_t start = proposal.match_end + 1 - proposal.match_length;
        for (int offset = 0; offset < proposal.match_length; ++offset) {
          const size_t query_pos =
              history.size() + 1 - proposal.match_length + offset;
          const int32_t token = query_pos == history.size()
                                    ? sequence.output[pos]
                                    : history[query_pos];
          Require(history[start + offset] == token,
                  "independent exact source/query token match");
        }
        const bool keep = proposal.match_length >= 32;
        Require(proposal.ready == keep, "fixed length admission decision");
        Require(
            proposal.reason == (keep ? q4t::mtp::SuffixReason::kReady
                                     : q4t::mtp::SuffixReason::kShortRejected),
            "explicit fixed length admission reason");
        Require(drafter.Counters().numeric_checks == static_cast<uint64_t>(keep) &&
                    drafter.Counters().numeric_rejected == 0 &&
                    drafter.Counters().short_rejected == !keep &&
                    drafter.Counters().hits == keep,
                "lazy numeric work follows fixed length admission");
        ++known_hits;
        retained += keep;
        request_retained += keep;
        rejected += !keep;
        if (probe) {
          Require(!keep, "all four probe source hits rejected");
          ++probe_rejected;
        }
      }
      if (!probe) {
        const bool old_text = fixture == "old-repeat-text";
        Require(hits->array.size() == (old_text ? 61 : 62),
                "frozen repetition source hit count");
        Require(request_retained == (old_text ? 54 : 55),
                "long repetition source matches retained");
      }
    }
    Require(source_on_requests == 58 && known_hits == 1396 && retained == 876 &&
                rejected == 520 && probe_rejected == 408 &&
                unknown_d0_steps == 14,
            "complete bounded source replay totals");
    std::cout << "{\"exhaustive_model_ids\":248320,\"cached_passes\":2,"
                 "\"full_capacity_ids\":262144,\"source_requests\":116,"
                 "\"source_on_requests\":58,"
                 "\"unique_sequences\":8,\"known_source_hits\":1396,"
                 "\"retained_known_hits\":876,\"short_rejected_hits\":520,"
                 "\"probe_rejected_hits\":408,\"unknown_native_d0_steps\":14,"
                 "\"unknown_ids\":243,\"new_gpu_requests\":0,"
                 "\"new_http_requests\":0,"
                 "\"scope\":\"inspected old prefixes only; unknown native d0 "
                 "not replayed; no new trajectory or throughput "
                 "prediction\"}\n";
    return 0;
  } catch (const std::exception& error) {
    std::cerr << error.what() << '\n';
    return 1;
  }
}
