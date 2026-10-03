# Terminal MCP curated Tabler icon set

Selected SVG assets from Tabler Icons 3.48.0 (MIT). The full Tabler package is not a Terminal MCP dependency.

## Semantic icon API

Application code should prefer semantic `Icon` names. Tabler filenames are an implementation detail and may be changed centrally without rewriting screens.

### Entities and navigation

| Semantic name | Tabler asset |
| --- | --- |
| `fleet` | `server-2.svg` |
| `slots` | `square-key.svg` |
| `connections` | `plug-connected.svg` |
| `settings` | `settings.svg` |
| `server` | `server.svg` |
| `mesh` | `topology-ring-3.svg` |
| `agents` | `users.svg` |
| `tasks` | `list-check.svg` |
| `activity` | `activity.svg` |
| `context` | `braces.svg` |
| `health` | `heart-rate-monitor.svg` |

### Actions

| Semantic name | Tabler asset | Intended use |
| --- | --- | --- |
| `save` | `device-floppy.svg` | Save staged settings/policy |
| `apply` | `check.svg` | Apply/commit a selected change |
| `reset` | restore.svg` | Restore/reset values |
| `cancel` | `x.svg` | Cancel a dialog/action |
| `close` | `x.svg` | Close a surface |
| `create` | `plus.svg` | Create/add an entity |
| `edit` | `pencil.svg` | Rename/edit |
| `delete` | `trash.svg` | Destructive deletion |
| `retry` | `refresh.svg` | Retry failed work |
| `copy` | `copy.svg` | Copy to clipboard |
| `play` | `player-play.svg` | Start/resume |
| `pause` | `player-pause.svg` | Suspend/pause |
| `stop` | `player-stop.svg` | Cancel/stop armed work |
| `rotate` | `rotate.svg` | Rotate access/trust material |
| `connect` | `plug-connected.svg` | Pair/connect |
| `disconnect` | `plug-off.svg` | Remove/disconnect connection |
| `release` | `unlink.svg` | Release a claim/association |
| `reassign` | `arrows-exchange.svg` | Reassign/transfer |
| `details` | `eye.svg` | View details |
| `back` | `arrow-left.svg` | Contextual back |
| `more` | `dots.svg` | Overflow actions |
| `menu` | `menu-2.svg` | Navigation drawer |
| `access` | `key.svg` | Access/admission action |
| `secure` | `shield-check.svg` | Trust/security action |
| `open-external` | `external-link.svg` | Open external destination |

### States

| Semantic name | Tabler asset |
| --- | --- |
| `loading` | `loader.svg` |
| `success` | `circle-check.svg` |
| `warning` | `alert-triangle.svg` |
| `error` | `alert-circle.svg` |
| `info` | `info-circle.svg` |

Raw compatibility aliases remain available for existing call sites, but new code should use semantic names unless the raw visual is itself the semantic concept (for example disclosure chevrons).

## Rules

1. Use the shared `Icon` primitive; do not import icon SVGs directly in feature components.
2. Prefer semantic names so one product action/entity has one icon everywhere.
3. Add another Tabler SVG only when this curated set has no suitable semantic icon.
4. Keep original 24x24 Tabler geometry and outline style.
5. Decorative icons are aria-hidden; interactive controls retain accessible labels.
6. Keep `LICENSE.tabler.txt` with redistributed icons.
7. Do not add `@tabler/icons` or `@tabler/icons-react` as an application dependency.

Source: Tabler Icons 3.48.0, MIT license.
