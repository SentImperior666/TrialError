"""vast.ai as a rented GPU: for the DEV worker's marker OCR, and for the
embed offload queue (the public TrialError copy's embedding backend; this
package is a strict superset of its ``trialerror.vastai``).

Design: ``docs/VASTAI_OCR_DESIGN.md`` (OCR) and
``docs/VASTAI_EMBED_DESIGN.md`` (embedding). The OCR switch is DEV's
``[ingest.ocr] executor = "vastai"``; with it absent (``"local"``) nothing of
the OCR lane runs and the DEV worker behaves byte for byte as before. The
embedding switch is ``[ingest.embed] gpu = "vastai"``, and only
``trialerror vastai run`` rents for it.

Module map and public API::

    errors.py   REASON_CODES; VastError(.next_actions), VastConfigError (also
                a ValueError; the package's one config error),
                VastApiError(.status), VastKeyMissing, OfferUnavailable(.offer_id),
                HostFailure(.machine_id) [F]; JobRefused [R, a
                trialerror.offload.settle.ClaimReturned] and its subclasses
                EgressRefused, VastSpendRefused, VastPlanRefused,
                StackMismatch (disable_executor = True)
    config.py   load_vast_config(toml, *, config_root) -> VastConfig (frozen;
                .enabled, .status, .ocr, .egress, .active_tier, .gpu_factor());
                CONFIG_KEYS (ConfigKey: table, key, default, default_text,
                meaning); runtime_files(cfg); check_runtime_files(cfg);
                LICENSE_TIERS, NAMEABLE_LICENSE_TIERS
    api.py      VastClient(api_key_path, *, http, key_reader): search_offers
                (the public keyword form, or the OCR lane's query), create_instance, list_instances, show_instance,
                destroy_instance, account_credit; read_api_key; urllib_http
                (the one network function)
    tiers.py    Tier (.admits, .refusals), make_tier, DEFAULT_TIERS,
                DEFAULT_TIER, effective_dph, normalise_gpu_name,
                FORBIDDEN_KEYS, RENTAL_TYPE, ABSOLUTE_TTL_CAP_S; the embedding
                lane's model: DEV_TOKENS_PER_S, TOKENS_PER_BYTE,
                DEFAULT_GPU_FACTORS, VastConfig, load_vast_config(raw,
                program_root), PlanRefused, Plan, estimate_tokens, ttl_for,
                rank_offers, plan_run; VastConfigError (re-exported)
    pricing.py  plan_document, offer_query, offer_refusals, estimate_offer,
                rank_estimates, price_job -> PricedJob, check_envelopes,
                spend_from_ledger, job_cap_usd, approval_cap_usd,
                derived_range_timeout_s
    guard.py    program_fingerprint, canonical_json, mac, parse_ts,
                is_interactive; the high-tier approval (verify / mint /
                banner), HighTierRefused(.reason_code)
    egress.py   egress_digest, egress_summary, approval_body,
                sign_egress_approval, write_egress_approval,
                read_egress_approval, verify_egress_approval, document_facts,
                decide_egress -> EgressDecision
    ledger.py   Ledger(state_dir).append(kind, **fields) / .read() ->
                LedgerRead(rows, torn); ledger_path, default_state_dir,
                utc_iso; lease_spend, spent_under_approval, spent_in_run,
                unsettled_leases, spend_view -> SpendView
    lease.py    OcrInstanceLease (the OCR lane's context manager),
                LeaseExpired [X, a KeyboardInterrupt; the package's one],
                make_label, parse_label, new_run_id, state_runs_dir,
                read_state_run_records, write_run_record,
                record_for_instance; the embedding lane's InstanceLease,
                runs_dir(program_root), read_run_records(program_root)
    reaper.py   reap(client, program_root, *, dry_run, ...) and classify (the
                embedding lane's, the public policy; never a live OCR lease);
                reap_ocr(client, *, config_root, state_dir, ledger, dry_run,
                ...) and classify_ocr (the OCR lane's, VOCR- only); pid_alive
    remote.py   the embedding lane's channel and backend: SshChannel,
                RemoteEmbedBackend, SERVE_SOURCE, REMOTE_DIR,
                module_bytes_and_sha; LeaseExpired (re-exported)
    runner.py   the embedding lane's run: VastRunRefused, select_embed_jobs,
                prepare_run, run_vastai (events vastai_run,
                vastai_destroy_failed, vastai_high_tier_use)
    checks.py   doctor category ``vastai``: vastai_live_instances,
                vastai_high_tier (both lanes' run records and high-tier
                use), vastai_ocr_egress, vastai_ocr_ledger
    shell.py    RemoteShell (Protocol), SshShell (hardened ssh, one command
                per call), wait_reachable, upload_verified /
                download_verified (sha256-checked), lease_dir, known_hosts_path,
                q; REMOTE_ROOT_SHM, REMOTE_ROOT_DISK, SSH_HARDENING_OPTIONS;
                ShellError, ConnectionLost, IdentityRejected, RemoteTimeout,
                TransferMismatch
    ocr.py      VastaiMarkerOcrBackend (.from_toml; admit, run, startup,
                runtime_report, on_pause, close, result_fields): the
                ``executor = "vastai"`` OCR backend the offload worker builds;
                VastRunState (per worker run: run id, disable latch);
                canary_text, canary_similarity, parse_models_manifest,
                PEAK_RSS_SOURCE_REMOTE, TOOL_FILES
    envlock.py  PACKAGED_PINS, parse_pins, release_hashes, build_lock ->
                LockResult, write_lock, EnvLockError; urllib_get (PyPI, the
                ``lock-deps`` network function); IMAGE_SUPPLIED
    remote/     package data (beside remote.py; found by path, never
                imported): the OCR lane's instance-side tools
                (ocr.TOOL_FILES: the bootstrap, the range wrapper, the
                canary), the pins that ``lock-deps`` reads, and the marker
                model manifest

There is deliberately no keep-alive, reuse or TTL-extension option anywhere
in this package, and no interruptible (``bid``) rental.
"""
