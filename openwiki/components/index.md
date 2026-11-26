# Files

- [Dispatch Engine](dispatcher.md) - The dispatcher subsystem — the centralized task state machine, the run_dispatch_cycle lifecycle (in-dispatcher improvement scanner, autonomous recovery routing), multi-repo isolated Git worktrees, worker spawning with a spawn-grace period, the deterministic precommit self-healing gate plus the zero-token anti-slop diff gate, git/PR ops incl. the packaging-failure retry budget and HTTPS-to-SSH push fallback, the per-PR review cap with bounded arbitration, automerge, and the WAL-tolerant stuck-worker reaper.
