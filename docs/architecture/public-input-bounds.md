# Public MCP input bounds

All agent-controlled public MCP inputs are finite and rejected by the public schema before application execution. Existing stricter task limits remain authoritative.

| Semantic field | Limit |
| --- | ---: |
| Access code | 4 digits |
| Session display name | 80 chars |
| Namespace / task id / public agent name | 120 chars |
| Opaque cursor | 512 chars |
| Command/message hash | 128 chars |
| Tag | 64 chars |
| Tags per request | 50 |
| Message text | 16 Ki characters |
| Command / recovery command | 32 Ki characters |
| Task scope | 256 chars |
| Context summary | 100 chars |
| Context content | 32 Ki characters |
| Queue id | 1..65,535 |
| Context id / task revision | 1..2^63-1 |
| Task resource_context | 8 KiB serialized UTF-8 JSON |
| Task checkpoint | 16 KiB serialized UTF-8 JSON |
| Task result | 32 KiB serialized UTF-8 JSON |
| Task review evidence | 16 KiB serialized UTF-8 JSON |

Structured task extension values are measured after deterministic JSON serialization. Their JSON Schema exposes `x-maxSerializedBytes`, and the shared Application request model enforces the same byte budget. Larger material belongs in bounded context, files/attachments, or explicit references rather than unbounded inline JSON.
