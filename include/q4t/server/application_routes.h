#pragma once

#include <string_view>

namespace q4t::server {

inline bool ValidApplicationId(std::string_view id) {
  if (id.empty() || id.size() > 128) return false;
  for (const char c : id)
    if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
          (c >= '0' && c <= '9') || c == '_' || c == '-'))
      return false;
  return true;
}

inline std::string_view LiveResourceId(std::string_view path, std::string_view suffix) {
  constexpr std::string_view prefix = "/v1/live/sessions/";
  if (!path.starts_with(prefix) || !path.ends_with(suffix) ||
      path.size() <= prefix.size() + suffix.size())
    return {};
  const auto id =
      path.substr(prefix.size(), path.size() - prefix.size() - suffix.size());
  return id.starts_with("live_") && id.size() > 5 && ValidApplicationId(id)
             ? id
             : std::string_view{};
}

inline std::string_view SipControlId(std::string_view path, std::string_view suffix) {
  constexpr std::string_view prefix = "/v1/realtime/calls/";
  if (!path.starts_with(prefix)) return LiveResourceId(path, suffix);
  path.remove_prefix(prefix.size());
  if (!path.ends_with(suffix)) return {};
  path.remove_suffix(suffix.size());
  return path.starts_with("rtc_") && path.size() > 4 && ValidApplicationId(path)
             ? path : std::string_view{};
}
inline std::string_view SipRejectId(std::string_view path) {
  return SipControlId(path, "/reject");
}
inline bool IsSipHangup(std::string_view method, std::string_view path) {
  return method == "POST" && !SipControlId(path, "/hangup").empty();
}
inline bool IsSipRefer(std::string_view method, std::string_view path) {
  return method == "POST" && !SipControlId(path, "/refer").empty();
}
inline bool IsSipReject(std::string_view method, std::string_view path) {
  return method == "POST" && !SipRejectId(path).empty();
}

inline std::string_view LiveRecordingId(std::string_view path) {
  return LiveResourceId(path, "/content");
}
inline std::string_view LiveSidebandId(std::string_view target) {
  return LiveResourceId(target.substr(0, target.find('?')), "/attach");
}
inline std::string_view RealtimeSidebandId(std::string_view target) {
  constexpr std::string_view prefix = "/v1/realtime?call_id=";
  if (!target.starts_with(prefix)) return {};
  target.remove_prefix(prefix.size());
  return target.starts_with("rtc_") && target.size() > 4 &&
                 ValidApplicationId(target)
             ? target : std::string_view{};
}
inline bool IsCallSideband(std::string_view target) {
  return !LiveSidebandId(target).empty() || !RealtimeSidebandId(target).empty();
}
inline std::string_view LiveForkId(std::string_view path) {
  return LiveResourceId(path, "/fork");
}
// Exact WebSocket targets; adapters validate their own query semantics.
inline bool IsRealtimeTranslationTarget(std::string_view target) {
  return target.substr(0, target.find('?')) == "/v1/realtime/translations";
}
inline bool IsTranslationCall(std::string_view target) {
  return target == "/v1/realtime/translations/calls";
}
inline bool IsRealtimeOffer(std::string_view target) {
  return target == "/v1/realtime/calls" || IsTranslationCall(target);
}
inline bool ValidRealtimeTranslationTarget(std::string_view target) {
  return target == "/v1/realtime/translations" ||
         target == "/v1/realtime/translations?model=gpt-realtime-translate";
}
inline bool IsRealtimeCredentialTarget(std::string_view path) {
  return path == "/v1/realtime/client_secrets" ||
         path == "/v1/realtime/translations/client_secrets" ||
         path == "/v1/realtime/transcription_sessions" ||
         path == "/v1/realtime/sessions";
}

inline bool IsRealtimeTarget(std::string_view target) {
  const auto path = target.substr(0, target.find('?'));
  return path == "/v1/realtime" || IsRealtimeTranslationTarget(target) ||
         path == "/v1/live/sessions" ||
         !LiveSidebandId(target).empty() || !LiveForkId(target).empty();
}
inline bool IsRealtimeUpgrade(std::string_view method,
                              std::string_view target) {
  return method == "GET" && IsRealtimeTarget(target);
}

inline bool IsLiveMediaCreate(std::string_view method, std::string_view target) {
  return method == "POST" &&
         (IsRealtimeOffer(target) || target == "/v1/live/sessions" ||
          !LiveForkId(target).empty() ||
          !SipControlId(target, "/accept").empty());
}

inline bool IsRealtimeTraceTarget(std::string_view target) {
  const auto path = target.substr(0, target.find('?'));
  return path == "/v1/realtime/traces" ||
         path.starts_with("/v1/realtime/traces/");
}
inline bool RealtimeTraceMethodAllowed(std::string_view method,
                                       std::string_view target) {
  const auto path = target.substr(0, target.find('?'));
  constexpr std::string_view root = "/v1/realtime/traces";
  if (target.find('#') != target.npos) return false;
  if (path == root) return method == "GET";
  return path.starts_with(root) && path.size() > root.size() &&
         path[root.size()] == '/' &&
         ValidApplicationId(path.substr(root.size() + 1)) &&
         (method == "GET" || method == "DELETE");
}

