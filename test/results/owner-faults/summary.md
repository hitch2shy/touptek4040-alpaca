# Camera Owner Fault Audit

- Result: PASS
- Cases: 14 / 14
- Live child processes: 0
- Registered shared-memory leaks: 0
- Live shared-memory segments: 0
- Duplicate owner operations: 0
- Hard-bound violations: 0

| Kind | Case | Result |
|---|---|---|
| hard-boundary | startup | PASS |
| hard-boundary | connect | PASS |
| hard-boundary | get | PASS |
| hard-boundary | set | PASS |
| hard-boundary | trigger | PASS |
| hard-boundary | heater_read | PASS |
| hard-boundary | heater_write | PASS |
| hard-boundary | frame_publication | PASS |
| hard-boundary | abort | PASS |
| hard-boundary | disconnect | PASS |
| shared-memory | before_creation | PASS |
| shared-memory | after_creation | PASS |
| shared-memory | after_descriptor_send | PASS |
| shared-memory | after_proxy_copy_before_release_ack | PASS |
