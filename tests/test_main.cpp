// Strict by default. --required-list selects exact names and forbids skips.
#include "q4t/test.h"

#include <fstream>
#include <string>

#ifdef Q4T_EMBEDDING_ASSET_WORKER_ENTRY
#include "q4t/server/embedding_assets.h"
#endif

#if defined(Q4T_GRAMMAR_PROCESS_TESTS) || defined(Q4T_CACHE_WORKER_ENTRY)
#include "q4t/server/cache_io.h"
#endif

#ifdef Q4T_ENABLE_XGRAMMAR
#include "q4t/server/structured_decoder.h"
#endif

#ifdef Q4T_GRAMMAR_PROCESS_TESTS
#include "q4t/server/object_store.h"
#include "q4t/server/tool_result_schema.h"
#include "q4t/server/file_fetch.h"
#include "q4t/server/webhook.h"
#include "q4t/server/sip_webhook.h"
#include "q4t/server/web_search.h"
#include "q4t/server/file_document_worker.h"
#include "q4t/server/skill_worker.h"
#include "q4t/server/program_worker.h"
#include "q4t/server/mcp_transport.h"
#include "q4t/server/mcp_tunnel_agent.h"
#include "q4t/server/file_pdf.h"
#include "q4t/server/file_webp.h"
#include "q4t/server/file_video.h"
#include "q4t/server/file_audio.h"
#include "q4t/server/speech_encode.h"
#include "q4t/server/speech_codec_stream.h"
#include "q4t/server/asr_statistics.h"
#include "q4t/server/file_legacy.h"
#include "q4t/server/state_key_admin.h"
int SipOwnerFixture(int argc, char** argv);
int CacheIoFixture(int argc, char** argv);
int GrammarProcessFixture(int argc, char** argv);
int ConversationApiFixture(int argc, char** argv);
int FilesApiFixture(int argc, char** argv);
int FileFetchFixture(int argc, char** argv);
int WebPageSandboxFixture(int argc, char** argv);
int McpTransportFixture(int argc, char** argv);
int FileSourceFixture(int argc, char** argv);
int NestedDocumentFixture(int argc, char** argv);
int DocumentSandboxFixture(int argc, char** argv);
int WebpWorkerFixture(int argc, char** argv);
int VideoWorkerFixture(int argc, char** argv);
int AudioWorkerFixture(int argc, char** argv);
int SpeechEncodeFixture(int argc, char** argv);
int SpeechCodecStreamFixture(int argc, char** argv);
int WorkerIoBudgetFixture(int argc, char** argv);
int LegacyDocumentFixture(int argc, char** argv);
int PdfSandboxFixture(int argc, char** argv);
int PackageFixture(int argc, char** argv);
int ResponsesApiFixture(int argc, char** argv);
int ResponsesJobsFixture(int argc, char** argv);
#endif

