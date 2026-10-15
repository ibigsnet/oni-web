# oni-rcon connect: run on the game box (POSIX sh, read-only) to find its Project Reclaimer servers. Passwords never
# come back: they're masked here, and read at each start instead. The terminal console and the desktop app both send it.
D=docker
if command -v docker >/dev/null 2>&1; then
  if ! docker ps -q >/dev/null 2>&1; then
    if sudo -n docker ps -q >/dev/null 2>&1; then D="sudo -n docker"; else echo "@@nodocker"; D=; fi
  fi
else D=; fi
toml() { [ -f "$1/dedicated.toml" ] || return 0; echo "@@toml $1"
  sed -e 's/^\([[:space:]]*\(rcon_\)\{0,1\}password[[:space:]]*=[[:space:]]*\)["'"'"'][^"'"'"'].*/\1"hidden"/' "$1/dedicated.toml"
  echo "@@end"; }
if [ -n "$D" ]; then
  echo "@@docker $D"
  $D ps --format '{{.Names}}' | while IFS= read -r c; do
    case "$($D inspect -f '{{.Config.Image}}' "$c")" in *project-reclaimer-dedicated*) ;; *) continue ;; esac
    echo "@@container $c"
    $D inspect -f '{{range .Mounts}}{{if eq .Destination "/server"}}@@mount {{.Source}}{{println}}{{end}}{{end}}{{range .Config.Env}}{{println .}}{{end}}' "$c" |
      sed -n -e 's/^@@mount /@@mount /p' -e 's/^RECLAIMER_DEDICATED_ONLY=/@@only /p' -e 's/^RECLAIMER_DEDICATED_RCON_ADDRESS=/@@address /p' \
        -e 's/^RECLAIMER_DEDICATED_RCON_PASSWORD=..*/@@envpassword/p'
    m=$($D inspect -f '{{range .Mounts}}{{if eq .Destination "/server"}}{{.Source}}{{end}}{{end}}' "$c")
    [ -n "$m" ] && toml "$m"
  done
fi
find "$HOME" /opt /srv -maxdepth 4 -name dedicated.toml 2>/dev/null | while IFS= read -r f; do toml "${f%/dedicated.toml}"; done
echo "@@done"