enum class McpTunnelRouteKind { kInvalid, kMetadata, kPoll, kResponse };
struct McpTunnelApiRoute {
  McpTunnelRouteKind kind = McpTunnelRouteKind::kInvalid;
  std::string_view id;
};
inline bool IsMcpTunnelTarget(std::string_view path) {
  path = path.substr(0, path.find('?'));
  return path == "/v1/tunnels" || path.starts_with("/v1/tunnels/");
}
inline McpTunnelApiRoute ParseMcpTunnelRoute(std::string_view path) {
  if (path.find('#') != path.npos || !path.starts_with("/v1/tunnels/"))
    return {};
  path = path.substr(12);
  path = path.substr(0, path.find('?'));
  const auto slash = path.find('/');
  const auto id = path.substr(0, slash);
  if (!ValidApplicationId(id)) return {};
  if (slash == path.npos) return {McpTunnelRouteKind::kMetadata, id};
  const auto action = path.substr(slash);
  if (action == "/poll") return {McpTunnelRouteKind::kPoll, id};
  if (action == "/response") return {McpTunnelRouteKind::kResponse, id};
  return {};
}
inline bool McpTunnelMethodAllowed(std::string_view method,
                                   std::string_view path) {
  const auto route = ParseMcpTunnelRoute(path);
  return route.kind == McpTunnelRouteKind::kResponse ? method == "POST"
         : route.kind == McpTunnelRouteKind::kMetadata ||
                 route.kind == McpTunnelRouteKind::kPoll
             ? method == "GET"
             : false;
}
inline bool IsMcpTunnelPoll(std::string_view method, std::string_view path) {
  return method == "GET" &&
         ParseMcpTunnelRoute(path).kind == McpTunnelRouteKind::kPoll;
}
inline bool IsMcpTunnelResponse(std::string_view method,
                                std::string_view path) {
  return method == "POST" &&
         ParseMcpTunnelRoute(path).kind == McpTunnelRouteKind::kResponse;
}
inline bool IsMcpTunnelMetadata(std::string_view method,
                                std::string_view path) {
  return method == "GET" &&
         ParseMcpTunnelRoute(path).kind == McpTunnelRouteKind::kMetadata;
}

enum class VectorStoreRouteKind {
  kInvalid, kStores, kStore, kFiles, kFile, kContent, kBatches, kBatch,
  kBatchFiles, kBatchCancel, kSearch
};
struct VectorStoreRoute {
  VectorStoreRouteKind kind = VectorStoreRouteKind::kInvalid;
  std::string_view store, child;
};
inline bool IsVectorStoreTarget(std::string_view target) {
  const auto path = target.substr(0, target.find('?'));
  return path == "/v1/vector_stores" ||
         path.starts_with("/v1/vector_stores/");
}
inline VectorStoreRoute ParseVectorStoreRoute(std::string_view target) {
  using Kind = VectorStoreRouteKind;
  if (target.find('#') != target.npos) return {};
  auto path = target.substr(0, target.find('?'));
  constexpr std::string_view root = "/v1/vector_stores";
  if (path == root) return {Kind::kStores, {}, {}};
  if (!path.starts_with("/v1/vector_stores/")) return {};
  path.remove_prefix(root.size() + 1);
  auto slash = path.find('/');
  const auto store = path.substr(0, slash);
  if (!ValidApplicationId(store)) return {};
  if (slash == path.npos) return {Kind::kStore, store, {}};
  path.remove_prefix(slash + 1);
  if (path == "search") return {Kind::kSearch, store, {}};
  slash = path.find('/');
  const auto collection = path.substr(0, slash);
  const bool files = collection == "files";
  if (!files && collection != "file_batches") return {};
  if (slash == path.npos)
    return {files ? Kind::kFiles : Kind::kBatches, store, {}};
  path.remove_prefix(slash + 1);
  slash = path.find('/');
  const auto child = path.substr(0, slash);
  if (!ValidApplicationId(child)) return {};
  if (slash == path.npos)
    return {files ? Kind::kFile : Kind::kBatch, store, child};
  const auto action = path.substr(slash);
  if (files && action == "/content") return {Kind::kContent, store, child};
  if (!files && action == "/files") return {Kind::kBatchFiles, store, child};
  if (!files && action == "/cancel") return {Kind::kBatchCancel, store, child};
  return {};
}
inline std::string_view VectorStoreAllowedMethods(VectorStoreRouteKind kind) {
  using Kind = VectorStoreRouteKind;
  switch (kind) {
    case Kind::kStores:
    case Kind::kFiles: return "GET, POST";
    case Kind::kStore:
    case Kind::kFile: return "GET, POST, DELETE";
    case Kind::kContent:
    case Kind::kBatch:
    case Kind::kBatchFiles: return "GET";
    case Kind::kBatches:
    case Kind::kSearch:
    case Kind::kBatchCancel: return "POST";
    case Kind::kInvalid: return {};
  }
  return {};
}
inline bool VectorStoreMethodAllowed(std::string_view method,
                                      std::string_view target) {
  const auto allowed =
      VectorStoreAllowedMethods(ParseVectorStoreRoute(target).kind);
  return (method == "GET" || method == "POST" || method == "DELETE") &&
         allowed.find(method) != allowed.npos;
}
inline bool IsVectorStoreCancellation(std::string_view method,
                                       std::string_view target) {
  return method == "POST" && ParseVectorStoreRoute(target).kind ==
                                 VectorStoreRouteKind::kBatchCancel;
}
inline bool IsVectorStoreObservation(std::string_view method,
                                      std::string_view target) {
  using Kind = VectorStoreRouteKind;
  const auto kind = ParseVectorStoreRoute(target).kind;
  return ((method == "GET" || method == "DELETE") &&
          (kind == Kind::kStore || kind == Kind::kFile)) ||
         (method == "GET" && kind == Kind::kBatch);
}

inline bool IsConversationTarget(std::string_view target) {
  const auto path = target.substr(0, target.find('?'));
  return path == "/v1/conversations" || path.starts_with("/v1/conversations/");
}

