"""Bounded read-only project records. Model input is never a command or URL."""
import hashlib
import os
import re
from pathlib import Path, PurePosixPath
import stat

from chat_client import require


MAX_FILE = 1024 * 1024
MAX_SEARCH_BYTES = 8 * 1024 * 1024
MAX_SEARCH_FILES = 256
TOOLS = [
    {"type": "function", "function": {
        "name": "read_project_record",
        "description": "Read real project documentation or acceptance JSON under "
            "docs/, or the installed release metadata build/q4t.release.json. "
            "Returns numbered lines, a source SHA256 and an exact citation. "
            "Use the returned citation verbatim in the answer.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "start_line": {"type": "integer"},
            "line_count": {"type": "integer"}},
            "required": ["path", "start_line", "line_count"],
            "additionalProperties": False}}},
    {"type": "function", "function": {
        "name": "search_project_docs",
        "description": "Search a literal phrase in project Markdown documents. "
            "Bounded results; a truncated search is not proof of absence. "
            "Use read_project_record for surrounding context or JSON records.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}}, "required": ["query"],
            "additionalProperties": False}}}]


def validate_read(args):
    require(set(args) == {"path", "start_line", "line_count"} and
            isinstance(args["path"], str) and 0 < len(args["path"]) <= 512,
            "read_project_record expects path/start_line/line_count")
    require(type(args["start_line"]) is int and
            1 <= args["start_line"] <= 1000000 and
            type(args["line_count"]) is int and 1 <= args["line_count"] <= 80,
            "read range must be 1-based and at most 80 lines")


def validate_search(args):
    require(set(args) == {"query"} and isinstance(args["query"], str) and
            1 <= len(args["query"].strip()) <= 128 and
            "\n" not in args["query"], "search expects a literal 1..128 query")


VALIDATORS = {"read_project_record": validate_read,
              "search_project_docs": validate_search}


CUSTOM_READ_NAMES = {"read_project_record_text", "read_project_record_regex",
                     "read_project_record_lark"}


def parse_custom_read(value):
    require(isinstance(value, str) and len(value) <= 1024, "custom read input must be bounded text")
    match = re.fullmatch(r"READ (docs/[A-Za-z0-9_./-]+\.(?:md|json)) ([1-9][0-9]{0,5}) ([1-9][0-9]?)", value)
    require(match is not None, "expected READ path start_line line_count")
    args = dict(path=match[1], start_line=int(match[2]), line_count=int(match[3]))
    validate_read(args)
    return args


def validate_custom_read(value):
    parse_custom_read(value)


CUSTOM_VALIDATORS = {name: validate_custom_read for name in CUSTOM_READ_NAMES}


class RecordError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class ProjectRecords:
    def __init__(self, root):
        self.root = Path(root).resolve(strict=True)

    def _read(self, name):
        path = PurePosixPath(name)
        parts = name.split("/")
        if (path.is_absolute() or any(not p or p.startswith(".") for p in parts)
                or "\\" in name or "\x00" in name or
                not (name == "build/q4t.release.json" or
                     (parts[0] == "docs" and path.suffix in {".md", ".json"}))):
            raise RecordError("path_not_allowed")
        descriptors = []
        try:
            descriptors.append(os.open(self.root, os.O_RDONLY | os.O_DIRECTORY))
            for part in parts[:-1]:
                descriptors.append(os.open(part, os.O_RDONLY | os.O_DIRECTORY |
                    os.O_NOFOLLOW, dir_fd=descriptors[-1]))
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=descriptors[-1])
            descriptors.append(fd)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise RecordError("not_regular_file")
            if info.st_size > MAX_FILE:
                raise RecordError("file_too_large")
            chunks, size = [], 0
            while True:
                data = os.read(fd, min(65536, MAX_FILE + 1 - size))
                if not data:
                    break
                chunks.append(data)
                size += len(data)
                if size > MAX_FILE:
                    raise RecordError("file_too_large")
            data = b"".join(chunks)
            return data, data.decode("utf-8").splitlines()
        except FileNotFoundError as error:
            raise RecordError("record_not_found") from error
        except UnicodeError as error:
            raise RecordError("record_not_utf8") from error
        except OSError as error:
            raise RecordError("record_unavailable") from error
        finally:
            for fd in reversed(descriptors):
                os.close(fd)

    @staticmethod
    def _excerpt(name, data, lines, start, count):
        end = min(len(lines), start + count - 1)
        # Bound unusually long lines as well as the number of lines. A clipped
        # excerpt is marked; the SHA identifies the complete bytes actually read.
        selected = lines[start - 1:end]
        text = "\n".join(f"{n}: {line}" for n, line in
                         enumerate(selected, start))
        return dict(path=name, sha256=hashlib.sha256(data).hexdigest(),
                    start_line=start, end_line=end,
                    citation=f"[{name}:L{start}-L{end}]",
                    text=text[:12000], truncated=len(text) > 12000)

    def read(self, args):
        validate_read(args)
        data, lines = self._read(args["path"])
        if not lines or args["start_line"] > len(lines):
            raise RecordError("line_out_of_range")
        return dict(ok=True, source=self._excerpt(args["path"], data, lines,
                    args["start_line"], args["line_count"]), total_lines=len(lines))

    def search(self, args):
        validate_search(args)
        query = args["query"].strip().casefold()
        # Top-level current docs first, then other Markdown (never raw wires,
        # weights, .q4t-work, or the reference tree). No symlink traversal.
        names = []
        for directory, dirs, files in os.walk(self.root / "docs",
                                              followlinks=False):
            dirs[:] = sorted(d for d in dirs if not d.startswith(".") and
                              not (Path(directory) / d).is_symlink())
            for file in sorted(files):
                if file.endswith(".md") and not file.startswith("."):
                    names.append((Path(directory) / file).relative_to(
                        self.root).as_posix())
                    if len(names) > MAX_SEARCH_FILES:
                        break
            if len(names) > MAX_SEARCH_FILES:
                break
        used = scanned = skipped = 0
        results = []
        truncated = len(names) > MAX_SEARCH_FILES
        for name in names[:MAX_SEARCH_FILES]:
            try:
                data, lines = self._read(name)
            except RecordError:
                skipped += 1
                continue
            if used + len(data) > MAX_SEARCH_BYTES:
                truncated = True
                break
            used += len(data)
            scanned += 1
            for index, line in enumerate(lines):
                if query in line.casefold():
                    results.append(self._excerpt(name, data, lines,
                                                  max(1, index), 4))
                    break
            if len(results) == 6:
                truncated = True
                break
        return dict(ok=True, matches=results, scanned_files=scanned,
                    skipped_files=skipped, truncated=truncated,
                    scope="bounded docs/*.md search; not an absence proof")

    def execute(self, invocation):
        # Programming/validation failures propagate; expected source access
        # failures are business tool results the model can report to its caller.
        if getattr(invocation, "type", None) == "custom":
            require(invocation.name in CUSTOM_READ_NAMES, "unknown custom project tool")
            args = parse_custom_read(invocation.input)
            try:
                return self.read(args)
            except RecordError as error:
                return dict(ok=False, error=dict(code=error.code,
                    message="Project record could not be read."))
        method = {"read_project_record": self.read,
                  "search_project_docs": self.search}.get(invocation.name)
        require(method is not None, "unknown project tool")
        try:
            return method(invocation.arguments)
        except RecordError as error:
            return dict(ok=False, error=dict(code=error.code,
                        message="Project record could not be read."))
