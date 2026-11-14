#!/usr/bin/env python3
"""PreToolUse guard for sci-fast-io v2.0.0.

Enforces tiered byte/line-range limits on scientific data & notebook reads
and blocks high-confidence catastrophic unfiltered recursive grep scans.
Compatible with Python 3.9+ stdlib only; always emits valid JSON on stdout.
"""
from __future__ import annotations

import json
import os
import select
import shlex
import stat
import sys
from typing import Any, List, Optional, Tuple

SMALL_TEXT_LIMIT = 32 * 1024          # 32 KiB: unconditional allow for text data
LARGE_TEXT_HARD_LIMIT = 2 * 1024 * 1024  # 2 MiB: deny raw read_file/view_file
NOTEBOOK_DIRECT_LIMIT = 32 * 1024     # 32 KiB: larger .ipynb may hold huge single-line base64
MAX_BOUNDED_LINES = 80                # max line span allowed for 32 KiB - 2 MiB text data
LONG_LINE_THRESHOLD = 8192            # 8 KiB without newline => wide matrix table

COMPOUND_BINARY_SUFFIXES: Tuple[str, ...] = (
    ".nii.gz",
    ".fastq.gz",
    ".fq.gz",
    ".fasta.gz",
    ".fa.gz",
    ".fna.gz",
    ".vcf.gz",
    ".bcf.gz",
    ".csv.gz",
    ".tsv.gz",
    ".txt.gz",
    ".bed.gz",
    ".gtf.gz",
    ".gff.gz",
    ".gff3.gz",
    ".pdb.gz",
    ".cif.gz",
    ".mmcif.gz",
    ".sdf.gz",
    ".tar.gz",
    ".tar.bz2",
    ".tar.xz",
    ".zarr.zip",
)

BINARY_EXTS = frozenset({
    # NumPy / ML checkpoints / serialized models
    ".npy", ".npz", ".pt", ".pth", ".ckpt", ".pkl", ".pickle", ".joblib",
    ".safetensors", ".onnx", ".bin",
    # Columnar / scientific containers / R & MATLAB
    ".parquet", ".pq", ".feather", ".arrow", ".ipc",
    ".h5", ".hdf5", ".he5", ".h5ad", ".loom", ".mat", ".rds", ".rdata", ".rda",
    ".duckdb", ".sqlite", ".sqlite3", ".db",
    # Bioinformatics / medical imaging / compressed archives
    ".bam", ".cram", ".bai", ".crai", ".csi", ".tbi", ".bcf",
    ".bigwig", ".bw", ".bigbed", ".bb", ".2bit",
    ".nii", ".mha", ".mhd", ".nrrd", ".dcm",
    ".gz", ".bz2", ".xz", ".zst", ".zip", ".7z", ".tar",
})

SCI_TEXT_EXTS = frozenset({
    ".csv", ".tsv", ".tab",
    ".vcf", ".fastq", ".fq", ".fasta", ".fa", ".fna", ".faa",
    ".bed", ".gtf", ".gff", ".gff3", ".sam",
    ".pdb", ".cif", ".mmcif", ".sdf", ".mol2", ".smi", ".xyz", ".gro",
})

GREP_HAZARD_DIRS = frozenset({
    ".venv", "venv", ".git", "node_modules",
    "wandb", "lightning_logs", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache",
})

GREP_ARG_TAKING_SHORT_OPTS = frozenset({"e", "f", "m", "A", "B", "C", "D", "d"})
GREP_ARG_TAKING_LONG_OPTS = frozenset({
    "--regexp", "--file", "--max-count",
    "--after-context", "--before-context", "--context",
    "--devices", "--directories", "--binary-files",
    "--color", "--colour", "--label",
    "--include", "--exclude", "--exclude-from", "--exclude-dir",
})


def _emit(decision: str, reason: Optional[str] = None) -> None:
    out: dict[str, Any] = {"decision": decision}
    if reason:
        out["reason"] = reason
    log_path = os.environ.get("SCI_FAST_IO_GUARD_LOG")
    if log_path:
        try:
            with open(log_path, "a", encoding="utf-8") as lf:
                lf.write(json.dumps(out, ensure_ascii=False) + "\n")
        except OSError:
            pass
    sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _unwrap_str(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, str):
        s = val.strip()
        if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
            try:
                inner = json.loads(s)
                if isinstance(inner, str):
                    return inner
            except Exception:
                pass
        return val
    return str(val)


