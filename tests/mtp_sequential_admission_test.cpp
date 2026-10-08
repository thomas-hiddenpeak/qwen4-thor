// Fixed direct admission: ordinary scheduler B1 is an independent trajectory
// oracle. No T4 replay, seed search, performance inference or automatic retry.
#include "q4t/io/json.h"
#include "q4t/model/model_head.h"
#include "q4t/model/model_owner.h"
#include "q4t/mtp/mtp.h"
#include "q4t/test.h"
#include "q4t/text/tokenizer.h"
#include "support/mtp_sequential_state.h"

#include <array>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <memory>
#include <numeric>
#include <span>

namespace {
using q4t::model::Model;
using q4t::model::ModelSequence;
using q4t::mtp::MtpModel;
using q4t::mtp::MtpSequentialResult;
using namespace q4t::test::sequential;
constexpr int kRows = 4, kNaturalSteps = 4;
constexpr size_t kArtifactLimit = 96u * 1024u * 1024u;

template <typename T>
class Device {
 public:
  explicit Device(size_t count) {
    Cuda(cudaMalloc(reinterpret_cast<void**>(&pointer_), count * sizeof(T)),
         "allocate bounded test buffer");
  }
  ~Device() {
    if (cudaFree(pointer_) != cudaSuccess) std::abort();
  }
  Device(const Device&) = delete;
  Device& operator=(const Device&) = delete;
  T* get() const { return pointer_; }

 private:
  T* pointer_ = nullptr;
};
struct DraftOwner {
  MtpModel model;
  ~DraftOwner() { model.Free(); }
};
class Evidence {
 public:
  explicit Evidence(const char* directory) : directory_(directory) {
    Require(std::filesystem::create_directory(directory_),
            "sequential evidence directory must be new");
  }
  template <typename T>
  void Save(const std::string& name, const std::vector<T>& data) {
    const size_t bytes = data.size() * sizeof(T);
    Require(bytes <= kArtifactLimit - bytes_, "96 MiB artifact cap exceeded");
    const auto path = directory_ / name;
    Require(!std::filesystem::exists(path), "refusing to replace evidence");
    std::ofstream output(path, std::ios::binary);
    output.exceptions(std::ios::failbit | std::ios::badbit);
    output.write(reinterpret_cast<const char*>(data.data()),
                 static_cast<std::streamsize>(bytes));
    output.close();
    bytes_ += bytes;
  }
  size_t bytes() const { return bytes_; }

