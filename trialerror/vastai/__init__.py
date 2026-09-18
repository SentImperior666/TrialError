"""vast.ai as a second executor for the embed offload queue.

Design: ``docs/VASTAI_EMBED_DESIGN.md``. The switch is one line,
``[ingest.embed] gpu = "vastai"``; nothing about the handlers, the manifests,
the verification (``trialerror.offload.protocol.verify_published``) or the
``emb`` write path changes.

Module map::

    tiers.py    config loading, the precalculated low/mid/high tiers, the
                throughput/TTL/cost model and offer ranking
    guard.py    the high-tier approval (HMAC keyed by the vast.ai API key,
                TTY-only minting), the stderr banner
    api.py      the vast.ai REST client (stdlib urllib; injectable ``http``
                so tests never touch the network) and the key-file reader
    remote.py   the SSH JSON-lines channel to the instance and the
                ``RemoteEmbedBackend`` the offload worker code drives
    lease.py    create -> run -> destroy-in-finally, with the TTL watchdog
                and the local run records the reaper reads
    runner.py   ``trialerror vastai run``: select embed markers, plan, guard,
                lease, reuse ``offload.worker._process_one`` per job
    reaper.py   ``trialerror vastai reap``: destroy tagged instances past
                their deadline or orphaned
    checks.py   doctor: ``vastai_high_tier``, ``vastai_live_instances``

There is deliberately no keep-alive, reuse or TTL-extension option anywhere
in this package (design section 4.2 item 7).
"""
