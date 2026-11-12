#include "file_office_internal.h"

#include <charconv>
#include <cmath>
#include <limits>
#include <unordered_set>

#include "file_rtf_encoding.h"

namespace q4t::server::file_detail {
namespace {
constexpr size_t kUnset = std::numeric_limits<size_t>::max();
struct SheetRef {
  std::string name, id;
};
struct Styles {
  std::unordered_map<uint64_t, std::string> formats;
  std::vector<uint64_t> cells;
};
// ST_Xstring escapes UTF-16 code units. A decoded underscore is not scanned
// again: _x005F_x0041_ denotes the literal spelling _x0041_.
ObjectReply StringValue(std::string_view input, std::string* output) {
  output->clear();
  auto code = [&](size_t at, uint32_t* value) {
    if (at + 7 > input.size() || input[at] != '_' || input[at + 1] != 'x' ||
        input[at + 6] != '_')
      return false;
    *value = 0;
    for (size_t i = at + 2; i < at + 6; ++i) {
      const char c = input[i];
      const int digit = c >= '0' && c <= '9'   ? c - '0'
                        : c >= 'a' && c <= 'f' ? c - 'a' + 10
                        : c >= 'A' && c <= 'F' ? c - 'A' + 10
                                               : -1;
      if (digit < 0) return false;
      *value = *value * 16 + digit;
    }
    return true;
  };
  for (size_t i = 0; i < input.size();) {
    uint32_t value;
    if (!code(i, &value)) {
      output->push_back(input[i++]);
      continue;
    }
    i += 7;
    if (value >= 0xd800 && value <= 0xdbff) {
      uint32_t low;
      if (!code(i, &low) || low < 0xdc00 || low > 0xdfff)
        return OfficeInvalid();
      i += 7;
      value = 0x10000 + ((value - 0xd800) << 10) + low - 0xdc00;
    } else if (value >= 0xdc00 && value <= 0xdfff)
      return OfficeInvalid();
    RtfUtf8(value, output);
  }
  return {};
}
std::string_view Trim(std::string_view value) {
  constexpr std::string_view space = " \t\r\n";
  const auto first = value.find_first_not_of(space);
  if (first == value.npos) return {};
  return value.substr(first, value.find_last_not_of(space) - first + 1);
}
ObjectReply SharedStrings(OfficeContext& context, std::string_view part,
                          std::vector<std::string>* strings) {
  size_t item = kUnset, text = kUnset, phonetic = kUnset;
  std::string value;
  uint64_t bytes = 0;
  return context.Xml(part, [&](const XmlNode& node) -> ObjectReply {
    const bool sheet = OfficeSpace(node.space, "spreadsheetml");
    if (node.kind == XmlKind::kStart && !node.depth &&
        (!sheet || node.name != "sst"))
      return OfficeInvalid();
    if (phonetic != kUnset) {
      if (node.kind == XmlKind::kEnd && node.depth == phonetic)
        phonetic = kUnset;
      return {};
    }
    if (node.kind == XmlKind::kStart && sheet) {
      if (node.name == "si") {
        if (item != kUnset || node.depth != 1) return OfficeInvalid();
        item = node.depth;
        value.clear();
      } else if (node.name == "rPh")
        phonetic = node.depth;
      else if (node.name == "t" && item != kUnset)
        text = node.depth;
    } else if (node.kind == XmlKind::kText && text != kUnset) {
      if (node.value.size() > context.limits.max_cell_bytes - value.size())
        return OfficeCapacity();
      value += node.value;
    } else if (node.kind == XmlKind::kEnd && sheet) {
      if (node.depth == text) text = kUnset;
      if (node.depth == item) {
        std::string decoded;
        auto reply = StringValue(value, &decoded);
        if (!reply.ok()) return reply;
        value = std::move(decoded);
        if (strings->size() >= 1000000 ||
            value.size() > context.package_limits.max_bytes - bytes)
          return OfficeCapacity();
        bytes += value.size();
        strings->push_back(std::move(value));
        item = kUnset;
      }
    }
    return {};
  });
}
ObjectReply ReadStyles(OfficeContext& context, std::string_view part,
                       Styles* styles) {
  size_t cell_xfs = kUnset, number_formats = kUnset;
  return context.Xml(part, [&](const XmlNode& node) -> ObjectReply {
    const bool excel = OfficeSpace(node.space, "spreadsheetml");
    if (node.kind == XmlKind::kStart && !node.depth &&
        (!excel || node.name != "styleSheet"))
      return OfficeInvalid();
    if (!excel) return {};
    if (node.kind == XmlKind::kStart) {
      if (node.name == "numFmts" && node.depth == 1)
        number_formats = node.depth;
      else if (node.name == "numFmt" && number_formats != kUnset &&
               node.depth == number_formats + 1) {
        uint64_t id = 0;
        const auto code = node.Attribute("formatCode");
        if (!Unsigned(node.Attribute("numFmtId"), &id) || id > 65535 ||
            code.size() > 1024 || !styles->formats.emplace(id, code).second)
          return OfficeInvalid();
      } else if (node.name == "cellXfs" && node.depth == 1)
        cell_xfs = node.depth;
      else if (node.name == "xf" && cell_xfs != kUnset &&
               node.depth == cell_xfs + 1) {
        uint64_t id = 0;
        const auto value = node.Attribute("numFmtId");
        if ((!value.empty() && !Unsigned(value, &id)) || id > 65535)
          return OfficeInvalid();
        if (styles->cells.size() >= 65536) return OfficeCapacity();
        styles->cells.push_back(id);
      }
    } else if (node.kind == XmlKind::kEnd) {
      if (node.depth == cell_xfs) cell_xfs = kUnset;
      if (node.depth == number_formats) number_formats = kUnset;
    }
    return {};
  });
}
bool CellPosition(std::string_view reference, uint64_t* column, uint64_t* row) {
  *column = 0;
  size_t length = 0;
  while (length < reference.size() && reference[length] >= 'A' &&
         reference[length] <= 'Z') {
    *column = *column * 26 + reference[length++] - 'A' + 1;
    if (*column > 16384) return false;
  }
  return length && Unsigned(reference.substr(length), row) && *row &&
         *row <= 1048576;
}
ObjectReply Value(std::string_view type, std::string_view raw,
                  std::string_view inline_text, std::string_view formula,
                  bool formula_present, const std::vector<std::string>& shared,
                  const Styles& styles, std::string_view style, bool date1904,
                  std::string* output) {
  if (type == "s") {
    uint64_t index;
    if (!Unsigned(raw, &index) || index >= shared.size())
      return OfficeInvalid();
    *output = shared[index];
  } else if (type == "inlineStr") {
    auto reply = StringValue(inline_text, output);
    if (!reply.ok()) return reply;
  } else if (type == "b") {
    if (raw != "0" && raw != "1") return OfficeInvalid();
    *output = raw == "1" ? "true" : "false";
  } else if (type.empty() || type == "n") {
    if (!raw.empty()) {
      double value;
      auto number = Trim(raw);
      if (number.starts_with('+')) number.remove_prefix(1);
      if (number.empty()) return OfficeInvalid();
      const auto parsed =
          std::from_chars(number.data(), number.data() + number.size(), value);
      if (parsed.ec != std::errc{} ||
          parsed.ptr != number.data() + number.size() || !std::isfinite(value))
        return OfficeInvalid();
    }
    *output = raw;
  } else if (type == "str") {
    auto reply = StringValue(raw, output);
    if (!reply.ok()) return reply;
  } else if (type == "e" || type == "d")
    *output = raw;
  else
    return OfficeInvalid();
  if (!style.empty()) {
    uint64_t index;
    if (!Unsigned(style, &index) ||
        (index >= styles.cells.size() && !(index == 0 && styles.cells.empty())))
      return OfficeInvalid();
    const auto format = styles.cells.empty() ? 0 : styles.cells[index];
    if (format && !raw.empty() && (type.empty() || type == "n")) {
      const auto found = styles.formats.find(format);
      *output +=
          " [number format: " +
          (found == styles.formats.end() ? "builtin " + std::to_string(format)
                                         : found->second) +
          "; date system: " + (date1904 ? "1904" : "1900") + "]";
    }
  }
  if (formula_present) {
    if (output->empty())
      *output =
          "[formula: " + std::string(formula) + "; cached value unavailable]";
    else
      *output +=
          " [cached formula result; formula: " + std::string(formula) + "]";
  }
  return {};
}
ObjectReply Worksheet(OfficeContext& context, std::string_view part,
                      std::string name, const std::vector<std::string>& shared,
                      const Styles& styles, bool date1904) {
  FileDocumentSheet sheet;
  sheet.name = std::move(name);
  size_t data = kUnset, row_depth = kUnset, cell = kUnset, value = kUnset,
         inline_value = kUnset, formula_depth = kUnset, phonetic = kUnset;
  uint64_t row_number = 0, column = 0, previous_column = 0, row_bytes = 0;
  std::vector<std::string> row;
  std::string type, style, raw, inline_text, formula;
  bool formula_present = false;
  auto reply = context.Xml(part, [&](const XmlNode& node) -> ObjectReply {
    const bool excel = OfficeSpace(node.space, "spreadsheetml");
    if (node.kind == XmlKind::kStart && !node.depth &&
        (!excel || node.name != "worksheet"))
      return OfficeInvalid();
    if (phonetic != kUnset) {
      if (node.kind == XmlKind::kEnd && node.depth == phonetic)
        phonetic = kUnset;
      return {};
    }
    if (node.kind == XmlKind::kStart && excel) {
      if (node.name == "sheetData") {
        if (data != kUnset) return OfficeInvalid();
        data = node.depth;
      } else if (node.name == "row" && data != kUnset) {
        if (row_depth != kUnset || node.depth != data + 1)
          return OfficeInvalid();
        const auto r = node.Attribute("r");
        row_number = sheet.source_rows + 1;
        if ((!r.empty() && !Unsigned(r, &row_number)) || !row_number ||
            row_number > 1048576 || row_number <= sheet.source_rows)
          return OfficeInvalid();
        if (row_number > sheet.source_rows + 1) {
          auto gap =
              context.Rows(sheet, {}, row_number - sheet.source_rows - 1);
          if (!gap.ok()) return gap;
        }
        row_depth = node.depth;
        row.clear();
        row_bytes = previous_column = 0;
      } else if (node.name == "c" && row_depth != kUnset) {
        if (cell != kUnset || node.depth != row_depth + 1)
          return OfficeInvalid();
        cell = node.depth;
        column = previous_column + 1;
        const auto r = node.Attribute("r");
        uint64_t cell_row = row_number;
        if (!r.empty() &&
            (!CellPosition(r, &column, &cell_row) || cell_row != row_number))
          return OfficeInvalid();
        if (column <= previous_column) return OfficeInvalid();
        if (column > context.limits.max_columns) return OfficeCapacity();
        previous_column = column;
        type = node.Attribute("t");
        style = node.Attribute("s");
        raw.clear();
        inline_text.clear();
        formula.clear();
        formula_present = false;
      } else if (cell != kUnset) {
        if (node.name == "v")
          value = node.depth;
        else if (node.name == "t")
          inline_value = node.depth;
        else if (node.name == "f") {
          if (formula_present) return OfficeInvalid();
          formula_present = true;
          formula_depth = node.depth;
          // Shared/array/data-table formulas may have no body. Preserve their
          // actual linkage and range instead of treating an empty f as blank.
          for (const auto& attribute : node.attributes) {
            if (!attribute.space.empty()) continue;
            const auto item =
                "[" + attribute.name + "=" + attribute.value + "]";
            if (item.size() > context.limits.max_cell_bytes - formula.size())
              return OfficeCapacity();
            formula += item;
          }
        } else if (node.name == "rPh")
          phonetic = node.depth;
      }
    } else if (node.kind == XmlKind::kText && cell != kUnset) {
      std::string* destination = value != kUnset           ? &raw
                                 : inline_value != kUnset  ? &inline_text
                                 : formula_depth != kUnset ? &formula
                                                           : nullptr;
      if (destination) {
        if (node.value.size() >
            context.limits.max_cell_bytes - destination->size())
          return OfficeCapacity();
        *destination += node.value;
      }
    } else if (node.kind == XmlKind::kEnd && excel) {
      if (node.depth == value) value = kUnset;
      if (node.depth == inline_value) inline_value = kUnset;
      if (node.depth == formula_depth) formula_depth = kUnset;
      if (node.depth == cell) {
        std::string text;
        auto decoded = Value(type, raw, inline_text, formula, formula_present,
                             shared, styles, style, date1904, &text);
        if (!decoded.ok()) return decoded;
        if (text.size() > context.limits.max_cell_bytes ||
            text.size() > context.limits.max_text_bytes - row_bytes)
          return OfficeCapacity();
        row_bytes += text.size();
        row.resize(column);
        row[column - 1] = std::move(text);
        cell = kUnset;
      }
      if (node.depth == row_depth) {
        auto stored = context.Rows(sheet, row);
        if (!stored.ok()) return stored;
        row_depth = kUnset;
      }
      if (node.depth == data) data = kUnset;
    }
    return {};
  });
  if (!reply.ok()) return reply;
  context.document.sheets.push_back(std::move(sheet));
  return {};
}
}  // namespace

ObjectReply Workbook(OfficeContext& context, std::string_view main) {
  Relationships relations;
  auto reply = context.Relations(main, &relations);
  if (!reply.ok()) return reply;
  std::vector<SheetRef> sheets;
  std::unordered_set<std::string> names;
  bool date1904 = false;
  reply = context.Xml(main, [&](const XmlNode& node) -> ObjectReply {
    if (node.kind != XmlKind::kStart) return {};
    const bool excel = OfficeSpace(node.space, "spreadsheetml");
    if (!node.depth && (!excel || node.name != "workbook"))
      return OfficeInvalid();
    if (!excel) return {};
    if (node.name == "workbookPr") {
      const auto value = node.Attribute("date1904");
      if (!value.empty() && value != "0" && value != "1" && value != "true" &&
          value != "false")
        return OfficeInvalid();
      date1904 = value == "1" || value == "true";
    } else if (node.name == "sheet") {
      const auto name = node.Attribute("name"), id = RelationId(node);
      if (name.empty() || name.size() > 1024 || id.empty() ||
          !names.emplace(name).second)
        return OfficeInvalid();
      if (sheets.size() >= context.limits.max_sheets) return OfficeCapacity();
      sheets.push_back({std::string(name), std::string(id)});
    }
    return {};
  });
  if (!reply.ok()) return reply;
  std::vector<std::string> shared;
  Styles styles;
  bool have_shared = false, have_styles = false;
  for (const auto& [id, relation] : relations) {
    const bool shared_part = RelationType(relation.type, "sharedStrings");
    const bool style_part = RelationType(relation.type, "styles");
    if (!shared_part && !style_part) continue;
    std::string part;
    reply = context.Target(main, relation, &part);
    if (!reply.ok()) return reply;
    if (shared_part) {
      if (have_shared) return OfficeInvalid();
      have_shared = true;
      reply = SharedStrings(context, part, &shared);
    } else {
      if (have_styles) return OfficeInvalid();
      have_styles = true;
      reply = ReadStyles(context, part, &styles);
    }
    if (!reply.ok()) return reply;
  }
  std::unordered_set<std::string> ids;
  for (const auto& sheet : sheets) {
    const auto found = relations.find(sheet.id);
    if (found == relations.end() || !ids.insert(sheet.id).second)
      return OfficeInvalid();
    const bool chart = RelationType(found->second.type, "chartsheet");
    if (!chart && !RelationType(found->second.type, "worksheet"))
      return OfficeInvalid();
    std::string part;
    reply = context.Target(main, found->second, &part);
    if (!reply.ok()) return reply;
    if (chart) {
      // The text file-input contract does not render embedded chart images.
      // Keep the real sheet identity, with no invented tabular values.
      reply = context.Xml(part, [&](const XmlNode& node) -> ObjectReply {
        if (node.kind == XmlKind::kStart && !node.depth &&
            (!OfficeSpace(node.space, "spreadsheetml") ||
             node.name != "chartsheet"))
          return OfficeInvalid();
        return {};
      });
      if (reply.ok()) {
        FileDocumentSheet empty;
        empty.name = sheet.name;
        context.document.sheets.push_back(std::move(empty));
      }
    } else
      reply = Worksheet(context, part, sheet.name, shared, styles, date1904);
    if (!reply.ok()) return reply;
  }
  return {};
}
}  // namespace q4t::server::file_detail
