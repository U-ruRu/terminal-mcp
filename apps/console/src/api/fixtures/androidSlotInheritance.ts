// Generated from isolated candidate HTTP GET routes, 2026-10-12. No credentials or code receipts.
export const nativeInheritanceFixtures = {
  "standalone_initial": {
    "ok": true,
    "slot": {
      "issuer_node_id": "firstbyte",
      "slot_id": "as_tcl21wahQCYp6NYCGVZzRv4S",
      "logical_agent_id": "la_firstbyte_AXUykJM2Si1oMO8vML5MGLd9",
      "public_name": "Alpha-114",
      "mode": "persistent",
      "state": "active",
      "revision": 2,
      "code_generation": 1,
      "policy": {
        "duration_seconds": 1200,
        "cooldown_seconds": 60,
        "rearm_enabled": true,
        "warning_seconds": 120,
        "draining_seconds": 30,
        "release_on_end": null
      },
      "inheritance": {
        "mode": "inherit",
        "source": "standalone",
        "parent": {
          "source": "standalone",
          "id": "firstbyte",
          "revision": 1,
          "version": "server:firstbyte:1",
          "freshness": "authoritative",
          "convergence": "converged"
        },
        "reset_supported": true,
        "applies_to": "next_window",
        "pending": false
      },
      "inheritance_supported": true,
      "policy_source": "server",
      "policy_override": null,
      "inherited_policy": {
        "duration_seconds": 1200,
        "cooldown_seconds": 60,
        "rearm_enabled": true,
        "warning_seconds": 120,
        "draining_seconds": 30,
        "release_on_end": null
      },
      "parent": {
        "source": "server",
        "revision": 1
      },
      "current_policy": {
        "duration_seconds": 1200,
        "cooldown_seconds": 60,
        "rearm_enabled": true,
        "warning_seconds": 120,
        "draining_seconds": 30,
        "release_on_end": null
      },
      "policy_applies_to": "next_session",
      "session_lifecycle": {
        "state": "active",
        "started_at": "2030-01-01T00:00:00.000000+00:00",
        "hard_expires_at": "2030-01-01T00:20:00.000000+00:00",
        "remaining_seconds": 1200,
        "rearm_at": null
      }
    }
  },
  "standalone_defaults": {
    "ok": true,
    "revision": 2,
    "policy": {
      "cooldown_seconds": 60,
      "draining_seconds": 30,
      "duration_seconds": 10800,
      "rearm_enabled": true,
      "release_on_end": null,
      "warning_seconds": 120
    },
    "legacy_enabled": true,
    "inherit_policy": false,
    "parent": {
      "legacy_enabled": true,
      "policy": {
        "cooldown_seconds": 60,
        "draining_seconds": 30,
        "duration_seconds": 1200,
        "rearm_enabled": true,
        "release_on_end": null,
        "warning_seconds": 120
      },
      "revision": 1,
      "source": "server"
    },
    "policy_source": "server",
    "issuer_node_id": "firstbyte",
    "write_target": "access_defaults",
    "inheritance": {
      "mode": "override",
      "source": "standalone",
      "parent": {
        "source": "standalone",
        "id": "firstbyte",
        "revision": 1,
        "version": "standalone:firstbyte:1",
        "freshness": "authoritative",
        "convergence": "converged"
      },
      "reset_supported": true,
      "applies_to": "next_window",
      "pending": false
    },
    "inheritance_supported": true,
    "mesh_policy_supported": false,
    "policy_override": {
      "cooldown_seconds": 60,
      "draining_seconds": 30,
      "duration_seconds": 10800,
      "rearm_enabled": true,
      "release_on_end": null,
      "warning_seconds": 120
    },
    "inherited_policy": {
      "cooldown_seconds": 60,
      "draining_seconds": 30,
      "duration_seconds": 1200,
      "rearm_enabled": true,
      "release_on_end": null,
      "warning_seconds": 120
    },
    "policy_applies_to": "next_session"
  },
  "future_slot": {
    "ok": true,
    "slot": {
      "issuer_node_id": "firstbyte",
      "slot_id": "as_tcl21wahQCYp6NYCGVZzRv4S",
      "logical_agent_id": "la_firstbyte_AXUykJM2Si1oMO8vML5MGLd9",
      "public_name": "Alpha-114",
      "mode": "persistent",
      "state": "active",
      "revision": 3,
      "code_generation": 1,
      "policy": {
        "duration_seconds": 10800,
        "cooldown_seconds": 60,
        "rearm_enabled": true,
        "warning_seconds": 120,
        "draining_seconds": 30,
        "release_on_end": null
      },
      "inheritance": {
        "mode": "inherit",
        "source": "standalone",
        "parent": {
          "source": "standalone",
          "id": "firstbyte",
          "revision": 2,
          "version": "server:firstbyte:2",
          "freshness": "authoritative",
          "convergence": "converged"
        },
        "reset_supported": true,
        "applies_to": "next_window",
        "pending": true
      },
      "inheritance_supported": true,
      "policy_source": "server",
      "policy_override": null,
      "inherited_policy": {
        "cooldown_seconds": 60,
        "draining_seconds": 30,
        "duration_seconds": 10800,
        "rearm_enabled": true,
        "release_on_end": null,
        "warning_seconds": 120
      },
      "parent": {
        "source": "server",
        "revision": 2
      },
      "current_policy": {
        "duration_seconds": 1200,
        "cooldown_seconds": 60,
        "rearm_enabled": true,
        "warning_seconds": 120,
        "draining_seconds": 30,
        "release_on_end": null
      },
      "policy_applies_to": "next_session",
      "session_lifecycle": {
        "state": "active",
        "started_at": "2030-01-01T00:00:00.000000+00:00",
        "hard_expires_at": "2030-01-01T00:20:00.000000+00:00",
        "remaining_seconds": 1140,
        "rearm_at": null
      }
    }
  },
  "override_slot": {
    "ok": true,
    "slot": {
      "issuer_node_id": "firstbyte",
      "slot_id": "as_tcl21wahQCYp6NYCGVZzRv4S",
      "logical_agent_id": "la_firstbyte_AXUykJM2Si1oMO8vML5MGLd9",
      "public_name": "Alpha-114",
      "mode": "persistent",
      "state": "active",
      "revision": 4,
      "code_generation": 1,
      "policy": {
        "duration_seconds": 7200,
        "cooldown_seconds": 60,
        "rearm_enabled": true,
        "warning_seconds": 120,
        "draining_seconds": 30,
        "release_on_end": null
      },
      "inheritance": {
        "mode": "override",
        "source": "slot",
        "parent": {
          "source": "standalone",
          "id": "firstbyte",
          "revision": 2,
          "version": "server:firstbyte:2",
          "freshness": "authoritative",
          "convergence": "converged"
        },
        "reset_supported": true,
        "applies_to": "next_window",
        "pending": true
      },
      "inheritance_supported": true,
      "policy_source": "slot",
      "policy_override": {
        "duration_seconds": 7200,
        "cooldown_seconds": 60,
        "rearm_enabled": true,
        "warning_seconds": 120,
        "draining_seconds": 30,
        "release_on_end": null
      },
      "inherited_policy": {
        "cooldown_seconds": 60,
        "draining_seconds": 30,
        "duration_seconds": 10800,
        "rearm_enabled": true,
        "release_on_end": null,
        "warning_seconds": 120
      },
      "parent": {
        "source": "server",
        "revision": 2
      },
      "current_policy": {
        "duration_seconds": 1200,
        "cooldown_seconds": 60,
        "rearm_enabled": true,
        "warning_seconds": 120,
        "draining_seconds": 30,
        "release_on_end": null
      },
      "policy_applies_to": "next_session",
      "session_lifecycle": {
        "state": "active",
        "started_at": "2030-01-01T00:00:00.000000+00:00",
        "hard_expires_at": "2030-01-01T00:20:00.000000+00:00",
        "remaining_seconds": 1140,
        "rearm_at": null
      }
    }
  },
  "reset_slot": {
    "ok": true,
    "slot": {
      "issuer_node_id": "firstbyte",
      "slot_id": "as_tcl21wahQCYp6NYCGVZzRv4S",
      "logical_agent_id": "la_firstbyte_AXUykJM2Si1oMO8vML5MGLd9",
      "public_name": "Alpha-114",
      "mode": "persistent",
      "state": "active",
      "revision": 5,
      "code_generation": 1,
      "policy": {
        "duration_seconds": 10800,
        "cooldown_seconds": 60,
        "rearm_enabled": true,
        "warning_seconds": 120,
        "draining_seconds": 30,
        "release_on_end": null
      },
      "inheritance": {
        "mode": "inherit",
        "source": "standalone",
        "parent": {
          "source": "standalone",
          "id": "firstbyte",
          "revision": 2,
          "version": "server:firstbyte:2",
          "freshness": "authoritative",
          "convergence": "converged"
        },
        "reset_supported": true,
        "applies_to": "next_window",
        "pending": true
      },
      "inheritance_supported": true,
      "policy_source": "server",
      "policy_override": null,
      "inherited_policy": {
        "cooldown_seconds": 60,
        "draining_seconds": 30,
        "duration_seconds": 10800,
        "rearm_enabled": true,
        "release_on_end": null,
        "warning_seconds": 120
      },
      "parent": {
        "source": "server",
        "revision": 2
      },
      "current_policy": {
        "duration_seconds": 1200,
        "cooldown_seconds": 60,
        "rearm_enabled": true,
        "warning_seconds": 120,
        "draining_seconds": 30,
        "release_on_end": null
      },
      "policy_applies_to": "next_session",
      "session_lifecycle": {
        "state": "active",
        "started_at": "2030-01-01T00:00:00.000000+00:00",
        "hard_expires_at": "2030-01-01T00:20:00.000000+00:00",
        "remaining_seconds": 1140,
        "rearm_at": null
      }
    }
  },
  "mesh_defaults": {
    "ok": true,
    "revision": 4,
    "policy": {
      "cooldown_seconds": 180,
      "draining_seconds": 60,
      "duration_seconds": 10800,
      "rearm_enabled": true,
      "release_on_end": null,
      "warning_seconds": 180
    },
    "legacy_enabled": true,
    "inherit_policy": true,
    "parent": {
      "control_node_id": "firstbyte",
      "legacy_enabled": true,
      "mesh_id": "fixture-mesh",
      "policy": {
        "cooldown_seconds": 180,
        "draining_seconds": 60,
        "duration_seconds": 10800,
        "rearm_enabled": true,
        "release_on_end": null,
        "warning_seconds": 180
      },
      "revision": 2,
      "source": "mesh"
    },
    "policy_source": "mesh",
    "issuer_node_id": "firstbyte",
    "write_target": "access_defaults",
    "inheritance": {
      "mode": "inherit",
      "source": "mesh",
      "parent": {
        "source": "mesh",
        "id": "fixture-mesh",
        "revision": 2,
        "version": "mesh:fixture-mesh:2",
        "freshness": "authoritative",
        "convergence": "converged"
      },
      "reset_supported": true,
      "applies_to": "next_window",
      "pending": false
    },
    "inheritance_supported": true,
    "mesh_policy_supported": true,
    "policy_override": null,
    "inherited_policy": {
      "cooldown_seconds": 180,
      "draining_seconds": 60,
      "duration_seconds": 10800,
      "rearm_enabled": true,
      "release_on_end": null,
      "warning_seconds": 180
    },
    "policy_applies_to": "next_session"
  },
  "mesh_slot": {
    "ok": true,
    "slot": {
      "issuer_node_id": "firstbyte",
      "slot_id": "as_fvExA1SDYblC07g8C3vgi398",
      "logical_agent_id": "la_firstbyte_704-vkaw8x3LJSNe58Sf6xSh",
      "public_name": "X-ray-178",
      "mode": "persistent",
      "state": "active",
      "revision": 1,
      "code_generation": 1,
      "policy": {
        "duration_seconds": 10800,
        "cooldown_seconds": 180,
        "rearm_enabled": true,
        "warning_seconds": 180,
        "draining_seconds": 60,
        "release_on_end": null
      },
      "inheritance": {
        "mode": "inherit",
        "source": "mesh",
        "parent": {
          "source": "mesh",
          "id": "fixture-mesh",
          "revision": 4,
          "version": "server:firstbyte:4",
          "freshness": "authoritative",
          "convergence": "converged"
        },
        "reset_supported": true,
        "applies_to": "next_window",
        "pending": false
      },
      "inheritance_supported": true,
      "policy_source": "mesh",
      "policy_override": null,
      "inherited_policy": {
        "cooldown_seconds": 180,
        "draining_seconds": 60,
        "duration_seconds": 10800,
        "rearm_enabled": true,
        "release_on_end": null,
        "warning_seconds": 180
      },
      "parent": {
        "source": "mesh",
        "revision": 4
      },
      "current_policy": {
        "duration_seconds": 10800,
        "cooldown_seconds": 180,
        "rearm_enabled": true,
        "warning_seconds": 180,
        "draining_seconds": 60,
        "release_on_end": null
      },
      "policy_applies_to": "next_session",
      "session_lifecycle": {
        "state": "idle",
        "started_at": null,
        "hard_expires_at": null,
        "remaining_seconds": 0,
        "rearm_at": null
      }
    }
  }
} as const
