import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import RunControlOverlay from "./RunControlOverlay";
import ChatGptFallback from "./ChatGptFallback";
import "./styles.css";
import "./sam-review.css";
import "./run-control.css";
import "./chatgpt-fallback.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
    <RunControlOverlay />
    <ChatGptFallback />
  </React.StrictMode>
);
