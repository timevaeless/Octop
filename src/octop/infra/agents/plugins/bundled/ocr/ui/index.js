const React = window.__OCTOP_REACT__;
const { jsx: _jsx, jsxs: _jsxs } = window.__OCTOP_JSX__;

function OcrTextCard(props) {
  const theme = props.host.getToolContext().theme;
  const dark = theme === "dark";
  // ocr_recognize returns the recognized text as a plain string; anything
  // else (error envelopes, empty payload) falls back to the default bubble.
  const text = typeof props.data === "string" ? props.data.trim() : "";
  if (!text || text.startsWith("OCR 失败")) return null;
  return _jsxs("div", {
    style: {
      margin: 0,
      maxWidth: 520,
      borderRadius: 16,
      padding: 14,
      background: dark ? "#18181b" : "#f8fafc",
      border: dark ? "1px solid #3f3f46" : "1px solid #e2e8f0",
    },
    "data-octop-plugin-ui": "ocr",
    children: [
      _jsx("div", {
        style: { fontSize: 11, fontWeight: 700, letterSpacing: 1, opacity: 0.55, marginBottom: 8 },
        children: "OCR 文字识别",
      }),
      _jsx("div", {
        style: { whiteSpace: "pre-wrap", lineHeight: 1.6, fontSize: 14, color: "inherit" },
        children: text,
      }),
    ],
  });
}

export function setup(host) {
  host.registerRenderer({ id: "ocr_text_card", tools: ["ocr_recognize"], component: OcrTextCard });
}
