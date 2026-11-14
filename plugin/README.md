# Sci Fast IO v2.0.0（科研极速读写与防爆护栏插件）

基于 [`oil-oil/agy-fast-io`](https://github.com/oil-oil/agy-fast-io) 深度升级的科研工作流加速系统，在同一插件内集成 **轻量常驻规则 + 本地毫秒级 `PreToolUse` 硬护栏 + 按需 `sci-eda` 数据探查技能**。

## 组件结构

```text
~/.gemini/config/plugins/sci-fast-io/
├── plugin.json                # 插件清单 (v2.0.0)
├── hooks.json                 # PreToolUse 本地硬护栏入口 (兼容 read_file / view_file / run_command)
├── scripts/
│   └── pretool_guard.py       # 纯 Python 3.9+ 标准库实现的大文件分级门禁与灾难级 grep 拦截器
├── rules/
│   └── AGENTS.md              # 精简高信噪比常驻规则 (任务分级、免审白名单+逃生阀、批量修改三步闭环、子代理分流)
├── skills/
│   └── sci-eda/
│       └── SKILL.md           # 按需加载的二进制/大表/大 Notebook 单次探查标准脚本库
└── assets/
    └── logo.svg               # 插件图标
```

## 核心能力（v2.0.0）

1. **本地毫秒级 `PreToolUse` 硬门禁（`scripts/pretool_guard.py`）**
   - **二进制与压缩科研格式零容忍拦截**：自动识别 `.parquet`、`.feather`、`.npz`、`.npy`、`.pt`、`.ckpt`、`.safetensors`、`.h5`、`.h5ad`、`.rds`、`.bam` 以及复合后缀 `.nii.gz`、`.fastq.gz`、`.vcf.gz` 和目录型 `.zarr`。
   - **科研文本分级门禁**：`<= 32 KiB` 小指标表直接放行；`32 KiB ~ 2 MiB` 仅放行 `<= 80` 行有界读取（并自动拦截单行 `> 8 KiB` 的超宽矩阵表）；`>= 2 MiB` 直接拒绝并引导使用 `head` 或 `sci-eda`。
   - **Notebook 按字节体积门禁**：废弃行数判断，`> 32 KiB` 的 `.ipynb` 自动拦截全文读取，防止单行巨型 base64 图像撑爆上下文。
   - **保守精准 `grep` 防护（策略 1A）**：仅拒绝无过滤的工作区根目录递归 `grep`（如 `grep -rn '...' .`）或直扫 `.venv` / `wandb` / `lightning_logs`，绝不篡改原始 Shell 命令。
2. **保留桌面端免审白名单加速 + 效率逃生阀**
   - 完整保留白名单命令速查，同时允许在超大规模松散目录中按总耗时最优原则使用过滤后的 `rg` / `fd`。
3. **按需加载 `sci-eda` 技能与动态 `research` 子代理分流**
   - 标准探查脚本按需展开，不占用日常每轮上下文预算。
