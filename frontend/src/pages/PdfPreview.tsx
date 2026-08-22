/**
 * 内嵌 PDF 预览（pdfjs-dist 6.x）。
 * - worker 经 Vite `?url` 导入，产物自包含（Tauri 离线场景友好）；
 * - 数据源为 API artifacts 直链（同源/代理形态均可用）。
 */

import { useEffect, useRef, useState } from "react";
import { Button, Space, Spin } from "antd";
import { LeftOutlined, RightOutlined } from "@ant-design/icons";
import * as pdfjsLib from "pdfjs-dist";
import workerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";

pdfjsLib.GlobalWorkerOptions.workerSrc = workerUrl;

interface Props {
  url: string;
}

export default function PdfPreview({ url }: Props) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const docRef = useRef<pdfjsLib.PDFDocumentProxy | null>(null);
  const [page, setPage] = useState(1);
  const [numPages, setNumPages] = useState(0);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setFailed(false);
    setPage(1);
    setNumPages(0);

    const loadingTask = pdfjsLib.getDocument({ url });
    (async () => {
      try {
        const doc = await loadingTask.promise;
        if (cancelled) {
          
          return;
        }
        docRef.current = doc;
        setNumPages(doc.numPages);
        const pageObj = await doc.getPage(1);
        if (cancelled) return;
        const canvas = canvasRef.current;
        if (!canvas) return;
        const scale = 1.5;
        const viewport = pageObj.getViewport({ scale });
        canvas.width = viewport.width;
        canvas.height = viewport.height;
        await pageObj.render({ canvas, viewport }).promise;
        if (!cancelled) setLoading(false);
      } catch {
        if (!cancelled) {
          setFailed(true);
          setLoading(false);
        }
      }
    })();

    return () => {
      cancelled = true;
      void loadingTask.destroy();
      docRef.current = null;
    };
  }, [url]);

  async function goto(next: number) {
    const doc = docRef.current;
    const canvas = canvasRef.current;
    if (!doc || !canvas || next < 1 || next > doc.numPages) return;
    const pageObj = await doc.getPage(next);
    const scale = 1.5;
    const viewport = pageObj.getViewport({ scale });
    canvas.width = viewport.width;
    canvas.height = viewport.height;
    await pageObj.render({ canvas, viewport }).promise;
    setPage(next);
  }

  return (
    <div style={{ textAlign: "center" }}>
      <Space style={{ marginBottom: 8 }}>
        <Button size="small" icon={<LeftOutlined />} disabled={page <= 1} onClick={() => void goto(page - 1)} />
        <span>
          {page} / {numPages || "?"}
        </span>
        <Button size="small" icon={<RightOutlined />} disabled={page >= numPages} onClick={() => void goto(page + 1)} />
      </Space>
      <div style={{ position: "relative", minHeight: 120 }}>
        {loading && <Spin style={{ position: "absolute", inset: 0, margin: "auto" }} />}
        {failed ? (
          <div style={{ opacity: 0.6 }}>预览不可用（文件缺失或非 PDF）</div>
        ) : (
          <canvas
            ref={canvasRef}
            style={{ maxWidth: "100%", boxShadow: "0 2px 12px rgba(0,0,0,.15)" }}
          />
        )}
      </div>
    </div>
  );
}
