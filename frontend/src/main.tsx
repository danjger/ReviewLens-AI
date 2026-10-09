import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";

// Design system (dataset-library task 11): tokens first, then base styles.
import "./styles/theme.css";
import "./styles/base.css";

const root = document.getElementById("root");
if (!root) throw new Error("Root element not found");

createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