inline bool IsBatchTarget(std::string_view target) {
  const auto path = target.substr(0, target.find('?'));
  return path == "/v1/batches" || path.starts_with("/v1/batches/");
}
inline bool BatchMethodAllowed(std::string_view method,
                               std::string_view target) {
  if (target.find('#') != target.npos) return false;
  auto path = target.substr(0, target.find('?'));
  constexpr std::string_view root = "/v1/batches";
  if (path == root) return method == "GET" || method == "POST";
  if (!path.starts_with("/v1/batches/")) return false;
  path.remove_prefix(root.size() + 1);
  const auto slash = path.find('/');
  if (!ValidApplicationId(path.substr(0, slash))) return false;
  return slash == path.npos
             ? method == "GET"
             : path.substr(slash) == "/cancel" && method == "POST";
}
inline bool IsBatchCancellation(std::string_view method,
                                std::string_view target) {
  return BatchMethodAllowed(method, target) && method == "POST" &&
         target.substr(0, target.find('?')).ends_with("/cancel");
}
inline bool IsBatchObservation(std::string_view method,
                               std::string_view target) {
  // The bounded but larger list uses business capacity; small retrieve and
  // cancellation retain independent access while generation is saturated.
  return method == "GET" && BatchMethodAllowed(method, target) &&
         target.substr(0, target.find('?')) != "/v1/batches";
}

inline bool IsFileTarget(std::string_view target) {
  const auto path = target.substr(0, target.find('?'));
  return path == "/v1/files" || path.starts_with("/v1/files/");
}
inline bool FileMethodAllowed(std::string_view method,
                               std::string_view target) {
  if (target.find('#') != target.npos) return false;
  auto path = target.substr(0, target.find('?'));
  if (path == "/v1/files") return method == "GET" || method == "POST";
  if (!path.starts_with("/v1/files/")) return false;
  path.remove_prefix(10);
  const auto slash = path.find('/');
  if (!ValidApplicationId(path.substr(0, slash))) return false;
  if (slash == path.npos) return method == "GET" || method == "DELETE";
  return path.substr(slash) == "/content" && method == "GET";
}
inline bool IsFileMetadata(std::string_view method, std::string_view target) {
  const auto path = target.substr(0, target.find('?'));
  // Lists and content downloads use the business resource partition. Small
  // metadata/delete operations may share the reserved observation partition.
  return FileMethodAllowed(method, target) && path != "/v1/files" &&
         !path.ends_with("/content");
}

inline bool IsUploadTarget(std::string_view target) {
  const auto path = target.substr(0, target.find('?'));
  return path == "/v1/uploads" || path.starts_with("/v1/uploads/");
}
inline bool UploadMethodAllowed(std::string_view method,
                                std::string_view target) {
  if (method != "POST" || target.find('#') != target.npos) return false;
  auto path = target.substr(0, target.find('?'));
  if (path == "/v1/uploads") return true;
  if (!path.starts_with("/v1/uploads/")) return false;
  path.remove_prefix(12);
  const auto slash = path.find('/');
  if (slash == path.npos || !ValidApplicationId(path.substr(0, slash)))
    return false;
  const auto action = path.substr(slash);
  return action == "/parts" || action == "/complete" || action == "/cancel";
}
inline bool IsUploadCancellation(std::string_view method,
                                 std::string_view target) {
  return UploadMethodAllowed(method, target) &&
         target.substr(0, target.find('?')).ends_with("/cancel");
}
inline bool IsVoiceTarget(std::string_view target) {
  const auto path = target.substr(0, target.find('?'));
  return path == "/v1/audio/voices" || path.starts_with("/v1/audio/voices/") ||
         path == "/v1/audio/voice_consents" ||
         path.starts_with("/v1/audio/voice_consents/");
}
inline bool VoiceMethodAllowed(std::string_view method,
                               std::string_view target) {
  if (!IsVoiceTarget(target) || target.find('#') != target.npos) return false;
  const auto path = target.substr(0, target.find('?'));
  const bool consent = path.starts_with("/v1/audio/voice_consents");
  const std::string_view root =
      consent ? "/v1/audio/voice_consents" : "/v1/audio/voices";
  if (path == root) return method == "POST" || (consent && method == "GET");
  if (!ValidApplicationId(path.substr(root.size() + 1))) return false;
  return method == "DELETE" ||
         (consent && (method == "POST" || method == "GET"));
}
inline bool IsVoiceCreation(std::string_view method, std::string_view target) {
  const auto path = target.substr(0, target.find('?'));
  return method == "POST" &&
         (path == "/v1/audio/voices" || path == "/v1/audio/voice_consents");
}
inline bool IsVoiceMetadata(std::string_view method, std::string_view target) {
  return VoiceMethodAllowed(method, target) && !IsVoiceCreation(method, target);
}
inline bool IsTranscriptionTarget(std::string_view target) {
  const auto path = target.substr(0, target.find('?'));
  return path == "/v1/audio/transcriptions" || path == "/v1/audio/translations";
}

inline bool IsResponsesTarget(std::string_view target) {
  const auto path = target.substr(0, target.find('?'));
  return path == "/v1/responses" || path.starts_with("/v1/responses/");
}

inline bool IsResponsesCompaction(std::string_view target) {
  return target.substr(0, target.find('?')) == "/v1/responses/compact";
}

inline bool IsResponsesTokenCount(std::string_view target) {
  return target.substr(0, target.find('?')) == "/v1/responses/input_tokens";
}

inline bool IsAssistantTarget(std::string_view target) {
  const auto path = target.substr(0, target.find('?'));
  if (target.find('#') != target.npos) return false;
  if (path == "/v1/assistants") return true;
  constexpr std::string_view prefix = "/v1/assistants/";
  if (!path.starts_with(prefix)) return false;
  const auto id = path.substr(prefix.size());
  return id.starts_with("asst_") && id.size() > 5 && ValidApplicationId(id);
}

