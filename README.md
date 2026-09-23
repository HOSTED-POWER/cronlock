# cronlock

Run one copy of a command at a time using a short, renewable Redis lease. This
fork continues to accept `cronlock command [arguments...]` and the historical
`CRONLOCK_*` configuration file. It replaces the original 2012 timestamp lock,
which could suppress cron for `CRONLOCK_RELEASE` (48 hours in TurboStack) after
the worker rebooted.

The original project is [kvz/cronlock](https://github.com/kvz/cronlock), by
Kevin van Zonneveld and contributors. This fork remains MIT-licensed.

## Requirements

- Python 3.9 or newer; no third-party Python packages or `redis-cli` are needed.
- A reachable Redis server. Redis Sentinel and Redis Cluster redirects are
  supported through the existing settings.
- Bash only when loading an existing shell-style `cronlock.conf`. Treat that
  file as trusted executable configuration and keep it writable only by an
  administrator.
- Linux `ip` (iproute2) only when `CRONLOCK_LOCAL_VIP` is set.

Use a reviewed release of this fork for deployment. Do not fetch a floating
`master` branch into production; that can replace a tested executable during
an unrelated provisioning run.

## Basic use

```sh
CRONLOCK_HOST=127.0.0.1 cronlock /usr/bin/php /srv/shop/bin/magento cron:run
```

Two invocations with the same command and arguments derive the same lock key.
Set `CRONLOCK_KEY` explicitly if the command arguments differ between hosts or
if several commands must share one lock. A contender exits with code `200`
without starting the command. A successful owner passes through the command's
exit code.

The key format is deliberately unchanged from the original tool: MD5 of the
space-joined argument list plus a final newline, prefixed with
`CRONLOCK_PREFIX`. This avoids a duplicate-run window during a rolling upgrade.
An old numeric lock with no Redis TTL is given a TTL until its original
timestamp and is reclaimed atomically after that deadline. An old lock whose
timestamp is still 48 hours in the future cannot safely be discarded early;
inspect its owner before any manual removal.

## Optional VIP owner

Set `CRONLOCK_LOCAL_VIP` in `/etc/cronlock.conf` on both peers, or only for the
specific cron entry. When it is unset, no VIP check takes place. When set, a
non-owner skips before contacting Redis; the owner runs the job under the
normal Redis lease. The VIP is rechecked while the command runs, and a loss of
ownership terminates its process group.

```sh
# /etc/cronlock.conf on both members of one HASET cluster
CRONLOCK_HOST="10.100.30.31"
CRONLOCK_PORT=6378
CRONLOCK_LOCAL_VIP="10.100.30.20"
```

```cron
* * * * * cronlock /usr/bin/php /var/www/prod/magento2/current/bin/magento cron:run >> /var/www/prod/magento2/shared/var/log/crontab.log 2>&1
```

The VIP mode makes a Pacemaker-owned address choose the *starting node*. It is
not a distributed Magento lock provider. Detached application workers may
outlive the parent command or leave its process group, so a planned VIP handoff
still needs an application drain. If the VIP is absent everywhere, no node
starts new work. Monitor both the scheduler and the application's job backlog.

Do not set a global `CRONLOCK_LOCAL_VIP` on a host that also has unrelated
cronlock jobs unless those jobs should follow the same VIP. Per-job settings
can be supplied with `CRONLOCK_CONFIG=/path/to/job.conf`.

## Crash-safe lease

Acquisition uses Redis `SET key token NX PX lease_ms`. The token is random and
the Redis key has a TTL immediately. While the command runs, cronlock renews
the TTL only if it still owns the token. On normal completion it atomically
checks ownership and either deletes its key or retains it for the remaining
`CRONLOCK_GRACE` period. A killed owner cannot leave a key without a TTL; its
successor can try again after at most the remaining lease time.

The lease defaults to 90 seconds and renews roughly every 30 seconds. Use a
lease of at least 30 seconds in production so network retries and process
shutdown have time to complete before another worker can acquire the key. A Redis
outage or loss of token ownership stops the managed process rather than letting
it run indefinitely without a lock. `CRONLOCK_RELEASE` is deprecated and is
**not** the crash-recovery deadline in this version. Shortening an unrenewed
release timer is not a safe replacement for renewal: a long-running job could
then overlap its successor.

These are cooperative leases, not a promise of exactly-once execution. Redis
asynchronous failover can lose a recently written key; a process killed with
`SIGKILL` can leave its child running briefly; and a program may create
detached work outside the managed process group. Duplicate-sensitive jobs
still need application-level idempotency or stronger fencing/coordination.

## Configuration

Configuration is loaded from `CRONLOCK_CONFIG`, or from `cronlock.conf` beside
the executable, or from `/etc/cronlock.conf`, in that order. As in the original
tool, assignments in a config file override same-named environment variables.

| Setting | Purpose | Default |
| --- | --- | --- |
| `CRONLOCK_HOST`, `CRONLOCK_PORT`, `CRONLOCK_DB` | Redis connection | `localhost`, `6379`, `0` |
| `CRONLOCK_AUTH`, `CRONLOCK_USER` | Redis password and optional ACL user | unset |
| `CRONLOCK_KEY`, `CRONLOCK_PREFIX` | Shared lock identity | command hash, `cronlock.` |
| `CRONLOCK_LEASE` | Renewable lease seconds | `90` |
| `CRONLOCK_GRACE` | Minimum interval since acquisition before next owner | `40` seconds |
| `CRONLOCK_LOCAL_VIP` | Optional IP that must be local to run | unset |
| `CRONLOCK_TIMEOUT` | Maximum command run time; `0` disables | `0` |
| `CRONLOCK_REDIS_TIMEOUT` | Socket timeout seconds | `5` |
| `CRONLOCK_RECONNECT_ATTEMPTS`, `CRONLOCK_RECONNECT_BACKOFF` | Redis retry count and step seconds | `5`, `1` |
| `CRONLOCK_USE_SENTINEL`, `CRONLOCK_SENTINEL_MASTER`, `CRONLOCK_SENTINEL_HOST`, `CRONLOCK_SENTINEL_PORT` | Optional Sentinel lookup | `no`, `mymaster`, `localhost`, `26379` |
| `CRONLOCK_SENTINEL_AUTH` | Optional Sentinel password | unset |
| `CRONLOCK_VERBOSE` | Diagnostic messages | `no` |

`CRONLOCK_NTPDATE` is no longer used: Redis TTLs do not depend on synchronized
client clocks. `CRONLOCK_RESET=yes` now fails instead of unconditionally
deleting another live owner's key. An administrator can inspect and remove a
known-stale key through Redis after verifying no owner remains.

Exit codes: `200` means skipped (VIP absent or lock held); `201` means
configuration, Redis, or lease-safety failure; `202` means command timeout.
Otherwise the wrapped command's exit code is returned. As in the original
tool, child exit codes in the reserved 200–202 range are ambiguous.

## Testing

`make test` or `./test` starts a disposable Redis 7 container when Docker is
available. To use an existing disposable Redis instance, set
`CRONLOCK_TEST_PORT` (and optionally `CRONLOCK_TEST_HOST`) first. Never point
the tests at a production Redis instance.