 private:
  std::filesystem::path directory_;
  size_t bytes_ = 0;
};
struct Counts {
  int target = 0;
  int draft = 0;
  int target_limit = 96;
  int draft_limit = 40;
  void Target(int rows) {
    Require(rows >= 0 && target + rows <= target_limit,
            std::to_string(target_limit) + " target call cap exceeded");
    target += rows;
  }
  void Draft(int calls) {
    Require(calls >= 0 && draft + calls <= draft_limit,
            std::to_string(draft_limit) + " draft call cap exceeded");
    draft += calls;
  }
};
struct Row {
  int32_t input = -1;
  int position = -1;
  int32_t prediction = -1;
  std::vector<int32_t> history;
  std::vector<uint16_t> logits, trunk;
};
struct Buffers {
  explicit Buffers(const Model& main)
      : logits(main.cfg.vocab),
        token(1),
        ids(kRows),
        slots(kRows),
        hidden(kRows * main.hc_dim()),
        sample(kRows * main.cfg.hs),
        multi(kRows * main.hc_dim()),
        extend_logits(kRows * main.cfg.vocab),
        seed(main.hc_dim()) {}
  Device<uint16_t> logits;
  Device<int32_t> token, ids;
  Device<int> slots;
  Device<uint16_t> hidden, sample, multi, extend_logits, seed;
};

void SaveRow(Evidence& evidence, const std::string& label, const Row& row) {
  evidence.Save(label + ".logits.bf16", row.logits);
  evidence.Save(label + ".trunk.bf16", row.trunk);
  evidence.Save(label + ".input.i32",
                std::vector<int32_t>{row.input, row.position, row.prediction});
  evidence.Save(label + ".history.i32", row.history);
}
void SaveResult(Evidence& evidence, const std::string& label,
                const MtpSequentialResult& result) {
  std::vector<int32_t> fields{result.accepted_count,
                              result.next_b,
                              result.next_d0,
                              static_cast<int32_t>(result.terminal),
                              static_cast<int32_t>(result.next_seed_valid),
                              result.draft_forward_calls,
                              result.target_forward_calls,
                              result.extend_forward_calls};
  fields.insert(fields.end(), result.accepted_tokens.begin(),
                result.accepted_tokens.end());
  evidence.Save(label + ".result.i32", fields);
}
bool Stop(int32_t token, std::span<const int32_t> stops) {
  return std::find(stops.begin(), stops.end(), token) != stops.end();
}
void Publish(ModelSequence* seq, std::span<const int32_t> tokens) {
  Require(!seq->HasPending(), "publish with pending work");
  seq->history.insert(seq->history.end(), tokens.begin(), tokens.end());
  seq->position += static_cast<int>(tokens.size());
}

// Deliberately independent from the production sequential verifier: absolute
// history indexing, ordinary Submit/Complete ownership and no trunk capture.
Row Plain(const Model& main, ModelSequence* seq, int32_t token,
          Buffers& buffers, Counts& counts) {
  Require(
      main.layers.size() == 48 && seq->stage == ModelSequence::Stage::kDecode,
      "ordinary oracle requires 48-layer committed decode");
  Row row;
  row.input = token;
  row.position = seq->position;
  const int width = main.ple_hash.ngram_size - 1;
  row.history.resize(width);
  for (int j = 0; j < width; ++j) {
    const int source = seq->position - width + j;
    row.history[j] = source < 0 ? static_cast<int32_t>(main.cfg.eos_token_id)
                                : seq->history.at(source);
  }
  const int slot = seq->seq_id;
  Check(
      seq->Submit({&token, 1}, ModelSequence::Stage::kDecode, main.cfg.max_len),
      "ordinary submit");
  counts.Target(1);
  auto status = q4t::model::ModelDecodeBatchMulti(
      main, &token, &row.position, &slot, row.history.data(), 1,
      buffers.logits.get(), nullptr, nullptr);
  if (status.ok())
    status = q4t::model::ArgmaxBf16Rows(buffers.logits.get(), 1, main.cfg.vocab,
                                        buffers.token.get(), nullptr);
  ModelSequence* sequence = seq;
  status = q4t::model::CompleteSequenceWork({&sequence, 1}, status, [] {
    const auto error = cudaStreamSynchronize(nullptr);
    return error == cudaSuccess ? q4t::Status()
                                : q4t::Status::Fail(cudaGetErrorString(error));
  });
  Check(status, "ordinary completion");
  row.logits = Read(buffers.logits.get(), main.cfg.vocab);
  // RunLayers alternates ping/pong 48 times. HeadForward takes const trunk
  // and writes separate workspace, so this is the uncaptured ordinary trunk.
  row.trunk = Read(main.d_trunk, main.hc_dim());
  row.prediction = Read(buffers.token.get(), 1).front();
  Require(row.prediction == Argmax(row.logits), "ordinary GPU/host argmax");
  Finite(row.trunk, "ordinary final trunk");
  return row;
}
std::vector<Row> ReadTargets(const Model& main, const MtpModel& draft,
                             const ModelSequence& before,
                             const MtpSequentialResult& result) {
  Require(result.accepted_count >= 1 && result.accepted_count <= 4 &&
              result.target_forward_calls == result.accepted_count,
          "strict target row/count contract");
  std::vector<Row> rows;
  // Logits/trunks are retained production observations. Input/position/history
  // below describe the returned consumed prefix, not a CUDA argument probe;
  // the real-TU dependency-stub target checks actual call arguments separately.
  ModelSequence cursor = before;
  for (int i = 0; i < result.accepted_count; ++i) {
    Row row;
    row.input = result.accepted_tokens[i];
    row.position = cursor.position;
    row.logits =
        Read(draft.d_ms_vlogits + static_cast<size_t>(i) * main.cfg.vocab,
             main.cfg.vocab);
    row.trunk = Read(draft.d_ms_vtrunk + static_cast<size_t>(i) * main.hc_dim(),
                     main.hc_dim());
    row.prediction = Argmax(row.logits);
    Finite(row.trunk, "actual target trunk");
    const int width = main.ple_hash.ngram_size - 1;
    for (int j = 0; j < width; ++j) {
      const int source = cursor.position - width + j;
      row.history.push_back(source < 0 ? main.cfg.eos_token_id
                                       : cursor.history.at(source));
    }
    Publish(&cursor, {&row.input, 1});
    rows.push_back(std::move(row));
  }
  return rows;
}
void CompareRows(const Row& actual, const Row& expected,
                 const std::string& label) {
  Require(actual.input == expected.input &&
              actual.position == expected.position &&
              actual.prediction == expected.prediction,
          label + ": consumed input/position/argmax");
  Exact(actual.history, expected.history, label + ".history");
  Exact(actual.logits, expected.logits, label + ".logits");
  Exact(actual.trunk, expected.trunk, label + ".trunk");
}

std::string ReadText(const std::string& path) {
  std::ifstream input(path, std::ios::binary);
  Require(input.good(), "cannot read frozen fixture " + path);
  std::string text((std::istreambuf_iterator<char>(input)),
                   std::istreambuf_iterator<char>());
  Require(!input.bad(), "fixture read error " + path);
  return text;
}
std::vector<int32_t> Stops(const Model& main) {
  const std::string path = main.cfg.model_dir + "/generation_config.json";
  if (!std::filesystem::exists(path))
    return {static_cast<int32_t>(main.cfg.eos_token_id)};
  q4t::io::Json config;
  Check(q4t::io::ParseJson(ReadText(path), &config),
        "generation stop metadata");
  Require(config.IsObject(), "generation metadata is not an object");
  const auto* ids = config.Find("eos_token_id");
  if (!ids || ids->IsNull())
    return {static_cast<int32_t>(main.cfg.eos_token_id)};
  std::vector<int32_t> result;
  const auto append = [&](const q4t::io::Json& value) {
    const int64_t id = value.AsInt(-1);
    Require(value.IsNumber() && id >= 0 && id < main.cfg.vocab,
            "invalid generation stop ID");
    if (std::find(result.begin(), result.end(), id) == result.end())
      result.push_back(static_cast<int32_t>(id));
  };
  if (ids->IsArray()) {
    for (const auto& value : ids->array) append(value);
  } else {
    append(*ids);
  }
  Require(!result.empty(), "empty generation stop IDs");
  return result;
}
std::vector<int32_t> Prompt(q4t::text::Tokenizer& tokenizer, const char* path,
                            int base_length, Evidence& evidence,
                            const std::string& label) {
  const std::string text = ReadText(path);
  std::vector<uint32_t> encoded;
  Check(tokenizer.Encode(text, &encoded), "tokenize fixed raw prompt");
  Require(encoded.size() == static_cast<size_t>(base_length),
          "frozen prompt token length changed");
  std::vector<int32_t> result(encoded.begin(), encoded.end());
  if (base_length == 8192) result.insert(result.end(), {97, 131, 211, 313});
  evidence.Save(label + ".prompt.txt",
                std::vector<char>(text.begin(), text.end()));
  evidence.Save(label + ".prompt.i32", result);
  return result;
}
void Prefill(const Model& main, const std::vector<int32_t>& prompt,
             ModelSequence* seq, uint16_t* logits, uint16_t* trunk) {
  Check(q4t::model::ModelBeginSequence(main, seq, nullptr, 0), "fresh prefill");
  if (prompt.size() <= static_cast<size_t>(main.cfg.max_prefill)) {
    Check(q4t::model::ModelPrefill(
              main, seq, prompt.data(), static_cast<int>(prompt.size()), logits,
              nullptr, trunk, nullptr, 0, q4t::model::LogitsRows::kLastRow),
          "short ordinary/capture prefill");
  } else {
    int chunks = 0;
    while (seq->position < static_cast<int>(prompt.size())) {
      const int base = seq->position;
      const int count = std::min(main.cfg.max_prefill,
                                 static_cast<int>(prompt.size()) - base);
      const bool last = base + count == static_cast<int>(prompt.size());
      Check(q4t::model::ModelPrefillTextChunk(
                main, seq, prompt.data(), static_cast<int>(prompt.size()),
                count, last ? logits : nullptr, nullptr,
                trunk ? trunk + static_cast<size_t>(base) * main.hc_dim()
                      : nullptr,
                q4t::model::LogitsRows::kLastRow),
            "long prefill chunk");
      ++chunks;
    }
    Require(chunks == 2, "long prefill must be 8192+4");
  }
  Require(seq->history == prompt && !seq->HasPending(),
          "prefill committed state");
  Cuda(cudaStreamSynchronize(nullptr), "prefill complete");
}

// All four controlled branches call the actual production verifier helper.
// They do not claim production proposal or extend coverage.
void Forced(const Model& main, const MtpModel& draft, const State& baseline,
            int32_t bonus, std::span<const int32_t> stops, Buffers& buffers,
            Counts& counts, Evidence& evidence) {
  ModelSequence seq;
  baseline.RestoreMain(main, &seq);
  std::vector<Row> lookahead;
  int32_t pending = bonus;
  for (int i = 0; i < 4; ++i) {
    Require(!Stop(pending, stops), "fixed lookahead terminated; no resampling");
    auto row = Plain(main, &seq, pending, buffers, counts);
    SaveRow(evidence, "short.lookahead" + std::to_string(i), row);
    pending = row.prediction;
    Require(!Stop(pending, stops), "fixed lookahead stop; no seed search");
    lookahead.push_back(std::move(row));
  }
  for (int accepted = 0; accepted <= 3; ++accepted) {
    const std::string label = "short.forced" + std::to_string(accepted);
    baseline.RestoreMain(main, &seq);
    const ModelSequence original = seq;
    std::array<int32_t, 3> proposal{lookahead[0].prediction,
                                    lookahead[1].prediction,
                                    lookahead[2].prediction};
    if (accepted < 3)
      proposal[accepted] = (proposal[accepted] + 1) % main.cfg.vocab;
    evidence.Save(label + ".drafts.i32",
                  std::vector<int32_t>(proposal.begin(), proposal.end()));
    MtpSequentialResult result;
    const auto status = q4t::mtp::MtpSequentialVerify(
        main, draft, seq, bonus, proposal, 256, stops, &result, nullptr);
    counts.Target(result.target_forward_calls);
    SaveResult(evidence, label, result);
    Check(status, label + ".actual");
    SameSequence(seq, original, label + ".caller");
    Require(result.accepted_count == accepted + 1 && !result.terminal &&
                !result.next_seed_valid && result.draft_forward_calls == 0 &&
                result.extend_forward_calls == 0,
            label + ": forced branch was not covered");
    const auto actual = ReadTargets(main, draft, original, result);
    for (size_t i = 0; i < actual.size(); ++i)
      SaveRow(evidence, label + ".actual" + std::to_string(i), actual[i]);
    ModelSequence committed = original;
    Publish(&committed, {result.accepted_tokens.data(),
                         static_cast<size_t>(result.accepted_count)});
    const auto after = CaptureMain(main, committed);
    baseline.RestoreMain(main, &seq);
    pending = bonus;
    for (int i = 0; i <= accepted; ++i) {
      const auto reference = Plain(main, &seq, pending, buffers, counts);
      SaveRow(evidence, label + ".plain" + std::to_string(i), reference);
      CompareRows(reference, lookahead[i], label + ".fixed_reference");
      CompareRows(actual[i], reference, label + ".target" + std::to_string(i));
      pending = reference.prediction;
    }
    Require(result.next_b == pending, label + ": correction mismatch");
    after.CompareMain(main, seq, label + ".valid_state");
  }
  baseline.RestoreMain(main, &seq);
}

void Future(const Model& main, const State& baseline, int32_t bonus,
            Buffers& buffers, Counts& counts, Evidence& evidence,
            const std::string& label) {
  ModelSequence seq;
  baseline.RestoreMain(main, &seq);
  PoisonFuture(main, seq.position, 0x3f);
  const auto first = Plain(main, &seq, bonus, buffers, counts);
  SaveRow(evidence, label + ".future_a", first);
  const auto after = CaptureMain(main, seq);
  baseline.RestoreMain(main, &seq);
  PoisonFuture(main, seq.position, 0xbf);
  const auto second = Plain(main, &seq, bonus, buffers, counts);
  SaveRow(evidence, label + ".future_b", second);
  CompareRows(second, first, label + ".future_independence");
  after.CompareMain(main, seq, label + ".future_valid_state");
  baseline.RestoreMain(main, &seq);
}

struct ExtendObservation {
  std::vector<int32_t> ids;
  std::vector<uint16_t> logits, multi, sample, seed;
};
ExtendObservation ReadExtend(const MtpModel& draft, int rows,
                             const uint16_t* seed) {
  ExtendObservation result{
      Read(draft.d_ms_ext_ids, rows),
      Read(draft.d_ms_ext_logits, static_cast<size_t>(rows) * draft.cfg.vocab),
      Read(draft.d_ms_ext_multi, static_cast<size_t>(rows) * draft.hc_dim()),
      Read(draft.d_ms_ext_sample, static_cast<size_t>(rows) * draft.cfg.hs),
      Read(seed, draft.hc_dim())};
  Finite(result.logits, "actual extend logits");
  Finite(result.multi, "actual extend multi");
  Finite(result.sample, "actual extend sample");
  Finite(result.seed, "actual next seed");
  return result;
}
void SaveExtend(Evidence& evidence, const std::string& label,
                const ExtendObservation& value) {
  evidence.Save(label + ".ids.i32", value.ids);
  evidence.Save(label + ".logits.bf16", value.logits);
  evidence.Save(label + ".multi.bf16", value.multi);
  evidence.Save(label + ".sample.bf16", value.sample);
  evidence.Save(label + ".seed.bf16", value.seed);
}

void Natural(const Model& main, const MtpModel& draft, ModelSequence* seq,
             int32_t* bonus, int32_t* d0, std::span<const int32_t> stops,
             Buffers& buffers, Counts& counts, Evidence& evidence,
             const std::string& context) {
  std::array<int, 4> extend_shapes{};
  for (int step = 0; step < kNaturalSteps; ++step) {
    const std::string label = context + ".natural" + std::to_string(step);
    Require(!Stop(*bonus, stops), label + ": terminated before fixed step");
    const auto before = CaptureMain(main, *seq);
    const auto draft_before = CaptureDraft(draft, seq->position);
    const auto original = *seq;
    const int32_t input_bonus = *bonus, input_d0 = *d0;
    MtpSequentialResult result;
    const auto status = q4t::mtp::MtpSpeculativeStepSequentialTarget(
        main, draft, *seq, input_bonus, input_d0, buffers.seed.get(), 256,
        stops, &result, buffers.seed.get(), nullptr);
    counts.Target(result.target_forward_calls);
    counts.Draft(result.draft_forward_calls + result.extend_forward_calls);
    SaveResult(evidence, label, result);
    std::printf(
        "  sequential_actual=%s status=%s target=%d draft=%d extend=%d\n",
        label.c_str(), status.ok() ? "OK" : status.message().c_str(),
        result.target_forward_calls, result.draft_forward_calls,
        result.extend_forward_calls);
    Check(status, label + ".actual");
    SameSequence(*seq, original, label + ".caller");
    const auto proposals = Read(draft.d_ms_drafts, 3);
    Require(proposals[0] == input_d0 && result.draft_forward_calls == 2,
            label + ": actual proposal seed/count");
    for (int32_t token : proposals)
      Require(token >= 0 && token < main.cfg.vocab, "invalid real draft token");
    evidence.Save(label + ".real_drafts.i32", proposals);
    const auto actual = ReadTargets(main, draft, original, result);
    for (size_t i = 0; i < actual.size(); ++i)
      SaveRow(evidence, label + ".actual" + std::to_string(i), actual[i]);
    ModelSequence committed = original;
    Publish(&committed, {result.accepted_tokens.data(),
                         static_cast<size_t>(result.accepted_count)});
    const auto after = CaptureMain(main, committed);
    State draft_after;
    ExtendObservation actual_extend;
    if (!result.terminal) {
      draft_after = CaptureDraft(draft, committed.position);
      actual_extend =
          ReadExtend(draft, result.accepted_count, buffers.seed.get());
      SaveExtend(evidence, label + ".extend_actual", actual_extend);
    }
    before.RestoreMain(main, seq);
    std::vector<Row> reference;
    int32_t pending = input_bonus;
    bool terminal = false;
    for (int row = 0; row < 4; ++row) {
      auto observed = Plain(main, seq, pending, buffers, counts);
      pending = observed.prediction;
      SaveRow(evidence, label + ".plain" + std::to_string(row), observed);
      reference.push_back(std::move(observed));
      terminal = Stop(pending, stops);
      if (terminal || row == 3 || proposals[row] != pending) break;
    }
    Require(actual.size() == reference.size() && result.next_b == pending &&
                result.terminal == terminal &&
                result.next_seed_valid == !terminal,
            label + ": independent ordinary acceptance/terminal contract");
    for (size_t row = 0; row < reference.size(); ++row)
      CompareRows(actual[row], reference[row],
                  label + ".target" + std::to_string(row));
    after.CompareMain(main, *seq, label + ".valid_state");
    Require(!terminal, label + ": early stop, fixed four steps incomplete");
    Require(result.extend_forward_calls == 1,
            label + ": nonterminal must have one extend");

    // Independently construct EAGLE shift from ordinary predictions/trunks.
    // This invokes MtpForward directly, never the sequential step/extend
    // helper.
    const int rows = static_cast<int>(reference.size());
    std::vector<int32_t> ids;
    std::vector<int> positions, slots(rows, 0);
    std::vector<uint16_t> hidden;
    for (const auto& row : reference) {
      ids.push_back(row.prediction);
      positions.push_back(row.position);
      hidden.insert(hidden.end(), row.trunk.begin(), row.trunk.end());
    }
    Exact(actual_extend.ids, ids, label + ".independent_shift");
    draft_before.RestoreDevice();
    Write(buffers.ids.get(), ids);
    Write(buffers.slots.get(), slots);
    Write(buffers.hidden.get(), hidden);
    counts.Draft(1);
    Check(q4t::mtp::MtpForward(draft, buffers.ids.get(), positions.data(),
                               buffers.hidden.get(), buffers.sample.get(),
                               buffers.multi.get(), buffers.extend_logits.get(),
                               rows, nullptr, buffers.slots.get(), true,
                               q4t::model::LogitsRows::kAllRows, nullptr,
                               positions.back()),
          label + ".independent_extend");
    Cuda(cudaStreamSynchronize(nullptr), "independent extend complete");
    ExtendObservation expected{
        ids,
        Read(buffers.extend_logits.get(),
             static_cast<size_t>(rows) * main.cfg.vocab),
        Read(buffers.multi.get(), static_cast<size_t>(rows) * main.hc_dim()),
        Read(buffers.sample.get(), static_cast<size_t>(rows) * main.cfg.hs),
        Read(
            buffers.multi.get() + static_cast<size_t>(rows - 1) * main.hc_dim(),
            main.hc_dim())};
    SaveExtend(evidence, label + ".extend_plain", expected);
    Exact(actual_extend.logits, expected.logits, label + ".extend_logits");
    Exact(actual_extend.multi, expected.multi, label + ".extend_multi");
    Exact(actual_extend.sample, expected.sample, label + ".extend_sample");
    Exact(actual_extend.seed, expected.seed, label + ".next_hidden");
    const std::vector<uint16_t> last_logits(
        expected.logits.end() - main.cfg.vocab, expected.logits.end());
    Require(result.next_d0 == Argmax(last_logits),
            label + ": independent next draft seed");
    draft_after.CompareDevice(label + ".draft_valid_state");
    after.CompareMain(main, *seq, label + ".extend_main_unchanged");
    ++extend_shapes[rows - 1];
    *bonus = pending;
    *d0 = result.next_d0;
  }
  std::printf(
      "MTP_SEQUENTIAL_NATURAL context=%s steps=4 "
      "extend_rows1=%d rows2=%d rows3=%d rows4=%d\n",
      context.c_str(), extend_shapes[0], extend_shapes[1], extend_shapes[2],
      extend_shapes[3]);
}

void Context(const Model& main, const MtpModel& draft,
             const std::vector<int32_t>& prompt, std::span<const int32_t> stops,
             Buffers& buffers, Counts& counts, Evidence& evidence,
             const std::string& label) {
  ModelSequence sequence;
  int32_t bonus = -1, d0 = -1;
  {
    Prefill(main, prompt, &sequence, buffers.logits.get(), nullptr);
    const auto ordinary_logits = Read(buffers.logits.get(), main.cfg.vocab);
    bonus = Argmax(ordinary_logits);
    evidence.Save(label + ".prefill_plain.bf16", ordinary_logits);
    const auto ordinary = CaptureMain(main, sequence);
    Device<uint16_t> trunk(prompt.size() * main.hc_dim());
    Prefill(main, prompt, &sequence, buffers.logits.get(), trunk.get());
    const auto captured_logits = Read(buffers.logits.get(), main.cfg.vocab);
    evidence.Save(label + ".prefill_capture.bf16", captured_logits);
    Exact(captured_logits, ordinary_logits, label + ".prefill_logits");
    ordinary.CompareMain(main, sequence, label + ".prefill_valid_state");
    Require(!Stop(bonus, stops), label + ": prefill already terminal");
    // Scan the captured trunk in bounded chunks; never persist a 160 MiB raw
    // prompt trunk or keep it alongside multiple complete state snapshots.
    for (size_t base = 0; base < prompt.size(); base += 128) {
      const size_t rows = std::min(size_t{128}, prompt.size() - base);
      Finite(Read(trunk.get() + base * main.hc_dim(), rows * main.hc_dim()),
             label + ".prefill_trunk");
    }
    std::vector<int32_t> shifted(prompt.begin() + 1, prompt.end());
    shifted.push_back(bonus);
    std::vector<int> positions(prompt.size());
    std::iota(positions.begin(), positions.end(), 0);
    Check(q4t::mtp::MtpResetState(draft, nullptr, 0),
          "reset draft for context");
    counts.Draft(prompt.size() > 8192 ? 2 : 1);
    Check(q4t::mtp::MtpDraftExtend(
              draft, shifted.data(), trunk.get(), positions.data(),
              static_cast<int>(prompt.size()), &d0, buffers.seed.get(), nullptr,
              0, q4t::model::LogitsRows::kLastRow, nullptr,
              prompt.size() > 8192 ? q4t::mtp::MtpInitPolicy::kSkipUnusedTail
                                   : q4t::mtp::MtpInitPolicy::kFull),
          "actual server-policy draft initialization");
    Cuda(cudaStreamSynchronize(nullptr), "initialization complete");
    Require(d0 >= 0 && d0 < main.cfg.vocab, "invalid initialized draft seed");
    const auto initialized_seed = Read(buffers.seed.get(), main.hc_dim());
    Finite(initialized_seed, "initialized hidden");
    evidence.Save(label + ".init_g.bf16", initialized_seed);
    evidence.Save(label + ".init_result.i32",
                  std::vector<int32_t>{sequence.position, bonus, d0});
    ordinary.CompareMain(main, sequence, label + ".init_main_unchanged");
  }
  {
    const auto baseline = CaptureMain(main, sequence);
    if (prompt.size() == 1024)
      Forced(main, draft, baseline, bonus, stops, buffers, counts, evidence);
    Future(main, baseline, bonus, buffers, counts, evidence, label);
    baseline.RestoreMain(main, &sequence);
  }
  Natural(main, draft, &sequence, &bonus, &d0, stops, buffers, counts, evidence,
          label);
}
}  // namespace