enum class ContainerRouteKind {
  kNone,
  kCollection,
  kContainer,
  kFiles,
  kFile,
  kContent
};
struct ContainerRoute {
  ContainerRouteKind kind = ContainerRouteKind::kNone;
  std::string_view container, file;
};
inline ContainerRoute ParseContainerRoute(std::string_view target) {
  using Kind = ContainerRouteKind;
  if (target.find('#') != target.npos) return {};
  auto path = target.substr(0, target.find('?'));
  if (path == "/v1/containers") return {Kind::kCollection, {}, {}};
  constexpr std::string_view prefix = "/v1/containers/";
  if (!path.starts_with(prefix)) return {};
  path.remove_prefix(prefix.size());
  auto sep = path.find('/');
  const auto id = path.substr(0, sep);
  if (!id.starts_with("cntr_") || id.size() == 5 || !ValidApplicationId(id))
    return {};
  if (sep == path.npos) return {Kind::kContainer, id, {}};
  path.remove_prefix(sep + 1);
  if (path == "files") return {Kind::kFiles, id, {}};
  if (!path.starts_with("files/")) return {};
  path.remove_prefix(6);
  sep = path.find('/');
  const auto file = path.substr(0, sep);
  if (!file.starts_with("cfile_") || file.size() == 6 ||
      !ValidApplicationId(file))
    return {};
  if (sep == path.npos) return {Kind::kFile, id, file};
  if (path.substr(sep) == "/content") return {Kind::kContent, id, file};
  return {};
}
inline bool IsContainerTarget(std::string_view target) {
  return ParseContainerRoute(target).kind != ContainerRouteKind::kNone;
}
inline bool ContainerMethodAllowed(std::string_view method,
                                   std::string_view target) {
  using Kind = ContainerRouteKind;
  const auto kind = ParseContainerRoute(target).kind;
  if (method == "GET") return kind != Kind::kNone;
  if (method == "POST")
    return kind == Kind::kCollection || kind == Kind::kFiles;
  return method == "DELETE" &&
         (kind == Kind::kContainer || kind == Kind::kFile);
}
inline bool IsContainerObservation(std::string_view method,
                                   std::string_view target) {
  return (method == "GET" || method == "DELETE") &&
         ContainerMethodAllowed(method, target) &&
         ParseContainerRoute(target).kind != ContainerRouteKind::kContent;
}

enum class SkillRouteKind {
  kNone,
  kCollection,
  kSkill,
  kContent,
  kVersions,
  kVersion,
  kVersionContent
};
struct SkillRoute {
  SkillRouteKind kind = SkillRouteKind::kNone;
  std::string_view skill, version;
};

inline bool ValidSkillVersionNumber(std::string_view value) {
  return !value.empty() && value.size() <= 19 && value.front() != '0' &&
         value.find_first_not_of("0123456789") == value.npos;
}

inline SkillRoute ParseSkillRoute(std::string_view target) {
  using Kind = SkillRouteKind;
  if (target.find('#') != target.npos) return {};
  const auto path = target.substr(0, target.find('?'));
  constexpr std::string_view root = "/v1/skills";
  if (path == root) return {Kind::kCollection, {}, {}};
  if (!path.starts_with("/v1/skills/")) return {};
  auto suffix = path.substr(root.size() + 1);
  auto slash = suffix.find('/');
  const auto id = suffix.substr(0, slash);
  if (!ValidApplicationId(id) || !id.starts_with("skill_") || id.size() < 7)
    return {};
  if (slash == suffix.npos) return {Kind::kSkill, id, {}};
  suffix.remove_prefix(slash + 1);
  if (suffix == "content") return {Kind::kContent, id, {}};
  if (suffix == "versions") return {Kind::kVersions, id, {}};
  if (!suffix.starts_with("versions/")) return {};
  suffix.remove_prefix(9);
  slash = suffix.find('/');
  const auto v = suffix.substr(0, slash);
  if (!ValidApplicationId(v)) return {};
  if (slash == suffix.npos) return {Kind::kVersion, id, v};
  if (suffix.substr(slash) == "/content") return {Kind::kVersionContent, id, v};
  return {};
}
inline bool IsSkillTarget(std::string_view target) {
  return ParseSkillRoute(target).kind != SkillRouteKind::kNone;
}
inline bool SkillMethodAllowed(std::string_view method,
                               std::string_view target) {
  using Kind = SkillRouteKind;
  const auto kind = ParseSkillRoute(target).kind;
  if (method == "GET") return kind != Kind::kNone;
  if (method == "POST")
    return kind == Kind::kCollection || kind == Kind::kVersions ||
           kind == Kind::kSkill;
  return method == "DELETE" && (kind == Kind::kSkill || kind == Kind::kVersion);
}
inline bool IsSkillObservation(std::string_view method,
                               std::string_view target) {
  using Kind = SkillRouteKind;
  const auto kind = ParseSkillRoute(target).kind;
  return (method == "GET" || method == "DELETE") &&
         SkillMethodAllowed(method, target) && kind != Kind::kContent &&
         kind != Kind::kVersionContent;
}
inline bool IsSkillCreation(std::string_view method, std::string_view target) {
  const auto kind = ParseSkillRoute(target).kind;
  return method == "POST" && (kind == SkillRouteKind::kCollection ||
                              kind == SkillRouteKind::kVersions);
}

// Container file creation also accepts a small JSON reference. Only the MIME
// upload gets the streaming file allowance at both admission boundaries.
inline bool IsMultipartForm(std::string_view type) {
  type = type.substr(0, type.find(';'));
  const auto end = type.find_last_not_of(" \t");
  if (end == type.npos) return false;
  type = type.substr(0, end + 1);
  constexpr std::string_view form = "multipart/form-data";
  if (type.size() != form.size()) return false;
  for (size_t i = 0; i < type.size(); ++i) {
    auto c = type[i];
    if (c >= 'A' && c <= 'Z') c = static_cast<char>(c + ('a' - 'A'));
    if (c != form[i]) return false;
  }
  return true;
}

