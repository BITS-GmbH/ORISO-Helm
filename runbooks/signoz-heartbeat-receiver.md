# External Dev SigNoz heartbeat receiver

Run the receiver on the approved monitoring host outside the Dev cluster and
its hosting provider. Use the runner's approved production WSGI server and
TLS reverse proxy. Expose only `POST /pulse`, limit requests to 16 KiB and set
request timeouts at the proxy. Forward the Basic authorization header to the
receiver over loopback. Keep that header and the body out of access logs.

## Configure the receiver

Run with Python 3.10 or newer. Use a dedicated service identity and persistent
local storage for SQLite. Give that identity a private writable state directory
and an owner-readable password file from the operator's secret store. Linux
uses the kernel boot ID to identify the monotonic clock across processes.
The development probe also supports the macOS boot session ID.

Set these environment variables for the service.

| Variable | Value |
| --- | --- |
| `SIGNOZ_HEARTBEAT_DB` | Absolute path to the database in the persistent state directory |
| `SIGNOZ_HEARTBEAT_CLUSTER` | Exact Dev collector cluster name |
| `SIGNOZ_HEARTBEAT_PASSWORD_FILE` | Path to the file containing the receiver's Basic Auth password |

From the checkout, the Gunicorn factory can be started as follows.

```sh
gunicorn --chdir scripts --bind 127.0.0.1:9785 --timeout 10 \
  'signoz_heartbeat_receiver:from_environment()'
```

Create the dedicated SigNoz webhook channel with the receiver's HTTPS URL,
username `heartbeat`, the matching password and resolved delivery disabled.
Configure TLS verification in SigNoz. The Helm sender is opt-in and requires a
fresh sample from the named cluster collector before provisioning.

The receiver accepts one untruncated firing alert for the exact Dev rule and
cluster. It checks the rule labels and the evaluation timestamp. A repeat can
return HTTP 204 after a lost acknowledgment without extending the deadline.
The receiver commits valid advancing timestamps to SQLite before replying.
A storage failure returns HTTP 503 so the sender can retry. The database holds
the cluster key, evaluation timestamp, boot ID and monotonic receipt time.

## Connect the incident check

Give the monitor identity read access to the same database and set the database
path and cluster name in its environment. Run this local command on the
receiver host. The monitor configuration needs no password file.

```sh
python3 scripts/signoz_heartbeat_receiver.py
```

The command returns one JSON line. Exit 0 means a valid pulse was received
within 600 seconds on the current boot. Exit 2 reports an elapsed deadline,
no pulse, a changed boot ID, a clock error or unavailable state. A process
restart preserves the original deadline. A host restart requires an advancing
pulse before the status returns to OK.

Configure recurring execution and route nonzero results and recovery to the
approved incident destination. The polling interval adds to the 600-second
threshold before delivery. Monitor missed executions from a separate service
so an outage of the receiver host itself can also be detected.

Before enabling incidents, observe native pulses across multiple delivery
intervals. In the approved outage rehearsal, verify one incident after the
deadline, no extension from a replay, and recovery on a new native pulse.
Put the monitor in maintenance before deliberately disabling the sender or
migrating its receiver state.
