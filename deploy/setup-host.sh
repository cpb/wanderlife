#!/bin/sh
# setup-host.sh -- provision a host for git-push deployment of wander.
#
# Run ON the host as the deploy user, e.g. straight from your workstation:
#
#     ssh pi.local 'sh -s' < deploy/setup-host.sh
#
# Installs:
#   1. ~/wander.git (bare repo) + the post-receive deploy hook
#   2. /etc/systemd/system/wander.service (rendered for this user/home)
#   3. /etc/sudoers.d/wander (NOPASSWD for exactly `systemctl restart wander`)
#
# Steps 2-3 need sudo and are skipped with a note on non-systemd hosts.
# The heredoc content mirrors deploy/post-receive,
# deploy/wander.service.template and deploy/sudoers-wander.template --
# keep them in sync.
set -eu

U=$(id -un)
H=$HOME
GITDIR="$H/wander.git"

echo "== wander host setup (user: $U, home: $H)"

# --- 1. git + bare repo ---------------------------------------------------
if ! command -v git >/dev/null 2>&1; then
    if [ -x "$H/opt/git/root/usr/bin/git" ]; then
        # Rootless git unpacked from the distro .deb (see README).
        PATH="$H/opt/git/root/usr/bin:$PATH"
        export GIT_EXEC_PATH="$H/opt/git/root/usr/lib/git-core"
    else
        echo "!! git not found. See README 'Deployment' for the rootless-git"
        echo "!! bootstrap (apt-get download git + dpkg -x into ~/opt/git)."
        exit 1
    fi
fi
if [ ! -d "$GITDIR" ]; then
    git init --bare "$GITDIR"
    echo "++ created bare repo $GITDIR"
else
    echo "-- $GITDIR already exists, keeping it"
fi

# --- 2. post-receive deploy hook -------------------------------------------
cat > "$GITDIR/hooks/post-receive" <<'HOOK'
#!/bin/sh
# post-receive deploy hook for wander (installed at ~/wander.git/hooks/post-receive)
# - checks out main into $HOME
# - restarts the wander systemd service when game code changed
#   (via the NOPASSWD sudoers rule in /etc/sudoers.d/wander)
export PATH="$HOME/opt/git/root/usr/bin:$PATH"
export GIT_EXEC_PATH="$HOME/opt/git/root/usr/lib/git-core"
GITDIR="$HOME/wander.git"
SYSTEMCTL="/usr/bin/systemctl"

cd "$HOME"
while read oldrev newrev refname; do
    if [ "$refname" = "refs/heads/main" ]; then
        echo "post-receive: deploying main -> $HOME"
        git --git-dir="$GITDIR" --work-tree="$HOME" checkout -f main
        echo "post-receive: now at $(git --git-dir="$GITDIR" log --oneline -1 main)"

        if git --git-dir="$GITDIR" diff --name-only "$oldrev" "$newrev" \
                | grep -qE 'wander(_game|_server|_client)?\.py$'; then
            echo "post-receive: game code changed, restarting wander service"
            if sudo -n "$SYSTEMCTL" restart wander 2>/dev/null; then
                echo "post-receive: wander service restarted"
            else
                echo "post-receive: WARNING could not restart wander;"
                echo "post-receive: run 'sudo systemctl restart wander' manually"
            fi
        fi
    fi
done
HOOK
chmod +x "$GITDIR/hooks/post-receive"
echo "++ installed post-receive deploy hook"

# --- 3. systemd unit + sudoers (Linux only) --------------------------------
if command -v systemctl >/dev/null 2>&1 && [ -d /etc/systemd/system ]; then
    sed -e "s|__WANDER_USER__|$U|g" -e "s|__WANDER_HOME__|$H|g" <<'SERVICE' | sudo tee /etc/systemd/system/wander.service >/dev/null
[Unit]
Description=Wander Game of Life server
After=network.target

[Service]
Type=simple
User=__WANDER_USER__
Group=__WANDER_USER__
WorkingDirectory=__WANDER_HOME__
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 __WANDER_HOME__/wander_server.py
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
SERVICE
    sudo systemctl daemon-reload
    sudo systemctl enable --now wander
    echo "++ systemd service 'wander' enabled and started"

    sed "s|__WANDER_USER__|$U|g" <<'SUDOERS' | sudo tee /etc/sudoers.d/wander >/dev/null
# Allow the deploy user to restart (and only restart) the wander game
# service without a password, so the git post-receive deploy hook can
# bounce it. Installed by deploy/setup-host.sh.
__WANDER_USER__ ALL=(root) NOPASSWD: /usr/bin/systemctl restart wander
SUDOERS
    sudo chmod 0440 /etc/sudoers.d/wander
    if command -v visudo >/dev/null 2>&1; then
        sudo visudo -cf /etc/sudoers.d/wander >/dev/null
        echo "++ sudoers rule installed and validated (restart wander only)"
    fi
else
    echo "-- no systemd on this host; skipping service + sudoers."
    echo "   Run the server with wanderctl or your init system instead."
fi

echo
echo "== done. On your workstation:"
echo "   git remote add deploy $U@<this-host>:wander.git   # or set-url"
echo "   git push deploy main"
