#include "q4t/server/schema_prefix.h"

#include <unordered_set>

#include "q4t/server/custom_tools.h"

namespace q4t::server {
namespace {
using io::Json;
bool Whitespace(char c) {
  return c == ' ' || c == '\t' || c == '\r' || c == '\n';
}
bool Digit(char c) { return c >= '0' && c <= '9'; }
bool Delimiter(char c) {
  return Whitespace(c) || c == ',' || c == ']' || c == '}';
}

class Parser {
 public:
  Parser(std::string_view text, size_t* work) : text_(text), work_(work) {}
  Status Run(SchemaPrefixValue* out, size_t* consumed) {
    if (!work_ || text_.size() > 1024 * 1024 || text_.size() > *work_)
      return Resource("prefix bytes/work limit");
    *work_ -= text_.size();
    auto status = Value(out, 0);
    if (status && out->complete) Space();
    *consumed = at_;
    return status;
  }

 private:
  Status Invalid(std::string_view reason) const {
    return Status::Fail("invalid JSON prefix: " + std::string(reason));
  }
  Status Resource(std::string_view reason) const {
    return Status::Fail("schema_resource_exhausted: " + std::string(reason));
  }
  Status ChargeValue() {
    if (++values_ > 65536 || !*work_)
      return Resource("prefix value/work limit");
    --*work_;
    return {};
  }
  void Space() {
    while (at_ < text_.size() && Whitespace(text_[at_])) ++at_;
  }
  Status String(std::string* value, bool* complete) {
    CustomInputStream decoder;
    size_t used = 0;
    const auto status = decoder.Append(text_.substr(at_), &used, value);
    at_ += used;
    *complete = decoder.Done();
    return status ? Status{} : Invalid("string encoding");
  }
  Status Number(SchemaPrefixValue* out) {
    enum class State {
      kStart,
      kSign,
      kZero,
      kInteger,
      kPoint,
      kFraction,
      kExponent,
      kExponentSign,
      kExponentDigits
    };
    State state = State::kStart;
    const auto begin = at_;
    while (at_ < text_.size() && !Delimiter(text_[at_])) {
      const char c = text_[at_++];
      switch (state) {
        case State::kStart:
          if (c == '-') {
            state = State::kSign;
            break;
          }
          [[fallthrough]];
        case State::kSign:
          if (!Digit(c)) return Invalid("number integer digit");
          state = c == '0' ? State::kZero : State::kInteger;
          break;
        case State::kZero:
        case State::kInteger:
          if (c == '.')
            state = State::kPoint;
          else if (c == 'e' || c == 'E')
            state = State::kExponent;
          else if (!Digit(c) || state == State::kZero)
            return Invalid("number syntax");
          break;
        case State::kPoint:
          if (!Digit(c)) return Invalid("number fractional digit");
          state = State::kFraction;
          break;
        case State::kFraction:
          if (c == 'e' || c == 'E')
            state = State::kExponent;
          else if (!Digit(c))
            return Invalid("number fraction syntax");
          break;
        case State::kExponent:
          if (c == '+' || c == '-') {
            state = State::kExponentSign;
            break;
          }
          [[fallthrough]];
        case State::kExponentSign:
          if (!Digit(c)) return Invalid("number exponent digit");
          state = State::kExponentDigits;
          break;
        case State::kExponentDigits:
          if (!Digit(c)) return Invalid("number exponent syntax");
          break;
      }
    }
    out->value.type = Json::Type::kNumber;
    out->value.str = std::string(text_.substr(begin, at_ - begin));
    // At an input boundary another digit/exponent can still extend the value.
    // A delimiter, or the enclosing root's completion, settles its value.
    if (at_ == text_.size()) return {};
    if (state != State::kZero && state != State::kInteger &&
        state != State::kFraction && state != State::kExponentDigits)
      return Invalid("incomplete number before delimiter");
    const auto spelling = out->value.str;
    const auto status = io::ParseJson(spelling, &out->value, true,
                                      {64, 65536, 1024 * 1024, true, true});
    if (!status) return Invalid(status.message());
    out->complete = true;
    return {};
  }
  Status Literal(SchemaPrefixValue* out, std::string_view expected) {
    for (char c : expected) {
      if (at_ == text_.size()) return {};
      if (text_[at_++] != c) return Invalid("literal");
    }
    out->complete = true;
    return {};
  }
  Status Container(SchemaPrefixValue* out, size_t depth, bool object) {
    ++at_;
    out->value.type = object ? Json::Type::kObject : Json::Type::kArray;
    const char end = object ? '}' : ']';
    Space();
    if (at_ == text_.size()) return {};
    if (text_[at_] == end) {
      ++at_;
      out->complete = true;
      return {};
    }
    std::unordered_set<std::string> keys;
    while (true) {
      if (object) {
        if (at_ == text_.size()) return {};
        if (text_[at_] != '"') return Invalid("object key");
        auto status = ChargeValue();
        if (!status) return status;
        bool complete = false;
        status = String(&out->pending_key, &complete);
        if (!status || !complete) return status;
        out->has_pending_key = true;
        if (!keys.insert(out->pending_key).second)
          return Invalid("duplicate key");
        Space();
        if (at_ == text_.size()) return {};
        if (text_[at_++] != ':') return Invalid("object colon");
      }
      auto child = std::make_unique<SchemaPrefixValue>();
      auto status = Value(child.get(), depth + 1);
      if (!status) return status;
      if (!child->complete) {
        out->pending = std::move(child);
        return {};
      }
      if (object) {
        out->value.object.emplace_back(std::move(out->pending_key),
                                       std::move(child->value));
        out->pending_key.clear();
        out->has_pending_key = false;
      } else {
        out->value.array.push_back(std::move(child->value));
      }
      Space();
      if (at_ == text_.size()) return {};
      if (text_[at_] == end) {
        ++at_;
        out->complete = true;
        return {};
      }
      if (text_[at_++] != ',') return Invalid("container separator");
      Space();
      if (at_ < text_.size() && text_[at_] == end)
        return Invalid("trailing comma");
    }
  }
  Status Value(SchemaPrefixValue* out, size_t depth) {
    Space();
    if (at_ == text_.size()) return {};
    if (depth > 64) return Resource("prefix nesting limit");
    auto status = ChargeValue();
    if (!status) return status;
    out->started = true;
    const char c = text_[at_];
    if ((c == '{' || c == '[') && depth >= 64)
      return Resource("prefix container nesting limit");
    if (c == '{' || c == '[') return Container(out, depth, c == '{');
    if (c == '"') {
      out->value.type = Json::Type::kString;
      return String(&out->value.str, &out->complete);
    }
    if (c == 't' || c == 'f') {
      out->value.type = Json::Type::kBool;
      out->value.boolean = c == 't';
      return Literal(out, c == 't' ? "true" : "false");
    }
    if (c == 'n') return Literal(out, "null");
    if (c == '-' || Digit(c)) return Number(out);
    return Invalid("value start");
  }
  std::string_view text_;
  size_t* work_;
  size_t at_ = 0, values_ = 0;
};
}  // namespace

Status ParseSchemaPrefix(std::string_view text, SchemaPrefixValue* out,
                         size_t* consumed, size_t* work_remaining) {
  *out = {};
  *consumed = 0;
  return Parser(text, work_remaining).Run(out, consumed);
}

}  // namespace q4t::server
