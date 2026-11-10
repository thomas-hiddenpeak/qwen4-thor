#include "../src/server/file_package.h"

#include <unistd.h>

#include <chrono>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <map>

#include "../src/server/object_protocol.h"
#include "q4t/server/file_document_worker.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::file_detail;
using namespace q4t::server::object_detail;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
using Inputs = std::map<std::string, std::string>;
BinaryAccess Access() { return {"package-fixture", Clock::now() + 2s, {}}; }

bool Check(std::string_view mode, const Inputs& inputs) {
  const auto input = [&](const char* name) -> const std::string& {
    return inputs.at(name);
  };
  if (mode == "zip") {
    for (const char* name : {"valid.zip", "zip64.zip"}) {
      Package result;
      const auto reply = ReadPackage(input(name), {}, Access(), &result);
      if (!reply.ok())
        std::printf("container %s: %s\n", name, reply.code.c_str());
      Q4T_CHECK(reply.ok() && result.size() == 2 &&
                result[0].name == "word/document.xml" &&
                result[0].bytes == "<doc>真实 😀 &amp; text</doc>" &&
                result[1].bytes == "<Relationships/>");
    }
    return true;
  }
  if (mode == "names") {
    for (const char* name :
         {"duplicate.zip", "escape.zip", "symlink.zip", "encrypted.zip"}) {
      Package result = {{"stale", "data"}};
      Q4T_CHECK(ReadPackage(input(name), {}, Access(), &result).http_status ==
                    400 &&
                result.empty());
    }
    return true;
  }
  if (mode == "corruption") {
    for (const char* name : {"crc.zip", "truncated.zip"}) {
      Package result;
      Q4T_CHECK(ReadPackage(input(name), {}, Access(), &result).http_status ==
                    400 &&
                result.empty());
    }
    return true;
  }
  if (mode == "capacity") {
    Package result;
    PackageLimits limits;
    limits.max_entry_bytes = 1024;
    Q4T_CHECK(ReadPackage(input("expanded.zip"), limits, Access(), &result)
                      .http_status == 413 &&
              result.empty());
    limits = {};
    limits.max_bytes = 33;
    Q4T_CHECK(ReadPackage(input("valid.zip"), limits, Access(), &result)
                      .http_status == 413 &&
              result.empty());
    limits = {};
    limits.max_entries = 1;
    Q4T_CHECK(ReadPackage(input("valid.zip"), limits, Access(), &result)
                      .http_status == 413 &&
              result.empty());
    auto access = Access();
    int checks = 0;
    access.cancelled = [&] { return ++checks == 3; };
    Q4T_CHECK(
        ReadPackage(input("valid.zip"), {}, access, &result).http_status ==
            499 &&
        result.empty());
    access = Access();
    access.deadline = Clock::now();
    Q4T_CHECK(
        ReadPackage(input("valid.zip"), {}, access, &result).http_status ==
        408);
    return true;
  }
  if (mode == "xml") {
    std::vector<XmlNode> nodes;
    const auto visit = [&](const XmlNode& node) {
      nodes.push_back(node);
      return ObjectReply{};
    };
    Q4T_CHECK(
        VisitXml("<a:root xmlns:a='urn:test' a:id='one' plain='two'>"
                 "真实 &amp; 😀<a:empty/><a:child>  text\n</a:child></a:root>",
                 {}, Access(), visit)
            .ok());
    Q4T_CHECK(
        nodes.size() == 8 && nodes[0].space == "urn:test" &&
        nodes[0].Attribute("id", "urn:test") == "one" &&
        nodes[0].Attribute("plain") == "two" && nodes[1].value == "真实 & 😀" &&
        nodes[2].kind == XmlKind::kStart && nodes[3].kind == XmlKind::kEnd &&
        nodes[5].value == "  text\n" && nodes[7].depth == 0);
    nodes.clear();
    std::string utf16("\xff\xfe", 2);
    for (char c : std::string("<r>UTF16</r>")) {
      utf16 += c;
      utf16 += '\0';
    }
    Q4T_CHECK(VisitXml(utf16, {}, Access(), visit).ok() && nodes.size() == 3 &&
              nodes[1].value == "UTF16");
    return true;
  }
  if (mode == "entities") {
    for (const char* xml :
         {"<!DOCTYPE r SYSTEM 'file:///etc/hostname'><r/>",
          "<!DOCTYPE r [<!ENTITY x SYSTEM 'http://127.0.0.1/x'>]><r>&x;</r>",
          "<!DOCTYPE r [<!ENTITY x 'expanded'>]><r>&x;</r>",
          "<r>&undefined;</r>"}) {
      std::string text;
      const auto reply = VisitXml(xml, {}, Access(), [&](const XmlNode& node) {
        text += node.value;
        return ObjectReply{};
      });
      Q4T_CHECK(reply.http_status == 400 && text.empty());
    }
    return true;
  }
  if (mode == "xml_limits") {
    const auto ignore = [](const XmlNode&) { return ObjectReply{}; };
    for (const char* bad : {"<r><p></r>", "<r/>tail", "<r/><another/>"})
      Q4T_CHECK(VisitXml(bad, {}, Access(), ignore).http_status == 400);
    PackageLimits limits;
    limits.max_xml_depth = 1;
    Q4T_CHECK(VisitXml("<r><c><too_deep/></c></r>", limits, Access(), ignore)
                  .http_status == 413);
    limits = {};
    limits.max_xml_nodes = 2;
    Q4T_CHECK(VisitXml("<r>value</r>", limits, Access(), ignore).http_status ==
              413);
    limits = {};
    limits.max_bytes = 3;
    Q4T_CHECK(
        VisitXml("<r id='four'/>", limits, Access(), ignore).http_status ==
        413);
    return true;
  }
  if (mode == "xml_cancel") {
    size_t seen = 0;
    const auto stopped =
        VisitXml("<r><one/><two/></r>", {}, Access(), [&](const XmlNode&) {
          ++seen;
          return Error(409, "fixture_stop", "stop");
        });
    Q4T_CHECK(stopped.code == "fixture_stop" && seen == 1);
    auto access = Access();
    int checks = 0;
    access.cancelled = [&] { return ++checks == 3; };
    Q4T_CHECK(VisitXml("<r><one/><two/></r>", {}, access, [](const XmlNode&) {
                return ObjectReply{};
              }).http_status == 499);
    return true;
  }
  return false;
}