int main(int argc, char** argv) {
#ifdef Q4T_EMBEDDING_ASSET_WORKER_ENTRY
  const int embedding = q4t::server::MaybeRunEmbeddingAssetWorker(argc, argv);
  if (embedding >= 0) return embedding;
#endif
#if defined(Q4T_GRAMMAR_PROCESS_TESTS) || defined(Q4T_CACHE_WORKER_ENTRY)
  const int cache_worker = q4t::server::MaybeRunCacheWorker(argc, argv);
  if (cache_worker >= 0) return cache_worker;
#endif
#ifdef Q4T_ENABLE_XGRAMMAR
  const int worker = q4t::server::MaybeRunGrammarWorker(argc, argv);
  if (worker >= 0) return worker;
#endif
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
  const int cache_fixture = CacheIoFixture(argc, argv);
  if (cache_fixture >= 0) return cache_fixture;
  const int program = q4t::server::MaybeRunProgramWorker(argc, argv);
  if (program >= 0) return program;
  const int tool_results = q4t::server::MaybeRunToolResultWorker(argc, argv);
  if (tool_results >= 0) return tool_results;
  const int object_worker = q4t::server::MaybeRunObjectWorker(argc, argv);
  if (object_worker >= 0) return object_worker;
  const int mcp_worker = q4t::server::MaybeRunMcpWorker(argc, argv);
  if (mcp_worker >= 0) return mcp_worker;
  const int tunnel_agent =
      q4t::server::MaybeRunMcpTunnelAgentWorker(argc, argv);
  if (tunnel_agent >= 0) return tunnel_agent;
  if (argc > 1 && std::string_view(argv[1]) == "mcp-tunnel-agent")
    return q4t::server::RunMcpTunnelAgentCommand(argc, argv);
  const int web_sandbox = WebPageSandboxFixture(argc, argv);
  if (web_sandbox >= 0) return web_sandbox;
  const int web_page = q4t::server::MaybeRunWebPageWorker(argc, argv);
  if (web_page >= 0) return web_page;
  const int sip_owner = SipOwnerFixture(argc, argv);
  if (sip_owner >= 0) return sip_owner;
  const int sip_webhook = q4t::server::MaybeRunSipWebhookWorker(argc, argv);
  if (sip_webhook >= 0) return sip_webhook;
  const int webhook_worker = q4t::server::MaybeRunWebhookWorker(argc, argv);
  if (webhook_worker >= 0) return webhook_worker;
  const int fetch_worker = q4t::server::MaybeRunFileFetchWorker(argc, argv);
  if (fetch_worker >= 0) return fetch_worker;
  const int document_worker = q4t::server::MaybeRunFileDocumentWorker(argc, argv);
  if (document_worker >= 0) return document_worker;
  const int skill_worker = q4t::server::MaybeRunSkillWorker(argc, argv);
  if (skill_worker >= 0) return skill_worker;
  const int webp_fixture = WebpWorkerFixture(argc, argv);
  if (webp_fixture >= 0) return webp_fixture;
  const int webp_worker = q4t::server::MaybeRunFileWebpWorker(argc, argv);
  if (webp_worker >= 0) return webp_worker;
  const int video_fixture = VideoWorkerFixture(argc, argv);
  if (video_fixture >= 0) return video_fixture;
  const int video_worker = q4t::server::MaybeRunFileVideoWorker(argc, argv);
  if (video_worker >= 0) return video_worker;
  const int speech_fixture = SpeechEncodeFixture(argc, argv);
  if (speech_fixture >= 0) return speech_fixture;
  const int speech_stream =
      q4t::server::MaybeRunSpeechCodecStreamWorker(argc, argv);
  if (speech_stream >= 0) return speech_stream;
  const int stream_fixture = SpeechCodecStreamFixture(argc, argv);
  if (stream_fixture >= 0) return stream_fixture;
  const int speech_encoder = q4t::server::MaybeRunSpeechEncodeWorker(argc, argv);
  if (speech_encoder >= 0) return speech_encoder;
  const int audio_fixture = AudioWorkerFixture(argc, argv);
  if (audio_fixture >= 0) return audio_fixture;
  const int audio_worker = q4t::server::MaybeRunFileAudioWorker(argc, argv);
  if (audio_worker >= 0) return audio_worker;
  const int asr_statistics = q4t::server::MaybeRunAsrStatisticsWorker(argc, argv);
  if (asr_statistics >= 0) return asr_statistics;
  const int pdf_worker = q4t::server::MaybeRunFilePdfWorker(argc, argv);
  if (pdf_worker >= 0) return pdf_worker;
  const int legacy_worker = q4t::server::MaybeRunFileLegacyWorker(argc, argv);
  if (legacy_worker >= 0) return legacy_worker;
  const int state_keys = q4t::server::MaybeRunStateKeyCommand(argc, argv);
  if (state_keys >= 0) return state_keys;
  const int retention = q4t::server::MaybeRunStateRetentionCommand(argc, argv);
  if (retention >= 0) return retention;
  const int fixture = GrammarProcessFixture(argc, argv);
  if (fixture >= 0) return fixture;
  const int conversation = ConversationApiFixture(argc, argv);
  if (conversation >= 0) return conversation;
  const int files = FilesApiFixture(argc, argv);
  if (files >= 0) return files;
  const int file_fetch = FileFetchFixture(argc, argv);
  if (file_fetch >= 0) return file_fetch;
  const int mcp_fixture = McpTransportFixture(argc, argv);
  if (mcp_fixture >= 0) return mcp_fixture;
  const int file_source = FileSourceFixture(argc, argv);
  if (file_source >= 0) return file_source;
  const int nested_document = NestedDocumentFixture(argc, argv);
  if (nested_document >= 0) return nested_document;
  const int document_sandbox = DocumentSandboxFixture(argc, argv);
  if (document_sandbox >= 0) return document_sandbox;
  const int io_budget = WorkerIoBudgetFixture(argc, argv);
  if (io_budget >= 0) return io_budget;
  const int legacy_fixture = LegacyDocumentFixture(argc, argv);
  if (legacy_fixture >= 0) return legacy_fixture;
  const int pdf_sandbox = PdfSandboxFixture(argc, argv);
  if (pdf_sandbox >= 0) return pdf_sandbox;
  const int package_fixture = PackageFixture(argc, argv);
  if (package_fixture >= 0) return package_fixture;
  const int responses = ResponsesApiFixture(argc, argv);
  if (responses >= 0) return responses;
  const int jobs = ResponsesJobsFixture(argc, argv);
  if (jobs >= 0) return jobs;
#endif
  std::string filter, required;
  bool list = false, allow_skips = false, has_filter = false;
  for (int i = 1; i < argc; ++i) {
    const std::string arg = argv[i];
    if (arg == "--list" && !list) {
      list = true;
    } else if (arg == "--allow-skips" && !allow_skips) {
      allow_skips = true;
    } else if (arg == "--required-list" && required.empty() && i + 1 < argc) {
      required = argv[++i];
      if (required.empty()) return 2;
    } else if (!arg.starts_with('-') && !has_filter) {
      filter = arg;
      has_filter = true;
    } else {
      std::fprintf(stderr, "Invalid test arguments\n");
      return 2;
    }
  }
  if ((!required.empty() && (has_filter || allow_skips || list)) ||
      (list && (has_filter || allow_skips))) return 2;
  if (list) {
    for (const auto& c : q4t::test::Registry())
      std::printf("%s\n", c.name.c_str());
    return q4t::test::Registry().empty() ? 2 : 0;
  }
  std::vector<std::string> names;
  if (!required.empty()) {
    std::ifstream input(required);
    if (!input) return 2;
    std::string name;
    while (std::getline(input, name)) {
      if (!name.empty() && name.back() == '\r') name.pop_back();
      if (!name.empty() && !name.starts_with('#')) names.push_back(name);
    }
    if (input.bad()) return 2;
  } else {
    for (const auto& c : q4t::test::Registry())
      if (c.name.find(filter) != std::string::npos) names.push_back(c.name);
  }
  return q4t::test::RunTests(names, allow_skips);
}
