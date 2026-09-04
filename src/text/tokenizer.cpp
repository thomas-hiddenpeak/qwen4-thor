// GPT-2 style Byte-Level BPE tokenizer implementation.
//
// See include/q4t/text/tokenizer.h for the contract. The algorithm mirrors the
// reference `tokenizers` library: NFC-normalize, split on the Unicode
// pre-tokenize regex, map each piece through the GPT-2 byte alphabet, then run
// ranked BPE merges with a lazy-deletion priority queue.
#include "q4t/text/tokenizer.h"

#include <algorithm>
#include <array>
#include <cctype>
#include <cstdint>
#include <cstring>
#include <fstream>
#include <limits>
#include <map>
#include <queue>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>

#include <unicode/normlzr.h>
#include <unicode/parseerr.h>
#include <unicode/uregex.h>
#include <unicode/unistr.h>
#include <unicode/utypes.h>

#include "q4t/io/json.h"

namespace q4t {
namespace text {

namespace {

// The pinned GPT-2 pre-tokenize regex. Matches the exact pattern in the target
// model's tokenizer.json (pre_tokenizer.pretokenizers[0].pattern.Regex).
constexpr std::string_view kPretokenizeRegex =
    "(?i:'s|'t|'re|'ve|'m|'ll|'d)|[^\\r\\n\\p{L}\\p{N}]?"
    "[\\p{L}\\p{M}]+|\\p{N}| ?[^\\s\\p{L}\\p{M}\\p{N}]+"
    "[\\r\\n]*|\\s*[\\r\\n]+|\\s+(?!\\S)|\\s+";

struct AddedToken {
  std::uint32_t id = 0;
  std::string content;
  bool special = false;
};

struct MergeRule {
  std::uint32_t rank = 0;
  std::uint32_t result_id = 0;
};

struct TokenizerData {
  TokenizerLimits limits;
  // GPT-2 byte alphabet: byte value -> the token string for that byte.
  std::array<std::string, 256> byte_encoder{};
  // Reverse of byte_encoder: code point -> byte value.
  std::unordered_map<std::uint32_t, std::uint8_t> byte_decoder;
  // byte value -> base vocab token id (the single-byte token).
  std::array<std::uint32_t, 256> byte_token_ids{};
  // token string -> base vocab id.
  std::unordered_map<std::string, std::uint32_t> vocabulary;
  // base vocab id -> token string (contiguous, size == base vocab size).
  std::vector<std::string> id_to_token;
  // (left_id << 32 | right_id) -> merge rule.
  std::unordered_map<std::uint64_t, MergeRule> merges;
  std::vector<AddedToken> added_tokens;
  std::unordered_map<std::uint32_t, std::size_t> added_by_id;
  URegularExpression* regex = nullptr;

