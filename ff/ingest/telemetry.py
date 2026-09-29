"""Synthetic telemetry: log lines for every component around every planned incident.

The real LogHub logs cover five components for the last 72 hours only. For the rest of the six
months (and for the components LogHub does not have) this module writes log lines the way those
systems log in production, so the SAME detector (``ff/ingest/log_events.py``) derives the
ANOMALY and ERROR_SIGNATURE records from them:

* ``api-gateway``: ALB / API Gateway access lines (method, route, status, latency, target);
* ``auth-svc`` / ``image-svc``: Go structured JSON (zap-style);
* ``billing-svc``: Spring Boot logback lines; ``notification-svc``: Lambda Python logs;
* ``meta-db`` / ``billing-db``: PostgreSQL server logs; ``session-cache``: Redis server logs;
  ``usage-queue``: CloudWatch metric / alarm lines; ``object-store``: S3 server access logs;
* ``cloud-api`` / ``compute-svc`` / ``storage-svc`` / ``coord-svc`` / ``node-svc``: normal traffic is
  REAL LogHub lines, resampled verbatim (content untouched) with their WARN noise capped, plus
  fault lines in the same style.

For an incident: 6 h of normal traffic, then the scenario's fault signature (:data:`FAULTS`)
from the cause component up the dependency path to the alerting component (callers see
timeouts / refused connections / 5xx / 401 / 429 ... of the component below them), until the
mitigation time, then 1.5 h of recovery. Quiet windows (normal traffic only) give every component
a baseline, and a few harmless blips (a noisy client, a vacuum) keep the detector honest.

Every line carries ``source='synthetic'``; nothing here edits a real log line.
"""

from __future__ import annotations

import hashlib
import random
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from ff import config
from ff.ingest import repo

H = timedelta(hours=1)
M = timedelta(minutes=1)
D = timedelta(days=1)

NORMAL_PER_HOUR = 24  # one line every 2.5 minutes per component
PRE_HOURS, POST_HOURS = 6.0, 1.5
WINDOW_MINUTES = 10.0
NOISE_SHARE = 0.03  # WARN noise kept in resampled real traffic

# --------------------------------------------------------------------------- normal traffic
ROUTES = (("GET", "/v1/servers", "cloud-api"), ("POST", "/v1/servers", "cloud-api"), ("GET", "/v1/jobs", "cloud-api"),
          ("POST", "/v1/jobs", "cloud-api"), ("GET", "/v1/images", "cloud-api"), ("POST", "/v1/token", "auth-svc"),
          ("POST", "/v1/token/refresh", "auth-svc"), ("GET", "/v1/invoices", "billing-svc"),
          ("GET", "/v1/usage", "billing-svc"))
NORMAL: dict[str, tuple[tuple[str, str], ...]] = {
    "auth-svc": (("INFO", '{"level":"info","msg":"token validated","account":"{acct}","latency_ms":{ms}}'),
                 ("INFO", '{"level":"info","msg":"session refreshed","account":"{acct}","ttl_s":3600}'),
                 ("INFO", '{"level":"info","msg":"login succeeded","account":"{acct}","method":"password"}'),
                 ("INFO", '{"level":"info","msg":"login succeeded","account":"{acct}","method":"saml"}')),
    "image-svc": (("INFO", '{"level":"info","msg":"image served","image":"{img}","bytes":{bytes},"cache":"hit"}'),
                  ("INFO", '{"level":"info","msg":"image served","image":"{img}","bytes":{bytes},"cache":"miss",'
                           '"s3_ms":{ms}}'),
                  ("INFO", '{"level":"info","msg":"manifest listed","images":{n}}')),
    "billing-svc": (("INFO", "c.f.b.consumer.UsageEventConsumer - processed usage event account={acct} hours={n}"),
                    ("INFO", "c.f.b.invoice.InvoiceService - invoice {inv} generated lines={n} in {ms} ms"),
                    ("INFO", "c.f.b.api.UsageController - GET /v1/usage account={acct} {ms} ms")),
    "notification-svc": (("INFO", "[INFO] invoice e-mail sent account={acct} message_id=ses-{hex}"),
                         ("INFO", "[INFO] webhook delivered endpoint=hooks.example.com/{hex} status=200 in {ms} ms"),
                         ("INFO", "[INFO] REPORT Duration: {ms}.12 ms Billed Duration: {ms} ms Memory Used: 88 MB")),
    "meta-db": (("INFO", "LOG:  checkpoint complete: wrote {n} buffers; write={ms}.1 s, sync=0.0{n} s"),
                ("INFO", 'LOG:  automatic vacuum of table "meta.public.jobs": index scans: 1, pages: {n} removed'),
                ("INFO", "LOG:  duration: {ms}.4 ms  statement: SELECT * FROM servers WHERE tenant_id = $1")),
    "billing-db": (("INFO", "LOG:  checkpoint complete: wrote {n} buffers; write={ms}.1 s, sync=0.0{n} s"),
                   ("INFO", 'LOG:  automatic vacuum of table "billing.public.invoice_lines": index scans: 1'),
                   ("INFO", "LOG:  duration: {ms}.2 ms  statement: INSERT INTO invoice_lines VALUES ($1, $2, $3)")),
    "session-cache": (("INFO", "* Background saving started by pid {n}"), ("INFO", "* DB saved on disk"),
                      ("INFO", "* {n} changes in 60 seconds. Saving...")),
    "usage-queue": (("INFO", "metric usage-queue NumberOfMessagesSent={n} ApproximateNumberOfMessagesVisible={n} "
                             "ApproximateAgeOfOldestMessage={ms}s"),
                    ("INFO", "metric usage-queue-dlq ApproximateNumberOfMessagesVisible=0")),
    "object-store": (("INFO", "REST.GET.OBJECT images/base/{img}.qcow2 200 - {bytes} {ms}ms"),
                     ("INFO", "REST.PUT.OBJECT artifacts/{job}/output.tar.gz 200 - {bytes} {ms}ms"),
                     ("INFO", "REST.HEAD.OBJECT images/base/{img}.qcow2 200 - 0 {ms}ms")),
}
NOISE: dict[str, tuple[str, str]] = {
    "auth-svc": ("WARN", '{"level":"warn","msg":"login failed: bad password","account":"{acct}"}'),
    "image-svc": ("WARN", '{"level":"warn","msg":"slow s3 read","image":"{img}","s3_ms":{ms}00}'),
    "billing-svc": ("WARN", "c.f.b.api.UsageController - account {acct} requested usage for a closed period"),
    "notification-svc": ("WARN", "[WARNING] webhook endpoint hooks.example.com/{hex} answered 410 Gone, disabling"),
    "meta-db": ("WARN", "LOG:  checkpoints are occurring too frequently ({n} seconds apart)"),
    "billing-db": ("WARN", "LOG:  checkpoints are occurring too frequently ({n} seconds apart)"),
    "session-cache": ("WARN", "# WARNING: slow command HGETALL took {ms} ms"),
    "usage-queue": ("WARN", "metric usage-queue ApproximateAgeOfOldestMessage={n}0s"),
    "object-store": ("WARN", "REST.GET.OBJECT images/base/{img}.qcow2 404 NoSuchKey 0 {ms}ms"),
}