inline bool IsBinaryUpload(std::string_view method, std::string_view target,
                           std::string_view content_type = {}) {
  const auto path = target.substr(0, target.find('?'));
  return (method == "POST" &&
          (path == "/v1/files" || IsTranscriptionTarget(target) ||
           IsVoiceCreation(method, target) ||
           (IsSkillCreation(method,target) && IsMultipartForm(content_type)) ||
           (ParseContainerRoute(target).kind == ContainerRouteKind::kFiles &&
            IsMultipartForm(content_type)))) ||
         (UploadMethodAllowed(method, target) && path.ends_with("/parts"));
}

enum class AssistantThreadRouteKind { kNone, kCollection, kThread, kMessages, kMessage };
struct AssistantThreadRoute {
  AssistantThreadRouteKind kind = AssistantThreadRouteKind::kNone;
  std::string_view thread, message;
};

inline AssistantThreadRoute ParseAssistantThreadRoute(std::string_view target) {
  using Kind = AssistantThreadRouteKind;
  if (target.find('#') != target.npos) return {};
  auto path = target.substr(0, target.find('?'));
  if (path == "/v1/threads") return {Kind::kCollection, {}, {}};
  constexpr std::string_view prefix = "/v1/threads/";
  if (!path.starts_with(prefix)) return {};
  path.remove_prefix(prefix.size());
  const auto sep = path.find('/');
  const auto thread = path.substr(0, sep);
  if (!thread.starts_with("thread_") || thread.size() == 7 ||
      !ValidApplicationId(thread)) return {};
  if (sep == path.npos) return {Kind::kThread, thread, {}};
  path.remove_prefix(sep + 1);
  if (path == "messages") return {Kind::kMessages, thread, {}};
  if (!path.starts_with("messages/")) return {};
  const auto message = path.substr(9);
  if (!message.starts_with("msg_") || message.size() == 4 ||
      !ValidApplicationId(message)) return {};
  return {Kind::kMessage, thread, message};
}

enum class AssistantRunRouteKind {
  kNone,
  kCreateThread,
  kRuns,
  kRun,
  kCancel,
  kSubmit,
  kSteps,
  kStep
};
struct AssistantRunRoute {
  AssistantRunRouteKind kind = AssistantRunRouteKind::kNone;
  std::string_view thread, run, step;
};
inline AssistantRunRoute ParseAssistantRunRoute(std::string_view target) {
  using Kind = AssistantRunRouteKind;
  if (target.find('#') != target.npos) return {};
  auto path = target.substr(0, target.find('?'));
  if (path == "/v1/threads/runs") return {Kind::kCreateThread, {}, {}, {}};
  if (!path.starts_with("/v1/threads/")) return {};
  path.remove_prefix(12);
  const auto slash = path.find('/');
  const auto thread = path.substr(0, slash);
  if (slash == path.npos || !thread.starts_with("thread_") ||
      thread.size() <= 7 || !ValidApplicationId(thread))
    return {};
  path.remove_prefix(slash + 1);
  if (path == "runs") return {Kind::kRuns, thread, {}, {}};
  if (!path.starts_with("runs/")) return {};
  path.remove_prefix(5);
  const auto next = path.find('/');
  const auto run = path.substr(0, next);
  if (!run.starts_with("run_") || run.size() <= 4 || !ValidApplicationId(run))
    return {};
  if (next == path.npos) return {Kind::kRun, thread, run, {}};
  path.remove_prefix(next + 1);
  if (path == "cancel") return {Kind::kCancel, thread, run, {}};
  if (path == "submit_tool_outputs") return {Kind::kSubmit, thread, run, {}};
  if (path == "steps") return {Kind::kSteps, thread, run, {}};
  if (!path.starts_with("steps/")) return {};
  const auto step = path.substr(6);
  if (!step.starts_with("step_") || step.size() <= 5 ||
      !ValidApplicationId(step))
    return {};
  return {Kind::kStep, thread, run, step};
}
inline bool AssistantRunMethodAllowed(std::string_view method,
                                      std::string_view target) {
  using Kind = AssistantRunRouteKind;
  switch (ParseAssistantRunRoute(target).kind) {
    case Kind::kCreateThread:
    case Kind::kCancel:
    case Kind::kSubmit:
      return method == "POST";
    case Kind::kRuns:
    case Kind::kRun:
      return method == "GET" || method == "POST";
    case Kind::kSteps:
    case Kind::kStep:
      return method == "GET";
    case Kind::kNone:
      return false;
  }
  return false;
}

inline bool IsAssistantsV2Target(std::string_view target) {
  return IsAssistantTarget(target) ||
         ParseAssistantRunRoute(target).kind != AssistantRunRouteKind::kNone ||
         ParseAssistantThreadRoute(target).kind !=
             AssistantThreadRouteKind::kNone;
}

inline bool AssistantMethodAllowed(std::string_view method,
                                   std::string_view target) {
  if (AssistantRunMethodAllowed(method, target)) return true;
  using Kind = AssistantThreadRouteKind;
  const auto kind = ParseAssistantThreadRoute(target).kind;
  if (kind != Kind::kNone)
    return method == "POST" || (method == "GET" && kind != Kind::kCollection) ||
           (method == "DELETE" &&
            (kind == Kind::kThread || kind == Kind::kMessage));
  return IsAssistantTarget(target) &&
         (method == "GET" || method == "POST" ||
          (method == "DELETE" && target.starts_with("/v1/assistants/")));
}

inline bool IsAssistantObservation(std::string_view method,
                                   std::string_view target) {
  return (method != "POST" && AssistantMethodAllowed(method, target)) ||
         (method == "POST" && ParseAssistantRunRoute(target).kind ==
                                  AssistantRunRouteKind::kCancel);
}

enum class AutomationRouteKind {
  kNone, kCollection, kResource, kPause, kResume, kInvocations, kInvocation
};
struct AutomationRoute {
  AutomationRouteKind kind = AutomationRouteKind::kNone;
  std::string_view automation{};
  std::string_view invocation{};
};

