#include "file_office_internal.h"

#include <limits>
#include <unordered_set>

namespace q4t::server::file_detail {
namespace {
constexpr std::string_view kOffice =
    "urn:oasis:names:tc:opendocument:xmlns:office:1.0";
constexpr std::string_view kText =
    "urn:oasis:names:tc:opendocument:xmlns:text:1.0";
constexpr std::string_view kTable =
    "urn:oasis:names:tc:opendocument:xmlns:table:1.0";
constexpr std::string_view kDraw =
    "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0";
constexpr size_t kUnset = std::numeric_limits<size_t>::max();
bool Repeat(const XmlNode& node, std::string_view name, uint64_t* number) {
  const auto value = node.Attribute(name, kTable);
  *number = 1;
  return value.empty() ||
         (Unsigned(value, number) && *number && *number <= 1000000000);
}
ObjectReply Cell(const XmlNode& node, std::string_view displayed,
                 std::string* output) {
  const auto type = node.Attribute("value-type", kOffice);
  const auto formula = node.Attribute("formula", kTable);
  std::string_view value;
  if (type == "float" || type == "percentage" || type == "currency")
    value = node.Attribute("value", kOffice);
  else if (type == "date")
    value = node.Attribute("date-value", kOffice);
  else if (type == "time")
    value = node.Attribute("time-value", kOffice);
  else if (type == "boolean") {
    value = node.Attribute("boolean-value", kOffice);
    if (value != "true" && value != "false" && value != "0" && value != "1")
      return OfficeInvalid();
  } else if (type == "string")
    value = node.Attribute("string-value", kOffice);
  else if (!type.empty() && type != "void")
    return OfficeInvalid();
  if (value.empty())
    *output = displayed;
  else {
    *output = value;
    if (!displayed.empty() && displayed != value)
      *output += " [display: " + std::string(displayed) + "]";
  }
  if (type == "currency") {
    const auto currency = node.Attribute("currency", kOffice);
    if (!currency.empty())
      *output += " [currency: " + std::string(currency) + "]";
  }
  if (!formula.empty()) {
    if (output->empty())
      *output = "[formula: " + std::string(formula) + "]";
    else
      *output +=
          " [cached formula result; formula: " + std::string(formula) + "]";
  }
  return {};
}
}  // namespace

ObjectReply OpenDocument(OfficeContext& context) {
  size_t body = kUnset, content = kUnset, table = kUnset, row_depth = kUnset,
         cell_depth = kUnset, skipped = kUnset;
  std::string kind, cell_text;
  std::vector<size_t> paragraphs;
  XmlNode cell_node;
  bool covered = false, seen_body = false;
  uint64_t row_repeat = 1, cell_repeat = 1, column = 0, row_bytes = 0;
  uint64_t pending_blank_rows = 0;
  FileDocumentSheet sheet;
  std::vector<std::string> row;
  std::unordered_set<std::string> names;
  auto text = [&](std::string_view value) -> ObjectReply {
    if (kind != "spreadsheet") return context.Text(value);
    if (cell_depth == kUnset || covered) return {};
    if (value.size() > context.limits.max_cell_bytes - cell_text.size())
      return OfficeCapacity();
    cell_text += value;
    return {};
  };
  auto reply =
      context.Xml("content.xml", [&](const XmlNode& node) -> ObjectReply {
        if (skipped != kUnset) {
          if (node.kind == XmlKind::kEnd && node.depth == skipped)
            skipped = kUnset;
          return {};
        }
        if (node.kind == XmlKind::kStart && !node.depth &&
            (node.space != kOffice || node.name != "document-content"))
          return OfficeInvalid();
        if (node.space == kOffice && node.name == "body") {
          if (node.kind == XmlKind::kStart) {
            if (body != kUnset || seen_body) return OfficeInvalid();
            body = node.depth;
            seen_body = true;
          } else if (node.kind == XmlKind::kEnd)
            body = kUnset;
          return {};
        }
        if (body == kUnset) return {};
        if (node.kind == XmlKind::kStart && node.depth == body + 1) {
          if (content != kUnset || node.space != kOffice || !kind.empty() ||
              (node.name != "text" && node.name != "presentation" &&
               node.name != "spreadsheet" && node.name != "drawing"))
            return OfficeInvalid();
          content = node.depth;
          kind = node.name;
        }
        if (content == kUnset) return {};
        if (node.kind == XmlKind::kStart) {
          if (node.space == kText && node.name == "tracked-changes") {
            skipped = node.depth;
            return {};
          }
          if (kind == "spreadsheet" && node.space == kTable) {
            if (node.name == "table") {
              if (table != kUnset) return OfficeInvalid();
              const auto name = node.Attribute("name", kTable);
              if (name.empty() || name.size() > 1024 ||
                  !names.emplace(name).second)
                return OfficeInvalid();
              if (names.size() > context.limits.max_sheets)
                return OfficeCapacity();
              table = node.depth;
              sheet = {};
              sheet.name = name;
              pending_blank_rows = 0;
            } else if (table != kUnset && node.name == "table-row") {
              if (row_depth != kUnset ||
                  !Repeat(node, "number-rows-repeated", &row_repeat))
                return OfficeInvalid();
              row_depth = node.depth;
              column = row_bytes = 0;
              row.clear();
            } else if (row_depth != kUnset &&
                       (node.name == "table-cell" ||
                        node.name == "covered-table-cell")) {
              if (cell_depth != kUnset || node.depth != row_depth + 1 ||
                  !Repeat(node, "number-columns-repeated", &cell_repeat) ||
                  cell_repeat > 1000000000 - column)
                return OfficeInvalid();
              cell_depth = node.depth;
              cell_node = node;
              covered = node.name == "covered-table-cell";
              cell_text.clear();
            }
          }
          if (node.space == kText) {
            if (node.name == "p" || node.name == "h") {
              paragraphs.push_back(node.depth);
              if (kind == "spreadsheet" && !cell_text.empty())
                return text("\n");
            } else if (!paragraphs.empty() && node.name == "s") {
              uint64_t spaces = 1;
              const auto count = node.Attribute("c", kText);
              if (!count.empty() && (!Unsigned(count, &spaces) || !spaces))
                return OfficeInvalid();
              const auto limit = kind == "spreadsheet"
                                     ? context.limits.max_cell_bytes
                                     : context.limits.max_text_bytes;
              if (spaces > limit) return OfficeCapacity();
              return text(std::string(spaces, ' '));
            } else if (!paragraphs.empty() && node.name == "tab")
              return text("\t");
            else if (!paragraphs.empty() && node.name == "line-break")
              return text("\n");
          }
          if ((kind == "presentation" || kind == "drawing") &&
              node.space == kDraw && node.name == "page")
            return context.Text(
                std::string(kind == "drawing" ? "\n[Page " : "\n[Slide ") +
                std::string(node.Attribute("name", kDraw)) + "]\n");
        } else if (node.kind == XmlKind::kText) {
          if (!paragraphs.empty()) return text(node.value);
        } else if (node.kind == XmlKind::kEnd) {
          if (!paragraphs.empty() && node.depth == paragraphs.back()) {
            paragraphs.pop_back();
            if (kind != "spreadsheet") return text("\n");
          }
          if (kind == "spreadsheet" && node.space == kTable) {
            if (node.depth == cell_depth) {
              std::string value;
              if (!covered) {
                auto decoded = Cell(cell_node, cell_text, &value);
                if (!decoded.ok()) return decoded;
              }
              if (value.size() > context.limits.max_cell_bytes)
                return OfficeCapacity();
              // Repeated trailing blank grid/style cells do not allocate a huge
              // row. Any later nonempty cell still pays for its exact column
              // gap.
              if (!value.empty()) {
                if (column + cell_repeat > context.limits.max_columns ||
                    value.size() > (context.limits.max_text_bytes - row_bytes) /
                                       cell_repeat)
                  return OfficeCapacity();
                row.resize(column);
                for (uint64_t i = 0; i < cell_repeat; ++i) row.push_back(value);
                row_bytes += cell_repeat * value.size();
              }
              column += cell_repeat;
              cell_depth = kUnset;
            }
            if (node.depth == row_depth) {
              if (row_repeat >
                  1000000000 - sheet.source_rows - pending_blank_rows)
                return OfficeCapacity();
              // ODF exporters may append a million style-only grid rows.
              // Retain repeated blank gaps only if later data uses them, so
              // real row positions remain exact without inventing a data tail.
              if (row.empty() && (row_repeat > 1 || pending_blank_rows)) {
                pending_blank_rows += row_repeat;
              } else {
                if (pending_blank_rows) {
                  auto gap = context.Rows(sheet, {}, pending_blank_rows);
                  if (!gap.ok()) return gap;
                  pending_blank_rows = 0;
                }
                auto stored = context.Rows(sheet, row, row_repeat);
                if (!stored.ok()) return stored;
              }
              row_depth = kUnset;
            }
            if (node.depth == table) {
              context.document.sheets.push_back(std::move(sheet));
              table = kUnset;
            }
          } else if (node.space == kTable && node.name == "table-cell")
            return text("\t");
          else if (node.space == kTable && node.name == "table-row")
            return text("\n");
          if (node.depth == content) content = kUnset;
        }
        return {};
      });
  if (!reply.ok()) return reply;
  if (!seen_body || kind.empty()) return OfficeInvalid();
  return {};
}
}  // namespace q4t::server::file_detail
