// Qwen's generated text -> OpenAI message/delta fields. No model/GPU access.
#pragma once

#include <string>
#include <string_view>
#include <unordered_map>
#include <vector>

#include "q4t/io/json.h"
#include "q4t/server/output_constraints.h"
#include "q4t/status.h"

namespace q4t::server {

struct ResponseToolCall {
  size_t index = 0;
  std::string id;
  std::string name;
  std::string arguments;  // A JSON object, transported as a JSON string.
};

struct ResponseDelta {
  enum class Kind { kContent, kReasoning, kTool };
  Kind kind = Kind::kContent;
  std::string text;
  ResponseToolCall call;
  std::string ToJson() const;
};

class ChatResponseParser {
 public:
  static constexpr size_t kMaxToolBytes = 1024 * 1024;
  static constexpr size_t kMaxCalls = 128;

  // request must outlive this parser; collect=false bounds streaming memory
  // to one tool block plus delimiter lookahead. Input must be valid UTF-8.
  Status Init(const io::Json& request, std::string_view prompt,
              const std::string& request_id, bool collect);
  Status Append(std::string_view text, std::vector<ResponseDelta>* deltas);
  Status Finish(bool length_limited, std::vector<ResponseDelta>* deltas);
  std::string MessageJson() const;
  bool HasToolCalls() const { return tool_count_ != 0; }

 private:
  enum class Region { kContent, kReasoning, kTool };
  Status Fail(const std::string& message);
  Status ParseTool(std::string_view block, ResponseToolCall* call) const;
  void Emit(Region region, std::string_view text,
            std::vector<ResponseDelta>* deltas);

  bool parsed_ = false;
  bool collect_ = false;
  bool initialized_ = false;
  bool finished_ = false;
  Region region_ = Region::kContent;
  Status status_;
  std::string id_prefix_;
  std::string pending_;
  size_t tool_scan_ = 0;
  size_t tool_count_ = 0;
  std::unordered_map<std::string, const io::Json*> tools_;
  std::string content_;
  std::string reasoning_;
  std::vector<ResponseToolCall> calls_;
  const io::Json* request_ = nullptr;
  OutputConstraints constraints_;
  size_t constrained_bytes_ = 0;
  std::vector<ResponseDelta> deferred_;
};

}  // namespace q4t::server
