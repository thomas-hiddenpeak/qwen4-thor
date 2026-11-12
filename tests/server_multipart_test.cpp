#include "q4t/server/multipart.h"

#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <stdexcept>
#include <vector>

#include "q4t/io/json.h"
#include "q4t/test.h"

namespace {
using q4t::Status;
using namespace q4t::server;
struct Capture {
  std::vector<MultipartPart> parts;
  std::vector<std::string> bodies;
  size_t ended = 0;
  MultipartCallbacks Callbacks() {
    return {[&](const MultipartPart& part) {
              parts.push_back(part);
              bodies.emplace_back();
              return Status{};
            },
            [&](std::string_view bytes) {
              bodies.back().append(bytes);
              return Status{};
            },
            [&] {
              ++ended;
              return Status{};
            }};
  }
};
std::string Body(std::string_view bytes, std::string_view boundary = "AaB03x") {
  return "--" + std::string(boundary) +
         "\r\nContent-Disposition: form-data; name=\"file\"; "
         "filename=\"资料.txt\"\r\nContent-Type: "
         "application/octet-stream\r\n\r\n" +
         std::string(bytes) + "\r\n--" + std::string(boundary) + "--\r\n";
}
bool Decode(std::string_view wire, Capture* capture, size_t chunk = 1,
            std::string boundary = "AaB03x") {
  MultipartDecoder parser(std::move(boundary), wire.size(), 32,
                          capture->Callbacks());
  for (size_t offset = 0; offset < wire.size(); offset += chunk) {
    if (!parser.Feed(wire.substr(offset, chunk))) return false;
    if (parser.BufferedBytes() > 32768) return false;
  }
  return parser.Finish().ok();
}
std::string Read(const std::filesystem::path& path) {
  std::ifstream stream(path, std::ios::binary);
  if (!stream) throw std::runtime_error("missing SDK multipart fixture");
  return {std::istreambuf_iterator<char>(stream), {}};
}
}  // namespace

Q4T_TEST(multipart_boundary_quotes_case_and_ambiguity) {
  std::string boundary;
  for (const auto* content_type :
       {"multipart/form-data; boundary=AaB03x",
        "Multipart/Form-Data; boundary=\"AaB03x\"",
        "multipart/form-data; charset=UTF-8; boundary=\"AaB03x\""}) {
    Q4T_CHECK(MultipartBoundary(content_type, &boundary) &&
              boundary == "AaB03x");
  }
  Q4T_CHECK(
      MultipartBoundary("multipart/form-data; boundary=\"a:b ?\"", &boundary) &&
      boundary == "a:b ?");
  for (const auto& value : std::vector<std::string>{
           "application/json", "multipart/form-data",
           "multipart/form-data; boundary=",
           "multipart/form-data; boundary=a; Boundary=b",
           "multipart/form-data; boundary=\"a \"",
           "multipart/form-data; boundary=\"a\r\nb\"",
           "multipart/form-data; boundary=\"unterminated",
           "multipart/form-data; boundary=a:b",
           "multipart/form-data; boundary=" + std::string(71, 'a')})
    Q4T_CHECK(!MultipartBoundary(value, &boundary) && boundary.empty());
  return true;
}

Q4T_TEST(multipart_binary_content_all_fragment_sizes_and_utf8_filename) {
  std::string binary;
  for (int i = 0; i < 256; ++i) binary += static_cast<char>(i);
  binary += "\r\n\r\ncontent\r\n";
  const auto wire = Body(binary);
  for (size_t chunk = 1; chunk <= wire.size(); ++chunk) {
    Capture capture;
    Q4T_CHECK(Decode(wire, &capture, chunk) && capture.ended == 1 &&
              capture.bodies == std::vector<std::string>{binary});
    Q4T_CHECK(capture.parts[0].name == "file" &&
              capture.parts[0].filename == "资料.txt" &&
              capture.parts[0].has_filename &&
              capture.parts[0].content_type == "application/octet-stream");
  }
  return true;
}

