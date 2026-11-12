# Hosted images can make codec resource directories world-writable.
# Restore trusted read-only resources; never weaken the sandbox.
for path in /usr/share/fonts /usr/share/fontconfig \
    /usr/share/libreoffice /etc/libreoffice /usr/share/locale \
    /usr/share/mime /usr/share/zoneinfo /etc/localtime; do
  test -e "$path" || continue
  test "$(stat -Lc '%u' "$path")" = 0
  stat -Lc 'before %n owner=%u mode=%a' "$path"
  sudo chmod go-w "$path"
  stat -Lc 'after %n owner=%u mode=%a' "$path"
done
