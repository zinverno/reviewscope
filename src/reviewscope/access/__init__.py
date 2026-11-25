"""Local PostgreSQL account and workspace boundary."""

PRIVATE_BINDS_SAFE = """SELECT current_setting('log_parameter_max_length_on_error')='0'
        AND (current_setting('log_parameter_max_length')='0' OR (
          current_setting('log_statement')='none' AND current_setting('log_min_duration_statement')='-1'
          AND current_setting('log_min_duration_sample')='-1'
          AND current_setting('log_transaction_sample_rate')::float=0))
        AND coalesce(current_setting('pgaudit.log_parameter',true),'off')='off' AS safe"""
