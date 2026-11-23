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
inline bool IsRealtimeTarget(std::string_view target) {
  const auto path = target.substr(0, target.find('?'));
  return path == "/v1/realtime" || path == "/v1/live/sessions" ||
         !LiveSidebandId(target).empty() || !LiveForkId(target).empty();
}
inline bool IsRealtimeUpgrade(std::string_view method,
                              std::string_view target) {
  return method == "GET" && IsRealtimeTarget(target);
}

inline bool IsLiveMediaCreate(std::string_view method, std::string_view target) {
  return method == "POST" &&
         (target == "/v1/realtime/calls" || target == "/v1/live/sessions" ||
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
  return target.substr(0, target.find('?')) == "/v1/audio/transcriptions";
}
inline bool IsBinaryUpload(std::string_view method, std::string_view target) {
  const auto path = target.substr(0, target.find('?'));
  return (method == "POST" &&
          (path == "/v1/files" || IsTranscriptionTarget(target) ||
           IsVoiceCreation(method, target))) ||
         (UploadMethodAllowed(method, target) && path.ends_with("/parts"));
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

enum class AgentSessionRouteKind { kNone, kCollection, kSession, kTurns, kTurn, kItems, kTurnItems, kEvents };
struct AgentSessionRoute {
  AgentSessionRouteKind kind = AgentSessionRouteKind::kNone;
  std::string_view session, turn;
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
  if (!path.starts_with("turns/")) return {};
  path.remove_prefix(6);
  const auto tail = path.find('/');
  const auto turn = path.substr(0, tail);
  if (!turn.starts_with("turn_") || turn.size() <= 5 ||
      !ValidApplicationId(turn)) return {};
  if (tail == path.npos) return {Kind::kTurn, id, turn};
  if (path.substr(tail) == "/items") return {Kind::kTurnItems, id, turn};
  return {};
}

inline bool IsManagedAgentTarget(std::string_view target) {
  return IsAgentRegistrationTarget(target) ||
         ParseAgentSessionRoute(target).kind != AgentSessionRouteKind::kNone;
}

inline bool ManagedAgentMethodAllowed(std::string_view method,
                                      std::string_view target) {
  using Kind = AgentSessionRouteKind;
  switch (ParseAgentSessionRoute(target).kind) {
    case Kind::kNone: return AgentRegistrationMethodAllowed(method, target);
    case Kind::kEvents: return method == "GET" || method == "POST";
    case Kind::kCollection: return method == "POST";
    case Kind::kSession: return method == "GET" || method == "DELETE";
    default: return method == "GET";
  }
}

inline bool IsManagedAgentObservation(std::string_view method,
                                      std::string_view target) {
  return (method == "GET" || method == "DELETE") &&
         ManagedAgentMethodAllowed(method, target);
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
  return (method == "POST" && target == "/v1/requests/cancel") ||
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
