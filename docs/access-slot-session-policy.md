# Access slot and session policy

The Access service owns the slot registry. A slot reserves its four-digit number,
numeric NATO name and policy. It has no agent identity of its own. A manually
created slot has no session until it is first opened. The session owns the logical
agent identity; reopening that session keeps the identity, including when a new
native work session is needed for a fresh time window.

## Start and initial admission

The public Access method remains `session`. No new agent method is introduced.

```json
{"action": "start", "session_number": "0042"}
```

An explicit number selects an existing slot. An unknown explicit number is an
error and never falls back to creating another slot. Omitting the number reuses
the Access connector's existing binding. If there is neither a number nor a
binding, **Legacy** is the sole policy switch allowing automatic allocation of a
new temporary slot and number. Legacy defaults to disabled. It does not govern
reopening an existing slot.

```json
{"action": "end"}
```

End closes the session bound to the Access connector; it does not accept a number.
Slot kind and Legacy do not alter the explicit reopening calculation. A suspended
slot retains its existing suspension behavior.

## Deadlines and cooldown

An active session that has not been explicitly ended treats a repeated start as
idempotent: it retains the current deadline, native work session and intent.

After an explicit end, cooldown is measured from the actual end time:

| Start time | Result |
| --- | --- |
| Before `ended_at + cooldown` | Resume with the original deadline. |
| At or after `ended_at + cooldown` | Start a full window using the current duration. |

For example, ending at 12:05 with a one-minute cooldown preserves an original
20:00 deadline for a start before 12:06. Starting at 12:06 grants the full current
duration from 12:06, even though the old deadline has not passed. An early resume
retains the original end marker so it does not move the cooldown boundary.

Natural expiry uses the actual deadline as the end time. Discovering an expired
session later does not restart its cooldown. Starting during cooldown cannot
authorize work beyond an already expired original deadline.

Changing future policy preserves the currently granted deadline and the cooldown
captured for that window. A full new opening applies the current duration.

## Rearm

Console labels the existing `rearm_enabled` switch **Rearm**. It controls automatic
opening of an established session after cooldown. An explicit Access start does
not require Rearm. Enabling Rearm applies to the next automatic opening even if
the previous window captured a policy with Rearm disabled.

Attach and write admission use the same authoritative transition. A Mesh member
forwards automatic opening by immutable slot ID; a recycled number cannot cause
it to open another slot. Read-only status does not create a new window. Unused
slots do not renew in the background merely because Rearm is enabled.

## Retention and physical deletion

All slots, including manually created slots, are eligible for removal after 72
hours without use when no active session remains. For a never-used slot the
reference is creation time. For used slots the reference includes recorded use
and the end of the last allowed session window, so a primary does not remove a
slot while a Mesh member could still be using it. An explicitly ended window uses
its actual end time. Cleanup performs its final eligibility check in the deletion
transaction, preventing a concurrent touch or start from being lost.

Deletion removes the slot, its associated sessions and bindings, and its number
claim. The number and numeric NATO name can be allocated again. Console confirms
deletion through absence and removes the slot from its caches. There is no
`deleted` slot state or deleted-slot item in the list.

Replication keeps the deletion event in its journal to reject stale replay. That
event is not a slot or a session. A terminal deletion can converge on a replica
that missed earlier slot revisions without requesting a deleted-slot snapshot.
Late recipient holds cannot reserve the freed number again.

## Standalone and Mesh

Both modes use the same slot, session, deadline and retention calculations. Mesh
adds transport to the registry authority. Start and end metadata carry the
issuer's revision so equal timestamps and delayed delivery retain their exact
event order. Snapshot serialization reads the slot and its session together.

The storage change separates the current schema's slot identity into session
storage. It preserves current data and introduces no retired slot modes or
alternative compatibility protocol.
