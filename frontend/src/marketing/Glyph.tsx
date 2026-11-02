import type { CSSProperties } from "react";
export type GlyphName =
  | "play"
  | "file"
  | "summary"
  | "nodes"
  | "arrow"
  | "check"
  | "bookmark"
  | "sparkle"
  | "wave";
const paths: Record<GlyphName, string> = {
  play: "M9 5.5 19 12 9 18.5Z",
  file: "M7 3h7l4 4v14H6V3h1M14 3v5h4M9 12h6M9 16h6",
  summary: "M6 5h12M6 10h12M6 15h8M6 20h5",
  nodes: "M12 3v5M5 8h14M5 8v5M19 8v5M12 8v12M2 14h6v6H2ZM16 14h6v6h-6Z",
  arrow: "M4 12h15M13 6l6 6-6 6",
  check: "m5 12 4 4 10-10",
  bookmark: "M6 3h12v18l-6-4-6 4Z",
  wave: "M4 10v4M8 6v12M12 3v18M16 7v10M20 10v4",
  sparkle:
    "m12 2 2.5 7.5L22 12l-7.5 2.5L12 22l-2.5-7.5L2 12l7.5-2.5L12 2ZM20 2v4M18 4h4",
};
export default function Glyph({
  name,
  size = 22,
  style,
}: {
  name: GlyphName;
  size?: number;
  style?: CSSProperties;
}) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      style={style}
    >
      <path d={paths[name]} />
    </svg>
  );
}
