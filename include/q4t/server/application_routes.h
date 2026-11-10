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
inline bool IsBinaryUpload(std::string_view method, std::string_view target) {
  const auto path = target.substr(0, target.find('?'));
  return (method == "POST" && path == "/v1/files") ||
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