Q4T_TEST(multipart_false_delimiter_prefixes_remain_exact_payload) {
  const std::string content =
      "--AaB03x\r\nbody\r\n--AaB03x-extra\r\n--AaB03x--extra"
      "\r\n--AaB03x \tX\r\n--AaB03\r\n--AaB03x-";
  Capture capture;
  Q4T_CHECK(Decode(Body(content), &capture) && capture.bodies[0] == content);
  return true;
}

Q4T_TEST(multipart_order_repeated_fields_empty_parts_and_mime_padding) {
  const std::string wire =
      "preamble\r\n--b \t\r\nContent-Disposition: form-data;\r\n"
      " name=\"tag\"\r\n\r\nfirst\r\n--b\t\r\n"
      "Content-Disposition: form-data; name=tag\r\nX-Ignored: yes\r\n\r\n"
      "\r\n--b-- \t\r\nepilogue";
  Capture capture;
  Q4T_CHECK(Decode(wire, &capture, 1, "b") && capture.ended == 2 &&
            capture.parts[0].name == "tag" && capture.parts[1].name == "tag" &&
            !capture.parts[0].has_filename &&
            capture.parts[0].content_type == "text/plain");
  Q4T_CHECK((capture.bodies == std::vector<std::string>{"first", ""}));
  return true;
}

Q4T_TEST(multipart_closing_delimiter_without_final_crlf) {
  auto wire = Body("data");
  wire.resize(wire.size() - 2);
  Capture capture;
  Q4T_CHECK(Decode(wire, &capture) && capture.ended == 1 &&
            capture.bodies[0] == "data");
  return true;
}

Q4T_TEST(multipart_truncation_never_authorizes_publication) {
  const auto wire = Body("file bytes");
  // The final CRLF is optional. All prefixes before the complete '--' close
  // are truncated even if the sink already received staged payload bytes.
  for (size_t end = 1; end < wire.size() - 2; ++end) {
    Capture capture;
    MultipartDecoder parser("AaB03x", wire.size(), 32, capture.Callbacks());
    const auto fed = parser.Feed(std::string_view(wire).substr(0, end));
    Q4T_CHECK(!fed || !parser.Finish());
  }
  return true;
}

Q4T_TEST(multipart_rejects_ambiguous_or_malformed_part_headers) {
  for (const auto* headers :
       {"Content-Disposition: form-data; name=a; name=b",
        "Content-Disposition: form-data; name=a\r\nContent-Disposition: "
        "form-data; name=a",
        "Content-Disposition: attachment; name=a",
        "Content-Disposition: form-data; filename=f",
        "Content-Disposition: form-data; name=a; filename*=UTF-8''x",
        "Content-Disposition : form-data; name=a",
        "Content-Disposition: form-data; name=a\nX-Injected: b",
        "Content-Disposition: form-data; name=a\r\nContent-Transfer-Encoding: "
        "base64"}) {
    Capture capture;
    const auto wire =
        "--AaB03x\r\n" + std::string(headers) + "\r\n\r\nx\r\n--AaB03x--";
    Q4T_CHECK(!Decode(wire, &capture));
  }
  return true;
}

Q4T_TEST(multipart_wire_parts_headers_and_ignored_text_are_bounded) {
  const auto wire = Body("x");
  Capture capture;
  MultipartDecoder small("AaB03x", wire.size() - 1, 1, capture.Callbacks());
  Q4T_CHECK(!small.Feed(wire) && capture.parts.empty());
  const std::string part =
      "--AaB03x\r\nContent-Disposition: form-data; name=a\r\n\r\nx\r\n";
  const auto two = part + part + "--AaB03x--";
  MultipartDecoder limited("AaB03x", two.size(), 1, capture.Callbacks());
  Q4T_CHECK(!limited.Feed(two) && capture.parts.size() == 1 &&
            capture.ended == 1);
  for (const auto& body :
       {std::string(4097, 'x') + "\r\n" + wire, wire + std::string(4097, 'x'),
        std::string("--AaB03x\r\nX: ") + std::string(8193, 'x') +
            "\r\n\r\nx"}) {
    Capture invalid;
    Q4T_CHECK(!Decode(body, &invalid, 8192));
  }
  return true;
}