# --------------------------------------------------------------------------- how callers report a failing callee
STYLE = {"auth-svc": "go", "image-svc": "go", "billing-svc": "spring", "notification-svc": "lambda",
         "cloud-api": "nova", "compute-svc": "hadoop", "storage-svc": "hadoop", "coord-svc": "hadoop",
         "node-svc": "bgl", "api-gateway": "alb"}
PHRASE: dict[str, dict[str, str]] = {  # flavor -> generic error phrase per style
    "go": {"timeout": "context deadline exceeded", "refused": "dial tcp {ip}:443: connect: connection refused",
           "5xx": "unexpected status 503 Service Unavailable", "auth": "unexpected status 401 Unauthorized",
           "throttle": "unexpected status 429 Too Many Requests", "notfound": "unexpected status 404 Not Found",
           "data": "unexpected status 500 Internal Server Error"},
    "java": {"timeout": "java.net.SocketTimeoutException: Read timed out",
             "refused": "java.net.ConnectException: Connection refused", "5xx": "HTTP 503 Service Unavailable",
             "auth": "HTTP 401 Unauthorized", "throttle": "HTTP 429 Too Many Requests", "notfound": "HTTP 404 Not Found",
             "data": "HTTP 500 Internal Server Error"},
    "python": {"timeout": "ReadTimeout: Read timed out. (read timeout=2.0)",
               "refused": "ConnectionError: [Errno 111] Connection refused",
               "5xx": "HTTPError: 503 Server Error: Service Unavailable", "auth": "HTTPError: 401 Client Error",
               "throttle": "HTTPError: 429 Client Error: Too Many Requests", "notfound": "HTTPError: 404 Not Found",
               "data": "HTTPError: 500 Server Error"},
    "c": {"timeout": "ETIMEDOUT", "refused": "ECONNREFUSED", "5xx": "EIO", "auth": "EACCES", "throttle": "EAGAIN",
          "notfound": "ENOENT", "data": "EPROTO"},
}
PG_PHRASE = {"timeout": "canceling statement due to statement timeout", "refused": "FATAL:  sorry, too many clients "
             "already", "5xx": "server closed the connection unexpectedly", "auth": 'FATAL:  password authentication '
             'failed for user "{user}"', "throttle": "FATAL:  remaining connection slots are reserved",
             "notfound": 'relation does not exist', "data": "duplicate key value violates unique constraint"}
REDIS_PHRASE = {"timeout": "dial tcp {ip}:6379: i/o timeout", "refused": "dial tcp {ip}:6379: connect: connection "
                "refused", "5xx": "OOM command not allowed when used memory > 'maxmemory'.", "auth": "redis: nil",
                "throttle": "redis: connection pool timeout", "notfound": "redis: nil", "data": "WRONGTYPE Operation"}
S3_PHRASE = {"timeout": "operation error S3: GetObject, context deadline exceeded", "refused": "operation error S3: "
             "GetObject, dial tcp: connection refused", "5xx": "api error SlowDown: Please reduce your request rate.",
             "auth": "api error AccessDenied: Access Denied", "throttle": "api error SlowDown: Please reduce your "
             "request rate.", "notfound": "api error NoSuchKey: The specified key does not exist.",
             "data": "api error InvalidRange: The requested range is not satisfiable"}
