// Standalone link-wrapped capture. Production objects and call sites unchanged.
#include "capture_context.h"
#include "q4t/io/json.h"
#include "q4t/io/model_config.h"
#include "q4t/model/model.h"
#include "q4t/model/moe_replay_gpu.h"
#include "q4t/model/moe_replay_io.h"
#include "q4t/quant/moe_gemm.h"
#include "q4t/text/tokenizer.h"
#include "q4t/trace/sha256.h"

#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <map>
#include <memory>
#include <sstream>
#include <stdexcept>

namespace q4t::diagnostic::real_capture {
using namespace q4t;
using namespace q4t::model;
namespace fs = std::filesystem;
constexpr int kLayer = 2;
constexpr size_t kGemmBytes = 32 * 1024 * 1024;
using Identity = std::map<std::string, std::string>;

void Check(bool ok, const std::string& message) {
  if (!ok) throw std::runtime_error(message);
}
void Check(const Status& status) { Check(status.ok(), status.message()); }
void Cuda(cudaError_t error) {
  Check(error == cudaSuccess, cudaGetErrorString(error));
}
std::string Text(const fs::path& path) {
  std::ifstream input(path);
  Check(bool(input), "open " + path.string());
  return {std::istreambuf_iterator<char>(input), {}};
}
std::string FileHash(const fs::path& path) {
  trace::Digest digest;
  Check(trace::HashFile(path.string(), &digest));
  return trace::HexDigest(digest);
}
trace::Digest Digest(const std::string& text) {
  Check(text.size() == 64 && text != std::string(64, '0'), "invalid digest");
  trace::Digest result;
  const std::string digits = "0123456789abcdef";
  for (size_t i = 0; i < 32; ++i) {
    const auto a = digits.find(text[2 * i]), b = digits.find(text[2 * i + 1]);
    Check(a != std::string::npos && b != std::string::npos, "invalid hex");
    result[i] = static_cast<uint8_t>(16 * a + b);
  }
  return result;
}
template <typename T>
trace::Digest Hash(const std::vector<T>& data) {
  return trace::Sha256(
      {reinterpret_cast<const uint8_t*>(data.data()), data.size() * sizeof(T)});
}
template <typename T>
void Save(const fs::path& path, const std::vector<T>& data) {
  std::ofstream output(path, std::ios::binary);
  output.write(reinterpret_cast<const char*>(data.data()),
               data.size() * sizeof(T));
  output.close();
  Check(bool(output), "write " + path.string());
}
template <typename T>
void Read(std::vector<T>& host, const void* device, size_t count,
          cudaStream_t stream) {
  host.resize(count);
  const auto copied = cudaMemcpyAsync(host.data(), device, count * sizeof(T),
                                      cudaMemcpyDeviceToHost, stream);
  // Drain even if submission failed, before any host storage can be released.
  const auto completed = cudaStreamSynchronize(stream);
  Cuda(copied);
  Cuda(completed);
}
template <typename T>
bool Bits(const std::vector<T>& a, const std::vector<T>& b) {
  return a.size() == b.size() &&
         std::memcmp(a.data(), b.data(), a.size() * sizeof(T)) == 0;
}

struct Boundary {
  int position = 0, rows = 0, hs = 0, experts = 0, top_k = 0, vocab = 0;
  std::vector<int32_t> tokens;
  std::vector<uint16_t> hidden, output;
  MoEReplayObservation route;
};
diagnostic::CaptureContext context;
Boundary captured;
bool capture_enabled = true;

void WriteBoundary(const fs::path& path, const Boundary& data,
                   const Identity& identity) {
  Check(!fs::exists(path), "record already exists");
  fs::create_directories(path);
  Save(path / "hidden", data.hidden);
  Save(path / "logits", data.route.logits);
  Save(path / "ids", data.route.ids);
  Save(path / "weights", data.route.weights);
  Save(path / "output", data.output);
  std::ofstream out(path / "record.json");
  out << "{\"version\":1,\"role\":\"main\",\"layer\":" << kLayer
      << ",\"position\":" << data.position << ",\"rows\":" << data.rows
      << ",\"experts\":" << data.experts << ",\"top_k\":" << data.top_k
      << ",\"hidden_size\":" << data.hs << ",\"vocab\":" << data.vocab
      << ",\"tokens\":[";
  for (size_t i = 0; i < data.tokens.size(); ++i) {
    if (i) out << ',';
    out << data.tokens[i];
  }
  out << "],\"identity\":{";
  bool first = true;
  for (const auto& [key, value] : identity) {
    if (!first) out << ',';
    first = false;
    out << '"' << key << "\":\"" << value << '"';
  }
  out << "},\"files\":{";
  first = true;
  for (const auto* name : {"hidden", "logits", "ids", "weights", "output"}) {
    if (!first) out << ',';
    first = false;
    out << '"' << name << "\":{\"path\":\"" << name << "\",\"sha256\":\""
        << FileHash(path / name) << "\"}";
  }
  out << "}}\n";
  out.close();
  Check(bool(out), "record manifest write");
}

// Exact public MoE ABI, resolved by the independent executable's --wrap flag.
#define MOE_ABI                                                      \
  "_ZN3q4t5model10MoEForwardEPKtRKNS_5quant15MoEWeightLayoutERKNS0_" \
  "15MoEExtraWeightsEPtiiPvmSB_mP11CUstream_stPNS_"                  \
  "5trace15RouterCollectorEiPKNS3_12MoEResidencyEPNS0_"              \
  "21MoEForwardDiagnosticsE"
Status RealMoE(const uint16_t*, const quant::MoEWeightLayout&,
               const MoEExtraWeights&, uint16_t*, int, int, void*, size_t,
               void*, size_t, cudaStream_t, trace::RouterCollector*, int,
               const quant::MoEResidency*,
               MoEForwardDiagnostics*) asm("__real_" MOE_ABI);
Status WrappedMoE(const uint16_t*, const quant::MoEWeightLayout&,
                  const MoEExtraWeights&, uint16_t*, int, int, void*, size_t,
                  void*, size_t, cudaStream_t, trace::RouterCollector*, int,
                  const quant::MoEResidency*,
                  MoEForwardDiagnostics*) asm("__wrap_" MOE_ABI);

Status WrappedMoE(const uint16_t* x, const quant::MoEWeightLayout& routed,
                  const MoEExtraWeights& extra, uint16_t* y, int rows, int k,
                  void* workspace, size_t workspace_bytes, void* gemm,
                  size_t gemm_bytes, cudaStream_t stream,
                  trace::RouterCollector* trace, int layer,
                  const quant::MoEResidency* residency,
                  MoEForwardDiagnostics* diagnostics) {
  try {
    if (capture_enabled) Check(context.ObserveLayer(layer, rows));
    const bool selected = capture_enabled && layer == context.target_layer();
    if (selected) {
      Check(!residency && !routed.slot_mode, "capture requires full residency");
      captured = {};
      captured.position = context.position();
      captured.rows = rows;
      captured.tokens = context.tokens();
      captured.hs = extra.hs;
      captured.experts = extra.E;
      captured.top_k = k;
      Read(captured.hidden, x, static_cast<size_t>(rows) * extra.hs, stream);
    }
    Status status =
        RealMoE(x, routed, extra, y, rows, k, workspace, workspace_bytes, gemm,
                gemm_bytes, stream, trace, layer, residency, diagnostics);
    if (!status.ok() || !selected) return status;
    const size_t routed_bytes =
        quant::MoEWorkspace::RequiredBytes(rows, k, extra.hs, routed.moe_is);
    const auto* scratch = static_cast<const uint8_t*>(workspace) +
                          ((routed_bytes + 7) & ~size_t(7));
    Read(captured.route.logits, scratch, static_cast<size_t>(rows) * extra.E,
         stream);
    scratch += static_cast<size_t>(rows) * extra.E * 2;
    Read(captured.route.ids, scratch, static_cast<size_t>(rows) * k, stream);
    scratch += static_cast<size_t>(rows) * k * 4;
    Read(captured.route.weights, scratch, static_cast<size_t>(rows) * k,
         stream);
    Read(captured.output, y, static_cast<size_t>(rows) * extra.hs, stream);
    return context.Captured(layer);
  } catch (const std::exception& error) {
    cudaStreamSynchronize(stream);
    return Status::Fail(error.what());
  }
}

struct Resources {
  Model model;
  cudaStream_t stream = nullptr;
  std::vector<void*> buffers;
  Resources() {
    Cuda(cudaStreamCreateWithFlags(&stream, cudaStreamNonBlocking));
  }
  ~Resources() {
    cudaStreamSynchronize(stream);
    for (void* buffer : buffers) cudaFree(buffer);
    model.Free();
    cudaStreamDestroy(stream);
  }
  void* Alloc(size_t size) {
    void* pointer = nullptr;
    Cuda(cudaMalloc(&pointer, size));
    buffers.push_back(pointer);
    return pointer;
  }
};

void Equal(const Boundary& a, const Boundary& b, const std::string& label) {
  Check(Bits(a.hidden, b.hidden) && Bits(a.route.logits, b.route.logits) &&
            Bits(a.route.ids, b.route.ids) &&
            Bits(a.route.weights, b.route.weights) && Bits(a.output, b.output),
        label + " differs");
  std::cout << label << " BITEXACT_PASS\n" << std::flush;
}

int Run(int argc, char** argv) {
  Check(argc == 7,
        "usage: capture MODEL TEXT OUTPUT MODEL_SHA WEIGHTS_SHA SOURCE_SHA");
  const fs::path model_dir = fs::canonical(argv[1]);
  const fs::path text_path = fs::canonical(argv[2]);
  const fs::path output = fs::weakly_canonical(argv[3]);
  const auto relative = output.lexically_relative(fs::current_path());
  Check(!relative.empty() &&
            (*relative.begin() == ".q4t-work" || *relative.begin() == "build"),
        "output location");
  Check(!fs::exists(output), "output already exists");
  Identity identity{{"model", argv[4]},
                    {"weights", argv[5]},
                    {"source", argv[6]},
                    {"binary", FileHash("/proc/self/exe")},
                    {"tokenizer", FileHash(model_dir / "tokenizer.json")}};
  for (const auto& item : identity) Digest(item.second);
  for (const auto* name :
       {"Q4T_FP8_PROJ", "Q4T_FP8_SHARED", "Q4T_FP8_ATTN", "Q4T_FP8_GDN",
        "Q4T_FP8_LMHEAD", "Q4T_FP8_HC", "Q4T_HC_UP", "Q4T_MOE_DUMP"}) {
    Check(std::getenv(name) == nullptr, std::string("unset ") + name);
  }
  std::unique_ptr<text::Tokenizer> tokenizer;
  Check(text::Tokenizer::Load((model_dir / "tokenizer.json").string(), {},
                              &tokenizer));
  const std::string text = Text(text_path);
  std::vector<uint32_t> unsigned_tokens;
  Check(tokenizer->Encode(text, &unsigned_tokens));
  Check(unsigned_tokens.size() > 4 && unsigned_tokens.size() <= 260,
        "fixture requires 1..256 prompt tokens followed by four forced tokens");
  std::vector<int32_t> tokens(unsigned_tokens.begin(), unsigned_tokens.end());
  const int prompt = static_cast<int>(tokens.size()) - 4;
  fs::create_directories(output);
  fs::copy_file(text_path, output / "text.txt");
  Save(output / "tokens.i32", tokens);
  ModelConfig config;
  config.model_dir = model_dir.string();
  config.index_path = (model_dir / "model.safetensors.index.json").string();
  config.ple_sidecar =
      (model_dir / "ple/qwen3.8-flash-next-ple-fp8.bin").string();
  config.max_prefill = 256;
  config.max_len = 8192;
  config.max_seq = 1;
  io::ModelConfig parsed;
  Check(io::ParseModelConfig((model_dir / "config.json").string(), &parsed));
  Check(parsed.num_hidden_layers == config.num_layers &&
            parsed.hidden_size == config.hs && parsed.hc_count == config.hc &&
            parsed.hc_lowrank == config.lowrank &&
            parsed.num_experts == config.E &&
            parsed.num_experts_per_tok == config.topk &&
            parsed.moe_intermediate_size == config.moe_is &&
            parsed.shared_expert_intermediate_size == config.shared_is &&
            parsed.vocab_size == config.vocab,
        "unsupported model configuration");
  Check(tokenizer->VocabSize() <= static_cast<uint32_t>(config.vocab),
        "tokenizer/model vocabulary mismatch");
  for (auto token : tokens)
    Check(token >= 0 && token < config.vocab, "token range");
  Resources resources;
  auto& model = resources.model;
  auto stream = resources.stream;
  Check(LoadModel(config, &model, stream));
  auto* logits = static_cast<uint16_t*>(resources.Alloc(4 * config.vocab * 2));
  std::vector<Boundary> singles;
  Boundary first_prefill, batch;
  ModelSequence sequence;
  const auto begin = [&](int position, int rows) {
    Check(context.Begin({tokens.data(), static_cast<size_t>(position + rows)},
                        position, rows, config.num_layers, kLayer,
                        config.vocab));
  };
  const auto finish = [&](const Status& status, const std::string& name) {
    const auto complete = cudaStreamSynchronize(stream);
    const auto closed = context.Finish(status.ok() && complete == cudaSuccess);
    Check(status);
    Cuda(complete);
    Check(closed);
    captured.vocab = config.vocab;
    WriteBoundary(output / name, captured, identity);
    std::cout << "captured " << name << " position=" << captured.position
              << " rows=" << captured.rows << '\n'
              << std::flush;
    return captured;
  };
  for (int mode = 0; mode < 2; ++mode) {
    Check(ModelBeginSequence(model, &sequence, stream));
    begin(0, prompt);
    auto prefill =
        finish(ModelPrefill(model, &sequence, tokens.data(), prompt, logits,
                            stream, nullptr, nullptr, -1, LogitsRows::kLastRow),
               mode == 0 ? "prefill-t1" : "prefill-t4");
    if (mode == 0) {
      first_prefill = prefill;
      for (int row = 0; row < 4; ++row) {
        Check(sequence.position == prompt + row &&
                  sequence.history ==
                      std::vector<int32_t>(tokens.begin(),
                                           tokens.begin() + prompt + row),
              "actual sequence prefix mismatch");
        begin(prompt + row, 1);
        singles.push_back(
            finish(ModelDecodeStepSeq(model, &sequence, tokens[prompt + row],
                                      logits, stream),
                   "t1-row" + std::to_string(row)));
      }
    } else {
      Equal(first_prefill, prefill, "independent-prefill");
      Check(
          sequence.position == prompt &&
              sequence.history ==
                  std::vector<int32_t>(tokens.begin(), tokens.begin() + prompt),
          "batch prefix mismatch");
      begin(prompt, 4);
      batch = finish(
          ModelDecodeBatch(model, tokens.data() + prompt, 4, prompt,
                           sequence.history.data(), prompt, logits, stream),
          "t4");
    }
    Check(ModelEndSequence(&sequence));
  }
  capture_enabled = false;
  const auto& layer = model.layers[kLayer];
  const size_t ws_bytes = MoEForwardWorkspaceBytes(
      4, config.topk, config.hs, config.moe_is, config.shared_is, config.E);
  void* workspace = resources.Alloc(ws_bytes);
  void* gemm = resources.Alloc(kGemmBytes);
  auto* x = static_cast<uint16_t*>(resources.Alloc(4 * config.hs * 2));
  auto* y = static_cast<uint16_t*>(resources.Alloc(4 * config.hs * 2));
  const auto replay = [&](const Boundary& input,
                          const std::vector<int32_t>& ids,
                          const std::string& name) {
    MoEReplayIdentity id;
    id.model = Digest(identity.at("model"));
    id.weights = Digest(identity.at("weights"));
    id.binary = Digest(identity.at("binary"));
    id.hidden = Hash(input.hidden);
    id.layer = kLayer;
    id.position = input.position;
    id.rows = input.rows;
    id.experts = input.experts;
    id.top_k = input.top_k;
    id.tokens = input.tokens;
    MoEReplayRecord record;
    const bool normal = ids.empty();
    if (!normal) Check(record.Prepare(id, id, ids));
    Cuda(cudaMemcpyAsync(x, input.hidden.data(), input.hidden.size() * 2,
                         cudaMemcpyHostToDevice, stream));
    Boundary result = input;
    Check(MoEReplayForward(x, layer.routed, layer.mlp, y, workspace, ws_bytes,
                           gemm, kGemmBytes, stream, id,
                           normal ? nullptr : &record, &result.route));
    Read(result.output, y, input.hidden.size(), stream);
    WriteBoundary(output / name, result, identity);
    if (normal) Check(record.Prepare(id, id, result.route.ids));
    Check(WriteMoEReplay((output / name / "route.bin").string(), record));
    return result;
  };
  Equal(batch, replay(batch, batch.route.ids, "t4-replay"), "t4-same-shape");
  for (int row = 0; row < 4; ++row) {
    const auto tag = "t1-row" + std::to_string(row);
    Equal(singles[row],
          replay(singles[row], singles[row].route.ids, tag + "-replay"),
          tag + "-same-shape");
    // Preserve original capture identity. The intervention is a new derived
    // record with explicit row/source linkage, never relabel an old record.
    const std::vector<int32_t> ids(
        batch.route.ids.begin() + row * config.topk,
        batch.route.ids.begin() + (row + 1) * config.topk);
    replay(singles[row], ids, tag + "-batch-ids");
    Boundary sliced = batch;
    sliced.position = batch.position + row;
    sliced.rows = 1;
    sliced.tokens.resize(sliced.position + 1);
    sliced.hidden.assign(batch.hidden.begin() + row * config.hs,
                         batch.hidden.begin() + (row + 1) * config.hs);
    replay(sliced, {}, tag + "-same-input-normal");
    replay(sliced, ids, tag + "-same-input-batch-ids");
    for (const auto& derived :
         {tag + "-batch-ids", tag + "-same-input-batch-ids"}) {
      const bool same_input = derived == tag + "-same-input-batch-ids";
      std::ofstream link(output / derived / "intervention.json");
      link << "{\"kind\":\"fixed_ids_from_explicit_row\",\"source_record_"
              "sha256\":\""
           << FileHash(output / "t4/record.json") << "\",\"source_row\":" << row
           << ",\"target_record_sha256\":\""
           << FileHash(output / derived / "record.json")
           << "\",\"target_original_record_sha256\":\""
           << FileHash(output / (same_input ? "t4" : tag) / "record.json")
           << "\",\"target_input_row\":" << (same_input ? row : 0) << "}\n";
      link.close();
      Check(bool(link), "intervention provenance write");
    }
  }
  std::ofstream done(output / "completion.json");
  done << "{\"status\":\"PASS\",\"prompt_tokens\":" << prompt
       << ",\"forced_tokens\":4,\"same_shape_cases\":5,\"model_invocations\":7}"
          "\n";
  done.close();
  Check(bool(done), "completion write");
  std::cout << "real_text_capture PASS\n";
  return 0;
}
}  // namespace q4t::diagnostic::real_capture

int main(int argc, char** argv) {
  try {
    return q4t::diagnostic::real_capture::Run(argc, argv);
  } catch (const std::exception& error) {
    std::cerr << "FAIL " << error.what() << '\n';
    return 1;
  }
}
