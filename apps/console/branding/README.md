# Terminal MCP Console branding

These six PNG files are the approved M5 branding masters. Keep them byte-for-byte intact; `scripts/verify-branding-assets.mjs` pins their SHA-256 hashes.

Derived Android resources use the masters as follows:

- `adapted-background.png` -> adaptive launcher background and splash background;
- `adapted-foreground.png` -> adaptive launcher foreground, preserving its built-in safe-zone padding;
- `adapted-themed.png` -> Android 13+ monochrome launcher layer;
- `adapted-gplay.png` -> legacy/round launcher fallback and Web favicon;
- `adapted-notifications.png` -> Android small-notification icon as a white alpha mask for system tinting;
- `adapted-splash.png` -> centered splash foreground over the approved background.

The density-specific launcher/notification resources and orientation-specific splash rasters are committed so Android builds require no image-processing dependency. Physical launcher-mask and notification-status rendering remains part of combined device acceptance.