inline AutomationRoute ParseAutomationRoute(std::string_view target) {
  using Kind = AutomationRouteKind;
  if (target.find('#') != target.npos) return {};
  auto path = target.substr(0, target.find('?'));
  constexpr std::string_view root = "/v1/agents/automations";
  if (path == root) return {Kind::kCollection};
  if (!path.starts_with(root) || path.size() <= root.size() ||
      path[root.size()] != '/')
    return {};
  path.remove_prefix(root.size() + 1);
  const auto slash = path.find('/');
  const auto id = path.substr(0, slash);
  if (!id.starts_with("automation_") || id.size() <= std::string_view("automation_").size() ||
      !ValidApplicationId(id))
    return {};
  if (slash == path.npos) return {Kind::kResource, id};
  path.remove_prefix(slash + 1);
  if (path == "pause") return {Kind::kPause, id};
  if (path == "resume") return {Kind::kResume, id};
  if (path == "invocations") return {Kind::kInvocations, id};
  constexpr std::string_view invocations = "invocations/";
  if (!path.starts_with(invocations)) return {};
  path.remove_prefix(invocations.size());
  return path.starts_with("invocation_") && path.size() > std::string_view("invocation_").size() &&
                 ValidApplicationId(path)
             ? AutomationRoute{Kind::kInvocation, id, path}
             : AutomationRoute{};
}

inline bool IsAutomationAction(std::string_view target) {
  const auto kind = ParseAutomationRoute(target).kind;
  return kind == AutomationRouteKind::kPause ||
         kind == AutomationRouteKind::kResume ||
         kind == AutomationRouteKind::kInvocations;
}

inline bool AutomationMethodAllowed(std::string_view method,
                                    AutomationRouteKind kind) {
  using Kind = AutomationRouteKind;
  switch (kind) {
    case Kind::kCollection:
    case Kind::kInvocations:
      return method == "POST" || method == "GET";
    case Kind::kResource:
      return method == "GET" || method == "POST" || method == "DELETE";
    case Kind::kPause:
    case Kind::kResume:
      return method == "POST";
    case Kind::kInvocation:
      return method == "GET";
    case Kind::kNone:
      return false;
  }
  return false;
}

inline bool IsAgentRegistrationTarget(std::string_view target) {
  const auto path = target.substr(0, target.find('?'));
  if (target.find('#') != target.npos) return false;
  if (path == "/v1/agents") return true;
  constexpr std::string_view prefix = "/v1/agents/";
  if (!path.starts_with(prefix)) return false;
  const auto id = path.substr(prefix.size());
  return id.starts_with("agent_") && id.size() > 6 && ValidApplicationId(id);
}

inline bool AgentRegistrationMethodAllowed(std::string_view method,
                                           std::string_view target) {
  if (!IsAgentRegistrationTarget(target)) return false;
  return method == "GET" || method == "POST" ||
         (method == "DELETE" && target.substr(0, target.find('?')) != "/v1/agents");
}

enum class AgentSessionRouteKind {
  kNone, kCollection, kSession, kTurns, kTurn, kItems, kTurnItems, kEvents,
  kTraces, kArtifacts, kArtifact, kArtifactContent, kSubagents, kSubagent,
  kSubagentItems, kSubagentTurns, kSubagentTurn, kSubagentTurnItems
};
struct AgentSessionRoute {
  AgentSessionRouteKind kind = AgentSessionRouteKind::kNone;
  std::string_view session, turn, artifact = {}, subagent = {};
};

inline AgentSessionRoute ParseAgentSessionRoute(std::string_view target) {
  using Kind = AgentSessionRouteKind;
  if (target.find('#') != target.npos) return {};
  auto path = target.substr(0, target.find('?'));
  constexpr std::string_view root = "/v1/agents/sessions";
  if (path == root) return {Kind::kCollection, {}, {}};
  if (!path.starts_with(root) || path.substr(root.size(), 1) != "/") return {};
  path.remove_prefix(root.size() + 1);
  const auto split = path.find('/');
  const auto id = path.substr(0, split);
  if (!id.starts_with("agent_session_") || id.size() <= 14 ||
      !ValidApplicationId(id)) return {};
  if (split == path.npos) return {Kind::kSession, id, {}};
  path.remove_prefix(split + 1);
  if (path == "events") return {Kind::kEvents, id, {}};
  if (path == "items") return {Kind::kItems, id, {}};
  if (path == "turns") return {Kind::kTurns, id, {}};
  if (path == "traces") return {Kind::kTraces, id, {}};
  if (path == "artifacts") return {Kind::kArtifacts, id, {}};
  if (path.starts_with("artifacts/")) {
    path.remove_prefix(10);
    const auto tail = path.find('/');
    const auto artifact = path.substr(0, tail);
    if (!artifact.starts_with("artifact_") || artifact.size() <= 9 ||
        !ValidApplicationId(artifact)) return {};
    if (tail == path.npos) return {Kind::kArtifact, id, {}, artifact};
    if (path.substr(tail) == "/content")
      return {Kind::kArtifactContent, id, {}, artifact};
    return {};
  }
  if (path == "subagents") return {Kind::kSubagents, id, {}};
  std::string_view subagent;
  if (path.starts_with("subagents/")) {
    path.remove_prefix(10);
    const auto tail = path.find('/');
    subagent = path.substr(0, tail);
    if (!subagent.starts_with("subagent_") || subagent.size() <= 9 ||
        !ValidApplicationId(subagent)) return {};
    if (tail == path.npos) return {Kind::kSubagent, id, {}, {}, subagent};
    path.remove_prefix(tail + 1);
    if (path == "items") return {Kind::kSubagentItems, id, {}, {}, subagent};
    if (path == "turns") return {Kind::kSubagentTurns, id, {}, {}, subagent};
  }
  if (!path.starts_with("turns/")) return {};
  path.remove_prefix(6);
  const auto tail = path.find('/');
  const auto turn = path.substr(0, tail);
  if (!turn.starts_with("turn_") || turn.size() <= 5 ||
      !ValidApplicationId(turn)) return {};
  if (tail == path.npos)
    return {subagent.empty() ? Kind::kTurn : Kind::kSubagentTurn, id, turn,
            {}, subagent};
  if (path.substr(tail) == "/items")
    return {subagent.empty() ? Kind::kTurnItems : Kind::kSubagentTurnItems,
            id, turn, {}, subagent};
  return {};
}

