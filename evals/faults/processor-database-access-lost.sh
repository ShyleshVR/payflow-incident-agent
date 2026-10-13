#!/bin/bash
# The card processor is running but cannot use its database: its own database login is locked
# and its open connections are dropped. Every request it handles then fails.
# Expected diagnosis: processor_dependency_down.
#
#   evals/faults/processor-database-access-lost.sh start
#   evals/faults/processor-database-access-lost.sh stop
#
# Postgres itself stays up, and no other service's login is touched: PayFlow's services share
# one Postgres instance, so stopping it would break all of them at once.
source "$(dirname "$0")/lib.sh"
ROLE=processor_user

case "${1:-}" in
  start)
    sql postgres "ALTER ROLE $ROLE NOLOGIN" > /dev/null
    dropped=$(sql postgres "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity WHERE usename = '$ROLE'")
    echo "$(now) $ROLE can no longer log in; $dropped connection(s) dropped" ;;
  stop)
    sql postgres "ALTER ROLE $ROLE LOGIN" > /dev/null
    echo "$(now) $ROLE can log in again: $(sql postgres "SELECT rolcanlogin FROM pg_roles WHERE rolname = '$ROLE'")" ;;
  *) usage ;;
esac
