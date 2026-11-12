#include "file_rtf_encoding.h"

#include <iconv.h>

#include <array>
#include <cerrno>
#include <unordered_map>

#include "file_rtf.h"
#include "object_protocol.h"

namespace q4t::server::file_detail {
namespace {
std::unordered_map<int, iconv_t>& Converters() {
  // Owned for the lifetime of the isolated process. No parent initialization.
  static std::unordered_map<int, iconv_t> converters;
  return converters;
}
}  // namespace
ObjectReply LoadRtfRuntime() {
  auto& converters = Converters();
  if (converters.empty()) {
    for (int page :
         {437,   708,   709,   710,   711,   720,   819,   850,   852,
          860,   862,   863,   864,   865,   866,   874,   932,   936,
          949,   950,   1250,  1251,  1252,  1253,  1254,  1255,  1256,
          1257,  1258,  1361,  10000, 10001, 10002, 10003, 10004, 10005,
          10006, 10007, 10008, 10021, 10029, 10081, 57002, 57003, 57004,
          57005, 57006, 57007, 57008, 57009, 57010, 57011, 65001}) {
      std::string name = "CP" + std::to_string(page);
      if (page == 65001) name = "UTF-8";
      if (page == 10000) name = "MACINTOSH";
      if (page == 10007) name = "MAC-CYRILLIC";
      if (page == 10029) name = "MAC-CENTRALEUROPE";
      if (page == 708) name = "ASMO-708";
      if (page == 819) name = "ISO-8859-1";
      const auto converter = iconv_open("UTF-8", name.c_str());
      if (converter != reinterpret_cast<iconv_t>(-1))
        converters.emplace(page, converter);
    }
  }
  if (!converters.contains(1252) || !converters.contains(65001))
    return object_detail::Error(503, "file_character_encoding_unavailable",
                                "Document character conversion unavailable");
  return {};
}
void RtfUtf8(uint32_t scalar, std::string* out) {
  if (scalar < 0x80)
    out->push_back(scalar);
  else if (scalar < 0x800) {
    out->push_back(0xc0 | (scalar >> 6));
    out->push_back(0x80 | (scalar & 63));
  } else if (scalar < 0x10000) {
    out->push_back(0xe0 | (scalar >> 12));
    out->push_back(0x80 | ((scalar >> 6) & 63));
    out->push_back(0x80 | (scalar & 63));
  } else {
    out->push_back(0xf0 | (scalar >> 18));
    out->push_back(0x80 | ((scalar >> 12) & 63));
    out->push_back(0x80 | ((scalar >> 6) & 63));
    out->push_back(0x80 | (scalar & 63));
  }
}
ObjectReply DecodeRtfBytes(int page, std::string_view bytes, uint64_t max_bytes,
                           std::string* output) {
  output->clear();
  if (bytes.empty()) return {};
  if (page == 42) {
    // Word's documented SYMBOL_CHARSET representation preserves the font's
    // private glyph codes; do not pretend they are ANSI Latin characters.
    if (bytes.size() > max_bytes / 3)
      return object_detail::Error(413, "file_document_capacity",
                                  "Document character capacity exceeded");
    for (unsigned char c : bytes) RtfUtf8(0xf000 + c, output);
    return {};
  }
  const auto found = Converters().find(page);
  if (found == Converters().end())
    return object_detail::Error(503, "file_character_encoding_unavailable",
                                "Document code page unavailable");
  const auto converter = found->second;
  if (iconv(converter, nullptr, nullptr, nullptr, nullptr) == size_t(-1))
    return object_detail::Error(503, "file_character_encoding_unavailable",
                                "Cannot reset character conversion");
  char* cursor = const_cast<char*>(bytes.data());
  size_t left = bytes.size();
  std::array<char, 65536> buffer;
  while (left) {
    char* destination = buffer.data();
    size_t space = buffer.size();
    const auto converted =
        iconv(converter, &cursor, &left, &destination, &space);
    const int error = errno;
    const auto produced = buffer.size() - space;
    if (produced > max_bytes - output->size()) {
      output->clear();
      return object_detail::Error(413, "file_document_capacity",
                                  "Document character capacity exceeded");
    }
    output->append(buffer.data(), produced);
    if (converted == size_t(-1) && error != E2BIG) {
      output->clear();
      return object_detail::Error(400, "invalid_file_document",
                                  "Invalid document character sequence");
    }
  }
  return {};
}
int RtfCharsetCodepage(int charset) {
  switch (charset) {
    case 0:
      return 1252;
    case 1:
      return 0;
    case 2:
      return 42;
    case 77:
      return 10000;
    case 78:
      return 10001;
    case 79:
      return 10003;
    case 80:
      return 10008;
    case 81:
      return 10002;
    case 83:
      return 10005;
    case 84:
      return 10004;
    case 85:
      return 10006;
    case 86:
      return 10081;
    case 87:
      return 10021;
    case 88:
      return 10029;
    case 89:
      return 10007;
    case 128:
      return 932;
    case 129:
      return 949;
    case 130:
      return 1361;
    case 134:
      return 936;
    case 136:
      return 950;
    case 161:
      return 1253;
    case 162:
      return 1254;
    case 163:
      return 1258;
    case 177:
      return 1255;
    case 178:
      return 1256;
    case 186:
      return 1257;
    case 204:
      return 1251;
    case 222:
      return 874;
    case 238:
      return 1250;
    case 254:
      return 437;
    case 255:
      return 850;
    default:
      return -1;
  }
}
}  // namespace q4t::server::file_detail