enum class VaultRouteKind { kNone, kCollection, kVault, kCredentials, kCredential };
struct VaultRoute {
  VaultRouteKind kind = VaultRouteKind::kNone;
  std::string_view vault;
  std::string_view credential;
};

inline VaultRoute ParseVaultRoute(std::string_view target) {
  constexpr std::string_view root = "/v1/vaults";
  auto path = target.substr(0, target.find('?'));
  if (target.find('#') != target.npos || !path.starts_with(root)) return {};
  path.remove_prefix(root.size());
  if (path.empty()) return {VaultRouteKind::kCollection, {}, {}};
  if (path.front() != '/') return {};
  path.remove_prefix(1);
  const auto vault = path.substr(0, path.find('/'));
  if (!vault.starts_with("vault_") || vault.size() <= 6 || !ValidApplicationId(vault))
    return {};
  path.remove_prefix(vault.size());
  if (path.empty()) return {VaultRouteKind::kVault, vault, {}};
  constexpr std::string_view collection = "/credentials";
  if (path == collection) return {VaultRouteKind::kCredentials, vault, {}};
  if (!path.starts_with("/credentials/")) return {};
  const auto id = path.substr(collection.size() + 1);
  if (!id.starts_with("cred_") || id.size() <= 5 || !ValidApplicationId(id)) return {};
  return {VaultRouteKind::kCredential, vault, id};
}

inline bool IsVaultTarget(std::string_view target) {
  return ParseVaultRoute(target).kind != VaultRouteKind::kNone;
}

inline constexpr std::string_view kEnvironmentTemplatesPath =
    "/v1/agents/environments/templates";

inline bool IsEnvironmentTemplateTarget(std::string_view target) {
  if (target.find('#') != target.npos) return false;
  const auto path = target.substr(0, target.find('?'));
  if (path == kEnvironmentTemplatesPath) return true;
  if (!path.starts_with("/v1/agents/environments/templates/")) return false;
  const auto id = path.substr(kEnvironmentTemplatesPath.size() + 1);
  return id.starts_with("environment_template_") && id.size() > 21 &&
         ValidApplicationId(id);
}

inline bool EnvironmentTemplateMethodAllowed(std::string_view method,
                                             std::string_view target) {
  return IsEnvironmentTemplateTarget(target) &&
         (method == "GET" || method == "POST" ||
          (method == "DELETE" &&
           target.substr(0, target.find('?')) != kEnvironmentTemplatesPath));
}

inline constexpr std::string_view kHostedEnvironmentsPath = "/v1/agents/environments";
enum class HostedRouteKind { kNone, kCollection, kResource, kFiles };
struct HostedRoute {
  HostedRouteKind kind = HostedRouteKind::kNone;
  std::string_view id;
};
inline HostedRoute ParseHostedRoute(std::string_view target) {
  if (target.find('#') != target.npos) return {};
  auto path = target.substr(0, target.find('?'));
  if (path == kHostedEnvironmentsPath) return {HostedRouteKind::kCollection, {}};
  if (!path.starts_with("/v1/agents/environments/")) return {};
  path.remove_prefix(kHostedEnvironmentsPath.size() + 1);
  const auto slash = path.find('/');
  const auto id = path.substr(0, slash);
  if (!id.starts_with("environment_") || id.size() <= 12 ||
      id.starts_with("environment_template_") || !ValidApplicationId(id))
    return {};
  if (slash == path.npos) return {HostedRouteKind::kResource, id};
  if (path.substr(slash) == "/files") return {HostedRouteKind::kFiles, id};
  return {};
}

inline bool IsManagedAgentTarget(std::string_view target) {
  return ParseAutomationRoute(target).kind != AutomationRouteKind::kNone ||
         ParseHostedRoute(target).kind != HostedRouteKind::kNone ||
         IsVaultTarget(target) || IsEnvironmentTemplateTarget(target) ||
         IsAgentRegistrationTarget(target) ||
         ParseAgentSessionRoute(target).kind != AgentSessionRouteKind::kNone;
}

inline bool ManagedAgentMethodAllowed(std::string_view method,
                                      std::string_view target) {
  using Kind = AgentSessionRouteKind;
  const auto automation = ParseAutomationRoute(target).kind;
  if (automation != AutomationRouteKind::kNone)
    return AutomationMethodAllowed(method, automation);
  const auto hosted = ParseHostedRoute(target).kind;
  if (hosted != HostedRouteKind::kNone)
    return method == "GET" || (method == "POST" && hosted != HostedRouteKind::kResource);
  if (IsEnvironmentTemplateTarget(target))
    return EnvironmentTemplateMethodAllowed(method, target);
  const auto vault = ParseVaultRoute(target).kind;
  if (vault != VaultRouteKind::kNone)
    return method == "GET" || method == "POST" ||
           (method == "DELETE" && (vault == VaultRouteKind::kVault ||
                                   vault == VaultRouteKind::kCredential));
  switch (ParseAgentSessionRoute(target).kind) {
    case Kind::kNone: return AgentRegistrationMethodAllowed(method, target);
    case Kind::kEvents: return method == "GET" || method == "POST";
    case Kind::kCollection: return method == "GET" || method == "POST";
    case Kind::kSession: return method == "GET" || method == "POST" || method == "DELETE";
    case Kind::kArtifact: return method == "GET" || method == "DELETE";
    default: return method == "GET";
  }
}

