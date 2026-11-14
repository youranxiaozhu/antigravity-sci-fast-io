#!/usr/bin/env python3
"""Automated 19-case regression suite for Sci Fast IO v2.0 PreToolUse guard."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_DIR = os.path.join(REPO_ROOT, "plugin")
GUARD_SCRIPT = os.path.join(PLUGIN_DIR, "scripts", "pretool_guard.py")


def run_suite() -> None:
    tmp_root = tempfile.mkdtemp(prefix="sci_fast_io_test_")
    try:
        py_file = os.path.join(tmp_root, "model.py")
        with open(py_file, "w", encoding="utf-8") as f:
            f.write("def forward(x):\n    return x * 2\n" * 20)

        small_csv = os.path.join(tmp_root, "metrics_10kb.csv")
        with open(small_csv, "w", encoding="utf-8") as f:
            f.write("epoch,loss,val_acc\n" + "1,0.123,0.987\n" * 600)

        mid_csv = os.path.join(tmp_root, "table_85kb.csv")
        with open(mid_csv, "w", encoding="utf-8") as f:
            f.write("id,gene,tpm\n" + "ENSG0001,TP53,42.5\n" * 4500)

        wide_csv = os.path.join(tmp_root, "wide_matrix_60kb.csv")
        with open(wide_csv, "w", encoding="utf-8") as f:
            f.write(",".join([f"col_{i}" for i in range(2500)]) + "\n" + ",".join(["0.1234"] * 2500) + "\n" * 3)

        huge_csv = os.path.join(tmp_root, "huge_10mb.csv")
        with open(huge_csv, "wb") as f:
            f.seek(10 * 1024 * 1024 - 1)
            f.write(b"\n")

        npz_file = os.path.join(tmp_root, "tiny.npz")
        with open(npz_file, "wb") as f:
            f.write(b"PK\x03\x04dummy")

        niigz_file = os.path.join(tmp_root, "brain_scan.nii.gz")
        with open(niigz_file, "wb") as f:
            f.write(b"\x1f\x8b\x08dummy")

        zarr_dir = os.path.join(tmp_root, "atlas.zarr", "X")
        os.makedirs(zarr_dir, exist_ok=True)
        zarr_chunk = os.path.join(zarr_dir, "0.0")
        with open(zarr_chunk, "w", encoding="utf-8") as f:
            f.write("chunk")

        large_nb = os.path.join(tmp_root, "big_plot.ipynb")
        with open(large_nb, "w", encoding="utf-8") as f:
            f.write('{"cells":[{"cell_type":"code","source":["x=1"],"outputs":[{"data":{"image/png":"' + ("A" * 60000) + '"}}]}]}')

        unicode_dir = os.path.join(tmp_root, "实验 数据 2026")
        os.makedirs(unicode_dir, exist_ok=True)
        unicode_csv = os.path.join(unicode_dir, "汇总 结果.csv")
        with open(unicode_csv, "w", encoding="utf-8") as f:
            f.write("样本,数值\nA,1.5\n")

        symlink_npz = os.path.join(tmp_root, "linked_data.txt")
        os.symlink(npz_file, symlink_npz)

        def invoke(payload_raw):
            t0 = time.perf_counter()
            inp = payload_raw if isinstance(payload_raw, str) else json.dumps(payload_raw)
            proc = subprocess.run(
                [sys.executable, "./scripts/pretool_guard.py"],
                input=inp,
                text=True,
                capture_output=True,
                cwd=PLUGIN_DIR,
                timeout=3,
            )
            dt_ms = (time.perf_counter() - t0) * 1000.0
            assert proc.returncode == 0, f"Non-zero exit: {proc.returncode}, stderr={proc.stderr}"
            return json.loads(proc.stdout), dt_ms

        cases = [
            ("1. view_file normal Python file", {"toolCall": {"name": "view_file", "args": {"AbsolutePath": py_file}}, "workspacePaths": [tmp_root]}, "allow"),
            ("2. read_file 10 KB metrics CSV", {"toolCall": {"name": "read_file", "args": {"Path": small_csv}}, "workspacePaths": [tmp_root]}, "allow"),
            ("3. read_file 85 KB CSV unbounded", {"toolCall": {"name": "read_file", "args": {"Path": mid_csv}}, "workspacePaths": [tmp_root]}, "deny"),
            ("4. read_file 85 KB CSV bounded 1..40", {"toolCall": {"name": "read_file", "args": {"Path": mid_csv, "StartLine": 1, "EndLine": 40}}, "workspacePaths": [tmp_root]}, "allow"),
            ("5. read_file wide >8KB single-line CSV", {"toolCall": {"name": "read_file", "args": {"Path": wide_csv, "StartLine": 1, "EndLine": 5}}, "workspacePaths": [tmp_root]}, "deny"),
            ("6. read_file >=2 MiB CSV unbounded", {"toolCall": {"name": "read_file", "args": {"Path": huge_csv}}, "workspacePaths": [tmp_root]}, "deny"),
            ("7. read_file tiny .npz binary", {"toolCall": {"name": "read_file", "args": {"Path": npz_file}}, "workspacePaths": [tmp_root]}, "deny"),
            ("8. read_file compound .nii.gz", {"toolCall": {"name": "read_file", "args": {"Path": niigz_file}}, "workspacePaths": [tmp_root]}, "deny"),
            ("9. read_file inside .zarr directory", {"toolCall": {"name": "read_file", "args": {"Path": zarr_chunk}}, "workspacePaths": [tmp_root]}, "deny"),
            ("10. read_file symlink pointing to .npz", {"toolCall": {"name": "read_file", "args": {"Path": symlink_npz}}, "workspacePaths": [tmp_root]}, "deny"),
            ("11. read_file >32KB .ipynb even with line range", {"toolCall": {"name": "read_file", "args": {"Path": large_nb, "StartLine": 1, "EndLine": 5}}, "workspacePaths": [tmp_root]}, "deny"),
            ("12. relative path + spaces & CJK Unicode", {"toolCall": {"name": "read_file", "args": {"Path": "实验 数据 2026/汇总 结果.csv"}}, "workspacePaths": [tmp_root]}, "allow"),
            ("13. stringified Path arg compatibility", {"toolCall": {"name": "read_file", "args": {"Path": json.dumps(huge_csv)}}, "workspacePaths": [tmp_root]}, "deny"),
            ("14. run_command grep -rn 'pat' . (unfiltered root)", {"toolCall": {"name": "run_command", "args": {"CommandLine": "grep -rn 'forward' ."}}, "workspacePaths": [tmp_root]}, "deny"),
            ("15. run_command grep -rn 'pat' .venv", {"toolCall": {"name": "run_command", "args": {"CommandLine": "grep -rn 'torch' .venv"}}, "workspacePaths": [tmp_root]}, "deny"),
            ("16. run_command scoped grep -Rn --include='*.py' 'pat' 'src'", {"toolCall": {"name": "run_command", "args": {"CommandLine": "grep -Rn --include='*.py' 'forward' 'src'"}}, "workspacePaths": [tmp_root]}, "allow"),
            ("17. run_command piped git ls-files | grep -i 'model'", {"toolCall": {"name": "run_command", "args": {"CommandLine": "git ls-files | grep -i 'model'"}}, "workspacePaths": [tmp_root]}, "allow"),
            ("18. malformed JSON fail-open", "{broken json", "allow"),
        ]

        latencies = []
        for title, payload, expected in cases:
            res, ms = invoke(payload)
            latencies.append(ms)
            got = res.get("decision")
            assert got == expected, f"[FAIL] {title}: expected {expected}, got {res}"
            print(f"[PASS] {title} -> {got} ({ms:.1f}ms)")

        # Case 19: unclosed stdin pipe simulation
        p = subprocess.Popen(
            [sys.executable, GUARD_SCRIPT],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=PLUGIN_DIR,
        )
        t0 = time.perf_counter()
        assert p.stdin is not None and p.stdout is not None
        p.stdin.write(json.dumps({"toolCall": {"name": "read_file", "args": {"Path": npz_file}}, "workspacePaths": [tmp_root]}))
        p.stdin.flush()
        line = p.stdout.readline()
        p.wait(timeout=1.5)
        p.stdin.close()
        dt_ms = (time.perf_counter() - t0) * 1000.0
        res19 = json.loads(line)
        assert res19.get("decision") == "deny", f"[FAIL] unclosed stdin: {res19}"
        latencies.append(dt_ms)
        print(f"[PASS] 19. unclosed stdin pipe non-blocking frame parse -> deny ({dt_ms:.1f}ms)")

        latencies.sort()
        print(f"\nAll {len(cases) + 1} tests passed! Median={latencies[len(latencies)//2]:.1f}ms, P95={latencies[int(len(latencies)*0.95)]:.1f}ms")
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


if __name__ == "__main__":
    run_suite()
