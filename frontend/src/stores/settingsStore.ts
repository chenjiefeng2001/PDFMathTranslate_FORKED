/** 外观与连接偏好：localStorage 持久化（pdf2zh.theme / pdf2zh.apiBase）。 */

import { create } from "zustand";

const THEME_KEY = "pdf2zh.theme";

function detectDark(): boolean {
  try {
    const stored = window.localStorage.getItem(THEME_KEY);
    if (stored === "dark") return true;
    if (stored === "light") return false;
  } catch {
    /* ignore */
  }
  try {
    return window.matchMedia?.("(prefers-color-scheme: dark)").matches ?? false;
  } catch {
    return false;
  }
}

interface SettingsState {
  dark: boolean;
  toggleTheme(): void;
}

export const useSettingsStore = create<SettingsState>((set, get) => ({
  dark: detectDark(),
  toggleTheme() {
    const dark = !get().dark;
    set({ dark });
    try {
      window.localStorage.setItem(THEME_KEY, dark ? "dark" : "light");
    } catch {
      /* ignore */
    }
  },
}));
