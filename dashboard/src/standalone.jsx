import React from "react";
import { createRoot } from "react-dom/client";
import { ZeroFactoryKanbanApp } from "./App.jsx";

const fetchJSON = async (url, options = {}) => {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || data.error || response.statusText);
  return data;
};

window.__HERMES_PLUGIN_SDK__ = {
  React,
  hooks: React,
  fetchJSON,
  utils: {},
};
createRoot(document.getElementById("root")).render(<ZeroFactoryKanbanApp />);
