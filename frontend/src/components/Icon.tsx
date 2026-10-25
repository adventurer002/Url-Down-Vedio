const paths: Record<string, string> = {
  bookmark: "M6 3h12v18l-6-4-6 4z",
  chat: "M4 4h16v12H9l-5 4zM8 8h8M8 12h5",
  search: "M21 21l-6-6M17 10a7 7 0 1 1-14 0 7 7 0 0 1 14 0",
  library: "M4 3h4v18H4zM10 3h4v18h-4zM16 5l4-1 4 16-4 1z",
  sparkles:
    "m12 3 2.6 6.4L21 12l-6.4 2.6L12 21l-2.6-6.4L3 12l6.4-2.6L12 3M20 2v4m-2-2h4",
  seedling:
    "M12 21v-8M12 16C5 16 3 12 3 6c6 0 9 3 9 8M12 12c0-5 3-8 9-8 0 6-3 10-9 10",
  refresh:
    "M20 7v5h-5M4 17v-5h5M6 7a7 7 0 0 1 11-2l3 3M4 16l3 3a7 7 0 0 0 11-2",
  trash: "M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7",
  "plus-circle": "M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0M8 12h8M12 8v8",
  external: "M14 3h7v7m0-7L11 13M10 3H4v17h17v-7",
  audio:
    "M9 18V5l12-2v13M9 8l12-2M9 18a3 3 0 1 1-3-3c2 0 3 1 3 3m12-2a3 3 0 1 1-3-3c2 0 3 1 3 3",
  user: "M16 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0M4 21v-2a8 8 0 0 1 16 0v2",
  arrow: "M5 12h14m-6-6 6 6-6 6",
  download: "M12 3v12m-4-4 4 4 4-4M4 16v5h16v-5",
  text: "M5 4h14v16H5zM8 8h8M8 12h8M8 16h4",
  map: "M4 12h6m0 0V5h7m-7 7v7h7M3 10h2v4H3zM17 3h4v4h-4zM17 17h4v4h-4z",
  history: "M3 11a9 9 0 1 1 3 8M3 4v7h7m2-4v5l3 2",
  close: "m6 6 12 12M6 18 18 6",
  link: "m10 14 4-4m-6 5-1 1a4 4 0 0 1-6-6l4-4a4 4 0 0 1 6 0m2 3 1-1a4 4 0 0 1 6 6l-4 4a4 4 0 0 1-6 0",
  plus: "M5 12h14M12 5v14",
  minus: "M5 12h14",
  fit: "M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5",
  play: "m9 5 11 7-11 7z",
  copy: "M9 9h11v11H9zM5 15H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v1",
};
export default function Icon({
  name = "check",
  size = 20,
}: {
  name?: string;
  size?: number;
}) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d={paths[name] || "m6 12 4 4 8-8"} />
    </svg>
  );
}
