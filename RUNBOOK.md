# Theo — Runbook

## Running instances

| Instance | Port | Branch | Directory | Data |
|---|---|---|---|---|
| Production | 5111 | `stable` | `Theo-stable/` | `Theo-stable/instance/` |
| Dev | 5333 | `main` | `Theo/` | `Theo/instance/` |

---

## Adding a new instance

### 1. Create a branch
```bash
cd /Users/sindhus/Desktop/ss_life/Theo
git checkout -b <branch-name>
git push -u origin <branch-name>
git checkout main
```

### 2. Create a git worktree
```bash
git worktree add ../Theo-<name> <branch-name>
```

### 3. Symlink the venv
```bash
ln -s /Users/sindhus/Desktop/ss_life/Theo/venv \
      /Users/sindhus/Desktop/ss_life/Theo-<name>/venv
```

### 4. Create an instance folder
```bash
# Fresh data (clean slate):
mkdir -p /Users/sindhus/Desktop/ss_life/Theo-<name>/instance/images

# Shared data (same DB as another instance):
# Set INSTANCE_PATH in the runit script instead — see step 5.
```

### 5. Create a runit service
```bash
mkdir -p /opt/homebrew/var/service/theo-<name>/log
```

Write `/opt/homebrew/var/service/theo-<name>/run`:

```sh
#!/bin/sh
set -e
cd /Users/sindhus/Desktop/ss_life/Theo-<name>
. venv/bin/activate
exec 2>&1
exec env PORT=<port> python3 app.py
```

To share data with another instance, add `INSTANCE_PATH`:
```sh
exec env PORT=<port> \
  INSTANCE_PATH=/Users/sindhus/Desktop/ss_life/Theo/instance \
  python3 app.py
```

```bash
chmod +x /opt/homebrew/var/service/theo-<name>/run
sv start theo-<name>
```

---

## Release workflow

When `main` is ready to release to production:

```bash
cd /Users/sindhus/Desktop/ss_life/Theo-stable
git merge main
git push
sv restart theo
```

---

## Common commands

```bash
sv start theo          # start production
sv stop theo           # stop production
sv restart theo        # restart production

sv start theo-dev      # start dev
sv stop theo-dev       # stop dev
sv restart theo-dev    # restart dev

tail -f /opt/homebrew/var/log/runit.log   # view logs
```

---

## Resetting an instance

Stop the service, delete its instance folder, then restart (Flask will recreate the database on boot):

```bash
sv stop theo-<name>
rm -rf /Users/sindhus/Desktop/ss_life/Theo-<name>/instance
mkdir -p /Users/sindhus/Desktop/ss_life/Theo-<name>/instance/images
sv start theo-<name>
```