def _unwrap_int(val: Any) -> Optional[int]:
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return int(val)
    if isinstance(val, str):
        s = _unwrap_str(val).strip()
        if not s:
            return None
        try:
            return int(s)
        except ValueError:
            return None
    return None


def _resolve_target_path(raw_path: str, workspace_paths: List[str]) -> Optional[str]:
    expanded = os.path.expanduser(raw_path)
    if os.path.isabs(expanded):
        return os.path.normpath(expanded)
    for wp in workspace_paths:
        wp_str = _unwrap_str(wp)
        if wp_str and os.path.isabs(wp_str):
            return os.path.normpath(os.path.join(wp_str, expanded))
    return None


def _has_zarr_ancestor(path_str: str) -> bool:
    parts = [p for p in path_str.split(os.sep) if p]
    return any(p.lower().endswith(".zarr") for p in parts)


def _match_binary_ext(filename: str) -> Optional[str]:
    lower = filename.lower()
    for comp in COMPOUND_BINARY_SUFFIXES:
        if lower.endswith(comp):
            return comp
    _, ext = os.path.splitext(lower)
    if ext in BINARY_EXTS:
        return ext
    return None


def _has_ultra_long_lines(filepath: str) -> bool:
    try:
        with open(filepath, "rb") as f:
            chunk = f.read(65536)
        if not chunk:
            return False
        for segment in chunk.split(b"\n"):
            if len(segment) > LONG_LINE_THRESHOLD:
                return True
    except OSError:
        return False
    return False


def _check_file_read(tool_name: str, args: dict[str, Any], workspace_paths: List[str]) -> Tuple[str, Optional[str]]:
    raw_path = (
        _unwrap_str(args.get("Path"))
        or _unwrap_str(args.get("AbsolutePath"))
        or _unwrap_str(args.get("file_path"))
        or _unwrap_str(args.get("path"))
    )
    if not raw_path:
        return "allow", None

    resolved = _resolve_target_path(raw_path, workspace_paths)
    if not resolved:
        return "allow", None

    if _has_zarr_ancestor(resolved):
        return (
            "deny",
            f"目录型数组数据 (.zarr) 禁止直接通过 {tool_name} 文本读取；请使用单次 Python/zarr 脚本探查结构与 shape（参考 sci-eda 技能）。",
        )

    # Check extension on both requested filename and symlink target filename
    names_to_check = [os.path.basename(resolved)]
    try:
        real_resolved = os.path.realpath(resolved)
        if _has_zarr_ancestor(real_resolved):
            return (
                "deny",
                f"符号链接指向 .zarr 数组目录，禁止通过 {tool_name} 直接读取；请使用 Python 脚本探查。",
            )
        real_base = os.path.basename(real_resolved)
        if real_base and real_base not in names_to_check:
            names_to_check.append(real_base)
    except OSError:
        real_resolved = resolved

    for fname in names_to_check:
        bin_ext = _match_binary_ext(fname)
        if bin_ext:
            return (
                "deny",
                f"二进制或压缩科研文件 ({bin_ext}) 禁止使用 {tool_name} 直接读取；请合并进单次 Python/R 脚本探查 shape、dtype 与摘要统计（参考 sci-eda 技能）。",
            )

    try:
        st = os.stat(resolved)
    except OSError:
        # Missing file or inaccessible path: let the tool return its native error
        return "allow", None

    if stat.S_ISDIR(st.st_mode):
        return "allow", None

    size_bytes = st.st_size
    ext = os.path.splitext(names_to_check[0].lower())[1]

    # Special handling for Jupyter Notebooks (.ipynb) by byte size, not line count
    if ext == ".ipynb":
        if size_bytes <= NOTEBOOK_DIRECT_LIMIT:
            return "allow", None
        size_kb = size_bytes / 1024.0
        if size_bytes < 5 * 1024 * 1024:
            return (
                "deny",
                f"Notebook 文件较大 ({size_kb:.1f} KiB > 32 KiB)，常含单行大体积 base64 图像输出，禁止直接 {tool_name} 全文读取。可用免审 jq 仅提取代码/文本并排除 outputs，或用 Python 脚本按 cell 索引提取（参考 sci-eda 技能）。",
            )
        size_mb = size_bytes / (1024.0 * 1024.0)
        return (
            "deny",
            f"超大型 Notebook ({size_mb:.2f} MiB >= 5 MiB) 禁止直接 {tool_name} 或全量 jq 解析；请使用单次 Python 脚本仅读取元信息与指定 cell 的 source（参考 sci-eda 技能）。",
        )

    # Tiered gate for scientific text data (.csv, .tsv, .vcf, .fasta, .pdb, etc.)
    if ext in SCI_TEXT_EXTS:
        if size_bytes <= SMALL_TEXT_LIMIT:
            return "allow", None

        if size_bytes >= LARGE_TEXT_HARD_LIMIT:
            size_mb = size_bytes / (1024.0 * 1024.0)
            return (
                "deny",
                f"大型科研数据文件 ({size_mb:.2f} MiB >= 2 MiB) 禁止直接 {tool_name}。请改用免审命令 ls -lh / head -n 5 / wc -l 查看表头与规模，或用单次 Python/Polars 脚本探查（参考 sci-eda 技能）。",
            )

        # 32 KiB < size < 2 MiB: allow bounded line range (<= 80 lines) if lines aren't ultra-wide
        start_line = _unwrap_int(args.get("StartLine"))
        end_line = _unwrap_int(args.get("EndLine"))
        if (
            start_line is not None
            and end_line is not None
            and start_line >= 1
            and end_line >= start_line
            and (end_line - start_line + 1) <= MAX_BOUNDED_LINES
        ):
            if _has_ultra_long_lines(resolved):
                return (
                    "deny",
                    "检测到超宽单行数据表（单行 > 8 KiB），按行读取仍会撑爆上下文；请改用单次 Python/Polars 脚本查看列名数量与前几列摘要（参考 sci-eda 技能）。",
                )
            return "allow", None

        size_kb = size_bytes / 1024.0
        return (
            "deny",
            f"中等体积科研文本文件 ({size_kb:.1f} KiB > 32 KiB) 禁止无范围整份读取。请显式传入 StartLine 与 EndLine（跨度 <= {MAX_BOUNDED_LINES} 行），或使用免审命令 head -n 5 查看表头。",
        )

    return "allow", None


