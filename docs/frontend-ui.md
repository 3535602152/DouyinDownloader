# Frontend UI foundations

The desktop frontend remains vanilla JavaScript and CSS. The bridge and backend
contracts are unchanged. Shared UI rules live in `gui/web/ui-foundation.css` and
`gui/web/ui-foundation.js`; page-specific composition lives in `style.css` and
`app.js`. Load the foundations before page code and preserve the existing scoped
dark theme and intentionally dark media-player palette.

## Ownership

- **Surface (`.panel`)** owns its border, radius, background and clipping. Do not
  disable clipping to make a menu fit. The table's square header is clipped by
  this surface, including after columns are hidden or reordered.
- **Table (`.table-wrap`)** owns horizontal overflow and its visible scrollbar.
  Generic table styles are scoped to table containers. Use a stable
  `data-ui-scroll-key` when a container can be rebuilt. Include a page identity
  when pagination should start at the top.
- **Controls (`.button`)** share one set of state rules. Themes change semantic
  control tokens, rather than copying the whole hover/active/disabled cascade.
  Legacy color aliases remain for existing page and player consumers.
- **Overlays** use the body-level `#ui-overlay-host`. Page-owned menus and
  dialogs are moved there by `AppUI.mountPageOverlays`; opening a row menu uses
  `AppUI.setPageMenu` without rebuilding the table. They must not be descendants
  of an animated or clipping page surface.
- **Pinned monitor header** preserves the upstream page-scroll behavior and MB
  storage format. The clone uses the same table primitives, mirrors select-all
  state, tracks container resizing, and is inert behind a modal. When pinned,
  it meets the viewport at `top: 0` and continues the original panel's side
  borders with square corners and the table divider, without a floating-card
  shadow or an exposed strip of body rows above it. Use fractional panel/table
  bounds to preserve every column's alignment. Observe main-area resizing too:
  the centered, max-width content can move during sidebar animation without
  changing the table's own width. Push the header out with the table's bottom.
- **Layers** use `--layer-*` tokens. Drawers/popovers/menus are below modal
  dialogs; the player has a separate higher layer and toast feedback stays
  visible. Internal player layers remain local to its stacking context.
- **Creator work-grid motion** is owned by `CreatorView`. The shell uses 250ms
  `cubic-bezier(.2, 0, 0, 1)`, a utility-focused curve with a gradual stop. The
  duration is informed by [Fluent timing guidance](https://learn.microsoft.com/en-us/windows/apps/design/motion/timing-and-easing)
  and the curve by [Material's motion tokens](https://github.com/material-components/material-components-android/blob/master/docs/theming/Motion.md).
  Native grid layout handles continuous width changes. Only when auto-fill
  changes column count does a short uniform scale soften visible covers' size
  change. Cards take their new row immediately; there is no position / cross-row
  flight. IntersectionObserver maintains the visible set; geometry reads are
  bounded to that viewport-sized set, offscreen cards never animate, and
  continuous resizing never restarts animations.
  Reversals reuse the active scale. Cover decoding/hover remain independent.
  Leaving the viewport cancels motion; navigation/rebuild disconnect observers.
  Reduced motion cancels ongoing animations and skips future ones.

## Updates and focus

- Selection and validation-progress events update existing controls. Use
  `AppUI.setRowSelected` for row state; keep native `checked`, `aria-selected`
  and button `aria-pressed` in sync. Select-all supports the mixed state.
- Structural/data changes may still call `renderPage`. It captures and restores
  scroll containers on the same page, restores logical focus after replacing a
  focused control, and leaves connected sidebar/topbar focus alone.
- A page change or an import-list page change uses a different scroll identity
  and must not inherit the old container's position.
- Menu position is anchored to the trigger. Scroll events close menus when the
  trigger actually moves, not when delayed scroll-into-view/restoration events
  arrive after the menu opened. Resize and outside clicks dismiss menus.
- Row menus support arrows, Home/End, Escape and return focus. The column picker
  also returns focus on Escape. Modal dismissal retains existing operation
  guards, including busy deletions and pending import cancellation.
- `AppUI.syncModal` locks background roots with `inert`, closes status popovers,
  prevents those popovers from reopening, and locks document scrolling. It
  preserves prior inert state and observes standalone QR/image/resume dialogs.
  The visible top modal owns Escape through its `data-ui-dismiss` control,
  reusing feature-specific cancellation/busy guards. Existing keyboard handling
  traps Tab within that modal. A nonblocking popover is not a modal.

## Regression checks

Run the standard backend/lint checks and fast frontend tests:

```sh
python -m pytest -q
ruff check .
node --test tests/*.test.cjs
```

With Playwright and Chromium available, run the isolated mock-data browser suites:

```sh
node --test --test-concurrency=1 tests/sidebar_browser.cjs tests/ui_foundation_browser.cjs tests/creator_grid_browser.cjs
```

`CHROMIUM_EXECUTABLE` selects an existing Chromium. `CHROMIUM_ARGS` accepts a JSON
array of platform-specific launch arguments; do not disable browser security
for this suite. `SIDEBAR_SCREENSHOTS` and `UI_SCREENSHOTS` optionally save visual
and geometry evidence outside the source tree.

The browser matrix covers 1440/1024/901/900/640/400 CSS-pixel widths, both themes,
expanded/collapsed navigation, long tables, horizontal position, local selection,
background rebuilds, progress, row menus, modal composition, long import lists,
keyboard dismissal, standalone-dialog lock release, and pinned-header alignment
and selection/focus after refresh or sidebar resizing. The sidebar suite covers
rapid interrupted animation, persistence and reduced motion. The pinned-header
suite additionally checks every column and both side edges across 100/125/150/200%
DPI approximations, fractional widths, the 900/901 breakpoint, horizontal scroll
extremes, capped-content sidebar motion, column rebuilds and table entry/exit.
The creator-grid suite checks size-only reflow in both directions at
1440/1200/1024/901px, rapid reversals, cover proportions, retained focus and
scroll, 900/640/480/400/1920px viewport changes, loading images, appended cards,
rebuild/navigation cleanup and reduced motion. A 1200-card test verifies bounded
visible-only measurement/animation work and the sidebar’s eased stop. `GRID_SCREENSHOTS` saves its
screenshots and pre-paint frame geometry outside the source tree.
Test Windows
WebView2/DPI integration separately; Linux Chromium is not a packaged-desktop
or real-account download test.