bool Run(const char* mode) {
  GrammarControl control;
  control.domain = IsolatedWorkerDomain::kFileParse;
  control.counters = std::make_shared<GrammarWorkerCounters>();
  std::unique_ptr<detail::GrammarProcess> worker;
  const std::string entry = std::string("--package-fixture-") + mode;
  Q4T_CHECK(detail::GrammarProcess::Start(entry.c_str(), control, &worker));
  std::string output;
  Q4T_CHECK(worker->CollectOutput(&output, 65536, Clock::now() + 5s));
  if (!output.ends_with("PACKAGE_OK\n")) std::fputs(output.c_str(), stderr);
  Q4T_CHECK(output.ends_with("PACKAGE_OK\n") && control.counters->active == 0 &&
            control.counters->started == 1 && control.counters->reaped == 1);
  return true;
}
}  // namespace

int PackageFixture(int argc, char** argv) {
  const std::string_view prefix = "--package-fixture-";
  if (argc < 2 || !std::string_view(argv[1]).starts_with(prefix)) return -1;
  if (!detail::EnterGrammarWorker(argc, argv)) return 2;
  Inputs inputs;
  const auto root =
      std::filesystem::path(__FILE__).parent_path() / "fixtures/file_package";
  for (const auto& path : std::filesystem::directory_iterator(root)) {
    if (path.path().extension() != ".zip") continue;
    std::ifstream file(path.path(), std::ios::binary);
    inputs[path.path().filename().string()] =
        std::string(std::istreambuf_iterator<char>(file), {});
  }
  if (!LoadPackageRuntime().ok() || dup2(3, 1) != 1 || dup2(3, 2) != 2 ||
      !detail::EnterNativeDocumentSandbox())
    return 2;
  const bool ok =
      Check(std::string_view(argv[1]).substr(prefix.size()), inputs);
  std::puts(ok ? "PACKAGE_OK" : "PACKAGE_FAILED");
  return 0;
}

Q4T_TEST(file_package_isolated_deflate_zip64_and_original_bytes) {
  return Run("zip");
}
Q4T_TEST(file_package_rejects_alias_names_links_and_encryption) {
  return Run("names");
}
Q4T_TEST(file_package_requires_crc_and_complete_central_directory) {
  return Run("corruption");
}
Q4T_TEST(file_package_bounds_expansion_entry_count_and_cancellation) {
  return Run("capacity");
}
Q4T_TEST(file_package_xml_preserves_expanded_names_unicode_and_whitespace) {
  return Run("xml");
}
Q4T_TEST(file_package_xml_rejects_dtd_and_external_internal_entities) {
  return Run("entities");
}
Q4T_TEST(file_package_xml_rejects_damage_and_bounded_structure) {
  return Run("xml_limits");
}
Q4T_TEST(file_package_xml_stops_on_callback_error_and_cancellation) {
  return Run("xml_cancel");
}
