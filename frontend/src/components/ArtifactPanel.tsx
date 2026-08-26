/**
 * 产物下载与预览交互组件：
 * - ArtifactRow：列表行，整行点选切换预览，行内图标承担单个「另存为」。
 * - ZipDownload：全部产物打包 ZIP，主操作。
 * - BatchSaveToFolder：桌面壳专属，一次选夹后批量落盘。
 * 三者共用 fetchArtifactBlob / saveViaAnchor，落盘走原生写盘、浏览器回退锚点。
 */

import { Button, List, message } from "antd";
import {
  DownloadOutlined,
  EyeOutlined,
  FolderOpenOutlined,
} from "@ant-design/icons";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { resultZipUrl } from "../api/endpoints";
import {
  isTauri,
  joinPath,
  pickExistingDirectory,
  pickSavePath,
  writeBytesAt,
} from "../api/nativeSave";

/** 抓取产物为 Blob（大小校验），供原生写盘与锚点两条路径共用。 */
async function fetchArtifactBlob(url: string): Promise<Blob> {
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  const blob = await resp.blob();
  if (blob.size === 0) throw new Error("empty file");
  return blob;
}

/** 浏览器回退：objectURL + 锚点点击（落到 webview 默认下载目录）。 */
function saveViaAnchor(blob: Blob, name: string): void {
  const objectUrl = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = objectUrl;
  anchor.download = name;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(objectUrl), 60_000);
}

/**
 * 产物列表行：整行可点（切换预览），行内图标承担单个「另存为」。
 * 桌面壳走系统另存为对话框 + 写盘命令；纯浏览器回退锚点下载。
 * 结果以全局 toast 反馈，避免每行堆叠状态 Tag。
 */
export function ArtifactRow({
  name,
  url,
  selected,
  onSelect,
}: {
  name: string;
  url: string;
  selected: boolean;
  onSelect(): void;
}) {
  const { t } = useTranslation();
  const [busy, setBusy] = useState(false);

  async function save() {
    setBusy(true);
    try {
      const blob = await fetchArtifactBlob(url);
      if (isTauri()) {
        const path = await pickSavePath(name);
        if (!path) return; // 用户取消
        await writeBytesAt(path, new Uint8Array(await blob.arrayBuffer()));
        message.success(`${t("ui.download_done")} · ${path}`);
      } else {
        saveViaAnchor(blob, name);
        message.success(t("ui.download_done"));
      }
    } catch (err) {
      message.error(`${t("ui.download_failed")} · ${String(err)}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <List.Item
      style={{
        cursor: "pointer",
        background: selected ? "var(--color-accent-soft)" : undefined,
        borderRadius: 6,
        paddingInline: 8,
      }}
      onClick={onSelect}
      actions={[
        <Button
          key="preview"
          type="text"
          size="small"
          icon={<EyeOutlined />}
          title={t("ui.download_preview_action")}
          onClick={(e) => {
            e.stopPropagation();
            onSelect();
          }}
        />,
        <Button
          key="save"
          type="text"
          size="small"
          icon={<DownloadOutlined />}
          loading={busy}
          title={t("ui.download_save_as")}
          onClick={(e) => {
            e.stopPropagation();
            void save();
          }}
        />,
      ]}
    >
      <span style={{ fontFamily: "var(--text-font-mono)", fontSize: 13 }}>{name}</span>
    </List.Item>
  );
}

/** 全部产物打包 ZIP：主操作按钮，落盘/回退逻辑与单文件一致。 */
export function ZipDownload({ taskId }: { taskId: string }) {
  const { t } = useTranslation();
  const [busy, setBusy] = useState(false);

  async function download() {
    setBusy(true);
    try {
      const blob = await fetchArtifactBlob(resultZipUrl(taskId));
      if (isTauri()) {
        const path = await pickSavePath("pdf2zh-results.zip");
        if (!path) return;
        await writeBytesAt(path, new Uint8Array(await blob.arrayBuffer()));
        message.success(`${t("ui.download_done")} · ${path}`);
      } else {
        saveViaAnchor(blob, "pdf2zh-results.zip");
        message.success(t("ui.download_done"));
      }
    } catch (err) {
      message.error(`${t("ui.download_failed")} · ${String(err)}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Button
      type="primary"
      icon={<DownloadOutlined />}
      loading={busy}
      onClick={() => void download()}
    >
      {t("ui.download_all_zip")}
    </Button>
  );
}

/**
 * 批量保存到指定文件夹（桌面壳专属）：一次系统「选择文件夹」对话框选定
 * 目标目录后，逐个抓取全部产物并按原名写入该目录。部分失败不中断其余
 * 文件，结束时 toast 汇总；取消选夹静默返回。
 */
export function BatchSaveToFolder({
  items,
}: {
  items: { name: string; url: string }[];
}) {
  const { t } = useTranslation();
  const [busy, setBusy] = useState(false);

  async function run() {
    const dir = await pickExistingDirectory(t("ui.download_folder_pick_title"));
    if (!dir) return; // 用户取消
    setBusy(true);
    const failed: string[] = [];
    for (const item of items) {
      try {
        const blob = await fetchArtifactBlob(item.url);
        await writeBytesAt(
          joinPath(dir, item.name),
          new Uint8Array(await blob.arrayBuffer()),
        );
      } catch (err) {
        failed.push(`${item.name}: ${String(err)}`);
      }
    }
    setBusy(false);
    if (failed.length === 0) {
      message.success(`${t("ui.download_done")} · ${dir}`);
    } else {
      message.warning(`${t("ui.download_failed")} · ${failed.join("; ")}`);
    }
  }

  return (
    <Button
      icon={<FolderOpenOutlined />}
      loading={busy}
      disabled={!isTauri()}
      onClick={() => void run()}
    >
      {t("ui.download_folder_batch", { count: items.length })}
    </Button>
  );
}
