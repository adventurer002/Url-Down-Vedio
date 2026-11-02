import type { MindMapNode, TranscriptSegment } from "./types/api";
export function transcriptToSrt(segments: TranscriptSegment[]) {
  function timestamp(seconds: number) {
    const ms = Math.max(0, Math.round(seconds * 1000));
    return `${String(Math.floor(ms / 3600000)).padStart(2, "0")}:${String(Math.floor(ms / 60000) % 60).padStart(2, "0")}:${String(Math.floor(ms / 1000) % 60).padStart(2, "0")},${String(ms % 1000).padStart(3, "0")}`;
  }
  return segments
    .map(
      (segment, index) =>
        `${index + 1}\n${timestamp(segment.start)} --> ${timestamp(segment.end)}\n${segment.text.trim()}\n`,
    )
    .join("\n");
}
export function timecode(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds)) return "--:--";
  const s = Math.max(0, Math.floor(seconds));
  return s >= 3600
    ? `${Math.floor(s / 3600)}:${String(Math.floor(s / 60) % 60).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`
    : `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}
export function filesize(bytes: number | null) {
  if (bytes == null) return "大小待计算";
  if (bytes < 1024) return `${Math.max(0, bytes)} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  return bytes >= 1024 ** 3
    ? `${(bytes / 1024 ** 3).toFixed(1)} GB`
    : `${(bytes / 1024 ** 2).toFixed(1)} MB`;
}
export function treeToMarkdown(root: MindMapNode): string {
  const lines: string[] = [];
  function visit(node: MindMapNode, depth: number) {
    const text = node.title
      .replace(/\s+/g, " ")
      .replace(/([\\`*_[\]<>#])/g, "\\$1");
    lines.push(
      (depth < 3
        ? "#".repeat(depth + 1) + " "
        : "  ".repeat(depth - 3) + "- ") + text,
    );
    node.children.forEach((child) => visit(child, depth + 1));
  }
  visit(root, 0);
  return lines.join("\n") + "\n";
}
export function downloadText(text: string, filename: string, type: string) {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
