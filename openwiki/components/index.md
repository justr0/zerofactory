# Files

- [Dispatch Engine](dispatcher.md) - The dispatcher subsystem — the centralized 13-stage task state machine, the run_dispatch_cycle lifecycle (in-dispatcher improvement scanner, autonomous recovery routing), multi-repo isolated Git worktrees, worker spawning with a spawn-grace period, the deterministic precommit self-healing gate plus the zero-token anti-slop diff gate, git/PR ops incl. the packaging-failure retry budget and the HTTPS-to-SSH push fallback, the per-commit review cap, automerge, and the WAL-tolerant stuck-worker reaper.
