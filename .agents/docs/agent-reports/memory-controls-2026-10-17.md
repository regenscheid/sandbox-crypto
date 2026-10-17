# Memory positive-control repairs

The broad orchestrator/workspace gate passed 4,586 tests and found two fixture
failures, with 17 opt-in/dependency skips. The legacy inline positive control must
now opt out explicitly from the 8 MiB default read limit. The folded-view fixture
used `getrusage`'s high-water mark, which survives exec and could include the large
pytest parent's pre-exec address space: the unbounded control allocated 84,028,752
Python bytes but reported zero additional peak RSS. It now uses the fresh address
space's `/proc/self/status` VmHWM, consistently with the other memory fixtures.

The [Linux getrusage documentation](https://man7.org/linux/man-pages/man2/getrusage.2.html)
documents preservation across exec; [proc status](https://man7.org/linux/man-pages/man5/proc_pid_status.5.html)
documents VmHWM. No acceptance threshold was relaxed. The repaired memory gates
pass within a 106-test follow-up covering inputs, projects, delegation, export,
migrations and the authenticated download route. Evidence outside Git:
`research-foundations-inputs-final.log`, SHA-256
`5adc5f5d38b7cea5e0b6dd08b38117f74ed9d31d9aca857288fe662766785f59`;
`research-foundations-recovery-broad.log`, SHA-256
`9a12bb9b8be8b65fe8724898dbbe5bfba64dbea73a8343eccced71f01827af0a`.
