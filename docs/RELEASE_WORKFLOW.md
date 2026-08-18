# 远端打包发布 Release（GitHub Actions）

`.github/workflows/release.yml` 提供一条在 GitHub Actions 远端完成「打包 → 测试 → 发布」的完整流水线，
适配 fork / 任意仓库（不限制主仓库 `Byaidu/PDFMathTranslate`）。

## 触发方式

### 1. 手动触发（推荐）

仓库页面 **Actions → Build & Publish Release → Run workflow**，填写：

| 输入项 | 说明 |
|---|---|
| `version` | 发布版本号，如 `1.9.12` 或 `v1.9.12`；留空则读取 `pyproject.toml` 的 `version` |
| `publish_pypi` | 是否同时把 sdist/wheel 发布到 PyPI（需配置凭据，见下） |
| `run_tests` | 发布前是否运行完整测试套件（复用 `.github/workflows/python-test.yml`） |
| `prerelease` | 是否标记为预发布（GitHub Release 显示 "Pre-release"） |

### 2. 推送 tag 自动触发

```bash
git tag v1.9.12
git push origin v1.9.12
```

推送 `v*` 形式的 tag 会自动构建并发布（不发布到 PyPI，避免与主仓库发布流程冲突）。

## 流水线结构

```
version          解析并校验版本号（输入优先，否则读 pyproject.toml）
  ├─ test            可选：运行完整测试套件（python-test.yml）
  ├─ build-python    构建 sdist + wheel（uv build）
  └─ build-win64     调用 script/build-win64.ps1 构建 Windows x64 可执行版，
                     打包 with-assets / without-assets 两个 zip
       └─ smoke-test-win64   下载 exe 冒烟测试（--version + 真实翻译两个测试 PDF）
              └─ publish     创建 / 更新 GitHub Release 并上传全部资产
publish-pypi（可选） 发布 Python 包到 PyPI
```

## 发布产物

上传到 `v<version>` 对应 Release：

- `pdf2zh-<tag>-with-assets-win64.zip` — **推荐**，内置字体 / 模型等离线资源（解压即用）
- `pdf2zh-<tag>-win64.zip` — 不含离线资源，首次运行按需下载
- `pdf2zh-<version>.whl` / `pdf2zh-<version>.tar.gz` — Python 包

## 前置配置

1. **Actions 读写权限**（发布 Release / 打 tag 必需）
   `Settings → Actions → General → Workflow permissions` 勾选 *Read and write permissions*。

2. **PyPI 发布（可选，二选一）**
   - 配置 `PYPI_API_TOKEN` secret（`Settings → Secrets and variables → Actions`），或
   - 为仓库配置 [Trusted Publishing](https://docs.pypi.org/trusted-publishers/)（`id-token` 已在 job 中声明）。

## 幂等性

同一 tag 重复发布安全：Release 已存在时自动改为 `gh release upload --clobber` 覆盖资产，
不会重复创建 Release；tag 不存在时由 `gh release create` 自动创建。

## 常见问题

- **手动触发失败提示权限不足**：检查 Actions 读写权限是否为 Read and write（见上）。
- **Windows exe 构建超时**：默认 `timeout-minutes: 120`，可调大；首次运行会下载 Python / PyStand / BabelDOC 离线资源，耗时较长。
- **不想跑完整测试**：手动触发时把 `run_tests` 关闭即可。
- **本地构建方式**：`./script/build-win64.ps1 -GenerateOfflineAssets -DownloadVCRedist`，产物在 `script/build/`。