STATUS = {"timeout": (504, "ERROR"), "refused": (502, "ERROR"), "5xx": (503, "ERROR"), "auth": (401, "WARN"),
          "throttle": (429, "WARN"), "notfound": (404, "WARN"), "data": (500, "ERROR")}

# --------------------------------------------------------------------------- fault signatures
# scenario -> (flavor callers see, {component or 'cause' or 'cause:<lang>' or 'db' -> [(level, template)]})
# 'real:<text>' picks REAL LogHub error lines of that component containing <text> (fallback: the template after '|').
FAULTS: dict[str, tuple[str, dict[str, list[tuple[str, str]]]]] = {
    "NULL_DEREF": ("5xx", {
        "cause:java": [("ERROR", 'java.lang.NullPointerException: Cannot invoke "Account.resolve()" because the return '
                                 'value of "Event.getAccount()" is null')],
        "cause:python": [("ERROR", "AttributeError: 'NoneType' object has no attribute 'owner'")],
        "cause:go": [("ERROR", '{"level":"error","msg":"panic recovered","err":"runtime error: invalid memory address '
                               'or nil pointer dereference"}')],
        "cause:c": [("FATAL", "segfault at 0000000000000008 ip {hex} in ciod[{n}]")]}),
    "N_PLUS_ONE": ("timeout", {
        "cause": [("WARN", "slow request: {ms}00 ms, {n} SQL queries")],
        "db": [("WARN", "LOG:  duration: {n}{ms}.4 ms  statement: SELECT * FROM {table} WHERE id = $1"),
               ("ERROR", "ERROR:  canceling statement due to statement timeout")]}),
    "MEMORY_LEAK": ("5xx", {
        "cause:go": [("WARN", '{"level":"warn","msg":"memory usage high","rss_mb":{n}0}'),
                     ("ERROR", "fatal error: runtime: out of memory"),
                     ("ERROR", "ECS task stopped: OutOfMemoryError: Container killed due to memory usage")],
        "cause:java": [("ERROR", "java.lang.OutOfMemoryError: Java heap space"),
                       ("ERROR", "ECS task stopped: OutOfMemoryError: Container killed due to memory usage")],
        "cause:python": [("ERROR", "MemoryError in worker pid {n}"),
                         ("ERROR", "Worker (pid:{n}) was sent SIGKILL! Perhaps out of memory?")]}),
    "CONTRACT_BREAK": ("data", {
        "billing-svc": [("ERROR", "c.f.b.consumer.UsageEventConsumer - failed to parse usage event: "
                                  'UnrecognizedPropertyException: Unrecognized field "flavor" (class UsageEvent)'),
                        ("WARN", "c.f.b.consumer.UsageEventConsumer - message {hex} returned to queue (receive {n})")],
        "usage-queue": [("ERROR", "ALARM usage-queue ApproximateAgeOfOldestMessage={n}00s > 600s"),
                        ("ERROR", "ALARM usage-queue-dlq ApproximateNumberOfMessagesVisible={n} > 0")]}),
    "DEPENDENCY_UPGRADE": ("5xx", {
        "billing-svc": [("WARN", "c.z.h.pool.PoolBase - HikariPool-1 - Failed to validate connection "
                                 "org.postgresql.jdbc.PgConnection@{hex} (This connection has been closed.)"),
                        ("ERROR", "c.f.b.repo.InvoiceRepository - HikariPool-1 - Connection is not available, request "
                                  "timed out after 30000ms.")],
        "notification-svc": [("ERROR", "[ERROR] SSLError: urllib3 v2 only supports OpenSSL 1.1.1+, currently the 'ssl' "
                                       "module is compiled with 'OpenSSL 1.0.2k-fips'")],
        "auth-svc": [("ERROR", '{"level":"error","msg":"session lookup failed","err":"ERR unknown subcommand '
                               "'TRACKING'\"}")],
        "image-svc": [("ERROR", '{"level":"error","msg":"s3 GetObject failed","err":"api error AccessDenied: '
                                'x-amz-checksum-mode not allowed by bucket policy"}')],
        "api-gateway": [("WARN", "authorizer: ImmatureSignatureError: The token is not yet valid (iat)")]}),
    "HOTFIX_REGRESSION": ("timeout", {
        "cause": [("WARN", "request waited {ms}00 ms for the global lock"),
                  ("ERROR", "request timed out after 30000 ms (lock contention)")]}),
    "MISSING_INDEX": ("timeout", {
        "db": [("WARN", "LOG:  duration: {n}{ms}.4 ms  statement: SELECT * FROM {table} WHERE account_id = $1"),
               ("ERROR", "ERROR:  canceling statement due to statement timeout")],
        "cause": [("ERROR", "query timed out after 30000 ms: canceling statement due to statement timeout")]}),
    "CACHE_TTL": ("timeout", {
        "auth-svc": [("WARN", '{"level":"warn","msg":"session cache miss","account":"{acct}","ttl_s":36}'),
                     ("ERROR", '{"level":"error","msg":"account lookup failed","err":"timeout: context deadline '
                               'exceeded"}')],
        "meta-db": [("WARN", "LOG:  duration: {ms}{n}.0 ms  statement: SELECT * FROM sessions WHERE token = $1"),
                    ("ERROR", "FATAL:  remaining connection slots are reserved for non-replication superuser "
                              "connections")]}),
    "CONSUMER_CONCURRENCY": ("5xx", {
        "billing-db": [("ERROR", "FATAL:  remaining connection slots are reserved for non-replication superuser "
                                 "connections"), ("ERROR", "FATAL:  sorry, too many clients already")],
        "billing-svc": [("ERROR", "c.f.b.consumer.UsageEventConsumer - HikariPool-1 - Connection is not available, "
                                  "request timed out after 30000ms.")]}),
    "WEBHOOK_RETRY": ("throttle", {
        "notification-svc": [("WARN", "[WARNING] webhook delivery failed endpoint=hooks.acme.example.com attempt={n}/25 "
                                      "status=502"),
                             ("ERROR", "[ERROR] Lambda throttled: Rate Exceeded (reserved concurrency exhausted)")]}),
    "BREAKER_SENSITIVE": ("5xx", {
        "cause": [("ERROR", "circuit breaker OPEN: failure rate 6.{n}% above threshold 5%"),
                  ("WARN", "circuit breaker HALF_OPEN: trial call succeeded")]}),
    "RDS_MAX_CONNECTIONS": ("refused", {
        "cause": [("ERROR", "FATAL:  sorry, too many clients already"),
                  ("ERROR", "FATAL:  remaining connection slots are reserved for non-replication superuser "
                            "connections")]}),
    "RDS_DOWNSIZE": ("timeout", {
        "cause": [("WARN", "LOG:  duration: {n}{ms}.8 ms  statement: SELECT * FROM jobs WHERE state = $1"),
                  ("ERROR", "ERROR:  canceling statement due to statement timeout")]}),
    "RDS_MAINTENANCE": ("refused", {
        "cause": [("ERROR", "FATAL:  terminating connection due to administrator command"),
                  ("WARN", "LOG:  received fast shutdown request"),
                  ("ERROR", "FATAL:  the database system is starting up")]}),
    "SECRET_ROTATION": ("auth", {
        "billing-db": [("ERROR", 'FATAL:  password authentication failed for user "billing_app"')],
        "billing-svc": [("ERROR", "c.f.b.repo.InvoiceRepository - org.postgresql.util.PSQLException: FATAL: password "
                                  'authentication failed for user "billing_app"')]}),
    "REDIS_DOWNSIZE": ("auth", {
        "session-cache": [("WARN", "# WARNING: used_memory is above maxmemory, evicting keys (evicted_keys={n}00)")],
        "auth-svc": [("WARN", '{"level":"warn","msg":"session not found, forcing re-login","account":"{acct}"}')]}),
    "REDIS_NOEVICTION": ("5xx", {
        "session-cache": [("ERROR", "-OOM command not allowed when used memory > 'maxmemory'.")],
        "auth-svc": [("ERROR", '{"level":"error","msg":"session write failed","err":"OOM command not allowed when used '
                               "memory > 'maxmemory'.\"}")]}),
    "REDIS_SG_PORT": ("5xx", {
        "auth-svc": [("ERROR", '{"level":"error","msg":"session lookup failed","err":"dial tcp {ip}:6379: i/o '
                               'timeout"}')]}),
    "SQS_VISIBILITY": ("data", {
        "billing-svc": [("ERROR", "c.f.b.consumer.UsageEventConsumer - duplicate key value violates unique constraint "
                                  '"invoice_lines_usage_event_id_key"'),
                        ("WARN", "c.f.b.consumer.UsageEventConsumer - message {hex} received {n} times")],
        "billing-db": [("ERROR", 'ERROR:  duplicate key value violates unique constraint "invoice_lines_usage_event_id_key"')],
        "usage-queue": [("WARN", "metric usage-queue NumberOfMessagesReceived={n}00 exceeds NumberOfMessagesSent={n}")]}),
    "SQS_REDRIVE": ("data", {
        "usage-queue": [("ERROR", "ALARM usage-queue-dlq ApproximateNumberOfMessagesVisible={n}0 > 0")],
        "billing-svc": [("WARN", "c.f.b.consumer.UsageEventConsumer - transient error on usage event {hex}: "
                                 "SocketTimeoutException, leaving it for redelivery"),
                        ("ERROR", "c.f.b.invoice.InvoiceService - invoice {inv} total does not match usage records")]}),
    "IAM_POLICY": ("notfound", {
        "image-svc": [("ERROR", '{"level":"error","msg":"s3 GetObject failed","image":"{img}","err":"https response '
                                'error StatusCode: 403, api error AccessDenied: Access Denied"}')],
        "object-store": [("WARN", "REST.GET.OBJECT images/base/{img}.qcow2 403 AccessDenied 0 {ms}ms")],
        "cloud-api": [("WARN", "real:Unknown base file|Unknown base file: /var/lib/nova/instances/_base/{hex}")]}),
    "S3_LIFECYCLE": ("notfound", {
        "object-store": [("WARN", "REST.GET.OBJECT images/base/{img}.qcow2 404 NoSuchKey 0 {ms}ms")],
        "image-svc": [("ERROR", '{"level":"error","msg":"image not found in registry","image":"{img}","err":"api error '
                                'NoSuchKey: The specified key does not exist."}')],
        "cloud-api": [("WARN", "real:Unknown base file|Unknown base file: /var/lib/nova/instances/_base/{hex}")]}),
    "APIGW_THROTTLE": ("throttle", {"api-gateway": []}),
    "ALB_IDLE_TIMEOUT": ("timeout", {
        "billing-svc": [("WARN", "c.f.b.api.UsageController - ClientAbortException: java.io.IOException: Broken pipe")]}),
    "LB_HEALTHCHECK": ("refused", {
        "cause": [("WARN", "health check took {n}.{ms} s (target group timeout 2 s)"),
                  ("WARN", "received SIGTERM, draining connections (target deregistered)")]}),
    "ECS_MEMORY": ("5xx", {
        "cause": [("ERROR", "ECS task stopped: OutOfMemoryError: Container killed due to memory usage"),
                  ("WARN", "ECS service started 1 task (replacing a stopped task)")]}),
    "ASG_CAPACITY": ("timeout", {
        "node-svc": [("WARN", "autoscaling: launch of a new instance deferred: desired capacity {n} exceeds max_size 6")],
        "compute-svc": [("WARN", "Container request pending for {n}00 s: insufficient cluster capacity")]}),
    "LAMBDA_CONCURRENCY": ("throttle", {
        "notification-svc": [("ERROR", "[ERROR] Lambda throttled: Rate Exceeded (TooManyRequestsException)")]}),
    "TRAFFIC_SPIKE": ("throttle", {"api-gateway": []}),
    "CERT_EXPIRY": ("data", {
        "notification-svc": [("ERROR", "[ERROR] webhook delivery failed endpoint=hooks.acme.example.com: SSLError "
                                       "certificate verify failed: certificate has expired")]}),
    "AWS_S3_DEGRADED": ("5xx", {
        "object-store": [("ERROR", "REST.GET.OBJECT images/base/{img}.qcow2 503 SlowDown 0 {ms}00ms")],
        "image-svc": [("ERROR", '{"level":"error","msg":"s3 GetObject failed","err":"api error SlowDown: Please reduce '
                                'your request rate."}')]}),
    "SES_THROTTLED": ("throttle", {
        "notification-svc": [("ERROR", "[ERROR] ses.send_email failed: Throttling: Maximum sending rate exceeded.")]}),
    # original platform scenarios
    "RETRY_STORM": ("timeout", {
        "compute-svc": [("ERROR", "real:CONTACTING RM|ERROR IN CONTACTING RM."),
                        ("WARN", "Retrying connect to server: resourcemanager:8030. Already tried {n} time(s)")]}),
    "ELECTION_PORT": ("refused", {
        "coord-svc": [("WARN", "real:election address|Cannot open channel to {n} at election address /{ip}:3888"),
                      ("WARN", "real:Connection broken|Connection broken for id {n}, my id = 1, error =")]}),
    "CROSS_SERVICE": ("5xx", {
        "storage-svc": [("WARN", "real:exception while serving|{ip}:50010:Got exception while serving blk_{id} to /{ip}:")],
        "coord-svc": [("WARN", "Expiring session 0x{hex}, timeout of 400ms exceeded")],
        "image-svc": [("ERROR", '{"level":"error","msg":"layer read failed","err":"api error InvalidRange: The '
                                'requested range is not satisfiable"}')],
        "notification-svc": [("ERROR", "[ERROR] jinja2.exceptions.TemplateNotFound: receipt_v2.html")],
        "billing-svc": [("WARN", "c.f.b.api.UsageController - GET /v1/usage took {n}{ms}00 ms (rows={n}000)")],
        "auth-svc": [("WARN", '{"level":"warn","msg":"token expired","account":"{acct}","age_s":{n}}')]}),
    "IMAGE_CACHE_SYNC": ("timeout", {
        "cloud-api": [("WARN", "real:Unknown base file|Unknown base file: /var/lib/nova/instances/_base/{hex}")]}),
    "DB_MIGRATION": ("timeout", {
        "db": [("WARN", "LOG:  process {n} still waiting for AccessExclusiveLock on relation {table} after 1000.0 ms"),
               ("ERROR", "ERROR:  canceling statement due to lock timeout")]}),
    "EBS_THROUGHPUT": ("timeout", {
        "storage-svc": [("WARN", "real:exception while serving|{ip}:50010:Got exception while serving blk_{id} to /{ip}:"),
                        ("WARN", "Slow BlockReceiver write data to disk cost:{n}{ms}ms (threshold=300ms)")]}),
    "DB_POOL": ("timeout", {
        "cause": [("ERROR", "connection pool exhausted: timed out after 30000 ms waiting for a connection")]}),
    "CIOD_LEAK": ("5xx", {
        "node-svc": [("FATAL", "real:ciod: Error loading|ciod: Error loading /{hex}: invalid or missing program image, "
                               "Cannot allocate memory")]}),
    "TIMEOUT_UNIT": ("timeout", {"cause": [("ERROR", "outbound call timed out after 2 ms (configured 2000)")]}),
    "FLAG_DORMANT": ("5xx", {"cause": [("ERROR", "flag-guarded path failed: {n} requests rejected in the last minute")]}),
    "CONFIG_CHANGE": ("timeout", {"cause": [("ERROR", "outbound call failed after {n} attempts")]}),
    "LATENT_DISK": ("5xx", {"cause": [("ERROR", "write failed: No space left on device")]}),
    "MEMORY_INFRA": ("5xx", {"cause": [("ERROR", "out of memory: killed process {n}")],
                             "node-svc": [("FATAL", "real:data TLB error|data TLB error interrupt")]}),
    "MEMORY_HEAP": ("5xx", {"cause": [("ERROR", "java.lang.OutOfMemoryError: Java heap space")]}),
    "BAD_DEPLOY": ("5xx", {"cause": [("ERROR", "unhandled error in request handler: {n} failures in 60 s")]}),
    "SKIPPED_TEST": ("timeout", {"cause": [("WARN", "request took {n}{ms}0 ms (batch size 1)")]}),
    "PARTIAL_ROLLBACK": ("5xx", {"cause": [("ERROR", "unhandled error in request handler: {n} failures in 60 s")]}),
    "DEPENDENCY_DEPLOY": ("5xx", {"cause": [("ERROR", "unhandled error in request handler: {n} failures in 60 s")]}),
    "NO_CHANGE": ("5xx", {"cause": [("WARN", "elevated error rate without a recent change")]}),
}
TABLES = {"meta-db": ("servers", "jobs", "accounts", "flavors"), "billing-db": ("invoice_lines", "invoices", "prices")}

