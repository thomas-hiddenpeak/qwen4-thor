#pragma once

#include "object_protocol.h"
#include "q4t/server/vector_stores.h"

namespace q4t::server::vector_store_detail {
using namespace object_detail;
inline constexpr std::string_view kStore = "q4t.vector_store.v1";
inline constexpr std::string_view kMember = "q4t.vector_store_file.v1";
ObjectReply Invalid(std::string message, std::string param = {});
ObjectReply Corrupt();
ObjectReply Expired();
ObjectReply Result(Json value);
bool Retry(const ObjectReply& reply);
bool Live(const Json& data);
ObjectRequest Write(const BinaryAccess& access, ObjectKind kind,
                    const std::string& id, int64_t revision, Json data,
                    int64_t mutate_before = 0);
const Json* Edge(const Json& data, const std::string& file_id);
bool ValidateStore(const Json& record);
bool ValidateMember(const Json& record, const Json& store);
Json PublicStore(const Json& data);
void Summarize(Json* data);
void Activity(Json* data);
ObjectReply Fields(const Json& request, bool creating);
ObjectReply PrepareMember(ObjectStore& objects, const BinaryAccess& access,
                          const std::string& id, const Json& request,
                          int64_t created_at, Json* data, Json* edge);
ObjectReply MatchClaim(const Json& data, const VectorIngestionClaim& claim,
                       bool require_live);
}  // namespace q4t::server::vector_store_detail