def _split_simple_pipeline(cmd: str) -> Optional[List[str]]:
    """Return pipeline stages if command is a simple single/piped command without &&, ||, ;, subshells, or newlines."""
    if "\n" in cmd or "\r" in cmd or "`" in cmd or "$(" in cmd:
        return None
    stages: List[str] = []
    buf: List[str] = []
    in_single = False
    in_double = False
    escaped = False
    i = 0
    n = len(cmd)
    while i < n:
        ch = cmd[i]
        if escaped:
            buf.append(ch)
            escaped = False
            i += 1
            continue
        if ch == "\\" and not in_single:
            buf.append(ch)
            escaped = True
            i += 1
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
            buf.append(ch)
            i += 1
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            buf.append(ch)
            i += 1
            continue
        if not in_single and not in_double:
            if ch == ";":
                return None
            if ch == "&":
                return None
            if ch == "|":
                if i + 1 < n and cmd[i + 1] == "|":
                    return None
                stages.append("".join(buf).strip())
                buf = []
                i += 1
                continue
        buf.append(ch)
        i += 1
    if in_single or in_double:
        return None
    stages.append("".join(buf).strip())
    return [s for s in stages if s]


def _inspect_grep_stage(tokens: List[str], is_piped_stdin: bool, workspace_paths: List[str]) -> Optional[str]:
    if not tokens:
        return None
    exe = os.path.basename(tokens[0])
    if exe not in ("grep", "egrep", "fgrep"):
        return None

    is_recursive = False
    has_include = False
    has_exclude_dir = False
    explicit_pattern = False
    positional: List[str] = []
    after_double_dash = False

    i = 1
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if after_double_dash:
            positional.append(tok)
            i += 1
            continue
        if tok == "--":
            after_double_dash = True
            i += 1
            continue
        if tok.startswith("--"):
            opt_name = tok.split("=", 1)[0]
            if opt_name in ("--recursive", "--dereference-recursive"):
                is_recursive = True
            elif opt_name == "--include":
                has_include = True
            elif opt_name in ("--exclude-dir", "--exclude-from"):
                has_exclude_dir = True
            elif opt_name in ("--regexp", "--file"):
                explicit_pattern = True
            if "=" not in tok and opt_name in GREP_ARG_TAKING_LONG_OPTS:
                i += 2
                continue
            i += 1
            continue
        if tok.startswith("-") and len(tok) > 1:
            chars = tok[1:]
            j = 0
            consumed_next = False
            while j < len(chars):
                c = chars[j]
                if c in ("r", "R"):
                    is_recursive = True
                if c in GREP_ARG_TAKING_SHORT_OPTS:
                    if c in ("e", "f"):
                        explicit_pattern = True
                    # If nothing remains in this cluster (e.g. -e 'pat'), next token is its argument
                    if j == len(chars) - 1:
                        i += 2
                        consumed_next = True
                    break
                j += 1
            if not consumed_next:
                i += 1
            continue

        positional.append(tok)
        i += 1

    if not is_recursive:
        return None

    target_paths = positional if explicit_pattern else positional[1:]
    if not target_paths:
        if is_piped_stdin:
            return None
        target_paths = ["."]

    norm_workspaces = {
        os.path.normpath(_unwrap_str(wp))
        for wp in workspace_paths
        if _unwrap_str(wp) and os.path.isabs(_unwrap_str(wp))
    }

    for tp in target_paths:
        cleaned = tp.rstrip("/") or "/"
        base = os.path.basename(cleaned)
        parts = [p for p in cleaned.split("/") if p and p != "."]

        # Hazard 1: direct recursive grep into virtualenvs, caches, or heavy training logs
        if base in GREP_HAZARD_DIRS or any(p in GREP_HAZARD_DIRS for p in parts):
            return (
                f"禁止对虚拟环境、缓存或训练日志目录 ({tp}) 执行递归 grep；请限定到具体源码目录或单个日志文件。"
            )

        # Hazard 2: unfiltered recursive grep across '.' or the entire workspace root
        is_root_target = cleaned in (".", "./") or (
            os.path.isabs(cleaned) and os.path.normpath(cleaned) in norm_workspaces
        )
        if is_root_target and not has_include and not has_exclude_dir:
            return (
                "禁止对工作区根目录执行无过滤递归 grep（易扫入 .venv/数据/缓存目录引发 I/O 卡死）。"
                "请显式指定源码/文稿子目录（如 'src' 'scripts' 'paper'），或添加 --include='*.py' 与 --exclude-dir='.venv'。"
            )

    return None