Q4T_TEST(multipart_consumer_failure_and_exception_are_sticky) {
  for (int failure = 0; failure < 3; ++failure) {
    size_t callbacks = 0;
    MultipartCallbacks consumer{
        [&](const MultipartPart&) {
          ++callbacks;
          return failure == 0 ? Status::Fail("no storage") : Status{};
        },
        [&](std::string_view) {
          ++callbacks;
          if (failure == 1) throw std::runtime_error("storage writer failed");
          return Status{};
        },
        [&] {
          ++callbacks;
          return Status::Fail("unconfirmed staging receipt");
        }};
    const auto wire = Body("payload");
    MultipartDecoder parser("AaB03x", wire.size() * 2, 2, std::move(consumer));
    Q4T_CHECK(!parser.Feed(wire));
    const auto stopped = callbacks;
    Q4T_CHECK(!parser.Feed(wire) && !parser.Finish() && callbacks == stopped);
  }
  return true;
}

Q4T_TEST(multipart_large_input_is_incremental_with_fixed_parser_buffer) {
  size_t bytes = 0, largest = 0;
  MultipartCallbacks sink{[](const MultipartPart&) { return Status{}; },
                          [&](std::string_view chunk) {
                            bytes += chunk.size();
                            largest = std::max(largest, chunk.size());
                            return Status{};
                          },
                          [] { return Status{}; }};
  const std::string payload(2 * 1024 * 1024, 'x');
  const auto wire = Body(payload);
  MultipartDecoder parser("AaB03x", wire.size(), 1, std::move(sink));
  Q4T_CHECK(parser.Feed(wire) && parser.Finish() && bytes == payload.size() &&
            largest <= 2 * 8192 && parser.BufferedBytes() == 0);
  Q4T_CHECK(!parser.Feed("extra") && !parser.Finish());
  return true;
}

Q4T_TEST(multipart_actual_official_sdk_file_and_upload_part_bytes) {
  const auto* override = std::getenv("Q4T_MULTIPART_FIXTURES");
  const auto directory = override
      ? std::filesystem::path(override)
      : std::filesystem::path(__FILE__).parent_path().parent_path() /
            "docs/evidence/serve-multipart-cpu-20261110/"
            "multipart-cpu-02/sdk";
  for (const auto* name : {"file", "part"}) {
    q4t::io::Json fixture;
    Q4T_CHECK(q4t::io::ParseJson(
        Read(std::filesystem::path(directory) / (std::string(name) + ".json")),
        &fixture, true));
    std::string boundary;
    Q4T_CHECK(MultipartBoundary(fixture.GetString("content_type"), &boundary));
    Capture capture;
    const auto wire =
        Read(std::filesystem::path(directory) / (std::string(name) + ".body"));
    Q4T_CHECK(Decode(wire, &capture, 3, boundary));
    const auto* fields = fixture.GetArray("fields");
    Q4T_CHECK(fields && fields->array.size() == capture.parts.size());
    for (size_t i = 0; i < fields->array.size(); ++i) {
      const auto& expected = fields->array[i];
      Q4T_CHECK(expected.GetString("name") == capture.parts[i].name);
      if (expected.Has("filename")) {
        Q4T_CHECK(capture.parts[i].has_filename &&
                  expected.GetString("filename") == capture.parts[i].filename);
        Q4T_CHECK(capture.bodies[i] == Read(std::filesystem::path(directory) /
                                            expected.GetString("bytes_file")));
      } else {
        Q4T_CHECK(!capture.parts[i].has_filename &&
                  capture.bodies[i] == expected.GetString("value"));
      }
    }
    Q4T_CHECK(capture.ended == fields->array.size());
  }
  return true;
}