Q4T_TEST(mtp_sequential_full_model_admission) {
  const char* short_path = std::getenv("Q4T_MTP_SEQUENTIAL_PROMPT");
  const char* long_path = std::getenv("Q4T_MTP_SEQUENTIAL_LONG_PROMPT");
  const char* output = std::getenv("Q4T_MTP_SEQUENTIAL_DIR");
  Require(
      short_path && *short_path && long_path && *long_path && output && *output,
      "set both frozen sequential prompts and a new evidence directory");
  for (const char* name :
       {"Q4T_GDN_REG", "Q4T_GDN_CHUNKED", "Q4T_GDN_SPLIT", "Q4T_FP8_PROJ",
        "Q4T_FP8_HC", "Q4T_FP8_ALL", "Q4T_MOE_STREAMS", "Q4T_MOE_BATCH_GATHER",
        "Q4T_LIN_DUMP", "Q4T_MLP_DUMP", "Q4T_MTP_INIT_TIMING",
        "Q4T_MTP_CYCLE_TIMING", "Q4T_MTP_VERIFY_MOE_TIMING", "Q4T_MTP_DEBUG",
        "Q4T_MTP_TIMING"})
    Require(std::getenv(name) == nullptr, std::string("unset ") + name);
  Evidence evidence(output);
  Cuda(cudaSetDevice(0), "CUDA required, no SKIP");
  q4t::model::ModelConfig cfg;
  cfg.model_dir =
      "/home/rm01/models/dev/llm/garnermccloud/"
      "Qwen3.8-Flash-Next-NVFP4-SSD-Stream";
  cfg.index_path = cfg.model_dir + "/model.safetensors.index.json";
  cfg.ple_sidecar = cfg.model_dir + "/ple/qwen3.8-flash-next-ple-fp8.bin";
  cfg.num_layers = 48;
  cfg.max_len = 208896;
  cfg.max_prefill = cfg.ple_capacity_tokens = 8192;
  cfg.max_seq = 1;
  std::unique_ptr<q4t::text::Tokenizer> tokenizer;
  Check(q4t::text::Tokenizer::Load(cfg.model_dir + "/tokenizer.json",
                                   q4t::text::TokenizerLimits{}, &tokenizer),
        "load tokenizer");
  const auto short_prompt =
      Prompt(*tokenizer, short_path, 1024, evidence, "short");
  const auto long_prompt =
      Prompt(*tokenizer, long_path, 8192, evidence, "long");
  std::printf(
      "MTP_SEQUENTIAL_SCOPE layers=48 S1=1 slot=0 max_len=208896 "
      "max_prefill=8192 prompts=1024,8196 natural_steps=4,4 "
      "forced_verify=0,1,2,3 independent_plain_trunk_capture=0 "
      "complete_valid_state=1 page_map=checked_identity "
      "future_patterns=3f,bf performance_evidence=0 retries=0\n");
  std::fflush(stdout);
  q4t::model::ModelOwner owner;
  Check(owner.Load(cfg, nullptr), "load main once");
  const Model& main = owner.Get();
  DraftOwner draft;
  q4t::mtp::MtpConfig draft_config;
  draft_config.mtp_dir = cfg.model_dir + "/mtp";
  draft_config.max_len = cfg.max_len;
  draft_config.max_prefill = cfg.max_prefill;
  draft_config.max_seq = 1;
  Check(q4t::mtp::LoadMtp(draft_config, main.head.embed_tokens,
                          main.head.lm_head, &draft.model, nullptr),
        "load draft once");
  Check(q4t::mtp::MtpReserveScratch(draft.model, kRows),
        "reserve existing scratch");
  const auto stops = Stops(main);
  evidence.Save("generation_stop_ids.i32", stops);
  Buffers buffers(main);
  Counts counts;
  Context(main, draft.model, short_prompt, stops, buffers, counts, evidence,
          "short");
  Context(main, draft.model, long_prompt, stops, buffers, counts, evidence,
          "long");
  std::printf(
      "MTP_SEQUENTIAL_SUMMARY natural_steps=8 forced_verify_steps=4 "
      "poison_targets=4 prefill_chunks=6 target_calls=%d "
      "draft_calls_including_init=%d artifact_bytes=%zu "
      "all_required=PASS cross_t4_explained=0 http_tested=0\n",
      counts.target, counts.draft, evidence.bytes());
  return true;
}

