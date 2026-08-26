# pr-gate：PR 门禁与自动合并工作流——实施报告

> **日期**：2026-08-26 · 新增：`.github/workflows/pr-gate.yml`
> **目标**：PR 满足「全部测试通过 + 代码合规 + 同一主题修改」时自动 squash 合并

---

## 1. 设计总览

现有 CI 覆盖矩阵（本报告落地前）：

| 关注点 | 已有工作流 | 触发 |
|---|---|---|
| Python 合规 | `black.format.yml` | push/PR |
| Python 测试 | `python-test.yml` | PR |
| 前端构建/类型 | `frontend-build.yml` | push/PR（frontend/** 路径） |
| **Rust 合规/测试** | ❌ 无（exe-build 仅手动） | — |
| **PR 主题一致性** | ❌ 无 | — |
| **自动合并** | ❌ 无 | — |

`pr-gate` 只补缺口、不重复已有检查：

```
pull_request → [changes] 路径分类
                 ├─► [single-topic] 标题+全部提交 Conventional Commits 且 type 一致
                 ├─► [rust-compliance] 仅 frontend/src-tauri/** 变更时：fmt --check + clippy -D warnings + cargo test
                 └─► [auto-merge] needs 全绿后：
                        preflight（OPEN/非草稿/无冲突/同仓库分支）
                        → 按 head_sha 轮询 Actions API，预期工作流全绿才 gh pr merge --squash
```

## 2. 关键决策

### 2.1 同一主题的机械化定义
「同一主题」不可直接静态判定，取可执行近似：**PR 标题与所有非 merge 提交均符合 Conventional Commits，且提交 type 彼此一致并与标题 type 相同**。允许 `type(scope)` 与 `!`；type 集合对齐仓库既有实践（`doc:`/`doc(` 为仓库惯例，故同时接受 `docs`）。混杂 feat+fix 的 PR 直接拒绝并逐条列出违规提交。

### 2.2 Rust 门禁跑 windows-latest
tauri v2 在 ubuntu 需 apt 装 webkitgtk 系列依赖，重且脆；Windows runner 开箱即链（与本机开发环境一致，LNK2019 类问题可在门禁暴露）。缓存用 Swatinem/rust-cache。

### 2.3 自动合并为何不用 `gh pr merge --auto`
原生 auto-merge 依赖 branch protection 的 required checks 配置，无法以代码形式落在 fork 里。改为显式轮询：按 PR head_sha 列出 Actions runs，排除自身 run_id（否则自等死锁），预期集合 = Black + python-test（doc-only 标题时两者豁免）+ frontend-build（仅前端变更时）。任一失败立即终止，90 分钟超时。

### 2.4 安全边界
- **fork PR 不自动合并**（fork 触发的 GITHUB_TOKEN 本就只读，且外部代码不应凭绿灯直入 main）；
- 草稿 PR、已关闭 PR、有冲突 PR 一律跳过/报错；
- squash 合并与仓库现行线性历史风格一致。

## 3. 生效条件

1. 本文件合入 **main** 后，后续新 PR 才会触发（pull_request 工作流读取 base 分支版本）；
2. 仓库设置中需允许 squash merge（GitHub 默认开启）；
3. 若希望 fork PR 也走自动合并，需另行评估 token 权限策略，当前刻意不支持。

## 4. 验证记录（本地）

| 项 | 结果 |
|---|---|
| YAML 解析（PyYAML） | ✅ 4 jobs 结构正确 |
| 内嵌 bash `bash -n` ×9 | ✅ 全部通过 |
| single-topic 干跑（真实 HEAD=fix(desktop) 提交） | ✅ 通过；构造混合类型样本正确 FAIL |
| changes 路径分类干跑 | ✅ src-tauri/frontend/python 三类命中符合预期 |
| `set -e` 陷阱排查 | ✅ 条件赋值改用 if 形式，避免 `[[ ]] && cmd` 短路退出 |

## 5. 追加：移除 push 触发的重型构建（同日）

落地 pr-gate 后，push 到 main 仍会触发 `fork-test`（完整 Python 测试矩阵）与 `frontend-build`（npm 构建），对直接推送形成重复验证。调整：

| 工作流 | 调整 | 理由 |
|---|---|---|
| `fork-test.yml` | push → 仅 `workflow_dispatch` | 重型矩阵（pytest + 双 PDF 翻译 + uv build ×4 job）不应随每次 push 空转；PR 场景由 `python-test.yml` 自身的 pull_request 触发覆盖 |
| `frontend-build.yml` | 移除 push 触发，保留 pull_request | 构建产物在合并前的 PR 上已验证；pr-gate 依赖的是其 **pull_request** 触发器，自动合并不受影响 |
| `black.format.yml` / `python-publish.yml` | 不动 | 前者为秒级 lint，后者在 fork 被 `is_main_repo` 守卫空跑 |

关键约束核对：pr-gate 的 `auto-merge` 预期工作流（Black、python-test、frontend-build）全部依赖 **pull_request 触发器**，本次改动零影响。

## 6. 后续可选

- 前端目前仅有 tsc/vite 构建，可补 eslint 门禁；
- `cargo test` 在 Windows runner 首次冷跑较慢，如超时可加二进制缓存或降级为 clippy-only。