_PH = re.compile(r"\{(acct|ms|n|hex|ip|id|inv|job|img|bytes|user|table)\}")


def _lang(comp: str) -> str:
    return config.COMPONENTS[comp].lang or "managed"


def _style_lang(comp: str) -> str:
    return {"go": "go", "spring": "java", "hadoop": "java", "lambda": "python", "nova": "python", "bgl": "c"}.get(
        STYLE.get(comp, ""), "java")


@dataclass
class IncidentPlanView:
    """What telemetry needs from a planned incident."""

    key: str
    scenario: str
    cause: str | None
    alert: str
    start: datetime
    mitigated: datetime
    extra: tuple[str, ...] = ()


class Telemetry:
    """Generates synthetic lines (seeded)."""

    def __init__(self, seed: int, real: dict[str, pd.DataFrame] | None = None) -> None:
        self.rng = random.Random(seed)
        self.rows: list[dict[str, Any]] = []
        self.real = real or {}
        self.pools = {svc: self._pool(df) for svc, df in self.real.items()}
        self.real_errors = {svc: df[df["is_error"]] for svc, df in self.real.items()}

    # ------------------------------------------------------------------ helpers
    def _pool(self, df: pd.DataFrame) -> pd.DataFrame:
        """Normal traffic to resample: the real INFO lines plus a little of the real WARN noise."""
        info = df[~df["is_error"]]
        noise = df[df["is_error"]]
        k = int(len(info) * NOISE_SHARE)
        if len(noise) and k:
            info = pd.concat([info, noise.sample(n=min(k, len(noise)), random_state=self.rng.randint(0, 10**6))])
        return info.reset_index(drop=True)

    def fill(self, tpl: str, **fixed: str) -> str:
        r = self.rng

        def rep(m: re.Match[str]) -> str:
            k = m.group(1)
            if k in fixed:
                return fixed[k]
            return {"acct": f"acct-{r.randint(1000, 9999)}", "ms": str(r.randint(2, 95)), "n": str(r.randint(2, 60)),
                    "hex": f"{r.getrandbits(32):08x}", "ip": f"10.0.{r.randint(1, 40)}.{r.randint(2, 250)}",
                    "id": str(r.randint(10**15, 10**16)), "inv": f"INV-{r.randint(100000, 999999)}",
                    "job": f"job-{r.randint(10000, 99999)}", "img": f"img-{r.getrandbits(24):06x}",
                    "bytes": str(r.randint(10**6, 9 * 10**8)), "user": "billing_app",
                    "table": "jobs"}[k]
        return _PH.sub(rep, tpl)

    def add(self, comp: str, t: datetime, level: str, content: str, template: str, status: float | None = None,
            latency: float | None = None, component: str = "") -> None:
        self.rows.append({"ts": t, "service": comp, "level": level, "component": component or comp,
                          "content": content, "event_template": template,
                          "event_id": "S" + hashlib.sha1(template.encode()).hexdigest()[:8],
                          "http_status": status, "latency_s": latency})

    # ------------------------------------------------------------------ normal traffic
    def normal(self, comp: str, t: datetime) -> None:
        r = self.rng
        if comp == "api-gateway":
            m, path, target = r.choice(ROUTES)
            status = r.choice((200, 200, 200, 200, 201, 204, 304)) if r.random() > 0.02 else r.choice((404, 401))
            lat = round(r.lognormvariate(-2.4, 0.5), 3)
            self.add(comp, t, "INFO", f"{m} {path} {status} {lat:.3f}s target={target}",
                     f"{m} {path} <*> <*> target={target}", status, lat)
            return
        if comp in self.pools and len(self.pools[comp]):
            row = self.pools[comp].iloc[r.randrange(len(self.pools[comp]))]
            lat = row["latency_s"] if pd.notna(row.get("latency_s")) else None
            st = row["http_status"] if pd.notna(row.get("http_status")) else None
            self.rows.append({"ts": t, "service": comp, "level": row["level"], "component": row["component"],
                              "content": row["content"], "event_template": row["event_template"],
                              "event_id": row["event_id"], "http_status": st, "latency_s": lat})
            return
        tpls = NORMAL.get(comp)
        if not tpls:
            return
        if comp in NOISE and r.random() < NOISE_SHARE:
            level, tpl = NOISE[comp]
        else:
            level, tpl = r.choice(tpls)
        self.add(comp, t, level, self.fill(tpl), tpl)

    def quiet(self, comp: str, t0: datetime, t1: datetime) -> None:
        t = t0
        while t < t1:
            self.normal(comp, t)
            t += self.rng.expovariate(NORMAL_PER_HOUR) * H

    # ------------------------------------------------------------------ faults
    def caller_line(self, comp: str, callee: str, flavor: str, t: datetime) -> None:
        """How ``comp`` reports that ``callee`` fails with ``flavor``."""
        r = self.rng
        kind = config.COMPONENTS[callee].kind
        if comp == "api-gateway":
            st, level = STATUS[flavor]
            route = [x for x in ROUTES if x[2] == callee] or [(r.choice(("GET", "POST")), "/v1/servers", callee)]
            m, path, target = r.choice(route)
            lat = round(r.uniform(5, 29), 3) if flavor == "timeout" else round(r.lognormvariate(-2.0, 0.6), 3)
            self.add(comp, t, level, f"{m} {path} {st} {lat:.3f}s target={target}", f"{m} {path} <*> <*> target={target}",
                     st, lat)
            return
        lang = _style_lang(comp)
        if kind == "datastore":
            err = PG_PHRASE[flavor]
        elif kind == "cache":
            err = REDIS_PHRASE[flavor]
        elif kind == "objectstore":
            err = S3_PHRASE[flavor]
        elif kind == "queue":
            err = {"python": "botocore.exceptions.ClientError: An error occurred (ThrottlingException) when calling "
                             "the SendMessage operation", "java": "SqsException: Rate exceeded"}.get(lang, "sqs error")
        else:
            err = PHRASE[lang][flavor]
        style = STYLE.get(comp, "")
        if style == "go":
            what = {"datastore": "db query failed", "cache": "session lookup failed",
                    "objectstore": "s3 GetObject failed"}.get(kind, f"call to {callee} failed")
            tpl = '{"level":"error","msg":"' + what + '","err":"' + err + '","latency_ms":{ms}}'
        elif style == "spring":
            logger = {"datastore": "c.f.b.repo.InvoiceRepository", "queue": "c.f.b.consumer.UsageEventConsumer"}.get(
                kind, f"c.f.b.client.{callee.split('-')[0].title()}Client")
            tpl = f"{logger} - call to {callee} failed: {err}"
        elif style == "lambda":
            tpl = f"[ERROR] call to {callee} failed: {err}"
        elif style == "nova":
            st, _ = STATUS[flavor]
            lat = round(r.uniform(5, 30), 7) if flavor == "timeout" else round(r.uniform(0.2, 2.0), 7)
            content = (f'10.11.10.1 "GET /v2/{r.getrandbits(128):032x}/servers/detail HTTP/1.1" status: {st} len: '
                       f"{r.randint(200, 2000)} time: {lat}")
            self.add(comp, t, "ERROR" if st >= 500 else "WARN", content, "<*> \"GET <*> HTTP/1.1\" status: <*> len: <*> "
                     "time: <*>", st, lat, component="nova.osapi_compute.wsgi.server")
            tpl = f"Unexpected exception while calling {callee}: {err}"
        elif style == "bgl":
            tpl = f"{callee} request failed: {err}"
        else:  # hadoop-style Java without a logger prefix
            tpl = f"Call to {callee} failed: {err}"
        level = "WARN" if flavor in ("auth", "notfound", "throttle") and style != "nova" else "ERROR"
        self.add(comp, t, level, self.fill(tpl), tpl)

    def fault_line(self, comp: str, tpls: Sequence[tuple[str, str]], t: datetime, table: str | None = None) -> None:
        level, tpl = self.rng.choice(tpls)
        if tpl.startswith("real:"):
            key, _, fallback = tpl[5:].partition("|")
            errs = self.real_errors.get(comp)
            hit = errs[errs["content"].str.contains(key, regex=False)] if errs is not None and len(errs) else None
            if hit is not None and len(hit):
                row = hit.iloc[self.rng.randrange(len(hit))]
                self.rows.append({"ts": t, "service": comp, "level": row["level"], "component": row["component"],
                                  "content": row["content"], "event_template": row["event_template"],
                                  "event_id": row["event_id"], "http_status": None, "latency_s": None})
                return
            tpl = fallback
        fixed = {"table": table} if table else {}
        self.add(comp, t, level, self.fill(tpl, **fixed), tpl)

    def incident(self, p: IncidentPlanView) -> None:
        """Normal traffic around one incident plus its fault signature on every involved component."""
        r = self.rng
        flavor, spec = FAULTS.get(p.scenario, FAULTS["BAD_DEPLOY"])
        cause = p.cause or p.alert
        path = config.dependency_path(p.alert, cause) or [p.alert]
        involved = list(dict.fromkeys(path + list(p.extra)))
        db = repo.DB_OF.get(cause)
        if "db" in spec and db:
            involved.append(db)
        for k in spec:
            if k in config.COMPONENTS and k not in involved:
                involved.append(k)
        from ff.ingest.scenarios import blast_radius

        if "api-gateway" in blast_radius(cause) and "api-gateway" not in involved:
            involved.append("api-gateway")
        t0, t1 = p.start - PRE_HOURS * H, p.mitigated + POST_HOURS * H
        for comp in involved:
            self.quiet(comp, t0, t1)
        # onset: the cause first, the alerting component exactly at the planned start, callers above it later
        onset = {c: p.start + r.uniform(1, 4) * M for c in involved}
        onset[p.alert] = p.start
        if cause in onset and cause != p.alert:
            onset[cause] = p.start - r.uniform(0, 6) * M
        rate = {c: r.uniform(24, 48) for c in involved}
        rate[p.alert] = r.uniform(40, 70)
        for comp in involved:
            lines: list[tuple[str, str]] = []
            if comp == cause:
                lines = spec.get(f"cause:{_lang(comp) if _lang(comp) != 'managed' else ''}", []) or spec.get("cause", [])
            lines = spec.get(comp, []) or lines
            if comp == db and "db" in spec:
                lines = spec["db"]
            callee = None
            if comp in path and comp != cause:
                callee = path[path.index(comp) + 1]
            elif comp == "api-gateway":
                callee = next((c for c in path if c in config.dependencies("api-gateway")), path[0])
            t = onset[comp]
            table = r.choice(TABLES.get(comp, ("jobs",)))
            own = 0.85 if lines and callee is not None and config.COMPONENTS[callee].kind == "queue" else 0.6
            while t < p.mitigated:
                if lines and (callee is None or r.random() < own or config.COMPONENTS[callee].kind == "queue"):
                    self.fault_line(comp, lines, t, table)
                elif callee is not None:
                    self.caller_line(comp, callee, flavor, t)
                elif comp == "api-gateway":  # the edge is the cause (throttling, traffic spike)
                    m, path_, target = r.choice(ROUTES)
                    st, level = STATUS[flavor]
                    lat = round(r.lognormvariate(-1.5, 0.7), 3)
                    self.add(comp, t, level, f"{m} {path_} {st} {lat:.3f}s target={target}",
                             f"{m} {path_} <*> <*> target={target}", st, lat)
                t += r.expovariate(rate[comp]) * H

    def blip(self, comp: str, t: datetime) -> None:
        """A harmless 20-40 minute burst of a known warning (a noisy client, a vacuum)."""
        end = t + self.rng.uniform(20, 40) * M
        self.quiet(comp, t - 3 * H, end + H)
        level, tpl = NOISE.get(comp, ("WARN", "retrying request (attempt {n})"))
        while t < end:
            if comp == "api-gateway":
                m, path, target = self.rng.choice(ROUTES)
                self.add(comp, t, "WARN", f"{m} {path} 401 0.004s target={target}", f"{m} {path} <*> <*> target={target}",
                         401, 0.004)
            else:
                self.add(comp, t, level, self.fill(tpl), tpl)
            t += self.rng.expovariate(40) * H

    # ------------------------------------------------------------------ output
    def frame(self) -> pd.DataFrame:
        cols = ["ts", "service", "level", "component", "content", "event_template", "event_id", "http_status",
                "latency_s"]
        df = pd.DataFrame(self.rows, columns=cols)
        if df.empty:
            return df
        df["ts"] = pd.to_datetime(df["ts"], utc=True)
        df = df.sort_values(["service", "ts"], kind="stable").reset_index(drop=True)
        df["original_ts"] = df["ts"]
        df["line_id"] = df.groupby("service").cumcount() + 1
        df["label"] = None
        df["is_error"] = ~df["level"].isin(config.INFO_LEVELS)
        df["system"] = "synthetic"
        df["source"] = "synthetic"
        df["http_status"] = pd.to_numeric(df["http_status"], errors="coerce")
        df["latency_s"] = pd.to_numeric(df["latency_s"], errors="coerce")
        return df


