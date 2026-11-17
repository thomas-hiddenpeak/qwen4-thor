// Request-owned audio sources and complete, source-bound transcript projection.
#pragma once

#include <optional>
#include <vector>

#include "q4t/server/file_input.h"

namespace q4t::server {
struct ChatAudioLimits {
  size_t max_parts = 8;
  uint64_t max_source_bytes = 25ULL << 20;
  size_t max_transcript_bytes = 1ULL << 20;
};
struct ChatAudioInput {
  size_t message_index = 0, part_index = 0;
  std::string param, format, part_sha256;
  FileInputContent source;
};

bool HasChatAudioInputs(const io::Json& request);
// Shape-only planning before acquisition. The source borrows the input JSON.
ObjectReply ParseChatAudioSource(const io::Json& part, std::string_view role,
                                 const std::string& path,
                                 FileInputSource* source);

class ChatAudioInputs {
 public:
  // Strict shape/base64/container validation only. No codec/model/network IO.
  // A failed preparation preserves the previous owner and caller's request.
  ObjectReply Prepare(const io::Json& request, const BinaryAccess& access,
                      ChatAudioLimits limits = {});
  const std::vector<ChatAudioInput>& Inputs() const { return inputs_; }
  bool Active() const { return !inputs_.empty(); }
  // Empty actual transcripts (silence) are valid; missing results are not.
  ObjectReply Complete(size_t index, std::string transcript);
  // Preserve all other fields/parts. Require every original audio part and
  // every actual result; reject a changed source even at the same coordinate.
  // Output is atomic on error or cancellation and never contains audio bytes.
  ObjectReply Render(const io::Json& request, const BinaryAccess& access,
                     io::Json* output) const;

 private:
  ChatAudioLimits limits_;
  std::vector<ChatAudioInput> inputs_;
  std::vector<std::optional<std::string>> transcripts_;
  size_t transcript_bytes_ = 0;
  bool prepared_ = false;
};
struct ChatPlanConfig;
struct ChatAudioBudget {
  uint64_t prompt_capacity = 0;
  uint64_t output = 0, auxiliary_output = 0, preparation_output = 0;
};
// Reservation upper bound only; never actual usage or proof of context fit.
ObjectReply PlanChatAudioBudget(const io::Json& request,
                                const ChatPlanConfig& config,
                                const ChatAudioInputs& inputs,
                                ChatAudioBudget* output);
io::Json ChatAudioPreflight(const ChatAudioInputs& inputs,
                            const ChatAudioBudget& budget);
}  // namespace q4t::server