def _check_run_command(args: dict[str, Any], workspace_paths: List[str]) -> Tuple[str, Optional[str]]:
    cmd = _unwrap_str(args.get("CommandLine")) or _unwrap_str(args.get("command"))
    if not cmd or "grep" not in cmd:
        return "allow", None

    stages = _split_simple_pipeline(cmd)
    if not stages:
        # Complex compound command: do not guess or rewrite
        return "allow", None

    for idx, stage in enumerate(stages):
        try:
            tokens = shlex.split(stage, posix=True)
        except ValueError:
            continue
        deny_reason = _inspect_grep_stage(tokens, is_piped_stdin=(idx > 0), workspace_paths=workspace_paths)
        if deny_reason:
            return "deny", deny_reason

    return "allow", None


def _read_stdin_json(max_wait_sec: float = 0.25) -> Optional[dict[str, Any]]:
    """Read JSON object from fd 0 without blocking on unclosed stdin pipe."""
    chunks: List[bytes] = []
    deadline_step = 0.05
    waited = 0.0
    while waited < max_wait_sec:
        rlist, _, _ = select.select([0], [], [], deadline_step)
        if not rlist:
            if chunks:
                break
            waited += deadline_step
            continue
        data = os.read(0, 65536)
        if not data:
            break
        chunks.append(data)
        raw_bytes = b"".join(chunks).strip()
        if raw_bytes.endswith(b"}"):
            try:
                parsed = json.loads(raw_bytes.decode("utf-8", errors="replace"))
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                pass
    if not chunks:
        return None
    raw_text = b"".join(chunks).decode("utf-8", errors="replace").strip()
    if not raw_text:
        return None
    parsed = json.loads(raw_text)
    return parsed if isinstance(parsed, dict) else None


def main() -> None:
    try:
        payload = _read_stdin_json()
        if payload is None:
            _emit("allow")
            return

        tool_call = payload.get("toolCall")
        if not isinstance(tool_call, dict):
            _emit("allow")
            return

        tool_name = _unwrap_str(tool_call.get("name")).lower()
        args = tool_call.get("args")
        if not isinstance(args, dict):
            _emit("allow")
            return

        workspace_paths = payload.get("workspacePaths")
        if not isinstance(workspace_paths, list):
            workspace_paths = []

        if tool_name in ("read_file", "view_file", "view_file_range"):
            decision, reason = _check_file_read(tool_name, args, workspace_paths)
            _emit(decision, reason)
            return

        if tool_name == "run_command":
            decision, reason = _check_run_command(args, workspace_paths)
            _emit(decision, reason)
            return

        _emit("allow")
    except Exception:
        # Fail-open with valid JSON so hook errors never break normal workflow
        _emit("allow")


if __name__ == "__main__":
    main()
