#include "q4t/server/file_document_worker.h"

#include <cstdio>

#include "../src/server/object_protocol.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
ObjectReply Read(std::string bytes, FileDocument* output,
                 FileDocumentLimits limits = {}) {
  FileInputContent input;
  input.filename = "original.rtf";
  input.bytes = std::move(bytes);
  GrammarControl control;
  control.counters = std::make_shared<GrammarWorkerCounters>();
  auto reply = ParseNativeFileDocument(
      input, limits, {"rtf-tests", Clock::now() + 3s, {}}, output, control);
  if (control.counters->started != 1 || control.counters->reaped != 1 ||
      control.counters->active != 0)
    return object_detail::Error(500, "fixture_ownership_failed", "rtf");
  if (reply.ok() &&
      (output->source_sha256 != object_detail::Digest(input.bytes) ||
       output->encoding != "utf-8" || !output->sheets.empty()))
    return object_detail::Error(500, "fixture_identity_failed", "rtf");
  if (!reply.ok())
    std::printf("rtf: %d %s\n", reply.http_status, reply.code.c_str());
  return reply;
}
}  // namespace

Q4T_TEST(file_rtf_unicode_scoped_fallback_surrogates_and_alternate_text) {
  FileDocument output;
  Q4T_CHECK(
      Read(
          R"({\rtf1\ansi\uc1 \u20013?\u25991? \u-10179?\u-8704? {\uc0\u945} {\uc2\u946??} {\upr{ANSI WRONG}{\*\ud Unicode \u947?}}})",
          &output)
          .ok() &&
      output.text == "中文 😀 α β Unicode γ");
  return true;
}
Q4T_TEST(file_rtf_font_codepage_and_multibyte_hex_are_not_replaced) {
  FileDocument output;
  Q4T_CHECK(
      Read(
          R"({\rtf1\ansi\ansicpg1252\deff0{\fonttbl{\f0\fcharset0 Latin;}{\f1\fcharset134 Chinese;}{\f2\fcharset204 Russian;}}\f0 caf\'e9 \f1\'d6\'d0\'ce\'c4 \f2\'cf\'f0\'e8\'e2\'e5\'f2})",
          &output)
          .ok() &&
      output.text == "café 中文 Привет");
  Q4T_CHECK(
      Read(
          R"({\rtf1\ansi{\fonttbl{\f0\fcharset0\cpg1251 Override;}}\f0\'cf\'f0})",
          &output)
          .ok() &&
      output.text == "Пр");
  return true;
}
Q4T_TEST(file_rtf_ignored_objects_fields_and_deleted_text_cannot_leak) {
  FileDocument output;
  Q4T_CHECK(
      Read(
          R"({\rtf1\ansi{\info{\title SECRET}}Visible {\object SECRET{\pict 001122}}{\*\unknown SECRET}{\deleted OLD}{\field{\*\fldinst INCLUDETEXT "file:///etc/hostname"}{\fldrslt cached result}}\par end})",
          &output)
          .ok() &&
      output.text == "Visible cached result\nend");
  return true;
}
Q4T_TEST(file_rtf_symbols_table_controls_and_binary_fallback_count) {
  FileDocument output;
  Q4T_CHECK(
      Read(
          R"({\rtf1\ansi \{x\}\\\tab A\cell B\row \emdash\~\bullet\par\uc1\u945\bin3 {x}Z})",
          &output)
          .ok() &&
      output.text == "{x}\\\tA\tB\n— •\nαZ");
  Q4T_CHECK(Read(R"({\rtf1\ansi\uc2\u945\'3f\~X})", &output).ok() &&
            output.text == "αX");
  return true;
}
Q4T_TEST(file_rtf_scope_end_stops_fallback_and_font_metadata_stays_private) {
  FileDocument output;
  Q4T_CHECK(
      Read(
          R"({\rtf1\ansi{\fonttbl{\f0\fcharset0 Name;}{\*\unknown\ansicpg936 BAD;}}{\uc4\u945?}X\'e9})",
          &output)
          .ok() &&
      output.text == "αXé");
  Q4T_CHECK(
      Read(
          R"({\rtf1\ansi{\fonttbl{\f0\fcharset0 Latin;}{\f1\fcharset134 Chinese;}}\f0\af1\dbch\'d6\'d0\loch\'e9})",
          &output)
          .ok() &&
      output.text == "中é");
  return true;
}
Q4T_TEST(file_rtf_malformed_group_unicode_and_multibyte_publish_nothing) {
  for (const auto* input :
       {R"({\rtf1 unclosed)", R"({\rtf1 ok}tail)", R"({\rtf1\u-10179?x})",
        R"({\rtf1\u-8704?})", R"({\rtf1\bin100 short})", R"({\rtf1\'gg})",
        R"({\rtf1\ansicpg936\'d6})", R"({\rtf1\uc-1 bad})"}) {
    FileDocument output;
    output.text = "stale";
    Q4T_CHECK(Read(input, &output).http_status == 400 && output.text.empty() &&
              output.source_sha256.empty());
  }
  return true;
}
Q4T_TEST(file_rtf_capacity_and_missing_codepage_leave_next_document_healthy) {
  FileDocument output;
  FileDocumentLimits limits;
  limits.max_text_bytes = 2;
  Q4T_CHECK(Read(R"({\rtf1\u20013?})", &output, limits).http_status == 413 &&
            output.text.empty());
  Q4T_CHECK(
      Read("{\\rtf1" + std::string(129, '{') + "x" + std::string(130, '}'),
           &output)
              .http_status == 413 &&
      output.text.empty());
  Q4T_CHECK(Read(R"({\rtf1\ansicpg65535 x})", &output).http_status == 503 &&
            output.text.empty());
  Q4T_CHECK(Read(R"({\rtf1 healthy})", &output).ok() &&
            output.text == "healthy");
  return true;
}
