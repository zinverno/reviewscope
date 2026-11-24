"""Sanitized nested EXPLAIN ANALYZE from the real SECURITY DEFINER call, disposable DB only."""

import json
import re
from time import perf_counter

import psycopg
from psycopg import sql


def enable_client_plans(db):
    # Keep nested plans in the test connection. Raw expressions may contain private bindings.
    settings = {
        "session_preload_libraries": "auto_explain",
        "log_min_messages": "panic",
        "auto_explain.log_level": "notice",
        "auto_explain.log_min_duration": "0",
        "auto_explain.log_analyze": "on",
        "auto_explain.log_buffers": "on",
        "auto_explain.log_settings": "on",
        "auto_explain.log_timing": "off",
        "auto_explain.log_format": "json",
        "auto_explain.log_nested_statements": "on",
        "auto_explain.log_parameter_max_length": "0",
    }
    with psycopg.connect(user="rs_test", autocommit=True) as admin:
        for name, value in settings.items():
            admin.execute(
                sql.SQL("ALTER ROLE rs_runtime IN DATABASE {} SET {} TO {}").format(
                    sql.Identifier(db.info.dbname), sql.SQL(name), sql.Literal(value)
                )
            )


def plan_observation(scope, proof, mode, stage, *, fixed, expensive_io=False):
    """Only publish operation counts, index/column names and timing. Never retain raw notices."""
    observations = []

    def summarize(notice):
        _, marker, raw = notice.message_primary.partition("plan:\n")
        if not marker:
            return
        plan = json.loads(raw)
        nodes = [plan["Plan"]]
        scans, joins = [], []
        removed = repeated = page_visits = 0
        while nodes:
            node = nodes.pop()
            nodes.extend(node.get("Plans", ()))
            loops = node["Actual Loops"]
            rows = node["Actual Rows"]
            removed += loops * node.get("Rows Removed by Join Filter", 0)
            if node["Node Type"] == "CTE Scan":
                if node["CTE Name"] in {"page", "bodies"}:
                    page_visits = max(page_visits, rows * loops)
                else:
                    repeated = max(repeated, rows * loops)
            if node.get("Relation Name") == "review_version":
                cond = node.get("Index Cond", "")
                keys = re.findall(r"\b(workspace_id|source_id|identity_id|id)\s*=", cond)
                scans.append(
                    dict(
                        index=node.get("Index Name", node["Node Type"]),
                        keys=keys,
                        loops=loops,
                        rows=rows,
                        filtered=node.get("Rows Removed by Filter", 0),
                    )
                )
            if node.get("Join Type") == "Full":
                joins.append(node["Node Type"])
        if scans:
            observations.append(
                dict(
                    scans=scans,
                    joins=joins,
                    join_removed=removed,
                    cte_visits=repeated,
                    page_visits=page_visits,
                    blocks=plan["Plan"].get("Shared Hit Blocks", 0)
                    + plan["Plan"].get("Shared Read Blocks", 0),
                )
            )

    with psycopg.connect(user="rs_runtime") as reader:
        reader.execute(
            "SELECT set_config('rs.workspace',%s,true), set_config('rs.session_proof',%s,true)",
            (str(scope[0]), proof),
        )
        reader.execute("SELECT set_config('plan_cache_mode',%s,true)", (mode,))
        reader.execute("SET LOCAL statement_timeout='12s'")
        if expensive_io:
            reader.execute("SET LOCAL random_page_cost=100")
        reader.add_notice_handler(summarize)
        started = perf_counter()
        doc = reader.execute("SELECT reviewer_context_page(%s,%s,%s,%s,0,50)", scope).fetchone()[0]
        elapsed = perf_counter() - started
        reader.remove_notice_handler(summarize)
    assert doc["status"] == "computed" and observations
    print(
        json.dumps(
            dict(
                probe="nested-context-plan",
                stage=stage,
                seconds=round(elapsed, 4),
                mode=mode,
                total=doc["total"],
                statements=observations,
            )
        )
    )
    if fixed:
        assert any(
            join in {"Hash Join", "Merge Join"} for obs in observations for join in obs["joins"]
        )
        assert all(
            scan["loops"] <= 1
            or ({"workspace_id", "source_id", "id"} <= set(scan["keys"]) and scan["rows"] <= 1)
            for obs in observations
            for scan in obs["scans"]
        )
        assert all(
            obs["page_visits"] <= 50 * 50
            and obs["join_removed"] < 100 * max(doc["total"], 50)
            and obs["cte_visits"] <= 10 * max(doc["total"], 50)
            for obs in observations
        )
    return doc
