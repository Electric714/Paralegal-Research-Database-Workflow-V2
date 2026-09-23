import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import RunControlOverlay from "./RunControlOverlay";
import "./styles.css";
import "./sam-review.css";
import "./run-control.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
    <RunControlOverlay />
  </React.StrictMode>
);
