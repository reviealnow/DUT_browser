import React from "react";
import { createRoot } from "react-dom/client";

import AppShell from "./pages/AppShell";
import "./styles/dashboard.css";

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <AppShell />
  </React.StrictMode>,
);
