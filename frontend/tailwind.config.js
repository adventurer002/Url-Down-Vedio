/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: [
          '"Geist Sans"',
          '"PingFang SC"',
          '"Hiragino Sans GB"',
          '"Microsoft YaHei"',
          "sans-serif",
        ],
        mono: ['"Geist Mono"', '"SF Mono"', "SFMono-Regular", "Consolas", "monospace"],
      },
      colors: {
        ink: "#171717",
        brand: "#000000",
        paper: "#ffffff",
        line: "#eaeaea",
        muted: "#666666",
        faint: "#fafafa",
        graphite: "#8f8f8f",
        ok: "#297a3a",
      },
      fontSize: {
        eyebrow: ["11px", { lineHeight: "16px", letterSpacing: "0.071em" }],
        caption: ["13px", { lineHeight: "20px" }],
        heading: ["30px", { lineHeight: "38px", letterSpacing: "-0.02em" }],
        "heading-lg": ["56px", { lineHeight: "1", letterSpacing: "-0.06em" }],
      },
      boxShadow: {
        hairline: "0 0 0 1px rgba(0,0,0,0.08), 0 0 0 2px #fafafa",
      },
    },
  },
  plugins: [],
};
