#include "file_rtf.h"

#include <charconv>
#include <chrono>
#include <unordered_map>
#include <unordered_set>

#include "file_rtf_encoding.h"
#include "object_protocol.h"

namespace q4t::server::file_detail {
namespace {
using object_detail::Error;
struct Font {
  int page = 0;
  bool explicit_page = false;
};
struct State {
  bool skip = false, star = false, fonts = false, deleted = false;
  bool upr = false, double_byte = false;
  int upr_children = 0, font = -1, associated_font = -1, unicode_fallback = 1;
};
ObjectReply Invalid() {
  return Error(400, "invalid_file_document", "Invalid RTF document");
}
ObjectReply Capacity() {
  return Error(413, "file_document_capacity",
               "RTF extraction capacity exceeded");
}
ObjectReply Live(const BinaryAccess& access) {
  if (access.cancelled && access.cancelled())
    return Error(499, "request_cancelled", "Document extraction cancelled");
  if (std::chrono::steady_clock::now() >= access.deadline)
    return Error(408, "request_deadline_exceeded",
                 "Document extraction timed out");
  return {};
}
bool Letter(char c) { return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z'); }
int Hex(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  return -1;
}
}  // namespace

ObjectReply ParseRtfDocument(const FileInputContent& input,
                             const FileDocumentLimits& limits,
                             const BinaryAccess& access, FileDocument* output) {
  if (!output)
    return Error(500, "file_document_state_invalid", "Missing output");
  *output = {};
  if (!ValidFileDocumentLimits(limits))
    return Error(500, "file_document_budget_invalid", "Invalid RTF budget");
  const auto bytes = std::string_view(input.bytes);
  if (bytes.size() > kFileInputBytes) return Capacity();
  if (!bytes.starts_with("{\\rtf")) return Invalid();
  if (auto reply = Live(access); !reply.ok()) return reply;
  std::vector<State> stack;
  std::unordered_map<int, Font> fonts;
  std::string text, encoded;
  int default_page = 1252, default_font = 0, fallback = 0;
  uint32_t high_surrogate = 0;
  bool root = false, closed = false;
  const auto visible = [&] {
    return !stack.empty() && !stack.back().skip && !stack.back().deleted;
  };
  const auto flush = [&]() -> ObjectReply {
    if (encoded.empty()) return {};
    if (high_surrogate || stack.empty()) return Invalid();
    const auto& state = stack.back();
    int font = state.double_byte && state.associated_font >= 0
                   ? state.associated_font
                   : state.font;
    if (font < 0) font = default_font;
    const auto found = fonts.find(font);
    int page = found == fonts.end() || !found->second.page ? default_page
                                                           : found->second.page;
    std::string decoded;
    auto reply = DecodeRtfBytes(page, encoded,
                                limits.max_text_bytes - text.size(), &decoded);
    if (!reply.ok()) return reply;
    text += decoded;
    encoded.clear();
    return {};
  };
  const auto scalar = [&](uint32_t value, bool utf16 = false) -> ObjectReply {
    if (!visible()) return {};
    if (auto reply = flush(); !reply.ok()) return reply;
    if (utf16 && value >= 0xd800 && value <= 0xdbff) {
      if (high_surrogate) return Invalid();
      high_surrogate = value;
      return {};
    }
    if (utf16 && value >= 0xdc00 && value <= 0xdfff) {
      if (!high_surrogate) return Invalid();
      value = 0x10000 + ((high_surrogate - 0xd800) << 10) + value - 0xdc00;
      high_surrogate = 0;
    } else if (high_surrogate)
      return Invalid();
    if (!value || value > 0x10ffff || (value >= 0xd800 && value <= 0xdfff))
      return Invalid();
    std::string next;
    RtfUtf8(value, &next);
    if (next.size() > limits.max_text_bytes - text.size()) return Capacity();
    text += next;
    return {};
  };
  const auto byte = [&](unsigned char c) -> ObjectReply {
    if (fallback) {
      --fallback;
      return {};
    }
    if (!visible()) return {};
    if (high_surrogate || c == 0) return Invalid();
    encoded.push_back(c);
    // Never cut a multibyte character at an arbitrary buffer boundary. This
    // staging limit is conservative for the supported code pages; conversion
    // applies the exact UTF-8 output limit at a syntactic run boundary.
    if (encoded.size() > 4 * (limits.max_text_bytes - text.size()))
      return Capacity();
    return {};
  };
  size_t next_check = 0;
  for (size_t i = 0; i < bytes.size();) {
    if (i >= next_check) {
      if (auto reply = Live(access); !reply.ok()) return reply;
      next_check = i + 4096;
    }
    const unsigned char c = bytes[i++];
    if (closed) {
      if (c != ' ' && c != '\r' && c != '\n' && c != '\t') return Invalid();
      continue;
    }
    if (c == '\r' || c == '\n') continue;
    if (c == '{') {
      if (auto reply = flush(); !reply.ok()) return reply;
      fallback = 0;
      if (stack.size() >= 128) return Capacity();
      State child = stack.empty() ? State{} : stack.back();
      if (!stack.empty() && stack.back().upr) {
        const auto branch = ++stack.back().upr_children;
        if (branch > 2) return Invalid();
        if (branch == 1) child.skip = true;
      }
      child.star = child.upr = false;
      child.upr_children = 0;
      stack.push_back(child);
      continue;
    }
    if (c == '}') {
      if (stack.empty()) return Invalid();
      if (auto reply = flush(); !reply.ok()) return reply;
      if (stack.back().upr && stack.back().upr_children != 2) return Invalid();
      stack.pop_back();
      fallback = 0;
      if (stack.empty()) closed = true;
      continue;
    }
    if (stack.empty()) return Invalid();
    auto& state = stack.back();
    if (c != '\\') {
      if (!root) return Invalid();
      if (auto reply = byte(c); !reply.ok()) return reply;
      continue;
    }
    if (i == bytes.size()) return Invalid();
    char symbol = bytes[i++];
    if (!Letter(symbol)) {
      if (symbol == '\'') {
        if (bytes.size() - i < 2 || Hex(bytes[i]) < 0 || Hex(bytes[i + 1]) < 0)
          return Invalid();
        const auto value = Hex(bytes[i]) * 16 + Hex(bytes[i + 1]);
        i += 2;
        if (auto reply = byte(value); !reply.ok()) return reply;
      } else {
        if (fallback) {
          --fallback;
          continue;
        }
        if (symbol == '\\' || symbol == '{' || symbol == '}') {
          if (auto reply = byte(symbol); !reply.ok()) return reply;
        } else if (symbol == '*')
          state.star = true;
        else if (symbol == '~' || symbol == '-' || symbol == '_') {
          const uint32_t value = symbol == '~'   ? 0xa0
                                 : symbol == '-' ? 0xad
                                                 : 0x2011;
          if (auto reply = scalar(value); !reply.ok()) return reply;
        } else if (symbol == '\r' || symbol == '\n') {
          if (symbol == '\r' && i < bytes.size() && bytes[i] == '\n') ++i;
          if (auto reply = scalar('\n'); !reply.ok()) return reply;
        }
      }
      continue;
    }
    const size_t start = i - 1;
    while (i < bytes.size() && Letter(bytes[i])) ++i;
    const auto word = bytes.substr(start, i - start);
    if (word.size() > 64) return Capacity();
    const size_t number_start = i;
    if (i < bytes.size() && bytes[i] == '-') ++i;
    const size_t digits_start = i;
    while (i < bytes.size() && bytes[i] >= '0' && bytes[i] <= '9') ++i;
    const bool numbered = i > digits_start;
    if (digits_start != number_start && !numbered) return Invalid();
    int number = 0;
    if (numbered) {
      const auto parsed = std::from_chars(bytes.data() + number_start,
                                          bytes.data() + i, number);
      if (parsed.ec != std::errc{}) return Invalid();
    }
    if (i < bytes.size() && bytes[i] == ' ') ++i;
    if (word == "bin") {
      if (!numbered || number < 0 ||
          static_cast<size_t>(number) > bytes.size() - i)
        return Invalid();
      i += number;
      if (fallback) --fallback;
      continue;
    }
    if (fallback) {
      --fallback;
      continue;
    }
    if (auto reply = flush(); !reply.ok()) return reply;
    if (!root && word != "rtf") return Invalid();
    if (state.star) {
      const bool text_destination = word == "ud" || word == "shpinst" ||
                                    word == "shptxt" || word == "fldrslt";
      if (!text_destination) {
        state.skip = true;
        state.fonts = false;
      }
      state.star = false;
    }
    if (state.fonts && (word == "fontemb" || word == "fontfile"))
      state.fonts = false;
    if (state.skip && !state.fonts) continue;
    if (state.fonts && word != "f" && word != "fcharset" && word != "cpg" &&
        word != "fonttbl")
      continue;
    if (word == "rtf") {
      if (root || stack.size() != 1 || !numbered || number != 1)
        return Invalid();
      root = true;
    } else if (word == "uc") {
      if (!numbered || number < 0 || number > 1024) return Invalid();
      state.unicode_fallback = number;
    } else if (word == "u") {
      if (!numbered || number < -32768 || number > 65535) return Invalid();
      if (auto reply = scalar(static_cast<uint16_t>(number), true); !reply.ok())
        return reply;
      fallback = state.unicode_fallback;
    } else if (word == "upr") {
      state.upr = true;
      state.upr_children = 0;
    } else if (word == "ud" || word == "shpinst" || word == "shptxt") {
      state.star = false;
    } else if (word == "fonttbl") {
      state.fonts = true;
      state.skip = true;
    } else if (word == "f" || word == "af") {
      if (!numbered || number < 0 || number > 65535) return Invalid();
      if (word == "af")
        state.associated_font = number;
      else
        state.font = number;
      if (state.fonts && !fonts.contains(number)) {
        if (fonts.size() >= 4096) return Capacity();
        fonts.emplace(number, Font{});
      }
    } else if (state.fonts && (word == "cpg" || word == "fcharset")) {
      if (!numbered || number < 0 || number > 65535 || state.font < 0)
        return Invalid();
      auto& font = fonts[state.font];
      if (word == "cpg") {
        font.page = number;
        font.explicit_page = true;
      } else if (!font.explicit_page)
        font.page = RtfCharsetCodepage(number);
    } else if (word == "deff") {
      if (!numbered || number < 0 || number > 65535) return Invalid();
      default_font = number;
    } else if (word == "ansicpg") {
      if (!numbered || number <= 0 || number > 65535) return Invalid();
      default_page = number;
    } else if (word == "ansi" || word == "mac" || word == "pc" ||
               word == "pca") {
      default_page = word == "ansi"  ? 1252
                     : word == "mac" ? 10000
                     : word == "pc"  ? 437
                                     : 850;
    } else if (word == "plain") {
      state.font = default_font;
      state.associated_font = -1;
      state.deleted = false;
    } else if (word == "dbch")
      state.double_byte = true;
    else if (word == "loch" || word == "hich")
      state.double_byte = false;
    else if (word == "deleted")
      state.deleted = !numbered || number != 0;
    else if (state.star) {
      state.skip = true;
      state.star = false;
    } else {
      static const std::unordered_set<std::string_view> ignored = {
          "colortbl",
          "stylesheet",
          "info",
          "pict",
          "object",
          "listtable",
          "listoverridetable",
          "rsidtbl",
          "revtbl",
          "xmlnstbl",
          "themedata",
          "colorschememapping",
          "datastore",
          "generator",
          "filetbl",
          "latentstyles",
          "fldinst",
          "datafield",
          "bkmkstart",
          "bkmkend",
          "nonshppict",
          "shppict",
          "fontemb",
          "fontfile",
          "atnauthor",
          "atnid",
          "atndate"};
      if (ignored.contains(word))
        state.skip = true;
      else {
        uint32_t value = 0;
        if (word == "par" || word == "line" || word == "row" ||
            word == "nestrow" || word == "page")
          value = '\n';
        else if (word == "tab" || word == "cell" || word == "nestcell")
          value = '\t';
        else if (word == "emdash")
          value = 0x2014;
        else if (word == "endash")
          value = 0x2013;
        else if (word == "emspace")
          value = 0x2003;
        else if (word == "enspace")
          value = 0x2002;
        else if (word == "qmspace")
          value = 0x2005;
        else if (word == "bullet")
          value = 0x2022;
        else if (word == "lquote")
          value = 0x2018;
        else if (word == "rquote")
          value = 0x2019;
        else if (word == "ldblquote")
          value = 0x201c;
        else if (word == "rdblquote")
          value = 0x201d;
        if (value)
          if (auto reply = scalar(value); !reply.ok()) return reply;
      }
    }
  }
  if (!root || !closed || !stack.empty() || high_surrogate) return Invalid();
  if (auto reply = Live(access); !reply.ok()) return reply;
  output->text = std::move(text);
  output->encoding = "utf-8";
  output->source_sha256 = object_detail::Digest(input.bytes);
  return {};
}
}  // namespace q4t::server::file_detail
