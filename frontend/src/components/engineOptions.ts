/**
 * 翻译服务下拉框的选项构造与搜索匹配。Dashboard 与 SettingsDrawer 共用，
 * 避免同一个列表在两处渲染出不同的名字。
 *
 * 修掉的三个问题：
 *
 * 1. **后端 label 是 Python 类名。** `/api/engines` 直接返回 `cls.__name__`
 *    （见 `pdf2zh/services/api.py`），而 `cls.__name__` 永远不可能等于
 *    `cls.name`，于是前端那句 `e.label !== e.name ? ... : e.name` 的
 *    fallback 是死代码，25 个服务无一例外显示成 `GoogleTranslator (google)`
 *    这种「类名 (id)」。现在改为先查 i18n，只在缺键时退回类名（并去掉
 *    `Translator` 后缀，至少得到 `Google` 而不是 `GoogleTranslator`）。
 * 2. **搜索搜不到自己看得见的字符串。** 原先两处都是
 *    `optionFilterProp="value"`，只能匹配 id：中文界面打「OpenAI」搜不到，
 *    英文界面打「谷歌」也搜不到。现在 id / 英文名 / 本地化名任一命中即可。
 * 3. **缺键会把 key 当文案渲染。** i18next 没设 `parseMissingKeyHandler`
 *    （见 `i18n/index.ts`），所以每处 `t()` 都必须带 `defaultValue`。
 */

import type { TFunction } from "i18next";
import type { EngineInfo } from "../api/types";

/** 服务 id → i18n 键。`azure-openai` → `ui.engine_azure_openai`。 */
export function engineLabelKey(name: string): string {
  return `ui.engine_${name.replace(/-/g, "_")}`;
}

/** `OpenAITranslator` → `OpenAI`；没有该后缀时原样返回。 */
function classNameFallback(label: string, name: string): string {
  const suffix = "Translator";
  const trimmed = label.endsWith(suffix)
    ? label.slice(0, -suffix.length)
    : label;
  return trimmed.trim() || name;
}

export interface EngineOption {
  value: string;
  label: string;
  /** 小写化的「id 英文名 本地名」，供 `engineFilterOption` 匹配。 */
  searchText: string;
}

export function buildEngineOptions(
  engines: EngineInfo[],
  t: TFunction,
): EngineOption[] {
  return engines.map((e) => {
    const fallback = classNameFallback(e.label ?? "", e.name);
    const label = t(engineLabelKey(e.name), { defaultValue: fallback });
    return {
      value: e.name,
      label,
      // 本地名也并进来：中文界面搜「谷歌」和搜「google」都要能命中同一条。
      searchText: `${e.name} ${fallback} ${label}`.toLowerCase(),
    };
  });
}

/**
 * antd `filterOption`：空查询一律放行；否则对 `searchText` 做子串匹配。
 * 本地化名已经并入 label，而 label 又来自 `t()`，所以同一份查询在两种界面
 * 语言下都能命中对应的那个词。
 */
export function engineFilterOption(
  input: string,
  option?: EngineOption,
): boolean {
  if (!option) return true;
  const q = input.trim().toLowerCase();
  if (!q) return true;
  return option.searchText.includes(q);
}