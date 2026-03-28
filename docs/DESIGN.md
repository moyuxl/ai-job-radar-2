# Design System Specification: The Architectural Navigator

## 1. Overview & Creative North Star

This design system is built to transform the complex, often chaotic process of job seeking into a structured, high-end editorial experience. Moving beyond the "standard SaaS dashboard," this system treats data as a curated narrative.

**Creative North Star: "The Intelligent Cartographer"**
Like a premium physical map or a high-end architectural blueprint, the UI utilizes a structural grid, purposeful negative space, and a clear sense of "place." We break the traditional box-heavy template by using intentional asymmetry, overlapping layers of information, and a deep focus on tonal depth. Every element should feel like it was placed with surgical precision, guiding the user through their career journey with intelligence and reliability.

---

## 2. Colors & Surface Philosophy

The color palette is anchored in a professional **Deep Blue** for authoritative trust and a **Vibrant Orange** to signal momentum and action. 

### The Palette (Material Design Convention)
*   **Primary:** `#005bbf` (Core Brand Trust)
*   **Primary Container:** `#1a73e8` (Interactive Accents)
*   **Secondary:** `#9f4200` (Call to Action)
*   **Secondary Container:** `#fd6c00` (Energy/Highlight)
*   **Surface:** `#f7f9fb` (The Canvas)
*   **Surface Container (Low to Highest):** `#f2f4f6` to `#e0e3e5`

### Structural Rules
*   **The "No-Line" Rule:** We do not use 1px solid borders to separate sections. Structure is defined through background shifts. A `surface-container-low` section sitting on a `surface` background creates a clear boundary without the visual "noise" of a line.
*   **Surface Hierarchy & Nesting:** Treat the UI as stacked sheets of fine paper. An outer container might use `surface-container`, while a nested card uses `surface-container-lowest` (#ffffff) to "pop" forward.
*   **The "Glass & Gradient" Rule:** For floating components or AI-driven insights, use Glassmorphism. Apply a semi-transparent surface color with a `backdrop-blur` of 12px-20px. 
*   **Signature Textures:** Main CTAs should not be flat. Use a subtle linear gradient (e.g., `primary` to `primary-container`) to provide a "jeweled" depth that feels premium and custom.

---

## 3. Typography: The Editorial Voice

We utilize a dual-typeface system to balance technical intelligence with human readability.

*   **Display & Headline (Manrope):** A geometric sans-serif that feels modern and architectural. 
    *   *Role:* Used for large data points, page titles, and "High-Level" insights. 
    *   *Scale:* `display-lg` (3.5rem) to `headline-sm` (1.5rem).
*   **Body & Label (Inter):** A high-legibility workhorse.
    *   *Role:* Used for job descriptions, input fields, and UI labels.
    *   *Scale:* `body-lg` (1rem) for readability; `label-sm` (0.6875rem) for metadata.

**Hierarchy Note:** Use wide tracking (letter-spacing) on `label` styles to give a sophisticated, technical feel to small metadata text.

### Workbench implementation (Navi AI)

The live workbench (`/workbench`) maps the above principles into concrete tokens in `static/styles/console.css` and `static/styles/workbench.css`:

- **Step title** (`.workbench-step-title`): `1.3125rem` — intended as ~1.5× the crawl form body size for clear hierarchy.
- **Crawl form body** (`#crawlSection` inputs, selects, experience chips): `--font-wb-crawl-body` = `0.875rem` in `:root`.
- **Field labels + primary CTA alignment**: `--font-wb-crawl-label` = `0.95rem` on `#crawlSection #crawlForm .form-group > label`; global `.btn` uses `0.95rem` for parity with「开始抓取」.
- **Hints**: `--font-wb-crawl-hint` = `0.8125rem`.

Exact pixel values depend on root font size (default 16px). See **README_WEB_CONSOLE.md** for a rem ↔ px table.

---

## 4. Elevation & Depth

We eschew traditional drop shadows in favor of **Tonal Layering** and **Ambient Light**.

*   **The Layering Principle:** Depth is achieved by "stacking" the surface-container tiers. Placing a `#ffffff` (lowest) card on a `#f7f9fb` (base) background provides natural elevation.
*   **Ambient Shadows:** If a card must float (e.g., a hover state), use an extra-diffused shadow: `box-shadow: 0 12px 40px rgba(25, 28, 30, 0.06)`. Note the low opacity; it should feel like a soft glow of light, not a dark smudge.
*   **The "Ghost Border":** For high-density data areas where separation is mandatory, use the `outline-variant` token at **15% opacity**. This creates a "suggestion" of a boundary rather than a hard wall.
*   **The Grid Background:** Always maintain the subtle grid pattern on the base `surface`. This reinforces the "Navigator" personality, making the app feel like a precise tool for discovery.

---

## 5. Components

### Buttons
*   **Primary:** Gradient-filled (`primary` to `primary-container`), 8px-12px rounded corners. White text (`on-primary`). High-gloss polish.
*   **Secondary:** Ghost style. Transparent background with a `ghost-border`. 
*   **Action:** For job applications, use the `secondary-container` (Orange) to drive urgency.

### Cards & Lists
*   **The Forbidding of Dividers:** Never use a horizontal line between list items. Use vertical white space (Token `spacing-6` or `spacing-8`) to create breathing room. 
*   **Interactive Cards:** Use `surface-container-lowest` with a subtle hover transition that scales the card 1.01% and increases shadow diffusion.

### AI Insight Chips
*   Used for "Smart Filters" or AI-suggested keywords. These should use a semi-transparent `primary-fixed-dim` background with a subtle blue "Ghost Border" to distinguish them from standard tags.

### Form Inputs
*   Background: `surface-container-lowest`.
*   Active State: No heavy border; use a 2px `primary` bottom-bar or a soft `primary-container` inner glow to signal focus.

---

## 6. Do's and Don'ts

### Do
*   **Do use asymmetrical layouts.** Align text to a strict grid, but let imagery or decorative grid lines break the container boundaries.
*   **Do prioritize white space.** If a screen feels "busy," increase the spacing scale (e.g., from `4` to `8`) rather than adding more borders.
*   **Do use "Surface Tint" for high-energy areas.** A 5% tint of blue over a surface makes it feel more "tech-forward" than pure grey.

### Don't
*   **Don't use 100% black.** Always use `on-surface` (`#191c1e`) for text to maintain a sophisticated, soft-contrast look.
*   **Don't use sharp 90-degree corners.** Every element must adhere to the **Roundedness Scale** (8px-12px minimum) to maintain the "Modern Reliable" persona.
*   **Don't use "Default" Blue.** Ensure all blues are pulled from the specific `primary` tokens to avoid a generic "browser-default" appearance.