inline bool IsAgentEventStream(std::string_view method, std::string_view target) {
  return method == "GET" && ParseAgentSessionRoute(target).kind == AgentSessionRouteKind::kEvents;
}
inline bool IsAgentEventSubmission(std::string_view method, std::string_view target) {
  return method == "POST" && ParseAgentSessionRoute(target).kind == AgentSessionRouteKind::kEvents;
}

inline bool IsManagedAgentObservation(std::string_view method,
                                      std::string_view target) {
  return (method == "GET" || method == "DELETE") &&
         ParseAgentSessionRoute(target).kind != AgentSessionRouteKind::kArtifactContent &&
         !IsAgentEventStream(method, target) && ManagedAgentMethodAllowed(method, target);
}

inline bool IsPromptTarget(std::string_view target) {
  const auto path = target.substr(0, target.find('?'));
  return path == "/v1/prompts" || path.starts_with("/v1/prompts/");
}

inline bool PromptMethodAllowed(std::string_view method,
                                 std::string_view target) {
  if (target.find('#') != target.npos) return false;
  auto path = target.substr(0, target.find('?'));
  constexpr std::string_view root = "/v1/prompts";
  if (path == root) return method == "GET" || method == "POST";
  if (!path.starts_with("/v1/prompts/")) return false;
  path.remove_prefix(root.size() + 1);
  const auto end = path.find('/');
  if (!ValidApplicationId(path.substr(0, end))) return false;
  if (end == path.npos)
    return method == "GET" || method == "POST" || method == "DELETE";
  path.remove_prefix(end);
  if (path == "/versions") return method == "POST";
  return path.starts_with("/versions/") &&
         ValidApplicationId(path.substr(10)) && method == "GET";
}

inline bool IsPromptObservation(std::string_view method,
                                 std::string_view target) {
  return (method == "GET" || method == "DELETE") &&
         PromptMethodAllowed(method, target);
}

inline bool IsStoredChatTarget(std::string_view target) {
  const auto path = target.substr(0, target.find('?'));
  return path == "/v1/chat/completions" ||
         path.starts_with("/v1/chat/completions/");
}

inline bool StoredChatMethodAllowed(std::string_view method,
                                    std::string_view target) {
  if (target.find('#') != target.npos) return false;
  auto path = target.substr(0, target.find('?'));
  constexpr std::string_view root = "/v1/chat/completions";
  if (path == root) return method == "GET" || method == "POST";
  if (!path.starts_with("/v1/chat/completions/")) return false;
  path.remove_prefix(root.size() + 1);
  const auto end = path.find('/');
  if (!ValidApplicationId(path.substr(0, end))) return false;
  if (end == path.npos)
    return method == "GET" || method == "POST" || method == "DELETE";
  return path.substr(end) == "/messages" && method == "GET";
}

inline bool IsStoredChatManagement(std::string_view method,
                                   std::string_view target) {
  return StoredChatMethodAllowed(method, target) &&
         !(method == "POST" &&
           target.substr(0, target.find('?')) == "/v1/chat/completions");
}

inline bool ResponsesMethodAllowed(std::string_view method,
                                   std::string_view target) {
  if (target.find('#') != target.npos) return false;
  auto path = target.substr(0, target.find('?'));
  constexpr std::string_view root = "/v1/responses";
  if (path == root) return method == "POST";
  if (IsResponsesCompaction(path)) return method == "POST";
  if (IsResponsesTokenCount(path)) return method == "POST";
  if (!path.starts_with("/v1/responses/")) return false;
  path.remove_prefix(root.size() + 1);
  const auto end = path.find('/');
  if (!ValidApplicationId(path.substr(0, end))) return false;
  if (end == path.npos) return method == "GET" || method == "DELETE";
  if (path.substr(end) == "/cancel") return method == "POST";
  return path.substr(end) == "/input_items" && method == "GET";
}

inline bool IsResponseCancellation(std::string_view method,
                                   std::string_view target) {
  const auto path = target.substr(0, target.find('?'));
  return method == "POST" && path.ends_with("/cancel") &&
         ResponsesMethodAllowed(method, target);
}

inline bool IsCancellationRequest(std::string_view method,
                                  std::string_view target) {
  return (method == "POST" && ParseAssistantRunRoute(target).kind ==
                                  AssistantRunRouteKind::kCancel) ||
         (method == "POST" && target == "/v1/requests/cancel") ||
         IsResponseCancellation(method, target) ||
         IsBatchCancellation(method, target) ||
         IsVectorStoreCancellation(method, target) ||
         IsUploadCancellation(method, target);
}

inline bool IsResponsesObservation(std::string_view method,
                                   std::string_view target) {
  return (method == "GET" || method == "DELETE") &&
         ResponsesMethodAllowed(method, target);
}

// Shared ingress/backend route recognition. Query semantics remain the native
// endpoint's responsibility; no normalization changes the forwarded target.
inline bool ConversationMethodAllowed(std::string_view method,
                                      std::string_view target) {
  if (target.find('#') != target.npos) return false;
  auto path = target.substr(0, target.find('?'));
  constexpr std::string_view root = "/v1/conversations";
  if (path == root) return method == "POST";
  if (!path.starts_with("/v1/conversations/")) return false;
  path.remove_prefix(root.size() + 1);
  const auto end = path.find('/');
  if (!ValidApplicationId(path.substr(0, end))) return false;
  if (end == path.npos)
    return method == "GET" || method == "POST" || method == "DELETE";
  path.remove_prefix(end);
  if (path == "/items") return method == "GET" || method == "POST";
  if (path.starts_with("/items/") && ValidApplicationId(path.substr(7)))
    return method == "GET" || method == "DELETE";
  return false;
}

}  // namespace q4t::server
