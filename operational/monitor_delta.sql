-- Both snapshots have already been verified and admitted by the caller. Return only scoped IDs.
WITH selected AS MATERIALIZED (
    SELECT s.id FROM snapshot s
    WHERE s.workspace_id = %(workspace)s AND s.source_id = %(source)s
      AND s.id IN (%(previous)s, %(target)s)
      AND s.filters->>'selection' = 'accepted_in_delivery'
), observation AS MATERIALIZED (
    SELECT m.snapshot_id, v.identity_id, v.id AS version_id, i.external_review_id IS NOT NULL AS stable
    FROM snapshot_member m
    JOIN selected s ON s.id = m.snapshot_id
    JOIN review_version v ON (v.workspace_id, v.source_id, v.id) =
                             (m.workspace_id, m.source_id, m.version_id)
    JOIN review_identity i ON (i.workspace_id, i.source_id, i.id) =
                              (v.workspace_id, v.source_id, v.identity_id)
    WHERE m.workspace_id = %(workspace)s AND m.source_id = %(source)s
      AND m.snapshot_id IN (%(previous)s, %(target)s)
), previous AS (
    SELECT * FROM observation WHERE snapshot_id = %(previous)s
), target AS (
    SELECT * FROM observation WHERE snapshot_id = %(target)s
)
SELECT coalesce(t.identity_id, p.identity_id) AS identity_id,
       p.version_id AS previous_version_id, t.version_id AS target_version_id,
       CASE
         WHEN t.version_id IS NULL AND p.stable THEN 'not_observed_in_target'
         WHEN t.version_id IS NULL THEN 'unlinked_predecessor'
         WHEN NOT t.stable THEN 'unlinked_observation'
         WHEN p.version_id IS NULL THEN 'newly_observed'
         WHEN p.version_id = t.version_id THEN 'unchanged_version'
         ELSE 'changed_version'
       END AS category
FROM previous p FULL JOIN target t ON t.identity_id = p.identity_id
WHERE (SELECT count(*) FROM selected) = 2
ORDER BY identity_id