def generate(plans: Sequence[IncidentPlanView], seed: int, real: dict[str, pd.DataFrame] | None = None,
             blips: int = 12, quiet_every_days: float = 14.0) -> pd.DataFrame:
    """All synthetic lines: incident windows, quiet baseline windows, harmless blips."""
    tel = Telemetry(seed, real)
    rng = tel.rng
    for p in plans:
        tel.incident(p)
    busy = [(p.start - PRE_HOURS * H, p.mitigated + POST_HOURS * H) for p in plans]
    end = config.LOG_START - 12 * H

    def free(t0: datetime, t1: datetime) -> bool:
        return all(t1 < a or t0 > b for a, b in busy)

    for comp in config.SERVICES:
        t = config.DAY0 + rng.uniform(1, quiet_every_days) * D
        while t < end:
            if free(t, t + 6 * H):
                tel.quiet(comp, t, t + 6 * H)
                busy.append((t, t + 6 * H))
            t += rng.uniform(0.7, 1.3) * quiet_every_days * D
    for _ in range(blips):
        for _try in range(20):
            comp = rng.choice(config.SERVICES)
            t = config.DAY0 + rng.uniform(2, max(3.0, (end - config.DAY0) / D - 1)) * D
            if free(t - 3 * H, t + 2 * H):
                tel.blip(comp, t)
                busy.append((t - 3 * H, t + 2 * H))
                break
    return tel.frame()
