/**
 * ThemeSwitcher — a TEMPORARY palette preview control (dataset-library task 11).
 *
 * The design system ships two palette directions (theme.css): A "Slate & Teal"
 * (default) and B "Ink & Amber" (`<html data-theme="b">`). This fixed, corner
 * control lets a reviewer flip between them live to pick one, persisting the
 * choice to localStorage so it survives reloads. Once a direction is signed
 * off, this component (and the losing palette) can be removed.
 */
import { useEffect, useState } from "react";

type Direction = "a" | "b";

const STORAGE_KEY = "reviewlens-theme";

function applyTheme(dir: Direction): void {
  const root = document.documentElement;
  if (dir === "b") root.dataset.theme = "b";
  else root.removeAttribute("data-theme");
}

export default function ThemeSwitcher() {
  const [dir, setDir] = useState<Direction>(() => {
    if (typeof window === "undefined") return "a";
    return (window.localStorage.getItem(STORAGE_KEY) as Direction | null) ?? "a";
  });

  useEffect(() => {
    applyTheme(dir);
    try {
      window.localStorage.setItem(STORAGE_KEY, dir);
    } catch {
      /* storage may be unavailable; the in-memory choice still applies */
    }
  }, [dir]);

  return (
    <div className="theme-switcher" data-testid="theme-switcher" role="group" aria-label="Preview palette">
      <span className="theme-switcher__label">Palette</span>
      <button
        type="button"
        className="theme-switcher__btn"
        data-active={dir === "a"}
        aria-pressed={dir === "a"}
        onClick={() => setDir("a")}
      >
        A · Slate &amp; Teal
      </button>
      <button
        type="button"
        className="theme-switcher__btn"
        data-active={dir === "b"}
        aria-pressed={dir === "b"}
        onClick={() => setDir("b")}
      >
        B · Ink &amp; Amber
      </button>
    </div>
  );
}
