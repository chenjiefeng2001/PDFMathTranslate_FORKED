#!/usr/bin/env bash
# CI 冒烟：真跑一次翻译，但只对「翻译后端不可达」放行。
#
# 为什么需要这个包装
# ------------------
# python-test.yml 里那两个 translate 步骤用**默认 google 服务**真发一次请求。
# GitHub 托管 runner 走云出口 IP，Google 对这类 IP 限流极狠：实测连续 429
# 后 tenacity 抛 RetryError，_enforce_translation_error_policy 随即硬失败退出 1。
# 后端不可达描述的是第三方网络状况，跟本仓库代码无关。让它硬红只会把 CI 变成
# 随机点亮的交通灯 —— 真正该拦的回归（比如「恒等译文当成功交付」）反而被淹没。
#
# 放行边界（重要）
# ----------------
# 只认下面这些**后端特征串**，命中才放行；其余任何失败——解析、版面、渲染、
# 写盘、译文恒等、参数错误——都原样退出非零，CI 照样红。
# 换句话说：这个脚本放宽的是「网络」，不是「正确性」。
set -uo pipefail

if [ "$#" -eq 0 ]; then
    echo "usage: translate_smoke.sh <pdf2zh args...>" >&2
    exit 2
fi

log=$(mktemp)
trap 'rm -f "$log"' EXIT

# 被测的入口：默认源码入口（uv run pdf2zh），win64 作业通过
# PDF2ZH_SMOKE_CMD 换成构建好的 exe（./script/build/pdf2zh.exe）。
# 故意用字符串 + 不加引号展开，让 "uv run pdf2zh" 这样的多词命令也能工作。
smoke_cmd=${PDF2ZH_SMOKE_CMD:-uv run pdf2zh}
# shellcheck disable=SC2086
$smoke_cmd "$@" 2>&1 | tee "$log"
rc=${PIPESTATUS[0]}

if [ "$rc" -eq 0 ]; then
    exit 0
fi

# 逐条列全，避免一条宽泛的 "timeout|error" 把真 bug 一起放过去。
backend_down='RetryError
Too Many Requests
HTTP 429
ConnectionError
ConnectTimeout
ConnectionResetError
Max retries exceeded
SSLError
ProxyError
Temporary failure in name resolution
Name or service not known
nodename nor servname provided'

if grep -Eq "$backend_down" "$log"; then
    matched=$(grep -Eo "$backend_down" "$log" | sort -u | paste -sd, -)
    echo "::warning title=翻译后端不可达，冒烟步骤放行::pdf2zh 退出码 $rc，" \
        "命中后端特征：${matched}。本步骤只验证解析→版面→渲染→写盘链路能跑通，" \
        "翻译本身未成功不代表代码有问题，故不放 CI 红。"
    exit 0
fi

echo "::error title=翻译冒烟失败::pdf2zh 退出码 $rc，且输出中没有后端不可达特征，" \
    "按真实失败处理。"
exit "$rc"
