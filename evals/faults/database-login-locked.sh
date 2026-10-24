#!/bin/bash
# A service runs but cannot use its database: its own database login is locked and its open
# connections are dropped (processor-database-access-lost.sh, for any service).
#
#   evals/faults/database-login-locked.sh start ledger_user     # the ledger: its consumer stalls (KafkaConsumerLag, consumer_stalled)
#   evals/faults/database-login-locked.sh stop ledger_user
#
# Postgres stays up and no other service's login is touched.
source "$(dirname "$0")/lib.sh"
ROLE="${2:-}"
case "$ROLE" in ledger_user|notification_user|webhook_user|merchant_user|recon_user|payout_user|processor_user) ;;
  *) echo "usage: $(basename "$0") start|stop <a service's database role, such as ledger_user>" >&2; exit 2 ;;
esac

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
