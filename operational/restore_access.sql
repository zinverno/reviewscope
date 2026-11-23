-- Stable names instead of catalog OIDs. An independent operator pins the digest on the live installation.
WITH roles AS (
 SELECT oid,rolname FROM pg_roles WHERE rolname IN ('rs_runtime','rs_worker','rs_retention','rs_migration','rs_upload')
), rels AS (
 SELECT c.* FROM pg_class c WHERE c.relnamespace='public'::regnamespace AND c.relkind IN ('r','v','m','S','p','f')
), procs AS (
 SELECT p.*,p.proname || '(' || oidvectortypes(p.proargtypes) || ')' AS signature
 FROM pg_proc p WHERE p.pronamespace='public'::regnamespace
), acls(kind,name,acl) AS (
 SELECT 'relation',relname::text,coalesce(relacl,acldefault(CASE WHEN relkind='S' THEN 's'::"char" ELSE 'r'::"char" END,relowner)) FROM rels
 UNION ALL SELECT 'column',jsonb_build_array(c.relname,a.attname)::text,a.attacl FROM rels c JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped
 UNION ALL SELECT 'function',signature,coalesce(proacl,acldefault('f',proowner)) FROM procs
 UNION ALL SELECT 'schema','public',coalesce(nspacl,acldefault('n',nspowner)) FROM pg_namespace WHERE oid='public'::regnamespace
 UNION ALL SELECT 'database','current',coalesce(datacl,acldefault('d',datdba)) FROM pg_database WHERE datname=current_database()
), entries(value) AS (
 SELECT jsonb_build_array('acl',kind,name,CASE WHEN grantee=0 THEN 'PUBLIC' ELSE pg_get_userbyid(grantee) END,
 pg_get_userbyid(grantor),privilege_type,is_grantable) FROM acls CROSS JOIN LATERAL aclexplode(acl)
 UNION ALL SELECT jsonb_build_array('relation',relname,pg_get_userbyid(relowner),relrowsecurity,relforcerowsecurity) FROM rels
 UNION ALL SELECT jsonb_build_array('function',signature,pg_get_userbyid(proowner),prosecdef,proconfig,prokind,
 encode(sha256(convert_to(pg_get_functiondef(oid),'UTF8')),'hex'))
 FROM procs
 UNION ALL SELECT jsonb_build_array('policy',c.relname,p.polname,p.polcmd,p.polpermissive,
 ARRAY(SELECT CASE WHEN i=0 THEN 'PUBLIC' ELSE pg_get_userbyid(i) END FROM unnest(p.polroles) i ORDER BY 1),
 pg_get_expr(p.polqual,c.oid),pg_get_expr(p.polwithcheck,c.oid)) FROM pg_policy p JOIN rels c ON c.oid=p.polrelid
 UNION ALL SELECT jsonb_build_array('role',r.rolname,r.rolcanlogin,r.rolsuper,r.rolinherit,r.rolcreaterole,r.rolcreatedb,
 r.rolreplication,r.rolbypassrls) FROM pg_roles r JOIN roles ON r.oid=roles.oid
 UNION ALL SELECT jsonb_build_array('membership',pg_get_userbyid(m.roleid),pg_get_userbyid(m.member),m.admin_option,
 m.inherit_option,m.set_option) FROM pg_auth_members m WHERE m.member IN (SELECT oid FROM roles) OR m.roleid IN (SELECT oid FROM roles)
)
SELECT encode(sha256(convert_to(string_agg(value::text,E'\n' ORDER BY value::text COLLATE "C"),'UTF8')),'hex') FROM entries
