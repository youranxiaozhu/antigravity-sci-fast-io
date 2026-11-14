---
name: sci-eda
description: Standardized single-shot exploratory inspection templates for scientific datasets (.parquet, .csv, .tsv, .h5ad, .npz, .pt, .ckpt, .safetensors, .hdf5, .rds, .vcf, .ipynb, .zarr). Use when inspecting dataset shapes, schemas, missing rates, summary quantiles, tensor keys, or extracting code cells from large Jupyter notebooks in a single round-trip without overflowing context.
---

# Sci EDA — 科研数据与大型 Notebook 单次探查模板

核心原则：**把维度、字段类型、缺失率、分位数摘要合在同一次 `run_command` 脚本里输出，严格限制打印条数**，绝不拆成多轮 `python3 -c` 逐项试探，也不向主上下文打印完整数组或长表。

---

## 1. 表格与列式存储（`.parquet` / `.feather` / 大型 `.csv` / `.tsv`）

优先用 `polars`（本机若未装则自动回退 `pandas`），单次输出行数、列数、内存估算、字段类型、缺失率 Top 15 及数值列分位数：

```bash
python3 - 'data/dataset.parquet' <<'PY'
import os, sys
path = sys.argv[1]
print(f"File: {path} ({os.path.getsize(path)/1048576:.2f} MiB)")
try:
    import polars as pl
    lf = pl.scan_parquet(path) if path.endswith((".parquet", ".pq")) else (
        pl.scan_ipc(path) if path.endswith((".feather", ".arrow", ".ipc")) else pl.scan_csv(path, infer_schema_length=2000)
    )
    schema = lf.collect_schema()
    n_rows = lf.select(pl.len()).collect().item()
    sample = lf.head(5).collect()
    nulls = lf.null_count().collect().row(0, named=True)
    print(f"Shape: {n_rows:,} rows x {len(schema)} cols")
    for k in list(schema.names())[:30]:
        print(f"  - {k}: {schema[k]} (nulls={nulls[k]:,}, {nulls[k]/max(n_rows,1):.1%})")
    if len(schema) > 30:
        print(f"  ... ({len(schema)-30} more columns omitted)")
    print("\nHead(3):")
    print(sample.head(3))
except ImportError:
    import pandas as pd
    df = pd.read_parquet(path) if path.endswith((".parquet", ".pq")) else pd.read_csv(path, nrows=5000)
    print(f"Sampled Shape: {df.shape}")
    na = df.isna().mean().sort_values(ascending=False)
    for col in df.columns[:30]:
        print(f"  - {col}: {df[col].dtype} (sampled_na={na[col]:.1%})")
    print("\nHead(3):")
    print(df.head(3).iloc[:, :12])
PY
```

---

## 2. 单细胞 / HDF5 / Zarr（`.h5ad` / `.loom` / `.h5` / `.hdf5` / `.zarr`）

优先用 `h5py` 只读元数据组（无需把整个稀疏表达矩阵 `X` 载入内存！）：

```bash
python3 - 'data/sample.h5ad' <<'PY'
import os, sys
path = sys.argv[1]
if path.endswith(".zarr") or os.path.isdir(path):
    import zarr
    g = zarr.open(path, mode="r")
    print(g.tree(level=3))
else:
    import h5py
    print(f"File: {path} ({os.path.getsize(path)/1048576:.2f} MiB)")
    with h5py.File(path, "r") as f:
        def walk(name, obj):
            depth = name.count("/")
            if depth <= 2:
                info = f"shape={obj.shape} dtype={obj.dtype}" if isinstance(obj, h5py.Dataset) else "Group"
                print(f"{'  '*depth}{name}: {info} attrs={list(obj.attrs.keys())[:6]}")
        f.visititems(walk)
PY
```

---

## 3. 数组与模型权重（`.npz` / `.npy` / `.pt` / `.pth` / `.ckpt` / `.safetensors`）

只看键名、张量 `shape`、`dtype` 与有限个标量元数据，绝不直接 `print(tensor)`：

```bash
python3 - 'checkpoints/last.ckpt' <<'PY'
import os, sys
path = sys.argv[1]
ext = os.path.splitext(path)[1].lower()
print(f"File: {path} ({os.path.getsize(path)/1048576:.2f} MiB)")
if ext in (".npy", ".npz"):
    import numpy as np
    obj = np.load(path, mmap_mode="r", allow_pickle=False)
    if hasattr(obj, "files"):
        for k in obj.files[:40]:
            arr = obj[k]
            print(f"  {k}: shape={arr.shape}, dtype={arr.dtype}")
    else:
        print(f"  array: shape={obj.shape}, dtype={obj.dtype}")
elif ext == ".safetensors":
    from safetensors import safe_open
    with safe_open(path, framework="pt", device="cpu") as f:
        keys = list(f.keys())
        print(f"Total tensors: {len(keys)}, metadata={f.metadata()}")
        for k in keys[:30]:
            sl = f.get_slice(k)
            print(f"  {k}: shape={sl.get_shape()}, dtype={sl.get_dtype()}")
else:
    import torch
    ckpt = torch.load(path, map_location="cpu", weights_only=False)
    if isinstance(ckpt, dict):
        print("Top keys:", list(ckpt.keys())[:20])
        sd = ckpt.get("state_dict") or ckpt.get("model") or ckpt
        if isinstance(sd, dict):
            items = list(sd.items())
            print(f"Entries: {len(items)}")
            for k, v in items[:25]:
                desc = f"Tensor(shape={tuple(v.shape)}, dtype={v.dtype})" if hasattr(v, "shape") else type(v).__name__
                print(f"  {k}: {desc}")
PY
```

---

## 4. 大型 Jupyter Notebook（`.ipynb`，自动剥离 `outputs` / base64 图片）

先列目录与单元概览，或指定提取前 N 个代码/文本单元，避免 `jq` 全量输出撑爆上下文：

```bash
python3 - 'analysis.ipynb' <<'PY'
import json, os, sys
path = sys.argv[1]
print(f"Notebook: {path} ({os.path.getsize(path)/1024:.1f} KiB)")
with open(path, "r", encoding="utf-8") as f:
    nb = json.load(f)
cells = nb.get("cells", [])
print(f"Total cells: {len(cells)}, kernel={nb.get('metadata', {}).get('kernelspec', {}).get('display_name')}")
for idx, c in enumerate(cells[:40]):
    ctype = c.get("cell_type", "?")
    src = "".join(c.get("source", []))
    first_line = next((ln.strip() for ln in src.splitlines() if ln.strip()), "<empty>")[:90]
    n_out = len(c.get("outputs", []))
    print(f"[{idx:02d}] ({ctype}, lines={src.count(chr(10))+1}, outputs={n_out}): {first_line}")
PY
```
