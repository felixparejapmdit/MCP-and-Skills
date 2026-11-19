# MCP & Skills design system

## Reference and direction

Chosen reference: [Linear in VoltAgent Awesome Design MD](https://github.com/voltagent/awesome-design-md/blob/main/design-md/linear.app/DESIGN.md). This is a third-party design analysis, adapted to this application's tasks rather than an official Linear specification.

The interface uses precise developer-tool navigation, neutral surfaces, hairline dividers, compact information, and a restrained lavender accent. It preserves the app's actual inventory, source checks, global update settings, and activity. It introduces prominent agent selection and installation states. It avoids a marketing hero, decorative gradients, fictitious metrics, and external font dependencies.

## Surface

Mode: Operate. People need to inspect tools, identify a gap, and understand an update result quickly.

Desktop uses a persistent compact sidebar, a topbar, a concise overview, and grouped task sections. Mobile collapses navigation and stacks controls. Large inventories use searchable rows and collapsible skill collections. Tables either adapt their rows or remain contained in accessible horizontal scroll regions; the page itself must not overflow.

## Color and type

- Dark: graphite canvas, charcoal panels, light neutral text, muted secondary text, and subtle gray borders.
- Light: cool off-white canvas, white panels, dark neutral text, and equivalent visible gray borders.
- Accent: Linear's lavender family, rooted at `#5e6ad2`, reserved for selection, primary actions, links, and focus.
- Status: green for available/current, amber for missing or attention, and red for failures. Text and icons accompany color.
- Type: system sans stack with SF Pro/Segoe UI fallbacks. Headings use moderate negative tracking; compact monospaced text is reserved for package names and versions.
- Body text is 14px; functional metadata and labels have an 11px minimum across breakpoints.
- Components: 4px spacing rhythm, 8px controls, restrained 12px panel corners, surface elevation through borders rather than decorative shadows.

Light, dark, and system preferences persist locally. The theme applies before the first render and respects reduced motion. The reference's marketing-only restrictions yield to the requested application light mode.

## Interaction and truth

The agent selector offers all agents, Claude, Codex, and Gemini. Selection persists and scopes inventory, missing items, and recommendations. Global update actions state their global effect. Native capabilities retain their compatibility scope.

Installed, Missing, and Not installed are distinct searchable states. Removed items retain their last known identity; a failed scan does not invent removals. Account connection badges remain explicitly based on saved health evidence. New items link to their publisher for review.

Keyboard focus is visible, controls are labeled, mobile touch targets are at least 44px, and text remains readable at zoom. Layouts are checked at 320, 375, 768, 1024, 1440, and 1920 CSS pixels in both themes.

## PWA and offline behavior

The manifest supplies local icons, an app name, scope, and standalone display. The service worker caches static assets and a generic offline shell. It never caches live authorization-bearing HTML, API responses, or mutation requests. A sanitized, dated inventory snapshot supports offline browsing, while update controls remain disabled. Reconnection obtains a fresh session token in memory.
