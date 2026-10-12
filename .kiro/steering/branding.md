# Branding guidelines: ReviewLens AI — "Warm Amber"

The visual identity is warm, calm, and confident. Think a well-lit workshop:
amber and honey tones over a soft cream background, with grounding browns for
text and borders. Surfaces are rounded, borders are thin, and everything is
spaced generously so an analyst's eye can rest.

These guidelines are the single source of truth for the look of the app. The
frontend implements them as CSS custom properties (design tokens) in
`frontend/src/styles/theme.css`, consumed by `frontend/src/styles/base.css`.
**Change the look by changing the tokens, not by hard-coding colors, radii, or
spacing in components.**

## Palette

A warm orange / yellow / brown system. Each role has a token name; always
reference the token, never the hex.

### Neutrals (cream & brown)
| Role | Token | Light | Notes |
|---|---|---|---|
| App background | `--color-bg` | `#fdf7ee` | warm cream |
| Surface (cards) | `--color-surface` | `#fffdf9` | near-white, warm |
| Sunken surface | `--color-surface-sunken` | `#f5ebdc` | insets, table hover |
| Border | `--color-border` | `#e4d4bd` | thin, soft tan |
| Text | `--color-text` | `#3b2a1a` | deep brown (not black) |
| Muted text | `--color-text-muted` | `#725741` | warm brown-grey |

### Accent (amber / honey)
| Role | Token | Light | Notes |
|---|---|---|---|
| Accent | `--color-accent` | `#e08a1e` | honey orange — primary actions |
| Accent strong | `--color-accent-strong` | `#9c5a0c` | hover / pressed, amber-brown |
| Accent weak | `--color-accent-weak` | `#fbeccd` | tinted fills, chips |
| On accent | `--color-on-accent` | `#2a1b0b` | text on amber (dark brown, legible) |

### Status (kept in the warm family)
| Role | Token | Light |
|---|---|---|
| OK | `--color-ok` / `--color-ok-weak` | `#4c6a19` / `#eef3dc` (warm olive-green) |
| Warn | `--color-warn` / `--color-warn-weak` | `#8a5f06` / `#fbf1d4` (goldenrod) |
| Danger | `--color-danger` / `--color-danger-weak` | `#b23c17` / `#fbe3d7` (burnt sienna) |
| Info | `--color-info` / `--color-info-weak` | `#875a24` / `#f6e8d3` (amber-brown, no blue) |

Status colors stay inside the orange/yellow/brown family on purpose — no cool
blues or greys — so the whole UI reads as one warm palette.

### Dark mode
Honor `prefers-color-scheme: dark` with a warm, low-chroma dark: espresso-brown
surfaces (`#1c1510`-ish), cream text, and a brighter honey accent so it still
pops. Keep the same token names; only the values change.

## Shape

- **Rounded objects.** Corners are generous. Tokens:
  `--radius-sm: 10px`, `--radius-md: 14px`, `--radius-lg: 20px`. Pills (badges,
  chips) stay fully rounded at `999px`.
- **Thin borders.** `1px solid var(--color-border)`. The soft tan border does
  the separating work; avoid heavy 2px+ rules. Prefer a thin border plus a
  very soft shadow over a hard line.
- **Soft elevation.** Shadows are low and warm-tinted, never harsh:
  `--shadow-sm`, `--shadow-md` using brown-based rgba.

## Spacing

Generous. A 4px base scale, but surfaces breathe:
`--space-1: 0.25rem` … `--space-6: 2.5rem`, `--space-8: 4rem`. Card and section
padding is `--space-5`+; vertical rhythm between sections is `--space-6`. When
in doubt, add space.

## Typography

- **Sans-serif, always.** `--font-sans` is a clean humanist sans stack
  (`"Inter", ui-sans-serif, system-ui, …`). No serif or display faces.
- Monospace (`--font-mono`) only for code / IDs / raw HTML tabs.
- Scale: `--text-xs` → `--text-2xl`. Headings use `--weight-bold` (680),
  body `--weight-normal`, labels/CTAs `--weight-medium` (550).
- Comfortable line height (`--leading: 1.6`) and slight negative letter-spacing
  on large headings.

## Components (how the tokens express the brand)

- **Buttons:** rounded (`--radius-sm`), thin border; primary = amber fill with
  dark-brown text; hover deepens to `--color-accent-strong`.
- **Cards / panels:** `--radius-lg`, thin tan border, `--shadow-sm`, generous
  padding. The "Add new reviews" panel is a tinted amber surface so it reads as
  distinct from the tracked list (product Requirement 1.1).
- **Badges / chips / pills:** fully rounded, weak-tint background with the
  matching strong color for text.
- **Tables:** thin tan row separators, warm sunken hover, uppercase muted
  column labels.
- **Focus:** a visible amber focus ring (`--focus-ring`) on every interactive
  element — never remove it.

## Accessibility

- Keep text/background contrast at WCAG AA. Deep-brown text on cream and
  dark-brown text on amber both clear AA; verify any new pairing.
- Full WCAG validation needs manual testing with assistive tech and expert
  review; these tokens are a starting point, not a guarantee.
- The light-mode text colors above were tuned so normal-size text clears
  AA (≥4.5:1) on its intended surface; keep that bar when adjusting values.
- Always keep the keyboard focus ring visible. Honor
  `prefers-reduced-motion`.

## Rule of thumb

If a component needs a color, radius, or spacing value, it comes from a token
here. New tokens are added to `theme.css` and documented in this file so every
spec stays visually consistent.
