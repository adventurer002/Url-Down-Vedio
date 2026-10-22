const paths: Record<string, string> = {
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
