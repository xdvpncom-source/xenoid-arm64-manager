#!/usr/bin/env python3
"""Convert saved `ninja -t commands` output into bounded read-only evidence.

This never executes the saved shell commands. Run Ninja's read-only tool for
the exact Soong outputs being built, then provide its stdout with --input.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shlex


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def capture(root, source):
    root = root.resolve(strict=True)
    response_files, rows = {}, []

    def expand(argv, seen=()):
        result = []
        for arg in argv:
            if not arg.startswith("@"):
                result.append(arg)
                continue
            name = arg[1:]
            path = Path(name)
            if not path.is_absolute():
                path = root / path
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(root) or resolved in seen or len(seen) > 8:
                raise ValueError("unsafe/recursive response file")
            if resolved.stat().st_size > 64 * 1024 * 1024:
                raise ValueError("response file exceeds evidence limit")
            response_files[str(resolved.relative_to(root))] = digest(resolved)
            result += expand(shlex.split(resolved.read_text()), (*seen, resolved))
        return result

    for line in source.read_text().splitlines():
        # Ninja -t commands has one shell command per line. Select each actual
        # compiler invocation, ignoring directory creation and cleanup clauses.
        lexer = shlex.shlex(line, posix=True, punctuation_chars=";&|()")
        lexer.whitespace_split = True
        lexer.commenters = ""
        segments, current = [], []
        for word in lexer:
            if word in {"&&", "||", ";", "|", "(", ")"}:
                segments.append(current)
                current = []
            else:
                current.append(word)
        segments.append(current)
        for segment in segments:
            # Rust's -C linker=.../clang++ is an option, not another invocation.
            indices = [i for i, arg in enumerate(segment)
                       if not arg.startswith("-") and "=" not in arg
                       and re.search(r"/(?:clang(?:\+\+)?|rustc)$", arg)]
            if not indices:
                continue
            if len(indices) != 1:
                raise ValueError("ambiguous compiler invocation")
            argv = expand(segment[indices[0]:])
            if "-o" not in argv:
                continue
            output = argv[argv.index("-o") + 1]
            if not any("/" + module + "/" in output for module in [
                    "libbt-rootcanal", "liblmp", "android.hardware.bluetooth@1.1-service.sim",
                    "android.hardware.bluetooth@1.1-impl-sim"]):
                continue
            rows.append(dict(argv=argv, raw_command_sha256=hashlib.sha256(line.encode()).hexdigest()))
    if not rows:
        raise ValueError("no selected target compiler commands")
    return dict(origin="expanded_target_ninja_commands", commands=rows,
                input_sha256=digest(source), response_files=response_files)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source-root", type=Path, required=True)
    p.add_argument("--input", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    result = capture(args.source_root, args.input)
    with args.output.open("x") as stream:
        json.dump(result, stream, sort_keys=True)
        stream.write("\n")
    print(json.dumps(dict(commands=len(result["commands"]), response_files=len(result["response_files"]),
                          output_sha256=digest(args.output))))


if __name__ == "__main__":
    main()
