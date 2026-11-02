"""Planted truth: anomalies, waste, and planned changes layered onto the catalog.

Every item here becomes a row in RAW.GROUND_TRUTH. Its dollar impact is not written by hand.
The engine measures it by simulating the warehouse with and without the item.
"""

from __future__ import annotations

from dataclasses import dataclass

from spend_desk.generate.catalog import Modifier, P, Warehouse, build_warehouses


@dataclass
class Truth:
    truth_id: str
    category: str  # anomaly | waste | planned_change
    truth_type: str
    warehouse_name: str
    start: str
    end: str | None
    description: str
    expected_action: str
    user_name: str | None = None
    pattern_key: str | None = None
    linked_truth_id: str | None = None


def _wh(whs: list[Warehouse], name: str) -> Warehouse:
    return next(w for w in whs if w.name == name)


def _pattern(wh: Warehouse, key: str):
    return next(p for p in wh.patterns if p.key == key)


def build_world() -> tuple[list[Warehouse], list[Truth]]:
    whs = build_warehouses()
    truth: list[Truth] = []

    def plant(t: Truth, *mods: Modifier, patterns=()):
        wh = _wh(whs, t.warehouse_name)
        for m in mods:
            m.truth_id = m.truth_id or t.truth_id
            wh.modifiers.append(m)
        for p in patterns:
            p.truth_id = t.truth_id
            wh.patterns.append(p)
        truth.append(t)

    # Planned changes during warm up. Real growth, not anomalies.
    plant(
        Truth(
            "C01",
            "planned_change",
            "onboarding_step",
            "CD_SYNC_WH",
            "2025-06-02",
            None,
            "New CDP source onboarded, profile sync volume up 50 percent.",
            "None",
        ),
        Modifier("scale", "2025-06-02", None, "cd_sync_profiles", 1.5, role="base"),
    )
    plant(
        Truth(
            "C02",
            "planned_change",
            "onboarding_step",
            "ML_INFER_WH",
            "2025-07-14",
            None,
            "Second scoring model launched, scoring volume up 40 percent.",
            "None",
        ),
        Modifier("scale", "2025-07-14", None, "ml_infer_score", 1.4, role="base"),
    )
    plant(
        Truth(
            "C03",
            "planned_change",
            "onboarding_step",
            "PA_BI_WH",
            "2025-08-04",
            None,
            "New executive dashboard suite, dashboard queries up 30 percent.",
            "None",
        ),
        Modifier("scale", "2025-08-04", None, "pa_bi_dash_core", 1.3, role="base"),
    )

    # ---------------- Anomalies, validation window ----------------
    plant(
        Truth(
            "A01",
            "anomaly",
            "runaway_query_loop",
            "PA_EXPERIMENT_WH",
            "2025-10-14 15:00",
            "2025-10-17 03:00",
            "Analyst notebook re-ran an exploratory query in a loop for 60 hours.",
            "Cancel the session and add a STATEMENT_TIMEOUT_IN_SECONDS on PA_EXPERIMENT_WH.",
            user_name="LFISCHER",
            pattern_key="pa_adhoc_loop",
        ),
        patterns=[
            P(
                "pa_adhoc_loop",
                "continuous",
                2000,
                75,
                0.6,
                "SELECT",
                "SELECT * FROM ANALYTICS.PRODUCT.EVENTS WHERE user_id IN (SELECT user_id FROM ANALYTICS.PRODUCT.SESSIONS) AND {n}=1",
                ["LFISCHER"],
                40,
                60000,
                0.5,
                working_set_gb=30,
                weekend=1.0,
                grows=False,
                window=("2025-10-14 15:00", "2025-10-17 03:00"),
            )
        ],
    )
    plant(
        Truth(
            "A02",
            "anomaly",
            "weekend_left_running",
            "GTM_ENRICH_WH",
            "2025-11-07 18:00",
            "2025-11-10 08:00",
            "A misconfigured sync sent a heartbeat query every 30 seconds all weekend, so the warehouse never suspended.",
            "Fix the sync schedule and add a resource monitor on GTM_ENRICH_WH.",
            user_name="SVC_HIGHTOUCH",
            pattern_key="gtm_enrich_heartbeat",
        ),
        patterns=[
            P(
                "gtm_enrich_heartbeat",
                "continuous",
                2880,
                0.2,
                0.0,
                "SELECT",
                "SELECT 1 /* heartbeat {n} */",
                ["SVC_HIGHTOUCH"],
                0,
                0,
                0.0,
                weekend=1.0,
                grows=False,
                query_tag="hightouch",
                window=("2025-11-07 18:00", "2025-11-10 08:00"),
            )
        ],
    )
    plant(
        Truth(
            "A03",
            "anomaly",
            "backfill_spike",
            "DP_TRANSFORM_WH",
            "2025-12-01 00:00",
            "2025-12-04 00:00",
            "Three day historical backfill of FCT_ORDERS, warehouse resized to X-Large for it.",
            "Expected one-off. Confirm with Data Platform and annotate.",
            user_name="BNGUYEN",
            pattern_key="dp_backfill_orders",
        ),
        Modifier("size", "2025-12-01 00:00", "2025-12-04 00:00", size="X-Large"),
        patterns=[
            P(
                "dp_backfill_orders",
                "continuous",
                220,
                1400,
                0.8,
                "INSERT",
                "INSERT INTO ANALYTICS.CORE.FCT_ORDERS SELECT ... WHERE order_month = {n}",
                ["BNGUYEN"],
                900,
                9000,
                0.1,
                working_set_gb=120,
                weekend=1.0,
                grows=False,
                window=("2025-12-01 00:00", "2025-12-04 00:00"),
            )
        ],
    )
    plant(
        Truth(
            "A04",
            "anomaly",
            "upsized_never_downsized",
            "DP_TRANSFORM_WH",
            "2025-12-04 00:00",
            "2025-12-29 10:00",
            "After the backfill the warehouse stayed X-Large for 25 days running the normal dbt load.",
            "ALTER WAREHOUSE DP_TRANSFORM_WH SET WAREHOUSE_SIZE = 'MEDIUM';",
            user_name="BNGUYEN",
            linked_truth_id="A03",
        ),
        Modifier("size", "2025-12-04 00:00", "2025-12-29 10:00", size="X-Large"),
    )
    plant(
        Truth(
            "A05",
            "anomaly",
            "cloud_services_spike",
            "PA_BI_WH",
            "2026-01-20 00:00",
            "2026-01-28 00:00",
            "A BI tool upgrade polled INFORMATION_SCHEMA tens of thousands of times a day.",
            "Roll back the connector setting. Cloud services above 10 percent of compute is billed.",
            user_name="SVC_TABLEAU",
            pattern_key="pa_bi_infoschema_poll",
        ),
        patterns=[
            P(
                "pa_bi_infoschema_poll",
                "interactive",
                40000,
                0.15,
                0.0,
                "SELECT",
                "SELECT * FROM ANALYTICS.INFORMATION_SCHEMA.COLUMNS WHERE table_name = 'T{n}'",
                ["SVC_TABLEAU"],
                0.01,
                1,
                1.0,
                weekend=0.6,
                cs_per_query=1e-3,
                grows=False,
                query_tag="tableau",
                window=("2026-01-20 00:00", "2026-01-28 00:00"),
            )
        ],
    )
    plant(
        Truth(
            "A06",
            "anomaly",
            "legit_step_change",
            "ML_TRAIN_WH",
            "2026-02-02 00:00",
            None,
            "New recommendation model added to the nightly training run. Planned and permanent.",
            "Accept as new baseline and raise the ML Training budget.",
            pattern_key="ml_train_features",
        ),
        Modifier("scale", "2026-02-02 00:00", None, "ml_train_features", 1.6),
    )
    plant(
        Truth(
            "A07",
            "anomaly",
            "runaway_query_loop",
            "CD_ADHOC_WH",
            "2026-02-24 02:00",
            "2026-02-25 20:00",
            "A script with a broken retry loop re-ran an identity join for 42 hours.",
            "Kill the script and set STATEMENT_TIMEOUT_IN_SECONDS on CD_ADHOC_WH.",
            user_name="WOKONKWO",
            pattern_key="cd_adhoc_retry",
        ),
        patterns=[
            P(
                "cd_adhoc_retry",
                "continuous",
                2400,
                60,
                0.6,
                "SELECT",
                "SELECT * FROM CUSTOMER.CDP.PROFILES p JOIN CUSTOMER.CDP.IDENTITY_GRAPH g ON p.id = g.id WHERE g.batch = {n}",
                ["WOKONKWO"],
                6,
                2500,
                0.6,
                weekend=1.0,
                grows=False,
                window=("2026-02-24 02:00", "2026-02-25 20:00"),
            )
        ],
    )

    # ---------------- Anomalies, held-out window ----------------
    plant(
        Truth(
            "A08",
            "anomaly",
            "runaway_query_loop",
            "ML_TRAIN_WH",
            "2026-08-04 06:00",
            "2026-08-05 12:00",
            "A failing training job was retried continuously for 30 hours by the orchestrator.",
            "Cap retries in the DAG and set STATEMENT_TIMEOUT_IN_SECONDS on ML_TRAIN_WH.",
            user_name="SVC_MLFLOW",
            pattern_key="ml_train_retry",
        ),
        patterns=[
            P(
                "ml_train_retry",
                "continuous",
                60,
                1200,
                0.8,
                "CREATE_TABLE_AS_SELECT",
                "CREATE OR REPLACE TABLE ML.TRAINING.RECS_TRAIN AS SELECT ... /* retry {n} */",
                ["SVC_MLFLOW"],
                900,
                30000,
                0.6,
                working_set_gb=200,
                weekend=1.0,
                grows=False,
                query_tag="mlflow",
                window=("2026-08-04 06:00", "2026-08-05 12:00"),
            )
        ],
    )
    plant(
        Truth(
            "A09",
            "anomaly",
            "weekend_left_running",
            "PA_BI_WH",
            "2026-04-17 19:00",
            "2026-04-20 07:00",
            "An extract refresh was set to every two minutes on Friday night and ran all weekend.",
            "Restore the refresh schedule. Consider a weekend resource monitor.",
            user_name="SVC_TABLEAU",
            pattern_key="pa_bi_extract_refresh",
        ),
        patterns=[
            P(
                "pa_bi_extract_refresh",
                "continuous",
                720,
                20,
                0.4,
                "SELECT",
                "SELECT * FROM ANALYTICS.CORE.FCT_ORDERS WHERE updated_at > '{n}'",
                ["SVC_TABLEAU"],
                2,
                9000,
                0.05,
                weekend=1.0,
                grows=False,
                query_tag="tableau",
                window=("2026-04-17 19:00", "2026-04-20 07:00"),
            )
        ],
    )
    plant(
        Truth(
            "A10",
            "anomaly",
            "upsized_never_downsized",
            "CD_TRANSFORM_WH",
            "2026-05-11 10:00",
            None,
            "Upsized from Small to Large to debug a slow job and never sized back down.",
            "ALTER WAREHOUSE CD_TRANSFORM_WH SET WAREHOUSE_SIZE = 'SMALL';",
            user_name="UBANERJEE",
        ),
        Modifier("size", "2026-05-11 10:00", None, size="Large"),
    )
    plant(
        Truth(
            "A11",
            "anomaly",
            "backfill_spike",
            "FIN_ETL_WH",
            "2026-06-08 00:00",
            "2026-06-11 00:00",
            "Revenue restatement backfill reprocessed two years of GL entries.",
            "Expected one-off. Confirm with Finance BI and annotate.",
            user_name="PREYES",
            pattern_key="fin_restatement_backfill",
        ),
        patterns=[
            P(
                "fin_restatement_backfill",
                "continuous",
                60,
                1100,
                0.7,
                "INSERT",
                "INSERT INTO FINANCE.CORE.GL_ENTRIES SELECT ... WHERE fiscal_month = {n}",
                ["PREYES"],
                200,
                3000,
                0.8,
                working_set_gb=40,
                weekend=1.0,
                grows=False,
                window=("2026-06-08 00:00", "2026-06-11 00:00"),
            )
        ],
    )
    plant(
        Truth(
            "A12",
            "anomaly",
            "cloud_services_spike",
            "GTM_CRM_WH",
            "2026-07-13 00:00",
            "2026-07-18 00:00",
            "A new CRM connector issued DESCRIBE style metadata queries every few seconds.",
            "Throttle the connector metadata refresh.",
            user_name="SVC_FIVETRAN",
            pattern_key="gtm_crm_metadata_poll",
        ),
        patterns=[
            P(
                "gtm_crm_metadata_poll",
                "continuous",
                20000,
                0.1,
                0.0,
                "SELECT",
                "SELECT * FROM GTM.INFORMATION_SCHEMA.TABLES WHERE table_name = 'T{n}'",
                ["SVC_FIVETRAN"],
                0.01,
                1,
                1.0,
                weekend=1.0,
                cs_per_query=5e-4,
                grows=False,
                query_tag="fivetran",
                window=("2026-07-13 00:00", "2026-07-18 00:00"),
            )
        ],
    )
    plant(
        Truth(
            "A13",
            "anomaly",
            "legit_step_change",
            "CD_SYNC_WH",
            "2026-06-01 00:00",
            None,
            "Real time profile sync launched for all product lines. Planned and permanent.",
            "Accept as new baseline and raise the Customer Data budget.",
            pattern_key="cd_sync_profiles",
        ),
        Modifier("scale", "2026-06-01 00:00", None, "cd_sync_profiles", 2.5),
    )
    plant(
        Truth(
            "A14",
            "anomaly",
            "weekend_left_running",
            "FIN_ADHOC_WH",
            "2026-09-04 17:00",
            "2026-09-08 08:00",
            "An Excel connector session kept the warehouse awake over the Labor Day weekend.",
            "Close idle connector sessions and set a client session keep alive policy.",
            user_name="OHUGHES",
            pattern_key="fin_adhoc_keepalive",
        ),
        patterns=[
            P(
                "fin_adhoc_keepalive",
                "continuous",
                1440,
                0.3,
                0.0,
                "SELECT",
                "SELECT CURRENT_TIMESTAMP() /* {n} */",
                ["OHUGHES"],
                0,
                0,
                0.0,
                weekend=1.0,
                grows=False,
                window=("2026-09-04 17:00", "2026-09-08 08:00"),
            )
        ],
    )

    # ---------------- Waste, persistent ----------------
    _wh(whs, "PA_BI_WH").max_clusters = 3
    _wh(whs, "GTM_REPORTING_WH").auto_suspend = 1800
    plant(
        Truth(
            "W01",
            "waste",
            "idle_long_autosuspend",
            "GTM_REPORTING_WH",
            "2025-03-16",
            None,
            "Sporadic dashboard queries, but AUTO_SUSPEND is 30 minutes, so most billed time is idle.",
            "ALTER WAREHOUSE GTM_REPORTING_WH SET AUTO_SUSPEND = 60;",
        ),
        Modifier("auto_suspend", seconds=60, role="fix"),
    )
    _wh(whs, "PA_ADHOC_WH").auto_suspend = 900
    plant(
        Truth(
            "W02",
            "waste",
            "idle_long_autosuspend",
            "PA_ADHOC_WH",
            "2025-03-16",
            None,
            "Ad hoc analyst warehouse with AUTO_SUSPEND of 15 minutes between bursty queries.",
            "ALTER WAREHOUSE PA_ADHOC_WH SET AUTO_SUSPEND = 60;",
        ),
        Modifier("auto_suspend", seconds=60, role="fix"),
    )
    _wh(whs, "FIN_REPORTING_WH").size = "Large"
    plant(
        Truth(
            "W03",
            "waste",
            "oversized_low_load",
            "FIN_REPORTING_WH",
            "2025-03-16",
            None,
            "Small dashboard lookups on a Large warehouse. Load is low and nothing queues.",
            "ALTER WAREHOUSE FIN_REPORTING_WH SET WAREHOUSE_SIZE = 'MEDIUM';",
        ),
        Modifier("size", size="Medium", role="fix"),
    )
    plant(
        Truth(
            "W04",
            "waste",
            "oversized_low_load",
            "DP_UTILITY_WH",
            "2025-03-16",
            None,
            "Tiny audit lookups on a Medium warehouse.",
            "ALTER WAREHOUSE DP_UTILITY_WH SET WAREHOUSE_SIZE = 'XSMALL';",
        ),
        Modifier("size", size="X-Small", role="fix"),
    )
    plant(
        Truth(
            "W05",
            "waste",
            "undersized_queue_spill",
            "ML_FEATURE_WH",
            "2025-03-16",
            None,
            "Feature builds spill to remote storage and queue on an X-Small warehouse.",
            "ALTER WAREHOUSE ML_FEATURE_WH SET WAREHOUSE_SIZE = 'MEDIUM';",
        ),
        Modifier("size", size="Medium", role="fix"),
    )
    _pattern(_wh(whs, "PA_DBT_WH"), "pa_dbt_sessions").working_set_gb = 150
    plant(
        Truth(
            "W06",
            "waste",
            "undersized_queue_spill",
            "PA_DBT_WH",
            "2025-03-16",
            None,
            "Sessionization model spills to remote storage and queues on a Small warehouse.",
            "ALTER WAREHOUSE PA_DBT_WH SET WAREHOUSE_SIZE = 'LARGE';",
        ),
        Modifier("size", size="Large", role="fix"),
    )
    plant(
        Truth(
            "W07",
            "waste",
            "poor_pruning_repeated",
            "PA_BI_WH",
            "2025-03-16",
            None,
            "Dashboard tile filters on TO_DATE(event_ts), which defeats pruning, and runs hundreds of times a day.",
            "Rewrite the filter on event_date and add a clustering key on ANALYTICS.PRODUCT.EVENTS (event_date), or materialize the tile.",
            pattern_key="pa_bi_dash_events",
        ),
        Modifier("prune", pattern="pa_bi_dash_events", factor=0.04, role="fix"),
        patterns=[
            P(
                "pa_bi_dash_events",
                "interactive",
                900,
                90,
                0.7,
                "SELECT",
                "SELECT event_name, COUNT(*) FROM ANALYTICS.PRODUCT.EVENTS WHERE TO_DATE(event_ts) >= DATEADD(day, -{n}, CURRENT_DATE) GROUP BY 1",
                ["SVC_TABLEAU"],
                180,
                60000,
                0.97,
                working_set_gb=20,
                weekend=0.3,
                query_tag="tableau",
            )
        ],
    )
    plant(
        Truth(
            "W08",
            "waste",
            "poor_pruning_repeated",
            "GTM_CRM_WH",
            "2025-03-16",
            None,
            "Hourly lead dedupe scans the full LEADS table because it joins on a lowercased email.",
            "Add a normalized email column with a clustering key, or materialize the dedupe as an incremental model.",
            pattern_key="gtm_crm_dedupe",
        ),
        Modifier("prune", pattern="gtm_crm_dedupe", factor=0.1, role="fix"),
        patterns=[
            P(
                "gtm_crm_dedupe",
                "continuous",
                24,
                320,
                0.7,
                "SELECT",
                "SELECT LOWER(email), COUNT(*) FROM GTM.CRM.LEADS GROUP BY 1 HAVING COUNT(*) > {n}",
                ["SVC_AIRFLOW"],
                60,
                1200,
                0.99,
                working_set_gb=10,
                weekend=1.0,
                query_tag="airflow",
            )
        ],
    )
    plant(
        Truth(
            "W09",
            "waste",
            "unused_scheduled_output",
            "FIN_ETL_WH",
            "2025-03-16",
            None,
            "Nightly job rebuilds LEGACY_GL_DAILY, which no query has read in the whole history.",
            "Pause the job in Airflow, confirm with the owner, then drop the table.",
            pattern_key="fin_etl_legacy_gl",
        ),
        Modifier("remove", pattern="fin_etl_legacy_gl", role="fix"),
        patterns=[
            P(
                "fin_etl_legacy_gl",
                "batch",
                6,
                700,
                0.7,
                "CREATE_TABLE_AS_SELECT",
                "CREATE OR REPLACE TABLE FINANCE.REPORTING.LEGACY_GL_DAILY AS SELECT * FROM FINANCE.CORE.GL_ENTRIES /* run {n} */",
                ["SVC_AIRFLOW"],
                80,
                3000,
                1.0,
                working_set_gb=20,
                hours=(4,),
                weekend=1.0,
                writes="FINANCE.REPORTING.LEGACY_GL_DAILY",
                query_tag="airflow",
            )
        ],
    )
    plant(
        Truth(
            "W10",
            "waste",
            "unused_scheduled_output",
            "DP_SNAPSHOT_WH",
            "2025-03-16",
            None,
            "Nightly CRM account snapshot that nothing reads.",
            "Pause the snapshot job, confirm with the owner, then drop the table.",
            pattern_key="dp_snapshot_crm",
        ),
        Modifier("remove", pattern="dp_snapshot_crm", role="fix"),
    )
    plant(
        Truth(
            "W11",
            "waste",
            "oversized_low_load",
            "CD_TRANSFORM_WH",
            "2026-05-11 10:00",
            None,
            "Still Large after the May upsize. The identity job does not benefit from the extra size.",
            "ALTER WAREHOUSE CD_TRANSFORM_WH SET WAREHOUSE_SIZE = 'SMALL';",
            linked_truth_id="A10",
        ),
        Modifier("size", "2026-05-11 10:00", None, size="Small", role="fix"),
    )

    for t in truth:
        if t.pattern_key:
            p = next(
                (p for p in _wh(whs, t.warehouse_name).patterns if p.key == t.pattern_key), None
            )
            assert p is not None, f"{t.truth_id} names unknown pattern {t.pattern_key}"
    return whs, truth