  ~TokenizerData() {
    if (regex != nullptr) {
      uregex_close(regex);
    }
  }
};

std::uint64_t PairKey(std::uint32_t left, std::uint32_t right) noexcept {
  return (static_cast<std::uint64_t>(left) << 32U) |
         static_cast<std::uint64_t>(right);
}

// Build the GPT-2 byte-level alphabet. Bytes 33-126, 161-172, and 174-255 map
// to themselves; the remaining 100 bytes map to code points 256+.
bool InitializeByteMapping(TokenizerData& data, std::string* err) {
  std::array<bool, 256> direct{};
  for (std::uint32_t v = 33; v <= 126; ++v) direct[v] = true;
  for (std::uint32_t v = 161; v <= 172; ++v) direct[v] = true;
  for (std::uint32_t v = 174; v <= 255; ++v) direct[v] = true;
  std::uint32_t extension = 0;
  for (std::uint32_t v = 0; v <= 255; ++v) {
    const std::uint32_t cp = direct[v] ? v : 256U + extension++;
    // Encode the code point as UTF-8 into byte_encoder[v].
    if (cp < 0x80) {
      data.byte_encoder[v].push_back(static_cast<char>(cp));
    } else if (cp < 0x800) {
      data.byte_encoder[v].push_back(static_cast<char>(0xC0 | (cp >> 6)));
      data.byte_encoder[v].push_back(static_cast<char>(0x80 | (cp & 0x3F)));
    } else {
      data.byte_encoder[v].push_back(static_cast<char>(0xE0 | (cp >> 12)));
      data.byte_encoder[v].push_back(static_cast<char>(
          0x80 | ((cp >> 6) & 0x3F)));
      data.byte_encoder[v].push_back(static_cast<char>(0x80 | (cp & 0x3F)));
    }
    if (!data.byte_decoder.emplace(cp, static_cast<std::uint8_t>(v)).second) {
      *err = "duplicate GPT-2 byte mapping";
      return false;
    }
  }
  return true;
}

// Decode a single UTF-8 code point at `offset`, advancing `offset`.
bool DecodeUtf8At(std::string_view input, std::size_t* offset,
                  std::uint32_t* codepoint) {
  const std::size_t start = *offset;
  if (start >= input.size()) return false;
  const unsigned char* b =
      reinterpret_cast<const unsigned char*>(input.data() + start);
  auto cont = [&](std::size_t i) -> bool {
    return start + i < input.size() &&
           (b[i] & 0xC0) == 0x80;
  };
  std::uint32_t cp = 0;
  std::size_t len = 0;
  if (b[0] < 0x80) {
    cp = b[0];
    len = 1;
  } else if ((b[0] & 0xE0) == 0xC0 && cont(1)) {
    cp = ((b[0] & 0x1F) << 6) | (b[1] & 0x3F);
    len = 2;
  } else if ((b[0] & 0xF0) == 0xE0 && cont(1) && cont(2)) {
    cp = ((b[0] & 0x0F) << 12) | ((b[1] & 0x3F) << 6) | (b[2] & 0x3F);
    len = 3;
  } else if ((b[0] & 0xF8) == 0xF0 && cont(1) && cont(2) && cont(3)) {
    cp = ((b[0] & 0x07) << 18) | ((b[1] & 0x3F) << 12) |
         ((b[2] & 0x3F) << 6) | (b[3] & 0x3F);
    len = 4;
  } else {
    return false;
  }
  // Reject overlong encodings and surrogates.
  if (len == 2 && cp < 0x80) return false;
  if (len == 3 && cp < 0x800) return false;
  if (len == 4 && cp < 0x10000) return false;
  if (cp >= 0xD800 && cp <= 0xDFFF) return false;
  *codepoint = cp;
  *offset = start + len;
  return true;
}

bool ValidateUtf8(std::string_view input) {
  std::size_t offset = 0;
  std::uint32_t cp = 0;
  while (offset < input.size()) {
    if (!DecodeUtf8At(input, &offset, &cp)) return false;
  }
  return true;
}

// Validate the top-level schema is the expected BPE + ByteLevel + NFC shape.
bool ValidateSchema(const io::Json& root, std::string* err) {
  if (root.GetString("version") != "1.0") {
    *err = "tokenizer version is not 1.0";
    return false;
  }
  const io::Json* model = root.Find("model");
  if (model == nullptr || !model->IsObject()) {
    *err = "missing model object";
    return false;
  }
  if (model->GetString("type") != "BPE") {
    *err = "model.type is not BPE";
    return false;
  }
  if (model->GetBool("byte_fallback") || model->GetBool("ignore_merges") ||
      model->GetBool("fuse_unk")) {
    *err = "unsupported BPE flags (byte_fallback/ignore_merges/fuse_unk)";
    return false;
  }
  const io::Json* normalizer = root.Find("normalizer");
  if (normalizer == nullptr || !normalizer->IsObject() ||
      normalizer->GetString("type") != "NFC") {
    *err = "normalizer must be NFC";
    return false;
  }
  const io::Json* pre = root.Find("pre_tokenizer");
  if (pre == nullptr || !pre->IsObject() ||
      pre->GetString("type") != "Sequence") {
    *err = "pre_tokenizer must be a Sequence";
    return false;
  }
  const io::Json* pretoks = pre->GetArray("pretokenizers");
  if (pretoks == nullptr || pretoks->array.size() != 2) {
    *err = "pre_tokenizer must have exactly 2 pretokenizers";
    return false;
  }
  const io::Json& split = pretoks->array[0];
  const io::Json& bytelvl = pretoks->array[1];
  if (!split.IsObject() || split.GetString("type") != "Split" ||
      split.GetString("behavior") != "Isolated" ||
      split.GetBool("invert")) {
    *err = "pre_tokenizer[0] must be Split/Isolated/non-invert";
    return false;
  }
  const io::Json* pattern = split.Find("pattern");
  if (pattern == nullptr || !pattern->IsObject() ||
      pattern->GetString("Regex") != kPretokenizeRegex) {
    *err = "pre_tokenizer[0].pattern.Regex does not match the pinned regex";
    return false;
  }
  if (!bytelvl.IsObject() || bytelvl.GetString("type") != "ByteLevel" ||
      bytelvl.GetBool("add_prefix_space") || bytelvl.GetBool("use_regex")) {
    *err = "pre_tokenizer[1] must be ByteLevel (no prefix space)";
    return false;
  }
  const io::Json* decoder = root.Find("decoder");
  if (decoder == nullptr || !decoder->IsObject() ||
      decoder->GetString("type") != "ByteLevel" ||
      decoder->GetBool("add_prefix_space")) {
    *err = "decoder must be ByteLevel (no prefix space)";
    return false;
  }
  return true;
}

// Parse model.vocab + model.merges into the data structures.
bool ParseModel(const io::Json& root, TokenizerData& data, std::string* err) {
  const io::Json* model = root.Find("model");
  const io::Json* vocab = model->Find("vocab");
  if (vocab == nullptr || !vocab->IsObject()) {
    *err = "model.vocab must be an object";
    return false;
  }
  if (vocab->object.size() > data.limits.max_vocab_size) {
    *err = "vocabulary exceeds max_vocab_size";
    return false;
  }
  if (vocab->object.size() != Tokenizer::kPinnedBaseVocabSize) {
    *err = "base vocabulary size does not match the pinned tokenizer";
    return false;
  }
  data.vocabulary.reserve(vocab->object.size());
  data.id_to_token.resize(vocab->object.size());
  std::vector<bool> seen(vocab->object.size(), false);
  for (const auto& kv : vocab->object) {
    const io::Json& value = kv.second;
    if (!value.IsNumber()) {
      *err = "vocabulary value is not a number";
      return false;
    }
    const std::uint64_t id = static_cast<std::uint64_t>(value.number);
    if (id >= vocab->object.size()) {
      *err = "vocabulary id out of range";
      return false;
    }
    if (seen[id]) {
      *err = "vocabulary ids are not unique";
      return false;
    }
    seen[id] = true;
    data.id_to_token[id] = kv.first;
    data.vocabulary.emplace(kv.first, static_cast<std::uint32_t>(id));
  }
  if (std::find(seen.begin(), seen.end(), false) != seen.end()) {
    *err = "vocabulary ids are not contiguous";
    return false;
  }
  // Resolve the single-byte token ids.
  for (std::size_t byte = 0; byte < data.byte_encoder.size(); ++byte) {
    const auto found = data.vocabulary.find(data.byte_encoder[byte]);
    if (found == data.vocabulary.end()) {
      *err = "vocabulary is missing a GPT-2 byte token";
      return false;
    }
    data.byte_token_ids[byte] = found->second;
  }
  // Parse merges.
  const io::Json* merges = model->Find("merges");
  if (merges == nullptr || !merges->IsArray()) {
    *err = "model.merges must be an array";
    return false;
  }
  if (merges->array.size() > data.limits.max_merges) {
    *err = "merges exceed max_merges";
    return false;
  }
  if (merges->array.size() != Tokenizer::kPinnedMergeCount) {
    *err = "merge count does not match the pinned tokenizer";
    return false;
  }
  data.merges.reserve(merges->array.size());
  for (std::size_t rank = 0; rank < merges->array.size(); ++rank) {
    const io::Json& entry = merges->array[rank];
    if (!entry.IsString()) {
      *err = "merge entry must be a string";
      return false;
    }
    const std::string& text = entry.str;
    const std::size_t sep = text.find(' ');
    if (sep == std::string::npos || sep == 0 || sep + 1 >= text.size() ||
        text.find(' ', sep + 1) != std::string::npos) {
      *err = "merge entry must contain exactly two tokens";
      return false;
    }
    const std::string left = text.substr(0, sep);
    const std::string right = text.substr(sep + 1);
    const auto left_id = data.vocabulary.find(left);
    const auto right_id = data.vocabulary.find(right);
    const auto result_id = data.vocabulary.find(left + right);
    if (left_id == data.vocabulary.end() ||
        right_id == data.vocabulary.end() ||
        result_id == data.vocabulary.end()) {
      *err = "merge references a token absent from the vocabulary";
      return false;
    }
    const auto inserted = data.merges.emplace(
        PairKey(left_id->second, right_id->second),
        MergeRule{static_cast<std::uint32_t>(rank), result_id->second});
    if (!inserted.second) {
      *err = "duplicate merge pair";
      return false;
    }
  }
  return true;
}

// Parse added_tokens (dynamic: the target model embeds 33, ids 248044+).
bool ParseAddedTokens(const io::Json& root, TokenizerData& data,
                      std::string* err) {
  const io::Json* tokens = root.GetArray("added_tokens");
  if (tokens == nullptr) {
    *err = "added_tokens must be an array";
    return false;
  }
  if (tokens->array.size() > data.limits.max_added_tokens) {
    *err = "added tokens exceed max_added_tokens";
    return false;
  }
  data.added_tokens.reserve(tokens->array.size());
  for (const io::Json& token : tokens->array) {
    if (!token.IsObject()) {
      *err = "added token must be an object";
      return false;
    }
    const io::Json* id = token.Find("id");
    const io::Json* content = token.Find("content");
    if (id == nullptr || !id->IsNumber() || content == nullptr ||
        !content->IsString()) {
      *err = "added token must have numeric id and string content";
      return false;
    }
    const std::uint32_t token_id =
        static_cast<std::uint32_t>(id->number);
    if (token_id < Tokenizer::kPinnedBaseVocabSize) {
      *err = "added token id collides with the base vocabulary";
      return false;
    }
    AddedToken added;
    added.id = token_id;
    added.content = content->str;
    added.special = token.GetBool("special");
    if (!data.added_by_id.emplace(added.id, data.added_tokens.size())
             .second) {
      *err = "duplicate added token id";
      return false;
    }
    data.added_tokens.push_back(std::move(added));
  }
  return true;
}

bool CompileRegex(TokenizerData& data, std::string* err) {
  UErrorCode status = U_ZERO_ERROR;
  // Convert the UTF-8 pattern to UTF-16. uregex_open copies the pattern
  // contents, so the temporary UnicodeString may be destroyed afterwards.
  const icu::UnicodeString pattern = icu::UnicodeString::fromUTF8(
      icu::StringPiece(kPretokenizeRegex.data(),
                       static_cast<std::int32_t>(kPretokenizeRegex.size())));
  UParseError pe;
  data.regex =
      uregex_open(pattern.getBuffer(), pattern.length(), 0, &pe, &status);
  if (U_FAILURE(status) || data.regex == nullptr) {
    *err = std::string("could not compile tokenizer regex: ") +
           u_errorName(status);
    data.regex = nullptr;
    return false;
  }
  return true;
}

struct BpeNode {
  std::uint32_t token_id = 0;
  std::size_t previous = std::numeric_limits<std::size_t>::max();
  std::size_t next = std::numeric_limits<std::size_t>::max();
  std::uint32_t generation = 0;
  bool alive = true;
};

struct BpeCandidate {
  std::uint32_t rank = 0;
  std::size_t left = 0;
  std::size_t right = 0;
  std::uint32_t left_generation = 0;
  std::uint32_t right_generation = 0;
};

struct CandidateLater {
  bool operator()(const BpeCandidate& a, const BpeCandidate& b) const noexcept {
    if (a.rank != b.rank) return a.rank > b.rank;
    return a.left > b.left;
  }
};

// Run ranked BPE merges on a single pre-tokenized piece (already a valid
// UTF-8 byte string of GPT-2 byte tokens).
bool EncodeBpePiece(const TokenizerData& data, std::string_view piece,
                    std::vector<std::uint32_t>* output, std::string* err) {
  if (piece.empty()) return true;
  std::vector<BpeNode> nodes(piece.size());
  for (std::size_t i = 0; i < piece.size(); ++i) {
    nodes[i].token_id =
        data.byte_token_ids[static_cast<std::uint8_t>(piece[i])];
    nodes[i].previous =
        i == 0 ? std::numeric_limits<std::size_t>::max() : i - 1;
    nodes[i].next = i + 1 < piece.size() ? i + 1
                                         : std::numeric_limits<std::size_t>::max();
  }
  std::priority_queue<BpeCandidate, std::vector<BpeCandidate>, CandidateLater>
      candidates;
  auto add_candidate = [&](std::size_t left) {
    if (left == std::numeric_limits<std::size_t>::max() ||
        !nodes[left].alive ||
        nodes[left].next == std::numeric_limits<std::size_t>::max()) {
      return;
    }
    const std::size_t right = nodes[left].next;
    const auto rule =
        data.merges.find(PairKey(nodes[left].token_id, nodes[right].token_id));
    if (rule != data.merges.end()) {
      candidates.push(BpeCandidate{rule->second.rank, left, right,
                                   nodes[left].generation,
                                   nodes[right].generation});
    }
  };
  for (std::size_t i = 0; i + 1 < nodes.size(); ++i) add_candidate(i);
  while (!candidates.empty()) {
    const BpeCandidate c = candidates.top();
    candidates.pop();
    BpeNode& left = nodes[c.left];
    BpeNode& right = nodes[c.right];
    if (!left.alive || !right.alive || left.next != c.right ||
        left.generation != c.left_generation ||
        right.generation != c.right_generation) {
      continue;
    }
    const auto rule =
        data.merges.find(PairKey(left.token_id, right.token_id));
    if (rule == data.merges.end() || rule->second.rank != c.rank) continue;
    left.token_id = rule->second.result_id;
    ++left.generation;
    right.alive = false;
    ++right.generation;
    left.next = right.next;
    if (right.next != std::numeric_limits<std::size_t>::max()) {
      nodes[right.next].previous = c.left;
    }
    add_candidate(left.previous);
    add_candidate(c.left);
  }
  std::size_t current = 0;
  while (current != std::numeric_limits<std::size_t>::max()) {
    if (output->size() >= data.limits.max_tokens) {
      *err = "encoded output exceeds max_tokens";
      return false;
    }
    output->push_back(nodes[current].token_id);
    current = nodes[current].next;
  }
  return true;
}

// NFC-normalize `segment` (valid UTF-8), split on the pre-tokenize regex, and
// BPE-encode every piece into `output`.
bool EncodeNormalSegment(const TokenizerData& data, std::string_view segment,
                         std::vector<std::uint32_t>* output,
                         std::string* err) {
  if (segment.empty()) return true;
  if (!ValidateUtf8(segment)) {
    *err = "input is not valid UTF-8";
    return false;
  }
  if (segment.size() >
      static_cast<std::size_t>(std::numeric_limits<std::int32_t>::max())) {
    *err = "input exceeds ICU string length";
    return false;
  }
  UErrorCode status = U_ZERO_ERROR;
  const icu::Normalizer2* normalizer = icu::Normalizer2::getNFCInstance(status);
  if (U_FAILURE(status) || normalizer == nullptr) {
    *err = std::string("could not acquire ICU NFC normalizer: ") +
           u_errorName(status);
    return false;
  }
  const icu::UnicodeString unicode = icu::UnicodeString::fromUTF8(
      icu::StringPiece(segment.data(),
                       static_cast<std::int32_t>(segment.size())));
  icu::UnicodeString normalized;
  normalizer->normalize(unicode, normalized, status);
  if (U_FAILURE(status)) {
    *err = std::string("ICU NFC normalization failed: ") +
           u_errorName(status);
    return false;
  }
  // Point the compiled regex at the normalized subject text. The buffer must
  // remain valid (unmodified) for the duration of matching.
  uregex_setText(data.regex, normalized.getBuffer(), normalized.length(),
                 &status);
  if (U_FAILURE(status)) {
    *err = std::string("could not set tokenizer regex text: ") +
           u_errorName(status);
    return false;
  }
  std::string piece;
  std::int32_t previous_end = 0;
  auto encode_span = [&](std::int32_t start, std::int32_t end) -> bool {
    const icu::UnicodeString sub =
        normalized.tempSubStringBetween(start, end);
    piece.clear();
    sub.toUTF8String(piece);
    if (piece.size() > data.limits.max_input_bytes) {
      *err = "normalized piece exceeds max_input_bytes";
      return false;
    }
    return EncodeBpePiece(data, piece, output, err);
  };
  UErrorCode mstatus = U_ZERO_ERROR;
  bool found = uregex_find(data.regex, -1, &mstatus);
  while (found && !U_FAILURE(mstatus)) {
    const std::int32_t start = uregex_start(data.regex, 0, &mstatus);
    const std::int32_t end = uregex_end(data.regex, 0, &mstatus);
    if (U_FAILURE(mstatus) || start < previous_end || end <= start) {
      *err = "tokenizer regex returned invalid match offsets";
      return false;
    }
    if (start > previous_end && !encode_span(previous_end, start)) {
      return false;
    }
    if (!encode_span(start, end)) {
      return false;
    }
    previous_end = end;
    found = uregex_findNext(data.regex, &mstatus);
  }
  if (U_FAILURE(mstatus)) {
    *err = std::string("tokenizer regex matching failed: ") +
           u_errorName(mstatus);
    return false;
  }
  if (previous_end < normalized.length() &&
      !encode_span(previous_end, normalized.length())) {
    return false;
  }
  return true;
}

// Encode the full text, matching added/special tokens as whole substrings
// before the byte-level BPE path. This mirrors the `tokenizers` library and
// transformers' Qwen2Tokenizer: a special marker such as 超级用户 is emitted as
// its added-token id (248045) rather than being BPE-encoded into byte tokens.
// The earliest occurrence wins; at the same position the longest content wins.
bool EncodeImpl(const TokenizerData& data, std::string_view text,
                std::vector<std::uint32_t>* output, std::string* err) {
  if (text.size() > data.limits.max_input_bytes) {
    *err = "input exceeds max_input_bytes";
    return false;
  }
  output->clear();
  output->reserve(std::min(text.size(), data.limits.max_tokens));
  std::size_t cursor = 0;
  while (cursor < text.size()) {
    std::size_t match_position = std::string_view::npos;
    const AddedToken* matched = nullptr;
    for (const AddedToken& token : data.added_tokens) {
      if (token.content.empty()) continue;
      const std::size_t position = text.find(token.content, cursor);
      if (position != std::string_view::npos &&
          (match_position == std::string_view::npos ||
           position < match_position ||
           (position == match_position &&
            token.content.size() > matched->content.size()))) {
        match_position = position;
        matched = &token;
      }
    }
    if (matched == nullptr) {
      return EncodeNormalSegment(data, text.substr(cursor), output, err);
    }
    if (match_position > cursor &&
        !EncodeNormalSegment(data,
                             text.substr(cursor, match_position - cursor),
                             output, err)) {
      return false;
    }
    if (output->size() >= data.limits.max_tokens) {
      *err = "encoded output exceeds max_tokens";
      return false;
    }
    output->push_back(matched->id);
    cursor = match_position + matched->content.size();
  }
  return true;
}

}  // namespace

struct Tokenizer::Impl {
  TokenizerData data;
};

Tokenizer::Tokenizer(std::unique_ptr<Impl> impl) noexcept
    : impl_(std::move(impl)) {}
Tokenizer::~Tokenizer() = default;
Tokenizer::Tokenizer(Tokenizer&&) noexcept = default;
Tokenizer& Tokenizer::operator=(Tokenizer&&) noexcept = default;

Status Tokenizer::LoadJson(std::string_view json, const TokenizerLimits& limits,
                           std::unique_ptr<Tokenizer>* out) {
  if (json.size() > limits.max_tokenizer_bytes) {
    return Status::Fail("tokenizer JSON exceeds max_tokenizer_bytes");
  }
  io::Json root;
  Status parse = io::ParseJson(std::string(json), &root);
  if (!parse.ok()) return parse;
  std::string err;
  if (!ValidateSchema(root, &err)) {
    return Status::Fail("tokenizer schema mismatch: " + err);
  }
  auto impl = std::make_unique<Impl>();
  impl->data.limits = limits;
  if (!InitializeByteMapping(impl->data, &err)) {
    return Status::Fail(err);
  }
  if (!ParseModel(root, impl->data, &err)) {
    return Status::Fail("tokenizer model error: " + err);
  }
  if (!ParseAddedTokens(root, impl->data, &err)) {
    return Status::Fail("tokenizer added_tokens error: " + err);
  }
  if (!CompileRegex(impl->data, &err)) {
    return Status::Fail(err);
  }
  *out = std::unique_ptr<Tokenizer>(new Tokenizer(std::move(impl)));
  return Status();
}

Status Tokenizer::Load(const std::string& path, const TokenizerLimits& limits,
                       std::unique_ptr<Tokenizer>* out) {
  std::ifstream file(path, std::ios::binary);
  if (!file) {
    return Status::Fail("could not open tokenizer file: " + path);
  }
  std::string json((std::istreambuf_iterator<char>(file)),
                   std::istreambuf_iterator<char>());
  return LoadJson(json, limits, out);
}

Status Tokenizer::Encode(std::string_view text,
                         std::vector<std::uint32_t>* out) const {
  std::string err;
  if (!EncodeImpl(impl_->data, text, out, &err)) {
    return Status::Fail(err);
  }
  return Status();
}

Status Tokenizer::Decode(const std::vector<std::uint32_t>& ids,
                         bool skip_special_tokens, std::string* out) const {
  const TokenizerData& data = impl_->data;
  if (ids.size() > data.limits.max_tokens) {
    return Status::Fail("decode input exceeds max_tokens");
  }
  out->clear();
  std::string byte_symbols;
  auto flush = [&]() -> bool {
    std::string bytes;
    bytes.reserve(byte_symbols.size());
    std::size_t offset = 0;
    while (offset < byte_symbols.size()) {
      std::uint32_t cp = 0;
      if (!DecodeUtf8At(byte_symbols, &offset, &cp)) {
        return false;
      }
      const auto decoded = data.byte_decoder.find(cp);
      if (decoded == data.byte_decoder.end()) return false;
      bytes.push_back(static_cast<char>(decoded->second));
    }
    byte_symbols.clear();
    if (out->size() > data.limits.max_input_bytes ||
        bytes.size() > data.limits.max_input_bytes - out->size()) {
      return false;
    }
    out->append(bytes);
    return true;
  };
  for (const std::uint32_t id : ids) {
    if (id < data.id_to_token.size()) {
      const std::string& token = data.id_to_token[id];
      if (byte_symbols.size() >
          data.limits.max_input_bytes - token.size()) {
        return Status::Fail("decoded output exceeds max_input_bytes");
      }
      byte_symbols += token;
      continue;
    }
    const auto added = data.added_by_id.find(id);
    if (added == data.added_by_id.end()) {
      return Status::Fail("token id is outside the vocabulary");
    }
    if (!flush()) {
      return Status::Fail("decoded output exceeds max_input_bytes");
    }
    const AddedToken& token = data.added_tokens[added->second];
    if (!skip_special_tokens || !token.special) {
      if (out->size() > data.limits.max_input_bytes ||
          token.content.size() >
              data.limits.max_input_bytes - out->size()) {
        return Status::Fail("decoded output exceeds max_input_bytes");
      }
      out->append(token.content);
    }
  }
  if (!flush()) {
    return Status::Fail("decoded output exceeds max_input_bytes");
  }
  return Status();
}

std::size_t Tokenizer::BaseVocabSize() const noexcept {
  return impl_->data.id_to_token.size();
}
std::size_t Tokenizer::MergeCount() const noexcept {
  return impl_->data.merges.size();
}
std::size_t Tokenizer::AddedTokenCount() const noexcept {
  return impl_->data.added_tokens.size();
}
std::uint32_t Tokenizer::VocabSize() const noexcept {
  return static_cast<std::uint32_t>(impl_->data.id_to_token.size() +
                                    impl_->data.added_tokens.size());
}

}  // namespace text
}  // namespace q4t