// The original 8192+4 shape fixture predicted a real stop before decoding.
// Its FAIL and the completed short evidence remain unchanged. This separate
// named entry consumes one prebound, exact 8196-token text prompt, with no
// appended IDs and no retries. It supplements only the missing long domain.
Q4T_TEST(mtp_sequential_long_full_model_admission) {
  const char* prompt_path = std::getenv("Q4T_MTP_SEQUENTIAL_LONG_PROMPT");
  const char* output = std::getenv("Q4T_MTP_SEQUENTIAL_DIR");
  Require(
      prompt_path && *prompt_path && output && *output,
      "set the frozen exact 8196-token prompt and a new evidence directory");
  Require(std::getenv("Q4T_MTP_SEQUENTIAL_PROMPT") == nullptr,
          "long-only entry must not receive a short fixture");
  for (const char* name :
       {"Q4T_GDN_REG", "Q4T_GDN_CHUNKED", "Q4T_GDN_SPLIT", "Q4T_FP8_PROJ",
        "Q4T_FP8_HC", "Q4T_FP8_ALL", "Q4T_MOE_STREAMS", "Q4T_MOE_BATCH_GATHER",
        "Q4T_LIN_DUMP", "Q4T_MLP_DUMP", "Q4T_MTP_INIT_TIMING",
        "Q4T_MTP_CYCLE_TIMING", "Q4T_MTP_VERIFY_MOE_TIMING", "Q4T_MTP_DEBUG",
        "Q4T_MTP_TIMING"})
    Require(std::getenv(name) == nullptr, std::string("unset ") + name);
  Evidence evidence(output);
  Cuda(cudaSetDevice(0), "CUDA required, no SKIP");
  q4t::model::ModelConfig cfg;
  cfg.model_dir =
      "/home/rm01/models/dev/llm/garnermccloud/"
      "Qwen3.8-Flash-Next-NVFP4-SSD-Stream";
  cfg.index_path = cfg.model_dir + "/model.safetensors.index.json";
  cfg.ple_sidecar = cfg.model_dir + "/ple/qwen3.8-flash-next-ple-fp8.bin";
  cfg.num_layers = 48;
  cfg.max_len = 208896;
  cfg.max_prefill = cfg.ple_capacity_tokens = 8192;
  cfg.max_seq = 1;
  std::unique_ptr<q4t::text::Tokenizer> tokenizer;
  Check(q4t::text::Tokenizer::Load(cfg.model_dir + "/tokenizer.json",
                                   q4t::text::TokenizerLimits{}, &tokenizer),
        "load tokenizer");
  const auto prompt = Prompt(*tokenizer, prompt_path, 8196, evidence, "long");
  Require(prompt.size() == 8196, "long-only input must not append token IDs");
  std::printf(
      "MTP_SEQUENTIAL_LONG_SCOPE layers=48 S1=1 slot=0 max_len=208896 "
      "max_prefill=8192 prompt_tokens=8196 appended_tokens=0 "
      "natural_steps=4 forced_verify=0 short_rerun=0 "
      "independent_plain_trunk_capture=0 complete_valid_state=1 "
      "page_map=checked_identity future_patterns=3f,bf "
      "max_target_calls=34 max_draft_calls=18 performance_evidence=0 "
      "retries=0\n");
  std::fflush(stdout);
  q4t::model::ModelOwner owner;
  Check(owner.Load(cfg, nullptr), "load main once");
  const Model& main = owner.Get();
  DraftOwner draft;
  q4t::mtp::MtpConfig draft_config;
  draft_config.mtp_dir = cfg.model_dir + "/mtp";
  draft_config.max_len = cfg.max_len;
  draft_config.max_prefill = cfg.max_prefill;
  draft_config.max_seq = 1;
  Check(q4t::mtp::LoadMtp(draft_config, main.head.embed_tokens,
                          main.head.lm_head, &draft.model, nullptr),
        "load draft once");
  Check(q4t::mtp::MtpReserveScratch(draft.model, kRows),
        "reserve existing scratch");
  const auto stops = Stops(main);
  evidence.Save("generation_stop_ids.i32", stops);
  Buffers buffers(main);
  Counts counts{.target_limit = 34, .draft_limit = 18};
  Context(main, draft.model, prompt, stops, buffers, counts, evidence, "long");
  Require(counts.target <= 34 && counts.draft <= 18,
          "long-only call bounds changed");
  std::printf(
      "MTP_SEQUENTIAL_LONG_SUMMARY natural_steps=4 forced_verify_steps=0 "
      "short_rerun=0 poison_targets=2 prefill_chunks=4 target_calls=%d "
      "draft_calls_including_init=%d artifact_bytes=%zu "
      "all_required=PASS old_fixture_still_failed=1 cross_t4_explained=0 "
      "http_tested=0\n",
      counts.target, counts.draft, evidence.bytes());
  return true;
}
