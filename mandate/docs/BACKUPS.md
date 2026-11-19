# Backups

The bundled Postgres is dumped every night, encrypted with age, and uploaded to Cloud
Storage. The private key stays with you. The server only has the public key, so a copy of
the disk is not enough to read a dump.

The scripts live in `deploy/backup/`. They pipe `pg_dump -Fc` straight into `age`. A failed
run does not leave a dump file behind.

## Make a key on your laptop

Do this on your own machine, not on the VM.

```bash
sudo apt install age          # macOS: brew install age
age-keygen -o mandate-backup.key
chmod 600 mandate-backup.key
```

`mandate-backup.key` is the private key. The public key is the `age1...` value in the
comment at the top of that file (`# public key: age1...`).

## Put only the public key on the server

Copy the `age1...` value into `/etc/mandate/backup.env` (see `deploy/backup/backup.env.example`):

```
BACKUP_BUCKET=
AGE_RECIPIENT=
```

`BACKUP_BUCKET` is the bucket name without `gs://`. `AGE_RECIPIENT` may instead be a path to
a file with one `age1` key per line. `chmod 600` the env file.

You can keep that same public value in Secret Manager and render it into the env file. Do not
put the private key in Secret Manager, on the VM, or in git.

`gcloud storage` uses the VM's service account. Do not copy a JSON key onto the box. That
account needs to create objects in the backup bucket and nothing else.

## Keep the private key offline

Keep `mandate-backup.key` in your password manager, and keep a second copy offline (a USB
drive that stays unplugged). It is not on the VM, not in git, and not in email.

`restore.sh` reads the key from `--identity` and does not copy it. It refuses a path inside
the repo or under `/etc/mandate`. Delete the file from the server when the restore is finished
(`shred -u`, or `rm` if shred is not installed).

## If the private key is lost

Dumps encrypted with that public key cannot be opened. Generate a new key pair, put the new
public key in `AGE_RECIPIENT`, and take a fresh backup. The old objects stay in the bucket and
stay unreadable. That gap is why the offline copy exists.

## Install the nightly timer

On the VM:

```bash
sudo apt-get install -y age
sudo install -d -m 700 /etc/mandate
sudo install -m 600 deploy/backup/backup.env.example /etc/mandate/backup.env
# fill in BACKUP_BUCKET and AGE_RECIPIENT, then:
sudo cp deploy/backup/mandate-backup.service /etc/systemd/system/
sudo cp deploy/backup/mandate-backup.timer /etc/systemd/system/
```

Edit the service if the checkout is not `/srv/mandate/app/mandate`. Both `WorkingDirectory`
and `ExecStart` have to point at this repo's `mandate/` directory. The timer uses the VM
clock, so set that clock to America/Toronto or the run will not be at 03:00 Eastern:

```bash
sudo timedatectl set-timezone America/Toronto
sudo systemctl daemon-reload
sudo systemctl enable --now mandate-backup.timer
systemctl list-timers mandate-backup.timer
```

`Persistent=true` runs a missed backup after a reboot. A manual run is
`sudo systemctl start mandate-backup.service`. Logs: `journalctl -u mandate-backup.service`.

Each run writes `pg/daily/mandate-<timestamp>.dump.age`. A Sunday also copies that object to
`pg/weekly/`, and the first of the month also copies it to `pg/monthly/`. The copy is
server-side. The plaintext dump is not written to disk.

## Restore drill

Default is a new database named `mandate_restore_<timestamp>`. That cannot replace the live
`mandate` database. Replacing it needs `--overwrite-production`, which stops the gateway first.

From `mandate/`, with the private key only for the length of the drill:

```bash
deploy/backup/restore.sh --identity /path/to/mandate-backup.key
```

That fetches the latest daily object, decrypts it, restores into the scratch database, runs
`python -m backend.migrate`, and checks `/ready` against that database. Pass `--dump` with a
`gs://` URL or a file name to choose an older object.

When the scratch database looks right, drop it:

```bash
docker compose --env-file .env exec postgres \
  psql -U postgres -d postgres -c "DROP DATABASE mandate_restore_<timestamp>"
```

Then delete the identity file from the server. Write the date and the scratch database name
down with the drill notes.

To replace the live database (only after a scratch restore has already succeeded):

```bash
deploy/backup/restore.sh --identity /path/to/mandate-backup.key --overwrite-production
```

The gateway is stopped, the live database is restored, migrations run, and the gateway is
started again. `/ready` has to be green before you call the drill done.

`deploy/backup/roundtrip.sh` is a local check (dump, encrypt, decrypt, restore, migrate). It
uses its own compose project and will not start if `mandate-postgres-1` is already running.
Do not point it at the server.

## Bucket

Create a private bucket with uniform access and versioning. The name below is a placeholder.

```bash
gcloud storage buckets update gs://PROJECT-mandate-backups --versioning
```

Suggested lifecycle, so a bad run cannot be the only remaining copy and old dumps do not pile
up. 14 days of dailies, 8 weeks, 6 months:

```json
{
  "rule": [
    {"action": {"type": "Delete"}, "condition": {"age": 14, "matchesPrefix": ["pg/daily/"]}},
    {"action": {"type": "Delete"}, "condition": {"age": 56, "matchesPrefix": ["pg/weekly/"]}},
    {"action": {"type": "Delete"}, "condition": {"age": 184, "matchesPrefix": ["pg/monthly/"]}},
    {"action": {"type": "Delete"}, "condition": {"daysSinceNoncurrentTime": 7, "numNewerVersions": 1}}
  ]
}
```

```bash
gcloud storage buckets update gs://PROJECT-mandate-backups --lifecycle-file=lifecycle.json
```

Versioning keeps an overwritten object for a week (`numNewerVersions` leaves the live one in
place). The bucket stays private. Do not make the `pg/` prefix public.
