// Tests for the minimal JSON parser.
#include "q4t/io/json.h"
#include "q4t/test.h"

#include <cmath>
#include <string>

namespace {

using q4t::io::Json;
using q4t::io::ParseJson;
using q4t::Status;

}  // namespace

Q4T_TEST(json_parse_object_scalars) {
  Json j;
  Status s = ParseJson(
      R"({"a": 1, "b": -2.5, "c": true, "d": false, "e": "hi", "f": null})", &j);
  Q4T_CHECK(s.ok());
  Q4T_CHECK(j.IsObject());
  Q4T_CHECK(j.GetInt("a") == 1);
  Q4T_CHECK(std::fabs(j.GetNumber("b") - (-2.5)) < 1e-12);
  Q4T_CHECK(j.GetBool("c") == true);
  Q4T_CHECK(j.GetBool("d") == false);
  Q4T_CHECK(j.GetString("e") == "hi");
  Q4T_CHECK(j.Has("f") && j.Find("f")->IsNull());
  Q4T_CHECK(!j.Has("missing"));
  return true;
}

Q4T_TEST(json_parse_nested_arrays) {
  Json j;
  Status s =
      ParseJson(R"({"shape": [2, 3, 4], "data_offsets": [0, 24], "n": 1e3})", &j);
  Q4T_CHECK(s.ok());
  const Json* shape = j.GetArray("shape");
  Q4T_CHECK(shape != nullptr);
  Q4T_CHECK(shape->array.size() == 3);
  Q4T_CHECK(shape->array[0].AsInt() == 2);
  Q4T_CHECK(shape->array[1].AsInt() == 3);
  Q4T_CHECK(shape->array[2].AsInt() == 4);
  const Json* off = j.GetArray("data_offsets");
  Q4T_CHECK(off->array[0].AsInt() == 0);
  Q4T_CHECK(off->array[1].AsInt() == 24);
  Q4T_CHECK(j.GetInt("n") == 1000);  // 1e3
  return true;
}

Q4T_TEST(json_parse_string_escapes) {
  Json j;
  // \n, \t, \" and a \u escape (U+4E2D = 中).
  Status s = ParseJson(R"({"s": "a\nb\tc\"d\u4e2d"})", &j);
  Q4T_CHECK(s.ok());
  const std::string v = j.GetString("s");
  // Decoded: a \n b \t c " d (7 ASCII) + U+4E2D (3 UTF-8 bytes E4 B8 AD) = 10.
  Q4T_CHECK(v.size() == 10);
  Q4T_CHECK(v[0] == 'a' && v[1] == '\n' && v[2] == 'b' && v[3] == '\t');
  Q4T_CHECK(v[4] == 'c' && v[5] == '"' && v[6] == 'd');
  Q4T_CHECK(static_cast<unsigned char>(v[7]) == 0xE4);
  Q4T_CHECK(static_cast<unsigned char>(v[8]) == 0xB8);
  Q4T_CHECK(static_cast<unsigned char>(v[9]) == 0xAD);
  return true;
}

Q4T_TEST(json_parse_errors) {
  Json j;
  Q4T_CHECK(!ParseJson("{\"a\": 1})", &j).ok());  // trailing ')'
  Q4T_CHECK(!ParseJson(R"({"a": })", &j).ok());  // bad value
  Q4T_CHECK(!ParseJson(R"([1, 2)", &j).ok());  // bad array
  Q4T_CHECK(!ParseJson(R"("unterminated)", &j).ok());
  Q4T_CHECK(!ParseJson("", &j).ok());  // empty
  return true;
}
