// Synthetic slot contract fixtures based on isolated TestClient source e515d2b.
// Contains read projections only; no codes, selectors, keys or production state.
export const runtimeSlotFixtures = {
  "managed_denial": {
    "ok": false,
    "code": "authority_unavailable",
    "error": "authority_unavailable"
  },
  "access_defaults": {
    "ok": true,
    "revision": 1,
    "legacy_admission_enabled": false,
    "policy": {
      "duration_seconds": 1200,
      "cooldown_seconds": 60,
      "rearm_enabled": true,
      "warning_seconds": 120,
      "draining_seconds": 30,
      "release_on_end": null
    },
  },
  "access_persistent_initial": {
    "ok": true,
    "slot": {
      "issuer_node_id": "firstbyte",
      "slot_id": "as_ErCTdZoFVVcdDx02wh8U9q2j",
      "public_name": "Charlie-136",
      "mode": "persistent",
      "state": "active",
      "revision": 1,
      "policy": {
        "duration_seconds": 10800,
        "cooldown_seconds": 60,
        "rearm_enabled": true,
        "warning_seconds": 180,
        "draining_seconds": 90,
        "release_on_end": null
      },
      "session_lifecycle": {
        "state": "idle",
        "started_at": null,
        "hard_expires_at": null,
        "remaining_seconds": 0,
        "rearm_at": null
      }
    }
  },
  "access_persistent_active": {
    "ok": true,
    "slot": {
      "issuer_node_id": "firstbyte",
      "slot_id": "as_ErCTdZoFVVcdDx02wh8U9q2j",
      "public_name": "Charlie-136",
      "mode": "persistent",
      "state": "active",
      "revision": 2,
      "policy": {
        "duration_seconds": 10800,
        "cooldown_seconds": 60,
        "rearm_enabled": true,
        "warning_seconds": 180,
        "draining_seconds": 90,
        "release_on_end": null
      },
      "session_lifecycle": {
        "state": "active",
        "started_at": "2026-10-11T23:08:23.970035+00:00",
        "hard_expires_at": "2026-10-12T02:08:23.970035+00:00",
        "remaining_seconds": 10799,
        "rearm_at": null
      }
    }
  },
  "access_persistent_changed": {
    "ok": true,
    "slot": {
      "issuer_node_id": "firstbyte",
      "slot_id": "as_ErCTdZoFVVcdDx02wh8U9q2j",
      "public_name": "Charlie-136",
      "mode": "persistent",
      "state": "active",
      "revision": 3,
      "policy": {
        "duration_seconds": 18000,
        "cooldown_seconds": 60,
        "rearm_enabled": true,
        "warning_seconds": 180,
        "draining_seconds": 90,
        "release_on_end": null
      },
      "session_lifecycle": {
        "state": "active",
        "started_at": "2026-10-11T23:08:23.970035+00:00",
        "hard_expires_at": "2026-10-12T04:08:23.970035+00:00",
        "remaining_seconds": 17999,
        "rearm_at": null
      }
    }
  },
  "access_legacy_initial": {
    "ok": true,
    "slot": {
      "issuer_node_id": "firstbyte",
      "slot_id": "as_hrFa0-dIwy_6f343HH1OiXQi",
      "public_name": "Papa-220",
      "mode": "temporary",
      "state": "active",
      "revision": 1,
      "policy": {
        "duration_seconds": 10800,
        "cooldown_seconds": 60,
        "rearm_enabled": true,
        "warning_seconds": 180,
        "draining_seconds": 90,
        "release_on_end": null
      },
      "session_lifecycle": {
        "state": "active",
        "started_at": "2026-10-11T23:08:24.038263+00:00",
        "hard_expires_at": "2026-10-12T02:08:24.038263+00:00",
        "remaining_seconds": 10799,
        "rearm_at": null
      }
    }
  },
  "access_collection": {
    "ok": true,
    "slots": [
      {
        "issuer_node_id": "firstbyte",
        "slot_id": "as_ErCTdZoFVVcdDx02wh8U9q2j",
        "public_name": "Charlie-136",
        "mode": "persistent",
        "state": "active",
        "revision": 3,
        "policy": {
          "duration_seconds": 18000,
          "cooldown_seconds": 60,
          "rearm_enabled": true,
          "warning_seconds": 180,
          "draining_seconds": 90,
          "release_on_end": null
        },
        "session_lifecycle": {
          "state": "active",
          "started_at": "2026-10-11T23:08:23.970035+00:00",
          "hard_expires_at": "2026-10-12T04:08:23.970035+00:00",
          "remaining_seconds": 17999,
          "rearm_at": null
        }
      },
      {
        "issuer_node_id": "firstbyte",
        "slot_id": "as_hrFa0-dIwy_6f343HH1OiXQi",
        "public_name": "Papa-220",
        "mode": "temporary",
        "state": "active",
        "revision": 1,
        "policy": {
          "duration_seconds": 10800,
          "cooldown_seconds": 60,
          "rearm_enabled": true,
          "warning_seconds": 180,
          "draining_seconds": 90,
          "release_on_end": null
        },
        "session_lifecycle": {
          "state": "active",
          "started_at": "2026-10-11T23:08:24.038263+00:00",
          "hard_expires_at": "2026-10-12T02:08:24.038263+00:00",
          "remaining_seconds": 10799,
          "rearm_at": null
        }
      }
    ],
    "next_cursor": null
  }
} as const
