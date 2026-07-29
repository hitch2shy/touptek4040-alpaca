# Camera Owner Fault Audit

- Result: PASS
- Cases: 14 / 14
- Manifest violations: 0
- Pytest violations: 0
- Live child violations: 0
- Registry leak violations: 0
- Live segment violations: 0
- Generation invocation violations: 0
- Hard-bound violations: 0
- Connected replacement violations: 0

| Kind | Case | Result | Violations |
|---|---|---|---|
| hard-boundary | startup | PASS |  |
| hard-boundary | connect | PASS |  |
| hard-boundary | get | PASS |  |
| hard-boundary | set | PASS |  |
| hard-boundary | trigger | PASS |  |
| hard-boundary | heater_read | PASS |  |
| hard-boundary | heater_write | PASS |  |
| hard-boundary | frame_publication | PASS |  |
| hard-boundary | abort | PASS |  |
| hard-boundary | disconnect | PASS |  |
| shared-memory | before_creation | PASS |  |
| shared-memory | after_creation | PASS |  |
| shared-memory | after_descriptor_send | PASS |  |
| shared-memory | after_proxy_copy_before_release_ack | PASS |  |
