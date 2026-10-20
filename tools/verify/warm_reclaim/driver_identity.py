#!/usr/bin/env python3
"""Read-only identity and NVIDIA shrinker callback attribution on this Thor.

Only a JSON evidence file is written, under the repository's .q4t-work tree.
The AArch64 recognizer deliberately rejects a changed instruction sequence;
callback addresses are never inferred from a nearest symbol or a known offset.
No GPU, NVML, trace configuration, reclaim, or module parameter API is used.
"""

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import time


ROOT = Path(__file__).resolve().parents[3]
SYMBOLS = ("nv_mem_pool_init", "nv_mem_pool_alloc_pages",
           "nv_alloc_system_pages")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha256(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


class Evidence:
    def __init__(self):
        self.commands = []

    def run(self, *argv):
        entry = {"argv": list(argv), "start_monotonic_ns": time.monotonic_ns()}
        self.commands.append(entry)
        try:
            result = subprocess.run(argv, capture_output=True, text=True,
                                    timeout=30, check=False,
                                    env={**os.environ, "LC_ALL": "C"})
        except subprocess.TimeoutExpired as error:
            entry.update({"error": "timeout", "stdout": str(error.stdout),
                          "stderr": str(error.stderr)})
            raise RuntimeError(f"command timed out: {argv[0]}") from error
        entry.update({"returncode": result.returncode,
                      "stdout": result.stdout, "stderr": result.stderr,
                      "end_monotonic_ns": time.monotonic_ns()})
        require(result.returncode == 0, f"command failed: {argv}")
        return result.stdout

    def protected_read(self, path):
        prefix = [] if os.geteuid() == 0 else ["sudo", "-n"]
        return self.run(*prefix, "cat", str(path))


def note_build_id(text):
    """Decode a GNU build-id ELF note read with od; reject extra/malformed data."""
    raw = bytes.fromhex(text)
    require(len(raw) >= 12, "loaded build-id note is too short")
    namesz, descsz, kind = struct.unpack_from("<III", raw)
    desc_at = 12 + ((namesz + 3) & ~3)
    require(kind == 3 and raw[12:12 + namesz] == b"GNU\0",
            "loaded note is not NT_GNU_BUILD_ID")
    require(descsz > 0 and len(raw) == desc_at + ((descsz + 3) & ~3),
            "loaded build-id note size mismatch")
    return raw[desc_at:desc_at + descsz].hex()


def parse_instructions(text):
    instructions = []
    relocations = {}
    for line in text.splitlines():
        match = re.fullmatch(r"\s*([0-9a-f]+):\s+[0-9a-f]{8}\s+"
                             r"(\w+)\s*(.*?)\s*", line)
        if match:
            operands = match[3].split("//", 1)[0].strip()
            instructions.append((int(match[1], 16), match[2],
                                 re.sub(r"\s+", "", operands)))
        match = re.fullmatch(r"\s*([0-9a-f]+):\s+"
                             r"(R_AARCH64_\w+)\s+(\S+)\s*", line)
        if match:
            address = int(match[1], 16)
            require(address not in relocations, "duplicate relocation")
            relocations[address] = (match[2], match[3])
    require(instructions, "no AArch64 instructions")
    return instructions, relocations


def extract_callbacks(text):
    """Recognize the local reviewed two-pointer store into struct shrinker."""
    insns, relocs = parse_instructions(text)
    alloc = [i for i, (addr, op, _) in enumerate(insns)
             if op == "bl" and relocs.get(addr) ==
             ("R_AARCH64_CALL26", "shrinker_alloc")]
    register = [i for i, (addr, op, _) in enumerate(insns)
                if op == "bl" and relocs.get(addr) ==
                ("R_AARCH64_CALL26", "shrinker_register")]
    require(len(alloc) == len(register) == 1,
            "expected exactly one shrinker allocation and registration")
    first, last = alloc[0], register[0]
    body = insns[first + 1:last]
    require(len(body) == 10, "unrecognized shrinker initialization length")
    expected = [("mov", "x20,x0"), ("cbz", None),
                ("adrp", None), ("adrp", None),
                ("add", "x2,x2,#0x0"), ("add", "x1,x1,#0x0"),
                ("mov", "w3,#0x1"), ("stp", "x2,x1,[x0]"),
                ("str", "w3,[x0,#24]"), ("str", "x19,[x0,#88]")]
    for (_, op, operands), (wanted_op, wanted_args) in zip(body, expected):
        require(op == wanted_op and
                (wanted_args is None or operands == wanted_args),
                "unrecognized shrinker callback register/store sequence")
    require(body[1][2].startswith("x0,"), "unexpected allocation failure test")
    require(body[2][2].startswith("x2,") and
            body[3][2].startswith("x1,"), "unexpected callback registers")
    offsets = {}
    for name, high, low in (("count", body[2], body[4]),
                            ("scan", body[3], body[5])):
        high_reloc = relocs.get(high[0])
        low_reloc = relocs.get(low[0])
        require(high_reloc is not None and low_reloc is not None,
                f"missing {name} callback relocation")
        require(high_reloc[0] == "R_AARCH64_ADR_PREL_PG_HI21" and
                low_reloc[0] == "R_AARCH64_ADD_ABS_LO12_NC" and
                high_reloc[1] == low_reloc[1],
                f"inconsistent {name} callback relocations")
        match = re.fullmatch(r"\.text\+0x([0-9a-f]+)", high_reloc[1])
        require(match is not None, f"{name} callback is not .text relative")
        offsets[name] = int(match[1], 16)
    require(offsets["count"] != offsets["scan"], "callbacks unexpectedly alias")
    return {"offsets": offsets,
            "allocation_call_offset": hex(insns[first][0]),
            "registration_call_offset": hex(insns[last][0]),
            "store_offset": hex(body[7][0]),
            "recognized_store": "stp x2, x1, [x0]",
            "count_register": "x2", "scan_register": "x1"}


def kernel_symbol_display(nm_text, kallsyms_text, base, offset):
    """Explain %pS's nearest retained symbol, not a function-body attribution."""
    address = base + offset
    loaded = [(int(value, 16), kind, name) for value, kind, name in
              re.findall(r"^([0-9a-f]+) (\w) (\S+)\s+\[nvidia\]$",
                         kallsyms_text, re.M)
              if not name.startswith(("$", ".L", "L0"))]
    before = [value for value, _, _ in loaded if value <= address]
    after = [value for value, _, _ in loaded if value > address]
    require(before and after, "callback has no bounded loaded symbol interval")
    start, end = max(before), min(after)
    predecessors = [(kind, name) for value, kind, name in loaded
                    if value == start]
    successors = [(kind, name) for value, kind, name in loaded if value == end]
    require(len(predecessors) == len(successors) == 1,
            "ambiguous aliases around callback in loaded kallsyms")
    previous_type, previous_name = predecessors[0]
    next_type, next_name = successors[0]
    require(previous_type.lower() == next_type.lower() == "t",
            "callback does not lie between retained text symbols")
    symbol_sizes = {}
    for name, absolute in ((previous_name, start), (next_name, end)):
        matches = re.findall(rf"^([0-9a-f]+) ([0-9a-f]+) [tT] "
                             rf"{re.escape(name)}$", nm_text, re.M)
        require(len(matches) == 1 and
                int(matches[0][0], 16) + base == absolute,
                "loaded nearest symbol differs from ELF: " + name)
        symbol_sizes[name] = int(matches[0][1], 16)
    relative, span = address - start, end - start
    return {
        "symbol": previous_name, "offset": hex(relative),
        "display_size": hex(span), "module": "nvidia",
        "text": f"{previous_name}+{hex(relative)}/{hex(span)} [nvidia]",
        "symbol_address": hex(start), "resolved_address": hex(address),
        "next_symbol": next_name, "next_symbol_address": hex(end),
        "elf_symbol_size": symbol_sizes[previous_name],
        "outside_elf_symbol_body": relative >= symbol_sizes[previous_name],
        "ignored_mapping_symbol_prefixes": ["$", ".L", "L0"],
        "meaning": "nearest retained symbol plus offset; not function ownership",
    }


def collect(evidence, result):
    kernel = evidence.run("uname", "-r").strip()
    require(evidence.run("uname", "-m").strip() == "aarch64",
            "recognizer only supports the reviewed AArch64 ABI")
    result["kernel"] = {
        "release": kernel, "uname": evidence.run("uname", "-a").strip(),
        "proc_version": evidence.run("cat", "/proc/version").strip(),
        "boot_id": evidence.run("cat", "/proc/sys/kernel/random/boot_id").strip(),
        "tainted": evidence.run("cat", "/proc/sys/kernel/tainted").strip(),
        "page_size": os.sysconf("SC_PAGE_SIZE"),
    }
    boot_image = Path("/boot/Image")
    if boot_image.is_file():
        result["kernel"]["on_disk_boot_image"] = {
            "path": str(boot_image), "sha256": sha256(boot_image),
            "loaded_image_identity_proven": False,
        }
    module = Path(evidence.run("modinfo", "-F", "filename", "nvidia").strip())
    require(module.is_file() and module.suffix == ".ko",
            "expected an uncompressed NVIDIA ELF module")
    module_hash = sha256(module)
    srcversion = evidence.run("modinfo", "-F", "srcversion", str(module)).strip()
    loaded_src = evidence.protected_read("/sys/module/nvidia/srcversion").strip()
    require(srcversion and srcversion == loaded_src,
            "on-disk and loaded module srcversion differ")
    base_raw = evidence.protected_read("/sys/module/nvidia/sections/.text").strip()
    require(re.fullmatch(r"0x[0-9a-fA-F]+", base_raw), "invalid .text base")
    base = int(base_raw, 16)
    require(base > (1 << 63), "hidden or invalid kernel .text base")
    prefix = [] if os.geteuid() == 0 else ["sudo", "-n"]
    loaded_note = evidence.run(*prefix, "od", "-An", "-tx1",
                               "/sys/module/nvidia/notes/.note.gnu.build-id")
    loaded_build_id = note_build_id(loaded_note)
    elf_notes = evidence.run("readelf", "-n", str(module))
    build_ids = re.findall(r"Build ID: ([0-9a-f]+)", elf_notes)
    require(build_ids == [loaded_build_id], "loaded and disk build-id mismatch")
    result["module"] = {
        "path": str(module), "sha256": module_hash,
        "srcversion": srcversion, "loaded_srcversion": loaded_src,
        "build_id": loaded_build_id, "text_base": hex(base),
        "driver_version": evidence.protected_read("/proc/driver/nvidia/version"),
        "params": evidence.protected_read("/proc/driver/nvidia/params"),
    }
    header_root = Path(f"/lib/modules/{kernel}/build")
    header = header_root / "include/linux/shrinker.h"
    header_release = evidence.run("cat", str(header_root /
                                             "include/config/kernel.release"))
    require(header_release.strip() == kernel, "kernel header release mismatch")
    header_text = evidence.run("cat", str(header))
    without_comments = re.sub(r"/\*.*?\*/", "", header_text, flags=re.S)
    require(re.search(r"struct shrinker\s*\{\s*"
                      r"unsigned long\s*\(\*count_objects\)\([^;]+;\s*"
                      r"unsigned long\s*\(\*scan_objects\)\([^;]+;",
                      without_comments), "unrecognized shrinker callback ABI")
    result["shrinker_abi"] = {
        "header_path": str(header.resolve()), "sha256": sha256(header),
        "release": header_release.strip(),
        "first_two_fields": ["count_objects", "scan_objects"],
        "pointer_bytes": 8,
    }
    mapping_header = header_root / "include/linux/module_symbol.h"
    mapping_text = evidence.run("cat", str(mapping_header))
    mapping_code = re.sub(r"/\*.*?\*/", "", mapping_text, flags=re.S)
    mapping_code = re.sub(r"\s+", "", mapping_code)
    mapping_rule = ("staticinlineboolis_mapping_symbol(constchar*str){"
                    "if(str[0]=='.'&&str[1]=='L')returntrue;"
                    "if(str[0]=='L'&&str[1]=='0')returntrue;"
                    "returnstr[0]=='$';}")
    require(mapping_rule in mapping_code, "unrecognized kernel mapping-symbol rule")
    result["symbol_mapping_abi"] = {
        "header_path": str(mapping_header.resolve()),
        "sha256": sha256(mapping_header),
        "ignored_name_prefixes": ["$", ".L", "L0"],
    }
    nm_text = evidence.run("nm", "-S", "--defined-only", str(module))
    symbols = {}
    for name in SYMBOLS:
        matches = re.findall(rf"^([0-9a-f]+) ([0-9a-f]+) T {name}$",
                             nm_text, re.M)
        require(len(matches) == 1, f"missing or ambiguous symbol: {name}")
        symbols[name] = {"offset": int(matches[0][0], 16),
                         "size": int(matches[0][1], 16)}
    # grep is available in sudo's fixed PATH, unlike the user's rg installation.
    kallsyms = evidence.run(*prefix, "grep", "-E",
                            r"\[nvidia\]$", "/proc/kallsyms")
    for name, symbol in symbols.items():
        matches = re.findall(rf"^([0-9a-f]+) [tT] {name}\s+\[nvidia\]$",
                             kallsyms, re.M)
        require(len(matches) == 1 and
                int(matches[0], 16) == base + symbol["offset"],
                f"loaded address does not match module .text: {name}")
        symbol["loaded_address"] = matches[0]
    result["named_function_crosschecks"] = symbols
    init_text = evidence.run("objdump", "-dr", "--disassemble=nv_mem_pool_init",
                             str(module))
    parsed = extract_callbacks(init_text)
    result["callback_extraction"] = parsed
    result["callbacks"] = {}
    for name, offset in parsed["offsets"].items():
        require(0 < offset < symbols["nv_mem_pool_init"]["offset"],
                "callback is outside reviewed pre-init .text region")
        disassembly = evidence.run("objdump", "-dr",
                                    f"--start-address={hex(offset)}",
                                    f"--stop-address={hex(offset + 32)}",
                                    str(module))
        instructions, _ = parse_instructions(disassembly)
        require(instructions[0][:2] == (offset, "paciasp"),
                f"{name} callback lacks reviewed function-entry instruction")
        result["callbacks"][name] = {
            "text_offset": hex(offset), "address": hex(base + offset),
            "address_decimal": base + offset,
            "kernel_symbol": kernel_symbol_display(
                nm_text, kallsyms, base, offset),
        }
    require(evidence.protected_read("/sys/module/nvidia/sections/.text").strip()
            == base_raw, "module base changed during capture")
    require(evidence.protected_read("/sys/module/nvidia/srcversion").strip()
            == loaded_src, "loaded module changed during capture")
    require(note_build_id(evidence.run(
        *prefix, "od", "-An", "-tx1",
        "/sys/module/nvidia/notes/.note.gnu.build-id")) == loaded_build_id,
        "loaded build-id changed during capture")
    require(sha256(module) == module_hash, "module file changed during capture")
    result["attribution_valid"] = True
    result["limitations"] = [
        "Callback addresses are valid only for this module load and boot.",
        "mm_shrink_slab start/end expose the scan callback, not pool order.",
        "Shrinker object counts must not be converted to bytes without order.",
        "No live pool count, pool order, or exact lock holder is read here.",
        "On-disk boot Image hash alone does not prove loaded kernel identity.",
        "%pS may use an unrelated preceding symbol when static names are stripped.",
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path,
                        help="new output directory inside this repo's .q4t-work")
    args = parser.parse_args()
    output = args.output.resolve()
    work = (ROOT / ".q4t-work").resolve()
    require(output.is_relative_to(work) and output != work,
            "output must be a child of repository .q4t-work")
    output.mkdir(parents=True, exist_ok=True)
    target = output / "driver_identity.json"
    require(not target.exists(), "refusing to overwrite identity evidence")
    evidence = Evidence()
    result = {"schema": "q4t.warm_reclaim.driver_identity.v1",
              "status": "FAILED", "attribution_valid": False,
              "created_utc": datetime.datetime.now(
                  datetime.timezone.utc).isoformat(),
              "script_sha256": sha256(__file__)}
    try:
        collect(evidence, result)
        result["status"] = "PASS"
    except (OSError, ValueError, RuntimeError) as error:
        result["error"] = f"{type(error).__name__}: {error}"
        result["attribution_valid"] = False
    result["raw_commands"] = evidence.commands
    with target.open("x") as destination:
        json.dump(result, destination, ensure_ascii=False, indent=2)
        destination.write("\n")
    print(json.dumps({"status": result["status"], "output": str(target),
                      "error": result.get("error")}, ensure_ascii=False))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
