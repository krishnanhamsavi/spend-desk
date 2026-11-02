"""The fictional company: teams, users, warehouses, and the query patterns each warehouse runs.

Numbers here describe normal behavior at the start of history. Growth, seasonality,
planted anomalies, and waste are layered on in scenario.py and engine.py.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

SIZE_CREDITS = {
    "X-Small": 1,
    "Small": 2,
    "Medium": 4,
    "Large": 8,
    "X-Large": 16,
    "2X-Large": 32,
}
SIZES = list(SIZE_CREDITS)

TEAM_SLUG = {
    "Data Platform": "DATA_PLATFORM",
    "ML Training": "ML_TRAINING",
    "Product Analytics": "PRODUCT_ANALYTICS",
    "Finance BI": "FINANCE_BI",
    "GTM Ops": "GTM_OPS",
    "Customer Data": "CUSTOMER_DATA",
}

# Monthly compound growth in workload per team.
TEAM_GROWTH = {
    "Data Platform": 0.020,
    "ML Training": 0.035,
    "Product Analytics": 0.015,
    "Finance BI": 0.008,
    "GTM Ops": 0.010,
    "Customer Data": 0.025,
}

HUMANS = {
    "Data Platform": ["AKIM", "BNGUYEN", "CRIVERA", "DSHAH"],
    "ML Training": ["EMORALES", "FOKAFOR", "GLIN", "HBROOKS"],
    "Product Analytics": ["IWALSH", "JTANAKA", "KADEYEMI", "LFISCHER", "MSINGH", "NPARK"],
    "Finance BI": ["OHUGHES", "PREYES", "QZHANG"],
    "GTM Ops": ["RCOLLINS", "SDIAZ", "TMEYER"],
    "Customer Data": ["UBANERJEE", "VROSSI", "WOKONKWO"],
}


def pattern_hash(key: str) -> str:
    return hashlib.md5(key.encode()).hexdigest()


@dataclass
class Pattern:
    key: str
    kind: str  # interactive | batch | continuous
    runs_per_day: float  # underlying weekday runs per day at history start
    exec_s: float  # median execution seconds at the warehouse's base size
    elasticity: float  # 0 to 1. How execution time shrinks as the warehouse grows
    query_type: str
    sql: str  # {n} is replaced with a varying literal
    users: list[str]
    gb_scanned: float
    partitions_total: int
    prune: float  # share of partitions scanned
    working_set_gb: float = 0.0  # memory needed; above warehouse memory it spills
    hours: tuple[int, ...] = ()  # run hours for batch patterns
    weekend: float = 0.25  # weekend multiplier
    writes: str | None = None
    cs_per_query: float = 2e-6  # cloud services credits per query
    query_tag: str | None = None
    grows: bool = True  # follows team growth and day noise
    window: tuple[str, str | None] | None = None  # active only inside [start, end)
    truth_id: str | None = None  # set when the pattern itself is a planted anomaly or waste

    @property
    def param_hash(self) -> str:
        return pattern_hash(self.key)


@dataclass
class Modifier:
    kind: str  # scale | size | auto_suspend | prune | remove
    start: str | None = None
    end: str | None = None
    pattern: str | None = None
    factor: float = 1.0
    size: str | None = None
    seconds: int | None = None
    truth_id: str | None = None
    role: str = "plant"  # plant: part of history. fix: counterfactual only. base: always on


@dataclass
class Warehouse:
    name: str
    team: str
    size: str
    auto_suspend: int
    concurrency: float
    cs_ratio: float
    patterns: list[Pattern] = field(default_factory=list)
    modifiers: list[Modifier] = field(default_factory=list)
    warehouse_id: int = 0
    max_clusters: int = 1
    ref_size: str = ""  # size the pattern exec_s values were written for

    def __post_init__(self):
        self.ref_size = self.ref_size or self.size

    @property
    def role_name(self) -> str:
        return f"{TEAM_SLUG[self.team]}_ROLE"


def P(key, kind, runs, exec_s, el, qtype, sql, users, gb, parts, prune, **kw) -> Pattern:
    return Pattern(key, kind, runs, exec_s, el, qtype, sql, users, gb, parts, prune, **kw)


NIGHT = (1, 2, 3, 4)
H = HUMANS


def build_warehouses() -> list[Warehouse]:
    W = Warehouse
    whs = [
        # Data Platform
        W(
            "DP_INGEST_WH",
            "Data Platform",
            "Medium",
            60,
            2.0,
            0.06,
            [
                P(
                    "dp_ingest_copy",
                    "continuous",
                    288,
                    45,
                    0.5,
                    "COPY",
                    "COPY INTO RAW.EVENTS.APP_EVENTS FROM @RAW.EVENTS.S3_STAGE/{n}/",
                    ["SVC_FIVETRAN"],
                    6,
                    1,
                    1.0,
                    weekend=1.0,
                    writes="RAW.EVENTS.APP_EVENTS",
                    query_tag="fivetran",
                ),
                P(
                    "dp_ingest_merge",
                    "batch",
                    60,
                    420,
                    0.7,
                    "MERGE",
                    "MERGE INTO RAW.CRM.ACCOUNTS t USING RAW.CRM.ACCOUNTS_STG_{n} s ON t.id = s.id",
                    ["SVC_AIRFLOW"],
                    120,
                    4200,
                    0.35,
                    working_set_gb=40,
                    hours=NIGHT,
                    weekend=1.0,
                    writes="RAW.CRM.ACCOUNTS",
                    query_tag="airflow",
                ),
            ],
        ),
        W(
            "DP_TRANSFORM_WH",
            "Data Platform",
            "Medium",
            120,
            1.5,
            0.05,
            [
                P(
                    "dp_dbt_core",
                    "batch",
                    220,
                    180,
                    0.75,
                    "CREATE_TABLE_AS_SELECT",
                    "CREATE OR REPLACE TABLE ANALYTICS.CORE.FCT_ORDERS AS SELECT ... /* run {n} */",
                    ["SVC_DBT"],
                    300,
                    9000,
                    0.4,
                    working_set_gb=50,
                    hours=(2, 3, 4, 5, 6),
                    weekend=1.0,
                    writes="ANALYTICS.CORE.FCT_ORDERS",
                    query_tag="dbt",
                ),
                P(
                    "dp_dbt_tests",
                    "batch",
                    400,
                    25,
                    0.3,
                    "SELECT",
                    "SELECT COUNT(*) FROM ANALYTICS.CORE.FCT_ORDERS WHERE order_id IS NULL AND {n}=1",
                    ["SVC_DBT"],
                    20,
                    9000,
                    0.1,
                    hours=(6, 7),
                    weekend=1.0,
                    query_tag="dbt",
                ),
            ],
        ),
        W(
            "DP_UTILITY_WH",
            "Data Platform",
            "Medium",
            60,
            2.0,
            0.07,
            [
                P(
                    "dp_util_audit",
                    "interactive",
                    140,
                    4,
                    0.1,
                    "SELECT",
                    "SELECT * FROM ANALYTICS.ADMIN.LOAD_AUDIT WHERE batch_id = {n}",
                    H["Data Platform"],
                    0.5,
                    40,
                    0.2,
                    weekend=0.2,
                ),
            ],
        ),
        W(
            "DP_SNAPSHOT_WH",
            "Data Platform",
            "Small",
            60,
            1.0,
            0.04,
            [
                P(
                    "dp_snapshot_crm",
                    "batch",
                    6,
                    900,
                    0.7,
                    "CREATE_TABLE_AS_SELECT",
                    "CREATE OR REPLACE TABLE RAW.SNAPSHOTS.CRM_ACCOUNTS_SNAP AS SELECT * FROM RAW.CRM.ACCOUNTS /* run {n} */",
                    ["SVC_AIRFLOW"],
                    200,
                    4200,
                    1.0,
                    working_set_gb=20,
                    hours=(0, 1, 2),
                    weekend=1.0,
                    writes="RAW.SNAPSHOTS.CRM_ACCOUNTS_SNAP",
                    query_tag="airflow",
                ),
                P(
                    "dp_snapshot_orders",
                    "batch",
                    4,
                    600,
                    0.7,
                    "CREATE_TABLE_AS_SELECT",
                    "CREATE OR REPLACE TABLE RAW.SNAPSHOTS.ORDERS_SNAP AS SELECT * FROM ANALYTICS.CORE.FCT_ORDERS /* run {n} */",
                    ["SVC_AIRFLOW"],
                    150,
                    9000,
                    1.0,
                    working_set_gb=20,
                    hours=(3,),
                    weekend=1.0,
                    writes="RAW.SNAPSHOTS.ORDERS_SNAP",
                    query_tag="airflow",
                ),
                P(
                    "dp_snapshot_reads",
                    "interactive",
                    10,
                    30,
                    0.5,
                    "SELECT",
                    "SELECT * FROM RAW.SNAPSHOTS.ORDERS_SNAP WHERE snapshot_date = '{n}'",
                    H["Data Platform"],
                    10,
                    900,
                    0.1,
                    weekend=0.1,
                ),
            ],
        ),
        W(
            "DP_ORCH_WH",
            "Data Platform",
            "X-Small",
            60,
            2.0,
            0.08,
            [
                P(
                    "dp_orch_checks",
                    "continuous",
                    1440,
                    2,
                    0.1,
                    "SELECT",
                    "SELECT MAX(loaded_at) FROM RAW.EVENTS.APP_EVENTS WHERE source = {n}",
                    ["SVC_AIRFLOW"],
                    0.2,
                    20000,
                    0.001,
                    weekend=1.0,
                    query_tag="airflow",
                ),
            ],
        ),
        # ML Training
        W(
            "ML_TRAIN_WH",
            "ML Training",
            "X-Large",
            120,
            1.0,
            0.03,
            [
                P(
                    "ml_train_features",
                    "batch",
                    12,
                    900,
                    0.8,
                    "CREATE_TABLE_AS_SELECT",
                    "CREATE OR REPLACE TABLE ML.TRAINING.CHURN_TRAIN AS SELECT ... /* run {n} */",
                    ["SVC_MLFLOW"],
                    900,
                    30000,
                    0.6,
                    working_set_gb=200,
                    hours=(20, 21, 22),
                    weekend=1.0,
                    writes="ML.TRAINING.CHURN_TRAIN",
                    query_tag="mlflow",
                ),
                P(
                    "ml_train_eval",
                    "batch",
                    30,
                    200,
                    0.6,
                    "SELECT",
                    "SELECT * FROM ML.TRAINING.CHURN_TRAIN SAMPLE ({n})",
                    ["SVC_MLFLOW"],
                    100,
                    30000,
                    0.2,
                    hours=(23,),
                    weekend=1.0,
                    query_tag="mlflow",
                ),
            ],
        ),
        W(
            "ML_FEATURE_WH",
            "ML Training",
            "X-Small",
            60,
            1.5,
            0.05,
            [
                P(
                    "ml_feature_build",
                    "batch",
                    32,
                    600,
                    0.8,
                    "CREATE_TABLE_AS_SELECT",
                    "CREATE OR REPLACE TABLE ML.FEATURES.USER_FEATURES AS SELECT ... /* run {n} */",
                    ["SVC_AIRFLOW"],
                    150,
                    8000,
                    0.5,
                    working_set_gb=110,
                    hours=(5, 6, 7, 8),
                    weekend=1.0,
                    writes="ML.FEATURES.USER_FEATURES",
                    query_tag="airflow",
                ),
            ],
        ),
        W(
            "ML_INFER_WH",
            "ML Training",
            "Medium",
            60,
            1.5,
            0.05,
            [
                P(
                    "ml_infer_score",
                    "continuous",
                    24,
                    300,
                    0.7,
                    "INSERT",
                    "INSERT INTO ML.SCORING.CHURN_SCORES SELECT ... FROM ML.FEATURES.USER_FEATURES WHERE batch = {n}",
                    ["SVC_MLFLOW"],
                    40,
                    8000,
                    0.15,
                    working_set_gb=20,
                    weekend=1.0,
                    writes="ML.SCORING.CHURN_SCORES",
                    query_tag="mlflow",
                ),
            ],
        ),
        W(
            "ML_NOTEBOOK_WH",
            "ML Training",
            "Small",
            120,
            2.0,
            0.06,
            [
                P(
                    "ml_notebook_explore",
                    "interactive",
                    120,
                    35,
                    0.6,
                    "SELECT",
                    "SELECT * FROM ML.FEATURES.USER_FEATURES WHERE cohort = {n} LIMIT 100000",
                    H["ML Training"],
                    8,
                    8000,
                    0.2,
                    weekend=0.15,
                ),
            ],
        ),
        # Product Analytics
        W(
            "PA_BI_WH",
            "Product Analytics",
            "Medium",
            120,
            3.0,
            0.07,
            [
                P(
                    "pa_bi_dash_core",
                    "interactive",
                    1600,
                    6,
                    0.4,
                    "SELECT",
                    "SELECT region, SUM(revenue) FROM ANALYTICS.CORE.FCT_ORDERS WHERE order_date = '{n}' GROUP BY 1",
                    ["SVC_TABLEAU"],
                    2,
                    9000,
                    0.03,
                    weekend=0.3,
                    query_tag="tableau",
                ),
                P(
                    "pa_bi_scores",
                    "interactive",
                    300,
                    4,
                    0.3,
                    "SELECT",
                    "SELECT * FROM ML.SCORING.CHURN_SCORES WHERE account_id = {n}",
                    ["SVC_TABLEAU"],
                    1,
                    3000,
                    0.02,
                    weekend=0.3,
                    query_tag="tableau",
                ),
                P(
                    "pa_bi_ext_accounts",
                    "interactive",
                    120,
                    8,
                    0.4,
                    "SELECT",
                    "SELECT * FROM RAW.CRM.ACCOUNTS WHERE segment = {n}",
                    ["SVC_TABLEAU"],
                    3,
                    4200,
                    0.05,
                    weekend=0.3,
                    query_tag="tableau",
                ),
            ],
        ),
        W(
            "PA_ADHOC_WH",
            "Product Analytics",
            "Medium",
            60,
            2.0,
            0.07,
            [
                P(
                    "pa_adhoc_explore",
                    "interactive",
                    260,
                    40,
                    0.6,
                    "SELECT",
                    "SELECT user_id, COUNT(*) FROM ANALYTICS.PRODUCT.EVENTS WHERE event_name = 'e{n}' GROUP BY 1",
                    H["Product Analytics"],
                    25,
                    60000,
                    0.08,
                    working_set_gb=30,
                    weekend=0.1,
                ),
            ],
        ),
        W(
            "PA_DBT_WH",
            "Product Analytics",
            "Small",
            60,
            1.0,
            0.05,
            [
                P(
                    "pa_dbt_sessions",
                    "batch",
                    30,
                    500,
                    0.8,
                    "CREATE_TABLE_AS_SELECT",
                    "CREATE OR REPLACE TABLE ANALYTICS.PRODUCT.SESSIONS AS SELECT ... /* run {n} */",
                    ["SVC_DBT"],
                    250,
                    60000,
                    0.15,
                    working_set_gb=45,
                    hours=(3, 4, 5),
                    weekend=1.0,
                    writes="ANALYTICS.PRODUCT.SESSIONS",
                    query_tag="dbt",
                ),
            ],
        ),
        W(
            "PA_EXPERIMENT_WH",
            "Product Analytics",
            "Medium",
            120,
            2.0,
            0.06,
            [
                P(
                    "pa_exp_readout",
                    "interactive",
                    60,
                    90,
                    0.7,
                    "SELECT",
                    "SELECT variant, AVG(converted) FROM ANALYTICS.PRODUCT.SESSIONS WHERE experiment_id = {n} GROUP BY 1",
                    H["Product Analytics"],
                    40,
                    20000,
                    0.1,
                    working_set_gb=20,
                    weekend=0.1,
                ),
            ],
        ),
        W(
            "PA_EVENTS_WH",
            "Product Analytics",
            "Large",
            60,
            1.5,
            0.04,
            [
                P(
                    "pa_events_rollup",
                    "continuous",
                    24,
                    420,
                    0.8,
                    "INSERT",
                    "INSERT INTO ANALYTICS.PRODUCT.EVENTS SELECT ... FROM RAW.EVENTS.APP_EVENTS WHERE hr = {n}",
                    ["SVC_AIRFLOW"],
                    200,
                    60000,
                    0.02,
                    working_set_gb=60,
                    weekend=1.0,
                    writes="ANALYTICS.PRODUCT.EVENTS",
                    query_tag="airflow",
                ),
            ],
        ),
        # Finance BI
        W(
            "FIN_ETL_WH",
            "Finance BI",
            "Medium",
            60,
            1.0,
            0.05,
            [
                P(
                    "fin_etl_gl",
                    "batch",
                    12,
                    600,
                    0.7,
                    "MERGE",
                    "MERGE INTO FINANCE.CORE.GL_ENTRIES t USING RAW.ERP.GL_{n} s ON t.id = s.id",
                    ["SVC_AIRFLOW"],
                    80,
                    3000,
                    0.3,
                    working_set_gb=30,
                    hours=(1, 2),
                    weekend=1.0,
                    writes="FINANCE.CORE.GL_ENTRIES",
                    query_tag="airflow",
                ),
                P(
                    "fin_etl_rev",
                    "batch",
                    8,
                    500,
                    0.7,
                    "CREATE_TABLE_AS_SELECT",
                    "CREATE OR REPLACE TABLE FINANCE.CORE.REVENUE_DAILY AS SELECT ... /* run {n} */",
                    ["SVC_AIRFLOW"],
                    60,
                    3000,
                    0.3,
                    working_set_gb=20,
                    hours=(3,),
                    weekend=1.0,
                    writes="FINANCE.CORE.REVENUE_DAILY",
                    query_tag="airflow",
                ),
            ],
        ),
        W(
            "FIN_REPORTING_WH",
            "Finance BI",
            "Medium",
            60,
            3.0,
            0.06,
            [
                P(
                    "fin_rep_dash",
                    "interactive",
                    450,
                    5,
                    0.2,
                    "SELECT",
                    "SELECT account, SUM(amount) FROM FINANCE.CORE.GL_ENTRIES WHERE period = '{n}' GROUP BY 1",
                    ["SVC_TABLEAU"],
                    1,
                    3000,
                    0.05,
                    weekend=0.2,
                    query_tag="tableau",
                ),
                P(
                    "fin_rep_rev",
                    "interactive",
                    200,
                    4,
                    0.2,
                    "SELECT",
                    "SELECT * FROM FINANCE.CORE.REVENUE_DAILY WHERE day >= '{n}'",
                    ["SVC_TABLEAU"],
                    1,
                    3000,
                    0.05,
                    weekend=0.2,
                    query_tag="tableau",
                ),
            ],
        ),
        W(
            "FIN_ADHOC_WH",
            "Finance BI",
            "Small",
            60,
            2.0,
            0.06,
            [
                P(
                    "fin_adhoc_recon",
                    "interactive",
                    90,
                    45,
                    0.6,
                    "SELECT",
                    "SELECT * FROM FINANCE.CORE.GL_ENTRIES g JOIN FINANCE.CORE.REVENUE_DAILY r ON g.day = r.day WHERE g.entity = {n}",
                    H["Finance BI"],
                    15,
                    3000,
                    0.3,
                    working_set_gb=12,
                    weekend=0.05,
                ),
            ],
        ),
        W(
            "FIN_PLANNING_WH",
            "Finance BI",
            "X-Small",
            60,
            1.0,
            0.05,
            [
                P(
                    "fin_plan_build",
                    "batch",
                    2,
                    900,
                    0.7,
                    "CREATE_TABLE_AS_SELECT",
                    "CREATE OR REPLACE TABLE FINANCE.PLANNING.HEADCOUNT_PLAN AS SELECT ... /* run {n} */",
                    ["SVC_AIRFLOW"],
                    20,
                    500,
                    0.5,
                    hours=(4,),
                    weekend=1.0,
                    writes="FINANCE.PLANNING.HEADCOUNT_PLAN",
                    query_tag="airflow",
                ),
                P(
                    "fin_plan_read",
                    "interactive",
                    6,
                    20,
                    0.5,
                    "SELECT",
                    "SELECT * FROM FINANCE.PLANNING.HEADCOUNT_PLAN WHERE dept = {n}",
                    H["Finance BI"],
                    1,
                    500,
                    0.3,
                    weekend=0.0,
                ),
            ],
        ),
        # GTM Ops
        W(
            "GTM_ENRICH_WH",
            "GTM Ops",
            "Small",
            60,
            1.0,
            0.06,
            [
                P(
                    "gtm_enrich_leads",
                    "batch",
                    40,
                    120,
                    0.6,
                    "MERGE",
                    "MERGE INTO GTM.CRM.LEADS t USING GTM.STAGE.ENRICHED_{n} s ON t.id = s.id",
                    ["SVC_HIGHTOUCH"],
                    10,
                    1200,
                    0.4,
                    hours=(9, 13, 17),
                    weekend=0.0,
                    writes="GTM.CRM.LEADS",
                    query_tag="hightouch",
                ),
            ],
        ),
        W(
            "GTM_REPORTING_WH",
            "GTM Ops",
            "Small",
            60,
            2.0,
            0.07,
            [
                P(
                    "gtm_rep_pipeline",
                    "interactive",
                    70,
                    8,
                    0.4,
                    "SELECT",
                    "SELECT stage, SUM(amount) FROM GTM.CRM.OPPORTUNITIES WHERE quarter = '{n}' GROUP BY 1",
                    ["SVC_TABLEAU"] + H["GTM Ops"],
                    2,
                    800,
                    0.2,
                    weekend=0.1,
                    query_tag="tableau",
                ),
            ],
        ),
        W(
            "GTM_CRM_WH",
            "GTM Ops",
            "Medium",
            60,
            1.5,
            0.06,
            [
                P(
                    "gtm_crm_sync",
                    "continuous",
                    48,
                    90,
                    0.6,
                    "MERGE",
                    "MERGE INTO GTM.CRM.OPPORTUNITIES t USING GTM.STAGE.SFDC_OPPS_{n} s ON t.id = s.id",
                    ["SVC_FIVETRAN"],
                    15,
                    800,
                    0.4,
                    weekend=1.0,
                    writes="GTM.CRM.OPPORTUNITIES",
                    query_tag="fivetran",
                ),
                P(
                    "gtm_crm_reads",
                    "interactive",
                    40,
                    12,
                    0.5,
                    "SELECT",
                    "SELECT * FROM GTM.CRM.LEADS WHERE owner = {n}",
                    H["GTM Ops"],
                    2,
                    1200,
                    0.1,
                    weekend=0.1,
                ),
            ],
        ),
        W(
            "GTM_SYNC_WH",
            "GTM Ops",
            "X-Small",
            60,
            1.5,
            0.08,
            [
                P(
                    "gtm_sync_reverse",
                    "continuous",
                    96,
                    20,
                    0.3,
                    "SELECT",
                    "SELECT * FROM GTM.CRM.LEADS WHERE updated_at > '{n}'",
                    ["SVC_HIGHTOUCH"],
                    1,
                    1200,
                    0.05,
                    weekend=1.0,
                    query_tag="hightouch",
                ),
            ],
        ),
        # Customer Data
        W(
            "CD_SYNC_WH",
            "Customer Data",
            "Small",
            60,
            2.0,
            0.07,
            [
                P(
                    "cd_sync_profiles",
                    "continuous",
                    96,
                    60,
                    0.6,
                    "MERGE",
                    "MERGE INTO CUSTOMER.CDP.PROFILES t USING CUSTOMER.STAGE.PROFILE_UPDATES_{n} s ON t.id = s.id",
                    ["SVC_SEGMENT"],
                    8,
                    2500,
                    0.3,
                    weekend=1.0,
                    writes="CUSTOMER.CDP.PROFILES",
                    query_tag="segment",
                ),
            ],
        ),
        W(
            "CD_TRANSFORM_WH",
            "Customer Data",
            "Small",
            60,
            1.5,
            0.05,
            [
                P(
                    "cd_transform_identity",
                    "batch",
                    40,
                    240,
                    0.25,
                    "CREATE_TABLE_AS_SELECT",
                    "CREATE OR REPLACE TABLE CUSTOMER.CDP.IDENTITY_GRAPH AS SELECT ... /* run {n} */",
                    ["SVC_DBT"],
                    30,
                    2500,
                    0.3,
                    hours=(2, 3, 14),
                    weekend=1.0,
                    writes="CUSTOMER.CDP.IDENTITY_GRAPH",
                    query_tag="dbt",
                ),
            ],
        ),
        W(
            "CD_ADHOC_WH",
            "Customer Data",
            "X-Small",
            60,
            2.0,
            0.06,
            [
                P(
                    "cd_adhoc_explore",
                    "interactive",
                    70,
                    25,
                    0.6,
                    "SELECT",
                    "SELECT * FROM CUSTOMER.CDP.PROFILES p JOIN CUSTOMER.CDP.IDENTITY_GRAPH g ON p.id = g.id WHERE p.region = {n}",
                    H["Customer Data"],
                    5,
                    2500,
                    0.2,
                    weekend=0.1,
                ),
            ],
        ),
    ]
    for i, wh in enumerate(whs, start=1):
        wh.warehouse_id = 1000 + i
    return whs
