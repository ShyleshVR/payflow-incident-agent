---
alert: GatewayErrorRate
based_on: PayFlow docs/DEPLOYMENT.md, section "Runbooks", GatewayErrorRate; the alert rule (over 5% of gateway responses 5xx for 5 minutes)
causes:
  upstream_down: A service behind the gateway has no ready pod, so the gateway answers 502 or 503 for its routes.
  upstream_failing: A service behind the gateway runs but answers its own requests with 5xx.
  gateway_failing: The gateway itself fails (its pods crash or it cannot validate tokens), for every route.
shown_by:
  upstream_down: [payment_service_pods, merchant_service_pods, payout_service_pods, ledger_pods, gateway_errors]
  upstream_failing: [payment_service_health, merchant_service_health, payout_service_health]
  gateway_failing: [gateway_pods, gateway_errors]
requires_clear:
  gateway_failing: [payment_service_pods]
proposals:
  upstream_down: >
    Bring the service with no ready pod back (see ServiceDown); the gateway routes to it as soon as
    a pod is ready.
  upstream_failing: >
    Look at why the failing service answers 5xx (its log, its database, what it calls); the gateway
    only passes its answers on.
  gateway_failing: >
    Look at the gateway's own pods and log (and merchant-service, which signs the tokens it checks).
checks:
  gateway_health:
    tool: get_service_health
    arguments: {service: api-gateway}
    tells: The gateway's request rate and its share of 5xx answers.
  gateway_pods:
    tool: get_pod_status
    arguments: {workload: api-gateway}
    tells: Whether the gateway's pods are running and ready.
  gateway_errors:
    tool: get_recent_errors
    arguments: {workload: api-gateway, minutes: 15}
    tells: What the gateway logged, including the service it could not reach for a route.
  payment_service_pods:
    tool: get_pod_status
    arguments: {workload: payment-service}
    tells: Whether payment-service, behind /api/v1/payments, has ready pods.
  payment_service_health:
    tool: get_service_health
    arguments: {service: payment-service}
    tells: How payment-service answers its own requests (its share of 5xx).
  merchant_service_pods:
    tool: get_pod_status
    arguments: {workload: merchant-service}
    tells: Whether merchant-service, behind /oauth2/token and /api/v1/merchants, has ready pods.
  merchant_service_health:
    tool: get_service_health
    arguments: {service: merchant-service}
    tells: How merchant-service answers its own requests.
  payout_service_pods:
    tool: get_pod_status
    arguments: {workload: payout-service}
    tells: Whether payout-service, behind /api/v1/payouts, has ready pods.
  payout_service_health:
    tool: get_service_health
    arguments: {service: payout-service}
    tells: How payout-service answers its own requests.
  ledger_pods:
    tool: get_pod_status
    arguments: {workload: ledger-service}
    tells: Whether the ledger, behind /api/v1/ledger, has ready pods.
---
# GatewayErrorRate

## What it means

Every request from merchants and operators enters PayFlow through the gateway, which routes it to
a service. The alert fires when over 5% of the gateway's answers have been 5xx for five minutes.
The 5xx are usually not the gateway's own: it answers 502 or 503 when the service behind a route
has no ready pod, and passes on a service's own 5xx.

## Telling the causes apart

- **A service down**: one service has no ready pod (`ServiceDown` usually fires too); the gateway's
  log names the route it could not reach.
- **A service failing**: its pods are fine but its own share of 5xx is high.
- **The gateway itself**: its pods crash or are not ready, or its log shows it failing for every route.

## What resolves it

The failing service (or the gateway) healthy again. The alert looks back five minutes, so it
resolves a few minutes after the errors stop.
